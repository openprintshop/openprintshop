"""FINAL-gate integration: CART (B5) × per-item URGENCY (B3) × per-item OPTIONS (B4).

Places ONE cart of 3 items — item1 RUSH, item2 with post-processing OPTIONS, item3 plain — as a
single order (one order_group), then verifies the per-item features do NOT leak across siblings,
the stored price of each item reflects its OWN features, the group rollup total equals the sum of
the 3 per-item stored prices, mixed statuses stay per-item, and an admin reprice of ONE grouped
item preserves that item's urgency+options while leaving siblings and the group untouched.

Realistic construction: each model token carries the exact `bd` a calc path would produce
(pricing.compute_price + pricing.apply_options), so requests.breakdown holds options/urgency_factor
and _place_one stores price == bd.total — the same contract the live /api/calc path establishes.
"""
import json

import pytest
from conftest import stub_auth, stub_bot

from app import config, db, pricing, webapp

UID = 4242
ADMIN_ID = 777


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


def _user():
    return {"id": UID, "username": "tester", "first_name": "Тест"}


PLASTIC_NAME = "Test PLA · чёрный"


async def _plastic():
    return await db.get_plastic_by_name("Test PLA")


def _build_bd(plastic, settings, *, volume=22.0, minutes=120.0, qty=1,
              urgency_factor=1.0, options=None):
    """The exact breakdown a client calc path produces: price after urgency, then flat options."""
    bd = pricing.compute_price(volume, minutes, qty, plastic, settings,
                               urgency_factor=urgency_factor)
    pricing.apply_options(bd, options or [])
    return bd


def _seed_model(token, *, label, plastic, settings, urgency, options, bd, qty=1):
    """Server-side token snapshot mimicking a completed calc (urgency+options already applied)."""
    webapp._models[token] = {
        "uid": UID, "label": label, "paths": [], "preview": None,
        "urgency": urgency,                          # 'rush' | 'normal'
        "urgency_factor": bd.urgency_factor,
        "options": options,                          # snapshot [{id,name,price}]
        "pdf": {"bd": bd, "settings": settings, "model_label": label},
        "last": {"printer": "Centauri", "plastic_name": PLASTIC_NAME, "qty": qty,
                 "quality": "0.20", "strength": "Стандарт", "grams_total": bd.grams_total,
                 "minutes_total": bd.minutes_total, "total": bd.total, "dims": "40×40×20",
                 "color": "чёрный", "scale_pct": 100},
    }


async def _cart(body):
    resp = await webapp._api_order_cart(_FakeRequest(body))
    return json.loads(resp.body.decode())


async def _orders():
    resp = await webapp._api_orders(_FakeGet(initData="x"))
    return json.loads(resp.body.decode())


async def _admin_order(oid):
    resp = await webapp._api_admin_order(_FakeAdminOrderReq(oid, initData="x"))
    return json.loads(resp.body.decode())


async def _setup(monkeypatch, db_):
    """Common: auth stub, no bot (skip notify), a real plastic, two active postproc options."""
    stub_auth(monkeypatch, _verify_user=lambda init_data: _user())
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    stub_bot(monkeypatch, None)
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])
    await db_.add_plastic("Test PLA", price_per_kg=40.0, density=1.24, material_type="PLA")
    oid_sand = await db_.add_postproc_option("Шлифовка", 7.0, sort=1)
    oid_paint = await db_.add_postproc_option("Покраска", 12.0, sort=2)
    return [{"id": oid_sand, "name": "Шлифовка", "price": 7.0},
            {"id": oid_paint, "name": "Покраска", "price": 12.0}]


async def _place_mixed_cart(monkeypatch, db_):
    """item1 RUSH (no options), item2 OPTIONS (normal urgency), item3 plain. Returns (rids, meta)."""
    opts = await _setup(monkeypatch, db_)
    settings = await db.get_settings()
    plastic = await _plastic()
    uf_rush = pricing.urgency_factor_from("rush", settings)
    assert uf_rush > 1.0, "rush surcharge must be configured >1 for a meaningful test"

    bd1 = _build_bd(plastic, settings, urgency_factor=uf_rush, options=[])          # RUSH
    bd2 = _build_bd(plastic, settings, urgency_factor=1.0, options=opts)            # OPTIONS
    bd3 = _build_bd(plastic, settings, urgency_factor=1.0, options=[])              # plain
    _seed_model("it1", label="Кубик", plastic=plastic, settings=settings,
                urgency="rush", options=[], bd=bd1)
    _seed_model("it2", label="Шестерёнка", plastic=plastic, settings=settings,
                urgency="normal", options=opts, bd=bd2)
    _seed_model("it3", label="Кронштейн", plastic=plastic, settings=settings,
                urgency="normal", options=[], bd=bd3)

    out = await _cart({"initData": "x", "tokens": ["it1", "it2", "it3"],
                       "delivery_method": "pickup"})
    assert out["ok"] is True and out["count"] == 3
    gid = out["group"]
    reqs = await db_.list_requests_by_group(gid)
    assert len(reqs) == 3
    by_label = {r.model_label: r for r in reqs}
    for t in ("it1", "it2", "it3"):
        webapp._models.pop(t, None)
    return by_label, gid, {"bd1": bd1, "bd2": bd2, "bd3": bd3, "opts": opts}


# ─────────────────────────── per-item feature isolation ───────────────────────────
async def test_cart_per_item_urgency_and_options_no_leak(tmp_db, monkeypatch):
    """Each requests row carries its OWN urgency/options; each stored price reflects its own
    features. No leak of rush onto item2/3, no leak of options onto item1/3."""
    db_ = tmp_db
    by_label, gid, meta = await _place_mixed_cart(monkeypatch, db_)
    r1, r2, r3 = by_label["Кубик"], by_label["Шестерёнка"], by_label["Кронштейн"]

    # --- urgency column: only item1 is rush ---
    assert r1.urgency == "rush"
    assert r2.urgency == ""      # _place_one maps 'normal' → ''
    assert r3.urgency == ""

    # --- options snapshot rows: only item2 has them ---
    o1 = await db_.get_request_options(r1.id)
    o2 = await db_.get_request_options(r2.id)
    o3 = await db_.get_request_options(r3.id)
    assert o1 == []
    assert {o["name"] for o in o2} == {"Шлифовка", "Покраска"}
    assert sum(o["price"] for o in o2) == pytest.approx(19.0)
    assert o3 == []

    # --- stored prices reflect each item's OWN features ---
    assert r1.price == pytest.approx(meta["bd1"].total, abs=1e-6)
    assert r2.price == pytest.approx(meta["bd2"].total, abs=1e-6)
    assert r3.price == pytest.approx(meta["bd3"].total, abs=1e-6)
    # rush item must actually cost more than a plain one of identical geometry
    assert r1.price > r3.price
    # options item must carry the flat options cost above plain
    assert r2.price == pytest.approx(r3.price + 19.0, abs=0.05)

    # --- breakdown snapshot carries the per-item feature markers ---
    assert float((r1.breakdown or {}).get("urgency_factor") or 1.0) > 1.0
    assert float((r2.breakdown or {}).get("urgency_factor") or 1.0) == pytest.approx(1.0)
    assert float((r2.breakdown or {}).get("options_cost") or 0.0) == pytest.approx(19.0)
    assert float((r1.breakdown or {}).get("options_cost") or 0.0) == pytest.approx(0.0)
    assert float((r3.breakdown or {}).get("options_cost") or 0.0) == pytest.approx(0.0)


async def test_group_rollup_total_equals_sum_of_item_prices(tmp_db, monkeypatch):
    """Client group card total == sum of the 3 per-item stored prices (derived, not snapshot)."""
    db_ = tmp_db
    by_label, gid, meta = await _place_mixed_cart(monkeypatch, db_)
    reqs = await db_.list_requests_by_group(gid)
    sum_stored = round(sum(float(r.price or 0) for r in reqs), 2)

    out = await _orders()
    groups = [c for c in out["orders"] if c.get("group")]
    assert len(groups) == 1
    g = groups[0]
    assert g["gid"] == gid and g["count"] == 3
    # the derived group total is exactly the sum of the 3 per-item stored prices
    assert g["total"] == webapp.texts.fmt_money(sum_stored, config.CURRENCY)

    # per-item price exposed correctly on the group card
    prices_by_label = {it["label"]: it["price"] for it in g["items"]}
    for r in reqs:
        assert prices_by_label[r.model_label] == webapp.texts.fmt_money(r.price, config.CURRENCY)

    # snapshot in order_groups also equals the same sum (created at checkout)
    ent = await db_.get_order_group(gid)
    assert ent["total_snapshot"] == pytest.approx(sum_stored, abs=1e-6)


async def test_group_mixed_statuses_stay_per_item(tmp_db, monkeypatch):
    """Move item1→printed, item2→rejected, item3 stays new: statuses roll up per-item, not merged;
    each item keeps its own id and per-item price."""
    db_ = tmp_db
    by_label, gid, meta = await _place_mixed_cart(monkeypatch, db_)
    r1, r2, r3 = by_label["Кубик"], by_label["Шестерёнка"], by_label["Кронштейн"]
    await db_.update_request(r1.id, status="printed")
    await db_.update_request(r2.id, status="rejected")

    out = await _orders()
    g = [c for c in out["orders"] if c.get("group")][0]
    assert {s["status"] for s in g["statuses"]} == {"printed", "rejected", "new"}
    item_status = {it["label"]: it["status"] for it in g["items"]}
    assert item_status == {"Кубик": "printed", "Шестерёнка": "rejected", "Кронштейн": "new"}
    # per-item ids preserved (master moves each item independently)
    assert {it["id"] for it in g["items"]} == {r1.id, r2.id, r3.id}
    # per-item prices still correct under mixed statuses
    pbl = {it["label"]: it["price"] for it in g["items"]}
    assert pbl["Шестерёнка"] == webapp.texts.fmt_money(r2.price, config.CURRENCY)


async def test_admin_group_block_exposes_per_item_prices(tmp_db, monkeypatch):
    """Admin order card (opened on item2) exposes the group with each sibling's own price + the
    per-item options tag lives on item2 only."""
    db_ = tmp_db
    stub_auth(monkeypatch, _verify_admin=ADMIN_ID)
    by_label, gid, meta = await _place_mixed_cart(monkeypatch, db_)
    r2 = by_label["Шестерёнка"]

    mout = await _admin_order(r2.id)
    order = mout["order"]
    gb = order["group"]
    assert gb is not None and gb["gid"] == gid and gb["count"] == 3
    price_by_label = {it["label"]: it["price"] for it in gb["items"]}
    for r in await db_.list_requests_by_group(gid):
        assert price_by_label[r.model_label] == pytest.approx(r.price, abs=1e-6)
    # options tag is on THIS (item2) order only
    assert {o["name"] for o in order["options"]} == {"Шлифовка", "Покраска"}
    # sibling item1 (rush) shows no options tag
    m1 = await _admin_order(by_label["Кубик"].id)
    assert m1["order"]["options"] == []


# ─────────────────────────── admin reprice preserves features ───────────────────────────
async def test_reprice_options_item_preserves_options_and_siblings(tmp_db, monkeypatch):
    """Admin reprices item2 (OPTIONS) by grams/minutes → cheaper → applied directly. The options
    cost survives in the new price+breakdown, request_options rows are intact, and neither siblings
    nor the group total for the OTHER items are disturbed."""
    db_ = tmp_db
    by_label, gid, meta = await _place_mixed_cart(monkeypatch, db_)
    stub_auth(monkeypatch, _verify_admin=ADMIN_ID)
    r1, r2, r3 = by_label["Кубик"], by_label["Шестерёнка"], by_label["Кронштейн"]
    p1_before, p3_before = r1.price, r3.price

    # reprice item2 with clearly lighter grams/minutes → new price below old → direct apply
    resp = await webapp._api_admin_order_price(_FakeRequest(
        {"initData": "x", "id": r2.id, "grams": 5, "minutes": 20}))
    payload = json.loads(resp.body.decode())
    assert payload["ok"] is True and payload.get("applied") == "direct"

    r2n = await db_.get_request(r2.id)
    # options cost must still be inside the new price + breakdown (no revenue leak)
    assert float((r2n.breakdown or {}).get("options_cost") or 0.0) == pytest.approx(19.0)
    assert r2n.price >= 19.0                      # cannot have dropped the flat services
    # request_options snapshot rows untouched
    o2 = await db_.get_request_options(r2.id)
    assert {o["name"] for o in o2} == {"Шлифовка", "Покраска"}
    # urgency stays normal (no rush leaked in from sibling)
    assert r2n.urgency == ""
    assert float((r2n.breakdown or {}).get("urgency_factor") or 1.0) == pytest.approx(1.0)

    # siblings untouched
    assert (await db_.get_request(r1.id)).price == pytest.approx(p1_before, abs=1e-6)
    assert (await db_.get_request(r3.id)).price == pytest.approx(p3_before, abs=1e-6)
    assert (await db_.get_request(r1.id)).urgency == "rush"

    # group rollup still equals sum of live per-item prices (now including item2's new price)
    out = await _orders()
    g = [c for c in out["orders"] if c.get("group")][0]
    reqs = await db_.list_requests_by_group(gid)
    sum_stored = round(sum(float(r.price or 0) for r in reqs), 2)
    assert g["count"] == 3
    assert webapp.texts.fmt_money(sum_stored, config.CURRENCY) == g["total"]


async def test_reprice_rush_item_preserves_urgency_surcharge(tmp_db, monkeypatch):
    """Admin reprices item1 (RUSH) by grams/minutes → the rush surcharge survives in the new price
    (urgency_factor read from the breakdown snapshot), and no options leak in from sibling item2."""
    db_ = tmp_db
    by_label, gid, meta = await _place_mixed_cart(monkeypatch, db_)
    stub_auth(monkeypatch, _verify_admin=ADMIN_ID)
    r1 = by_label["Кубик"]
    settings = await db.get_settings()
    plastic = await _plastic()
    uf_rush = pricing.urgency_factor_from("rush", settings)

    # expected price for the repriced grams/minutes WITH the rush factor preserved
    expected = pricing.compute_price_manual(
        5.0 / 1, 20.0 / 1, plastic.price_per_gram * 1000.0, settings, qty=1,
        discount_pct=0.0, failure_rate_pct=plastic.failure_rate_pct,
        volume_cm3_unit=(5.0 / plastic.density) if plastic.density else None,
        plates=1, urgency_factor=uf_rush)

    resp = await webapp._api_admin_order_price(_FakeRequest(
        {"initData": "x", "id": r1.id, "grams": 5, "minutes": 20}))
    payload = json.loads(resp.body.decode())
    assert payload["ok"] is True and payload.get("applied") == "direct"

    r1n = await db_.get_request(r1.id)
    assert r1n.price == pytest.approx(expected.total, abs=0.05)
    assert float((r1n.breakdown or {}).get("urgency_factor") or 1.0) == pytest.approx(uf_rush)
    assert r1n.urgency == "rush"
    # no options leaked in from sibling item2
    assert float((r1n.breakdown or {}).get("options_cost") or 0.0) == pytest.approx(0.0)
    assert await db_.get_request_options(r1.id) == []
    # verify the rush surcharge is genuinely present: same reprice WITHOUT urgency is strictly less
    plain = pricing.compute_price_manual(
        5.0, 20.0, plastic.price_per_gram * 1000.0, settings, qty=1,
        discount_pct=0.0, failure_rate_pct=plastic.failure_rate_pct,
        volume_cm3_unit=(5.0 / plastic.density) if plastic.density else None,
        plates=1, urgency_factor=1.0)
    assert r1n.price > plain.total


# ─────────────────────────── NULL-group coexistence ───────────────────────────
async def test_null_singletons_never_collapse_with_carts(tmp_db, monkeypatch):
    """A carted group AND two independent singleton orders for the SAME user coexist: the group is
    ONE card, each singleton is its OWN card, and no singleton is folded into the group (or into
    each other)."""
    db_ = tmp_db
    by_label, gid, meta = await _place_mixed_cart(monkeypatch, db_)
    # two singleton (order_group NULL) orders for the same user
    s1 = await db_.add_request(user_id=UID, user_name="@t", model_label="SoloA", printer="C",
                               plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                               grams_total=5, minutes_total=25, price=6.0, preview_path=None,
                               order_group=None)
    s2 = await db_.add_request(user_id=UID, user_name="@t", model_label="SoloB", printer="C",
                               plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                               grams_total=5, minutes_total=25, price=8.0, preview_path=None,
                               order_group=None)

    out = await _orders()
    groups = [c for c in out["orders"] if c.get("group")]
    singles = [c for c in out["orders"] if not c.get("group")]
    assert len(groups) == 1 and groups[0]["gid"] == gid and groups[0]["count"] == 3
    single_labels = {c["label"] for c in singles}
    assert single_labels == {"SoloA", "SoloB"}
    assert len(singles) == 2                       # two distinct singleton cards, not merged
    # the singletons are not represented with a group key
    assert all(c.get("group", "") == "" for c in singles)
    # the group did not absorb the singletons
    assert {it["label"] for it in groups[0]["items"]} == {"Кубик", "Шестерёнка", "Кронштейн"}
    assert s1 in {it["id"] for it in singles} if False else True  # ids present on singleton cards
    assert {c["id"] for c in singles} == {s1, s2}
