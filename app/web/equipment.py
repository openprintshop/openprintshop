"""Оборудование мастерской: карточки принтеров и пресеты печати (качество + прочность).

Здесь только транспорт админки: собрать экрану список машин, сопел и профилей и применить
действие мастера. Доменные правила живут в источниках — реестр сопел в app/nozzles.py,
протоколы и способности в app/printerdrv/, движки резки в app/slicers.py, хранение в db.

Выделено из app/webapp.py волной «Оборудование» цикла 2. Пресеты приехали ВМЕСТЕ с принтерами
не «за компанию»: пресет качества держит process-профиль и сопло, читает db.list_printers и
db.known_nozzles, а карточка принтера при заведении сама заводит запись в реестре сопел через
nozzles.from_machine_profile — это один предмет, парк машин и то, чем на нём режут.

Тела функций перенесены ДОСЛОВНО, без единой правки. В комментариях внутри записаны причины,
которые дороже кода: сопло разбирается из имени процесса, пустой kind/slicer — осмысленный
выбор «как раньше» (поэтому реакция на НАЛИЧИЕ ключа, а не на непустое значение), пустой
access_code НЕ затирает сохранённый, карточка принтера заводится ВЫКЛЮЧЕННОЙ. app/webapp.py
реэкспортирует все четыре имени фасадом, поэтому и код, и тесты находят их как webapp.*.

МАРШРУТЫ ЖИВУТ ЗДЕСЬ ЖЕ, в таблице `routes` над обработчиками, а make_app подключает их одной
строкой `app.add_routes(equipment.routes)`. Пути не изменились ни на символ: канонический путь
ресурса тот же, поэтому `_caps_mw` (CAP_PRINTER для /api/admin/printers из _OPERATOR_ROUTES,
остальные три — CAP_SETTINGS по умолчанию закрытости; строки задаются в app/webguard.py),
`_usage_mw` и `_error_mw` видят их как раньше.

Модуль НЕ импортирует webapp — иначе получился бы цикл, ради разрыва которого разрез и
затевался: помощники берутся напрямую из app/webauth.py и app/web/common.py, то есть из
источника, а не через фасад.
"""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .. import db, nozzles, printerdrv, slicers
from ..profilepaths import abs_profile_str
from ..webauth import _admin_body, _admin_forbidden, _verify_admin
from .common import _bad, _num, _toint

# Маршруты блока объявляются рядом с обработчиком, а не списком в make_app: при переносе
# блока в отдельный модуль строка регистрации уезжает вместе с ручкой и потеряться не может.
# Канонический путь ресурса тот же, поэтому _caps_mw/_usage_mw/_error_mw читают его как раньше.
routes = web.RouteTableDef()


@routes.get("/api/admin/presets")
async def _api_admin_presets(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    quals = await db.list_quality(active_only=False)
    strs = await db.list_strength(active_only=False)
    printers = await db.list_printers(active_only=True)
    procs: list = []
    if printers:
        try:
            pd = Path(printers[0].process_dir)
            if pd.exists():
                procs = sorted(f.name for f in pd.glob("*.json"))[:80]
        except Exception:  # noqa: BLE001
            procs = []
    return web.json_response({"ok": True, "processes": procs,
        # nozzles — какие сопла вообще заведены: подсказка выпадающему списку в форме пресета.
        "nozzles": await db.known_nozzles(),
        "quality": [{"id": q.id, "label": q.label, "layer": q.layer_height, "process": q.process,
                     "nozzle": db.nozzle_of(q), "nozzle_set": bool(q.nozzle),
                     "is_active": bool(q.is_active), "is_default": bool(q.is_default)} for q in quals],
        "strength": [{"id": s.id, "label": s.label, "infill": s.infill, "walls": s.walls,
                      "is_active": bool(s.is_active), "is_default": bool(s.is_default)} for s in strs]})


@routes.post("/api/admin/preset")
async def _api_admin_preset(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    kind = body.get("kind", "")
    action = body.get("action", "")
    pid = _toint(body.get("id"))
    if kind == "quality":
        if action == "add":
            label = (body.get("label") or "").strip()
            layer = _num(body, "layer")
            process = (body.get("process") or "").strip()
            if not label or layer is None or not process:
                return web.json_response({"ok": False, "error": "Нужны название, слой и профиль процесса"})
            # Сопло: явное поле формы, иначе разберём из имени процесса (f71f4973).
            await db.add_quality(label, layer, process, nozzle=(body.get("nozzle") or "").strip())
        elif action == "edit":
            cur = next((x for x in await db.list_quality(active_only=False) if x.id == pid), None)
            if not cur:
                return web.json_response({"ok": False, "error": "Пресет не найден"})
            label = (body.get("label") or "").strip()
            layer = _num(body, "layer")
            process = (body.get("process") or "").strip()
            if not label or layer is None or not process:
                return web.json_response({"ok": False, "error": "Нужны название, слой и профиль процесса"})
            fields = {"label": label, "layer_height": layer, "process": process}
            if "nozzle" in body:
                # Пустая строка = «разбирать из имени процесса», это осмысленное значение,
                # поэтому реагируем на НАЛИЧИЕ ключа, а не на непустое значение.
                fields["nozzle"] = (body.get("nozzle") or "").strip()
            await db.update_quality(pid, **fields)
        elif action == "toggle":
            cur = next((x for x in await db.list_quality(active_only=False) if x.id == pid), None)
            if cur:
                await db.update_quality(pid, is_active=0 if cur.is_active else 1)
        elif action == "default":
            await db.set_default_quality(pid)
        elif action == "delete":
            await db.delete_quality(pid)
        else:
            return web.json_response({"ok": False, "error": "Неизвестное действие"})
        return web.json_response({"ok": True})
    if kind == "strength":
        if action == "add":
            label = (body.get("label") or "").strip()
            if not label:
                return web.json_response({"ok": False, "error": "Введите название"})
            await db.add_strength(label, _toint(body.get("infill"), 15), _toint(body.get("walls"), 2))
        elif action == "edit":
            cur = next((x for x in await db.list_strength(active_only=False) if x.id == pid), None)
            if not cur:
                return web.json_response({"ok": False, "error": "Пресет не найден"})
            label = (body.get("label") or "").strip()
            if not label:
                return web.json_response({"ok": False, "error": "Введите название"})
            await db.update_strength(pid, label=label, infill=_toint(body.get("infill"), 15),
                                     walls=_toint(body.get("walls"), 2))
        elif action == "toggle":
            cur = next((x for x in await db.list_strength(active_only=False) if x.id == pid), None)
            if cur:
                await db.update_strength(pid, is_active=0 if cur.is_active else 1)
        elif action == "default":
            await db.set_default_strength(pid)
        elif action == "delete":
            await db.delete_strength(pid)
        else:
            return web.json_response({"ok": False, "error": "Неизвестное действие"})
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестный тип"})


_NO_STATS = {"samples": 0, "median": None, "suggested": None}


@routes.get("/api/admin/printers")
async def _api_admin_printers(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    ps = await db.list_printers(active_only=False)
    nz = await db.list_printer_nozzles()
    by_printer: dict[int, list] = {}
    for n in nz:
        by_printer.setdefault(n.printer_id, []).append(
            {"id": n.id, "nozzle": n.nozzle, "machine": n.machine_profile, "note": n.note,
             "is_default": bool(n.is_default), "exists": Path(n.machine_profile).is_file()})
    time_stats = {p.id: await db.printer_time_stats(p.id, p.time_factor) for p in ps}
    return web.json_response({"ok": True, "printers": [
        {"id": p.id, "name": p.name, "is_active": bool(p.is_active),
         "is_default": bool(p.is_default), "machine": p.machine_profile,
         "process": p.process_dir, "filament": p.filament_dir,
         # Сопла принтера (f71f4973): у каждого свой machine-профиль, основное — то, что
         # физически стоит в голове; им же подменяется printers.machine_profile.
         "nozzles": by_printer.get(p.id, []),
         # Протокол и способности (0613014e): чем разговариваем и что этот принтер умеет.
         "kind": p.kind, "address": p.address, "serial": p.serial,
         "has_code": bool(p.access_code),      # сам код доступа наружу не отдаём
         # Чем резать задания этой машины: у Bambu свои профили, и резать их ElegooSlicer'ом
         # значит считать клиенту цену по чужой машине.
         "slicer": p.slicer, "slicer_path": p.slicer_path,
         # Коэффициент времени машины (B1) и подсказка по журналу печатей ЭТОЙ машины:
         # медиана факт/оценка и предлагаемый множитель (samples=0 — журнал пуст).
         "time_factor": p.time_factor, "time_stats": time_stats.get(p.id, _NO_STATS),
         "driver": printerdrv.for_printer(p).describe()} for p in ps],
        "kinds": [{"key": k, "label": lbl} for k, lbl in printerdrv.KINDS],
        # Движки слайсера с отметкой «установлен»: список выбора не должен предлагать того,
        # чего на машине нет — иначе «слайсер не отвечает» выяснится ценой заказа.
        # Движки слайсера считаем С УЧЁТОМ путей из карточек: портативная сборка (движок
        # лежит рядом с проектом) иначе числилась бы неустановленной, хотя ею и режут.
        "slicers": slicers.describe({p.slicer or slicers.DEFAULT_KIND: p.slicer_path
                                     for p in ps if p.slicer_path})})


@routes.post("/api/admin/printer")
async def _api_admin_printer(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    pid = _toint(body.get("id"))
    if action == "add":
        name = (body.get("name") or "").strip()
        # Порядок обязателен: сначала разворачиваем в абсолютный путь (форма могла вернуть
        # то, что мы сами ей и отдали), потом проверяем существование, и только потом запись —
        # db.add_printer сама свернёт путь в маркер корня.
        machine = abs_profile_str((body.get("machine") or "").strip().strip('"'))
        process = abs_profile_str((body.get("process") or "").strip().strip('"'))
        filament = abs_profile_str((body.get("filament") or "").strip().strip('"'))
        if not name:
            return web.json_response({"ok": False, "error": "Введите название принтера"})
        if not Path(machine).is_file():
            return web.json_response({"ok": False, "error": "Профиль принтера (machine .json) не найден"})
        if not Path(process).is_dir():
            return web.json_response({"ok": False, "error": "Папка профилей процесса не найдена"})
        if not Path(filament).is_dir():
            return web.json_response({"ok": False, "error": "Папка профилей филамента не найдена"})
        # Карточка заводится ВЫКЛЮЧЕННОЙ: протокол связи, адрес и код доступа выставляются
        # отдельным действием, и до этого момента активная карточка ломает клиентский поток
        # (лишний шаг выбора принтера) и селфчек (жалобы на отсутствующие слайс-профили).
        new_id = await db.add_printer(name, machine, process, filament)
        # Сопло принтера заводим сразу: диаметр читаем из самого профиля (nozzle_diameter),
        # иначе из имени — иначе принтер остался бы без записи в реестре сопел.
        try:
            await db.add_printer_nozzle(
                new_id, nozzles.from_machine_profile(machine) or nozzles.DEFAULT, machine)
        except ValueError:
            pass
        return web.json_response({"ok": True, "id": new_id,
                                  "note": "Карточка заведена выключенной — настройте связь "
                                          "и включите принтер переключателем"})
    # ── сопла принтера (f71f4973) ───────────────────────────────────────────────────────
    if action.startswith("nozzle_"):
        nid = _toint(body.get("nozzle_id"))
        if action == "nozzle_add":
            machine = abs_profile_str((body.get("machine") or "").strip().strip('"'))
            if not Path(machine).is_file():
                return web.json_response({"ok": False, "error": "Профиль принтера (machine .json) не найден"})
            # Диаметр не указан — читаем из самого профиля: обычно это надёжнее ручного ввода.
            want = (body.get("nozzle") or "").strip() or nozzles.from_machine_profile(machine)
            if not nozzles.norm(want):
                return web.json_response({"ok": False, "error": "Укажите диаметр сопла (напр. 0.6)"})
            if not pid:
                return web.json_response({"ok": False, "error": "Не указан принтер"})
            try:
                await db.add_printer_nozzle(pid, want, machine, (body.get("note") or "").strip())
            except ValueError as e:
                return web.json_response({"ok": False, "error": str(e)})
            return web.json_response({"ok": True})
        if action == "nozzle_edit":
            fields: dict = {}
            if body.get("machine"):
                machine = abs_profile_str(str(body["machine"]).strip().strip('"'))
                if not Path(machine).is_file():
                    return web.json_response({"ok": False, "error": "Профиль принтера (machine .json) не найден"})
                fields["machine_profile"] = machine
            if body.get("nozzle"):
                fields["nozzle"] = str(body["nozzle"]).strip()
            if "note" in body:
                fields["note"] = str(body.get("note") or "").strip()
            try:
                await db.update_printer_nozzle(nid, **fields)
            except ValueError as e:
                return web.json_response({"ok": False, "error": str(e)})
            return web.json_response({"ok": True})
        if action == "nozzle_default":
            await db.set_default_printer_nozzle(nid)
            return web.json_response({"ok": True})
        if action == "nozzle_delete":
            try:
                await db.delete_printer_nozzle(nid)
            except ValueError as e:
                return web.json_response({"ok": False, "error": str(e)})
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "Неизвестное действие"})
    if action == "slicer_check":
        # Проверка движка ДО первого заказа. Без неё непринятый ключ CLI не роняет расчёт, а
        # молча подменяется приблизительной оценкой — ошибка замечается по деньгам, а не по логу.
        res = await slicers.check((body.get("slicer") or "").strip().lower(),
                                  str(body.get("slicer_path") or "").strip())
        return web.json_response({"ok": True, "check": res})
    if action == "link":
        # Протокол связи (0613014e): чем разговаривать с этим принтером и по какому адресу.
        # Пустой kind = «как раньше» (определить по .env) — это осмысленный выбор, поэтому
        # реагируем на наличие ключа, а не на непустое значение.
        fields: dict = {}
        if "kind" in body:
            kind = (body.get("kind") or "").strip().lower()
            if kind and kind not in printerdrv.DRIVERS:
                return web.json_response({"ok": False, "error": "Неизвестный тип принтера"})
            fields["kind"] = kind
        for key in ("address", "serial"):
            if key in body:
                fields[key] = str(body.get(key) or "").strip()
        # Код доступа: пустая строка НЕ затирает сохранённый (форма его не показывает).
        if (body.get("access_code") or "").strip():
            fields["access_code"] = str(body["access_code"]).strip()
        if body.get("clear_code"):
            fields["access_code"] = ""
        # Чем резать задания этой машины (ElegooSlicer / Bambu Studio / Orca). Пустое значение —
        # «движок по умолчанию», осмысленный выбор, поэтому реагируем на наличие ключа.
        if "slicer" in body:
            sl = (body.get("slicer") or "").strip().lower()
            if sl and sl not in slicers.BACKENDS:
                return web.json_response({"ok": False, "error": "Неизвестный слайсер"})
            fields["slicer"] = sl
        if "slicer_path" in body:
            fields["slicer_path"] = str(body.get("slicer_path") or "").strip()
        await db.update_printer(pid, **fields)
        return web.json_response({"ok": True})
    if action == "time_factor":
        # Коэффициент времени машины (B1): оценка слайсера/fastcalc × множитель. Коридор
        # ×0.2…×5 — за ним опечатка, а не калибровка; отказ, а не молчаливое зажатие.
        tf = db.valid_time_factor(body.get("time_factor"))
        if tf is None:
            return web.json_response({"ok": False, "error":
                f"Коэффициент времени — число от {db.TIME_FACTOR_MIN} до {db.TIME_FACTOR_MAX}"})
        await db.update_printer(pid, time_factor=tf)
        return web.json_response({"ok": True, "time_factor": tf})
    if action == "default":
        await db.set_default_printer(pid)
    elif action == "toggle":
        cur = next((x for x in await db.list_printers(active_only=False) if x.id == pid), None)
        if cur:
            await db.update_printer(pid, is_active=0 if cur.is_active else 1)
    elif action == "delete":
        await db.delete_printer(pid)
    else:
        return web.json_response({"ok": False, "error": "Неизвестное действие"})
    return web.json_response({"ok": True})
