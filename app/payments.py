"""Онлайн-оплата заказа через Telegram Payments (A1, competitor-gap 2026-07).

До этого модуля деньги шли полностью мимо системы: статус «счёт выставлен» и PDF-смета
есть, а заплатить клиенту было нечем — только наличные/перевод и ручная галочка «оплачен»
у мастера. Здесь появляется настоящий платёжный тракт: send_invoice / pre_checkout /
successful_payment, кнопка «Оплатить» в чате бота и в мини-аппе (openInvoice).

Как это устроено:
- Провайдер подключается владельцем бота в BotFather (Bot Settings → Payments): для РФ —
  ЮKassa, для Беларуси — bePaid, для карт мира — Stripe и т.д. BotFather выдаёт
  provider_token — его мастер вписывает в админке («Онлайн-оплата»). Пустой токен =
  фича выключена, ни одной кнопки оплаты нигде не появляется.
- Валюта платежа (pay_currency, ISO-код: RUB/BYN/…) обязана совпадать с валютой цен
  мастерской И поддерживаться выбранным провайдером — суммы уходят как price×100
  (минорные единицы), никакой конвертации нет.
- Payload счёта — «pay:<id заказа>:<сумма в минорных>»: на pre_checkout сверяем сумму с
  ТЕКУЩЕЙ ценой заказа, поэтому старый счёт в чате после смены цены безопасно отклонится.
- successful_payment идемпотентен по telegram charge id (UNIQUE в payments): повторная
  доставка апдейта не задвоит ни леджер, ни уведомления.
- Оплата НЕ трогает внутренний баланс клиента (деньги пришли провайдеру напрямую, зачислять
  их на счёт было бы двойным учётом) и НЕ двигает статус заказа — очередью управляет мастер.
  Возвраты — только через кабинет провайдера: Bot API возврата обычных платежей не даёт.
"""
from __future__ import annotations

import logging
import re
import time

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LabeledPrice

from . import config, db, notify
from .web.common import signed_url

log = logging.getLogger(__name__)

# Статусы, в которых заказ можно оплатить. «Счёт выставлен»/«изменена цена» нарочно не здесь:
# сначала клиент принимает сумму (pending_price → price), потом платит — иначе оплата
# зафиксировала бы старую цену. awaiting_payment — мастер одобрил и выставил счёт (этап
# «Ждёт оплаты», фидбэк 31.07); оплата из него автоматически переводит заказ «В работе».
# printed — оплата при получении; handed нет: готовое изделие «вручено» закрывается
# мастером вместе с деньгами.
PAYABLE_STATUSES = ("new", "awaiting_payment", "in_progress", "printed")

# Telegram требует total_amount ≥ ~1 у.е. у большинства провайдеров; счёт на 0 бессмыслен.
MIN_AMOUNT_MINOR = 100

PAYLOAD_PREFIX = "pay"

# Токен провайдера из BotFather: «<цифры>:TEST:…» или «<цифры>:LIVE:…». Валидируем при
# сохранении в админке: на практике сюда вписывают что угодно — публичный ключ из кабинета
# провайдера (MIIB…, 392 символа — реальный случай 31.07), API-ключ ЮKassa и т.п. Такой
# «токен» проходит проверку «непустой» и молча ломает оплату у всех клиентов
# (PAYMENT_PROVIDER_INVALID на каждом счёте).
_TOKEN_RE = re.compile(r"^\d+:(TEST|LIVE):\S+$")


def clean_provider_token(value: str) -> str | None:
    """Нормализованный токен провайдера, '' = выключить оплату, None = это не токен."""
    v = (value or "").strip()
    if not v:
        return ""
    return v if _TOKEN_RE.match(v) else None


async def pay_settings() -> tuple[str, str]:
    """(provider_token, currency) из настроек. Пустой токен = оплата выключена."""
    t = await db.get_text_settings()
    token = (t.get("pay_provider_token") or "").strip()
    currency = (t.get("pay_currency") or "RUB").strip().upper() or "RUB"
    return token, currency


async def enabled() -> bool:
    token, _ = await pay_settings()
    return bool(token)


def amount_minor(price: float) -> int:
    """Цена в минорных единицах (копейках). round, не int(): 12.34*100 == 1233.999…"""
    return int(round(float(price or 0) * 100))


def is_payable(r, pay_on: bool = True) -> bool:
    """Можно ли предложить клиенту оплатить заказ r прямо сейчас."""
    if not pay_on or r is None:
        return False
    return (r.status in PAYABLE_STATUSES and not int(getattr(r, "paid", 0) or 0)
            and amount_minor(r.price) >= MIN_AMOUNT_MINOR)


async def delivery_extra_minor(r) -> int:
    """Стоимость доставки в минорных единицах, которую должен нести счёт ЭТОГО заказа (A2).

    Доставка одна на весь заказ-корзину, а платят его по позициям — значит, у надбавки должен
    быть ровно один детерминированный носитель, иначе клиент оплатит доставку столько раз,
    сколько в заказе позиций. Носитель — ЯКОРНАЯ позиция группы (минимальный id). Для всех
    остальных позиций и для одиночных заказов надбавки нет.

    0 также если доставка уже оплачена (delivery_paid) — повторный счёт её не добавит."""
    gid = (getattr(r, "order_group", "") or "").strip()
    if not gid:
        return 0
    ent = await db.get_order_group(gid)
    if not ent or int(ent.get("delivery_paid") or 0):
        return 0
    cost = float(ent.get("delivery_cost") or 0)
    if cost <= 0:
        return 0
    anchor = await db.group_anchor_request_id(gid)
    return amount_minor(cost) if anchor == int(r.id) else 0


async def order_total_minor(r) -> int:
    """Сколько всего должен списать счёт по заказу: цена позиции + доставка (если носитель)."""
    return amount_minor(r.price) + await delivery_extra_minor(r)


def build_payload(rid: int, minor: int) -> str:
    return f"{PAYLOAD_PREFIX}:{int(rid)}:{int(minor)}"


def parse_payload(payload: str) -> tuple[int, int] | None:
    """«pay:<rid>:<minor>» → (rid, minor); мусор/чужой payload → None."""
    try:
        prefix, rid, minor = str(payload or "").split(":")
        if prefix != PAYLOAD_PREFIX:
            return None
        return int(rid), int(minor)
    except (ValueError, AttributeError):
        return None


def _invoice_kwargs(r, token: str, currency: str, note: str = "", extra_minor: int = 0) -> dict:
    """Общие аргументы send_invoice/create_invoice_link по заказу.

    note — приписка в начало описания («Мастер подтвердил заказ», «Цена уточнена: …»):
    выставление счёта и его контекст живут в ОДНОМ сообщении-инвойсе, а не в двух подряд.
    Лимиты Telegram: title ≤ 32 символа, description ≤ 255 — поэтому номер в title,
    а название модели в description с обрезкой.

    extra_minor — доставка (A2) ОТДЕЛЬНОЙ строкой счёта: клиент видит в платёжке, за что
    именно платит, а payload несёт полную сумму — по ней сверяемся на pre_checkout."""
    item_minor = amount_minor(r.price)
    minor = item_minor + max(0, int(extra_minor))
    desc = f"{r.model_label} · {r.quantity} шт"
    if r.plastic_name:
        desc += f" · {r.plastic_name}"
    if extra_minor > 0:
        desc += " + доставка"
    if note:
        desc = f"{note}\n{desc}"
    prices = [LabeledPrice(label=f"3D-печать, заказ #{r.id}", amount=item_minor)]
    if extra_minor > 0:
        prices.append(LabeledPrice(label="Доставка", amount=int(extra_minor)))
    kwargs = dict(
        title=f"Заказ #{r.id}"[:32],
        description=desc[:255],
        payload=build_payload(r.id, minor),
        provider_token=token,
        currency=currency,
        prices=prices,
    )
    # Превью модели в счёте — если у мини-аппа есть публичный URL (провайдеру нужен https).
    if config.WEBAPP_PUBLIC_URL and getattr(r, "preview_path", None):
        kwargs["photo_url"] = (config.WEBAPP_PUBLIC_URL.rstrip("/")
                               + signed_url("req", r.id))
        kwargs["photo_size"] = 512
        kwargs["photo_width"] = 512
        kwargs["photo_height"] = 512
    return kwargs


async def _alert_provider_error(exc: Exception) -> None:
    """Ошибка от Telegram при создании счёта — почти всегда проблема НАСТРОЙКИ (кривой токен,
    неподдерживаемая провайдером валюта), и молчать о ней нельзя: клиенты жмут «Оплатить»
    и видят отказ, а мастер не в курсе. Алерт с троттлингом, чтобы не спамить на каждый клик."""
    if not isinstance(exc, TelegramBadRequest):
        return
    msg = str(exc)
    if "PAYMENT_PROVIDER_INVALID" in msg:
        hint = ("Telegram не принял токен провайдера. Нужен токен из BotFather "
                "(/mybots → Bot Settings → Payments), вида 123456789:TEST:… или "
                "…:LIVE:… — ключи из кабинета провайдера не подходят.")
    elif "CURRENCY" in msg.upper():
        hint = "Провайдер не принимает валюту счетов — проверьте «Валюта счетов» в настройках."
    else:
        hint = msg[:200]
    try:
        await notify.alert(f"💳 Онлайн-оплата не работает: {hint}",
                           key="pay_provider", throttle_sec=1800)
    except Exception:  # noqa: BLE001
        log.debug("pay provider alert failed", exc_info=True)


async def send_invoice(bot, r, note: str = "") -> bool:
    """Счёт сообщением в чат клиента с ботом. False — оплата выключена/заказ не оплачиваем/
    Telegram отказал (например, провайдер не принимает валюту) — падать из-за этого нельзя."""
    token, currency = await pay_settings()
    if not token or not is_payable(r):
        return False
    try:
        extra = await delivery_extra_minor(r)     # доставка заказа-корзины (A2)
        await bot.send_invoice(chat_id=r.user_id,
                               **_invoice_kwargs(r, token, currency, note, extra))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("send_invoice failed for order #%s", r.id, exc_info=True)
        await _alert_provider_error(e)
        return False


async def create_link(bot, r) -> str | None:
    """Ссылка-счёт для мини-аппа: фронт откроет её через Telegram.WebApp.openInvoice —
    оплата происходит, не выходя из приложения."""
    token, currency = await pay_settings()
    if not token or not is_payable(r):
        return None
    try:
        extra = await delivery_extra_minor(r)     # доставка заказа-корзины (A2)
        return await bot.create_invoice_link(**_invoice_kwargs(r, token, currency, "", extra))
    except Exception as e:  # noqa: BLE001
        log.warning("create_invoice_link failed for order #%s", r.id, exc_info=True)
        await _alert_provider_error(e)
        return None


async def precheckout_error(user_id: int, payload: str, total_amount: int,
                            currency: str) -> str | None:
    """Проверка перед списанием денег (Telegram даёт 10 секунд на ответ).

    None — всё сходится, можно подтверждать. Строка — текст отказа клиенту.
    Главная защита: сумма в payload сверяется с ТЕКУЩЕЙ ценой заказа — счёт, выставленный
    до смены цены/отмены/другой оплаты, отклонится, а не спишет неактуальную сумму."""
    parsed = parse_payload(payload)
    if not parsed:
        return "Счёт устарел или повреждён. Откройте заказ и попробуйте ещё раз."
    rid, minor = parsed
    r = await db.get_request(rid)
    if not r or r.user_id != user_id:
        return "Заказ не найден. Возможно, он был удалён."
    if int(getattr(r, "paid", 0) or 0):
        return "Этот заказ уже оплачен — второй раз платить не нужно."
    if r.status not in PAYABLE_STATUSES:
        return "Заказ в статусе, который сейчас нельзя оплатить."
    _, cur_setting = await pay_settings()
    if currency != cur_setting:
        return "Валюта счёта устарела. Откройте заказ и создайте счёт заново."
    # Ожидаемая сумма = цена позиции + доставка, если счёт её несёт (A2). Мастер поменял
    # стоимость доставки после выставления счёта → суммы разойдутся, и счёт отклонится —
    # ровно так же, как при смене цены заказа.
    if minor != await order_total_minor(r) or total_amount != minor:
        return ("Цена заказа изменилась, и этот счёт больше не действует. "
                "Откройте заказ и оплатите по актуальной цене.")
    return None


async def _mark_delivery_paid(rid: int, total_amount: int) -> None:
    """Отметить доставку заказа-корзины оплаченной, если платёж её включал (A2)."""
    try:
        r = await db.get_request(rid)
        gid = (getattr(r, "order_group", "") or "").strip() if r else ""
        if not gid or int(total_amount) <= amount_minor(r.price):
            return
        ent = await db.get_order_group(gid)
        if ent and float(ent.get("delivery_cost") or 0) > 0 and not int(ent.get("delivery_paid") or 0):
            await db.update_order_group(gid, delivery_paid=1)
    except Exception:  # noqa: BLE001
        log.warning("не удалось отметить доставку оплаченной по заказу #%s", rid, exc_info=True)


async def record_success(user_id: int, payload: str, total_amount: int, currency: str,
                         tg_charge_id: str, provider_charge_id: str = "") -> "db.Request | None":
    """Фиксация успешного платежа: строка в леджер payments + заказ «оплачен».

    Возвращает обновлённый заказ или None (payload не наш / повторная доставка того же
    charge id — тогда ничего не делаем: идемпотентность)."""
    parsed = parse_payload(payload)
    if not parsed:
        log.warning("successful_payment с чужим payload: %r", payload)
        return None
    rid, _ = parsed
    pay_id = await db.add_payment(
        request_id=rid, user_id=user_id, amount=round(int(total_amount) / 100, 2),
        currency=currency, tg_charge_id=tg_charge_id,
        provider_charge_id=provider_charge_id or "")
    if pay_id is None:                       # дубль апдейта Telegram — уже учтено
        return None
    fields = {"paid": 1, "paid_at": int(time.time()), "pay_method": "online"}
    # Доставка (A2) едет в счёте якорной позиции отдельной строкой: заплатили больше цены
    # позиции — значит, доставка в этом платеже и была. Отмечаем её оплаченной, чтобы
    # следующий счёт по другой позиции той же группы не выставил её второй раз.
    await _mark_delivery_paid(rid, int(total_amount))
    # Этап «Ждёт оплаты»: оплата и была недостающим шагом — заказ сам уходит «В работе»
    # (фидбэк 31.07). Из других статусов оплату статуса не двигаем: очередью правит мастер.
    prev = await db.get_request(rid)
    if prev is not None and prev.status == "awaiting_payment":
        fields["status"] = "in_progress"
    await db.update_request(rid, **fields)
    await db.log_event(user_id, "оплатил заказ", f"#{rid}")
    return await db.get_request(rid)
