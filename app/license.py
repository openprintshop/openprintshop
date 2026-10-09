"""Лицензионные ключи (трек C, п.4): офлайн-проверяемый подписанный ключ студии.

ЗАЧЕМ. Продукт продаётся чужим студиям и уезжает к ним ИСХОДНЫМ КОДОМ (установка =
git clone, см. tools/installer/install.ps1). Значит любой секрет, вшитый в клиента,
у покупателя в руках — симметричная подпись (HMAC) не годится в принципе: тем же
секретом покупатель выпишет себе ключ. Поэтому ключ подписывается ПРИВАТНЫМ ключом
владельца (он никогда не покидает машину владельца и не лежит в репозитории), а в
сборку вшит только ПУБЛИЧНЫЙ — им можно лишь проверять.

СХЕМА — Ed25519 (RFC 8032), проверка на чистом Python (hashlib.sha512 + целочисленная
арифметика, см. низ модуля). Почему не `cryptography`: её нет ни в requirements.txt, ни
в венве бота, а новая зависимость едет к клиенту через `pip install -r requirements.txt`
внутри selfupdate._apply_sync(), где ошибка pip НЕ откатывает уже сделанный merge —
неудачно вставшее колесо оставило бы обновлённого бота с ImportError ровно в модуле
лицензии. Чистый Python: ноль зависимостей, ноль ABI-рисков на чужой Windows, а скорость
здесь неважна (проверка кэшируется, зовётся раз в цикл статуса).

ФОРМАТ КЛЮЧА — одна строка, пригодная для копипаста в .env:

    PSB1.<base64url(payload JSON)>.<base64url(подпись Ed25519, 64 байта)>

Подпись считается по БАЙТАМ СРЕДНЕГО СЕГМЕНТА (ascii base64url), а не по «каноничному»
JSON: канонизации не существует, а сегмент байт-в-байт один и тот же у выпускающего и
у проверяющего. Поля нагрузки:

    studio  — кому выдан (название студии/покупателя), id — номер ключа для реестра,
    edition — на какую редакцию продукта выдан (см. app/edition.py),
    issued  — дата выдачи 'YYYY-MM-DD', expires — дата окончания или null (бессрочный),
    plan    — 'perpetual' / 'subscription' / что угодно ещё (свободная строка).

Модель продажи владельцем ещё не выбрана, поэтому механизм держит ОБА варианта: нет
`expires` = разовая покупка (бессрочно), есть `expires` = подписка.

ЧТО ДЕЛАЕТ ПРОВЕРКА. По умолчанию — почти ничего: показывает состояние в «Статусе» и
самодиагностике и запрещает САМООБНОВЛЕНИЕ КОДА без действующей лицензии. Приём
заказов, расчёты, доступ к данным студии не трогает НИКОГДА: продукт, который
«выключается», уносит с собой чужую выручку, и такого решения владелец не принимал.
Ужесточение политики — одна явная точка, `enforcement_level()` (настройка
LICENSE_ENFORCEMENT в .env), новые уровни добавляются туда, а не расползаются по коду.

ПРОБНЫЙ ПЕРИОД (решение владельца 23.08.2026). Купленный ключ — не единственный путь: при
первой установке продукт САМ выдаёт 90 дней (TRIAL_DAYS), чтобы студия могла попробовать,
не дожидаясь поставщика. Отметка о первом запуске ставится в ДВУХ местах — строкой в
таблице settings и файлом DATA_DIR/.trial, — и побеждает самая ранняя из них: удаление
одной пробу не сбрасывает. Рядом лежит «водяная отметка» максимально виденного времени,
поэтому перевод часов назад пробы не продлевает. Это удобство, а не защита: чистый каталог
данных = новая проба, и так это и описано в docs/Перспективные разработки/licensing-2026-08-23.md.

ЧАСЫ. Сети нет по определению, поэтому единственный источник времени — часы машины.
Сбитые назад часы дают ложное «истекла», сдвинутые вперёд — обход срока. Отсюда два
правила: (1) ключ с датой выдачи в будущем считаем невалидным и прямо пишем «проверьте
дату на компьютере»; (2) истечение не ломает ничего, кроме обновлений, — цена ошибки
часов должна быть маленькой.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import config, edition

log = logging.getLogger(__name__)

# Публичные ключи выпуска (hex, 32 байта каждый). Сюда владелец вставляет строку,
# напечатанную tools/licensing/keygen.py; список — чтобы можно было ввести новый ключ
# выпуска, не обесценив уже проданные лицензии (старый лежит рядом, пока не истечёт).
# ПУСТО = сборка не умеет проверять ключи вовсе: тогда enforcement по умолчанию 'off'
# и продукт ведёт себя ровно как до появления лицензий. НЕПУСТО = это распространяемая
# сборка, и умолчание становится 'update' (см. enforcement_level: политика вяжется именно
# сюда, а не к edition() — та у покупателя опознаёт git-clone-установку как 'dev').
# Открытая версия (10.10.2026): ключей выпуска нет — механизм спит, обновления доступны всем.
# Он оставлен целиком: сборка, которой понадобятся ключи, впишет сюда свой публичный ключ.
PUBLIC_KEYS: tuple[str, ...] = ()

KEY_PREFIX = "PSB1"
KEY_FILENAME = "license.key"
MAX_KEY_CHARS = 4096        # ключ владельца — это ~250 символов; всё длиннее не разбираем
EXPIRING_DAYS = 14          # за сколько дней до конца начинаем предупреждать
_CLOCK_SKEW_DAYS = 1.0      # допуск на часовые пояса в дате выдачи

# Автоматическая проба (решение владельца 23.08.2026): при первой установке продукт сам
# даёт срок на попробовать, без обращения к поставщику. Переопределяется TRIAL_DAYS в .env —
# покупателю эта ручка не нужна, она для наших тестов и особых договорённостей.
TRIAL_DAYS = 90
TRIAL_FILENAME = ".trial"          # JSON в корне каталога данных, рядом с license.key
TRIAL_STARTED_KEY = "trial_started_at"   # ключи таблицы settings; в DEFAULT_SETTINGS/
TRIAL_SEEN_KEY = "trial_seen_at"         # SETTINGS_ORDER их НЕТ — в админке не настройка

# Уровни ужесточения. Единственная точка расширения политики:
#   off    — ничего не требуем (сборка без вшитого публичного ключа, машина разработчика);
#   update — без действующей лицензии не применяем обновления кода (по умолчанию у покупателя).
# Новый уровень (например «предупреждать клиента в мини-апке») добавляется СЮДА и
# читается через enforcement_level(), чтобы политика не расползлась по модулям.
LEVELS = ("off", "update")

_cache: dict[str, Any] = {}      # {"key": сырой ключ, "at": time, "res": результат verify}
_CACHE_TTL_S = 60.0              # «Статус» деска поллится раз в 20 с — не разбирать ключ каждый раз


# ---------------------------------------------------------------- загрузка ключа

def _read_key_file() -> str:
    """Ключ из файла DATA_DIR/license.key (альтернатива .env: файл переживает
    переустановку и переезжает вместе с каталогом данных). Путь считаем ЛЕНИВО —
    константа уровня модуля не переживает monkeypatch config.DATA_DIR в тестах."""
    try:
        p = config.DATA_DIR / KEY_FILENAME
        if p.is_file():
            # utf-8-sig: Блокнот сохраняет «UTF-8 с BOM», и невидимый префикс превращал
            # верный ключ в «не распознан» — владелец видел в файле ровно то, что копировал.
            return p.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        log.warning("лицензия: не прочитать %s", KEY_FILENAME, exc_info=True)
    return ""


def load() -> tuple[str, str]:
    """(ключ, откуда). Приоритет у .env: он ближе к владельцу, чем каталог данных."""
    env = (getattr(config, "LICENSE_KEY", "") or "").strip()
    if env:
        return env, "env"
    fromfile = _read_key_file()
    return (fromfile, "file") if fromfile else ("", "")


# ---------------------------------------------------------------- разбор и проверка

def _b64u_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _day_ts(s: str, end_of_day: bool = False) -> float | None:
    """'YYYY-MM-DD' → epoch (UTC-полночь). end_of_day: конец указанных суток —
    день окончания лицензии считаем ВКЛЮЧИТЕЛЬНО, иначе ключ «до 31.12» умирает 30-го."""
    try:
        d = date.fromisoformat(str(s).strip())
    except (TypeError, ValueError):
        return None
    ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
    return ts + 86400.0 if end_of_day else ts


def _fail(state: str, reason: str) -> dict:
    return {"ok": False, "state": state, "reason": reason, "studio": "", "edition": "",
            "plan": "", "id": "", "issued": "", "expires": "", "expires_at": None,
            "days_left": None, "expired": False, "edition_mismatch": False}


def verify(key: str, now: float | None = None) -> dict:
    """Разобрать и проверить ключ. Никогда не бросает: результат уезжает в общий
    /api/admin/status, и исключение погасило бы владельцу весь экран «Статус»."""
    now = time.time() if now is None else now
    key = (key or "").strip()
    if not key:
        return _fail("missing", "лицензионный ключ не задан")
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return _fail("invalid", "ключ не распознан — скопируйте строку лицензии целиком, "
                                f"она начинается с «{KEY_PREFIX}.»")
    if len(key) > MAX_KEY_CHARS:
        # «Сначала аутентифицируй, потом парси»: base64+json на неподписанных байтах
        # разбирать нечего. Настоящий ключ на два порядка короче.
        return _fail("invalid", "ключ неправдоподобно длинный — скопирована не та строка")
    try:
        raw = _b64u_decode(parts[1])
        sig = _b64u_decode(parts[2])
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return _fail("invalid", "ключ повреждён — при копировании потерялись символы")
    if not isinstance(payload, dict):
        return _fail("invalid", "ключ повреждён — нагрузка не распознана")
    if not PUBLIC_KEYS:
        return _fail("invalid", "в эту сборку не вшит публичный ключ выпуска — "
                                "проверить лицензию нечем")
    msg = parts[1].encode("ascii")
    if not any(verify_signature(msg, sig, _pub_bytes(h)) for h in PUBLIC_KEYS):
        return _fail("invalid", "подпись ключа не сходится — ключ подделан "
                                "или испорчен при копировании")

    issued = str(payload.get("issued") or "")
    expires = str(payload.get("expires") or "") or ""
    res: dict[str, Any] = {
        "ok": True, "state": "ok", "reason": "",
        "studio": str(payload.get("studio") or ""),
        "edition": str(payload.get("edition") or ""),
        "plan": str(payload.get("plan") or ""),
        "id": str(payload.get("id") or ""),
        "issued": issued, "expires": expires,
        "expires_at": None, "days_left": None, "expired": False,
        "edition_mismatch": False,
    }
    issued_ts = _day_ts(issued)
    if issued and issued_ts is None:
        # Симметрично дате окончания: нечитаемая дата — опечатка выпускающего, и молча
        # принимать её нельзя (она уедет покупателю в карточку «Лицензия» как есть).
        return _fail("invalid", "в ключе нечитаемая дата выдачи")
    if issued_ts is not None and issued_ts > now + _CLOCK_SKEW_DAYS * 86400:
        return _fail("invalid", "дата выдачи ключа в будущем — проверьте дату "
                                "и время на этом компьютере")
    if expires:
        exp_ts = _day_ts(expires, end_of_day=True)
        if exp_ts is None:
            return _fail("invalid", "в ключе нечитаемая дата окончания")
        res["expires_at"] = exp_ts
        res["days_left"] = round((exp_ts - now) / 86400.0, 1)
        if exp_ts <= now:
            res.update(ok=False, state="expired", expired=True,
                       reason=f"лицензия истекла {_human(expires)}")
        elif exp_ts - now <= EXPIRING_DAYS * 86400:
            res.update(state="expiring",
                       reason=f"лицензия истекает {_human(expires)}")
    want = res["edition"]
    declared = _declared_edition()
    if want and declared and want != declared:
        # Сверяем ТОЛЬКО с явно заданной в .env редакцией. Автоопределение (по наличию
        # .git) у покупателя честно врёт — установка это git clone, — и сверка с ним
        # вешала честному покупателю вечное «проверьте EDITION в .env» на действующем
        # ключе. Расхождение с ЯВНЫМ значением — настоящая опечатка владельца.
        # Отказывать всё равно не за что: право покупки важнее ярлыка сборки.
        res["edition_mismatch"] = True
    return res


def _declared_edition() -> str:
    """Редакция, ЯВНО прописанная в .env (пусто — владелец её не задавал)."""
    val = (getattr(config, "EDITION", "") or "").strip().lower()
    return val if val in (edition.DEV, edition.STUDIO) else ""


def _human(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    return d.strftime("%d.%m.%Y")


def _pub_bytes(h: str) -> bytes:
    try:
        return bytes.fromhex(h.strip())
    except ValueError:
        return b""


# ---------------------------------------------------------------- состояние наружу

def enforcement_level() -> str:
    """Насколько строго спрашивать лицензию. ЕДИНСТВЕННАЯ точка политики.

    Явное значение LICENSE_ENFORCEMENT в .env сильнее всего.

    Умолчание вяжем к ОДНОМУ признаку — вшит ли в сборку публичный ключ выпуска. Пусто =
    сборка проверять ключи не умеет, спрашивать нечего → 'off' (ровно как до появления
    лицензий). Непусто = это распространяемая сборка → 'update': обновления кода только
    по действующей лицензии.

    Почему НЕ по edition(): та определяет редакцию по наличию .git, а установка покупателя
    — это git clone (tools/installer/install.ps1), то есть у покупателя автоопределение
    даёт 'dev' и гейт не включился бы НИКОГДА. На машине разработчика выключатель явный:
    LICENSE_ENFORCEMENT=off либо EDITION=dev в личном .env (второе — заодно и признак
    сборки разработчика)."""
    val = (getattr(config, "LICENSE_ENFORCEMENT", "") or "").strip().lower()
    if val in LEVELS:
        return val
    if not PUBLIC_KEYS or _declared_edition() == edition.DEV:
        return "off"
    return "update"


# ---------------------------------------------------------------- пробный период
# ЗАЧЕМ ДВА МЕСТА. Отметка о первом запуске лежит и строкой в БД (settings), и файлом
# DATA_DIR/.trial; побеждает САМАЯ РАННЯЯ. Удалили файл — дата придёт из БД; откатили БД
# из data/backups (backup.py копирует только bot.db) — дата придёт из файла. Это удобство,
# а не защита: снести каталог данных целиком по-прежнему значит начать пробу заново, см.
# docs/Перспективные разработки/licensing-2026-08-23.md.
#
# ПОЧЕМУ ЧТЕНИЕ СИНХРОННОЕ. update_allowed() зовётся из selfupdate._apply_sync(), которую
# поднимают через asyncio.to_thread; соединение aiosqlite привязано к главному циклу и из
# рабочего потока недоступно. Поэтому читаем файл и read-only sqlite3, а пишем отметки
# отдельной async-функцией ensure_trial() — один раз при старте из run.py.


def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f > 0 and math.isfinite(f) else 0.0


def trial_days() -> int:
    """Длительность пробы. TRIAL_DAYS из .env сильнее константы (мусор игнорируем)."""
    try:
        n = int(getattr(config, "TRIAL_DAYS", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else TRIAL_DAYS


def _read_trial_file() -> tuple[float, float]:
    """(начало, водяная отметка) из DATA_DIR/.trial; (0, 0) — файла нет.

    Каталог тут НЕ создаём ни при каких условиях: путь чтения зовётся из status(), а тот
    работает и на машинах, где DATA_DIR указывает в никуда (и в тестах — тоже)."""
    try:
        p = config.DATA_DIR / TRIAL_FILENAME
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return _num(data.get("started_at")), _num(data.get("seen_at"))
    except (OSError, ValueError):
        log.warning("проба: не прочитать %s", TRIAL_FILENAME, exc_info=True)
    return 0.0, 0.0


def _read_trial_db() -> tuple[float, float]:
    """То же из таблицы settings, отдельным read-only соединением sqlite3.

    Заблокированная или ещё не созданная БД не имеет права уронить статус — любая
    неприятность здесь означает «отметок не видно», и решение принимается по файлу."""
    try:
        p = Path(config.DB_PATH)
        if not p.is_file():
            return 0.0, 0.0
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?)",
                (TRIAL_STARTED_KEY, TRIAL_SEEN_KEY)).fetchall()
        finally:
            con.close()
        got = {str(k): _num(v) for k, v in rows}
        return got.get(TRIAL_STARTED_KEY, 0.0), got.get(TRIAL_SEEN_KEY, 0.0)
    except Exception:  # noqa: BLE001 — статус важнее любой беды с файлом БД
        log.warning("проба: не прочитать отметку из БД", exc_info=True)
        return 0.0, 0.0


def trial_info(now: float | None = None) -> dict | None:
    """Состояние пробы или None, если отметок нет вовсе (пробу никто не начинал).

    ЧАСЫ. Рядом со стартом хранится максимальное виденное время; эффективным считаем
    max(часы, отметка). Перевод часов назад пробу не продлевает, перевод вперёд — только
    сокращает (это честно: продукт был доступен ровно столько, сколько машина считала)."""
    now = time.time() if now is None else now
    fstart, fseen = _read_trial_file()
    dstart, dseen = _read_trial_db()
    marks = [v for v in (fstart, dstart) if v > 0]
    if not marks:
        return None
    started = min(marks)                      # побеждает самая ранняя отметка
    seen = max(fseen, dseen, started)
    eff = max(now, seen)
    ends = started + trial_days() * 86400.0
    return {"started_at": started, "ends_at": ends, "seen_at": seen,
            "days_left": round((ends - eff) / 86400.0, 1), "over": eff >= ends}


def _trial_iso(ts: float) -> str:
    """epoch → 'YYYY-MM-DD' по МЕСТНЫМ часам: дата показывается человеку за этой машиной."""
    try:
        return datetime.fromtimestamp(ts).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _apply_trial(out: dict, now: float | None) -> dict:
    """Наложить пробу на состояние 'missing'. Действующий КЛЮЧ всегда сильнее пробы,
    поэтому трогаем ровно тот случай, когда ключа нет вовсе."""
    if out.get("enforcement") == "off":
        return out
    if out.get("state") == "invalid":
        # Опечатка в ключе гасит живую пробу — и раньше человек не понимал, почему. Само
        # поведение верное (сломанный ключ надо ЗАМЕТИТЬ, а не прикрыть пробой), но текст
        # обязан подсказать выход. Состояние и запрет обновлений не трогаем.
        info = trial_info(now=now)
        if info and not info["over"]:
            out["reason"] = (f"{out.get('reason') or 'ключ не проходит проверку'}. "
                             f"Пробный период ещё идёт (до {_human(_trial_iso(info['ends_at']))}) — "
                             f"уберите строку LICENSE_KEY, чтобы вернуться к нему")
        return out
    if out.get("state") != "missing":
        return out
    info = trial_info(now=now)
    if not info:
        # Отметок нет — значит ensure_trial() ещё не звали (или enforcement был 'off').
        # status() отметок НЕ создаёт: у чтения не должно быть побочных эффектов.
        return out
    ends_iso = _trial_iso(info["ends_at"])
    out["expires"] = ends_iso
    out["expires_at"] = info["ends_at"]
    # Карточка деска показывает days_left, запасная ветка — текст reason: в последние сутки
    # пробы round(0.4, 1) дал бы «0 дн.» рядом с фразой «осталось 1 дн.». Показываем ОДНО и то
    # же число: неполный день считаем за день, пока проба действительно жива.
    out["days_left"] = (max(1.0, float(math.ceil(info["days_left"])))
                        if not info["over"] else info["days_left"])
    out["trial_started"] = _trial_iso(info["started_at"])
    out["trial_days"] = trial_days()
    if info["over"]:
        out.update(ok=False, state="trial_over", expired=True,
                   reason=f"Пробный период закончился {_human(ends_iso)} — "
                          f"обновления кода больше не применяются")
    else:
        left = int(out["days_left"])
        out.update(ok=True, state="trial", expired=False,
                   reason=f"Пробный период: осталось {left} дн., до {_human(ends_iso)}")
    return out


async def ensure_trial(now: float | None = None) -> dict | None:
    """Поставить (и обновить водяную отметку) пробы. Зовётся ОДИН РАЗ при старте бота.

    Отдельно от status() намеренно: чтение состояния не должно ничего создавать, а запись
    должна происходить в главном цикле, где жив aiosqlite. При enforcement 'off' (машина
    разработчика, сборка без вшитого публичного ключа) не создаём ни файла, ни строки в БД."""
    if enforcement_level() == "off":
        return None
    # Лениво: db тяжёлый, а license.py импортируется в том числе из синхронных мест.
    from . import db

    now = time.time() if now is None else now
    fstart, fseen = _read_trial_file()
    dstart, dseen = 0.0, 0.0
    try:
        s = await db.get_settings()
        dstart, dseen = _num(s.get(TRIAL_STARTED_KEY)), _num(s.get(TRIAL_SEEN_KEY))
    except Exception:  # noqa: BLE001 — без БД проба живёт по файлу
        log.warning("проба: отметка из БД недоступна", exc_info=True)
    started = min([v for v in (fstart, dstart) if v > 0] or [now])
    seen = max(fseen, dseen, now, started)
    if (fstart, fseen) != (started, seen):
        _write_trial_file(started, seen)
    if (dstart, dseen) != (started, seen):
        try:
            await db.set_setting(TRIAL_STARTED_KEY, started)
            await db.set_setting(TRIAL_SEEN_KEY, seen)
        except Exception:  # noqa: BLE001
            log.warning("проба: отметку в БД записать не удалось", exc_info=True)
    _cache.clear()
    info = trial_info(now=now)
    if info:
        log.info("Пробный период: до %s (осталось %.0f дн.)",
                 _human(_trial_iso(info["ends_at"])), max(0.0, info["days_left"]))
    return info


def _write_trial_file(started: float, seen: float) -> None:
    """Атомарно (временный файл + os.replace), как стор аккаунтов в mwtoken._save:
    обрыв посреди записи не должен оставить пустую отметку — это сбросило бы пробу."""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        p = config.DATA_DIR / TRIAL_FILENAME
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"started_at": started, "seen_at": seen}), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        log.warning("проба: не записать %s", TRIAL_FILENAME, exc_info=True)


def status(now: float | None = None) -> dict:
    """Состояние лицензии для «Статуса» деска и самодиагностики.

    Наружу отдаём РАСШИФРОВАННЫЕ поля (кому выдана, план, срок) — это не секрет и
    именно их владелец должен видеть; сам ключ и подпись — никогда."""
    try:
        key, source = load()
        cached = _cache.get("key") == key and (time.time() - float(_cache.get("at") or 0)) < _CACHE_TTL_S
        if cached and now is None:
            res = dict(_cache["res"])
        else:
            res = verify(key, now=now)
            if now is None:
                _cache.update(key=key, at=time.time(), res=dict(res))
        out = dict(res)
        out["source"] = source
        out["key_present"] = bool(key)
        out["enforcement"] = enforcement_level()
        out["build_edition"] = edition.edition()
        # Пробу накладываем СНАРУЖИ кэша: кэшируется только разбор ключа (verify), иначе
        # «проба жива» и «проба кончилась» делили бы один слот (ключ там пустой), а счётчик
        # разборов в tests/test_license.py начал бы врать.
        return _apply_trial(out, now)
    except Exception:  # noqa: BLE001 — статус не имеет права падать целиком
        log.warning("лицензия: проверка не отработала", exc_info=True)
        out = _fail("invalid", "проверка лицензии не отработала — подробности в логе")
        out.update(source="", key_present=False, enforcement=enforcement_level(),
                   build_edition=edition.edition())
        return out


def update_allowed(now: float | None = None) -> tuple[bool, str]:
    """Можно ли применять обновление кода. (True, "") — можно.

    Мягко по замыслу: 'expiring' пропускаем (лицензия ещё жива), отказ формулируем так,
    чтобы владелец понял, что делать, — текст уезжает прямо в тост админки."""
    if enforcement_level() == "off":
        return True, ""
    st = status(now=now)
    state = st.get("state")
    if state in ("ok", "expiring", "trial"):
        return True, ""
    if state == "trial_over":
        return False, (f"обновления недоступны: {st.get('reason') or 'пробный период закончился'} — "
                       "впишите LICENSE_KEY в .env (или положите файл license.key в каталог "
                       "данных). Приём заказов и расчёты работают как обычно")
    if state == "missing":
        return False, ("обновления недоступны: лицензионный ключ не задан — впишите "
                       "LICENSE_KEY в .env (или положите файл license.key в каталог данных)")
    if state == "expired":
        return False, (f"обновления недоступны: {st.get('reason') or 'лицензия истекла'} — "
                       "продлите лицензию у поставщика; если дата на компьютере сбита, "
                       "поправьте её. Всё остальное в боте работает как обычно")
    return False, f"обновления недоступны: {st.get('reason') or 'лицензия не проходит проверку'}"


# ---------------------------------------------------------------- Ed25519 (RFC 8032)
# Только ПРОВЕРКА подписи: приватного ключа в продукте нет и быть не может.
# Подпись живёт в tools/licensing (машина владельца) и переиспользует эти же примитивы.

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# Точка в расширенных координатах (X, Y, Z, T), кривая a = -1.
_By = 4 * pow(5, _P - 2, _P) % _P


def _point_add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


_IDENT = (0, 1, 1, 0)      # нейтральный элемент группы


def _point_mul(p: tuple[int, int, int, int], n: int) -> tuple[int, int, int, int]:
    q = _IDENT
    while n > 0:
        if n & 1:
            q = _point_add(q, p)
        p = _point_add(p, p)
        n >>= 1
    return q


def _point_eq(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    """x по y на кривой. None — точка не на кривой (в т.ч. неканоничное кодирование)."""
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decode_point(raw: bytes) -> tuple[int, int, int, int] | None:
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def encode_point(p: tuple[int, int, int, int]) -> bytes:
    """Точка → 32 байта (нужна и проверке, и генератору ключей в tools/licensing)."""
    zi = pow(p[2], _P - 2, _P)
    x = p[0] * zi % _P
    y = p[1] * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


_Bx = _recover_x(_By, 0) or 0
_B = (_Bx, _By, 1, _Bx * _By % _P)


def verify_signature(msg: bytes, sig: bytes, pub: bytes) -> bool:
    """Ed25519-проверка. Любой мусор на входе — просто False, без исключений.
    Некорректная длина, точка не на кривой и s ≥ L отвергаются явно."""
    try:
        if len(sig) != 64 or len(pub) != 32:
            return False
        a = _decode_point(pub)
        r = _decode_point(sig[:32])
        if a is None or r is None:
            return False
        if _point_eq(_point_mul(a, 8), _IDENT):
            # Точка малого порядка (в т.ч. публичный ключ из 32 нулей) — против неё
            # проходит любая подпись. В нашей модели PUBLIC_KEYS приходит только из
            # исходника, но проверка стоит копейки и закрывает ловушку на будущее.
            return False
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _L
        return _point_eq(_point_mul(_B, s), _point_add(r, _point_mul(a, h)))
    except Exception:  # noqa: BLE001 — проверка подписи не имеет права упасть наружу
        log.warning("лицензия: сбой проверки подписи", exc_info=True)
        return False
