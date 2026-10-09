"""Баланс клиента в личном кабинете (OpenTodo 8c428c43)."""


async def test_balance_lifecycle_scenario(tmp_db):
    db = tmp_db
    assert await db.get_balance(5) == 0.0
    # сценарий из задачи: заказ 6.60, клиент принёс 10 — мастер зачислил, остаток на будущее
    assert await db.adjust_balance(5, 10.0, "Пополнение наличными") == 10.0
    assert await db.adjust_balance(5, -6.60, "Оплата заказа #1") == 3.40
    assert await db.get_balance(5) == 3.40
    tx = await db.list_balance_tx(5)
    assert len(tx) == 2
    assert tx[0]["amount"] == -6.60 and "Оплата" in tx[0]["reason"]   # новые сверху
    assert tx[1]["amount"] == 10.0


async def test_balance_rounding_no_float_tail(tmp_db):
    db = tmp_db
    await db.adjust_balance(1, 0.1)
    await db.adjust_balance(1, 0.2)
    assert await db.get_balance(1) == 0.3        # округление до копеек, без 0.30000000004


async def test_balance_can_go_negative(tmp_db):
    db = tmp_db
    assert await db.adjust_balance(2, -5.0, "долг") == -5.0   # допускаем минус (клиент должен)
    assert await db.get_balance(2) == -5.0


async def test_balance_tx_comment_lifecycle(tmp_db):
    """Комментарии к транзакциям (OpenTodo ccb1e4b6): при начислении и задним числом."""
    db = tmp_db
    # комментарий сразу при начислении
    await db.adjust_balance(7, 10.0, "Пополнение мастером", comment="принёс наличными")
    await db.adjust_balance(7, -3.0, "Оплата заказа #2")
    tx = await db.list_balance_tx(7)
    assert tx[0]["comment"] == "" and tx[1]["comment"] == "принёс наличными"
    assert tx[0]["id"] > tx[1]["id"]              # id отдаются — по ним аннотируем задним числом

    # комментарий к уже совершённой транзакции
    assert await db.set_balance_tx_comment(tx[0]["id"], "за брелоки со скидкой")
    tx2 = await db.list_balance_tx(7)
    assert tx2[0]["comment"] == "за брелоки со скидкой"
    # пустая строка стирает заметку
    assert await db.set_balance_tx_comment(tx[0]["id"], "  ")
    assert (await db.list_balance_tx(7))[0]["comment"] == ""
    # несуществующая транзакция → False
    assert not await db.set_balance_tx_comment(999999, "нет такой")
