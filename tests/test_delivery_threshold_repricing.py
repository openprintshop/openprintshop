"""Порог «бесплатно от суммы» у заказа, которому цену назначил МАСТЕР (волна 5, 09.08.2026).

Стоимость доставки фиксируется в шапке заказа при оформлении — по той сумме позиций, которая
известна ТОГДА. Но у половины путей оформления цены в этот момент нет вовсе: заказ без расчёта
(слайс не удался), индивидуальный заказ, «уточню цену вместе со счётом». Порог считался по нулю,
и доставка у таких заказов оставалась платной навсегда, даже когда итог давно перевалил
«бесплатно от». Здесь проверяется пересчёт под новую цену и всё, что он задевает.

Главное, что проверяется НЕ на удобство, а на деньги:

  • пересчёт двусторонний. Цена ниже порога → доставка снова становится ПЛАТНОЙ. Это не
    недосмотр: порог — правило прайса, а не подарок задним числом (см. осознанный тест ниже);
  • пересчёт МЕНЯЕТ СУММУ УЖЕ ВЫСТАВЛЕННОГО СЧЁТА, а Telegram сверяет её на pre_checkout —
    старый счёт после этого отклоняется, и клиент видит отказ платежа. Поэтому карточка заказа
    отдаёт мастеру блок предупреждения (group.delivery_recalc) с номерами позиций, у которых
    счёт висит;
  • стоимость, назначенную мастером РУКАМИ, тариф затирать не имеет права;
  • оплаченную доставку не трогаем вовсе — деньги уже получены.
"""
import json

import pytest
from conftest import stub_auth, stub_bot

from app import db, delivery, payments, webapp

UID = 6110
ADMIN_ID = 991


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeAdminOrderReq:
    """GET-запрос карточки заказа. caps=None → деньги видны (см. webguard._money_visible)."""

    def __init__(self, oid, caps=None, **query):
        self.query = query
        self.match_info = {"id": str(oid)}
        self._caps = caps

    def get(self, key, default=None):
        return self._caps if key == "caps" else default


class _FakeBot:
    def __init__(self):
        self.invoices = []
        self.messages = []

    async def send_invoice(self, chat_id, **kwargs):
        self.invoices.append((chat_id, kwargs))

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.messages.append((chat_id, text))


async def _group(*, prices=(50.0,), tariff=6.0, free_from=100.0, cost=None,
                 status="new", gid="thr01"):
    """Заказ с доставкой: тип с порогом + шапка группы. cost=None → стоимость по тарифу."""
    tid = await db.add_delivery_type("По городу", tariff, free_from=free_from)
    rids = []
    for i, price in enumerate(prices):
        rids.append(await db.add_request(
            user_id=UID, user_name="@t", model_label=f"M{i}", printer="C", plastic_name="PLA",
            quantity=1, quality="0.2", strength="Ст", grams_total=5, minutes_total=25,
            price=price, preview_path=None, order_group=gid, status=status))
    dcost = tariff if cost is None else cost
    await db.create_order_group(gid, UID, delivery_method="delivery", delivery_address="адрес",
                                total_snapshot=round(sum(prices) + dcost, 2),
                                delivery_type_id=tid, delivery_type_name="По городу",
                                delivery_cost=dcost)
    return tid, gid, rids


@pytest.fixture
def admin_env(monkeypatch):
    stub_auth(monkeypatch, _admin_body=lambda body: (ADMIN_ID, None))
    stub_auth(monkeypatch, _verify_admin=lambda init_data: ADMIN_ID)
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)
    return bot


async def _pay_on():
    await db.set_text_setting("pay_provider_token", "123:TEST:token")
    await db.set_text_setting("pay_currency", "RUB")


async def _bill(rid, price=None):
    body = {"initData": "x", "id": rid}
    if price is not None:
        body["price"] = price
    return json.loads((await webapp._api_admin_order_bill(_FakeRequest(body))).body.decode())


async def _set_price(rid, price):
    return json.loads((await webapp._api_admin_order_price(
        _FakeRequest({"initData": "x", "id": rid, "price": price}))).body.decode())


# ─────────────────────────── чистое правило ───────────────────────────
def test_threshold_recalcable_needs_a_threshold():
    """Без «бесплатно от» цена доставки от суммы не зависит — пересекать нечего."""
    assert not delivery.threshold_recalcable({"price": 6.0, "free_from": 0}, 6.0)
    assert not delivery.threshold_recalcable(None, 6.0)          # тип удалили после оформления
    assert delivery.threshold_recalcable({"price": 6.0, "free_from": 100}, 6.0)
    assert delivery.threshold_recalcable({"price": 6.0, "free_from": 100}, 0.0)


def test_threshold_recalcable_refuses_manual_cost():
    """Стоимость не равна ни тарифу, ни нулю → её вписал мастер, тариф её не затирает.

    Проверка держится на том, что type_cost умеет вернуть ровно два значения: тариф или 0."""
    z = {"price": 6.0, "free_from": 100}
    assert not delivery.threshold_recalcable(z, 12.5)
    assert not delivery.threshold_recalcable(z, 5.99)


def test_recalc_threshold_cost_both_directions():
    z = {"price": 6.0, "free_from": 100}
    assert delivery.recalc_threshold_cost(z, 6.0, 120) == 0.0     # перевалили порог → бесплатно
    assert delivery.recalc_threshold_cost(z, 0.0, 80) == 6.0      # упали ниже → снова платно
    assert delivery.recalc_threshold_cost(z, 6.0, 80) is None     # ничего не изменилось
    assert delivery.recalc_threshold_cost(z, 0.0, 100) is None    # порог нестрогий: 100 = бесплатно


def test_free_from_price_subtracts_sibling_positions():
    """Порог смотрит на ВЕСЬ заказ, а мастер вводит цену одной позиции — сёстры вычитаются."""
    z = {"price": 6.0, "free_from": 100}
    # заказ из двух позиций на 30 и 50; правим ту, что стоит 50 → своей ценой надо закрыть 70
    assert delivery.free_from_price(z, 6.0, items_total=80.0, own_price=50.0) == 70.0
    # сёстры уже перекрыли порог сами — доставка бесплатна при любой цене этой позиции
    assert delivery.free_from_price(z, 6.0, items_total=150.0, own_price=10.0) == -40.0
    assert delivery.free_from_price(z, 12.5, items_total=80.0, own_price=50.0) is None


# ──────────────── подарок мастера против бесплатного по порогу (11.08) ────────────────
def test_manual_source_blocks_recalc_even_at_zero():
    """Ноль двусмыслен: «перевалили порог» и «мастер подарил» — по числу неразличимы.

    До появления источника подарок считался пересчитываемым, то есть первая же смена цены
    молча выставляла клиенту счёт за доставку, которую ему подарили."""
    z = {"price": 6.0, "free_from": 100.0}
    assert delivery.threshold_recalcable(z, 0.0)                        # старая запись — как раньше
    assert not delivery.threshold_recalcable(z, 0.0, source=delivery.MANUAL)
    assert delivery.recalc_threshold_cost(z, 0.0, 50.0, delivery.MANUAL) is None
    assert delivery.recalc_threshold_cost(z, 0.0, 50.0) == 6.0          # без пометки — прежнее
    assert delivery.free_from_price(z, 0.0, 50.0, 50.0, delivery.MANUAL) is None


def test_is_gift_distinguishes_gift_from_threshold():
    """Карточке заказа нужно уметь сказать «подарена мастером», а не «бесплатно от 5 000»."""
    assert delivery.is_gift({"delivery_cost": 0, "delivery_cost_source": "manual"})
    assert not delivery.is_gift({"delivery_cost": 0, "delivery_cost_source": "auto"})
    assert not delivery.is_gift({"delivery_cost": 0})                   # старая запись — не знаем
    assert not delivery.is_gift({"delivery_cost": 6, "delivery_cost_source": "manual"})
    assert not delivery.is_gift(None)


@pytest.mark.asyncio
async def test_new_group_records_auto_source(tmp_db):
    """Заказ, оформленный клиентом, помечается как посчитанный сервером."""
    _tid, gid, _rids = await _group(prices=(50.0,))
    assert delivery.cost_source(await db.get_order_group(gid)) == delivery.AUTO


@pytest.mark.asyncio
async def test_master_gift_survives_price_drop(tmp_db, admin_env):
    """Сквозной сценарий дыры. Заказ перевалил порог → доставка ноль автоматом. Мастер
    подтверждает ноль руками (дарит). Потом роняет цену ниже порога — и подарок обязан
    устоять, хотя автоматика вернула бы тариф.

    Отдельно проверяется, что пометка записывается, даже когда СУММА не изменилась: ноль
    так и остался нулём, изменился только смысл."""
    _tid, gid, rids = await _group(prices=(150.0,), tariff=6.0, free_from=100.0, cost=0.0)
    j = json.loads((await webapp._api_admin_order_delivery(
        _FakeRequest({"initData": "x", "gid": gid, "cost": 0}))).body.decode())
    assert j["ok"], j
    ent = await db.get_order_group(gid)
    assert delivery.cost_source(ent) == delivery.MANUAL and delivery.is_gift(ent)

    await _set_price(rids[0], 50.0)                     # ниже порога: автоматика вернула бы 6.0
    assert delivery.group_delivery_cost(await db.get_order_group(gid)) == 0.0


@pytest.mark.asyncio
async def test_without_gift_mark_threshold_still_reverses(tmp_db, admin_env):
    """Контроль к предыдущему: без пометки обратный ход работает как прежде — это не
    регресс, а осознанное правило прайса."""
    _tid, gid, rids = await _group(prices=(150.0,), tariff=6.0, free_from=100.0, cost=0.0)
    await _set_price(rids[0], 50.0)
    assert delivery.group_delivery_cost(await db.get_order_group(gid)) == 6.0


# ─────────────────────────── пересечение порога ВВЕРХ ───────────────────────────
@pytest.mark.asyncio
async def test_bill_above_threshold_makes_delivery_free(tmp_db, admin_env):
    """Мастер выставил счёт с ценой выше порога — доставка обязана стать бесплатной."""
    _tid, gid, rids = await _group(prices=(0.0,), tariff=6.0, free_from=100.0)
    await _pay_on()
    out = await _bill(rids[0], price=120.0)
    assert out["ok"], out
    g = await db.get_order_group(gid)
    assert g["delivery_cost"] == 0.0
    assert g["total_snapshot"] == 120.0
    assert out["delivery"]["old"] == 6.0 and out["delivery"]["new"] == 0.0


@pytest.mark.asyncio
async def test_invoice_carries_recalculated_delivery(tmp_db, admin_env):
    """Пересчёт обязан случиться ДО отправки счёта, иначе счёт унесёт старую доставку."""
    _tid, _gid, rids = await _group(prices=(0.0,), tariff=6.0, free_from=100.0)
    await _pay_on()
    assert (await _bill(rids[0], price=120.0))["ok"]
    _chat, kwargs = admin_env.invoices[-1]
    assert [p.label for p in kwargs["prices"]] == [f"3D-печать, заказ #{rids[0]}"]
    assert kwargs["payload"] == payments.build_payload(rids[0], 12000)   # 120.00 без доставки


@pytest.mark.asyncio
async def test_client_accepting_price_recalculates_delivery(tmp_db, admin_env):
    """Мастер прислал цену на подтверждение — порог считается в момент ПРИНЯТИЯ клиентом."""
    _tid, gid, rids = await _group(prices=(0.0,), tariff=6.0, free_from=100.0)
    await db.update_request(rids[0], status="invoiced", pending_price=150.0)
    out = json.loads((await webapp._api_order_respond(
        _FakeRequest({"initData": "x", "order_id": rids[0], "accept": True}))).body.decode())
    assert out["ok"], out
    assert (await db.get_order_group(gid))["delivery_cost"] == 0.0


# ─────────────────────────── пересечение порога ВНИЗ ───────────────────────────
@pytest.mark.asyncio
async def test_price_below_threshold_makes_delivery_paid_again(tmp_db, admin_env):
    """ОСОЗНАННОЕ поведение: цена упала ниже порога → доставка снова платная.

    Порог — правило прайса («бесплатно от N»), а не подарок задним числом. Если заказ,
    подешевевший до порога, сохранял бы бесплатную доставку, мастер платил бы за перевозку
    из своего кармана — и ровно на тех заказах, где он и так уступил в цене."""
    _tid, gid, rids = await _group(prices=(150.0,), tariff=6.0, free_from=100.0, cost=0.0)
    out = await _set_price(rids[0], 40.0)          # дешевле → применяется сразу
    assert out["ok"] and out["applied"] == "direct"
    g = await db.get_order_group(gid)
    assert g["delivery_cost"] == 6.0
    assert g["total_snapshot"] == 46.0
    assert out["delivery"]["old"] == 0.0 and out["delivery"]["new"] == 6.0


@pytest.mark.asyncio
async def test_small_price_cut_that_raises_the_total_asks_the_client(tmp_db, admin_env):
    """Цена позиции упала, а платить клиенту БОЛЬШЕ — такое обязано идти на подтверждение.

    Ловушка, найденная приёмкой волны 5. Ветку «дешевле → применяем сразу, без спроса»
    выбирали по цене ПОЗИЦИИ, а доставка едет следом за ней через порог. Снижение 102 → 99
    роняет сумму ниже порога 100, доставка 0 → 6, и клиент получает «🎉 стало дешевле»
    вместе со счётом 105 вместо 102. Решение принимается по ИТОГУ."""
    _tid, gid, rids = await _group(prices=(102.0,), tariff=6.0, free_from=100.0, cost=0.0)
    out = await _set_price(rids[0], 99.0)
    assert out["ok"], out
    assert out.get("applied") != "direct", "подорожание по итогу применили без спроса клиента"

    # цена не записана, доставка не тронута — ждём ответа клиента
    g = await db.get_order_group(gid)
    assert g["delivery_cost"] == 0.0
    r = await db.get_request(rids[0])
    assert r.price == 102.0 and r.pending_price == 99.0

    # и клиенту НЕ ушло радостное «стало дешевле»
    assert not any("🎉" in str(m) for m in admin_env.messages), admin_env.messages


# ─────────────────────────── чего трогать нельзя ───────────────────────────
@pytest.mark.asyncio
async def test_order_without_delivery_is_untouched(tmp_db, admin_env):
    """Заказ без шапки и доставки: пересчёт не должен ни падать, ни заводить группу."""
    rid = await db.add_request(
        user_id=UID, user_name="@t", model_label="M", printer="C", plastic_name="PLA",
        quantity=1, quality="0.2", strength="Ст", grams_total=5, minutes_total=25,
        price=150.0, preview_path=None, status="new")
    assert await delivery.recalc_group_delivery(rid) is None
    out = await _set_price(rid, 40.0)
    assert out["ok"] and "delivery" not in out
    assert await db.group_id_of_request(rid) == ""


@pytest.mark.asyncio
async def test_manual_delivery_cost_survives_price_change(tmp_db, admin_env):
    """Мастер назначил стоимость руками (нестандартный адрес) — тариф её не перебивает."""
    _tid, gid, rids = await _group(prices=(150.0,), tariff=6.0, free_from=100.0, cost=12.5)
    out = await _set_price(rids[0], 40.0)
    assert out["ok"] and "delivery" not in out
    assert (await db.get_order_group(gid))["delivery_cost"] == 12.5


@pytest.mark.asyncio
async def test_paid_delivery_survives_price_change(tmp_db, admin_env):
    """Доставка уже оплачена онлайн — её сумму не пересчитываем: деньги получены."""
    _tid, gid, rids = await _group(prices=(50.0,), tariff=6.0, free_from=100.0)
    await db.update_order_group(gid, delivery_paid=1)
    out = await _set_price(rids[0], 10.0)
    assert out["ok"] and "delivery" not in out
    assert (await db.get_order_group(gid))["delivery_cost"] == 6.0


@pytest.mark.asyncio
async def test_deleted_delivery_type_leaves_cost_alone(tmp_db, admin_env):
    """Тип удалили после оформления — считать не по чему, снимок стоимости остаётся."""
    tid, gid, rids = await _group(prices=(50.0,), tariff=6.0, free_from=100.0)
    await db.delete_delivery_type(tid)
    assert await delivery.recalc_group_delivery(rids[0]) is None
    assert (await db.get_order_group(gid))["delivery_cost"] == 6.0


# ─────────────────────── предупреждение мастеру в карточке ───────────────────────
@pytest.mark.asyncio
async def test_card_warns_master_before_he_sets_the_price(tmp_db, admin_env):
    """Карточка отдаёт всё, чтобы предупредить ДО назначения цены: порог, тариф, текущую
    стоимость, цену этой позиции для бесплатной доставки и номера позиций с висящим счётом."""
    _tid, _gid, rids = await _group(prices=(0.0, 30.0), tariff=6.0, free_from=100.0,
                                    status="awaiting_payment")
    card = json.loads((await webapp._api_admin_order(
        _FakeAdminOrderReq(rids[0], initData="x"))).body.decode())
    dr = card["order"]["group"]["delivery_recalc"]
    assert dr["free_from"] == 100.0 and dr["tariff"] == 6.0 and dr["cost"] == 6.0
    assert dr["free_price"] == 70.0                     # сестра уже даёт 30 из 100
    assert dr["invoice_ids"] == rids                    # обе позиции ждут оплаты
    # доставка едет в счёте ЯКОРНОЙ позиции: правка цены одной ломает счёт другой
    assert dr["anchor_id"] == min(rids)


@pytest.mark.asyncio
async def test_card_is_silent_when_nothing_will_be_recalculated(tmp_db, admin_env):
    """Порога нет — предупреждать не о чем, блока в карточке быть не должно."""
    _tid, _gid, rids = await _group(prices=(50.0,), tariff=6.0, free_from=0.0)
    card = json.loads((await webapp._api_admin_order(
        _FakeAdminOrderReq(rids[0], initData="x"))).body.decode())
    assert card["order"]["group"]["delivery_recalc"] is None


@pytest.mark.asyncio
async def test_operator_does_not_see_the_money_in_the_hint(tmp_db, admin_env):
    """Блок целиком про деньги (тариф, порог) — у оператора его быть не может."""
    _tid, _gid, rids = await _group(prices=(50.0,), tariff=6.0, free_from=100.0)
    card = json.loads((await webapp._api_admin_order(
        _FakeAdminOrderReq(rids[0], caps=["orders"], initData="x"))).body.decode())
    assert card["money"] is False
    assert card["order"]["group"]["delivery_recalc"] is None


# ─────────────────── выставленный счёт после пересчёта ───────────────────
@pytest.mark.asyncio
async def test_open_invoice_stops_working_after_recalc(tmp_db, admin_env):
    """Главная плата за пересчёт: висящий у клиента счёт отклонится по несовпадению суммы.

    Именно это и должен увидеть мастер В ИНТЕРФЕЙСЕ до того, как назначит цену — иначе
    клиент вместо «стало дешевле» получит отказ платежа."""
    _tid, gid, rids = await _group(prices=(50.0,), tariff=6.0, free_from=100.0)
    await _pay_on()
    assert (await _bill(rids[0]))["ok"]                     # счёт по текущей цене: 50 + 6
    old_payload = admin_env.invoices[-1][1]["payload"]
    assert payments.parse_payload(old_payload) == (rids[0], 5600)
    assert await payments.precheckout_error(UID, old_payload, 5600, "RUB") is None

    assert (await _bill(rids[0], price=120.0))["ok"]        # цена выше порога → доставка 0
    assert (await db.get_order_group(gid))["delivery_cost"] == 0.0
    err = await payments.precheckout_error(UID, old_payload, 5600, "RUB")
    assert err and "изменилась" in err
    # а свежий счёт — на новую сумму без доставки, по нему платить можно
    new_payload = admin_env.invoices[-1][1]["payload"]
    assert payments.parse_payload(new_payload) == (rids[0], 12000)
    assert await payments.precheckout_error(UID, new_payload, 12000, "RUB") is None


# ─────────────── опоздавшая цена: заказ, оформленный ВО ВРЕМЯ слайса ───────────────
@pytest.mark.asyncio
async def test_late_default_delivery_keeps_the_gift(tmp_db, admin_env):
    """Второй заход доставки по умолчанию не имеет права отменить подарок мастера.

    Путь редкий, но денежный. Заказ, оформленный кнопкой «Оставить заказ» ВО ВРЕМЯ слайса,
    создаётся с ценой 0: порог по нулю не берётся, и шапка получает полный тариф. Мастер дарит
    доставку. И тут долетает результат слайса — `_apply_default_delivery` зовётся ВТОРОЙ раз,
    уже с настоящей ценой. Своя арифметика по профилю клиента возвращала в этот момент тариф,
    то есть выставляла счёт за подаренную доставку."""
    tid, gid, rids = await _group(prices=(0.0,), tariff=6.0, free_from=100.0)
    await db.set_user_delivery_prefs(UID, tid, "адрес")
    j = json.loads((await webapp._api_admin_order_delivery(
        _FakeRequest({"initData": "x", "gid": gid, "cost": 0}))).body.decode())
    assert j["ok"], j
    assert delivery.is_gift(await db.get_order_group(gid))

    await db.update_request(rids[0], price=50.0)          # слайс досчитался: цена НИЖЕ порога
    await webapp._apply_default_delivery(UID, rids[0], 50.0)

    ent = await db.get_order_group(gid)
    assert delivery.group_delivery_cost(ent) == 0.0
    assert delivery.cost_source(ent) == delivery.MANUAL


@pytest.mark.asyncio
async def test_late_default_delivery_reads_the_group_not_the_profile(tmp_db):
    """Опоздавшая цена пересчитывает доставку по тарифу ИЗ ШАПКИ, а не из профиля клиента.

    Профиль — не власть над уже оформленным заказом: клиент мог стереть адрес, пока шёл слайс,
    а мастер — сменить способ доставки. Прежний код в первом случае выходил ВООБЩЕ, оставляя
    стоимость, посчитанную по НУЛЕВОЙ сумме, — то есть полный тариф вместо бесплатной доставки,
    ради которой порог и заведён."""
    _tid, gid, rids = await _group(prices=(0.0,), tariff=6.0, free_from=100.0)
    prefs = await db.get_user_prefs(UID)
    assert not int(prefs.get("delivery_type_id") or 0), "у клиента умолчаний доставки нет"

    await db.update_request(rids[0], price=150.0)         # слайс досчитался: цена ВЫШЕ порога
    await webapp._apply_default_delivery(UID, rids[0], 150.0)

    ent = await db.get_order_group(gid)
    assert delivery.group_delivery_cost(ent) == 0.0       # порог сработал
    assert ent["total_snapshot"] == 150.0


@pytest.mark.asyncio
async def test_failed_invoice_rolls_delivery_back_with_the_price(tmp_db, admin_env, monkeypatch):
    """Telegram не принял счёт → откатываем цену; доставка обязана откатиться вместе с ней,
    иначе заказ остался бы со старой ценой и новой (обнулённой) доставкой."""
    _tid, gid, rids = await _group(prices=(50.0,), tariff=6.0, free_from=100.0)
    await _pay_on()

    async def _refuse(bot, r, note=""):
        return False

    monkeypatch.setattr(payments, "send_invoice", _refuse)
    out = await _bill(rids[0], price=120.0)
    assert out["ok"] is False
    r = await db.get_request(rids[0])
    assert r.price == 50.0 and r.status == "new"
    assert (await db.get_order_group(gid))["delivery_cost"] == 6.0
