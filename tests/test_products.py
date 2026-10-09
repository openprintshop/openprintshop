"""Готовые изделия / витрина (OpenTodo bd896aa6):

- мастер заводит товар (название/цена/наличие/описание), витрина отдаёт активные товары;
- клиент покупает СРАЗУ по фикс-цене — заказ создаётся с product_id, наличие списывается
  атомарно (две одновременные покупки последнего экземпляра не уводят склад в минус);
- нет в наличии → честный отказ, без заказа;
- админ-CRUD: создать/обновить/удалить.
"""
import json

from conftest import stub_auth, stub_bot

from app import db as dbmod
from app import webapp


class _FakeRequest:
    def __init__(self, body=None, query=None, match_info=None):
        self._body = body or {}
        self.query = query or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._body


class _FakeMsg:
    def __init__(self, mid):
        self.message_id = mid


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", chat_id, text))
        return _FakeMsg(len(self.sent))

    async def send_photo(self, chat_id, photo, caption="", **kw):
        self.sent.append(("photo", chat_id, caption))
        return _FakeMsg(len(self.sent))


async def test_db_add_list_get_count(tmp_db):
    pid = await tmp_db.add_product(title="Ваза", description="спираль", price=25.0, stock=3)
    hidden = await tmp_db.add_product(title="Черновик", price=10.0, stock=0, is_active=0)
    p = await tmp_db.get_product(pid)
    assert p.title == "Ваза" and p.price == 25.0 and p.stock == 3 and p.is_active == 1

    active = await tmp_db.list_products(active_only=True)
    assert [x.id for x in active] == [pid]                 # скрытый не в витрине
    allp = await tmp_db.list_products(active_only=False)
    assert {x.id for x in allp} == {pid, hidden}
    assert await tmp_db.count_products(active_only=True) == 1
    assert await tmp_db.count_products(active_only=False) == 2


async def test_buy_product_atomic(tmp_db):
    pid = await tmp_db.add_product(title="Брелок", price=5.0, stock=1)
    assert await tmp_db.buy_product(pid, 1) is True         # первый — успех
    assert await tmp_db.buy_product(pid, 1) is False        # второй — не хватило, склад не в минус
    p = await tmp_db.get_product(pid)
    assert p.stock == 0 and p.sold == 1

    pid2 = await tmp_db.add_product(title="Набор", price=5.0, stock=2)
    assert await tmp_db.buy_product(pid2, 5) is False       # запросили больше, чем есть — отказ целиком
    assert (await tmp_db.get_product(pid2)).stock == 2


async def test_api_product_order_creates_request(tmp_db, monkeypatch):
    uid = 300
    pid = await tmp_db.add_product(title="Подставка", description="под телефон", price=12.5, stock=4)

    stub_auth(monkeypatch,
              _verify_user=lambda s: {"id": uid, "username": "buyer", "first_name": "Б"})
    monkeypatch.setattr(webapp.config, "WEBAPP_PUBLIC_URL", "https://example.ts.net", raising=False)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    bot = _FakeBot()
    stub_bot(monkeypatch, bot)

    resp = await webapp._api_product_order(_FakeRequest({"product_id": pid, "qty": 2, "comment": "самовывоз"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and j["order_id"] > 0

    # наличие списано на 2
    assert (await tmp_db.get_product(pid)).stock == 2
    assert (await tmp_db.get_product(pid)).sold == 2

    rows = await tmp_db.list_user_requests(uid)
    assert len(rows) == 1
    r = rows[0]
    assert r.product_id == pid
    assert r.model_label == "Подставка" and r.quantity == 2
    assert abs(r.price - 25.0) < 1e-6                       # фикс-цена × количество
    assert "Готовое изделие" in (r.description or "") and "самовывоз" in (r.description or "")

    # уведомлены мастер и клиент
    assert any(s[1] == 999 for s in bot.sent)
    assert any(s[1] == uid and "принят" in s[2] for s in bot.sent)


async def test_api_product_order_out_of_stock(tmp_db, monkeypatch):
    uid = 301
    pid = await tmp_db.add_product(title="Распродано", price=9.0, stock=0)
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": uid})
    stub_bot(monkeypatch, None)

    resp = await webapp._api_product_order(_FakeRequest({"product_id": pid, "qty": 1}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is False and "наличии" in j["error"]
    assert await tmp_db.list_user_requests(uid) == []


async def test_api_admin_product_save_create_update_delete(tmp_db, monkeypatch):
    stub_auth(monkeypatch, _verify_admin=999)

    # создать
    resp = await webapp._api_admin_product_save(_FakeRequest(
        {"initData": "x", "title": "Кашпо", "price": "18,90", "stock": 5, "description": "для суккулентов"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and j["id"] > 0
    pid = j["id"]
    p = await tmp_db.get_product(pid)
    assert p.title == "Кашпо" and abs(p.price - 18.9) < 1e-6 and p.stock == 5

    # обновить (наличие + скрыть)
    resp = await webapp._api_admin_product_save(_FakeRequest(
        {"initData": "x", "id": pid, "title": "Кашпо XL", "price": 22, "stock": 8, "is_active": False}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    p = await tmp_db.get_product(pid)
    assert p.title == "Кашпо XL" and p.stock == 8 and p.is_active == 0

    # пустое название — отказ
    resp = await webapp._api_admin_product_save(_FakeRequest({"initData": "x", "title": "   "}))
    assert json.loads(resp.body.decode())["ok"] is False

    # удалить
    resp = await webapp._api_admin_product_delete(_FakeRequest({"initData": "x", "id": pid}))
    assert json.loads(resp.body.decode())["ok"] is True
    assert await tmp_db.get_product(pid) is None


async def test_product_order_applies_client_discount(tmp_db, monkeypatch):
    """Скидка клиента действует и на готовые изделия (OpenTodo 737b0a20)."""
    uid = 310
    await tmp_db.set_user_discount(uid, 20.0)
    pid = await tmp_db.add_product(title="Ваза со скидкой", price=10.0, stock=3)
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": uid, "username": "vip"})
    stub_bot(monkeypatch, None)

    resp = await webapp._api_product_order(_FakeRequest({"product_id": pid, "qty": 2}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True

    r = (await tmp_db.list_user_requests(uid))[0]
    assert abs(r.price - 16.0) < 1e-6                      # 10 × 2 × (1 − 20%)
    bd = r.breakdown or {}
    assert bd.get("discount_pct") == 20.0
    assert abs(bd.get("discount_amount", 0) - 4.0) < 1e-6
    assert "Скидка клиента 20%" in (r.description or "")


async def test_product_order_statuses_new_and_handed_only(tmp_db, monkeypatch):
    """У заказа готового изделия ровно два статуса — «Новый» и «Вручено» (OpenTodo 26a44d79)."""
    uid = 320
    pid = await tmp_db.add_product(title="Брелок-статус", price=5.0, stock=2)
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": uid})
    stub_bot(monkeypatch, None)
    resp = await webapp._api_product_order(_FakeRequest({"product_id": pid, "qty": 1}))
    rid = json.loads(resp.body.decode())["order_id"]

    stub_auth(monkeypatch, _admin_body=lambda b: (999, None))
    # печатные статусы товару недоступны
    resp = await webapp._api_admin_order_status(_FakeRequest({"id": rid, "status": "in_progress"}))
    assert json.loads(resp.body.decode())["ok"] is False
    resp = await webapp._api_admin_order_status(_FakeRequest({"id": rid, "status": "printed"}))
    assert json.loads(resp.body.decode())["ok"] is False
    # «Вручено» — доступно
    resp = await webapp._api_admin_order_status(_FakeRequest({"id": rid, "status": "handed"}))
    assert json.loads(resp.body.decode())["ok"] is True
    r = await tmp_db.get_request(rid)
    assert r.status == "handed"
    assert dbmod.REQ_STATUS_LABELS["handed"]
    # обычному (печатному) заказу «Вручено» недоступно
    orid = await tmp_db.add_request(
        user_id=uid, user_name="c", model_label="M", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=1, minutes_total=2, price=3,
        preview_path=None)
    resp = await webapp._api_admin_order_status(_FakeRequest({"id": orid, "status": "handed"}))
    assert json.loads(resp.body.decode())["ok"] is False
