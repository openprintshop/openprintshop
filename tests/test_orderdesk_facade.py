"""Списки заказов и карточка мастера после выноса в app/web/orderdesk.py: фасад и права.

Волна 5 endgame-плана (после волны 1a: карточка держится за общий дом ordermedia). Денежные
и статусные ручки заказа остались в webapp — они держатся за `_bot` (волны 13/16).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import orderdesk, ordermedia

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_api_admin_order", "_api_admin_orders", "_api_admin_orders_csv",
          "_api_admin_self_orders", "_delivery_recalc_hint", "_order_scale_pct")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.orderdesk.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(orderdesk, name)


def test_facade_covers_every_public_name_of_the_module():
    """Фасад полон: новая функция в app/web/orderdesk.py обязана появиться в списке выше."""
    src = io.open(REPO / "app" / "web" / "orderdesk.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(FACADE)


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_orderdesk_does_not_import_webapp():
    """app/web/orderdesk.py не тянет webapp — иначе разрез спрятал цикл, а не разорвал."""
    code = "import app.web.orderdesk, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.orderdesk подтянул webapp: {out}"


def test_media_helpers_come_from_ordermedia():
    """Карточка заказа берёт медиа-сборщики из их дома, а не копией: тот же объект."""
    for name in ("_slicer_url", "_timelapse_url", "_order_prints_media", "_msgs_json"):
        assert getattr(orderdesk, name) is getattr(ordermedia, name)


def test_self_orders_stays_closed_by_default():
    """`/api/admin/self_orders` держится ТОЛЬКО на «закрыто по умолчанию»: путь не должен
    появиться в _OPERATOR_ROUTES «для симметрии» — самопечать мастера оператору не отдаём."""
    from app import webguard
    assert "/api/admin/self_orders" not in dict(getattr(webguard, "_OPERATOR_ROUTES", ()))


def test_module_holds_no_bot():
    """Держателя бота здесь нет и быть не должно: списки и карточка только читают."""
    for name in ("_bot", "set_bot", "_storage", "_models", "_active_print"):
        assert not hasattr(orderdesk, name)
