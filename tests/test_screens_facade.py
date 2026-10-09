"""Экраны студии после выноса в app/web/screens.py: фасад, состояние, герметичность.

Волна 3 endgame-плана. Особенность этой волны — МОДУЛЬНОЕ СОСТОЯНИЕ уехало вместе с кодом:
кэш `_lcd_cache` и трекер стола `_lcd_prn`. Оба словаря только мутируются, никогда не
пере-присваиваются, поэтому фасад обязан реэкспортировать ТЕ ЖЕ объекты — иначе тесты,
чистящие их через webapp, изолировали бы копию, а ручка копила бы настоящее состояние.

Плоские константы (`_LCD_KEY`, `_LCD_LHM_URL`, `_LCD_DIR`) фасадом НЕ реэкспортируются
намеренно: подмена значения через webapp перепривязала бы копию, а ручка читала бы своё.
Их подменяют в самом модуле (tests/test_lcd_guard.py::_hermetic).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import screens

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_api_admin_screen_power", "_api_lcd_data", "_api_screen_power",
          "_api_screen_status", "_first_hex", "_lcd_cpu_temp", "_lcd_file",
          "_lcd_find_cpu_temp", "_lcd_gpu", "_lcd_prn_for", "_lcd_queue",
          "_turzx_enabled", "lcd_print_handled", "reap_process")

# Константы, которым в фасаде НЕ место (см. докстринг). Появятся — это ошибка разреза.
NOT_IN_FACADE = ("_LCD_KEY", "_LCD_LHM_URL", "_LCD_DIR", "psutil")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.screens.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(screens, name)


@pytest.mark.parametrize("name", ("_lcd_cache", "_lcd_prn"))
def test_state_dicts_are_the_same_objects(name):
    """Словарь состояния через фасад — ТОТ ЖЕ объект: чистка с любой стороны чистит настоящее."""
    assert getattr(webapp, name) is getattr(screens, name)


@pytest.mark.parametrize("name", NOT_IN_FACADE)
def test_plain_values_are_not_reexported(name):
    """Плоское значение в фасаде — ловушка: setattr(webapp, …) перепривязал бы копию, а ручка
    читала бы своё из screens. Пусть имя живёт ровно в одном месте."""
    assert not hasattr(webapp, name), (
        f"{name} появился в webapp: плоские значения фасадом не реэкспортируем — "
        "подменяйте app.web.screens")


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/screens.py обязана появиться в списке выше."""
    src = io.open(REPO / "app" / "web" / "screens.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_screens_does_not_import_webapp():
    """app/web/screens.py не тянет webapp — иначе разрез спрятал цикл, а не разорвал."""
    code = "import app.web.screens, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.screens подтянул webapp: {out}"


def test_lcd_static_dir_survived_the_move():
    """`_LCD_DIR` при переезде в app/web/ обязан был подняться на уровень (app/lcd) —
    единственное разрешённое отличие от дословного переноса. Каталог существует и в нём
    лежит index.html, который отдаёт `_lcd_file` по умолчанию."""
    assert screens._LCD_DIR.name == "lcd"
    assert screens._LCD_DIR.parent.name == "app", str(screens._LCD_DIR)
    assert (screens._LCD_DIR / "index.html").is_file(), (
        "app/lcd/index.html не найден — экран потерял статику")


def test_module_holds_no_bot():
    """Держателя бота здесь нет и быть не должно."""
    for name in ("_bot", "set_bot", "_storage", "_models"):
        assert not hasattr(screens, name)
