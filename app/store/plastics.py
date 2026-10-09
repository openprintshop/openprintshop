"""Склад филамента ЦЕЛИКОМ: пластики, остатки по цветам, катушки, журнал, резерв, COGS.

Один модуль на весь склад — это не укрупнение, а требование двух правил метода:

1. ТРАНЗАКЦИОННАЯ СВЯЗКА. deduct_stock/deduct_color_stock пишут plastics.stock_grams и
   plastic_color_stock и тем же вызовом зеркалят расход в spools+stock_ledger через
   _dual_write_spool (dual-write Фазы A мастер-плана учёта). apply_order_stock атомарно
   захватывает requests.stock_deducted, раскладывает план по цветам и считает
   requests.cogs_actual. Четыре таблицы в одном логическом действии режутся только целиком.
2. ЦИКЛ. Пластики зовут катушечные _canon_color/_dual_write_spool, катушки зовут
   deduct_color_stock_split/get_color_stock — двусторонний импорт между двумя store-модулями
   метод запрещает.

Пользовательские оттенки цветов (color_shades → materials.color_to_rgbs) прицеплены сюда
же: это тот же цветовой словарь, что и пер-цветовые остатки.

Выделено из app/db.py волной 2 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ (ленивых импортов и global в них не было). Изменилось
одно: модульный `log` теперь app.store.plastics, а не app.db — в боевом логе поменяется
префикс у записей этого модуля (см. вызовы log.* ниже; фильтров по имени «app.db» в коде нет).
Три функции самопечати (create_self_print_order/admin_would_spend/is_self_print_order)
лежали внутри секции катушек, но пишут только requests — волной 4 они уехали
в app/store/orders.py, а не сюда.

ЛОВУШКА ДЛЯ ТЕСТОВ: get_plastic зовётся ГОЛЫМ именем из _deduct_stock_legacy, поэтому
monkeypatch.setattr(db, "get_plastic", …) до складского пути больше не достаёт — подменять
надо app.store.plastics.get_plastic (тем же правилом, что у web-модулей endgame).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

import aiosqlite

from ..profilepaths import abs_profile_str, rel_profile
from .core import _abs, _conn, _jload, _rel
from .models import Plastic

log = logging.getLogger(__name__)


# ---- пластики ----
def _to_plastic(r: aiosqlite.Row) -> Plastic:
    return Plastic(
        id=r["id"], name=r["name"], price_per_kg=r["price_per_kg"], density=r["density"],
        is_active=r["is_active"], material_type=r["material_type"],
        filament_profile=abs_profile_str(r["filament_profile"]),
        photo_path=_abs(r["photo_path"]) or "", description=r["description"], uses=r["uses"], colors=r["colors"],
        is_default=r["is_default"],
        stock_grams=(r["stock_grams"] if "stock_grams" in r.keys() else 0.0) or 0.0,
        low_threshold=(r["low_threshold"] if "low_threshold" in r.keys() else 0.0) or 0.0,
        failure_rate_pct=(r["failure_rate_pct"] if "failure_rate_pct" in r.keys() else None),
        best_quality_id=(r["best_quality_id"] if "best_quality_id" in r.keys() else None),
        price_factor=(r["price_factor"] if "price_factor" in r.keys() else 1.0) or 1.0,
        category=(r["category"] if "category" in r.keys() else "") or "",
        spool_weight_g=(r["spool_weight_g"] if "spool_weight_g" in r.keys() else 0.0) or 0.0,
        spool_note=(r["spool_note"] if "spool_note" in r.keys() else "") or "",
    )


async def list_plastics(active_only: bool = False) -> list[Plastic]:
    sql = "SELECT * FROM plastics"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    cur = await _conn().execute(sql)
    return [_to_plastic(r) for r in await cur.fetchall()]


async def get_plastic(plastic_id: int) -> Plastic | None:
    cur = await _conn().execute("SELECT * FROM plastics WHERE id = ?", (plastic_id,))
    r = await cur.fetchone()
    return _to_plastic(r) if r else None


async def get_plastic_by_name(name: str) -> Plastic | None:
    """Пластик по точному имени — для привязки печати к складу (журнал/опрос, 0f848906)."""
    if not name:
        return None
    cur = await _conn().execute("SELECT * FROM plastics WHERE name = ? LIMIT 1", (name,))
    r = await cur.fetchone()
    return _to_plastic(r) if r else None


async def add_plastic(name: str, price_per_kg: float, density: float, material_type: str = "PLA",
                      filament_profile: str = "", description: str = "") -> int:
    cur = await _conn().execute(
        "INSERT INTO plastics(name, price_per_kg, density, is_active, created_at, material_type, "
        "filament_profile, description) VALUES(?,?,?,1,?,?,?,?)",
        (name, price_per_kg, density, int(time.time()), material_type,
         rel_profile(filament_profile), description),
    )
    await _conn().commit()
    return cur.lastrowid


async def update_plastic(plastic_id: int, **fields) -> None:
    allowed = {"name", "price_per_kg", "density", "is_active", "material_type", "filament_profile",
               "photo_path", "description", "uses", "colors", "stock_grams", "low_threshold",
               "failure_rate_pct", "best_quality_id", "price_factor", "category",
               "spool_weight_g", "spool_note"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "photo_path" in sets:      # C2: в БД путь только относительно DATA_DIR
        sets["photo_path"] = _rel(sets["photo_path"])
    if "filament_profile" in sets:   # C2 (хвост): профиль слайсера — маркером корня
        sets["filament_profile"] = rel_profile(sets["filament_profile"])
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE plastics SET {assignments} WHERE id = ?", (*sets.values(), plastic_id))
    await _conn().commit()


async def delete_plastic(plastic_id: int) -> None:
    await _conn().execute("DELETE FROM plastics WHERE id = ?", (plastic_id,))
    await _conn().commit()


async def set_default_plastic(plastic_id: int) -> None:
    """Делает пластик значением по умолчанию (как «основной принтер»): снимает флаг
    у всех, ставит у выбранного и включает его (по умолчанию обязан быть доступен)."""
    await _conn().execute("UPDATE plastics SET is_default = 0")
    await _conn().execute("UPDATE plastics SET is_default = 1, is_active = 1 WHERE id = ?", (plastic_id,))
    await _conn().commit()


async def get_default_plastic() -> Plastic | None:
    cur = await _conn().execute(
        "SELECT * FROM plastics WHERE is_active = 1 ORDER BY is_default DESC, name COLLATE NOCASE LIMIT 1")
    r = await cur.fetchone()
    return _to_plastic(r) if r else None


async def recent_plastics_for_user(user_id: int, limit: int = 5) -> list[Plastic]:
    cur = await _conn().execute(
        "SELECT p.* FROM plastics p JOIN (SELECT plastic_id, MAX(id) mx FROM orders WHERE user_id=? "
        "GROUP BY plastic_id) o ON o.plastic_id = p.id WHERE p.is_active = 1 ORDER BY o.mx DESC LIMIT ?",
        (user_id, limit))
    return [_to_plastic(r) for r in await cur.fetchall()]


async def popular_plastics(limit: int = 5) -> list[Plastic]:
    cur = await _conn().execute(
        "SELECT * FROM plastics WHERE is_active = 1 ORDER BY uses DESC, name COLLATE NOCASE LIMIT ?", (limit,))
    return [_to_plastic(r) for r in await cur.fetchall()]


async def increment_plastic_uses(pid: int) -> None:
    await _conn().execute("UPDATE plastics SET uses = uses + 1 WHERE id = ?", (pid,))
    await _conn().commit()


async def _deduct_stock_legacy(pid: int, grams: float) -> tuple[float, bool]:
    """Списание ТОЛЬКО общего склада материала (plastics.stock_grams), без зеркала в катушки —
    чтобы вызов из deduct_color_stock не задваивал dual-write. Логика прежняя.

    Списываем ОТНОСИТЕЛЬНЫМ UPDATE, а не «прочитал → посчитал в питоне → записал абсолют»:
    соединение с БД одно на бота и веб, транзакционных границ вокруг операции нет, и чужой
    коммит, прошедший между чтением и записью, абсолютной записью затирался молча (два
    одновременных списания уносили со склада граммы только одного). Клампим в нуле прямо в
    SQL — это прежний max(0.0, ...): ниже нуля общий склад не уходил и не уходит.
    Предварительное чтение осталось: по нему решается «учёт не ведётся» (stock_grams <= 0 →
    не пишем ничего) и считается пересечение порога «мало»."""
    p = await get_plastic(pid)
    if not p or p.stock_grams <= 0:
        return (0.0, False)
    await _conn().execute(
        "UPDATE plastics SET stock_grams = MAX(0, COALESCE(stock_grams, 0) - ?) WHERE id = ?",
        (max(0.0, grams), pid))
    await _conn().commit()
    cur = await _conn().execute("SELECT stock_grams FROM plastics WHERE id = ?", (pid,))
    row = await cur.fetchone()
    new = float((row["stock_grams"] if row else 0.0) or 0.0)
    crossed = p.low_threshold > 0 and p.stock_grams > p.low_threshold and new <= p.low_threshold
    return (new, crossed)


async def deduct_stock(pid: int, grams: float, *, ref_type: str | None = None,
                       ref_id: int | None = None) -> tuple[float, bool]:
    """Списывает grams со склада пластика + best-effort зеркалит в катушки «без цвета» (Фаза A).
    Если общий склад не ведётся (stock_grams<=0) — legacy ничего, но катушки всё равно спишутся,
    если они по этому пластику есть. ref_type/ref_id — на что списание ссылается (для линка
    печать↔заказ/журнал в stock_ledger). Возвращает (новый_остаток, пересёк_ли_порог «мало»).

    Общий склад, который ведёт принтер (катушки CC2 Control без цвета), не списываем: расход
    он меряет сам, а наше списание было бы вторым."""
    if await _cc2_owned(pid, ""):
        return (await _stock_now(pid, ""), False)
    await _dual_write_spool(pid, "", grams, ref_type=ref_type, ref_id=ref_id)
    return await _deduct_stock_legacy(pid, grams)


# ---- остаток филамента по отдельным цветам ----
async def get_color_stock(pid: int) -> dict[str, dict]:
    """{цвет: {"stock": г, "low": г}} — пер-цветовые остатки материала (для админ-карточки)."""
    cur = await _conn().execute(
        "SELECT color, stock_grams, low_threshold FROM plastic_color_stock WHERE plastic_id = ?", (pid,))
    return {r["color"]: {"stock": r["stock_grams"] or 0.0, "low": r["low_threshold"] or 0.0}
            for r in await cur.fetchall()}


async def set_color_stock(pid: int, color: str, stock: float, low: float) -> None:
    disp = " ".join((color or "").split())          # показываем цвет как ввели (регистр сохраняем)
    if not disp:
        return
    canon = _canon_color(disp)                        # но дедуп по canon (Решение 5): 'Чёрный'/'черный' → одно
    cur = await _conn().execute(
        "SELECT color FROM plastic_color_stock WHERE plastic_id=?", (pid,))
    existing = next((r["color"] for r in await cur.fetchall() if _canon_color(r["color"]) == canon), None)
    key = existing or disp                            # обновляем уже существующую строку того же цвета
    await _conn().execute(
        "INSERT INTO plastic_color_stock(plastic_id, color, stock_grams, low_threshold) "
        "VALUES(?,?,?,?) ON CONFLICT(plastic_id, color) DO UPDATE SET "
        "stock_grams=excluded.stock_grams, low_threshold=excluded.low_threshold",
        (pid, key, max(0.0, stock), max(0.0, low)))
    await _conn().commit()


async def deduct_color_stock(pid: int, color: str | None, grams: float, *, ref_type: str | None = None,
                             ref_id: int | None = None) -> tuple[float, bool]:
    """Списывает grams со склада КОНКРЕТНОГО цвета, если по нему ведётся остаток. Если цвет не
    задан или по нему склад не ведётся (stock<=0) — откат на общий склад материала (deduct_stock),
    т.е. без пер-цветового учёта поведение прежнее. ref_type/ref_id пробрасываются в stock_ledger
    (линк печать↔заказ/журнал). Возвращает (новый_остаток, пересёк_порог).

    Остаток цвета уменьшается ОТНОСИТЕЛЬНЫМ UPDATE — по той же причине, что и в
    _deduct_stock_legacy: абсолютная запись затирала чужое списание того же цвета, прошедшее
    между чтением и записью. Кламп в нуле в SQL — прежний max(0.0, ...).

    Цвет, который ведёт принтер (есть катушка CC2 Control этого цвета), не списываем вовсе:
    его остаток — сумма катушек, а их расход принтер меряет сам."""
    canon = _canon_color(color)
    if await _cc2_owned(pid, canon):
        return (await _stock_now(pid, canon), False)
    if canon:
        cur = await _conn().execute(
            "SELECT color, stock_grams, low_threshold FROM plastic_color_stock WHERE plastic_id=?", (pid,))
        match = next((r for r in await cur.fetchall()          # сопоставляем по canon (регистр не важен)
                      if _canon_color(r["color"]) == canon and (r["stock_grams"] or 0) > 0), None)
        if match:
            old = float(match["stock_grams"])
            low = float(match["low_threshold"] or 0)
            await _conn().execute(
                "UPDATE plastic_color_stock SET stock_grams = MAX(0, COALESCE(stock_grams, 0) - ?) "
                "WHERE plastic_id=? AND color=?", (max(0.0, grams), pid, match["color"]))
            await _conn().commit()
            cur = await _conn().execute(
                "SELECT stock_grams FROM plastic_color_stock WHERE plastic_id=? AND color=?",
                (pid, match["color"]))
            row = await cur.fetchone()
            new = float((row["stock_grams"] if row else 0.0) or 0.0)
            await _dual_write_spool(pid, canon, grams, ref_type=ref_type, ref_id=ref_id)
            return (new, low > 0 and old > low and new <= low)
    # фолбэк на общий склад — но цвет для катушек НЕ теряем (мимо публичного deduct_stock, чтобы
    # dual-write не задвоился): зеркалим с реальным цветом + списываем legacy-агрегат.
    await _dual_write_spool(pid, canon, grams, ref_type=ref_type, ref_id=ref_id)
    return await _deduct_stock_legacy(pid, grams)


async def deduct_color_stock_split(
    pid: int, colors: list[str] | None, grams_total: float,
    per_color_grams: list[float] | None = None, *, ref_type: str | None = None,
    ref_id: int | None = None) -> tuple[float, bool]:
    """Списывает филамент многоцветного заказа по РЕАЛЬНОМУ расходу каждого цвета.

    colors — выбранные цвета заказа В ПОРЯДКЕ экструдеров; per_color_grams — граммы на тот же
    экструдер (из inspect3mf/слайсера), в том же порядке. Списываем граммы каждого цвета с его
    собственного склада (deduct_color_stock на каждый цвет). Возвращаем (остаток ПЕРВОГО цвета,
    пересечён ли его порог) — для прежнего текста алерта «низкий остаток».

    Фолбэк на прежнее поведение (весь объём с ПЕРВОГО цвета) если: цветов меньше двух,
    разбивка не дана / не совпадает по длине с цветами / неположительная в сумме. Так
    одноцветные заказы и случаи без пер-экструдерных данных работают ровно как раньше."""
    colors = [c for c in (colors or []) if (c or "").strip()]
    first = colors[0] if colors else None
    # Нет осмысленной разбивки по >=2 цветам → прежний путь: весь объём с первого цвета.
    if (len(colors) < 2 or not per_color_grams or len(per_color_grams) != len(colors)
            or sum(max(0.0, g) for g in per_color_grams) <= 0):
        return await deduct_color_stock(pid, first, grams_total, ref_type=ref_type, ref_id=ref_id)
    # Масштабируем разбивку под фактический grams_total (включает продувку на многоцвет и ×qty),
    # сохраняя пропорции пер-экструдерного расхода — сумма списанного сходится к grams_total.
    parts = [max(0.0, g) for g in per_color_grams]
    scale = grams_total / sum(parts)
    first_new, first_crossed = 0.0, False
    for i, (color, part) in enumerate(zip(colors, parts)):
        new, crossed = await deduct_color_stock(pid, color, part * scale, ref_type=ref_type, ref_id=ref_id)
        if i == 0:
            first_new, first_crossed = new, crossed
    return (first_new, first_crossed)


# ---- катушки (spools) + журнал движений (stock_ledger) — Фаза A мастер-плана учёта ----
# Остаток катушки = кэш remaining_g = SUM(stock_ledger.delta_g по ней) (по образцу balance_tx:
# balance = SUM(amount)). net_weight_g — номинал для «% осталось». В Фазе A катушки — надстройка
# и best-effort dual-write рядом с legacy plastics.stock_grams/plastic_color_stock, НЕ источник
# истины: если по пластику катушек нет, ничего не ломается (учёт просто не ведётся катушками).
def _canon_color(s: str) -> str:
    """Канонический ключ цвета для сопоставления остатков: без крайних/двойных пробелов, регистр
    сложен (casefold), ё→е. «Чёрный»/«черный »/«Черный» → «черный». Пусто → ''. Использовать
    ВЕЗДЕ, где цвет сравнивается с остатком (Решение 5 мастер-плана)."""
    return " ".join((s or "").split()).casefold().replace("ё", "е")


def _to_spool(r: aiosqlite.Row) -> dict:
    return {"id": r["id"], "plastic_id": r["plastic_id"], "color": r["color"],
            "net_weight_g": r["net_weight_g"] or 0.0, "remaining_g": r["remaining_g"] or 0.0,
            "low_threshold": r["low_threshold"] or 0.0, "cost": r["cost"], "supplier": r["supplier"],
            "status": r["status"], "note": r["note"], "purchased_at": r["purchased_at"],
            "created_at": r["created_at"],
            # id катушки в CC2 Control: остаток такой катушки меряет принтер (см. секцию CC2 ниже)
            "cc2_id": (r["cc2_spool_id"] if "cc2_spool_id" in r.keys() else None) or None}


async def _spool_recalc(spool_id: int) -> float:
    """Пересчитать кэш остатка катушки из журнала (remaining_g = SUM(delta_g)); при <=0 пометить
    'empty' (archived не трогаем). Возвращает новый остаток. Коммит — на вызывающей стороне."""
    cur = await _conn().execute(
        "SELECT COALESCE(SUM(delta_g),0) AS s FROM stock_ledger WHERE spool_id=?", (int(spool_id),))
    rem = float((await cur.fetchone())["s"] or 0.0)
    await _conn().execute(
        "UPDATE spools SET remaining_g=?, status=CASE WHEN status='archived' THEN 'archived' "
        "WHEN ? <= 0 THEN 'empty' ELSE 'active' END WHERE id=?", (rem, rem, int(spool_id)))
    return rem


async def add_stock_ledger(spool_id: int, delta_g: float, reason: str, *, ref_type: str | None = None,
                           ref_id: int | None = None, note: str | None = None) -> float:
    """Записать движение остатка в журнал и пересчитать кэш катушки. Возвращает новый остаток."""
    await _conn().execute(
        "INSERT INTO stock_ledger(spool_id, delta_g, reason, ref_type, ref_id, note, at) "
        "VALUES(?,?,?,?,?,?,?)",
        (int(spool_id), float(delta_g), reason, ref_type, ref_id, note, int(time.time())))
    rem = await _spool_recalc(spool_id)
    await _conn().commit()
    return rem


async def add_spool(plastic_id: int, color: str, net_weight_g: float, *, low_threshold: float = 0.0,
                    cost: float | None = None, supplier: str | None = None, note: str | None = None,
                    purchased_at: int | None = None, reason: str = "purchase") -> int:
    """Приход катушки: создать + стартовое движение на её вес нетто. Возвращает id катушки."""
    now = int(time.time())
    net = max(0.0, float(net_weight_g or 0))
    cur = await _conn().execute(
        "INSERT INTO spools(plastic_id, color, net_weight_g, remaining_g, low_threshold, cost, "
        "supplier, status, note, purchased_at, created_at) VALUES(?,?,?,?,?,?,?, 'active', ?, ?, ?)",
        (int(plastic_id), _canon_color(color), net, net, max(0.0, float(low_threshold or 0)),
         cost, supplier, note, purchased_at or now, now))
    sid = cur.lastrowid
    await _conn().execute("INSERT INTO stock_ledger(spool_id, delta_g, reason, at) VALUES(?,?,?,?)",
                          (sid, net, reason, now))
    await _conn().commit()
    return sid


async def get_spool(spool_id: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM spools WHERE id=?", (int(spool_id),))
    r = await cur.fetchone()
    return _to_spool(r) if r else None


async def list_spools(plastic_id: int | None = None, *, active_only: bool = False) -> list[dict]:
    conds, args = [], []
    if plastic_id is not None:
        conds.append("plastic_id=?"); args.append(int(plastic_id))
    if active_only:
        conds.append("status='active'")
    sql = "SELECT * FROM spools" + (" WHERE " + " AND ".join(conds) if conds else "")
    sql += " ORDER BY (status='active') DESC, id"
    cur = await _conn().execute(sql, args)
    return [_to_spool(r) for r in await cur.fetchall()]


async def spool_ledger(spool_id: int) -> list[dict]:
    cur = await _conn().execute(
        "SELECT * FROM stock_ledger WHERE spool_id=? ORDER BY id", (int(spool_id),))
    return [{"id": r["id"], "delta_g": r["delta_g"], "reason": r["reason"], "ref_type": r["ref_type"],
             "ref_id": r["ref_id"], "note": r["note"], "at": r["at"]} for r in await cur.fetchall()]


async def set_spool_status(spool_id: int, status: str) -> None:
    """Пометить катушку active|empty|archived вручную. При возврате в active — пересчёт из журнала."""
    status = status if status in ("active", "empty", "archived") else "active"
    await _conn().execute("UPDATE spools SET status=? WHERE id=?", (status, int(spool_id)))
    if status == "active":
        await _spool_recalc(spool_id)
    await _conn().commit()


async def spool_stock_by_color(plastic_id: int) -> dict[str, dict]:
    """{canon_color: {"stock": г, "low": г, "spools": n}} — агрегат остатка катушек по цвету.
    Считаем НЕ-архивные (active+empty), чтобы цвет с опустевшей катушкой честно показывал остаток
    0 (out-of-stock), а не «исчезал» из сигнала. Ключ '' = обобщённый (без цвета) остаток."""
    cur = await _conn().execute(
        "SELECT color, COALESCE(SUM(remaining_g),0) AS stock, MAX(low_threshold) AS low, "
        "COUNT(*) AS n FROM spools WHERE plastic_id=? AND status!='archived' GROUP BY color",
        (int(plastic_id),))
    return {r["color"]: {"stock": float(r["stock"] or 0), "low": float(r["low"] or 0),
                         "spools": r["n"]} for r in await cur.fetchall()}


async def spool_stock_total(plastic_id: int) -> float:
    cur = await _conn().execute(
        "SELECT COALESCE(SUM(remaining_g),0) AS s FROM spools WHERE plastic_id=? AND status='active'",
        (int(plastic_id),))
    return float((await cur.fetchone())["s"] or 0.0)


async def deduct_from_spools(plastic_id: int, color: str | None, grams: float, *, reason: str = "print",
                             ref_type: str | None = None, ref_id: int | None = None) -> float:
    """Списать grams с активных катушек пластика нужного цвета (FIFO: старые сначала), запись в
    журнал по каждой затронутой катушке. Приоритет: точный цвет → «без цвета» ('') того же
    пластика. В другие НАЗВАННЫЕ цвета не переливаем (чтобы не путать учёт). Возвращает реально
    списанное (0, если катушек нет — тогда учёт катушками просто не ведётся).

    Катушки CC2 Control не трогаем: их расход меряет принтер (см. секцию CC2 ниже)."""
    grams = max(0.0, float(grams or 0))
    if grams <= 0:
        return 0.0
    canon = _canon_color(color) if color else ""
    order = [canon, ""] if canon else [""]
    left, taken, seen = grams, 0.0, set()
    for want in order:
        if left <= 1e-9:
            break
        cur = await _conn().execute(
            "SELECT id, remaining_g FROM spools WHERE plastic_id=? AND color=? AND status='active' "
            "AND remaining_g > 0 AND cc2_spool_id IS NULL ORDER BY id", (int(plastic_id), want))
        for r in await cur.fetchall():
            if left <= 1e-9:
                break
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            take = min(float(r["remaining_g"] or 0), left)
            if take <= 0:
                continue
            await _conn().execute(
                "INSERT INTO stock_ledger(spool_id, delta_g, reason, ref_type, ref_id, at) "
                "VALUES(?,?,?,?,?,?)", (r["id"], -take, reason, ref_type, ref_id, int(time.time())))
            await _spool_recalc(r["id"])
            left -= take
            taken += take
    if taken > 0:
        await _conn().commit()
    return taken


async def _dual_write_spool(plastic_id: int, color: str | None, grams: float, *,
                            ref_type: str | None = None, ref_id: int | None = None) -> None:
    """Best-effort зеркалирование расхода в катушки рядом с legacy-списанием (Фаза A). Никогда не
    роняет основной путь: если катушек нет или что-то не так — тихо ничего. ref_type/ref_id линкуют
    движение к заказу/журналу (для реальной себестоимости по факту, Фаза C)."""
    try:
        await deduct_from_spools(plastic_id, color, grams, reason="print",
                                 ref_type=ref_type, ref_id=ref_id)
    except Exception:  # noqa: BLE001
        log.debug("spool dual-write failed pid=%s", plastic_id, exc_info=True)


# ---- катушки CC2 Control (08.10.2026): принтер меряет расход сам, здесь — кэш и фолбэк ----
# Служба CC2 Control на принтере с прошивкой сообщества ведёт свой склад катушек (spools.json) и
# списывает с катушки то, что принтер РЕАЛЬНО выдавил из её лотка (print_stats.filament_used,
# с продувками между цветами, отменёнными и упавшими печатями). Оценка слайсера на трёхцветной
# печати владельца дала 8,68 м при 11,32 м настоящих — поэтому её остаток и есть правда.
#
# Катушка с cc2_spool_id ПРИНАДЛЕЖИТ принтеру, и наше списание того же расхода было бы вторым:
#   * deduct_from_spools такие катушки не трогает;
#   * цвет пластика, у которого есть хоть одна такая катушка, тоже принадлежит принтеру:
#     deduct_color_stock / deduct_stock его не списывают, а plastic_color_stock (и общий склад —
#     для катушек без цвета) пересчитывается из катушек на каждой сверке;
#   * stock_ledger таких катушек пишет только сверка (cc2_apply_snapshot): события журнала
#     принтера (печать, кончилась, поправка) переносятся по одному разу, невязка закрывается
#     строкой «сверка с CC2 Control». remaining_g по-прежнему = SUM(delta_g).
# Сеть, частота опроса и алерты — app/cc2spools.py; здесь только база.

CC2_RECONCILE_G = 0.05      # невязка меньше — округление их журнала (%.3f), строку не пишем
CC2_JOB_SLACK_S = 180       # окно сверки события принтера с записью журнала печатей, ±с
_CC2_REASON = {"print": "print", "runout": "writeoff", "correction": "correction"}
_CC2_ID = re.compile(r"^[0-9a-f]{16}$")


def cc2_id_ok(cc2_id: str) -> bool:
    """id катушки CC2 Control: 16 строчных шестнадцатеричных цифр (их spool_id_ok)."""
    return bool(_CC2_ID.match(cc2_id or ""))


async def spool_is_cc2(spool_id: int) -> bool:
    """Остаток этой катушки меряет принтер (она заведена в CC2 Control)."""
    cur = await _conn().execute("SELECT cc2_spool_id FROM spools WHERE id=?", (int(spool_id),))
    r = await cur.fetchone()
    return bool(r and r["cc2_spool_id"])


async def _cc2_owned(plastic_id: int, canon: str) -> bool:
    """Склад этого цвета (canon; '' — общий склад без цвета) ведёт принтер."""
    cur = await _conn().execute(
        "SELECT 1 FROM spools WHERE plastic_id=? AND color=? AND cc2_spool_id IS NOT NULL LIMIT 1",
        (int(plastic_id), canon or ""))
    return await cur.fetchone() is not None


async def cc2_owned_colors(plastic_id: int) -> set[str]:
    """canon-цвета пластика, чей склад ведёт принтер ('' — общий склад без цвета)."""
    cur = await _conn().execute(
        "SELECT DISTINCT color FROM spools WHERE plastic_id=? AND cc2_spool_id IS NOT NULL",
        (int(plastic_id),))
    return {r["color"] or "" for r in await cur.fetchall()}


async def _stock_now(pid: int, canon: str) -> float:
    """Текущий склад цвета (canon) или общий склад пластика ('') — без списаний."""
    if not canon:
        p = await get_plastic(pid)
        return float(p.stock_grams) if p else 0.0
    for color, v in (await get_color_stock(pid)).items():
        if _canon_color(color) == canon:
            return float(v["stock"] or 0.0)
    return 0.0


async def cc2_recompute_stock(pid: int) -> list[dict]:
    """Склад цветов, которые ведёт принтер, — из катушек: сумма остатков неархивных катушек
    цвета (отрицательный остаток катушки — «счёт разошёлся» — считаем нулём, а не вычитаем из
    соседних). Строки цветов без катушек принтера не трогаем: там учёт ведёт мастер.

    Возвращает пересечения порога «мало» сверху вниз — [{plastic_id, plastic, color, stock, low}],
    алерт по ним шлёт вызывающий. Коммит — на вызывающей стороне."""
    owned = await cc2_owned_colors(pid)
    p = await get_plastic(pid)
    if not owned or not p:
        return []
    cur = await _conn().execute(
        "SELECT color, COALESCE(SUM(MAX(remaining_g, 0)), 0) AS g FROM spools "
        "WHERE plastic_id=? AND status!='archived' GROUP BY color", (int(pid),))
    agg = {(r["color"] or ""): float(r["g"] or 0.0) for r in await cur.fetchall()}
    rows = {_canon_color(c): (c, v) for c, v in (await get_color_stock(pid)).items()}
    palette = {_canon_color(c): c for c in p.color_list}
    crossed: list[dict] = []
    for canon in sorted(owned - {""}):
        new = round(agg.get(canon, 0.0), 3)
        hit = rows.get(canon)
        if hit:
            disp, v = hit
            old, low = float(v["stock"] or 0.0), float(v["low"] or 0.0)
            if abs(old - new) < 0.001:
                continue
            await _conn().execute(
                "UPDATE plastic_color_stock SET stock_grams=? WHERE plastic_id=? AND color=?",
                (new, int(pid), disp))
        else:
            # Цвет заведён катушкой на принтере, а строки склада у него нет: заводим под тем
            # именем, каким он записан в палитре пластика (по нему её ищут экраны админки).
            disp, old, low = palette.get(canon, canon), None, 0.0
            await _conn().execute(
                "INSERT OR IGNORE INTO plastic_color_stock(plastic_id, color, stock_grams, "
                "low_threshold) VALUES(?,?,?,0)", (int(pid), disp, new))
        if old is not None and low > 0 and old > low >= new:
            crossed.append({"plastic_id": int(pid), "plastic": p.name, "color": disp,
                            "stock": new, "low": low})
    if "" in owned:
        new, old, low = round(agg.get("", 0.0), 3), float(p.stock_grams), float(p.low_threshold)
        if abs(old - new) >= 0.001:
            await _conn().execute("UPDATE plastics SET stock_grams=? WHERE id=?", (new, int(pid)))
            if low > 0 and old > low >= new:
                crossed.append({"plastic_id": int(pid), "plastic": p.name, "color": "",
                                "stock": new, "low": low})
    return crossed


async def set_color_low(pid: int, color: str, low: float) -> None:
    """Порог «мало» цвета, чей остаток ведёт принтер: порог наш, а остаток — сумма катушек
    (пересчитываем тут же, без сети: кэш катушек в базе)."""
    canon = _canon_color(color)
    key = next((c for c in await get_color_stock(pid) if _canon_color(c) == canon), None)
    if key is None:
        await _conn().execute(
            "INSERT OR IGNORE INTO plastic_color_stock(plastic_id, color, stock_grams, low_threshold) "
            "VALUES(?,?,0,?)", (int(pid), " ".join((color or "").split()), max(0.0, float(low))))
    else:
        await _conn().execute(
            "UPDATE plastic_color_stock SET low_threshold=? WHERE plastic_id=? AND color=?",
            (max(0.0, float(low)), int(pid), key))
    await cc2_recompute_stock(pid)
    await _conn().commit()


def cc2_event_key(host: str, e: dict) -> str:
    """Отпечаток события журнала катушек CC2 Control. Номеров у событий нет, а журнал — окно
    из 150 последних, поэтому событие узнаётся по всем своим полям сразу."""
    raw = "|".join((host, str(int(e["time"])), e["spool"], str(e["slot"]), e["kind"],
                    f"{float(e['grams']):.3f}", f"{float(e['mm']):.1f}", e["job"], e["result"]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _ledger_sum(spool_id: int) -> float:
    cur = await _conn().execute(
        "SELECT COALESCE(SUM(delta_g),0) AS s FROM stock_ledger WHERE spool_id=?", (int(spool_id),))
    return float((await cur.fetchone())["s"] or 0.0)


async def cc2_apply_snapshot(host: str, snap: dict, *, now: int | None = None) -> dict:
    """Привести кэш катушек к снимку GET /api/spools службы CC2 Control (снимок уже разобран
    app/cc2spools.parse_spools). Одна транзакция, один commit.

    1. Журнал принтера → stock_ledger: каждое событие ровно раз (cc2_spool_seen), старые первыми.
       «Печать» → print, «кончилась» → writeoff, «поправка» → correction; «новая катушка» не
       переносится — стартовый остаток пишется при заведении/связывании. Первая сверка с этим
       принтером переносит НИЧЕГО: его прошлое уже сидит в остатках (метка baseline|адрес).
    2. Состав: нет на принтере — в архив; архив/номинал/порог/цена/заметка — как на принтере.
    3. Невязка остатка закрывается строкой «сверка с CC2 Control». Кроме катушек, которые сейчас
       печатают: их расход принтер допишет в журнал в конце печати, а сверка посреди неё
       посчитала бы его второй раз.
    4. Склад цветов, которые ведёт принтер, — из катушек (cc2_recompute_stock).

    Возвращает {mirrored, reconciled, archived, crossed: [...], jobs: [имена файлов печатей]}."""
    now = int(now or time.time())
    c = _conn()
    cur = await c.execute("SELECT * FROM spools WHERE cc2_spool_id IS NOT NULL")
    linked = {r["cc2_spool_id"]: _to_spool(r) for r in await cur.fetchall()}
    remote = {s["id"]: s for s in snap.get("spools") or []}
    out: dict = {"mirrored": 0, "reconciled": 0, "archived": 0, "crossed": [], "jobs": []}
    # Сверка идёт раз в минуту, а страницы базы раз в минуту уезжают копией на VPS
    # (app/replica.py): пишем только то, что изменилось, иначе каждая сверка гоняла бы весь склад.
    touched: set[int] = set()

    # 1. журнал
    base_key = f"baseline|{host}"
    cur = await c.execute("SELECT 1 FROM cc2_spool_seen WHERE key=?", (base_key,))
    baseline = await cur.fetchone() is None
    log_rows = list(snap.get("log") or [])
    for e in reversed(log_rows):                       # журнал приходит новыми первыми
        cur = await c.execute("INSERT OR IGNORE INTO cc2_spool_seen(key, at) VALUES(?,?)",
                              (cc2_event_key(host, e), int(e["time"])))
        if cur.rowcount != 1 or baseline:
            continue
        sp, reason = linked.get(e["spool"]), _CC2_REASON.get(e["kind"])
        if not sp or not reason or abs(float(e["grams"])) < 1e-9:
            continue
        job = e["job"] or None
        note = "CC2: " + " · ".join(x for x in (e["job"], e["result"]) if x) if (job or e["result"]) else "CC2"
        await c.execute(
            "INSERT INTO stock_ledger(spool_id, delta_g, reason, ref_type, note, at, job_name) "
            "VALUES(?,?,?,'cc2',?,?,?)", (sp["id"], float(e["grams"]), reason, note[:300],
                                          int(e["time"]), job))
        touched.add(sp["id"])
        out["mirrored"] += 1
        if job and reason == "print" and job not in out["jobs"]:
            out["jobs"].append(job)
    if baseline:
        await c.execute("INSERT OR IGNORE INTO cc2_spool_seen(key, at) VALUES(?, 0)", (base_key,))
    if log_rows:
        # Окно их журнала — 150 событий; отпечатки, выпавшие из него больше суток назад, больше
        # не встретятся. Метки baseline живут вечно.
        oldest = min(int(e["time"]) for e in log_rows)
        await c.execute("DELETE FROM cc2_spool_seen WHERE at < ? AND key NOT LIKE 'baseline|%'",
                        (oldest - 86400,))

    # 2–3. состав, атрибуты, невязка
    busy: set[str] = set()
    job_now = snap.get("job") or {}
    if job_now.get("active"):
        busy |= {s.get("spool") for s in job_now.get("slots") or [] if s.get("spool")}
        busy |= {s.get("spool") for s in snap.get("slots") or [] if s.get("spool")}
    for cc2_id, sp in linked.items():
        s = remote.get(cc2_id)
        if s is None:
            # Удалена на принтере (их журнал хранит id, но катушки больше нет). Только по полному
            # снимку: «библиотека не загружена» — не повод архивировать весь склад.
            if snap.get("available") and sp["status"] != "archived":
                await c.execute("UPDATE spools SET status='archived' WHERE id=?", (sp["id"],))
                out["archived"] += 1
            continue
        cost = float(s["price"]) if float(s.get("price") or 0) > 0 else sp["cost"]
        note = s.get("note") or sp["note"] or None
        status = "archived" if s.get("archived") else (
            "active" if sp["status"] == "archived" else sp["status"])
        if (float(sp["net_weight_g"]), float(sp["low_threshold"]), sp["cost"], sp["note"], sp["status"]) \
                != (float(s["net"]), float(s["low"]), cost, note, status):
            await c.execute(
                "UPDATE spools SET net_weight_g=?, low_threshold=?, cost=?, note=?, status=? WHERE id=?",
                (float(s["net"]), float(s["low"]), cost, note, status, sp["id"]))
            touched.add(sp["id"])
        if cc2_id not in busy:
            diff = float(s["remaining"]) - await _ledger_sum(sp["id"])
            if abs(diff) >= CC2_RECONCILE_G:
                await c.execute(
                    "INSERT INTO stock_ledger(spool_id, delta_g, reason, ref_type, note, at) "
                    "VALUES(?,?,'correction','cc2','сверка с CC2 Control',?)", (sp["id"], diff, now))
                touched.add(sp["id"])
                out["reconciled"] += 1
    for sid in sorted(touched):
        await _spool_recalc(sid)

    # 4. склад по цветам
    for pid in sorted({sp["plastic_id"] for sp in linked.values()}):
        out["crossed"] += await cc2_recompute_stock(pid)
    await c.commit()
    return out


async def cc2_link_spool(spool_id: int, cc2_id: str) -> str:
    """Связать нашу катушку с катушкой CC2 Control. '' — готово (или уже связаны так же),
    иначе причина отказа для тоста. Остаток подтянет ближайшая сверка."""
    if not cc2_id_ok(cc2_id):
        return "неверный id катушки принтера"
    sp = await get_spool(spool_id)
    if not sp:
        return "катушка не найдена"
    if sp["cc2_id"] == cc2_id:
        return ""
    if sp["cc2_id"]:
        return f"катушка #{sp['id']} уже связана с другой катушкой принтера"
    cur = await _conn().execute("SELECT id FROM spools WHERE cc2_spool_id=?", (cc2_id,))
    other = await cur.fetchone()
    if other:
        return f"эта катушка принтера уже связана с катушкой #{other['id']}"
    await _conn().execute("UPDATE spools SET cc2_spool_id=? WHERE id=?", (cc2_id, int(spool_id)))
    await _conn().commit()
    return ""


async def cc2_add_spool(plastic_id: int, color: str, *, cc2_id: str, net_weight_g: float,
                        remaining_g: float, low_threshold: float = 0.0, cost: float | None = None,
                        supplier: str | None = None, note: str | None = None) -> int:
    """Строка-кэш катушки, которую ведёт CC2 Control (заведена нами через него или прямо на
    принтере). Стартовое движение — остаток по данным принтера, а не номинал: начатая катушка
    приходит уже начатой."""
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO spools(plastic_id, color, net_weight_g, remaining_g, low_threshold, cost, "
        "supplier, status, note, purchased_at, created_at, cc2_spool_id) "
        "VALUES(?,?,?,?,?,?,?, 'active', ?, ?, ?, ?)",
        (int(plastic_id), _canon_color(color), max(0.0, float(net_weight_g or 0)),
         float(remaining_g or 0), max(0.0, float(low_threshold or 0)), cost, supplier, note,
         now, now, cc2_id))
    sid = int(cur.lastrowid or 0)
    await _conn().execute(
        "INSERT INTO stock_ledger(spool_id, delta_g, reason, ref_type, note, at) "
        "VALUES(?,?, 'purchase', 'cc2', 'заведена в CC2 Control', ?)", (sid, float(remaining_g or 0), now))
    await _spool_recalc(sid)
    await _conn().commit()
    return sid


def cc2_job_same(a: str, b: str) -> bool:
    """Один ли это файл печати. Их журнал режет имя до 128 байт, наш журнал — до 120 символов,
    поэтому длинное имя сравнивается префиксом (не короче 24 символов — иначе совпадали бы
    разные файлы с общим началом «ECC2_0.4_»)."""
    a = (a or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    b = (b or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 24 and long_.startswith(short)


async def cc2_job_usage(job_name: str, start: int, end: int) -> list[dict]:
    """Расход по журналу принтера (события «печать» CC2 Control) для файла job_name в окне
    времени [start, end] — со стоимостью катушки для себестоимости."""
    cur = await _conn().execute(
        "SELECT l.id, l.spool_id, l.delta_g, l.at, l.job_name, s.plastic_id, s.color, s.cost, "
        "s.net_weight_g FROM stock_ledger l JOIN spools s ON s.id = l.spool_id "
        "WHERE l.ref_type='cc2' AND l.reason='print' AND l.job_name IS NOT NULL "
        "AND l.delta_g < 0 AND l.at BETWEEN ? AND ? ORDER BY l.id", (int(start), int(end)))
    return [dict(r) for r in await cur.fetchall() if cc2_job_same(r["job_name"], job_name)]


async def cc2_recent_jobs(since: int) -> list[str]:
    """Файлы печатей, чей расход пришёл журналом принтера начиная с since (unix-время)."""
    cur = await _conn().execute(
        "SELECT DISTINCT job_name FROM stock_ledger WHERE ref_type='cc2' AND reason='print' "
        "AND job_name IS NOT NULL AND at >= ?", (int(since),))
    return [r["job_name"] for r in await cur.fetchall()]


async def cc2_has_links() -> bool:
    """Есть ли катушки, чей остаток ведёт принтер."""
    cur = await _conn().execute("SELECT 1 FROM spools WHERE cc2_spool_id IS NOT NULL LIMIT 1")
    return await cur.fetchone() is not None


async def cc2_linked_ids() -> dict[str, int]:
    """{id катушки CC2 Control: id нашей катушки} — все связанные."""
    cur = await _conn().execute("SELECT id, cc2_spool_id FROM spools WHERE cc2_spool_id IS NOT NULL")
    return {r["cc2_spool_id"]: int(r["id"]) for r in await cur.fetchall()}


async def cc2_printed_requests_for_jobs(jobs: list[str], *, within_s: int = 3 * 86400) -> list[int]:
    """Напечатанные заказы, у которых в журнале печатей есть печать одного из этих файлов
    (за последние дни): журнал принтера дописал их расход — себестоимость пора пересчитать."""
    if not jobs:
        return []
    cur = await _conn().execute(
        "SELECT DISTINCT j.request_id AS rid, j.label FROM print_journal j "
        "JOIN requests r ON r.id = j.request_id "
        "WHERE j.request_id IS NOT NULL AND r.status = 'printed' AND j.created_at >= ?",
        (int(time.time()) - int(within_s),))
    out: list[int] = []
    for r in await cur.fetchall():
        if r["rid"] not in out and any(cc2_job_same(j, r["label"] or "") for j in jobs):
            out.append(int(r["rid"]))
    return out


async def cc2_request_usage(rid: int) -> list[dict]:
    """Расход заказа по журналу принтера: события «печать» CC2 Control, чей файл и время
    совпали с печатями этого заказа в журнале печатей (print_journal.request_id). Время записи
    журнала — финиш печати, minutes — её длительность."""
    cur = await _conn().execute(
        "SELECT label, created_at, minutes FROM print_journal WHERE request_id=?", (int(rid),))
    out, seen = [], set()
    for j in await cur.fetchall():
        end = int(j["created_at"] or 0)
        start = end - int(float(j["minutes"] or 0) * 60)
        for r in await cc2_job_usage(j["label"] or "", start - CC2_JOB_SLACK_S,
                                     end + CC2_JOB_SLACK_S):
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
    return out


async def color_availability(plastic_id: int) -> dict[str, dict]:
    """Единый остаток по цвету для клиентского сигнала «в наличии/мало/нет» (canon-ключи):
    объединяет legacy plastic_color_stock и агрегат катушек. Катушки перекрывают legacy там, где
    есть (бэкфилл+dual-write держат их в синхроне). Как только по пластику заводят катушки, сигнал
    работает и БЕЗ legacy-строк — закрывает разрыв «пластик без учёта всегда в наличии» (id=11)."""
    out: dict[str, dict] = {}
    for color, v in (await get_color_stock(plastic_id)).items():
        out[_canon_color(color)] = {"stock": v["stock"], "low": v["low"]}
    # Переходный период (§4.5): legacy plastic_color_stock — источник истины, пока мастер ведёт
    # склад старой админкой; катушки лишь ЗАПОЛНЯЮТ ПРОБЕЛЫ (цвета/пластики без legacy-строки, напр.
    # id=11). Так клиент не увидит устаревший остаток катушки, если legacy пополнили старым UI
    # (dual-write зеркалит только расход, не пополнение).
    for canon, v in (await spool_stock_by_color(plastic_id)).items():
        if canon and canon not in out:            # только цвета БЕЗ legacy-строки
            out[canon] = {"stock": v["stock"], "low": v["low"]}
    return out


async def compute_request_cogs(rid: int) -> float:
    """Реальная себестоимость заказа по факту (Фаза C мастер-плана): Σ (списанные граммы × цена
    катушки за грамм) по движениям stock_ledger, привязанным к заказу (ref_type='request'). Катушки
    без закупочной цены дают 0 (cogs считается только по тому, у чего цена внесена — Решение 3).
    Пишет и возвращает requests.cogs_actual.

    Катушки CC2 Control заказ не списывает: их расход приходит журналом принтера по файлу
    печати (cc2_request_usage) — он и идёт в себестоимость."""
    cur = await _conn().execute(
        "SELECT COALESCE(SUM(-l.delta_g * s.cost / s.net_weight_g), 0) AS cogs "
        "FROM stock_ledger l JOIN spools s ON s.id = l.spool_id "
        "WHERE l.ref_type='request' AND l.ref_id=? AND l.delta_g < 0 "
        "AND s.cost IS NOT NULL AND s.net_weight_g > 0", (int(rid),))
    cogs = float((await cur.fetchone())["cogs"] or 0.0)
    for r in await cc2_request_usage(rid):
        if r["cost"] is not None and (r["net_weight_g"] or 0) > 0:
            cogs += -float(r["delta_g"]) * float(r["cost"]) / float(r["net_weight_g"])
    cogs = round(cogs, 2)
    await _conn().execute("UPDATE requests SET cogs_actual=? WHERE id=?", (cogs, int(rid)))
    await _conn().commit()
    return cogs


async def inventory_summary() -> dict:
    """Сводка склада по катушкам (Фаза C мастер-плана): активных катушек, суммарный остаток,
    сколько с закупочной ценой, сколько цветов «на исходе»/«нет». Для дашборда владельца."""
    c = _conn()
    cur = await c.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(remaining_g),0) AS g, "
        "SUM(CASE WHEN cost IS NOT NULL THEN 1 ELSE 0 END) AS priced "
        "FROM spools WHERE status='active'")
    r = await cur.fetchone()
    cur = await c.execute(
        "SELECT SUM(remaining_g) AS g, MAX(low_threshold) AS low FROM spools "
        "WHERE status!='archived' GROUP BY plastic_id, color")
    low_n, out_n = 0, 0
    for x in await cur.fetchall():
        g, low = float(x["g"] or 0), float(x["low"] or 0)
        if g <= 0:
            out_n += 1
        elif low > 0 and g <= low:
            low_n += 1
    return {"active_spools": r["n"] or 0, "remaining_g": round(r["g"] or 0, 1),
            "priced_spools": r["priced"] or 0, "low_colors": low_n, "out_colors": out_n}


async def cogs_summary() -> dict:
    """Себестоимость по факту (Фаза C): по напечатанным заказам с посчитанным cogs_actual — сумма
    выручки vs сумма себестоимости = маржа; покрытие (доля заказов, у которых cogs известен)."""
    c = _conn()
    cur = await c.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(price),0) AS rev, COALESCE(SUM(cogs_actual),0) AS cogs "
        "FROM requests WHERE status='printed' AND cogs_actual IS NOT NULL AND cogs_actual > 0")
    r = await cur.fetchone()
    printed = (await (await c.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE status='printed'")).fetchone())["n"] or 0
    n = r["n"] or 0
    rev, cogs = float(r["rev"] or 0), float(r["cogs"] or 0)
    return {"orders_with_cogs": n, "printed_orders": printed,
            "coverage_pct": round((n / printed * 100) if printed else 0, 1),
            "revenue": round(rev, 2), "cogs": round(cogs, 2), "margin": round(rev - cogs, 2),
            "margin_pct": round(((rev - cogs) / rev * 100) if rev else 0, 1)}


# ── резерв филамента под заказ (OpenTodo a89586a2) ──
# Заказ НЕ списывает пластик сразу — он РЕЗЕРВИРУЕТ его (план списания хранится в
# requests.stock_plan), а реальное списание происходит при «напечатано». На полосе остатка
# зарезервированное показывается отдельным (синим) сегментом. Старые заказы (до фичи) уже
# списали склад при оформлении → им stock_deducted=1 (бэкфилл), повторно не спишутся.
async def set_order_stock_plan(rid: int, plan: list) -> None:
    """Сохранить план списания склада под заказ (применяется при «напечатано»)."""
    await _conn().execute("UPDATE requests SET stock_plan = ? WHERE id = ?",
                          (json.dumps(plan or []), int(rid)))
    await _conn().commit()


async def get_order_stock_plan(rid: int) -> list:
    """План резерва склада заказа (requests.stock_plan, JSON); [] — не строился/битый."""
    cur = await _conn().execute("SELECT stock_plan FROM requests WHERE id = ?", (int(rid),))
    row = await cur.fetchone()
    return _jload(row["stock_plan"] if row else None, [])


async def apply_order_stock(rid: int) -> list[tuple[str, float]]:
    """Списать склад по плану заказа РОВНО ОДИН РАЗ (при «напечатано»). Идемпотентно через
    stock_deducted. Возвращает [(имя_пластика, остаток)] для пластиков, пересёкших порог.

    Отметку взводим ПЕРВЫМ ДЕЙСТВИЕМ и УСЛОВНЫМ UPDATE: так идемпотентность из «прочитал флаг
    → списал → поставил флаг» становится атомарным ЗАХВАТОМ — списывает только тот вызов, чей
    UPDATE реально изменил строку (rowcount == 1). В прежнем порядке между проверкой флага и
    его установкой лежало всё списание целиком, и два «напечатано» подряд (из веба и из бота,
    либо повторный клик) успевали пройти проверку оба — склад уходил дважды.
    rowcount == 0 — заказа нет либо склад по нему уже списан: делать нечего."""
    cur = await _conn().execute(
        "UPDATE requests SET stock_deducted = 1 WHERE id = ? AND COALESCE(stock_deducted, 0) = 0",
        (int(rid),))
    await _conn().commit()
    if cur.rowcount != 1:
        return []
    cur = await _conn().execute("SELECT stock_plan FROM requests WHERE id = ?", (int(rid),))
    row = await cur.fetchone()
    plan = _jload(row["stock_plan"] if row else None, [])
    alerts: list[tuple[str, float]] = []
    for op in plan:
        pid = op.get("plastic_id")
        if not pid:
            continue
        try:
            new, low = await deduct_color_stock_split(
                pid, op.get("colors") or [], float(op.get("grams") or 0), op.get("color_grams"),
                ref_type="request", ref_id=int(rid))
            if low:
                alerts.append((op.get("name") or "пластик", new))
        except Exception:  # noqa: BLE001
            # warning, а не debug: отметка stock_deducted уже ЗАХВАЧЕНА, значит повтора не будет
            # и заказ выпал из резерва (reserved_grams_by_plastic смотрит на неё же). Провал
            # здесь означает филамент, исчезнувший разом и из остатка, и из резерва, и нулевую
            # себестоимость заказа — то есть ровно ту беду, ради которой волна и затевалась.
            # Тихий debug тут был бы гарантией, что об этом никто не узнает.
            log.warning("apply_order_stock: списание по заказу #%s провалилось, "
                        "остаток и себестоимость разъехались", rid, exc_info=True)
    try:
        await compute_request_cogs(rid)      # реальная себестоимость по факту (Фаза C), best-effort
    except Exception:  # noqa: BLE001
        log.debug("compute_request_cogs failed rid=%s", rid, exc_info=True)
    return alerts


_RESERVING_STATUSES = ("new", "awaiting_payment", "in_progress")
# Плейсхолдеры под _RESERVING_STATUSES: статусы «активного» заказа перечисляются в нескольких
# запросах, и переписанный руками литерал разъезжается с константой (так, добавление
# awaiting_payment пришлось править в четырёх местах).
_RES_PH = ",".join("?" * len(_RESERVING_STATUSES))


async def reserved_grams_by_plastic() -> dict[int, float]:
    """Сколько филамента ЗАРЕЗЕРВИРОВАНО под активные (ещё не списанные) заказы — по plastic_id.
    Источник — stock_plan заказов в статусах new/in_progress с stock_deducted=0."""
    cur = await _conn().execute(
        f"SELECT stock_plan FROM requests WHERE status IN ({_RES_PH}) "
        "AND COALESCE(stock_deducted,0) = 0 AND stock_plan IS NOT NULL", _RESERVING_STATUSES)
    out: dict[int, float] = {}
    for r in await cur.fetchall():
        for op in _jload(r["stock_plan"], []):
            pid = op.get("plastic_id")
            if pid:
                out[pid] = out.get(pid, 0.0) + float(op.get("grams") or 0)
    return out


# ---- пользовательские оттенки цветов (админ задаёт точный hex для названия) ----
async def list_color_shades() -> list[tuple[str, str]]:
    cur = await _conn().execute("SELECT name, hex FROM color_shades ORDER BY name")
    return [(r["name"], r["hex"]) for r in await cur.fetchall()]


async def set_color_shade(name: str, hex_value: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    await _conn().execute(
        "INSERT INTO color_shades(name, hex, created_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET hex=excluded.hex",
        (name, hex_value.strip(), int(time.time())))
    await _conn().commit()


async def delete_color_shade(name: str) -> None:
    await _conn().execute("DELETE FROM color_shades WHERE name = ?", (name,))
    await _conn().commit()


def _hex_to_rgb(h: str):
    h = (h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
    except ValueError:
        return None


async def get_color_overrides() -> dict:
    """{имя_в_нижнем_регистре: (r,g,b)} — для materials.color_to_rgbs."""
    out: dict = {}
    for name, hexv in await list_color_shades():
        rgb = _hex_to_rgb(hexv)
        if rgb is not None:
            out[name.strip().lower()] = rgb
    return out
