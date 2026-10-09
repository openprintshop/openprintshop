"""FINAL-GATE intersection sweep (Opus reviewer): verify the six features that share the pricing/
breakdown path (B3 urgency + B4 postproc options + B2 DFM) coexist without one clobbering another.

Focus: the *combined* money math. Each feature was reviewed alone; here we apply urgency AND
options to one breakdown and assert the visible rows still sum to the rounded total — the invariant
that both compute_price (urgency) and apply_options (options) claim to preserve independently.
"""
import re
from types import SimpleNamespace

from app import pricing, texts

SETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.0,
    "failure_rate_pct": 8.0, "profit_margin_pct": 50.0, "min_price": 5.0,
    "multicolor_waste_pct": 12.0, "multicolor_time_pct": 15.0,
    "volume_threshold_cm3": 0.0, "volume_surcharge_pct": 0.0,
    "rush_surcharge_pct": 50.0, "term_split_hours": 12,
    "dfm_min_wall_mm": 0.8,
}


def _plastic():
    return SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None)


def _visible_rows_sum(bd, cur="р."):
    """Sum the signed money in every breakdown row that texts.breakdown_rows emits, mirroring how
    the mini-app renders the receipt. Returns rubles."""
    total = 0.0
    for row in texts.breakdown_rows(bd, SETTINGS, cur):
        raw = row["value"]
        sign = -1.0 if ("−" in raw or raw.strip().startswith("-")) else 1.0
        m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", raw)
        assert m, f"no number in row value {raw!r}"
        num = float(m.group(1).replace(" ", "").replace(",", "."))
        total += sign * num
    return total


def test_urgency_and_options_stack_on_one_breakdown():
    """B3 then B4 on the same bd: total must equal base + urgency_surcharge + options_cost,
    modulo the <=10-kopeck ceil that apply_options re-hides into machine_cost."""
    base = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    before_options = bd.total
    opts = [{"id": 1, "name": "Шлифовка", "price": 5.0},
            {"id": 2, "name": "Покраска", "price": 12.0}]
    pricing.apply_options(bd, opts)
    assert bd.urgency_factor == 1.5
    assert bd.urgency_surcharge > 0
    assert bd.options_cost == 17.0
    assert bd.options == opts
    # total grew by exactly the options cost (plus <=0.10 rounding)
    assert 0 <= bd.total - (before_options + 17.0) < 0.10
    # urgency still ~1.5x the base print price
    assert abs((before_options) - base.total * 1.5) <= 0.2


def test_visible_receipt_rows_sum_to_total_with_urgency_and_options():
    """The load-bearing intersection invariant: with BOTH urgency and options present, the rows
    the client sees in the receipt must add up to bd.total. If B4's apply_options failed to
    re-hide rounding after B3's urgency line, this sum would drift."""
    bd = pricing.compute_price(42.0, 240.0, 3, _plastic(), SETTINGS, discount_pct=15.0,
                               urgency_factor=1.5)
    pricing.apply_options(bd, [{"id": 1, "name": "Латунные вставки", "price": 8.0},
                               {"id": 2, "name": "Бесплатная опция", "price": 0.0}])
    # zero-priced option must NOT appear as a row but must be retained in bd.options snapshot
    labels = [r["label"] for r in texts.breakdown_rows(bd, SETTINGS, "р.")]
    assert "Срочность" in labels
    assert "Латунные вставки" in labels
    assert "Бесплатная опция" not in labels
    assert len(bd.options) == 2  # snapshot keeps the free one for the master's tag
    got = _visible_rows_sum(bd)
    assert abs(got - bd.total) < 0.02, f"receipt rows {got} != total {bd.total}"


def test_options_free_only_does_not_move_total():
    """A catalog of only free options must leave total untouched (options_cost=0 short-circuit)."""
    bd = pricing.compute_price(30.0, 180.0, 1, _plastic(), SETTINGS)
    t0 = bd.total
    pricing.apply_options(bd, [{"id": 9, "name": "Консультация", "price": 0.0}])
    assert bd.options_cost == 0.0
    assert bd.total == t0
    assert bd.options  # still snapshotted


def test_dfm_verdict_gate_single_vs_set():
    """B2 gate-of-one-part: a thin proxy triggers 'risky' only for a single part; a multi-part set
    with the same thin proxy stays 'ok' (the fix that stopped false alarms on valid part sets)."""
    v_single, dfm_single = texts.printability_verdict(
        "10×10×0.5", bed_mm=256.0, n_parts=1, min_extent_mm=0.5, wall_proxy_mm=0.5, min_wall_mm=0.8)
    assert v_single == "risky"
    assert any(d["key"] == "wall" for d in dfm_single)
    v_set, dfm_set = texts.printability_verdict(
        "10×10×0.5", bed_mm=256.0, n_parts=5, min_extent_mm=0.5, wall_proxy_mm=0.5, min_wall_mm=0.8)
    assert v_set == "ok"
    assert not any(d["key"] == "wall" for d in dfm_set)


def test_dfm_never_blocks_wont_fit_is_max_severity():
    """wont_fit is the worst verdict B2 emits — there is no 'blocked' state (feature never blocks)."""
    v, dfm = texts.printability_verdict(
        "300×10×10", bed_mm=256.0, n_parts=1, max_part_mm=300.0, min_wall_mm=0.8)
    assert v == "wont_fit"
    assert dfm and dfm[0]["status"] == "bad"
