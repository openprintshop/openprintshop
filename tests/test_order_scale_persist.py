"""Масштаб заказа не теряется ни в заказах без сметы, ни при пересборке сметы.

Масштаб живёт в снимке сметы (requests.breakdown.scale_pct): по нему мастер видит размер в
карточке, а пересчёт/смена параметров/допечатка слайсят в нём же.

- /api/calc упал → контекст «заказа без сметы» помнит запрошенный масштаб, /api/order_unsliced
  кладёт его в заявку снимком из одного масштаба (сметой он не притворяется);
- order_now (заявка во время слайса) берёт масштаб идущего расчёта, досчёт без снимка цены
  его не стирает;
- update_request переносит масштаб, когда смету пересобирают из PriceBreakdown без него.
"""
import json

from conftest import stub_auth, stub_bot

from app import calcservice, texts, webapp
from app.web.orderdesk import _order_scale_pct


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
        self.sent.append((chat_id, text))
        return _FakeMsg(len(self.sent))

    async def send_photo(self, chat_id, photo, caption="", **kw):
        self.sent.append((chat_id, caption))
        return _FakeMsg(len(self.sent))


async def _presets(tmp_db):
    printer_id = await tmp_db.add_printer("P", "mp.json", "pd", "fd")
    await tmp_db.set_default_printer(printer_id)
    quality_id = await tmp_db.add_quality("Standard", 0.2, "proc")
    await tmp_db.set_default_quality(quality_id)
    strength_id = await tmp_db.add_strength("Normal", 15, 3)
    await tmp_db.set_default_strength(strength_id)
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    await tmp_db.update_plastic(pid, colors="Белый")
    return (await tmp_db.get_default_printer(), await tmp_db.get_plastic(pid),
            await tmp_db.get_quality(quality_id), await tmp_db.get_strength(strength_id))


def _wire(monkeypatch, uid):
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid,
              _verify_user=lambda s: {"id": uid, "username": "tester", "first_name": "Т"})
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    stub_bot(monkeypatch, _FakeBot())


async def test_failed_calc_then_unsliced_order_keeps_client_scale(tmp_db, tmp_path, monkeypatch):
    uid = 300
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = tmp_path / "godzilla.stl"
    stl.write_bytes(b"fake-stl")
    token = "tok-scale-unsliced"
    webapp._models[token] = {"paths": [str(stl)], "label": "Годзилла", "uid": uid,
                             "plates": 1, "multicolor": False, "num_colors": 1}

    async def fake_compute(**kw):
        raise calcservice.CalcError("слайсер упал")

    monkeypatch.setattr(calcservice, "compute", fake_compute)
    _wire(monkeypatch, uid)
    resp = await webapp._api_calc_inner(_FakeRequest(
        {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
         "strength_id": strength.id, "qty": 1, "color": "Белый", "scale": 250}))
    assert json.loads(resp.body.decode())["can_order_unsliced"] is True
    assert webapp._models[token]["unsliced_ctx"]["scale_pct"] == 250

    resp = await webapp._api_order_unsliced(_FakeRequest({"token": token}))
    assert json.loads(resp.body.decode())["ok"] is True
    r = (await tmp_db.list_user_requests(uid))[0]
    assert r.unsliced == 1 and r.breakdown == {"scale_pct": 250}
    assert _order_scale_pct(r) == 250                       # карточка мастера
    assert calcservice._snapshot_scale_pct(r) == 250         # пересчёт мастера слайсит в нём
    # снимок из одного масштаба — не смета: ни клиенту, ни мастеру нулевой расчётник не рисуем
    assert texts.order_breakdown(r, "BYN") is None
    assert webapp._order_json(r)["breakdown"] is None
    assert webapp._order_json(r)["price"] == "Оценит мастер"


async def test_order_now_takes_scale_of_running_calc(tmp_db, tmp_path, monkeypatch):
    uid = 301
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = tmp_path / "m.stl"
    stl.write_bytes(b"fake-stl")
    _wire(monkeypatch, uid)
    body = {"plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
            "qty": 1, "color": "Белый"}

    webapp._models["tok-now-scale"] = {"paths": [str(stl)], "label": "M", "uid": uid, "plates": 1,
                                       "multicolor": False, "num_colors": 1, "calc_scale_pct": 150}
    resp = await webapp._api_order_now(_FakeRequest({**body, "token": "tok-now-scale"}))
    rid = json.loads(resp.body.decode())["order_id"]
    r = await tmp_db.get_request(rid)
    assert r.breakdown == {"scale_pct": 150} and _order_scale_pct(r) == 150

    # расчёт не запускался в этом процессе (рестарт) → масштаб не выдумываем
    webapp._models["tok-now-noscale"] = {"paths": [str(stl)], "label": "M2", "uid": uid,
                                         "plates": 1, "multicolor": False, "num_colors": 1}
    resp = await webapp._api_order_now(_FakeRequest({**body, "token": "tok-now-noscale"}))
    r2 = await tmp_db.get_request(json.loads(resp.body.decode())["order_id"])
    assert r2.breakdown is None and _order_scale_pct(r2) is None


async def test_finalize_without_price_snapshot_keeps_scale(tmp_db, tmp_path, monkeypatch):
    """Досчёт order_now без снимка цены писал breakdown=NULL — масштаб заявки стирался."""
    uid = 302
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = tmp_path / "m.stl"
    stl.write_bytes(b"fake-stl")
    rid = await tmp_db.add_request(
        user_id=uid, user_name="tester", model_label="M", printer=printer.name,
        plastic_name="Test PLA · Белый", quantity=1, quality=quality.label, strength=strength.label,
        grams_total=0, minutes_total=0, price=0, preview_path=None, file_path=str(stl),
        breakdown={"scale_pct": 300}, unsliced=1,
        price_pending=1)   # Э0: живой хвост применяет цену только заказу, ждущему её
    last = {"plastic_name": "Test PLA · Белый", "printer": printer.name, "qty": 1,
            "quality": quality.label, "strength": strength.label, "dims": "1×1×1 мм",
            "grams_total": 10.0, "minutes_total": 30.0, "total": 9.0,
            "quality_id": quality.id, "strength_id": strength.id, "scale_pct": 240}
    _wire(monkeypatch, uid)
    await webapp._finalize_pending_order(rid, {"paths": [str(stl)], "label": "M", "uid": uid,
                                               "plates": 1, "last": last, "pdf": {}}, uid)
    r = await tmp_db.get_request(rid)
    assert r.unsliced == 0
    assert r.breakdown == {"scale_pct": 240}   # фактический после подгонки под стол, из last


async def _order_with(tmp_db, breakdown):
    return await tmp_db.add_request(
        user_id=1, user_name="x", model_label="M", printer="P", plastic_name="Pl", quantity=1,
        quality="Q", strength="S", grams_total=1, minutes_total=1, price=1, preview_path=None,
        file_path="m.stl", breakdown=breakdown)


async def test_rebuilt_breakdown_inherits_scale(tmp_db):
    """Цена «по граммам и времени»/смена параметров/пересчёт пересобирают снимок без масштаба."""
    rid = await _order_with(tmp_db, {"qty": 1, "total": 5.0, "scale_pct": 250})
    fresh = {"qty": 2, "total": 9.0}
    await tmp_db.update_request(rid, price=9.0, breakdown=fresh)
    r = await tmp_db.get_request(rid)
    assert r.breakdown == {"qty": 2, "total": 9.0, "scale_pct": 250}
    assert "scale_pct" not in fresh                  # словарь вызывающего не трогаем


async def test_explicit_scale_in_new_breakdown_wins(tmp_db):
    rid = await _order_with(tmp_db, {"qty": 1, "scale_pct": 250})
    await tmp_db.update_request(rid, breakdown={"qty": 1, "scale_pct": 120})
    assert (await tmp_db.get_request(rid)).breakdown["scale_pct"] == 120


async def test_no_old_scale_nothing_invented(tmp_db):
    rid = await _order_with(tmp_db, {"qty": 1})
    await tmp_db.update_request(rid, breakdown={"qty": 3})
    assert (await tmp_db.get_request(rid)).breakdown == {"qty": 3}
    rid2 = await _order_with(tmp_db, None)
    await tmp_db.update_request(rid2, breakdown={"qty": 3})
    assert (await tmp_db.get_request(rid2)).breakdown == {"qty": 3}
