"""Заказ на верстаке после выноса в app/web/orderbench.py: фасад, таблица маршрутов, помощники, права.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна «Заказ на верстаке» цикла 2 увезла из app/webapp.py первый честный кусок
самого большого домена файла: четыре ручки, которыми мастер работает у станка с ФАЙЛОМ и
МАТЕРИАЛОМ заказа (сменить пластик/цвет, слайснуть модель по ссылке, открыть в слайсере, открыть
папку на ПК бота) плюс их эксклюзивный помощник `_printer_filament_names`. В webapp остался
построчный ФАСАД, а регистрация переехала со списка `router.add_*` в make_app на таблицу
`web.RouteTableDef()` рядом с обработчиками.

Прибор здесь единственный сторож для ТРЁХ имён из пяти: у `_api_admin_order_material`,
`_api_admin_order_open_folder` и `_printer_filament_names` во всём дереве нет ни одного прямого
обращения `webapp.<имя>` и ни одного monkeypatch. Их единственное упоминание — снимок
tests/test_routes_inventory.py, где имя лежит СТРОКОЙ в эталоне троек: забытую строку фасада он
не видит вовсе, а `handler.__qualname__` у функции уровня модуля от переезда не меняется, так
что снимок остаётся зелёным при любом исходе.

Чего не ловит ни один из существующих приборов и что закрывается здесь:

  * ЗАБЫТАЯ СТРОКА ФАСАДА либо оставшаяся в webapp вторая копия. Импорт от этого не падает:
    ручки продолжают работать (их зовёт роутер напрямую из orderbench), а `webapp.<имя>` просто
    исчезает — и первым это замечает не тест, а код, который знает имя как webapp.*.
  * СДВИГ БЛОКА при переводе на `add_routes`: одна строка вместо четырёх уезжает по make_app
    незаметно, снимок при этом остаётся зелёным.
  * ПОТЕРЯННАЯ ПРИВЯЗКА ЧУЖОГО ПОМОЩНИКА. Модуль резолвит `_admin_body`, `_bad`, `_toint` из
    СВОИХ глобалов; заведись где-то вторая копия — подмены в тестах перестали бы доставать до
    блока МОЛЧА (положительный тест краснеет честным 403, а отрицательный «не пустили → 403»
    проходит сам собой, уже без всякой заглушки).
  * ПРИЕХАВШЕЕ СЛЕДОМ МОДУЛЬНОЕ СОСТОЯНИЕ. Партия взята именно потому, что не касается ни
    `_bot`, ни `_storage`, ни `_models`, ни `_active_print`. Следующая волна домена соблазнится
    дотащить сюда соседа (`_api_admin_order_print_net` стоял ВНУТРИ блока и пишет `_active_print`
    через `_set_active_print`) — и «богатая шапка» экрана принтера погаснет для сетевой печати
    молча, без строки в логе. Держатель бота в модуле морозится отдельной проверкой.

ОТДЕЛЬНО ПРО ПОДМЕНУ ПОДПИСИ. `_AUTH_MODULES` лечит только идиому `stub_auth(...)`. Прямой
`monkeypatch.setattr(webapp, "_admin_body", ...)` — а так написано большинство тестов набора,
23 сайта в семи файлах — до app/web/orderbench.py НЕ достаёт, и conftest от этого не спасает:
все четыре ручки читают подпись ТОЛЬКО через свой `_admin_body`. Новый тест на эти четыре
маршрута писать ТОЛЬКО через stub_auth.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest
from conftest import _AUTH_MODULES, stub_auth

from app import roles, webapp, webauth
from app.web import common, orderbench

REPO = pathlib.Path(__file__).resolve().parents[1]

# Имена, которые app/webapp.py обязан реэкспортировать из app/web/orderbench.py. Список ведётся
# руками именно потому, что он и есть контракт фасада. `_printer_filament_names` внешних
# обращений не имеет вовсе, но реэкспортируется по прецеденту calib (там же поимённо уехали
# помощники экрана): цена лишней строки — ноль, цена забытой — отказ у живого клиента.
FACADE = ("_api_admin_order_material", "_api_admin_order_open_folder",
          "_api_admin_order_open_slicer", "_api_admin_order_slice",
          "_printer_filament_names")

# Маршруты блока В ПОРЯДКЕ РЕГИСТРАЦИИ — снято с роутера ПОСЛЕ перевода на RouteTableDef, но
# порядок повторяет прежние строки add_post в make_app (material → slice → open_slicer →
# open_folder), между которыми раньше стояли чужие ручки денег и переписки.
ORDERED_ROUTES = [
    ("POST", "/api/admin/order/material", "_api_admin_order_material"),
    ("POST", "/api/admin/order/slice", "_api_admin_order_slice"),
    ("POST", "/api/admin/order/open_slicer", "_api_admin_order_open_slicer"),
    ("POST", "/api/admin/order/open_folder", "_api_admin_order_open_folder"),
]

# Соседи блока в make_app: что зарегистрировано непосредственно ДО и ПОСЛЕ него. Раньше четыре
# строки блока стояли вперемешку с чужими (цена, пересчёт, счёт, создание заказа, баланс,
# переписка, сетевая печать); после сборки в один кусок соседями стали эти двое.
NEIGHBOURS = (("POST", "/api/admin/order/status", "_api_admin_order_status"),
              ("POST", "/api/admin/order/price", "_api_admin_order_price"))

# Право каждого пути блока — ПОИМЁННО. Карта однородна, и это не совпадение: все четыре пути
# названы CAP_ORDERS ЯВНОЙ строкой в app/webguard.py::_OPERATOR_ROUTES, то есть верстак открыт
# наёмному оператору намеренно (производственный контур без денег). Право берётся по
# канонической строке пути и от модуля обработчика не зависит — но именно поэтому его легко
# молча ужесточить до «закрыто по умолчанию», отобрав у оператора работу у станка.
CAPS = {
    "/api/admin/order/material": roles.CAP_ORDERS,
    "/api/admin/order/slice": roles.CAP_ORDERS,
    "/api/admin/order/open_slicer": roles.CAP_ORDERS,
    "/api/admin/order/open_folder": roles.CAP_ORDERS,
}


def _live_routes():
    """(метод, канонический путь, имя обработчика) из РОУТЕРА, в порядке регистрации."""
    return [(r.method, r.resource.canonical, r.handler.__qualname__)
            for r in webapp.make_app().router.routes()
            if r.method != "HEAD" and r.resource is not None]


# ─────────────────────────────── фасад ───────────────────────────────

@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.orderbench.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(orderbench, name), (
        f"webapp.{name} — не тот объект, что orderbench.{name}: либо в webapp осталась вторая "
        "копия, либо фасад импортирует что-то другое")


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/orderbench.py обязана появиться и в списке выше."""
    src = io.open(REPO / "app" / "web" / "orderbench.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён.

    Вторая копия не ломает ни импорт, ни тесты, ходящие через webapp.*: маршрут при этом
    обслуживает функция из orderbench, а правку разработчик вносит в оставшуюся в webapp."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_orderbench_does_not_import_webapp():
    """app/web/orderbench.py не тянет webapp — иначе разрез не разорвал цикл, а спрятал его.

    Отдельный процесс: в текущем webapp уже импортирован, и sys.modules ничего не докажет."""
    code = "import app.web.orderbench, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.orderbench подтянул webapp: {out}"


def test_module_holds_no_bot_and_no_printer_state():
    """Модуль НЕ заводит держателя бота и НЕ уносит состояние принтерного блока.

    Партия взята первым этапом домена ровно потому, что от модульного состояния webapp чиста.
    Заведись здесь `_bot`/`set_bot` — run.wire_bot() их не проводит (см. run.py и
    tests/test_bot_wiring.py), и доставка сообщений молча выключилась бы; утащи сюда следующая
    волна `_set_active_print`/`_active_print` — писалось бы в чужой словарь, и «богатая шапка»
    экрана принтера погасла бы для сетевой печати без единой строки в логе."""
    for name in ("_bot", "set_bot", "_storage", "_models", "_active_print",
                 "_set_active_print", "_active_print_key"):
        assert not hasattr(orderbench, name), (
            f"в app/web/orderbench.py появилось {name} — это модульное состояние webapp, "
            "и его перенос требует ОТДЕЛЬНОГО решения (образец app/printmedia.py + run.wire_bot)")


# ─────────────────────────── таблица маршрутов ───────────────────────────

def test_route_table_registers_in_the_frozen_order():
    """Блок стоит в роутере тем же куском и в том же порядке, что до перевода на add_routes."""
    live = _live_routes()
    got = [r for r in live if r[1] in {p for _, p, _ in ORDERED_ROUTES}]
    assert got == ORDERED_ROUTES


def test_route_table_stays_one_contiguous_block_in_place():
    """`app.add_routes(orderbench.routes)` стоит там же, где стояли строки add_*.

    Одна строка вместо четырёх уезжает по make_app незаметно, а снимок маршрутов сравнивает
    множества и места в очереди не видит вовсе."""
    live = _live_routes()
    paths = {p for _, p, _ in ORDERED_ROUTES}
    at = [i for i, r in enumerate(live) if r[1] in paths]
    assert at == list(range(at[0], at[-1] + 1)), "блок разорван: маршруты блока не подряд"
    assert (live[at[0] - 1], live[at[-1] + 1]) == NEIGHBOURS, "блок переехал по make_app"


def test_route_table_is_the_only_source_of_orderbench_routes():
    """Все маршруты блока приходят ИЗ таблицы модуля — ни одного мимо неё."""
    table = [(rd.method, rd.path, rd.handler.__qualname__) for rd in orderbench.routes]
    assert table == ORDERED_ROUTES


def test_caps_of_the_block_are_frozen():
    """Право каждого пути блока — то же, что до переезда: CAP_ORDERS явной строкой webguard.

    Право берётся по канонической строке пути (webguard._route_cap) и от модуля обработчика не
    зависит — но именно поэтому его легко потерять при «уборке» карты: выпав из
    _OPERATOR_ROUTES, путь молча уедет в «закрыто по умолчанию» и наёмный оператор перестанет
    менять материал заказа и открывать его файл."""
    for path, cap in CAPS.items():
        assert webapp._route_cap(path) == cap, f"право маршрута {path} уехало"


# ─────────────────── чужие помощники: один объект, а не копия ───────────────────

@pytest.mark.parametrize(("name", "home"), [
    ("_admin_body", webauth), ("_bad", common), ("_toint", common),
])
def test_external_helpers_are_bound_to_their_real_home(name, home):
    """Помощник в orderbench — ТОТ ЖЕ объект, что в своём модуле, и тот же, что видит webapp.

    Вторая копия здесь отказывает молча: подмена в тестах ставится на дом помощника, а блок
    продолжает звать оригинал (или наоборот)."""
    assert getattr(orderbench, name) is getattr(home, name)
    assert getattr(orderbench, name) is getattr(webapp, name)


def test_stub_auth_reaches_the_new_module(monkeypatch):
    """Общий помощник подмены знает про app/web/orderbench.py.

    Забыть эту строку — типовая ошибка разреза. Здесь она особенно тихая: `_verify_admin` блок
    не импортирует вовсе, всё держится на `_admin_body`, а он связан в глобалах модуля. Пока
    существующие тесты (tests/test_admin_slice.py, tests/test_webapp_helpers.py) идут через
    stub_auth и кованую подпись, отказ не виден — он вылезет на первом новом тесте."""
    assert orderbench in _AUTH_MODULES, (
        "app/web/orderbench.py не добавлен в conftest._AUTH_MODULES")
    stub_auth(monkeypatch, _admin_body=lambda _b: (4242, None))
    assert orderbench._admin_body({}) == (4242, None)
    assert webauth._admin_body({}) == (4242, None)
