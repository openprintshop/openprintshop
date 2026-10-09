"""Очередь печати с позициями: активные заказы, нормализация queue_pos, перестановка, срез LCD.

Лист по зависимостям: ни одного вызова в другие доменные модули — только _conn и два
складских литерала статусов (_RESERVING_STATUSES/_RES_PH), уехавших волной 2.
queue_normalize/queue_move/_queue_active_rows перенумеровывают requests сплошняком под одним
commit — связка режется только целиком.

РЕШЕНО ВОЛНОЙ 4: _RESERVING_STATUSES/_RES_PH ОСТАЛИСЬ в store/plastics, а не переехали
в модуль заказов, как предполагала прежняя заметка. Причина: складу они нужны самому
(резерв под активные заказы), и перенос дал бы обратный импорт plastics -> orders поверх
уже существующего orders -> plastics. Теперь их импортируют трое: склад, эта очередь и
store/orders.queue_load. Тела queue_items/lcd_print_queue читают requests и plastics голым
SQL — имён чужих доменов не зовут, волна 4 их не сломала, но JOIN-ы держать в голове.

Выделено из app/db.py волной 3 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ.
"""
from __future__ import annotations

from .core import _abs, _conn
from .plastics import _RES_PH, _RESERVING_STATUSES


# ---- Очередь печати с позициями (d79b54bb) ----
async def _queue_active_rows():
    """Активные заказы (new/in_progress) в порядке очереди: по queue_pos, новые (NULL) — в хвост."""
    cur = await _conn().execute(
        "SELECT id, queue_pos, minutes_total, model_label, user_id, user_name, status, quantity, "
        "preview_path, done_photo "
        f"FROM requests WHERE status IN ({_RES_PH}) "
        "ORDER BY CASE WHEN queue_pos IS NULL THEN 1 ELSE 0 END, queue_pos, id",
        _RESERVING_STATUSES)
    return await cur.fetchall()


async def queue_normalize() -> None:
    """Переназначить активным заказам позиции 1..N в текущем порядке (контигуально, без дыр)."""
    rows = await _queue_active_rows()
    changed = False
    for i, r in enumerate(rows, 1):
        if r["queue_pos"] != i:
            await _conn().execute("UPDATE requests SET queue_pos=? WHERE id=?", (i, r["id"]))
            changed = True
    if changed:
        await _conn().commit()


async def queue_items() -> list[dict]:
    """Очередь печати: активные заказы по порядку с позицией, временем «впереди» и готовности."""
    await queue_normalize()
    rows = await _queue_active_rows()
    items: list[dict] = []
    ahead = 0.0
    for i, r in enumerate(rows, 1):
        mins = float(r["minutes_total"] or 0)
        items.append({"id": r["id"], "pos": i, "label": r["model_label"], "user": r["user_name"],
                      "status": r["status"], "qty": r["quantity"], "minutes": mins,
                      # пути для миниатюры: рендер модели, а если его нет (заказ мастера /
                      # индивидуальный) — фото результата. Подписанные URL строит webapp.
                      "preview_path": _abs(r["preview_path"]), "done_photo": _abs(r["done_photo"]),
                      "ahead_minutes": ahead, "done_minutes": ahead + mins})
        ahead += mins
    return items


async def lcd_print_queue(limit: int = 10) -> list[dict]:
    """Очередь ненапечатанных заказов для LCD-дашборда (design_handoff_lcd): активные
    (new/in_progress) по порядку очереди печати, с материалом, временем и датой создания.
    Тот же набор, что admin «Очередь печати», но с полями под контракт экрана."""
    await queue_normalize()
    cur = await _conn().execute(
        "SELECT id, model_label, user_name, quantity, minutes_total, plastic_name, created_at "
        f"FROM requests WHERE status IN ({_RES_PH}) "
        "ORDER BY CASE WHEN queue_pos IS NULL THEN 1 ELSE 0 END, queue_pos, id LIMIT ?",
        (*_RESERVING_STATUSES, limit))
    return [dict(r) for r in await cur.fetchall()]


async def queue_move(rid: int, delta: int) -> bool:
    """Сдвинуть заказ в очереди на delta позиций (−1 вверх / +1 вниз) обменом с соседом."""
    await queue_normalize()
    rows = await _queue_active_rows()
    idx = next((i for i, r in enumerate(rows) if r["id"] == rid), -1)
    if idx < 0:
        return False
    j = idx + delta
    if j < 0 or j >= len(rows):
        return False
    a, pa = rows[idx]["id"], rows[idx]["queue_pos"]
    b, pb = rows[j]["id"], rows[j]["queue_pos"]
    await _conn().execute("UPDATE requests SET queue_pos=? WHERE id=?", (pb, a))
    await _conn().execute("UPDATE requests SET queue_pos=? WHERE id=?", (pa, b))
    await _conn().commit()
    return True


async def queue_position(rid: int) -> dict | None:
    """Для клиента: {pos, total, done_minutes} активного заказа, иначе None (не в очереди)."""
    items = await queue_items()
    for it in items:
        if it["id"] == rid:
            return {"pos": it["pos"], "total": len(items), "done_minutes": it["done_minutes"]}
    return None
