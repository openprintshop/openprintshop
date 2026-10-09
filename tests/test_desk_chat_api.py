"""Эндпоинты чата настольной админки: сплошная переписка клиента, поиск, вложения.

Бот здесь подменён заглушкой — реальные сообщения живым клиентам в тестах не уходят.
"""
import json

import pytest
from conftest import stub_auth, stub_bot

from app import config, webapp
from app.web import ordermedia


class _FakeRequest:
    def __init__(self, body=None, query=None, remote="127.0.0.1", headers=None):
        self._body = body or {}
        self.query = query or {}
        self.remote = remote
        self.headers = headers or {}

    async def json(self):
        return self._body


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "tok", raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    webapp._REQ_IS_LOCAL.set(True)
    return "tok"


def _json(resp):
    return json.loads(resp.body.decode())


async def _await(v):
    return v


def _async(v):
    """Обёртка для monkeypatch: подменяем корутину простым значением."""
    return _await(v)


async def test_client_chat_merges_orders_and_standalone(tmp_db, admin):
    """Сплошной чат клиента — треды заказов ПЛЮС сообщения вне заказа, по времени."""
    await tmp_db.add_order_message(0, from_admin=False, text='по заказу', user_id=77)
    await tmp_db.add_client_message(77, from_admin=True, text='вне заказа')

    resp = await webapp._api_admin_client_chat(_FakeRequest(query={"initData": admin, "uid": "77"}))
    d = _json(resp)
    assert d["ok"]
    texts = [m["text"] for m in d["messages"]]
    assert texts == ['по заказу', 'вне заказа']
    # своё сообщение мастера — справа, клиентское — слева
    assert [m["mine"] for m in d["messages"]] == [False, True]


async def test_opening_client_chat_clears_unread(tmp_db, admin):
    """Открытие чата сбрасывает непрочитанное — иначе бейдж висел бы вечно."""
    await tmp_db.add_client_message(77, from_admin=False, text='вопрос')
    assert (await tmp_db.clients_unread_admin()).get(77) == 1

    await webapp._api_admin_client_chat(_FakeRequest(query={"initData": admin, "uid": "77"}))
    assert 77 not in await tmp_db.clients_unread_admin()


async def test_client_chat_requires_uid(tmp_db, admin):
    resp = await webapp._api_admin_client_chat(_FakeRequest(query={"initData": admin}))
    assert _json(resp)["ok"] is False


async def test_chat_search_finds_by_text(tmp_db, admin):
    await tmp_db.add_client_message(77, from_admin=False, text='нужен PETG синий')
    resp = await webapp._api_admin_chat_search(
        _FakeRequest(query={"initData": admin, "q": "petg"}))
    d = _json(resp)
    assert d["ok"] and len(d["hits"]) == 1
    assert d["hits"][0]["user_id"] == 77


async def test_chat_search_ignores_short_query(tmp_db, admin):
    """Однобуквенный запрос вернул бы пол-базы — палитра не должна этого делать."""
    await tmp_db.add_client_message(77, from_admin=False, text='нужен PETG')
    resp = await webapp._api_admin_chat_search(_FakeRequest(query={"initData": admin, "q": "n"}))
    assert _json(resp)["hits"] == []


async def test_client_message_saved_even_when_not_delivered(tmp_db, admin, monkeypatch):
    """Клиент мог не запускать бота. Сообщение всё равно обязано остаться в переписке,
    иначе мастер не увидит, что он вообще писал."""
    async def _no_delivery(*a, **kw):
        return False
    monkeypatch.setattr(webapp.notify, "notify_user", _no_delivery)
    stub_bot(monkeypatch, object())

    resp = await webapp._api_admin_client_message(
        _FakeRequest(body={"initData": admin, "uid": 77, "text": "привет"}))
    d = _json(resp)
    assert d["ok"] is False and d["saved"] is True      # честно говорим, что не доставлено
    msgs = await tmp_db.list_client_messages(77)
    assert [m["text"] for m in msgs] == ["привет"]      # но сообщение сохранено


async def test_client_message_needs_text_or_image(tmp_db, admin, monkeypatch):
    stub_bot(monkeypatch, object())
    resp = await webapp._api_admin_client_message(
        _FakeRequest(body={"initData": admin, "uid": 77, "text": "  "}))
    assert _json(resp)["ok"] is False
    assert await tmp_db.list_client_messages(77) == []


async def test_client_can_send_photo_too(tmp_db, monkeypatch):
    """Фото ходят в ОБЕ стороны: мастер видит брак на фото, а не в описании словами."""
    stub_bot(monkeypatch, None)

    class _R:
        id, user_id, user_name, model_label = 5, 77, 'Клиент', 'Модель'
    monkeypatch.setattr(webapp.db, "get_request", lambda _i: _async(_R()))
    stub_auth(monkeypatch, _verify_init_data=lambda _d: 77)
    # Подмена в ОБА модуля: _save_chat_image живёт в app/web/ordermedia.py, а webapp лишь
    # реэкспортирует его фасадом (образец — tests/test_attach_print_media.py).
    monkeypatch.setattr(webapp, "_save_chat_image", lambda _m, _d: _async('/tmp/p.jpg'))
    monkeypatch.setattr(ordermedia, "_save_chat_image", lambda _m, _d: _async('/tmp/p.jpg'))

    resp = await webapp._api_order_message(
        _FakeRequest(body={"initData": "x", "order_id": 5, "text": "", "image": "data:image/jpeg;base64,AAAA"}))
    assert _json(resp)["ok"]
    msgs = await tmp_db.list_order_messages(5)
    assert len(msgs) == 1 and msgs[0]["from_admin"] == 0


async def test_client_photo_is_rate_limited(tmp_db, monkeypatch):
    """Вложение — это загрузка файла: без лимита клиент забил бы диск мастерской."""
    stub_bot(monkeypatch, None)

    class _R:
        id, user_id, user_name, model_label = 6, 78, 'Клиент', 'Модель'
    monkeypatch.setattr(webapp.db, "get_request", lambda _i: _async(_R()))
    stub_auth(monkeypatch, _verify_init_data=lambda _d: 78)
    monkeypatch.setattr(webapp, "_save_chat_image", lambda _m, _d: _async(''))
    monkeypatch.setattr(ordermedia, "_save_chat_image", lambda _m, _d: _async(''))
    webapp.ratelimit._hits.clear()

    body = {"initData": "x", "order_id": 6, "text": "", "image": "data:image/jpeg;base64,AAAA"}
    oks = [_json(await webapp._api_order_message(_FakeRequest(body=dict(body))))["ok"]
           for _ in range(12)]
    assert oks[0] is True and oks[-1] is False, "лимит на фото не сработал"
    # текст лимитом не задет: переписку ограничивать незачем
    r = await webapp._api_order_message(
        _FakeRequest(body={"initData": "x", "order_id": 6, "text": "просто текст"}))
    assert _json(r)["ok"] is True


async def test_chat_photo_url_is_signed(tmp_db):
    """Вложение отдаётся по подписанному URL: перебором id его не достать."""
    mid = await tmp_db.add_client_message(77, from_admin=True, text='', image_path='/tmp/x.jpg')
    msg = await tmp_db.get_message(mid)
    url = webapp._chat_photo_url(msg)
    assert url.startswith(f"/admin/photo/chat/{mid}?s=")
    assert len(url.split("s=")[1]) == 12

    # без вложения ссылки нет вовсе
    mid2 = await tmp_db.add_client_message(77, from_admin=True, text='без фото')
    assert webapp._chat_photo_url(await tmp_db.get_message(mid2)) == ""


async def test_msgs_json_exposes_thread_for_separators(tmp_db):
    """request_id и order_label нужны сплошному чату: по ним рисуются разделители
    «— по заказу #N —», и они кликабельны."""
    await tmp_db.add_client_message(77, from_admin=False, text='вне заказа')
    rows = await tmp_db.list_client_messages(77)
    out = webapp._msgs_json(rows, mine_is_admin=True)
    assert out[0]["request_id"] is None
    assert out[0]["order_label"] == ""
    assert "id" in out[0] and "ts" in out[0]
