"""Серверный поиск по всему каталогу (жалоба «Беззубик — пусто»): подстрока по
label + ИИ-тегам + orig_label, кириллица без учёта регистра (LIKE в SQLite её не складывает),
совпадения в названии раньше тегов.

Плюс фаззи-слой на опечатки (OpenTodo c52f32b4, app/db.py::search_collection) —
бакеты: точный label, точный tag/orig_label, фаззи label, фаззи tag/orig_label."""


async def _seed(db):
    a = await db.add_to_collection(
        hash="h1", label="Флекси-дракон Беззубик", paths=["a.stl"], preview_path=None,
        added_by=None)
    b = await db.add_to_collection(
        hash="h2", label="Подставка для наушников", paths=["b.stl"], preview_path=None,
        added_by=None)
    c = await db.add_to_collection(
        hash="h3", label="Призрачное лицо", paths=["c.stl"], preview_path=None,
        added_by=None)
    await db._conn().execute(
        "UPDATE collection SET tags = ?, orig_label = ? WHERE id = ?",
        ("ghostface, крик, scream, хоррор", "Hex3D Ghostface", c))
    await db._conn().commit()
    return a, b, c


async def test_search_cyrillic_case_insensitive(tmp_db):
    db = tmp_db
    a, _, _ = await _seed(db)
    # в label «Беззубик» с заглавной — запрос в нижнем регистре обязан найти
    assert [it.id for it in await db.search_collection("беззубик")] == [a]
    assert [it.id for it in await db.search_collection("БЕЗЗУБИК")] == [a]


async def test_search_tags_and_orig_label(tmp_db):
    db = tmp_db
    _, _, c = await _seed(db)
    assert [it.id for it in await db.search_collection("ghostface")] == [c]   # ИИ-тег
    assert [it.id for it in await db.search_collection("hex3d")] == [c]       # orig_label
    assert [it.id for it in await db.search_collection("крик")] == [c]
    assert await db.search_collection("зубная фея") == []
    assert await db.search_collection("") == []


async def test_search_label_matches_before_tag_matches(tmp_db):
    db = tmp_db
    a, _, c = await _seed(db)
    # «дракон» в теге у c, в label у a — label-совпадение первым
    await db._conn().execute(
        "UPDATE collection SET tags = tags || ', дракон' WHERE id = ?", (c,))
    await db._conn().commit()
    assert [it.id for it in await db.search_collection("дракон")] == [a, c]


async def test_search_fuzzy_typo_in_label(tmp_db):
    """Опечатки (замена одной буквы) в label — фаззи-бакет находит без подстроки."""
    db = tmp_db
    a, b, _ = await _seed(db)
    # «беззубек» вместо «Беззубик» — 1 правка на слово длиной 8 (порог 2)
    assert [it.id for it in await db.search_collection("беззубек")] == [a]
    # «падставка» вместо «Подставка» — 1 правка на слово длиной 9 (порог 2)
    assert [it.id for it in await db.search_collection("падставка")] == [b]


async def test_search_fuzzy_typo_in_tag_latin(tmp_db):
    """Опечатка латиницей в ИИ-теге («gostface» — пропущена буква h в «ghostface»)."""
    db = tmp_db
    _, _, c = await _seed(db)
    assert [it.id for it in await db.search_collection("gostface")] == [c]


async def test_search_fuzzy_multiword_query(tmp_db):
    """Многословный запрос: каждый токен должен найтись сам по себе (точно или фаззи) —
    «дракон» точно, «беззубек»→«беззубик» фаззи."""
    db = tmp_db
    a, _, _ = await _seed(db)
    assert [it.id for it in await db.search_collection("дракон беззубек")] == [a]


async def test_search_short_query_no_fuzzy_noise(tmp_db):
    """Слова короче 4 букв — только точное совпадение: «дла» — опечатка в «для»
    (реальный токен label у b), но токен запроса слишком короткий для фаззи —
    пустая выдача, а не мусорное совпадение."""
    db = tmp_db
    await _seed(db)
    assert await db.search_collection("дла") == []


async def test_search_ranking_exact_tag_before_fuzzy(tmp_db):
    """Точное совпадение в теге ранжируется раньше фаззи-совпадения (опечатки) в label."""
    db = tmp_db
    d = await db.add_to_collection(
        hash="h4", label="Дракон беззубек", paths=["d.stl"], preview_path=None,
        added_by=None)
    e = await db.add_to_collection(
        hash="h5", label="Просто фигурка", paths=["e.stl"], preview_path=None,
        added_by=None)
    await db._conn().execute(
        "UPDATE collection SET tags = ? WHERE id = ?", ("беззубик", e))
    await db._conn().commit()
    # у d — опечатка «беззубек» в label (фаззи), у e — точный тег «беззубик»
    assert [it.id for it in await db.search_collection("беззубик")] == [e, d]
