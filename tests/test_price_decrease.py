"""OpenTodo: снижение цены админом не требует подтверждения клиента — применяем сразу и уведомляем.

Зеркалит поведение `_api_admin_order_recalc_apply` (дешевле → direct): цена пишется в price
(не pending_price), статус НЕ переводится в price_changed, клиенту уходит user_recalc_note
вместо user_price_note. Подорожание/равно — прежний поток подтверждения через price_changed.
"""
import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from conftest import stub_bot

from app import config, webapp

_TOKEN = "123:test-token"          # фиксированный тестовый токен (подписываем initData им же)
_ADMIN_ID = 424242                 # тестовый админ


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    """Детерминированные ADMIN_IDS/BOT_TOKEN — тесты не зависят от .env окружения
    (в dev-дереве .env нет, и config.ADMIN_IDS иначе пуст)."""
    monkeypatch.setattr(config, "BOT_TOKEN", _TOKEN)
    monkeypatch.setattr(config, "ADMIN_IDS", [_ADMIN_ID])


class _JsonRequest:
    """Фейковый aiohttp-запрос: только асинхронный .json() с заранее заданным телом."""
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _forge(uid: int, token: str = _TOKEN) -> str:
    """Валидная Telegram initData для пользователя uid (подпись по BOT_TOKEN)."""
    user = {"id": uid, "first_name": "T"}
    params = {"user": json.dumps(user, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


async def _seed_request(db_, price: float) -> int:
    return await db_.add_request(
        user_id=111, user_name="t", model_label="Кубик", printer="p", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=10, minutes_total=20,
        price=price, preview_path=None)


async def test_price_decrease_applies_directly_and_notifies_recalc(tmp_db, monkeypatch):
    """Цена снижена (1000 → 900): price=900, pending_price=None, статус НЕ price_changed,
    клиенту уходит именно user_recalc_note (а не user_price_note)."""
    db_ = tmp_db
    rid = await _seed_request(db_, 1000.0)

    sent = {}
    stub_bot(monkeypatch, object())                       # включаем ветку уведомления
    monkeypatch.setattr(webapp.notify, "notify_user",
                        lambda bot, uid, text, **kw: sent.update(uid=uid, text=text))

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "price": 900, "comment": "пересчитали дешевле"}))
    assert resp.status == 200
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["ok"] is True and payload["applied"] == "direct" and payload["price"] == 900

    r = await db_.get_request(rid)
    assert r.price == 900.0
    assert r.pending_price is None
    assert r.status != "price_changed"                                  # подтверждение не требуется

    # уведомление — текст снижения цены (user_recalc_note), не запрос подтверждения (user_price_note)
    expected = webapp.texts.user_recalc_note(r, 1000.0, 900.0, config.CURRENCY)
    assert sent["uid"] == r.user_id and sent["text"] == expected


async def test_price_increase_keeps_confirmation_flow(tmp_db, monkeypatch):
    """Цена выросла (1000 → 1100): статус price_changed, pending_price=1100, price не тронут;
    клиенту уходит user_price_note (запрос подтверждения)."""
    db_ = tmp_db
    rid = await _seed_request(db_, 1000.0)

    sent = {}
    stub_bot(monkeypatch, object())
    monkeypatch.setattr(webapp.notify, "notify_user",
                        lambda bot, uid, text, **kw: sent.update(uid=uid, text=text))

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "price": 1100, "comment": "дороже"}))
    assert resp.status == 200
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload == {"ok": True}                                      # прежний ответ без applied

    r = await db_.get_request(rid)
    assert r.status == "price_changed"
    assert r.pending_price == 1100.0
    assert r.price == 1000.0                                            # текущая цена не меняется до подтверждения

    expected = webapp.texts.user_price_note(r, config.CURRENCY)
    assert sent["uid"] == r.user_id and sent["text"] == expected


async def test_price_unchanged_uses_confirmation_flow(tmp_db, monkeypatch):
    """Равная цена (в пределах допуска) — не считаем снижением: остаётся прежний поток price_changed,
    чтобы не слать клиенту «цена снижена 🎉», когда ничего не изменилось."""
    db_ = tmp_db
    rid = await _seed_request(db_, 1000.0)
    stub_bot(monkeypatch, None)                           # уведомление не важно для этого кейса

    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid, "price": 1000}))
    assert resp.status == 200
    assert json.loads(resp.body.decode("utf-8")) == {"ok": True}

    r = await db_.get_request(rid)
    assert r.status == "price_changed" and r.pending_price == 1000.0


async def test_price_set_missing_order(tmp_db):
    """Несуществующий заказ → ok:false «не найден» (а не падение на r.price)."""
    init = _forge(_ADMIN_ID)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": 999999, "price": 500}))
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["ok"] is False and "не найден" in payload["error"].lower()


async def test_price_set_rejects_non_admin(tmp_db):
    init = _forge(999999)                                              # валидная подпись, но не админ
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": 1, "price": 5}))
    assert resp.status == 403


async def test_self_order_price_not_notified(tmp_db, monkeypatch):
    """Фидбэк 20.07: свой заказ (заказчик = админ) не гоняем по клиентскому ценовому флоу —
    «цена изменилась / подтвердите» самому себе не шлём. Клиентские заказы не затронуты."""
    db_ = tmp_db
    # заказ мастера самому себе: user_id = админ
    rid_self = await db_.add_request(
        user_id=_ADMIN_ID, user_name="@master", model_label="Своё", printer="p", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=10, minutes_total=20,
        price=1000.0, preview_path=None)
    sent = []
    stub_bot(monkeypatch, object())
    monkeypatch.setattr(webapp.notify, "notify_user",
                        lambda bot, uid, text, **kw: sent.append(uid))
    init = _forge(_ADMIN_ID)

    # цена вниз (применяется сразу) — уведомления быть не должно
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid_self, "price": 900}))
    assert resp.status == 200 and sent == []
    # цена вверх (обычно просит подтверждение клиента) — тоже молчим
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid_self, "price": 1200}))
    assert resp.status == 200 and sent == []
    assert (await db_.get_request(rid_self)).status == "price_changed"   # в БД статус как обычно

    # контроль: настоящему клиенту уведомление по-прежнему уходит
    rid_client = await _seed_request(db_, 1000.0)
    resp = await webapp._api_admin_order_price(
        _JsonRequest({"initData": init, "id": rid_client, "price": 1200}))
    assert resp.status == 200 and sent == [111]
