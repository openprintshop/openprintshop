"""Хендлеры онлайн-оплаты (A1, app/payments.py): кнопка «Оплатить» в чате,
pre_checkout и successful_payment. Вся платёжная логика — в app/payments.py,
здесь только связка с aiogram-апдейтами."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from .. import config, db, notify, payments, texts

log = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data.startswith("upay:"))
async def user_pay(cb: CallbackQuery) -> None:
    """Клиент жмёт «Оплатить» в карточке заказа в чате → счёт сообщением."""
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r.user_id != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return
    if int(getattr(r, "paid", 0) or 0):
        await cb.answer("Заказ уже оплачен ✅")
        return
    if await payments.send_invoice(cb.bot, r):
        await cb.answer("Счёт отправлен 👇")
    else:
        # оплата выключена/провайдер отказал — честно говорим, а не молчим
        await cb.answer("Сейчас оплатить онлайн не получается — напишите мастеру.",
                        show_alert=True)


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    """Telegram даёт 10 секунд подтвердить списание. Сверяем счёт с ТЕКУЩИМ состоянием
    заказа (цена/статус/не оплачен ли уже) — устаревший счёт отклоняется с внятным текстом."""
    err = await payments.precheckout_error(
        q.from_user.id, q.invoice_payload, q.total_amount, q.currency)
    try:
        if err:
            await q.answer(ok=False, error_message=err)
        else:
            await q.answer(ok=True)
    except Exception:  # noqa: BLE001
        log.warning("pre_checkout answer failed", exc_info=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    """Деньги списаны. Пишем леджер, помечаем заказ оплаченным, благодарим клиента,
    уведомляем мастера. Идемпотентно по telegram charge id (повторный апдейт — тишина)."""
    sp = message.successful_payment
    r = await payments.record_success(
        message.from_user.id, sp.invoice_payload, sp.total_amount, sp.currency,
        sp.telegram_payment_charge_id, sp.provider_payment_charge_id or "")
    if r is None:
        return
    amount = texts.fmt_money(sp.total_amount / 100, config.CURRENCY)
    # Оплата из «Ждёт оплаты» сама перевела заказ «В работе» (payments.record_success) —
    # клиенту и мастеру говорим об этом сразу, отдельного пуша о смене статуса нет.
    in_work = r.status == "in_progress"
    try:
        await message.answer(
            f"✅ <b>Платёж получен</b> — {amount} по заказу #{r.id} "
            f"«{texts.esc(r.model_label)}».\n"
            + ("🔧 <b>Берёмся за ваш заказ!</b> Напишем, когда будет готов.\n" if in_work
               else "Спасибо!\n")
            + "🧾 Чек придёт от платёжного провайдера.")
        log.info("payment confirmation sent: order #%s, user %s", r.id, r.user_id)
    except Exception:  # noqa: BLE001
        # warning, не debug: клиент заплатил и НЕ получил подтверждения — это надо видеть
        log.warning("payment thanks failed for order #%s", r.id, exc_info=True)
    try:
        await notify.notify_admins(
            message.bot,
            f"💳 <b>Заказ #{r.id} оплачен онлайн</b>: {amount}"
            + (" → 🔧 В работе" if in_work else "") + "\n"
            f"👤 {texts.esc(r.user_name or message.from_user.full_name)} "
            f"(id <code>{r.user_id}</code>)\n"
            f"📦 {texts.esc(r.model_label)} · {r.quantity} шт")
    except Exception:  # noqa: BLE001
        log.warning("payment admin notify failed for order #%s", r.id, exc_info=True)
    # Рефералка: «напечатан И оплачен» — оплата могла стать последним недостающим условием.
    from .. import webapp
    await webapp._maybe_referral_reward(r.user_id)
