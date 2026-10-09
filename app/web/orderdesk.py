"""Заказы у мастера: списки (клиентские, самопечать, CSV) и карточка заказа.

Один предмет: то, что мастер ЧИТАЕТ о заказах, — три списка и полная карточка с шапкой
заказа-корзины и подсказкой о пересчёте доставки. Ничего не пишет клиенту и не двигает
статусы: денежные и статусные ручки держатся за `_bot` и остаются в webapp до волн 13/16.

Выделено из app/webapp.py волной 5 endgame-плана — ПОСЛЕ волны 1a: карточка заказа держится
за общий дом медиа и переписки (`_slicer_url`, `_timelapse_url`, `_order_prints_media`,
`_msgs_json` из app/web/ordermedia.py), и до него этот блок был неотделим от webapp.

Права: пути закрыты обычной картой webguard (оператору список и карточка доступны, деньги
вырезает `_hide_money` по `_money_visible` — сервер, а не фронт). `/api/admin/self_orders`
держится ТОЛЬКО на «закрыто по умолчанию» — НЕ вписывать его в `_OPERATOR_ROUTES`
«для симметрии» (см. «Чего нельзя делать никогда» endgame-плана).

Регистрация маршрутов осталась в make_app строками на фасадные имена: забытая строка фасада
падает громко на старте, а не молча у клиента.

Тела перенесены ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

from aiohttp import web

from .. import config, db, delivery, moonraker, texts
from ..webauth import _admin_forbidden, _verify_admin
from ..webguard import _hide_money, _money_visible
from ..websign import _file_ver
from .common import _color_css, _photo_url, _toint, sig, signed_url
from .ordermedia import _msgs_json, _order_prints_media, _slicer_url, _timelapse_url


async def _api_admin_orders(request: web.Request) -> web.Response:
    # Только клиентские заказы: самопечать мастера («печать для себя») живёт в отдельном
    # списке /api/admin/self_orders, чтобы не засорять клиентскую воронку.
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    q = (request.query.get("q") or "").strip()   # сквозной поиск (crm-roadmap этап 2)
    rows = await db.list_requests(100 if q else 40, search=q, kind="client")
    unread = await db.orders_unread_admin()
    # Агрегаты заказа-корзины (B5) по загруженному списку: сколько позиций и общий итог у каждой
    # НЕПУСТОЙ группы — мастеру видно в строке, что заказ мультифайловый. Фильтр gid != '' обязателен.
    gagg: dict[str, dict] = {}
    for r in rows:
        gid = (getattr(r, "order_group", "") or "").strip()
        if gid:
            a = gagg.setdefault(gid, {"count": 0, "total": 0.0})
            a["count"] += 1
            a["total"] += float(r.price or 0)
    money = _money_visible(request)     # оператор (A4) видит очередь работ, но не суммы
    return web.json_response({"ok": True, "currency": config.CURRENCY, "money": money, "orders": [
        _hide_money(o, ("price", "pending", "group_total", "paid", "pay_method")) if not money else o
        for o in (
        {"id": r.id, "label": r.model_label, "user": r.user_name, "status": r.status,
         # user_id нужен настольной админке: из строки заказа открывается карточка клиента
         "user_id": r.user_id,
         "status_label": db.REQ_STATUS_LABELS.get(r.status, r.status), "price": r.price,
         "paid": bool(r.paid),   # значок 💲 в строке списка (OpenTodo 7f257a33)
         "pay_method": getattr(r, "pay_method", "") or "",   # 'online' = Telegram Payments (A1)
         "qty": r.quantity, "created_at": r.created_at, "pending": r.pending_price,
         # заказ-корзина (B5): группа + число позиций/итог — только если позиций в группе ≥2
         # (одиночная «группа» в списке ничем не отличается от обычного заказа).
         "group": ((getattr(r, "order_group", "") or "").strip()
                   if gagg.get((getattr(r, "order_group", "") or "").strip(), {}).get("count", 0) >= 2
                   else ""),
         "group_count": gagg.get((getattr(r, "order_group", "") or "").strip(), {}).get("count", 0),
         "group_total": round(gagg.get((getattr(r, "order_group", "") or "").strip(),
                                       {}).get("total", 0.0), 2),
         # миниатюра модели в строке списка (аудит UX: мастер вспоминает заказ по картинке).
         # У заказов мастера и индивидуальных рендера нет — показываем фото результата.
         "preview": (signed_url("req", r.id, {"v": _file_ver(r.preview_path)})
                     if r.preview_path else (_photo_url("order", r.id, r.done_photo) or "")),
         "unread": unread.get(r.id, 0)} for r in rows)]})


async def _api_admin_self_orders(request: web.Request) -> web.Response:
    """«Печать для себя»: отдельный список заказов-самопечатей мастера (status='self' ИЛИ клиент —
    админ), исключённых из клиентского списка /api/admin/orders. Открываются той же карточкой
    заказа (app.openAdminOrder). Отдаём граммы/минуты — в самопечати это ключевая инфа."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    q = (request.query.get("q") or "").strip()
    rows = await db.list_requests(100 if q else 60, search=q, kind="self")
    return web.json_response({"ok": True, "currency": config.CURRENCY, "orders": [
        {"id": r.id, "label": r.model_label, "user": r.user_name, "status": r.status,
         "status_label": db.REQ_STATUS_LABELS.get(r.status, r.status), "price": r.price,
         "qty": r.quantity, "created_at": r.created_at,
         "grams": r.grams_total, "minutes": r.minutes_total,
         "plastic": r.plastic_name,
         "preview": (signed_url("req", r.id, {"v": _file_ver(r.preview_path)})
                     if r.preview_path else "")} for r in rows]})


async def _api_admin_orders_csv(request: web.Request) -> web.Response:
    """Экспорт всех заказов в CSV (; разделитель, UTF-8 BOM — чтобы Excel открыл без кракозябр).
    Зеркалит чат-хендлер a:reqcsv (handlers/admin.py), отдаёт файл как attachment."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.list_requests(limit=100000)
    buf = io.StringIO()
    buf.write("﻿")        # BOM
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ID", "Дата", "Статус", "Клиент", "Модель", "Материал", "Кол-во",
                "Граммы", "Минуты", "Цена", "Ожид.цена", "Комментарий"])
    for r in items:
        dt = datetime.fromtimestamp(r.created_at).strftime("%Y-%m-%d %H:%M") if r.created_at else ""
        w.writerow([
            r.id, dt, r.status, r.user_name or r.user_id, r.model_label, r.plastic_name,
            r.quantity,
            f"{r.grams_total:.1f}" if r.grams_total else "",
            f"{r.minutes_total:.0f}" if r.minutes_total else "",
            f"{r.price:.2f}" if r.price is not None else "",
            f"{r.pending_price:.2f}" if r.pending_price is not None else "",
            (r.price_comment or "").replace("\n", " "),
        ])
    fname = f"orders_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return web.Response(body=buf.getvalue().encode("utf-8"), content_type="text/csv",
                        charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def _delivery_recalc_hint(ent: dict | None, sibs: list, r, items_total: float) -> dict | None:
    """Данные для предупреждения мастера о пересчёте доставки (волна 5, 09.08.2026).

    Стоимость доставки зависит от суммы заказа через порог «бесплатно от», а цену мастер
    назначает уже после оформления. Значит, назначение цены МЕНЯЕТ СУММУ УЖЕ ВЫСТАВЛЕННОГО
    СЧЁТА, а Telegram сверяет её на pre_checkout (payments.precheckout_error) — висящий у
    клиента счёт после этого отклонится, и клиент увидит отказ платежа, а не «стало дешевле».
    Поэтому мастер обязан узнать об этом ДО того, как нажмёт «Изменить цену»/«Выставить счёт».

    None — предупреждать не о чем: пересчёта не будет (нет типа/порога, доставка оплачена,
    стоимость назначена руками). Условие ровно то же, что у delivery.recalc_group_delivery,
    и живёт оно в одном месте — delivery.threshold_recalcable.

    invoice_ids — позиции с ВИСЯЩИМ счётом («Ждёт оплаты» и не оплачена). Их может быть
    несколько и среди них может не быть текущей: доставка едет в счёте ЯКОРНОЙ позиции
    группы (payments.delivery_extra_minor), поэтому пересчёт цены одной позиции ломает счёт
    другой — мастеру это надо видеть номерами."""
    if not ent or int(ent.get("delivery_paid") or 0):
        return None
    tid = int(ent.get("delivery_type_id") or 0)
    dtype = await db.get_delivery_type(tid) if tid else None
    cost = delivery.group_delivery_cost(ent)
    src = delivery.cost_source(ent)
    if not delivery.threshold_recalcable(dtype, cost, items_total, src):
        return None
    return {
        "free_from": round(float((dtype or {}).get("free_from") or 0), 2),
        "tariff": delivery.tariff_cost(dtype),
        "cost": cost,
        # цена ЭТОЙ позиции, с которой доставка всего заказа станет бесплатной
        "free_price": delivery.free_from_price(dtype, cost, items_total, float(r.price or 0), src),
        "anchor_id": min((int(s.id) for s in sibs), default=int(r.id)),
        "invoice_ids": [int(s.id) for s in sibs
                        if s.status == "awaiting_payment" and not int(getattr(s, "paid", 0) or 0)],
    }


# Тег масштаба в ярлыке заказа: clientflow дописывает «· масштаб 150%», когда масштаб ≠ 100.
_SCALE_TAG = re.compile(r"·\s*масштаб\s+(\d{1,4})\s*%")


def _order_scale_pct(r) -> int | None:
    """Масштаб модели заказа в % для карточки мастера: печатать надо в нём, а не в 100% файла.

    Источник — снимок сметы (breakdown.scale_pct, пишется с 04.07 вместе с самой фичей
    масштаба; по нему же слайсят пересчёт и допечатка). Снимка нет (расчёт истёк до
    оформления) — тег в ярлыке. Тега нет — 100, ровно как слайсит reprint. Заказы без сметы
    (unsliced) кладут в снимок только масштаб — он берётся первой веткой. None — масштаба не
    знаем: у заказа нет файла модели (индивидуальный, готовое изделие, заказ от мастера) или
    это unsliced-заказ до 01.10.2026, где выбор клиента не записан, — «100%» было бы враньём."""
    bd = getattr(r, "breakdown", None)
    if isinstance(bd, dict) and bd.get("scale_pct"):
        try:
            return int(bd["scale_pct"])
        except (TypeError, ValueError):
            pass
    m = _SCALE_TAG.search(r.model_label or "")
    if m:
        return int(m.group(1))
    if (int(getattr(r, "is_custom", 0) or 0) or int(getattr(r, "product_id", 0) or 0)
            or not r.file_path or int(getattr(r, "unsliced", 0) or 0)):
        return None
    return 100


async def _api_admin_order(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    r = await db.get_request(_toint(request.match_info.get("id")))
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"}, status=404)
    await db.mark_request_seen(r.id)   # «непросмотренные» на LCD-дашборде (design_handoff_lcd)
    hist = await db.get_status_history(r.id)
    msgs = await db.list_order_messages(r.id)
    await db.mark_order_read(r.id, by_admin=True)   # админ открыл заказ — сообщения прочитаны
    # Заказ-корзина (B5): если позиция входит в группу (≥2 позиций) — отдаём шапку группы для
    # карточки мастера (сёстры-позиции, общий итог, доставка, комментарий). Пер-позиционные
    # действия (статус/цена/пересчёт) остаются на ЭТОЙ позиции — группу не трогаем скопом.
    # Доставка (A2) живёт на ГРУППЕ, а не на позиции: цена, тип и трек-номер одни на заказ.
    # Поэтому блок группы отдаём и для одиночной позиции с доставкой — иначе мастеру негде
    # ввести трек по заказу из одной модели.
    gid = (getattr(r, "order_group", "") or "").strip()
    group_block = None
    if gid:
        sibs = await db.list_requests_by_group(gid)
        ent = await db.get_order_group(gid)
        dcost = delivery.group_delivery_cost(ent)
        has_delivery = bool((ent or {}).get("delivery_method") or dcost
                            or (ent or {}).get("track_number"))
        if len(sibs) >= 2 or has_delivery:
            items_total = round(sum(float(s.price or 0) for s in sibs), 2)
            group_block = {
                "gid": gid, "count": len(sibs),
                # Порог «бесплатно от суммы» (волна 5). Мастер назначает цену ПОЗЖЕ оформления,
                # и цена двигает стоимость доставки — а значит и сумму уже выставленного счёта.
                # Отдаём фронту всё, чтобы предупредить ДО нажатия: тариф, текущую стоимость,
                # цену ЭТОЙ позиции, с которой доставка станет бесплатной (арифметику по
                # сёстрам считает сервер — четвёртой копии правила во фронте не заводим),
                # и позиции с ВИСЯЩИМ счётом, который пересчёт сделает недействительным.
                # None — пересчёта не будет (нет порога/типа, доставка оплачена, цену
                # доставки мастер назначил руками) → фронту предупреждать не о чем.
                "delivery_recalc": await _delivery_recalc_hint(ent, sibs, r, items_total),
                "total": items_total,
                "items_total": items_total,
                "delivery_cost": dcost,
                # Откуда взялась стоимость: 'manual' — назвал мастер, 'auto'/'' — посчитал
                # сервер. Плюс готовый флаг подарка, чтобы карточка могла честно сказать
                # «подарена мастером» вместо «бесплатно от N» — это разные вещи, и оператору
                # чужой мастерской надо понимать, почему у одного заказа бесплатно, а у
                # соседнего нет. Фронт пока не рисует, поле аддитивное.
                "delivery_cost_source": delivery.cost_source(ent),
                "delivery_is_gift": delivery.is_gift(ent),
                "total_with_delivery": round(items_total + dcost, 2),
                "delivery_type_id": (ent or {}).get("delivery_type_id") or 0,
                "delivery_type": (ent or {}).get("delivery_type_name") or "",
                "delivery_paid": int((ent or {}).get("delivery_paid") or 0),
                "track": delivery.group_track(ent),
                "delivery_method": (ent or {}).get("delivery_method") or "",
                "delivery_address": (ent or {}).get("delivery_address") or "",
                "comment": (ent or {}).get("comment") or "",
                "items": [{"id": s.id, "label": s.model_label, "qty": s.quantity,
                           "price": s.price, "status": s.status,
                           "status_label": db.REQ_STATUS_LABELS.get(s.status, s.status),
                           "current": s.id == r.id} for s in sibs]}
    # Цвета заказа для 3D-вьюера деска (OpenTodo a3dff103): из plastic_name
    # («Пластик · Цвет1 - Цвет2») → css по имени (те же оттенки/переливы, что в мини-аппе).
    # Старые/ручные заказы без « · » — пустой список, вьюер остаётся нейтрально-серым.
    _ov = await db.get_color_overrides()
    _pn_parts = (r.plastic_name or "").split(" · ", 1)
    colors_css = ([c for c in (_color_css(x.strip(), _ov) for x in _pn_parts[1].split(" - ")) if c]
                  if len(_pn_parts) == 2 else [])
    order_json = {
        "id": r.id, "label": r.model_label, "user": r.user_name, "user_id": r.user_id,
        "status": r.status, "status_label": db.REQ_STATUS_LABELS.get(r.status, r.status),
        "balance": await db.get_balance(r.user_id),     # баланс клиента (OpenTodo 8c428c43)
        "price": r.price, "pending_price": r.pending_price, "price_comment": r.price_comment,
        "qty": r.quantity, "printer": r.printer, "plastic": r.plastic_name, "quality": r.quality,
        "strength": r.strength, "grams": r.grams_total, "minutes": r.minutes_total, "dims": r.dims,
        "scale_pct": _order_scale_pct(r),   # None — масштаб неизвестен/неприменим, строку не рисуем
        # 3D деска красит детали по слотам этими цветами; multicolor — бейдж/режим раскраски
        "colors_css": colors_css, "multicolor": int(getattr(r, "multicolor", 0) or 0),
        "due": getattr(r, "due_date", "") or "",
        "reject_reason": getattr(r, "reject_reason", None) or "",
        "src_url": r.src_url, "file_path": r.file_path, "promo": r.promo, "promo_ok": r.promo_ok,
        "rating": r.rating, "review": r.review, "created_at": r.created_at,
        "is_custom": int(getattr(r, "is_custom", 0) or 0), "description": r.description or "",
        # заказ готового изделия (витрина): в UI ровно два статуса — «Новый»/«Вручено»
        "product_id": int(getattr(r, "product_id", 0) or 0),
        "unsliced": int(getattr(r, "unsliced", 0) or 0),   # автослайс не удался (OpenTodo 55a1cf8f)
        # цену посчитает ПК сам (очередь досчёта, Э0) — НЕ «автослайс не удался»: оценивать
        # руками нечего, иначе мастер сделал бы работу, которую ПК вот-вот сделает за него
        "price_pending": int(bool(int(getattr(r, "price_pending", 0) or 0)) and r.status == "new"),
        # Картинка/фото модели в карточке заказа у мастера (OpenTodo aff57787): тот же подписанный
        # /reqprev, что и в списке заказов; пусто, если превью не сохранилось.
        "preview": (signed_url("req", r.id, {"v": _file_ver(r.preview_path)})
                    if r.preview_path and Path(r.preview_path).exists() else ""),
        "photos": [f"/custphoto/{r.id}/{i}?s={sig('cust', r.id)}"
                   for i in range(len(r.photos or []))],
        "dev_fee": float(getattr(r, "dev_fee", 0.0) or 0.0),
        "slicer_shot": _slicer_url(r),
        "timelapse": _timelapse_url(r),
        # все печати заказа из журнала с их медиа (мастеру — включая полный брак)
        "prints": (await _order_prints_media(r.id, for_admin=True))[0],
        "paid": int(getattr(r, "paid", 0) or 0),
        # онлайн-оплата (A1): способ/время оплаты + леджер платежей — мастеру видно,
        # что деньги пришли через провайдера, а не отмечены галочкой
        "pay_method": getattr(r, "pay_method", "") or "",
        "paid_at": int(getattr(r, "paid_at", 0) or 0),
        "payments": await db.payments_for_request(r.id),
        # Реальная себестоимость по факту + маржа (Фаза C мастер-плана): из stock_ledger × цена
        # катушки, посчитано при «напечатано». None — если катушки без закупочной цены/не считалось.
        "cogs_actual": getattr(r, "cogs_actual", None),
        "margin": (round((r.price or 0) - r.cogs_actual, 2)
                   if getattr(r, "cogs_actual", None) is not None else None),
        "done_url": _photo_url("order", r.id, getattr(r, "done_photo", None)),
        "messages": _msgs_json(msgs, mine_is_admin=True),
        "can_print_net": moonraker.enabled() and bool(r.file_path),
        "can_open_slicer": bool(r.file_path),   # локально на ПК бота: открыть .3mf в ElegooSlicer
        "can_open_folder": bool(r.file_path),   # локально на ПК бота: открыть папку файла в проводнике
        "breakdown": texts.order_breakdown(r, config.CURRENCY),
        # Опции постобработки (B4): снимок заказа для тега-плашки в шапке — мастеру сразу видно,
        # что нужна ручная работа после печати (строки цены уже в breakdown.rows).
        "options": await db.get_request_options(r.id),
        "group": group_block,   # шапка заказа-корзины (B5) или None для одиночного заказа
        "history": [{"status": s, "label": db.REQ_STATUS_LABELS.get(s, s), "at": t} for s, t in hist]}
    # Права сотрудника (A4): у оператора карточка заказа приезжает БЕЗ денег — ни цены, ни
    # баланса клиента, ни себестоимости с маржой, ни платежей. Фронт по флагу money просто
    # не рисует эти блоки, но решает здесь сервер: скрытая кнопка защитой не является.
    money = _money_visible(request)
    if not money:
        _hide_money(order_json, ("price", "pending_price", "price_comment", "balance", "dev_fee",
                                 "cogs_actual", "margin", "paid", "pay_method", "paid_at",
                                 "breakdown", "promo", "promo_ok"))
        order_json["payments"] = []
        if group_block:
            # delivery_recalc — целиком про деньги (тариф, порог, стоимость доставки) и нужен
            # только тому, кто назначает цену. У оператора такого права нет (cap finance).
            _hide_money(group_block, ("total", "items_total", "delivery_cost",
                                      "total_with_delivery", "delivery_recalc"))
            for it in group_block.get("items") or []:
                _hide_money(it, ("price",))
    return web.json_response({"ok": True, "currency": config.CURRENCY, "money": money,
                              "order": order_json})
