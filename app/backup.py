"""Резервные копии БД: онлайн-бэкап SQLite (безопасно при работающем боте) + ротация.

Используется SQLite Online Backup API (sqlite3.Connection.backup) — он копирует страницы с
учётом конкурентных записей и WAL, поэтому не нужно останавливать бота. Копия делается
ОТДЕЛЬНЫМ sqlite3-соединением внутри asyncio.to_thread: единственное aiosqlite-соединение
бота (db.py) при этом не блокируется. Запускается фоновой задачей из run.py (раз при старте
+ каждые BACKUP_INTERVAL_HOURS).

Порядок публикации снимка (OpenTodo b557e341): пишем во временный `.bot_backup.partial`,
проверяем копию `PRAGMA integrity_check`, и только потом os.replace в `bot_<stamp>.db`. Так в
каталоге не может оказаться обрезанного файла, который выглядит как бэкап: потерянный архив
клиента = репутационная смерть, а «бэкап, который не открывается» хуже отсутствия бэкапа.

Ротация трогает ТОЛЬКО автоснимки (см. SNAPSHOT_RE). Ручные копии — `bot_pre_*.db` и любые
другие имена — не считаются и не удаляются: раньше они сортировались в конец glob-а, вечно
занимали слоты keep и вытесняли настоящие автобэкапы.

ГДЕ ЭТО ЛЕЖИТ ФИЗИЧЕСКИ. Локальный контур пишет в data/backups (config.BACKUP_DIR) — это тот
же том, что и сама БД (data/ — junction на второй SSD). Второй контур — backup_offsite.py в
корне репозитория (задача Планировщика pcb_db_backup), его каталог задаётся переменной
окружения BOT_DB_BACKUP_DIR; с 20.08.2026 дефолт (OFFSITE_DEFAULT_DIR) — C:\\Bot backups на
ДРУГОМ физическом диске (диск 0, системный), то есть смерть одного диска больше не уносит
базу вместе с обоими комплектами копий. Настоящий офсайт (другая машина/облако) остаётся за
владельцем; предупреждение same_volume в админке гаснет, когда каталоги на разных томах.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from . import config, db, errorlog, notify

log = logging.getLogger(__name__)

# Имя автоснимка: bot_YYYYMMDD_HHMMSS.db (+ _1, _2… при коллизии в пределах секунды).
# Только такие файлы участвуют в ротации и считаются «снимками» в статусе для админки.
SNAPSHOT_RE = re.compile(r"^bot_\d{8}_\d{6}(_\d+)?\.db$")

# Незавершённая копия. Имя фиксированное (без метки времени): остаток от прерванного запуска
# перезаписывается следующим, а не копится мусором в каталоге бэкапов.
PARTIAL_NAME = ".bot_backup.partial"

# Каталог офсайт-копий. Значение ДОЛЖНО совпадать с backup_offsite.py — тот скрипт
# самодостаточный (stdlib-only, app.* не импортирует), поэтому дефолт продублирован осознанно.
OFFSITE_DEFAULT_DIR = r"C:\Bot backups"


def offsite_dir() -> Path:
    """Каталог офсайт-копий: BOT_DB_BACKUP_DIR или дефолт из backup_offsite.py."""
    return Path(os.getenv("BOT_DB_BACKUP_DIR", OFFSITE_DEFAULT_DIR))


def _free_name(dest_dir: Path, stamp: str) -> Path:
    """bot_<stamp>.db, а при занятом имени — с суффиксом _1, _2… (два запуска в одну секунду)."""
    dest = dest_dir / f"bot_{stamp}.db"
    n = 0
    while dest.exists():
        n += 1
        dest = dest_dir / f"bot_{stamp}_{n}.db"
    return dest


def rotate(dest_dir: Path, keep: int) -> int:
    """Оставить `keep` самых свежих АВТОСНИМКОВ. Ручные копии не трогает. Возвращает счётчик."""
    if keep <= 0:
        return 0
    snaps = sorted(p for p in Path(dest_dir).glob("bot_*.db") if SNAPSHOT_RE.match(p.name))
    removed = 0
    for old in snaps[:-keep]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _backup_sync(src: Path, dst_dir: Path, keep: int) -> dict:
    """Снимок БД: partial → integrity_check и размер копии → атомарная публикация → ротация.

    Возвращает {ok, path, size, duration_ms, error} — результат едет в журнал backup_runs,
    поэтому исключения наружу не выпускаем, а превращаем в текст ошибки."""
    t0 = time.monotonic()
    res: dict = {"ok": False, "path": "", "size": 0, "duration_ms": 0, "error": ""}
    src, dst_dir = Path(src), Path(dst_dir)
    tmp = dst_dir / PARTIAL_NAME
    try:
        if not src.exists():
            res["error"] = f"исходная БД не найдена: {src}"
            return res
        dst_dir.mkdir(parents=True, exist_ok=True)
        tmp.unlink(missing_ok=True)          # остаток от прошлого прерванного запуска
        conn = sqlite3.connect(str(src))
        try:
            out = sqlite3.connect(str(tmp))
            try:
                conn.backup(out)             # онлайн-бэкап: страницы копируются с учётом WAL
            finally:
                out.close()
        finally:
            conn.close()
        chk = sqlite3.connect(str(tmp))      # проверяем КОПИЮ, а не оригинал
        try:
            verdict = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
        if verdict != "ok":
            res["error"] = "integrity_check копии: " + str(verdict)[:200]
            tmp.unlink(missing_ok=True)
            return res
        if tmp.stat().st_size <= 0:
            # integrity_check пустого файла возвращает «ok»: sqlite3.connect создаёт БД нулевого
            # размера, и такой «снимок» опубликовался бы, а ротация вытеснила бы им настоящий.
            res["error"] = "копия нулевого размера"
            tmp.unlink(missing_ok=True)
            return res
        dest = _free_name(dst_dir, time.strftime("%Y%m%d_%H%M%S"))
        os.replace(tmp, dest)                # публикуем только проверенный файл
        res.update(ok=True, path=str(dest), size=dest.stat().st_size)
        try:
            rotate(dst_dir, keep)
        except OSError:
            # осечка уборки не отменяет удачный снимок — он уже опубликован
            log.debug("ротация бэкапов не удалась", exc_info=True)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"[:300]
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        res["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return res


async def backup_once() -> dict:
    """Один снимок + запись в журнал backup_runs. На провале — алерт владельцу и телеметрия."""
    try:
        res = await asyncio.to_thread(_backup_sync, config.DB_PATH, config.BACKUP_DIR,
                                      config.BACKUP_KEEP)
    except Exception as e:  # noqa: BLE001
        log.warning("Бэкап БД не удался", exc_info=True, extra={"notelemetry": True})
        res = {"ok": False, "path": "", "size": 0, "duration_ms": 0,
               "error": f"{type(e).__name__}: {e}"[:300]}
    await db.add_backup_run(res["ok"], res["path"], res["size"], res["duration_ms"], res["error"])
    if res["ok"]:
        log.info("Бэкап БД → %s (%d КБ)", Path(res["path"]).name, res["size"] // 1024)
    else:
        err = res["error"] or "неизвестная ошибка"
        log.warning("Бэкап БД не удался: %s", err, extra={"notelemetry": True})
        # Молчаливый провал бэкапа = самый дорогой из возможных: узнаём о нём сразу.
        try:
            await notify.alert("💾 Бэкап БД не удался: " + err, key="backup_fail")
        except Exception:  # noqa: BLE001
            log.debug("backup alert failed", exc_info=True)
        # alert=False: владельцу уже написали строкой выше — телеметрия прислала бы второе
        # сообщение о том же провале. Свой алерт надёжнее: он не ждёт дренажа буфера.
        errorlog.capture("backup", "Бэкап БД не удался: " + err, context="backup_once",
                         alert=False)
    return res


async def backup_loop() -> None:
    """Бэкап при старте, затем каждые config.BACKUP_INTERVAL_HOURS часов."""
    interval = max(0.25, config.BACKUP_INTERVAL_HOURS) * 3600
    while True:
        await backup_once()
        await asyncio.sleep(interval)


# ───────────────────────── статус для админки (b557e341) ─────────────────────────
def _nearest_existing(p: Path) -> Path | None:
    """Сам путь или ближайший существующий родитель — чтобы os.stat не падал на пустом каталоге."""
    p = Path(p)
    for cand in (p, *p.parents):
        try:
            if cand.exists():
                return cand
        except OSError:
            return None
    return None


def same_volume(a: Path, b: Path) -> bool | None:
    """Лежат ли пути на одном физическом томе (None — определить не удалось).

    os.stat().st_dev на Windows различает тома и РАЗВОРАЧИВАЕТ junction: проверено на машине
    с двумя SSD — у C:\\ и у второго тома разные st_dev, а data/ (junction на второй SSD)
    совпадает со вторым томом. Запасной вариант — сравнение корней развёрнутых resolve()-путей."""
    pa, pb = _nearest_existing(a), _nearest_existing(b)
    if pa is not None and pb is not None:
        try:
            return os.stat(pa).st_dev == os.stat(pb).st_dev
        except OSError:
            pass
    try:
        return Path(a).resolve().anchor.lower() == Path(b).resolve().anchor.lower()
    except OSError:
        return None


def _files(folder: Path) -> list[dict]:
    """[{name, size, mtime}] по bot_*.db, свежие сверху. Пустой список, если каталога нет."""
    out = []
    try:
        for p in Path(folder).glob("bot_*.db"):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    except OSError:
        return []
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


def scan() -> dict:
    """Снимок состояния обоих контуров для админки. Синхронный — звать через asyncio.to_thread."""
    local_dir, off_dir = Path(config.BACKUP_DIR), offsite_dir()
    files = _files(local_dir)
    off = _files(off_dir)
    return {
        "local": {
            "dir": str(local_dir),
            "snapshots": [f for f in files if SNAPSHOT_RE.match(f["name"])],
            "manual": [f for f in files if not SNAPSHOT_RE.match(f["name"])],
            "keep": config.BACKUP_KEEP,
            "interval_hours": config.BACKUP_INTERVAL_HOURS,
        },
        "offsite": {
            "dir": str(off_dir),
            "exists": Path(off_dir).is_dir(),
            "count": len(off),
            "latest_mtime": off[0]["mtime"] if off else 0,
        },
        # Локальный контур на томе БД всегда; смысл имеет именно офсайтный каталог.
        "same_volume": same_volume(off_dir, Path(config.DB_PATH)),
    }
