"""Резервные копии БД (OpenTodo b557e341): снимок, целостность, ротация, журнал, восстановление.

Один потерянный архив клиента = репутационная смерть, поэтому здесь проверяется не «функция
не упала», а свойства данных: копия открывается, integrity_check зелёный, число строк совпадает,
незакоммиченная транзакция в снимок не протекает, ротация не съедает ручные копии, а снимок
реально годится как рабочая БД (db.init() поверх него проходит).
"""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import backup, config, db, errorlog, notify, roles, webapp
from app.store import ops


async def _seed(n: int) -> None:
    """n заявок в текущей БД — чтобы было что искать в снимке."""
    for i in range(n):
        await db.add_request(
            user_id=100 + i, user_name=f"Клиент {i}", model_label=f"Деталь {i}",
            printer="Centauri", plastic_name="Test PLA", quantity=1, quality="0.20",
            strength="Стандарт", grams_total=10.0 + i, minutes_total=30.0,
            price=5.0 + i, preview_path=None)


async def _count(table: str) -> int:
    cur = await db._conn().execute(f"SELECT COUNT(*) c FROM {table}")
    return (await cur.fetchone())["c"]


def _snapshot_count(path: str, table: str) -> int:
    """Читаем снимок ГОЛЫМ sqlite3 — так же, как это будет делать владелец при восстановлении."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _integrity(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


# ─────────────────────────── (а) снимок живой БД ───────────────────────────
async def test_snapshot_is_valid_and_complete(tmp_db, tmp_path):
    await _seed(4)
    expected = await _count("requests")

    res = backup._backup_sync(config.DB_PATH, tmp_path / "bk", 5)

    assert res["ok"], res["error"]
    assert res["size"] > 0 and res["duration_ms"] >= 0
    assert _integrity(res["path"]) == "ok"
    assert _snapshot_count(res["path"], "requests") == expected == 4
    # .partial не остаётся: в каталоге ровно один файл — опубликованный снимок
    assert [p.name for p in (tmp_path / "bk").iterdir()] == [Path(res["path"]).name]


# ─────────────────── (б) снимок при открытой незакоммиченной транзакции ───────────────────
async def test_snapshot_skips_uncommitted_transaction(tmp_db, tmp_path):
    """Онлайн-бэкап обязан видеть последнее ЗАКОММИЧЕННОЕ состояние, а не грязное."""
    await _seed(2)
    committed = await _count("requests")

    # незакоммиченная запись на живом aiosqlite-соединении (isolation_level по умолчанию —
    # неявный BEGIN на первом INSERT, commit не зовём)
    await db._conn().execute(
        "INSERT INTO requests(user_id, user_name, model_label, printer, plastic_name, quantity, "
        "quality, strength, grams_total, minutes_total, price, created_at, updated_at) "
        "VALUES(999, 'Призрак', 'Не должно попасть в бэкап', 'P', 'PLA', 1, '0.20', 'S', 1, 1, 1, 0, 0)")
    try:
        res = backup._backup_sync(config.DB_PATH, tmp_path / "bk", 5)
        assert res["ok"], res["error"]
        assert _integrity(res["path"]) == "ok"
        assert _snapshot_count(res["path"], "requests") == committed, "грязная транзакция утекла в снимок"
    finally:
        await db._conn().rollback()


# ─────────────────────────── (в) ротация только по регулярке ───────────────────────────
async def test_rotation_keeps_manual_and_garbage(tmp_db, tmp_path):
    dst = tmp_path / "bk"
    dst.mkdir()
    old = [f"bot_2020010{i}_000001.db" for i in range(1, 6)]     # 5 «старых» автоснимков
    keepers = ["bot_pre_manual.db", "bot_abc.db", "bot_pre_2026_merge.db", "notes.txt"]
    for name in old + keepers:
        (dst / name).write_text("x", encoding="utf-8")

    res = backup._backup_sync(config.DB_PATH, dst, 3)
    assert res["ok"], res["error"]

    left = {p.name for p in dst.iterdir()}
    # keep=3 считается ТОЛЬКО по автоснимкам: свежий + два последних старых
    snaps = sorted(n for n in left if backup.SNAPSHOT_RE.match(n))
    assert len(snaps) == 3, snaps
    assert snaps[:2] == ["bot_20200104_000001.db", "bot_20200105_000001.db"]
    assert snaps[2] == Path(res["path"]).name
    # ручные копии и посторонние файлы не тронуты
    assert set(keepers) <= left, left


async def test_rotation_disabled_when_keep_zero(tmp_db, tmp_path):
    dst = tmp_path / "bk"
    dst.mkdir()
    (dst / "bot_20200101_000001.db").write_text("x", encoding="utf-8")
    assert backup.rotate(dst, 0) == 0
    assert (dst / "bot_20200101_000001.db").exists()


async def test_name_collision_gets_suffix(tmp_db, tmp_path):
    """Два снимка в одну секунду не должны затирать друг друга."""
    dst = tmp_path / "bk"
    first = backup._backup_sync(config.DB_PATH, dst, 5)
    second = backup._backup_sync(config.DB_PATH, dst, 5)
    assert first["ok"] and second["ok"]
    assert first["path"] != second["path"]
    assert len([p for p in dst.iterdir() if backup.SNAPSHOT_RE.match(p.name)]) == 2


# ─────────────────────────── (г) битый источник не публикуется ───────────────────────────
async def test_broken_source_publishes_nothing(tmp_path):
    src = tmp_path / "notadb.db"
    src.write_bytes(b"\x00 \xff not a sqlite database at all " * 64)
    dst = tmp_path / "bk"

    res = backup._backup_sync(src, dst, 5)

    assert not res["ok"] and res["error"]
    assert not list(dst.glob("bot_*.db")), "опубликован снимок из мусора"
    assert not (dst / backup.PARTIAL_NAME).exists(), ".partial не подчищен"


async def test_missing_source_is_reported(tmp_path):
    res = backup._backup_sync(tmp_path / "нет.db", tmp_path / "bk", 5)
    assert not res["ok"] and "не найдена" in res["error"]


# ───────────────── (г2) битая КОПИЯ не публикуется: проверка целостности ─────────────────
class _Cur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _BadCheckConn:
    """Соединение-проверяльщик, которому integrity_check возвращает битый вердикт."""

    def __init__(self, conn, verdict):
        self._conn, self._verdict = conn, verdict

    def execute(self, sql, *a):
        if "integrity_check" in sql:
            return _Cur((self._verdict,))
        return self._conn.execute(sql, *a)

    def close(self):
        self._conn.close()


class _Sqlite3Shim:
    """Шим модуля sqlite3 для app/backup.py: второе открытие .partial (это `chk`) отдаёт
    вердикт «копия битая». Без шва эту ветку не достать — conn.backup() битых копий не
    делает, — а она и есть главная защита: «публикуем только проверенное»."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.partial_opens = 0

    def connect(self, path, *a, **kw):
        conn = sqlite3.connect(path, *a, **kw)
        if str(path).endswith(backup.PARTIAL_NAME):
            self.partial_opens += 1
            if self.partial_opens == 2:
                return _BadCheckConn(conn, self.verdict)
        return conn


async def test_corrupt_copy_is_not_published(monkeypatch, tmp_db, tmp_path):
    """Копия не прошла integrity_check → снимка в каталоге нет, .partial убран, провал в res.
    Без этой проверки в data/backups копились бы файлы, о негодности которых узнают только
    в момент восстановления."""
    dst = tmp_path / "bk"
    shim = _Sqlite3Shim("*** in database main ***\nPage 5 is never used")
    monkeypatch.setattr(backup, "sqlite3", shim)

    res = backup._backup_sync(config.DB_PATH, dst, 5)

    assert shim.partial_opens == 2, "шим не сработал — тест ничего не проверил"
    assert res["ok"] is False
    assert "integrity_check" in res["error"] and "Page 5" in res["error"]
    assert not list(dst.glob("bot_*.db")), "опубликована непроверенная копия"
    assert not (dst / backup.PARTIAL_NAME).exists(), ".partial не подчищен"
    assert not res["path"]


class _RacingSqlite3Shim:
    """Имя .partial фиксированное, поэтому параллельный прогон (кнопка «Сделать бэкап
    сейчас» во время планового) успевает снести копию между её созданием и проверкой.
    sqlite3.connect тут же заводит файл заново — ПУСТЫМ."""

    def __init__(self):
        self.partial_opens = 0

    def connect(self, path, *a, **kw):
        if str(path).endswith(backup.PARTIAL_NAME):
            self.partial_opens += 1
            if self.partial_opens == 2:
                Path(path).unlink(missing_ok=True)
        return sqlite3.connect(path, *a, **kw)


async def test_zero_size_copy_is_not_published(monkeypatch, tmp_db, tmp_path):
    """Пустой файл проходит integrity_check («ok»), так что одной проверки целостности мало:
    0-байтовый «снимок» опубликовался бы и вытеснил ротацией настоящий."""
    dst = tmp_path / "bk"
    shim = _RacingSqlite3Shim()
    monkeypatch.setattr(backup, "sqlite3", shim)

    res = backup._backup_sync(config.DB_PATH, dst, 5)

    assert shim.partial_opens == 2, "шим не сработал — тест ничего не проверил"
    assert res["ok"] is False and "нулевого размера" in res["error"]
    assert not list(dst.glob("bot_*.db"))
    assert not (dst / backup.PARTIAL_NAME).exists()


async def test_leftover_partial_is_overwritten(tmp_db, tmp_path):
    """Остаток .partial от прерванного запуска не должен блокировать следующий бэкап."""
    dst = tmp_path / "bk"
    dst.mkdir()
    (dst / backup.PARTIAL_NAME).write_text("обрывок прошлой копии", encoding="utf-8")
    res = backup._backup_sync(config.DB_PATH, dst, 5)
    assert res["ok"], res["error"]
    assert not (dst / backup.PARTIAL_NAME).exists()


# ─────────────────────────── (д) восстановление из снимка ───────────────────────────
async def test_restored_snapshot_boots_and_keeps_data(tmp_db, tmp_path):
    """Снимок подставляем как рабочую БД: db.init() поверх него проходит (миграции идемпотентны)."""
    await _seed(3)
    expected = await _count("requests")
    res = backup._backup_sync(config.DB_PATH, tmp_path / "bk", 5)
    assert res["ok"], res["error"]

    await db.close()
    restored = tmp_path / "restored"
    restored.mkdir()
    shutil.copy2(res["path"], restored / "bot.db")
    config.DB_PATH = restored / "bot.db"          # фикстура tmp_db вернёт исходный путь

    await db.init()
    assert await _count("requests") == expected
    # Снимок открывается ТЕКУЩИМ кодом: версия схемы — та же, что у живой БД. Числа тут
    # не пишем, иначе каждая новая миграция роняла бы тест про восстановление.
    cur = await db._conn().execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == db.SCHEMA_VERSION, \
        "версия схемы снимка не совпала с текущей"
    cur = await db._conn().execute("PRAGMA integrity_check")
    assert (await cur.fetchone())[0] == "ok"


# ─────────────────────────── (е) журнал backup_runs ───────────────────────────
async def test_backup_runs_journal(tmp_db):
    assert await db.last_backup_run() is None

    await db.add_backup_run(True, r"data\backups\bot_20260802_120000.db", 41_000_000, 640)
    await db.add_backup_run(False, "", 0, 12, "integrity_check копии: page 3 is never used")

    last = await db.last_backup_run()
    assert last["ok"] == 0 and "integrity_check" in last["error"]
    runs = await db.list_backup_runs(10)
    assert len(runs) == 2 and runs[1]["ok"] == 1 and runs[1]["size"] == 41_000_000
    assert runs[1]["duration_ms"] == 640 and runs[1]["ts"]


async def test_backup_runs_pruned_to_200(tmp_db):
    for i in range(205):
        await db.add_backup_run(True, f"bot_{i}.db", i, i)
    cur = await db._conn().execute("SELECT COUNT(*) c FROM backup_runs")
    assert (await cur.fetchone())["c"] == db._BACKUP_RUNS_KEEP == 200
    # обрезаются САМЫЕ СТАРЫЕ: свежая запись на месте. Имя, а не полный путь: журнал
    # хранит путь относительно DATA_DIR (C2), а наружу отдаётся абсолютный.
    assert Path((await db.last_backup_run())["path"]).name == "bot_204.db"


async def test_add_backup_run_never_raises(monkeypatch, tmp_db):
    """Сбой журнала не имеет права сорвать бэкап — функция глотает исключение.

    Подмена делается В МОДУЛЕ-ДОМЕ (волна 6 разреза db: журнал бэкапов живёт в
    app/store/ops.py и зовёт `_conn()` голым именем, разрешая его в СВОЁМ пространстве).
    Через `db._conn` подмена до тела больше не достаёт — тест прошёл бы вхолостую,
    не вызвав исключения вовсе, то есть перестал бы проверять то, ради чего написан.
    """
    def boom(*a, **kw):
        raise RuntimeError("БД недоступна")
    monkeypatch.setattr(ops, "_conn", boom)
    with pytest.raises(RuntimeError):                    # подмена реально доехала до тела
        ops._conn()
    await db.add_backup_run(True, "x.db", 1, 1)          # не должно бросить


# ─────────────────────── (ж) backup_once: журнал + алерт при провале ───────────────────────
async def test_backup_once_success_writes_run(monkeypatch, tmp_db, tmp_path):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr(config, "BACKUP_KEEP", 3)
    await _seed(2)

    res = await backup.backup_once()

    assert res["ok"], res["error"]
    run = await db.last_backup_run()
    assert run["ok"] == 1 and run["path"] == res["path"] and run["size"] == res["size"]


async def test_backup_once_failure_alerts_and_logs(monkeypatch, tmp_db, tmp_path):
    sent = []

    async def fake_alert(text, **kw):
        sent.append((text, kw.get("key")))

    monkeypatch.setattr(notify, "alert", fake_alert)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "bk")
    # источник исчез (диск отвалился) — соединение БД уже открыто, поэтому фикстура жива
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "сгинула.db")
    errorlog._BUF.clear()

    res = await backup.backup_once()

    assert not res["ok"]
    assert sent and sent[0][1] == "backup_fail" and "Бэкап БД не удался" in sent[0][0]
    run = await db.last_backup_run()
    assert run["ok"] == 0 and "не найдена" in run["error"]
    assert errorlog._BUF and errorlog._BUF[-1]["source"] == "backup"

    # …и ровно ОДНО сообщение владельцу: телеметрия по этой записи молчит (alert=False),
    # иначе на каждый провал бэкапа приходил бы дубль
    assert len(sent) == 1
    await errorlog.flush_once()
    assert len(sent) == 1, sent
    assert len(await db.list_errors()) == 1   # при этом в телеметрии провал записан


async def test_backup_once_survives_thread_crash(monkeypatch, tmp_db, tmp_path):
    """Даже если сама процедура снимка взорвалась, цикл не падает, а пишет провал в журнал."""
    sent = []

    async def fake_alert(text, **kw):
        sent.append(text)

    def boom(*a, **kw):
        raise OSError("устройство не готово")

    monkeypatch.setattr(notify, "alert", fake_alert)
    monkeypatch.setattr(backup, "_backup_sync", boom)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "bk")

    res = await backup.backup_once()
    assert not res["ok"] and "устройство не готово" in res["error"]
    assert sent
    assert (await db.last_backup_run())["ok"] == 0


# ─────────────────────────── статус для админки ───────────────────────────
async def test_scan_splits_snapshots_and_manual(monkeypatch, tmp_db, tmp_path):
    dst = tmp_path / "bk"
    dst.mkdir()
    for name in ("bot_20260101_000001.db", "bot_20260101_000002_1.db", "bot_pre_manual.db"):
        (dst / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "BACKUP_DIR", dst)
    monkeypatch.setenv("BOT_DB_BACKUP_DIR", str(tmp_path / "offsite"))

    snap = backup.scan()

    assert {f["name"] for f in snap["local"]["snapshots"]} == {
        "bot_20260101_000001.db", "bot_20260101_000002_1.db"}
    assert [f["name"] for f in snap["local"]["manual"]] == ["bot_pre_manual.db"]
    assert snap["local"]["keep"] == config.BACKUP_KEEP
    assert snap["offsite"]["exists"] is False and snap["offsite"]["count"] == 0
    assert snap["same_volume"] in (True, False, None)


def test_same_volume_detects_identity(tmp_path):
    """Один и тот же том обязан определяться — иначе предупреждение никогда не покажется."""
    assert backup.same_volume(tmp_path, tmp_path / "нет-такого-каталога") is True


# ─────────────────────── второй контур (backup_offsite.py) ───────────────────────
def test_offsite_name_collision_gets_suffix(monkeypatch, tmp_path):
    """Публикация во втором контуре обязана вести себя как в app/backup.py: при занятом
    имени — суффикс _1/_2. Задача Планировщика демонстрируемо даёт по 2-3 прогона в подряд
    идущие секунды, а os.replace на Windows затирает файл молча, без ошибки."""
    import backup_offsite

    src = tmp_path / "bot.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    dst = tmp_path / "offsite"
    monkeypatch.setattr(backup_offsite, "SRC", src)
    monkeypatch.setattr(backup_offsite, "DEST_DIR", dst)
    monkeypatch.setattr(backup_offsite, "KEEP", 5)
    dst.mkdir()

    assert backup_offsite._backup_db("20260802_120000") == 0
    assert backup_offsite._backup_db("20260802_120000") == 0

    assert sorted(p.name for p in dst.glob("bot_*.db")) == [
        "bot_20260802_120000.db", "bot_20260802_120000_1.db"]
    assert not list(dst.glob("*.partial")), ".partial не подчищен"


class _Req:
    """Минимальный aiohttp-подобный запрос: query для GET, json() для POST."""

    def __init__(self, query=None, body=None):
        self.query = query or {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def test_backup_routes_are_owner_only():
    """В _OPERATOR_ROUTES бэкапы не вносим: карта закрыта по умолчанию, наёмный оператор
    не должен ни видеть пути к копиям базы, ни дёргать снятие снимка."""
    assert webapp._route_cap("/api/admin/backup_status") == roles.CAP_SETTINGS
    assert webapp._route_cap("/api/admin/backup_now") == roles.CAP_SETTINGS


async def test_backup_api_rejects_non_admin(monkeypatch):
    monkeypatch.setattr(webapp, "_verify_admin", lambda init_data: None)
    assert (await webapp._api_admin_backup_status(_Req({"initData": "x"}))).status == 403
    assert (await webapp._api_admin_backup_now(_Req(body={"initData": "x"}))).status == 403


async def test_backup_status_api_shape(monkeypatch, tmp_db, tmp_path):
    monkeypatch.setattr(webapp, "_verify_admin", lambda init_data: 4471)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "bk")
    await db.add_backup_run(True, "bot_20260802_120000.db", 1234, 42)

    body = json.loads((await webapp._api_admin_backup_status(_Req({"initData": "x"}))).text)

    assert body["ok"] and body["last"]["size"] == 1234 and len(body["runs"]) == 1
    assert set(body["local"]) == {"dir", "snapshots", "manual", "keep", "interval_hours"}
    assert set(body["offsite"]) == {"dir", "exists", "count", "latest_mtime"}
    assert "same_volume" in body


@pytest.mark.parametrize("name, ok", [
    ("bot_20260802_120000.db", True),
    ("bot_20260802_120000_2.db", True),
    ("bot_pre_merge.db", False),
    ("bot_abc.db", False),
    ("bot_20260802_120000.db.bak", False),
    ("bot_2026080_120000.db", False),
])
def test_snapshot_regex(name, ok):
    assert bool(backup.SNAPSHOT_RE.match(name)) is ok


def test_offsite_reads_its_settings_from_env_file(monkeypatch, tmp_path):
    """Каталог копий из .env обязан доходить до ВТОРОГО контура.

    До 27.08.2026 скрипт читал из .env один DATA_DIR, а BOT_DB_BACKUP_DIR и соседи брал только
    из os.getenv. Планировщик запускает его без окружения бота, поэтому владелец студии,
    прописавший каталог в .env по инструкции, молча получал умолчание: админка показывала
    новый путь, а копии продолжали ложиться в старый."""
    import backup_offsite

    (tmp_path / ".env").write_text(
        "# каталог копий\n"
        r'BOT_DB_BACKUP_DIR="G:\Мой диск\PrintStudio"' "\n"
        "BOT_DB_BACKUP_KEEP=7\n",
        encoding="utf-8")
    monkeypatch.setattr(backup_offsite, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(backup_offsite, "_ENV_FILE", None)
    monkeypatch.delenv("BOT_DB_BACKUP_DIR", raising=False)
    monkeypatch.delenv("BOT_DB_BACKUP_KEEP", raising=False)

    assert backup_offsite._env("BOT_DB_BACKUP_DIR") == r"G:\Мой диск\PrintStudio"
    assert backup_offsite._env("BOT_DB_BACKUP_KEEP", "120") == "7"
    # ключа нет ни там, ни там — умолчание
    assert backup_offsite._env("BOT_BACKUP_EXTRA_KEEP", "30") == "30"
    # окружение сильнее файла: setx на машине владельца перекрывает .env
    monkeypatch.setenv("BOT_DB_BACKUP_DIR", r"D:\другой каталог")
    assert backup_offsite._env("BOT_DB_BACKUP_DIR") == r"D:\другой каталог"
