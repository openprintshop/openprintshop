"""Срочность в расчёте цены (B3): множитель наценки в pricing.compute_price/compute_price_manual.

Проверяем: срочный итог = обычный ×K (по модулю скрытого округления вверх до 10 копеек);
наценка считается ПОСЛЕ скидки и ДО минимальной цены (премия за срочность сама не скидируется);
поля urgency_factor/urgency_surcharge переживают снимок разбивки заказа (round-trip), а старая
запись без этих полей грузится с дефолтом 1.0 (обратная совместимость с историей заказов).
"""
from types import SimpleNamespace

from app import config, pricing, quote, texts

# Минимальный набор ставок, достаточный для compute_price (без «большой модели»/многоцвета).
SETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.0,
    "failure_rate_pct": 8.0, "profit_margin_pct": 50.0, "min_price": 5.0,
    "multicolor_waste_pct": 12.0, "multicolor_time_pct": 15.0,
    "volume_threshold_cm3": 0.0, "volume_surcharge_pct": 0.0,
    "rush_surcharge_pct": 50.0, "term_split_hours": 12,
}


def _plastic():
    return SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None)


def test_urgency_factor_from_settings():
    assert pricing.urgency_factor_from("rush", SETTINGS) == 1.5
    assert pricing.urgency_factor_from("normal", SETTINGS) == 1.0
    assert pricing.urgency_factor_from("", SETTINGS) == 1.0
    assert pricing.urgency_factor_from(None, SETTINGS) == 1.0
    # неизвестное значение = обычная печать (старые кэшированные клиенты без поля)
    assert pricing.urgency_factor_from("express", SETTINGS) == 1.0


def test_rush_multiplies_total():
    normal = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    rush = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    assert normal.urgency_factor == 1.0
    assert normal.urgency_surcharge == 0.0
    assert rush.urgency_factor == 1.5
    assert rush.urgency_surcharge > 0.0
    # срочный итог ≈ обычный ×1.5, с допуском на скрытое округление вверх до 10 копеек на каждой стороне
    assert abs(rush.total - normal.total * 1.5) <= 0.2
    assert rush.total > normal.total


def test_urgency_after_discount():
    """Наценка за срочность считается на ПОСТ-скидочной сумме; сама скидка не растёт от срочности
    (премия за срочность не скидируется), а минимальная цена остаётся полом."""
    normal = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, discount_pct=20.0)
    rush = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, discount_pct=20.0,
                                 urgency_factor=1.5)
    assert normal.discount_amount > 0
    # скидка одинакова у обычного и срочного: она считается ДО срочности, премия ею не режется
    assert abs(rush.discount_amount - normal.discount_amount) < 1e-9
    # срочный итог всё равно ≈ ×1.5 к обычному (по модулю округления)
    assert abs(rush.total - normal.total * 1.5) <= 0.2


def test_min_price_still_floors_after_urgency():
    """Даже срочный расчёт не опускается ниже минимальной цены (порядок: скидка → срочность → min)."""
    tiny = dict(SETTINGS, min_price=50.0)
    rush = pricing.compute_price(1.0, 5.0, 1, _plastic(), tiny, urgency_factor=1.5)
    assert rush.min_applied is True
    assert abs(rush.total - 50.0) < 1e-6


def test_manual_urgency():
    normal = pricing.compute_price_manual(40.0, 180.0, 1000.0, SETTINGS, qty=1)
    rush = pricing.compute_price_manual(40.0, 180.0, 1000.0, SETTINGS, qty=1, urgency_factor=1.5)
    assert normal.urgency_factor == 1.0
    assert rush.urgency_factor == 1.5
    assert rush.urgency_surcharge > 0.0
    assert rush.total > normal.total
    # тот же инвариант, что у compute_price: срочный итог ≈ обычный ×1.5 (по модулю округления)
    assert abs(rush.total - normal.total * 1.5) <= 0.2


def test_manual_urgency_after_discount_and_floor():
    """compute_price_manual: наценка на ПОСТ-скидочной сумме (скидка ею не растёт), а минимальная
    цена остаётся полом и в срочном ручном расчёте (тот же порядок скидка → срочность → min)."""
    normal = pricing.compute_price_manual(40.0, 180.0, 1000.0, SETTINGS, qty=1, discount_pct=20.0)
    rush = pricing.compute_price_manual(40.0, 180.0, 1000.0, SETTINGS, qty=1, discount_pct=20.0,
                                        urgency_factor=1.5)
    assert normal.discount_amount > 0
    assert abs(rush.discount_amount - normal.discount_amount) < 1e-9   # скидка до срочности
    assert abs(rush.total - normal.total * 1.5) <= 0.2
    # минимум-пол переживает срочность и в ручном пути
    tiny = dict(SETTINGS, min_price=500.0)
    floored = pricing.compute_price_manual(40.0, 180.0, 1000.0, tiny, qty=1, urgency_factor=1.5)
    assert floored.min_applied is True
    assert abs(floored.total - 500.0) < 1e-6


def test_breakdown_snapshot_roundtrip():
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    d = pricing.breakdown_to_dict(bd, SETTINGS)
    assert d["urgency_factor"] == 1.5
    assert d["urgency_surcharge"] > 0.0
    bd2 = pricing.breakdown_from_dict(d)
    assert bd2 is not None
    assert bd2.urgency_factor == 1.5
    assert abs(bd2.urgency_surcharge - bd.urgency_surcharge) < 1e-9
    assert abs(bd2.total - bd.total) < 1e-9


def test_legacy_breakdown_defaults_to_normal():
    """Историческая разбивка БЕЗ полей срочности грузится с urgency_factor=1.0 — старые сметы
    не падают и считаются обычными."""
    legacy = {"qty": 1, "grams_unit": 20.0, "minutes_unit": 100.0, "grams_total": 20.0,
              "minutes_total": 100.0, "filament_cost": 1.0, "machine_cost": 3.0, "waste_cost": 0.3,
              "setup_fee": 5.0, "margin": 4.0, "discount_pct": 0.0, "discount_amount": 0.0,
              "subtotal": 9.0, "total": 13.0, "min_applied": False}
    bd = pricing.breakdown_from_dict(legacy)
    assert bd is not None
    assert bd.urgency_factor == 1.0
    assert bd.urgency_surcharge == 0.0


def _legacy_bd():
    """Объект разбивки СТАРОГО заказа — без атрибутов urgency_factor/urgency_surcharge вовсе
    (не через дефолт dataclass, а физически отсутствуют). Проверяем защиту getattr в рендерах."""
    return SimpleNamespace(
        failure_rate_pct=8.0, grams_total=20.0, grams_unit=20.0, purge_grams_unit=0.0,
        extra_colors=0, minutes_total=100.0, minutes_unit=100.0, filament_cost=1.0,
        machine_cost=3.0, waste_cost=0.3, setup_fee=5.0, margin=4.0,
        discount_pct=0.0, discount_amount=0.0, subtotal=9.0, total=13.0)


def test_breakdown_rows_legacy_object_no_crash():
    """texts.breakdown_rows на объекте БЕЗ полей срочности (старый заказ) не падает и не рисует
    строку «Срочность» — getattr(..., 1.0) отрабатывает как обратная совместимость."""
    assert not hasattr(_legacy_bd(), "urgency_factor")
    rows = texts.breakdown_rows(_legacy_bd(), SETTINGS, "руб.")
    assert not any(r["label"] == "Срочность" for r in rows)


def test_quote_renders_rush_and_legacy(tmp_path, monkeypatch):
    """quote.build_quote рендерит и срочную смету (со строкой наценки), и старую разбивку без
    полей срочности — без падения. QUOTE_DIR подменяем на tmp, чтобы не писать в data/."""
    monkeypatch.setattr(config, "QUOTE_DIR", tmp_path)

    def _build(bd):
        return quote.build_quote(
            business_name="Тест", business_contact="", model_label="Кубик",
            printer_name="ECC2", plastic_name="Test PLA", qty=1, quality_label="0.2",
            strength_label="норм", bd=bd, cur="руб.", settings=SETTINGS, preview_path=None)

    rush = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    out_rush = _build(rush)
    assert out_rush.exists() and out_rush.stat().st_size > 0

    # старый заказ: объект без атрибутов срочности вовсе — getattr в quote.py не должен уронить PDF
    legacy = _legacy_bd()
    assert not hasattr(legacy, "urgency_factor")
    out_legacy = _build(legacy)
    assert out_legacy.exists() and out_legacy.stat().st_size > 0
