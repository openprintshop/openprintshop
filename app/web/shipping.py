"""Доставка: справочник типов для клиента и мастера, доставка и отгрузка заказа.

Выделено из app/webapp.py волной 13 endgame-плана. Считает СЕРВЕР, и всё меняющее цену
идёт с уведомлением клиенту — поэтому модуль держит `_bot` (веер _BOT_FAN, волна 12,
вариант «б»): ветки отправки закрыты `if _bot is None` и у студии с выключенной
мини-аппой молчат, как и раньше.

Правила дома: НЕ импортировать app.webapp (разрез односторонний, проверяется тестом в
отдельном процессе); регистрация маршрутов осталась в make_app строками на фасадные
имена; тела перенесены ДОСЛОВНО (сверка против git show HEAD:app/webapp.py).
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

from .. import config, db, delivery, notify, roles, texts
from ..webauth import _admin_body, _admin_forbidden, _verify_admin, _verify_init_data
from .common import _bad, _num, _toint

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


async def _api_delivery_types(request: web.Request) -> web.Response:
    """Тарифы доставки для оформления корзины (клиентская ручка).

    total — сумма позиций корзины: по ней считается порог «бесплатно от …», поэтому клиент
    видит не абстрактный прайс, а сколько доставка стоит ИМЕННО СЕЙЧАС. Цена, которую вернули
    здесь, — справочная: при оформлении сервер пересчитывает её заново по тому же типу."""
    if not _verify_init_data(request.query.get("initData", "")):
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    total = _num(request.query, "total", 0.0) or 0.0
    types = await db.list_delivery_types(active_only=True)
    return web.json_response({"ok": True, "enabled": bool(types),
                              "types": [delivery.type_json(t, total) for t in types]})

async def _api_admin_delivery(request: web.Request) -> web.Response:
    """Прайс доставки для админки: все типы (включая выключенные) + реестр перевозчиков."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    types = await db.list_delivery_types(active_only=False)
    return web.json_response({"ok": True, "currency": config.CURRENCY,
                              "types": [delivery.type_json(t) for t in types],
                              "carriers": delivery.carriers_json()})

async def _api_admin_delivery_type(request: web.Request) -> web.Response:
    """CRUD типа доставки: add / update / del. Это ПРАЙС — только владелец (cap pricing)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    if action == "del":
        await db.delete_delivery_type(_toint(body.get("id")))
        return web.json_response({"ok": True})
    if action == "add":
        name = (body.get("name") or "").strip()
        price = _num(body, "price")
        if not name or price is None or price < 0:
            return web.json_response({"ok": False, "error": "Нужны название и цена ≥ 0"})
        free_from = _num(body, "free_from") or 0
        zid = await db.add_delivery_type(
            name[:80], price, free_from=max(0.0, free_from),
            eta=(body.get("eta") or "").strip()[:40],
            comment=(body.get("comment") or "").strip()[:200],
            sort=_toint(body.get("sort"), 0))
        return web.json_response({"ok": True, "id": zid})
    if action == "update":
        zid = _toint(body.get("id"))
        if not zid:
            return web.json_response({"ok": False, "error": "Нет id"})
        fields: dict = {}
        if "name" in body:
            nm = (body.get("name") or "").strip()
            if not nm:
                return web.json_response({"ok": False, "error": "Название не может быть пустым"})
            fields["name"] = nm[:80]
        for key in ("price", "free_from"):
            if key in body:
                val = _num(body, key)
                if val is None or val < 0:
                    return web.json_response({"ok": False, "error": "Сумма ≥ 0"})
                fields[key] = val
        if "eta" in body:
            fields["eta"] = (body.get("eta") or "").strip()[:40]
        if "comment" in body:
            fields["comment"] = (body.get("comment") or "").strip()[:200]
        if "is_active" in body:
            fields["is_active"] = 1 if body.get("is_active") else 0
        if "sort" in body:
            fields["sort"] = _toint(body.get("sort"), 0)
        await db.update_delivery_type(zid, **fields)
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})

async def _group_or_error(gid: str):
    """(группа, позиции, ошибка) по id заказа-корзины — общий вход админских ручек группы."""
    gid = (gid or "").strip()
    if not gid:
        return None, [], web.json_response({"ok": False, "error": "Нет группы"}, status=400)
    ent = await db.get_order_group(gid)
    sibs = await db.list_requests_by_group(gid)
    if ent is None and not sibs:
        return None, [], web.json_response({"ok": False, "error": "Заказ не найден"}, status=404)
    return (ent or {"id": gid}), sibs, None

async def _api_admin_order_delivery(request: web.Request) -> web.Response:
    """Правка доставки в заказе мастером (A2): способ, адрес, тип, стоимость.

    Это ДЕНЬГИ (итог заказа = позиции + доставка), поэтому право finance — оператору сюда
    нельзя. Стоимость мастер может назначить вручную (нестандартный адрес, договорились
    иначе) либо пересчитать по типу: type_id без явной cost → берём тариф на текущую
    сумму позиций. Клиент получает уведомление, только если сумма реально изменилась."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    ent, sibs, err = await _group_or_error(body.get("gid") or body.get("group") or "")
    if err:
        return err
    gid = str(ent.get("id") or (body.get("gid") or "").strip())
    if int(ent.get("delivery_paid") or 0):
        return web.json_response({"ok": False, "error": "Доставка уже оплачена — сумму не меняем"})
    fields: dict = {}
    if "delivery_method" in body:
        m = (body.get("delivery_method") or "").strip().lower()
        fields["delivery_method"] = m if m in ("pickup", "delivery") else None
    if "delivery_address" in body:
        fields["delivery_address"] = (body.get("delivery_address") or "").strip()[:300] or None
    items_total = round(sum(float(s.price or 0) for s in sibs), 2)
    old_cost = delivery.group_delivery_cost(ent)
    new_cost = old_cost
    if "type_id" in body:
        dtype = await db.get_delivery_type(_toint(body.get("type_id")))
        fields["delivery_type_id"] = int(dtype["id"]) if dtype else None
        fields["delivery_type_name"] = (dtype or {}).get("name") or None
        if "cost" not in body:                       # цену не задали руками → тариф типа
            new_cost = delivery.type_cost(dtype, items_total)
    manual = False
    if "cost" in body:
        c = _num(body, "cost")
        if c is None or c < 0:
            return web.json_response({"ok": False, "error": "Стоимость доставки ≥ 0"})
        new_cost = round(c, 2)
        manual = True          # мастер назвал цену сам — автоматика её больше не трогает
    if new_cost != old_cost:
        fields["delivery_cost"] = new_cost
        # Источник пишем ВСЕГДА вместе с ценой. Ради этого колонка и заведена: подаренная
        # доставка (ноль руками) и бесплатная по порогу (ноль автоматом) по самому числу
        # неразличимы, и раньше первая же смена цены заказа молча отменяла подарок.
        fields["delivery_cost_source"] = delivery.MANUAL if manual else delivery.AUTO
        fields["total_snapshot"] = round(items_total + new_cost, 2)
    elif manual and delivery.cost_source(ent) != delivery.MANUAL:
        # Сумма та же, но СМЫСЛ изменился: ноль «бесплатно по порогу» мастер подтвердил
        # руками, и это уже подарок. Без этой ветки пометка терялась ровно в том случае,
        # ради которого колонка и заводилась, — заказ уже перевалил порог, доставка и так
        # ноль, мастер дарит её явно, а через час роняет цену ниже порога.
        fields["delivery_cost_source"] = delivery.MANUAL
    if not fields:
        return web.json_response({"ok": False, "error": "Нечего менять"})
    await db.update_order_group(gid, **fields)
    await roles.log_action(uid, "изменил доставку в заказе", f"группа {gid}")
    if _bot is not None and new_cost != old_cost and sibs:
        try:
            cur = config.CURRENCY
            await notify.notify_user(
                _bot, int(sibs[0].user_id),
                f"🚚 Стоимость доставки по заказу изменена: "
                f"<b>{texts.fmt_money(new_cost, cur)}</b>\n"
                f"Итого с доставкой: <b>{texts.fmt_money(items_total + new_cost, cur)}</b>")
        except Exception:  # noqa: BLE001
            log.debug("delivery change notify failed", exc_info=True)
    return web.json_response({"ok": True, "delivery_cost": new_cost,
                              "total": round(items_total + new_cost, 2)})

async def _api_admin_order_shipment(request: web.Request) -> web.Response:
    """Отправление заказа (A2): перевозчик, трек-номер, ссылка отслеживания.

    Производственное действие — доступно и оператору (cap orders). action:
      save      — сохранить/обновить данные отправления (без уведомления);
      ship      — отметить отправленным и УВЕДОМИТЬ клиента ссылкой отслеживания;
      delivered — отметить доставленным;
      clear     — стереть отправление (ошиблись перевозчиком/номером)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    ent, sibs, err = await _group_or_error(body.get("gid") or body.get("group") or "")
    if err:
        return err
    gid = str(ent.get("id") or (body.get("gid") or "").strip())
    action = (body.get("action") or "save").strip()
    if action == "clear":
        await db.update_order_group(gid, track_carrier=None, track_number=None, track_url=None,
                                    shipped_at=None, delivered_at=None)
        await roles.log_action(uid, "снял отметку об отправке", f"группа {gid}")
        return web.json_response({"ok": True, "track": delivery.group_track(None)})
    if action == "delivered":
        await db.update_order_group(gid, delivered_at=int(time.time()))
        await roles.log_action(uid, "отметил заказ доставленным", f"группа {gid}")
        ent = await db.get_order_group(gid)
        return web.json_response({"ok": True, "track": delivery.group_track(ent)})

    carrier_id = (body.get("carrier") or ent.get("track_carrier") or "").strip().lower()
    number = delivery.clean_number(body.get("number") or ent.get("track_number") or "")
    custom_url = (body.get("url") or "").strip()
    url = delivery.track_url(carrier_id, number, custom_url)
    if not url and not custom_url:
        url = (ent.get("track_url") or "") if not body.get("carrier") else ""
    if action == "ship" and not (carrier_id or number):
        return web.json_response({"ok": False, "error": "Укажите перевозчика или номер отправления"})
    fields = {"track_carrier": carrier_id or None, "track_number": number or None,
              "track_url": url or None}
    if action == "ship":
        fields["shipped_at"] = int(ent.get("shipped_at") or 0) or int(time.time())
    await db.update_order_group(gid, **fields)
    ent = await db.get_order_group(gid)
    track = delivery.group_track(ent)
    if action == "ship":
        await roles.log_action(uid, "отправил заказ", f"группа {gid} · {track['carrier_name']} {number}")
        if _bot is not None and sibs:
            try:
                await notify.notify_user(
                    _bot, int(sibs[0].user_id),
                    texts.user_shipped(gid=gid, carrier=track["carrier_name"], number=number,
                                       url=track["url"], count=len(sibs)))
            except Exception:  # noqa: BLE001
                log.debug("shipment notify failed", exc_info=True)
    return web.json_response({"ok": True, "track": track})
