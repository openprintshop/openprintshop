"""Миграция схемы v9 → v10 (Э0, очередь досчёта).

Строим базу состояния v9 — полная схема прошлой версии без таблиц очереди и без
requests.price_pending, user_version=9, живые заказы, — и прогоняем настоящий db.init():
таблицы deferred_calc / tg_seen / relay_events_seen появляются, колонка добавляется с 0 у
старых заказов (старые «без сметы» в очередь НЕ ставятся), данные целы, версия = 10, повторный
init идемпотентен.
"""
import pytest_asyncio

from app import config, db


async def _tables() -> set[str]:
    cur = await db._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in await cur.fetchall()}


async def _cols(table: str) -> set[str]:
    cur = await db._conn().execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in await cur.fetchall()}


async def _uv() -> int:
    cur = await db._conn().execute("PRAGMA user_version")
    return (await cur.fetchone())[0]


@pytest_asyncio.fixture
async def v9_db(tmp_path):
    """Файл БД в состоянии схемы v9: собираем текущую и откатываем ровно то, что добавил v10."""
    orig = config.DB_PATH
    config.DB_PATH = tmp_path / "v9.db"
    await db.init()
    c = db._conn()
    for t in ("deferred_calc", "tg_seen", "relay_events_seen"):
        await c.execute(f"DROP TABLE {t}")
    await c.execute("ALTER TABLE requests DROP COLUMN price_pending")
    await c.execute("PRAGMA user_version = 9")
    for i, (unsliced, status) in enumerate([(1, "new"), (0, "in_progress")], start=1):
        await c.execute(
            "INSERT INTO requests(id, user_id, user_name, model_label, price, status, unsliced, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (100 + i, 7, "u", f"старый #{i}", 0 if unsliced else 50.0, status, unsliced, 1, 1))
    await c.commit()
    await db.close()
    try:
        yield config.DB_PATH
    finally:
        await db.close()
        config.DB_PATH = orig


async def test_v9_to_v10(v9_db):
    await db.init()
    # v10 — очередь досчёта; поверх неё тот же init доводит до v11 (VACUUM под копию базы).
    assert await _uv() == 11 == db.SCHEMA_VERSION
    assert {"deferred_calc", "tg_seen", "relay_events_seen"} <= await _tables()
    assert {"id", "rid", "kind", "source", "params", "status", "claim_token", "attempts",
            "next_try_at", "error", "client_req_id", "created_at", "updated_at"} \
        <= await _cols("deferred_calc")
    assert "price_pending" in await _cols("requests")
    old = await db.get_request(101)
    assert old.model_label == "старый #1" and old.unsliced == 1
    assert old.price_pending == 0, "старые заказы «без сметы» в очередь не ставим"
    assert (await db.get_request(102)).price == 50.0
    assert (await db.deferred_counts())["queued"] == 0
    # новая схема рабочая сразу после миграции
    assert await db.tg_seen_mark(1) is True
    await db.enqueue_deferred(101, source={"type": "request", "id": 101}, params={"quick": True},
                              client_req_id="now:101")
    assert (await db.get_request(101)).price_pending == 1


async def test_v10_init_is_idempotent(v9_db):
    await db.init()
    await db.enqueue_deferred(101, source={}, params={}, client_req_id="k")
    await db.close()
    await db.init()
    assert await _uv() == db.SCHEMA_VERSION
    assert len(await db.deferred_for_request(101)) == 1
    assert (await db.get_request(101)).price_pending == 1
