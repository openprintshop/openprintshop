"""Тесты оценок: дата готовности (#515) и предупреждения печатопригодности (#9724)."""
import datetime
from types import SimpleNamespace

from app import db, pricing, texts


def test_eta_date_min_one_day():
    today = datetime.date.today()
    assert db.eta_date(0) == (today + datetime.timedelta(days=1)).strftime("%d.%m")
    assert len(db.eta_date(0)) == 5            # формат DD.MM


def test_eta_date_scales_with_work():
    big = 60 * db.config.PRINT_HOURS_PER_DAY * 4   # ~4 дня печати
    today = datetime.date.today()
    assert db.eta_date(big) == (today + datetime.timedelta(days=4)).strftime("%d.%m")


def test_printability_bed_warning():
    assert texts.printability_warnings("300×40×40 мм", 256.0)        # большая сторона > стола
    assert not texts.printability_warnings("60×40×40 мм", 256.0)     # влезает
    assert not texts.printability_warnings("", 256.0)                # нет габаритов — нет паники


_URG_SETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.0, "failure_rate_pct": 8.0,
    "profit_margin_pct": 50.0, "min_price": 5.0, "multicolor_waste_pct": 12.0,
    "multicolor_time_pct": 15.0, "volume_threshold_cm3": 0.0, "volume_surcharge_pct": 0.0,
}


def _urg_plastic():
    return SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None)


def test_breakdown_rows_includes_urgency_only_when_rush():
    """Строка «Срочность» появляется в разбивке только для срочного заказа (B3); обычный — без неё."""
    normal = pricing.compute_price(30.0, 180.0, 2, _urg_plastic(), _URG_SETTINGS)
    rush = pricing.compute_price(30.0, 180.0, 2, _urg_plastic(), _URG_SETTINGS, urgency_factor=1.5)
    normal_rows = texts.breakdown_rows(normal, _URG_SETTINGS, "руб.")
    rush_rows = texts.breakdown_rows(rush, _URG_SETTINGS, "руб.")
    assert not any(r["label"] == "Срочность" for r in normal_rows)
    urg = [r for r in rush_rows if r["label"] == "Срочность"]
    assert len(urg) == 1
    assert urg[0]["value"].startswith("+")   # наценка, не скидка
