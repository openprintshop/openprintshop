"""Права команды после выноса в app/webguard.py: тождественность фасада и решение по канону.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна 4 цикла 2 вынесла слой прав (обе карты маршрутов, _route_cap,
вырезание денег и обе middleware) из app/webapp.py в app/webguard.py, оставив в webapp
построчный ФАСАД. Снимок карты прав (tests/test_routes_inventory.py) ловит подмену ПРАВА,
но не ловит две другие поломки переноса — их закрывает этот файл:

  * ЗАБЫТОЕ ИМЯ В ФАСАДЕ либо оставшаяся в webapp ВТОРАЯ КОПИЯ. Импорт от этого не падает,
    и снимок остаётся зелёным: он зовёт webapp._route_cap, а тот исправно приходит из
    webguard. А вот _hide_money или _forbidden_cap, потерянные в фасаде, дадут NameError
    у живого клиента — в ветке, куда тесты заглядывают не на каждом прогоне. Проверка
    `getattr(webapp, n) is getattr(webguard, n)` ловит и пропажу, и вторую копию разом.

  * КАРТА, СКОПИРОВАННАЯ ПО ЗНАЧЕНИЮ вместо реэкспорта объекта. Двух карт прав в проекте
    быть не должно: правка в одной молча разошлась бы со второй.

Плюс здесь закреплено свойство, ради которого middleware вообще смотрит в match_info:
решение принимается по КАНОНИЧЕСКОМУ пути маршрута, а не по фактическому request.path.
Перенос модуля канон не меняет, и этот тест — расписка в том, что не изменил.
"""
import pytest
from conftest import stub_auth

from app import config, roles, webapp, webauth, webguard

OWNER_ID = 960
OPERATOR_ID = 961

# Имена, которые app/webapp.py обязан реэкспортировать из app/webguard.py. Список ведётся
# руками именно потому, что он и есть контракт фасада.
FACADE = ("_OPERATOR_ROUTES", "_OPERATOR_PREFIXES", "_OWNER_ROUTES", "_route_cap",
          "_forbidden_cap", "_money_visible", "_hide_money", "_edition_mw", "_caps_mw")


@pytest.fixture
def team(monkeypatch):
    """Владелец из .env + оператор в кэше ролей (как в tests/test_roles.py)."""
    monkeypatch.setattr(config, "ADMIN_IDS", [OWNER_ID], raising=False)
    config.set_staff_roles({OPERATOR_ID: config.ROLE_OPERATOR})


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и webguard.<имя>, а не копия и не остаток старого кода."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(webguard, name), (
        f"webapp.{name} и webguard.{name} — РАЗНЫЕ объекты: либо в webapp осталась вторая копия, "
        f"либо фасад собран копированием значения вместо реэкспорта")


def test_webguard_takes_auth_from_webauth():
    """_caps_mw опознаёт сотрудника ТОЙ ЖЕ проверкой подписи, что и хендлеры.

    Заведись у webguard своя копия — разошлись бы поведение и, главное, подмены в тестах:
    половина путей осталась бы с настоящей проверкой на фальшивом initData."""
    assert webguard._verify_init_data is webauth._verify_init_data
    assert webapp._verify_init_data is webauth._verify_init_data


def test_no_second_route_map():
    """Карт прав ровно по одной, и обе стороны видят один объект (не снимок значений)."""
    webguard._OPERATOR_ROUTES["/api/admin/__probe__"] = roles.CAP_ORDERS
    try:
        assert webapp._OPERATOR_ROUTES["/api/admin/__probe__"] == roles.CAP_ORDERS
        assert webapp._route_cap("/api/admin/__probe__") == roles.CAP_ORDERS
    finally:
        webguard._OPERATOR_ROUTES.pop("/api/admin/__probe__")
    assert webapp._route_cap("/api/admin/__probe__") == roles.CAP_SETTINGS   # снова закрыто


async def _run_mw(request, monkeypatch, uid):
    stub_auth(monkeypatch, _verify_init_data=lambda init: uid)
    ran = {"n": 0}

    async def handler(_req):
        ran["n"] += 1
        return "handler-ran"

    return await webapp._caps_mw(request, handler), ran["n"]


@pytest.mark.asyncio
async def test_caps_decided_by_canonical_not_path(team, monkeypatch, post_req):
    """Право берётся по КАНОНУ маршрута: /api/admin/order/17 и /api/admin/order/42 — одно право.

    Пойди middleware по request.path — ни один заказ не нашёлся бы в карте, и карточка заказа
    закрылась бы оператору (закрытое по умолчанию = права владельца)."""
    for rid in ("17", "42"):
        req = post_req({"initData": "x"}, path=f"/api/admin/order/{rid}",
                       canonical="/api/admin/order/{id}", match_info={"id": rid})
        res, ran = await _run_mw(req, monkeypatch, OPERATOR_ID)
        assert ran == 1 and res == "handler-ran", rid
        assert req.get("caps") == roles.caps_of(OPERATOR_ID)


@pytest.mark.asyncio
async def test_caps_falls_back_to_path_without_resource(team, monkeypatch, post_req):
    """Роут без resource (канона нет) → сверяемся по фактическому пути, и он закрыт по умолчанию.

    Это прежняя ветка `(res.canonical if res is not None else None) or path`, перенесённая
    дословно: незнакомый путь оператору не открывается."""
    req = post_req({"initData": "x"}, path="/api/admin/order/17")
    req.match_info.route.resource = None
    res, ran = await _run_mw(req, monkeypatch, OPERATOR_ID)
    assert ran == 0 and res.status == 403
