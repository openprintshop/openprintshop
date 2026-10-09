"""Классификация печати «заказ / для себя» в конце печати (мастер-план, Фаза B): реально
прогоняем aiogram-хендлеры pe: (admin_print_classify + admin_print_order_no) с фейковыми
CallbackQuery/Message/FSM — то, что раньше было проверено только импортом."""
import pytest

from app import config
from app.handlers import user_flow
from app.states import PrintStat


class _FakeState:
    def __init__(self):
        self._state = None
        self.data = {}
    async def set_state(self, s=None):
        self._state = s
    async def get_state(self):
        return self._state
    async def update_data(self, **kw):
        self.data.update(kw)
    async def get_data(self):
        return dict(self.data)


class _FakeMsg:
    def __init__(self, text=None, chat_id=1):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.message_id = 555
        self.answers = []
    async def answer(self, text, **kw):
        self.answers.append(text)
    async def edit_reply_markup(self, **kw):
        pass


class _FakeCb:
    def __init__(self, data, uid=777):
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.message = _FakeMsg()
        self.answers = []
    async def answer(self, text="", **kw):
        self.answers.append(text)


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(config, "is_admin", lambda uid: True)


async def _mk_journal(db, **kw):
    base = dict(label="job", plastic_id=None, plastic_name="PLA", grams=50, minutes=120, graded=0)
    base.update(kw)
    return await db.add_print_journal(**base)


async def _mk_request(db, status="new"):
    return await db.add_request(user_id=1, user_name="U", model_label="Дракон", printer="P",
                                plastic_name="PLA", quantity=1, quality="0.2", strength="норм",
                                grams_total=50, minutes_total=100, price=10, preview_path=None,
                                status=status)


async def test_chat_classify_dismisses_app_notification(tmp_db):
    """printwatch заводит карточку «чья печать?» на КАЖДУЮ печать, а гасил её раньше только
    экран мини-аппа. Чат — основной путь: без гашения бейдж «Уведомления» рос на 1 за печать
    и никогда не обнулялся, а живая кнопка «Выбрать заказ» на уже разобранной печати вела в
    повторную классификацию (дубль самопечатного заказа)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    jid = await _mk_journal(db, plastic_id=pid, label="калибровка")
    nid = await db.add_admin_notification(
        "print_classify", title="Печать завершена", body="?", payload={"jid": jid})

    await user_flow.admin_print_classify(_FakeCb(f"pe:self:{jid}"), _FakeState())

    left = [n["id"] for n in await db.list_admin_notifications(kind="print_classify")]
    assert nid not in left, "карточка должна погаснуть после разбора из чата"


async def test_chat_classify_order_dismisses_app_notification(tmp_db):
    """То же для ветки «подтвердить заказ» (pe:ord) — самый частый путь."""
    db = tmp_db
    rid = await _mk_request(db)
    jid = await _mk_journal(db, request_id=rid)
    nid = await db.add_admin_notification(
        "print_classify", title="Печать завершена", body="?", payload={"jid": jid})

    await user_flow.admin_print_classify(_FakeCb(f"pe:ord:{jid}:{rid}"), _FakeState())

    left = [n["id"] for n in await db.list_admin_notifications(kind="print_classify")]
    assert nid not in left


async def test_classify_self_creates_self_order_and_would_spend(tmp_db):
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    jid = await _mk_journal(db, plastic_id=pid, label="калибровка")
    cb = _FakeCb(f"pe:self:{jid}")
    await user_flow.admin_print_classify(cb, _FakeState())
    # заказ-самопечать создан, «было бы потрачено» > 0
    assert await db.admin_would_spend(cb.from_user.id) > 0
    j = await db.get_print_journal(jid)
    assert j["source"] == "self" and j["request_id"]        # журнал остался self, привязан к самозаказу
    r = await db.get_request(j["request_id"])
    assert r.status == "self"
    assert any("для себя" in a.lower() for a in cb.message.answers)          # подтверждение
    # чат-опрос качества выпилен (OpenTodo b02bc4c6): оценка — в «Журнале печатей» приложения
    assert not any("оцените" in a.lower() for a in cb.message.answers)


async def test_classify_bound_order_attaches_without_status_change(tmp_db):
    db = tmp_db
    rid = await _mk_request(db, status="new")
    jid = await _mk_journal(db)
    cb = _FakeCb(f"pe:ord:{jid}:{rid}")
    await user_flow.admin_print_classify(cb, _FakeState())
    assert (await db.get_print_journal(jid))["request_id"] == rid            # прикреплена к заказу
    assert (await db.get_request(rid)).status == "new"                        # статус НЕ изменён
    assert any("прикреплена к заказу" in a for a in cb.message.answers)
    # чат-опрос качества выпилен (OpenTodo b02bc4c6): оценка — в «Журнале печатей» приложения
    assert not any("оцените" in a.lower() for a in cb.message.answers)


async def test_classify_other_order_via_number(tmp_db):
    db = tmp_db
    rid = await _mk_request(db)
    jid = await _mk_journal(db)
    st = _FakeState()
    await user_flow.admin_print_classify(_FakeCb(f"pe:oth:{jid}"), st)
    assert await st.get_state() == PrintStat.order_no and st.data.get("pe_jid") == jid
    msg = _FakeMsg(text=f"#{rid}")                                            # ввод номера (с #)
    await user_flow.admin_print_order_no(msg, st)
    assert (await db.get_print_journal(jid))["request_id"] == rid
    assert await st.get_state() is None


async def test_classify_other_order_bad_number_stays_in_state(tmp_db):
    db = tmp_db
    jid = await _mk_journal(db)
    st = _FakeState()
    await st.update_data(pe_jid=jid, pe_name="job")
    await st.set_state(PrintStat.order_no)
    msg = _FakeMsg(text="абв")                                               # не число
    await user_flow.admin_print_order_no(msg, st)
    assert await st.get_state() == PrintStat.order_no                        # остались для переввода
    assert msg.answers and "номер" in msg.answers[0].lower()
    assert (await db.get_print_journal(jid))["request_id"] is None           # не привязано


async def test_classify_missing_journal_entry(tmp_db):
    cb = _FakeCb("pe:self:99999")                                            # запись не существует
    await user_flow.admin_print_classify(cb, _FakeState())
    assert any("не найдена" in a.lower() for a in cb.answers)                # мягкая ошибка, без падения
