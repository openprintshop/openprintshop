"""Ответ реплаем на сообщение бота → в чат заказа (просьба клиента 13.08.2026).

Роутер намеренно тонкий: КУДА класть ответ и что при этом отправить, решает app/orderchat.py —
там же живут шапки уведомлений, по которым цитата и опознаётся. Здесь остаётся телеграмная
часть: разобрать апдейт, скачать фото, ответить человеку.

Порядок роутеров важен (run.build_dispatcher): этот подключается ДО common и user_flow, иначе
реплай съест либо catch-all фолбэк «Нажмите /start», либо шаг мастера расчёта, если клиент
отвечает, не закрыв мастер. Гейт стоит в ФИЛЬТРЕ, а не в теле: не наш случай — фильтр говорит
False, и апдейт спокойно идёт дальше прежним путём.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.types import Message, MessageReactionUpdated

from .. import config, db, orderchat, preview, ratelimit, roles

log = logging.getLogger(__name__)
router = Router()

_MAX_LEN = 2000        # столько же принимает чат в приложении (webapp._api_order_message)


async def _reply_target(message: Message) -> dict | bool:
    """Фильтр: это ответ на сообщение бота, которому есть место в переписке?

    Возвращает {"target": …, "reply_to": …} — aiogram отдаст их хендлеру аргументами;
    False — «не наш апдейт», обработка продолжится следующими роутерами.

    Адресата ищем ДВАЖДЫ и в таком порядке: сначала по карте отправленных сообщений (точно,
    и заодно даёт «ответ на сообщение» внутри переписки), потом — разбором текста цитаты.
    Второе нужно для всего, что бот отправил до появления карты: реплаить люди будут и на
    позавчерашнее."""
    src = message.reply_to_message
    if src is None or message.from_user is None:
        return False
    if getattr(message.chat, "type", "private") != "private":
        return False      # фича про личный чат с ботом; в группе «ответ клиенту» бессмыслен
    if not (src.from_user and src.from_user.is_bot):
        return False      # цитируют своё же сообщение — нам там нечего разбирать
    body = (message.text or message.caption or "").strip()
    if body.startswith("/"):
        return False      # команда остаётся командой, даже если её прислали реплаем
    if not body and not message.photo:
        return False      # стикер/голосовое/кружок реплаем — прежним путём
    uid = message.from_user.id
    hit = await orderchat.resolve_link(uid, message.chat.id, src.message_id)
    if hit:
        return {"target": hit[0], "reply_to": hit[1]}
    target = await orderchat.resolve(uid, src.text or src.caption or "")
    return {"target": target, "reply_to": None} if target else False


@router.message(_reply_target)
async def on_reply(message: Message, target: orderchat.Target,
                   reply_to: int | None = None) -> None:
    uid = message.from_user.id
    text = (message.text or message.caption or "").strip()[:_MAX_LEN]
    # Вложение = файл на диске мастерской, поэтому частоту ограничиваем ровно так же, как в
    # приложении: на текст лимита нет и не нужно, а фото без него позволяет забить диск.
    if message.photo and not ratelimit.allow(f"chatphoto:{uid}", 10, 300):
        await message.answer("Слишком много фото подряд — попробуйте через минуту.")
        return
    mid = await orderchat.save(target, text, reply_to=reply_to)
    photo_path = await _save_photo(message, mid) if message.photo else ""
    await orderchat.mark_read(target)
    if target.from_admin:
        await _sent_by_staff(message, target, text, photo_path, mid)
    else:
        await _sent_by_client(message, target, text, photo_path, mid)


@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated, bot) -> None:
    """Реакция на сообщение бота → тот же эмодзи отдельным сообщением в переписку, ответом на
    то, на что её поставили.

    Работает ТОЛЬКО по карте отправленных сообщений: в апдейте message_reaction нет ни текста,
    ни номера заказа — только чат и номер сообщения. Поэтому реакция на сообщение, отправленное
    до появления карты, молча игнорируется: угадывать, к какому заказу её приписать, нечем.

    Апдейт этого типа Telegram присылает, только если он есть в allowed_updates; aiogram
    собирает список по ЗАРЕГИСТРИРОВАННЫМ хендлерам (resolve_used_update_types), так что
    снос этого хендлера тихо выключит и подписку."""
    user = event.user
    if user is None or getattr(event.chat, "type", "private") != "private":
        return
    emoji = _added_emoji(event)
    if not emoji:
        return                    # реакцию сняли или она не эмодзи (кастомная/платная)
    hit = await orderchat.resolve_link(user.id, event.chat.id, event.message_id)
    if hit is None:
        return
    target, reply_to = hit
    # Реакция снимается и ставится одним тапом, апдейт приходит на каждый — без этой проверки
    # одно сердечко, поставленное трижды, стало бы тремя сообщениями в переписке.
    if await orderchat.reaction_seen(target, reply_to, emoji):
        return
    mid = await orderchat.save(target, emoji, reply_to=reply_to)
    await orderchat.mark_read(target)
    if target.from_admin:
        await orderchat.notify_client(bot, target, emoji, msg_id=mid)
        await roles.log_action(user.id, "поставил реакцию",
                               f"{emoji} #{target.rid}" if target.rid else emoji)
    else:
        await orderchat.notify_staff(bot, target, await _client_name(target, user), emoji,
                                     msg_id=mid)


def _added_emoji(event: MessageReactionUpdated) -> str:
    """Что ДОБАВИЛОСЬ этим апдейтом. Telegram присылает списки целиком (было/стало), а
    премиум-аккаунт ставит несколько реакций сразу — берём разницу, иначе повтор старой
    реакции ехал бы в переписку заново."""
    old = {getattr(r, "emoji", "") for r in (event.old_reaction or [])}
    fresh = [getattr(r, "emoji", "") for r in (event.new_reaction or [])]
    return "".join(e for e in fresh if e and e not in old)


async def _client_name(target: orderchat.Target, user) -> str:
    """Как звать клиента в уведомлении команде: имя из заказа, иначе — из Telegram."""
    r = await db.get_request(target.rid) if target.rid else None
    return (r.user_name if r else "") or getattr(user, "full_name", "") or f"id {user.id}"


async def _sent_by_client(message: Message, target: orderchat.Target, text: str,
                          photo_path: str, mid: int) -> None:
    uid = message.from_user.id
    who = await _client_name(target, message.from_user)
    await orderchat.notify_staff(message.bot, target, who, text, photo_path, msg_id=mid)
    await db.log_event(uid, "написал в чат",
                       (f"#{target.rid}: " if target.rid else "") + text[:80],
                       user_name=message.from_user.full_name)
    where = f" в чат заказа #{target.rid}" if target.rid else ""
    await message.answer(f"✅ Отправлено мастеру{where} — ответ придёт сюда же.")


async def _sent_by_staff(message: Message, target: orderchat.Target, text: str,
                         photo_path: str, mid: int) -> None:
    delivered = await orderchat.notify_client(message.bot, target, text, photo_path, msg_id=mid)
    await roles.log_action(message.from_user.id, "ответил клиенту",
                           f"#{target.rid}" if target.rid else f"id {target.client_id}")
    if delivered:
        await message.answer("✅ Отправлено клиенту.")
    else:
        # Сообщение осталось в переписке — мастер видит, что писал, но доставки не было
        # (та же развилка, что у «написать клиенту» в админке).
        await message.answer("⚠️ Не доставлено — клиент не запускал бота или заблокировал его. "
                             "Сообщение осталось в переписке.")


async def _save_photo(message: Message, mid: int) -> str:
    """Фото из реплая — на диск и в строку переписки. Папка и имя те же, что у вложения из
    приложения (webapp._save_chat_image): чат в админке отдаёт файл по image_path и про
    происхождение картинки ничего знать не обязан."""
    config.COLLECTION_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.COLLECTION_DIR / f"chat_{mid}.jpg"
    try:
        await message.bot.download(message.photo[-1], destination=str(dest))
        await asyncio.to_thread(preview.optimize_photo, dest)
    except Exception:  # noqa: BLE001
        log.warning("не сохранил фото из реплая", exc_info=True)
        return ""
    await db.set_message_image(mid, str(dest))
    return str(dest)
