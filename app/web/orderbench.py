"""Заказ на верстаке: материал, слайс модели по ссылке, открыть в слайсере, открыть папку.

Один предмет: что мастер делает у станка с ФАЙЛОМ и МАТЕРИАЛОМ заказа, не трогая ни денег, ни
клиента. Четыре ручки — сменить пластик и цвет уже принятого заказа, слайснуть модель по ссылке
ради итоговых граммов и времени, открыть файл заказа в слайсере на ПК бота и открыть его папку в
проводнике. Ни одна не считает цену и ни одна ничего не пишет клиенту.

Выделено из app/webapp.py волной «Заказ на верстаке» цикла 2 — ПЕРВЫМ этапом самого большого и
самого связного домена файла. Взято ровно то, что уезжает дословно и без модульного состояния
webapp: ни `_bot`, ни `_storage`, ни `_models`, ни `_active_print` в этих телах нет вовсе.
Поэтому решение «свой `_bot` + `set_bot`» (образец app/printmedia.py + run.wire_bot) здесь не
нужно и заводить его НЕЛЬЗЯ: отсюда некому и нечего посылать, а заведённый про запас держатель
бота сделал бы вид, что доставка есть.

ЧТО ОСТАЛОСЬ В app/webapp.py И ПОЧЕМУ — чтобы следующая волна не считала это недоделкой:
  * `_api_admin_order_print_net` физически стоял МЕЖДУ slice и open_slicer и выглядит родным
    братом верстака: то же право CAP_ORDERS, тот же файл заказа. ОСТАЁТСЯ. Он через
    `_set_active_print` пишет словарь `_active_print` — состояние ПРИНТЕРНОГО блока webapp, а не
    заказов (читают `_active_print_header` и app/printwatch.py). Уехав, ручка писала бы в чужой
    словарь, и «богатая шапка» экрана принтера погасла бы для сетевой печати МОЛЧА, без строки в
    логе. Утащить писателя за собой тоже нельзя: `_set_active_print` нужен ещё двум местам webapp.
  * `_save_slicer_shot` стоит вплотную ПЕРЕД slice, но зовётся только из
    `_api_admin_order_invoice`, а тот читает `_bot`. Уедет вместе с деньгами, не с верстаком.
  * ГИГИЕНА СПИСКА (orders/clean_test, orders/mark_all_seen, order/delete) от состояния тоже
    чиста, но это ДРУГОЙ предмет — разметка и удаление СПИСКА, а не работа с файлом конкретного
    заказа; и карта прав там иная: у верстака все четыре пути названы CAP_ORDERS явной строкой в
    app/webguard.py, а те держатся на «закрыто по умолчанию». Готовый самостоятельный этап.
  * КАРТОЧКА ЗАКАЗА и вся денежная половина держатся за помощников, общих с КЛИЕНТСКОЙ частью
    webapp (`_msgs_json`, `_slicer_url`, `_timelapse_url`, `_order_prints_media`), и за `_bot`.
    Импортировать их сюда нельзя (цикл — то самое, ради чего разрез), копировать нельзя
    (дословность). Сначала отдельной волной нужен общий дом для медиа и сообщений заказа.

МАРШРУТЫ ЖИВУТ ЗДЕСЬ ЖЕ, в таблице `routes` над обработчиками, а make_app подключает их одной
строкой `app.add_routes(orderbench.routes)` — потерять ручку при переносе нечем. Пути не
изменились ни на символ, поэтому `_caps_mw` читает те же канонические строки app/webguard.py
(все четыре — CAP_ORDERS), `_usage_mw` и `_error_mw` видят маршруты как раньше, а
app/webui/api.js бьёт по тем же адресам: фронт не трогался вовсе.

Модуль НЕ импортирует webapp — иначе получился бы цикл, ради разрыва которого разрез и
затевался: помощники берутся напрямую из app/webauth.py и app/web/common.py, то есть из
источника, а не через фасад. app/webapp.py реэкспортирует все пять имён фасадом, поэтому и код,
и тесты находят их как webapp.*.

ДВЕ ПОМЕТКИ, БЕЗ КОТОРЫХ СЛЕДУЮЩИЙ ЧИТАТЕЛЬ ПОТЕРЯЕТ ВРЕМЯ:
  * ИМЯ ЛОГГЕРА. `log` здесь — logging.getLogger("app.web.orderbench"), а не "app.webapp":
    искать эти строки в bot.log грепом по «app.webapp» больше нельзя. Телеметрия ошибок при этом
    не теряется — errorlog.TelemetryHandler висит на КОРНЕВОМ логгере (run.py), по имени он не
    фильтрует, и `log.exception("admin slice failed")` по-прежнему доедет до экрана ошибок.
  * ПОДМЕНА ПОДПИСИ В ТЕСТАХ. Все четыре ручки читают подпись ТОЛЬКО через `_admin_body`, а он
    связан в ГЛОБАЛАХ этого модуля. Прямой `monkeypatch.setattr(webapp, "_admin_body", ...)` —
    а так написано большинство тестов набора — сюда НЕ достанет, и conftest от этого не спасает.
    Новый тест на эти четыре маршрута писать ТОЛЬКО через conftest.stub_auth, который обходит
    весь _AUTH_MODULES. Отказ иначе молчаливый и асимметричный: положительный тест краснеет
    честным 403, а отрицательный («постороннему 403») продолжает ПРОХОДИТЬ — но уже сам собой,
    настоящей проверкой на фальшивом initData.

Тела функций перенесены ДОСЛОВНО, без единой правки. Комментарии внутри дороже кода и уехали
буква в букву — в частности разбор 11.08 про `cmd /c start` (прямой Popen делал GUI слайсера
ребёнком бота, и следующий же перезапуск сторожа закрывал мастеру слайсер с несохранённой
работой), правило «план резерва правим только однозначные случаи» и «мастер явно попросил СЛАЙС
— слайсим даже при выключенном slicing_enabled». Одна ссылка в докстринге
`_printer_filament_names` устарела ещё ДО переноса: обещанного зеркала handlers.admin в
репозитории нет вовсе (в app/handlers/ лежат __init__, common, payments, user_flow), второго
такого имени в проекте тоже нет. Строка оставлена дословно, но искать по ней нечего.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

from aiohttp import web

from .. import calcservice, config, db, downloader, materials, reslice
from ..webauth import _admin_body
from .common import _bad, _toint

log = logging.getLogger(__name__)


# Маршруты блока объявляются рядом с обработчиком, а не списком в make_app: при переносе
# блока в отдельный модуль строка регистрации уезжает вместе с ручкой и потеряться не может.
# Канонический путь ресурса тот же, поэтому _caps_mw/_usage_mw/_error_mw читают его как раньше.
routes = web.RouteTableDef()


@routes.post("/api/admin/order/material")
async def _api_admin_order_material(request: web.Request) -> web.Response:
    """Смена ПЛАСТИКА и ЦВЕТА заказа мастером (OpenTodo e132724b): клиент выбрал одно, а
    печатать договорились другим — раньше это правилось только через БД. Обновляет
    requests.plastic_name («Пластик · Цвет1 - Цвет2») и, где однозначно, план резерва
    склада (stock_plan): 1 строка → переписываем пластик/цвет; строк столько же, сколько
    цветов, → цвета попарно; иначе план не трогаем (граммы по цветам знал только расчёт).
    Цену НЕ пересчитывает — для этого есть отдельные «Изменить цену»/«Пересчитать»."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    r = await db.get_request(_toint(body.get("id")))
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    plastic = await db.get_plastic(_toint(body.get("plastic_id")))
    if not plastic:
        return web.json_response({"ok": False, "error": "Пластик не найден"})
    raw = body.get("colors") if isinstance(body.get("colors"), list) else [body.get("color")]
    sel = [c for c in (str(x or "").strip() for x in (raw or [])[:8])
           if c and c in (plastic.color_list or [])]
    color = " - ".join(sel)
    pdisp = f"{plastic.name} · {color}" if color else plastic.name
    await db.update_request(r.id, plastic_name=pdisp)
    # план резерва: правим только однозначные случаи (см. докстринг)
    try:
        plan = await db.get_order_stock_plan(r.id)
        if plan and sel:
            if len(plan) == 1:
                plan[0].update({"plastic_id": plastic.id, "color": sel[0]})
                await db.set_order_stock_plan(r.id, plan)
            elif len(plan) == len(sel):
                for row, c in zip(plan, sel):
                    row.update({"plastic_id": plastic.id, "color": c})
                await db.set_order_stock_plan(r.id, plan)
            else:
                log.info("заказ #%s: план резерва (%d строк) не совпал с цветами (%d) — не трогаю",
                         r.id, len(plan), len(sel))
    except Exception:  # noqa: BLE001
        log.debug("order material: stock plan update failed", exc_info=True)
    await db.log_event(_uid, "сменил материал заказа", f"#{r.id} → {pdisp}")
    return web.json_response({"ok": True, "plastic": pdisp})


@routes.post("/api/admin/order/slice")
async def _api_admin_order_slice(request: web.Request) -> web.Response:
    """Мастер «закидывает» модель по ссылке для расчёта индивидуального заказа (OpenTodo
    e78bf8f2): скачиваем + слайсим выбранными пластиком/качеством/прочностью и возвращаем готовые
    итоговые граммы/время — мастер не считает руками, а подставляет их в счёт."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    if not await db.get_request(_toint(body.get("id"))):
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    url = (body.get("url") or "").strip()
    if not downloader.is_http_url(url):
        return web.json_response({"ok": False, "error": "Вставьте ссылку на модель (http/https)"})
    try:
        meshes, label = await downloader.fetch_from_url(url)
    except downloader.MakerWorldBlocked:
        return web.json_response({"ok": False, "error": "MakerWorld просит капчу — скачайте файл "
                                  "и слайсните вручную, либо вставьте прямую ссылку на файл."})
    except downloader.DownloadError as exc:
        return web.json_response({"ok": False, "error": f"Не удалось скачать модель: {exc}"})
    printer = await db.get_default_printer()
    quals = await db.list_quality(active_only=True)
    strs = await db.list_strength(active_only=True)
    plastic = await db.get_plastic(_toint(body.get("plastic_id")))
    quality = (await db.get_quality(_toint(body.get("quality_id")))
               or next((q for q in quals if q.is_default), quals[0] if quals else None))
    strength = (await db.get_strength(_toint(body.get("strength_id")))
                or next((s for s in strs if s.is_default), strs[0] if strs else None))
    qty = max(1, _toint(body.get("qty"), 1))
    if not (printer and plastic and quality and strength):
        return web.json_response({"ok": False, "error": "Проверьте параметры (принтер/пластик/пресеты)"})
    try:
        # мастер явно попросил СЛАЙС — слайсим даже при выключенном slicing_enabled
        res = await calcservice.compute(
            user_id=uid, model_paths=meshes, model_label=label, printer=printer, plastic=plastic,
            color="", qty=qty, quality=quality, strength=strength, save=False, force_slice=True)
    except calcservice.CalcError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("admin slice failed")
        return web.json_response({"ok": False, "error": f"Ошибка слайсинга: {exc}"})
    bd = res["bd"]
    await db.log_event(uid, "слайснул модель для заказа", label)
    return web.json_response({"ok": True, "grams": round(bd.grams_total, 1),
                              "minutes": round(bd.minutes_total), "dims": res.get("dims", ""),
                              "label": label})


async def _printer_filament_names(r) -> tuple[str, str]:
    """Имена профилей принтера и филамента из заказа — чтобы пересохранённый .3mf открылся под
    КОНКРЕТНЫЙ принтер (printer_settings_id / filament_settings_id в project_settings.config).
    Зеркало handlers.admin._printer_filament_names (логика одна на обе админки)."""
    printer_name = filament_name = ""
    try:
        printers = await db.list_printers(active_only=False)
        pr = next((x for x in printers if x.name == r.printer), None) or await db.get_default_printer()
        if pr and Path(pr.machine_profile).exists():
            printer_name = json.loads(Path(pr.machine_profile).read_text(encoding="utf-8")).get("name", "")
        plastics = await db.list_plastics(active_only=False)
        pl = next((x for x in plastics if x.name == r.plastic_name), None)
        if pr and pl:
            fp = materials.resolve_filament(
                pr.filament_dir, pl.material_type,
                fallback=pl.filament_profile or str(config.DEFAULT_FILAMENT_PROFILE))
            if fp and Path(fp).exists():
                filament_name = json.loads(Path(fp).read_text(encoding="utf-8")).get("name", "")
    except Exception:  # noqa: BLE001
        log.debug("printer/filament name resolve failed", exc_info=True)
    return printer_name, filament_name


@routes.post("/api/admin/order/open_slicer")
async def _api_admin_order_open_slicer(request: web.Request) -> web.Response:
    """Открыть модель заказа в ElegooSlicer НА МАШИНЕ БОТА — зеркало чат-команды a:rslice.
    Локальное действие (subprocess/startfile): полезно только когда бот и админ за одним ПК;
    для удалённого app-клиента файл всё равно откроется на сервере бота, а не у клиента."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    r = await db.get_request(_toint(body.get("id")))
    path = getattr(r, "file_path", "") if r else ""
    if not (path and Path(path).exists()):
        return web.json_response({"ok": False, "error": "Файл не найден на диске"})
    open_path, note = path, "Открываю в ElegooSlicer на ПК бота"
    # #6 + «.3mf под конкретный принтер»: пересохраняем проектный .3mf с НАШИМ пресетом
    # (слой/заполнение/стенки) и профилями принтера+филамента, чтобы открылось готовым.
    try:
        q = await db.get_quality(r.quality_id) if getattr(r, "quality_id", None) else None
        s = await db.get_strength(r.strength_id) if getattr(r, "strength_id", None) else None
        if q and s and Path(path).suffix.lower() == ".3mf":
            pname, fname = await _printer_filament_names(r)
            re3mf = await asyncio.to_thread(
                reslice.resave_with_preset, path, q.layer_height, s.infill, s.walls, pname, fname)
            if re3mf and Path(re3mf).exists():
                open_path = str(re3mf)
                note = "Открываю в ElegooSlicer с вашим пресетом и принтером (на ПК бота)"
    except Exception:  # noqa: BLE001
        log.debug("reslice for slicer-open failed", exc_info=True)
    try:
        if config.SLICER_PATH.exists():
            # ОТВЯЗЫВАЕМ слайсер от бота через промежуточный `cmd /c start`. Раньше это был
            # прямой Popen, то есть GUI слайсера становился ребёнком бота — и следующий же
            # перезапуск (сторож бьёт taskkill /T по всему дереву) закрывал мастеру слайсер
            # с несохранённой работой. cmd мгновенно завершается, слайсер остаётся сиротой
            # и в дерево бота больше не входит (разбор 11.08).
            subprocess.Popen(   # noqa: S603 — открыть в слайсере на машине бота
                ["cmd", "/c", "start", "", str(config.SLICER_PATH), open_path],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.startfile(open_path)   # noqa: S606 — фолбэк: ассоциированное приложение
            note = "Открываю файл на ПК бота"
    except Exception:  # noqa: BLE001
        log.warning("open in slicer failed for order", exc_info=True)
        return web.json_response({"ok": False, "error": "Не удалось открыть"})
    return web.json_response({"ok": True, "note": note})


@routes.post("/api/admin/order/open_folder")
async def _api_admin_order_open_folder(request: web.Request) -> web.Response:
    """Открыть папку файла модели заказа в проводнике НА МАШИНЕ БОТА — зеркало чат-команды a:rfolder.
    Локальное действие (os.startfile): полезно только когда бот и админ за одним ПК;
    для удалённого app-клиента папка откроется на сервере бота, а не у клиента."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    r = await db.get_request(_toint(body.get("id")))
    path = getattr(r, "file_path", "") if r else ""
    if not (path and Path(path).exists()):
        return web.json_response({"ok": False, "error": "Файл не найден на диске"})
    try:
        os.startfile(str(Path(path).parent))   # noqa: S606 — локально, только админ
    except Exception:  # noqa: BLE001
        log.warning("open_folder failed for order", exc_info=True)
        return web.json_response({"ok": False, "error": "Не удалось открыть папку"})
    return web.json_response({"ok": True, "note": "Открываю папку на ПК бота"})
