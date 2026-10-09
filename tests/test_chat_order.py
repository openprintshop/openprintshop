"""Заказ прямо из чата по кнопке под быстрой сметой (ux-paths-next.md, идея 3;
webapp.place_order_from_chat) — оформление по токену без мини-аппа + идемпотентность."""
from conftest import stub_bot

from app import webapp


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


async def _std_presets(db):
    printer_id = await db.add_printer("P", "mp.json", "pd", "fd")
    await db.set_default_printer(printer_id)
    quality_id = await db.add_quality("Standard", 0.2, "proc")
    strength_id = await db.add_strength("Normal", 15, 3)
    return (await db.get_default_printer(), await db.get_quality(quality_id),
            await db.get_strength(strength_id))


def _last(plastic, printer, quality, strength):
    return {"plastic_name": f"{plastic.name} · Белый", "printer": printer.name, "qty": 1,
            "quality": quality.label, "strength": strength.label, "dims": "40×30×20 мм",
            "grams_total": 40.0, "minutes_total": 90.0, "total": 12.5,
            "plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
            "color": "Белый", "colors_sel": [], "color_grams": [], "plastic_grams": [],
            "scale_pct": 100, "approx": False}


async def test_chat_order_places_and_notifies(tmp_db, tmp_path, monkeypatch):
    uid = 300
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)
    stl = tmp_path / "m.stl"; stl.write_bytes(b"fake")
    token = "tok-chat-1"
    webapp._models[token] = {"paths": [str(stl)], "label": "Коала", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1,
                             "last": _last(plastic, printer, quality, strength), "pdf": {}}
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://x.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    bot = _FakeBot(); stub_bot(monkeypatch, bot)

    u = {"id": uid, "username": "tester", "first_name": "Т"}
    ok, _msg = await webapp.place_order_from_chat(u, token)
    assert ok is True
    rows = await tmp_db.list_user_requests(uid)
    assert len(rows) == 1 and rows[0].price == 12.5 and rows[0].quantity == 1
    assert webapp._models[token]["ordered_rid"] == rows[0].id
    # клиент и мастер уведомлены
    assert any(s[1] == uid for s in bot.sent) and any(s[1] == 999 for s in bot.sent)

    # повторный тап — идемпотентно, второй заказ НЕ создаётся
    ok2, _ = await webapp.place_order_from_chat(u, token)
    assert ok2 is True
    assert len(await tmp_db.list_user_requests(uid)) == 1


async def test_chat_order_expired_token(tmp_db, monkeypatch):
    bot = _FakeBot(); stub_bot(monkeypatch, bot)
    ok, msg = await webapp.place_order_from_chat({"id": 1}, "no-such-token")
    assert ok is False and "устарел" in msg


async def test_chat_order_foreign_token_rejected(tmp_db, tmp_path, monkeypatch):
    """Чужой токен (uid не совпал) не оформляется."""
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("PLA", 1000.0, 1.24, "PLA")
    plastic = await tmp_db.get_plastic(pid)
    stl = tmp_path / "x.stl"; stl.write_bytes(b"fake")
    webapp._models["tok-foreign"] = {"paths": [str(stl)], "label": "M", "uid": 111, "plates": 1,
                                     "multicolor": False, "num_colors": 1,
                                     "last": _last(plastic, printer, quality, strength), "pdf": {}}
    bot = _FakeBot(); stub_bot(monkeypatch, bot)
    ok, _ = await webapp.place_order_from_chat({"id": 222}, "tok-foreign")
    assert ok is False
    assert await tmp_db.list_user_requests(222) == []
