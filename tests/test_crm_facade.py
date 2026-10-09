"""CRM после выноса в app/web/crm.py: фасад и держатель бота.

Волна 6 endgame-плана разрезала домен ПОПОЛАМ («написать клиенту» и рассылка читали
`_bot` и оставались в webapp); волна 13 собрала его обратно — модуль получил свой `_bot`
через веер webapp._BOT_FAN (волна 12, вариант «б»). Этот тест держит фасад и проверяет,
что модуль — полноправный держатель бота (полноту веера сторожит test_bot_wiring).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import crm

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_api_admin_broadcast", "_api_admin_calc", "_api_admin_chat_search", "_api_admin_client",
          "_api_admin_client_chat", "_api_admin_client_discount", "_api_admin_client_message",
          "_api_admin_client_save", "_api_admin_clients", "_api_admin_clients_csv",
          "_api_admin_events", "_api_admin_task", "_api_admin_tasks", "_parse_task_due")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.crm.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(crm, name)


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/crm.py обязана появиться в списке выше.
    set_bot — не фасадное имя: это точка проводки веера, тесты зовут её через webapp."""
    src = io.open(REPO / "app" / "web" / "crm.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined - {"set_bot"} == set(FACADE)


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    defs = {n.name for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    own = defs & set(FACADE)
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_crm_does_not_import_webapp():
    """app/web/crm.py не тянет webapp — иначе разрез спрятал цикл, а не разорвал."""
    code = "import app.web.crm, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.crm подтянул webapp: {out}"


def test_module_holds_a_bot_but_no_other_state():
    """Волна 13: crm — держатель бота (иначе «написать клиенту» молчала бы), но чужого
    состояния webapp (_storage, _models) здесь по-прежнему быть не должно."""
    assert hasattr(crm, "_bot") and callable(crm.set_bot)
    for name in ("_storage", "_models", "_active_print"):
        assert not hasattr(crm, name)
