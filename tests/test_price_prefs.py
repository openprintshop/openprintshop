"""Режим подтверждения цены клиентом (OpenTodo 0d11db78).

Три состояния: всегда подтверждать, всегда принимать, принимать подорожание до X%.
Решение принимает чистая функция db.price_auto_accept — её и проверяем, плюс хранение
и нормализацию значений, ради которой настройку не пускают в базу «как пришла».
"""
import pytest


async def test_prefs_default_is_ask(tmp_db):
    """Строки в user_prefs нет — молча считаем, что клиент хочет подтверждать сам."""
    db = tmp_db
    prefs = await db.get_user_prefs(777)
    # доставка по умолчанию (A2) живёт в тех же настройках и по умолчанию не задана
    assert prefs == {"price_mode": "ask", "price_pct": 0.0,
                     "delivery_type_id": 0, "delivery_address": ""}
    assert db.price_auto_accept(prefs, 10.0, 10.5) is False


async def test_prefs_roundtrip(tmp_db):
    db = tmp_db
    await db.set_user_price_prefs(1, "pct", 15)
    got = await db.get_user_prefs(1)
    assert (got["price_mode"], got["price_pct"]) == ("pct", 15.0)
    # повторное сохранение обновляет ту же строку, а не заводит вторую
    await db.set_user_price_prefs(1, "auto", 0)
    assert (await db.get_user_prefs(1))["price_mode"] == "auto"


@pytest.mark.parametrize(("mode", "pct"), [("нечто", 5), ("", 5)])
async def test_unknown_mode_falls_back_to_ask(tmp_db, mode, pct):
    """Настройка про деньги: чего не знаем — то подтверждаем вручную, а не принимаем."""
    db = tmp_db
    await db.set_user_price_prefs(2, mode, pct)
    assert (await db.get_user_prefs(2))["price_mode"] == "ask"


async def test_pct_is_clamped(tmp_db):
    db = tmp_db
    assert (await db.set_user_price_prefs(3, "pct", 5000))["price_pct"] == db.PRICE_PCT_MAX
    assert (await db.set_user_price_prefs(3, "pct", -20))["price_mode"] == "ask"   # 0% = спрашивать


def test_auto_accepts_any_growth():
    assert db_accept({"price_mode": "auto"}, 10.0, 1000.0) is True


def test_pct_threshold_boundaries():
    prefs = {"price_mode": "pct", "price_pct": 10}
    assert db_accept(prefs, 100.0, 109.0) is True     # +9%
    assert db_accept(prefs, 100.0, 110.0) is True     # ровно порог — принимаем
    assert db_accept(prefs, 100.0, 110.01) is False   # чуть выше — спрашиваем


def test_pct_needs_previous_price():
    """Заказ без сметы: прежней цены нет, «+15% от нуля» не существует — спрашиваем всегда."""
    assert db_accept({"price_mode": "pct", "price_pct": 50}, 0.0, 12.0) is False
    assert db_accept({"price_mode": "auto"}, 0.0, 12.0) is True   # «всегда принимать» — принимает


def db_accept(prefs, old, new):
    from app import db
    return db.price_auto_accept(prefs, old, new)
