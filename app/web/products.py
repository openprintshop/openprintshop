"""Готовые товары и индивидуальный заказ: витринные точки входа заказа МИМО конвейера сметы.

Товары (готовые изделия студии с фиксированной ценой) и индивидуальный заказ («пришлите
фото — мастер оценит») создают заказ без расчёта по модели — поэтому живут вместе, отдельно
от конвейера webapp. Обе точки шлют мастеру уведомление в Telegram.

Выделено из app/webapp.py волной 13 endgame-плана. Правила дома:
* модуль НЕ имеет права импортировать app.webapp (разрез односторонний; проверяется тестом
  в отдельном процессе);
* `_CUSTOM_DIR`/`_CUSTOM_MAX_PHOTOS`/`_CUSTOM_MAX_BYTES` уехали сюда; `_CUSTOM_DIR` читает
  ещё webapp._save_slicer_shot (денежный блок, уедет волной 16) — через фасад;
* бота ставит webapp.set_bot веером _BOT_FAN (волна 12, вариант «б»): ветки отправки
  закрыты `if _bot is None` и у студии с выключенной мини-аппой молчат, как и раньше;
* регистрация маршрутов осталась в make_app строками на фасадные имена;
* тела перенесены ДОСЛОВНО (сверка против git show HEAD:app/webapp.py).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from aiohttp import web

from .. import config, db, keyboards, notify, ratelimit, texts
from ..webauth import _admin_body, _admin_forbidden, _verify_admin, _verify_user
from .common import _bad, _img_headers, _maybe_webp, _product_photo_url, _toint, check_sig
from .orderdefaults import _apply_default_delivery, _delivery_note

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`

_CUSTOM_DIR = config.DATA_DIR / "custom_orders"
_CUSTOM_MAX_PHOTOS = 8
_CUSTOM_MAX_BYTES = 12 * 1024 * 1024     # 12 МБ на одно фото


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


def _product_json(p) -> dict:
    return {"id": p.id, "title": p.title, "description": p.description, "price": p.price,
            "stock": p.stock, "in_stock": p.stock > 0, "sold": p.sold,
            "is_active": int(p.is_active), "photo": _product_photo_url(p)}

async def _product_photo_file(request: web.Request) -> web.StreamResponse:
    """Отдать фото товара по публичному подписанному URL (путь берём из БД)."""
    pid = request.match_info.get("id", "")
    if not check_sig(request, "prod", pid):
        raise web.HTTPNotFound()
    p = await db.get_product(_toint(pid))
    if p and p.photo_path and Path(p.photo_path).exists():
        served = await _maybe_webp(request, Path(p.photo_path))
        return web.FileResponse(served, headers=_img_headers(request, private=True))
    raise web.HTTPNotFound()

async def _api_products(request: web.Request) -> web.Response:
    """Витрина готовых изделий для клиента: активные товары; в наличии — выше, распроданные — в конец.

    Персональная скидка клиента действует и на готовые изделия (OpenTodo 737b0a20): отдаём
    discount_pct — фронт показывает цену со скидкой (и перечёркнутую без неё), списание по
    той же скидке считает сервер при покупке (_api_product_order)."""
    u = _verify_user(request.query.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    discount = await db.get_user_discount(int(u["id"]))
    return web.json_response({"ok": True, "currency": config.CURRENCY,
                              "discount_pct": discount,
                              "products": await list_products_json()})


async def list_products_json() -> list[dict]:
    """Активные товары витрины: в наличии — выше, распроданные — в конец. Общее у /api/products
    и пакета каталога приёмной (app/relaybundle.py) — без ПК витрина та же."""
    items = await db.list_products(active_only=True)
    items.sort(key=lambda p: (p.stock <= 0, p.sort, -p.id))
    return [_product_json(p) for p in items]

async def _api_product_order(request: web.Request) -> web.Response:
    """Покупка готового изделия: заказ создаётся сразу по фикс-цене, наличие списывается атомарно.
    Слайс/расчёт не нужны — товар уже напечатан; мастер видит заказ в общем списке (product_id)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    p = await db.get_product(_toint(body.get("product_id")))
    if not p or not p.is_active:
        return web.json_response({"ok": False, "error": "Товар не найден"}, status=404)
    if p.stock <= 0:
        return web.json_response({"ok": False, "error": "Товара нет в наличии"})
    qty = max(1, min(p.stock, _toint(body.get("qty"), 1)))
    comment = (body.get("comment") or "").strip()[:500]
    # Атомарное списание: если между показом витрины и покупкой наличие ушло — честный отказ.
    if not await db.buy_product(p.id, qty):
        return web.json_response({"ok": False, "error": "Не хватает в наличии — обновите витрину"})
    uname = ("@" + u["username"]) if u.get("username") else \
        (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {u['id']}")
    # Скидка распространяется и на готовые изделия (OpenTodo 737b0a20): как у обычных
    # заказов — максимум из тиражной и персональной клиента. Скидку и цену без неё пишем
    # в снимок разбивки, чтобы мастер видел, откуда цена.
    discount = max(await db.best_discount(qty), await db.get_user_discount(int(u["id"])))
    full = round(p.price * qty, 2)
    total = round(full * (1.0 - discount / 100.0), 2)
    note = ("🛍 Готовое изделие из витрины — печатать не нужно, выдать со склада."
            + (f"\n\n🏷 Скидка клиента {discount:g}% — цена без скидки {full:g}"
               if discount > 0 else "")
            + (f"\n\n💬 {comment}" if comment else ""))
    rid = await db.add_request(
        user_id=int(u["id"]), user_name=uname, model_label=p.title, printer="—",
        plastic_name="—", quantity=qty, quality="—", strength="—",
        grams_total=0.0, minutes_total=0.0, price=total,
        preview_path=(p.photo_path or None), description=note, product_id=p.id,
        breakdown={"qty": qty, "unit_price": p.price, "discount_pct": discount,
                   "discount_amount": round(full - total, 2), "total": total})
    await db.log_event(int(u["id"]), "купил готовое изделие",
                       f'#{rid} «{p.title}» ×{qty}', user_name=uname)
    # Доставка по умолчанию (A2): готовое изделие тем более едет физически — адрес нужен.
    await _apply_default_delivery(int(u["id"]), rid, float(total or 0))
    if _bot is not None:
        try:
            r = await db.get_request(rid)
            from aiogram.types import FSInputFile
            admin_text = texts.admin_new_order(r, config.CURRENCY) + "\n\n🛍 Готовое изделие из витрины — выдать со склада."
            await notify.notify_admins(
                _bot, admin_text,
                photo=FSInputFile(p.photo_path) if p.photo_path and Path(p.photo_path).exists() else None,
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                                text="Открыть заказ в приложении"))
            await _bot.send_message(
                int(u["id"]), f"🛍 Заказ на «{texts.esc(p.title)}» ×{qty} принят! "
                              "Мастер свяжется по выдаче." + await _delivery_note(rid))
        except Exception:  # noqa: BLE001
            log.debug("product order notify failed", exc_info=True)
    return web.json_response({"ok": True, "order_id": rid})

async def _api_admin_products(request: web.Request) -> web.Response:
    """Список ВСЕХ товаров для мастера (активные + скрытые), с наличием и числом продаж."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.list_products(active_only=False)
    return web.json_response({"ok": True, "currency": config.CURRENCY,
                              "products": [_product_json(p) for p in items]})

async def _api_admin_product_save(request: web.Request) -> web.Response:
    """Создать (id пуст) или обновить товар. Фото грузится отдельно (/api/admin/photo kind=product)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    title = (body.get("title") or "").strip()[:120]
    if not title:
        return web.json_response({"ok": False, "error": "Укажите название товара"})
    description = (body.get("description") or "").strip()[:2000]
    try:
        price = round(float(str(body.get("price", 0)).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректная цена"})
    if price < 0:
        return web.json_response({"ok": False, "error": "Цена не может быть отрицательной"})
    stock = max(0, _toint(body.get("stock"), 0))
    is_active = 1 if body.get("is_active", True) else 0
    pid = _toint(body.get("id"))
    if pid:
        if not await db.get_product(pid):
            return web.json_response({"ok": False, "error": "Товар не найден"}, status=404)
        await db.update_product(pid, title=title, description=description, price=price,
                                stock=stock, is_active=is_active)
    else:
        pid = await db.add_product(title=title, description=description, price=price,
                                   stock=stock, is_active=is_active)
    p = await db.get_product(pid)
    return web.json_response({"ok": True, "id": pid, "product": _product_json(p)})

async def _api_admin_product_delete(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    pid = _toint(body.get("id"))
    if not await db.get_product(pid):
        return web.json_response({"ok": False, "error": "Товар не найден"}, status=404)
    await db.delete_product(pid)
    return web.json_response({"ok": True})

async def _api_custom_order(request: web.Request) -> web.Response:
    """Индивидуальный заказ из мини-аппы: клиент описывает текстом + фото, что хочет.
    Создаёт заявку (без авторасчёта) — цену затем назначает мастер обычным потоком смены цены.
    multipart: initData, text, file (одно или несколько)."""
    if not (request.content_type or "").startswith("multipart/"):
        return _bad()
    init_data, text, incoming = "", "", []
    reader = await request.multipart()
    async for field in reader:
        if field.name == "initData":
            init_data = (await field.read()).decode("utf-8", "ignore")
        elif field.name == "text":
            text = (await field.read()).decode("utf-8", "ignore").strip()
        elif field.name == "file":
            data = await field.read()
            if data and len(data) <= _CUSTOM_MAX_BYTES and len(incoming) < _CUSTOM_MAX_PHOTOS:
                incoming.append((field.filename or "photo.jpg", data))
    u = _verify_user(init_data)
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    uid = int(u["id"])
    if not ratelimit.allow(f"custom:{uid}", config.RATE_UPLOADS_PER_MIN, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    text = text[:2000]
    if not text and not incoming:
        return web.json_response({"ok": False, "error": "Опишите заказ или приложите фото"})
    _CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, (fname, data) in enumerate(incoming):
        ext = Path(fname).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"):
            ext = ".jpg"
        p = _CUSTOM_DIR / f"{uid}_{int(time.time())}_{i}{ext}"
        try:
            p.write_bytes(data)
            saved.append(str(p))
        except Exception:  # noqa: BLE001
            log.debug("custom photo save failed", exc_info=True)
    uname = ("@" + u["username"]) if u.get("username") else \
        (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {uid}")
    rid = await db.add_request(
        user_id=uid, user_name=uname, model_label="Индивидуальный заказ", printer="",
        plastic_name="", quantity=1, quality="", strength="", grams_total=0, minutes_total=0,
        price=0, preview_path=(saved[0] if saved else None),
        is_custom=1, description=text, photos=saved)
    # Доставка по умолчанию (A2): цену мастер назначит счётом, но адрес нужен ему сразу.
    await _apply_default_delivery(uid, rid, 0.0)
    if _bot is not None:
        try:
            from aiogram.types import FSInputFile, InputMediaPhoto
            cap = (f"🎨 <b>Индивидуальный заказ #{rid}</b>\n"
                   f"👤 {texts.esc(uname)} (id <code>{uid}</code>)\n\n"
                   + (texts.esc(text) if text else "<i>без описания</i>"))[:1000]
            kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                    text="Открыть заказ в приложении")
            # Своя рассылка, а не notify_admins: тут альбом фотографий (send_media_group),
            # которого тот не умеет. Адресата берём общий — иначе оператор не узнает об
            # индивидуальном заказе, а печатать его ему. Цены в тексте нет (price=0 на этом шаге).
            for aid in notify.recipients(notify.ALL_STAFF):
                try:
                    if saved:
                        await _bot.send_photo(aid, FSInputFile(saved[0]), caption=cap, reply_markup=kb)
                        rest = saved[1:10]
                        if len(rest) >= 2:
                            await _bot.send_media_group(
                                aid, [InputMediaPhoto(media=FSInputFile(p)) for p in rest])
                        elif len(rest) == 1:
                            await _bot.send_photo(aid, FSInputFile(rest[0]))
                    else:
                        await _bot.send_message(aid, cap, reply_markup=kb)
                except Exception:  # noqa: BLE001
                    log.debug("custom order notify admin %s failed", aid, exc_info=True)
        except Exception:  # noqa: BLE001
            log.debug("custom order notify failed", exc_info=True)
    await db.log_event(uid, "индивидуальный заказ", f"#{rid}", user_name=uname)
    return web.json_response({"ok": True, "id": rid})

async def _custom_photo(request: web.Request) -> web.StreamResponse:
    rid = request.match_info.get("rid", "")
    if not check_sig(request, "cust", rid):
        raise web.HTTPNotFound()
    r = await db.get_request(_toint(rid))
    idx = _toint(request.match_info.get("idx"))
    if r and r.photos and 0 <= idx < len(r.photos) and Path(r.photos[idx]).exists():
        served = await _maybe_webp(request, Path(r.photos[idx]))
        return web.FileResponse(served, headers=_img_headers(request, private=True))
    raise web.HTTPNotFound()
