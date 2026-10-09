"""Центр уведомлений админки и семейные счета.

Один предмет, хотя выглядит как два: клиентские заявки семейного счёта (пикер партнёра,
запрос по нику) СОЗДАЮТ уведомления админки, а мастер резолвит их (и остальные виды)
кнопками в /api/admin/notification_action — домен режется только вместе.

Выделено из app/webapp.py волной 13 endgame-плана. Модуль держит `_bot` (веер _BOT_FAN,
волна 12, вариант «б»): и заявки, и вердикты мастера уходят клиентам в Telegram; ветки
отправки закрыты `if _bot is None` и у студии с выключенной мини-аппой молчат, как раньше.

Правила дома: НЕ импортировать app.webapp (разрез односторонний, проверяется тестом в
отдельном процессе); регистрация маршрутов осталась в make_app строками на фасадные
имена; тела перенесены ДОСЛОВНО (сверка против git show HEAD:app/webapp.py) с одним
отличием: ленивый `from . import avatars` внутри _api_admin_notification_action поднялся
на `from .. import avatars` — модуль теперь в app/web/ (закреплено фасадным тестом,
образец — mesh3d).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from aiohttp import web

from .. import config, db, keyboards, notify, ratelimit
from ..webauth import _admin_body, _admin_forbidden, _verify_admin, _verify_user
from .common import _ago, _bad, _photo_url, _toint

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


async def _api_family_pick_partner(request: web.Request) -> web.Response:
    """Кнопка «Выбрать из контактов» в мини-аппе (OpenTodo 0528ba47): шлём клиенту в чат
    reply-клавиатуру с нативным пикером Telegram (request_users) — выбранный контакт придёт
    users_shared-хендлеру с точным user_id партнёра."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    uid = int(u["id"])
    if await db.family_of(uid) is not None:
        return web.json_response({"ok": False, "error": "У вас уже есть семейный счёт"})
    if _bot is None:
        return web.json_response({"ok": False, "error": "Бот недоступен — попробуйте позже"})
    try:
        await _bot.send_message(
            uid, "Выберите близкого, с кем открыть <b>общий семейный счёт</b> — кнопкой ниже. "
                 "Нужен человек из ваших контактов/чатов Telegram.",
            reply_markup=keyboards.family_pick_partner())
    except Exception:  # noqa: BLE001
        log.debug("family pick prompt failed", exc_info=True)
        return web.json_response({"ok": False, "error": "Не удалось отправить сообщение в чат"})
    return web.json_response({"ok": True})

async def _api_family_request(request: web.Request) -> web.Response:
    """Семейный счёт (редизайн 2026-07-16): клиент из профиля просит открыть ОБЩИЙ счёт с
    близкими. Заявка падает в центр уведомлений админки (кнопки «Создать счёт»/«Отклонить»),
    плюс телеграм-пинг админам со ссылкой на раздел."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    uid = int(u["id"])
    if not ratelimit.allow(f"family:{uid}", 3, 3600):
        return web.json_response({"ok": False, "error": "Заявка уже отправлена — мастер свяжется"})
    if await db.family_of(uid) is not None:
        return web.json_response({"ok": False, "error": "У вас уже есть семейный счёт"})
    partner = (body.get("partner") or "").strip()[:120]
    uname = ("@" + u["username"]) if u.get("username") else \
        (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {uid}")
    await db.log_event(uid, "запросил семейный счёт", partner, user_name=uname)
    # непросмотренная заявка от этого же клиента уже висит — не плодим дубли карточек
    dup = [n for n in await db.list_admin_notifications(kind="family_request")
           if n["payload"].get("from_id") == uid]
    body_txt = f"От {uname}" + (f", партнёр: {partner}" if partner else "")
    if dup:
        # Уточняем висящую карточку вместо молчаливого игнора нового имени (иначе мастер видит
        # одного партнёра, а клиент уверен, что заказал другого). НО не понижаем точность: если
        # там уже лежит partner_id из нативного пикера, текстовое имя его не перетирает —
        # именно неточное имя фронт резолвит подстрочным матчем и может попасть не в того
        # человека, а подтверждение УДАЛЯЕТ его баланс и вливает в чужую семью.
        old = dup[0]["payload"] or {}
        if not old.get("partner_id") and partner:
            new_payload = dict(old)
            new_payload["partner"] = partner
            await db.update_admin_notification(dup[0]["id"], body=body_txt, payload=new_payload)
        return web.json_response({"ok": True})
    await db.add_admin_notification(
        "family_request", title="Заявка на семейный счёт", body=body_txt,
        payload={"from_id": uid, "from_name": uname, "partner": partner})
    if _bot is not None:
        try:
            await notify.notify_admins(
                _bot, f"👨‍👩‍👧 <b>Заявка на семейный счёт</b>\nОт: {html.escape(uname)} (id {uid})\n"
                      f"Партнёр: {html.escape(partner or 'не указан')}",
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminfamilies",
                                                text="Открыть заявки"))
        except Exception:  # noqa: BLE001
            log.debug("family request notify failed", exc_info=True)
    return web.json_response({"ok": True})

def _cust_display(c: dict) -> str:
    return c.get("name") or f"id {c.get('uid')}"

async def _api_admin_notifications(request: web.Request) -> web.Response:
    """Центр уведомлений: черновики release notes и будущие типы. Семейные заявки живут
    в своём разделе (/api/admin/families, 2026-07-17). ?all=1 — вместе с обработанными."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.list_admin_notifications(include_done=bool(request.query.get("all")),
                                              exclude_kind="family_request")
    for it in items:
        it["ago"] = _ago(it["created_at"])
        it["image_url"] = _photo_url("notif", it["id"], it.pop("image_path", "")) or ""
    return web.json_response({"ok": True, "items": items})

async def _api_admin_families(request: web.Request) -> web.Response:
    """Раздел «Семейные счета»: заявки клиентов с кнопками «Создать»/«Отклонить»,
    существующие семьи, список клиентов для селектов. ?all=1 — с обработанными заявками."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    requests_ = await db.list_admin_notifications(include_done=bool(request.query.get("all")),
                                                  kind="family_request")
    for it in requests_:
        it["ago"] = _ago(it["created_at"])
        it.pop("image_path", None)
    customers = [{"uid": c["uid"], "name": _cust_display(c), "orders": c["orders"]}
                 for c in await db.list_customers()]
    return web.json_response({"ok": True, "currency": config.CURRENCY, "requests": requests_,
                              "families": await db.list_families(), "customers": customers})

async def _api_admin_family_create(request: web.Request) -> web.Response:
    """Открыть семейный счёт: body {member_ids: [uid, ...], notif_id?}. Личные балансы
    участников переносятся в общий + бонус за открытие; каждому уходит уведомление в бота."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    ids = [x for x in (_toint(i) for i in (body.get("member_ids") or [])) if x and x > 0]
    if len(set(ids)) < 2:
        return web.json_response({"ok": False, "error": "Выберите минимум двух клиентов"})
    nid = _toint(body.get("notif_id"))
    notif = await db.get_admin_notification(nid) if nid else None
    # имена: из списка клиентов, для заявителя БЕЗ заказов — из payload заявки (клиент
    # мог отправить заявку до первого заказа, это не повод отказывать в семейном счёте)
    by_uid = {c["uid"]: c for c in await db.list_customers()}
    pl = (notif or {}).get("payload") or {}

    def _name_of(i: int) -> str:
        if i in by_uid:
            return _cust_display(by_uid[i])
        if pl.get("from_id") == i and pl.get("from_name"):
            return str(pl["from_name"])[:80]
        # Партнёр из нативного пикера тоже может быть без заказов (в том и смысл фичи), а его
        # имя лежит в payload.partner. Без этой ветки он навсегда записывался в
        # family_members.user_name как «id N» — переименования у него нет, и этот «id N»
        # потом светился в каждой строке family_tx и в сообщении «Участники: …» всем членам.
        if pl.get("partner_id") == i and pl.get("partner"):
            return str(pl["partner"])[:80]
        return f"id {i}"

    members = [(i, _name_of(i)) for i in dict.fromkeys(ids)]
    try:
        fam = await db.create_family(members, opening_bonus=config.FAMILY_OPENING_BONUS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)})
    if notif:
        await db.update_admin_notification(nid, status="done")
    names = ", ".join(m[1] for m in members)
    await db.log_event(_uid, "открыл семейный счёт", names)
    if _bot is not None:
        # «подарок» обещаем только если бонус реально начислен (не съеден гардом
        # уже выплаченной рефералки внутри пары)
        gift = f"\nПодарок за открытие: +{config.FAMILY_OPENING_BONUS:g} " \
               f"{config.CURRENCY} на общий счёт 🎁" if fam.get("bonus_granted") else ""
        for m_uid, _n in members:
            try:
                await notify.notify_user(
                    _bot, m_uid,
                    f"👨‍👩‍👧 <b>Вам открыт семейный счёт</b>\nУчастники: {html.escape(names)}.\n"
                    f"Баланс теперь общий — в профиле видно, чей заказ и какое списание."
                    f"{gift}",
                    reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="profile",
                                                    text="Открыть профиль"))
            except Exception:  # noqa: BLE001
                log.debug("family member notify failed", exc_info=True)
    return web.json_response({"ok": True, "family": fam})

async def _api_admin_family_add(request: web.Request) -> web.Response:
    """Добавить участника в существующую семью: body {family_id, user_id}."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    fam_id, new_uid = _toint(body.get("family_id")), _toint(body.get("user_id"))
    by_uid = {c["uid"]: c for c in await db.list_customers()}
    if new_uid not in by_uid:
        return web.json_response({"ok": False, "error": "Клиент не из списка заказов"})
    try:
        fam = await db.add_family_member(fam_id, new_uid, _cust_display(by_uid[new_uid]))
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)})
    await db.log_event(_uid, "добавил в семейный счёт", _cust_display(by_uid[new_uid]))
    if _bot is not None:
        try:
            await notify.notify_user(
                _bot, new_uid, "👨‍👩‍👧 <b>Вас добавили в семейный счёт</b>\n"
                               "Баланс теперь общий — подробности в профиле.",
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="profile",
                                                text="Открыть профиль"))
        except Exception:  # noqa: BLE001
            log.debug("family member notify failed", exc_info=True)
    return web.json_response({"ok": True, "family": fam})

async def _api_admin_family_remove(request: web.Request) -> web.Response:
    """Исключить участника из семьи (исправление ошибочного добавления): body
    {family_id, user_id}. Останется один человек — семья распускается, остаток
    общего счёта вернётся ему на личный баланс."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    try:
        fam = await db.remove_family_member(_toint(body.get("family_id")),
                                            _toint(body.get("user_id")))
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)})
    await db.log_event(_uid, "исключил из семейного счёта", f"id {body.get('user_id')}")
    return web.json_response({"ok": True, "family": fam, "dissolved": fam is None})

async def _api_admin_notification_action(request: web.Request) -> web.Response:
    """Действия над уведомлением: body {id, action, title?, body?, prompt?}.
    reject — отклонить заявку; save — правка текста release note; delete — удалить
    (вместе с картинкой); image_regen — сгенерировать картинку по промту (цепочка
    аватарок, промт запоминается в payload.gen_prompt); publish — разослать release
    note всем клиентам (кроме отписавшихся) с картинкой, пометить published."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    nid, action = _toint(body.get("id")), str(body.get("action") or "")
    n = await db.get_admin_notification(nid)
    if not n:
        return web.json_response({"ok": False, "error": "Уведомление не найдено"})
    if action == "reject":
        await db.update_admin_notification(nid, status="rejected")
        return web.json_response({"ok": True})
    if action == "save":
        # пустая строка — тоже валидная правка (иначе стёртый заголовок молча
        # откатывался бы к старому и фронт застревал в вечном «не сохранено»)
        title = body.get("title") if body.get("title") is not None else n["title"]
        await db.update_admin_notification(
            nid, title=str(title).strip()[:120],
            body=str(body.get("body") or "").strip()[:4000])
        return web.json_response({"ok": True})
    if action == "delete":
        img = await db.delete_admin_notification(nid)
        if img:
            try:
                config.abs_data(img).unlink(missing_ok=True)   # C2: путь из БД → абсолютный
            except OSError:
                pass
        return web.json_response({"ok": True})
    if action == "image_del":
        if n["image_path"]:
            try:
                config.abs_data(n["image_path"]).unlink(missing_ok=True)   # C2
            except OSError:
                pass
        await db.update_admin_notification(nid, image_path=None)
        return web.json_response({"ok": True})
    if action == "image_regen":
        # Картинка release note по промту мастера (та же цепочка, что аватарки: diffusion →
        # процедурный фолбэк, ok есть всегда). Промт запоминаем в payload.gen_prompt — при
        # следующем открытии диалога мастер правит его, а не сочиняет заново.
        if n["kind"] != "release_note":
            return web.json_response({"ok": False, "error": "Картинка генерится только у release notes"})
        prompt = str(body.get("prompt") or "").strip()[:300]
        if not prompt:
            return web.json_response({"ok": False, "error": "Опишите, что нарисовать"})
        from .. import avatars
        # uid 0 — служебный (add_release_note); randomize — каждый повтор кнопки даёт НОВУЮ
        # картинку, а не детерминированную по промту (OpenTodo 83e1a735); scene — это
        # иллюстрация к посту (кадр 4:3, стиль сцены), а не портрет-иконка аватарки (02.09)
        res = await asyncio.to_thread(avatars.generate, 0, prompt, randomize=True, scene=True)
        if not res.get("ok"):
            return web.json_response({"ok": False, "error": res.get("error") or "Не удалось сгенерировать"})
        dest = config.NOTIF_IMG_DIR / f"notif_{nid}.jpg"

        def _save() -> None:
            from PIL import Image
            config.NOTIF_IMG_DIR.mkdir(parents=True, exist_ok=True)
            img = Image.open(res["path"])
            img.thumbnail((1600, 1600))                # лимит Telegram по длинной стороне
            img.convert("RGB").save(dest, "JPEG", quality=88)
        await asyncio.to_thread(_save)
        await db.update_admin_notification(
            nid, image_path=str(dest),
            payload={**(n.get("payload") or {}), "gen_prompt": prompt})
        # engine — чем реально нарисовано: фронт честно скажет про запасную модель/процедурный
        # рисунок вместо победного «сгенерировано» (фидбэк 21.07)
        return web.json_response({"ok": True, "engine": res.get("engine") or "",
                                  "image_url": _photo_url("notif", nid, str(dest)) or ""})
    if action == "publish":
        if n["kind"] != "release_note":
            return web.json_response({"ok": False, "error": "Публикуются только release notes"})
        if _bot is None:
            return web.json_response({"ok": False, "error": "Бот недоступен"})
        # статус new → published АТОМАРНО до рассылки: повторный тап во время
        # многосекундного цикла отправки не разошлёт клиентам дубль
        if not await db.try_mark_published(nid):
            return web.json_response({"ok": False, "error": "Уже опубликовано"})
        photo_path = (str(config.abs_data(n["image_path"]))
                      if n["image_path"] and config.abs_data(n["image_path"]).exists() else None)
        # у фото лимит подписи 1024: режем СЫРОЙ текст до экранирования и подчищаем
        # возможный обрубок HTML-сущности — иначе Telegram отклонит всё сообщение
        body = n["body"][:900] if photo_path else n["body"]
        text = "✨ <b>" + html.escape(n["title"] or "Что нового в приложении") + "</b>\n\n" \
               + html.escape(body)
        if photo_path:
            text = re.sub(r"&[#\w]{0,10}$", "", text[:1000])
        kb = keyboards.broadcast_footer(config.WEBAPP_PUBLIC_URL)
        from aiogram.types import FSInputFile
        # release notes редкие и важные — шлём всем, кроме отписавшихся (без гэпа рассылок).
        # «Все» = заказывавшие и заведённые вручную: посетителей без заказов (visitors=False)
        # новости приложения не касаются, см. store/crm.list_customers_crm
        clients = await db.list_customers_crm(segment="", visitors=False)
        sent = failed = 0
        for c in clients:
            if c["no_broadcast"]:
                continue
            ok_ = await notify.notify_user(_bot, c["uid"], text, reply_markup=kb,
                                           photo=FSInputFile(photo_path) if photo_path else None)
            if ok_:
                sent += 1
                await db.upsert_customer(c["uid"], last_broadcast_at=int(time.time()))
            else:
                failed += 1
            await asyncio.sleep(0.25)
        await db.log_event(None, "release notes", f"«{n['title']}»: доставлено {sent}, недоставлено {failed}")
        return web.json_response({"ok": True, "sent": sent, "failed": failed})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})
