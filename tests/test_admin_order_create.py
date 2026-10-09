"""Мастер оформляет заказ за клиента (OpenTodo cd3258d6).

Иногда клиенты пишут в личку мимо системы — мастер создаёт заказ сам, а бот шлёт его клиенту
на подтверждение. Проверяем эндпоинт /api/admin/order/create: создаётся заказ со статусом
'invoiced' + pending_price (тот же поток подтверждения, что у счёта), пишется id админа-оформителя,
уведомление уходит КЛИЕНТУ (а не админу), и цена авто-считается, когда мастер её не задал.
"""
from conftest import stub_auth, stub_bot

from app import config, pricing, webapp

ADMIN_ID = 777
USER_ID = 456


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeBot:
    """Запоминает все send_message — чтобы убедиться, что уведомили клиента, а не админа."""
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))


async def _seed(db):
    pid = await db.add_plastic("Test PLA", 1000.0, 1.0)
    quals = await db.list_quality(active_only=True)
    strs = await db.list_strength(active_only=True)
    return pid, quals[0].id, strs[0].id


def _patch_admin(monkeypatch, bot):
    stub_auth(monkeypatch, _verify_admin=ADMIN_ID)
    stub_bot(monkeypatch, bot)


async def _call(body):
    resp = await webapp._api_admin_order_create(_FakeRequest(body))
    import json
    return json.loads(resp.body.decode())


async def test_admin_create_order_for_user(tmp_db, monkeypatch):
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    out = await _call({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "qty": 2, "grams": 40, "minutes": 180, "price": 25.0,
        "label": "Подставка", "comment": "Клиент заказал лично",
    })
    assert out["ok"] is True
    rid = out["order_id"]

    r = await db.get_request(rid)
    assert r is not None
    assert r.user_id == USER_ID
    assert r.quantity == 2
    assert r.status == "invoiced"          # ждёт подтверждения клиента
    assert abs(r.pending_price - 25.0) < 1e-6
    assert abs(r.price - 25.0) < 1e-6
    assert r.created_by_admin_id == ADMIN_ID
    assert r.model_label == "Подставка"
    assert r.price_comment == "Клиент заказал лично"
    assert r.quality_id == qid and r.strength_id == sid
    # попадает в «нужно подтвердить» у клиента → существующий поток принять/отклонить
    assert await db.pending_price_count(USER_ID) == 1

    # уведомили КЛИЕНТА, а не админа
    assert len(bot.messages) == 1
    assert bot.messages[0][0] == USER_ID
    assert "подтверд" in bot.messages[0][1].lower()


async def test_admin_create_order_auto_price(tmp_db, monkeypatch):
    """Без явной цены сумма считается той же формулой, что и обычный заказ (compute_price_manual)."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    out = await _call({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "qty": 1, "grams": 40, "minutes": 180,   # price опущена
    })
    assert out["ok"] is True
    settings = await db.get_settings()
    ref = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=1)
    assert abs(out["price"] - ref.total) < 0.01
    r = await db.get_request(out["order_id"])
    assert r.price > 0
    assert abs(r.pending_price - ref.total) < 0.01


async def test_admin_create_order_dry_no_save(tmp_db, monkeypatch):
    """dry=true только считает сумму — заказ не создаётся и клиента не беспокоим."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    out = await _call({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "qty": 1, "grams": 40, "minutes": 180, "dry": True,
    })
    assert out["ok"] is True
    assert out["price"] > 0
    assert "order_id" not in out
    assert await db.count_user_requests(USER_ID) == 0
    assert bot.messages == []


async def test_admin_create_order_requires_user_and_presets(tmp_db, monkeypatch):
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    no_user = await _call({"initData": "x", "plastic_id": pid, "quality_id": qid,
                           "strength_id": sid, "grams": 40, "minutes": 180})
    assert no_user["ok"] is False

    bad_grams = await _call({"initData": "x", "user_id": USER_ID, "plastic_id": pid,
                             "quality_id": qid, "strength_id": sid, "grams": 0, "minutes": 180})
    assert bad_grams["ok"] is False
    assert await db.count_user_requests(USER_ID) == 0


async def test_admin_create_order_forbidden_for_non_admin(tmp_db, monkeypatch):
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bot = _FakeBot()
    stub_auth(monkeypatch, _verify_admin=None)   # не админ
    stub_bot(monkeypatch, bot)

    resp = await webapp._api_admin_order_create(_FakeRequest({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "grams": 40, "minutes": 180}))
    assert resp.status == 403
    assert await db.count_user_requests(USER_ID) == 0
    assert bot.messages == []


async def test_admin_create_order_applies_user_discount(tmp_db, monkeypatch):
    """Авто-цена учитывает персональную скидку клиента — как обычный заказ."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    await db.set_user_discount(USER_ID, 20.0, "vip")
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    out = await _call({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "qty": 1, "grams": 40, "minutes": 180})
    settings = await db.get_settings()
    ref = pricing.compute_price_manual(40.0, 180.0, 1000.0, settings, qty=1, discount_pct=20.0)
    assert abs(out["price"] - ref.total) < 0.01
    assert ref.discount_amount > 0          # скидка реально применилась


async def test_admin_create_order_applies_default_delivery(tmp_db, monkeypatch):
    """Заказ, заведённый мастером ЗА клиента, тоже едет по адресу из профиля клиента.

    До этого он был единственным путём оформления без шапки: группы не создавалось, доставку
    клиенту не выставляли вовсе, и порог «бесплатно от суммы» применять было не к чему —
    мастерская везла за свой счёт и узнавала об этом при отправке."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    tid = await db.add_delivery_type("Почтой", 7.0, free_from=100)
    await db.set_user_delivery_prefs(USER_ID, tid, "ул. Пример, 5")
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    out = await _call({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "qty": 1, "grams": 40, "minutes": 180, "price": 25.0})
    assert out["ok"] is True
    gid = await db.group_id_of_request(out["order_id"])
    assert gid, "заказу от мастера нужна шапка — иначе доставке негде жить"
    g = await db.get_order_group(gid)
    assert g["delivery_cost"] == 7.0 and g["delivery_type_name"] == "Почтой"
    assert g["delivery_address"] == "ул. Пример, 5"
    assert g["total_snapshot"] == 32.0                       # 25 позиция + 7 доставка
    assert out["delivery"]["cost"] == 7.0
    # про доставку сказано В ТОМ ЖЕ сообщении, где сумма: клиент жмёт «принять» по нему
    assert any("Почтой" in t for _chat, t in bot.messages)


async def test_admin_create_order_delivery_free_above_threshold(tmp_db, monkeypatch):
    """Порог «бесплатно от суммы» действует и когда цену назначил мастер."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    tid = await db.add_delivery_type("Почтой", 7.0, free_from=100)
    await db.set_user_delivery_prefs(USER_ID, tid, "ул. Пример, 5")
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    out = await _call({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "qty": 1, "grams": 40, "minutes": 180, "price": 250.0})
    g = await db.get_order_group(await db.group_id_of_request(out["order_id"]))
    assert g["delivery_cost"] == 0.0
    assert g["total_snapshot"] == 250.0


async def test_admin_create_order_without_prefs_has_no_group(tmp_db, monkeypatch):
    """У клиента без умолчаний доставки поведение прежнее: ни шапки, ни доставки."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    await db.add_delivery_type("Почтой", 7.0, free_from=100)
    bot = _FakeBot()
    _patch_admin(monkeypatch, bot)

    out = await _call({
        "initData": "x", "user_id": USER_ID, "plastic_id": pid, "quality_id": qid,
        "strength_id": sid, "qty": 1, "grams": 40, "minutes": 180, "price": 25.0})
    assert await db.group_id_of_request(out["order_id"]) == ""
    assert out["delivery"] is None


def test_currency_constant_present():
    assert isinstance(config.CURRENCY, str) and config.CURRENCY
