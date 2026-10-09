"""Общий дом медиа и переписки заказа: подписные файлы, агрегат печатей, JSON сообщений.

Один предмет: как заказ ПОКАЗЫВАЕТ себя наружу — превью и скрин слайсера, таймлапс, медиа
всех печатей журнала и представление переписки для обоих фронтов. Ни денег, ни статусов, ни
отправки сообщений здесь нет: это строительный материал, которым пользуются и клиентская
карточка заказа, и админская, и оба чата.

Выделено из app/webapp.py волной 1 endgame-плана (docs/Перспективные разработки/
refactoring-webapp-endgame-2026-08-12.md). Долг назван предыдущей волной прямо в коде:
app/web/orderbench.py:29-31 — карточка заказа и денежная половина держатся за общих
помощников (`_msgs_json`, `_slicer_url`, `_timelapse_url`, `_order_prints_media`), и пока у
тех не было дома вне webapp, ни один следующий разрез домена заказов был невозможен.

Волна 1 взяла ровно то, что уезжает без модульного состояния webapp; волной 13 сюда же
приехали PDF-документы (`_api_pdf` — смета по токену, `_api_order_pdf` — паспорт заказа),
а с ними и держатель бота: `_bot` ставит webapp.set_bot веером _BOT_FAN (волна 12,
вариант «б», полноту веера сторожит tests/test_bot_wiring.py). `_models` читается из
web/registry (волна 14) — тот же словарь, не копия.

ЧТО ОСТАЛОСЬ В app/webapp.py И ПОЧЕМУ — чтобы следующая волна не считала это недоделкой:
  * ЧИТАТЕЛИ этих помощников (`_api_order`, `_api_admin_order`, оба чата) остаются в
    webapp до своих волн: карточки держатся за конвейер сметы.
  * `_jtl_file` (таймлапс ЗАПИСИ ЖУРНАЛА, /jtl/{jid}) — сосед по смыслу, но его тело читает
    `_heal_journal_timelapse` вместе с админским фото журнала; он уедет с медиатекой журнала,
    а не с медиа ЗАКАЗА. Подписной префикс «jtl» при этом уже живёт здесь в
    `_order_prints_media` — это нормально: подпись считается общим `sig()` из web/common.

МАРШРУТЫ ЖИВУТ ЗДЕСЬ ЖЕ, в таблице `routes` над обработчиками, а make_app подключает их одной
строкой `app.add_routes(ordermedia.routes)` — потерять ручку при переносе нечем. Пути
(/reqprev/{rid}, /slicershot/{rid}, /timelapse/{rid}) не изменились ни на символ: это
ПОДПИСНЫЕ клиентские адреса, они разосланы в сообщениях и лежат в базе — переименование
сломало бы уже выданные ссылки. Прав у путей нет и не было (подпись вместо логина).

Модуль НЕ импортирует webapp — иначе получился бы цикл, ради разрыва которого разрез и
затевался: помощники берутся из своих домов (`app/web/common.py`, `app/websign.py`,
`app/printmedia.py`). app/webapp.py реэкспортирует все девять имён фасадом, поэтому и код,
и тесты находят их как webapp.*.

ДВЕ ПОМЕТКИ СЛЕДУЮЩЕМУ ЧИТАТЕЛЮ:
  * ИМЯ ЛОГГЕРА — "app.web.ordermedia", не "app.webapp": грепать bot.log по-старому нельзя.
    Телеметрия ошибок не теряется: errorlog.TelemetryHandler висит на корневом логгере.
  * ПОДМЕНА В ТЕСТАХ. `monkeypatch.setattr(webapp, "_save_chat_image", …)` до вызывающих
    внутри ЭТОГО модуля не достанет (их тут пока нет, но появятся); патчить оба модуля —
    образец tests/test_attach_print_media.py:345-346 и tests/test_desk_chat_api.py.

Тела функций перенесены ДОСЛОВНО, с одним отличием волны 13: ленивый `from . import quote`
внутри PDF-ручек поднялся на `from .. import quote` — модуль теперь в app/web/
(закреплено фасадным тестом, образец — mesh3d/notifhub).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path

from aiohttp import web

from .. import config, db, delivery, preview, pricing
from ..printmedia import _file_size, _heal_journal_timelapse
from ..webauth import _verify_init_data
from ..websign import _file_ver
from .common import _bad, _img_headers, _maybe_webp, _photo_url, _toint, check_sig, sig, signed_url
from .registry import _models

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev

log = logging.getLogger(__name__)


# Маршруты блока объявляются рядом с обработчиком, а не списком в make_app: при переносе
# блока в отдельный модуль строка регистрации уезжает вместе с ручкой и потеряться не может.
# Канонический путь ресурса тот же, поэтому _usage_mw/_error_mw читают его как раньше.
routes = web.RouteTableDef()


@routes.get("/reqprev/{rid}")
async def _request_preview(request: web.Request) -> web.StreamResponse:
    rid = request.match_info.get("rid", "")
    if not check_sig(request, "req", rid):
        raise web.HTTPNotFound()
    r = await db.get_request(_toint(rid))
    if r and r.preview_path and Path(r.preview_path).exists():
        served = await _maybe_webp(request, Path(r.preview_path))
        return web.FileResponse(served, headers=_img_headers(request, private=True))
    raise web.HTTPNotFound()


def _slicer_url(r) -> str:
    """Подписанный URL скрина из слайсера, приложенного к счёту индивидуального заказа
    (OpenTodo 6aaa6a19). Виден и админу, и клиенту. Пусто, если скрина нет."""
    path = getattr(r, "slicer_shot", None)
    if path and Path(path).exists():
        return signed_url("slicer", r.id, {"v": _file_ver(path)})
    return ""


@routes.get("/slicershot/{rid}")
async def _slicer_shot_file(request: web.Request) -> web.StreamResponse:
    rid = request.match_info.get("rid", "")
    if not check_sig(request, "slicer", rid):
        raise web.HTTPNotFound()
    r = await db.get_request(_toint(rid))
    path = getattr(r, "slicer_shot", None) if r else None
    if path and Path(path).exists():
        served = await _maybe_webp(request, Path(path))
        return web.FileResponse(served, headers=_img_headers(request, private=True))
    raise web.HTTPNotFound()


def _timelapse_url(r) -> str:
    """Подписанный URL таймлапса печати, привязанного к заказу (OpenTodo 70b5a590). Виден клиенту
    в приложении (для больших видео, не влезших в лимит Telegram). Пусто, если таймлапса нет."""
    path = getattr(r, "timelapse", None)
    if path and Path(path).exists():
        return signed_url("tl", r.id, {"v": _file_ver(path)})
    return ""


@routes.get("/timelapse/{rid}")
async def _timelapse_file(request: web.Request) -> web.StreamResponse:
    rid = request.match_info.get("rid", "")
    if not check_sig(request, "tl", rid):
        raise web.HTTPNotFound()
    r = await db.get_request(_toint(rid))
    path = getattr(r, "timelapse", None) if r else None
    if path and Path(path).exists():
        return web.FileResponse(path)
    raise web.HTTPNotFound()


async def _order_prints_media(rid: int, *, for_admin: bool = False) -> tuple[list[dict], set[int]]:
    """Медиа ВСЕХ печатей заказа: на многостольный заказ печатей несколько, и таймлапс со скрином
    стола копятся per-печать в print_journal — заказ показывает их агрегатом, а не одну штуку.
    URL подписные (/jtl и /admin/photo/journal проверяют только подпись), поэтому годятся и
    клиенту. Вторым значением — размеры файлов таймлапсов: одиночный requests.timelapse
    исторически является копией одного из журнальных видео, по размеру его отсекают от дубля.
    Клиенту полностью бракованные печати не показываем (мастер их перепечатывает)."""
    items, sizes = [], set()
    for e in await db.list_print_journal_for_request(rid):
        if not for_admin and float(e.get("fail_pct") or 0) >= 100:
            continue
        # пустое поле лечится из авто-кэша (кейс «Овечка Тимми», 2bc57e3e): видео могло
        # выгрузиться на ПК уже после того, как запись осталась без привязки
        tl = await _heal_journal_timelapse(e)
        tl_ok = bool(tl)
        ph_url = _photo_url("journal", e["id"], e.get("done_photo")) or ""
        if not (tl_ok or ph_url):
            continue
        if tl_ok:
            sizes.add(_file_size(tl))
        items.append({"jid": e["id"],
                      "timelapse": (signed_url("jtl", e["id"]) if tl_ok else ""),
                      "photo": ph_url, "label": e.get("label") or "",
                      "minutes": round(float(e.get("minutes") or 0))})
    return items, sizes


def _chat_photo_url(msg: dict) -> str:
    """Подписанный URL вложения сообщения (пустая строка, если фото нет)."""
    if not msg or not msg.get("image_path"):
        return ""
    mid = msg["id"]
    return f"/admin/photo/chat/{mid}?s={sig('aphoto', 'chat' + str(mid))}"


def _msgs_json(rows: list, mine_is_admin: bool) -> list:
    """Сообщения для UI: mine=True у своих (клиент видит свои справа, чужие — слева).

    id и request_id нужны настольной админке: по ним рисуются вложения и разделители
    «— по заказу #N —» в сплошном чате клиента.

    `read` — прочитал ли ПОЛУЧАТЕЛЬ (фронт показывает галочку только у своих сообщений).
    Это НАША отметка, а не телеграмная: Bot API не сообщает боту, что человек прочитал
    сообщение в чате. Она ставится, когда вторая сторона открыла переписку в приложении
    или ответила/поставила реакцию из Telegram, — то есть означает «увидел», а не
    «сообщение доставлено».

    `reply` — на что отвечают (реакция или ответ реплаем): короткая цитата, чья она и было
    ли там фото. None — обычное сообщение."""
    out = []
    for m in rows:
        quoted = None
        if m.get("reply_to") and (m.get("reply_text") or m.get("reply_image")):
            quoted = {"id": m["reply_to"],
                      "text": (m.get("reply_text") or "")[:120],
                      "image": bool(m.get("reply_image")),
                      "mine": bool(m.get("reply_from_admin")) == mine_is_admin}
        out.append({"id": m["id"], "request_id": m.get("request_id"), "text": m["text"],
                    "image": _chat_photo_url(m),
                    "order_label": m.get("order_label") or "",
                    "mine": bool(m["from_admin"]) == mine_is_admin,
                    "read": bool(m.get("read_user") if m["from_admin"] else m.get("read_admin")),
                    "reply": quoted,
                    "ts": m["at"],
                    "at": time.strftime("%d.%m %H:%M", time.localtime(m["at"]))})
    return out


async def _save_chat_image(msg_id: int, data: str) -> str:
    """Сохранить вложение сообщения. data — dataURL/base64. Возвращает путь либо ''."""
    if "," in data and data[:5].lower() == "data:":
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001
        raw = b""
    if not raw or len(raw) > 12 * 1024 * 1024:
        return ""
    base_dir = config.COLLECTION_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    dest = base_dir / f"chat_{msg_id}.jpg"
    try:
        dest.write_bytes(raw)
        await asyncio.to_thread(preview.optimize_photo, dest)
    except Exception:  # noqa: BLE001
        log.warning("save chat photo failed", exc_info=True)
        return ""
    await db.set_message_image(msg_id, str(dest))
    return str(dest)


async def _api_pdf(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    m = _models.get(body.get("token", ""))
    if not m or not m.get("pdf"):
        return web.json_response({"ok": False, "error": "Сначала сделайте расчёт"})
    if _bot is None:
        return web.json_response({"ok": False, "error": "бот недоступен"})
    p = m["pdf"]
    ts = await db.get_text_settings()
    from aiogram.types import FSInputFile

    from .. import quote
    try:
        pv = Path(p["preview_path"]) if p.get("preview_path") else None
        pdf = await asyncio.to_thread(
            quote.build_quote, business_name=ts.get("business_name", ""),
            business_contact=ts.get("business_contact", ""), model_label=p["model_label"],
            printer_name=p["printer_name"], plastic_name=p["plastic_name"], qty=p["qty"],
            quality_label=p["quality_label"], strength_label=p["strength_label"], bd=p["bd"],
            cur=config.CURRENCY, settings=p["settings"], preview_path=pv, plates=p["plates"])
        await _bot.send_document(uid, FSInputFile(str(pdf)), caption="📄 Смета на 3D-печать")
    except Exception:  # noqa: BLE001
        log.warning("web pdf failed", exc_info=True)
        return web.json_response({"ok": False, "error": "Не удалось сформировать PDF"})
    return web.json_response({"ok": True})

async def _api_order_pdf(request: web.Request) -> web.Response:
    """PDF-смета к существующему заказу из снимка requests.breakdown. Доступ — владелец заказа
    или админ; документ уходит в чат запросившему (как и живая смета /api/pdf)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if _bot is None:
        return web.json_response({"ok": False, "error": "бот недоступен"})
    try:
        rid = int(body.get("id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad id"}, status=400)
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    if r.user_id != int(uid) and not config.is_admin(int(uid)):
        return web.json_response({"ok": False, "error": "Доступ запрещён"}, status=403)
    bd = pricing.breakdown_from_dict(r.breakdown)
    if bd is None:
        return web.json_response({"ok": False,
                                  "error": "Для этого заказа смета недоступна (старый заказ без снимка расчёта)."})
    ts = await db.get_text_settings()
    # Доставка (A2) в смете: она одна на заказ-корзину, поэтому строку получает ТОЛЬКО
    # якорная позиция группы — та же, что несёт доставку в онлайн-счёте (app/payments.py).
    # Иначе клиент увидел бы доставку в смете каждой позиции и решил, что платит N раз.
    dcost, dlabel = 0.0, ""
    _gid = (getattr(r, "order_group", "") or "").strip()
    if _gid:
        _ent = await db.get_order_group(_gid)
        if delivery.group_delivery_cost(_ent) > 0 and await db.group_anchor_request_id(_gid) == r.id:
            dcost = delivery.group_delivery_cost(_ent)
            dlabel = (_ent or {}).get("delivery_type_name") or "доставка заказа"
    from aiogram.types import FSInputFile

    from .. import quote
    try:
        pv = Path(r.preview_path) if r.preview_path else None
        # Плата за разработку модели (6aaa6a19) живёт колонкой requests.dev_fee, а не в снимке
        # разбивки, поэтому её нужно передать отдельно — иначе ИТОГО сметы разойдётся с ценой
        # заказа и с расчётником приложения (texts.order_breakdown прибавляет её так же).
        pdf = await asyncio.to_thread(
            quote.build_quote, business_name=ts.get("business_name", ""),
            business_contact=ts.get("business_contact", ""), model_label=r.model_label,
            printer_name=r.printer, plastic_name=r.plastic_name, qty=r.quantity,
            quality_label=r.quality, strength_label=r.strength, bd=bd, cur=config.CURRENCY,
            settings=pricing.settings_from_breakdown(r.breakdown), preview_path=pv, plates=r.plates,
            delivery_cost=dcost, delivery_label=dlabel,
            dev_fee=float(getattr(r, "dev_fee", 0.0) or 0.0))
        await _bot.send_document(int(uid), FSInputFile(str(pdf)), caption=f"📄 Смета на заказ #{rid}")
    except Exception:  # noqa: BLE001
        log.warning("web order pdf failed", exc_info=True)
        return web.json_response({"ok": False, "error": "Не удалось сформировать PDF"})
    return web.json_response({"ok": True})
