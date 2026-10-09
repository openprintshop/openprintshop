"""Служебные журналы студии, которые читает админка: usage, бэкапы, ошибки, уведомления.

Четыре инбоксово-журнальных блока, у которых один и тот же характер: пишет их СЛУЖЕБНЫЙ
код (app/backup.py, app/errorlog.py, middleware _usage_mw веб-слоя), а не клиентский поток,
и писать они обязаны best-effort — «не смогли записать» никогда не роняет то, что писалось.

* USAGE-АГРЕГАТ обращений к эндпоинтам (endpoint_hits) — сознательная замена ротируемым
  access-логам. Это наблюдаемость, сосед errorlog/backup, а не CRM, хотя лежал в db.py
  сразу за лентой активности.
* ЖУРНАЛ РЕЗЕРВНЫХ КОПИЙ (backup_runs, OpenTodo b557e341): add_backup_run одним commit-ом
  вставляет запись и подрезает журнал до _BACKUP_RUNS_KEEP — константа едет тем же модулем.
* ТЕЛЕМЕТРИЯ ОШИБОК (error_log, OpenTodo f4de8627) — свёртка одинаковых трейсов, счётчики
  непросмотренного, уборка разобранного.
* ЦЕНТР УВЕДОМЛЕНИЙ АДМИНА (admin_notifications, 2026-07-16); try_mark_published —
  атомарный перевод release note new -> published ДО рассылки, антидубль.

Выделено из app/db.py волной 6 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ. Из внешнего мира нужны только core (_conn,
_jload для payload уведомлений) и stdlib json/time; логгер заведён — bump_endpoint_hit,
add_backup_run и upsert_error глушат свои ошибки через log.debug.

ЛОВУШКА ДЛЯ ТЕСТОВ: тела зовут `_conn()` голым именем, разрешая его в пространстве ЭТОГО
модуля. Подмена `monkeypatch.setattr(db, "_conn", ...)` до них больше не достаёт — патчить
надо app.store.ops (так починен test_backup.test_add_backup_run_never_raises).
"""
from __future__ import annotations

import json
import logging
import time

from .core import _abs, _conn, _jload, _rel

log = logging.getLogger(__name__)


# ---- usage-агрегат обращений к эндпоинтам (замена ротируемым access-логам) ----
async def bump_endpoint_hit(endpoint: str, method: str, status: int) -> None:
    """Best-effort суточный счётчик обращения к HTTP-эндпоинту. Никогда не роняет запрос
    (вызывается из _usage_mw в finally). Ключ — (день, путь, метод, статус)."""
    try:
        day = time.strftime("%Y-%m-%d", time.localtime())
        await _conn().execute(
            "INSERT INTO endpoint_hits(day, endpoint, method, status, hits) VALUES(?,?,?,?,1) "
            "ON CONFLICT(day, endpoint, method, status) DO UPDATE SET hits = hits + 1",
            (day, (endpoint or "?")[:120], (method or "?")[:8], int(status)))
        await _conn().commit()
    except Exception:  # noqa: BLE001
        log.debug("bump_endpoint_hit failed", exc_info=True)


async def usage_stats(days: int = 7) -> list[dict]:
    """Агрегат обращений за последние `days` суток: (endpoint, method) с суммой хитов и
    числом ошибок (status>=400). Для usage-аудитов без текстовых логов."""
    days = max(1, min(int(days or 7), 365))
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    cur = await _conn().execute(
        "SELECT endpoint, method, SUM(hits) AS hits, "
        "SUM(CASE WHEN status >= 400 THEN hits ELSE 0 END) AS errors "
        "FROM endpoint_hits WHERE day >= ? GROUP BY endpoint, method ORDER BY hits DESC",
        (since,))
    return [{"endpoint": r["endpoint"], "method": r["method"],
             "hits": r["hits"], "errors": r["errors"]} for r in await cur.fetchall()]


# ---- журнал резервных копий (OpenTodo b557e341) ----
# Пишет app/backup.py после каждой попытки снимка, читает админка. Запись журнальная и
# never-raise: сбой журнала не имеет права сорвать сам бэкап.
_BACKUP_RUNS_KEEP = 200        # больше и не нужно: при снимке раз в 6 ч это ~50 суток истории


async def add_backup_run(ok: bool, path: str = "", size: int = 0, duration_ms: int = 0,
                         error: str = "") -> None:
    """Best-effort запись о попытке бэкапа (+ прунинг журнала до _BACKUP_RUNS_KEEP строк)."""
    try:
        await _conn().execute(
            "INSERT INTO backup_runs(ts, ok, path, size, duration_ms, error) VALUES(?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), 1 if ok else 0,
             str(_rel(path) or "")[:400], int(size or 0), int(duration_ms or 0), str(error or "")[:400]))
        await _conn().execute(
            "DELETE FROM backup_runs WHERE id NOT IN "
            "(SELECT id FROM backup_runs ORDER BY id DESC LIMIT ?)", (_BACKUP_RUNS_KEEP,))
        await _conn().commit()
    except Exception:  # noqa: BLE001
        log.debug("add_backup_run failed", exc_info=True)


async def list_backup_runs(limit: int = 20) -> list[dict]:
    """Последние попытки бэкапа, свежие сверху."""
    cur = await _conn().execute(
        "SELECT id, ts, ok, path, size, duration_ms, error FROM backup_runs "
        "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit or 20), _BACKUP_RUNS_KEEP)),))
    # path наружу — абсолютный: журнал бэкапов читает человек (панель «Бэкапы»), и по
    # относительной строке он свой файл не найдёт (C2).
    out = []
    for r in await cur.fetchall():
        d = dict(r)
        d["path"] = _abs(d.get("path")) or ""
        out.append(d)
    return out


async def last_backup_run() -> dict | None:
    rows = await list_backup_runs(1)
    return rows[0] if rows else None


# ---- телеметрия ошибок (OpenTodo f4de8627) ----
# Пишет app/errorlog.py из буфера, читает админка. Запись — журнальная и never-raise:
# сбой телеметрии не имеет права уронить обработчик, в котором случилась исходная ошибка.

async def upsert_error(fingerprint: str, source: str, level: str, message: str,
                       traceback: str = "", context: str = "", ts: str = "") -> tuple[str, int]:
    """Схлопывание одинаковых ошибок: ('new', 1) на первую, ('inc', N) на повтор.

    Ключ схлопывания — (fingerprint, seen=0). Прочитанная строка больше не собирает повторы:
    так владелец видит, что сбой ВЕРНУЛСЯ после разбора, а не просто подрос счётчик.
    Возврат ('', 0) — телеметрия молча не записалась (например, БД уже закрыта)."""
    ts = ts or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        cur = await _conn().execute(
            "UPDATE error_log SET count = count + 1, last_at = ?, message = ?, traceback = ?, "
            "level = ? WHERE fingerprint = ? AND seen = 0",
            (ts, message, traceback, level, fingerprint))
        if cur.rowcount:
            await _conn().commit()
            got = await _conn().execute(
                "SELECT count FROM error_log WHERE fingerprint = ? AND seen = 0", (fingerprint,))
            row = await got.fetchone()
            return "inc", (row["count"] if row else 0)
        await _conn().execute(
            "INSERT INTO error_log(fingerprint, source, level, message, traceback, context, "
            "count, first_at, last_at, seen) VALUES(?,?,?,?,?,?,1,?,?,0)",
            (fingerprint, source, level, message, traceback, context, ts, ts))
        await _conn().commit()
        return "new", 1
    except Exception:  # noqa: BLE001
        log.debug("upsert_error failed", exc_info=True)
        return "", 0


async def list_errors(limit: int = 200, only_new: bool = False) -> list[dict]:
    """Последние ошибки: свежие сверху. only_new — только непрочитанные.

    Сортировка по last_at, а не по id: при схлопывании id остаётся от ПЕРВОГО появления, и
    по id активно льющийся прямо сейчас сбой уезжал вниз (а при упоре в limit — вовсе
    выпадал из выдачи). id — только добивка на равных отметках. Индекс (seen, id DESC) на
    порядок больше не работает, но таблица маленькая: одинаковые сбои схлопнуты, а
    разобранные вычищает purge_old_errors."""
    limit = max(1, min(int(limit or 200), 1000))
    where = "WHERE seen = 0 " if only_new else ""
    cur = await _conn().execute(
        f"SELECT * FROM error_log {where}ORDER BY last_at DESC, id DESC LIMIT ?", (limit,))
    return [{"id": r["id"], "fingerprint": r["fingerprint"], "source": r["source"],
             "level": r["level"], "message": r["message"], "traceback": r["traceback"] or "",
             "context": r["context"] or "", "count": r["count"], "first_at": r["first_at"],
             "last_at": r["last_at"], "seen": r["seen"]} for r in await cur.fetchall()]


async def count_new_errors() -> int:
    """Бейдж раздела «Ошибки»: сколько непрочитанных записей (не повторов, а именно строк)."""
    try:
        cur = await _conn().execute("SELECT COUNT(*) AS c FROM error_log WHERE seen = 0")
        return (await cur.fetchone())["c"]
    except Exception:  # noqa: BLE001
        log.debug("count_new_errors failed", exc_info=True)
        return 0


async def mark_errors_seen(err_id: int | None = None) -> int:
    """Пометить прочитанной одну запись (err_id) или сразу все. Возвращает число затронутых."""
    try:
        if err_id:
            cur = await _conn().execute(
                "UPDATE error_log SET seen = 1 WHERE id = ? AND seen = 0", (int(err_id),))
        else:
            cur = await _conn().execute("UPDATE error_log SET seen = 1 WHERE seen = 0")
        await _conn().commit()
        return cur.rowcount or 0
    except Exception:  # noqa: BLE001
        log.debug("mark_errors_seen failed", exc_info=True)
        return 0


async def purge_old_errors(days: int = 30) -> int:
    """Уборка разобранных записей старше N суток (из суточного maintenance_loop).
    Непрочитанные не трогаем: их владелец ещё не видел, сколько бы им ни было лет."""
    try:
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - max(1, int(days or 30)) * 86400))
        cur = await _conn().execute(
            "DELETE FROM error_log WHERE seen = 1 AND last_at < ?", (cutoff,))
        await _conn().commit()
        return cur.rowcount or 0
    except Exception:  # noqa: BLE001
        log.debug("purge_old_errors failed", exc_info=True)
        return 0


# ---- Центр уведомлений админа (2026-07-16) ----
async def add_admin_notification(kind: str, *, title: str = "", body: str = "",
                                 image_path: str | None = None,
                                 payload: dict | None = None) -> int:
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO admin_notifications(kind, title, body, image_path, payload, status, "
        "created_at, updated_at) VALUES(?,?,?,?,?,'new',?,?)",
        (kind, (title or "").strip()[:120], (body or "").strip()[:4000], _rel(image_path),
         json.dumps(payload or {}, ensure_ascii=False), now, now))
    await _conn().commit()
    return cur.lastrowid


async def list_admin_notifications(include_done: bool = False, kind: str | None = None,
                                    exclude_kind: str | None = None) -> list[dict]:
    """kind/exclude_kind — фильтр по типу: раздел «Семейные счета» берёт только
    family_request, «Уведомления» — всё остальное (2026-07-17)."""
    conds, args = [], []
    if not include_done:
        conds.append("status = 'new'")
    if kind:
        conds.append("kind = ?")
        args.append(kind)
    if exclude_kind:
        conds.append("kind != ?")
        args.append(exclude_kind)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    cur = await _conn().execute(
        f"SELECT * FROM admin_notifications {where} ORDER BY id DESC LIMIT 100", args)
    out = []
    for r in await cur.fetchall():
        payload = _jload(r["payload"], {})
        out.append({"id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"],
                    "image_path": _abs(r["image_path"]) or "", "payload": payload,
                    "status": r["status"], "created_at": r["created_at"]})
    return out


async def get_admin_notification(nid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM admin_notifications WHERE id = ?", (nid,))
    r = await cur.fetchone()
    if not r:
        return None
    payload = _jload(r["payload"], {})
    return {"id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"],
            "image_path": _abs(r["image_path"]) or "", "payload": payload,
            "status": r["status"], "created_at": r["created_at"]}


async def update_admin_notification(nid: int, **fields) -> bool:
    """Точечное обновление (title/body/image_path/status/payload). image_path=None стирает
    картинку. payload принимает dict и сериализуется в JSON — нужен, чтобы уточнить уже
    висящую заявку (клиент перевыбрал партнёра семейного счёта), а не плодить вторую."""
    allowed = {k: v for k, v in fields.items()
               if k in ("title", "body", "image_path", "status", "payload")}
    if not allowed:
        return False
    if "payload" in allowed and not isinstance(allowed["payload"], str):
        allowed["payload"] = json.dumps(allowed["payload"] or {}, ensure_ascii=False)
    if "image_path" in allowed:      # C2: в БД путь только относительно DATA_DIR
        allowed["image_path"] = _rel(allowed["image_path"])
    sets = ", ".join(f"{k} = ?" for k in allowed)
    cur = await _conn().execute(
        f"UPDATE admin_notifications SET {sets}, updated_at = ? WHERE id = ?",
        (*allowed.values(), int(time.time()), nid))
    await _conn().commit()
    return cur.rowcount > 0


async def delete_admin_notification(nid: int) -> str | None:
    """Удалить уведомление; возвращает image_path (если был) для подчистки файла."""
    cur = await _conn().execute(
        "SELECT image_path FROM admin_notifications WHERE id = ?", (nid,))
    r = await cur.fetchone()
    if not r:
        return None
    await _conn().execute("DELETE FROM admin_notifications WHERE id = ?", (nid,))
    await _conn().commit()
    return _abs(r["image_path"]) or ""


async def count_new_admin_notifications(kind: str | None = None,
                                         exclude_kind: str | None = None) -> int:
    conds, args = ["status = 'new'"], []
    if kind:
        conds.append("kind = ?")
        args.append(kind)
    if exclude_kind:
        conds.append("kind != ?")
        args.append(exclude_kind)
    cur = await _conn().execute(
        f"SELECT COUNT(*) AS c FROM admin_notifications WHERE {' AND '.join(conds)}", args)
    return (await cur.fetchone())["c"]


async def try_mark_published(nid: int) -> bool:
    """Атомарно перевести release note new → published ДО начала рассылки: повторный тап
    «Опубликовать» (или ретрай во время многосекундного цикла отправки) получает False —
    клиенты не получат дубль."""
    cur = await _conn().execute(
        "UPDATE admin_notifications SET status = 'published', updated_at = ? "
        "WHERE id = ? AND status = 'new'", (int(time.time()), nid))
    await _conn().commit()
    return cur.rowcount > 0
