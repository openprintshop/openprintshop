"""Доставка: деньги и трекинг (A2).

Корзина (B5) уже умела спрашивать способ получения и адрес, но доставка была информативной:
без цены, тарифов и трек-номера. Здесь проверяем ровно то, что добавилось:

- стоимость доставки считает СЕРВЕР по типу; клиент присылает только id тарифа;
- порог «бесплатно от суммы» смотрит на сумму ПОЗИЦИЙ и сравнивается нестрого;
- итог заказа = позиции + доставка (и в шапке группы, и в клиентской карточке);
- в онлайн-счёте доставка едет ОТДЕЛЬНОЙ строкой и ровно ОДИН раз на заказ — на якорной
  позиции группы (иначе клиент заплатил бы за доставку столько раз, сколько в заказе позиций);
- pre_checkout сверяет полную сумму (позиция + доставка), а оплата отмечает доставку
  оплаченной, чтобы следующий счёт по той же группе её не добавил;
- трек-номер: ссылка собирается по шаблону перевозчика, уведомление уходит клиенту.
"""
import json
from types import SimpleNamespace

import pytest
from conftest import stub_auth, stub_bot

from app import config, db, delivery, payments, pricing, quote, texts, webapp

UID = 5150
ADMIN_ID = 778


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeGet:
    def __init__(self, **query):
        self.query = query


class _FakeAdminOrderReq:
    def __init__(self, oid, **query):
        self.query = query
        self.match_info = {"id": str(oid)}


class _FakeBot:
    def __init__(self):
        self.messages = []
        self.invoices = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))

    async def send_invoice(self, chat_id, **kwargs):
        self.invoices.append((chat_id, kwargs))


def _user():
    return {"id": UID, "username": "tester", "first_name": "Тест"}


def _seed_model(token, *, label, price):
    webapp._models[token] = {
        "uid": UID, "label": label, "paths": [], "preview": None,
        "last": {"printer": "Centauri", "plastic_name": "PLA · чёрный", "qty": 1,
                 "quality": "0.20", "strength": "Стандарт", "grams_total": 20.0,
                 "minutes_total": 100.0, "total": price, "dims": "40×40×20",
                 "color": "чёрный", "scale_pct": 100},
    }


async def _order_cart(body):
    resp = await webapp._api_order_cart(_FakeRequest(body))
    return json.loads(resp.body.decode())


# ─────────────────────────── расчёт стоимости типа ───────────────────────────
def test_type_cost_plain():
    z = {"price": 7.5, "free_from": 0}
    assert delivery.type_cost(z, 0) == 7.5
    assert delivery.type_cost(z, 1000) == 7.5


def test_type_cost_free_threshold_is_inclusive():
    """«Бесплатно от 50» означает, что ровно 50 уже бесплатно (>=, а не >)."""
    z = {"price": 7.5, "free_from": 50}
    assert delivery.type_cost(z, 49.99) == 7.5
    assert delivery.type_cost(z, 50) == 0.0
    assert delivery.type_cost(z, 50.01) == 0.0


def test_type_cost_without_type_is_free():
    assert delivery.type_cost(None, 100) == 0.0
    assert delivery.type_cost({"price": -5, "free_from": 0}, 10) == 0.0   # мусор не уводит в минус


def test_track_url_from_carrier_template():
    url = delivery.track_url("cdek", "1234567890")
    assert url.endswith("1234567890") and url.startswith("https://")
    assert delivery.track_url("cdek", "") == ""            # без номера ссылки нет
    assert delivery.track_url("courier", "123") == ""      # у своего курьера шаблона нет


def test_track_url_custom_must_be_http():
    assert delivery.track_url("other", "1", "https://track.example/1") == "https://track.example/1"
    assert delivery.track_url("other", "1", "javascript:alert(1)") == ""   # чужая схема — мимо


def test_clean_number_strips_garbage():
    assert delivery.clean_number("  10 20-АБ 30x ") == "1020-30x"


# ─────────────────────────── оформление с типом доставки ───────────────────────────
@pytest.fixture
def cart_env(monkeypatch):
    stub_auth(monkeypatch, _verify_user=lambda init_data: _user())
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID], raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)
    return bot


@pytest.mark.asyncio
async def test_checkout_charges_delivery_by_type(tmp_db, cart_env):
    tid = await db.add_delivery_type("По городу", 6.0, free_from=100, eta="1–2 дня")
    _seed_model("t1", label="A", price=10.0)
    _seed_model("t2", label="B", price=15.0)
    out = await _order_cart({"initData": "x", "tokens": ["t1", "t2"],
                             "delivery_method": "delivery", "delivery_address": "ул. Пример, 5",
                             "delivery_type_id": tid})
    assert out["ok"] and out["count"] == 2
    g = await db.get_order_group(out["group"])
    assert g["delivery_cost"] == 6.0
    assert g["delivery_type_id"] == tid and g["delivery_type_name"] == "По городу"
    assert g["total_snapshot"] == 31.0        # 25 позиций + 6 доставка


@pytest.mark.asyncio
async def test_checkout_free_delivery_above_threshold(tmp_db, cart_env):
    tid = await db.add_delivery_type("По городу", 6.0, free_from=20)
    _seed_model("t1", label="A", price=25.0)
    out = await _order_cart({"initData": "x", "tokens": ["t1"], "delivery_method": "delivery",
                             "delivery_address": "адрес", "delivery_type_id": tid})
    g = await db.get_order_group(out["group"])
    assert g["delivery_cost"] == 0.0 and g["total_snapshot"] == 25.0


@pytest.mark.asyncio
async def test_pickup_ignores_type(tmp_db, cart_env):
    """Самовывоз не платный, даже если клиент прислал id типа доставки."""
    tid = await db.add_delivery_type("По городу", 6.0)
    _seed_model("t1", label="A", price=10.0)
    out = await _order_cart({"initData": "x", "tokens": ["t1"], "delivery_method": "pickup",
                             "delivery_type_id": tid})
    g = await db.get_order_group(out["group"])
    assert g["delivery_cost"] == 0.0 and g["delivery_type_id"] is None


@pytest.mark.asyncio
async def test_disabled_type_is_not_charged(tmp_db, cart_env):
    tid = await db.add_delivery_type("Старый тариф", 99.0)
    await db.update_delivery_type(tid, is_active=0)
    _seed_model("t1", label="A", price=10.0)
    out = await _order_cart({"initData": "x", "tokens": ["t1"], "delivery_method": "delivery",
                             "delivery_address": "адрес", "delivery_type_id": tid})
    g = await db.get_order_group(out["group"])
    assert g["delivery_cost"] == 0.0


@pytest.mark.asyncio
async def test_client_price_is_never_trusted(tmp_db, cart_env):
    """Даже если клиент пришлёт свою «стоимость доставки», сервер считает по типу сам."""
    tid = await db.add_delivery_type("По городу", 6.0)
    _seed_model("t1", label="A", price=10.0)
    out = await _order_cart({"initData": "x", "tokens": ["t1"], "delivery_method": "delivery",
                             "delivery_address": "адрес", "delivery_type_id": tid,
                             "delivery_cost": 0, "cost": 0})
    g = await db.get_order_group(out["group"])
    assert g["delivery_cost"] == 6.0


@pytest.mark.asyncio
async def test_client_card_totals_include_delivery(tmp_db, cart_env, monkeypatch):
    tid = await db.add_delivery_type("По городу", 6.0)
    _seed_model("t1", label="A", price=10.0)
    _seed_model("t2", label="B", price=15.0)
    await _order_cart({"initData": "x", "tokens": ["t1", "t2"], "delivery_method": "delivery",
                       "delivery_address": "адрес", "delivery_type_id": tid})
    out = json.loads((await webapp._api_orders(_FakeGet(initData="x"))).body.decode())
    card = [c for c in out["orders"] if c.get("group")][0]
    assert "31" in card["total"]                     # 25 + 6 — итог с доставкой
    assert card["delivery_cost"] and card["items_total"]
    assert card["delivery_type"] == "По городу"


@pytest.mark.asyncio
async def test_admin_group_block_has_delivery_and_track(tmp_db, cart_env, monkeypatch):
    tid = await db.add_delivery_type("По городу", 6.0)
    _seed_model("t1", label="A", price=10.0)
    out = await _order_cart({"initData": "x", "tokens": ["t1"], "delivery_method": "delivery",
                             "delivery_address": "адрес", "delivery_type_id": tid})
    rid = (await db.list_requests_by_group(out["group"]))[0].id
    stub_auth(monkeypatch, _verify_admin=lambda init_data: ADMIN_ID)
    card = json.loads((await webapp._api_admin_order(
        _FakeAdminOrderReq(rid, initData="x"))).body.decode())
    g = card["order"]["group"]
    # блок группы отдаётся даже для ОДНОЙ позиции — иначе мастеру негде ввести трек-номер
    assert g and g["delivery_cost"] == 6.0 and g["total_with_delivery"] == 16.0
    assert g["track"]["number"] == "" and g["track"]["shipped_at"] == 0


# ─────────────────────────── доставка в оплате ───────────────────────────
async def _group_with_delivery(cost=6.0, prices=(10.0, 15.0)):
    gid = "deliv" + str(int(cost * 100)).zfill(6)
    rids = []
    for i, price in enumerate(prices):
        rids.append(await db.add_request(
            user_id=UID, user_name="@t", model_label=f"M{i}", printer="C", plastic_name="PLA",
            quantity=1, quality="0.2", strength="Ст", grams_total=5, minutes_total=25,
            price=price, preview_path=None, order_group=gid, status="new"))
    await db.create_order_group(gid, UID, delivery_method="delivery", delivery_address="адрес",
                                total_snapshot=sum(prices) + cost, delivery_cost=cost)
    return gid, rids


@pytest.mark.asyncio
async def test_delivery_rides_only_on_anchor_position(tmp_db):
    gid, rids = await _group_with_delivery(cost=6.0)
    anchor, other = await db.get_request(rids[0]), await db.get_request(rids[1])
    assert await payments.delivery_extra_minor(anchor) == 600
    assert await payments.delivery_extra_minor(other) == 0        # второй раз не берём
    assert await payments.order_total_minor(anchor) == 1000 + 600
    assert await payments.order_total_minor(other) == 1500


@pytest.mark.asyncio
async def test_single_order_without_group_has_no_extra(tmp_db):
    rid = await db.add_request(
        user_id=UID, user_name="@t", model_label="Один", printer="C", plastic_name="PLA",
        quantity=1, quality="0.2", strength="Ст", grams_total=5, minutes_total=25,
        price=10.0, preview_path=None, status="new")
    assert await payments.delivery_extra_minor(await db.get_request(rid)) == 0


@pytest.mark.asyncio
async def test_invoice_has_separate_delivery_line(tmp_db, monkeypatch):
    gid, rids = await _group_with_delivery(cost=6.0)
    monkeypatch.setattr(payments, "pay_settings",
                        lambda: _async_value(("123:TEST:token", "RUB")))
    bot = _FakeBot()
    ok = await payments.send_invoice(bot, await db.get_request(rids[0]))
    assert ok and len(bot.invoices) == 1
    kwargs = bot.invoices[0][1]
    labels = [p.label for p in kwargs["prices"]]
    assert "Доставка" in labels
    assert sum(p.amount for p in kwargs["prices"]) == 1600
    assert kwargs["payload"].endswith(":1600")     # payload несёт ПОЛНУЮ сумму


def _async_value(v):
    async def _f():
        return v
    return _f()


@pytest.mark.asyncio
async def test_precheckout_expects_total_with_delivery(tmp_db, monkeypatch):
    gid, rids = await _group_with_delivery(cost=6.0)
    monkeypatch.setattr(payments, "pay_settings", lambda: _async_value(("123:TEST:t", "RUB")))
    payload = payments.build_payload(rids[0], 1600)
    assert await payments.precheckout_error(UID, payload, 1600, "RUB") is None
    # сумма без доставки — счёт устарел, деньги не спишутся
    stale = payments.build_payload(rids[0], 1000)
    assert await payments.precheckout_error(UID, stale, 1000, "RUB") is not None


@pytest.mark.asyncio
async def test_payment_marks_delivery_paid_once(tmp_db, monkeypatch):
    gid, rids = await _group_with_delivery(cost=6.0)
    monkeypatch.setattr(payments, "pay_settings", lambda: _async_value(("123:TEST:t", "RUB")))
    await payments.record_success(UID, payments.build_payload(rids[0], 1600), 1600, "RUB",
                                  tg_charge_id="chg-1")
    g = await db.get_order_group(gid)
    assert int(g["delivery_paid"]) == 1
    # доставка оплачена → счёт по этой же группе больше её не добавляет
    assert await payments.delivery_extra_minor(await db.get_request(rids[0])) == 0


@pytest.mark.asyncio
async def test_paying_non_anchor_does_not_mark_delivery(tmp_db, monkeypatch):
    gid, rids = await _group_with_delivery(cost=6.0)
    monkeypatch.setattr(payments, "pay_settings", lambda: _async_value(("123:TEST:t", "RUB")))
    await payments.record_success(UID, payments.build_payload(rids[1], 1500), 1500, "RUB",
                                  tg_charge_id="chg-2")
    g = await db.get_order_group(gid)
    assert int(g["delivery_paid"]) == 0     # платили только за позицию — доставка ещё висит


# ─────────────────────────── отправление и трек-номер ───────────────────────────
@pytest.mark.asyncio
async def test_ship_notifies_client_with_tracking(tmp_db, monkeypatch):
    gid, rids = await _group_with_delivery(cost=6.0)
    stub_auth(monkeypatch, _admin_body=lambda body: (ADMIN_ID, None))
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)
    resp = await webapp._api_admin_order_shipment(_FakeRequest(
        {"initData": "x", "gid": gid, "action": "ship", "carrier": "cdek", "number": "1234567890"}))
    out = json.loads(resp.body.decode())
    assert out["ok"] and out["track"]["number"] == "1234567890"
    assert out["track"]["url"].endswith("1234567890")
    g = await db.get_order_group(gid)
    assert int(g["shipped_at"]) > 0
    assert bot.messages and "1234567890" in bot.messages[0][1]
    assert bot.messages[0][0] == UID


@pytest.mark.asyncio
async def test_clear_shipment_resets_track(tmp_db, monkeypatch):
    gid, _rids = await _group_with_delivery(cost=6.0)
    stub_auth(monkeypatch, _admin_body=lambda body: (ADMIN_ID, None))
    stub_bot(monkeypatch, _FakeBot())
    await webapp._api_admin_order_shipment(_FakeRequest(
        {"initData": "x", "gid": gid, "action": "ship", "carrier": "cdek", "number": "111"}))
    await webapp._api_admin_order_shipment(_FakeRequest(
        {"initData": "x", "gid": gid, "action": "clear"}))
    g = await db.get_order_group(gid)
    assert not g["track_number"] and not g["shipped_at"]


@pytest.mark.asyncio
async def test_ship_requires_carrier_or_number(tmp_db, monkeypatch):
    gid, _rids = await _group_with_delivery(cost=6.0)
    stub_auth(monkeypatch, _admin_body=lambda body: (ADMIN_ID, None))
    stub_bot(monkeypatch, _FakeBot())
    out = json.loads((await webapp._api_admin_order_shipment(_FakeRequest(
        {"initData": "x", "gid": gid, "action": "ship"}))).body.decode())
    assert out["ok"] is False


# ─────────────────────────── правка доставки мастером ───────────────────────────
@pytest.mark.asyncio
async def test_master_can_change_delivery_cost(tmp_db, monkeypatch):
    gid, _rids = await _group_with_delivery(cost=6.0)
    stub_auth(monkeypatch, _admin_body=lambda body: (ADMIN_ID, None))
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)
    out = json.loads((await webapp._api_admin_order_delivery(_FakeRequest(
        {"initData": "x", "gid": gid, "cost": 12.5}))).body.decode())
    assert out["ok"] and out["delivery_cost"] == 12.5
    g = await db.get_order_group(gid)
    assert g["delivery_cost"] == 12.5 and g["total_snapshot"] == 37.5   # 25 позиций + 12.5
    assert bot.messages, "клиента предупреждаем: сумма к оплате изменилась"


@pytest.mark.asyncio
async def test_paid_delivery_is_not_editable(tmp_db, monkeypatch):
    """Доставка уже оплачена онлайн — менять её сумму нельзя: деньги получены."""
    gid, _rids = await _group_with_delivery(cost=6.0)
    await db.update_order_group(gid, delivery_paid=1)
    stub_auth(monkeypatch, _admin_body=lambda body: (ADMIN_ID, None))
    stub_bot(monkeypatch, _FakeBot())
    out = json.loads((await webapp._api_admin_order_delivery(_FakeRequest(
        {"initData": "x", "gid": gid, "cost": 1.0}))).body.decode())
    assert out["ok"] is False
    assert (await db.get_order_group(gid))["delivery_cost"] == 6.0


@pytest.mark.asyncio
async def test_type_recalc_uses_current_items_total(tmp_db, monkeypatch):
    """Мастер переставил тип — цена берётся по тарифу на ТЕКУЩУЮ сумму позиций."""
    gid, _rids = await _group_with_delivery(cost=0.0)
    tid = await db.add_delivery_type("Область", 9.0, free_from=100)
    stub_auth(monkeypatch, _admin_body=lambda body: (ADMIN_ID, None))
    stub_bot(monkeypatch, _FakeBot())
    out = json.loads((await webapp._api_admin_order_delivery(_FakeRequest(
        {"initData": "x", "gid": gid, "type_id": tid}))).body.decode())
    assert out["ok"] and out["delivery_cost"] == 9.0        # 25 < 100 → порог не сработал


# ─────────────────────────── тексты уведомлений ───────────────────────────
def test_delivery_line_prints_price_only_when_paid():
    free = texts._delivery_line("delivery", "ул. Пример", delivery_cost=0, delivery_type="Город",
                                cur="руб.")
    assert "Город" in free and "руб." not in free
    paid = texts._delivery_line("delivery", "ул. Пример", delivery_cost=6, delivery_type="Город",
                                cur="руб.")
    assert "6" in paid


def test_user_cart_note_shows_total_with_delivery():
    note = texts.user_cart_order_note(count=2, total=25.0, cur="руб.", delivery_method="delivery",
                                      delivery_address="адрес", delivery_cost=6.0,
                                      delivery_type="Город")
    assert "31" in note        # итог с доставкой проговорён отдельной строкой


# ─────────────────────────── доставка в PDF-смете ───────────────────────────
_QSETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.0, "failure_rate_pct": 8.0,
    "profit_margin_pct": 50.0, "min_price": 5.0, "multicolor_waste_pct": 12.0,
    "multicolor_time_pct": 15.0, "volume_threshold_cm3": 0.0, "volume_surcharge_pct": 0.0,
    "rush_surcharge_pct": 50.0, "term_split_hours": 12,
}


def _quote_pdf(tmp_path, monkeypatch, **extra):
    monkeypatch.setattr(config, "QUOTE_DIR", tmp_path)
    bd = pricing.compute_price(
        30.0, 180.0, 1, SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None),
        _QSETTINGS)
    pdf = quote.build_quote(
        business_name="X", business_contact="c", model_label="M", printer_name="P",
        plastic_name="PLA", qty=1, quality_label="q", strength_label="s", bd=bd, cur="руб.",
        settings=_QSETTINGS, preview_path=None, **extra)
    return bd, pdf


def test_quote_pdf_builds_with_delivery(tmp_path, monkeypatch):
    """Смета с доставкой строится и не падает; без доставки — прежний документ."""
    _bd, plain = _quote_pdf(tmp_path, monkeypatch)
    _bd2, with_d = _quote_pdf(tmp_path, monkeypatch, delivery_cost=6.0, delivery_label="По городу")
    assert plain.exists() and plain.stat().st_size > 0
    assert with_d.exists() and with_d.stat().st_size > 0
    # строка доставки и увеличенный ИТОГО делают документ длиннее
    assert with_d.stat().st_size != plain.stat().st_size


@pytest.mark.asyncio
async def test_pdf_delivery_line_only_on_anchor(tmp_db, monkeypatch, tmp_path):
    """Смета по ВТОРОЙ позиции группы доставку не показывает — иначе клиент решил бы,
    что платит за неё дважды. Проверяем через аргументы, с которыми зовётся build_quote."""
    gid, rids = await _group_with_delivery(cost=6.0)
    bd = pricing.compute_price(
        30.0, 180.0, 1, SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None),
        _QSETTINGS)
    for rid in rids:
        await db.update_request(rid, breakdown=pricing.breakdown_to_dict(bd, _QSETTINGS))

    seen = {}

    def _fake_build(**kwargs):
        seen[kwargs["model_label"]] = kwargs.get("delivery_cost", 0.0)
        p = tmp_path / f"{kwargs['model_label']}.pdf"
        p.write_bytes(b"%PDF-1.4")
        return p

    class _Bot:
        async def send_document(self, *a, **k):
            return None

    stub_auth(monkeypatch, _verify_init_data=lambda init: UID)
    stub_bot(monkeypatch, _Bot())
    monkeypatch.setattr(quote, "build_quote", _fake_build)

    for rid in rids:
        await webapp._api_order_pdf(_FakeRequest({"initData": "x", "id": rid}))
    assert seen["M0"] == 6.0        # якорь несёт доставку
    assert seen["M1"] == 0.0        # вторая позиция — нет


def test_types_api_marks_free_for_this_cart():
    z = {"id": 1, "name": "Город", "price": 6.0, "free_from": 20, "eta": "", "comment": "",
         "is_active": 1, "sort": 0}
    assert delivery.type_json(z, 25)["free"] is True
    assert delivery.type_json(z, 10)["cost"] == 6.0


# ═════════════════ доставка по умолчанию и заказ БЕЗ корзины (доработка 31.07) ═════════════════
# Клиент задаёт тип доставки и адрес один раз в профиле. Дальше:
#   - корзина подставляет их сама (и всё равно пересчитывает цену на сервере);
#   - заказ В ОДИН ТАП (со сметы, из чата, покупка готового изделия) получает шапку из ОДНОЙ
#     позиции с этой доставкой — иначе адресу и трек-номеру негде жить;
#   - ни одного типа не заведено → доставки у клиента нет вообще (мастерская на самовывозе).
@pytest.mark.asyncio
async def test_prefs_reject_disabled_type(tmp_db):
    """Выключенный тип в умолчания не попадёт: иначе он «протух» бы после правки прайса."""
    tid = await db.add_delivery_type("Курьером", 6.0)
    prefs = await db.set_user_delivery_prefs(UID, tid, "  ул. Пример, 5  ")
    assert prefs["delivery_type_id"] == tid
    assert prefs["delivery_address"] == "ул. Пример, 5"      # обрезка пробелов
    await db.update_delivery_type(tid, is_active=0)
    prefs = await db.set_user_delivery_prefs(UID, tid, "ул. Пример, 5")
    assert prefs["delivery_type_id"] == 0                    # тип выключен — не сохраняем


@pytest.mark.asyncio
async def test_prefs_do_not_clobber_price_settings(tmp_db):
    """Две независимые секции профиля живут в одной строке — и не затирают друг друга."""
    await db.set_user_price_prefs(UID, "pct", 15)
    tid = await db.add_delivery_type("Курьером", 6.0)
    await db.set_user_delivery_prefs(UID, tid, "адрес")
    prefs = await db.get_user_prefs(UID)
    assert prefs["price_mode"] == "pct" and prefs["price_pct"] == 15.0
    assert prefs["delivery_type_id"] == tid
    await db.set_user_price_prefs(UID, "ask", 0)
    prefs = await db.get_user_prefs(UID)
    assert prefs["delivery_address"] == "адрес"              # доставка пережила правку цены


@pytest.mark.asyncio
async def test_single_order_gets_delivery_from_prefs(tmp_db, cart_env):
    """Заказ со сметы (без корзины) уезжает по адресу из профиля и обрастает шапкой."""
    tid = await db.add_delivery_type("Курьером", 6.0)
    await db.set_user_delivery_prefs(UID, tid, "ул. Пример, 5")
    _seed_model("t1", label="Кубик", price=10.0)
    resp = await webapp._api_order(_FakeRequest({"initData": "x", "token": "t1"}))
    assert json.loads(resp.body.decode())["ok"]
    rows = await db.list_requests(10)
    rid = rows[0].id
    gid = await db.group_id_of_request(rid)
    assert gid, "одиночный заказ должен получить шапку с доставкой"
    g = await db.get_order_group(gid)
    assert g["delivery_method"] == "delivery" and g["delivery_address"] == "ул. Пример, 5"
    assert g["delivery_cost"] == 6.0 and g["delivery_type_name"] == "Курьером"
    assert g["total_snapshot"] == 16.0
    # клиенту про доставку сказали ДО того, как он увидит счёт
    assert any("Курьером" in t for _chat, t in cart_env.messages)


@pytest.mark.asyncio
async def test_single_order_without_prefs_has_no_group(tmp_db, cart_env):
    """Умолчаний нет — поведение ровно как раньше: ни шапки, ни доставки."""
    await db.add_delivery_type("Курьером", 6.0)
    _seed_model("t1", label="Кубик", price=10.0)
    await webapp._api_order(_FakeRequest({"initData": "x", "token": "t1"}))
    rid = (await db.list_requests(10))[0].id
    assert await db.group_id_of_request(rid) == ""


@pytest.mark.asyncio
async def test_single_order_delivery_rides_in_invoice(tmp_db, cart_env, monkeypatch):
    """Доставка одиночного заказа тоже попадает в онлайн-счёт — на его единственную позицию."""
    tid = await db.add_delivery_type("Курьером", 6.0)
    await db.set_user_delivery_prefs(UID, tid, "адрес")
    _seed_model("t1", label="Кубик", price=10.0)
    await webapp._api_order(_FakeRequest({"initData": "x", "token": "t1"}))
    r = await db.get_request((await db.list_requests(10))[0].id)
    assert await payments.delivery_extra_minor(r) == 600
    assert await payments.order_total_minor(r) == 1600


@pytest.mark.asyncio
async def test_single_order_card_is_not_a_cart_card(tmp_db, cart_env, monkeypatch):
    """Заказ из ОДНОЙ позиции остаётся обычной карточкой, а не «🧺 Заказ · 1 позиция»."""
    tid = await db.add_delivery_type("Курьером", 6.0)
    await db.set_user_delivery_prefs(UID, tid, "ул. Пример, 5")
    _seed_model("t1", label="Кубик", price=10.0)
    await webapp._api_order(_FakeRequest({"initData": "x", "token": "t1"}))
    out = json.loads((await webapp._api_orders(_FakeGet(initData="x"))).body.decode())
    cards = out["orders"]
    assert len(cards) == 1
    card = cards[0]
    assert card.get("group") is not True and not card.get("group")   # НЕ карточка-группа
    assert card["delivery_method"] == "delivery"
    assert card["delivery_address"] == "ул. Пример, 5"
    assert card["delivery_type"] == "Курьером"
    assert "6" in card["delivery_cost"] and "16" in card["total_with_delivery"]


@pytest.mark.asyncio
async def test_cart_falls_back_to_default_type(tmp_db, cart_env):
    """Корзина без явного типа берёт умолчание клиента, а не оформляет доставку бесплатной."""
    tid = await db.add_delivery_type("Курьером", 6.0)
    await db.set_user_delivery_prefs(UID, tid, "ул. Пример, 5")
    _seed_model("t1", label="A", price=10.0)
    out = await _order_cart({"initData": "x", "tokens": ["t1"], "delivery_method": "delivery",
                             "delivery_address": "другой адрес"})
    g = await db.get_order_group(out["group"])
    assert g["delivery_cost"] == 6.0
    assert g["delivery_address"] == "другой адрес"     # адрес этого заказа важнее умолчания


@pytest.mark.asyncio
async def test_options_hide_delivery_when_no_types(tmp_db):
    """Ни одного типа — enabled=false: у клиента пропадает и выбор доставки, и адрес."""
    cfg = await webapp._delivery_options(UID)
    assert cfg["enabled"] is False and cfg["types"] == []
    tid = await db.add_delivery_type("Курьером", 6.0)
    cfg = await webapp._delivery_options(UID)
    assert cfg["enabled"] is True and len(cfg["types"]) == 1
    # выключили единственный тип → снова «только самовывоз»
    await db.update_delivery_type(tid, is_active=0)
    assert (await webapp._delivery_options(UID))["enabled"] is False


@pytest.mark.asyncio
async def test_options_drop_stale_default_type(tmp_db):
    """Тип выключили после того, как клиент его выбрал — умолчание не подсвечивается."""
    tid = await db.add_delivery_type("Курьером", 6.0)
    await db.set_user_delivery_prefs(UID, tid, "адрес")
    assert (await webapp._delivery_options(UID))["default_type_id"] == tid
    await db.update_delivery_type(tid, is_active=0)
    cfg = await webapp._delivery_options(UID)
    assert cfg["default_type_id"] == 0 and cfg["default_address"] == "адрес"


@pytest.mark.asyncio
async def test_default_delivery_survives_type_removal(tmp_db, cart_env):
    """Тип удалили — заказ оформляется без доставки, а не падает и не берёт мусорный тариф."""
    tid = await db.add_delivery_type("Курьером", 6.0)
    await db.set_user_delivery_prefs(UID, tid, "адрес")
    await db.delete_delivery_type(tid)
    _seed_model("t1", label="Кубик", price=10.0)
    resp = await webapp._api_order(_FakeRequest({"initData": "x", "token": "t1"}))
    assert json.loads(resp.body.decode())["ok"]
    rid = (await db.list_requests(10))[0].id
    assert await db.group_id_of_request(rid) == ""


@pytest.mark.asyncio
async def test_migration_renames_zones_to_types(tmp_db):
    """«Зоны» → «типы доставки»: миграция переносит ДАННЫЕ, а не только имена.

    Симулируем БД, заведённую до переименования (31.07): старая таблица delivery_zones со
    строкой и старые колонки delivery_zone_* в шапке заказа. Повторный init обязан перелить
    и то и другое, а сам остаться идемпотентным."""
    db_ = tmp_db
    conn = db_._conn()
    await conn.execute("DROP TABLE IF EXISTS delivery_zones")
    await conn.execute(
        "CREATE TABLE delivery_zones(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "price REAL NOT NULL DEFAULT 0, free_from REAL NOT NULL DEFAULT 0, eta TEXT NOT NULL "
        "DEFAULT '', comment TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1, "
        "sort INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
    await conn.execute("INSERT INTO delivery_zones(id, name, price, free_from, eta, created_at) "
                       "VALUES(41, 'Старая зона', 7.5, 100, '1 день', 1)")
    # старые колонки шапки + заказ, оформленный ещё «по зоне»
    for col, decl in (("delivery_zone_id", "INTEGER"), ("delivery_zone_name", "TEXT")):
        try:
            await conn.execute(f"ALTER TABLE order_groups ADD COLUMN {col} {decl}")
        except Exception:  # noqa: BLE001
            pass
    await db_.create_order_group("oldgroup0001", UID, delivery_method="delivery",
                                 delivery_address="адрес", delivery_cost=7.5)
    await conn.execute("UPDATE order_groups SET delivery_zone_id = 41, "
                       "delivery_zone_name = 'Старая зона', delivery_type_id = NULL, "
                       "delivery_type_name = NULL WHERE id = 'oldgroup0001'")
    await conn.commit()

    await db_.init()          # повторный init прогоняет миграции по открытому соединению

    types = await db_.list_delivery_types()
    assert [t["name"] for t in types] == ["Старая зона"]
    assert types[0]["id"] == 41 and types[0]["price"] == 7.5
    g = await db_.get_order_group("oldgroup0001")
    assert g["delivery_type_id"] == 41 and g["delivery_type_name"] == "Старая зона"
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_zones'")
    assert await cur.fetchone() is None, "старая таблица должна исчезнуть"

    await db_.init()          # идемпотентность: второй прогон ничего не ломает
    assert len(await db_.list_delivery_types()) == 1
