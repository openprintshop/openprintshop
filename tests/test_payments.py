"""Онлайн-оплата через Telegram Payments (A1): payload, проверки pre_checkout,
идемпотентный леджер и отметка «оплачен»."""
import pytest
from conftest import stub_auth

import run
from app import payments, webapp
from app.handlers import common, user_flow
from app.handlers import payments as payments_handlers


async def _order(db, price=12.34, status="new", user_id=1):
    rid = await db.add_request(
        user_id=user_id, user_name="U", model_label="Бэйбизавр", printer="ECC2",
        plastic_name="PLA", quantity=1, quality="Стандарт", strength="Средняя",
        grams_total=17, minutes_total=100, price=price, preview_path=None, status=status)
    return rid


async def _enable_pay(db, token="123:TEST:token", currency="RUB"):
    await db.set_text_setting("pay_provider_token", token)
    await db.set_text_setting("pay_currency", currency)


# ---- payload ----

def test_payload_roundtrip():
    p = payments.build_payload(42, 1234)
    assert payments.parse_payload(p) == (42, 1234)


@pytest.mark.parametrize("junk", ["", None, "pay:x:1", "other:1:2", "pay:1", "pay:1:2:3"])
def test_payload_junk(junk):
    assert payments.parse_payload(junk) is None


def test_amount_minor_rounding():
    # int(12.34*100) == 1233 — обязателен round, иначе счёт на копейку меньше цены
    assert payments.amount_minor(12.34) == 1234
    assert payments.amount_minor(0) == 0
    assert payments.amount_minor(5) == 500


# ---- is_payable ----

class _R:
    def __init__(self, status="new", paid=0, price=10.0):
        self.status, self.paid, self.price = status, paid, price


def test_is_payable_matrix():
    assert payments.is_payable(_R())                                  # новый с ценой
    assert payments.is_payable(_R(status="awaiting_payment"))         # мастер выставил счёт
    assert payments.is_payable(_R(status="printed"))                  # оплата при получении
    assert not payments.is_payable(_R(), pay_on=False)                # оплата выключена
    assert not payments.is_payable(_R(paid=1))                        # уже оплачен
    assert not payments.is_payable(_R(status="invoiced"))             # сначала принять счёт
    assert not payments.is_payable(_R(status="cancelled"))
    assert not payments.is_payable(_R(price=0.5))                     # ниже минимума провайдера
    assert not payments.is_payable(None)


# ---- токен провайдера: валидация формы BotFather ----

def test_clean_provider_token():
    assert payments.clean_provider_token("  381764678:TEST:12345  ") == "381764678:TEST:12345"
    assert payments.clean_provider_token("381764678:LIVE:abc-def") == "381764678:LIVE:abc-def"
    assert payments.clean_provider_token("") == ""                    # пусто = выключить
    assert payments.clean_provider_token("   ") == ""
    # реальный случай 31.07: в поле вписали публичный ключ из кабинета провайдера
    assert payments.clean_provider_token("MIIBIjANBgkqhkiG9w0BAQEFA" + "A" * 300) is None
    assert payments.clean_provider_token("подключите оплату") is None
    assert payments.clean_provider_token("123:PROD:xxx") is None      # не TEST/LIVE
    assert payments.clean_provider_token(str(_BOT_TOKEN_LIKE)) is None  # токен БОТА — не провайдера


_BOT_TOKEN_LIKE = "1234567890:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-pc"


# ---- настройки ----

async def test_pay_settings_defaults(tmp_db):
    token, currency = await payments.pay_settings()
    assert token == ""                       # из коробки оплата выключена
    assert currency == "RUB"
    assert not await payments.enabled()
    await _enable_pay(tmp_db, currency="byn")   # регистр нормализуется
    assert await payments.enabled()
    assert (await payments.pay_settings())[1] == "BYN"


# ---- pre_checkout ----

async def test_precheckout_ok(tmp_db):
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db, price=12.34)
    payload = payments.build_payload(rid, 1234)
    assert await payments.precheckout_error(1, payload, 1234, "RUB") is None


async def test_precheckout_rejects(tmp_db):
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db, price=12.34)
    ok_payload = payments.build_payload(rid, 1234)
    # мусорный payload
    assert await payments.precheckout_error(1, "junk", 1234, "RUB")
    # чужой пользователь
    assert await payments.precheckout_error(2, ok_payload, 1234, "RUB")
    # валюта не совпадает с настройкой
    assert await payments.precheckout_error(1, ok_payload, 1234, "USD")
    # цена изменилась после выставления счёта — старый счёт больше не действует
    await tmp_db.update_request(rid, price=20.0)
    err = await payments.precheckout_error(1, ok_payload, 1234, "RUB")
    assert err and "измени" in err.lower()
    # уже оплачен
    await tmp_db.update_request(rid, price=12.34, paid=1)
    assert await payments.precheckout_error(1, ok_payload, 1234, "RUB")
    # статус, в котором платить нельзя
    await tmp_db.update_request(rid, paid=0, status="rejected")
    assert await payments.precheckout_error(1, ok_payload, 1234, "RUB")


# ---- successful_payment ----

async def test_record_success_marks_paid(tmp_db):
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db, price=12.34)
    payload = payments.build_payload(rid, 1234)
    r = await payments.record_success(1, payload, 1234, "RUB", "tg_ch_1", "prov_ch_1")
    assert r is not None and r.id == rid
    assert r.paid == 1 and r.pay_method == "online" and r.paid_at > 0
    ledger = await tmp_db.payments_for_request(rid)
    assert len(ledger) == 1
    assert ledger[0]["amount"] == 12.34
    assert ledger[0]["currency"] == "RUB"
    assert ledger[0]["tg_charge_id"] == "tg_ch_1"


async def test_record_success_idempotent(tmp_db):
    """Telegram может доставить successful_payment повторно — второй раз тишина."""
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db)
    payload = payments.build_payload(rid, payments.amount_minor(12.34))
    assert await payments.record_success(1, payload, 1234, "RUB", "tg_dup") is not None
    assert await payments.record_success(1, payload, 1234, "RUB", "tg_dup") is None
    assert len(await tmp_db.payments_for_request(rid)) == 1


async def test_record_success_foreign_payload(tmp_db):
    assert await payments.record_success(1, "garbage", 1234, "RUB", "tg_x") is None


# ---- этап «Ждёт оплаты» (фидбэк 31.07) ----

async def test_awaiting_payment_auto_moves_to_in_progress(tmp_db):
    """Оплата заказа в «Ждёт оплаты» сама переводит его «В работе» — оплата и была
    недостающим шагом. Таймлайн получает запись о переходе."""
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db, price=12.34, status="awaiting_payment")
    r = await payments.record_success(1, payments.build_payload(rid, 1234), 1234, "RUB", "tg_aw")
    assert r.status == "in_progress" and r.paid == 1 and r.pay_method == "online"
    hist = [s for s, _ in await tmp_db.get_status_history(rid)]
    assert hist[-1] == "in_progress"


async def test_paid_from_other_statuses_keeps_status(tmp_db):
    """Оплата из других статусов статус НЕ двигает: очередью управляет мастер."""
    await _enable_pay(tmp_db)
    for st in ("new", "printed"):
        rid = await _order(tmp_db, price=10.0, status=st)
        r = await payments.record_success(
            1, payments.build_payload(rid, 1000), 1000, "RUB", f"tg_keep_{st}")
        assert r.status == st and r.paid == 1


async def test_awaiting_payment_is_cancellable_and_reserves(tmp_db, monkeypatch, post_req, body):
    """«Ждёт оплаты» — ЖИВОЙ заказ, а не отложенный: клиент может отменить его из мини-аппа,
    филамент под него зарезервирован, а время учтено в загрузке очереди. Статус добавлялся
    в четыре списка сразу (31.07), и забытый список молча ломает либо склад, либо ETA."""
    rid = await _order(tmp_db, price=12.34, status="awaiting_payment")
    await tmp_db.set_order_stock_plan(rid, [{"plastic_id": 3, "grams": 17}])

    load = await tmp_db.queue_load()
    assert load["count"] == 1 and load["minutes"] == 100
    assert (await tmp_db.reserved_grams_by_plastic()).get(3) == 17

    stub_auth(monkeypatch, _verify_init_data=lambda s: 1)
    resp = await webapp._api_order_cancel(post_req({"initData": "x", "order_id": rid}))
    assert body(resp) == {"ok": True}
    assert (await tmp_db.get_request(rid)).status == "cancelled"

    # отменённый — уже не активный: ни в очереди, ни в резерве склада
    assert (await tmp_db.queue_load())["count"] == 0
    assert await tmp_db.reserved_grams_by_plastic() == {}


def test_payments_router_included_before_fallbacks():
    """Платёжный роутер обязан подключаться ПЕРВЫМ: catch-all message-фолбэк user_flow
    (StateFilter(None)) иначе съедает successful_payment — деньги списаны, заказ не помечен
    (инцидент 31.07, заказ #87: бот ответил на платёж «Нажмите /start»).

    Смотрим СОБРАННЫЙ диспетчер, а не текст run.py: sub_routers — список подключённых
    роутеров в порядке include_router (aiogram 3: атрибут, не метод). Роутеры — модульные
    синглтоны и помнят родителя, поэтому после сборки отвязываем: иначе второй вызов
    build_dispatcher() в процессе (его зовёт ещё и tests/test_errorlog.py) упал бы
    «Router is already attached»."""
    dp = run.build_dispatcher()
    subs = list(dp.sub_routers)
    for r in subs:
        r._parent_router = None
    assert subs.index(payments_handlers.router) == 0
    assert subs.index(payments_handlers.router) < subs.index(common.router)
    assert subs.index(payments_handlers.router) < subs.index(user_flow.router)


# ---- ручная галочка мастера не перетирает онлайн-оплату ----

async def test_manual_unpaid_keeps_ledger(tmp_db):
    """Снятие галочки «оплачен» с онлайн-оплаченного заказа блокируется на уровне API
    (webapp: pay_method == 'online' → отказ); здесь фиксируем, что леджер не зависит
    от manual-полей: запись остаётся даже после update_request."""
    await _enable_pay(tmp_db)
    rid = await _order(tmp_db)
    await payments.record_success(1, payments.build_payload(rid, 1234), 1234, "RUB", "tg_1")
    await tmp_db.update_request(rid, paid=0, paid_at=0, pay_method="")
    assert len(await tmp_db.payments_for_request(rid)) == 1
