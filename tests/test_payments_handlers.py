"""Хендлеры онлайн-оплаты (app/handlers/payments.py) — связка aiogram-апдейтов с app/payments.py.

На сам модуль хендлеров не было ни одного теста, хотя через него ходят деньги клиента: кнопка
«Оплатить» в чате, подтверждение списания (pre_checkout) и фиксация платежа. Проверяем три
вещи, поломка которых стоит дороже всего:

1. чужой заказ по «upay:» не превращается в счёт (иначе любой клиент выставит себе счёт по
   чужому заказу, зная только номер);
2. pre_checkout подтверждает живой заказ и отклоняет отменённый (Telegram даёт 10 секунд, и
   молчание/неверное «да» — это списанные деньги за заказ, которого нет);
3. повторная доставка successful_payment не задваивает ни леджер, ни благодарность клиенту,
   ни уведомление мастеру (идемпотентность по telegram charge id — UNIQUE в payments).

Фейки — по образцу tests/test_bot_order_flow.py (CallbackQuery/Message тех же очертаний),
бот — общий FakeBot из tests/conftest.py. Тесты фиксируют ТЕКУЩЕЕ поведение.
"""
from types import SimpleNamespace

from conftest import FakeBot

from app import config, payments
from app.handlers import payments as pay_handlers

USER_ID = 4001
OTHER_ID = 4002
ADMIN_ID = 999


# ─────────────────────────────── фейки ───────────────────────────────

class _FakeCb:
    """CallbackQuery для user_pay: данные кнопки, автор нажатия, бот и журнал ответов."""

    def __init__(self, data, uid=USER_ID, *, bot=None):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, username="tester", full_name="Тест Тестов")
        self.bot = bot if bot is not None else FakeBot()
        self.answers: list = []              # (текст, show_alert)

    async def answer(self, text="", **kw):
        self.answers.append((text, bool(kw.get("show_alert"))))


class _FakePcq:
    """PreCheckoutQuery: то, что Telegram спрашивает перед списанием, и наш ответ на это."""

    def __init__(self, payload, amount, currency="RUB", uid=USER_ID):
        self.from_user = SimpleNamespace(id=uid)
        self.invoice_payload = payload
        self.total_amount = amount
        self.currency = currency
        self.replies: list = []              # (ok, error_message)

    async def answer(self, ok, error_message=None, **kw):
        self.replies.append((ok, error_message))


class _FakePayMsg:
    """Message с successful_payment — апдейт «деньги списаны»."""

    def __init__(self, payload, amount, charge_id, *, uid=USER_ID, currency="RUB", bot=None):
        self.successful_payment = SimpleNamespace(
            invoice_payload=payload, total_amount=amount, currency=currency,
            telegram_payment_charge_id=charge_id,
            provider_payment_charge_id="prov_" + charge_id)
        self.from_user = SimpleNamespace(id=uid, full_name="Тест Тестов")
        self.bot = bot if bot is not None else FakeBot()
        self.answers: list = []

    async def answer(self, text, **kw):
        self.answers.append(text)


# ─────────────────────────────── данные ───────────────────────────────

async def _order(db, *, price=12.34, status="new", user_id=USER_ID) -> int:
    rid = await db.add_request(
        user_id=user_id, user_name="@tester", model_label="Бэйбизавр", printer="ECC2",
        plastic_name="PLA", quantity=1, quality="Стандарт", strength="Средняя",
        grams_total=17, minutes_total=100, price=price, preview_path=None, status=status)
    return rid


async def _enable_pay(db, token="123:TEST:token", currency="RUB") -> None:
    await db.set_text_setting("pay_provider_token", token)
    await db.set_text_setting("pay_currency", currency)


# ─────────────────── «Оплатить» в чате: чей заказ ───────────────────

async def test_user_pay_only_for_own_order(tmp_db, monkeypatch):
    """Кнопка «Оплатить» несёт номер заказа, а номер угадывается перебором: счёт по ЧУЖОМУ
    заказу выставляться не должен. Владелец того же заказа счёт получает — иначе тест был бы
    зелёным и при поломке самой отправки."""
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db, user_id=USER_ID)

    invoiced: list = []

    async def _fake_send_invoice(bot, r, note=""):
        invoiced.append(r.id)
        return True

    monkeypatch.setattr(payments, "send_invoice", _fake_send_invoice)

    # чужой человек жмёт кнопку по заказу USER_ID
    foreign = _FakeCb(f"upay:{rid}", uid=OTHER_ID)
    await pay_handlers.user_pay(foreign)
    assert invoiced == [], "счёт по чужому заказу выставлять нельзя"
    assert foreign.answers == [("Не найдено", True)]

    # хозяин заказа — счёт уходит
    own = _FakeCb(f"upay:{rid}", uid=USER_ID)
    await pay_handlers.user_pay(own)
    assert invoiced == [rid]
    assert own.answers == [("Счёт отправлен 👇", False)]


# ─────────────────── pre_checkout: подтвердить/отклонить ───────────────────

async def test_pre_checkout_confirms_live_and_rejects_cancelled(tmp_db):
    """Живой заказ — ok=True; тот же счёт после отмены заказа — ok=False с текстом отказа
    (отменённого статуса нет в PAYABLE_STATUSES). Ответить обязаны в обоих случаях: молчание
    в отведённые 10 секунд Telegram трактует как сбой."""
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db, price=12.34)
    payload = payments.build_payload(rid, 1234)

    live = _FakePcq(payload, 1234)
    await pay_handlers.pre_checkout(live)
    assert live.replies == [(True, None)]

    await tmp_db.update_request(rid, status="cancelled")
    dead = _FakePcq(payload, 1234)
    await pay_handlers.pre_checkout(dead)
    assert len(dead.replies) == 1, "на pre_checkout обязаны ответить"
    ok, err = dead.replies[0]
    assert ok is False and err, "отменённый заказ оплачивать нечем"


# ─────────────────── successful_payment: повтор апдейта ───────────────────

async def test_successful_payment_repeat_is_silent(tmp_db, monkeypatch):
    """Telegram может доставить successful_payment повторно (и доставляет). Второй апдейт с тем
    же telegram charge id не должен ни писать вторую строку леджера, ни благодарить клиента
    второй раз, ни дёргать мастера — идемпотентность держится на UNIQUE tg_charge_id в payments
    (db.add_payment → None) и раннем выходе хендлера по record_success() is None."""
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db, price=12.34)
    payload = payments.build_payload(rid, 1234)
    bot = FakeBot()

    first = _FakePayMsg(payload, 1234, "tg_ch_dup", bot=bot)
    await pay_handlers.successful_payment(first)
    repeat = _FakePayMsg(payload, 1234, "tg_ch_dup", bot=bot)
    await pay_handlers.successful_payment(repeat)

    ledger = await tmp_db.payments_for_request(rid)
    assert len(ledger) == 1, "повторный апдейт не должен задваивать леджер"
    assert ledger[0]["tg_charge_id"] == "tg_ch_dup" and ledger[0]["amount"] == 12.34

    assert len(first.answers) == 1 and "Платёж получен" in first.answers[0]
    assert repeat.answers == [], "второй раз клиента не благодарим"
    admin_msgs = [t for chat, t, _ in bot.messages if chat == ADMIN_ID]
    assert len(admin_msgs) == 1 and f"#{rid}" in admin_msgs[0]

    r = await tmp_db.get_request(rid)
    assert r.paid == 1 and r.pay_method == "online"
