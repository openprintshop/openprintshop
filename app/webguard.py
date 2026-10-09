"""Права команды на админских ручках (A4) и гейт возможностей сборки.

Выделено из app/webapp.py волной 4 цикла 2. Тела функций и обе карты маршрутов перенесены
ДОСЛОВНО — путь в карте это право доступа, и опечатка тут не падает, а молча открывает ручку
наёмному оператору либо закрывает нужную. Карты сверяются снимком
tests/test_routes_inventory.py::test_admin_route_caps_match_snapshot.

Модуль зависит только от app/config.py, app/edition.py, app/roles.py, app/webauth.py и aiohttp:
webapp он не импортирует, поэтому цикла нет. app/webapp.py реэкспортирует имена — см. фасад там же.

`_caps_mw` решает по КАНОНИЧЕСКОМУ пути маршрута (`request.match_info.route.resource.canonical`),
а не по фактическому `request.path`: `/api/admin/order/17` и `/api/admin/order/42` обязаны дать
одно и то же право. Переезд модуля канон не меняет — маршруты по-прежнему регистрируются в
webapp.make_app, middleware получает тот же объект запроса.

`_forbidden_cap` перенесён вместе с блоком, хотя в списке волны его нет: его зовёт `_caps_mw`,
и оставить его в webapp значило бы завести ребро webguard -> webapp, то есть цикл. Второй его
вызов (`_api_admin_brand_logo`) остался в webapp и работает через фасад.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import config, db, edition, errorlog, notify, roles
from .webauth import _verify_init_data

log = logging.getLogger(__name__)

# ───────────────────── Права команды на админских ручках (A4) ─────────────────────
# Карта «ручка → минимальное право». ПРИНЦИП: список — это ровно то, что разрешено ОПЕРАТОРУ;
# всё остальное под /api/admin/ по умолчанию доступно только владельцу. Так новая ручка,
# которую забудут внести в карту, не откроется наёмному сотруднику автоматически — ошибка
# по умолчанию идёт в сторону закрытости, а не утечки цен и выручки.
#
# Проверка живёт в middleware (_caps_mw) и работает по КАНОНИЧЕСКОМУ пути маршрута
# (/api/admin/order/{id}), поэтому не зависит от того, что хендлер забыл проверить.
_OPERATOR_ROUTES: dict[str, str] = {
    # заказы: производственный контур без денег
    "/api/admin/orders": roles.CAP_ORDERS,
    "/api/admin/orders/mark_all_seen": roles.CAP_ORDERS,
    "/api/admin/order/{id}": roles.CAP_ORDERS,
    "/api/admin/gridfinity/{rid}": roles.CAP_ORDERS,   # снимок раскладки заявки конфигуратора
    "/api/admin/gridfinity/part/{id}": roles.CAP_ORDERS,   # «деталь напечатана» из печать-листа
    "/api/admin/gridfinity/release/{rid}": roles.CAP_ORDERS,   # отказ по заявке освобождает детали
    "/api/admin/orderfile": roles.CAP_ORDERS,
    "/api/admin/order/status": roles.CAP_ORDERS,
    "/api/admin/order/material": roles.CAP_ORDERS,
    "/api/admin/order/slice": roles.CAP_ORDERS,
    "/api/admin/order/message": roles.CAP_ORDERS,
    "/api/admin/order/print_net": roles.CAP_ORDERS,
    "/api/admin/order/open_slicer": roles.CAP_ORDERS,
    "/api/admin/order/open_folder": roles.CAP_ORDERS,
    "/api/admin/order/attach_print": roles.CAP_ORDERS,
    "/api/admin/order/shipment": roles.CAP_ORDERS,   # трек-номер — работа склада, не касса
    "/api/admin/queue": roles.CAP_ORDERS,
    "/api/admin/queue/move": roles.CAP_ORDERS,
    "/api/admin/photo": roles.CAP_ORDERS,
    "/api/admin/order_media_delete": roles.CAP_ORDERS,   # фото результата/таймлапс заказа (1faab6cb)
    "/admin/photo/{kind}/{id}": roles.CAP_ORDERS,
    "/api/admin/tasks": roles.CAP_ORDERS,
    "/api/admin/task": roles.CAP_ORDERS,
    "/api/admin/events": roles.CAP_ORDERS,
    # сводки: отдаём, но деньги из ответа вырезаются (см. _money_visible).
    # Дашборд из этого ряда ВЫВЕДЕН 08.08.2026 — он ушёл в _OWNER_ROUTES под право «Деньги»:
    # оба фронта и так прячут его плитку за finance, сервер приведён к тому же решению.
    "/api/admin/hub": roles.CAP_ORDERS,
    "/api/admin/status": roles.CAP_ORDERS,
    # принтер и производство
    "/api/admin/printer_status": roles.CAP_PRINTER,
    "/api/admin/printer_files": roles.CAP_PRINTER,
    "/api/admin/printer_print": roles.CAP_PRINTER,
    "/api/admin/printer_control": roles.CAP_PRINTER,
    "/api/admin/printer_unload": roles.CAP_PRINTER,
    "/api/admin/printer_upload": roles.CAP_PRINTER,
    "/api/admin/printer_delete": roles.CAP_PRINTER,
    # допечатка без сбойной детали (Путь A, app/reprint.py) — то же право, что у печати:
    # это действие оператора У СТАНКА, ради которого фича и делается
    "/api/admin/reprint_parts": roles.CAP_PRINTER,
    "/api/admin/reprint": roles.CAP_PRINTER,
    "/api/admin/reprint_start": roles.CAP_PRINTER,
    "/api/admin/printer_load": roles.CAP_PRINTER,
    "/api/admin/printer_heat": roles.CAP_PRINTER,
    "/api/admin/printer_fan": roles.CAP_PRINTER,
    "/api/admin/printer_home": roles.CAP_PRINTER,
    "/api/admin/printer_move": roles.CAP_PRINTER,
    "/api/admin/printer_bind": roles.CAP_PRINTER,
    "/api/admin/printer_unbind": roles.CAP_PRINTER,
    "/api/admin/printer_history": roles.CAP_PRINTER,
    "/api/admin/print_confirm": roles.CAP_PRINTER,
    "/api/admin/print_classify": roles.CAP_PRINTER,
    "/api/admin/printers": roles.CAP_PRINTER,
    "/api/admin/camera": roles.CAP_PRINTER,
    "/api/admin/camera_check": roles.CAP_PRINTER,
    "/api/admin/service": roles.CAP_PRINTER,
    "/api/admin/journal": roles.CAP_PRINTER,
    "/api/admin/journal_add": roles.CAP_PRINTER,
    "/api/admin/journal_update": roles.CAP_PRINTER,
    "/api/admin/journal_delete": roles.CAP_PRINTER,
    "/api/admin/journal_media_delete": roles.CAP_PRINTER,   # скрин стола/таймлапс печати (1faab6cb)
    "/api/admin/spools": roles.CAP_PRINTER,          # остаток пластика нужен у станка (цены — режем)
    # Справочник пластиков 08.08.2026 сузили до «Прайс и каталог» и 09.08.2026 ВЕРНУЛИ обратно:
    # на pricing раздел закрывает только ЭКРАН «Пластики», а тот же /api/admin/plastics дёргают
    # ещё выбор материала в карточке заказа и журнал печати — оба оператору открыты. Без справочника
    # он не может ни сменить пластик в заказе, ни записать печать с материалом (филамент тогда молча
    # не списывается). Цена за кг и коэффициент сложности режутся ниже, в _api_admin_plastics.
    "/api/admin/plastics": roles.CAP_PRINTER,
    "/api/admin/screen_power": roles.CAP_PRINTER,
    # «Инструменты» → допечатка со слоя (Путь В, app/web/tools.py): то же право, что у печати —
    # за ручками заливка файла на принтер и старт.
    "/api/admin/tools/resume/sources": roles.CAP_PRINTER,
    "/api/admin/tools/resume/upload": roles.CAP_PRINTER,
    "/api/admin/tools/resume/analyze": roles.CAP_PRINTER,
    "/api/admin/tools/resume/build": roles.CAP_PRINTER,
    "/api/admin/tools/resume/download": roles.CAP_PRINTER,
    "/api/admin/tools/resume/send": roles.CAP_PRINTER,
    "/api/admin/tools/resume/start": roles.CAP_PRINTER,
    "/api/admin/tools/resume/job": roles.CAP_PRINTER,
    "/api/admin/tools/resume/paths": roles.CAP_PRINTER,
    "/api/admin/tools/resume/gates": roles.CAP_PRINTER,
    "/api/admin/tools/resume/history": roles.CAP_PRINTER,
    # Пульт, вторая очередь (app/web/printerext.py): всё — про железо, то же право, что у пульта.
    "/api/admin/printer_file_info": roles.CAP_PRINTER,
    "/api/admin/printer_thumb": roles.CAP_PRINTER,   # превью файла на принтере (CC2 Control)
    "/api/admin/printer_speed": roles.CAP_PRINTER,
    "/api/admin/printer_refill": roles.CAP_PRINTER,
    "/api/admin/printer_off": roles.CAP_PRINTER,
    "/api/admin/heat_presets": roles.CAP_PRINTER,
    "/api/admin/printer_community": roles.CAP_PRINTER,
}
# Калибровка — целиком производственная история (мастер-настройка принтера), правами не режется
# по одной ручке: их два десятка и все про железо.
_OPERATOR_PREFIXES: tuple[tuple[str, str], ...] = (("/api/admin/calib", roles.CAP_PRINTER),)

# Владельческие ручки, у которых право названо ЯВНО. На доступ это не влияет (оператору
# закрыты все четыре права разом), но отказ тогда честно говорит, чего не хватает —
# «Деньги и цены заказов», а не дежурное «Настройки».
_OWNER_ROUTES: dict[str, str] = {
    "/api/admin/order/price": roles.CAP_FINANCE,
    "/api/admin/order/recalc": roles.CAP_FINANCE,
    "/api/admin/order/recalc_apply": roles.CAP_FINANCE,
    "/api/admin/order/invoice": roles.CAP_FINANCE,
    "/api/admin/order/bill": roles.CAP_FINANCE,
    "/api/admin/order/paid": roles.CAP_FINANCE,
    "/api/admin/order/delivery": roles.CAP_FINANCE,
    "/api/admin/order/create": roles.CAP_FINANCE,
    "/api/admin/balance": roles.CAP_FINANCE,
    "/api/admin/balance_comment": roles.CAP_FINANCE,
    "/api/admin/orders/export.csv": roles.CAP_FINANCE,
    # Дашборд — сводка выручки, и оба фронта прячут его плитку за правом «Деньги». Решение
    # владельца 08.08.2026: прав фронт, сервер сужаем — оператору дашборд больше не отдаём
    # (раньше он был в _OPERATOR_ROUTES с вырезанными деньгами). Ему остаётся /api/admin/hub.
    "/api/admin/dashboard": roles.CAP_FINANCE,
    "/api/admin/bigdata": roles.CAP_FINANCE,          # выгрузка Big Data — аналитика выручки
    "/api/admin/batch_calc": roles.CAP_FINANCE,
    "/api/admin/batch_send": roles.CAP_FINANCE,
    "/api/admin/price_calc": roles.CAP_FINANCE,
    "/api/admin/plastic": roles.CAP_PRICING,     # РЕДАКТИРОВАНИЕ пластика — прайс; список см. выше
    "/api/admin/spool": roles.CAP_PRICING,
    "/api/admin/promo": roles.CAP_PRICING,
    "/api/admin/promos": roles.CAP_PRICING,
    "/api/admin/discount": roles.CAP_PRICING,
    "/api/admin/discounts": roles.CAP_PRICING,
    "/api/admin/option": roles.CAP_PRICING,
    "/api/admin/options": roles.CAP_PRICING,
    "/api/admin/delivery": roles.CAP_PRICING,
    "/api/admin/delivery_type": roles.CAP_PRICING,
    "/api/admin/products": roles.CAP_PRICING,
    "/api/admin/product/save": roles.CAP_PRICING,
    "/api/admin/product/delete": roles.CAP_PRICING,
    "/api/admin/clients": roles.CAP_CLIENTS,
    "/api/admin/calc":                     roles.CAP_CLIENTS,
    "/api/admin/client": roles.CAP_CLIENTS,
    "/api/admin/client_save": roles.CAP_CLIENTS,
    "/api/admin/client_discount": roles.CAP_CLIENTS,
    "/api/admin/client_message": roles.CAP_CLIENTS,
    "/api/admin/client_chat": roles.CAP_CLIENTS,
    "/api/admin/clients/export.csv": roles.CAP_CLIENTS,
    "/api/admin/broadcast": roles.CAP_CLIENTS,
    "/api/admin/families": roles.CAP_CLIENTS,
    "/api/admin/activity": roles.CAP_CLIENTS,
}


def _route_cap(canonical: str) -> str:
    """Право, нужное для маршрута. Не в списке разрешённого оператору → права владельца."""
    cap = _OPERATOR_ROUTES.get(canonical)
    if cap:
        return cap
    for prefix, pcap in _OPERATOR_PREFIXES:
        if canonical == prefix or canonical.startswith(prefix + "/"):
            return pcap
    # закрытое по умолчанию: всё прочее — только владельцу (явное право — лишь для текста отказа)
    return _OWNER_ROUTES.get(canonical, roles.CAP_SETTINGS)


def _forbidden_cap(cap: str) -> web.Response:
    return web.json_response(
        {"ok": False, "error": f"Недостаточно прав: раздел «{roles.CAP_LABELS.get(cap, cap)}» "
                               f"доступен только владельцу", "cap": cap}, status=403)


def _money_visible(request: web.Request) -> bool:
    """Показывать ли деньги в ответе. Оператор видит производство, но не цены и выручку.

    caps в запросе ставит _caps_mw. Их нет (не HTTP-путь админки, не опознали, вызов не из
    aiohttp — например из теста) → ничего не режем: единственный источник истины про доступ —
    сама проверка прав, а не эта функция."""
    caps = request.get("caps") if hasattr(request, "get") else None
    return roles.CAP_FINANCE in caps if caps is not None else True


def _hide_money(payload: dict, keys) -> dict:
    """Стереть денежные поля из ответа (значение → None). Именно стереть, а не оставить ноль:
    ноль оператор прочитал бы как «бесплатно», а None фронт показывает прочерком."""
    for k in keys:
        if k in payload:
            payload[k] = None
    return payload


@web.middleware
async def _edition_mw(request: web.Request, handler):
    """Выключенные возможности сборки (OpenTodo f98aa1ef) — закрыты на входе, а не в UI.

    Спрятать пункт меню мало: ручка остаётся доступной по прямому адресу, а в билде студии
    её и вовсе не должно быть. Реестр (app/edition.py) сам говорит, какие префиксы каким
    ключом закрыты — второго списка здесь нет намеренно, он бы разъехался с первым."""
    key = edition.guard_key(request.path)
    if key and not edition.enabled(key):
        f = edition.FEATURES_BY_KEY[key]
        return web.json_response(
            {"ok": False, "feature": key,
             "error": f"Возможность «{f.title}» выключена в этой сборке"}, status=404)
    return await handler(request)


@web.middleware
async def _caps_mw(request: web.Request, handler):
    """Права команды (A4) на входе в админские ручки — до хендлера и независимо от него.

    Опознаём сотрудника по той же подписи initData (или токену ПК-админки), что и хендлеры.
    Не опознали — пропускаем как есть: с авторизацией разберётся сам хендлер (_verify_admin),
    иначе мы бы дублировали и, что хуже, расходились с ним в поведении.

    multipart (загрузка файлов) не трогаем: чтение тела в middleware сломало бы request.multipart()
    у хендлера. Такие ручки проверяют право у себя — см. _api_admin_brand_logo."""
    path = request.path
    if not (path.startswith("/api/admin/") or path.startswith("/admin/")):
        return await handler(request)
    init = request.query.get("initData", "")
    if not init and request.method == "POST":
        if (request.content_type or "").startswith("multipart/"):
            return await handler(request)
        try:
            body = await request.json()          # тело кэшируется — хендлер прочитает его снова
            init = (body or {}).get("initData", "") if isinstance(body, dict) else ""
        except Exception:  # noqa: BLE001
            init = ""
    uid = _verify_init_data(init) if init else None
    if uid is None or not config.is_admin(uid):
        return await handler(request)
    caps = roles.caps_of(uid)
    request["uid"] = uid
    request["caps"] = caps
    res = getattr(request.match_info.route, "resource", None)
    canonical = (res.canonical if res is not None else None) or path
    need = _route_cap(canonical)
    if need not in caps:
        return _forbidden_cap(need)
    return await handler(request)

# ───────── Сквозные middleware (волна 8 endgame): телеметрия сбоев, gzip, usage-агрегат ─────────
# Переехали из webapp к _caps_mw/_edition_mw: middleware — предмет этого модуля. Тела дословно.
@web.middleware
async def _error_mw(request: web.Request, handler):
    """Ловит НЕобработанные исключения хендлеров: алерт админам + 500. HTTPException
    (404/403/редиректы) — нормальный поток, пробрасываем как есть."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # extra=notelemetry: ошибку уже записал errorlog.capture своим source='web';
        # без метки TelemetryHandler завёл бы вторую строку с source='log' на тот же сбой.
        log.exception("Сбой веб-хендлера %s", request.path, extra={"notelemetry": True})
        # Контекст отпечатка — КАНОНИЧЕСКИЙ путь маршрута (как в _usage_mw ниже): в сыром пути
        # сидят id (/img/105, /avatar/12345), и одна поломка каталога завела бы по строке и по
        # алерту на каждую карточку. Сырой путь остаётся в тексте сообщения.
        res = getattr(getattr(request, "match_info", None), "route", None)
        ctx = getattr(getattr(res, "resource", None), "canonical", None) or "<unmatched>"
        # alert=False: алерт шлём прямо здесь, не дожидаясь дренажа буфера; телеметрия
        # продублировала бы его вторым сообщением на тот же сбой.
        errorlog.capture("web", f"{type(exc).__name__}: {exc}", exc, context=ctx, alert=False)
        await notify.alert(f"Сбой веб-API {request.path}: {exc}", key=f"web_{ctx}")
        return web.json_response({"ok": False, "error": "Внутренняя ошибка"}, status=500)

@web.middleware
async def _compress_mw(request: web.Request, handler):
    """gzip для текстовых ответов (HTML/JSON). Картинки — FileResponse, не web.Response, поэтому
    не трогаем (они уже сжаты). enable_compression() сам согласует кодировку по Accept-Encoding,
    так что старые клиенты получат несжатый ответ."""
    resp = await handler(request)
    try:
        if isinstance(resp, web.Response) and isinstance(resp.body, (bytes, bytearray)) and len(resp.body) > 512:
            ct = resp.content_type or ""
            if ct.startswith("text/") or ct == "application/json":
                resp.enable_compression()
    except Exception:  # noqa: BLE001
        pass
    return resp

@web.middleware
async def _usage_mw(request: web.Request, handler):
    """Best-effort usage-агрегат в БД (endpoint_hits): суточный счётчик по (нормализованный
    путь маршрута, метод, статус). Устойчивая замена ротируемым текстовым access-логам для
    usage-аудитов (аудит 07.2026, реком. 6). Внешний слой — видит финальный статус после
    _error_mw. Никогда не влияет на ответ; неизвестные маршруты (сканеры) сводим в '<unmatched>',
    чтобы не плодить строки по случайным путям."""
    status = 500
    try:
        resp = await handler(request)
        status = resp.status
        return resp
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        try:
            res = getattr(request.match_info.route, "resource", None)
            ep = (res.canonical if res is not None else None) or "<unmatched>"
            await db.bump_endpoint_hit(ep, request.method, status)
        except Exception:  # noqa: BLE001
            pass
