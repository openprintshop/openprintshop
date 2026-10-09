"""Денежные расхождения между поверхностями (волна 5, шаг 2) — тесты на КОПЕЙКИ, не на факт вызова.

Что здесь закреплено:

1. Эффективная скидка «max(тираж, персональная, промокод)» была выписана дословно четырьмя
   копиями (calcservice + три ручки webapp). Сведение в `pricing.effective_discount_pct` +
   `calcservice.effective_discount` обязано быть БИТ-В-БИТ: голая таблица итогов на матрице
   0/5/20/100 % × срочность × опции × (мелкая/крупная/многоцветная модель) снята с кода ДО
   правки и вшита сюда числами.
2. Округление итога вверх до 10 копеек жило тремя копиями формулы в pricing.py. Сведено в
   `pricing.ceil10`; таблица значений — тоже с кода ДО правки.
3. Бот считал самопечать по `plastic.price_per_kg`, то есть МИМО коэффициента сложности
   материала (`price_factor`), тогда как все веб-пути берут `price_per_gram * 1000`. Тест
   красный на старом коде.
4. Плата за разработку модели (`dev_fee`) не попадала в PDF-смету: клиент видел в приложении
   одну сумму, а в присланном PDF — другую, меньше на dev_fee. Тест красный на старом коде.
"""
from types import SimpleNamespace

import pytest
from conftest import stub_auth, stub_bot

from app import calcservice, config, pricing, quote, texts, webapp
from app.handlers import user_flow

# ───────────────────────────── общий стенд расчёта ─────────────────────────────
SETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.5, "failure_rate_pct": 8.0,
    "profit_margin_pct": 50.0, "min_price": 5.0, "multicolor_waste_pct": 12.0,
    "multicolor_time_pct": 15.0, "volume_threshold_cm3": 200.0, "volume_surcharge_pct": 15.0,
    "rush_surcharge_pct": 50.0,
}
PLASTIC = SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None)
OPTS = [{"id": 1, "name": "Шлифовка", "price": 3.33}]

# (скидка %, множитель срочности, есть опции, объём см³, минуты, тираж, доп. цветов) → ИТОГО, руб.
# Снято прогоном ДО рефакторинга (app/pricing.py в состоянии волны 5, шаг 1).
GOLDEN_TOTALS = [
    (0.0, 1.0, False, 30.0, 180.0, 1, 0, 21.8),
    (0.0, 1.0, False, 250.0, 900.0, 3, 2, 302.6),
    (0.0, 1.0, False, 0.5, 2.0, 1, 0, 9.2),
    (0.0, 1.0, True, 30.0, 180.0, 1, 0, 25.1),
    (0.0, 1.0, True, 250.0, 900.0, 3, 2, 305.9),
    (0.0, 1.0, True, 0.5, 2.0, 1, 0, 12.5),
    (0.0, 1.5, False, 30.0, 180.0, 1, 0, 32.6),
    (0.0, 1.5, False, 250.0, 900.0, 3, 2, 453.9),
    (0.0, 1.5, False, 0.5, 2.0, 1, 0, 13.8),
    (0.0, 1.5, True, 30.0, 180.0, 1, 0, 36.0),
    (0.0, 1.5, True, 250.0, 900.0, 3, 2, 457.2),
    (0.0, 1.5, True, 0.5, 2.0, 1, 0, 17.1),
    (5.0, 1.0, False, 30.0, 180.0, 1, 0, 20.7),
    (5.0, 1.0, False, 250.0, 900.0, 3, 2, 287.5),
    (5.0, 1.0, False, 0.5, 2.0, 1, 0, 8.7),
    (5.0, 1.0, True, 30.0, 180.0, 1, 0, 24.0),
    (5.0, 1.0, True, 250.0, 900.0, 3, 2, 290.8),
    (5.0, 1.0, True, 0.5, 2.0, 1, 0, 12.1),
    (5.0, 1.5, False, 30.0, 180.0, 1, 0, 31.0),
    (5.0, 1.5, False, 250.0, 900.0, 3, 2, 431.2),
    (5.0, 1.5, False, 0.5, 2.0, 1, 0, 13.1),
    (5.0, 1.5, True, 30.0, 180.0, 1, 0, 34.3),
    (5.0, 1.5, True, 250.0, 900.0, 3, 2, 434.5),
    (5.0, 1.5, True, 0.5, 2.0, 1, 0, 16.4),
    (20.0, 1.0, False, 30.0, 180.0, 1, 0, 17.4),
    (20.0, 1.0, False, 250.0, 900.0, 3, 2, 242.1),
    (20.0, 1.0, False, 0.5, 2.0, 1, 0, 7.4),
    (20.0, 1.0, True, 30.0, 180.0, 1, 0, 20.8),
    (20.0, 1.0, True, 250.0, 900.0, 3, 2, 245.4),
    (20.0, 1.0, True, 0.5, 2.0, 1, 0, 10.7),
    (20.0, 1.5, False, 30.0, 180.0, 1, 0, 26.1),
    (20.0, 1.5, False, 250.0, 900.0, 3, 2, 363.1),
    (20.0, 1.5, False, 0.5, 2.0, 1, 0, 11.0),
    (20.0, 1.5, True, 30.0, 180.0, 1, 0, 29.5),
    (20.0, 1.5, True, 250.0, 900.0, 3, 2, 366.4),
    (20.0, 1.5, True, 0.5, 2.0, 1, 0, 14.4),
    (100.0, 1.0, False, 30.0, 180.0, 1, 0, 5.0),
    (100.0, 1.0, False, 250.0, 900.0, 3, 2, 5.0),
    (100.0, 1.0, False, 0.5, 2.0, 1, 0, 5.0),
    (100.0, 1.0, True, 30.0, 180.0, 1, 0, 8.4),
    (100.0, 1.0, True, 250.0, 900.0, 3, 2, 8.4),
    (100.0, 1.0, True, 0.5, 2.0, 1, 0, 8.4),
    (100.0, 1.5, False, 30.0, 180.0, 1, 0, 5.0),
    (100.0, 1.5, False, 250.0, 900.0, 3, 2, 5.0),
    (100.0, 1.5, False, 0.5, 2.0, 1, 0, 5.0),
    (100.0, 1.5, True, 30.0, 180.0, 1, 0, 8.4),
    (100.0, 1.5, True, 250.0, 900.0, 3, 2, 8.4),
    (100.0, 1.5, True, 0.5, 2.0, 1, 0, 8.4),
]


def _price(disc, uf, with_opts, vol, mins, qty, extra_colors):
    bd = pricing.compute_price(vol, mins, qty, PLASTIC, SETTINGS, discount_pct=disc,
                               extra_colors=extra_colors, plates=2, urgency_factor=uf)
    pricing.apply_options(bd, [dict(o) for o in OPTS] if with_opts else None)
    return bd


@pytest.mark.parametrize("row", GOLDEN_TOTALS, ids=lambda r: f"d{r[0]:g}-uf{r[1]:g}-opt{int(r[2])}-v{r[3]:g}")
def test_price_matrix_unchanged_to_the_kopeck(row):
    """Матрица цен с эталонными числами: сведение четырёх копий скидки и трёх копий округления
    в общие функции не имеет права сдвинуть ни одну копейку."""
    disc, uf, with_opts, vol, mins, qty, extra_colors, expected = row
    bd = _price(disc, uf, with_opts, vol, mins, qty, extra_colors)
    assert round(bd.total, 2) == expected
    # инвариант «круглого итога»: сумма всегда кратна 10 копейкам
    assert int(round(bd.total * 100)) % 10 == 0


def test_price_matrix_rows_sum_to_total():
    """Строки разбивки по-прежнему сходятся к итогу на всей матрице (округление спрятано
    в машинное время — если ceil10 вынести неверно, разъедется именно этот инвариант).

    Случаи, где сработала минимальная цена, из проверки исключены: там итог задан настройкой
    min_price, а не суммой строк, — это давнее и намеренное поведение compute_price."""
    checked = 0
    for disc, uf, with_opts, vol, mins, qty, extra_colors, _ in GOLDEN_TOTALS:
        bd = _price(disc, uf, with_opts, vol, mins, qty, extra_colors)
        if bd.min_applied:
            continue
        rows = (bd.filament_cost + bd.machine_cost + bd.waste_cost + bd.setup_fee + bd.margin
                - bd.discount_amount + bd.urgency_surcharge + bd.options_cost)
        assert abs(rows - bd.total) < 1e-9, (disc, uf, with_opts, vol)
        checked += 1
    assert checked >= 36


# ───────────────────────────── 1. эффективная скидка ─────────────────────────────
@pytest.mark.parametrize(("tier", "user", "promo", "expected"), [
    (0.0, 0.0, 0.0, 0.0),
    (5.0, 0.0, 0.0, 5.0),
    (0.0, 20.0, 0.0, 20.0),
    (0.0, 0.0, 15.0, 15.0),
    (5.0, 20.0, 15.0, 20.0),
    (5.0, 20.0, 30.0, 30.0),
    (100.0, 20.0, 0.0, 100.0),
    (10.0, 10.0, 10.0, 10.0),
])
def test_effective_discount_pct_is_plain_max(tier, user, promo, expected):
    assert pricing.effective_discount_pct(tier, user, promo) == expected


def test_effective_discount_pct_tolerates_none_promo():
    """Три из четырёх сведённых копий писали `promo_pct or 0.0` — None/пустая строка обязаны
    вести себя как «промокода нет», а не падать."""
    assert pricing.effective_discount_pct(5.0, 0.0, None) == 5.0
    assert pricing.effective_discount_pct(0.0, 0.0, None) == 0.0


def test_effective_discount_pct_does_not_clamp():
    """Клампа 0..100 здесь СОЗНАТЕЛЬНО нет: тираж и промокод пишутся в базу без верхней
    границы (app/web/prices.py, action='add_tier' и 'add'), и молча урезать уже заведённую
    мастером ставку — это смена поведения, а не рефакторинг."""
    assert pricing.effective_discount_pct(150.0, 0.0, 0.0) == 150.0


async def test_effective_discount_facade_equals_manual_max(tmp_db):
    """Асинхронный фасад ходит ровно в те же два запроса, что и снятые копии."""
    d = tmp_db
    await d.add_discount(10, 7.0)          # тираж от 10 шт → 7 %
    await d.set_user_discount(4242, 12.0, "постоянный")
    for uid, qty, promo in ((4242, 1, None), (4242, 10, None), (4242, 10, 25.0),
                            (777, 10, None), (777, 1, 3.0), (777, 1, None)):
        manual = max(await d.best_discount(qty), await d.get_user_discount(uid), promo or 0.0)
        assert await calcservice.effective_discount(uid, qty, promo) == manual


async def test_effective_discount_same_money_on_every_surface(tmp_db):
    """Одинаковые входы — одинаковые копейки: сведённая скидка даёт тот же итог, что и
    выписанная руками формула (проверяем через цену, а не через сам процент)."""
    d = tmp_db
    await d.add_discount(5, 8.0)
    await d.set_user_discount(4242, 12.0, "постоянный")
    settings = await d.get_settings()
    for qty, promo in ((1, None), (5, None), (5, 30.0), (10, 4.0)):
        manual = max(await d.best_discount(qty), await d.get_user_discount(4242), promo or 0.0)
        shared = await calcservice.effective_discount(4242, qty, promo)
        a = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=qty, discount_pct=manual)
        b = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=qty, discount_pct=shared)
        assert round(a.total, 2) == round(b.total, 2)
        assert round(a.discount_amount, 6) == round(b.discount_amount, 6)


# ───────────────────────────── 2. округление до 10 копеек ─────────────────────────────
@pytest.mark.parametrize(("value", "expected"), [
    (0.0, 0.0), (0.001, 0.0), (0.04, 0.1), (0.05, 0.1), (0.1, 0.1), (0.11, 0.2),
    (1.234, 1.3), (12.3, 12.3), (12.31, 12.4), (99.999, 100.0), (-0.03, 0.0),
])
def test_ceil10_table(value, expected):
    """Значения сняты с формулы `-(-k // 10) * 10`, которая жила тремя копиями в pricing.py."""
    assert pricing.ceil10(value) == expected


def test_ceil10_matches_inline_formula():
    """Прогон по сетке: вынесенная функция обязана совпасть с исходной формулой посимвольно."""
    for i in range(0, 20000, 7):
        v = i / 100.0 + 0.004
        k = int(round(v * 100))
        assert pricing.ceil10(v) == (-(-k // 10) * 10) / 100.0


# ───────────────── 3. самопечать бота: цена мимо коэффициента сложности ─────────────────
async def test_self_print_price_uses_price_factor(tmp_db):
    """КРАСНЫЙ на старом коде. `_register_self_print` считал по `plastic.price_per_kg`, то есть
    без `price_factor`, — «было бы потрачено» занижалось на сложных материалах. Все веб-пути
    считают по `price_per_gram * 1000`."""
    d = tmp_db
    pid = await d.add_plastic("Тяжёлый нейлон", 1000.0, 1.14)
    await d.update_plastic(pid, price_factor=1.5)
    plastic = await d.get_plastic(pid)
    assert plastic.price_factor == 1.5

    settings = await d.get_settings()
    # эталон — формула ВЕБ-путей (app/webapp.py: _mw_snapshot, заказ за клиента и т. д.)
    web_bd = pricing.compute_price_manual(
        40.0, 180.0, plastic.price_per_gram * 1000.0, settings, qty=1,
        failure_rate_pct=plastic.failure_rate_pct,
        volume_cm3_unit=(40.0 / plastic.density))
    # и заведомо ДРУГАЯ цена по «сырому» прайсу без коэффициента
    raw_bd = pricing.compute_price_manual(
        40.0, 180.0, plastic.price_per_kg, settings, qty=1,
        failure_rate_pct=plastic.failure_rate_pct,
        volume_cm3_unit=(40.0 / plastic.density))
    assert round(web_bd.total, 2) != round(raw_bd.total, 2)   # коэффициент реально влияет на цену

    await user_flow._register_self_print(
        9001, 0, {"grams": 40.0, "minutes": 180.0, "plastic_id": pid}, "тестовая печать")
    spent = await d.admin_would_spend(9001)
    assert spent == round(web_bd.total, 2)


async def test_self_print_price_unchanged_without_price_factor(tmp_db):
    """Границы правки: у материала без коэффициента (1.0) цена самопечати не меняется вовсе."""
    d = tmp_db
    pid = await d.add_plastic("Обычный PLA", 1000.0, 1.24)
    plastic = await d.get_plastic(pid)
    assert plastic.price_factor == 1.0
    settings = await d.get_settings()
    expect = pricing.compute_price_manual(
        40.0, 180.0, plastic.price_per_kg, settings, qty=1,
        failure_rate_pct=plastic.failure_rate_pct, volume_cm3_unit=(40.0 / plastic.density))
    await user_flow._register_self_print(
        9002, 0, {"grams": 40.0, "minutes": 180.0, "plastic_id": pid}, "обычная печать")
    assert await d.admin_would_spend(9002) == round(expect.total, 2)


# ───────────────── 4. плата за разработку модели в PDF-смете ─────────────────
def _bd_for_quote():
    return pricing.compute_price(30.0, 180.0, 1, PLASTIC, SETTINGS, discount_pct=10.0)


def test_quote_total_adds_dev_fee_and_delivery():
    """ИТОГО сметы = печать + разработка + доставка. Одна формула на строку ИТОГО и на штамп
    в футере (раньше `bd.total + _dc` был выписан в документе дважды)."""
    bd = _bd_for_quote()
    assert quote.quote_total(bd) == round(bd.total, 2)
    assert quote.quote_total(bd, dev_fee=7.5) == round(bd.total + 7.5, 2)
    assert quote.quote_total(bd, delivery_cost=6.0) == round(bd.total + 6.0, 2)
    assert quote.quote_total(bd, dev_fee=7.5, delivery_cost=6.0) == round(bd.total + 13.5, 2)
    # None/пусто ведут себя как ноль (в заказе dev_fee бывает не заполнен)
    assert quote.quote_total(bd, dev_fee=None, delivery_cost=None) == round(bd.total, 2)


def test_build_quote_renders_with_dev_fee(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUOTE_DIR", tmp_path)
    bd = _bd_for_quote()
    kw = dict(business_name="X", business_contact="c", model_label="M", printer_name="P",
              plastic_name="PLA", qty=1, quality_label="q", strength_label="s", bd=bd,
              cur="руб.", settings=SETTINGS, preview_path=None)
    plain = quote.build_quote(**kw)
    with_dev = quote.build_quote(dev_fee=12.0, **kw)
    assert plain.exists() and with_dev.exists()
    assert with_dev.stat().st_size != plain.stat().st_size   # добавилась строка и вырос ИТОГО


async def test_order_pdf_total_matches_order_price(tmp_db, monkeypatch, tmp_path):
    """КРАСНЫЙ на старом коде. Индивидуальный заказ: цена = печать + разработка. В PDF уезжала
    только печать — клиент видел в приложении одну сумму, а в присланной смете меньшую."""
    d = tmp_db
    uid = 5150
    settings = await d.get_settings()
    bd = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=1)
    dev_fee = 7.5
    total = round(bd.total + dev_fee, 2)
    rid = await d.add_request(
        user_id=uid, user_name="U", model_label="Индивидуальная штука", printer="P",
        plastic_name="Test PLA", quantity=1, quality="q", strength="s",
        grams_total=bd.grams_total, minutes_total=bd.minutes_total, price=total,
        preview_path=None, is_custom=1)
    await d.update_request(rid, breakdown=pricing.breakdown_to_dict(bd, settings), dev_fee=dev_fee)
    r = await d.get_request(rid)
    assert r.dev_fee == dev_fee

    # то, что видит клиент в приложении — единственный эталон суммы
    app_total = texts.order_breakdown(r, "руб.")["total"]
    assert app_total == texts.fmt_money(total, "руб.")

    seen: dict = {}

    def _fake_build(**kwargs):
        seen.update(kwargs)
        p = tmp_path / "q.pdf"
        p.write_bytes(b"%PDF-1.4")
        return p

    class _Bot:
        async def send_document(self, *a, **k):
            return None

    stub_auth(monkeypatch, _verify_init_data=uid)
    stub_bot(monkeypatch, _Bot())
    monkeypatch.setattr(quote, "build_quote", _fake_build)

    class _Req:
        async def json(self):
            return {"initData": "x", "id": rid}

    resp = await webapp._api_order_pdf(_Req())
    assert resp.status == 200
    assert seen, "build_quote не был вызван"
    pdf_total = quote.quote_total(seen["bd"], dev_fee=seen.get("dev_fee", 0.0),
                                  delivery_cost=seen.get("delivery_cost", 0.0))
    assert pdf_total == total                     # ИТОГО сметы = цена заказа
    assert texts.fmt_money(pdf_total, "руб.") == app_total


async def test_order_pdf_without_dev_fee_unchanged(tmp_db, monkeypatch, tmp_path):
    """Границы: у заказа без платы за разработку ИТОГО сметы прежнее (bd.total)."""
    d = tmp_db
    uid = 5151
    settings = await d.get_settings()
    bd = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=1)
    rid = await d.add_request(
        user_id=uid, user_name="U", model_label="Обычная штука", printer="P",
        plastic_name="Test PLA", quantity=1, quality="q", strength="s",
        grams_total=bd.grams_total, minutes_total=bd.minutes_total, price=bd.total,
        preview_path=None)
    await d.update_request(rid, breakdown=pricing.breakdown_to_dict(bd, settings))

    seen: dict = {}

    def _fake_build(**kwargs):
        seen.update(kwargs)
        p = tmp_path / "q2.pdf"
        p.write_bytes(b"%PDF-1.4")
        return p

    class _Bot:
        async def send_document(self, *a, **k):
            return None

    stub_auth(monkeypatch, _verify_init_data=uid)
    stub_bot(monkeypatch, _Bot())
    monkeypatch.setattr(quote, "build_quote", _fake_build)

    class _Req:
        async def json(self):
            return {"initData": "x", "id": rid}

    await webapp._api_order_pdf(_Req())
    assert float(seen.get("dev_fee") or 0.0) == 0.0
    assert quote.quote_total(seen["bd"], dev_fee=seen.get("dev_fee", 0.0)) == round(bd.total, 2)


# ───────────────── границы: два места, которые дублями НЕ являются ─────────────────
async def test_snapshot_discount_wins_over_current_rates(tmp_db):
    """`calcservice._request_discount` — НЕ копия правила: у заказа со снимком разбивки
    авторитетен снимок (условия сделки заморожены), даже если сегодняшние ставки выше.
    Сведение общей функции не имеет права это поведение тронуть."""
    d = tmp_db
    await d.add_discount(1, 50.0)                  # сегодня тираж даёт 50 %
    await d.set_user_discount(4243, 40.0, "vip")
    r = SimpleNamespace(breakdown={"discount_pct": 5.0}, user_id=4243, quantity=1)
    assert await calcservice._request_discount(r, 1, 4243) == 5.0      # снимок, а не 50
    r_nosnap = SimpleNamespace(breakdown={}, user_id=4243, quantity=1)
    assert await calcservice._request_discount(r_nosnap, 1, 4243) == 50.0


async def test_quick_paths_without_promo_stay_promo_free(tmp_db):
    """Быстрый расчёт по ссылке в чате и заказ за клиента от мастера промокода НЕ знают
    (он живёт только в памяти токена расчёта мини-аппы) — у них скидка честно из двух
    источников. Тест фиксирует, что «недостающий третий аргумент» там не баг."""
    d = tmp_db
    await d.add_discount(1, 6.0)
    await d.set_user_discount(4244, 9.0, "клиент")
    await d.add_promo("SALE50", 50.0, 0, 0)
    two = pricing.effective_discount_pct(await d.best_discount(1), await d.get_user_discount(4244))
    assert two == 9.0                                    # промокод в этих путях не участвует
    assert await d.validate_promo("SALE50") == 50.0      # но сам код в базе живой
