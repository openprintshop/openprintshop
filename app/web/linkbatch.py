"""Пакетный расчёт ссылок для клиента (OpenTodo 89251db6).

Клиенты шлют мастеру в личку пачку ссылок одним сообщением. Мастер выбирает клиента, кидает
все ссылки разом — бот считает каждую (дефолтные пластик/качество/прочность, 1 шт), мастер
просматривает результат и одним нажатием отправляет клиенту расчёт каждого товара.

Выделено из app/webapp.py волной 13 endgame-плана. Правила дома:
* модуль НЕ имеет права импортировать app.webapp (разрез односторонний; проверяется тестом
  в отдельном процессе);
* `_batches` мутируется ПО МЕСТУ и не пере-присваивается — фасад webapp реэкспортирует
  ТОТ ЖЕ словарь; `_BATCH_MAX_LINKS`/`_BATCH_KEEP` — плоские константы, подменять здесь;
* бота ставит webapp.set_bot веером _BOT_FAN (волна 12, вариант «б»): ветки отправки
  закрыты `if _bot is None` и у студии с выключенной мини-аппой молчат, как и раньше;
* регистрация маршрутов осталась в make_app строками на фасадные имена;
* тела перенесены ДОСЛОВНО (сверка против git show HEAD:app/webapp.py).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path

from aiohttp import web

from .. import cache, calcservice, config, cpupool, db, downloader, inspect3mf, keyboards, notify, texts
from ..webauth import _admin_body, _admin_forbidden, _verify_admin
from .common import _bad, _toint, check_sig, sig

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`

_batches: dict[str, dict] = {}          # bid -> {admin, user_id, items, done, started}
_BATCH_MAX_LINKS = 15
_BATCH_KEEP = 10                        # сколько последних пакетов держим в памяти


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


def _extract_links(text: str) -> list[str]:
    """Все http(s)-ссылки из свободного текста, без дублей, в порядке появления."""
    out: list[str] = []
    for m in re.finditer(r"https?://[^\s<>\"']+", text or ""):
        u = m.group(0).rstrip(".,;)»>")
        if u not in out:
            out.append(u)
    return out[:_BATCH_MAX_LINKS]

async def _batch_defaults(plastic_id: int | None = None):
    """Параметры пакетного расчёта: принтер/качество/прочность по умолчанию; пластик — выбранный
    мастером (OpenTodo fcf355eb) или дефолтный."""
    printer = await db.get_default_printer()
    plastic = await db.get_plastic(plastic_id) if plastic_id else None
    if plastic is None:
        plastics = await db.list_plastics(active_only=True)
        plastic = next((p for p in plastics if p.is_default), plastics[0] if plastics else None)
    quals = await db.list_quality(active_only=True)
    quality = next((q for q in quals if q.is_default), quals[0] if quals else None)
    strs = await db.list_strength(active_only=True)
    strength = next((s for s in strs if s.is_default), strs[0] if strs else None)
    return printer, plastic, quality, strength

async def _run_batch(bid: str) -> None:
    """Фоновый прогон пакета: скачиваем и считаем КАЖДУЮ ссылку последовательно (слайсер и так
    ограничен своим семафором; последовательность держит машину отзывчивой). Ошибки по ссылке
    не валят пакет — у позиции появляется status='err' с честным текстом."""
    b = _batches.get(bid)
    if not b:
        return
    printer, plastic, quality, strength = await _batch_defaults(b.get("plastic_id"))
    if not (printer and plastic and quality and strength):
        for it in b["items"]:
            it.update(status="err", error="Не настроены принтер/пластик/пресеты по умолчанию")
        b["done"] = True
        return
    for it in b["items"]:
        if b.get("cancel"):                          # мастер нажал «Отменить» (OpenTodo fcf355eb)
            it.update(status="err", error="Отменено")
            continue
        it["status"] = "run"
        try:
            meshes, label = await downloader.fetch_from_url(it["url"])
            paths = [str(p) for p in meshes]
            info = await cpupool.run(inspect3mf.inspect_model, paths)
            res = await calcservice.compute(
                user_id=b["user_id"], model_paths=paths, model_label=label, printer=printer,
                plastic=plastic, color="", qty=1, quality=quality, strength=strength,
                extra_colors=0, plates=info.plates, is_multicolor=info.is_multicolor,
                save=False)
            bd = res["bd"]
            it.update(status="ok", label=label, price=bd.total,
                      grams=round(bd.grams_total, 1), minutes=round(bd.minutes_total),
                      preview=res.get("preview_path") or "", paths=paths,
                      plastic_name=res["pdisp"], dims=res.get("dims", ""),
                      plates=info.plates, multicolor=int(info.is_multicolor))
        except Exception as exc:  # noqa: BLE001
            log.warning("batch item failed: %s", it["url"], exc_info=True)
            it.update(status="err", error=str(exc) or "Не удалось посчитать")
    b["done"] = True
    if b.get("cancel"):
        return                                       # отменён — без алерта
    ok = sum(1 for x in b["items"] if x["status"] == "ok")
    await notify.alert(f"Пакетный расчёт готов: {ok}/{len(b['items'])} ссылок посчитано — "
                       f"откройте «Заказы → Пакет ссылок» и отправьте клиенту.",
                       key=f"batch_{bid}")

def _batch_json(bid: str, b: dict) -> dict:
    items = []
    for i, it in enumerate(b["items"]):
        row = {"idx": i, "url": it["url"], "status": it["status"],
               "label": it.get("label", ""), "error": it.get("error", "")}
        if it["status"] == "ok":
            row.update({"price": it["price"], "grams": it["grams"], "minutes": it["minutes"],
                        "plastic": it.get("plastic_name", ""), "plates": it.get("plates", 1),
                        "preview": (f"/batchprev/{bid}/{i}?s={sig('batch', bid + str(i))}"
                                    if it.get("preview") else "")})
        items.append(row)
    return {"ok": True, "bid": bid, "user_id": b["user_id"], "user_name": b.get("user_name", ""),
            "plastic_id": b.get("plastic_id"), "links_raw": b.get("links_raw", ""),
            "cancelled": bool(b.get("cancel")), "sent": bool(b.get("sent")),
            "done": b["done"], "items": items, "currency": config.CURRENCY}

async def _api_admin_batch_calc(request: web.Request) -> web.Response:
    """Старт пакетного расчёта: клиент + текст со ссылками. Возвращает bid для опроса статуса."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    aid, err = _admin_body(body)
    if err:
        return err
    target = _toint(body.get("user_id"))
    if not target:
        return web.json_response({"ok": False, "error": "Укажите клиента"})
    links = _extract_links(body.get("links") or "")
    if not links:
        return web.json_response({"ok": False, "error": "В тексте не нашлось ни одной ссылки"})
    bid = uuid.uuid4().hex[:10]
    _batches[bid] = {"admin": aid, "user_id": target, "user_name": (body.get("user_name") or "").strip(),
                     "plastic_id": _toint(body.get("plastic_id")) or None,
                     "links_raw": (body.get("links") or "")[:4000],
                     "items": [{"url": u, "status": "wait"} for u in links],
                     "done": False, "started": time.time()}
    while len(_batches) > _BATCH_KEEP:                       # не копим старые пакеты бесконечно
        _batches.pop(next(iter(_batches)))
    asyncio.get_running_loop().create_task(_run_batch(bid))
    return web.json_response({"ok": True, "bid": bid, "count": len(links)})

async def _api_admin_batch_status(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    bid = request.query.get("bid", "")
    b = _batches.get(bid)
    if not b:
        return web.json_response({"ok": False, "error": "Пакет не найден (возможно, бот перезапускался)"})
    return web.json_response(_batch_json(bid, b))

async def _api_admin_batch_last(request: web.Request) -> web.Response:
    """Последний НЕотправленный пакет — для восстановления формы после закрытия приложения
    (OpenTodo fcf355eb): мини-апп убивается при выходе, а пакет живёт на сервере."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    for bid in reversed(list(_batches.keys())):
        b = _batches[bid]
        if not b.get("sent") and not b.get("cancel"):
            return web.json_response(_batch_json(bid, b))
    return web.json_response({"ok": True, "bid": None})

async def _api_admin_batch_cancel(request: web.Request) -> web.Response:
    """Отмена пакета (OpenTodo fcf355eb): оставшиеся ссылки не считаются; текущая ссылка
    дорабатывает (слайсер не прерываем), уведомление о готовности не шлётся."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _aid, err = _admin_body(body)
    if err:
        return err
    b = _batches.get(body.get("bid") or "")
    if not b:
        return web.json_response({"ok": False, "error": "Пакет не найден"})
    b["cancel"] = True
    return web.json_response({"ok": True})

async def _batch_preview(request: web.Request) -> web.StreamResponse:
    bid = request.match_info.get("bid", "")
    idx = _toint(request.match_info.get("idx"), -1)
    if not check_sig(request, "batch", f"{bid}{idx}"):
        raise web.HTTPNotFound()
    b = _batches.get(bid)
    it = (b["items"][idx] if b and 0 <= idx < len(b["items"]) else None) or {}
    p = it.get("preview")
    if p and Path(p).exists():
        return web.FileResponse(p, headers={"Cache-Control": "max-age=600"})
    raise web.HTTPNotFound()

async def _api_admin_batch_send(request: web.Request) -> web.Response:
    """Одобрение пакета мастером: выбранные позиции уходят клиенту — расчёт каждого товара
    отдельным сообщением с превью, плюс шапка; модели сохраняются клиенту в «Мои модели»,
    чтобы он мог открыть любую и оформить заказ в пару тапов."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _aid, err = _admin_body(body)
    if err:
        return err
    b = _batches.get(body.get("bid") or "")
    if not b:
        return web.json_response({"ok": False, "error": "Пакет не найден (возможно, бот перезапускался)"})
    if not b["done"]:
        return web.json_response({"ok": False, "error": "Пакет ещё считается"})
    include = body.get("include")
    idxs = ({_toint(x, -1) for x in include} if isinstance(include, list)
            else set(range(len(b["items"]))))
    chosen = [it for i, it in enumerate(b["items"]) if it["status"] == "ok" and i in idxs]
    if not chosen:
        return web.json_response({"ok": False, "error": "Нет посчитанных позиций для отправки"})
    if _bot is None:
        return web.json_response({"ok": False, "error": "Бот не запущен"})
    uid = b["user_id"]
    printer, plastic, quality, strength = await _batch_defaults()
    from aiogram.types import FSInputFile
    try:
        await _bot.send_message(
            uid, f"🧮 <b>Мастер посчитал ваши ссылки</b> — {len(chosen)} шт.\n"
                 f"Модели уже в «Мои модели»: откройте приложение, чтобы заказать.",
            reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="models"))
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"Не удалось написать клиенту: {exc}"})
    sent = 0
    for it in chosen:
        try:
            await db.upsert_saved_model(
                user_id=uid, hash=cache.model_hash(it["paths"]), label=it["label"],
                paths=it["paths"], preview_path=it.get("preview") or None,
                printer_id=printer.id if printer else None,
                plastic_id=plastic.id if plastic else None,
                quality_id=quality.id if quality else None,
                strength_id=strength.id if strength else None,
                quantity=1, multicolor=int(it.get("multicolor", 0)), plates=int(it.get("plates", 1)))
        except Exception:  # noqa: BLE001
            log.debug("batch upsert saved model failed", exc_info=True)
        cap = (f"«{it['label']}»\n⚖️ {it['grams']:g} г · ⏱ {texts.fmt_minutes(it['minutes'])} · "
               f"{it.get('plastic_name', '')}\n💰 {texts.fmt_money(it['price'], config.CURRENCY)}")
        try:
            pv = it.get("preview")
            if pv and Path(pv).exists():
                await _bot.send_photo(uid, FSInputFile(pv), caption=cap)
            else:
                await _bot.send_message(uid, cap)
            sent += 1
        except Exception:  # noqa: BLE001
            log.debug("batch item send failed", exc_info=True)
    await db.log_event(uid, "получил пакетный расчёт", f"{sent} поз. от мастера")
    b["sent"] = True                               # batch_last больше не восстанавливает его
    return web.json_response({"ok": True, "sent": sent})
