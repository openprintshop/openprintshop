"""Быстрый расчёт по ссылке в чат: фильтр «голая ссылка», выбор базового пластика, проводка."""
from types import SimpleNamespace

import trimesh
from conftest import stub_bot

from app import calcservice, downloader, pricing, webapp
from app.handlers.user_flow import _bare_url


def _msg(text):
    return SimpleNamespace(text=text)


def test_bare_url_filter_accepts_only_lone_links():
    assert _bare_url(_msg("https://makerworld.com/models/12345"))
    assert _bare_url(_msg("  https://www.printables.com/model/99  "))
    assert not _bare_url(_msg("посмотри https://makerworld.com/models/12345"))   # ссылка в тексте
    assert not _bare_url(_msg("https://a.com https://b.com"))                    # две ссылки
    assert not _bare_url(_msg("привет"))
    assert not _bare_url(_msg(""))
    assert not _bare_url(_msg(None))


async def test_quick_calc_plastic_always_elegoo_matte(tmp_db):
    """ВСЕГДА Elegoo PLA Matte (решение владельца); нет его в каталоге → дефолтный."""
    a = await tmp_db.add_plastic("Crown PLA Silk", 60, 1.24, "PLA")
    await tmp_db.update_plastic(a, is_active=1)
    await tmp_db.set_default_plastic(a)
    b = await tmp_db.add_plastic("Generic PLA Matte", 50, 1.24, "PLA Matte")
    await tmp_db.update_plastic(b, is_active=1)
    # Elegoo Matte нет → дефолтный (просто «матовый» НЕ перевешивает)
    p = await webapp._quick_calc_plastic()
    assert p and p.id == a

    c = await tmp_db.add_plastic("Elegoo PLA Matte чёрный", 45, 1.24, "PLA Matte")
    await tmp_db.update_plastic(c, is_active=1)
    p = await webapp._quick_calc_plastic()
    assert p and p.id == c            # Elegoo PLA Matte — всегда он

    # выключили катушку → фолбэк на дефолтный, фича не умирает
    await tmp_db.update_plastic(c, is_active=0)
    p = await webapp._quick_calc_plastic()
    assert p and p.id == a


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

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.sent.append(("edit", chat_id, text, kw.get("reply_markup")))

    async def edit_message_caption(self, chat_id=None, message_id=None, caption="", **kw):
        self.sent.append(("edit_caption", chat_id, caption, kw.get("reply_markup")))


async def test_quick_link_quote_wiring(tmp_db, tmp_path, monkeypatch):
    """Скачивание и слайс замоканы — проверяем проводку: модель регистрируется с токеном,
    смета (last/last_payload) заполняется, юзеру уходит сообщение с ценой и кнопкой в приложение."""
    pid = await tmp_db.add_plastic("Elegoo PLA Matte", 45, 1.24, "PLA Matte")
    await tmp_db.update_plastic(pid, is_active=1)

    stl = tmp_path / "box.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(stl))

    async def fake_variants(url):
        return None

    async def fake_fetch(url):
        return [stl], "Кубик"

    monkeypatch.setattr(downloader, "fetch_makerworld_variants", fake_variants)
    monkeypatch.setattr(downloader, "fetch_from_url", fake_fetch)

    async def fake_compute(**kw):
        bd = pricing.compute_price_manual(12.4, 30.0, 45.0, await tmp_db.get_settings())
        return {"bd": bd, "settings": await tmp_db.get_settings(),
                "preview_path": str(tmp_path / "нет_рендера.png"),   # нет файла → текстовая ветка
                "cached": False, "pdisp": kw["plastic"].name, "plates": 1, "dims": "10×10×10 мм",
                "color_grams": [], "plastic_grams": [], "scale_pct": 100,
                "scale_clamped": False, "scale_max_pct": None}

    monkeypatch.setattr(calcservice, "compute", fake_compute)
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    await webapp.quick_link_quote(chat_id=777, uid=42, url="https://example.com/model")

    assert len(bot.sent) == 1
    kind, chat_id, text, kb = bot.sent[0]
    assert kind == "text" and chat_id == 777          # рендера нет → текст (с фото — та же подпись)
    assert "Кубик" in text and "Расчёт готов" in text
    assert kb is not None
    # кнопка заказа прямо из чата (ux-paths-next.md идея 3) + «Открыть смету» (deep-link go=quote)
    order_btn = kb.inline_keyboard[0][0]
    assert order_btn.callback_data.startswith("qorder:")
    url = kb.inline_keyboard[1][0].web_app.url
    assert "go=quote" in url and "t=" in url

    # модель зарегистрирована, смета по токену живая (deep-link /api/quote_state)
    token = url.split("t=")[-1]
    assert order_btn.callback_data == "qorder:" + token
    m = webapp._models.get(token)
    assert m and m["uid"] == 42 and m["label"] == "Кубик"
    assert m["last"]["plastic_id"] == pid and m["last"]["qty"] == 1
    assert m["last_payload"]["dims"] == "10×10×10 мм"
    assert m["last_payload"]["total"]
    # решения владельца: качество = ★-стандарт, прочность = золотая середина списка
    quals = await tmp_db.list_quality(active_only=True)
    strs = await tmp_db.list_strength(active_only=True)
    assert m["last"]["quality_id"] == next((q for q in quals if q.is_default), quals[0]).id
    assert m["last"]["strength_id"] == strs[len(strs) // 2].id


async def test_quick_link_mw_estimate_instant(tmp_db, tmp_path, monkeypatch):
    """Быстрый расчёт из данных MW: цена мгновенно (approx), слайс бежит ФОНОМ и обновляет
    смету + редактирует сообщение точной ценой (97a91eee)."""
    import asyncio

    pid = await tmp_db.add_plastic("Elegoo PLA Matte", 45, 1.24, "PLA Matte")
    await tmp_db.update_plastic(pid, is_active=1)
    stl = tmp_path / "box.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(stl))

    async def fake_estimate(url):
        return {"title": "Дракон", "iid": 77, "weight_g": 100.0, "minutes": 120.0,
                "colors_cnt": 2, "plates": 1, "cover": "", "printer": "A1"}

    async def fake_instance(url, iid):
        assert iid == 77                       # качаем именно профиль из оценки
        return [stl], "Дракон"

    computed = {}

    async def fake_compute(**kw):
        computed["ran"] = True                 # фоновый слайс ДОЛЖЕН запуститься
        bd = pricing.compute_price_manual(90.0, 110.0, 45.0, await tmp_db.get_settings())
        return {"bd": bd, "settings": await tmp_db.get_settings(),
                "preview_path": "", "cached": False, "pdisp": kw["plastic"].name,
                "plates": 1, "dims": "10×10×10 мм", "color_grams": [], "plastic_grams": [],
                "scale_pct": 100, "scale_clamped": False, "scale_max_pct": None}

    monkeypatch.setattr(downloader, "fetch_makerworld_estimate", fake_estimate)
    monkeypatch.setattr(downloader, "download_makerworld_instance", fake_instance)
    monkeypatch.setattr(calcservice, "compute", fake_compute)
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    await webapp.quick_link_quote(chat_id=5, uid=42, url="https://makerworld.com/en/models/1")

    assert len(bot.sent) >= 2                  # мгновенная оценка + смета с кнопкой
    assert "Быстрая оценка" in bot.sent[0][2] and "MakerWorld" in bot.sent[0][2]
    _kind, _chat, text, kb = bot.sent[1]
    assert "Расчёт готов" in text and "уточнит" in text
    assert kb.inline_keyboard[0][0].callback_data.startswith("qorder:")   # заказ из чата (идея 3)
    token = kb.inline_keyboard[1][0].web_app.url.split("t=")[-1]
    m = webapp._models[token]
    assert m["last"]["approx"] is True and m["last_payload"]["approx"] is True
    assert m["last_payload"]["refining"] is True
    assert m["last"]["minutes_total"] == 120.0
    # объём для пересчётов восстановлен из граммов через плотность
    assert abs(m["last"]["volume_cm3_unit"] - m["last"]["grams_unit"] / 1.24) < 0.01 \
        if m["last"].get("grams_unit") else True
    assert m["last"]["plastic_id"] == pid

    # даём фоновому уточнению дорешать: смета становится точной, сообщение отредактировано
    # (щедрый потолок ожидания — под нагрузкой полного прогона 200 мс порой не хватало)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if m["last"].get("approx") is False:
            break
    assert computed.get("ran") is True
    assert m["last"]["approx"] is False
    assert "refining" not in m["last_payload"]
    assert m["last"]["minutes_total"] == 110.0          # цифры из слайса, не из MW
    assert any(s[0] in ("edit", "edit_caption") and "уточнена" in s[2] for s in bot.sent)

    # снимок сметы (26.08) дописан уточнёнными цифрами — на нём держится блок «Слайс уточнил
    # цену» в деске. Ждём отдельно: эта запись идёт САМОЙ последней, уже после правки caption.
    snap = None
    for _ in range(200):
        snap = await tmp_db.get_calc_snapshot(m.get("calc_snap_id") or 0)
        if snap and snap["refined_at"]:
            break
        await asyncio.sleep(0.01)
    assert snap and snap["refined_at"] > 0
    assert snap["refined_total"] == m["last"]["total"]
    assert snap["total"] != snap["refined_total"]      # показанная цена цела, уточнённая рядом


async def test_quick_link_mw_estimate_falls_back_to_slice(tmp_db, tmp_path, monkeypatch):
    """Сбой MW-пути (скачивание умерло) → честный фолбэк на прежний слайс-путь."""
    pid = await tmp_db.add_plastic("Elegoo PLA Matte", 45, 1.24, "PLA Matte")
    await tmp_db.update_plastic(pid, is_active=1)
    stl = tmp_path / "box.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(str(stl))

    async def fake_estimate(url):
        return {"title": "X", "iid": 1, "weight_g": 10.0, "minutes": 5.0,
                "colors_cnt": 1, "plates": 1, "cover": "", "printer": ""}

    async def broken_instance(url, iid):
        raise downloader.DownloadError("капча")

    async def fake_variants(url):
        return None

    async def fake_fetch(url):
        return [stl], "Кубик"

    async def fake_compute(**kw):
        bd = pricing.compute_price_manual(12.4, 30.0, 45.0, await tmp_db.get_settings())
        return {"bd": bd, "settings": await tmp_db.get_settings(),
                "preview_path": str(tmp_path / "нет.png"), "cached": False,
                "pdisp": kw["plastic"].name, "plates": 1, "dims": "",
                "color_grams": [], "plastic_grams": [], "scale_pct": 100,
                "scale_clamped": False, "scale_max_pct": None}

    monkeypatch.setattr(downloader, "fetch_makerworld_estimate", fake_estimate)
    monkeypatch.setattr(downloader, "download_makerworld_instance", broken_instance)
    monkeypatch.setattr(downloader, "fetch_makerworld_variants", fake_variants)
    monkeypatch.setattr(downloader, "fetch_from_url", fake_fetch)
    monkeypatch.setattr(calcservice, "compute", fake_compute)
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    await webapp.quick_link_quote(chat_id=5, uid=42, url="https://makerworld.com/en/models/1")
    # мгновенная оценка ушла, затем MW-путь упал → слайс-путь дослал обычную смету
    assert any("Быстрая оценка" in s[2] for s in bot.sent)
    assert any("Расчёт готов" in s[2] for s in bot.sent)


async def test_quick_link_downloads_default_variant(tmp_db, monkeypatch):
    """MakerWorld с вариантами: качаем ДЕФОЛТНЫЙ вариант (default_iid); без него — первый."""
    calls = {}

    async def fake_variants(url):
        return {"title": "X", "variants": [{"id": 7, "title": "Первый"}, {"id": 9}],
                "default_iid": 9}

    async def fake_instance(url, iid):
        calls["iid"] = iid
        raise downloader.DownloadError("стоп для теста")

    monkeypatch.setattr(downloader, "fetch_makerworld_variants", fake_variants)
    monkeypatch.setattr(downloader, "download_makerworld_instance", fake_instance)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)
    await webapp.quick_link_quote(chat_id=1, uid=2, url="https://makerworld.com/x")
    assert calls["iid"] == 9                       # default_iid, а не первый
    assert bot.sent and "Не удалось скачать" in bot.sent[0][2]

    async def fake_variants_no_default(url):
        return {"title": "X", "variants": [{"id": 7}, {"id": 9}], "default_iid": None}

    monkeypatch.setattr(downloader, "fetch_makerworld_variants", fake_variants_no_default)
    await webapp.quick_link_quote(chat_id=1, uid=2, url="https://makerworld.com/x")
    assert calls["iid"] == 7                       # фолбэк: первый вариант
