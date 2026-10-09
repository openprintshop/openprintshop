"""Телеметрия ошибок (OpenTodo f4de8627): единая точка сбора сбоев бота.

Раньше ошибки жили только в data/logs/bot.log — файл ротируется за считаные часы, и любой
сбой, замеченный клиентом «вчера», разыскать было уже негде. Здесь всё стекается в одну
таблицу error_log с схлопыванием по отпечатку (fingerprint), а владелец видит их экраном
в админке и получает алерт в Telegram на КАЖДЫЙ НОВЫЙ сбой (а не на каждое повторение).
Места, которые пишут владельцу сами (веб-мидлварь, бэкап), зовут capture(alert=False) —
иначе на один сбой приходило бы два сообщения.

Источники (source):
    web    — необработанное исключение aiohttp-хендлера (_error_mw);
    tg     — необработанное исключение обработчика aiogram (dp.errors);
    log    — любая запись уровня ERROR+ в logging (TelemetryHandler) — фоновые циклы и т.п.;
    js     — ошибка фронта мини-аппа (POST /api/jserr);
    task   — фоновые задачи, которые зовут capture() руками;
    backup — резервное копирование.

Запись идёт через буфер в памяти: capture() вызывается откуда угодно (в том числе из
синхронного logging.Handler.emit, где нет event loop), а в БД пачку сливает flush_loop().
Буфер ограничен — при шторме ошибок теряем старые записи, но не память процесса.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import time
import traceback as tb_mod

from . import db, notify

log = logging.getLogger(__name__)

_MAX_MESSAGE = 2000     # сообщение в БД (в алерт уходит ещё короче — 120 символов)
_MAX_TRACEBACK = 8000   # трейсбек: сотни кадров рекурсии в БД не нужны
_BUF: collections.deque = collections.deque(maxlen=500)
_FLUSH_SEC = 5.0

# Вехи повторов, на которых стоит написать владельцу ещё раз: «ошибка не разовая, она льётся».
_REPEAT_MILESTONES = (50, 500)

# Шум транспорта Windows (OpenTodo 03514860). asyncio на ProactorEventLoop роняет
# ConnectionResetError/ConnectionAbortedError в _call_connection_lost каждый раз, когда клиент
# (Telegram при обрыве long-poll, браузер при закрытии вкладки) закрывает сокет невежливо.
# Диагностической ценности ноль, а экран «Ошибки» в первый же день на боевой забило именно этим.
# Глушим ТОЧЕЧНО: только логгер asyncio и только эти два класса исключений — остальные сбои
# asyncio (в том числе «Task exception was never retrieved») в телеметрию по-прежнему идут.
_TRANSPORT_NOISE = (ConnectionResetError, ConnectionAbortedError)


def _is_transport_noise(record: logging.LogRecord, exc: BaseException | None) -> bool:
    if exc is None or not isinstance(exc, _TRANSPORT_NOISE):
        return False
    return record.name == "asyncio" or record.name.startswith("asyncio.")


def fingerprint(source: str, context: str, exc_type: str, message: str) -> str:
    """Отпечаток ошибки: одинаковые сбои схлопываются в одну строку.

    В ключ идут источник, контекст (путь запроса / имя логгера), тип исключения и начало
    сообщения. Хвост сообщения нарочно отброшен: там обычно меняющиеся id и пути, из-за
    которых один и тот же баг размножился бы на сотни «уникальных» записей."""
    raw = "|".join((source or "", context or "", exc_type or "", (message or "")[:200]))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def capture(source: str, message: str, exc: BaseException | None = None,
            context: str = "", level: str = "error", alert: bool = True) -> None:
    """Положить ошибку в буфер. НИКОГДА не бросает: вызывается из аварийных путей.

    alert=False — записать сбой, но НЕ слать алерт из flush_once: так помечены места, где
    вызывающий шлёт владельцу своё сообщение сам (_error_mw, backup_once). Иначе на один
    сбой приходило бы два уведомления. Алерт оставлен у вызывающего, а не у телеметрии,
    нарочно: он уходит сразу, даже если цикл дренажа буфера умер."""
    try:
        msg = (str(message or "") or "(без сообщения)")[:_MAX_MESSAGE]
        trace = ""
        if exc is not None:
            try:
                trace = "".join(tb_mod.format_exception(type(exc), exc,
                                                        exc.__traceback__))[:_MAX_TRACEBACK]
            except Exception:  # noqa: BLE001
                trace = ""
        exc_type = type(exc).__name__ if exc is not None else ""
        _BUF.append({
            "fingerprint": fingerprint(source, context, exc_type, msg),
            "source": (source or "log")[:16],
            "level": (level or "error")[:16],
            "message": msg,
            "traceback": trace,
            "context": (context or "")[:200],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "alert": bool(alert),
        })
    except Exception:  # noqa: BLE001
        pass   # телеметрия не имеет права уронить того, кто её позвал


class TelemetryHandler(logging.Handler):
    """Хвост logging: любая запись ERROR+ попадает в телеметрию.

    Так в БД оказываются и сбои фоновых циклов, которые нигде не перехватываются явно.
    Записи с extra={'notelemetry': True} пропускаем — ими помечены места, где ошибка уже
    записана в телеметрию своим source (web/tg), а log.exception оставлен ради bot.log.
    Отдельно отсеян шум обрыва соединений (см. _is_transport_noise)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR:
                return
            if record.name.startswith("app.errorlog"):
                return   # иначе сбой самой телеметрии зациклится сам на себя
            if getattr(record, "notelemetry", False):
                return
            exc = record.exc_info[1] if (record.exc_info and record.exc_info[1]) else None
            if _is_transport_noise(record, exc):
                return   # обрыв соединения клиентом — не сбой бота
            capture("log", record.getMessage(), exc, context=record.name,
                    level=record.levelname.lower())
        except Exception:  # noqa: BLE001
            pass


async def flush_once() -> int:
    """Слить буфер в БД. Возвращает число записанных строк.

    БД может быть ещё не поднята (ошибка на старте) или уже закрыта (ошибка на выходе):
    тогда запись возвращается в голову буфера, дренаж прекращается до следующего захода, а
    сам цикл (и тот, кто позвал capture) не страдает. Переполнение буфера ограничено maxlen."""
    n = 0
    while _BUF:
        try:
            rec = _BUF.popleft()
        except IndexError:
            break
        try:
            status, count = await db.upsert_error(
                rec["fingerprint"], rec["source"], rec["level"], rec["message"],
                rec["traceback"], rec["context"], rec["ts"])
            if not status:
                # БД не приняла запись (ещё не поднята / уже закрыта / залочена) — кладём
                # обратно и выходим: следующий заход попробует снова. Раньше пачка молча
                # терялась, хотя чаще всего это секундная недоступность.
                _BUF.appendleft(rec)
                break
            n += 1
            if not rec.get("alert", True):
                continue          # алерт уже отправил тот, кто позвал capture()
            short = rec["message"][:120]
            if status == "new":
                await notify.alert(f"⚠️ Ошибка {rec['source']}: {short}",
                                   key="err_" + rec["fingerprint"])
            elif status == "inc" and count in _REPEAT_MILESTONES:
                # Троттлинг notify.alert съел бы повтор с тем же key — веху шлём своим ключом.
                await notify.alert(
                    f"⚠️ Ошибка {rec['source']} повторилась {count} раз: {short}",
                    key=f"err_{rec['fingerprint']}_{count}")
        except Exception:  # noqa: BLE001
            log.debug("errorlog flush failed", exc_info=True)
    return n


async def flush_loop(interval_sec: float = _FLUSH_SEC) -> None:
    """Вечный цикл записи буфера в БД (запускается из run.py)."""
    while True:
        await asyncio.sleep(max(0.5, interval_sec))
        try:
            await flush_once()
        except Exception:  # noqa: BLE001
            log.debug("errorlog flush loop error", exc_info=True)
