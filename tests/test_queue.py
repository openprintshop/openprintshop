"""Тест очереди печати с позициями (d79b54bb)."""


async def _mk(db, label, minutes):
    return await db.add_request(
        user_id=1, user_name="t", model_label=label, printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=minutes,
        price=1, preview_path=None)


async def test_queue_order_and_position(tmp_db):
    db = tmp_db
    a = await _mk(db, "A", 60)
    b = await _mk(db, "B", 120)
    c = await _mk(db, "C", 30)
    items = await db.queue_items()
    assert [it["id"] for it in items] == [a, b, c]            # порядок создания
    assert [it["pos"] for it in items] == [1, 2, 3]
    # время «до готовности» = сумма впереди + своё
    assert items[2]["done_minutes"] == 60 + 120 + 30
    pos = await db.queue_position(b)
    assert pos["pos"] == 2 and pos["total"] == 3 and pos["done_minutes"] == 180


async def test_queue_move(tmp_db):
    db = tmp_db
    a = await _mk(db, "A", 10)
    b = await _mk(db, "B", 10)
    c = await _mk(db, "C", 10)
    assert await db.queue_move(c, -1) is True                 # C вверх → A, C, B
    assert [it["id"] for it in await db.queue_items()] == [a, c, b]
    assert await db.queue_move(a, -1) is False                # уже наверху — край
    assert await db.queue_move(b, +1) is False                # уже внизу — край


async def test_queue_excludes_finished(tmp_db):
    db = tmp_db
    a = await _mk(db, "A", 10)
    b = await _mk(db, "B", 10)
    await db.update_request(a, status="printed")              # ушёл из очереди
    items = await db.queue_items()
    assert [it["id"] for it in items] == [b]
    assert (await db.queue_position(b))["pos"] == 1           # перенумеровалось
    assert await db.queue_position(a) is None                 # не в очереди
