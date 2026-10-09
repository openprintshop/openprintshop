"""Медиа и переписка заказа после выноса в app/web/ordermedia.py: фасад, маршруты, помощники.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна 1a endgame-плана увезла из app/webapp.py общий дом медиа и переписки
заказа: три подписных файловых ручки (/reqprev, /slicershot, /timelapse), сборщики подписных
URL (`_slicer_url`, `_timelapse_url`, `_chat_photo_url`), агрегат печатей журнала
(`_order_prints_media`), представление переписки (`_msgs_json`) и сохранение вложения
(`_save_chat_image`). В webapp остался построчный ФАСАД, а регистрация переехала со списка
`router.add_get` в make_app на таблицу `web.RouteTableDef()` рядом с обработчиками.

Прибор здесь — единственный сторож фасада: снимок tests/test_routes_inventory.py держит имена
обработчиков строками и не видит ни забытую строку фасада, ни вторую копию функции в webapp
(`handler.__qualname__` у функции уровня модуля от переезда не меняется). Подробный разбор
слепых зон — в шапке tests/test_orderbench_facade.py, здесь он не повторяется.

ОТДЕЛЬНО ПРО АВТОРИЗАЦИЮ. Модуль в conftest._AUTH_MODULES НЕ входит НАМЕРЕННО: ни одно из
девяти тел не читает ни `_verify_*`, ни `_admin_body` — три ручки подписные (подпись вместо
логина), остальное — чистые помощники. Появится здесь ручка с авторизацией — сперва добавить
модуль в _AUTH_MODULES, иначе stub_auth до неё не достанет (см. test_module_binds_no_auth).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import printmedia, webapp, websign
from app.web import common, ordermedia

REPO = pathlib.Path(__file__).resolve().parents[1]

# Имена, которые app/webapp.py обязан реэкспортировать из app/web/ordermedia.py. Список
# ведётся руками именно потому, что он и есть контракт фасада: цена лишней строки — ноль,
# цена забытой — отказ у живого клиента (карточка заказа, чаты и PDF зовут их как webapp.*).
FACADE = ("_chat_photo_url", "_msgs_json", "_order_prints_media", "_request_preview",
          "_save_chat_image", "_slicer_shot_file", "_slicer_url", "_timelapse_file",
          "_timelapse_url",
          # волна 13: PDF-документы (смета по токену, паспорт заказа)
          "_api_pdf", "_api_order_pdf")

# Маршруты блока В ПОРЯДКЕ РЕГИСТРАЦИИ — порядок повторяет прежние строки add_get в make_app.
# Пути ПОДПИСНЫЕ и уже разосланы клиентам в сообщениях: менять нельзя ни на символ.
ORDERED_ROUTES = [
    ("GET", "/reqprev/{rid}", "_request_preview"),
    ("GET", "/slicershot/{rid}", "_slicer_shot_file"),
    ("GET", "/timelapse/{rid}", "_timelapse_file"),
]

# Соседи блока в make_app: что зарегистрировано непосредственно ДО и ПОСЛЕ него. Блок стоял
# между превью сохранённой модели и таймлапсом записи журнала — там и остался.
NEIGHBOURS = (("GET", "/smprev/{sid}", "_saved_preview"),
              ("GET", "/jtl/{jid}", "_jtl_file"))


def _live_routes():
    """(метод, канонический путь, имя обработчика) из РОУТЕРА, в порядке регистрации."""
    return [(r.method, r.resource.canonical, r.handler.__qualname__)
            for r in webapp.make_app().router.routes()
            if r.method != "HEAD" and r.resource is not None]


# ─────────────────────────────── фасад ───────────────────────────────

@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.ordermedia.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(ordermedia, name), (
        f"webapp.{name} — не тот объект, что ordermedia.{name}: либо в webapp осталась вторая "
        "копия, либо фасад импортирует что-то другое")


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/ordermedia.py обязана появиться и в списке выше."""
    src = io.open(REPO / "app" / "web" / "ordermedia.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined - {"set_bot"} == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_ordermedia_does_not_import_webapp():
    """app/web/ordermedia.py не тянет webapp — иначе разрез не разорвал цикл, а спрятал его.

    Отдельный процесс: в текущем webapp уже импортирован, и sys.modules ничего не докажет."""
    code = "import app.web.ordermedia, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.ordermedia подтянул webapp: {out}"


def test_module_holds_a_bot_but_no_printer_state():
    """Волна 13: модуль — держатель бота (_api_pdf может дослать документ в чат);
    ставит его webapp.set_bot веером _BOT_FAN (полноту сторожит test_bot_wiring).
    `_models` теперь ЧИТАЕТСЯ из web/registry (волна 14) — это тот же словарь, не копия;
    состояние принтера сюда по-прежнему не относится."""
    assert hasattr(ordermedia, "_bot") and callable(ordermedia.set_bot)
    from app.web import registry
    assert ordermedia._models is registry._models
    for name in ("_storage", "_active_print", "_set_active_print", "_active_print_key"):
        assert not hasattr(ordermedia, name), (
            f"в app/web/ordermedia.py появилось {name} — модульное состояние webapp")


def test_lazy_quote_import_climbed_one_level():
    """Единственное отличие тел от дословного переноса (волна 13): ленивый
    `from . import quote` внутри PDF-ручек поднялся на `from .. import quote`."""
    src = io.open(REPO / "app" / "web" / "ordermedia.py", encoding="utf-8").read()
    assert "from .. import quote" in src
    code_lines = [ln for ln in src.splitlines() if ln.lstrip().startswith("from . import")]
    assert not code_lines, code_lines


def test_module_is_in_auth_modules():
    """Волна 13 привязала `_verify_init_data` (PDF-ручки) — модуль ОБЯЗАН быть в
    conftest._AUTH_MODULES, иначе stub_auth до PDF не достаёт и отказ молчалив."""
    import conftest
    assert ordermedia in conftest._AUTH_MODULES
    assert hasattr(ordermedia, "_verify_init_data")


# ─────────────────────────── таблица маршрутов ───────────────────────────

def test_route_table_registers_in_the_frozen_order():
    """Блок стоит в роутере тем же куском и в том же порядке, что до перевода на add_routes."""
    live = _live_routes()
    got = [r for r in live if r[1] in {p for _, p, _ in ORDERED_ROUTES}]
    assert got == ORDERED_ROUTES


def test_route_table_stays_one_contiguous_block_in_place():
    """`app.add_routes(ordermedia.routes)` стоит там же, где стояли строки add_get."""
    live = _live_routes()
    paths = {p for _, p, _ in ORDERED_ROUTES}
    at = [i for i, r in enumerate(live) if r[1] in paths]
    assert at == list(range(at[0], at[-1] + 1)), "блок разорван: маршруты блока не подряд"
    assert (live[at[0] - 1], live[at[-1] + 1]) == NEIGHBOURS, "блок переехал по make_app"


def test_route_table_is_the_only_source_of_ordermedia_routes():
    """Все маршруты блока приходят ИЗ таблицы модуля — ни одного мимо неё."""
    table = [(rd.method, rd.path, rd.handler.__qualname__) for rd in ordermedia.routes]
    assert table == ORDERED_ROUTES


# ─────────────────── чужие помощники: один объект, а не копия ───────────────────

@pytest.mark.parametrize(("name", "home"), [
    ("_toint", common), ("_photo_url", common), ("_maybe_webp", common),
    ("_img_headers", common), ("check_sig", common), ("sig", common), ("signed_url", common),
    ("_file_ver", websign),
    ("_heal_journal_timelapse", printmedia), ("_file_size", printmedia),
])
def test_external_helpers_are_bound_to_their_real_home(name, home):
    """Помощник в ordermedia — ТОТ ЖЕ объект, что в своём модуле, и тот же, что видит webapp.

    Вторая копия здесь отказывает молча: подмена в тестах ставится на дом помощника, а блок
    продолжает звать оригинал (или наоборот)."""
    assert getattr(ordermedia, name) is getattr(home, name)
    assert getattr(ordermedia, name) is getattr(webapp, name)
