"""Роли и права команды (A4).

До этого админка была плоской: ADMIN_IDS в .env, is_admin() — единственная проверка, любой
админ мог всё. Теперь ролей две (владелец/оператор), и проверяем ровно те инварианты, ради
которых фича делалась:

- оператор попадает в админку (is_admin), но НЕ получает прав на деньги/прайс/настройки;
- карта прав на роуты ЗАКРЫТА ПО УМОЛЧАНИЮ: незнакомая админская ручка требует прав
  владельца (забыли внести новую ручку — она не откроется наёмному сотруднику);
- middleware отдаёт 403 оператору на владельческой ручке и пропускает на производственной;
- денежные поля вырезаются из ответов (цена заказа, выручка дашборда) — не только прячутся
  во фронте;
- корневого владельца (.env) нельзя понизить или удалить из приложения.

Запросы собираются ОБЩИМИ фабриками из tests/conftest.py (post_req/get_req/admin_req/body), а
не местным фейком: у самоделки не было ни remote, ни headers, и сквозной путь ПК-токена
(_local_mw → _REQ_IS_LOCAL → _verify_user) на таком запросе просто падал с AttributeError.

Где initData лежит — существенно, и фабрика выбрана под это:
- post_req(...) кладёт его ТОЛЬКО в тело: _caps_mw сначала смотрит в query и лишь при пустом
  query читает тело POST-а. Положив initData ещё и в query, мы обошли бы ровно ту ветку,
  которую проверяем (и multipart-тест начал бы проходить сам собой);
- admin_req(...) кладёт в оба места — это для ХЕНДЛЕРОВ админки, они читают его из query.
"""
import pytest
from conftest import stub_auth

from app import config, db, roles, webapp

OWNER_ID = 900
OPERATOR_ID = 901
STRANGER_ID = 902


@pytest.fixture
def team(monkeypatch):
    """Владелец из .env + оператор в кэше ролей."""
    monkeypatch.setattr(config, "ADMIN_IDS", [OWNER_ID], raising=False)
    config.set_staff_roles({OPERATOR_ID: config.ROLE_OPERATOR})
    return None


def test_operator_is_admin_but_not_owner(team):
    assert config.is_admin(OPERATOR_ID) is True         # админка ему доступна
    assert config.is_owner(OPERATOR_ID) is False
    assert config.is_admin(STRANGER_ID) is False
    assert config.role_of(OWNER_ID) == config.ROLE_OWNER


def test_operator_caps_are_production_only(team):
    assert roles.can(OPERATOR_ID, roles.CAP_ORDERS)
    assert roles.can(OPERATOR_ID, roles.CAP_PRINTER)
    for cap in (roles.CAP_FINANCE, roles.CAP_PRICING, roles.CAP_CLIENTS, roles.CAP_SETTINGS):
        assert not roles.can(OPERATOR_ID, cap), cap
    assert roles.caps_of(OWNER_ID) == frozenset(roles.ALL_CAPS)
    assert roles.caps_of(STRANGER_ID) == frozenset()     # не сотрудник — прав нет вовсе


def test_unknown_admin_route_requires_owner(team):
    """Ручку забыли внести в карту → она владельческая. Ошибка по умолчанию в сторону закрытости."""
    assert webapp._route_cap("/api/admin/something_new_2027") == roles.CAP_SETTINGS
    assert not roles.can(OPERATOR_ID, webapp._route_cap("/api/admin/something_new_2027"))
    # у денежных и прайсовых ручек право названо явно — чтобы отказ был человеческим
    assert webapp._route_cap("/api/admin/order/price") == roles.CAP_FINANCE
    assert webapp._route_cap("/api/admin/delivery_type") == roles.CAP_PRICING
    assert webapp._route_cap("/api/admin/clients") == roles.CAP_CLIENTS
    # ...но доступа это не добавляет: у оператора нет ни одного из этих прав
    for path in ("/api/admin/order/price", "/api/admin/delivery_type", "/api/admin/clients"):
        assert not roles.can(OPERATOR_ID, webapp._route_cap(path)), path
    assert webapp._route_cap("/api/admin/orders") == roles.CAP_ORDERS
    assert webapp._route_cap("/api/admin/printer_move") == roles.CAP_PRINTER
    # калибровка закрыта префиксом целиком — это производство, а не деньги
    assert webapp._route_cap("/api/admin/calib/run_save") == roles.CAP_PRINTER


async def _run_mw(request, monkeypatch, uid):
    """Прогон _caps_mw с подменённой проверкой подписи на заданного пользователя.

    Подмена идёт через stub_auth, а не setattr на webapp: волна 4 цикла 2 вынесла _caps_mw
    в app/webguard.py, и он резолвит _verify_init_data из СВОИХ глобалов — патч, поставленный
    только на фасад webapp, до него не доходит."""
    stub_auth(monkeypatch, _verify_init_data=lambda init: uid)
    called = {"n": 0}

    async def handler(_req):
        called["n"] += 1
        return "handler-ran"

    res = await webapp._caps_mw(request, handler)
    return res, called["n"]


@pytest.mark.asyncio
async def test_middleware_blocks_operator_on_money_route(team, monkeypatch, post_req, body):
    req = post_req({"initData": "x"}, path="/api/admin/order/price")
    res, ran = await _run_mw(req, monkeypatch, OPERATOR_ID)
    assert ran == 0                                   # хендлер даже не вызвался
    assert res.status == 403
    data = body(res)
    assert data["ok"] is False
    assert data["cap"] not in roles.caps_of(OPERATOR_ID)   # отказ назвал именно недостающее право


@pytest.mark.asyncio
async def test_middleware_allows_operator_on_production_route(team, monkeypatch, post_req):
    # канонический путь роута = сам путь (параметров в нём нет) — фабрика ставит его сама
    req = post_req({"initData": "x"}, path="/api/admin/order/status")
    res, ran = await _run_mw(req, monkeypatch, OPERATOR_ID)
    assert ran == 1 and res == "handler-ran"
    assert req.get("caps") == roles.caps_of(OPERATOR_ID)


@pytest.mark.asyncio
async def test_middleware_lets_owner_everywhere(team, monkeypatch, post_req):
    req = post_req({"initData": "x"}, path="/api/admin/setting")
    res, ran = await _run_mw(req, monkeypatch, OWNER_ID)
    assert ran == 1 and res == "handler-ran"


@pytest.mark.asyncio
async def test_middleware_ignores_non_admin_paths(team, monkeypatch, get_req):
    """Клиентские ручки middleware не трогает — иначе он ломал бы обычных покупателей."""
    req = get_req({"initData": "x"}, path="/api/orders")
    res, ran = await _run_mw(req, monkeypatch, STRANGER_ID)
    assert ran == 1 and res == "handler-ran"


@pytest.mark.asyncio
async def test_middleware_skips_multipart_body(team, monkeypatch, post_req):
    """multipart нельзя читать в middleware (сломается request.multipart у хендлера) —
    такие ручки проверяют право сами (см. _api_admin_brand_logo).

    initData умышленно только в теле: будь он в query, middleware разобрал бы запрос ещё до
    проверки content_type, и до ветки multipart дело бы не дошло."""
    req = post_req({"initData": "x"}, path="/api/admin/brand_logo",
                   content_type="multipart/form-data; boundary=zzz")
    res, ran = await _run_mw(req, monkeypatch, OPERATOR_ID)
    assert ran == 1 and res == "handler-ran"


@pytest.mark.asyncio
async def test_admin_order_hides_money_from_operator(tmp_db, team, monkeypatch, admin_req, body):
    """Карточка заказа приезжает оператору БЕЗ цены, баланса и платежей — режет сервер."""
    stub_auth(monkeypatch, _verify_admin=lambda init: OPERATOR_ID)
    rid = await db.add_request(
        user_id=1, user_name="клиент", model_label="Кубик", printer="Centauri",
        plastic_name="PLA", quantity=1, quality="0.20", strength="Стандарт",
        grams_total=10.0, minutes_total=60.0, price=123.45, preview_path=None, status="new")

    req = admin_req(path=f"/api/admin/order/{rid}", match_info={"id": str(rid)})
    req["caps"] = roles.caps_of(OPERATOR_ID)
    data = body(await webapp._api_admin_order(req))
    assert data["ok"] and data["money"] is False
    o = data["order"]
    assert o["price"] is None and o["pending_price"] is None and o["balance"] is None
    assert o["payments"] == []
    assert o["label"] == "Кубик"          # производственные поля на месте


@pytest.mark.asyncio
async def test_admin_order_shows_money_to_owner(tmp_db, team, monkeypatch, admin_req, body):
    stub_auth(monkeypatch, _verify_admin=lambda init: OWNER_ID)
    rid = await db.add_request(
        user_id=1, user_name="клиент", model_label="Кубик", printer="Centauri",
        plastic_name="PLA", quantity=1, quality="0.20", strength="Стандарт",
        grams_total=10.0, minutes_total=60.0, price=123.45, preview_path=None, status="new")

    req = admin_req(path=f"/api/admin/order/{rid}", match_info={"id": str(rid)})
    req["caps"] = roles.caps_of(OWNER_ID)
    data = body(await webapp._api_admin_order(req))
    assert data["money"] is True
    assert data["order"]["price"] == 123.45


@pytest.mark.asyncio
async def test_dashboard_revenue_hidden_from_operator(tmp_db, team, monkeypatch, admin_req, body):
    stub_auth(monkeypatch, _verify_admin=lambda init: OPERATOR_ID)
    req = admin_req(path="/api/admin/dashboard")
    req["caps"] = roles.caps_of(OPERATOR_ID)
    data = body(await webapp._api_admin_dashboard(req))
    assert data["money"] is False
    for key in webapp._DASH_MONEY_KEYS:
        assert data["stats"].get(key) is None, key
    assert data["stats"]["orders"] is not None      # счётчики заказов остаются


@pytest.mark.asyncio
async def test_root_owner_cannot_be_demoted_or_removed(tmp_db, team):
    ok, err = await roles.set_role(OWNER_ID, config.ROLE_OPERATOR)
    assert ok is False and err
    ok, err = await roles.remove(OWNER_ID)
    assert ok is False and err
    assert config.role_of(OWNER_ID) == config.ROLE_OWNER


@pytest.mark.asyncio
async def test_staff_roundtrip_updates_cache(tmp_db, team):
    ok, err = await roles.set_role(STRANGER_ID, config.ROLE_OPERATOR, name="Иван")
    assert ok and not err
    assert config.role_of(STRANGER_ID) == config.ROLE_OPERATOR     # кэш обновился сразу
    lst = await roles.staff_list()
    assert any(r["user_id"] == STRANGER_ID and r["name"] == "Иван" for r in lst)
    assert any(r["user_id"] == OWNER_ID and r["root"] for r in lst)
    ok, _ = await roles.set_role(STRANGER_ID, config.ROLE_OWNER)
    assert ok and config.is_owner(STRANGER_ID)
    ok, _ = await roles.remove(STRANGER_ID)
    assert ok and config.role_of(STRANGER_ID) is None

    ok, err = await roles.set_role(STRANGER_ID, "superuser")        # неизвестная роль
    assert ok is False and err


@pytest.mark.asyncio
async def test_refresh_ignores_unknown_role_in_db(tmp_db, team):
    """Строка с мусорной ролью не должна давать доступ: лучше не пустить, чем выдать права."""
    await db.set_staff(STRANGER_ID, "director")
    await roles.refresh()
    assert config.role_of(STRANGER_ID) is None
    assert config.is_admin(STRANGER_ID) is False


# ───────── чем «Пульт» заполняет слоты, закрытые оператору (настольная админка) ─────────
@pytest.mark.asyncio
async def test_hub_gives_operator_the_numbers_his_pult_shows(tmp_db, team, monkeypatch,
                                                             admin_req, body):
    """У оператора на «Пульте» вместо выручки стоит карточка «Работа» — и живёт она на ХАБЕ.

    Отдельного запроса у неё нет намеренно: дашборд оператору закрыт, а хаб открыт. Здесь
    проверяется ровно тот контракт, на котором она держится: деньги вырезаны, а счётчики,
    которые она рисует, приходят числами. Обнули их кто-нибудь заодно с выручкой — карточка
    молча покажет нули, и оператор снова будет смотреть на «сломанный» экран вместо честного
    «эти цифры не для тебя»."""
    stub_auth(monkeypatch, _verify_admin=lambda init: OPERATOR_ID)
    req = admin_req(path="/api/admin/hub")
    req["caps"] = roles.caps_of(OPERATOR_ID)
    data = body(await webapp._api_admin_hub(req))

    assert data["money"] is False
    rev = data["revenue"]
    assert rev["today"] is None and rev["week"] is None and rev["avg_order"] is None
    assert isinstance(rev["orders_week"], int) and isinstance(rev["orders_total"], int)
    for k in ("queue", "tasks", "plastics"):
        assert isinstance(data["counts"].get(k), int), k


def test_operator_may_read_the_print_journal(team):
    """Вторая подменённая карточка «Пульта» — лента последних печатей вместо ленты событий.

    События клиентов оператору закрыты (право «Клиенты»), а журнал печатей — его прямая
    работа. Если журнал когда-нибудь сузят до владельца, карточка станет такой же молчаливо
    пустой, какой была лента событий, — тест обязан упасть раньше пользователя."""
    assert webapp._route_cap("/api/admin/journal") == roles.CAP_PRINTER
    assert roles.can(OPERATOR_ID, roles.CAP_PRINTER)
    assert not roles.can(OPERATOR_ID, roles.CAP_CLIENTS)     # лента событий и правда закрыта
