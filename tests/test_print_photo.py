"""Обязательное фото при завершении печати (OpenTodo 7bb76b35): любая оценка в Telegram-опросе
сначала требует фото результата; «🕒 Позже» — выход без фото (печать остаётся неоценённой)."""
from pathlib import Path

import pytest

from app import config, preview
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


class _FakeBot:
    async def download(self, file, destination):
        Path(destination).write_bytes(b"\xff\xd8jpeg")


class _FakeMsg:
    def __init__(self, photo=None, chat_id=1):
        self.photo = photo
        self.chat = type("C", (), {"id": chat_id})()
        self.message_id = 555
        self.bot = _FakeBot()
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
def _patch_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_admin", lambda uid: True)
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path)
    monkeypatch.setattr(preview, "optimize_photo", lambda p: None)
    monkeypatch.setattr(user_flow, "_lcd_handled", lambda: None)


async def _entry(db):
    return await db.add_print_journal(label="Брелок", plastic_id=None, plastic_name="",
                                      grams=0, minutes=10, graded=0)


async def test_success_requires_photo_then_grades(tmp_db):
    db = tmp_db
    jid = await _entry(db)
    st = _FakeState()
    # «✅ Успех» → ещё НЕ оценено, ждём фото
    await user_flow.admin_print_journal_survey(_FakeCb(f"pj:ok:{jid}"), st)
    assert await st.get_state() == PrintStat.photo
    assert (await db.get_print_journal(jid))["graded"] == 0
    assert st.data.get("pj_action") == "ok"
    # прислали фото → оценка зафиксирована, фото сохранено
    await user_flow.printstat_photo(_FakeMsg(photo=[object()]), st)
    e = await db.get_print_journal(jid)
    assert e["graded"] == 1
    assert e["done_photo"] and "jphoto_" in e["done_photo"]
    assert await st.get_state() is None


async def test_fail_saves_photo_then_asks_reason(tmp_db):
    db = tmp_db
    jid = await _entry(db)
    st = _FakeState()
    await user_flow.admin_print_journal_survey(_FakeCb(f"pj:fail:{jid}"), st)
    assert await st.get_state() == PrintStat.photo
    await user_flow.printstat_photo(_FakeMsg(photo=[object()]), st)
    e = await db.get_print_journal(jid)
    assert e["done_photo"]              # фото сохранено
    assert e["graded"] == 0            # оценка ещё ждёт причину
    assert await st.get_state() == PrintStat.reason
    assert st.data.get("pj_pct") == 100


async def test_non_photo_blocks_in_photo_step(tmp_db):
    db = tmp_db
    jid = await _entry(db)
    st = _FakeState()
    await st.update_data(pj_jid=jid, pj_action="ok")
    await st.set_state(PrintStat.photo)
    msg = _FakeMsg(photo=None)          # прислали текст/стикер вместо фото
    await user_flow.printstat_photo_required(msg, st)
    assert msg.answers and "фото" in msg.answers[0].lower()
    assert (await db.get_print_journal(jid))["graded"] == 0


async def test_later_clears_photo_step_without_grading(tmp_db):
    db = tmp_db
    jid = await _entry(db)
    st = _FakeState()
    await st.set_state(PrintStat.photo)
    await user_flow.admin_print_journal_survey(_FakeCb(f"pj:later:{jid}"), st)
    assert await st.get_state() is None
    assert (await db.get_print_journal(jid))["graded"] == 0   # осталась неоценённой, без фото
