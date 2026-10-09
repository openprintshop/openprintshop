"""Экраны студии: LCD-дашборд в корпусе ПК и TURZX-экран рядом с ботом.

Один предмет: локальные ЖЕЛЕЗНЫЕ экраны мастерской — статика и данные LCD-дашборда
(design_handoff_lcd, поллер ходит каждые ~2 с) и сводка/питание TURZX-панели. Обе ручки
данных отдаются ТОЛЬКО локальным запросам (`_local_request`): рендереры живут на этой же
машине, а туннель наружу несёт публичный Host и отбрасывается.

Выделено из app/webapp.py волной 3 endgame-плана (docs/Перспективные разработки/
refactoring-webapp-endgame-2026-08-12.md), после волны 2: `_api_lcd_data` смотрит на принтер
через `_live_view`/`_driver_printer_id` из app/web/printerhub.py.

МОДУЛЬНОЕ СОСТОЯНИЕ ПЕРЕЕХАЛО СЮДА ЦЕЛИКОМ: кэш ответа `_lcd_cache` и трекер «модель на
столе» `_lcd_prn` — оба словаря только МУТИРУЮТСЯ и никогда не пере-присваиваются, поэтому
фасад webapp реэкспортирует ТЕ ЖЕ объекты, и тесты, чистящие их через `webapp._lcd_prn.clear()`,
продолжают чистить настоящее состояние. А вот плоские константы (`_LCD_KEY`, `_LCD_LHM_URL`,
`_LCD_DIR`) фасадом НЕ реэкспортируются НАМЕРЕННО: подмена значения через
`monkeypatch.setattr(webapp, "_LCD_KEY", …)` перепривязала бы копию в webapp, а ручка читала
бы своё — тихое враньё вместо теста. Тесты подменяют их здесь (tests/test_lcd_guard.py).

Регистрация маршрутов ОСТАЛАСЬ в make_app строками add_get/add_post на фасадные имена — они
резолвятся при сборке приложения, поэтому забытая строка фасада падает громко на старте, а
не молча у клиента.

ЕДИНСТВЕННОЕ отличие от дословного переноса: `_LCD_DIR` считался от `__file__` webapp
(app/lcd), при переезде в app/web/ путь обязан подняться на уровень выше — иначе экран
потерял бы всю статику. Остальные тела перенесены ДОСЛОВНО.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .. import afterburner, config, db, elegoo, materials
from ..webauth import _admin_body, _local_request
from .common import _bad
from .printerhub import _driver_printer_id, _live_view

log = logging.getLogger(__name__)

try:
    import psutil
except ImportError:  # psutil опционален: без него просто не будет cpu_load/ram_pct
    psutil = None

_LCD_DIR = Path(__file__).parent.parent / "lcd"   # app/lcd — статика экрана (см. докстринг)
_LCD_KEY = os.getenv("LCD_KEY", "")
_LCD_LHM_URL = os.getenv("LCD_LHM_URL", "http://localhost:8085/data.json")
_lcd_cache: dict = {"ts": 0.0, "data": None}
# Трекер «модель на столе» для printer.state="done" (контракт v2): печать была активна и
# закончилась → done (экран: «модель можно снимать»), пока мастер не подтвердил печать
# (confirm_print / опрос журнала) или не стартовала следующая. Состояние процесса, ПО МАШИНАМ:
# общий трекер гасил бы «модель на столе» первой машины, стоило второй начать печатать.
_lcd_prn: dict[int, dict] = {}


def _lcd_prn_for(printer_id: int | None = None) -> dict:
    """Трекер стола этой машины (0 — «машина не названа»)."""
    key = int(printer_id or 0)
    st = _lcd_prn.get(key)
    if st is None:
        st = {"was_printing": False, "done": False, "file": ""}
        _lcd_prn[key] = st
    return st


def lcd_print_handled(printer_id: int | None = None) -> None:
    """Мастер обработал завершённую печать (подтвердил заказ/ответил опросу журнала) —
    считаем стол разобранным: экран с done возвращается в idle.

    Без `printer_id` гасим у ВСЕХ машин: так вызывали, пока принтер был один, и вызывающие,
    которые машину не знают, обязаны продолжать работать. Знаешь машину — назови её, иначе
    разбор печати на одной погасит «модель на столе» у второй."""
    if printer_id:
        _lcd_prn_for(printer_id)["done"] = False
        return
    for st in _lcd_prn.values():
        st["done"] = False


async def _lcd_file(request: web.Request) -> web.StreamResponse:
    rel = request.match_info.get("path", "") or "index.html"
    full = (_LCD_DIR / rel).resolve()
    if not str(full).startswith(str(_LCD_DIR.resolve())) or not full.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(full)


def _lcd_find_cpu_temp(root: dict) -> float | None:
    """Температура CPU из дерева LibreHardwareMonitor data.json — МАКСИМУМ по всем ядрам.

    Экран показывает самое горячее место камня: у Intel это ядра `CPU Core #N` / `P-Core #N`,
    у AMD — Tctl/Tdie и кристаллы `CCDn (Tdie)` (поядерных датчиков у Ryzen нет вовсе).
    Одиночный «пакетный» датчик прятал горячее ядро, а «первый найденный» мог оказаться любым.

    Датчики CPU узнаём по `SensorId` (`/amdcpu/…`, `/intelcpu/…`, LHM ≥ 0.9). У старых сборок и
    OpenHardwareMonitor его нет — тогда берём градусы внутри узла-железа с CPU в названии.
    Узел-ДАТЧИК ветку не открывает: иначе «CPU»/«CPU Socket» датчика материнки попадали в CPU.

    В максимум НЕ идут:
      * `… Distance to TjMax` у Intel — это запас до троттлинга, а не температура: в простое он
        больше самой температуры и дал бы на экране 65° у холодного камня;
      * голый `Core (Tctl)` у Ryzen со смещением (1800X/2700X/Threadripper: +10…+27°), если
        рядом есть настоящий `Tdie`. Без смещения LHM отдаёт один `Core (Tctl/Tdie)` — он честный.
    """
    by_id: list[tuple[str, float]] = []      # (Text, °C) — датчики, опознанные по SensorId
    by_tree: list[tuple[str, float]] = []    # то же по названию ветки — фолбэк без SensorId

    def celsius(val: str) -> float | None:   # «52,3 °C» / «126.1 °F» (единицы в настройках LHM)
        for unit, conv in (("°C", lambda t: t), ("°F", lambda t: (t - 32) * 5 / 9)):
            if unit in val:
                try:
                    return conv(float(val.replace(unit, "").replace(",", ".").strip()))
                except ValueError:
                    return None
        return None

    def walk(node: dict, in_cpu: bool) -> None:
        text = str(node.get("Text") or "")
        sid = str(node.get("SensorId") or "")
        val = str(node.get("Value") or "")
        if sid or val:                                   # датчик
            t = celsius(val)
            if t is not None:
                if sid.startswith(("/amdcpu/", "/intelcpu/")):
                    by_id.append((text, t))
                elif in_cpu:
                    by_tree.append((text, t))
        else:                                            # железо или группа
            in_cpu = in_cpu or any(k in text for k in ("CPU", "Ryzen", "Intel", "Core i"))
        for ch in node.get("Children") or []:
            walk(ch, in_cpu)

    walk(root, False)
    found = by_id or by_tree
    has_tdie = any("Tdie" in n for n, _ in found)
    temps = [t for n, t in found
             if "TjMax" not in n and not (has_tdie and "Tctl" in n and "Tdie" not in n)]
    return max(temps) if temps else None


async def _lcd_cpu_temp() -> float | None:
    """Температура CPU: сперва MSI Afterburner (общая память — у кого стоит Afterburner +
    RivaTuner, как на машине владельца), иначе LibreHardwareMonitor (web-сервер, JSON).
    Нет ни того, ни другого → None, и экран покажет «—»."""
    t = afterburner.read_cpu_temp()
    if t is not None:
        return t
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(_LCD_LHM_URL, timeout=aiohttp.ClientTimeout(total=0.5)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        return _lcd_find_cpu_temp(data)
    except Exception:  # noqa: BLE001
        return None


async def reap_process(proc) -> None:
    """Добить внешний процесс, переживший таймаут.

    asyncio.wait_for отменяет ОЖИДАНИЕ, но сам процесс не трогает: после каждого таймаута
    чужой exe остаётся жить. Для nvidia-smi это особенно дорого — его дёргает экран LCD
    примерно раз в 2.5 с, то есть десятки тысяч запусков в сутки (разбор 11.08)."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
        await proc.wait()
    except (ProcessLookupError, OSError):
        pass


async def _lcd_gpu() -> dict:
    """GPU-метрики через nvidia-smi с коротким таймаутом. Недоступно → пустой dict."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=0.6)
        parts = [p.strip() for p in out.decode("utf-8", "ignore").strip().split(",")]
        if len(parts) >= 4 and float(parts[3]) > 0:
            return {"gpu_load": round(float(parts[0])), "gpu_temp": round(float(parts[1])),
                    "vram_pct": round(float(parts[2]) / float(parts[3]) * 100)}
    except Exception:  # noqa: BLE001
        pass
    finally:
        await reap_process(proc)
    return {}


def _first_hex(color_name: str, overrides: dict) -> str:
    """Первый HEX цвета по названию (для точки-индикатора в очереди LCD). '' если не распознан."""
    rgbs = materials.color_to_rgbs(color_name, overrides)
    if not rgbs:
        return ""
    r, g, b = rgbs[0]
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


async def _lcd_queue(limit: int = 10) -> list[dict]:
    """Очередь ненапечатанных заказов под контракт LCD (printer.queue). Материал — тип пластика
    (короткий, как в макете), цвет — HEX первого цвета пластика; created_ts в МС."""
    rows = await db.lcd_print_queue(limit)
    if not rows:
        return []
    overrides = await db.get_color_overrides()
    pmap: dict[str, dict] = {}                          # plastic_name → {mat, hex}
    for pl in await db.list_plastics(active_only=False):
        cols = pl.color_list or []
        pmap[pl.name] = {"mat": pl.material_type or pl.name,
                         "hex": _first_hex(cols[0], overrides) if cols else ""}
    out: list[dict] = []
    for r in rows:
        item = {
            "id": r["id"],
            "customer": (r["user_name"] or "").strip(),
            "model": (r["model_label"] or "").strip(),
            "qty": int(r["quantity"] or 1),
            "est_min": round(float(r["minutes_total"] or 0)),
            "created_ts": int((r["created_at"] or 0) * 1000),   # секунды → мс (Date.now())
        }
        pn = r["plastic_name"]
        if pn:
            meta = pmap.get(pn)
            item["material"] = (meta["mat"] if meta else pn)
            if meta and meta["hex"]:
                item["color"] = meta["hex"]
        out.append(item)
    return out


async def _api_lcd_data(request: web.Request) -> web.Response:
    """Данные LCD-дашборда (контракт design_handoff_lcd/CLAUDE_CODE_PROMPT.md): printer (+queue
    в простое) + alerts + system. Недоступные метрики НЕ включаются (фронт мержит по-секционно).
    Кэш ~2 с (поллер ходит каждые 2 с), внешние чтения с короткими таймаутами — event loop свободен."""
    # Локальность — ПЕРВЕЕ ключа и независимо от него: LCD_KEY по умолчанию пуст, поэтому одной
    # проверки «if _LCD_KEY» не хватало вовсе — состояние принтера, очередь заказов с именами
    # клиентов и метрики машины уходили любому, кто дотянулся до порта (туннель коннектится
    # с 127.0.0.1, но несёт публичный Host — его ловит именно _local_request). Рендерер экрана
    # живёт на ЭТОЙ же машине (turzx-display/bot_screen.py жёстко на 127.0.0.1:8081), так что
    # ужесточение его не отрезает — те же условия у соседних /api/screen-status и /api/screen-power.
    if not _local_request(request):
        return web.json_response({"ok": False, "error": "local only"}, status=403)
    if _LCD_KEY and request.query.get("k", "") != _LCD_KEY:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    now = time.time()
    if _lcd_cache["data"] is not None and now - _lcd_cache["ts"] < 2.0:
        return web.json_response(_lcd_cache["data"])
    out: dict = {}
    # ── printer: тот же источник, что /api/admin/printer_status, но ЧЕРЕЗ ДРАЙВЕР ──
    # Экран у мастера один и показывает машину по умолчанию; трекер стола при этом свой у
    # каждой — иначе вторая машина, начав печатать, погасила бы «модель на столе» у первой.
    state, printing, prog01, drv = await _live_view(max_age=6.0)
    prn = _lcd_prn_for(_driver_printer_id(drv))
    if state is not None:
        ps_ = state.get("print_stats") or {}
        raw = state.get("print_status") or {}
        info = state.get("info") or {}
        summary = elegoo.summarize(state)
        # печать была активна и перестала → «done»: модель на столе, можно снимать
        # (контракт v2); сбрасывается в lcd_print_handled() или со стартом следующей печати
        if printing:
            prn.update(was_printing=True, done=False)
        elif prn["was_printing"]:
            prn.update(was_printing=False, done=True)
        p: dict = {"state": "printing" if printing
                   else ("done" if prn["done"] else "idle")}
        if isinstance(ps_.get("filename"), str) and ps_["filename"].strip():
            p["file"] = ps_["filename"].strip()
            if printing:
                prn["file"] = p["file"]
        elif prn["done"] and prn["file"]:
            p["file"] = prn["file"]        # при done телеметрия имя уже не шлёт — отдаём последнее
        if prog01 is not None:
            p["progress"] = round(prog01 * 100, 1)
        if isinstance(info.get("current_layer"), int):
            p["layer"] = info["current_layer"]
        if isinstance(info.get("total_layer"), int):
            p["layers_total"] = info["total_layer"]
        # Остаток печати: remaining_sec из enrich Elegoo (прошивка, служба сообщества или оценка
        # по прогрессу), у прочих драйверов — сырой print_status. Ноль посреди печати значит «не
        # знаю» — не шлём: экран покажет «—», а не «0 мин» (файлы Orca на CC2, 06.10.2026).
        rem = state.get("remaining_sec")
        if not isinstance(rem, (int, float)):
            rem = raw.get("remaining_time_sec")
            if printing and not (isinstance(rem, (int, float)) and rem > 0):
                rem = None
        if isinstance(rem, (int, float)) and rem >= 0:
            p["remain_min"] = round(float(rem) / 60)
        pairs = (("nozzle", "extruder_temp"), ("nozzle_target", "extruder_target"),
                 ("bed", "bed_temp"), ("bed_target", "bed_target"))
        for dst, src in pairs:
            v = summary.get(src)
            if isinstance(v, (int, float)):
                p[dst] = round(float(v))
        if p["state"] == "done":
            p["nozzle_target"], p["bed_target"] = 0, 0   # остываем — целей нет (контракт v2)
    else:
        # нет телеметрии → принтер не печатает: держим done, если печать только что закончилась,
        # иначе idle. Эмитим ВСЕГДА (а не «оставить последнее»), иначе после рестарта/офлайна
        # экран навечно завис бы на «печатает» и не показал бы очередь.
        p = {"state": "done" if prn["done"] else "idle"}
        if prn["done"] and prn["file"]:
            p["file"] = prn["file"]
            p["nozzle_target"], p["bed_target"] = 0, 0
    # очередь ненапечатанных заказов — для экрана простоя (фронт игнорит при printing/done)
    if p["state"] != "printing":
        p["queue"] = await _lcd_queue()
    out["printer"] = p
    # ── alerts: те же счётчики, что в админ-хабе + «мастер не открывал карточку» ──
    unread = await db.orders_unread_admin()
    out["alerts"] = {"messages": sum(unread.values()),
                     "unseen_orders": await db.count_unseen_requests()}
    # ── system: метрики этого ПК ──
    sysd: dict = {}
    if psutil is not None:
        try:
            sysd["cpu_load"] = round(psutil.cpu_percent(interval=None))
            sysd["ram_pct"] = round(psutil.virtual_memory().percent)
        except Exception:  # noqa: BLE001
            pass
    sysd.update(await _lcd_gpu())
    ct = await _lcd_cpu_temp()
    if ct is not None:
        sysd["cpu_temp"] = round(ct)
    if sysd:
        out["system"] = sysd
    _lcd_cache["ts"], _lcd_cache["data"] = now, out
    return web.json_response(out)


async def _api_screen_status(request: web.Request) -> web.Response:
    """Сводка для TURZX-экрана рядом с ботом (docs/В процессе реализации/turzx-display.md).

    Без подписи Telegram (рендерер — простой локальный скрипт), но ТОЛЬКО для локальных
    запросов: peer должен быть loopback И Host — локальным. Второе важно, потому что
    cloudflared/tailscale-туннель тоже коннектится с 127.0.0.1, но несёт публичный Host —
    такие запросы отбрасываем, выручка наружу не утекает."""
    if not _local_request(request):
        return web.json_response({"ok": False, "error": "local only"}, status=403)
    stats = await db.dashboard_stats()
    series = stats.get("series14") or []
    status_mix = stats.get("status_mix") or {}
    unread = await db.orders_unread_admin()
    q = await db.queue_load()
    printer: dict = {"online": False, "printing": False}
    # Через драйвер, как и LCD-дашборд: своя копия цепочки cached_status→enrich→print_stats
    # знала ровно один принтер и ровно один диалект (см. _live_view).
    state, printing, prog01, _drv = await _live_view(max_age=30.0)
    if state is not None:
        ps = state.get("print_stats") or {}
        info = state.get("info") or {}
        summary = elegoo.summarize(state)
        printer = {
            "online": True,
            "printing": printing,
            "job": ps.get("filename") or "",
            "progress": round(prog01 * 100) if prog01 is not None else None,
            "layer": info.get("current_layer"),
            "remaining": state.get("remaining") or "",
            "eta": state.get("eta_at") or "",
            "nozzle": summary.get("extruder_temp"),
            "bed": summary.get("bed_temp"),
        }
    return web.json_response({"ok": True, "currency": config.CURRENCY,
        "enabled": await _turzx_enabled(),
        "revenue_today": (series[-1]["revenue"] if series else 0.0),
        "revenue_week": stats.get("orev_7d", 0),
        "orders_week": stats.get("orders_7d", 0),
        "avg_order": stats.get("avg_order", 0),
        "queue": q,
        "attention": {"new_orders": status_mix.get("new", 0), "unread": sum(unread.values()),
                      "price_wait": status_mix.get("price_changed", 0)},
        "printer": printer})


async def _turzx_enabled() -> bool:
    """Включён ли TURZX-экран (флаг из админки, хранится в settings.turzx_enabled).
    По умолчанию — включён (ключа в таблице нет → экран работает как раньше)."""
    s = await db.get_settings()
    try:
        return bool(int(s.get("turzx_enabled", 1)))
    except (TypeError, ValueError):
        return True


async def _api_screen_power(request: web.Request) -> web.Response:
    """Лёгкий флаг питания TURZX-экрана для рендерера (turzx-display/bot_screen.py поллит его
    ~раз в 3 с и гасит/зажигает подсветку). Без подписи Telegram, но ТОЛЬКО локально — как
    /api/screen-status (наружу состояние экрана не утекает)."""
    if not _local_request(request):
        return web.json_response({"ok": False, "error": "local only"}, status=403)
    return web.json_response({"ok": True, "enabled": await _turzx_enabled()})


async def _api_admin_screen_power(request: web.Request) -> web.Response:
    """Вкл/выкл TURZX-экран из админки. Тело: {on: bool}. Рендерер подхватит при следующем поле
    и погасит/зажжёт подсветку панели."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    await db.set_setting("turzx_enabled", 1 if body.get("on") else 0)
    return web.json_response({"ok": True, "turzx": {"enabled": await _turzx_enabled()}})
