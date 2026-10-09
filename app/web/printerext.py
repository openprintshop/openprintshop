"""Пульт принтера, вторая очередь: то, что подсмотрено у прошивки сообщества CC2 и сделано своим.

Разбор и план — docs/Перспективные разработки/cc2-community-features-2026-10-03.md. Две группы:

* **стоковые команды** (работают у любого CC2): режим скорости (1031), автодозаправка хаба
  (2004), сведения о файле перед стартом (1046), быстрые выключатели нагрева и обдува,
  «мягкий» аварийный стоп, профили прогрева по материалам;
* **прошивка сообщества** (`Neme77/centauri-carbon-2-community`, только если она стоит на
  машине): снять деталь на ходу, карта стола и калибровка, замер винтов, живой Z-offset,
  скорость и поток в %, консоль G-code, правка слота хаба, ресурсы принтера. Ходим к её службе
  по HTTP через app/cc2community.py; кнопки гейтятся способностями драйвера, которые он
  включает сам, когда находит её API.

Все ручки — право `printer` (webguard._OPERATOR_ROUTES). Аутентификация зовётся через модуль
(`webauth._verify_admin`), чтобы подмена в тестах (`stub_auth`) доставала сюда без правки
списка модулей conftest.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

from aiohttp import web

from .. import cc2community, config, db, elegoo, webauth
from ..webauth import _admin_forbidden
from .common import _bad, _toint
from .printerhub import _no_printer, _printer_driver, _printer_id_arg

log = logging.getLogger(__name__)
routes = web.RouteTableDef()


def _int(v) -> int | None:
    """Целое из тела запроса или None. Общий `_toint` по умолчанию отдаёт 0, а ноль здесь —
    законное значение (режим «тихий», вентилятор 0 %), поэтому «мусор» должен быть отличим."""
    if isinstance(v, bool):
        return None
    return _toint(v, None)


def _cant(drv) -> web.Response:
    return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})


async def _post_driver(request: web.Request):
    """Тело POST + драйвер карточки из `printer_id` (или принтер по умолчанию) — либо отказ."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None, None, _bad()
    _uid, err = webauth._admin_body(body)
    if err:
        return None, None, err
    drv = await _printer_driver(_toint(body.get("printer_id")) or None)
    if not drv.enabled():
        return None, None, _no_printer(drv)
    return body, drv, None


async def _printing(drv) -> bool:
    """Занята ли машина заданием (включая паузу и прогрев) — по кэшу, без сети."""
    st = await asyncio.to_thread(drv.status, 30.0) or {}
    return bool(drv.job_state(st.get("state") or {}).get("active"))


def _clean_slot_map(raw: list) -> tuple[list[dict], str]:
    """Карта «филамент gcode → слот хаба» из тела запроса → проверенная форма для 1020.

    t — номер инструмента в gcode (T0…T15), tray_id — слот−1 (0…3), canvas_id — хаб (у CC2
    один, 0). Один инструмент — один слот: два назначения на T1 принтер разрешит как ему
    вздумается. Два инструмента на один слот допустимы (одинаковый филамент под разными T)."""
    out: list[dict] = []
    seen: set[int] = set()
    for m in raw:
        if not isinstance(m, dict):
            return [], "Неверная карта слотов"
        try:
            t, tray, canvas = int(m.get("t")), int(m.get("tray_id")), int(m.get("canvas_id") or 0)
        except (TypeError, ValueError):
            return [], "Неверная карта слотов"
        if not (0 <= t <= 15) or not (0 <= tray <= 3) or canvas != 0:
            return [], "Слот вне диапазона: инструменты T0…T15, слоты 1…4"
        if t in seen:
            return [], f"Филаменту T{t} назначено несколько слотов"
        seen.add(t)
        out.append({"canvas_id": canvas, "t": t, "tray_id": tray})
    return out, ""


# ─── стоковые команды ────────────────────────────────────────────────────────────────────────

@routes.get("/api/admin/printer_file_info")
async def _api_printer_file_info(request: web.Request) -> web.Response:
    """Филаменты файла (color_map), время, слои — для диалога «какой слот на какой филамент»."""
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    drv = await _printer_driver(_printer_id_arg(request))
    if not drv.enabled():
        return _no_printer(drv)
    name = (request.query.get("name") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "Не указан файл"})
    storage = (request.query.get("storage") or "local").strip()
    if storage != "local" and not drv.caps.usb:
        return _cant(drv)
    return web.json_response(await asyncio.to_thread(drv.file_info, name, storage=storage))


_THUMB_KEEP = 400                    # сколько превью держать в кэше на диске ПК
# К принтеру за превью — строго по одному: список файлов открывают разом на десяток строк, а
# их служба однопоточная и на слабом железе (111 МБ, 2 ядра).
_thumb_gate = asyncio.Semaphore(1)


def _thumb_cache(host: str, storage: str, path: str, stamp: str):
    key = hashlib.sha1(f"{host}|{storage}|{path}|{stamp}".encode("utf-8")).hexdigest()
    return config.DATA_DIR / "cache" / "cc2_thumbs", key


def _thumb_hit(folder, key) -> web.StreamResponse | None:
    for ext in (".png", ".jpg"):
        f = folder / (key + ext)
        if f.exists():
            return web.FileResponse(f, headers={"Cache-Control": "private, max-age=86400"})
    if (folder / (key + ".none")).exists():
        return web.Response(status=404)
    return None


@routes.get("/api/admin/printer_thumb")
async def _api_printer_thumb(request: web.Request) -> web.StreamResponse:
    """Превью файла на принтере — из самого G-code (прошивка сообщества). Ключ кэша — путь и
    время изменения файла: тот же файл к принтеру второй раз не ходит, «превью нет» — тоже."""
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    drv = await _printer_driver(_printer_id_arg(request))
    if not drv.enabled() or not drv.caps.thumbs:
        return web.Response(status=404)
    path = (request.query.get("path") or "").strip()
    storage = (request.query.get("storage") or "local").strip()
    if (not path or len(path) > 512 or "\n" in path or ".." in path.split("/")
            or storage not in cc2community.STORAGES):
        return web.Response(status=400)
    folder, key = _thumb_cache(drv.community_host(), storage, path,
                               (request.query.get("m") or "").strip()[:20])
    hit = _thumb_hit(folder, key)
    if hit is not None:
        return hit
    async with _thumb_gate:
        hit = _thumb_hit(folder, key)               # пока ждали очереди, его мог принести сосед
        if hit is not None:
            return hit
        try:
            got = await asyncio.to_thread(drv.thumbnail, path, storage=storage)
        except cc2community.CommunityError:
            return web.Response(status=502)
    folder.mkdir(parents=True, exist_ok=True)
    if not got:
        (folder / (key + ".none")).write_bytes(b"")
        return web.Response(status=404)
    data, ctype = got
    (folder / (key + (".png" if ctype == "image/png" else ".jpg"))).write_bytes(data)
    try:                                            # кэш не растёт бесконечно: старые — вон
        old = sorted(folder.iterdir(), key=lambda f: f.stat().st_mtime)
        for f in old[:max(0, len(old) - _THUMB_KEEP)]:
            f.unlink(missing_ok=True)
    except OSError:
        log.debug("кэш превью не почищен", exc_info=True)
    return web.Response(body=data, content_type=ctype,
                        headers={"Cache-Control": "private, max-age=86400"})


@routes.post("/api/admin/printer_speed")
async def _api_printer_speed(request: web.Request) -> web.Response:
    """Режим скорости печати: 0 тихий, 1 баланс, 2 спорт, 3 безумный (method 1031)."""
    body, drv, err = await _post_driver(request)
    if err:
        return err
    if not drv.caps.speed_mode:
        return _cant(drv)
    mode = _int(body.get("mode"))
    if mode not in elegoo.SPEED_MODES:
        return web.json_response({"ok": False, "error": "Режим скорости — 0…3"})
    return web.json_response(await asyncio.to_thread(drv.set_speed_mode, mode))


@routes.post("/api/admin/printer_refill")
async def _api_printer_refill(request: web.Request) -> web.Response:
    """Автодозаправка хаба: вкл/выкл (method 2004)."""
    body, drv, err = await _post_driver(request)
    if err:
        return err
    if not drv.caps.auto_refill:
        return _cant(drv)
    return web.json_response(await asyncio.to_thread(drv.set_auto_refill, bool(body.get("on"))))


# Что выключается. heaters — сопло и стол в 0; fans — три пользовательских вентилятора в 0;
# all — и то и другое; soft_stop — «мягкий» аварийный стоп стоковой прошивки: прервать печать,
# если она идёт, и сразу всё выключить. Настоящий M112 бывает только с прошивкой сообщества.
_OFF_KINDS = {"heaters", "fans", "all", "soft_stop"}
_FANS = ("model", "aux", "box")


@routes.post("/api/admin/printer_off")
async def _api_printer_off(request: web.Request) -> web.Response:
    body, drv, err = await _post_driver(request)
    if err:
        return err
    what = (body.get("what") or "").strip()
    if what not in _OFF_KINDS:
        return web.json_response({"ok": False, "error": "Неизвестно, что выключать"})
    if not drv.caps.move:
        return _cant(drv)
    printing = await _printing(drv)
    # Обдув посреди печати сбивает слой, а «выключить всё» без остановки печати оставило бы
    # сопло ползать по холодному столу. Для печати есть ровно один честный путь — soft_stop.
    if printing and what in ("fans", "all"):
        return web.json_response({"ok": False, "error": (
            "Идёт печать: выключать обдув нельзя. Чтобы остановить всё — «Аварийный стоп»")})
    done: list[str] = []
    errors: list[str] = []

    def _run():
        if what == "soft_stop" and printing:
            if not drv.caps.control:
                errors.append("принтер не умеет останавливать печать")
                return
            r = drv.control("stop")
            (done if r.get("ok") else errors).append(
                "печать остановлена" if r.get("ok") else (r.get("error") or "стоп не прошёл"))
        if what in ("heaters", "all", "soft_stop"):
            for target in ("extruder", "heater_bed"):
                r = drv.set_temperature(target, 0)
                if not r.get("ok"):
                    errors.append(r.get("error") or f"{target}: не выключился")
            done.append("нагрев выключен")
        if what in ("fans", "all", "soft_stop"):
            for fan in _FANS:
                r = drv.set_fan(fan, 0)
                if not r.get("ok"):
                    errors.append(r.get("error") or f"{fan}: не выключился")
            done.append("обдув выключен")

    await asyncio.to_thread(_run)
    if errors:
        return web.json_response({"ok": False, "done": done, "error": "; ".join(errors)})
    return web.json_response({"ok": True, "done": done})


# Ход осей CC2 (мм) — те же пределы, что держит CC2 Control прошивки сообщества: X 0…256,
# Y 0…266, Z −2…256. Проверяем, только когда позиция головы известна из телеметрии и ось
# запаркована: без этого «пределы» были бы догадкой, а последнее слово всё равно за прошивкой.
_CC2_TRAVEL = {"x": (0.0, 256.0), "y": (0.0, 266.0), "z": (-2.0, 256.0)}


def _move_out_of_bounds(drv, axis: str, distance) -> str:
    if getattr(drv, "kind", "") != "elegoo":
        return ""
    ax = (axis or "").lower()
    lim = _CC2_TRAVEL.get(ax)
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return ""
    if lim is None:
        return ""
    st = (drv.status(30.0) or {}).get("state") or {}
    gm = st.get("gcode_move") or {}
    homed = str((st.get("tool_head") or {}).get("homed_axes") or "").lower()
    cur = gm.get(ax)
    if not isinstance(cur, (int, float)) or ax not in homed:
        return ""
    target = float(cur) + d
    if target < lim[0] - 1e-6 or target > lim[1] + 1e-6:
        return (f"Ось {ax.upper()} уйдёт за пределы хода ({lim[0]:g}…{lim[1]:g} мм): сейчас "
                f"{float(cur):.1f}, после сдвига {target:.1f}")
    return ""


# ─── профили прогрева ────────────────────────────────────────────────────────────────────────
# Раньше на пульте жили две зашитые кнопки (PLA 210/60, PETG 240/80). Теперь — список, который
# мастер правит сам; хранится в текстовой настройке. Значения по умолчанию — обычные паспортные
# температуры материалов.

HEAT_PRESETS_KEY = "heat_presets"
DEFAULT_HEAT_PRESETS = [
    {"name": "PLA", "nozzle": 210, "bed": 60},
    {"name": "PETG", "nozzle": 240, "bed": 80},
    {"name": "ABS", "nozzle": 250, "bed": 100},
    {"name": "ASA", "nozzle": 255, "bed": 100},
    {"name": "TPU", "nozzle": 220, "bed": 50},
    {"name": "PA-CF", "nozzle": 285, "bed": 100},
]
_PRESET_NAME = re.compile(r"^[\w+\-. ]{1,16}$")


def _clean_presets(raw) -> tuple[list[dict], str]:
    if not isinstance(raw, list) or not raw:
        return [], "Нужен хотя бы один профиль"
    if len(raw) > 12:
        return [], "Не больше 12 профилей"
    out, seen = [], set()
    for p in raw:
        if not isinstance(p, dict):
            return [], "Неверный профиль"
        name = str(p.get("name") or "").strip()
        nozzle, bed = _int(p.get("nozzle")), _int(p.get("bed"))
        if not _PRESET_NAME.match(name):
            return [], "Имя профиля — до 16 букв, цифр, + - . _"
        if name.casefold() in seen:
            return [], f"Профиль «{name}» повторяется"
        if nozzle is None or not (0 <= nozzle <= 320) or bed is None or not (0 <= bed <= 110):
            return [], f"«{name}»: сопло 0…320°, стол 0…110°"
        seen.add(name.casefold())
        out.append({"name": name, "nozzle": nozzle, "bed": bed})
    return out, ""


async def heat_presets() -> list[dict]:
    raw = await db.get_text_setting(HEAT_PRESETS_KEY, "")
    if raw:
        try:
            got, why = _clean_presets(json.loads(raw))
            if not why:
                return got
        except ValueError:
            log.warning("heat_presets: битый JSON в настройке, беру значения по умолчанию")
    return [dict(p) for p in DEFAULT_HEAT_PRESETS]


@routes.get("/api/admin/heat_presets")
async def _api_heat_presets_get(request: web.Request) -> web.Response:
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    return web.json_response({"ok": True, "presets": await heat_presets(),
                              "defaults": DEFAULT_HEAT_PRESETS})


@routes.post("/api/admin/heat_presets")
async def _api_heat_presets_set(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = webauth._admin_body(body)
    if err:
        return err
    if body.get("reset"):
        await db.set_text_setting(HEAT_PRESETS_KEY, "")
        return web.json_response({"ok": True, "presets": await heat_presets()})
    presets, why = _clean_presets(body.get("presets"))
    if why:
        return web.json_response({"ok": False, "error": why})
    await db.set_text_setting(HEAT_PRESETS_KEY, json.dumps(presets, ensure_ascii=False))
    return web.json_response({"ok": True, "presets": presets})


# ─── прошивка сообщества ─────────────────────────────────────────────────────────────────────

async def _community(drv, fn, *args):
    """Вызов клиента службы сообщества в потоке; её отказ — честная строка, не исключение."""
    host = drv.community_host()
    if not host or not drv.community():
        return {"ok": False, "error": "На принтере нет прошивки сообщества"}
    try:
        res = await asyncio.to_thread(fn, host, *args)
    except cc2community.CommunityError as e:
        return {"ok": False, "error": str(e)}
    if isinstance(res, dict) and "ok" not in res:
        res = {"ok": True, **res}
    return res


_GET_WHAT = {
    # что → (нужная способность, функция клиента)
    "objects": ("exclude_object", cc2community.objects),
    "mesh": ("bed_mesh", cc2community.mesh),
    "console": ("gcode", cc2community.console),
    "health": ("", cc2community.health),
    "tuning": ("live_tune", cc2community.tuning),
}


@routes.get("/api/admin/printer_community")
async def _api_community_get(request: web.Request) -> web.Response:
    """Чтение у службы сообщества: детали задания, карта стола, консоль, ресурсы, скорость/поток,
    результат замера винтов (`what=screws`)."""
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    drv = await _printer_driver(_printer_id_arg(request))
    if not drv.enabled():
        return _no_printer(drv)
    what = (request.query.get("what") or "").strip()
    if what == "info":
        return web.json_response({"ok": True, "community": drv.community(),
                                  "zoffset": cc2community.zoffset_session(drv.community_host())})
    if what == "screws":
        if not drv.caps.bed_mesh:
            return _cant(drv)
        res = await _community(drv, cc2community.console)
        if res.get("ok"):
            values = cc2community.screws_from_console(res.get("output") or "")
            res["plan"] = cc2community.screws_plan(values) if values else None
            res.pop("output", None)      # сырой лог замера мастеру не нужен, нужен итог
        return web.json_response(res)
    if what not in _GET_WHAT:
        return web.json_response({"ok": False, "error": "Неизвестный запрос"})
    need, fn = _GET_WHAT[what]
    if need and not getattr(drv.caps, need, False):
        return _cant(drv)
    res = await _community(drv, fn)
    if what == "tuning" and res.get("ok"):
        res["zoffset"] = cc2community.zoffset_session(drv.community_host())
    return web.json_response(res)


_SLOT_TYPE = re.compile(r"^[A-Za-z0-9+_\-]{1,16}$")
_HEX6 = re.compile(r"^#?[0-9A-Fa-f]{6}$")


@routes.post("/api/admin/printer_community")
async def _api_community_post(request: web.Request) -> web.Response:
    """Действия через службу сообщества. Набор op закрытый; всё, что не названо, — отказ."""
    body, drv, err = await _post_driver(request)
    if err:
        return err
    op = (body.get("op") or "").strip()
    caps = drv.caps

    if op == "exclude":
        if not caps.exclude_object:
            return _cant(drv)
        if not await _printing(drv):
            return web.json_response({"ok": False, "error": "Снять деталь можно только во время печати"})
        name = str(body.get("name") or "").strip()
        return web.json_response(await _community(drv, cc2community.exclude, name))

    if op == "mesh_calibrate":
        if not (caps.bed_mesh and caps.gcode):
            return _cant(drv)
        if await _printing(drv):
            return web.json_response({"ok": False, "error": "Калибровка стола — только в простое"})
        side = str(body.get("side") or "A").strip().upper()
        profile = {"A": "default", "B": "default1"}.get(side)
        if not profile:
            return web.json_response({"ok": False, "error": "Сторона пластины — A или B"})
        return web.json_response(await _community(
            drv, cc2community.console_send, f"BED_MESH_CALIBRATE PROFILE={profile} BED_TEMP=60"))

    if op == "screws_measure":
        if not caps.bed_mesh:
            return _cant(drv)
        if await _printing(drv):
            return web.json_response({"ok": False, "error": "Замер винтов — только в простое"})
        return web.json_response(await _community(drv, cc2community.action, "screws:measure"))

    if op == "zoffset":
        if not caps.live_tune:
            return _cant(drv)
        if body.get("undo"):
            return web.json_response(await _community(drv, cc2community.zoffset_undo))
        try:
            delta = float(body.get("delta"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Неверный шаг Z-offset"})
        return web.json_response(await _community(drv, cc2community.zoffset_adjust, delta))

    if op == "tune":
        if not caps.live_tune:
            return _cant(drv)
        if not await _printing(drv):
            return web.json_response({"ok": False, "error": "Скорость и поток меняются только во время печати"})
        kind = "speed" if body.get("speed") is not None else "flow"
        val = _int(body.get(kind))
        lo, hi = (25, 200) if kind == "speed" else (50, 150)
        if val is None or not (lo <= val <= hi):
            return web.json_response({"ok": False, "error": f"{'Скорость' if kind == 'speed' else 'Поток'}: {lo}…{hi}%"})
        return web.json_response(await _community(drv, cc2community.action, f"tune:{kind}:{val}"))

    if op == "heat":
        # Цели нагрева во время печати и паузы (их `heaters:set`: два SET_HEATER_TEMPERATURE без
        # ожидания и без движения). Стоковый 1028 в печати принтер сбивает задание, поэтому наш
        # прогрев в печати закрыт; их служба проверена на живом CC2. В простое — прежний 1028.
        if not caps.live_tune:
            return _cant(drv)
        if not await _printing(drv):
            return web.json_response({"ok": False, "error": "В простое нагрев — кнопками прогрева"})
        nozzle, bed = _int(body.get("nozzle")), _int(body.get("bed"))
        if nozzle is None or bed is None or not (0 <= nozzle <= 300) or not (0 <= bed <= 120):
            return web.json_response({"ok": False, "error": "Сопло 0…300 °C, стол 0…120 °C"})
        return web.json_response(await _community(drv, cc2community.action,
                                                  f"heaters:set:{nozzle}:{bed}"))

    if op == "extrude":
        if not caps.gcode:
            return _cant(drv)
        try:
            mm = round(float(body.get("mm")), 2)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Неверная длина"})
        if not (0.1 <= abs(mm) <= 25):
            return web.json_response({"ok": False, "error": "Экструзия — от 0.1 до 25 мм в любую сторону"})
        return web.json_response(await _community(drv, cc2community.action, f"extrude:{mm}"))

    if op == "motors_off":
        if not caps.gcode:
            return _cant(drv)
        return web.json_response(await _community(drv, cc2community.action, "system:motors_off"))

    if op == "estop":
        if not caps.gcode:
            return _cant(drv)
        log.warning("Аварийный стоп M112 по команде мастера (принтер %s)", drv.community_host())
        return web.json_response(await _community(drv, cc2community.action, "system:emergency_stop"))

    if op == "console":
        if not caps.gcode:
            return _cant(drv)
        cmd = str(body.get("command") or "").strip()
        log.info("Консоль G-code (принтер %s): %s", drv.community_host(), cmd[:200])
        return web.json_response(await _community(drv, cc2community.console_send, cmd))

    if op == "console_clear":
        if not caps.gcode:
            return _cant(drv)
        return web.json_response(await _community(drv, cc2community.console_clear))

    if op == "slot_edit":
        if not caps.slot_edit:
            return _cant(drv)
        if await _printing(drv):
            return web.json_response({"ok": False, "error": "Править слот можно только в простое"})
        slot = _int(body.get("slot"))
        typ = str(body.get("type") or "").strip()
        color = str(body.get("color") or "").strip()
        tmin, tmax = _int(body.get("tmin")), _int(body.get("tmax"))
        if slot is None or not (1 <= slot <= 4):
            return web.json_response({"ok": False, "error": "Слот — 1…4"})
        if not _SLOT_TYPE.match(typ):
            return web.json_response({"ok": False, "error": "Материал — до 16 латинских букв, цифр, + - _"})
        if not _HEX6.match(color):
            return web.json_response({"ok": False, "error": "Цвет — шесть шестнадцатеричных цифр"})
        if tmin is None or tmax is None or not (120 <= tmin < tmax <= 320):
            return web.json_response({"ok": False, "error": "Температуры сопла: 120…320°, мин меньше макс"})
        act = f"canvas:material:{slot - 1}:{typ}:{color.lstrip('#').upper()}:{tmin}:{tmax}"
        return web.json_response(await _community(drv, cc2community.action, act))

    return web.json_response({"ok": False, "error": "Неизвестное действие"})
