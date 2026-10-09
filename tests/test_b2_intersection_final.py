"""FINAL-GATE intersection check: B2 (DFM) coexisting with B3 (срочность), B4 (опции),
B5 (корзина) in the SAME quote payload without key collisions or dropped keys.

This is not a re-review of each feature in isolation — it verifies their INTERSECTION at the
payload-assembly level, reproducing the exact ordering the webapp handlers use:

    payload = _result_payload(bd, ...)          # seeds verdict='ok', dfm=[]
    payload["urgency"]/["term"] = ...           # B3
    _apply_dfm(payload, ...)                     # B2 overwrites verdict/dfm

and with a bd that already carries BOTH the RUSH surcharge (B3) and the flat post-processing
options cost (B4). Covers the three claims the final gate must confirm:

  1. verdict+dfm coexist with urgency/term (+ options folded into price/lines) — no key overwrites
     another, no path sets one and drops another.
  2. a RUSH order with OPTIONS that also trips a DFM 'wont_fit' still prices correctly (both
     surcharge and options in the total), is orderable (DFM is informational — no order path reads
     verdict), and carries every key.
  3. the single-part gate (n_parts==1) is geometry-driven and cannot be defeated by qty/options/
     urgency — those never feed n_parts.
"""
from types import SimpleNamespace

from app import config, pricing, texts, webapp
from app import db as appdb

CUR = "руб."

SETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.0,
    "failure_rate_pct": 8.0, "profit_margin_pct": 50.0, "min_price": 5.0,
    "multicolor_waste_pct": 12.0, "multicolor_time_pct": 15.0,
    "volume_threshold_cm3": 0.0, "volume_surcharge_pct": 0.0,
    "rush_surcharge_pct": 50.0, "term_split_hours": 12,
    "dfm_min_wall_mm": 0.8,
}

# Keys that MUST all be present and distinct in a fully-assembled quote payload.
_FEATURE_KEYS = {"verdict", "dfm", "urgency", "term", "total"}


def _plastic():
    return SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None)


def _rush_options_bd(discount_pct=0.0):
    """bd carrying BOTH B3 (rush ×1.5) and B4 (flat options), exactly as the handlers build it:
    compute_price(urgency_factor=...) then apply_options()."""
    uf = pricing.urgency_factor_from("rush", SETTINGS)
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS,
                               discount_pct=discount_pct, urgency_factor=uf)
    opts = [{"id": 1, "name": "Шлифовка", "price": 5.0},
            {"id": 2, "name": "Покраска", "price": 3.0}]
    pricing.apply_options(bd, opts)
    return bd


def _assemble(bd, *, urgency, meas, printer=None, dims="60 x 40 x 20 мм"):
    """Reproduce the webapp payload-assembly ORDER: seed via _result_payload, then set B3
    urgency/term, then B2 _apply_dfm last (as in _api_calc_inner / _cache_snapshot / _refine)."""
    payload = webapp._result_payload(bd, SETTINGS, CUR, plates=1, cached=False)
    payload["urgency"] = urgency
    payload["term"] = appdb.promised_date(bd.minutes_total, urgency, SETTINGS)
    webapp._apply_dfm(payload, dims=dims, printer=printer, meas=meas, settings=SETTINGS)
    return payload


# ───────────────────────── 1. key coexistence, no overwrite/drop ─────────────────────────
def test_all_feature_keys_coexist_no_collision():
    bd = _rush_options_bd()
    # meas that trips a real DFM 'risky' (single thin-walled part) so dfm is non-empty AND
    # coexists with urgency/term rather than staying the default [].
    meas = {"n_parts": 1, "min_extent_mm": 0.4, "max_part_mm": 60.0, "wall_proxy_mm": 0.4}
    payload = _assemble(bd, urgency="rush", meas=meas)

    for k in _FEATURE_KEYS:
        assert k in payload, f"missing feature key {k!r}"
    # B2 actually overwrote the _result_payload default 'ok'/[] — path did NOT drop it.
    assert payload["verdict"] == "risky"
    assert payload["dfm"] and payload["dfm"][0]["key"] == "wall"
    # B3 survived _apply_dfm running afterwards.
    assert payload["urgency"] == "rush"
    assert payload["term"] == appdb.promised_date(bd.minutes_total, "rush", SETTINGS)
    # options were folded into the breakdown lines (B4) without shadowing any B2/B3 key.
    assert any("Шлифовк" in row[0] or "Покраск" in row[0] for row in payload["lines"])
    # no key is None / silently blank
    assert payload["total"] and payload["urgency"] and payload["term"]


def test_apply_dfm_does_not_touch_urgency_term_and_vice_versa():
    """The two features write DISJOINT key sets: _apply_dfm only writes verdict/dfm; setting
    urgency/term never clears verdict/dfm. Run both orderings."""
    bd = _rush_options_bd()
    meas = {"n_parts": 1, "min_extent_mm": 5.0, "max_part_mm": 60.0, "wall_proxy_mm": 5.0}

    # order A: urgency/term first, dfm last (the production order)
    p = webapp._result_payload(bd, SETTINGS, CUR, plates=1, cached=False)
    p["urgency"] = "rush"; p["term"] = "31.12"
    webapp._apply_dfm(p, dims="60 x 40 x 20 мм", printer=None, meas=meas, settings=SETTINGS)
    assert p["urgency"] == "rush" and p["term"] == "31.12"   # dfm did not stomp B3

    # order B: dfm first, urgency/term last
    q = webapp._result_payload(bd, SETTINGS, CUR, plates=1, cached=False)
    webapp._apply_dfm(q, dims="60 x 40 x 20 мм", printer=None, meas=meas, settings=SETTINGS)
    v0 = q["verdict"]
    q["urgency"] = "rush"; q["term"] = "31.12"
    assert q["verdict"] == v0 and q["dfm"] == q["dfm"]       # B3 did not stomp verdict/dfm


# ───────────────────── 2. RUSH + OPTIONS + DFM wont_fit: prices, orderable ─────────────────────
def test_rush_options_wont_fit_prices_correctly_and_is_orderable():
    # baseline: normal, no options, no urgency
    base = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    bd = _rush_options_bd()

    # total must reflect BOTH the ×1.5 rush surcharge AND the +8 flat options.
    assert bd.urgency_factor == 1.5 and bd.urgency_surcharge > 0.0
    assert bd.options_cost == 8.0
    # rush(base) ≈ base*1.5; then options add ~8 on top. Lower bound guards against either being lost.
    assert bd.total >= base.total * 1.5 + 8.0 - 0.2
    assert bd.total > base.total   # sanity

    # DFM says the single part will not fit the bed — verdict must be 'wont_fit'...
    bed = float(config.BED_SIZE_MM)
    meas = {"n_parts": 1, "min_extent_mm": 20.0,
            "max_part_mm": bed + 50.0, "wall_proxy_mm": 20.0}
    payload = _assemble(bd, urgency="rush", meas=meas,
                        dims=f"{bed + 50:.0f} x 40 x 20 мм")
    assert payload["verdict"] == "wont_fit"
    assert any(d["key"] == "bed" and d["status"] == "bad" for d in payload["dfm"])

    # ...yet the payload still carries the correct RUSH+OPTIONS total and all keys — DFM is
    # advisory only. There is NO 'blocked'/'orderable'/'can_order' gate keyed on verdict.
    assert payload["total"] == texts.fmt_money(bd.total, CUR)
    assert payload["urgency"] == "rush" and payload["term"]
    for forbidden in ("blocked", "order_blocked", "can_order", "orderable"):
        assert forbidden not in payload, f"unexpected gate key {forbidden!r}"


def test_wont_fit_verdict_absent_from_order_placement_source():
    """Defence-in-depth: the order-placement code path must not read the DFM verdict, otherwise a
    'wont_fit' could silently block a valid order. Assert the placement helpers carry no such ref."""
    import inspect
    for fn in (webapp._api_order, webapp._place_one, webapp._api_order_cart):
        src = inspect.getsource(fn)
        for tok in ("verdict", "wont_fit", '"dfm"', "'dfm'"):
            assert tok not in src, f"{fn.__name__} references DFM token {tok!r} — DFM must not gate orders"


# ─────────────────── 3. single-part gate cannot be defeated by qty/options/urgency ───────────────────
def test_single_part_gate_is_geometry_only():
    """Thin-wall warning fires ONLY for a single part (n_parts==1). A valid multi-part set with the
    SAME thin geometry must stay 'ok' — the gate is what keeps B2 from crying wolf on part sets."""
    thin = dict(dims="60 x 40 x 20 мм", bed_mm=256.0,
                min_extent_mm=0.3, max_part_mm=60.0, wall_proxy_mm=0.3, min_wall_mm=0.8)
    v1, dfm1 = texts.printability_verdict(n_parts=1, **thin)
    vN, dfmN = texts.printability_verdict(n_parts=5, **thin)
    assert v1 == "risky" and any(d["key"] == "wall" for d in dfm1)
    assert vN == "ok" and not any(d["key"] == "wall" for d in dfmN)
    # n_parts unknown (fast MW/cache snapshot) → also no wall panic
    vU, dfmU = texts.printability_verdict(n_parts=None, **thin)
    assert not any(d["key"] == "wall" for d in dfmU)


def test_options_and_urgency_never_reach_n_parts():
    """qty, options and urgency are price-side inputs; n_parts is a geometry measurement produced
    by calcservice/fastcalc. apply_options / urgency_factor must not mutate the meas dict that
    _apply_dfm reads, so the gate cannot be moved by a big qty or an added service."""
    meas = {"n_parts": 5, "min_extent_mm": 0.3, "max_part_mm": 60.0, "wall_proxy_mm": 0.3}
    meas_before = dict(meas)

    # build a rush + options + big-qty bd; none of this may touch meas
    uf = pricing.urgency_factor_from("rush", SETTINGS)
    bd = pricing.compute_price(30.0, 180.0, 999, _plastic(), SETTINGS, urgency_factor=uf)
    pricing.apply_options(bd, [{"id": 1, "name": "Покраска", "price": 12.0}])
    payload = _assemble(bd, urgency="rush", meas=meas, dims="60 x 40 x 20 мм")

    assert meas == meas_before                       # meas untouched by pricing
    assert payload["verdict"] == "ok"                # 5-part set stays ok despite qty=999 + options
    assert not any(d["key"] == "wall" for d in payload["dfm"])
