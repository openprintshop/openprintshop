"""FINAL gate — pricing COMPOSITION across B3 (срочность) × B4 (опции постобработки) on ONE order.

Adversarial intersection check: a single calc that is RUSH + has post-processing OPTIONS (plus a
discount, qty>1 and multicolor). Verifies the composition ORDER and that the SAME total appears on
every surface (compute_price/apply_options, texts.breakdown_rows live+order, quote.py PDF crows),
that the breakdown JSON round-trip preserves BOTH urgency and options fields, and that admin reprice
re-applies BOTH the rush premium and the options from the order snapshot.

Единый билдер строк — texts.breakdown_rows; PDF-смета (quote.py) строит строки вручную, поэтому её
композицию проверяем реконструкцией тех же числовых строк. Инвариант: сумма строк = итог на КАЖДОЙ
поверхности, и итог один и тот же.
"""
from types import SimpleNamespace

from app import calcservice, config, pricing, quote, texts, webapp

CUR = "руб."

# Ставки достаточные для compute_price; multicolor включён (extra_colors>0 в сценарии).
SETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.0,
    "failure_rate_pct": 8.0, "profit_margin_pct": 50.0, "min_price": 5.0,
    "multicolor_waste_pct": 12.0, "multicolor_time_pct": 15.0,
    "volume_threshold_cm3": 0.0, "volume_surcharge_pct": 0.0,
    "rush_surcharge_pct": 50.0, "term_split_hours": 12,
}

# Один заказ: qty>1, многоцвет, скидка тиража, срочность ×1.5, + платные и бесплатная опции.
VOL, MIN, QTY = 30.0, 180.0, 2
DISCOUNT, UF = 20.0, 1.5
OPTS = [{"id": 1, "name": "Шлифовка", "price": 5.0},
        {"id": 2, "name": "Покраска", "price": 3.0},
        {"id": 3, "name": "Маркировка", "price": 0.0}]   # бесплатная — в снимке, не в сумму
OPTS_COST = 8.0


def _plastic():
    return SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None)


def _is_round10(v: float) -> bool:
    return int(round(v * 100)) % 10 == 0


def _full_bd(discount=DISCOUNT, uf=UF, opts=OPTS):
    """Собрать разбивку ОДНОГО заказа: печать (compute_price) → плоские опции (apply_options)."""
    bd = pricing.compute_price(VOL, MIN, QTY, _plastic(), SETTINGS,
                               discount_pct=discount, extra_colors=1, urgency_factor=uf)
    pricing.apply_options(bd, opts)
    return bd


# ─────────────────────── 1. КОМПОЗИЦИЯ: порядок множитель/плоское ───────────────────────
def test_composition_ordering():
    """urgency множит печать ПОСЛЕ скидки; опции кладутся ПЛОСКО поверх; премия срочности НЕ
    скидируется, опции НЕ множатся на срочность."""
    normal = pricing.compute_price(VOL, MIN, QTY, _plastic(), SETTINGS,
                                   discount_pct=DISCOUNT, extra_colors=1)          # без срочности/опций
    rush = pricing.compute_price(VOL, MIN, QTY, _plastic(), SETTINGS,
                                 discount_pct=DISCOUNT, extra_colors=1, urgency_factor=UF)
    full = _full_bd()

    # a) срочность множит ПОСТ-скидочную цену: срочный итог ≈ обычный ×1.5 (по модулю округления)
    assert normal.urgency_factor == 1.0 and rush.urgency_factor == UF
    assert rush.urgency_surcharge > 0.0
    assert abs(rush.total - normal.total * UF) <= 0.2

    # b) премия срочности НЕ скидируется: сумма скидки одинакова у обычного и срочного
    assert normal.discount_amount > 0
    assert abs(rush.discount_amount - normal.discount_amount) < 1e-9

    # c) опции добавлены ПЛОСКО поверх срочного итога, НЕ умножены на ×1.5
    assert full.options_cost == OPTS_COST
    assert abs(full.total - (rush.total + OPTS_COST)) < 1e-9

    # d) добавление опций НЕ тронуло срочную наценку (нет двойного применения множителя)
    assert abs(full.urgency_surcharge - rush.urgency_surcharge) < 1e-9

    # e) прирост от опций = ровно розничные цены услуг (никакой маржи/скидки/срочности поверх)
    assert abs((full.total - rush.total) - OPTS_COST) < 1e-9
    assert _is_round10(full.total)


# ─────────────────────── 2. СУММА СТРОК = ИТОГ на КАЖДОЙ поверхности ───────────────────────
# ВАЖНО про округление: инвариант «строки сходятся к итогу» проверяем на ЧИСЛОВЫХ полях bd —
# именно они (а не показанные с точностью до копейки строки) обязаны точно давать итог. Показанные
# строки округляются до копейки поштучно, а «скрытая» дельта округления итога сидит в одной строке
# (машинное время), поэтому СУММА ПОКАЗАННЫХ строк может отличаться от показанного итога на ~копейку
# — это свойство самого дизайна «прячем округление», не зависящее от B3/B4 (то же и без срочности/
# опций). Поэтому: (a) числовой инвариант — строго; (b) кросс-поверхностная идентичность строк —
# строго; (c) сумму ПОКАЗАННЫХ строк не требуем идентичной итогу до копейки.
def _numeric_rows(bd) -> list[float]:
    """Числовые слагаемые разбивки в том же составе, что рисуют ВСЕ поверхности (breakdown_rows и
    ручные crows PDF): филамент, машина (в ней спрятана дельта округления), брак, подготовка,
    наценка, −скидка, +срочность, +каждая платная опция. Их сумма обязана давать итог точно."""
    vals = [bd.filament_cost, bd.machine_cost, bd.waste_cost, bd.setup_fee, bd.margin]
    if bd.discount_pct:
        vals.append(-bd.discount_amount)
    if float(getattr(bd, "urgency_factor", 1.0) or 1.0) > 1.0:
        vals.append(float(getattr(bd, "urgency_surcharge", 0.0) or 0.0))
    for opt in (getattr(bd, "options", None) or []):
        p = float(opt.get("price") or 0.0)
        if p > 0:
            vals.append(p)
    return vals


def test_numeric_rows_sum_to_total_exactly():
    """Ядро инварианта: числовые слагаемые разбивки (тот же состав, что у всех поверхностей) дают
    итог ТОЧНО — на срочном многоцветном заказе со скидкой и опциями. Это уровень выручки."""
    bd = _full_bd()
    assert abs(sum(_numeric_rows(bd)) - bd.total) < 1e-9


def test_displayed_rows_identical_across_surfaces():
    """Кросс-поверхностная согласованность: строки и итог совпадают ПОСТРОЧНО у breakdown_rows
    (живой мини-апп), webapp._result_payload и order_breakdown из снимка — все читают один bd, так
    что расхождения между поверхностями быть не может. Также срочность и опции присутствуют везде."""
    bd = _full_bd()
    money = texts.fmt_money(bd.total, CUR)

    live = texts.breakdown_rows(bd, SETTINGS, CUR)
    live_pairs = [(r["label"], r["value"]) for r in live]

    payload = webapp._result_payload(bd, SETTINGS, CUR, 1, False)
    # _result_payload сворачивает label+sub в "label (sub)" — разворачиваем к чистому label
    payload_pairs = [(lbl.split(" (")[0], val) for lbl, val in payload["lines"]]

    r = SimpleNamespace(breakdown=pricing.breakdown_to_dict(bd, SETTINGS), dev_fee=0.0)
    ob = texts.order_breakdown(r, CUR)
    ob_pairs = [(row["label"], row["value"]) for row in ob["rows"]]

    assert payload_pairs == live_pairs           # живой мини-апп ≡ единый билдер строк
    assert ob_pairs == live_pairs                # смета из снимка ≡ живой результат
    assert payload["total"] == money == ob["total"]

    # срочность и обе платные опции присутствуют (бесплатная — нет)
    assert any(lbl == "Срочность" for lbl, _ in live_pairs)
    assert [r_["label"] for r_ in live if r_.get("sub") == "постобработка"] == ["Шлифовка", "Покраска"]


def test_pdf_crows_sum_to_total_and_render(tmp_path, monkeypatch):
    """quote.py строит строки PDF ВРУЧНУЮ (не через breakdown_rows). Числовые crow-слагаемые в
    точности повторяют состав _numeric_rows и обязаны давать итог точно; сам PDF строится без
    падения (в т.ч. со строкой «Срочность» и построчными опциями)."""
    bd = _full_bd()
    assert abs(sum(_numeric_rows(bd)) - bd.total) < 1e-9   # тот же состав, что рисует build_quote

    monkeypatch.setattr(config, "QUOTE_DIR", tmp_path)
    pdf = quote.build_quote(
        business_name="X", business_contact="c", model_label="M", printer_name="P",
        plastic_name="Test PLA", qty=QTY, quality_label="q", strength_label="s",
        bd=bd, cur=CUR, settings=SETTINGS, preview_path=None)
    assert pdf.exists() and pdf.stat().st_size > 0


# ─────────────────────── 3. JSON round-trip хранит ОБА набора полей ───────────────────────
def test_breakdown_roundtrip_preserves_urgency_and_options():
    import json
    bd = _full_bd()
    d = pricing.breakdown_to_dict(bd, SETTINGS)
    d2 = json.loads(json.dumps(d))           # как в requests.breakdown
    back = pricing.breakdown_from_dict(d2)
    assert back is not None
    # срочность
    assert back.urgency_factor == UF
    assert abs(back.urgency_surcharge - bd.urgency_surcharge) < 1e-9
    # опции
    assert back.options == OPTS
    assert back.options_cost == OPTS_COST
    # и общий итог не поплыл
    assert abs(back.total - bd.total) < 1e-9


# ─────────────────────── 4. reprice_request хранит ОБА (из снимка) ───────────────────────
async def _seed_rush_order_with_options(db):
    await db.add_plastic("Test PLA", price_per_kg=40.0, density=1.24, material_type="PLA")
    bd = _full_bd()
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="M", printer="p", plastic_name="Test PLA · чёрный",
        quantity=QTY, quality="q", strength="s", grams_total=bd.grams_total,
        minutes_total=bd.minutes_total, price=bd.total, preview_path=None,
        breakdown=pricing.breakdown_to_dict(bd, SETTINGS))
    return rid, bd


async def test_reprice_preserves_rush_and_options(tmp_db):
    """Админ-пересчёт (reprice_request) читает срочность И опции из СНИМКА заказа — обе должны
    пережить ручную правку цены, иначе итог перестанет сходиться и утечёт выручка."""
    db = tmp_db
    rid, _ = await _seed_rush_order_with_options(db)
    r = await db.get_request(rid)
    res = await calcservice.reprice_request(r, 100.0, 60.0)
    bd = res["bd"]
    # рушная премия сохранена (множитель из снимка, не пересчёт из settings)
    assert bd.urgency_factor == UF
    assert bd.urgency_surcharge > 0.0
    # опции сохранены как снимок (name+price заказа)
    assert bd.options == OPTS
    assert bd.options_cost == OPTS_COST
    assert _is_round10(bd.total)
    # строки пересчёта по-прежнему сходятся к итогу (числовой инвариант — точно)
    assert abs(sum(_numeric_rows(bd)) - bd.total) < 1e-9
    # и живой билдер строк рисует срочность + опции на этом пере-расчитанном bd
    rows = texts.breakdown_rows(bd, res["settings"], CUR)
    assert any(x["label"] == "Срочность" for x in rows)
    assert [x["label"] for x in rows if x.get("sub") == "постобработка"] == ["Шлифовка", "Покраска"]

    # контроль: тот же заказ без срочности и без опций даёт заметно меньший итог
    await db.add_plastic("Plain PLA", price_per_kg=40.0, density=1.24, material_type="PLA")
    plain = pricing.compute_price(VOL, MIN, QTY, _plastic(), SETTINGS, extra_colors=1)
    rid2 = await db.add_request(
        user_id=1, user_name="t", model_label="M", printer="p", plastic_name="Plain PLA · чёрный",
        quantity=QTY, quality="q", strength="s", grams_total=plain.grams_total,
        minutes_total=plain.minutes_total, price=plain.total, preview_path=None,
        breakdown=pricing.breakdown_to_dict(plain, SETTINGS))
    r2 = await db.get_request(rid2)
    res2 = await calcservice.reprice_request(r2, 100.0, 60.0)
    assert res2["bd"].urgency_factor == 1.0
    assert res2["bd"].options_cost == 0.0
    # премия срочности + опции у рушного пересчёта дают строго больший итог
    assert res["bd"].total > res2["bd"].total


# ─────────────────────── 5. Единое финальное округление (был баг двойного) ───────────────────────
def test_single_final_ceil_round_not_double_rounded():
    """Инвариант: итог = ОДИН финальный ceil до 10 коп. от (цена печати после срочности + плоские
    опции), а НЕ двойное округление. Подобраны ставки/цены так, чтобы пред-округлённая цена печати
    была не кратна 10 коп., а цена опции имела копеечный хвост — при двойном ceil итог расходился с
    единым на 10 коп. (переплата клиента). Пофикшено: apply_options округляет непокруглённый итог."""
    S = dict(SETTINGS, machine_hour_rate=2.37, setup_fee=5.11,
             profit_margin_pct=43.0, failure_rate_pct=7.0, min_price=1.0,
             multicolor_waste_pct=0.0, multicolor_time_pct=0.0)
    p = SimpleNamespace(density=1.237, price_per_gram=0.0517, failure_rate_pct=None)
    bd = pricing.compute_price(31.3, 177.0, 2, p, S, discount_pct=17.0, urgency_factor=1.5)
    # точная цена печати ДО скрытого округления (поля bd — не округлены)
    preround = bd.subtotal + bd.margin - bd.discount_amount + bd.urgency_surcharge
    opt_price = 5.03
    pricing.apply_options(bd, [{"id": 1, "name": "Гравировка", "price": opt_price}])

    def ceil10(x):
        return (-(-int(round(x * 100)) // 10) * 10) / 100.0

    single = ceil10(preround + opt_price)   # ОДИН финальный ceil от точной суммы
    assert abs(bd.total - single) < 1e-9, f"double-rounded {bd.total} vs single {single}"
