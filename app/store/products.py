"""Готовые изделия / витрина (OpenTodo bd896aa6): таблица products и покупка со списанием.

Магазин уже напечатанного: карточка товара (заголовок, описание, цена, наличие, фото),
активность/сортировка и счётчик проданного.

buy_product — ТРАНЗАКЦИОННАЯ СВЯЗКА в одном UPDATE: наличие проверяется условием
`stock >= qty` прямо в UPDATE, поэтому две одновременные покупки последней штуки не могут
обе получить True (проигравший увидит rowcount 0).

Выделено из app/db.py волной 6 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ. Из внешнего мира нужны core._conn, models.Product
и stdlib time; aiosqlite — только ради аннотации `_to_product(r: aiosqlite.Row)` (под
`from __future__ import annotations` symtable её «не видит», брали текстовой сверкой).
"""
from __future__ import annotations

import time

import aiosqlite

from .core import _abs, _conn, _rel
from .models import Product

# ─────────────────────────── Готовые изделия / витрина (bd896aa6) ───────────────────────────


def _to_product(r: aiosqlite.Row) -> Product:
    return Product(
        id=r["id"], title=r["title"], description=r["description"] or "",
        price=r["price"] or 0.0, stock=int(r["stock"] or 0),
        photo_path=_abs(r["photo_path"]) or "", is_active=int(r["is_active"] or 0),
        sold=int(r["sold"] or 0), sort=int(r["sort"] or 0),
        created_at=r["created_at"] or 0, updated_at=r["updated_at"] or 0)


async def add_product(*, title: str, description: str = "", price: float = 0.0,
                      stock: int = 0, is_active: int = 1) -> int:
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO products(title, description, price, stock, is_active, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (title, description, float(price), int(stock), int(is_active), now, now))
    await _conn().commit()
    return cur.lastrowid


async def update_product(pid: int, **fields) -> None:
    allowed = {"title", "description", "price", "stock", "photo_path", "is_active", "sort"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "photo_path" in sets:      # C2: в БД путь только относительно DATA_DIR
        sets["photo_path"] = _rel(sets["photo_path"])
    sets["updated_at"] = int(time.time())
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE products SET {assignments} WHERE id = ?", (*sets.values(), pid))
    await _conn().commit()


async def delete_product(pid: int) -> None:
    await _conn().execute("DELETE FROM products WHERE id = ?", (pid,))
    await _conn().commit()


async def get_product(pid: int) -> Product | None:
    cur = await _conn().execute("SELECT * FROM products WHERE id = ?", (pid,))
    r = await cur.fetchone()
    return _to_product(r) if r else None


async def list_products(*, active_only: bool = True, in_stock_only: bool = False) -> list[Product]:
    conds = []
    if active_only:
        conds.append("is_active = 1")
    if in_stock_only:
        conds.append("stock > 0")
    q = "SELECT * FROM products" + (" WHERE " + " AND ".join(conds) if conds else "")
    q += " ORDER BY sort ASC, id DESC"
    cur = await _conn().execute(q)
    return [_to_product(r) for r in await cur.fetchall()]


async def count_products(active_only: bool = True) -> int:
    q = "SELECT COUNT(*) AS n FROM products" + (" WHERE is_active = 1" if active_only else "")
    cur = await _conn().execute(q)
    row = await cur.fetchone()
    return int(row["n"] or 0)


async def buy_product(pid: int, qty: int = 1) -> bool:
    """Атомарно списывает qty из наличия активного товара, если его хватает. True — списано,
    False — не хватило/товар неактивен. Гонку двух одновременных покупок исключает условие
    stock >= qty прямо в UPDATE (последний, кому не хватило, получит rowcount 0)."""
    qty = max(1, int(qty))
    cur = await _conn().execute(
        "UPDATE products SET stock = stock - ?, sold = sold + ?, updated_at = ? "
        "WHERE id = ? AND is_active = 1 AND stock >= ?",
        (qty, qty, int(time.time()), pid, qty))
    await _conn().commit()
    return cur.rowcount > 0
