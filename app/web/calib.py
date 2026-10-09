"""Калибровка филамента: 24 ручки админки плюс страница образца, открываемая по QR с этикетки.

Доменные правила и работа с пресетами — app/calibration.py, хранение — db.calib_*.
Здесь только транспорт: собрать ответ экрану и применить действие мастера.

Выделено из app/webapp.py волной 4 цикла 2 — это главный разрез цикла и самый чистый доменный
блок файла. Тела функций перенесены ДОСЛОВНО, без единой правки: блок закрыт tests/test_calib_api.py
целиком, и «заодно поправил» здесь стоило бы дороже самого переноса. app/webapp.py реэкспортирует
все имена фасадом, поэтому и код, и тесты продолжают находить их как webapp.*.

МАРШРУТЫ ЖИВУТ ЗДЕСЬ ЖЕ, в таблице `routes` над обработчиками, а make_app подключает их одной
строкой `app.add_routes(calib.routes)`. Пути и порядок регистрации не изменились ни на символ:
канонический путь ресурса тот же, поэтому `_caps_mw` (право CAP_PRINTER задано одной строкой
префикса `/api/admin/calib` в app/webguard.py), `_usage_mw` и `_error_mw` видят его как раньше.

`/csample/{sid}` — единственный маршрут блока вне `/api/admin`: страница образца открывается по
подписи ссылки, без initData. QR-этикетки с этими ссылками НАКЛЕЕНЫ НА ФИЗИЧЕСКИЕ ОБРАЗЦЫ, их
нельзя перевыпустить — ни путь, ни префикс подписи `csample` трогать нельзя.

Модуль не импортирует webapp: помощники берутся напрямую из app/web/common.py, app/webauth.py,
app/websign.py и app/printmedia.py (последний завела волна 3 — ради `_heal_journal_timelapse`,
и это та самая причина, по которой медиа шли перед калибровкой).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from .. import calibpdf, calibration, cc2plates, config, db
from ..printmedia import _heal_journal_timelapse
from ..webauth import _admin_body, _admin_forbidden, _verify_admin
from ..websign import _file_ver
from .common import _ago, _bad, _fnum, _num, _photo_url, _print_settings_view, _toint, check_sig, sig, signed_url

log = logging.getLogger(__name__)


# Маршруты блока объявляются рядом с обработчиком, а не списком в make_app: при переносе
# блока в отдельный модуль строка регистрации уезжает вместе с ручкой и потеряться не может.
# Канонический путь ресурса тот же, поэтому _caps_mw/_usage_mw/_error_mw читают его как раньше.
routes = web.RouteTableDef()


def _calib_test_dicts() -> list[dict]:
    """Реестр тестов в виде, пригодном для UI (dataclass наружу не отдаём)."""
    out = []
    for k in calibration.TEST_ORDER:
        t = calibration.TESTS[k]
        out.append({"key": t.key, "label": t.label, "order": t.order, "param": t.param,
                    "scope": t.scope, "unit": t.unit, "method": t.method, "look": t.look,
                    "hint": t.hint, "depends": list(t.depends), "formula": t.formula,
                    "kind": t.kind, "params": _calib_params(t)})
    return out


def _calib_params(t) -> list[dict]:
    """Параметры теста для UI. Глажка настраивается парой (поток + скорость), поэтому экран
    рисует по полю на параметр, а не одно поле «результат»."""
    return [{"key": p.key, "label": p.label, "unit": p.unit, "scope": p.scope, "pct": p.pct}
            for p in t.params]


def _calib_step_view(st: dict) -> dict:
    """Шаг + сведения о тесте: экран не должен знать реестр наизусть."""
    t = calibration.test_info(st.get("test_key") or "")
    d = dict(st)
    if t:
        vals = d.get("values") or {}
        d.update({"label": t.label, "method": t.method, "look": t.look, "hint": t.hint,
                  "param": t.param, "scope": t.scope, "test_unit": t.unit, "test_kind": t.kind,
                  "formula": t.formula, "params": _calib_params(t)})
        # у основного параметра значение может лежать только в value (старые шаги до колонки
        # values_json) — показываем его, чтобы поле не выглядело пустым
        if t.params and t.param not in vals and d.get("value") is not None:
            vals = {**vals, t.param: d["value"]}
        d["values"] = vals
    return d


async def _calib_journal_view(entry: dict) -> dict:
    """Печать журнала для экрана калибровки: фото + таймлапс уже есть у каждой печати."""
    it = dict(entry)
    it["ago"] = _ago(it["created_at"])
    it["photo"] = (_photo_url("journal", it["id"], it.get("done_photo"))
                   if it.get("done_photo") else "")
    tl = await _heal_journal_timelapse(it)
    it["timelapse_url"] = (signed_url("jtl", it["id"]) if tl else "")
    t = calibration.test_info(it.get("calib_type") or "")
    it["test_label"] = t.label if t else ""
    # Калибровочная печать без «чем печатали» бесполезна: шаг кампании и есть настройка.
    it["settings"] = await _print_settings_view(it)
    return it


def _sample_url(sid: int) -> str:
    return signed_url("csample", sid)


@routes.get("/api/admin/calib")
async def _api_admin_calib(request: web.Request) -> web.Response:
    """Экран «Калибровка»: сводка, кампании, паспорта, снимки пресетов, справочники."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    passports = await db.list_calib_passports()
    for p in passports:
        p["ago"] = _ago(p.get("calibrated_at") or p.get("updated_at") or 0)
    loose = [await _calib_journal_view(e)
             for e in await db.list_calib_journal(calib_only=True, limit=60)]
    samples = await db.list_calib_samples()
    for s in samples:
        s["photo_url"] = _photo_url("calib", s["id"], s.get("photo")) or ""
        s["url"] = _sample_url(s["id"])
    # Пластины принтера с CC2 Control ведёт его библиотека: экран открыли — сверяемся (не чаще
    # раза в cc2plates.STALE_S, фоном не опрашиваем).
    src = await cc2plates.source()
    if src:
        await cc2plates.refresh(src)
    snap, snap_err = cc2plates.snapshot(src.host) if src else (None, "")
    return web.json_response({
        "ok": True,
        "stats": await db.calib_overview(),
        "runs": await db.list_calib_runs(),
        "passports": passports,
        "presets": await db.list_calib_preset_latest(),
        "presets_disk": await asyncio.to_thread(calibration.list_presets, None, True),
        "samples": samples,
        "journal": loose,
        "tests": _calib_test_dicts(),
        "diagnostics": [dict(d, tests=list(d["tests"])) for d in calibration.DIAGNOSTICS],
        "verdict_kinds": [dict(v, fields=[dict(f) for f in v["fields"]])
                          for v in calibration.VERDICT_KINDS],
        "passport_fields": [{"key": k, "label": calibration.PASSPORT_LABELS.get(k, (k, ""))[0],
                             "unit": calibration.PASSPORT_LABELS.get(k, (k, ""))[1],
                             "scope": v[0], "param": v[1]}
                            for k, v in calibration.PASSPORT_FIELDS.items()],
        "plastics": [{"id": p.id, "name": p.name, "material_type": p.material_type}
                     for p in await db.list_plastics(active_only=False)],
        "printers": [{"id": p.id, "name": p.name} for p in await db.list_printers()],
        # Сопла из реестра принтеров (f71f4973): кампания и паспорт держат сопло строкой,
        # и выбор из своего списка надёжнее, чем «как я его записал в прошлый раз».
        "nozzles": await db.known_nozzles(),
        "plates": cc2plates.decorate(await db.list_calib_plates(), snap),
        "plates_cc2": cc2plates.view(src, snap, snap_err),
    })


@routes.get("/api/admin/calib/run")
async def _api_admin_calib_run(request: web.Request) -> web.Response:
    """Одна кампания целиком: шаги, привязанные печати, замеры, образцы."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    rid = _toint(request.query.get("id"))
    run = await db.get_calib_run(rid)
    if not run:
        return web.json_response({"ok": False, "error": "Кампания не найдена"}, status=404)
    steps = [_calib_step_view(s) for s in await db.list_calib_steps(rid)]
    jmap: dict[int, dict] = {}
    for e in await db.list_calib_journal(run_id=rid, calib_only=False, limit=200):
        jmap[e["id"]] = await _calib_journal_view(e)
    for s in steps:
        if s.get("journal_id") and s["journal_id"] not in jmap:
            e = await db.get_print_journal(s["journal_id"])
            if e:
                jmap[e["id"]] = await _calib_journal_view(e)
        s["print"] = jmap.get(s.get("journal_id") or 0)
        s["measures"] = await db.list_calib_measures(step_id=s["id"])
    return web.json_response({"ok": True, "run": run, "steps": steps,
                              "journal": list(jmap.values()),
                              "measures": await db.list_calib_measures(run_id=rid),
                              "samples": await db.list_calib_samples(run_id=rid)})


@routes.post("/api/admin/calib/run_save")
async def _api_admin_calib_run_save(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    title = (body.get("title") or "").strip()
    if not title:
        return web.json_response({"ok": False, "error": "Как назовём кампанию?"})
    plastic = await db.get_plastic(_toint(body.get("plastic_id"))) if body.get("plastic_id") else None
    fields = {"title": title[:120], "reason": (body.get("reason") or "").strip()[:200],
              "printer_id": _toint(body.get("printer_id")) or None,
              "nozzle": (body.get("nozzle") or "0.4").strip()[:12],
              "plastic_id": plastic.id if plastic else None,
              "plastic_name": plastic.name if plastic else (body.get("plastic_name") or ""),
              "spool_id": _toint(body.get("spool_id")) or None,
              "note": (body.get("note") or "").strip()}
    if body.get("status"):
        fields["status"] = str(body["status"])[:20]
    if rid:
        if not await db.update_calib_run(rid, **fields):
            return web.json_response({"ok": False, "error": "Кампания не найдена"}, status=404)
        if body.get("steps"):
            await db.seed_calib_steps(rid, [str(x) for x in body["steps"]])
        return web.json_response({"ok": True, "id": rid})
    steps = [str(x) for x in (body.get("steps") or [])] or None
    fields.pop("status", None)
    new_id = await db.add_calib_run(steps=steps, **fields)
    # Свежие калибровочные печати, ещё не привязанные ни к одной кампании, подтягиваем в новую:
    # кампанию обычно заводят ПОСЛЕ того, как пара тестов уже напечатана. Окно ограничено
    # неделей — иначе в новую кампанию уехали бы тесты полугодовой давности по другой катушке.
    picked = 0
    if _toint(body.get("adopt")):
        since = int(time.time()) - int(_num(body, "adopt_days", 7) or 7) * 86400
        for e in await db.list_calib_journal(calib_only=True, limit=200):
            if e.get("calib_run_id") or e["created_at"] < since:
                continue
            if plastic and e.get("plastic_id") and e["plastic_id"] != plastic.id:
                continue
            if await db.attach_journal_to_calib(e["id"]):
                picked += 1
    return web.json_response({"ok": True, "id": new_id, "adopted": picked})


@routes.post("/api/admin/calib/run_delete")
async def _api_admin_calib_run_delete(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    await db.delete_calib_run(_toint(body.get("id")))
    return web.json_response({"ok": True})


@routes.post("/api/admin/calib/step_save")
async def _api_admin_calib_step_save(request: web.Request) -> web.Response:
    """Правка шага: результат, вывод, статус, привязанная печать."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    sid = _toint(body.get("id"))
    step = await db.get_calib_step(sid)
    if not step:
        return web.json_response({"ok": False, "error": "Шаг не найден"}, status=404)
    fields: dict = {}
    for k in ("status", "verdict", "note", "unit"):
        if k in body:
            fields[k] = str(body.get(k) or "")[:1000]
    for k in ("value", "range_from", "range_to"):
        if k in body:
            fields[k] = _num(body, k, None)
    if isinstance(body.get("values"), dict):     # тест с парой параметров (глажка)
        fields["values"] = body["values"]
    if "journal_id" in body:
        jid = _toint(body.get("journal_id")) or None
        fields["journal_id"] = jid
        if jid:
            await db.set_journal_calib(jid, run_id=step["run_id"], step_id=sid,
                                       calib_type=step["test_key"])
    await db.update_calib_step(sid, **fields)
    return web.json_response({"ok": True, "step": _calib_step_view(await db.get_calib_step(sid))})


def _calib_preset_path(scope: str, presets_map: dict | None, override: str = "") -> str:
    """Файл пресета, в который ложится параметр шага: явный путь → путь из паспорта."""
    if override:
        return override
    return str((presets_map or {}).get(scope) or "")


@routes.post("/api/admin/calib/step_apply")
async def _api_admin_calib_step_apply(request: web.Request) -> web.Response:
    """Записать результат шага в пресет слайсера и сразу снять снимок.

    Единственное место, где бот ПИШЕТ в пресеты слайсера. Значение и файл — из шага и паспорта,
    поэтому «применить» не может уехать не в тот пресет молча: при отсутствии пути отвечаем
    ошибкой, а не пишем наугад.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    step = await db.get_calib_step(_toint(body.get("id")))
    if not step:
        return web.json_response({"ok": False, "error": "Шаг не найден"}, status=404)
    t = calibration.test_info(step["test_key"])
    if not t or not t.params:
        return web.json_response({"ok": False, "error": "Этот тест не пишет значение в пресет"})
    # Значения всех параметров теста: тело запроса → сохранённые в шаге → основное value.
    sent = body.get("values") if isinstance(body.get("values"), dict) else {}
    saved = step.get("values") or {}
    vals: dict[str, float] = {}
    for p in t.params:
        v = sent.get(p.key, saved.get(p.key))
        if v in (None, "") and p.key == t.param:
            v = _num(body, "value", step.get("value"))
        try:
            if v not in (None, ""):
                vals[p.key] = float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            continue
    if not vals:
        return web.json_response({"ok": False, "error": "Нет значения для записи"})
    run = await db.get_calib_run(step["run_id"]) or {}
    passport = None
    if run.get("plastic_id"):
        passport = await db.get_calib_passport(run["plastic_id"], run.get("printer_id"),
                                               run.get("nozzle") or "0.4")
    written, missing = [], []
    for p in t.params:
        if p.key not in vals:
            continue
        path = _calib_preset_path(p.scope, (passport or {}).get("presets"),
                                  str(body.get("path") or "") if p.scope == t.scope else "")
        if not path:
            missing.append(p.scope)
            continue
        if await asyncio.to_thread(calibration.set_preset_value, path, p.key, vals[p.key]):
            written.append({"param": p.key, "value": vals[p.key], "path": path})
    if not written:
        kinds = ", ".join(sorted(set(missing))) or t.scope
        return web.json_response({"ok": False, "error":
                                  f"Не указан пресет ({kinds}) — заполните паспорт филамента"})
    value = vals.get(t.param)
    await db.update_calib_step(step["id"], value=value, values=vals, applied=1,
                               applied_at=int(time.time()), status="done")
    await db.snapshot_calib_presets(await asyncio.to_thread(calibration.snapshot_user_presets))
    if passport:
        await _calib_refresh_passport(passport)
    return web.json_response({"ok": True, "param": t.param, "value": value,
                              "written": written, "path": written[0]["path"]})


async def _calib_refresh_passport(passport: dict) -> dict:
    """Перечитать значения паспорта из пресетов (после записи параметра)."""
    fresh = await asyncio.to_thread(calibration.passport_from_presets,
                                    passport.get("presets") or {})
    await db.save_calib_passport(
        plastic_id=passport["plastic_id"], printer_id=passport.get("printer_id"),
        nozzle=passport.get("nozzle") or "0.4", values=fresh.get("values"),
        source=fresh.get("source"), presets_map=passport.get("presets") or {},
        status=passport.get("status") or "calibrated", run_id=passport.get("run_id"),
        spool_id=passport.get("spool_id"), note=passport.get("note") or "",
        calibrated_at=passport.get("calibrated_at"))
    return fresh


@routes.post("/api/admin/calib/measure_add")
async def _api_admin_calib_measure_add(request: web.Request) -> web.Response:
    """Замер штангенциркулем → вердикт правила (считается и сохраняется вместе с числами)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    kind = (body.get("kind") or "").strip()
    if not any(k["key"] == kind for k in calibration.VERDICT_KINDS):
        return web.json_response({"ok": False, "error": "Неизвестный вид замера"})
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return web.json_response({"ok": False, "error": "Некорректные данные замера"})
    clean = {}
    for k, v in data.items():
        try:
            clean[str(k)[:24]] = float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            continue
    res = await db.add_calib_measure(kind=kind, data=clean, run_id=_toint(body.get("run_id")) or None,
                                     step_id=_toint(body.get("step_id")) or None,
                                     label=(body.get("label") or ""))
    # вердикт с готовым значением сразу кладём в шаг: иначе мастер переписывает число руками
    sid = _toint(body.get("step_id"))
    if sid and res.get("value") is not None and _toint(body.get("to_step"), 1):
        await db.update_calib_step(sid, value=res["value"], verdict=res.get("verdict") or "",
                                   status="measured")
    return web.json_response({"ok": True, "result": res})


@routes.post("/api/admin/calib/measure_delete")
async def _api_admin_calib_measure_delete(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    await db.delete_calib_measure(_toint(body.get("id")))
    return web.json_response({"ok": True})


@routes.post("/api/admin/calib/sample_save")
async def _api_admin_calib_sample_save(request: web.Request) -> web.Response:
    """Карточка образца с полки."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    sid = await db.save_calib_sample(
        sid=_toint(body.get("id")) or None, run_id=_toint(body.get("run_id")) or None,
        step_id=_toint(body.get("step_id")) or None,
        journal_id=_toint(body.get("journal_id")) or None,
        code=(body.get("code") or ""), title=(body.get("title") or ""),
        proves=(body.get("proves") or ""), value=(body.get("value") or ""),
        place=(body.get("place") or ""), note=(body.get("note") or ""))
    # фото образца можно не снимать заново — берём из привязанной печати журнала
    if _toint(body.get("copy_photo")) and _toint(body.get("journal_id")):
        e = await db.get_print_journal(_toint(body.get("journal_id")))
        if e and e.get("done_photo") and Path(e["done_photo"]).exists():
            await db.set_calib_sample_photo(sid, e["done_photo"])
    s = await db.get_calib_sample(sid)
    return web.json_response({"ok": True, "id": sid, "url": _sample_url(sid),
                              "photo": _photo_url("calib", sid, (s or {}).get("photo")) or ""})


@routes.post("/api/admin/calib/sample_delete")
async def _api_admin_calib_sample_delete(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    await db.delete_calib_sample(_toint(body.get("id")))
    return web.json_response({"ok": True})


@routes.get("/api/admin/calib/presets")
async def _api_admin_calib_presets(request: web.Request) -> web.Response:
    """Пресеты слайсера: что лежит на диске, что в снимках, чего уже нет."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    disk = await asyncio.to_thread(calibration.list_presets, None, True)
    snaps = {s["path"]: s for s in await db.list_calib_preset_latest()}
    items = []
    for d in disk:
        s = snaps.pop(d["path"], None)
        items.append({**d, "snapped": bool(s), "versions": (s or {}).get("versions") or 0,
                      "snap_at": (s or {}).get("at") or 0,
                      "changed": bool(s and s.get("mtime") != d.get("mtime")),
                      "snap_id": (s or {}).get("id") or 0})
    for path, s in snaps.items():          # в снимках есть, на диске нет — пресет пропал
        items.append({"kind": s.get("kind") or "", "name": s.get("name") or "", "path": path,
                      "user": 1, "mtime": 0, "snapped": True, "gone": 1,
                      "versions": s.get("versions") or 0, "snap_at": s.get("at") or 0,
                      "snap_id": s.get("id") or 0})
    return web.json_response({"ok": True, "items": items,
                              "user_dir": str(calibration.user_root())})


@routes.post("/api/admin/calib/presets_snapshot")
async def _api_admin_calib_presets_snapshot(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    items = await asyncio.to_thread(calibration.snapshot_user_presets)
    return web.json_response({"ok": True, **await db.snapshot_calib_presets(items),
                              "files": len(items)})


@routes.get("/api/admin/calib/preset_history")
async def _api_admin_calib_preset_history(request: web.Request) -> web.Response:
    """История правок пресета: снимки + дифф по ключам между соседними версиями."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    path = request.query.get("path") or ""
    hist = await db.list_calib_preset_history(path)
    out = []
    for i, h in enumerate(hist):
        prev = hist[i + 1] if i + 1 < len(hist) else None
        out.append({"id": h["id"], "at": h["at"], "sha": h["sha"], "gone": h["gone"],
                    "mtime": h["mtime"], "body": h["body"],
                    "diff": calibration.diff_presets((prev or {}).get("body") or "",
                                                     h["body"]) if h["body"] else []})
    return web.json_response({"ok": True, "items": out, "path": path})


@routes.post("/api/admin/calib/preset_restore")
async def _api_admin_calib_preset_restore(request: web.Request) -> web.Response:
    """Вернуть пресет из снимка — лекарство от «обновил слайсер, всё слетело к стоку»."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    snap = await db.get_calib_preset_snapshot(_toint(body.get("id")))
    if not snap or not snap.get("body"):
        return web.json_response({"ok": False, "error": "Снимок не найден"}, status=404)
    p = Path(snap["path"])
    try:
        await asyncio.to_thread(p.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_text, snap["body"], encoding="utf-8")
    except OSError:
        log.warning("preset restore failed", exc_info=True)
        return web.json_response({"ok": False, "error": "Не удалось записать файл"})
    await db.snapshot_calib_presets(await asyncio.to_thread(calibration.snapshot_user_presets))
    return web.json_response({"ok": True, "path": str(p)})


@routes.post("/api/admin/calib/passport_save")
async def _api_admin_calib_passport_save(request: web.Request) -> web.Response:
    """Паспорт филамента: собрать значения из выбранных пресетов (или принять ручные)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    plastic = await db.get_plastic(_toint(body.get("plastic_id")))
    if not plastic:
        return web.json_response({"ok": False, "error": "Пластик не найден"}, status=404)
    presets_map = {k: str(v) for k, v in (body.get("presets") or {}).items()
                   if k in calibration.PRESET_KINDS and v}
    values = body.get("values") if isinstance(body.get("values"), dict) else None
    source = {}
    if presets_map:
        built = await asyncio.to_thread(calibration.passport_from_presets, presets_map)
        source = built.get("source") or {}
        if values is None:
            values = built.get("values") or {}
    clean: dict = {}
    for k, v in (values or {}).items():
        if k not in calibration.PASSPORT_FIELDS:
            continue
        try:
            clean[k] = float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            continue
    pid = await db.save_calib_passport(
        plastic_id=plastic.id, printer_id=_toint(body.get("printer_id")) or None,
        nozzle=(body.get("nozzle") or "0.4").strip()[:12], values=clean, source=source,
        presets_map=presets_map, status=(body.get("status") or "calibrated"),
        run_id=_toint(body.get("run_id")) or None, spool_id=_toint(body.get("spool_id")) or None,
        note=(body.get("note") or ""),
        # Пластина и её вылет для этого филамента (1028ab67): пустой Z-offset — законное
        # «на этой пластине ещё не мерил», поэтому не подменяем его нулём.
        plate_id=_toint(body.get("plate_id")) or None, z_offset=_fnum(body.get("z_offset")),
        calibrated_at=_toint(body.get("calibrated_at")) or None)
    # карточка пластика знает свой откалиброванный профиль (раньше поле пустовало у 5 из 7)
    if presets_map.get("filament"):
        await db.update_plastic(plastic.id, filament_profile=presets_map["filament"])
    if presets_map:
        await db.snapshot_calib_presets(await asyncio.to_thread(calibration.snapshot_user_presets))
    return web.json_response({"ok": True, "id": pid, "values": clean, "source": source})


@routes.post("/api/admin/calib/passport_apply")
async def _api_admin_calib_passport_apply(request: web.Request) -> web.Response:
    """Записать значения паспорта обратно в пресеты слайсера (восстановление после сброса)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    p = await db.get_calib_passport_by_id(_toint(body.get("id")))
    if not p:
        return web.json_response({"ok": False, "error": "Паспорт не найден"}, status=404)
    presets_map = p.get("presets") or {}
    only = set(body.get("fields") or []) or None
    done, skipped = [], []
    for fkey, val in (p.get("values") or {}).items():
        if only and fkey not in only:
            continue
        scope, param = calibration.PASSPORT_FIELDS.get(fkey, ("", ""))
        path = presets_map.get(scope)
        if not path or not param:
            skipped.append(fkey)
            continue
        if await asyncio.to_thread(calibration.set_preset_value, path, param, val):
            done.append(fkey)
        else:
            skipped.append(fkey)
    if done:
        await db.snapshot_calib_presets(await asyncio.to_thread(calibration.snapshot_user_presets))
    return web.json_response({"ok": True, "applied": done, "skipped": skipped})


@routes.post("/api/admin/calib/passport_delete")
async def _api_admin_calib_passport_delete(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    await db.delete_calib_passport(_toint(body.get("id")))
    return web.json_response({"ok": True})


@routes.post("/api/admin/calib/plate_save")
async def _api_admin_calib_plate_save(request: web.Request) -> web.Response:
    """Пластина стола и её Z-offset (OpenTodo 1028ab67).

    Z-offset хранится В ПРИНТЕРЕ (в слайсере он всегда 0), поэтому до этой задачи его негде
    было записать: значение жило в голове мастера и терялось при смене пластины."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = (body.get("action") or "save").strip()
    pid = _toint(body.get("id"))
    if action == "cc2_link":
        # Разовая связь «наша пластина → пластина CC2 Control» по карте мастера.
        errors = await cc2plates.link(body.get("links"))
        return web.json_response({"ok": not errors, "errors": errors,
                                  **({"error": "; ".join(errors)} if errors else {})})
    # Пластину с CC2 Control ведёт принтер (app/cc2plates.py): монтаж и удаление — в CC2
    # Control (монтаж может перезагрузить принтер, удаление стирает снятую сетку), имя и Z
    # отсюда уходят на принтер, тип покрытия и заметка — наши.
    cid = await db.calib_plate_cc2(pid) if pid else ""
    if action == "delete":
        if cid:
            return web.json_response({"ok": False, "error": (
                "Пластину ведёт принтер: удалить её вместе со снятой сеткой можно в CC2 Control")})
        await db.delete_calib_plate(pid)
        return web.json_response({"ok": True})
    if action == "default":
        if cid:
            return web.json_response({"ok": False, "error": (
                "Пластину на стол ставит CC2 Control: монтаж там — он может перезагрузить принтер")})
        mounted = await cc2plates.mounted_elsewhere(pid)
        if mounted:
            return web.json_response({"ok": False, "error": (
                f"На принтере смонтирована «{mounted}» — смените пластину в CC2 Control")})
        await db.set_default_calib_plate(pid)
        return web.json_response({"ok": True})
    if cid:
        try:
            res = await cc2plates.save_led(pid, name=body.get("name") or "",
                                           z=_fnum(body.get("z_offset")),
                                           kind=body.get("kind") or "", note=body.get("note") or "")
        except cc2plates.PlateError as e:
            return web.json_response({"ok": False, "error": str(e)})
        return web.json_response({"ok": True, "id": pid, "cc2": True, "applied": res["applied"]})
    try:
        new_id = await db.save_calib_plate(
            plate_id=pid or None, printer_id=_toint(body.get("printer_id")) or None,
            name=(body.get("name") or ""), kind=(body.get("kind") or ""),
            z_offset=_fnum(body.get("z_offset")), note=(body.get("note") or ""))
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)})
    return web.json_response({"ok": True, "id": new_id})


@routes.post("/api/admin/calib/photo")
async def _api_admin_calib_photo(request: web.Request) -> web.Response:
    """Подсказка по фото башни: где рябь. Не заменяет замер — только показывает, где мерить."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    path = ""
    if body.get("journal_id"):
        e = await db.get_print_journal(_toint(body.get("journal_id")))
        path = (e or {}).get("done_photo") or ""
    elif body.get("sample_id"):
        s = await db.get_calib_sample(_toint(body.get("sample_id")))
        path = (s or {}).get("photo") or ""
    if not path:
        return web.json_response({"ok": False, "error": "У печати нет фото"})
    res = await asyncio.to_thread(
        calibration.analyze_tower, path, _toint(body.get("bands"), 20),
        _num(body, "base", None), _num(body, "step", None), _num(body, "height_mm", None))
    if not res.get("ok"):
        return web.json_response({"ok": False, "error": res.get("error") or "Не удалось разобрать"})
    return web.json_response({"ok": True, **res})


@routes.get("/api/admin/calib/drift")
async def _api_admin_calib_drift(request: web.Request) -> web.Response:
    """Регресс-эталон: как уползают замеры куба и как расходятся оценка/факт по времени.

    Первое — прямая проверка «не поехала ли механика», второе — бесплатный побочный сигнал:
    журнал печатей и так хранит пару est_minutes/minutes на каждую печать.
    """
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    cubes = await db.list_calib_measures(kind="cube_xy", limit=200)
    series_x, series_y = [], []
    for m in reversed(cubes):
        try:
            data = json.loads(m.get("data") or "{}")
        except (TypeError, ValueError):
            continue
        if "x" in data:
            series_x.append({"at": m["at"], "value": data["x"]})
        if "y" in data:
            series_y.append({"at": m["at"], "value": data["y"]})
    rows = await db.list_print_journal(limit=200)
    est = [{"at": r["created_at"], "label": r["label"], "est": r.get("est_minutes"),
            "fact": r.get("minutes"),
            "dev": round((r["minutes"] - r["est_minutes"]) / r["est_minutes"] * 100, 1)
            if r.get("est_minutes") and r.get("minutes") else None}
           for r in rows if r.get("est_minutes") and r.get("minutes")]
    devs = [e["dev"] for e in est if e["dev"] is not None]
    return web.json_response({"ok": True,
                              "cube_x": series_x, "cube_y": series_y,
                              "cube_x_stat": calibration.drift_summary(series_x),
                              "cube_y_stat": calibration.drift_summary(series_y),
                              "time_dev": est[:40],
                              "time_dev_stat": calibration.drift_summary(
                                  [{"value": d} for d in devs])})


@routes.post("/api/admin/calib/backfill")
async def _api_admin_calib_backfill(request: web.Request) -> web.Response:
    """Пересканировать журнал печатей и проставить типы калибровочных тестов."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    res = await db.backfill_calib_types()
    if _toint(body.get("attach")):
        for e in await db.list_calib_journal(calib_only=True, limit=300):
            if not e.get("calib_run_id"):
                await db.attach_journal_to_calib(e["id"])
    return web.json_response({"ok": True, **res})


@routes.post("/api/admin/calib/link")
async def _api_admin_calib_link(request: web.Request) -> web.Response:
    """Привязать печать журнала к шагу кампании вручную (когда имя файла не распозналось)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    jid = _toint(body.get("journal_id"))
    if not await db.get_print_journal(jid):
        return web.json_response({"ok": False, "error": "Печать не найдена"}, status=404)
    sid = _toint(body.get("step_id"))
    if sid:
        step = await db.get_calib_step(sid)
        if not step:
            return web.json_response({"ok": False, "error": "Шаг не найден"}, status=404)
        await db.update_calib_step(sid, journal_id=jid,
                                   status="measured" if step["status"] == "todo" else step["status"])
        await db.set_journal_calib(jid, run_id=step["run_id"], step_id=sid,
                                   calib_type=step["test_key"])
        return web.json_response({"ok": True})
    if "calib_type" in body:
        await db.set_journal_calib(jid, calib_type=str(body.get("calib_type") or ""))
    return web.json_response({"ok": True, **(await db.attach_journal_to_calib(jid))})


@routes.get("/api/admin/calib/export")
async def _api_admin_calib_export(request: web.Request) -> web.StreamResponse:
    """PDF-паспорт калибровки (значения + чем получены + образцы + правила диагностики)."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    pid = _toint(request.query.get("id"))
    p = await db.get_calib_passport_by_id(pid)
    if not p:
        return web.json_response({"ok": False, "error": "Паспорт не найден"}, status=404)
    plastic = await db.get_plastic(p["plastic_id"])
    printer = None
    for pr in await db.list_printers():
        if pr.id == p.get("printer_id"):
            printer = pr
    p["plastic"] = plastic.name if plastic else ""
    p["printer"] = printer.name if printer else ""
    # Пластина и её базовый вылет — в паспорте по id, а в PDF нужны именем и числом (1028ab67).
    if p.get("plate_id"):
        plate = await db.get_calib_plate(p["plate_id"])
        if plate:
            p["plate"], p["plate_z"] = plate.get("name") or "", plate.get("z_offset")
    run = await db.get_calib_run(p["run_id"]) if p.get("run_id") else None
    steps = await db.list_calib_steps(run["id"]) if run else []
    measures = await db.list_calib_measures(run_id=run["id"]) if run else []
    samples = await db.list_calib_samples(run_id=run["id"]) if run else []
    shop = (await db.get_text_settings()).get("shop_name") or ""
    out = calibpdf.out_dir() / f"passport_{pid}.pdf"
    await asyncio.to_thread(calibpdf.passport_pdf, out, passport=p, run=run, steps=steps,
                            measures=measures, samples=samples, shop=shop)
    return web.FileResponse(out, headers={
        "Content-Disposition": f'inline; filename="calibration_{pid}.pdf"'})


@routes.get("/api/admin/calib/labels")
async def _api_admin_calib_labels(request: web.Request) -> web.StreamResponse:
    """PDF с этикетками (QR + шифр) на коробку с образцами."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    run_id = _toint(request.query.get("run")) or None
    items = await db.list_calib_samples(run_id=run_id)
    only = {_toint(x) for x in (request.query.get("ids") or "").split(",") if x.strip()}
    if only:
        items = [x for x in items if x["id"] in only]
    base = (config.WEBAPP_PUBLIC_URL or "").rstrip("/")
    for x in items:
        x["sig"] = sig("csample", x["id"])
        x["url"] = f"{base}{signed_url('csample', x['id'])}" if base else ""
    out = calibpdf.out_dir() / f"labels_{run_id or 'all'}.pdf"
    await asyncio.to_thread(calibpdf.labels_pdf, out, items, base_url=base)
    return web.FileResponse(out, headers={
        "Content-Disposition": 'inline; filename="calibration_labels.pdf"'})


@routes.get("/csample/{sid}")
async def _csample_page(request: web.Request) -> web.Response:
    """Страница образца по QR с этикетки: подписанная ссылка, читается с телефона без логина.

    Только чтение и только про этот образец — ни списка, ни правки: этикетка может уехать
    вместе с деталью, поэтому ссылка не должна открывать доступ ни к чему ещё.
    """
    sid = _toint(request.match_info.get("sid"))
    if not check_sig(request, "csample", sid):
        return web.Response(status=403, text="forbidden")
    s = await db.get_calib_sample(sid)
    if not s:
        return web.Response(status=404, text="not found")
    run = await db.get_calib_run(s["run_id"]) if s.get("run_id") else None
    step = await db.get_calib_step(s["step_id"]) if s.get("step_id") else None
    t = calibration.test_info((step or {}).get("test_key") or "")
    when = datetime.fromtimestamp(s["created_at"]).strftime("%d.%m.%Y") if s.get("created_at") else ""
    rows = [("Шифр", s.get("code")), ("Что доказывает", s.get("proves")),
            ("Значение", s.get("value")), ("Тест", t.label if t else ""),
            ("Кампания", (run or {}).get("title")), ("Филамент", (run or {}).get("plastic_name")),
            ("Где лежит", s.get("place")), ("Дата", when), ("Заметка", s.get("note"))]
    body = "".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
                   for k, v in rows if v)
    photo = ""
    if s.get("photo") and Path(s["photo"]).exists():
        photo = (f'<img src="/admin/photo/calib/{sid}?s={sig("aphoto", "calib" + str(sid))}'
                 f'&v={_file_ver(s["photo"])}" alt="">')
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Образец {html.escape(str(s.get('code') or sid))}</title><style>
:root{{color-scheme:dark light}}
body{{margin:0;padding:20px;background:#10151b;color:#e7edf3;
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
.card{{max-width:520px;margin:0 auto;background:#171e27;border:1px solid #232d3a;
border-radius:16px;padding:18px}}
h1{{margin:0 0 4px;font-size:20px}} .sub{{color:#8fa0b3;font-size:13px;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:7px 0;
border-bottom:1px solid #232d3a;vertical-align:top}}
th{{color:#8fa0b3;font-weight:400;width:42%}} img{{max-width:100%;border-radius:12px;margin-top:14px}}
</style></head><body><div class="card">
<h1>{html.escape(str(s.get('title') or 'Образец'))}</h1>
<div class="sub">Калибровочный образец · {html.escape(str(s.get('code') or ''))}</div>
<table>{body}</table>{photo}</div></body></html>"""
    return web.Response(text=page, content_type="text/html")
