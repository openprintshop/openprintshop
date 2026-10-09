"""Задание, уехавшее на принтер: привязка текущей печати к заказу + снимок её настроек.

Две таблицы про один объект — «что сейчас крутится на машине и на чём»:

* print_binding — ключ по машине (binding_key: id принтера, а None -> принтер по умолчанию);
  set_print_binding/clear_print_binding/clear_print_binding_for_request пишутся парой на
  старте и финише печати;
* print_settings — снимок настроек, с которыми задание уехало, с поиском по хэшу и подбором.

Зависимость наружу ровно одна и односторонняя: binding_key -> store.printers.get_default_printer.

Выделено из app/db.py волной 3 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ.
"""
from __future__ import annotations

import json
import time

from .core import _conn, _jload
from .printers import get_default_printer


# ---- Привязка текущей печати к заказу (OpenTodo 70b5a590; по машинам — цикл 3, волна 6) ----
async def binding_key(printer_id: int | None = None) -> int:
    """Ключ привязки: id машины, а `None` — принтер по умолчанию.

    `None` означает «та машина, о которой говорят, когда машину не назвали» — так работали все
    вызовы, пока принтер был один, и так же обязаны работать дальше, иначе привязка мастера и
    её финиш разъедутся по разным строкам. 0 — установка вообще без карточек (всё из .env)."""
    if printer_id:
        return int(printer_id)
    p = await get_default_printer()
    return int(p.id) if p else 0


async def set_print_binding(request_id: int, job_name: str = "",
                            printer_id: int | None = None) -> None:
    """Привязать текущую печать МАШИНЫ к заказу. Сбрасывает флаги.

    Привязка своя у каждой машины: при одновременной печати общая строка означала бы, что
    вторая привязка вытесняет первую."""
    key = await binding_key(printer_id)
    now = int(time.time())
    await _conn().execute("DELETE FROM print_binding WHERE printer_id = ?", (key,))
    await _conn().execute(
        "INSERT INTO print_binding(printer_id, request_id, job_name, started_at) VALUES(?,?,?,?)",
        (key, request_id, job_name or "", now))
    await _conn().commit()


async def get_print_binding(printer_id: int | None = None) -> dict | None:
    """Активная привязка печать→заказ этой машины или None, если её нет."""
    key = await binding_key(printer_id)
    cur = await _conn().execute("SELECT * FROM print_binding WHERE printer_id = ?", (key,))
    r = await cur.fetchone()
    if not r or not r["request_id"]:
        return None
    return {k: r[k] for k in r.keys()}


async def list_print_bindings() -> list[dict]:
    """Все активные привязки — по одной на машину. Нужен экранам, которые показывают студию
    целиком, чтобы не спрашивать каждую карточку отдельно."""
    cur = await _conn().execute(
        "SELECT * FROM print_binding WHERE request_id IS NOT NULL ORDER BY printer_id")
    return [{k: r[k] for k in r.keys()} for r in await cur.fetchall()]


async def update_print_binding(printer_id: int | None = None, **fields) -> None:
    allowed = {"request_id", "job_name", "was_active", "finished", "confirmed"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    key = await binding_key(printer_id)
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE print_binding SET {assignments} WHERE printer_id = ?",
                          (*sets.values(), key))
    await _conn().commit()


async def clear_print_binding(printer_id: int | None = None) -> None:
    key = await binding_key(printer_id)
    await _conn().execute("DELETE FROM print_binding WHERE printer_id = ?", (key,))
    await _conn().commit()


async def clear_print_binding_for_request(request_id: int) -> int:
    """Снять привязки этого заказа НА ВСЕХ машинах. Нужно там, где заказ отменяют или удаляют:
    машину, на которой он печатался, вызывающий не знает, а оставленная привязка закрыла бы
    финишем уже несуществующий заказ."""
    cur = await _conn().execute("DELETE FROM print_binding WHERE request_id = ?", (int(request_id),))
    await _conn().commit()
    return cur.rowcount if cur.rowcount is not None else 0


# ---- настройки, с которыми задание уехало на принтер (OpenTodo 8e8d4873) ----
def _settings_row(r) -> dict:
    d = dict(r)
    for src, dst in (("values_json", "values"), ("start_json", "start")):
        d[dst] = _jload(d.pop(src), {})
    return d


async def save_print_settings(name: str, *, values: dict | None = None,
                              start: dict | None = None, slicer_name: str = "",
                              grams: float | None = None, minutes: float | None = None,
                              layers: int | None = None, uploaded: bool = False,
                              started: bool = False, norm_name: str = "",
                              printer_id: int | None = None) -> int:
    """Записать/дополнить снимок настроек задания по имени файла НА КОНКРЕТНОЙ МАШИНЕ.

    Заливка и старт — ДВА разных события одного файла (мастер может залить сегодня, а
    запустить завтра), поэтому обновляем поштучно и не затираем то, чего в этот раз не знаем.

    printer_id обязателен по смыслу, но необязателен по сигнатуре: старые вызовы и печати без
    карточки честно кладут NULL — это «машина неизвестна», а не «машина №0»."""
    nm = (name or "").strip()[:200]
    if not nm:
        return 0
    now = int(time.time())
    pid = int(printer_id) if printer_id else None
    cur = await _conn().execute(
        "SELECT * FROM print_settings WHERE name = ? AND printer_id IS ?", (nm, pid))
    row = await cur.fetchone()
    if row:
        sets: dict = {}
        if values:
            sets["values_json"] = json.dumps(values, ensure_ascii=False)
        if start is not None:
            sets["start_json"] = json.dumps(start, ensure_ascii=False)
        if slicer_name:
            sets["slicer"] = slicer_name[:80]
        for k, v in (("grams", grams), ("minutes", minutes), ("layers", layers)):
            if v is not None:
                sets[k] = v
        if norm_name:
            sets["norm_name"] = norm_name[:200]
        if uploaded:
            sets["uploaded_at"] = now
        if started:
            sets["started_at"] = now
        if sets:
            assignments = ", ".join(f"{k} = ?" for k in sets)
            await _conn().execute(f"UPDATE print_settings SET {assignments} WHERE id = ?",
                                  (*sets.values(), row["id"]))
            await _conn().commit()
        return int(row["id"])
    cur = await _conn().execute(
        "INSERT INTO print_settings(name, norm_name, values_json, start_json, slicer, grams, "
        "minutes, layers, uploaded_at, started_at, printer_id, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (nm, (norm_name or "")[:200], json.dumps(values or {}, ensure_ascii=False),
         json.dumps(start or {}, ensure_ascii=False), (slicer_name or "")[:80],
         grams, minutes, layers, now if uploaded else None, now if started else None, pid, now))
    await _conn().commit()
    return cur.lastrowid


async def get_print_settings(sid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM print_settings WHERE id = ?", (int(sid),))
    r = await cur.fetchone()
    return _settings_row(r) if r else None


async def find_print_settings(name: str, norm_name: str = "",
                              printer_id: int | None = None) -> dict | None:
    """Снимок по имени задания. Принтер отдаёт имя то с путём, то без расширения, поэтому
    ищем и по нормализованному имени (elegoo.normalize_job_name) — иначе снимок не нашёлся бы
    именно тогда, когда он нужен: в момент попадания печати в журнал.

    printer_id сужает поиск до СВОЕЙ машины: `plate_1.gcode` есть у обеих, и отдать снимок
    чужого принтера значит соврать про то, чем резали. Не нашли у своей — ищем без разбора
    машины: у записей до цикла 3 принтер не проставлен, и терять их нельзя."""
    nm = (name or "").strip()
    if not nm:
        return None
    pid = int(printer_id) if printer_id else None
    if pid:
        cur = await _conn().execute(
            "SELECT * FROM print_settings WHERE name = ? AND printer_id = ?", (nm, pid))
        r = await cur.fetchone()
        if r:
            return _settings_row(r)
    cur = await _conn().execute(
        "SELECT * FROM print_settings WHERE name = ? ORDER BY printer_id IS NULL, id DESC LIMIT 1",
        (nm,))
    r = await cur.fetchone()
    if r:
        return _settings_row(r)
    key = (norm_name or "").strip()
    if not key:
        return None
    cur = await _conn().execute(
        "SELECT * FROM print_settings WHERE norm_name = ? "
        "ORDER BY printer_id IS NULL, id DESC LIMIT 1", (key,))
    r = await cur.fetchone()
    return _settings_row(r) if r else None


async def list_print_settings(limit: int = 100) -> list[dict]:
    cur = await _conn().execute(
        "SELECT * FROM print_settings ORDER BY COALESCE(started_at, uploaded_at, created_at) DESC "
        "LIMIT ?", (int(limit),))
    return [_settings_row(r) for r in await cur.fetchall()]


async def prune_print_settings(keep: int = 400) -> int:
    """Оставить последние keep снимков: файл на принтере живёт недели, а таблица росла бы вечно.

    Снимки, на которые ССЫЛАЕТСЯ журнал печатей, не трогаем ни при каком лимите: карточка
    печати без «чем печатали» — ровно та потеря, ради которой задача и делалась."""
    cur = await _conn().execute(
        "DELETE FROM print_settings WHERE id NOT IN "
        "(SELECT id FROM print_settings ORDER BY id DESC LIMIT ?) "
        "AND id NOT IN (SELECT settings_id FROM print_journal WHERE settings_id IS NOT NULL)",
        (int(keep),))
    await _conn().commit()
    return cur.rowcount or 0
