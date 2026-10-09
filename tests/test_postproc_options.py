"""Опции постобработки (B4): каталог платных ручных услуг мастера (шлифовка, покраска, вставки),
которые клиент добавляет к заказу плоской ценой ПОВЕРХ печати.

Проверяем: CRUD каталога и active_only; apply_options прибавляет плоскую сумму к итогу после
скидки/срочности и держит округление до 10 копеек; поля options/options_cost переживают снимок
разбивки заказа (JSON round-trip); breakdown_rows даёт по строке на услугу; снимок в
request_options данные-сейф (удаление услуги из каталога НЕ трогает историю заказа); эндпоинт
_api_admin_option (add/del) под замоканной админ-проверкой.
"""
import json
from types import SimpleNamespace

from conftest import stub_auth

from app import calcservice, config, pricing, quote, texts, webapp

CUR = "руб."

SETTINGS = {
    "machine_hour_rate": 2.0, "setup_fee": 5.0, "plate_fee": 0.0,
    "failure_rate_pct": 8.0, "profit_margin_pct": 50.0, "min_price": 5.0,
    "multicolor_waste_pct": 12.0, "multicolor_time_pct": 15.0,
    "volume_threshold_cm3": 0.0, "volume_surcharge_pct": 0.0,
    "rush_surcharge_pct": 50.0, "term_split_hours": 12,
}

ADMIN_ID = 777


def _plastic():
    return SimpleNamespace(density=1.24, price_per_gram=0.05, failure_rate_pct=None)


def _is_round10(v: float) -> bool:
    """Итог всегда кратен 10 копейкам (скрытое округление вверх)."""
    return int(round(v * 100)) % 10 == 0


def _ceil10(rub: float) -> float:
    """Клиентская формула B4-инстант (app.js ceil10): вверх до 10 копеек через целые копейки."""
    k = int(round(rub * 100))
    return (-(-k // 10) * 10) / 100.0


# ─────────────────────────────── каталог CRUD ───────────────────────────────
async def test_options_crud(tmp_db):
    db = tmp_db
    assert await db.list_postproc_options() == []
    oid = await db.add_postproc_option("Шлифовка", 5.0, sort=1)
    await db.add_postproc_option("Покраска", 12.0, sort=0)
    all_opts = await db.list_postproc_options()
    assert [o.name for o in all_opts] == ["Покраска", "Шлифовка"]   # порядок по sort
    # деактивация исключает из active_only, но не из полного списка
    await db.update_postproc_option(oid, is_active=0, price=6.5)
    active = await db.list_postproc_options(active_only=True)
    assert [o.name for o in active] == ["Покраска"]
    off = [o for o in await db.list_postproc_options() if o.id == oid][0]
    assert off.price == 6.5 and off.is_active == 0
    await db.delete_postproc_option(oid)
    assert [o.id for o in await db.list_postproc_options()] != [oid]
    assert len(await db.list_postproc_options()) == 1


# ─────────────────────────────── математика цены ───────────────────────────────
def test_apply_options_math():
    base = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    assert base.options == [] and base.options_cost == 0.0
    opts = [{"id": 1, "name": "Шлифовка", "price": 5.0}, {"id": 2, "name": "Покраска", "price": 3.0}]
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(bd, opts)
    assert bd.options_cost == 8.0
    assert [o["name"] for o in bd.options] == ["Шлифовка", "Покраска"]
    # круглые цены (кратны 0.1) → итог вырос ровно на сумму опций, округление осталось
    assert abs(bd.total - (base.total + 8.0)) < 1e-9
    assert _is_round10(bd.total)


def test_apply_options_rounding_and_empty():
    base = pricing.compute_price(30.0, 180.0, 1, _plastic(), SETTINGS)
    # некруглая цена услуги — итог всё равно доводится вверх до 10 копеек
    bd = pricing.compute_price(30.0, 180.0, 1, _plastic(), SETTINGS)
    pricing.apply_options(bd, [{"id": 1, "name": "Гравировка", "price": 5.05}])
    assert bd.options_cost == 5.05
    assert _is_round10(bd.total)
    # опция добавляется к НЕокруглённому итогу печати и округляется вверх ОДИН раз (не двойное
    # округление, которое переплачивало бы до 10 коп.): итог = ceil10(итог_до_округления + 5.05)
    expected = (-(-int(round((base.total_unrounded + 5.05) * 100)) // 10) * 10) / 100.0
    assert abs(bd.total - expected) < 1e-9
    assert bd.total > base.total   # опция подняла итог
    # цена 0 (бесплатная услуга) хранится в снимке, но в сумму не идёт и итог не двигает
    bd0 = pricing.compute_price(30.0, 180.0, 1, _plastic(), SETTINGS)
    pricing.apply_options(bd0, [{"id": 9, "name": "Маркировка", "price": 0.0}])
    assert bd0.options_cost == 0.0
    assert abs(bd0.total - base.total) < 1e-9
    assert len(bd0.options) == 1
    # None/[] — bd без изменений
    bd_none = pricing.compute_price(30.0, 180.0, 1, _plastic(), SETTINGS)
    pricing.apply_options(bd_none, None)
    assert bd_none.options == [] and bd_none.options_cost == 0.0


# ─────────────────────────────── услуги «за деталь» (accee7a9) ───────────────────────────────
def test_apply_options_per_part_math():
    """Ставка × детали × тираж (кейс «склейка 20 коп/деталь»): price в снимке становится готовой
    суммой (данные-сейф для request_options/PDF), rate/parts остаются для пере-применения."""
    base = pricing.compute_price(30.0, 180.0, 3, _plastic(), SETTINGS)
    bd = pricing.compute_price(30.0, 180.0, 3, _plastic(), SETTINGS)     # qty=3
    opts = [{"id": 1, "name": "Склейка", "price": 0.2, "per_part": 1, "rate": 0.2}]
    pricing.apply_options(bd, opts, n_parts=50)
    assert bd.options[0]["price"] == 30.0 and bd.options[0]["parts"] == 50   # 0.2 × 50 × 3
    assert bd.options_cost == 30.0
    assert abs(bd.total - (base.total + 30.0)) < 1e-9
    assert _is_round10(bd.total)


def test_apply_options_per_part_reapply_new_qty():
    """Пере-применение снимка (requote/оформление с новым тиражом) пересчитывает «за-детальную»
    цену по сохранённому parts — даже когда свежего замера нет (n_parts=None). Плоские услуги
    при этом НЕ масштабируются."""
    bd = pricing.compute_price(30.0, 180.0, 1, _plastic(), SETTINGS)
    opts = [{"id": 1, "name": "Склейка", "price": 0.2, "per_part": 1, "rate": 0.2},
            {"id": 2, "name": "Покраска", "price": 12.0}]
    pricing.apply_options(bd, opts, n_parts=10)
    assert bd.options[0]["price"] == 2.0                                  # 0.2 × 10 × 1
    bd2 = pricing.compute_price(30.0, 180.0, 4, _plastic(), SETTINGS)
    snapshot = json.loads(json.dumps(bd.options))   # как из breakdown-JSON заказа
    pricing.apply_options(bd2, snapshot, None)
    assert bd2.options[0]["price"] == 8.0                                 # 0.2 × 10 × 4
    assert bd2.options[1]["price"] == 12.0                                # плоская — как была
    assert bd2.options_cost == 20.0


def test_apply_options_per_part_unknown_parts():
    """Деталей никто не мерил (кастомный/ручной путь): честный минимум — 1 деталь; ставка
    берётся из price каталога, когда rate ещё не проставлен."""
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(bd, [{"id": 1, "name": "Склейка", "price": 0.5, "per_part": 1}])
    o = bd.options[0]
    assert o["price"] == 1.0 and o["rate"] == 0.5 and o["parts"] == 1     # 0.5 × 1 × 2


def test_apply_options_per_part_idempotent():
    """Идемпотентность B4-инстант сохраняется и для «за-детальных»: повторное применение того же
    набора не удваивает доплату (price в снимке уже эффективная — пересчёт идёт от rate)."""
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    opts = [{"id": 1, "name": "Склейка", "price": 0.2, "per_part": 1, "rate": 0.2}]
    pricing.apply_options(bd, opts, n_parts=6)
    t1, c1 = bd.total, bd.options_cost
    pricing.apply_options(bd, bd.options, n_parts=6)
    assert abs(bd.total - t1) < 1e-9 and abs(bd.options_cost - c1) < 1e-9
    assert bd.options[0]["price"] == 2.4                                  # 0.2 × 6 × 2, не удвоилось


def test_breakdown_rows_per_part_sub():
    """Строка расчётника «за-детальной» услуги показывает множители: ставка × полное число
    деталей (на копию × тираж) — клиенту видно, откуда сумма склейки."""
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(bd, [{"id": 1, "name": "Склейка", "price": 0.2, "per_part": 1,
                                "rate": 0.2}], n_parts=6)
    row = [r for r in texts.breakdown_rows(bd, SETTINGS, CUR) if r["label"] == "Склейка"][0]
    assert "0,20 × 12 дет." in row["sub"]                                 # 6 дет./копию × 2 шт
    assert row["value"].startswith("+")


async def test_options_crud_per_part(tmp_db):
    """CRUD флага per_part: заводится, читается, переключается."""
    db = tmp_db
    oid = await db.add_postproc_option("Склейка деталей", 0.2, per_part=1)
    o = [x for x in await db.list_postproc_options() if x.id == oid][0]
    assert o.per_part == 1 and o.price == 0.2
    await db.update_postproc_option(oid, per_part=0)
    o = [x for x in await db.list_postproc_options() if x.id == oid][0]
    assert o.per_part == 0


def test_apply_options_after_rush():
    """Композиция со срочностью (B3×B4): rush×base считается ВНУТРИ compute_price (наценка
    срочности), и только ПОТОМ apply_options кладёт плоскую сумму услуг сверху — округление одно,
    в самом конце. Наценка срочности (как и маржа) на опции НЕ распространяется: услуга продаётся
    по розничной цене каталога, а не ×множитель заказа."""
    rush = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    assert rush.urgency_factor == 1.5 and rush.urgency_surcharge > 0
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    pricing.apply_options(bd, [{"id": 1, "name": "Шлифовка", "price": 5.0},
                               {"id": 2, "name": "Покраска", "price": 3.0}])
    # итог = срочный итог + плоская сумма опций (8), опции НЕ умножены на ×1.5
    assert bd.options_cost == 8.0
    assert abs(bd.total - (rush.total + 8.0)) < 1e-9
    assert _is_round10(bd.total)
    # строка наценки срочности не изменилась от добавления опций (нет двойного применения)
    assert abs(bd.urgency_surcharge - rush.urgency_surcharge) < 1e-9
    # прирост итога равен ровно сумме цен услуг (никакой маржи/наценки поверх)
    assert abs((bd.total - rush.total) - sum(o["price"] for o in bd.options)) < 1e-9


def test_apply_options_after_discount():
    """Композиция со скидкой (B1×B4): скидка режет ТОЛЬКО печать (внутри compute_price), опции
    кладутся ПЛОСКО поверх уже сниженного итога — на физическую услугу скидка тиража не
    распространяется. Прирост итога = ровно сумма цен услуг, discount_amount от опций не меняется."""
    disc = pricing.compute_price(30.0, 180.0, 5, _plastic(), SETTINGS, discount_pct=20.0)
    assert disc.discount_pct == 20.0 and disc.discount_amount > 0
    bd = pricing.compute_price(30.0, 180.0, 5, _plastic(), SETTINGS, discount_pct=20.0)
    pricing.apply_options(bd, [{"id": 1, "name": "Шлифовка", "price": 5.0},
                               {"id": 2, "name": "Покраска", "price": 3.0}])
    assert bd.options_cost == 8.0
    # опции добавлены ПОСЛЕ скидки: итог = сниженный итог + плоская сумма (никакой скидки на услугу)
    assert abs(bd.total - (disc.total + 8.0)) < 1e-9
    assert _is_round10(bd.total)
    # сумма скидки не сдвинулась от добавления опций (скидка не пересчиталась на услуги)
    assert abs(bd.discount_amount - disc.discount_amount) < 1e-9
    # прирост = ровно розничные цены услуг, без ×(1-скидка)
    assert abs((bd.total - disc.total) - sum(o["price"] for o in bd.options)) < 1e-9


def test_apply_options_idempotent_reapply():
    """B4-инстант: услуги переключаются ЛОКАЛЬНО на смете (без переслайса), а /api/order
    переприкладывает актуальный набор к уже посчитанной bd — apply_options ОБЯЗАНА быть
    идемпотентной (снять прошлый набор, положить новый), иначе повтор удвоил бы доплату."""
    setA = [{"id": 1, "name": "Шлифовка", "price": 5.0}, {"id": 2, "name": "Покраска", "price": 3.0}]
    setB = [{"id": 3, "name": "Латунные вставки", "price": 7.0}]
    # повтор ТОГО ЖЕ набора не должен удвоить доплату
    once = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(once, setA)
    twice = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(twice, setA)
    pricing.apply_options(twice, setA)
    assert twice.options_cost == 8.0
    assert abs(twice.total - once.total) < 1e-9
    assert abs(twice.machine_cost - once.machine_cost) < 1e-9
    assert abs(twice.total_unrounded - once.total_unrounded) < 1e-9
    # смена набора setA→setB эквивалентна свежему применению setB на чистой bd
    switched = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(switched, setA)
    pricing.apply_options(switched, setB)
    freshB = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(freshB, setB)
    assert switched.options == setB and switched.options_cost == 7.0
    assert abs(switched.total - freshB.total) < 1e-9
    assert abs(switched.machine_cost - freshB.machine_cost) < 1e-9
    # сброс к пустому набору = чистая печать (как будто опций не было)
    cleared = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    plain = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(cleared, setA)
    pricing.apply_options(cleared, [])
    assert cleared.options == [] and cleared.options_cost == 0.0
    assert abs(cleared.total - plain.total) < 1e-9
    assert abs(cleared.machine_cost - plain.machine_cost) < 1e-9
    assert abs(cleared.total_unrounded - plain.total_unrounded) < 1e-9


def test_result_payload_exposes_base_without_options():
    """B4-инстант: _result_payload отдаёт голую базу печати БЕЗ услуг (total_unrounded), по которой
    клиент считает итог локально при тапе по услуге: ceil10(база + Σ услуг) == серверный bd.total
    (то же округление до 10 копеек). Число одно и то же независимо от выбора на момент calc."""
    base = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    # без опций: база = итог печати до округления, ceil10(база) = показанный итог
    p0 = webapp._result_payload(base, SETTINGS, CUR, 1, False)
    assert abs(p0["total_unrounded"] - round(base.total_unrounded, 2)) < 1e-9
    assert _ceil10(p0["total_unrounded"]) == base.total
    # с опциями total_unrounded остаётся ГОЛОЙ базой (услуги в неё НЕ входят)
    opts = [{"id": 1, "name": "Шлифовка", "price": 5.0}, {"id": 2, "name": "Покраска", "price": 3.0}]
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(bd, opts)
    p = webapp._result_payload(bd, SETTINGS, CUR, 1, False)
    assert abs(p["total_unrounded"] - round(base.total_unrounded, 2)) < 1e-9
    # клиентская формула на голой базе + сумма услуг сходится к серверному итогу до копейки
    assert _ceil10(p["total_unrounded"] + 8.0) == bd.total
    # и со срочностью база остаётся без услуг, формула по-прежнему сходится
    rush = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    bdr = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS, urgency_factor=1.5)
    pricing.apply_options(bdr, opts)
    pr = webapp._result_payload(bdr, SETTINGS, CUR, 1, False)
    assert abs(pr["total_unrounded"] - round(rush.total_unrounded, 2)) < 1e-9
    assert _ceil10(pr["total_unrounded"] + 8.0) == bdr.total


# ─────────────────────────────── снимок разбивки ───────────────────────────────
def test_breakdown_snapshot_roundtrip():
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(bd, [{"id": 1, "name": "Шлифовка", "price": 5.0}])
    d = pricing.breakdown_to_dict(bd, SETTINGS)
    # проходит через JSON (как в add_request)
    d2 = json.loads(json.dumps(d))
    back = pricing.breakdown_from_dict(d2)
    assert back is not None
    assert back.options_cost == 5.0
    assert back.options == [{"id": 1, "name": "Шлифовка", "price": 5.0}]
    assert abs(back.total - bd.total) < 1e-9
    # старая запись без полей опций грузится с дефолтами (обратная совместимость истории)
    legacy = pricing.breakdown_from_dict({"qty": 1, "grams_unit": 1, "minutes_unit": 1,
        "grams_total": 1, "minutes_total": 1, "filament_cost": 1, "machine_cost": 1,
        "waste_cost": 0, "setup_fee": 1, "margin": 1, "discount_pct": 0, "discount_amount": 0,
        "subtotal": 1, "total": 5, "min_applied": False})
    assert legacy is not None
    assert legacy.options == [] and legacy.options_cost == 0.0


# ─────────────────────────────── строки расчётника ───────────────────────────────
def test_breakdown_rows_includes_options():
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(bd, [{"id": 1, "name": "Шлифовка", "price": 5.0},
                               {"id": 2, "name": "Латунные вставки", "price": 3.0},
                               {"id": 3, "name": "Маркировка", "price": 0.0}])
    rows = texts.breakdown_rows(bd, SETTINGS, CUR)
    opt_rows = [r for r in rows if r.get("sub") == "постобработка"]
    # две платные услуги → две строки; бесплатная (0) в расчётник не попадает
    assert [r["label"] for r in opt_rows] == ["Шлифовка", "Латунные вставки"]
    assert opt_rows[0]["value"] == "+" + texts.fmt_money(5.0, CUR)
    # PDF-смета строит строки вручную — тот же результат
    assert any("Шлифовка" in r["label"] for r in opt_rows)


# ─────────────────────────────── связь заказ↔опции (данные-сейф) ───────────────────────────────
async def test_request_options_linkage(tmp_db):
    db = tmp_db
    oid = await db.add_postproc_option("Покраска", 12.0)
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="M", printer="P", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=10.0, minutes_total=60.0,
        price=20.0, preview_path=None)
    await db.set_request_options(rid, [{"id": oid, "name": "Покраска", "price": 12.0}])
    got = await db.get_request_options(rid)
    assert got == [{"id": oid, "name": "Покраска", "price": 12.0}]
    # удаляем услугу из КАТАЛОГА — снимок заказа не должен измениться (историю не переписываем)
    await db.delete_postproc_option(oid)
    still = await db.get_request_options(rid)
    assert still == [{"id": oid, "name": "Покраска", "price": 12.0}]
    # перезапись набора идемпотентна
    await db.set_request_options(rid, [])
    assert await db.get_request_options(rid) == []


async def test_catalog_price_edit_does_not_rewrite_history(tmp_db):
    """КЛЮЧЕВОЙ тест данных-сейфа: цена услуги СНИМАЕТСЯ в момент заказа (в requests.breakdown и в
    request_options). Поздняя правка цены в КАТАЛОГЕ НЕ должна переписать ни итог исторического
    заказа, ни его снимок опций — иначе повторился бы класс бага account-merge, тихо менявшего
    старые суммы."""
    db = tmp_db
    oid = await db.add_postproc_option("Покраска", 12.0)
    # оформляем заказ: опция уходит и в снимок разбивки, и в таблицу связи
    bd = pricing.compute_price(30.0, 180.0, 1, _plastic(), SETTINGS)
    snap = [{"id": oid, "name": "Покраска", "price": 12.0}]
    pricing.apply_options(bd, snap)
    order_total = bd.total
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="M", printer="P", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=10.0, minutes_total=60.0,
        price=order_total, preview_path=None, breakdown=pricing.breakdown_to_dict(bd, SETTINGS))
    await db.set_request_options(rid, snap)
    # мастер поднимает цену услуги в каталоге ВОСЕМЬ раз уже ПОСЛЕ заказа
    await db.update_postproc_option(oid, price=99.0)
    # ─ снимок в request_options не сдвинулся
    assert await db.get_request_options(rid) == snap
    # ─ снимок в requests.breakdown не сдвинулся: итог и опции те же, что при оформлении
    r = await db.get_request(rid)
    back = pricing.breakdown_from_dict(r.breakdown)
    assert back is not None
    assert abs(back.total - order_total) < 1e-9
    assert back.options == snap
    assert back.options_cost == 12.0
    # активный каталог, конечно, уже показывает новую цену — расходится со снимком заказа намеренно
    live = [o for o in await db.list_postproc_options() if o.id == oid][0]
    assert live.price == 99.0


# ─────────────────────── пересчёт заказа сохраняет опции (B4 на recompute-путях) ───────────────────────
async def _seed_order_with_options(db, snap, *, qty=2, plastic_name="Test PLA · чёрный"):
    """Заказ, у которого в снимке разбивки лежат опции постобработки (как при оформлении)."""
    await db.add_plastic("Test PLA", price_per_kg=40.0, density=1.24, material_type="PLA")
    bd = pricing.compute_price(30.0, 180.0, qty, _plastic(), SETTINGS)
    pricing.apply_options(bd, snap)
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="M", printer="p", plastic_name=plastic_name,
        quantity=qty, quality="q", strength="s", grams_total=bd.grams_total,
        minutes_total=bd.minutes_total, price=bd.total, preview_path=None,
        breakdown=pricing.breakdown_to_dict(bd, SETTINGS))
    return rid, bd


async def test_reprice_request_preserves_options(tmp_db):
    """КРИТИЧНО: ручной пересчёт цены (reprice_request) должен re-apply опции из снимка заказа,
    иначе плоская доплата услуг молча пропадёт из stored price и разбивки, а тег/request_options
    останутся — итог перестанет сходиться со строками сметы + утечка выручки."""
    db = tmp_db
    snap = [{"id": 1, "name": "Покраска", "price": 12.0}, {"id": 2, "name": "Шлифовка", "price": 5.0}]
    rid, _ = await _seed_order_with_options(db, snap)
    r = await db.get_request(rid)
    res = await calcservice.reprice_request(r, 100.0, 60.0)
    assert res["bd"].options == snap
    assert res["bd"].options_cost == 17.0
    assert _is_round10(res["bd"].total)
    # тот же заказ БЕЗ снимка опций даёт итог ровно на сумму услуг меньше
    rid2, _ = await _seed_order_with_options(db, [])
    r2 = await db.get_request(rid2)
    res2 = await calcservice.reprice_request(r2, 100.0, 60.0)
    assert res2["bd"].options_cost == 0.0
    assert abs(res["bd"].total - (res2["bd"].total + 17.0)) < 1e-9


async def test_reprice_request_colors_preserves_options(tmp_db):
    """Пересчёт по цветам (многоцвет) тоже несёт опции из снимка заказа."""
    db = tmp_db
    snap = [{"id": 1, "name": "Покраска", "price": 12.0}]
    rid, _ = await _seed_order_with_options(db, snap)
    r = await db.get_request(rid)
    p = _plastic()
    res = await calcservice.reprice_request_colors(r, [(p, 20.0), (p, 15.0)], 120.0)
    assert res["bd"].options == snap
    assert res["bd"].options_cost == 12.0
    assert _is_round10(res["bd"].total)


# ─────────────────────────── обратная совместимость (заказы без опций) ───────────────────────────
def test_backward_compat_no_options(tmp_path, monkeypatch):
    """Заказ без постобработки рендерится на всех поверхностях без строк опций и с неизменным
    итогом; старый снимок разбивки БЕЗ полей options/options_cost не роняет breakdown_rows,
    order_breakdown и PDF-смету."""
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    base_total = bd.total
    # 1) breakdown_rows — ни одной строки постобработки, набор строк как раньше
    rows = texts.breakdown_rows(bd, SETTINGS, CUR)
    assert not [r for r in rows if r.get("sub") == "постобработка"]
    # 2) старый JSON вообще без ключей options/options_cost грузится и рендерится в смету
    legacy = {"qty": 2, "grams_unit": 15, "minutes_unit": 90, "grams_total": 30, "minutes_total": 180,
              "filament_cost": 1.5, "machine_cost": 6.0, "waste_cost": 0.6, "setup_fee": 5.0,
              "margin": 6.0, "discount_pct": 0, "discount_amount": 0, "subtotal": 19.1,
              "total": base_total, "min_applied": False}
    r = SimpleNamespace(breakdown=legacy, dev_fee=0.0)
    ob = texts.order_breakdown(r, CUR)
    assert ob is not None
    assert not [row for row in ob["rows"] if row.get("sub") == "постобработка"]
    assert ob["total"] == texts.fmt_money(base_total, CUR)
    # 3) PDF-смета из bd без опций строится без падения (crows-цикл по пустому options)
    monkeypatch.setattr(config, "QUOTE_DIR", tmp_path)
    pdf = quote.build_quote(
        business_name="X", business_contact="c", model_label="M", printer_name="P",
        plastic_name="PLA", qty=2, quality_label="q", strength_label="s",
        bd=bd, cur=CUR, settings=SETTINGS, preview_path=None)
    assert pdf.exists() and pdf.stat().st_size > 0


# ─────────────────────────── согласованность цены на поверхностях ───────────────────────────
def test_option_total_consistent_across_surfaces(tmp_path, monkeypatch):
    """Одна и та же сумма опций видна в живом результате мини-апки (_result_payload), в смете
    сохранённого заказа (order_breakdown) и в PDF-крауз — все три берут строки из одного bd.options,
    а итог уже включает options_cost. Если бы хоть одна поверхность считала опции сама, суммы бы
    разошлись."""
    opts = [{"id": 1, "name": "Шлифовка", "price": 5.0},
            {"id": 2, "name": "Покраска", "price": 3.0}]
    bd = pricing.compute_price(30.0, 180.0, 2, _plastic(), SETTINGS)
    pricing.apply_options(bd, opts)
    assert bd.options_cost == 8.0
    expected = {"Шлифовка": "+" + texts.fmt_money(5.0, CUR),
                "Покраска": "+" + texts.fmt_money(3.0, CUR)}

    # A) живой результат мини-апки — строки постобработки в lines[]
    payload = webapp._result_payload(bd, SETTINGS, CUR, 1, False)
    live = {}
    for label, val in payload["lines"]:
        if "(постобработка)" in label:
            live[label.split(" (")[0]] = val
    assert live == expected
    assert payload["total"] == texts.fmt_money(bd.total, CUR)

    # B) смета сохранённого заказа — те же строки из снимка breakdown
    r = SimpleNamespace(breakdown=pricing.breakdown_to_dict(bd, SETTINGS), dev_fee=0.0)
    ob = texts.order_breakdown(r, CUR)
    smeta = {row["label"]: row["value"] for row in ob["rows"] if row.get("sub") == "постобработка"}
    assert smeta == expected
    assert ob["total"] == texts.fmt_money(bd.total, CUR)

    # C) PDF-смета — crows строятся вручную из тех же bd.options; рендер без падения
    monkeypatch.setattr(config, "QUOTE_DIR", tmp_path)
    pdf = quote.build_quote(
        business_name="X", business_contact="c", model_label="M", printer_name="P",
        plastic_name="PLA", qty=2, quality_label="q", strength_label="s",
        bd=bd, cur=CUR, settings=SETTINGS, preview_path=None)
    assert pdf.exists() and pdf.stat().st_size > 0


# ─────────────────────────────── админ-эндпоинт ───────────────────────────────
class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def test_admin_option_endpoint(tmp_db, monkeypatch):
    db = tmp_db
    stub_auth(monkeypatch, _verify_admin=ADMIN_ID)

    async def _post(body):
        resp = await webapp._api_admin_option(_FakeRequest(body))
        return json.loads(resp.body.decode())

    out = await _post({"action": "add", "name": "Шлифовка", "price": 5.0})
    assert out["ok"] is True
    opts = await db.list_postproc_options()
    assert len(opts) == 1 and opts[0].name == "Шлифовка"
    # некорректные данные отбиваются
    bad = await _post({"action": "add", "name": "", "price": 1.0})
    assert bad["ok"] is False
    # список видит эндпоинт списка
    lst = json.loads((await webapp._api_admin_options(
        _FakeRequestQuery())).body.decode())
    assert lst["ok"] is True and len(lst["options"]) == 1
    # удаление
    out = await _post({"action": "del", "id": opts[0].id})
    assert out["ok"] is True
    assert await db.list_postproc_options() == []


class _FakeRequestQuery:
    query = {"initData": "x"}
