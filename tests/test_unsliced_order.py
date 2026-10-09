"""Заказ без сметы, если автослайс не удался (OpenTodo 55a1cf8f):

- /api/calc при calcservice.CalcError отдаёт can_order_unsliced=True и запоминает контекст
  (принтер/пластик/качество/прочность/тираж), с которым считали, в m["unsliced_ctx"];
- /api/order_unsliced по этому контексту заводит заявку с price=0, unsliced=1, файлом модели
  (мастер сможет открыть его в слайсере/папке — can_open_slicer/can_open_folder уже работают
  от file_path) и уведомляет клиента с мастером; без контекста — понятная ошибка, а не заказ;
- _order_json показывает «Оценит мастер» вместо «0,00 ₽», пока цену не назначили.
"""
import json

from conftest import stub_auth, stub_bot

from app import cache, calcservice, webapp


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


async def test_api_calc_error_offers_unsliced_order(tmp_db, tmp_path, monkeypatch):
    """Слайс упал → ответ помечен can_order_unsliced, а токен модели помнит, с чем считали."""
    uid = 100
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "broken.stl"
    stl.write_bytes(b"fake-stl-bytes")
    token = "tok-unsliced-1"
    webapp._models[token] = {"paths": [str(stl)], "label": "Кривая деталь", "uid": uid,
                             "plates": 1, "multicolor": False, "num_colors": 1}

    async def fake_compute(**kw):
        raise calcservice.CalcError("деталь не помещается на стол")

    monkeypatch.setattr(calcservice, "compute", fake_compute)
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 3, "color": "Белый"}
    resp = await webapp._api_calc_inner(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is False
    assert j["can_order_unsliced"] is True
    assert "не помещается" in j["error"]

    ctx = webapp._models[token]["unsliced_ctx"]
    assert ctx["printer"].id == printer.id and ctx["plastic"].id == plastic.id
    assert ctx["quality"].id == quality.id and ctx["strength"].id == strength.id
    assert ctx["qty"] == 3 and ctx["color"] == "Белый"
    assert "не помещается" in ctx["error"]


async def test_api_order_unsliced_creates_request_and_notifies(tmp_db, tmp_path, monkeypatch):
    """Заявка без сметы: price=0, unsliced=1, файл модели на месте — мастер и клиент уведомлены."""
    uid = 101
    printer, quality, strength = await _std_presets(tmp_db)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    plastic = await tmp_db.get_plastic(pid)

    stl = tmp_path / "broken2.stl"
    stl.write_bytes(b"fake-stl-bytes-2")
    token = "tok-unsliced-2"
    webapp._models[token] = {
        "paths": [str(stl)], "label": "Кривая деталь 2", "uid": uid, "plates": 1,
        "multicolor": False, "num_colors": 1,
        "unsliced_ctx": {"printer": printer, "plastic": plastic, "quality": quality,
                         "strength": strength, "color": "Белый", "qty": 2,
                         "error": "деталь не помещается на стол"},
    }

    stub_auth(monkeypatch,
              _verify_user=lambda s: {"id": uid, "username": "tester", "first_name": "Т"})
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    body = {"token": token, "comment": "нужно к пятнице"}
    resp = await webapp._api_order_unsliced(_FakeRequest(body))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True

    # контекст расчёта одноразовый — использован и снят с токена
    assert "unsliced_ctx" not in webapp._models[token]

    rows = await tmp_db.list_user_requests(uid)
    assert len(rows) == 1
    r = rows[0]
    assert r.unsliced == 1
    assert r.price == 0 and r.status == "new"
    assert r.plastic_name == "Test PLA · Белый" and r.quantity == 2
    assert r.printer == printer.name and r.quality == quality.label and r.strength == strength.label
    assert r.file_path == str(stl)                     # мастер сможет открыть файл в слайсере
    assert r.hash == cache.model_hash([str(stl)])
    assert "не помещается" in r.description and "нужно к пятнице" in r.description

    # уведомления: мастеру с пометкой про ручную оценку, клиенту — что заявка принята
    admin_msgs = [s for s in bot.sent if s[1] == 999]
    assert admin_msgs and "вручную" in admin_msgs[0][2]
    client_msgs = [s for s in bot.sent if s[1] == uid]
    assert client_msgs and "Заявка принята" in client_msgs[0][2]

    # в JSON для клиента цена — понятная подпись, а не «0,00 ₽»
    oj = webapp._order_json(r)
    assert oj["price"] == "Оценит мастер"
    assert oj["unsliced"] == 1


async def test_api_order_unsliced_without_context_fails_cleanly(tmp_db, monkeypatch):
    """Без предшествующего неудачного /api/calc — понятная ошибка, а не заявка вслепую."""
    uid = 102
    token = "tok-unsliced-3"
    webapp._models[token] = {"paths": [], "label": "X", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": uid})
    resp = await webapp._api_order_unsliced(_FakeRequest({"token": token}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is False
    assert await tmp_db.list_user_requests(uid) == []


async def test_order_json_shows_real_price_once_priced(tmp_db):
    """Как только у unsliced-заказа появляется реальная цена (клиент пересчитал/мастер назначил
    и она применилась), «Оценит мастер» больше не перекрывает настоящую сумму."""
    rid = await tmp_db.add_request(
        user_id=1, user_name="x", model_label="M", printer="P", plastic_name="Pl",
        quantity=1, quality="Q", strength="S", grams_total=0, minutes_total=0, price=0,
        preview_path=None, unsliced=1)
    r = await tmp_db.get_request(rid)
    assert webapp._order_json(r)["price"] == "Оценит мастер"

    await tmp_db.update_request(rid, price=12.5, grams_total=10.0, minutes_total=30.0)
    r2 = await tmp_db.get_request(rid)
    assert webapp._order_json(r2)["price"] != "Оценит мастер"
