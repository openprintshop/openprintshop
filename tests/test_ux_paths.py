"""UX-пути (docs/…/ux-paths-next.md).

- Идея 7: глубокая ссылка /start calc (QR/визитки) зовёт в калькулятор — подпись-подсказка
  в карточке меню + отметка «по ссылке-калькулятору» в ленте активности.

(Идея 5 «последние модели» на Главной убрана по просьбе владельца — её тесты удалены.)
"""
from types import SimpleNamespace

from app.handlers import common as common_handlers

# ---- Идея 7: /start calc -----------------------------------------------------------------

class _FakeState:
    def __init__(self):
        self.data = {}

    async def clear(self):
        self.data = {}

    async def update_data(self, **kw):
        self.data.update(kw)


class _FakeSent:
    def __init__(self):
        self.message_id = 1
        self.photo = [SimpleNamespace(file_id="cached_file_id")]


class _FakeMessage:
    def __init__(self, uid, name="Тест"):
        self.from_user = SimpleNamespace(id=uid, full_name=name)
        self.chat = SimpleNamespace(id=uid)
        self.bot = None
        self.sent = []

    async def answer_photo(self, photo, caption=None, reply_markup=None):
        self.sent.append(SimpleNamespace(kind="photo", caption=caption, kb=reply_markup))
        return _FakeSent()

    async def answer(self, text, **kw):
        self.sent.append(SimpleNamespace(kind="text", caption=text, kb=kw.get("reply_markup")))
        return _FakeSent()


async def test_start_calc_deeplink_opens_calc(tmp_db):
    msg = _FakeMessage(uid=501)
    await common_handlers.cmd_start(msg, _FakeState(), SimpleNamespace(args="calc"))
    assert len(msg.sent) == 1
    cap = msg.sent[0].caption or ""
    assert "стоимость печати" in cap and "🧮" in cap        # подпись-подсказка калькулятора
    acts = await tmp_db.list_activity(user_id=501)
    assert any(a["action"] == "открыл бота" and a["detail"] == "по ссылке-калькулятору" for a in acts)


async def test_start_plain_has_no_calc_prefix(tmp_db):
    msg = _FakeMessage(uid=502)
    await common_handlers.cmd_start(msg, _FakeState(), SimpleNamespace(args=None))
    assert len(msg.sent) == 1
    assert msg.sent[0].caption is None                       # без аргумента — просто карточка меню
    acts = await tmp_db.list_activity(user_id=502)
    assert any(a["action"] == "открыл бота" and a["detail"] == "" for a in acts)
