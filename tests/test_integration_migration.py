"""ФИНАЛЬНЫЙ ГЕЙТ на пересечении 6 фич (A5, B3, B4, B2, B5, B1): миграция + boot.

Каждая фича ревьюилась против состояния на СВОЙ момент; здесь проверяем их СОВМЕСТНУЮ
целостность на уровне схемы и одноразовых миграций:

  * свежая БД содержит ВСЮ новую схему всех фич сразу (A5 orders.nudged_at; B3 requests.urgency
    + requests.promised_date; B4 postproc_options + request_options; B5 order_groups) и
    PRAGMA user_version == SCHEMA_VERSION (B5 подняла до 6, снос чата сообщества — до 7);
  * PRE-batch БД (user_version=5, нет строк order_groups, но у requests уже есть order_group)
    после db.init() получает шапку order_groups через v6-бэкфилл, версия становится 6, а
    повторный init() идемпотентен (без дублей, версия не меняется).

Импорт/boot-целостность (нет циклических импортов, run.py стартует abandonednudge) проверяется
отдельно шелл-командой в отчёте субагента, не здесь.
"""
import json

import pytest_asyncio

from app import config, db


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    """Свежая БД во временном файле; после теста закрыть и вернуть путь."""
    orig = config.DB_PATH
    config.DB_PATH = tmp_path / "mig.db"
    await db.init()
    try:
        yield db
    finally:
        await db.close()
        config.DB_PATH = orig


async def _table_columns(table: str) -> set[str]:
    cur = await db._conn().execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in await cur.fetchall()}


async def _table_exists(name: str) -> bool:
    cur = await db._conn().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return await cur.fetchone() is not None


# Версия схемы — из кода, а не числом в тесте: иначе каждая миграция роняла бы этот файл.
SCHEMA_VERSION = db.SCHEMA_VERSION


async def _user_version() -> int:
    cur = await db._conn().execute("PRAGMA user_version")
    return (await cur.fetchone())[0]


# ─────────────────────────── 1. Свежая БД: вся схема + версия ───────────────────────────
async def test_fresh_db_has_all_new_schema(fresh_db):
    # A5 — догон брошенных расчётов
    assert "nudged_at" in await _table_columns("orders"), "A5: нет orders.nudged_at"

    # B3 — срочность и обещанный срок
    req_cols = await _table_columns("requests")
    assert "urgency" in req_cols, "B3: нет requests.urgency"
    assert "promised_date" in req_cols, "B3: нет requests.promised_date"

    # B4 — опции постобработки (каталог + снимок на заказ)
    assert await _table_exists("postproc_options"), "B4: нет таблицы postproc_options"
    assert await _table_exists("request_options"), "B4: нет таблицы request_options"
    ppo = await _table_columns("postproc_options")
    assert {"id", "name", "price", "is_active", "sort"} <= ppo, f"B4 postproc_options: {ppo}"
    ro = await _table_columns("request_options")
    assert {"request_id", "option_id", "name", "price"} <= ro, f"B4 request_options: {ro}"

    # B5 — заказ-корзина (шапка группы)
    assert await _table_exists("order_groups"), "B5: нет таблицы order_groups"
    og = await _table_columns("order_groups")
    assert {"id", "user_id", "total_snapshot", "created_at"} <= og, f"B5 order_groups: {og}"

    # только B5 двигает версию
    assert await _user_version() == SCHEMA_VERSION, "версия схемы отстала от миграций"


async def test_fresh_db_settings_defaults_present(fresh_db):
    """Новые настройки всех фич доезжают в settings при первом init (не ломают загрузку)."""
    s = await db.get_settings()
    for key in ("nudge_abandoned_enabled", "nudge_abandoned_hours", "nudge_abandoned_max_days",
                "rush_surcharge_pct", "term_split_hours", "dfm_min_wall_mm"):
        assert key in s, f"нет дефолта настройки {key}"


# ─────────────────────────── 2. PRE-batch → v6 бэкфилл + идемпотентность ───────────────────────────
async def test_v6_backfill_creates_group_header_and_is_idempotent(tmp_path):
    orig = config.DB_PATH
    config.DB_PATH = tmp_path / "prebatch.db"
    gid = "grpmig000001"
    try:
        # (a) свежая БД, затем сеем cart-позицию с общим order_group
        await db.init()
        await db.add_request(
            user_id=555, user_name="Мигратор", model_label="Кубик", printer="Centauri",
            plastic_name="Test PLA", quantity=1, quality="0.20", strength="Стандарт",
            grams_total=20.0, minutes_total=100.0, price=12.5, preview_path=None,
            order_group=gid)
        await db.add_request(
            user_id=555, user_name="Мигратор", model_label="Шестерёнка", printer="Centauri",
            plastic_name="Test PLA", quantity=1, quality="0.20", strength="Стандарт",
            grams_total=10.0, minutes_total=60.0, price=7.5, preview_path=None,
            order_group=gid)

        # (b) откатываем в PRE-batch состояние: нет шапок order_groups, версия 5
        await db._conn().execute("DELETE FROM order_groups")
        await db._conn().execute("PRAGMA user_version = 5")
        await db._conn().commit()
        assert await _user_version() == 5
        cur = await db._conn().execute("SELECT COUNT(*) c FROM order_groups")
        assert (await cur.fetchone())["c"] == 0

        # (c) повторный init → v6-бэкфилл заводит шапку, версия становится 6
        await db.close()
        await db.init()
        assert await _user_version() == SCHEMA_VERSION, "v6-бэкфилл не догнал версию схемы"
        hdr = await db.get_order_group(gid)
        assert hdr is not None, "v6-бэкфилл не создал шапку order_groups"
        assert hdr["user_id"] == 555
        assert abs(float(hdr["total_snapshot"]) - 20.0) < 1e-6, "итог != сумме цен позиций (12.5+7.5)"

        # (d) второй init идемпотентен: без дублей, версия та же
        await db.close()
        await db.init()
        assert await _user_version() == SCHEMA_VERSION
        cur = await db._conn().execute(
            "SELECT COUNT(*) c FROM order_groups WHERE id=?", (gid,))
        assert (await cur.fetchone())["c"] == 1, "повторный init продублировал шапку order_groups"
    finally:
        await db.close()
        config.DB_PATH = orig


async def test_v6_backfill_ignores_null_and_empty_group(tmp_path):
    """Одиночные заказы (order_group NULL или '') НЕ схлопываются в фейковую мега-группу."""
    orig = config.DB_PATH
    config.DB_PATH = tmp_path / "prebatch2.db"
    try:
        await db.init()
        # одиночные заказы: NULL-группа (по умолчанию) и явная пустая строка
        await db.add_request(
            user_id=1, user_name="A", model_label="m1", printer="P", plastic_name="PLA",
            quantity=1, quality="0.20", strength="S", grams_total=1.0, minutes_total=1.0,
            price=5.0, preview_path=None)  # order_group None
        await db.add_request(
            user_id=2, user_name="B", model_label="m2", printer="P", plastic_name="PLA",
            quantity=1, quality="0.20", strength="S", grams_total=1.0, minutes_total=1.0,
            price=6.0, preview_path=None, order_group="")  # пустая строка

        await db._conn().execute("DELETE FROM order_groups")
        await db._conn().execute("PRAGMA user_version = 5")
        await db._conn().commit()

        await db.close()
        await db.init()
        assert await _user_version() == SCHEMA_VERSION
        cur = await db._conn().execute("SELECT COUNT(*) c FROM order_groups")
        assert (await cur.fetchone())["c"] == 0, "NULL/'' группы не должны порождать шапок"
    finally:
        await db.close()
        config.DB_PATH = orig


# ─────────────────────────── 3. v8 (C2): пути в БД → относительные ───────────────────────────
async def test_v8_paths_migrate_to_relative_and_is_idempotent(tmp_path):
    """Шаг v8 переписывает пути под DATA_DIR в относительный вид.

    Проверяем ровно те грабли, из-за которых миграция «по содержимому» была бы опасна:
    JSON-список переписывается поэлементно, битый JSON пропускается (а не роняет старт бота),
    путь ВНЕ data/ (профиль слайсера) остаётся байт-в-байт, свободный текст журнала не
    трогается вовсе, а повторный прогон ничего не меняет.
    """
    orig = config.DB_PATH
    config.DB_PATH = tmp_path / "paths.db"
    d = str(config.DATA_DIR)
    alien = r"C:\Program Files\ElegooSlicer\resources\profiles\Elegoo\x.json"
    # Профиль ЗАВЕДОМО вне корней установки любого слайсера: с шагом v9 (хвост C2)
    # значения под корнями переезжают в маркер (@elegoo/resources/…), и путь из
    # Program Files доказывал бы уже не то — v8 его не трогает, но следом трогает v9.
    alien_prof = r"D:\чужие профили\slicer\x.json"
    try:
        await db.init()
        rid = await db.add_request(
            user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
            quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1, price=1,
            preview_path=None)
        jid = await db.add_print_journal(label="j", plastic_id=None, plastic_name="x",
                                         grams=1, minutes=1)
        cid = await db.add_to_collection(hash="hh", label="К", paths=[], preview_path=None,
                                         added_by=1)
        c = db._conn()
        # (a) состояние ДО C2: абсолютные пути старого вида, JSON-список, битый JSON
        await c.execute(
            "UPDATE requests SET preview_path=?, file_path=?, done_photo=?, timelapse=?, "
            "slicer_shot=?, photos=? WHERE id=?",
            (d + r"\previews\p.png", d + r"\downloads\m.3mf", d + r"\collection_photos\done.jpg",
             d + r"\timelapses\t.mp4", alien,
             json.dumps([d + r"\custom_orders\a.jpg", alien]), rid))
        await c.execute("UPDATE print_journal SET done_photo=?, timelapse=? WHERE id=?",
                        (d + r"\collection_photos\j.jpg", d + r"\timelapses\auto\j.mp4", jid))
        await c.execute("UPDATE collection SET preview_path=?, paths=? WHERE id=?",
                        (d + r"\previews\c.png", "{битый json", cid))
        await c.execute(
            "INSERT INTO activity_log(user_id, action, detail, created_at) VALUES(?,?,?,?)",
            (1, "c2test", "не открылся файл " + d + r"\previews\p.png", 1))
        await c.execute("UPDATE plastics SET filament_profile=? "
                        "WHERE id=(SELECT MIN(id) FROM plastics)", (alien_prof,))
        await c.execute("PRAGMA user_version = 7")
        await c.commit()

        # (b) повторный init → шаг v8
        await db.close()
        await db.init()
        assert await _user_version() == SCHEMA_VERSION

        async def one(sql, *a):
            cur = await db._conn().execute(sql, a)
            row = await cur.fetchone()
            return row[0] if row else None

        assert await one("SELECT preview_path FROM requests WHERE id=?", rid) == "previews/p.png"
        assert await one("SELECT file_path FROM requests WHERE id=?", rid) == "downloads/m.3mf"
        assert await one("SELECT done_photo FROM requests WHERE id=?", rid) \
            == "collection_photos/done.jpg"
        assert await one("SELECT timelapse FROM requests WHERE id=?", rid) == "timelapses/t.mp4"
        assert await one("SELECT slicer_shot FROM requests WHERE id=?", rid) == alien, \
            "путь ВНЕ data/ обязан остаться дословным"
        assert json.loads(await one("SELECT photos FROM requests WHERE id=?", rid)) \
            == ["custom_orders/a.jpg", alien], "JSON-список переписывается поэлементно"
        assert await one("SELECT done_photo FROM print_journal WHERE id=?", jid) \
            == "collection_photos/j.jpg"
        assert await one("SELECT timelapse FROM print_journal WHERE id=?", jid) \
            == "timelapses/auto/j.mp4"
        assert await one("SELECT preview_path FROM collection WHERE id=?", cid) == "previews/c.png"
        assert await one("SELECT paths FROM collection WHERE id=?", cid) == "{битый json", \
            "битый JSON пропускается, а не портится и не роняет старт"
        assert d in await one("SELECT detail FROM activity_log WHERE action='c2test'"), \
            "свободный текст журнала миграция трогать не должна"
        assert await one("SELECT filament_profile FROM plastics "
                         "WHERE filament_profile IS NOT NULL ORDER BY id LIMIT 1") == alien_prof, \
            "профиль слайсера — другой трек, C2 (v8) его не касается"

        # (c) второй init идемпотентен
        await db.close()
        await db.init()
        assert await _user_version() == SCHEMA_VERSION
        assert await one("SELECT preview_path FROM requests WHERE id=?", rid) == "previews/p.png"
    finally:
        await db.close()
        config.DB_PATH = orig
