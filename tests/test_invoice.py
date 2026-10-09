"""Счёт-расчёт по индивидуальному заказу (OpenTodo 6aaa6a19): хранение, разбивка, плата за разработку."""
from app import calcservice, pricing, texts


async def _custom_order(db):
    return await db.add_request(
        user_id=1, user_name="U", model_label="Индивидуальная штука", printer="",
        plastic_name="", quantity=1, quality="", strength="", grams_total=0,
        minutes_total=0, price=0, preview_path=None, is_custom=1, description="хочу вот это")


async def _issue_invoice(db, rid, dev_fee):
    settings = await db.get_settings()
    bd = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=1)
    bd_dict = pricing.breakdown_to_dict(bd, settings)
    total = round(bd.total + dev_fee, 2)
    await db.update_request(rid, status="invoiced", pending_price=total, plastic_name="Test PLA",
                            quantity=1, grams_total=bd.grams_total, minutes_total=bd.minutes_total,
                            breakdown=bd_dict, dev_fee=dev_fee, slicer_shot="/tmp/shot.jpg")
    return bd, total


async def test_custom_invoice_roundtrip(tmp_db):
    db = tmp_db
    rid = await _custom_order(db)
    bd, total = await _issue_invoice(db, rid, 5.0)
    r = await db.get_request(rid)
    assert r.status == "invoiced"
    assert r.dev_fee == 5.0
    assert r.slicer_shot == "/tmp/shot.jpg"
    assert abs(r.pending_price - total) < 1e-6
    # счёт ждёт подтверждения клиента — попадает в счётчик «нужно подтвердить»
    assert await db.pending_price_count(1) == 1


async def test_invoice_breakdown_adds_dev_fee_row(tmp_db):
    db = tmp_db
    rid = await _custom_order(db)
    bd, _ = await _issue_invoice(db, rid, 7.5)
    r = await db.get_request(rid)
    ob = texts.order_breakdown(r, "руб.")
    assert ob is not None
    assert "Разработка модели" in [row["label"] for row in ob["rows"]]
    assert ob["total"] == texts.fmt_money(bd.total + 7.5, "руб.")   # печать + разработка


async def test_invoice_zero_dev_fee_no_row(tmp_db):
    db = tmp_db
    rid = await _custom_order(db)
    bd, _ = await _issue_invoice(db, rid, 0.0)
    r = await db.get_request(rid)
    ob = texts.order_breakdown(r, "руб.")
    assert "Разработка модели" not in [row["label"] for row in ob["rows"]]
    assert ob["total"] == texts.fmt_money(bd.total, "руб.")


async def test_invoice_accept_sets_price(tmp_db):
    """Принятие счёта клиентом: pending_price → price, статус → in_progress (как смена цены)."""
    db = tmp_db
    rid = await _custom_order(db)
    _, total = await _issue_invoice(db, rid, 3.0)
    r = await db.get_request(rid)
    await db.update_request(rid, price=r.pending_price, pending_price=None, status="in_progress")
    r2 = await db.get_request(rid)
    assert r2.status == "in_progress"
    assert abs(r2.price - total) < 1e-6
    assert r2.pending_price is None
    assert await db.pending_price_count(1) == 0


# --- скидки при выставлении счёта по индивидуальному заказу -----------------------------------
# Баг: первичный счёт (заказ без снимка разбивки) игнорировал скидки клиента/тиража.
# reprice_request должен считать скидку как обычный заказ, когда разбивки ещё нет.

async def _reprice(db, rid, *, qty=1, plastic="Test PLA", grams=40.0, minutes=180.0):
    """Имитация вызова счёта: подставляем выбранный пластик/тираж и репрайсим (как в webapp)."""
    r = await db.get_request(rid)
    r.plastic_name = plastic
    r.quantity = qty
    r.breakdown = r.breakdown or {}
    return await calcservice.reprice_request(r, grams, minutes, user_id=r.user_id)


async def test_invoice_applies_user_discount(tmp_db):
    """Счёт по индивидуальному заказу применяет персональную скидку клиента (а не игнорирует)."""
    db = tmp_db
    await db.add_plastic("Test PLA", 1000.0, 1.0)
    await db.set_user_discount(1, 15.0, "loyal customer")
    rid = await _custom_order(db)            # user_id=1, без разбивки
    res = await _reprice(db, rid)
    bd = res["bd"]
    assert bd.discount_pct == 15.0
    assert bd.discount_amount > 0
    # совпадает с прямым расчётом той же скидки
    ref = pricing.compute_price_manual(40.0, 180.0, 1000.0, res["settings"], qty=1, discount_pct=15.0)
    assert abs(bd.total - ref.total) < 0.01


async def test_invoice_applies_qty_discount(tmp_db):
    """Счёт применяет скидку за тираж, когда персональной нет."""
    db = tmp_db
    await db.add_plastic("Test PLA", 1000.0, 1.0)
    await db.add_discount(5, 10.0)          # 5+ шт → 10%
    rid = await _custom_order(db)
    res = await _reprice(db, rid, qty=5)
    bd = res["bd"]
    assert bd.discount_pct == 10.0
    assert bd.discount_amount > 0


async def test_invoice_prefers_higher_discount(tmp_db):
    """При наличии и персональной, и тиражной скидки берём бОльшую (как в обычном расчёте)."""
    db = tmp_db
    await db.add_plastic("Test PLA", 1000.0, 1.0)
    await db.set_user_discount(1, 20.0, "vip")
    await db.add_discount(5, 10.0)
    rid = await _custom_order(db)
    res = await _reprice(db, rid, qty=5)
    assert res["bd"].discount_pct == 20.0   # max(20, 10)


async def test_invoice_preserves_breakdown_discount_on_recalc(tmp_db):
    """Совместимость: если у заказа уже есть снимок разбивки, скидку берём из него (не «плывёт»)."""
    db = tmp_db
    await db.add_plastic("Test PLA", 1000.0, 1.0)
    await db.set_user_discount(1, 50.0, "would-change-result")
    rid = await _custom_order(db)
    settings = await db.get_settings()
    bd_orig = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=1, discount_pct=12.0)
    await db.update_request(rid, breakdown=pricing.breakdown_to_dict(bd_orig, settings))
    res = await _reprice(db, rid)
    assert res["bd"].discount_pct == 12.0   # из снимка, а не персональные 50%


async def test_invoice_no_discount_when_none_configured(tmp_db):
    """Без скидок счёт считается без скидки (регресс-якорь поведения по умолчанию)."""
    db = tmp_db
    await db.add_plastic("Test PLA", 1000.0, 1.0)
    rid = await _custom_order(db)
    res = await _reprice(db, rid)
    assert res["bd"].discount_pct == 0.0
    assert res["bd"].discount_amount == 0.0


# --- многоцветная печать в счёте (OpenTodo efca76c3) -------------------------------------------
# Граммы по каждому цвету своим пластиком; материал = Σ грамм×цена (через средневзвешенную цену),
# надбавки multicolor_waste/time НЕ добавляются (числа из реального многоцветного слайса).

async def test_invoice_colors_weighted_material_price(tmp_db):
    db = tmp_db
    pa = await db.add_plastic("Цвет A", 1000.0, 1.0)   # 1 руб/г
    pb = await db.add_plastic("Цвет B", 3000.0, 1.0)   # 3 руб/г
    rid = await _custom_order(db)
    r = await db.get_request(rid)
    r.quantity = 1
    r.breakdown = r.breakdown or {}
    colors = [(await db.get_plastic(pa), 30.0), (await db.get_plastic(pb), 10.0)]
    res = await calcservice.reprice_request_colors(r, colors, 120.0, user_id=r.user_id)
    bd = res["bd"]
    # материал: 30г×1 + 10г×3 = 60; в одноцвете по цене A было бы 40
    assert abs(bd.filament_cost - 60.0) < 1e-6
    assert bd.grams_total == 40.0
    assert bd.extra_colors == 1                      # подпись «многоцветная (~2 цв.)»
    assert bd.purge_grams_unit == 0                  # продувку НЕ доначисляем
    assert bd.extra_minutes_unit == 0


async def test_invoice_colors_zero_grams_rejected(tmp_db):
    db = tmp_db
    pa = await db.add_plastic("Цвет A", 1000.0, 1.0)
    rid = await _custom_order(db)
    r = await db.get_request(rid)
    r.breakdown = {}
    try:
        await calcservice.reprice_request_colors(r, [(await db.get_plastic(pa), 0.0)], 120.0)
        raise AssertionError("ожидали CalcError")
    except calcservice.CalcError:
        pass


async def test_invoice_colors_failure_rate_mixed_uses_general(tmp_db):
    """Инд. % брака берём только когда он у всех цветов одинаковый; разные → общий из настроек."""
    db = tmp_db
    pa = await db.add_plastic("Цвет A", 1000.0, 1.0)
    pb = await db.add_plastic("Цвет B", 1000.0, 1.0)
    await db.update_plastic(pa, failure_rate_pct=20.0)
    rid = await _custom_order(db)
    r = await db.get_request(rid)
    r.quantity = 1
    r.breakdown = {}
    settings = await db.get_settings()
    colors = [(await db.get_plastic(pa), 10.0), (await db.get_plastic(pb), 10.0)]
    res = await calcservice.reprice_request_colors(r, colors, 60.0)
    assert res["bd"].failure_rate_pct == settings["failure_rate_pct"]   # общий, не 20
    same = [(await db.get_plastic(pa), 10.0), (await db.get_plastic(pa), 5.0)]
    res2 = await calcservice.reprice_request_colors(r, same, 60.0)
    assert res2["bd"].failure_rate_pct == 20.0                          # у всех один пластик
