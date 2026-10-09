"""Живой принтер после выноса в app/web/printerlive.py: фасад, состояние, границы волны.

Волна 4 endgame-плана. Уехали камера (ретранслятор `_CamRelay` + реестр `_cam_relays`),
скрин стола, файлы/заливка и ручной пульт. НЕ уехали и не должны были:
`_api_admin_printer_print` (пишет `_active_print` — волна 10) и `_api_admin_printer_status`
(читает `_active_print_header` — волна 11) — их переезд без указателя активной печати
погасил бы «богатую шапку» экрана принтера молча.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import printerlive

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_CamRelay", "_api_admin_camera", "_api_admin_camera_check",
          "_api_admin_printer_control", "_api_admin_printer_delete", "_api_admin_printer_fan",
          "_api_admin_printer_files", "_api_admin_printer_heat", "_api_admin_printer_home",
          "_api_admin_printer_load", "_api_admin_printer_move", "_api_admin_printer_unload",
          "_api_admin_printer_upload", "_cam_relay_for", "_grab_camera_frame",
          "_printer_cmd_endpoint", "_printer_printing", "_remember_gcode_settings",
          "capture_bed_shot", "copy_journal_photo_to_order")

# После волны 11 соседи по прежнему расположению живут в app/web/printerjob.py:
IN_PRINTERJOB = ("_api_admin_printer_print", "_api_admin_printer_status")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.printerlive.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(printerlive, name)


def test_cam_relays_registry_is_the_same_object():
    """Реестр ретрансляторов через фасад — ТОТ ЖЕ словарь: чистка тестами чистит настоящее."""
    assert webapp._cam_relays is printerlive._cam_relays


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция/класс в app/web/printerlive.py обязана попасть в список."""
    src = io.open(REPO / "app" / "web" / "printerlive.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert defined == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён; соседи по
    прежнему расположению после волны 11 живут в printerjob (не здесь и не в webapp)."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    defs = {n.name for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    own = defs & (set(FACADE) | set(IN_PRINTERJOB))
    assert not own, f"в webapp.py остались определения: {sorted(own)}"
    from app.web import printerjob
    for name in IN_PRINTERJOB:
        assert hasattr(printerjob, name), f"{name} потерялся при переезде в printerjob"
        assert not hasattr(printerlive, name), f"{name} оказался в printerlive — чужой дом"


def test_printerlive_does_not_import_webapp():
    """app/web/printerlive.py не тянет webapp — иначе разрез спрятал цикл, а не разорвал."""
    code = "import app.web.printerlive, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.printerlive подтянул webapp: {out}"


def test_printwatch_takes_bed_shot_from_printerlive():
    """Наблюдатель берёт capture_bed_shot из printerlive: у printwatch остался ровно один
    отложенный импорт webapp (_active_print_header — волна 10)."""
    src = io.open(REPO / "app" / "printwatch.py", encoding="utf-8").read()
    assert "webapp.capture_bed_shot" not in src
    assert "printerlive.capture_bed_shot" in src


def test_module_holds_no_bot_and_no_active_print():
    """Держателя бота и указателя активной печати здесь нет и быть не должно (волны 10/12)."""
    for name in ("_bot", "set_bot", "_storage", "_models", "_active_print",
                 "_set_active_print", "_active_print_key", "_active_print_header"):
        assert not hasattr(printerlive, name)
