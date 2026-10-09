"""Заказ ПРЯМО во время слайса (OpenTodo f1e80d67): «Заказ должен появляться мгновенно, а не
после слайса модели».

- /api/order_now создаёт заявку СРАЗУ из живого выбора клиента (принтер/пластик/качество/
  прочность/тираж/цвет), не дожидаясь нарезки: price=0, unsliced=1, статус new, файл модели на
  месте; токен помнит id заявки (m["pending_order_id"]); клиент и мастер уведомлены;
- когда идущий фоном /api/calc досчитывается, _finalize_pending_order проставляет реальную
  цену/граммы/время + план резерва склада, снимает флаг unsliced и уведомляет обоих;
- если на токене уже есть готовый расчёт (m["last"]) — order_now сразу оформляет ПОЛНЫЙ заказ
  с ценой (priced=True), без промежуточного «без сметы».
"""
import json

from conftest import stub_auth, stub_bot

from app import cache, calcservice, webapp
from app.web import clientflow


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeMsg:
    def __init__(self, mid):
        self.message_id = mid


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", chat_id, text, kw.get("reply_markup")))
        return _FakeMsg(len(self.sent))

    async def send_photo(self, chat_id, photo, caption="", **kw):
        self.sent.append(("photo", chat_id, caption, kw.get("reply_markup")))
        return _FakeMsg(len(self.sent))


async def _std_presets(tmp_db):
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


def _last(plastic, printer, quality, strength, *, total=12.5, grams=40.0, minutes=90.0, color="Белый"):
    """Снимок успешного расчёта m["last"] (минимум ключей, что читают _place_one/финализатор)."""
    return {"plastic_name": f"{plastic.name} · {color}", "printer": printer.name, "qty": 2,
            "quality": quality.label, "strength": strength.label, "dims": "40×30×20 мм",
            "grams_total": grams, "minutes_total": minutes, "total": total,
            "plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
            "color": color, "colors_sel": [], "color_grams": [], "plastic_grams": [],
            "scale_pct": 100, "approx": False}


async def test_order_now_creates_pending_instantly(tmp_db, tmp_path, monkeypatch):
    """Слайс ещё идёт (m["last"] нет) → заявка появляется СРАЗУ: price=0, unsliced=1, выбор клиента."""
    uid = 200
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "m.stl"
    stl.write_bytes(b"fake-stl")
    token = "tok-now-1"
    webapp._models[token] = {"paths": [str(stl)], "label": "Слайсится сейчас", "uid": uid,
                             "plates": 1, "multicolor": False, "num_colors": 1}

    stub_auth(monkeypatch,
              _verify_user=lambda s: {"id": uid, "username": "tester", "first_name": "Т"})
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 4, "color": "Белый", "comment": "к пятнице"}
    resp = await webapp._api_order_now(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and j["priced"] is False and j["order_id"] > 0

    # токен помнит заявку — чтобы фоновый расчёт потом проставил цену
    assert webapp._models[token]["pending_order_id"] == j["order_id"]

    rows = await tmp_db.list_user_requests(uid)
    assert len(rows) == 1
    r = rows[0]
    assert r.unsliced == 1 and r.price == 0 and r.status == "new"
    assert r.plastic_name == "Test PLA · Белый" and r.quantity == 4
    assert r.printer == printer.name and r.quality == quality.label and r.strength == strength.label
    assert r.file_path == str(stl) and r.hash == cache.model_hash([str(stl)])
    assert "во время расчёта" in r.description and "к пятнице" in r.description

    # мгновенно уведомлены и клиент, и мастер
    assert any(s[1] == uid and "принят" in s[2].lower() for s in bot.sent)
    assert any(s[1] == 999 for s in bot.sent)


async def test_finalize_pending_order_fills_price(tmp_db, tmp_path, monkeypatch):
    """Фоновый расчёт досчитался → цена/граммы/время проставлены, unsliced снят, оба уведомлены."""
    uid = 201
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "m2.stl"
    stl.write_bytes(b"fake-stl-2")
    rid = await tmp_db.add_request(
        user_id=uid, user_name="tester", model_label="Слайсится", printer=printer.name,
        plastic_name="Test PLA · Белый", quantity=2, quality=quality.label, strength=strength.label,
        grams_total=0, minutes_total=0, price=0, preview_path=None,
        hash=cache.model_hash([str(stl)]), file_path=str(stl),
        quality_id=quality.id, strength_id=strength.id, unsliced=1,
        price_pending=1)   # Э0: заказ order_now ждёт цену (живой хвост применяет только такой)
    # Э0: заказ ждёт цену от ПК — клиенту «Цена считается», а не «Оценит мастер»
    assert webapp._order_json(await tmp_db.get_request(rid))["price"] == "Цена считается"

    m = {"paths": [str(stl)], "label": "Слайсится", "uid": uid, "plates": 1,
         "multicolor": False, "num_colors": 1,
         "last": _last(plastic, printer, quality, strength, total=18.9, grams=55.0, minutes=140.0),
         "pdf": {}}

    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    await webapp._finalize_pending_order(rid, m, uid)

    r = await tmp_db.get_request(rid)
    assert r.unsliced == 0
    assert abs(r.price - 18.9) < 1e-6
    assert abs(r.grams_total - 55.0) < 1e-6 and abs(r.minutes_total - 140.0) < 1e-6
    assert webapp._order_json(r)["price"] != "Оценит мастер"

    assert any(s[1] == uid and "посчитан" in s[2] for s in bot.sent)
    assert any(s[1] == 999 and "досчитан" in s[2] for s in bot.sent)


async def test_finalize_keeps_base_preview_when_no_colored(tmp_db, tmp_path, monkeypatch):
    """Флекси-осьминог (OpenTodo c6d9cebc): заказ во время расчёта уже создан с СЕРЫМ превью
    (render_preview), а цветного (cpreview) к финалу не появилось. Финализатор НЕ должен затирать
    показанную клиенту картинку пустотой — иначе «заказ без картинки»."""
    uid = 220
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "octo.stl"
    stl.write_bytes(b"flexi-octopus")
    grey = tmp_path / "octo_grey.jpg"          # серое превью уже сохранено при order_now
    grey.write_bytes(b"jpeg-bytes")
    rid = await tmp_db.add_request(
        user_id=uid, user_name="tester", model_label="Флекси осьминог", printer=printer.name,
        plastic_name="Test PLA · Белый", quantity=1, quality=quality.label, strength=strength.label,
        grams_total=0, minutes_total=0, price=0, preview_path=str(grey),
        hash=cache.model_hash([str(stl)]), file_path=str(stl),
        quality_id=quality.id, strength_id=strength.id, unsliced=1,
        price_pending=1)   # Э0: заказ order_now ждёт цену (живой хвост применяет только такой)

    # у токена цветного превью нет, зато есть серое базовое
    m = {"paths": [str(stl)], "label": "Флекси осьминог", "uid": uid, "plates": 1,
         "multicolor": False, "num_colors": 1, "cpreview": None, "preview": str(grey),
         "last": _last(plastic, printer, quality, strength, total=9.0, grams=30.0, minutes=80.0),
         "pdf": {}}

    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    stub_bot(monkeypatch, None)

    await webapp._finalize_pending_order(rid, m, uid)

    r = await tmp_db.get_request(rid)
    assert r.unsliced == 0 and abs(r.price - 9.0) < 1e-6
    assert r.preview_path == str(grey)         # картинка на месте, а не стёрта в None


async def test_place_one_falls_back_to_base_preview(tmp_db, tmp_path, monkeypatch):
    """Обычное оформление (_place_one): цветного превью нет, но серое есть — заказ должен
    получить серое, а не остаться без картинки (OpenTodo c6d9cebc)."""
    uid = 221
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "octo2.stl"
    stl.write_bytes(b"flexi-octopus-2")
    grey = tmp_path / "octo2_grey.jpg"
    grey.write_bytes(b"jpeg")
    m = {"paths": [str(stl)], "label": "Флекси осьминог", "uid": uid, "plates": 1,
         "plates_sel": None, "multicolor": False, "num_colors": 1,
         "cpreview": None, "preview": str(grey),
         "last": _last(plastic, printer, quality, strength, total=9.0)}

    stub_bot(monkeypatch, None)
    rid = await webapp._place_one({"id": uid, "username": "u"}, m, comment="")
    assert rid is not None
    r = await tmp_db.get_request(rid)
    assert r.preview_path == str(grey)


async def test_order_now_with_ready_calc_places_priced(tmp_db, tmp_path, monkeypatch):
    """Расчёт уже долетел (m["last"] есть) → order_now сразу оформляет ПОЛНЫЙ заказ с ценой."""
    uid = 202
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "m3.stl"
    stl.write_bytes(b"fake-stl-3")
    token = "tok-now-3"
    webapp._models[token] = {"paths": [str(stl)], "label": "Готов", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1,
                             "last": _last(plastic, printer, quality, strength, total=21.0),
                             "cpreview": None}

    stub_auth(monkeypatch,
              _verify_user=lambda s: {"id": uid, "username": "tester", "first_name": "Т"})
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    resp = await webapp._api_order_now(_FakeRequest({"token": token, "comment": ""}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and j["priced"] is True

    rows = await tmp_db.list_user_requests(uid)
    assert len(rows) == 1
    r = rows[0]
    assert r.unsliced == 0 and abs(r.price - 21.0) < 1e-6
    assert "pending_order_id" not in webapp._models[token]


async def test_order_now_idempotent_no_duplicate(tmp_db, tmp_path, monkeypatch):
    """Двойной тап / повтор запроса по одному токену не плодит второй заказ (ревью-фикс B):
    второй вызов возвращает ТОТ ЖЕ order_id с dedup=True, в БД ровно одна заявка."""
    uid = 210
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)
    stl = tmp_path / "mi.stl"
    stl.write_bytes(b"fake")
    token = "tok-idem-1"
    webapp._models[token] = {"paths": [str(stl)], "label": "Слайсится", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": uid, "username": "u"})
    stub_bot(monkeypatch, None)

    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 1}
    r1 = json.loads((await webapp._api_order_now(_FakeRequest(body))).body.decode())
    r2 = json.loads((await webapp._api_order_now(_FakeRequest(body))).body.decode())
    assert r1["ok"] and r2["ok"]
    assert r1["order_id"] == r2["order_id"]        # тот же заказ
    assert r2.get("dedup") is True
    assert len(await tmp_db.list_user_requests(uid)) == 1   # ровно одна заявка, не две


class _FakeBD:
    grams_total = 55.0
    minutes_total = 140.0
    total = 18.9
    grams_unit = 55.0
    minutes_unit = 140.0
    extra_colors = 0


async def test_refine_calc_finalizes_pending_order(tmp_db, tmp_path, monkeypatch):
    """Заказ, оформленный во время БЫСТРОГО (MW/кэш) расчёта, досчитывается фоновым _refine_calc —
    цена проставляется отсюда, а не теряется навсегда (ревью-фикс A)."""
    uid = 211
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)
    stl = tmp_path / "mr.stl"
    stl.write_bytes(b"fake")
    rid = await tmp_db.add_request(
        user_id=uid, user_name="u", model_label="M", printer=printer.name,
        plastic_name="Test PLA · Белый", quantity=1, quality=quality.label, strength=strength.label,
        grams_total=0, minutes_total=0, price=0, preview_path=None,
        hash=cache.model_hash([str(stl)]), file_path=str(stl),
        quality_id=quality.id, strength_id=strength.id, unsliced=1,
        price_pending=1)   # Э0: заказ order_now ждёт цену (живой хвост применяет только такой)
    token = "tok-refine-1"
    m = {"paths": [str(stl)], "label": "M", "uid": uid, "plates": 1, "multicolor": False,
         "num_colors": 1, "calc_seq": 5, "pending_order_id": rid}
    webapp._models[token] = m

    res = {"bd": _FakeBD(), "pdisp": "Test PLA · Белый", "settings": {}, "preview_path": None,
           "dims": "40×30×20 мм", "color_grams": [], "cached": False}

    async def fake_compute(**kw):
        return res

    async def fake_remember(*a, **k):
        return None

    async def fake_queue_load(*a, **k):
        return {"minutes": 0}

    monkeypatch.setattr(calcservice, "compute", fake_compute)
    # читатель (_api_calc_inner) живёт в clientflow (волна 15): подменяем в его модуле,
    # вебапповский фасад — вдогонку, чтобы не разъезжались (образец — mesh3d)
    for _m in (clientflow, webapp):
        monkeypatch.setattr(_m, "_remember_calc_cache", fake_remember)
        monkeypatch.setattr(_m, "_result_payload", lambda *a, **k: {})
        monkeypatch.setattr(_m, "_live_progress", lambda *a, **k: {})
    monkeypatch.setattr(webapp.pricing, "breakdown_to_dict", lambda *a, **k: {})
    monkeypatch.setattr(webapp.db, "queue_load", fake_queue_load)
    monkeypatch.setattr(webapp.db, "eta_date", lambda *a, **k: "")
    stub_bot(monkeypatch, None)

    await webapp._refine_calc_inner(
        token, 5, m, uid=uid, printer=printer, plastic=plastic, quality=quality, strength=strength,
        color="Белый", colors_sel=[], qty=1, extra_colors=0, multicolor=False, promo_pct=0.0)

    r = await tmp_db.get_request(rid)
    assert r.unsliced == 0
    assert abs(r.price - 18.9) < 1e-6
    assert abs(r.grams_total - 55.0) < 1e-6
    assert "pending_order_id" not in m               # привязка снята — повторно не финализируем
