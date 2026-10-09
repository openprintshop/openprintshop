"""Цены и скидки после выноса в app/web/prices.py: фасад, таблица маршрутов, помощники, права.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна «Цены и скидки» цикла 2 увезла из app/webapp.py семь ручек экрана
pricing.jsx — промокоды, тиражные и персональные скидки, каталог опций постобработки и
калькулятор сметы, — оставив в webapp построчный ФАСАД, а регистрацию переведя со списка
`router.add_*` в make_app на таблицу `web.RouteTableDef()` рядом с обработчиками.

Прибор здесь единственный сторож: у ЧЕТЫРЁХ ручек из семи (`_api_admin_promos`,
`_api_admin_promo`, `_api_admin_discounts`, `_api_admin_discount`) нет ни одного прямого
обращения `webapp.<имя>` и ни одного monkeypatch во всём дереве. Их единственное упоминание —
снимок tests/test_routes_inventory.py, где имя лежит СТРОКОЙ в эталоне троек: забытую строку
фасада он не видит вовсе, а `handler.__qualname__` у функции уровня модуля от переезда не
меняется, так что снимок остаётся зелёным при любом исходе.

Чего не ловит ни один из существующих приборов и что закрывается здесь:

  * ЗАБЫТАЯ СТРОКА ФАСАДА либо оставшаяся в webapp вторая копия. Импорт от этого не падает:
    ручки продолжают работать (их зовёт роутер напрямую из prices), а `webapp.<имя>` просто
    исчезает — и первым это замечает не тест, а код, который знает имя как webapp.*.
  * СДВИГ БЛОКА при переводе на `add_routes`: одна строка вместо семи уезжает по make_app
    незаметно, снимок при этом остаётся зелёным.
  * ПОТЕРЯННАЯ ПРИВЯЗКА ЧУЖОГО ПОМОЩНИКА. Модуль резолвит `_verify_admin`, `_admin_body`,
    `_bad`, `_num`, `_toint` из СВОИХ глобалов; заведись где-то вторая копия — подмены в
    тестах перестали бы доставать до блока МОЛЧА (положительный тест краснеет честным 403, а
    отрицательный «не пустили → 403» проходит сам собой, уже без всякой заглушки).
  * ПРИЧЁСАННОЕ РАДИ ОДНОРОДНОСТИ ПРАВО. Карта блока НЕОДНОРОДНА: шесть ручек — CAP_PRICING,
    калькулятор — CAP_FINANCE. Утверждать «все маршруты модуля = CAP_PRICING» тут нельзя, и
    именно поэтому карта заморожена поимённо (test_caps_of_the_block_are_frozen).

ОТДЕЛЬНО ПРО ПОДМЕНУ ПОДПИСИ. `_AUTH_MODULES` лечит только идиому `stub_auth(...)`. Прямой
`monkeypatch.setattr(webapp, "_verify_admin"|"_admin_body", ...)` — а так написано большинство
тестов набора — до app/web/prices.py НЕ достаёт и conftest от этого не спасает: три GET-ручки
читают `_verify_admin` из своих глобалов, четыре POST — свой `_admin_body`. Новый тест на эти
семь маршрутов писать ТОЛЬКО через stub_auth.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest
from conftest import _AUTH_MODULES, stub_auth

from app import roles, webapp, webauth
from app.web import common, prices

REPO = pathlib.Path(__file__).resolve().parents[1]

# Имена, которые app/webapp.py обязан реэкспортировать из app/web/prices.py. Список ведётся
# руками именно потому, что он и есть контракт фасада.
FACADE = ("_api_admin_discount", "_api_admin_discounts", "_api_admin_option",
          "_api_admin_options", "_api_admin_price_calc", "_api_admin_promo",
          "_api_admin_promos")

# Маршруты блока В ПОРЯДКЕ РЕГИСТРАЦИИ — снято с роутера до перевода на RouteTableDef.
ORDERED_ROUTES = [
    ("GET", "/api/admin/promos", "_api_admin_promos"),
    ("POST", "/api/admin/promo", "_api_admin_promo"),
    ("POST", "/api/admin/price_calc", "_api_admin_price_calc"),
    ("GET", "/api/admin/discounts", "_api_admin_discounts"),
    ("POST", "/api/admin/discount", "_api_admin_discount"),
    ("GET", "/api/admin/options", "_api_admin_options"),
    ("POST", "/api/admin/option", "_api_admin_option"),
]

# Соседи блока в make_app: что зарегистрировано непосредственно ДО и ПОСЛЕ него. Раньше семь
# строк блока стояли вперемешку с чужими (коллекция, настройки, бренд, лента, bigdata); при
# переводе на add_routes чужие собраны по одну сторону, и соседями стали эти двое.
NEIGHBOURS = (("POST", "/api/admin/self_print", "_api_admin_self_print"),
              ("GET", "/api/admin/collection", "_api_admin_collection"))

# Право каждого пути блока — ПОИМЁННО. Карта неоднородна намеренно: калькулятор считает деньги
# (CAP_FINANCE), остальные шесть правят прайс (CAP_PRICING). Экран «Цены и скидки» при этом
# открыт правом pricing — сотрудник с одним «Прайсом» получит на калькуляторе 403, и это
# ЖИВОЕ поведение, а не дефект переноса. «Причесать» право под имя модуля = сломать боевой
# доступ, поэтому карта заморожена здесь.
CAPS = {
    "/api/admin/promos": roles.CAP_PRICING,
    "/api/admin/promo": roles.CAP_PRICING,
    "/api/admin/price_calc": roles.CAP_FINANCE,
    "/api/admin/discounts": roles.CAP_PRICING,
    "/api/admin/discount": roles.CAP_PRICING,
    "/api/admin/options": roles.CAP_PRICING,
    "/api/admin/option": roles.CAP_PRICING,
}


def _live_routes():
    """(метод, канонический путь, имя обработчика) из РОУТЕРА, в порядке регистрации."""
    return [(r.method, r.resource.canonical, r.handler.__qualname__)
            for r in webapp.make_app().router.routes()
            if r.method != "HEAD" and r.resource is not None]


# ─────────────────────────────── фасад ───────────────────────────────

@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.prices.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(prices, name), (
        f"webapp.{name} — не тот объект, что prices.{name}: либо в webapp осталась вторая "
        "копия, либо фасад импортирует что-то другое")


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/prices.py обязана появиться и в списке выше."""
    src = io.open(REPO / "app" / "web" / "prices.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён.

    Вторая копия не ломает ни импорт, ни тесты, ходящие через webapp.*: маршрут при этом
    обслуживает функция из prices, а правку разработчик вносит в оставшуюся в webapp."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_prices_does_not_import_webapp():
    """app/web/prices.py не тянет webapp — иначе разрез не разорвал цикл, а спрятал его.

    Отдельный процесс: в текущем webapp уже импортирован, и sys.modules ничего не докажет."""
    code = "import app.web.prices, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.prices подтянул webapp: {out}"


# ─────────────────────────── таблица маршрутов ───────────────────────────

def test_route_table_registers_in_the_frozen_order():
    """Блок стоит в роутере тем же куском и в том же порядке, что до перевода на add_routes."""
    live = _live_routes()
    got = [r for r in live if r[1] in {p for _, p, _ in ORDERED_ROUTES}]
    assert got == ORDERED_ROUTES


def test_route_table_stays_one_contiguous_block_in_place():
    """`app.add_routes(prices.routes)` стоит там же, где стояли строки add_*.

    Одна строка вместо семи уезжает по make_app незаметно, а снимок маршрутов сравнивает
    множества и места в очереди не видит вовсе."""
    live = _live_routes()
    paths = {p for _, p, _ in ORDERED_ROUTES}
    at = [i for i, r in enumerate(live) if r[1] in paths]
    assert at == list(range(at[0], at[-1] + 1)), "блок разорван: маршруты блока не подряд"
    assert (live[at[0] - 1], live[at[-1] + 1]) == NEIGHBOURS, "блок переехал по make_app"


def test_route_table_is_the_only_source_of_prices_routes():
    """Все маршруты блока приходят ИЗ таблицы модуля — ни одного мимо неё."""
    table = [(rd.method, rd.path, rd.handler.__qualname__) for rd in prices.routes]
    assert table == ORDERED_ROUTES


def test_caps_of_the_block_are_frozen():
    """Право каждого пути блока — то же, что до переезда, включая ЧУЖОЕ у калькулятора.

    Право берётся по канонической строке пути (webguard._route_cap) и от модуля обработчика не
    зависит — но именно поэтому его легко «причесать» под имя модуля, не сломав ни один тест."""
    for path, cap in CAPS.items():
        assert webapp._route_cap(path) == cap, f"право маршрута {path} уехало"


# ─────────────────── чужие помощники: один объект, а не копия ───────────────────

@pytest.mark.parametrize(("name", "home"), [
    ("_verify_admin", webauth), ("_admin_body", webauth), ("_admin_forbidden", webauth),
    ("_bad", common), ("_num", common), ("_toint", common),
])
def test_external_helpers_are_bound_to_their_real_home(name, home):
    """Помощник в prices — ТОТ ЖЕ объект, что в своём модуле, и тот же, что видит webapp.

    Вторая копия здесь отказывает молча: подмена в тестах ставится на дом помощника, а блок
    продолжает звать оригинал (или наоборот)."""
    assert getattr(prices, name) is getattr(home, name)
    assert getattr(prices, name) is getattr(webapp, name)


def test_stub_auth_reaches_the_new_module(monkeypatch):
    """Общий помощник подмены знает про app/web/prices.py.

    Забыть эту строку — типовая ошибка разреза: три GET-ручки блока читают `_verify_admin` из
    своих глобалов, подмена в webapp до них не достаёт, и отрицательные тесты («постороннему
    403») начинают проходить сами собой — уже настоящей проверкой на фальшивом initData.
    Проверяем ОБА имени: четыре POST-ручки из семи идут через `_admin_body`, и он остаётся
    непокрытым, если кто-то вынет prices из кортежа, понадеявшись на запись webauth."""
    assert prices in _AUTH_MODULES, "app/web/prices.py не добавлен в conftest._AUTH_MODULES"
    stub_auth(monkeypatch, _verify_admin=4242)
    assert prices._verify_admin("что угодно") == 4242
    assert webauth._verify_admin("что угодно") == 4242
    stub_auth(monkeypatch, _admin_body=lambda _b: (4242, None))
    assert prices._admin_body({}) == (4242, None)
