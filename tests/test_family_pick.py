"""Нативный выбор партнёра для семейного счёта (OpenTodo 0528ba47): кнопка мини-аппа шлёт
в чат пикер request_users; users_shared-хендлер заводит заявку с точным user_id партнёра."""
import json
from types import SimpleNamespace

from conftest import stub_auth, stub_bot

from app import config, keyboards, webapp
from app.handlers import user_flow


class _FakeRequest:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.sent))


class _FakeMsg:
    """Message с users_shared для хендлера family_partner_shared."""

    def __init__(self, uid, shared_users, request_id=keyboards.FAMILY_PICK_REQUEST_ID):
        self.users_shared = SimpleNamespace(request_id=request_id, users=shared_users)
        self.from_user = SimpleNamespace(id=uid, username="buyer", full_name="Покупатель")
        self.bot = _FakeBot()
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw.get("reply_markup")))


def test_family_pick_keyboard_shape():
    kb = keyboards.family_pick_partner()
    btn = kb.keyboard[0][0]
    assert btn.request_users.request_id == keyboards.FAMILY_PICK_REQUEST_ID
    assert btn.request_users.max_quantity == 1
    assert btn.request_users.user_is_bot is False


async def test_pick_partner_endpoint_sends_picker(tmp_db, monkeypatch):
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": 501, "username": "u"})
    resp = await webapp._api_family_pick_partner(_FakeRequest({}))
    assert json.loads(resp.body.decode())["ok"] is True
    chat_id, _text, markup = bot.sent[0]
    assert chat_id == 501
    assert markup.keyboard[0][0].request_users.request_id == keyboards.FAMILY_PICK_REQUEST_ID


async def test_pick_partner_endpoint_rejects_family_member(tmp_db, monkeypatch):
    await tmp_db.create_family([(601, "@a"), (602, "@b")])
    stub_bot(monkeypatch, _FakeBot())
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": 601})
    resp = await webapp._api_family_pick_partner(_FakeRequest({}))
    assert json.loads(resp.body.decode())["ok"] is False


async def test_users_shared_creates_request_with_partner_id(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    uid = 701
    shared = SimpleNamespace(user_id=702, username="partner", first_name="Аня", last_name="")
    msg = _FakeMsg(uid, [shared])

    await user_flow.family_partner_shared(msg)

    items = [n for n in await tmp_db.list_admin_notifications()
             if n["kind"] == "family_request" and n["payload"].get("from_id") == uid]
    assert len(items) == 1
    assert items[0]["payload"]["partner_id"] == 702
    assert "@partner" in items[0]["payload"]["partner"]
    # клиенту ответили и сняли reply-клавиатуру
    assert msg.answers and msg.answers[-1][1] is not None
    # мастера пингнули
    assert any(chat == 999 for chat, _t, _m in msg.bot.sent)

    # повторный выбор — дубль-заявка не плодится
    msg2 = _FakeMsg(uid, [shared])
    await user_flow.family_partner_shared(msg2)
    items = [n for n in await tmp_db.list_admin_notifications()
             if n["kind"] == "family_request" and n["payload"].get("from_id") == uid]
    assert len(items) == 1


async def test_users_shared_second_pick_updates_partner(tmp_db, monkeypatch):
    """Клиент перевыбрал близкого, пока заявка ещё висит — карточка мастера должна показать
    НОВОГО партнёра. Раньше новый выбор молча выбрасывался, а клиенту всё равно уходило
    «заявка принята» с новым именем: мастер подтверждал по старому, и create_family уносил
    баланс не того человека в чужую семью (тот же класс, что инцидент 16.07)."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    uid = 711
    wrong = SimpleNamespace(user_id=712, username="wrong", first_name="Не", last_name="Тот")
    right = SimpleNamespace(user_id=713, username="right", first_name="Тот", last_name="Самый")

    await user_flow.family_partner_shared(_FakeMsg(uid, [wrong]))
    msg2 = _FakeMsg(uid, [right])
    await user_flow.family_partner_shared(msg2)

    items = [n for n in await tmp_db.list_admin_notifications(kind="family_request")
             if n["payload"].get("from_id") == uid]
    assert len(items) == 1, "дубль-карточка плодиться не должна"
    assert items[0]["payload"]["partner_id"] == 713, "у мастера должен быть НОВЫЙ партнёр"
    assert "@right" in items[0]["payload"]["partner"]
    assert "@right" in items[0]["body"]
    # мастеру ушёл второй пинг: в чате у него уже лежит сообщение со старым партнёром
    assert any("уточнена" in t for _c, t, _m in msg2.bot.sent)


async def test_text_request_does_not_downgrade_picked_partner(tmp_db, monkeypatch):
    """Текстовая заявка не должна затирать точный partner_id, выбранный пикером: именно
    неточное имя фронт резолвит подстрочным матчем и может попасть в однофамильца."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    uid = 721
    shared = SimpleNamespace(user_id=722, username="exact", first_name="Аня", last_name="")
    await user_flow.family_partner_shared(_FakeMsg(uid, [shared]))

    stub_bot(monkeypatch, _FakeBot())
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": uid, "username": "buyer"})
    resp = await webapp._api_family_request(_FakeRequest({"partner": "Ваня"}))
    assert json.loads(resp.body.decode())["ok"] is True

    items = [n for n in await tmp_db.list_admin_notifications(kind="family_request")
             if n["payload"].get("from_id") == uid]
    assert len(items) == 1
    assert items[0]["payload"]["partner_id"] == 722, "точный id пикера должен уцелеть"
    assert "@exact" in items[0]["payload"]["partner"]


async def test_users_shared_ignores_foreign_request_id(tmp_db):
    msg = _FakeMsg(801, [SimpleNamespace(user_id=802, username=None,
                                         first_name="Б", last_name="")], request_id=123)
    await user_flow.family_partner_shared(msg)
    assert not msg.answers                                     # чужой пикер — молча мимо


async def test_users_shared_self_pick_rejected(tmp_db):
    msg = _FakeMsg(901, [SimpleNamespace(user_id=901, username="me",
                                         first_name="Я", last_name="")])
    await user_flow.family_partner_shared(msg)
    assert msg.answers and "сами" in msg.answers[0][0]
    items = [n for n in await tmp_db.list_admin_notifications()
             if n["kind"] == "family_request" and n["payload"].get("from_id") == 901]
    assert not items
