"""Печать как работа после выноса в app/web/printerjob.py: фасад, границы, ловушки.

Волна 11 endgame-плана — последняя волна свободной программы (1–11): дальше только волны,
требующие решения владельца о держателе бота (12–16).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import printmedia, webapp
from app.web import printerjob

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_api_admin_order_attach_print", "_api_admin_printer_bind",
          "_api_admin_printer_history", "_api_admin_printer_print",
          "_api_admin_printer_status", "_api_admin_printer_unbind",
          "_link_attached_print_journal",
          # волна 13: очередь печати, самопечать, обслуживание
          "_api_admin_queue", "_api_admin_queue_move", "_api_admin_self_print",
          "_api_admin_service", "_api_admin_service_action",
          # допечатка без сбойной детали (Путь A, app/reprint.py): ручки живут здесь же —
          # это жизненный цикл ПЕЧАТИ ЗАКАЗА, тот же предмет, что старт и привязка
          "_api_admin_reprint", "_api_admin_reprint_parts", "_api_admin_reprint_start")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(printerjob, name)


def test_facade_covers_every_public_name_of_the_module():
    src = io.open(REPO / "app" / "web" / "printerjob.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined - {"set_bot"} == set(FACADE)


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_printerjob_does_not_import_webapp():
    code = "import app.web.printerjob, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.printerjob подтянул webapp: {out}"


def test_printmedia_is_referenced_by_module_not_by_value():
    """Качалка/каталог таймлапсов зовутся ЧЕРЕЗ printmedia.* (call-time lookup): связывание по
    значению заморозило бы объект, и подмена каталога в тестах писала бы в БОЕВОЙ
    data/timelapses. Появление голого `_download_timelapse(` в коде — регресс."""
    src = io.open(REPO / "app" / "web" / "printerjob.py", encoding="utf-8").read()
    for bare in ("= _TIMELAPSE_DIR /", "await _download_timelapse(",
                 "await _deliver_timelapse(", " cached_timelapse_path("):
        assert bare not in src, f"голая ссылка printmedia в printerjob: {bare!r}"
    assert "printmedia._TIMELAPSE_DIR" in src
    assert printerjob.printmedia is printmedia


def test_module_holds_a_bot_but_no_other_state():
    """Волна 13: модуль — держатель бота (самопечать шлёт уведомление мастеру); ставит его
    webapp.set_bot веером _BOT_FAN, полноту веера сторожит test_bot_wiring. Таймлапсы
    клиенту по-прежнему доставляет printmedia со СВОИМ _bot (проводка run.wire_bot)."""
    assert hasattr(printerjob, "_bot") and callable(printerjob.set_bot)
    for name in ("_storage", "_models"):
        assert not hasattr(printerjob, name)
