"""Тесты ценообразования — самая важная для бизнеса логика."""
from app import pricing, texts
from app.db import Plastic


def _plastic(price_per_kg=1000.0, density=1.24, **over):
    return Plastic(id=1, name="PLA", price_per_kg=price_per_kg, density=density, is_active=1, **over)


def _settings(**over):
    s = {
        "machine_hour_rate": 60.0,
        "failure_rate_pct": 10.0,
        "setup_fee": 50.0,
        "profit_margin_pct": 20.0,
        "min_price": 1.0,
        "multicolor_waste_pct": 5.0,
        "multicolor_time_pct": 10.0,
    }
    s.update(over)
    return s


def test_total_is_rounded_up_to_10_kopecks():
    bd = pricing.compute_price(10.0, 30.0, 3, _plastic(), _settings())
    cents = round(bd.total * 100)
    assert cents % 10 == 0, f"итог {bd.total} не кратен 10 копейкам"


def test_manual_matches_compute_price_when_no_multicolor():
    """Инвариант пересчёта заказа: цена по ГОТОВЫМ граммам (compute_price_manual — путь reprice/
    apply) совпадает с ценой из слайс-объёма (compute_price — путь recalc), если граммы = объём×
    плотность и одноцвет. На этом держится «админ видит цену пересчёта = цене при применении»."""
    settings = _settings()
    plastic = _plastic(price_per_kg=1000.0, density=1.24)
    volume_unit, minutes_unit, qty = 50.0, 200.0, 3
    grams_unit = volume_unit * plastic.density
    a = pricing.compute_price(volume_unit, minutes_unit, qty, plastic, settings,
                              discount_pct=20.0, extra_colors=0)
    b = pricing.compute_price_manual(grams_unit, minutes_unit, plastic.price_per_kg, settings,
                                     qty=qty, discount_pct=20.0,
                                     failure_rate_pct=plastic.failure_rate_pct)
    assert abs(a.total - b.total) < 1e-9
    assert abs(a.grams_total - b.grams_total) < 1e-9
    assert abs(a.machine_cost - b.machine_cost) < 1e-9


def test_breakdown_sums_to_total():
    bd = pricing.compute_price(12.3, 45.0, 4, _plastic(price_per_kg=1500), _settings(profit_margin_pct=25))
    parts = (bd.filament_cost + bd.machine_cost + bd.waste_cost + bd.setup_fee
             + bd.margin - bd.discount_amount)
    assert abs(parts - bd.total) < 0.01, f"разбивка {parts} != итог {bd.total}"


def test_discount_applies_to_everything():
    """Скидка применяется ко ВСЕЙ цене, включая филамент (OpenTodo 07451408 — новая политика)."""
    s = _settings()
    base = pricing.compute_price(20.0, 60.0, 5, _plastic(), s, discount_pct=0)
    disc = pricing.compute_price(20.0, 60.0, 5, _plastic(), s, discount_pct=30)
    # скидка = 30% от полного итога без скидки (филамент тоже дисконтируется)
    expected = base.total * 0.30
    assert abs(disc.discount_amount - expected) < 0.01
    assert disc.discount_amount > 0
    # итог со скидкой = итог без скидки минус скидка
    assert abs(disc.total - (base.total - disc.discount_amount)) < 0.01


def test_multicolor_adds_purge_and_time():
    s = _settings()
    mono = pricing.compute_price(10.0, 30.0, 2, _plastic(), s, extra_colors=0)
    multi = pricing.compute_price(10.0, 30.0, 2, _plastic(), s, extra_colors=2)
    assert mono.purge_grams_unit == 0.0
    assert multi.purge_grams_unit > 0.0
    assert multi.extra_minutes_unit > 0.0
    assert multi.grams_total > mono.grams_total
    assert multi.extra_colors == 2


def test_min_price_applied_for_tiny_model():
    bd = pricing.compute_price(0.01, 0.5, 1, _plastic(), _settings(min_price=100.0))
    assert bd.min_applied is True
    assert bd.total >= 100.0


def test_qty_scales_consumables():
    one = pricing.compute_price(10.0, 30.0, 1, _plastic(), _settings(setup_fee=0, min_price=0))
    ten = pricing.compute_price(10.0, 30.0, 10, _plastic(), _settings(setup_fee=0, min_price=0))
    # без фикс-подготовки и мин-цены филамент должен масштабироваться ~×10
    assert abs(ten.filament_cost - one.filament_cost * 10) < 0.05


def test_per_plastic_failure_rate_changes_price():
    """Индивидуальный % брака пластика важнее общего из настроек и даёт другую цену."""
    s = _settings(failure_rate_pct=8.0, min_price=0)
    base = pricing.compute_price(20.0, 60.0, 3, _plastic(), s)
    # тот же расчёт, но у пластика свой (больший) % брака
    p_hi = _plastic()
    p_hi.failure_rate_pct = 25.0
    hi = pricing.compute_price(20.0, 60.0, 3, p_hi, s)
    assert hi.failure_rate_pct == 25.0 and base.failure_rate_pct == 8.0
    assert hi.waste_cost > base.waste_cost
    assert hi.total > base.total
    # индивидуальный 0% брака — тоже валиден (не путать с «не задано»): буфер обнуляется
    p_zero = _plastic()
    p_zero.failure_rate_pct = 0.0
    zero = pricing.compute_price(20.0, 60.0, 3, p_zero, s)
    assert zero.waste_cost == 0.0 and zero.total < base.total


def test_failure_rate_none_uses_global():
    """failure_rate_pct=None у пластика → берётся общий % из настроек (как раньше)."""
    p = _plastic()
    assert p.failure_rate_pct is None
    a = pricing.compute_price(15.0, 40.0, 2, p, _settings(failure_rate_pct=10.0))
    b = pricing.compute_price(15.0, 40.0, 2, p, _settings(failure_rate_pct=20.0))
    assert a.failure_rate_pct == 10.0 and b.failure_rate_pct == 20.0
    assert b.waste_cost > a.waste_cost


def test_breakdown_dict_roundtrip():
    """Сериализация разбивки в dict (для хранения в заказе) и обратно сохраняет числа,
    а также снимок %-ставок для подписей строк."""
    s = _settings(failure_rate_pct=8, profit_margin_pct=40)
    bd = pricing.compute_price(12.0, 50.0, 3, _plastic(price_per_kg=1500), s, discount_pct=15)
    d = pricing.breakdown_to_dict(bd, s)
    assert d["failure_rate_pct"] == 8 and d["profit_margin_pct"] == 40
    back = pricing.breakdown_from_dict(d)
    assert abs(back.total - bd.total) < 1e-9
    assert abs(back.filament_cost - bd.filament_cost) < 1e-9
    assert back.qty == bd.qty and back.extra_colors == bd.extra_colors
    # снимок %-ставок восстанавливается для build_quote (PDF-смета к заказу из requests.breakdown)
    sett = pricing.settings_from_breakdown(d)
    assert sett["failure_rate_pct"] == 8 and sett["profit_margin_pct"] == 40
    assert pricing.settings_from_breakdown(None) == {"failure_rate_pct": 0.0, "profit_margin_pct": 0.0}
    # пустой/битый вход → None (старые заказы без разбивки)
    assert pricing.breakdown_from_dict(None) is None
    assert pricing.breakdown_from_dict({}) is None


def test_breakdown_rows_match_total():
    """Строки расчётника покрывают все слагаемые: материал + работа + буфер + подготовка +
    наценка − скидка должны сходиться к итогу (как и сама PriceBreakdown)."""
    s = _settings()
    bd = pricing.compute_price(15.0, 40.0, 2, _plastic(), s, discount_pct=20)
    rows = texts.breakdown_rows(bd, pricing.breakdown_to_dict(bd, s), "BYN")
    labels = [r["label"] for r in rows]
    assert labels[:5] == ["Филамент", "Машинное время", "Буфер на брак", "Подготовка", "Наценка"]
    assert rows[-1]["label"] == "Скидка" and rows[-1].get("discount") is True
    # сумма строк-слагаемых (со знаком скидки) ≈ итог
    raw = (bd.filament_cost + bd.machine_cost + bd.waste_cost + bd.setup_fee
           + bd.margin - bd.discount_amount)
    assert abs(raw - bd.total) < 0.01


def test_compute_price_manual_matches_slicing():
    """Калькулятор админки (ручной ввод грамм/времени/цены) даёт ТОТ ЖЕ итог, что и обычный
    расчёт по слайсингу при эквивалентных входах: граммы = объём×плотность, та же цена за кг."""
    s = _settings()
    pl = _plastic(price_per_kg=1500.0, density=1.24)
    sliced = pricing.compute_price(12.0, 45.0, 3, pl, s, discount_pct=15)
    grams = 12.0 * 1.24
    manual = pricing.compute_price_manual(grams, 45.0, 1500.0, s, qty=3, discount_pct=15)
    assert abs(manual.total - sliced.total) < 1e-6
    assert abs(manual.filament_cost - sliced.filament_cost) < 1e-6
    assert abs(manual.grams_total - sliced.grams_total) < 1e-6
    assert manual.qty == 3 and manual.extra_colors == 0


def test_compute_price_manual_failure_rate_from_settings():
    """У ручного расчёта нет «пластика», поэтому буфер на брак берётся из общих настроек."""
    a = pricing.compute_price_manual(20.0, 60.0, 1000.0, _settings(failure_rate_pct=10.0))
    b = pricing.compute_price_manual(20.0, 60.0, 1000.0, _settings(failure_rate_pct=20.0))
    assert b.waste_cost > a.waste_cost
    assert a.qty == 1                                   # по умолчанию одна штука
    rows = texts.breakdown_rows(a, _settings(), "BYN")  # совместимо с расчётником UI
    assert rows[0]["label"] == "Филамент"


def test_plastic_price_factor_scales_filament():
    """Коэффициент сложности пластика: филамент дорожает ×коэф, клиент видит только цену."""
    s = _settings(min_price=0)
    base = pricing.compute_price(20.0, 60.0, 2, _plastic(), s)
    fac = pricing.compute_price(20.0, 60.0, 2, _plastic(price_factor=1.5), s)
    assert abs(fac.filament_cost - base.filament_cost * 1.5) < 1e-9
    assert fac.total > base.total
    # граммы НЕ меняются — дорожает только цена материала
    assert abs(fac.grams_total - base.grams_total) < 1e-9
    # коэф = 1 (по умолчанию) — цена прежняя (обратная совместимость со старыми пластиками)
    same = pricing.compute_price(20.0, 60.0, 2, _plastic(price_factor=1.0), s)
    assert abs(same.total - base.total) < 1e-9


def test_plastic_price_factor_hidden_in_catalog_card():
    """Карточка каталога показывает цену уже ×коэф — сам коэффициент клиенту не светим."""
    card = texts.catalog_card(_plastic(price_per_kg=1000.0, price_factor=1.2), "BYN")
    assert "1200" in card and "1000" not in card
    assert "коэф" not in card.lower()
    # eff_price_per_kg не тащит хвост двоичной дроби — максимум копейки (2 знака)
    eff = _plastic(price_per_kg=25.9, price_factor=1.15).eff_price_per_kg
    assert abs(eff * 100 - round(eff * 100)) < 1e-6


def test_plate_fee_charged_per_plate_and_hidden_in_machine_cost():
    """Доплата за столы (OpenTodo 8c89a701): фикс. ставка × число столов, за КАЖДЫЙ стол.
    Прячется в строке машинного времени — филамент честный, а разбивка по-прежнему сходится."""
    s = _settings(plate_fee=0.5, min_price=0, setup_fee=0)
    one = pricing.compute_price(20.0, 60.0, 1, _plastic(), s, plates=1)
    three = pricing.compute_price(20.0, 60.0, 1, _plastic(), s, plates=3)
    # доплата сохранена в разбивке: сырое значение ставка×столы
    assert abs(one.plate_fee - 0.5) < 1e-9
    assert abs(three.plate_fee - 1.5) < 1e-9
    # больше столов — дороже; надбавка спрятана в машинном времени (филамент не меняется)
    assert three.total > one.total
    assert abs(three.filament_cost - one.filament_cost) < 1e-9
    assert three.machine_cost > one.machine_cost
    # разбивка сходится к итогу и с доплатой
    parts = (three.filament_cost + three.machine_cost + three.waste_cost
             + three.setup_fee + three.margin - three.discount_amount)
    assert abs(parts - three.total) < 0.01
    # ставка 0 → доплаты нет (обратная совместимость со старыми установками без ключа)
    off = pricing.compute_price(20.0, 60.0, 1, _plastic(), _settings(min_price=0), plates=5)
    assert off.plate_fee == 0.0


def test_plate_fee_manual_matches_slicing():
    """Пересчёт по готовым граммам (reprice) с тем же числом столов даёт ту же цену, что и слайс —
    инвариант «цена пересчёта = цене применения» сохраняется и с доплатой за столы."""
    s = _settings(plate_fee=0.5, min_price=0)
    pl = _plastic(price_per_kg=1500.0, density=1.24)
    vol, minutes, qty, plates = 40.0, 90.0, 2, 3
    sliced = pricing.compute_price(vol, minutes, qty, pl, s, discount_pct=10, plates=plates)
    manual = pricing.compute_price_manual(vol * 1.24, minutes, 1500.0, s, qty=qty,
                                          discount_pct=10, failure_rate_pct=pl.failure_rate_pct,
                                          volume_cm3_unit=vol, plates=plates)
    assert abs(manual.total - sliced.total) < 1e-6
    assert abs(manual.plate_fee - sliced.plate_fee) < 1e-9


def test_volume_surcharge_off_by_default_and_below_threshold():
    """Настроек нет / порог 0 / объём ниже порога → коэффициент «большой модели» не применяется."""
    s = _settings()   # ключей volume_* нет — как у старых установок
    assert pricing.compute_price(500.0, 60.0, 1, _plastic(), s).volume_factor == 1.0
    s2 = _settings(volume_threshold_cm3=200.0, volume_surcharge_pct=15.0, min_price=0)
    bd = pricing.compute_price(150.0, 60.0, 1, _plastic(), s2)
    assert bd.volume_factor == 1.0
    # ровно на пороге — тоже без надбавки (непрерывность: скачка цены на границе нет)
    assert pricing.compute_price(200.0, 60.0, 1, _plastic(), s2).volume_factor == 1.0


def test_volume_surcharge_grows_linearly_and_hides_in_machine_cost():
    """Выше порога расходники дорожают линейно (+pct за каждый порог сверх), надбавка прячется
    в строке машинного времени: строка филамента остаётся «граммы × цена каталога»."""
    s = _settings(volume_threshold_cm3=200.0, volume_surcharge_pct=15.0,
                  min_price=0, setup_fee=0)
    base = pricing.compute_price(200.0, 60.0, 1, _plastic(), s)
    big = pricing.compute_price(400.0, 60.0, 1, _plastic(), s)      # ×2 порога → ×1.15
    assert abs(big.volume_factor - 1.15) < 1e-9
    huge = pricing.compute_price(600.0, 60.0, 1, _plastic(), s)     # ×3 порога → ×1.30
    assert abs(huge.volume_factor - 1.30) < 1e-9
    # филамент честный: ровно вдвое больше, чем у модели вдвое меньшего объёма (без ×1.15)
    assert abs(big.filament_cost - base.filament_cost * 2) < 1e-9
    # весь консюмабль подорожал ×1.15 (без setup_fee всё пропорционально, кроме округления итога)
    plain = base.filament_cost * 2 + base.machine_cost   # те же граммы×2 и минуты без надбавки
    got = big.filament_cost + big.machine_cost
    assert abs(got - plain * 1.15) < 0.11                # 0.1 BYN — скрытое округление в машинке
    assert big.total > base.total


def test_volume_surcharge_manual_path_matches_slicing():
    """Пересчёт по готовым граммам (reprice) с переданным объёмом даёт ту же цену, что и слайс —
    инвариант «цена пересчёта = цене применения» сохраняется и при включённой надбавке."""
    s = _settings(volume_threshold_cm3=100.0, volume_surcharge_pct=20.0, min_price=0)
    pl = _plastic(price_per_kg=1500.0, density=1.24)
    vol, minutes, qty = 350.0, 90.0, 2
    sliced = pricing.compute_price(vol, minutes, qty, pl, s, discount_pct=10)
    manual = pricing.compute_price_manual(vol * 1.24, minutes, 1500.0, s, qty=qty,
                                          discount_pct=10, volume_cm3_unit=vol)
    assert abs(manual.total - sliced.total) < 1e-6
    assert abs(manual.machine_cost - sliced.machine_cost) < 1e-6
    assert manual.volume_factor == sliced.volume_factor > 1.0
    # объём не передан (чистый ручной калькулятор) → надбавка не применяется
    no_vol = pricing.compute_price_manual(vol * 1.24, minutes, 1500.0, s, qty=qty, discount_pct=10)
    assert no_vol.volume_factor == 1.0 and no_vol.total < manual.total
