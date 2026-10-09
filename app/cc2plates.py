"""Пластины стола CC2 Control: библиотеку ведёт принтер, Print Studio — записи для паспортов.

Служба CC2 Control (прошивка сообщества, `:8081`, способность `bed_plates`) держит именованные
пластины: сторона листа A/B, сетка стола, снятая для неё, и Z-offset (до ±1 мм). Смонтированная
пластина действует по-настоящему: её сетка лежит в слоте стороны, её Z применён принтеру
(`SET_GCODE_OFFSET Z=`). Наша calib_plates до того была только записью «что мастер выставил на
экране принтера» — для паспортов калибровки.

Решения владельца 08.10.2026:

* связанную пластину ведёт принтер: имя, Z и «на столе» — из CC2; правка имени и Z в нашей
  админке уходит в CC2 (`/api/plates/edit`), тип покрытия и заметка — наши;
* пластины принтера без пары заводятся у нас сами (db.cc2_plates_apply);
* монтаж — только в CC2 Control: там он может перезагрузить принтер; удалить пластину со
  снятой сеткой — тоже там;
* связь — колонкой calib_plates.cc2_plate_id (db.cc2_plate_link), по имени ничего не
  сопоставляется.

Фоном библиотека не опрашивается: снимок — когда мастер открыл экран калибровки (не чаще раза
в STALE_S) и после правки из нашей админки. Кода их службы здесь нет (GPL): только HTTP.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

from . import cc2community, db, printerdrv
from .cc2spools import clean_text, fmt_num

log = logging.getLogger(__name__)

CAP = "bed_plates"
STALE_S = 30.0          # снимок моложе — экрану годится без похода к принтеру
FETCH_TIMEOUT = 4.0
POST_TIMEOUT = 8.0      # правка Z смонтированной пластины применяется принтеру сразу
DOWN_S = 30.0           # служба не ответила — столько не стучимся (экран получит кэш)
NAME_MAX = 64           # байт UTF-8 (их PLATE_NAME_MAX)
Z_LIMIT = 1.0           # мм (их PLATE_Z_LIMIT)


class PlateError(Exception):
    """Отказ службы или сети — текст годится в тост."""


@dataclass(frozen=True)
class Source:
    """Принтер, чья служба ведёт библиотеку пластин."""
    printer_id: int | None
    host: str


@dataclass
class _Host:
    snap: dict | None = None
    fetched: float = 0.0
    down_until: float = 0.0
    error: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_hosts: dict[str, _Host] = {}


def _state(host: str) -> _Host:
    st = _hosts.get(host)
    if st is None:
        st = _hosts[host] = _Host()
    return st


def forget() -> None:
    """Сбросить кэш снимков (тесты, смена принтера)."""
    _hosts.clear()


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def parse_plates(obj) -> dict | None:
    """Ответ `GET /api/plates` → снимок без сеток; None — это не их библиотека пластин."""
    if not isinstance(obj, dict) or not isinstance(obj.get("plates"), list):
        return None
    plates = []
    for p in obj["plates"]:
        if not isinstance(p, dict) or not db.cc2_id_ok(_str(p.get("id"))) \
                or _str(p.get("side")) not in ("A", "B") or not _str(p.get("name")):
            continue
        plates.append({"id": p["id"], "name": p["name"], "side": p["side"],
                       "z": round(_num(p.get("z_offset")), 3), "measured": int(_num(p.get("measured"))),
                       "in_printer": p.get("in_printer") is True})
    return {"available": obj.get("available") is True, "error": _str(obj.get("error")),
            "current": _str(obj.get("current")), "pending": _str(obj.get("pending")),
            "result": _str(obj.get("result")), "z_applied": obj.get("z_applied") is True,
            "plates": plates}


# Их ответы — по-английски; мастеру нужны слова.
_ERRORS = {
    "Invalid plate": "Имя пластины — до 64 байт без кавычек, Z-offset — не больше ±1 мм",
    "Unknown plate": "Такой пластины на принтере уже нет — обновите экран",
    "A plate with this name already exists": "Пластина с таким именем на принтере уже есть",
    "Cannot save the plate library": "Принтер не смог сохранить библиотеку пластин",
    "Plate verification is still running": "CC2 Control ещё проверяет пластину — повторите через минуту",
    "A plate change is waiting for the printer restart": "Смена пластины ждёт перезагрузки принтера",
    "Plate library not loaded": "Библиотека пластин на принтере не загружена",
}


def human_error(msg: str) -> str:
    msg = (msg or "").strip()
    return _ERRORS.get(msg) or (f"CC2 Control: {msg}" if msg else "CC2 Control не ответил")


async def source() -> Source | None:
    """Принтер, чья служба CC2 Control ведёт пластины: сначала основной. Без сети — по кэшу
    автоопределения (его пробу драйвер гоняет фоном из каждого опроса статуса)."""
    printers = await db.list_printers(active_only=True)
    for p in sorted(printers, key=lambda x: (not getattr(x, "is_default", 0), x.id)):
        drv = printerdrv.for_printer(p)
        com = drv.community() if hasattr(drv, "community") else None
        if not com or not (com.get("caps") or {}).get(CAP):
            continue
        host = cc2community._host(drv.community_host())
        if host:
            return Source(printer_id=p.id, host=host)
    return None


def snapshot(host: str) -> tuple[dict | None, str]:
    """(последний снимок, текст последней ошибки) без похода в сеть."""
    st = _hosts.get(host)
    return (st.snap, st.error) if st else (None, "")


async def refresh(src: Source, *, force: bool = False) -> dict | None:
    """Снимок библиотеки и сверка базы. Свежий снимок (моложе STALE_S) — без сети; служба
    недавно не ответила — тоже без сети, экран получит прежний снимок (или None)."""
    st = _state(src.host)
    async with st.lock:
        now = time.time()
        if not force and st.snap is not None and now - st.fetched < STALE_S:
            return st.snap
        if not force and now < st.down_until:
            return st.snap
        try:
            obj = await asyncio.to_thread(cc2community._get, src.host, "/api/plates", FETCH_TIMEOUT)
            snap = parse_plates(obj)
            if snap is None:
                raise cc2community.CommunityError("служба не отдала библиотеку пластин")
        except cc2community.CommunityError as e:
            st.down_until, st.error = now + DOWN_S, str(e)
            log.info("CC2 %s: библиотека пластин не получена (%s)", src.host, e)
            return st.snap
        st.snap, st.fetched, st.error, st.down_until = snap, time.time(), "", 0.0
        try:
            await db.cc2_plates_apply(src.printer_id, snap)
        except Exception:  # noqa: BLE001 — сверка не должна ронять экран калибровки
            log.exception("CC2 %s: сверка пластин не удалась", src.host)
        return snap


async def edit(src: Source, plate_id: int, name: str, z: float | None) -> dict:
    """Имя и Z связанной пластины — на принтер (их /edit); затем снимок и сверка.
    {applied} — Z смонтированной пластины принтер применил сразу (он простаивал)."""
    cid = await db.calib_plate_cc2(plate_id)
    if not cid:
        raise PlateError("Эту пластину ведёт не принтер")
    nm = clean_text(name, NAME_MAX)
    if not nm:
        raise PlateError("Введите имя пластины")
    if z is None or not math.isfinite(z):
        raise PlateError("Укажите Z-offset: у пластины принтера он есть всегда")
    if abs(z) > Z_LIMIT:
        raise PlateError("Z-offset — не больше ±1 мм")
    try:
        res = await asyncio.to_thread(cc2community._post, src.host, "/api/plates/edit",
                                      f"{cid}\n{nm}\n{fmt_num(z)}", POST_TIMEOUT)
    except cc2community.CommunityError as e:
        raise PlateError(human_error(str(e))) from e
    await refresh(src, force=True)
    return {"applied": isinstance(res, dict) and res.get("applied") is True}


async def save_led(plate_id: int, *, name: str, z: float | None, kind: str, note: str) -> dict:
    """Правка пластины, которую ведёт принтер: имя и Z — на принтер, тип покрытия и заметка —
    у нас. Принтер не на связи — отказ с причиной, а не тихая запись в кэш."""
    src = await source()
    if not src:
        raise PlateError("Пластину ведёт принтер, а он сейчас не на связи — повторите позже")
    res = await edit(src, plate_id, name, z)
    row = await db.get_calib_plate(plate_id) or {}
    await db.save_calib_plate(plate_id=plate_id, printer_id=row.get("printer_id"),
                              name=row.get("name") or "", kind=kind,
                              z_offset=row.get("z_offset"), note=note)
    return res


async def mounted_elsewhere(plate_id: int) -> str:
    """Имя пластины, смонтированной на принтере этой (нашей) пластины, по последнему снимку,
    или пусто. Пока принтер держит свою пластину, «на столе» решает он, а не отметка у нас."""
    row = await db.get_calib_plate(plate_id)
    src = await source()
    if not row or not src or row.get("printer_id") != src.printer_id:
        return ""
    snap, _err = snapshot(src.host)
    cur = (snap or {}).get("current") or ""
    return next((p["name"] for p in (snap or {}).get("plates") or [] if p["id"] == cur), "")


async def link(links) -> list[str]:
    """Разовая связь «наша пластина → пластина CC2 Control» по карте мастера
    ({наш id: id в CC2}); затем снимок и сверка. Возвращает отказы (пусто — всё связано)."""
    if not isinstance(links, dict) or not links:
        return ["нужна карта связей {наш id: id пластины в CC2}"]
    errors = []
    for ps_id, cid in links.items():
        try:
            pid = int(ps_id)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0 or not db.cc2_id_ok(str(cid or "")):
            errors.append(f"{ps_id}: неверная пара")
            continue
        why = await db.cc2_plate_link(pid, str(cid))
        if why:
            errors.append(f"{ps_id}: {why}")
    src = await source()
    if src:
        await refresh(src, force=True)
    return errors


def decorate(rows: list[dict], snap: dict | None) -> list[dict]:
    """Живые поля связанных пластин для экрана: сторона, смонтирована ли, сетка в слоте."""
    live = {p["id"]: p for p in (snap or {}).get("plates") or []}
    current = (snap or {}).get("current") or ""
    for r in rows:
        cid = r.get("cc2_plate_id") or ""
        if not cid:
            continue
        p = live.get(cid)
        r["cc2"] = {} if p is None else {
            "side": p["side"], "in_printer": p["in_printer"], "mounted": cid == current,
            "z_applied": bool(snap and snap.get("z_applied")) if cid == current else None}
    return rows


def view(src: Source | None, snap: dict | None, error: str) -> dict | None:
    """Блок «пластины ведёт CC2 Control» для экрана калибровки; None — принтер без библиотеки."""
    if src is None:
        return None
    ok = bool(snap and snap.get("available"))
    return {"printer_id": src.printer_id, "ok": ok,
            "error": "" if ok and not error else human_error((snap or {}).get("error") or error),
            "stale": bool(error) and snap is not None,
            "pending": bool(snap and snap.get("pending"))}
