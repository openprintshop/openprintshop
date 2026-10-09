"""«Мастерская органайзеров» после выноса в app/web/gridfinity.py: фасад, таблица, помощники.

ЗАЧЕМ ЭТОТ ФАЙЛ. Конфигуратор въехал в проект одним экраном и жил в витрине
(app/web/storefront.py) двенадцатью функциями. Разросшись до проектов, спецификации
деталей и частичного заказа, он занял бы в витрине больше места, чем сама витрина, —
и уехал в свой модуль с собственной таблицей маршрутов.

Что закрывается здесь и не ловится больше нигде:

  * ЗАБЫТАЯ СТРОКА ФАСАДА либо оставшаяся в webapp вторая копия. Импорт от этого не падает:
    ручки продолжают работать (их зовёт роутер напрямую из модуля), а `webapp._gf_summary`
    просто исчезает — и первым это замечает не тест, а код, который знает имя как webapp.*.
  * ПОТЕРЯННАЯ ПРИВЯЗКА ЧУЖОГО ПОМОЩНИКА: модуль резолвит `_verify_admin`/`_toint`/`_bad`
    из СВОИХ глобалов, и вторая копия отказывала бы молча — отрицательный тест «постороннему
    403» проходил бы сам собой, уже без всякой заглушки.
  * ПОТЕРЯННЫЙ БОТ: у модуля свой `_bot` (он шлёт и мастеру, и клиенту), и раздаёт его
    только webapp.set_bot веером _BOT_FAN. Забыть строку веера — значит молча выключить
    все уведомления мастерской.
  * УЕХАВШИЙ МАРШРУТ: ручка, дописанная мимо таблицы модуля, потеряется при следующем
    переносе; объявленная в таблице, но не подключённая — просто не ответит клиенту.

Карточка конфигуратора в ленте (`_GF_FEED_SHOWN`) сознательно ОСТАЛАСЬ в storefront: это
логика ленты, а не конфигуратора, и её сторожит tests/test_recommend.py.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest
from conftest import _AUTH_MODULES, _BOT_MODULES, stub_auth

from app import webapp, webauth
from app.web import common, gridfinity, orderdefaults, storefront

REPO = pathlib.Path(__file__).resolve().parents[1]

# Имена, которые app/webapp.py обязан реэкспортировать из app/web/gridfinity.py. Список
# ведётся руками именно потому, что он и есть контракт фасада.
FACADE = (
    # старый контур (совместимость со старым мини-аппом)
    "_api_gridfinity_config", "_api_gridfinity_config_set", "_api_gridfinity_order",
    "_api_admin_gridfinity_config",
    # проекты
    "_api_gf_projects", "_api_gf_project", "_api_gf_project_add", "_api_gf_project_save",
    "_api_gf_project_patch",
    # спецификация и партии
    "_api_gf_part_patch", "_api_gf_parts_select", "_api_gf_preflight", "_api_gf_order",
    # мерки, заготовки, шаблон, ссылка «показать»
    "_api_gf_measure_add", "_api_gf_presets", "_api_gf_preset_add", "_api_gf_template",
    "_gf_share_page", "_gf_share_html",
    # сессия «продолжить на компьютере» (ссылка на правку ОДНОГО проекта без Telegram)
    "_gf_actor", "_gf_denied", "_gf_uname",
    "_gf_desk_page", "_gf_desk_html", "_gf_desk_info", "_gf_desk_notify", "_gf_desk_url",
    "_gf_script",
    # админские
    "_api_admin_gf_part_printed", "_api_admin_gf_release",
    # домен: формулы, сверка, оценка
    "_gf_auto_name", "_gf_batch_summary", "_gf_batches", "_gf_beds", "_gf_body",
    "_gf_cfg_of", "_gf_color_need", "_gf_counts", "_gf_dims", "_gf_discount", "_gf_dump",
    "_gf_estimate",
    "_gf_f", "_gf_filament", "_gf_free", "_gf_frozen", "_gf_grid", "_gf_head_lines", "_gf_i", "_gf_inner_h",
    "_gf_live_state", "_gf_migrate_draft", "_gf_money", "_gf_minutes", "_gf_mod_line", "_gf_modules",
    "_gf_notify_change",
    "_gf_notify_order", "_gf_oversize", "_gf_owned", "_gf_part_view",
    "_gf_parts_view", "_gf_preflight", "_gf_preview", "_gf_price_ctx", "_gf_reconcile",
    "_gf_settings", "_gf_sig", "_gf_statuses", "_gf_std_discount", "_gf_std_text",
    "_gf_summary", "_gf_too_big", "_gf_units", "_gf_valid",
)

# Маршруты блока В ПОРЯДКЕ РЕГИСТРАЦИИ. Порядок здесь не украшение: динамические ресурсы
# aiohttp разрешает перебором в порядке добавления, а у блока их девять.
ORDERED_ROUTES = [
    ("GET", "/api/gridfinity/projects", "_api_gf_projects"),
    ("POST", "/api/gridfinity/projects", "_api_gf_project_add"),
    ("GET", "/api/gridfinity/project/{pid}", "_api_gf_project"),
    ("POST", "/api/gridfinity/project/{pid}", "_api_gf_project_save"),
    ("POST", "/api/gridfinity/project/{pid}/patch", "_api_gf_project_patch"),
    ("POST", "/api/gridfinity/part/{id}", "_api_gf_part_patch"),
    ("POST", "/api/gridfinity/project/{pid}/select", "_api_gf_parts_select"),
    ("GET", "/api/gridfinity/project/{pid}/preflight", "_api_gf_preflight"),
    ("POST", "/api/gridfinity/project/{pid}/order", "_api_gf_order"),
    ("POST", "/api/gridfinity/measures", "_api_gf_measure_add"),
    ("GET", "/api/gridfinity/presets", "_api_gf_presets"),
    ("POST", "/api/gridfinity/presets", "_api_gf_preset_add"),
    ("GET", "/api/gridfinity/config", "_api_gridfinity_config"),
    ("POST", "/api/gridfinity/config", "_api_gridfinity_config_set"),
    ("POST", "/api/gridfinity/order", "_api_gridfinity_order"),
    ("GET", "/gfx/{token}", "_gf_share_page"),
    ("GET", "/gfx/desk/{token}", "_gf_desk_page"),
    ("GET", "/api/gridfinity/project/{pid}/template", "_api_gf_template"),
    ("GET", "/api/admin/gridfinity/{rid}", "_api_admin_gridfinity_config"),
    ("POST", "/api/admin/gridfinity/part/{id}", "_api_admin_gf_part_printed"),
    ("POST", "/api/admin/gridfinity/release/{rid}", "_api_admin_gf_release"),
]


def _live_routes():
    """(метод, канонический путь, имя обработчика) из РОУТЕРА, в порядке регистрации."""
    return [(r.method, r.resource.canonical, r.handler.__qualname__)
            for r in webapp.make_app().router.routes()
            if r.method != "HEAD" and r.resource is not None]


# ─────────────────────────────── фасад ───────────────────────────────

@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.gridfinity.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(gridfinity, name), (
        f"webapp.{name} — не тот объект, что gridfinity.{name}")


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/gridfinity.py обязана появиться и в списке выше."""
    src = io.open(REPO / "app" / "web" / "gridfinity.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined - {"set_bot"} == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - {'set_bot'} - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp_or_storefront():
    """Ни в webapp, ни в витрине не осталось СОБСТВЕННЫХ определений перенесённых имён.

    Вторая копия не ломает ни импорт, ни тесты: маршрут обслуживает функция из gridfinity,
    а правку разработчик вносит в оставшуюся."""
    for mod, path in ((webapp, REPO / "app" / "webapp.py"),
                      (storefront, REPO / "app" / "web" / "storefront.py")):
        src = io.open(path, encoding="utf-8").read()
        own = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
        assert not own, f"в {path.name} остались определения: {sorted(own)}"
        assert not any(hasattr(mod, n) and getattr(mod, n) is not getattr(gridfinity, n)
                       for n in FACADE)


def test_gridfinity_does_not_import_webapp():
    """app/web/gridfinity.py не тянет webapp — иначе разрез не разорвал цикл, а спрятал его."""
    code = "import app.web.gridfinity, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.gridfinity подтянул webapp: {out}"


# ─────────────────────────── таблица маршрутов ───────────────────────────

def test_route_table_registers_in_the_frozen_order():
    """Блок стоит в роутере тем же куском и в том же порядке, что объявлен в модуле."""
    live = _live_routes()
    got = [r for r in live if r[1].startswith("/api/gridfinity")
           or r[1].startswith("/api/admin/gridfinity") or r[1].startswith("/gfx/")]
    assert got == ORDERED_ROUTES


def test_route_table_is_the_only_source_of_gridfinity_routes():
    """Все маршруты блока приходят ИЗ таблицы модуля — ни одного мимо неё."""
    table = [(rd.method, rd.path, rd.handler.__qualname__) for rd in gridfinity.routes]
    assert table == ORDERED_ROUTES


def test_route_block_stays_contiguous():
    """Блок не разорван по make_app: одна строка add_routes легко уезжает незаметно."""
    live = _live_routes()
    at = [i for i, r in enumerate(live)
          if r[1].startswith("/api/gridfinity") or r[1].startswith("/api/admin/gridfinity")
          or r[1].startswith("/gfx/")]
    assert at == list(range(at[0], at[-1] + 1)), "маршруты мастерской идут не подряд"


# ─────────────────── чужие помощники и веер бота ───────────────────

@pytest.mark.parametrize(("name", "home"), [
    ("_verify_admin", webauth), ("_verify_init_data", webauth), ("_verify_user", webauth),
    ("_admin_forbidden", webauth), ("_toint", common), ("_bad", common),
    ("_apply_default_delivery", orderdefaults),
])
def test_external_helpers_are_bound_to_their_real_home(name, home):
    """Помощник в gridfinity — ТОТ ЖЕ объект, что в своём модуле, и тот же, что видит webapp."""
    assert getattr(gridfinity, name) is getattr(home, name)
    assert getattr(gridfinity, name) is getattr(webapp, name)


def test_stub_auth_and_bot_fan_reach_the_new_module(monkeypatch):
    """conftest знает про модуль, и webapp.set_bot до него достаёт.

    Забыть эти две строки — типовая ошибка разреза: отрицательные тесты начинают проходить
    сами собой, а уведомления мастера молча выключаются."""
    assert gridfinity in _AUTH_MODULES, "app/web/gridfinity.py не в conftest._AUTH_MODULES"
    assert gridfinity in _BOT_MODULES, "app/web/gridfinity.py не в conftest._BOT_MODULES"
    assert gridfinity in webapp._BOT_FAN, "модуль не в боевом веере webapp._BOT_FAN"
    stub_auth(monkeypatch, _verify_admin=4242)
    assert gridfinity._verify_admin("что угодно") == 4242
    sentinel = object()
    prev = webapp.set_bot(sentinel)
    try:
        assert gridfinity._bot is sentinel
    finally:
        webapp.set_bot(prev)
