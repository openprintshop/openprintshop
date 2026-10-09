"""Дом драйвера принтера после выноса в app/web/printerhub.py: фасад и герметичность.

Волна 2 endgame-плана. Маршрутов у модуля нет — семь помощников, через которые весь веб-слой
разговаривает с принтером. `_printer_driver` — самый горячий узел бывшего webapp: двадцать
вызывающих из трёх доменов; забытая строка фасада уронила бы принтерный блок сразу в
двадцати местах (отказ громкий — и это хорошо), а вот ВТОРАЯ КОПИЯ отказывала бы молча:
подмена в тестах ставится на один модуль, а ручки зовут другой.

app/printwatch.py с этой волны берёт `_printer_ep` НАПРЯМУЮ из printerhub (два отложенных
`from . import webapp` наблюдателя сняты) — фасад держит остальных читателей и тесты.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import printerhub

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_camera_url", "_driver_printer_id", "_live_view", "_no_printer",
          "_printer_driver", "_printer_ep", "_printer_id_arg",
          # волна 10: указатель активной печати переехал сюда вместе с функциями
          "_active_print_header", "_active_print_key", "_request_printer_id",
          "_set_active_print")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.printerhub.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(printerhub, name)


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/printerhub.py обязана появиться в списке выше."""
    src = io.open(REPO / "app" / "web" / "printerhub.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE)


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_printerhub_does_not_import_webapp():
    """app/web/printerhub.py не тянет webapp — иначе разрез спрятал цикл, а не разорвал."""
    code = "import app.web.printerhub, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.printerhub подтянул webapp: {out}"


def test_printwatch_needs_no_webapp_at_all():
    """После волны 10 у наблюдателя НЕ ОСТАЛОСЬ отложенных `from . import webapp`:
    _printer_ep и _active_print_header берутся из printerhub, capture_bed_shot — из
    printerlive. Появление импорта webapp в printwatch — регресс разреза."""
    src = io.open(REPO / "app" / "printwatch.py", encoding="utf-8").read()
    assert "webapp._printer_ep" not in src, "printwatch вернулся к webapp._printer_ep"
    assert "from . import webapp" not in src, (
        "в printwatch вернулся отложенный импорт webapp — сверь с планом волны 10")


def test_active_print_dict_is_the_same_object():
    """Словарь указателя через фасад — ТОТ ЖЕ объект (волна 10): чистка
    webapp._active_print.clear() в тестах чистит настоящее состояние."""
    assert webapp._active_print is printerhub._active_print


def test_module_holds_no_bot():
    """Держателя бота здесь нет и быть не должно (волна 12)."""
    for name in ("_bot", "set_bot", "_storage", "_models"):
        assert not hasattr(printerhub, name)
