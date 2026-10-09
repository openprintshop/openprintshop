"""Катушки CC2 Control: склад филамента ведёт принтер, Print Studio — кэш, экраны и алерты.

Служба CC2 Control (прошивка сообщества, `:8081`, способность `spool_tracking`) знает, какая
катушка стоит в каком лотке Canvas, и списывает с неё то, что принтер РЕАЛЬНО выдавил
(`print_stats.filament_used` по лотку, с продувками, отменёнными и упавшими печатями). Оценка
слайсера у владельца на трёхцветной печати: 8,68 м против 11,32 м настоящих. Поэтому остаток
катушки — их, а наш учёт (app/store/plastics.py, секция CC2) катушки принтера не списывает.

Этот модуль — сеть и ритм:

* разбор `GET /api/spools` (parse_spools) и запись в их службу (save/adjust/assign/...);
* источник — принтер с этой способностью (`source()`), по умолчанию — основной;
* фоном склад НЕ опрашивается (решение владельца 08.10.2026: принтер слабый, 111 МБ). Снимок
  берётся только (1) когда мастер открыл экран катушек — сначала короткий `GET /api/printer`, а
  тяжёлый `/api/spools` (их служба выделяет под него ~270 КБ) лишь если на принтере что-то
  поменялось или идёт печать; (2) после изменения из нашей админки — чтобы показать итог;
  (3) один раз после конца печати (after_print), когда принтер уже свободен: иначе склад цветов
  для клиентов, граммы журнала печатей и алерты ждали бы, пока кто-нибудь откроет экран;
* после каждого снимка — сверка базы (db.cc2_apply_snapshot) и то, что из неё следует: алерты
  «мало филамента», граммы журнала печатей, пересчёт себестоимости напечатанных заказов.

Кода их службы здесь нет (GPL): только HTTP, как к любой машине в сети. Сеть — через
cc2community._http (голый http.client: прокси владельца не должен увезти запрос к принтеру).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field

from . import cc2community, db, materials, notify, printerdrv

log = logging.getLogger(__name__)

CAP = "spool_tracking"
STALE_S = 10.0          # снимок моложе — экрану годится без вопросов
REV_CHECK_S = 5.0       # проверка ревизии по запросу экрана (GET /api/printer) — не чаще
LIVE_S = 60.0           # идёт печать и снимок старше — перечитать: живой остаток ревизию не двигает
AFTER_PRINT_S = 30.0    # снимок после конца печати: их служба пишет журнал через 8 с после финиша
FETCH_TIMEOUT = 4.0
POST_TIMEOUT = 5.0
DOWN_S = 30.0           # служба не ответила — столько не стучимся (экран получит кэш)
OFF_ALERT_S = 6 * 3600  # «учёт на принтере выключен» — не чаще
JOURNAL_FILL_S = 3 * 3600   # файлы печатей за столько назад сверяются с журналом печатей
# Выключатель снимка после печати: тесты гасят его автофикстурой (в сеть ходить нельзя).
AUTO_SYNC = True

SLOTS = 5               # лотки Canvas 0..3 и внешний держатель 4
EXTERNAL = 4
TEXT_MAX = 96           # байт UTF-8 на имя, бренд, заметку (их SPOOL_TEXT_MAX)
MATERIAL_MAX = 32


class SpoolError(Exception):
    """Отказ службы или сети — текст годится в тост."""


@dataclass(frozen=True)
class Source:
    """Принтер, чья служба ведёт склад катушек."""
    printer_id: int | None
    host: str
    name: str = ""


@dataclass
class _Host:
    snap: dict | None = None
    fetched: float = 0.0        # когда получен снимок (time.time)
    synced_rev: int = -1        # ревизия снимка, с которым сверена база
    messages: int = -1          # счётчик сообщений их службы при последней сверке ревизии
    rev_checked: float = 0.0    # последняя своя проверка ревизии
    down_until: float = 0.0
    error: str = ""
    gen: int = 0                # номер снимка: «пока ждал замка, склад уже перечитали»
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


# ─── разбор ответа ───────────────────────────────────────────────────────────────────────────

def _num(v, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def parse_spools(obj) -> dict | None:
    """Ответ `GET /api/spools` → нормализованный снимок; None — это не их склад катушек.

    Битые элементы выбрасываются поштучно: одна странная катушка не должна ронять сверку
    остальных сорока."""
    if not isinstance(obj, dict) or not isinstance(obj.get("spools"), list) \
            or not isinstance(obj.get("revision"), int):
        return None
    spools = []
    for s in obj["spools"]:
        if not isinstance(s, dict) or not db.cc2_id_ok(_str(s.get("id"))):
            continue
        spools.append({
            "id": s["id"], "name": _str(s.get("name")), "brand": _str(s.get("brand")),
            "material": _str(s.get("material")), "color": _str(s.get("color")).upper(),
            "diameter": _num(s.get("diameter"), 1.75), "density": _num(s.get("density"), 1.24),
            "net": _num(s.get("net"), 1000.0), "remaining": _num(s.get("remaining")),
            "tare": _num(s.get("tare")), "low": _num(s.get("low")), "price": _num(s.get("price")),
            "note": _str(s.get("note")), "created": int(_num(s.get("created"))),
            "used": int(_num(s.get("used"))), "archived": s.get("archived") is True})
    slots = []
    for s in obj.get("slots") if isinstance(obj.get("slots"), list) else []:
        if not isinstance(s, dict) or not isinstance(s.get("slot"), int):
            continue
        pr = s.get("printer") if isinstance(s.get("printer"), dict) else None
        slots.append({
            "slot": s["slot"], "spool": _str(s.get("spool")), "last": _str(s.get("last")),
            "question": _str(s.get("question")), "question_mm": _num(s.get("question_mm")),
            "runout": s.get("runout") is True,
            "printer": None if pr is None else {
                "status": int(_num(pr.get("status"))), "type": _str(pr.get("type")),
                "name": _str(pr.get("name")), "color": _str(pr.get("color")).upper(),
                "brand": _str(pr.get("brand"))}})
    job = obj.get("job") if isinstance(obj.get("job"), dict) else {}
    job_slots = [{"mm": _num(j.get("mm")), "grams": _num(j.get("grams")), "spool": _str(j.get("spool"))}
                 for j in (job.get("slots") if isinstance(job.get("slots"), list) else [])
                 if isinstance(j, dict)]
    log_rows = []
    for e in obj.get("log") if isinstance(obj.get("log"), list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("time"), int) \
                or _str(e.get("kind")) not in ("print", "runout", "correction", "new"):
            continue
        log_rows.append({
            "time": e["time"], "spool": _str(e.get("spool")), "slot": int(_num(e.get("slot"), -1)),
            "kind": e["kind"], "grams": _num(e.get("grams")), "mm": _num(e.get("mm")),
            "job": _str(e.get("job")), "result": _str(e.get("result"))})
    return {"available": obj.get("available") is True, "error": _str(obj.get("error")),
            "enabled": obj.get("enabled") is True, "revision": obj["revision"],
            "canvas": obj.get("canvas") is True,
            "active_tray": int(_num(obj.get("active_tray"), -1)),
            "slots": slots,
            "job": {"active": job.get("active") is True, "file": _str(job.get("file")),
                    "started": int(_num(job.get("started"))), "slots": job_slots},
            "spools": spools, "log": log_rows}


# ─── запись в их службу ──────────────────────────────────────────────────────────────────────

def clean_text(text: str, max_bytes: int = TEXT_MAX) -> str:
    """Текст, который их служба примет: без кавычек, обратной косой черты, управляющих символов
    и разделителей строк, без пробелов по краям, не длиннее max_bytes UTF-8 (режем по символу)."""
    out = []
    for ch in str(text or ""):
        if ch == '"':
            ch = "'"
        elif ch == "\\":
            ch = "/"
        elif ch in "\r\n\t":
            ch = " "
        if unicodedata.category(ch) == "Cc" or ch in "  ":
            continue
        out.append(ch)
    s = " ".join("".join(out).split())
    raw = s.encode("utf-8")[:max_bytes]
    return raw.decode("utf-8", "ignore").strip()


def fmt_num(v: float) -> str:
    """Число для их формы: только цифры, точка и минус (экспоненту они не примут)."""
    s = f"{float(v):.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def form(fields: dict) -> str:
    """Тело изменения: строки key=value (их spool_form_parse). None — поле не передаём."""
    lines = []
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "1" if v else "0"
        elif isinstance(v, (int, float)):
            v = fmt_num(v)
        v = str(v)
        if "\n" in v or "\r" in v or not re.match(r"^[a-z_]{1,15}$", k):
            raise SpoolError(f"поле «{k}» в таком виде служба не примет")
        lines.append(f"{k}={v}")
    return "\n".join(lines)


# Их ответы — по-английски; мастеру нужны слова, а не «Invalid spool number».
_ERRORS = {
    "Unknown spool": "Такой катушки на принтере нет — обновите список",
    "Enter the empty spool weight first": "Сначала укажите вес пустой катушки (тару)",
    "Correct the remaining weight with a weigh-in": "Остаток меняется только взвешиванием или поправкой",
    "A spool needs a material and a colour": "Катушке нужны материал и цвет",
    "Invalid colour": "Цвет — в виде #RRGGBB",
    "Invalid spool text": "Текст без кавычек и обратной косой черты, до 96 байт",
    "Invalid material": "Материал — до 32 байт, без кавычек",
    "Invalid spool number": "Число вне допустимого диапазона",
    "Invalid weight": "Вес — число не меньше тары",
    "Invalid slot": "Неверный лоток",
    "Invalid assignment": "Неверная привязка катушки к лотку",
    "The spool library is full": "На принтере уже 128 катушек — уберите лишние в архив и удалите",
    "An archived spool cannot be loaded": "Архивную катушку нельзя поставить в лоток",
    "Spool library not loaded": "Склад катушек на принтере не загружен",
    "Cannot save the spool library": "Принтер не смог сохранить склад катушек",
    "Use on or off": "Включить или выключить — других вариантов нет",
}


def human_error(msg: str) -> str:
    msg = (msg or "").strip()
    return _ERRORS.get(msg) or (f"CC2 Control: {msg}" if msg else "CC2 Control не ответил")


def _post_blocking(host: str, path: str, body: str) -> dict:
    try:
        res = cc2community._post(host, path, body, timeout=POST_TIMEOUT)
    except cc2community.CommunityError as e:
        raise SpoolError(human_error(str(e))) from e
    return res if isinstance(res, dict) else {}


def _fetch_blocking(host: str) -> dict:
    snap = parse_spools(cc2community._get(host, "/api/spools", timeout=FETCH_TIMEOUT))
    if snap is None:
        raise cc2community.CommunityError("служба не отдала склад катушек")
    return snap


def _summary_blocking(host: str) -> dict | None:
    """Короткий опрос `/api/printer` ради одной ревизии склада (их `spools` в ответе)."""
    try:
        obj = cc2community._get(host, "/api/printer", timeout=2.0)
    except cc2community.CommunityError:
        return None
    return cc2community.remember_spools_summary(host, obj)


# ─── источник ────────────────────────────────────────────────────────────────────────────────

async def source() -> Source | None:
    """Принтер, чья служба CC2 Control ведёт катушки: сначала основной. Без сети — по кэшу
    автоопределения (его пробу драйвер гоняет фоном из каждого опроса статуса)."""
    printers = await db.list_printers(active_only=True)
    for p in sorted(printers, key=lambda x: (not getattr(x, "is_default", 0), x.id)):
        drv = printerdrv.for_printer(p)
        com = drv.community() if hasattr(drv, "community") else None
        if not com or not (com.get("caps") or {}).get(CAP):
            continue
        host = cc2community._host(drv.community_host())
        if host:
            return Source(printer_id=p.id, host=host, name=getattr(p, "name", "") or "")
    return None


# ─── снимок и сверка ─────────────────────────────────────────────────────────────────────────

def snapshot(host: str) -> tuple[dict | None, float, str]:
    """(снимок, возраст в секундах, текст последней ошибки) без похода в сеть."""
    st = _hosts.get(host)
    if not st or not st.snap:
        return None, float("inf"), st.error if st else ""
    return st.snap, max(0.0, time.time() - st.fetched), st.error


async def refresh(src: Source, *, force: bool = False) -> dict | None:
    """Перечитать `/api/spools` и сверить базу. Нет связи — прежний снимок (или None).

    Под замком — только чтение и сверка: два экрана, пришедшие разом, не читают склад дважды
    (второй, дождавшись замка, получает только что снятый снимок). Алерты и себестоимость —
    уже без замка: Telegram может думать секундами."""
    st = _state(src.host)
    gen = st.gen
    async with st.lock:
        now = time.time()
        if not force and (now < st.down_until or (st.snap is not None and st.gen != gen)):
            return st.snap
        try:
            snap = await asyncio.to_thread(_fetch_blocking, src.host)
        except cc2community.CommunityError as e:
            st.down_until, st.error = now + DOWN_S, str(e)
            log.debug("cc2spools: %s не отдал склад катушек: %s", src.host, e)
            return st.snap
        st.snap, st.fetched, st.error, st.down_until = snap, time.time(), "", 0.0
        st.gen += 1
        try:
            res = await db.cc2_apply_snapshot(src.host, snap)
        except Exception:  # noqa: BLE001 — сверка не должна ронять экран; повторится сама
            log.warning("cc2spools: сверка склада с %s не удалась", src.host, exc_info=True)
            return snap
        st.synced_rev = snap["revision"]
    await _after_sync(src, snap, res)
    return snap


async def ensure_fresh(src: Source) -> dict | None:
    """Снимок для экрана мастера. Фоном склад не опрашивается, поэтому решаем здесь.

    Свежий (моложе STALE_S) — как есть. Иначе один короткий `GET /api/printer` (не чаще
    REV_CHECK_S), а тяжёлый `/api/spools` — только если сменилась ревизия склада, служба
    перезапускалась (её счётчик сообщений сбросился) или идёт печать и снимок старше LIVE_S:
    живой остаток печатающей катушки ревизию не двигает. Принтер не ответил — кэш."""
    st = _state(src.host)
    now = time.time()
    if st.snap is not None and now - st.fetched < STALE_S:
        return st.snap
    if now < st.down_until:
        return st.snap
    if st.snap is None:
        return await refresh(src)
    summ = _known_summary(src.host, newer_than=st.fetched, max_age=REV_CHECK_S)
    if summ is None and now - st.rev_checked >= REV_CHECK_S:
        st.rev_checked = now
        summ = await asyncio.to_thread(_summary_blocking, src.host)
    if summ is None:
        return st.snap
    restarted = 0 <= summ["messages"] < st.messages
    st.messages = summ["messages"]
    if summ["revision"] != st.synced_rev or restarted or \
            (summ["printing"] and now - st.fetched >= LIVE_S):
        return await refresh(src)
    return st.snap


def _known_summary(host: str, *, newer_than: float, max_age: float) -> dict | None:
    """Сводка склада из последнего ответа /api/printer (его во время печати и так берёт опрос
    задания) — если она моложе max_age и пришла ПОСЛЕ нашего снимка: снятая до снимка, она о
    новых переменах ничего не говорит."""
    hit = cc2community.spools_summary(host)
    if not hit:
        return None
    ts, summ = hit
    if ts <= newer_than or time.time() - ts > max_age:
        return None
    return summ


# Задачи «снимок после печати»: держим ссылки, иначе asyncio вправе собрать задачу на полпути.
_tasks: set[asyncio.Task] = set()


def schedule_after_print(printer_id: int | None = None) -> None:
    """Запланировать after_print фоном (зовёт printwatch, записав печать в журнал)."""
    task = asyncio.create_task(after_print(printer_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def after_print(printer_id: int | None = None) -> None:
    """Печать кончилась — один снимок склада, когда принтер уже свободен: перенос расхода,
    склад цветов для клиентов, алерты, граммы журнала печатей (запись печати к этому моменту
    уже есть), себестоимость. Их служба пишет журнал через 8 с после финиша — ждём AFTER_PRINT_S.
    Без связанных катушек к принтеру не идём: сверять нечего."""
    if not AUTO_SYNC:
        return
    await asyncio.sleep(AFTER_PRINT_S)
    try:
        src = await source()
        if src is None or (printer_id and src.printer_id and src.printer_id != printer_id):
            return
        if await db.cc2_has_links():
            await refresh(src)
    except Exception:  # noqa: BLE001 — снимок после печати best-effort, экран обновит сам
        log.warning("cc2spools: снимок склада после печати не удался", exc_info=True)


async def _after_sync(src: Source, snap: dict, res: dict) -> None:
    """Последствия сверки: алерты и себестоимость. Всё best-effort."""
    for c in res.get("crossed") or []:
        what = f"«{c['plastic']}» {c['color']}".strip()
        try:
            await notify.alert(
                f"Мало филамента: {what} — осталось {c['stock']:.0f} г (порог {c['low']:.0f} г, "
                f"по весу катушек на принтере)",
                key=f"lowstock_{c['plastic_id']}_{c['color']}", throttle_sec=3600)
        except Exception:  # noqa: BLE001
            log.debug("cc2spools: алерт о низком остатке не ушёл", exc_info=True)
    try:
        # Граммы печатей в журнал — из реального расхода. Берём не только новые файлы: запись
        # журнала печатей могла появиться ПОСЛЕ сверки (printwatch и этот цикл не синхронны).
        jobs = list(dict.fromkeys(res.get("jobs", []) + await db.cc2_recent_jobs(
            int(time.time()) - JOURNAL_FILL_S)))
        if jobs:
            await db.cc2_fill_journal(jobs)
    except Exception:  # noqa: BLE001
        log.debug("cc2spools: граммы журнала печатей по журналу принтера не заполнены", exc_info=True)
    if res.get("jobs"):
        try:
            for rid in await db.cc2_printed_requests_for_jobs(res["jobs"]):
                await db.compute_request_cogs(rid)
        except Exception:  # noqa: BLE001
            log.debug("cc2spools: себестоимость по журналу принтера не пересчитана", exc_info=True)
    try:
        if await db.cc2_has_links():
            if not snap.get("available"):
                await notify.alert(
                    "CC2 Control не загрузил склад катушек (spools.json): остатки катушек не "
                    "обновляются, расход печати не считается", key="cc2_spools_unavailable",
                    throttle_sec=OFF_ALERT_S)
            elif not snap.get("enabled"):
                await notify.alert(
                    "Учёт катушек в CC2 Control выключен: расход печати сейчас никто не считает. "
                    "Включите его на странице «Катушки» (в приложении или на самом принтере)",
                    key="cc2_spools_off", throttle_sec=OFF_ALERT_S)
    except Exception:  # noqa: BLE001
        log.debug("cc2spools: алерт о состоянии учёта не ушёл", exc_info=True)


# ─── действия ────────────────────────────────────────────────────────────────────────────────

async def post(src: Source, path: str, body: str) -> dict:
    """Изменение в их службе и сразу — свежий снимок и сверка, чтобы экран показал итог."""
    res = await asyncio.to_thread(_post_blocking, src.host, path, body)
    await refresh(src, force=True)
    return res


def color_hex(name: str, overrides: dict) -> str:
    """#RRGGBB для названия цвета: оттенок админа, затем встроенная таблица. '' — не узнали."""
    rgb = materials.color_to_rgb(name, overrides)
    if rgb is None:
        return ""
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in rgb)


def spool_name(plastic_name: str, color: str) -> str:
    """Имя катушки на принтере — как при переносе склада 08.10: «<пластик> <цвет>»."""
    return clean_text(f"{plastic_name} {color}".strip())


def brand_of(plastic_name: str) -> str:
    """Бренд — первое слово названия пластика («Elegoo PLA Matte» → Elegoo), как при переносе."""
    words = (plastic_name or "").split()
    return clean_text(words[0] if words else "")


def location(snap: dict | None, cc2_id: str) -> int | None:
    """Где стоит катушка: лоток 0..3, внешний держатель 4, None — на полке."""
    for s in (snap or {}).get("slots") or []:
        if cc2_id and s.get("spool") == cc2_id:
            return int(s["slot"])
    return None


def live(snap: dict | None) -> dict[str, dict]:
    """Катушки снимка по id — для живого остатка поверх кэша базы."""
    return {s["id"]: s for s in (snap or {}).get("spools") or []}


# ─── ручки админки (/api/admin/spools, /api/admin/spool) ────────────────────────────────────

SLOT_NAMES = {0: "лоток 1", 1: "лоток 2", 2: "лоток 3", 3: "лоток 4", EXTERNAL: "внешний держатель"}


async def view(src: Source) -> dict:
    """Блок `cc2` ответа /api/admin/spools: связь с принтером, вопросы лотков, лотки и катушки
    принтера без пары в Print Studio. Снимок — ensure_fresh (ревизия, а не слепой опрос)."""
    await ensure_fresh(src)
    snap, age, err = snapshot(src.host)
    linked = await db.cc2_linked_ids()
    snap = snap or {}
    unlinked = [{"cc2_id": s["id"], "name": s["name"], "brand": s["brand"], "material": s["material"],
                 "hex": s["color"], "remaining_g": round(s["remaining"], 1), "net_weight_g": s["net"],
                 "slot": location(snap, s["id"])}
                for s in snap.get("spools") or [] if s["id"] not in linked and not s["archived"]]
    slots = []
    for s in snap.get("slots") or []:
        slots.append({"slot": s["slot"], "name": SLOT_NAMES.get(s["slot"], f"лоток {s['slot'] + 1}"),
                      "cc2_id": s["spool"], "spool_id": linked.get(s["spool"]),
                      "question": s["question"], "question_mm": round(s["question_mm"], 1),
                      "printer": s["printer"]})
    job = snap.get("job") or {}
    return {"printer": src.name, "printer_id": src.printer_id,
            "online": bool(snap) and not err, "age_s": None if age == float("inf") else round(age),
            "error": err, "available": bool(snap.get("available")), "enabled": bool(snap.get("enabled")),
            "canvas": bool(snap.get("canvas")), "active_tray": snap.get("active_tray", -1),
            "job": {"active": bool(job.get("active")), "file": job.get("file") or ""},
            "questions": [x for x in slots if x["question"]], "slots": slots, "unlinked": unlinked}


def decorate(row: dict, snap: dict | None) -> dict:
    """Строка катушки /api/admin/spools + живые поля принтера: где стоит, остаток прямо сейчас
    (во время печати он меньше кэша — журнал принтер допишет в конце), тара, цвет #RRGGBB."""
    cid = row.get("cc2_id")
    if not cid:
        return row
    row["slot"] = location(snap, cid)
    s = live(snap).get(cid)
    if s:
        row.update({"live_g": round(s["remaining"], 1), "tare": s["tare"], "hex": s["color"],
                    "cc2_name": s["name"], "brand": s["brand"], "material": s["material"]})
        if row.get("net_weight_g"):
            row["pct"] = round(100 * max(0.0, s["remaining"]) / row["net_weight_g"])
    return row


def _f(v, default=None):
    """Число из админ-формы ('12,5' → 12.5); пусто/мусор → default."""
    if v in (None, ""):
        return default
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _bad(msg: str) -> dict:
    return {"ok": False, "error": msg}


async def handle(src: Source | None, body: dict) -> dict | None:
    """Действие /api/admin/spool, которое решает CC2 Control. None — действие не его (нет
    принтера с учётом катушек или катушка не принтерная): пусть решает учёт Print Studio.

    Изменения уходят в их службу, кэш базы догоняет сразу же (post → refresh → сверка). Нет
    связи — отказ с причиной, а не тихая запись в кэш: иначе склад разъехался бы надолго."""
    action = str(body.get("action") or "")
    if action.startswith("cc2_"):
        if src is None:
            return _bad("Принтер с учётом катушек CC2 Control не найден")
        return await _handle_cc2(src, action, body)
    if src is None:
        # Принтер пропал из автоопределения (перезагрузка, сеть). Катушку принтера править
        # своим учётом нельзя: ближайшая сверка вернула бы его остаток и затёрла правку.
        sp = await db.get_spool(int(_f(body.get("spool_id"), 0) or 0)) if action != "add" else None
        if sp and sp["cc2_id"]:
            return _bad("Эту катушку ведёт принтер, а он сейчас не на связи — повторите позже")
        return None
    try:
        if action == "add":
            return await _add(src, body)
        sid = int(_f(body.get("spool_id"), 0) or 0)
        sp = await db.get_spool(sid) if sid else None
        if not sp or not sp["cc2_id"]:
            return None
        return await _spool_action(src, sp, action, body)
    except SpoolError as e:
        return _bad(str(e))


async def _add(src: Source, body: dict) -> dict:
    """Приход катушки: заводим её на принтере (/api/spools/save) и кэш-строку с его id."""
    p = await db.get_plastic(int(_f(body.get("plastic_id"), 0) or 0))
    if not p:
        return _bad("Не выбран пластик")
    color = " ".join(str(body.get("color") or "").split())
    net = _f(body.get("net_weight"), 0.0) or 0.0
    if not 1 <= net <= 20000:
        return _bad("Вес нетто — от 1 до 20000 г")
    remaining = _f(body.get("remaining"), net)
    if remaining is None or not 0 <= remaining <= 20000:
        return _bad("Остаток — от 0 до 20000 г")
    tare = _f(body.get("tare"), float(p.spool_weight_g or 0)) or 0.0
    low = _f(body.get("low"), 0.0) or 0.0
    cost = _f(body.get("cost"))
    if not 0 <= tare <= 5000 or not 0 <= low <= 20000 or (cost is not None and not 0 <= cost <= 1e7):
        return _bad("Тара до 5000 г, порог до 20000 г, цена не отрицательная")
    hexv = str(body.get("hex") or "").strip().upper()
    if not re.match(r"^#[0-9A-F]{6}$", hexv):
        hexv = color_hex(color or p.name, await db.get_color_overrides())
    if not hexv:
        return _bad(f"Не знаю, как выглядит цвет «{color}» — выберите оттенок")
    if not 0.5 <= float(p.density or 0) <= 3:
        return _bad("Плотность пластика вне 0,5–3 г/см³ — поправьте карточку пластика")
    material = clean_text(p.material_type or "PLA", MATERIAL_MAX) or "PLA"
    note = clean_text(body.get("note") or "")
    fields = {"name": spool_name(p.name, color), "brand": brand_of(p.name), "material": material,
              "color": hexv, "diameter": 1.75, "density": float(p.density), "net": net,
              "remaining": remaining, "tare": tare, "low": low, "price": cost or 0.0, "note": note}
    res = await asyncio.to_thread(_post_blocking, src.host, "/api/spools/save", form(fields))
    cc2_id = str(res.get("id") or "")
    if not db.cc2_id_ok(cc2_id):
        return _bad("Принтер не вернул id новой катушки")
    sid = await db.cc2_add_spool(p.id, color, cc2_id=cc2_id, net_weight_g=net, remaining_g=remaining,
                                 low_threshold=low, cost=cost,
                                 supplier=(str(body.get("supplier") or "").strip() or None),
                                 note=note or None)
    await refresh(src, force=True)
    return {"ok": True, "id": sid, "cc2_id": cc2_id}


async def _spool_action(src: Source, sp: dict, action: str, body: dict) -> dict | None:
    cid = sp["cc2_id"]
    if action == "correction":                      # ввод фактического остатка
        actual = _f(body.get("actual"))
        if actual is None or not 0 <= actual <= 20000:
            return _bad("Фактический остаток — число от 0 до 20000 г")
        await post(src, "/api/spools/adjust", form({"id": cid, "remaining": actual}))
    elif action == "weigh":                         # катушка на весах целиком, минус тара
        gross = _f(body.get("gross"))
        if gross is None or not 0 <= gross <= 25000:
            return _bad("Вес на весах — число до 25000 г")
        tare = _f(body.get("tare"))
        if tare is not None:
            if not 0 < tare <= 5000 or tare > gross:
                return _bad("Тара — от 1 до 5000 г и не больше веса на весах")
            await asyncio.to_thread(_post_blocking, src.host, "/api/spools/save",
                                    form({"id": cid, "tare": tare}))
        await post(src, "/api/spools/adjust", form({"id": cid, "gross": gross}))
    elif action == "move":                          # ручное движение ±г от живого остатка
        delta = _f(body.get("delta"))
        if delta is None:
            return _bad("Дельта — число (+ приход, − расход)")
        snap = await ensure_fresh(src)
        s = live(snap).get(cid)
        if not s:
            return _bad("Принтер не ответил — остаток катушки неизвестен")
        await post(src, "/api/spools/adjust",
                   form({"id": cid, "remaining": min(20000.0, max(0.0, s["remaining"] + delta))}))
    elif action == "status":
        status = str(body.get("status") or "").strip()
        if status == "archived":
            await post(src, "/api/spools/save", form({"id": cid, "archived": True}))
        elif status == "active":
            await post(src, "/api/spools/save", form({"id": cid, "archived": False}))
        elif status == "empty":
            await post(src, "/api/spools/adjust", form({"id": cid, "remaining": 0}))
        else:
            return _bad("Статус — active, empty или archived")
        return {"ok": True}
    else:
        return None
    fresh = await db.get_spool(sp["id"])
    return {"ok": True, "remaining_g": round((fresh or sp)["remaining_g"], 1)}


async def _handle_cc2(src: Source, action: str, body: dict) -> dict:
    try:
        if action == "cc2_refresh":
            await refresh(src, force=True)
            return {"ok": True}
        if action == "cc2_enable":
            on = bool(body.get("on"))
            await post(src, "/api/spools/enable", "on" if on else "off")
            return {"ok": True, "enabled": on}
        if action == "cc2_assign":                  # ответ на «какая это катушка?» / смена лотка
            slot = int(_f(body.get("slot"), -1))
            cid = str(body.get("cc2_id") or "")
            if slot not in SLOT_NAMES or (cid and not db.cc2_id_ok(cid)):
                return _bad("Неверный лоток или катушка")
            await post(src, "/api/spools/assign", form({"slot": slot, "spool": cid}))
            return {"ok": True}
        if action == "cc2_dismiss":                 # «в лотке не катушка со склада»
            slot = int(_f(body.get("slot"), -1))
            if slot not in (0, 1, 2, 3):
                return _bad("Неверный лоток")
            await post(src, "/api/spools/dismiss", form({"slot": slot}))
            return {"ok": True}
        if action == "cc2_edit":                    # тара, порог, цена, заметка катушки принтера
            sp = await db.get_spool(int(_f(body.get("spool_id"), 0) or 0))
            if not sp or not sp["cc2_id"]:
                return _bad("Катушка не связана с принтером")
            fields: dict = {"id": sp["cc2_id"]}
            for key, lo, hi in (("tare", 0, 5000), ("low", 0, 20000), ("cost", 0, 1e7)):
                v = _f(body.get(key))
                if v is None:
                    continue
                if not lo <= v <= hi:
                    return _bad(f"«{key}» вне {lo}–{hi:g}")
                fields["price" if key == "cost" else key] = v
            if "note" in body:
                fields["note"] = clean_text(body.get("note") or "")
            await post(src, "/api/spools/save", form(fields))
            return {"ok": True}
        if action == "cc2_link":                    # наша катушка ↔ катушка принтера
            pairs = body.get("links") if isinstance(body.get("links"), dict) \
                else {body.get("spool_id"): body.get("cc2_id")}
            snap = await ensure_fresh(src)
            known = live(snap)
            errors, done = [], 0
            for sid_raw, cid_raw in pairs.items():
                sid_i, cid_s = int(_f(sid_raw, 0) or 0), str(cid_raw or "")
                if snap is not None and cid_s not in known:
                    errors.append(f"#{sid_i}: на принтере нет катушки {cid_s}")
                    continue
                why = await db.cc2_link_spool(sid_i, cid_s)
                if why:
                    errors.append(f"#{sid_i}: {why}")
                else:
                    done += 1
            await refresh(src, force=True)
            return {"ok": not errors, "linked": done, "errors": errors,
                    **({"error": "; ".join(errors[:3])} if errors else {})}
        if action == "cc2_adopt":                   # катушку завели на принтере — берём в учёт
            cid = str(body.get("cc2_id") or "")
            p = await db.get_plastic(int(_f(body.get("plastic_id"), 0) or 0))
            if not p:
                return _bad("Выберите пластик")
            snap = await ensure_fresh(src)
            s = live(snap).get(cid)
            if not s:
                return _bad("На принтере нет такой катушки — обновите список")
            if cid in await db.cc2_linked_ids():
                return _bad("Эта катушка принтера уже в учёте")
            color = " ".join(str(body.get("color") or "").split())
            sid = await db.cc2_add_spool(p.id, color, cc2_id=cid, net_weight_g=s["net"],
                                         remaining_g=s["remaining"], low_threshold=s["low"],
                                         cost=(s["price"] or None), note=(s["note"] or None))
            await refresh(src, force=True)
            return {"ok": True, "id": sid}
        if action == "cc2_push":                    # своя катушка → завести на принтере
            sp = await db.get_spool(int(_f(body.get("spool_id"), 0) or 0))
            if not sp:
                return _bad("Катушка не найдена")
            if sp["cc2_id"]:
                return _bad("Катушка уже на принтере")
            if sp["status"] == "archived":
                return _bad("Архивную катушку переносить незачем")
            p = await db.get_plastic(sp["plastic_id"])
            if not p:
                return _bad("У катушки нет пластика")
            hexv = color_hex(sp["color"] or p.name, await db.get_color_overrides())
            if not hexv:
                return _bad(f"Не знаю, как выглядит цвет «{sp['color']}» — задайте оттенок")
            fields = {"name": spool_name(p.name, sp["color"]), "brand": brand_of(p.name),
                      "material": clean_text(p.material_type or "PLA", MATERIAL_MAX) or "PLA",
                      "color": hexv, "diameter": 1.75, "density": float(p.density or 1.24),
                      "net": max(1.0, float(sp["net_weight_g"] or 1000)),
                      "remaining": min(20000.0, max(0.0, float(sp["remaining_g"] or 0))),
                      "tare": float(p.spool_weight_g or 0), "low": float(sp["low_threshold"] or 0),
                      "price": float(sp["cost"] or 0), "note": clean_text(sp["note"] or "")}
            res = await asyncio.to_thread(_post_blocking, src.host, "/api/spools/save", form(fields))
            cid = str(res.get("id") or "")
            if not db.cc2_id_ok(cid):
                return _bad("Принтер не вернул id новой катушки")
            why = await db.cc2_link_spool(sp["id"], cid)
            await refresh(src, force=True)
            return _bad(why) if why else {"ok": True, "cc2_id": cid}
    except SpoolError as e:
        return _bad(str(e))
    return _bad("Неизвестное действие")
