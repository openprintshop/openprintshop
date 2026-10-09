"""Доставка по умолчанию после выноса в app/web/orderdefaults.py: фасад и герметичность.

Волна 1c endgame-плана. Маршрутов у модуля нет — только два помощника, которых зовут восемь
мест конвейера заказа в webapp. Прибор здесь — единственный сторож фасада: прямых обращений
`webapp._delivery_note` в тестах нет вовсе, а `webapp._apply_default_delivery` читают только
tests/test_delivery_threshold_repricing.py (через фасад, без setattr).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import orderdefaults

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_apply_default_delivery", "_delivery_note")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.orderdefaults.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(orderdefaults, name)


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/orderdefaults.py обязана появиться в списке выше."""
    src = io.open(REPO / "app" / "web" / "orderdefaults.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE)


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_orderdefaults_does_not_import_webapp():
    """app/web/orderdefaults.py не тянет webapp — иначе разрез спрятал цикл, а не разорвал."""
    code = "import app.web.orderdefaults, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.orderdefaults подтянул webapp: {out}"


def test_module_holds_no_bot_and_no_state():
    """Держателя бота и модульного состояния webapp здесь нет и быть не должно: сообщения
    клиенту шлют вызывающие. Заведись тут `_bot` — run.wire_bot его не проводит, и доставка
    уведомлений молча выключилась бы."""
    for name in ("_bot", "set_bot", "_storage", "_models", "_active_print"):
        assert not hasattr(orderdefaults, name)
