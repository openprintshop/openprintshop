"""Цены и скидки: промокоды, тиражные и персональные скидки, опции постобработки, калькулятор.

Здесь только транспорт админского экрана «Цены и скидки» (app/webui/desk/src/screens/
pricing.jsx): отдать его формам справочники и применить действие мастера. Сама формула цены
живёт в app/pricing.py, человеческая разбивка сметы — в app/texts.py, хранение — в db.
Модуль назван prices, а НЕ pricing, именно поэтому: app/pricing.py — это ДВИЖОК ФОРМУЛЫ,
который данный модуль импортирует, и два разных смысла одного слова в одном файле читались бы
как ошибка.

Выделено из app/webapp.py волной «Цены и скидки» цикла 2. Семь ручек — один предмет: всё, чем
мастер правит ПРАЙС-ЛИСТ (промокоды, тиражные и персональные скидки, каталог опций
постобработки), плюс калькулятор, который тем же прайсом считает смету на ручной ввод.

ПОЧЕМУ КАЛЬКУЛЯТОР ЗДЕСЬ, ХОТЯ ЕГО ПРАВО — CAP_FINANCE, а у остальных шести CAP_PRICING.
Довод не в праве, а в коде: единственный вызывающий — CalcDrawer того же экрана pricing.jsx,
а зависимости pricing.compute_price_manual и texts.breakdown_rows/fmt_money/fmt_minutes не
нужны больше ни одной ручке блока. Денежные ручки ЗАКАЗА (_api_admin_order_price,
_api_admin_order_recalc, _api_admin_batch_calc) остались в webapp: они считают ПО ЗАКАЗУ, а
здесь compute_price_MANUAL — без заказа вовсе. Права трогать нельзя ни на символ: неоднородную
карту (6×pricing + 1×finance) морозит tests/test_prices_facade.py.

ЧТО ОСТАЛОСЬ В app/webapp.py И ПОЧЕМУ — чтобы следующая волна не считала это недоделкой:
  * ТАРИФЫ ДОСТАВКИ (_api_delivery_types, _api_admin_delivery, _api_admin_delivery_type). У двух
    из трёх право тоже CAP_PRICING, но первая — КЛИЕНТСКАЯ: она проверяет _verify_init_data, а
    не _verify_admin, и отдаёт свой отказ вместо _admin_forbidden(). Завести её сюда значило бы
    держать в одном модуле два контракта аутентификации и два вида 403, а соблазн «причесать»
    их под соседей молча поменял бы контракт корзины. У домена есть свой дом (app/delivery.py)
    и своя будущая цель app/web/delivery.py, которая захочет тарифы вместе с отправлениями.
  * ПЛАСТИКИ И КАТУШКИ. Прайсом объявлена только ЗАПИСЬ; ЧТЕНИЕ /api/admin/plastics и
    /api/admin/spools — CAP_PRINTER (см. app/webguard.py): справочник дёргают карточка заказа и
    журнал печати. Это половина производственного контура, а не прайс.
  * НАСТРОЙКИ (_api_admin_settings / _api_admin_setting, CAP_SETTINGS) обслуживают ЧЕТЫРЕ экрана
    деска, а не только прайс: спрятать под именем «цены» валидацию токена BotFather, brand_accent
    и режим входа в деск — значит потерять их.
CAP_PRICING — это ПАКЕТ доступа (shell.jsx гейтит им пять экранов), а не граница домена.

ДВЕ ВЕЩИ, КОТОРЫЕ ЛЕГКО СЧЕСТЬ СЛУЧАЙНЫМИ, А ОНИ НАРОЧНЫЕ:
  * _api_admin_discounts читает db.list_customers() — список заказчиков для выпадашки
    ПЕРСОНАЛЬНОЙ скидки. Это чтение CRM внутри прайсовой ручки; будущая волна CRM не должна
    счесть его своим.
  * Персональная скидка пишется ДВУМЯ путями: отсюда (action=add_user, CAP_PRICING) и из
    карточки клиента _api_admin_client_discount (app/webapp.py:5789, CAP_CLIENTS). Две ручки,
    два права, одна таблица через db.set_user_discount — объединять нельзя, разойдутся права.

Тела функций перенесены ДОСЛОВНО, без единой правки. Что здесь ломается от «поправлю по дороге»:
  * `_num(body, "discount_pct", 0.0)` в калькуляторе — ТРЕТИЙ позиционный аргумент
    _num(body, key, default=None) (app/web/common.py:60). Не сокращать до двух.
  * `bd.extra_colors = extra_colors` — мутация уже собранного PriceBreakdown ПОСЛЕ
    compute_price_manual, и работает она потому, что это обычный @dataclass. Передать
    extra_colors аргументом ВНУТРЬ compute_price_manual — поменять числа: там extra_colors > 0
    включает надбавки multicolor_waste_pct/multicolor_time_pct, а докстринг ручки прямо говорит,
    что при многоцвете они НЕ добавляются (числа вводятся из реального многоцветного слайса).
    Цена в калькуляторе уехала бы вверх.

МАРШРУТЫ ЖИВУТ ЗДЕСЬ ЖЕ, в таблице `routes` над обработчиками, а make_app подключает их одной
строкой `app.add_routes(prices.routes)`. Пути не изменились ни на символ: канонический путь
ресурса тот же, поэтому `_caps_mw` (право берётся _route_cap(canonical) в app/webguard.py и от
модуля обработчика не зависит), `_usage_mw` и `_error_mw` видят их как раньше.

Модуль НЕ импортирует webapp — иначе разрез не разорвал бы цикл, а спрятал его: помощники
берутся напрямую из app/webauth.py и app/web/common.py, то есть из источника, а не через фасад.
app/webapp.py реэкспортирует все семь имён фасадом, поэтому и код, и тесты находят их как
webapp.*.

ПОДМЕНА ПРОВЕРКИ ПОДПИСИ В ТЕСТАХ — ТОЛЬКО через conftest.stub_auth (модуль перечислен в
_AUTH_MODULES). Прямой `monkeypatch.setattr(webapp, "_verify_admin"|"_admin_body", ...)` —
господствующая в наборе идиома — до ЭТОГО модуля НЕ достаёт: три GET-ручки читают _verify_admin
из своих глобалов, четыре POST — свой _admin_body. Отказ безмолвный: положительный тест
краснеет честным 403, а отрицательный («постороннему 403») начинает проходить сам собой,
настоящей проверкой на фальшивом initData.
"""
from __future__ import annotations

import time

from aiohttp import web

from .. import config, db, pricing, texts
from ..webauth import _admin_body, _admin_forbidden, _verify_admin
from .common import _bad, _num, _toint

# Маршруты блока объявляются рядом с обработчиком, а не списком в make_app: при переносе
# блока в отдельный модуль строка регистрации уезжает вместе с ручкой и потеряться не может.
# Канонический путь ресурса тот же, поэтому _caps_mw/_usage_mw/_error_mw читают его как раньше.
routes = web.RouteTableDef()


@routes.get("/api/admin/promos")
async def _api_admin_promos(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    return web.json_response({"ok": True, "promos": await db.list_promos()})


@routes.post("/api/admin/promo")
async def _api_admin_promo(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    if action == "delete":
        await db.delete_promo((body.get("code") or "").strip())
        return web.json_response({"ok": True})
    if action == "add":
        code = (body.get("code") or "").strip().upper()
        if not code:
            return web.json_response({"ok": False, "error": "Введите код"})
        try:
            pct = float(str(body.get("discount_pct")).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Скидка — число"})
        days = _toint(body.get("days"), 0)
        expires = (int(time.time()) + days * 86400) if days > 0 else 0
        await db.add_promo(code, pct, expires, _toint(body.get("max_uses"), 0))
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})


@routes.post("/api/admin/price_calc")
async def _api_admin_price_calc(request: web.Request) -> web.Response:
    """Калькулятор цены в админке: ручной ввод грамм / времени / цены-за-кг / скидки / кол-ва →
    ТА ЖЕ формула, что и при слайсинге модели (pricing.compute_price), с текущими настройками.

    Многоцвет (OpenTodo 82ebeeef): colors=[{grams, price_per_kg}, ...] (≥2) — граммы по каждому
    цвету своей ценой; материал = Σ грамм×цена (средневзвешенная цена/кг), надбавки за продувку
    НЕ добавляются (числа вводятся из реального многоцветного слайса). Одноцвет — как раньше."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    minutes = _num(body, "minutes")
    if minutes is None or minutes < 0:
        return web.json_response({"ok": False, "error": "Нужно неотрицательное время"})
    colors_in = body.get("colors")
    extra_colors = 0
    if isinstance(colors_in, list) and colors_in:
        rows: list[tuple[float, float]] = []
        for c in colors_in:
            g = _num(c or {}, "grams")
            pk = _num(c or {}, "price_per_kg")
            if g is None or pk is None or g <= 0 or pk < 0:
                return web.json_response(
                    {"ok": False, "error": "В каждом цвете нужны граммы > 0 и цена за кг"})
            rows.append((g, pk))
        if len(rows) < 2:
            return web.json_response({"ok": False, "error": "Многоцвет — минимум два цвета"})
        grams = sum(g for g, _ in rows)
        price_per_kg = sum(g * pk for g, pk in rows) / grams
        extra_colors = len(rows) - 1
    else:
        grams = _num(body, "grams")
        price_per_kg = _num(body, "price_per_kg")
        if (grams is None or price_per_kg is None or grams < 0 or price_per_kg < 0):
            return web.json_response({"ok": False,
                                      "error": "Нужны неотрицательные числа: граммы, цена за кг"})
    discount = min(100.0, max(0.0, _num(body, "discount_pct", 0.0)))
    qty = max(1, _toint(body.get("qty"), 1))
    settings = await db.get_settings()
    bd = pricing.compute_price_manual(grams, minutes, price_per_kg, settings,
                                      qty=qty, discount_pct=discount)
    if extra_colors:
        bd.extra_colors = extra_colors   # подпись «многоцветная (~N цв.)» в разбивке
    cur = config.CURRENCY
    rows = texts.breakdown_rows(bd, settings, cur)
    total = bd.total
    # «Готовое изделие»: печать на склад без заказчика — риск не продать. Наценка идёт
    # ПОВЕРХ итога и только по явной галочке мастера; клиентские расчёты её не видят.
    ready_pct = 0.0
    if body.get("ready_product"):
        ready_pct = max(0.0, float(settings.get("ready_product_markup_pct", 0.0) or 0.0))
        if ready_pct:
            extra = round(total * ready_pct / 100.0, 2)
            total = round(total + extra, 2)
            rows = list(rows) + [{"label": "Готовое изделие", "sub": f"{ready_pct:g}%",
                                  "value": texts.fmt_money(extra, cur)}]
    return web.json_response({"ok": True, "ready_markup_pct": ready_pct, "bd": {
        "rows": rows,
        "total": texts.fmt_money(total, cur),
        "total_num": total,
        "grams_total": f"{bd.grams_total:.1f}",
        "time_total": texts.fmt_minutes(bd.minutes_total),
        "qty": bd.qty,
    }})


@routes.get("/api/admin/discounts")
async def _api_admin_discounts(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    tiers = await db.list_discounts()
    users = await db.list_user_discounts()
    customers = await db.list_customers()
    return web.json_response({"ok": True,
        "tiers": [{"id": d.id, "min_qty": d.min_qty, "pct": d.discount_pct} for d in tiers],
        "users": [{"uid": u[0], "pct": u[1], "name": u[2]} for u in users],
        "customers": customers})


@routes.post("/api/admin/discount")
async def _api_admin_discount(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    if action == "add_tier":
        pct = _num(body, "pct")
        if pct is None:
            return web.json_response({"ok": False, "error": "Скидка — число"})
        await db.add_discount(_toint(body.get("min_qty")), pct)
        return web.json_response({"ok": True})
    if action == "del_tier":
        await db.delete_discount(_toint(body.get("id")))
        return web.json_response({"ok": True})
    if action == "add_user":
        pct = _num(body, "pct")
        uid = _toint(body.get("uid"))
        if pct is None or not uid:
            return web.json_response({"ok": False, "error": "Нужны id и %"})
        await db.set_user_discount(uid, pct, (body.get("name") or "").strip() or None)
        return web.json_response({"ok": True})
    if action == "del_user":
        await db.delete_user_discount(_toint(body.get("uid")))
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})


@routes.get("/api/admin/options")
async def _api_admin_options(request: web.Request) -> web.Response:
    """Каталог опций постобработки (B4) для десктоп-админки: весь список, включая выключенные."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    opts = await db.list_postproc_options(active_only=False)
    return web.json_response({"ok": True,
        "options": [{"id": o.id, "name": o.name, "price": o.price,
                     "is_active": int(o.is_active), "sort": o.sort,
                     "per_part": int(o.per_part)} for o in opts]})


@routes.post("/api/admin/option")
async def _api_admin_option(request: web.Request) -> web.Response:
    """CRUD одной опции постобработки (B4): add / update / del. Цена — розничная (наценка/скидка
    на неё не начисляются). Зеркалит _api_admin_discount."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    if action == "add":
        name = (body.get("name") or "").strip()
        price = _num(body, "price")
        if not name or price is None or price < 0:
            return web.json_response({"ok": False, "error": "Нужны название и цена ≥ 0"})
        await db.add_postproc_option(name, price, _toint(body.get("sort"), 0),
                                     per_part=1 if body.get("per_part") else 0)
        return web.json_response({"ok": True})
    if action == "update":
        oid = _toint(body.get("id"))
        if not oid:
            return web.json_response({"ok": False, "error": "Нет id"})
        fields: dict = {}
        if "name" in body:
            nm = (body.get("name") or "").strip()
            if not nm:
                return web.json_response({"ok": False, "error": "Название не может быть пустым"})
            fields["name"] = nm
        if "price" in body:
            price = _num(body, "price")
            if price is None or price < 0:
                return web.json_response({"ok": False, "error": "Цена ≥ 0"})
            fields["price"] = price
        if "is_active" in body:
            fields["is_active"] = 1 if body.get("is_active") else 0
        if "sort" in body:
            fields["sort"] = _toint(body.get("sort"), 0)
        if "per_part" in body:
            fields["per_part"] = 1 if body.get("per_part") else 0
        await db.update_postproc_option(oid, **fields)
        return web.json_response({"ok": True})
    if action == "del":
        await db.delete_postproc_option(_toint(body.get("id")))
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})
