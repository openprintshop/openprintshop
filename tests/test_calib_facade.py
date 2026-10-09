"""Калибровка после выноса в app/web/calib.py: фасад, таблица маршрутов, чужие помощники.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна 4 цикла 2 увезла блок калибровки из app/webapp.py целиком — 24 ручки
админки, страницу образца по QR и доменные помощники экрана, — оставив в webapp построчный
ФАСАД, а регистрацию маршрутов переведя со списка `router.add_*` в make_app на таблицу
`web.RouteTableDef()` рядом с обработчиками. Оба уже существующих прибора этот шаг проходят,
но каждый видит только свою половину:

  * снимок троек и снимок карты прав (tests/test_routes_inventory.py) сравнивают МНОЖЕСТВА;
    порядок регистрации им не виден вовсе, а у динамического `/csample/{sid}` порядок — это
    поведение роутера, и `add_routes` в другом месте make_app сдвинул бы весь блок разом;
  * сторож регистрации (tests/test_calib_api.py::test_calib_routes_stay_registered) сверяет
    словарь путь→обработчик, то есть тоже без порядка, и ничего не знает о фасаде.

Чего не ловит ни один из них и что закрывается здесь:

  * ЗАБЫТАЯ СТРОКА ФАСАДА либо оставшаяся в webapp вторая копия. Импорт от этого не падает:
    ручки продолжают работать (их зовёт роутер напрямую из calib), а `webapp._csample_page`
    просто исчезает — и первым это замечает не тест, а код, который знает имя как webapp.*.
  * СДВИГ БЛОКА при переводе на `add_routes`: одна строка вместо двадцати пяти легко уезжает
    в другое место make_app, и снимок остаётся зелёным.
  * ПОТЕРЯННАЯ ПРИВЯЗКА ЧУЖОГО ПОМОЩНИКА. Модуль резолвит `_verify_admin`, `_photo_url`,
    `_heal_journal_timelapse` из СВОИХ глобалов; заведись где-то вторая копия — подмены в
    тестах перестали бы доставать до калибровки МОЛЧА (положительный тест краснеет честным
    403, а отрицательный «не пустили → 403» проходит сам собой, уже без всякой заглушки).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest
from conftest import _AUTH_MODULES, stub_auth

from app import printmedia, webapp, webauth
from app.web import calib, common

REPO = pathlib.Path(__file__).resolve().parents[1]

# Имена, которые app/webapp.py обязан реэкспортировать из app/web/calib.py. Список ведётся
# руками именно потому, что он и есть контракт фасада: 24 обработчика ручек, страница образца
# и семь доменных помощников экрана.
FACADE = (
    "_api_admin_calib", "_api_admin_calib_backfill", "_api_admin_calib_drift",
    "_api_admin_calib_export", "_api_admin_calib_labels", "_api_admin_calib_link",
    "_api_admin_calib_measure_add", "_api_admin_calib_measure_delete",
    "_api_admin_calib_passport_apply", "_api_admin_calib_passport_delete",
    "_api_admin_calib_passport_save", "_api_admin_calib_photo", "_api_admin_calib_plate_save",
    "_api_admin_calib_preset_history", "_api_admin_calib_preset_restore",
    "_api_admin_calib_presets", "_api_admin_calib_presets_snapshot", "_api_admin_calib_run",
    "_api_admin_calib_run_delete", "_api_admin_calib_run_save", "_api_admin_calib_sample_delete",
    "_api_admin_calib_sample_save", "_api_admin_calib_step_apply", "_api_admin_calib_step_save",
    "_calib_journal_view", "_calib_params", "_calib_preset_path", "_calib_refresh_passport",
    "_calib_step_view", "_calib_test_dicts", "_csample_page", "_sample_url",
)

# Маршруты блока В ПОРЯДКЕ РЕГИСТРАЦИИ — снято с роутера до перевода на RouteTableDef.
# Порядок здесь не украшение: aiohttp разрешает динамические ресурсы перебором в порядке
# добавления, а `/csample/{sid}` — как раз динамический.
ORDERED_ROUTES = [
    ("GET", "/api/admin/calib", "_api_admin_calib"),
    ("GET", "/api/admin/calib/run", "_api_admin_calib_run"),
    ("POST", "/api/admin/calib/run_save", "_api_admin_calib_run_save"),
    ("POST", "/api/admin/calib/run_delete", "_api_admin_calib_run_delete"),
    ("POST", "/api/admin/calib/step_save", "_api_admin_calib_step_save"),
    ("POST", "/api/admin/calib/step_apply", "_api_admin_calib_step_apply"),
    ("POST", "/api/admin/calib/measure_add", "_api_admin_calib_measure_add"),
    ("POST", "/api/admin/calib/measure_delete", "_api_admin_calib_measure_delete"),
    ("POST", "/api/admin/calib/sample_save", "_api_admin_calib_sample_save"),
    ("POST", "/api/admin/calib/sample_delete", "_api_admin_calib_sample_delete"),
    ("GET", "/api/admin/calib/presets", "_api_admin_calib_presets"),
    ("POST", "/api/admin/calib/presets_snapshot", "_api_admin_calib_presets_snapshot"),
    ("GET", "/api/admin/calib/preset_history", "_api_admin_calib_preset_history"),
    ("POST", "/api/admin/calib/preset_restore", "_api_admin_calib_preset_restore"),
    ("POST", "/api/admin/calib/passport_save", "_api_admin_calib_passport_save"),
    ("POST", "/api/admin/calib/passport_apply", "_api_admin_calib_passport_apply"),
    ("POST", "/api/admin/calib/passport_delete", "_api_admin_calib_passport_delete"),
    ("POST", "/api/admin/calib/plate_save", "_api_admin_calib_plate_save"),
    ("POST", "/api/admin/calib/photo", "_api_admin_calib_photo"),
    ("GET", "/api/admin/calib/drift", "_api_admin_calib_drift"),
    ("POST", "/api/admin/calib/backfill", "_api_admin_calib_backfill"),
    ("POST", "/api/admin/calib/link", "_api_admin_calib_link"),
    ("GET", "/api/admin/calib/export", "_api_admin_calib_export"),
    ("GET", "/api/admin/calib/labels", "_api_admin_calib_labels"),
    ("GET", "/csample/{sid}", "_csample_page"),
]

# Соседи блока в make_app: что зарегистрировано непосредственно ДО и ПОСЛЕ него.
NEIGHBOURS = (("POST", "/api/admin/order_media_delete", "_api_admin_order_media_delete"),
              ("POST", "/api/family_request", "_api_family_request"))


def _live_routes():
    """(метод, канонический путь, имя обработчика) из РОУТЕРА, в порядке регистрации."""
    return [(r.method, r.resource.canonical, r.handler.__qualname__)
            for r in webapp.make_app().router.routes()
            if r.method != "HEAD" and r.resource is not None]


# ─────────────────────────────── фасад ───────────────────────────────

@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.calib.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(calib, name), (
        f"webapp.{name} — не тот объект, что calib.{name}: либо в webapp осталась вторая копия, "
        "либо фасад импортирует что-то другое")


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/calib.py обязана появиться и в списке выше.

    Иначе следующая ручка блока заведётся мимо реэкспорта, и обнаружится это не здесь."""
    src = io.open(REPO / "app" / "web" / "calib.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён.

    Вторая копия не ломает ни импорт, ни тесты, ходящие через webapp.*: маршрут при этом
    обслуживает функция из calib, а правку разработчик вносит в оставшуюся в webapp."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_calib_does_not_import_webapp():
    """app/web/calib.py не тянет webapp — иначе разрез не разорвал цикл, а спрятал его.

    Отдельный процесс: в текущем webapp уже импортирован, и sys.modules ничего не докажет."""
    code = ("import app.web.calib, sys; print(int('app.webapp' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.calib подтянул webapp: {out}"


# ─────────────────────────── таблица маршрутов ───────────────────────────

def test_route_table_registers_in_the_frozen_order():
    """Блок стоит в роутере тем же куском и в том же порядке, что до перевода на add_routes."""
    live = _live_routes()
    got = [r for r in live if r[1].startswith("/api/admin/calib") or r[1] == "/csample/{sid}"]
    assert got == ORDERED_ROUTES


def test_route_table_stays_one_contiguous_block_in_place():
    """`app.add_routes(calib.routes)` стоит там же, где стояли двадцать пять строк add_*.

    Одна строка вместо двадцати пяти уезжает по make_app незаметно, а для динамического
    `/csample/{sid}` место в очереди разрешения — это поведение, а не оформление."""
    live = _live_routes()
    at = [i for i, r in enumerate(live)
          if r[1].startswith("/api/admin/calib") or r[1] == "/csample/{sid}"]
    assert at == list(range(at[0], at[-1] + 1)), "блок разорван: маршруты калибровки не подряд"
    assert (live[at[0] - 1], live[at[-1] + 1]) == NEIGHBOURS, "блок переехал по make_app"


def test_route_table_is_the_only_source_of_calib_routes():
    """Все маршруты блока приходят ИЗ таблицы модуля — ни одного мимо неё.

    Ручка, дописанная в make_app старым `router.add_*`, уедет при следующем разрезе, а
    ручка, объявленная в таблице, но не подключённая, просто не ответит клиенту."""
    table = [(rd.method, rd.path, rd.handler.__qualname__) for rd in calib.routes]
    assert table == ORDERED_ROUTES


# ─────────────────── чужие помощники: один объект, а не копия ───────────────────

@pytest.mark.parametrize(("name", "home"), [
    ("_verify_admin", webauth), ("_admin_body", webauth), ("_admin_forbidden", webauth),
    ("_heal_journal_timelapse", printmedia),
    ("_photo_url", common), ("_print_settings_view", common),
    ("_toint", common), ("_fnum", common), ("_num", common), ("_bad", common),
    ("_ago", common), ("check_sig", common), ("sig", common), ("signed_url", common),
])
def test_external_helpers_are_bound_to_their_real_home(name, home):
    """Помощник в calib — ТОТ ЖЕ объект, что в своём модуле, и тот же, что видит webapp.

    Вторая копия здесь отказывает молча: подмена в тестах ставится на дом помощника, а
    калибровка продолжает звать оригинал (или наоборот)."""
    assert getattr(calib, name) is getattr(home, name)
    assert getattr(calib, name) is getattr(webapp, name)


def test_stub_auth_reaches_the_new_module(monkeypatch):
    """Общий помощник подмены знает про app/web/calib.py.

    Забыть эту строку — типовая ошибка разреза: ручки блока читают проверку из своих
    глобалов, подмена в webapp до них не достаёт, и отрицательные тесты («постороннему 403»)
    начинают проходить сами собой — уже настоящей проверкой на фальшивом initData."""
    assert calib in _AUTH_MODULES, "app/web/calib.py не добавлен в conftest._AUTH_MODULES"
    stub_auth(monkeypatch, _verify_admin=4242)
    assert calib._verify_admin("что угодно") == 4242
    assert webauth._verify_admin("что угодно") == 4242
