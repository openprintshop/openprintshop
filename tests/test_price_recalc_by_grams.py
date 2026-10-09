"""OpenTodo 8c1cab08: «Изменить цену» заказа — режим «по граммам и времени» как альтернатива
вписыванию цены руками. Тот же эндпоинт _api_admin_order_price: если пришли grams+minutes,
цена считается формулой ручного калькулятора (compute_price_manual) по пластику/тиражу заказа
через calcservice.reprice_request, дальше — тот же путь применения (дешевле → сразу+уведомление,
дороже/равно → price_changed на подтверждение), что и у ручной цены."""
import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from conftest import stub_bot

from app import calcservice, config, webapp

_TOKEN = "123:test-token"
_ADMIN_ID = 424242


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", _TOKEN)
    monkeypatch.setattr(config, "ADMIN_IDS", [_ADMIN_ID])


class _JsonRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _forge(uid: int, token: str = _TOKEN) -> str:
    user = {"id": uid, "first_name": "T"}
    params = {"user": json.dumps(user, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


async def _seed_plastic(db_, name: str = "Test PLA") -> int:
    return await db_.add_plastic(name, price_per_kg=40.0, density=1.24, material_type="PLA")


async def _seed_request(db_, price: float, plastic_name: str, qty: int = 2) -> int:
    return await db_.add_request(
        user_id=111, user_name="t", model_label="Кубик", printer="p", plastic_name=plastic_name,
        quantity=qty, quality="q", strength="s", grams_total=10, minutes_total=20,
        price=price, preview_path=None)


async def test_price_recalc_by_grams_cheaper_applies_directly_and_notifies(tmp_db, monkeypatch):
    """Пересчёт по граммам/времени дал цену НИЖЕ текущей: применяем сразу (как и у ручной цены),
    клиенту уходит user_recalc_note, в заказе обновляются grams_total/minutes_total/breakdown."""
    db_ = tmp_db
    await _seed_plastic(db_, "Test PLA")
    rid = await _seed_request(db_, price=100000.0, plastic_name="Test PLA · чёрный", qty=2)

    r_before = await db_.get_request(rid)
    expected = await calcservice.reprice_request(r_before, 100.0, 60.0)

    sent = {}
    stub_bot(monkeypatch, object())
    monkeypatch.setattr(webapp.notify, "notify_user",
                        lambda bot, uid, text, **kw: sent.update(uid=uid, text=text))

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "grams": 100, "minutes": 60, "comment": "по факту печати"}))
    assert resp.status == 200
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["ok"] is True and payload["applied"] == "direct"
    assert payload["price"] == pytest.approx(expected["bd"].total, abs=0.01)

    r = await db_.get_request(rid)
    assert r.price == pytest.approx(expected["bd"].total, abs=0.01)
    assert r.pending_price is None
    assert r.status != "price_changed"
    assert r.grams_total == pytest.approx(expected["bd"].grams_total, abs=0.01)
    assert r.minutes_total == pytest.approx(expected["bd"].minutes_total, abs=0.01)
    assert r.breakdown is not None
    assert r.price_comment == "по факту печати"

    expected_text = webapp.texts.user_recalc_note(r, 100000.0, r.price, config.CURRENCY)
    assert sent["uid"] == r.user_id and sent["text"] == expected_text


async def test_price_recalc_by_grams_pricier_keeps_confirmation_flow(tmp_db, monkeypatch):
    """Пересчёт дал цену ВЫШЕ текущей: тот же поток подтверждения, что и у ручной цены —
    status=price_changed, pending_price=новая цена, клиенту уходит user_price_note. Ответ
    (в отличие от старого «просто price» режима) несёт посчитанную цену."""
    db_ = tmp_db
    await _seed_plastic(db_, "Test PLA")
    rid = await _seed_request(db_, price=0.01, plastic_name="Test PLA · чёрный", qty=2)

    sent = {}
    stub_bot(monkeypatch, object())
    monkeypatch.setattr(webapp.notify, "notify_user",
                        lambda bot, uid, text, **kw: sent.update(uid=uid, text=text))

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "grams": 100, "minutes": 60}))
    assert resp.status == 200
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["ok"] is True and payload["applied"] == "pending" and payload["price"] > 0

    r = await db_.get_request(rid)
    assert r.status == "price_changed"
    assert r.pending_price == pytest.approx(payload["price"], abs=0.01)
    assert r.price == 0.01                                              # текущая цена не тронута

    expected_text = webapp.texts.user_price_note(r, config.CURRENCY)
    assert sent["uid"] == r.user_id and sent["text"] == expected_text


async def test_price_recalc_by_grams_unknown_plastic_returns_clear_error(tmp_db):
    """Пластик заказа не найден в справочнике — пересчёт невозможен, ошибка понятная (не 500)."""
    db_ = tmp_db
    rid = await _seed_request(db_, price=10.0, plastic_name="Несуществующий пластик XYZ", qty=1)

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "grams": 100, "minutes": 60}))
    assert resp.status == 200
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["ok"] is False
    assert "пластик" in payload["error"].lower()

    r = await db_.get_request(rid)
    assert r.price == 10.0                                              # ничего не поменялось


async def test_price_recalc_by_grams_rejects_non_positive_grams(tmp_db):
    """grams=0 — некорректный ввод, понятная ошибка вместо тихого расчёта нулевой цены."""
    db_ = tmp_db
    await _seed_plastic(db_, "Test PLA")
    rid = await _seed_request(db_, price=10.0, plastic_name="Test PLA", qty=1)

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "grams": 0, "minutes": 60}))
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["ok"] is False and "граммы" in payload["error"].lower()


async def test_price_plain_mode_response_shape_unchanged(tmp_db, monkeypatch):
    """Старый режим (просто price, без grams/minutes) не тронут: pending-ответ по-прежнему
    {"ok": True} без "applied"/"price" — фронт старого поведения не должен увидеть новых полей."""
    db_ = tmp_db
    rid = await _seed_request(db_, price=1000.0, plastic_name="PLA", qty=1)
    stub_bot(monkeypatch, None)

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "price": 1100}))
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload == {"ok": True}
