"""Раздача файлов мини-аппа/деска после выноса в app/web/uifiles.py (вопрос 5 endgame).

Ручки `_ui_file`/`_favicon` были ЗАМЫКАНИЯМИ внутри make_app — у них не было ни имени в
модуле, ни фасада, а корень статики вычислялся от webapp.py. Теперь они модульные, и
контракт двойной: (1) маршруты make_app ведут именно в uifiles, (2) корень `_WEBUI`
остался тем же каталогом app/webui (модуль лежит на уровень глубже — `parent.parent`).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import uifiles

REPO = pathlib.Path(__file__).resolve().parents[1]
FUNCS = ("_ui_file", "_favicon")


@pytest.mark.parametrize("name", FUNCS)
def test_uifiles_facade_reexports(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(uifiles, name)


def test_routes_lead_to_uifiles():
    """Забытая регистрация в make_app = мини-апп не открывается; проверяем ручки по маршрутам."""
    got = {}
    for r in webapp.make_app().router.routes():
        info = r.resource.canonical if r.resource is not None else ""
        if info in ("/app", "/app/", "/app/{path}", "/favicon.ico"):
            got[info] = r.handler
    assert set(got) == {"/app", "/app/", "/app/{path}", "/favicon.ico"}, sorted(got)
    assert got["/app"] is uifiles._ui_file
    assert got["/app/"] is uifiles._ui_file
    assert got["/app/{path}"] is uifiles._ui_file
    assert got["/favicon.ico"] is uifiles._favicon


def test_webui_root_unchanged():
    """Модуль переехал на уровень глубже — корень статики обязан остаться app/webui."""
    assert uifiles._WEBUI.resolve() == (REPO / "app" / "webui").resolve()
    assert uifiles._DESK == (uifiles._WEBUI / "desk").resolve()
    assert uifiles._FAVICON == uifiles._WEBUI / "favicon.ico"
    assert (uifiles._WEBUI / "index.html").is_file()


def test_no_leftover_definitions_in_make_app():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    tree = ast.parse(src)
    make_app = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_app")
    inner = {n.name for n in ast.walk(make_app)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS}
    assert not inner, f"в make_app остались замыкания: {sorted(inner)}"
    top = {n.name for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS}
    assert not top, f"в webapp.py остались определения: {sorted(top)}"


def test_uifiles_does_not_import_webapp():
    code = "import app.web.uifiles, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "0", "uifiles тянет за собой webapp — цикл импорта"
