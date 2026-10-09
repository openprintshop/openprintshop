"""Срочность в клиентском потоке (B3): настройки/сроки + проброс в мгновенную смету и заказ.

tmp_db + прямой вызов серверных хелперов webapp (без реального aiohttp-сервера, как
test_admin_order_create): аналитический снимок цены _cache_snapshot применяет срочную наценку и
возвращает срок/срочность в payload, а _place_one фиксирует urgency + promised_date в заявке.
"""
import datetime

from app import db, webapp


async def _seed(db):
    pid = await db.add_plastic("Test PLA", 1000.0, 1.24)
    quals = await db.list_quality(active_only=True)
    strs = await db.list_strength(active_only=True)
    printer = await db.get_default_printer()
    plastic = await db.get_plastic(pid)
    return printer, plastic, quals[0], strs[0]


async def test_settings_defaults(tmp_db):
    s = await tmp_db.get_settings()
    assert s.get("rush_surcharge_pct") == 50.0
    # B3 (решение владельца): фикс-дни term_days_* убраны, порог быстро/долго — term_split_hours.
    assert s.get("term_split_hours") == 12.0
    assert "term_days_normal" not in db.DEFAULT_SETTINGS
    assert "term_days_rush" not in db.DEFAULT_SETTINGS


def _dd(days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).strftime("%d.%m")


def test_promised_date_rush_always_tomorrow():
    """«Срочно» → всегда завтра (+1), сколько бы ни печаталось: и короткий, и очень долгий заказ."""
    settings = {"term_split_hours": 12}
    assert db.promised_date(30.0, "rush", settings) == _dd(1)         # 0.5 ч
    assert db.promised_date(5000.0, "rush", settings) == _dd(1)       # ~83 ч — всё равно завтра


def test_promised_date_normal_split_by_print_time():
    """«Обычно» → печать короче 12 ч → завтра (+1); ровно 12 ч и дольше → послезавтра (+2)."""
    settings = {"term_split_hours": 12}
    assert db.promised_date(0.0, "normal", settings) == _dd(1)        # 0 ч
    assert db.promised_date(11 * 60.0, "normal", settings) == _dd(1)  # 11 ч < порог
    assert db.promised_date(12 * 60.0 - 1, "normal", settings) == _dd(1)  # чуть меньше порога
    assert db.promised_date(12 * 60.0, "normal", settings) == _dd(2)  # ровно порог → долгий
    assert db.promised_date(20 * 60.0, "normal", settings) == _dd(2)  # 20 ч ≥ порог
    # пустое/неизвестное значение срочности трактуем как «обычно»
    assert db.promised_date(11 * 60.0, "", settings) == _dd(1)
    assert db.promised_date(20 * 60.0, None, settings) == _dd(2)


def test_promised_date_threshold_setting_is_honoured():
    """Порог берётся из настройки term_split_hours, а не зашит: при пороге 6 ч заказ на 8 ч уже долгий."""
    assert db.promised_date(8 * 60.0, "normal", {"term_split_hours": 6}) == _dd(2)
    assert db.promised_date(8 * 60.0, "normal", {"term_split_hours": 24}) == _dd(1)
    # битое/пустое значение настройки → дефолт 12 ч
    assert db.promised_date(11 * 60.0, "normal", {"term_split_hours": ""}) == _dd(1)
    assert db.promised_date(13 * 60.0, "normal", {}) == _dd(2)


async def test_cache_snapshot_rush_costs_more_and_returns_term(tmp_db):
    db_ = tmp_db
    printer, plastic, quality, strength = await _seed(db_)
    cc = {"volume_cm3": 20.0, "minutes": 120.0, "plates": 1}

    m_norm = {"label": "Кубик", "urgency": "normal"}
    p_norm = await webapp._cache_snapshot(
        "tok1", m_norm, cc, uid=555, printer=printer, plastic=plastic, quality=quality,
        strength=strength, color="Чёрный", colors_sel=[], qty=2, extra_colors=0,
        urgency_factor=1.0)

    m_rush = {"label": "Кубик", "urgency": "rush"}
    p_rush = await webapp._cache_snapshot(
        "tok2", m_rush, cc, uid=555, printer=printer, plastic=plastic, quality=quality,
        strength=strength, color="Чёрный", colors_sel=[], qty=2, extra_colors=0,
        urgency_factor=1.5)

    # срочный дороже обычного (сравниваем сырые float из m["last"], не форматированные строки)
    assert m_rush["last"]["total"] > m_norm["last"]["total"]
    # payload несёт срочность и обещанный срок
    assert p_rush["urgency"] == "rush"
    assert p_norm["urgency"] == "normal"
    assert p_rush.get("term")
    assert p_norm.get("term")
    # печать 120 мин/шт × 2 = 240 мин = 4 ч < 12 ч → и срочный, и обычный обещаны на завтра (+1)
    assert p_rush["term"] == _dd(1)
    assert p_norm["term"] == _dd(1)
    # срочная наценка попала в снимок разбивки заказа
    assert m_rush["pdf"]["bd"].urgency_factor == 1.5
    assert m_rush["pdf"]["bd"].urgency_surcharge > 0.0


async def test_mw_snapshot_rush_costs_more_and_returns_term(tmp_db):
    """MW-путь цены (_mw_snapshot, отдельный от кэш-снимка) тоже применяет срочную наценку и
    возвращает срок/срочность — иначе срочный MW-заказ молча посчитался бы обычной ценой."""
    db_ = tmp_db
    printer, plastic, quality, strength = await _seed(db_)
    est = {"weight_g": 25.0, "minutes": 140.0, "colors_cnt": 1, "cover": "", "iid": ""}

    m_norm = {"label": "MW-модель", "urgency": "normal", "cpreview": "x.png"}
    p_norm = await webapp._mw_snapshot(
        "tokN", m_norm, est, uid=555, printer=printer, plastic=plastic, quality=quality,
        strength=strength, color="Чёрный", colors_sel=[], qty=2, plates=1, urgency_factor=1.0)

    m_rush = {"label": "MW-модель", "urgency": "rush", "cpreview": "x.png"}
    p_rush = await webapp._mw_snapshot(
        "tokR", m_rush, est, uid=555, printer=printer, plastic=plastic, quality=quality,
        strength=strength, color="Чёрный", colors_sel=[], qty=2, plates=1, urgency_factor=1.5)

    assert m_rush["last"]["total"] > m_norm["last"]["total"]
    assert p_rush["urgency"] == "rush"
    assert p_norm["urgency"] == "normal"
    # печать 140 мин/шт × 2 = 280 мин ≈ 4.7 ч < 12 ч → оба обещаны на завтра (+1)
    assert p_rush["term"] == _dd(1)
    assert p_norm["term"] == _dd(1)
    assert m_rush["pdf"]["bd"].urgency_factor == 1.5
    assert m_rush["pdf"]["bd"].urgency_surcharge > 0.0


async def test_order_persists_urgency(tmp_db):
    db_ = tmp_db
    printer, plastic, quality, strength = await _seed(db_)
    cc = {"volume_cm3": 20.0, "minutes": 120.0, "plates": 1}
    # готовим серверное состояние расчёта (m["last"]/m["pdf"]) срочным снимком, затем оформляем заказ
    m = {"label": "Кубик", "urgency": "rush"}
    await webapp._cache_snapshot(
        "tok", m, cc, uid=555, printer=printer, plastic=plastic, quality=quality,
        strength=strength, color="Чёрный", colors_sel=[], qty=1, extra_colors=0,
        urgency_factor=1.5)
    m["paths"] = ["fake_model.stl"]
    m["plates"] = 1
    m["cpreview"] = "fake_preview.png"

    u = {"id": 555, "username": "tester"}
    rid = await webapp._place_one(u, m)
    assert rid is not None

    r = await db_.get_request(rid)
    assert r.urgency == "rush"
    assert r.promised_date
    # срочный заказ обещан на завтра (+1) независимо от времени печати
    assert r.promised_date == _dd(1)
    # срочная наценка доехала и до JSON-снимка разбивки заказа
    assert float((r.breakdown or {}).get("urgency_factor", 1.0)) == 1.5
    assert float((r.breakdown or {}).get("urgency_surcharge", 0.0)) > 0.0


async def test_normal_order_stores_empty_urgency(tmp_db):
    """Обычный заказ хранит urgency='' — старое поведение без изменений (бейджа «срочно» нет)."""
    db_ = tmp_db
    printer, plastic, quality, strength = await _seed(db_)
    cc = {"volume_cm3": 20.0, "minutes": 120.0, "plates": 1}
    m = {"label": "Кубик", "urgency": "normal"}
    await webapp._cache_snapshot(
        "tok", m, cc, uid=555, printer=printer, plastic=plastic, quality=quality,
        strength=strength, color="Чёрный", colors_sel=[], qty=1, extra_colors=0,
        urgency_factor=1.0)
    m["paths"] = ["fake_model.stl"]
    m["plates"] = 1
    m["cpreview"] = "fake_preview.png"

    rid = await webapp._place_one({"id": 555, "username": "tester"}, m)
    r = await db_.get_request(rid)
    assert r.urgency == ""
    assert float((r.breakdown or {}).get("urgency_factor", 1.0)) == 1.0
    # печать 120 мин/шт × 1 = 2 ч < 12 ч → обычный заказ обещан на завтра (+1)
    assert r.promised_date == _dd(1)


async def test_long_normal_order_promised_day_after_tomorrow(tmp_db):
    """Обычный ДОЛГИЙ заказ (печать ≥ 12 ч) обещан на послезавтра (+2) — новое правило по времени."""
    db_ = tmp_db
    printer, plastic, quality, strength = await _seed(db_)
    # 800 мин/шт × 1 = 800 мин ≈ 13.3 ч ≥ порог 12 ч → долгий
    cc = {"volume_cm3": 120.0, "minutes": 800.0, "plates": 1}
    m = {"label": "Большая деталь", "urgency": "normal"}
    p = await webapp._cache_snapshot(
        "tokL", m, cc, uid=555, printer=printer, plastic=plastic, quality=quality,
        strength=strength, color="Чёрный", colors_sel=[], qty=1, extra_colors=0,
        urgency_factor=1.0)
    assert p["term"] == _dd(2)          # смета уже показывает послезавтра
    m["paths"] = ["fake_model.stl"]
    m["plates"] = 1
    m["cpreview"] = "fake_preview.png"
    rid = await webapp._place_one({"id": 555, "username": "tester"}, m)
    r = await db_.get_request(rid)
    assert r.urgency == ""
    assert r.promised_date == _dd(2)    # и зафиксированный в заявке срок — тоже


async def test_reprice_uses_urgency_column_when_breakdown_empty(tmp_db):
    """Ручной пересчёт мастером срочного заказа order_now со СБОЕМ слайса: снимка разбивки нет
    (breakdown пуст), но колонка urgency='rush'. Пересчёт должен сохранить срочную наценку —
    вывести множитель из колонки по настройкам, а не молча потерять +50% (edit-краевой случай)."""
    import types

    from app import calcservice
    db_ = tmp_db
    pid = await db_.add_plastic("Test PLA", 1000.0, 1.24)
    plastic = await db_.get_plastic(pid)
    r_rush = types.SimpleNamespace(
        plastic_name=plastic.name, quantity=1, breakdown={}, urgency="rush", plates=1, user_id=555)
    r_norm = types.SimpleNamespace(
        plastic_name=plastic.name, quantity=1, breakdown={}, urgency="", plates=1, user_id=555)
    res_rush = await calcservice.reprice_request(r_rush, grams_total=25.0, minutes_total=120.0)
    res_norm = await calcservice.reprice_request(r_norm, grams_total=25.0, minutes_total=120.0)
    assert res_rush["bd"].urgency_factor == 1.5   # наценка восстановлена из колонки, не из пустого снимка
    assert res_rush["bd"].urgency_surcharge > 0.0
    assert res_rush["bd"].total > res_norm["bd"].total
