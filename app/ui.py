"""Управление одним «экраном» бота: бот редактирует одно сообщение, заменяя
текст и кнопки (как просили — старые исчезают, появляются новые).

screen_id хранится в FSM-data. render() редактирует его, при неудаче — шлёт новое.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup

log = logging.getLogger(__name__)


async def render(bot: Bot, state: FSMContext, chat_id: int, text: str,
                 kb: InlineKeyboardMarkup | None = None) -> int:
    data = await state.get_data()
    mid = data.get("screen_id")
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=kb)
            return mid
        except TelegramBadRequest as exc:
            # "message is not modified" — это ок; прочее — пере-отправим
            if "message is not modified" in str(exc):
                return mid
            # Экран нельзя отредактировать в текст (например, это фото-карточка /start
            # или сообщение устарело) — убираем старое, чтобы не плодить дубли.
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:  # noqa: BLE001
                pass
    msg = await bot.send_message(chat_id, text, reply_markup=kb)
    await state.update_data(screen_id=msg.message_id)
    return msg.message_id


async def consume(message) -> None:
    """Удаляем введённое пользователем сообщение, чтобы чат оставался чистым.

    В личных чатах боту это разрешено; ошибки игнорируем.
    """
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
