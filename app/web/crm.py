"""CRM мастера: клиенты, карточка, скидка, сплошной чат, события, поиск, задачи.

Один предмет: работа мастера с КЛИЕНТСКОЙ БАЗОЙ (crm-roadmap.md) — списки и карточка
клиента, ручные CRM-поля, персональная скидка, сплошная переписка, события для тостов
настольной админки, поиск по чатам и задачи-напоминания.

Выделено из app/webapp.py волной 6 endgame-плана (после волны 1a: чат читается через
`_msgs_json` из app/web/ordermedia.py, аватарка — `_avatar_url` из web/common).

Домен, разрезанный волной 6 пополам по держателю бота, СОБРАН ОБРАТНО волной 13:
`_api_admin_client_message` («написать клиенту») и `_api_admin_broadcast` (рассылка)
переехали сюда вместе с модульным `_bot` — его ставит webapp.set_bot веером _BOT_FAN
(волна 12, вариант «б»), полноту веера сторожит tests/test_bot_wiring.py.

Права: экраны CRM закрыты от оператора обычной картой webguard («закрыто по умолчанию» —
`/api/admin/chat_search` и задачи держатся именно на нём, в `_OPERATOR_ROUTES` их не
вписывать). Регистрация маршрутов осталась в make_app строками на фасадные имена.

Тела перенесены ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

from .. import config, db, deskevents, keyboards, notify, orderchat, texts
from ..webauth import _admin_body, _admin_forbidden, _request_is_local, _verify_admin
from ..webguard import _money_visible
from .common import _ago, _avatar_url, _bad, _num, _photo_url, _served_img, _toint
from .ordermedia import _msgs_json, _save_chat_image

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


async def _api_admin_clients(request: web.Request) -> web.Response:
    """Список клиентов (этапы 1–3): агрегаты из заказов, поиск, фильтр по вычисляемым
    сегментам, сортировка LTV/последний заказ/кол-во заказов. В списке есть и посетители
    без заказов (сегмент «Посетитель») — им показываем время последнего визита."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    q = request.query.get("q", "")
    segment = request.query.get("segment", "")
    items = await db.list_customers_crm(search=q, segment=segment,
                                        sort=request.query.get("sort", "last"))
    # счётчики сегментов для чипов — по полному списку (без поиска/фильтра)
    all_items = items if not (q or segment) else await db.list_customers_crm()
    seg_counts: dict[str, int] = {}
    for it in all_items:
        for sgm in it["segments"]:
            seg_counts[sgm] = seg_counts.get(sgm, 0) + 1
    # непрочитанное по клиенту — бейдж в списке настольной админки; считает и сообщения
    # вне заказа, поэтому берётся по user_id, а не через заказы
    unread = await db.clients_unread_admin()
    for it in items:
        it["last_ago"] = _ago(it["last_at"])
        it["seen_ago"] = _ago(it["last_seen_at"])   # «был в боте», см. _crm_seen_map
        it["unread"] = unread.get(it["uid"], 0)
        it["avatar"] = _avatar_url(it["uid"])   # аватарка профиля, если сгенерирована (4f0aa54f)
    return web.json_response({"ok": True, "currency": config.CURRENCY, "clients": items,
                              "segments": db.CRM_SEGMENT_LABELS,
                              "segment_counts": seg_counts, "total": len(all_items)})


async def _api_admin_clients_csv(request: web.Request) -> web.Response:
    """Экспорт клиентов в CSV (этап 6) — тот же приём с BOM, что и экспорт заказов."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.list_customers_crm(limit=100000)
    buf = io.StringIO()
    buf.write("﻿")        # BOM
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ID", "Имя", "Телефон", "Заказов", "Напечатано", "Сумма", "Средний чек",
                "Первый заказ", "Последний заказ", "Последний визит", "Скидка %",
                "Сегменты", "Заметка"])
    for c in items:
        first = datetime.fromtimestamp(c["first_at"]).strftime("%Y-%m-%d") if c["first_at"] else ""
        last = datetime.fromtimestamp(c["last_at"]).strftime("%Y-%m-%d") if c["last_at"] else ""
        seen = (datetime.fromtimestamp(c["last_seen_at"]).strftime("%Y-%m-%d")
                if c["last_seen_at"] else "")
        segs = ", ".join(db.CRM_SEGMENT_LABELS.get(s, s) for s in c["segments"])
        w.writerow([c["uid"], c["name"], c["phone"], c["orders"], c["printed"],
                    f"{c['spent']:.2f}", f"{c['avg_check']:.2f}", first, last, seen,
                    (f"{c['discount_pct']:g}" if c["discount_pct"] is not None else ""),
                    segs, (c["note"] or "").replace("\n", " ")])
    fname = f"clients_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return web.Response(body=buf.getvalue().encode("utf-8"), content_type="text/csv",
                        charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def _api_admin_calc(request: web.Request) -> web.Response:
    """Смета, которую ВИДЕЛ КЛИЕНТ, по id снимка (26.08) — то, что открывается кликом по плитке
    ленты событий. Раньше клик вёл только в карточку клиента, а самой цены мастер не видел
    нигде: смета живёт в памяти процесса и умирает вместе с рестартом бота.

    Снимок могло вытеснить обрезкой (calc_log держит последние ~3000) — тогда честно говорим
    «не сохранился», и фронт откатывается к прежнему поведению."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    cid = _toint(request.query.get("id"))
    c = await db.get_calc_snapshot(cid) if cid else None
    if not c:
        return web.json_response({"ok": False, "error": "Снимок расчёта не сохранился"})
    p = c.get("payload") or {}
    cur = config.CURRENCY
    total = float(c["total"] or 0)
    disc = float(c["discount_pct"] or 0)
    qty = max(1, int(c["qty"] or 1))
    out = {
        "id": c["id"], "at": c["created_at"], "ago": _ago(c["created_at"]),
        "action": c["action"], "source": c["source"],
        "source_label": db.CALC_SOURCE_LABELS.get(c["source"], c["source"]),
        "user_id": c["user_id"], "model_label": c["model_label"], "printer": c["printer"],
        "plastic_name": c["plastic_name"], "color": c["color"], "quality": c["quality"],
        "strength": c["strength"], "qty": qty,
        "grams_total": round(float(c["grams_total"] or 0), 1),
        "time_label": texts.fmt_minutes(float(c["minutes_total"] or 0)),
        "total": round(total, 2), "total_label": texts.fmt_money(total, cur),
        "each_label": texts.fmt_money(total / qty, cur) if qty > 1 else "",
        "total_before_label": (texts.fmt_money(float(c["total_before"] or 0), cur)
                               if disc and c["total_before"] else ""),
        "discount_pct": disc, "approx": bool(c["approx"]), "estimated": bool(c["estimated"]),
        # разбивка — РОВНО та, что видел клиент («Из чего складывается цена» на его экране)
        "lines": p.get("lines") or [], "dims": p.get("dims") or "",
        "plates": p.get("plates") or 1, "scale_pct": p.get("scale_pct") or 100,
        "urgency": p.get("urgency") or "", "term": p.get("term") or "", "eta": p.get("eta") or "",
        "verdict": p.get("verdict") or "ok", "dfm": p.get("dfm") or [],
        "promo": p.get("promo") or None, "purge": p.get("purge") or "",
        "extra_colors": p.get("extra_colors") or 0, "currency": cur,
        "preview": _photo_url("calc", c["id"], c["preview_path"]) or "",
    }
    if c.get("refined_at"):
        rt = float(c["refined_total"] or 0)
        out["refined"] = {
            "total_label": texts.fmt_money(rt, cur), "at": c["refined_at"],
            "ago": _ago(c["refined_at"]),
            "grams": round(float(c["refined_grams"] or 0), 1),
            "time_label": texts.fmt_minutes(float(c["refined_minutes"] or 0)),
            "delta_label": texts.fmt_money(rt - total, cur),
            "delta_pct": round((rt / total - 1) * 100) if total else 0,
        }
    if c["user_id"]:
        card = await db.customer_card(c["user_id"])
        if card:
            out["client"] = {"name": card["name"], "orders": card["orders"],
                             "spent": card["spent"], "avatar": _avatar_url(c["user_id"])}
    nxt = await db.calc_next_request(c["user_id"], c["created_at"], c["model_label"])
    if nxt:
        nxt["ago"] = _ago(nxt["at"])
        nxt["status_label"] = db.REQ_STATUS_LABELS.get(nxt["status"], nxt["status"])
        out["next_order"] = nxt
    return web.json_response({"ok": True, "calc": out})


async def _api_admin_client(request: web.Request) -> web.Response:
    """Карточка клиента (этап 1): агрегаты, ручные поля, скидка, баланс, последние заказы,
    задачи-напоминания по клиенту."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    uid = _toint(request.query.get("uid"))
    card = await db.customer_card(uid)
    if not card:
        return web.json_response({"ok": False, "error": "Клиент не найден"})
    card["last_ago"], card["first_ago"] = _ago(card["last_at"]), _ago(card["first_at"])
    card["seen_ago"] = _ago(card["last_seen_at"])       # «был в боте» — есть и без заказов
    card["avatar"] = _avatar_url(card["uid"])   # аватарка профиля (4f0aa54f)
    for o in card["recent"]:
        o["ago"] = _ago(o["at"])
    card["tasks"] = await db.list_crm_tasks(user_id=uid)
    for t in card["tasks"]:
        t["due_label"] = datetime.fromtimestamp(t["due_at"]).strftime("%d.%m") if t["due_at"] else ""
        t["overdue"] = bool(t["due_at"] and t["due_at"] < time.time() and not t["done"])
    # история зачислений клиента (ccb1e4b6) — мастер видит и аннотирует каждую транзакцию
    card["balance_tx"] = await db.list_balance_tx(uid, 30)
    for tx in card["balance_tx"]:
        tx["at_label"] = datetime.fromtimestamp(tx["at"]).strftime("%d.%m.%Y")
    # семейный счёт (2026-07-16): баланс в KPI уже общий (get_balance), здесь — состав семьи
    fam = await db.get_family(uid)
    if fam:
        fam["tx"] = await db.list_family_tx(fam["id"], 15)
        for tx in fam["tx"]:
            tx["at_label"] = datetime.fromtimestamp(tx["at"]).strftime("%d.%m.%Y")
    card["family"] = fam
    # живая (неоформленная) корзина клиента (a327c3ec) — что он собрал, но не заказал
    cart = await db.get_user_cart(uid)
    if cart:
        cart["updated_label"] = datetime.fromtimestamp(cart["updated_at"]).strftime("%d.%m %H:%M")
    card["cart"] = cart
    return web.json_response({"ok": True, "currency": config.CURRENCY,
                              "segments": db.CRM_SEGMENT_LABELS, "client": card})


async def _api_admin_client_save(request: web.Request) -> web.Response:
    """Ручные CRM-поля клиента: имя-override, телефон, заметка (вариант C — только их и храним)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    uid = _toint(body.get("uid"))
    if not uid:
        return web.json_response({"ok": False, "error": "Нет клиента"})
    fields = {}
    for key, lim in (("name", 80), ("phone", 40), ("note", 500), ("birthday", 5)):
        if body.get(key) is not None:
            fields[key] = str(body.get(key) or "").strip()[:lim]
    # «ДД.ММ» — принимаем только валидную дату, иначе поздравление уедет в никуда
    if fields.get("birthday"):
        if not re.fullmatch(r"(0[1-9]|[12]\d|3[01])\.(0[1-9]|1[0-2])", fields["birthday"]):
            return web.json_response({"ok": False, "error": "День рождения — в виде ДД.ММ"})
    if body.get("no_broadcast") is not None:
        fields["no_broadcast"] = 1 if body.get("no_broadcast") else 0
    await db.upsert_customer(uid, **fields)
    return web.json_response({"ok": True})


async def _api_admin_client_discount(request: web.Request) -> web.Response:
    """Скидка из карточки клиента — переиспользует существующий user_discounts (pct≤0 — снять)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    uid = _toint(body.get("uid"))
    if not uid:
        return web.json_response({"ok": False, "error": "Нет клиента"})
    pct = _num(body, "pct", 0.0) or 0.0
    if pct <= 0:
        await db.delete_user_discount(uid)
        return web.json_response({"ok": True, "pct": None})
    pct = min(100.0, pct)
    await db.set_user_discount(uid, pct, (body.get("name") or "").strip() or None)
    return web.json_response({"ok": True, "pct": pct})


async def _api_admin_client_chat(request: web.Request) -> web.Response:
    """Сплошная переписка с клиентом: треды всех заказов плюс сообщения вне заказа.
    Открытие чата сбрасывает непрочитанное — как в треде заказа."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    uid = _toint(request.query.get("uid"))
    if not uid:
        return web.json_response({"ok": False, "error": "Нет клиента"})
    rows = await db.list_client_messages(uid)
    await db.mark_client_read(uid, by_admin=True)
    return web.json_response({"ok": True, "messages": _msgs_json(rows, mine_is_admin=True)})


async def _api_admin_events(request: web.Request) -> web.Response:
    """События для Windows-тостов настольной админки (этап 3).

    Отдаётся только локально: это зеркало админских уведомлений, наружу ему нечего делать.
    Оболочка передаёт последний виденный id — поэтому её перезапуск не показывает разом
    всё, что накопилось. Без since=... возвращаем только вершину курсора, чтобы первый
    запрос не вывалил на мастера пачку старых тостов.

    Спрашивает ленту тот, кто вошёл в деск (desktop_admin/shell.py берёт токен окна), а не
    «кто-то за этим компьютером», как было до личного входа. Поэтому текст события подбираем
    по правам смотрящего: оператору — тот же вариант без цены, что уходит ему в Telegram.
    """
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    if not _request_is_local(request):
        raise web.HTTPNotFound()
    raw = request.query.get("since")
    if raw is None:
        return web.json_response({"ok": True, "cursor": deskevents.last_id(), "events": []})
    return web.json_response(
        {"ok": True, "cursor": deskevents.last_id(),
         "events": deskevents.since(_toint(raw), money=_money_visible(request))})


async def _api_admin_chat_search(request: web.Request) -> web.Response:
    """Поиск по тексту переписки — командная палитра (Ctrl+K)."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return web.json_response({"ok": True, "hits": []})
    return web.json_response({"ok": True, "hits": await db.search_messages(q, limit=20)})


def _parse_task_due(s: str) -> int:
    """Срок задачи: '' — без срока; '+3'/'3' — через N дней; 'ДД.ММ[.ГГГГ]' — дата (10:00).
    Дата без года в прошлом → следующий год. Непонятный ввод — 0 (без срока)."""
    s = (s or "").strip()
    if not s:
        return 0
    m = re.fullmatch(r"\+?(\d{1,3})", s)
    if m:
        return int(time.time()) + int(m.group(1)) * 86400
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", s)
    if not m:
        return 0
    now = datetime.now()
    try:
        dt = datetime(int(m.group(3)) if m.group(3) else now.year,
                      int(m.group(2)), int(m.group(1)), 10, 0)
    except ValueError:
        return 0
    if not m.group(3) and dt < now - timedelta(days=1):
        dt = dt.replace(year=dt.year + 1)
    return int(dt.timestamp())


async def _api_admin_tasks(request: web.Request) -> web.Response:
    """Задачи-напоминания мастера (этап 4): «сегодня / просрочено / все»."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.list_crm_tasks(include_done=bool(request.query.get("all")))
    names = {c["uid"]: c["name"] for c in await db.list_customers_crm()}
    now = time.time()
    for t in items:
        t["client"] = names.get(t["user_id"], "") if t["user_id"] else ""
        t["due_label"] = datetime.fromtimestamp(t["due_at"]).strftime("%d.%m") if t["due_at"] else ""
        t["overdue"] = bool(t["due_at"] and t["due_at"] < now and not t["done"])
        t["today"] = bool(t["due_at"] and 0 <= t["due_at"] - now < 86400 and not t["done"])
    return web.json_response({"ok": True, "tasks": items})


async def _api_admin_task(request: web.Request) -> web.Response:
    """Создание/закрытие/удаление задачи-напоминания (этап 4)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = (body.get("action") or "add").strip()
    if action == "add":
        title = str(body.get("title") or "").strip()
        if not title:
            return web.json_response({"ok": False, "error": "Что напомнить?"})
        tid = await db.add_crm_task(
            title, due_at=_parse_task_due(str(body.get("due") or "")),
            user_id=_toint(body.get("uid")) or None,
            request_id=_toint(body.get("request_id")) or None)
        return web.json_response({"ok": True, "id": tid})
    tid = _toint(body.get("id"))
    if action == "done":
        return web.json_response({"ok": await db.set_crm_task_done(tid, True)})
    if action == "reopen":
        return web.json_response({"ok": await db.set_crm_task_done(tid, False)})
    if action == "delete":
        await db.delete_crm_task(tid)
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})


async def _api_admin_client_message(request: web.Request) -> web.Response:
    """«Написать клиенту» из карточки (этапы 1 и 3-win-back) — существующий notify_user."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    uid = _toint(body.get("uid"))
    text = str(body.get("text") or "").strip()
    image = body.get("image") or ""
    if not uid or (not text and not image):
        return web.json_response({"ok": False, "error": "Нет клиента или текста"})
    if _bot is None:
        return web.json_response({"ok": False, "error": "Бот недоступен"})
    # Сообщение вне заказа СОХРАНЯЕМ: раньше оно только улетало в Telegram и переписка
    # в админке обрывалась — общий чат клиента показывал дыры.
    mid = await db.add_client_message(uid, from_admin=True, text=text)
    photo_path = await _save_chat_image(mid, image) if image else ""
    # Шапка та же, что у чата заказа (app/orderchat.py): на неё клиент тоже может ответить
    # реплаем — ответ ляжет в переписку вне заказа и придёт команде.
    delivered = await orderchat.notify_client(
        _bot, orderchat.Target(None, uid, True), text[:1500], photo_path,
        reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL), msg_id=mid)
    await db.log_event(uid, "сообщение от мастера", text[:80])
    if not delivered:
        # Сообщение осталось в переписке — мастер видит, что писал, но доставки не было.
        return web.json_response({"ok": False, "saved": True,
                                  "error": "Не доставлено — клиент не запускал бота "
                                           "или заблокировал его"})
    return web.json_response({"ok": True, "id": mid})

async def _api_admin_broadcast(request: web.Request) -> web.Response:
    """Рассылка по сегменту (этап 5). dry=true — только предпросмотр охвата. Безопасные
    дефолты: отписавшиеся и «недавно получавшие» исключены (crm_broadcast_gap_days),
    кнопка «Отписаться» в каждом сообщении, лёгкий троттлинг между отправками."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    segment = str(body.get("segment") or "").strip()
    if segment and segment not in db.CRM_SEGMENT_LABELS:
        return web.json_response({"ok": False, "error": "Неизвестный сегмент"})
    targets = await db.crm_broadcast_targets(segment)
    base = {"total": targets["total"], "eligible": len(targets["eligible"]),
            "skipped_optout": targets["skipped_optout"],
            "skipped_recent": targets["skipped_recent"]}
    # Конструктор контента (6fbed3ad): чекбоксы «Случайная модель из коллекции» и
    # «Что нового в приложении» дополняют свободный текст рассылки.
    opts = body.get("options") if isinstance(body.get("options"), dict) else {}
    whats_new = str(opts.get("whats_new_text") or "").strip()[:600] if opts.get("whats_new") else ""
    item = photo_path = None
    if opts.get("random_model"):
        for it in await db.random_collection_items():
            p = _served_img(it)
            if p and Path(p).exists():
                item, photo_path = it, p
                break
    if body.get("dry"):
        return web.json_response({"ok": True, "dry": True,
                                  "sample": [t["name"] for t in targets["eligible"][:6]],
                                  "attach": {"model": item.label if item else None,
                                             "whats_new": bool(whats_new)}, **base})
    text = str(body.get("text") or "").strip()
    if not (text or whats_new or item):
        return web.json_response({"ok": False, "error": "Пустой текст рассылки"})
    if _bot is None:
        return web.json_response({"ok": False, "error": "Бот недоступен"})
    parts = []
    if text:
        parts.append(html.escape(text[:1000 if item else 1500]))
    if whats_new:
        parts.append("✨ <b>Что нового в приложении</b>\n" + html.escape(whats_new))
    if item:
        price = f" — от {texts.fmt_money(item.last_price, config.CURRENCY)}" \
            if getattr(item, "last_price", 0) else ""
        parts.append(f"🎲 Модель дня: <b>{html.escape(item.label)}</b>{price}")
    full = "📣 " + "\n\n".join(parts)
    if item:
        full = full[:1000]      # лимит подписи к фото в Telegram — 1024 символа
    kb = keyboards.broadcast_footer(config.WEBAPP_PUBLIC_URL,
                                    go="collection" if item else "",
                                    button_text="🗂 Открыть каталог" if item else "Открыть приложение")
    from aiogram.types import FSInputFile
    sent = failed = 0
    for t in targets["eligible"]:
        ok_ = await notify.notify_user(_bot, t["uid"], full, reply_markup=kb,
                                       photo=FSInputFile(photo_path) if photo_path else None)
        if ok_:
            sent += 1
            await db.upsert_customer(t["uid"], last_broadcast_at=int(time.time()))
        else:
            failed += 1
        await asyncio.sleep(0.25)   # бережно к лимитам Telegram; объёмы — десятки клиентов
    await db.log_event(None, "рассылка", f"{db.CRM_SEGMENT_LABELS.get(segment, 'все')}: "
                                         f"доставлено {sent}, недоставлено {failed}")
    return web.json_response({"ok": True, "sent": sent, "failed": failed, **base})
