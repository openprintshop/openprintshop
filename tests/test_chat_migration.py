"""Миграция переписки (user_version 5): сообщения вне заказа + вложения.

Главный риск — пересоздание таблицы order_messages ради снятия NOT NULL с request_id:
в боевой базе там лежит реальная переписка с клиентами, потерять её нельзя.
Поэтому тест строит СТАРУЮ схему, прогоняет db.init() и проверяет, что всё уцелело.
"""
import sqlite3

import pytest

from app import config, db

OLD_SCHEMA = """
CREATE TABLE order_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL,
  from_admin INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  read_user INTEGER NOT NULL DEFAULT 0,
  read_admin INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_omsg_req ON order_messages(request_id, id);
"""


@pytest.fixture
def old_db(tmp_path):
    """БД со СТАРОЙ таблицей переписки и парой сообщений в ней."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.executescript("""
        CREATE TABLE requests(id INTEGER PRIMARY KEY, user_id INTEGER, model_label TEXT);
        INSERT INTO requests(id, user_id, model_label) VALUES(13, 777, 'Card Shuffler');
    """)
    con.executemany(
        "INSERT INTO order_messages(id, request_id, from_admin, text, created_at, read_user, "
        "read_admin) VALUES(?,?,?,?,?,?,?)",
        [(1, 13, 0, 'Когда будет готово?', 1000, 1, 0),
         (2, 13, 1, 'Завтра к обеду', 1100, 0, 1)])
    con.commit()
    con.close()
    return path


async def test_old_messages_survive_migration(old_db, monkeypatch):
    """Пересоздание таблицы не теряет ни одного сообщения и сохраняет флаги прочтения."""
    monkeypatch.setattr(config, "DB_PATH", old_db)
    await db.init()
    try:
        rows = await db.list_order_messages(13)
        assert [r["text"] for r in rows] == ['Когда будет готово?', 'Завтра к обеду']
        assert [r["from_admin"] for r in rows] == [0, 1]
        # непрочитанное админом сообщение клиента осталось непрочитанным
        assert (await db.orders_unread_admin()).get(13) == 1
    finally:
        await db.close()


async def test_user_id_backfilled_from_orders(old_db, monkeypatch):
    """user_id у старых сообщений проставляется из заказа — иначе общий чат клиента пуст."""
    monkeypatch.setattr(config, "DB_PATH", old_db)
    await db.init()
    try:
        msgs = await db.list_client_messages(777)
        assert len(msgs) == 2, "переписка по заказам обязана попадать в общий чат клиента"
        assert all(m["request_id"] == 13 for m in msgs)
        assert msgs[0]["order_label"] == 'Card Shuffler'
    finally:
        await db.close()


async def test_migration_is_idempotent(old_db, monkeypatch):
    """Второй запуск бота не должен ни падать, ни дублировать сообщения."""
    monkeypatch.setattr(config, "DB_PATH", old_db)
    await db.init()
    await db.close()
    await db.init()
    try:
        assert len(await db.list_order_messages(13)) == 2
    finally:
        await db.close()


async def test_message_outside_order(tmp_db):
    """Сообщение вне заказа сохраняется и попадает в общий чат клиента."""
    mid = await tmp_db.add_client_message(777, from_admin=True, text='Как вам печать?')
    assert mid
    msgs = await tmp_db.list_client_messages(777)
    assert len(msgs) == 1
    assert msgs[0]["request_id"] is None
    # своё сообщение админа не считается непрочитанным для админа
    assert 777 not in await tmp_db.clients_unread_admin()


async def test_client_unread_counts_both_kinds(tmp_db):
    """Непрочитанное клиента считается и по заказам, и вне заказов."""
    await tmp_db.add_client_message(777, from_admin=False, text='вопрос вне заказа')
    await tmp_db.add_order_message(0, from_admin=False, text='вопрос по заказу', user_id=777)
    assert (await tmp_db.clients_unread_admin()).get(777) == 2
    await tmp_db.mark_client_read(777, by_admin=True)
    assert 777 not in await tmp_db.clients_unread_admin()


async def test_search_messages_finds_text(tmp_db):
    await tmp_db.add_client_message(777, from_admin=False, text='нужен PETG синий')
    hits = await tmp_db.search_messages('petg')
    assert len(hits) == 1 and hits[0]["user_id"] == 777


async def test_image_attachment_roundtrip(tmp_db):
    mid = await tmp_db.add_client_message(777, from_admin=True, text='', image_path='/tmp/a.jpg')
    msg = await tmp_db.get_message(mid)
    assert msg["image_path"] == '/tmp/a.jpg'
    await tmp_db.set_message_image(mid, '/tmp/b.jpg')
    assert (await tmp_db.get_message(mid))["image_path"] == '/tmp/b.jpg'
