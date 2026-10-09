"""Тест переписки по заказу (тред клиент↔админ, 9d222a19)."""


async def _mk(db, uid=1):
    return await db.add_request(
        user_id=uid, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None)


async def test_thread_roundtrip(tmp_db):
    db = tmp_db
    rid = await _mk(db, uid=42)
    await db.add_order_message(rid, from_admin=False, text="какой цвет?")
    await db.add_order_message(rid, from_admin=True, text="синий есть")
    msgs = await db.list_order_messages(rid)
    assert [m["text"] for m in msgs] == ["какой цвет?", "синий есть"]
    assert [m["from_admin"] for m in msgs] == [0, 1]


async def test_feedback_notifies_admins(tmp_db, monkeypatch):
    """OpenTodo e6ef5ffb: оценка и отзыв клиента должны слать уведомление админам."""
    from app import notify
    from app.handlers import user_flow
    db = tmp_db
    rid = await _mk(db, uid=7)
    sent = []

    async def fake_notify(bot, text, **kw):
        sent.append(text)

    monkeypatch.setattr(notify, "notify_admins", fake_notify)

    await db.update_request(rid, rating=9)
    await user_flow._notify_admins_feedback(object(), rid, rating=9)
    await db.update_request(rid, review="всё супер")
    await user_flow._notify_admins_feedback(object(), rid, review="всё супер")

    assert len(sent) == 2
    assert "оценил" in sent[0] and "9/10" in sent[0]
    assert "Отзыв" in sent[1] and "всё супер" in sent[1]


async def test_unread_counters(tmp_db):
    db = tmp_db
    rid = await _mk(db, uid=42)
    await db.add_order_message(rid, from_admin=False, text="привет")       # клиент написал
    assert await db.orders_unread_admin() == {rid: 1}                      # админ не читал
    assert await db.orders_unread_user(42) == {}                           # своё не в счётчике
    await db.mark_order_read(rid, by_admin=True)
    assert await db.orders_unread_admin() == {}
    await db.add_order_message(rid, from_admin=True, text="здравствуйте")  # админ ответил
    assert await db.orders_unread_user(42) == {rid: 1}                     # клиент не читал
    assert await db.orders_unread_admin() == {}                           # ответ админа — не админу
    await db.mark_order_read(rid, by_admin=False)
    assert await db.orders_unread_user(42) == {}
