"""Общие хендлеры: /start, /cancel, главное меню, отмена мастера."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import config, db, keyboards, menucard, notify, texts
from ..ui import render

router = Router()

# Брендовая карточка-картинка для /start: бот рисует её под бренд мастерской (app/menucard.py).
# file_id кэшируем после первой отправки — но только для ТОЙ ЖЕ картинки: сменили бренд —
# новая картинка уходит файлом, иначе клиенты видели бы старое имя.
_menu_card: tuple[str, str] | None = None     # (путь картинки, file_id в Telegram)


async def _open_menu_new(message: Message, state: FSMContext, prefix: str = "") -> None:
    """Отправить меню-карточку (картинкой) новым сообщением и запомнить её как экран."""
    global _menu_card
    caption = prefix.strip() or None    # просто картинка + кнопка; на /cancel — короткое «Отменено.»
    has_saved = await db.count_saved_models(message.from_user.id) > 0
    has_coll = await db.count_collection() > 0
    has_req = await db.count_user_requests(message.from_user.id) > 0
    pending = await db.pending_price_count(message.from_user.id)
    kb = keyboards.main_menu(
        config.is_admin(message.from_user.id), has_saved, has_coll, has_req,
        webapp_url=config.WEBAPP_PUBLIC_URL, pending=pending)
    path = str(await menucard.card_path())
    cached = _menu_card[1] if _menu_card and _menu_card[0] == path else None
    msg = await message.answer_photo(cached or FSInputFile(path), caption=caption, reply_markup=kb)
    if msg.photo:                       # кэшируем file_id, чтобы не перезаливать
        _menu_card = (path, msg.photo[-1].file_id)
    await state.update_data(screen_id=msg.message_id)


async def _handle_referral(message: Message, arg: str) -> None:
    """Глубокая ссылка /start ref<ID>: привязываем нового клиента к пригласившему.
    Скидок рефералка больше не даёт (OpenTodo 7f257a33): бонус +N на счёт ОБОИМ придёт после
    первого напечатанного и оплаченного заказа приглашённого (webapp._maybe_referral_reward)."""
    try:
        referrer = int(arg[3:])
    except (ValueError, IndexError):
        return
    referee = message.from_user.id
    if referrer == referee:
        return
    if await db.count_user_requests(referee) > 0:   # не новичок — пропускаем
        return
    if not await db.add_referral(referee, referrer):
        return
    await notify.notify_user(
        message.bot, referrer,
        f"🎁 По вашей ссылке зарегистрировался друг! Когда его первый заказ будет напечатан "
        f"и оплачен — вам обоим придёт по {config.REF_REWARD_AMOUNT:g} {config.CURRENCY} на счёт.")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    arg = (command.args or "").strip()
    prefix = ""
    if arg.startswith("ref"):
        await _handle_referral(message, arg)
    elif arg == "calc":
        # Глубокая ссылка /start calc (QR на принтере/визитках → t.me/<bot>?start=calc,
        # ux-paths-next.md идея 7): сразу зовём в тот же короткий путь — прислать ссылку/название.
        prefix = ("🧮 Пришлите <b>ссылку на модель</b> или её название — посчитаю "
                  "стоимость печати за секунды.\n\n")
    await db.log_event(message.from_user.id, "открыл бота",
                       "по ссылке-калькулятору" if arg == "calc" else "",
                       user_name=message.from_user.full_name)
    await _open_menu_new(message, state, prefix=prefix)


@router.callback_query(F.data == "m:invite")
async def cb_invite(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id)
    me = await cb.bot.get_me()
    uid = cb.from_user.id
    link = f"https://t.me/{me.username}?start=ref{uid}"
    n = await db.count_referrals(uid)
    text = (f"🎁 <b>Пригласите друга</b>\n\nПоделитесь ссылкой — когда первый заказ друга "
            f"будет напечатан и оплачен, вы ОБА получите по "
            f"{config.REF_REWARD_AMOUNT:g} {config.CURRENCY} на счёт.\n\n"
            f"🔗 <code>{link}</code>\n\nПриглашено друзей: <b>{n}</b>")
    await render(cb.bot, state, cb.message.chat.id, text, keyboards.back_home())
    await cb.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _open_menu_new(message, state, prefix="Отменено.\n\n")


async def _back_to_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(screen_id=cb.message.message_id)
    has_saved = await db.count_saved_models(cb.from_user.id) > 0
    has_coll = await db.count_collection() > 0
    has_req = await db.count_user_requests(cb.from_user.id) > 0
    pending = await db.pending_price_count(cb.from_user.id)
    ts = await db.get_text_settings()
    await render(cb.bot, state, cb.message.chat.id, texts.menu(ts.get("brand_title", "")),
                 keyboards.main_menu(config.is_admin(cb.from_user.id), has_saved, has_coll, has_req,
                                     webapp_url=config.WEBAPP_PUBLIC_URL, pending=pending))


@router.callback_query(F.data == "m:help")
async def cb_help(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id)
    await render(cb.bot, state, cb.message.chat.id, texts.HELP, keyboards.back_home())
    await cb.answer()


@router.callback_query(F.data == "m:home")
async def cb_home(cb: CallbackQuery, state: FSMContext) -> None:
    await _back_to_menu(cb, state)
    await cb.answer()


@router.callback_query(F.data == "c:cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await _back_to_menu(cb, state)
    await cb.answer("Отменено")


#: Ответ на кнопку предложения приёмной, дошедшую до ПК (docs/…/vps-relay-2026-10-04.md §4а).
RELAY_OFFER_STALE = ("Это предложение устарело: компьютер мастерской уже на связи. "
                     "Пришлите модель или ссылку ещё раз — посчитаю цену сразу.")


@router.callback_query(F.data.startswith("rly:"))
async def cb_relay_offer_stale(cb: CallbackQuery) -> None:
    """Кнопки «Оставить заказ / Не нужно» из предложения приёмной на VPS (rly:ord/rly:no). Пока
    бот слушает Telegram через приёмную, они до ПК не доходят — их разбирает она. Но после
    отката (TG_SOURCE=poll) и в аварийном прямом режиме Telegram отдаёт их боту, а обработчика
    не было: клиент видел вечные «часики», заявка терялась молча. Заказ из такого предложения
    ПК принять не может (модель и выбор лежат в журнале приёмной) — честно просим прислать
    модель ещё раз."""
    await cb.answer(RELAY_OFFER_STALE, show_alert=True)


# ─── Чат-админка убрана: вход в админку теперь только в приложении ───
async def _send_app_admin(bot, chat_id: int, user_id: int) -> None:
    if not config.is_admin(user_id):
        return
    kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="admin", text="🛠 Открыть админку")
    if kb is not None:
        await bot.send_message(chat_id, "🛠 Админ-панель теперь в приложении.", reply_markup=kb)
    else:
        await bot.send_message(chat_id, "Приложение недоступно — проверьте, что туннель поднят.")


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await _send_app_admin(message.bot, message.chat.id, message.from_user.id)


@router.message(Command("desk"))
async def cmd_desk(message: Message, state: FSMContext) -> None:
    """Код для входа в НАСТОЛЬНУЮ админку — по нему сотрудник входит ПОД СОБОЙ.

    Личность берём из Telegram, а не из нового пароля: сотрудника заводит владелец по его
    user_id, бот его уже знает. Код бесполезен постороннему — обменять его на сессию можно
    только с компьютера мастерской, а сами файлы админки наружу не отдаются."""
    uid = message.from_user.id
    if not config.is_admin(uid):
        # Молчать нельзя (человек ждёт ответа), но и подсказывать, что команда существует
        # для кого-то, — незачем: отвечаем как на любую незнакомую команду.
        await message.answer("Команда доступна только сотрудникам мастерской.")
        return
    from .. import desklogin
    code = await desklogin.issue_code(uid)
    await message.answer(texts.desk_login_code(code, desklogin.CODE_TTL_SEC // 60))


@router.callback_query(F.data == "m:admin")     # старые кнопки чат-админки → ведём в приложение
async def cb_admin_legacy(cb: CallbackQuery, state: FSMContext) -> None:
    await _send_app_admin(cb.bot, cb.message.chat.id, cb.from_user.id)
    await cb.answer()
