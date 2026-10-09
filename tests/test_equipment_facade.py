"""Оборудование после выноса в app/web/equipment.py: фасад, таблица маршрутов, помощники.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна «Оборудование» цикла 2 увезла из app/webapp.py четыре ручки — пресеты
качества и прочности плюс карточки принтеров с соплами, протоколом связи и выбором слайсера, —
оставив в webapp построчный ФАСАД, а регистрацию переведя со списка `router.add_*` в make_app
на таблицу `web.RouteTableDef()` рядом с обработчиками.

Прибор здесь нужен острее, чем у прошлых разрезов: у ТРЁХ ручек из четырёх собственных тестов
нет вовсе. Прямые обращения `webapp.<имя>` есть только у `_api_admin_preset`
(tests/test_webapp_helpers.py), по путям блока через make_app не ходит никто, а снимок
(tests/test_routes_inventory.py) сверяет МНОЖЕСТВА троек и карту прав — фасада он не видит, и
про порядок регистрации ему ничего не известно.

Чего не ловит ни один из существующих приборов и что закрывается здесь:

  * ЗАБЫТАЯ СТРОКА ФАСАДА либо оставшаяся в webapp вторая копия. Импорт от этого не падает:
    ручки продолжают работать (их зовёт роутер напрямую из equipment), а `webapp.<имя>` просто
    исчезает — и первым это замечает не тест, а код, который знает имя как webapp.*.
  * СДВИГ БЛОКА при переводе на `add_routes`: одна строка вместо четырёх уезжает по make_app
    незаметно, снимок при этом остаётся зелёным.
  * ПОТЕРЯННАЯ ПРИВЯЗКА ЧУЖОГО ПОМОЩНИКА. Модуль резолвит `_verify_admin`, `_admin_body`,
    `_bad`, `_num`, `_toint` из СВОИХ глобалов; заведись где-то вторая копия — подмены в
    тестах перестали бы доставать до блока МОЛЧА (положительный тест краснеет честным 403, а
    отрицательный «не пустили → 403» проходит сам собой, уже без всякой заглушки).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest
from conftest import _AUTH_MODULES, stub_auth

from app import webapp, webauth
from app.web import common, equipment

REPO = pathlib.Path(__file__).resolve().parents[1]

# Имена, которые app/webapp.py обязан реэкспортировать из app/web/equipment.py. Список ведётся
# руками именно потому, что он и есть контракт фасада.
FACADE = ("_api_admin_preset", "_api_admin_presets", "_api_admin_printer", "_api_admin_printers")

# Маршруты блока В ПОРЯДКЕ РЕГИСТРАЦИИ — снято с роутера до перевода на RouteTableDef.
ORDERED_ROUTES = [
    ("GET", "/api/admin/presets", "_api_admin_presets"),
    ("POST", "/api/admin/preset", "_api_admin_preset"),
    ("GET", "/api/admin/printers", "_api_admin_printers"),
    ("POST", "/api/admin/printer", "_api_admin_printer"),
]

# Соседи блока в make_app: что зарегистрировано непосредственно ДО и ПОСЛЕ него. Между
# пресетами и принтерами раньше стояла ЧУЖАЯ строка `/api/admin/feature` — при переносе она
# осталась в make_app и оказалась сразу за блоком.
NEIGHBOURS = (("POST", "/api/admin/shade", "_api_admin_shade"),
              ("POST", "/api/admin/feature", "_api_admin_feature"))


def _live_routes():
    """(метод, канонический путь, имя обработчика) из РОУТЕРА, в порядке регистрации."""
    return [(r.method, r.resource.canonical, r.handler.__qualname__)
            for r in webapp.make_app().router.routes()
            if r.method != "HEAD" and r.resource is not None]


# ─────────────────────────────── фасад ───────────────────────────────

@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.equipment.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(equipment, name), (
        f"webapp.{name} — не тот объект, что equipment.{name}: либо в webapp осталась вторая "
        "копия, либо фасад импортирует что-то другое")


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/equipment.py обязана появиться и в списке выше."""
    src = io.open(REPO / "app" / "web" / "equipment.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён.

    Вторая копия не ломает ни импорт, ни тесты, ходящие через webapp.*: маршрут при этом
    обслуживает функция из equipment, а правку разработчик вносит в оставшуюся в webapp."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_equipment_does_not_import_webapp():
    """app/web/equipment.py не тянет webapp — иначе разрез не разорвал цикл, а спрятал его.

    Отдельный процесс: в текущем webapp уже импортирован, и sys.modules ничего не докажет."""
    code = "import app.web.equipment, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.equipment подтянул webapp: {out}"


# ─────────────────────────── таблица маршрутов ───────────────────────────

def test_route_table_registers_in_the_frozen_order():
    """Блок стоит в роутере тем же куском и в том же порядке, что до перевода на add_routes."""
    live = _live_routes()
    got = [r for r in live if r[1] in {p for _, p, _ in ORDERED_ROUTES}]
    assert got == ORDERED_ROUTES


def test_route_table_stays_one_contiguous_block_in_place():
    """`app.add_routes(equipment.routes)` стоит там же, где стояли строки add_*.

    Одна строка вместо четырёх уезжает по make_app незаметно, а снимок маршрутов сравнивает
    множества и места в очереди не видит вовсе."""
    live = _live_routes()
    paths = {p for _, p, _ in ORDERED_ROUTES}
    at = [i for i, r in enumerate(live) if r[1] in paths]
    assert at == list(range(at[0], at[-1] + 1)), "блок разорван: маршруты блока не подряд"
    assert (live[at[0] - 1], live[at[-1] + 1]) == NEIGHBOURS, "блок переехал по make_app"


def test_route_table_is_the_only_source_of_equipment_routes():
    """Все маршруты блока приходят ИЗ таблицы модуля — ни одного мимо неё."""
    table = [(rd.method, rd.path, rd.handler.__qualname__) for rd in equipment.routes]
    assert table == ORDERED_ROUTES


# ─────────────────── чужие помощники: один объект, а не копия ───────────────────

@pytest.mark.parametrize(("name", "home"), [
    ("_verify_admin", webauth), ("_admin_body", webauth), ("_admin_forbidden", webauth),
    ("_bad", common), ("_num", common), ("_toint", common),
])
def test_external_helpers_are_bound_to_their_real_home(name, home):
    """Помощник в equipment — ТОТ ЖЕ объект, что в своём модуле, и тот же, что видит webapp.

    Вторая копия здесь отказывает молча: подмена в тестах ставится на дом помощника, а блок
    продолжает звать оригинал (или наоборот)."""
    assert getattr(equipment, name) is getattr(home, name)
    assert getattr(equipment, name) is getattr(webapp, name)


def test_stub_auth_reaches_the_new_module(monkeypatch):
    """Общий помощник подмены знает про app/web/equipment.py.

    Забыть эту строку — типовая ошибка разреза: ручки блока читают проверку из своих
    глобалов, подмена в webapp до них не достаёт, и отрицательные тесты («постороннему 403»)
    начинают проходить сами собой — уже настоящей проверкой на фальшивом initData."""
    assert equipment in _AUTH_MODULES, "app/web/equipment.py не добавлен в conftest._AUTH_MODULES"
    stub_auth(monkeypatch, _verify_admin=4242)
    assert equipment._verify_admin("что угодно") == 4242
    assert webauth._verify_admin("что угодно") == 4242
