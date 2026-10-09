"""Атомарность денежных и складских записей в app/db.py.

Соединение с БД в процессе ОДНО на бота и веб, транзакционных границ вокруг операций нет.
Любая схема «прочитал остаток → посчитал в питоне → записал АБСОЛЮТНОЕ значение» поэтому
дырявая: между чтением и записью успевает пройти чужой коммит, и абсолютная запись затирает
его молча — журнал движений показывает обе операции, а итог соответствует только последней.

Каждый тест ниже даёт ровно этот сценарий: несколько одновременных операций над одной
строкой (asyncio.gather; каждый await на соединении — точка переключения, так что все
успевают прочитать состояние ДО чужой записи) и сверка «журнал против итога».

На старом коде — read-modify-write — они красные: движения все на месте, итог потерян.
"""
import asyncio

import pytest


async def _seed_plastic(db, *, stock: float = 1000.0, low: float = 0.0) -> int:
    pid = await db.add_plastic("PLA атомарность", 1000, 1.24, "PLA")
    await db.update_plastic(pid, stock_grams=stock, low_threshold=low)
    return pid


async def test_adjust_balance_concurrent_keeps_every_tx(tmp_db):
    """Пять одновременных начислений: журнал balance_tx и остаток обязаны сойтись.

    Ради этого теста всё и затевалось: старый adjust_balance читал баланс, прибавлял amount
    в питоне и писал АБСОЛЮТ — пять параллельных пополнений видели один и тот же ноль, и на
    счёте оставалось последнее из них вместо суммы. Деньги клиента исчезали бесследно,
    притом что в истории транзакций все пять записей на месте.
    """
    db = tmp_db
    amounts = [10.0, 20.0, 30.0, 40.0, 50.0]
    await asyncio.gather(*(db.adjust_balance(77, a, f"Пополнение {a}") for a in amounts))

    tx = await db.list_balance_tx(77, limit=100)
    assert len(tx) == len(amounts)                                   # журнал полный
    assert sum(t["amount"] for t in tx) == pytest.approx(sum(amounts))
    assert await db.get_balance(77) == pytest.approx(sum(amounts))   # и остаток ему равен


async def test_adjust_balance_concurrent_mixed_signs(tmp_db):
    """Пополнения вперемешку со списаниями — итог = алгебраическая сумма журнала."""
    db = tmp_db
    await db.adjust_balance(78, 100.0, "Стартовое пополнение")
    moves = [25.0, -10.0, 12.5, -7.5, -20.0]
    await asyncio.gather(*(db.adjust_balance(78, a, "Движение") for a in moves))

    tx = await db.list_balance_tx(78, limit=100)
    assert sum(t["amount"] for t in tx) == pytest.approx(100.0 + sum(moves))
    assert await db.get_balance(78) == pytest.approx(100.0 + sum(moves))


async def test_deduct_stock_concurrent_deducts_every_gram(tmp_db):
    """Пять одновременных списаний общего склада материала списывают все пять порций."""
    db = tmp_db
    pid = await _seed_plastic(db, stock=1000.0)
    await asyncio.gather(*(db.deduct_stock(pid, 100.0) for _ in range(5)))
    assert (await db.get_plastic(pid)).stock_grams == pytest.approx(500.0)


async def test_deduct_color_stock_concurrent_deducts_every_gram(tmp_db):
    """То же для пер-цветового остатка: пять списаний по 30 г с одного цвета."""
    db = tmp_db
    pid = await _seed_plastic(db, stock=0.0)
    await db.set_color_stock(pid, "Красный", 500.0, 0.0)
    await asyncio.gather(*(db.deduct_color_stock(pid, "Красный", 30.0) for _ in range(5)))
    assert (await db.get_color_stock(pid))["Красный"]["stock"] == pytest.approx(350.0)


async def test_apply_order_stock_concurrent_deducts_once(tmp_db):
    """Два одновременных «напечатано» по одному заказу списывают склад РОВНО ОДИН раз.

    Идемпотентность держится на requests.stock_deducted, но проверка флага и его установка
    стояли по разные стороны всего списания — оба вызова успевали прочитать ноль. Захват
    флага обязан быть атомарным: списывает тот, чей UPDATE ... WHERE stock_deducted = 0
    реально изменил строку.
    """
    db = tmp_db
    pid = await _seed_plastic(db, stock=1000.0)
    rid = await db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                               plastic_name="P", quantity=1, quality="0.2", strength="н",
                               grams_total=120, minutes_total=30, price=10, preview_path=None)
    await db.set_order_stock_plan(rid, [{"plastic_id": pid, "name": "P", "colors": [],
                                         "grams": 120, "color_grams": None}])

    await asyncio.gather(db.apply_order_stock(rid), db.apply_order_stock(rid))
    assert (await db.get_plastic(pid)).stock_grams == pytest.approx(880.0)
    assert (await db.reserved_grams_by_plastic()).get(pid) is None


async def test_deduct_stock_boundaries_unchanged(tmp_db):
    """Границы общего склада ровно те же, что были у питоновского max(0.0, ...).

    Кламп переехал из питона в SQL (MAX(0, ...)), поэтому его края закреплены явно: списание
    больше остатка садит склад в НОЛЬ, а не в минус; отрицательные граммы (мусор на входе)
    ничего не меняют; при выключенном учёте (stock_grams = 0) не пишем вообще ничего.
    """
    db = tmp_db
    pid = await _seed_plastic(db, stock=100.0, low=30.0)
    new, crossed = await db.deduct_stock(pid, 250.0)              # больше, чем есть
    assert new == 0.0 and crossed is True                          # ноль, не минус; порог пересечён
    assert (await db.get_plastic(pid)).stock_grams == 0.0

    await db.update_plastic(pid, stock_grams=100.0, low_threshold=0.0)
    new, crossed = await db.deduct_stock(pid, -50.0)               # отрицательные граммы
    assert new == 100.0 and crossed is False                       # склад не вырос
    assert (await db.get_plastic(pid)).stock_grams == 100.0

    await db.update_plastic(pid, stock_grams=0.0)                  # учёт не ведётся
    assert await db.deduct_stock(pid, 10.0) == (0.0, False)
    assert (await db.get_plastic(pid)).stock_grams == 0.0


async def test_deduct_color_stock_boundaries_unchanged(tmp_db):
    """То же для пер-цветового остатка: ноль вместо минуса, отрицательные граммы — no-op."""
    db = tmp_db
    pid = await _seed_plastic(db, stock=0.0)                       # общий склад не ведётся
    await db.set_color_stock(pid, "Красный", 80.0, 20.0)
    new, crossed = await db.deduct_color_stock(pid, "Красный", 200.0)
    assert new == 0.0 and crossed is True
    assert (await db.get_color_stock(pid))["Красный"]["stock"] == 0.0

    await db.set_color_stock(pid, "Синий", 80.0, 0.0)
    new, crossed = await db.deduct_color_stock(pid, "Синий", -30.0)
    assert new == 80.0 and crossed is False
    assert (await db.get_color_stock(pid))["Синий"]["stock"] == 80.0


async def test_apply_order_stock_missing_request_is_noop(tmp_db):
    """Захват флага не выдумывает заказ: несуществующий rid — пустой результат и никаких записей."""
    db = tmp_db
    assert await db.apply_order_stock(999999) == []


async def test_award_referral_concurrent_pays_once(tmp_db):
    """Два одновременных начисления реферального бонуса платят один раз, не два."""
    db = tmp_db
    assert await db.add_referral(200, 100) is True
    rid = await db.add_request(user_id=200, user_name="t", model_label="m", printer="p",
                               plastic_name="x", quantity=1, quality="q", strength="s",
                               grams_total=1, minutes_total=1, price=1, preview_path=None)
    await db.update_request(rid, status="printed", paid=1)

    got = await asyncio.gather(db.award_referral_if_due(200, 5.0),
                               db.award_referral_if_due(200, 5.0))
    assert sorted(got, key=lambda x: x is None) == [100, None]   # наградил ровно один вызов
    assert await db.get_balance(200) == pytest.approx(5.0)
    assert await db.get_balance(100) == pytest.approx(5.0)
