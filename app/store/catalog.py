"""Справочники печати без склада: сопла, пресеты качества/прочности, скидки, опции.

Один предмет: то, из чего собирается расчёт, кроме материала — сопла принтера
(printer_nozzles + подбор machine_profile через app/nozzles.py), пресеты качества и
прочности, скидки за тираж и платные опции постобработки вместе с их привязкой к заказу
(request_options).

Имя модуля — catalog, а НЕ presets: имя `presets` в app/db.py уже занято модулем
app/presets.py (его QUALITY_PRESETS/STRENGTH_PRESETS читает _seed_presets() внутри init),
и фасадная строка `from .store import presets` перебила бы его — засев пресетов на свежей
БД упал бы с AttributeError.

Выделено из app/db.py волной 2 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ.
"""
from __future__ import annotations

import logging
import time

import aiosqlite

from .. import nozzles
from ..profilepaths import abs_profile_str, rel_profile
from .core import _conn
from .models import (
    DiscountTier,
    PostprocOption,
    PrinterNozzle,
    QualityPreset,
    StrengthPreset,
)

log = logging.getLogger(__name__)


# ---- сопла принтера (OpenTodo f71f4973) ----
def _to_nozzle(r: aiosqlite.Row) -> PrinterNozzle:
    # Как и у карточки принтера: в БД маркер корня, наружу — абсолютный путь.
    return PrinterNozzle(id=r["id"], printer_id=r["printer_id"], nozzle=r["nozzle"],
                         machine_profile=abs_profile_str(r["machine_profile"]),
                         note=r["note"] or "",
                         is_default=r["is_default"])


async def list_printer_nozzles(printer_id: int | None = None) -> list[PrinterNozzle]:
    """Сопла принтера (или всех сразу) — основное первым, дальше по диаметру."""
    sql = "SELECT * FROM printer_nozzles"
    args: tuple = ()
    if printer_id:
        sql += " WHERE printer_id = ?"
        args = (printer_id,)
    sql += " ORDER BY printer_id, is_default DESC, CAST(nozzle AS REAL)"
    cur = await _conn().execute(sql, args)
    return [_to_nozzle(r) for r in await cur.fetchall()]


async def add_printer_nozzle(printer_id: int, nozzle: str, machine_profile: str,
                             note: str = "") -> int:
    """Завести сопло. Первое сопло принтера автоматически становится основным."""
    n = nozzles.norm(nozzle)
    if not n:
        raise ValueError("Неверный диаметр сопла")
    machine_profile = rel_profile(machine_profile) or ""
    cur = await _conn().execute(
        "SELECT COUNT(*) AS c FROM printer_nozzles WHERE printer_id = ?", (printer_id,))
    first = (await cur.fetchone())["c"] == 0
    # Такое сопло уже заведено (мастер добавляет второй раз, поправив путь) → это правка,
    # а не новая запись: иначе INSERT OR REPLACE молча снял бы с него признак основного.
    cur = await _conn().execute(
        "SELECT id FROM printer_nozzles WHERE printer_id = ? AND nozzle = ?", (printer_id, n))
    dup = await cur.fetchone()
    if dup:
        await _conn().execute(
            "UPDATE printer_nozzles SET machine_profile = ?, note = ? WHERE id = ?",
            (machine_profile, note, dup["id"]))
        await _conn().commit()
        return dup["id"]
    cur = await _conn().execute(
        "INSERT INTO printer_nozzles(printer_id, nozzle, machine_profile, note, "
        "is_default, created_at) VALUES(?,?,?,?,?,?)",
        (printer_id, n, machine_profile, note, 1 if first else 0, int(time.time())))
    await _conn().commit()
    return cur.lastrowid


async def update_printer_nozzle(nozzle_id: int, **fields) -> None:
    allowed = {"nozzle", "machine_profile", "note"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "machine_profile" in sets:
        sets["machine_profile"] = rel_profile(sets["machine_profile"])
    if "nozzle" in sets:
        sets["nozzle"] = nozzles.norm(sets["nozzle"])
        if not sets["nozzle"]:
            raise ValueError("Неверный диаметр сопла")
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE printer_nozzles SET {assignments} WHERE id = ?",
                          (*sets.values(), nozzle_id))
    await _conn().commit()


async def set_default_printer_nozzle(nozzle_id: int) -> None:
    """Основное сопло принтера — то, что физически стоит сейчас: им считаются пресеты без
    явного диаметра и им подписан принтер в интерфейсе."""
    cur = await _conn().execute("SELECT printer_id, machine_profile FROM printer_nozzles "
                                "WHERE id = ?", (nozzle_id,))
    row = await cur.fetchone()
    if not row:
        return
    await _conn().execute("UPDATE printer_nozzles SET is_default = 0 WHERE printer_id = ?",
                          (row["printer_id"],))
    await _conn().execute("UPDATE printer_nozzles SET is_default = 1 WHERE id = ?", (nozzle_id,))
    # printers.machine_profile = профиль основного сопла: старый код (bed_size, selfcheck,
    # «Открыть в слайсере») читает его напрямую и должен видеть то, что реально стоит.
    await _conn().execute("UPDATE printers SET machine_profile = ? WHERE id = ?",
                          (row["machine_profile"], row["printer_id"]))
    await _conn().commit()


async def delete_printer_nozzle(nozzle_id: int) -> None:
    """Удалить сопло. Основное не удаляем — иначе принтер остался бы без машинного профиля."""
    cur = await _conn().execute("SELECT is_default FROM printer_nozzles WHERE id = ?", (nozzle_id,))
    row = await cur.fetchone()
    if row and row["is_default"]:
        raise ValueError("Нельзя удалить основное сопло — сначала выберите другое основным")
    await _conn().execute("DELETE FROM printer_nozzles WHERE id = ?", (nozzle_id,))
    await _conn().commit()


async def machine_profile_for(printer, nozzle: str | None) -> str:
    """Машинный профиль принтера ПОД СОПЛО (OpenTodo f71f4973).

    Сердце задачи: пресет качества 0.30/0.36 идёт процессом сопла 0.6, и слайсить его
    профилем 0.4 нельзя. Не нашли профиль под запрошенный диаметр — честно отдаём
    `printer.machine_profile` (прежнее поведение): лучше посчитать как раньше, чем упасть."""
    if printer is None:
        return ""
    want = nozzles.norm(nozzle or "")
    if not want:
        return printer.machine_profile
    cur = await _conn().execute(
        "SELECT machine_profile FROM printer_nozzles WHERE printer_id = ? AND nozzle = ?",
        (printer.id, want))
    row = await cur.fetchone()
    if row and row["machine_profile"]:
        # printer.machine_profile сюда приходит уже абсолютным (_to_printer), а значение сопла
        # читается прямо из БД — разворачиваем, иначе маркер уехал бы в slicer.slice_unit.
        return abs_profile_str(row["machine_profile"],
                               getattr(printer, "slicer", ""), getattr(printer, "slicer_path", ""))
    return printer.machine_profile


async def known_nozzles() -> list[str]:
    """Все диаметры, заведённые у принтеров + встречающиеся в пресетах качества.

    Нужны выпадающим спискам калибровки (кампания/паспорт держат сопло строкой) — чтобы
    мастер выбирал из своих сопел, а не вспоминал, как он его записал в прошлый раз."""
    out: list[str] = []
    for r in await (await _conn().execute(
            "SELECT DISTINCT nozzle FROM printer_nozzles")).fetchall():
        n = nozzles.norm(r["nozzle"])
        if n and n not in out:
            out.append(n)
    for q in await list_quality(active_only=False):
        n = nozzle_of(q)
        if n and n not in out:
            out.append(n)
    if not out:
        out = [nozzles.DEFAULT]
    return sorted(out, key=float)


# ---- пресеты качества ----
def _to_quality(r: aiosqlite.Row) -> QualityPreset:
    return QualityPreset(id=r["id"], label=r["label"], layer_height=r["layer_height"],
                         process=r["process"], sort=r["sort"], is_active=r["is_active"],
                         is_default=r["is_default"],
                         nozzle=(r["nozzle"] if "nozzle" in r.keys() else "") or "")


def nozzle_of(quality) -> str:
    """Сопло пресета качества: явное поле, иначе имя процесс-файла, иначе исторический 0.4."""
    if quality is None:
        return nozzles.DEFAULT
    val = nozzles.norm(getattr(quality, "nozzle", "") or "")
    if val:
        return val
    return nozzles.from_process_name(getattr(quality, "process", "") or "") or nozzles.DEFAULT


async def list_quality(active_only: bool = False) -> list[QualityPreset]:
    sql = "SELECT * FROM quality_presets"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort, layer_height"
    cur = await _conn().execute(sql)
    return [_to_quality(r) for r in await cur.fetchall()]


async def get_quality(qid: int) -> QualityPreset | None:
    cur = await _conn().execute("SELECT * FROM quality_presets WHERE id = ?", (qid,))
    r = await cur.fetchone()
    return _to_quality(r) if r else None


async def add_quality(label: str, layer_height: float, process: str, sort: int = 100,
                      nozzle: str = "") -> int:
    # Сопло не задали явно — берём из имени процесс-профиля: «…@Elegoo CC2 0.6 nozzle.json».
    n = nozzles.norm(nozzle) or nozzles.from_process_name(process)
    cur = await _conn().execute(
        "INSERT INTO quality_presets(label, layer_height, process, sort, is_active, nozzle) "
        "VALUES(?,?,?,?,1,?)",
        (label, layer_height, process, sort, n),
    )
    await _conn().commit()
    return cur.lastrowid


async def update_quality(qid: int, **fields) -> None:
    if "nozzle" in fields:
        fields["nozzle"] = nozzles.norm(fields["nozzle"]) or ""
    allowed = {"label", "layer_height", "process", "sort", "is_active", "nozzle"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE quality_presets SET {assignments} WHERE id = ?", (*sets.values(), qid))
    await _conn().commit()


async def delete_quality(qid: int) -> None:
    await _conn().execute("DELETE FROM quality_presets WHERE id = ?", (qid,))
    await _conn().commit()


async def set_default_quality(qid: int) -> None:
    await _conn().execute("UPDATE quality_presets SET is_default = 0")
    await _conn().execute("UPDATE quality_presets SET is_default = 1, is_active = 1 WHERE id = ?", (qid,))
    await _conn().commit()


# ---- пресеты прочности ----
def _to_strength(r: aiosqlite.Row) -> StrengthPreset:
    return StrengthPreset(id=r["id"], label=r["label"], infill=r["infill"], walls=r["walls"],
                          sort=r["sort"], is_active=r["is_active"], is_default=r["is_default"])


async def list_strength(active_only: bool = False) -> list[StrengthPreset]:
    sql = "SELECT * FROM strength_presets"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort, infill"
    cur = await _conn().execute(sql)
    return [_to_strength(r) for r in await cur.fetchall()]


async def get_strength(sid: int) -> StrengthPreset | None:
    cur = await _conn().execute("SELECT * FROM strength_presets WHERE id = ?", (sid,))
    r = await cur.fetchone()
    return _to_strength(r) if r else None


async def add_strength(label: str, infill: int, walls: int, sort: int = 100) -> int:
    cur = await _conn().execute(
        "INSERT INTO strength_presets(label, infill, walls, sort, is_active) VALUES(?,?,?,?,1)",
        (label, infill, walls, sort),
    )
    await _conn().commit()
    return cur.lastrowid


async def update_strength(sid: int, **fields) -> None:
    allowed = {"label", "infill", "walls", "sort", "is_active"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE strength_presets SET {assignments} WHERE id = ?", (*sets.values(), sid))
    await _conn().commit()


async def delete_strength(sid: int) -> None:
    await _conn().execute("DELETE FROM strength_presets WHERE id = ?", (sid,))
    await _conn().commit()


async def set_default_strength(sid: int) -> None:
    await _conn().execute("UPDATE strength_presets SET is_default = 0")
    await _conn().execute("UPDATE strength_presets SET is_default = 1, is_active = 1 WHERE id = ?", (sid,))
    await _conn().commit()


# ---- скидки за тираж ----
async def list_discounts() -> list[DiscountTier]:
    cur = await _conn().execute("SELECT * FROM discount_tiers ORDER BY min_qty")
    return [DiscountTier(id=r["id"], min_qty=r["min_qty"], discount_pct=r["discount_pct"])
            for r in await cur.fetchall()]


async def add_discount(min_qty: int, discount_pct: float) -> int:
    cur = await _conn().execute(
        "INSERT INTO discount_tiers(min_qty, discount_pct) VALUES(?, ?)", (min_qty, discount_pct))
    await _conn().commit()
    return cur.lastrowid


async def delete_discount(did: int) -> None:
    await _conn().execute("DELETE FROM discount_tiers WHERE id = ?", (did,))
    await _conn().commit()


async def best_discount(qty: int) -> float:
    cur = await _conn().execute(
        "SELECT discount_pct FROM discount_tiers WHERE min_qty <= ? ORDER BY min_qty DESC LIMIT 1", (qty,))
    r = await cur.fetchone()
    return float(r["discount_pct"]) if r else 0.0


# ---- опции постобработки (B4: шлифовка, покраска, вставки — платные услуги мастера) ----
def _to_postproc(r) -> PostprocOption:
    return PostprocOption(id=r["id"], name=r["name"], price=float(r["price"]),
                          is_active=r["is_active"], sort=r["sort"],
                          per_part=int(r["per_part"] or 0))


async def list_postproc_options(active_only: bool = False) -> list[PostprocOption]:
    sql = "SELECT * FROM postproc_options"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort, id"
    cur = await _conn().execute(sql)
    return [_to_postproc(r) for r in await cur.fetchall()]


async def add_postproc_option(name: str, price: float, sort: int = 0, per_part: int = 0) -> int:
    cur = await _conn().execute(
        "INSERT INTO postproc_options(name, price, is_active, sort, per_part, created_at) "
        "VALUES(?,?,1,?,?,?)",
        (name, price, sort, 1 if per_part else 0, int(time.time())))
    await _conn().commit()
    return cur.lastrowid


async def update_postproc_option(oid: int, **fields) -> None:
    allowed = {"name", "price", "is_active", "sort", "per_part"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE postproc_options SET {assignments} WHERE id = ?",
                          (*sets.values(), oid))
    await _conn().commit()


async def delete_postproc_option(oid: int) -> None:
    await _conn().execute("DELETE FROM postproc_options WHERE id = ?", (oid,))
    await _conn().commit()


async def set_request_options(rid: int, options: list | None) -> None:
    """Записать снимок выбранных опций постобработки на заказ (данные-сейф: имя+цена фиксируем,
    поздняя правка каталога историю не трогает). Перезаписывает набор целиком (идемпотентно)."""
    await _conn().execute("DELETE FROM request_options WHERE request_id = ?", (rid,))
    for o in (options or []):
        try:
            oid = o.get("id")
            name = str(o.get("name") or "").strip()
            price = float(o.get("price") or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        if not name:
            continue
        await _conn().execute(
            "INSERT INTO request_options(request_id, option_id, name, price) VALUES(?,?,?,?)",
            (rid, oid if isinstance(oid, int) else None, name, price))
    await _conn().commit()


async def get_request_options(rid: int) -> list[dict]:
    cur = await _conn().execute(
        "SELECT option_id, name, price FROM request_options WHERE request_id = ? ORDER BY id", (rid,))
    return [{"id": r["option_id"], "name": r["name"], "price": float(r["price"])}
            for r in await cur.fetchall()]
