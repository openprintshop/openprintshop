"""Кэш результата слайса каталожных моделей + обобщённый фоновый refine (OpenTodo ae038968):

- calc_cache (модель+принтер+профиль+масштаб -> объём/время) читается/пишется в db.py;
- /api/calc отдаёт мгновенную приближённую цену из calc_cache (approx+refining), пока честный
  слайс досчитывает точные цифры в фоне (_refine_calc, обобщённый из бывшего _mw_refine_calc);
- /api/requote (смена пластика без переслайса, 050aff56) теперь тоже помечает approx+refining
  и запускает тот же фоновый честный слайс;
- дефолтный пластик/цвет карточки товара (Elegoo Matte, Белый; фолбэк Тёмно-синий) отдаётся в /api/options.
"""
import asyncio
import json

from conftest import stub_auth

from app import cache, calcservice, config, cpupool, pricing, texts, webapp
from app.web import clientflow


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeGetRequest:
    def __init__(self, query):
        self.query = query


async def _wait_until(pred, tries: int = 100) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.01)


# ---- db.get_calc_cache / put_calc_cache -------------------------------------------------

async def test_calc_cache_roundtrip(tmp_db):
    assert await tmp_db.get_calc_cache("hash1", 1, 2, 3, 100) is None
    await tmp_db.put_calc_cache("hash1", 1, 2, 3, 100, volume_cm3=12.5, minutes=88.0, plates=2)
    cc = await tmp_db.get_calc_cache("hash1", 1, 2, 3, 100)
    assert cc["volume_cm3"] == 12.5 and cc["minutes"] == 88.0 and cc["plates"] == 2

    # другая комбинация (иное качество) — промах, ключ учитывает ВЕСЬ профиль печати
    assert await tmp_db.get_calc_cache("hash1", 1, 99, 3, 100) is None
    assert await tmp_db.get_calc_cache("hash1", 1, 2, 3, 150) is None   # и масштаб тоже

    # upsert перезаписывает старую запись той же комбинации (модель могли поправить)
    await tmp_db.put_calc_cache("hash1", 1, 2, 3, 100, volume_cm3=20.0, minutes=90.0, plates=1)
    cc2 = await tmp_db.get_calc_cache("hash1", 1, 2, 3, 100)
    assert cc2["volume_cm3"] == 20.0 and cc2["plates"] == 1

    # нулевые/отрицательные грамм/время — защита от мусора, не пишутся вовсе
    await tmp_db.put_calc_cache("hash2", 1, 2, 3, 100, volume_cm3=0.0, minutes=10.0)
    assert await tmp_db.get_calc_cache("hash2", 1, 2, 3, 100) is None


# ---- общая обвязка тестов веб-эндпоинтов -------------------------------------------------

async def _std_presets(tmp_db):
    """Принтер/качество/прочность по умолчанию — минимум для _api_calc_inner/_api_requote."""
    printer_id = await tmp_db.add_printer("P", "mp.json", "pd", "fd")
    await tmp_db.set_default_printer(printer_id)
    quality_id = await tmp_db.add_quality("Standard", 0.2, "proc")
    await tmp_db.set_default_quality(quality_id)
    strength_id = await tmp_db.add_strength("Normal", 15, 3)
    await tmp_db.set_default_strength(strength_id)
    printer = await tmp_db.get_default_printer()
    quality = await tmp_db.get_quality(quality_id)
    strength = await tmp_db.get_strength(strength_id)
    return printer, quality, strength


def _fake_compute(tmp_db, computed, *, volume=None, minutes=None):
    """calcservice.compute-заглушка: считает цену штатной формулой по подставленным
    объёму/времени (или по тем, что реально пришли от вызывающего — kw['plastic']/qty),
    запоминает kw для проверки проводки фонового refine."""
    async def compute(**kw):
        computed["kw"] = kw
        settings = await tmp_db.get_settings()
        bd = pricing.compute_price(volume if volume is not None else 9.0,
                                   minutes if minutes is not None else 50.0,
                                   kw["qty"], kw["plastic"], settings,
                                   extra_colors=kw.get("extra_colors", 0))
        return {"bd": bd, "settings": settings, "preview_path": "", "cached": False,
                "pdisp": kw["plastic"].name, "plates": 1, "dims": "",
                "color_grams": [], "plastic_grams": [], "scale_pct": kw.get("scale_pct", 100),
                "scale_clamped": False, "scale_max_pct": None}
    return compute


# ---- /api/calc: мгновенный ответ из calc_cache + фоновый честный слайс -------------------

async def test_api_calc_instant_from_cache_then_refines(tmp_db, tmp_path, monkeypatch):
    uid = 42
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "box.stl"
    stl.write_bytes(b"fake-stl-bytes")
    token = "tok-cache-1"
    webapp._models[token] = {"paths": [str(stl)], "label": "Кубик", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}

    model_hash = cache.model_hash([str(stl)])
    await tmp_db.put_calc_cache(model_hash, printer.id, quality.id, strength.id, 100,
                                volume_cm3=8.0, minutes=45.0, plates=1)

    computed = {}
    monkeypatch.setattr(calcservice, "compute",
                        _fake_compute(tmp_db, computed, volume=8.2, minutes=46.0))
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 2}
    resp = await webapp._api_calc_inner(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    res = j["result"]
    assert res["approx"] is True and res["refining"] is True
    # Цена в ОТВЕТЕ — уже сериализованные байты, посчитанные из calc_cache (8.0 см³/45 мин), а
    # НЕ из честного слайса (8.2/46): сравниваем с замороженным `res`, а не с живым m["last"] —
    # фоновая задача честного слайса могла успеть выполниться на первом же await ПОСЛЕ возврата
    # ответа (event loop планирует её сразу же), так что живое состояние тут гонка, а `res` нет.
    settings = await tmp_db.get_settings()
    expected = pricing.compute_price(8.0, 45.0, 2, plastic, settings)
    assert res["total"] == texts.fmt_money(expected.total, config.CURRENCY)
    assert res["grams_total"] == f"{expected.grams_total:.1f}"

    # фон догоняет: честный слайс запущен и обновляет смету честными цифрами
    await _wait_until(lambda: computed.get("kw") is not None)
    assert computed["kw"]["plastic"].id == plastic.id and computed["kw"]["qty"] == 2

    await _wait_until(lambda: "refining" not in (webapp._models[token].get("last_payload") or {}))
    assert webapp._models[token]["last"]["approx"] is False
    honest = pricing.compute_price(8.2, 46.0, 2, plastic, settings)
    assert abs(webapp._models[token]["last"]["total"] - honest.total) < 1e-6

    # честный слайс сам обновил calc_cache свежими цифрами (OpenTodo ae038968)
    cc = await tmp_db.get_calc_cache(model_hash, printer.id, quality.id, strength.id, 100)
    assert abs(cc["volume_cm3"] - 8.2) < 1e-6 and abs(cc["minutes"] - 46.0) < 1e-6


async def test_api_calc_populates_cache_on_full_slice(tmp_db, tmp_path, monkeypatch):
    """Без данных в calc_cache — обычный слайс, но результат ЗАПОМИНАЕТСЯ для следующего раза."""
    uid = 43
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "box2.stl"
    stl.write_bytes(b"fake-stl-2")
    token = "tok-cache-2"
    webapp._models[token] = {"paths": [str(stl)], "label": "Кубик2", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}

    computed = {}
    monkeypatch.setattr(calcservice, "compute",
                        _fake_compute(tmp_db, computed, volume=12.0, minutes=70.0))
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    model_hash = cache.model_hash([str(stl)])
    assert await tmp_db.get_calc_cache(model_hash, printer.id, quality.id, strength.id, 100) is None

    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
           "strength_id": strength.id, "qty": 1}
    resp = await webapp._api_calc_inner(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert computed.get("kw") is not None   # первый расчёт — честный слайс сразу (кэш пуст)

    cc = await tmp_db.get_calc_cache(model_hash, printer.id, quality.id, strength.id, 100)
    assert cc is not None
    assert abs(cc["volume_cm3"] - 12.0) < 1e-6 and abs(cc["minutes"] - 70.0) < 1e-6


async def test_api_calc_instant_estimate_then_refines_no_cache(tmp_db, tmp_path, monkeypatch):
    """НОВАЯ модель без MW/кэша: мгновенная аналитическая ≈оценка (approx+estimated+refining)
    отдаётся СРАЗУ, честный слайс уточняет цену в ФОНЕ (goal 16.07 — «быстрый расчёт при заходе,
    полноценный слайс потом фоном»)."""
    import trimesh
    uid = 49
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(stl))   # реальный меш → fastcalc оценит
    token = "tok-est-1"
    webapp._models[token] = {"paths": [str(stl)], "label": "Кубик", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}
    model_hash = cache.model_hash([str(stl)])
    assert await tmp_db.get_calc_cache(model_hash, printer.id, quality.id, strength.id, 100) is None

    computed = {}
    monkeypatch.setattr(calcservice, "compute",
                        _fake_compute(tmp_db, computed, volume=8.0, minutes=44.0))
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 1}
    resp = await webapp._api_calc_inner(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    res = j["result"]
    # мгновенная ≈оценка: approx + estimated + refining, непустая цена
    assert res["approx"] is True and res["refining"] is True and res.get("estimated") is True
    assert res["total"]

    # честный слайс догоняет ФОНОМ и уточняет цену — approx снимается
    await _wait_until(lambda: computed.get("kw") is not None)
    assert computed["kw"]["plastic"].id == plastic.id
    await _wait_until(lambda: "refining" not in (webapp._models[token].get("last_payload") or {}))
    assert webapp._models[token]["last"]["approx"] is False   # честный слайс заменил оценку
    honest = pricing.compute_price(8.0, 44.0, 1, plastic, await tmp_db.get_settings())
    assert abs(webapp._models[token]["last"]["total"] - honest.total) < 1e-6


async def test_api_calc_huge_mesh_estimate_without_bg_refine(tmp_db, tmp_path, monkeypatch):
    """Тяжёлый меш (выше _REFINE_MAX_BYTES, koala-класс): отдаём ≈оценку, но фоновый честный
    слайс НЕ гоняем (он бы упёрся в память/коллизию на десятки минут) — refining не выставляем."""
    import trimesh
    uid = 50
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(stl))
    token = "tok-est-huge"
    webapp._models[token] = {"paths": [str(stl)], "label": "Кубик", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}

    computed = {}
    monkeypatch.setattr(calcservice, "compute", _fake_compute(tmp_db, computed))
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    # плоская константа читается из clientflow (волна 15) — подмена через webapp перепривязала бы копию
    monkeypatch.setattr(clientflow, "_REFINE_MAX_BYTES", 1)   # любой реальный файл «тяжёлый»

    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 1}
    resp = await webapp._api_calc_inner(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    res = j["result"]
    assert res["approx"] is True and res.get("estimated") is True
    assert "refining" not in res              # фон не запускался — фронт не поллит
    await asyncio.sleep(0.05)
    assert computed.get("kw") is None         # честный слайс НЕ вызывался


async def test_api_calc_scale_not_100_skips_cache_fast_path(tmp_db, tmp_path, monkeypatch):
    """Масштаб ≠ 100% требует пересборки геометрии — приближённые пути (MW/calc_cache) не
    применяются (та же граница, что у MW-пути, 97a91eee): честный слайс идёт сразу, синхронно."""
    uid = 44
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "box3.stl"
    stl.write_bytes(b"fake-stl-3")
    token = "tok-cache-3"
    webapp._models[token] = {"paths": [str(stl)], "label": "Кубик3", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}
    model_hash = cache.model_hash([str(stl)])
    await tmp_db.put_calc_cache(model_hash, printer.id, quality.id, strength.id, 100,
                                volume_cm3=8.0, minutes=45.0, plates=1)

    computed = {}
    monkeypatch.setattr(calcservice, "compute", _fake_compute(tmp_db, computed))
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
           "strength_id": strength.id, "qty": 1, "scale": 150}
    resp = await webapp._api_calc_inner(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert computed.get("kw") is not None       # честный слайс вызван СИНХРОННО, не из фона
    assert "refining" not in j["result"]        # это не приближённая цена


# ---- /api/requote: пересчёт пластика по плотности + фоновое уточнение --------------------

async def test_api_requote_density_reprice_then_refines(tmp_db, tmp_path, monkeypatch):
    uid = 45
    printer, quality, strength = await _std_presets(tmp_db)
    pa = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pa, colors="Белый")
    pb = await tmp_db.add_plastic("Test PETG", 2000.0, 1.27, "PETG")
    await tmp_db.update_plastic(pb, colors="Чёрный")
    plastic_a = await tmp_db.get_plastic(pa)
    plastic_b = await tmp_db.get_plastic(pb)

    stl = tmp_path / "box4.stl"
    stl.write_bytes(b"fake-stl-4")
    token = "tok-requote-1"
    webapp._models[token] = {
        "paths": [str(stl)], "label": "Кубик4", "uid": uid, "plates": 1,
        "multicolor": False, "num_colors": 1,
        "last": {"volume_cm3_unit": 10.0, "minutes_unit": 60.0, "extra_colors": 0,
                 "quality_id": quality.id, "strength_id": strength.id, "printer": printer.name,
                 "qty": 1, "mixed": False, "plastic_id": plastic_a.id, "colors_sel": [],
                 "scale_pct": 100, "dims": ""},
    }

    computed = {}
    # честный фоновый слайс даёт слегка ДРУГИЕ цифры (10.5/62), чтобы отличить мгновенный ответ
    # (10.0/60 — из прежнего слайса, по плотности) от честного уточнения.
    monkeypatch.setattr(calcservice, "compute",
                        _fake_compute(tmp_db, computed, volume=10.5, minutes=62.0))
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    async def fake_run_render(fn, *a, **kw):
        raise RuntimeError("no render in tests")
    monkeypatch.setattr(cpupool, "run_render", fake_run_render)

    body = {"token": token, "plastic_id": plastic_b.id, "qty": 1}
    resp = await webapp._api_requote(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    res = j["result"]
    assert res["approx"] is True and res.get("refining") is True

    settings = await tmp_db.get_settings()
    # тот же объём, что дал прошлый слайс (10 см³), но плотность НОВОГО пластика — «умножаем на
    # коэффициент» честно. Сравниваем с ЗАМОРОЖЕННЫМ `res` (см. комментарий в тесте calc_cache
    # выше) — фоновый честный слайс мог уже стартовать к этому моменту.
    expected = pricing.compute_price(10.0, 60.0, 1, plastic_b, settings)
    assert res["total"] == texts.fmt_money(expected.total, config.CURRENCY)

    await _wait_until(lambda: computed.get("kw") is not None)
    assert computed["kw"]["plastic"].id == plastic_b.id

    await _wait_until(lambda: "refining" not in (webapp._models[token].get("last_payload") or {}))
    assert webapp._models[token]["last"]["approx"] is False
    honest = pricing.compute_price(10.5, 62.0, 1, plastic_b, settings)
    assert abs(webapp._models[token]["last"]["total"] - honest.total) < 1e-6


async def test_api_requote_unresolvable_preset_stays_approx_without_refine(tmp_db, tmp_path, monkeypatch):
    """Профиль печати прошлого расчёта не резолвится (пресет качества удалили) — остаёмся на
    approx БЕЗ фонового слайса (нечем честно пересчитать), без падения запроса."""
    uid = 46
    printer, _quality, strength = await _std_presets(tmp_db)
    pa = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    pb = await tmp_db.add_plastic("Test PETG", 2000.0, 1.27, "PETG")
    plastic_b = await tmp_db.get_plastic(pb)

    stl = tmp_path / "box5.stl"
    stl.write_bytes(b"fake-stl-5")
    token = "tok-requote-2"
    webapp._models[token] = {
        "paths": [str(stl)], "label": "Кубик5", "uid": uid, "plates": 1,
        "multicolor": False, "num_colors": 1,
        "last": {"volume_cm3_unit": 10.0, "minutes_unit": 60.0, "extra_colors": 0,
                 "quality_id": 999999, "strength_id": strength.id, "printer": printer.name,
                 "qty": 1, "mixed": False, "plastic_id": pa, "colors_sel": [],
                 "scale_pct": 100, "dims": ""},
    }
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    async def fake_run_render(fn, *a, **kw):
        raise RuntimeError("no render in tests")
    monkeypatch.setattr(cpupool, "run_render", fake_run_render)

    body = {"token": token, "plastic_id": plastic_b.id, "qty": 1}
    resp = await webapp._api_requote(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert "refining" not in j["result"]   # фон не запускался — профиль печати не восстановлен


# ---- /api/options: дефолтный пластик/цвет карточки товара ---------------------------------

def test_default_mw_plastic_color_matches_elegoo_matte():
    from app import db
    plastics = [
        db.Plastic(id=1, name="Elegoo PLA Matte", price_per_kg=45, density=1.24, is_active=1,
                  colors="Черный,Белый,Темно-синий,Красный"),
        db.Plastic(id=2, name="Generic PLA", price_per_kg=30, density=1.24, is_active=1,
                  colors="Белый"),
    ]
    # стандарт открытия — БЕЛЫЙ (b2507a91, решение владельца 24.07)
    pid, color = webapp._default_mw_plastic_color(plastics)
    assert pid == 1 and color == "Белый"
    # белого в палитре нет → фолбэк на прежний стандарт Тёмно-синий (ae038968), «ё» не мешает
    plastics[0].colors = "Черный,Тёмно-синий,Красный"
    pid2, color2 = webapp._default_mw_plastic_color(plastics)
    assert pid2 == 1 and color2 == "Тёмно-синий"


def test_default_mw_plastic_color_no_match_returns_none():
    from app import db
    # ни одной Elegoo Matte катушки
    assert webapp._default_mw_plastic_color(
        [db.Plastic(id=1, name="Generic PLA", price_per_kg=30, density=1.24, is_active=1,
                    colors="Белый")]) == (None, None)
    # Elegoo Matte есть, но ни белого, ни тёмно-синего в палитре
    assert webapp._default_mw_plastic_color(
        [db.Plastic(id=2, name="Elegoo PLA Matte", price_per_kg=45, density=1.24, is_active=1,
                    colors="Жёлтый,Красный")]) == (None, None)
    # неактивная катушка не считается вовсе
    assert webapp._default_mw_plastic_color(
        [db.Plastic(id=3, name="Elegoo PLA Matte", price_per_kg=45, density=1.24, is_active=0,
                    colors="Белый")]) == (None, None)


async def test_api_options_exposes_default_plastic(tmp_db, monkeypatch):
    uid = 47
    pid = await tmp_db.add_plastic("Elegoo PLA Matte", 45.0, 1.24, "PLA Matte")
    await tmp_db.update_plastic(pid, colors="Черный,Темно-синий,Белый")
    cheap = await tmp_db.add_plastic("Cheap PLA", 20.0, 1.24, "PLA")
    await tmp_db.update_plastic(cheap, colors="Белый")
    await _std_presets(tmp_db)

    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    resp = await webapp._api_options(_FakeGetRequest({"initData": "x"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert j["default_plastic_id"] == pid
    assert j["default_color"] == "Белый"    # стандарт открытия — белый (b2507a91)


async def test_api_options_default_plastic_none_when_no_elegoo_matte(tmp_db, monkeypatch):
    uid = 48
    await tmp_db.add_plastic("Cheap PLA", 20.0, 1.24, "PLA")
    await _std_presets(tmp_db)
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    resp = await webapp._api_options(_FakeGetRequest({"initData": "x"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert j["default_plastic_id"] is None and j["default_color"] is None
