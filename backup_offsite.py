"""Второй контур бэкапа: отдельная копия БД и секретов бота вне каталога data/, чтобы было чем
поднять инфраструктуру заново.

Зачем отдельно от app/backup.py: тот живёт внутри процесса бота и кладёт копии в data/backups/.
Здесь — независимый от бота запуск и своя, более глубокая ретенция; плюс копии секрет/конфиг-файлов
(по умолчанию .env). Копия БД консистентная: SQLite Online Backup API (безопасно при работающем
боте, учитывает WAL) + проверка целостности + атомарная публикация + ротация.

ПРО «ОФСАЙТ»: каталог задаётся переменной окружения BOT_DB_BACKUP_DIR, по умолчанию
C:\\Bot backups — системный диск, а БД может жить на другом диске (data/ — junction на
второй SSD). С 20.08.2026 контуры разведены по ФИЗИЧЕСКИМ дискам: смерть любого одного
диска не уносит базу вместе с копиями. До этого дефолт лежал на том же томе, что БД. Настоящий офсайт (другая машина/облако) — по-прежнему за владельцем; админка
показывает предупреждение same_volume, пока каталог на одном томе с БД
(см. docs/Уже в продукте/backups.md).

Самодостаточный (только stdlib, app.* не импортирует). Запускается Планировщиком раз в 6 часов
(задача pcb_db_backup).

Переопределяется env ИЛИ .env: BOT_DB, BOT_DB_BACKUP_DIR, BOT_DB_BACKUP_KEEP,
BOT_BACKUP_EXTRA (через ';'), BOT_BACKUP_EXTRA_KEEP. Окружение сильнее файла.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent               # ...\print_cost_bot


_ENV_FILE: dict[str, str] | None = None


def _env_file() -> dict[str, str]:
    """Пары `ключ=значение` из .env рядом со скриптом. Свой парсер, а не app.config: скрипт
    самодостаточный (только stdlib) и запускается Планировщиком отдельно от бота."""
    global _ENV_FILE
    if _ENV_FILE is None:
        vals: dict[str, str] = {}
        try:
            for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
        _ENV_FILE = vals
    return _ENV_FILE


def _env(name: str, default: str = "") -> str:
    """Настройка скрипта: переменная окружения сильнее .env, .env сильнее умолчания.

    До 27.08.2026 из .env читался ОДИН DATA_DIR, а BOT_DB_BACKUP_DIR и соседи брались только
    из os.getenv. Владелец студии, прописавший каталог копий в .env (как велит
    docs/Уже в продукте/backups.md), молча получал умолчание: бот показывал новый каталог,
    а второй контур продолжал писать в старый. Порядок «окружение сильнее файла» сохранён —
    setx на машине по-прежнему перекрывает .env.
    """
    v = (os.getenv(name) or "").strip().strip('"').strip("'")
    return v or _env_file().get(name, "") or default


def _data_dir() -> Path:
    """Каталог данных: DATA_DIR из окружения/.env, иначе <папка бота>/data (C2)."""
    raw = _env("DATA_DIR")
    return Path(raw) if raw else PROJECT_ROOT / "data"


# Раньше здесь стоял абсолютный путь к БД машины владельца — на чужой установке офсайт молча
# копировал бы не тот файл. Теперь дефолт выводится из DATA_DIR (C2); BOT_DB по-прежнему
# перекрывает всё. PROJECT_ROOT считаем от САМОГО СКРИПТА, а не от SRC: при data/ вне
# репозитория «.env рядом с БД» не существует, и второй контур перестал бы копировать секреты.
SRC = Path(_env("BOT_DB") or str(_data_dir() / "bot.db"))
DEST_DIR = Path(_env("BOT_DB_BACKUP_DIR", r"C:\Bot backups"))
KEEP = int(_env("BOT_DB_BACKUP_KEEP", "120"))           # ~30 дней при запуске каждые 6 ч
EXTRA = [s.strip() for s in _env("BOT_BACKUP_EXTRA", str(PROJECT_ROOT / ".env")).split(";") if s.strip()]
KEEP_EXTRA = int(_env("BOT_BACKUP_EXTRA_KEEP", "30"))

# Автоснимок этого скрипта: bot_YYYYMMDD_HHMMSS.db. Ротация трогает ТОЛЬКО их — ручные копии
# (bot_pre_*.db и любые другие имена) сортировались в конец glob-а, вечно занимали слоты keep
# и вытесняли настоящие автобэкапы. Регулярка совпадает с app/backup.py::SNAPSHOT_RE.
SNAPSHOT_RE = re.compile(r"^bot_\d{8}_\d{6}(_\d+)?\.db$")


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line, flush=True)
    try:
        DEST_DIR.mkdir(parents=True, exist_ok=True)
        with open(DEST_DIR / "backup.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001  — лог не должен ронять бэкап
        pass


def _rotate(folder: Path, pattern: str, keep: int, match: re.Pattern | None = None) -> int:
    """Оставить `keep` самых свежих файлов. `match` (если задан) сужает набор до наших
    автоснимков: всё, что не подошло под регулярку, не считается и не удаляется."""
    if keep <= 0:
        return 0
    files = sorted(p for p in folder.glob(pattern) if match is None or match.match(p.name))
    removed = 0
    for old in files[:-keep]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _free_name(stamp: str) -> Path:
    """bot_<stamp>.db, а при занятом имени — с суффиксом _1, _2… Совпадает с
    app/backup.py::_free_name: планировщик реально даёт по 2-3 прогона в подряд идущие
    секунды, и без суффикса os.replace молча затирал бы уже опубликованный снимок."""
    dest = DEST_DIR / f"bot_{stamp}.db"
    n = 0
    while dest.exists():
        n += 1
        dest = DEST_DIR / f"bot_{stamp}_{n}.db"
    return dest


def _backup_db(stamp: str) -> int:
    if not SRC.exists():
        log(f"ERROR: исходная БД не найдена: {SRC}")
        return 2
    # pid в имени partial: два прогона в одну секунду иначе пишут в один файл и портят
    # копию друг другу (в app/backup.py процесс один, здесь их сколько угодно).
    tmp = DEST_DIR / f".bot_{stamp}.{os.getpid()}.partial"
    try:
        src = sqlite3.connect(str(SRC))
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)          # онлайн-бэкап: консистентно даже при записи бота (учитывает WAL)
            finally:
                dst.close()
        finally:
            src.close()
        chk = sqlite3.connect(str(tmp))   # проверка целостности копии ДО публикации
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
        if ok != "ok":
            log(f"ERROR: integrity_check копии провалился: {ok}")
            tmp.unlink(missing_ok=True)
            return 3
        dest = _free_name(stamp)         # имя занимаем как можно позже — окно гонки меньше
        os.replace(tmp, dest)            # атомарная публикация
        log(f"OK db: {dest.name} ({dest.stat().st_size // 1024} KB)")
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: бэкап БД не удался: {e!r}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return 1
    n = _rotate(DEST_DIR, "bot_*.db", KEEP, SNAPSHOT_RE)
    if n:
        log(f"ротация БД: удалено {n} (храним {KEEP})")
    return 0


def _backup_extra(stamp: str) -> None:
    """Секреты/конфиг (.env и т.п.) — для быстрого восстановления инфраструктуры. Best-effort:
    не валит бэкап БД, если файла нет или копия не удалась."""
    cfg = DEST_DIR / "config"
    for raw in EXTRA:
        p = Path(raw)
        if not p.exists():
            log(f"extra: пропуск (нет файла) {p}")
            continue
        try:
            cfg.mkdir(parents=True, exist_ok=True)
            out = cfg / f"{p.name}_{stamp}"
            shutil.copy2(p, out)
            log(f"OK extra: config/{out.name}")
            n = _rotate(cfg, f"{p.name}_*", KEEP_EXTRA)
            if n:
                log(f"ротация {p.name}: удалено {n} (храним {KEEP_EXTRA})")
        except Exception as e:  # noqa: BLE001
            log(f"WARN extra '{p}': {e!r}")


def main() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rc = _backup_db(stamp)
    if rc == 0:
        _backup_extra(stamp)             # секреты бэкапим только если БД ушла успешно
    return rc


if __name__ == "__main__":
    sys.exit(main())
