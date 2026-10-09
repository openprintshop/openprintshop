"""Порядок разделов каталога «Модели» (OpenTodo 475b413e): при равном sort — по числу моделей
DESC (первичная сортировка задачи), set_collection_category_sort даёт админу приоритет над этим."""


async def _add_items(db, category: str, n: int) -> None:
    """Кладёт n моделей в раздел category (каждая — отдельная запись коллекции)."""
    for i in range(n):
        cid = await db.add_to_collection(
            hash=f"{category}-{i}", label=f"{category} модель {i}",
            paths=[f"{category}-{i}.stl"], preview_path=None, added_by=None)
        await db.set_collection_category(cid, category)


async def _order(db, names) -> list[str]:
    """Порядок разделов из names — по факту прихода в list_collection_categories()."""
    names = set(names)
    rows = await db.list_collection_categories()
    return [r[0] for r in rows if r[0] in names]


async def test_equal_sort_orders_by_count_desc(tmp_db):
    db = tmp_db
    # три новых раздела с одинаковым (дефолтным) sort — их порядок решает число моделей
    await db.upsert_collection_category("A")
    await db.upsert_collection_category("B")
    await db.upsert_collection_category("C")
    await _add_items(db, "A", 1)
    await _add_items(db, "B", 3)
    await _add_items(db, "C", 2)

    assert await _order(db, ["A", "B", "C"]) == ["B", "C", "A"]


async def test_admin_sort_overrides_count(tmp_db):
    db = tmp_db
    await db.upsert_collection_category("A")
    await db.upsert_collection_category("B")
    await db.upsert_collection_category("C")
    # A — меньше всех моделей, но админ выводит его в начало через sort
    await _add_items(db, "A", 1)
    await _add_items(db, "B", 3)
    await _add_items(db, "C", 2)

    assert await _order(db, ["A", "B", "C"]) == ["B", "C", "A"]   # до сортировки — как раньше

    await db.set_collection_category_sort("A", 0)
    assert await _order(db, ["A", "B", "C"]) == ["A", "B", "C"]   # A теперь первый, sort сильнее count

    # опускаем B ниже C явным sort, хотя моделей у B больше
    await db.set_collection_category_sort("B", 50)
    await db.set_collection_category_sort("C", 10)
    assert await _order(db, ["A", "B", "C"]) == ["A", "C", "B"]
