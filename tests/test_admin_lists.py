"""Админка держит ДВА разных списка: клиентские заказы и «печать для себя» мастера.

Клиентский список (kind='client') не должен содержать самопечать мастера — заказы со
статусом 'self' (create_self_print_order) ИЛИ заказы, где клиент сам админ. Список самопечати
(kind='self') должен содержать ровно их. Регресс-защита на разделение из webapp-админки.
"""
from app import config


async def _mk_client(db, uid, label="Клиентская модель"):
    return await db.add_request(
        user_id=uid, user_name="client", model_label=label, printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=30, price=5,
        preview_path=None)


async def test_orders_and_self_prints_are_split(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)

    # два настоящих клиентских заказа
    c1 = await _mk_client(tmp_db, 111, "Бэйбизавр")
    c2 = await _mk_client(tmp_db, 222, "Ваза")

    # две самопечати мастера (status='self', self_print=1, user_id=админ)
    s1 = await tmp_db.create_self_print_order(
        999, plastic_name="PETG", grams=4.2, minutes=53, price=3.0, label="Самопечать A")
    s2 = await tmp_db.create_self_print_order(
        999, plastic_name="PLA", grams=1.2, minutes=8, price=3.0, label="Самопечать B")

    # заказ, где клиент — сам админ (обычный статус, но это тоже «для себя»)
    s3 = await _mk_client(tmp_db, 999, "Заказ мастера самому себе")

    client_ids = {r.id for r in await tmp_db.list_requests(kind="client")}
    self_ids = {r.id for r in await tmp_db.list_requests(kind="self")}
    all_ids = {r.id for r in await tmp_db.list_requests(kind="all")}

    # клиентский список — только реальные клиенты, без самопечати
    assert client_ids == {c1, c2}
    assert s1 not in client_ids and s2 not in client_ids and s3 not in client_ids

    # список самопечати — ровно исключённые из клиентского
    assert self_ids == {s1, s2, s3}

    # 'all' (обратная совместимость: CSV-экспорт) — всё вместе
    assert all_ids == {c1, c2, s1, s2, s3}

    # разбиение не пересекается и покрывает всё
    assert client_ids.isdisjoint(self_ids)
    assert client_ids | self_ids == all_ids

    # все строки из self-списка действительно самопечать (status='self' или клиент-админ)
    for r in await tmp_db.list_requests(kind="self"):
        assert r.status == "self" or config.is_admin(r.user_id)


async def test_split_respects_search(tmp_db, monkeypatch):
    """Сквозной поиск работает внутри каждого списка и не протаскивает чужие строки."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    await _mk_client(tmp_db, 111, "Уникальная деталь")
    sid = await tmp_db.create_self_print_order(
        999, plastic_name="PETG", grams=4.2, minutes=53, price=3.0, label="Уникальная деталь")

    # поиск в клиентском списке не находит самопечать даже при совпадении названия
    client_hits = await tmp_db.list_requests(kind="client", search="Уникальная деталь")
    assert all(not config.is_admin(r.user_id) and r.status != "self" for r in client_hits)
    assert sid not in {r.id for r in client_hits}

    # тот же поиск в списке самопечати находит именно её
    self_hits = await tmp_db.list_requests(kind="self", search="Уникальная деталь")
    assert sid in {r.id for r in self_hits}


async def test_no_admins_configured_falls_back_to_status(tmp_db, monkeypatch):
    """Без сконфигурированных админов «для себя» = только status='self' (без user_id-эвристики)."""
    monkeypatch.setattr(config, "ADMIN_IDS", [], raising=False)
    c1 = await _mk_client(tmp_db, 111, "Клиент")
    # create_self_print_order всё равно ставит status='self'
    s1 = await tmp_db.create_self_print_order(
        111, plastic_name="PLA", grams=1, minutes=1, price=1, label="Самопечать")
    client_ids = {r.id for r in await tmp_db.list_requests(kind="client")}
    self_ids = {r.id for r in await tmp_db.list_requests(kind="self")}
    assert client_ids == {c1}
    assert self_ids == {s1}
