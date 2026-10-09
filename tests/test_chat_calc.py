"""Быстрый расчёт прямо из чата (OpenTodo 81d0f7ec): ссылка/файл, присланные ВНЕ мастера,
сразу запускают расчёт (поднимают мастер с загруженной моделью). Тестируем маршрутизацию:
ссылка → расчёт, файл → расчёт, прочий текст/команда → подсказка-меню, троттлинг."""
import pytest

from app.handlers import user_flow
from app.states import Calc


class _FakeState:
    def __init__(self):
        self.state = "PREV"
        self.data = {}
    async def clear(self):
        self.data = {}
    async def set_state(self, s):
        self.state = s
    async def update_data(self, **kw):
        self.data.update(kw)


class _FakeMessage:
    def __init__(self, text=None, document=None, uid=1):
        self.text = text
        self.document = document
        self.from_user = type("U", (), {"id": uid})()
        self.chat = type("C", (), {"id": uid})()
        self.bot = object()
        self.answers = []
    async def answer(self, text, **kw):
        self.answers.append(text)


@pytest.fixture(autouse=True)
def _allow_rate(monkeypatch):
    monkeypatch.setattr(user_flow.ratelimit, "allow", lambda *a, **k: True)


async def test_chat_link_starts_calc(monkeypatch):
    called = {}
    async def fake_model_text(message, state):
        called["ok"] = True
    monkeypatch.setattr(user_flow, "model_text", fake_model_text)
    st = _FakeState()
    msg = _FakeMessage(text="https://makerworld.com/en/models/123")
    await user_flow.chat_text_calc(msg, st)
    assert called.get("ok") is True          # расчёт запущен
    assert st.state == Calc.model            # мастер поднят с моделью
    assert msg.answers == []                 # без лишней подсказки — сразу к делу


async def test_chat_plain_text_shows_menu(monkeypatch):
    called = {}
    monkeypatch.setattr(user_flow, "model_text",
                        lambda *a: called.setdefault("ok", True))
    msg = _FakeMessage(text="привет, посчитай мне корпус")
    await user_flow.chat_text_calc(msg, _FakeState())
    assert "ok" not in called
    assert msg.answers and "/start" in msg.answers[0]


async def test_chat_command_not_treated_as_link(monkeypatch):
    async def boom(*a):
        raise AssertionError("команда не должна уходить в расчёт")
    monkeypatch.setattr(user_flow, "model_text", boom)
    msg = _FakeMessage(text="/admin")
    await user_flow.chat_text_calc(msg, _FakeState())
    assert msg.answers                        # показана подсказка, расчёт НЕ запущен


async def test_chat_document_starts_calc(monkeypatch):
    called = {}
    async def fake_doc(message, state):
        called["ok"] = True
    monkeypatch.setattr(user_flow, "model_document", fake_doc)
    st = _FakeState()
    await user_flow.chat_doc_calc(_FakeMessage(document=object()), st)
    assert called.get("ok") is True
    assert st.state == Calc.model


async def test_chat_ratelimit_blocks_link(monkeypatch):
    monkeypatch.setattr(user_flow.ratelimit, "allow", lambda *a, **k: False)
    called = {}
    async def fake_model_text(message, state):
        called["ok"] = True
    monkeypatch.setattr(user_flow, "model_text", fake_model_text)
    msg = _FakeMessage(text="https://www.thingiverse.com/thing:1")
    await user_flow.chat_text_calc(msg, _FakeState())
    assert "ok" not in called
    assert msg.answers and "часто" in msg.answers[0].lower()
