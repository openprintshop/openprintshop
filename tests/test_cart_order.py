"""Мультифайловый заказ-корзина (B5).

Корзина уже была реализована «слабой группировкой»: _api_order_cart кладёт каждую позицию
отдельной строкой requests с общим тегом order_group. B5 повышает order_group до сущности
первого класса (таблица order_groups) и сворачивает выдачу по группам в клиентском и
мастерском списках. requests остаётся источником истины по позиции — НЕ нормализуем в
order/order_items. Ключевые инварианты, которые проверяем:

- оформление корзины создаёт N строк requests с ОДНИМ непустым order_group + одну строку
  order_groups с total_snapshot = сумме цен позиций;
- _api_orders сворачивает позиции одной непустой группы в ОДНУ карточку с суммарным итогом;
- одиночный заказ (order_group NULL) НЕ схлопывается (иначе все синглтоны стали бы одним);
- бэкфилл order_groups идемпотентен (повтор не дублирует);
- смешанные статусы позиций группы отдаются rollup'ом, а не единым лживым статусом.
"""
import json

from conftest import stub_auth, stub_bot

from app import config, webapp

UID = 4242
ADMIN_ID = 777


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeGet:
    """GET-запрос для хендлеров, читающих request.query (а не body)."""
    def __init__(self, **query):
        self.query = query


class _FakeAdminOrderReq:
    """GET-запрос для _api_admin_order: initData в query, id заказа в match_info."""
    def __init__(self, oid, **query):
        self.query = query
        self.match_info = {"id": str(oid)}


class _FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))


def _user():
    return {"id": UID, "username": "tester", "first_name": "Тест"}


def _seed_model(token, *, label, price, plastic="Test PLA · чёрный"):
    """Серверный снимок токена расчёта — минимум, который читает _place_one."""
    webapp._models[token] = {
        "uid": UID, "label": label, "paths": [], "preview": None,
        "last": {"printer": "Centauri", "plastic_name": plastic, "qty": 1,
                 "quality": "0.20", "strength": "Стандарт", "grams_total": 20.0,
                 "minutes_total": 100.0, "total": price, "dims": "40×40×20",
                 "color": "чёрный", "scale_pct": 100},
    }


async def _call_cart(body):
    resp = await webapp._api_order_cart(_FakeRequest(body))
    return json.loads(resp.body.decode())


async def _call_orders():
    resp = await webapp._api_orders(_FakeGet(initData="x"))
    return json.loads(resp.body.decode())


async def _call_admin_orders():
    resp = await webapp._api_admin_orders(_FakeGet(initData="x"))
    return json.loads(resp.body.decode())


async def _call_admin_order(oid):
    resp = await webapp._api_admin_order(_FakeAdminOrderReq(oid, initData="x"))
    return json.loads(resp.body.decode())


async def test_cart_creates_group(tmp_db, monkeypatch):
    db = tmp_db
    stub_auth(monkeypatch, _verify_user=lambda init_data: _user())
    stub_bot(monkeypatch, _FakeBot())
    _seed_model("t1", label="Кубик", price=10.0)
    _seed_model("t2", label="Шестерёнка", price=15.5)
    _seed_model("t3", label="Кронштейн", price=4.5)

    out = await _call_cart({"initData": "x", "tokens": ["t1", "t2", "t3"],
                            "delivery_method": "delivery", "delivery_address": "ул. Пример, 1",
                            "comment": "к пятнице"})
    assert out["ok"] is True
    assert out["count"] == 3
    gid = out["group"]
    assert gid and gid.strip()

    reqs = await db.list_requests_by_group(gid)
    assert len(reqs) == 3
    # все позиции несут ОДИН непустой order_group
    assert {r.order_group for r in reqs} == {gid}
    assert all(r.order_group for r in reqs)

    ent = await db.get_order_group(gid)
    assert ent is not None
    assert ent["user_id"] == UID
    assert abs(ent["total_snapshot"] - 30.0) < 1e-6      # сумма цен позиций
    assert ent["delivery_method"] == "delivery"
    assert ent["delivery_address"] == "ул. Пример, 1"
    assert ent["comment"] == "к пятнице"
    # серверная копия корзины очищена после оформления
    for t in ("t1", "t2", "t3"):
        webapp._models.pop(t, None)


async def test_pickup_has_no_address(tmp_db, monkeypatch):
    """Самовывоз не тащит адрес, даже если клиент что-то прислал в поле."""
    db = tmp_db
    stub_auth(monkeypatch, _verify_user=lambda init_data: _user())
    stub_bot(monkeypatch, _FakeBot())
    _seed_model("p1", label="Брелок", price=6.0)
    out = await _call_cart({"initData": "x", "tokens": ["p1"],
                            "delivery_method": "pickup", "delivery_address": "должен отвалиться"})
    ent = await db.get_order_group(out["group"])
    assert ent["delivery_method"] == "pickup"
    assert not (ent["delivery_address"] or "")
    webapp._models.pop("p1", None)


async def test_orders_rollup_groups(tmp_db, monkeypatch):
    """Свёртка: 2 позиции одной группы → 1 карточка-группа с суммарным итогом; одиночный
    заказ (order_group NULL) остаётся отдельной карточкой и НЕ схлопывается."""
    db = tmp_db
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    gid = "grp0000000aa"
    await db.add_request(user_id=UID, user_name="@tester", model_label="A", printer="C",
                         plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                         grams_total=10, minutes_total=50, price=12.0, preview_path=None,
                         order_group=gid)
    await db.add_request(user_id=UID, user_name="@tester", model_label="B", printer="C",
                         plastic_name="PLA", quantity=2, quality="0.2", strength="Ст",
                         grams_total=20, minutes_total=90, price=8.0, preview_path=None,
                         order_group=gid)
    # одиночный заказ — БЕЗ группы
    await db.add_request(user_id=UID, user_name="@tester", model_label="Solo", printer="C",
                         plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                         grams_total=5, minutes_total=25, price=5.0, preview_path=None,
                         order_group=None)
    await db.create_order_group(gid, UID, delivery_method="pickup", total_snapshot=20.0)

    out = await _call_orders()
    assert out["ok"] is True
    cards = out["orders"]
    groups = [c for c in cards if c.get("group")]
    singles = [c for c in cards if not c.get("group")]
    assert len(groups) == 1              # ровно одна карточка-группа
    assert len(singles) == 1             # одиночный НЕ схлопнут
    g = groups[0]
    assert g["gid"] == gid
    assert g["count"] == 2
    assert len(g["items"]) == 2
    assert "20" in g["total"]            # 12 + 8 = 20 (деривировано из позиций)
    assert singles[0]["label"] == "Solo"


async def test_null_group_not_collapsed(tmp_db, monkeypatch):
    """Три одиночных заказа (order_group NULL) — три отдельные карточки, не одна фейковая группа."""
    db = tmp_db
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    for lbl in ("X", "Y", "Z"):
        await db.add_request(user_id=UID, user_name="@t", model_label=lbl, printer="C",
                             plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                             grams_total=5, minutes_total=25, price=3.0, preview_path=None,
                             order_group=None)
    out = await _call_orders()
    assert all(not c.get("group") for c in out["orders"])
    assert len(out["orders"]) == 3


async def test_single_order_unaffected(tmp_db, monkeypatch):
    """Регресс: одиночный заказ рендерится как раньше, _order_json не ломается, group пуст."""
    db = tmp_db
    rid = await db.add_request(user_id=UID, user_name="@t", model_label="Одиночка", printer="C",
                               plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                               grams_total=5, minutes_total=25, price=7.0, preview_path=None)
    r = await db.get_request(rid)
    assert r.order_group == ""
    j = webapp._order_json(r)
    assert j["group"] == ""
    assert j["id"] == rid


async def test_group_backfill_migration(tmp_db):
    """Бэкфилл заводит order_groups по существующим requests.order_group и идемпотентен."""
    db = tmp_db
    gid = "backfillgrp1"
    for lbl, price in (("A", 4.0), ("B", 6.0)):
        await db.add_request(user_id=UID, user_name="@t", model_label=lbl, printer="C",
                             plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                             grams_total=5, minutes_total=25, price=price, preview_path=None,
                             order_group=gid)
    # строки-сущности ещё нет (положили позиции напрямую)
    assert await db.get_order_group(gid) is None

    await db.backfill_order_groups()
    ent = await db.get_order_group(gid)
    assert ent is not None
    assert ent["user_id"] == UID
    assert abs(ent["total_snapshot"] - 10.0) < 1e-6

    # идемпотентность: повтор не создаёт дубль и не меняет запись
    await db.backfill_order_groups()
    cur = await db._conn().execute("SELECT COUNT(*) AS n FROM order_groups WHERE id = ?", (gid,))
    assert (await cur.fetchone())["n"] == 1


async def test_backfill_ignores_null_group(tmp_db):
    """Бэкфилл НЕ создаёт группу из заказов без order_group (иначе NULL-схлопывание)."""
    db = tmp_db
    await db.add_request(user_id=UID, user_name="@t", model_label="Solo", printer="C",
                         plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                         grams_total=5, minutes_total=25, price=5.0, preview_path=None,
                         order_group=None)
    await db.backfill_order_groups()
    cur = await db._conn().execute("SELECT COUNT(*) AS n FROM order_groups")
    assert (await cur.fetchone())["n"] == 0


async def test_migration_v6_backfills_on_reinit(tmp_db):
    """Версионная миграция v6: если позиции с order_group есть, а user_version < 6 — при init
    заводится шапка. Симулируем «до-B5» БД: откатываем user_version и стираем order_groups."""
    db = tmp_db
    gid = "reinitgrp001"
    await db.add_request(user_id=UID, user_name="@t", model_label="A", printer="C",
                         plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                         grams_total=5, minutes_total=25, price=9.0, preview_path=None,
                         order_group=gid)
    # состояние «до v6»: шапки нет, версия занижена
    await db._conn().execute("DELETE FROM order_groups")
    await db._conn().execute("PRAGMA user_version = 5")
    await db._conn().commit()
    assert await db.get_order_group(gid) is None

    await db.init()          # повторный init прогоняет миграции по уже открытому соединению
    ent = await db.get_order_group(gid)
    assert ent is not None and abs(ent["total_snapshot"] - 9.0) < 1e-6
    cur = await db._conn().execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] >= 6


async def test_group_mixed_statuses(tmp_db, monkeypatch):
    """Позиции группы в разных статусах: карточка группы отдаёт rollup статусов, не единый лживый."""
    db = tmp_db
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    gid = "mixedgrp0001"
    r1 = await db.add_request(user_id=UID, user_name="@t", model_label="A", printer="C",
                              plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                              grams_total=5, minutes_total=25, price=10.0, preview_path=None,
                              order_group=gid)
    r2 = await db.add_request(user_id=UID, user_name="@t", model_label="B", printer="C",
                              plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                              grams_total=5, minutes_total=25, price=10.0, preview_path=None,
                              order_group=gid)
    await db.update_request(r1, status="printed")
    await db.update_request(r2, status="rejected")

    out = await _call_orders()
    g = [c for c in out["orders"] if c.get("group")][0]
    statuses = {s["status"] for s in g["statuses"]}
    assert statuses == {"printed", "rejected"}      # оба статуса видны, не сведены в один
    # позиции внутри сохраняют свои индивидуальные статусы
    assert {it["status"] for it in g["items"]} == {"printed", "rejected"}
    # действия остаются пер-позиционными: у каждой позиции свой id, по которому мастер её двигает
    assert {it["id"] for it in g["items"]} == {r1, r2}
    assert all("id" in it and "status_label" in it for it in g["items"])


async def test_cart_one_admin_notification(tmp_db, monkeypatch):
    """ОДНО уведомление админам на весь чекаут корзины (сводка), а НЕ N (per-позицию).
    Критично: старый поток слал бы _notify_new_order на каждую из 3 позиций."""
    stub_auth(monkeypatch, _verify_user=lambda init_data: _user())
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)
    _seed_model("n1", label="A", price=10.0)
    _seed_model("n2", label="B", price=11.0)
    _seed_model("n3", label="C", price=12.0)

    out = await _call_cart({"initData": "x", "tokens": ["n1", "n2", "n3"]})
    assert out["ok"] is True and out["count"] == 3

    admin_ids = set(config.ADMIN_IDS)
    admin_msgs = [m for m in bot.messages if m[0] in admin_ids]
    user_msgs = [m for m in bot.messages if m[0] == UID]
    # ровно одна сводка на каждого админа (здесь один админ) — не три
    assert len(admin_msgs) == len(admin_ids) == 1
    assert len(user_msgs) == 1                       # клиенту — одно подтверждение
    # сводка перечисляет все позиции в одном сообщении
    body = admin_msgs[0][1]
    assert "3" in body                               # «3 позиции»
    for t in ("n1", "n2", "n3"):
        webapp._models.pop(t, None)


async def test_admin_orders_group_header(tmp_db, monkeypatch):
    """Мастерский список: 3 позиции одной группы несут общий gid + group_count/total под ОДНОЙ
    шапкой; одиночный заказ имеет пустой group и не мешается в группу."""
    db = tmp_db
    stub_auth(monkeypatch, _verify_admin=lambda init_data: ADMIN_ID)
    gid = "adminhdr0001"
    rids = []
    for lbl, price in (("A", 10.0), ("B", 20.0), ("C", 5.0)):
        rids.append(await db.add_request(
            user_id=UID, user_name="@t", model_label=lbl, printer="C", plastic_name="PLA",
            quantity=1, quality="0.2", strength="Ст", grams_total=5, minutes_total=25,
            price=price, preview_path=None, order_group=gid))
    await db.add_request(user_id=UID, user_name="@t", model_label="Solo", printer="C",
                         plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                         grams_total=5, minutes_total=25, price=9.0, preview_path=None,
                         order_group=None)
    await db.create_order_group(gid, UID, delivery_method="pickup", total_snapshot=35.0)

    out = await _call_admin_orders()
    rows = out["orders"]
    grouped = [r for r in rows if r.get("group")]
    singles = [r for r in rows if not r.get("group") and r["label"] == "Solo"]
    # все три позиции под ОДНИМ заголовком: единый gid, одинаковый счётчик и итог
    assert {r["group"] for r in grouped} == {gid}
    assert len(grouped) == 3
    assert all(r["group_count"] == 3 for r in grouped)
    assert all(abs(r["group_total"] - 35.0) < 1e-6 for r in grouped)
    # одиночный заказ не втянут в группу
    assert len(singles) == 1 and singles[0]["group"] == ""


async def test_orders_rollup_without_entity(tmp_db, monkeypatch):
    """ОБРАТНАЯ СОВМЕСТИМОСТЬ: cart-заказ, у которого строки order_groups НЕТ (до бэкфилла v6),
    всё равно сворачивается в одну карточку с итогом = СУММЕ цен позиций, доставка пуста."""
    db = tmp_db
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    gid = "preboot00001"
    for lbl, price in (("A", 12.0), ("B", 8.0)):
        await db.add_request(user_id=UID, user_name="@t", model_label=lbl, printer="C",
                             plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                             grams_total=5, minutes_total=25, price=price, preview_path=None,
                             order_group=gid)
    # НАМЕРЕННО не создаём order_groups — симулируем старый заказ до бэкфилла
    assert await db.get_order_group(gid) is None

    out = await _call_orders()
    groups = [c for c in out["orders"] if c.get("group")]
    assert len(groups) == 1                           # свёртка работает без сущности
    g = groups[0]
    assert g["gid"] == gid and g["count"] == 2
    assert "20" in g["total"]                         # 12 + 8 = 20, деривировано из позиций
    assert g["delivery_method"] == "" and g["delivery_address"] == "" and g["comment"] == ""


async def test_migration_backfill_idempotent_second_init(tmp_db):
    """v6-бэкфилл идемпотентен на ВТОРОМ init: повторный прогон init поверх уже гружёной БД
    (user_version уже 6) не создаёт дублей и не роняет БД."""
    db = tmp_db
    gid = "twiceinit001"
    await db.add_request(user_id=UID, user_name="@t", model_label="A", printer="C",
                         plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                         grams_total=5, minutes_total=25, price=7.0, preview_path=None,
                         order_group=gid)
    # первый бэкфилл (симулируем «до-v6» откатом версии) — заводит шапку
    await db._conn().execute("DELETE FROM order_groups")
    await db._conn().execute("PRAGMA user_version = 5")
    await db._conn().commit()
    await db.init()
    assert await db.get_order_group(gid) is not None
    cur = await db._conn().execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] >= 6

    # второй init — версия уже 6, гейт закрыт, дублей нет
    await db.init()
    cur = await db._conn().execute("SELECT COUNT(*) AS n FROM order_groups WHERE id = ?", (gid,))
    assert (await cur.fetchone())["n"] == 1


async def test_group_delivery_shows_on_client_and_master(tmp_db, monkeypatch):
    """Доставка/адрес/комментарий сохраняются на группе и ВИДНЫ и в клиентской карточке (_api_orders),
    и в мастерской (_api_admin_order.group)."""
    db = tmp_db
    gid = "deliv0000001"
    rids = []
    for lbl, price in (("A", 10.0), ("B", 15.0)):
        rids.append(await db.add_request(
            user_id=UID, user_name="@t", model_label=lbl, printer="C", plastic_name="PLA",
            quantity=1, quality="0.2", strength="Ст", grams_total=5, minutes_total=25,
            price=price, preview_path=None, order_group=gid))
    await db.create_order_group(gid, UID, delivery_method="delivery",
                                delivery_address="ул. Пример, 5", comment="позвонить перед",
                                total_snapshot=25.0)

    # клиентская карточка
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    cout = await _call_orders()
    cg = [c for c in cout["orders"] if c.get("group")][0]
    assert cg["delivery_method"] == "delivery"
    assert cg["delivery_address"] == "ул. Пример, 5"
    assert cg["comment"] == "позвонить перед"

    # мастерская карточка (открыта по одной из позиций группы)
    stub_auth(monkeypatch, _verify_admin=lambda init_data: ADMIN_ID)
    mout = await _call_admin_order(rids[0])
    gb = mout["order"]["group"]
    assert gb is not None
    assert gb["gid"] == gid and gb["count"] == 2
    assert gb["delivery_method"] == "delivery"
    assert gb["delivery_address"] == "ул. Пример, 5"
    assert gb["comment"] == "позвонить перед"
    # текущая открытая позиция помечена; действия остаются пер-позиционными (у каждой свой id)
    cur_items = [it for it in gb["items"] if it["current"]]
    assert len(cur_items) == 1 and cur_items[0]["id"] == rids[0]
    assert {it["id"] for it in gb["items"]} == set(rids)
