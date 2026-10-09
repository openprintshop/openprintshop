"""Деньги заказа после выноса в app/web/ordermoney.py (волна 16 — конец полной программы).

Особые точки: _maybe_referral_reward зовут оставшийся в webapp confirm_print и
handlers/payments.py:88 — потеря фасадной строки выключает реферальную награду молча;
инвариант NEIGHBOURS вокруг orderbench (test_orderbench_facade) держится на том, что
регистрация маршрутов в make_app осталась строками на фасадные имена.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import ordermoney

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_sane_actual", "_telemetry_actual_minutes", "_capture_print_sample",
          "_accrue_service_on_print", "_consume_order_stock", "_consume_journal_stock",
          "_apply_price_or_ask", "_api_admin_order_status", "_api_admin_order_bill",
          "_api_admin_order_paid", "_api_admin_order_price", "_api_admin_order_recalc",
          "_api_admin_order_recalc_apply", "_save_slicer_shot", "_api_admin_order_invoice",
          "_api_admin_order_create", "_api_admin_balance", "_api_admin_balance_comment",
          "_api_admin_order_print_net", "_maybe_referral_reward")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(ordermoney, name)


def test_facade_covers_every_public_name_of_the_module():
    src = io.open(REPO / "app" / "web" / "ordermoney.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined - {"set_bot"} == set(FACADE)


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    tree = ast.parse(src)
    own = {n.name for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_referral_reward_reachable_from_confirm_print_and_payments():
    """Оба внешних вызова живут на фасадном имени webapp._maybe_referral_reward."""
    assert webapp._maybe_referral_reward is ordermoney._maybe_referral_reward
    src = io.open(REPO / "app" / "handlers" / "payments.py", encoding="utf-8").read()
    assert "webapp._maybe_referral_reward" in src, \
        "handlers/payments.py больше не зовёт webapp._maybe_referral_reward — проверь проводку"


def test_ordermoney_does_not_import_webapp():
    code = "import app.web.ordermoney, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.ordermoney подтянул webapp: {out}"


def test_module_holds_a_bot():
    assert hasattr(ordermoney, "_bot") and callable(ordermoney.set_bot)
    for name in ("_storage", "_models"):
        assert not hasattr(ordermoney, name)
