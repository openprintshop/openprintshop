"""Студия владельца и гигиена списка после выноса в app/web/studio.py и orderlist.py.

Волна 7 endgame-плана. У ПЯТИ групп студии ноль якорей `webapp.<имя>` в тестах — этот файл
единственный прибор против забытой строки фасада (риск назван в плане волны прямо).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import orderlist, studio

REPO = pathlib.Path(__file__).resolve().parents[1]

STUDIO = ("_api_admin_brand_logo", "_api_admin_collection", "_api_admin_collection_edit",
          "_api_admin_errors", "_api_admin_errors_seen", "_api_admin_feature",
          "_api_admin_kb", "_api_admin_kb_delete", "_api_admin_kb_save",
          "_api_admin_mw_account", "_api_admin_mw_accounts", "_api_admin_setting",
          "_api_admin_settings", "_api_admin_shade", "_api_admin_shades",
          "_api_admin_staff", "_api_admin_staff_save", "_brand_logo_file")

ORDERLIST = ("_api_admin_order_delete", "_api_admin_orders_clean_test",
             "_api_admin_orders_mark_all_seen", "_cleanup_request_files")


@pytest.mark.parametrize("name", STUDIO)
def test_studio_facade_reexports_the_same_object(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(studio, name)


@pytest.mark.parametrize("name", ORDERLIST)
def test_orderlist_facade_reexports_the_same_object(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(orderlist, name)


@pytest.mark.parametrize(("mod", "path", "names"), [
    (studio, "studio.py", STUDIO), (orderlist, "orderlist.py", ORDERLIST)])
def test_facade_covers_every_public_name(mod, path, names):
    src = io.open(REPO / "app" / "web" / path, encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == set(names), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(names))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(names) - defined)}")


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    both = set(STUDIO) | set(ORDERLIST)
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in both}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


@pytest.mark.parametrize("modname", ["studio", "orderlist"])
def test_modules_do_not_import_webapp(modname):
    code = f"import app.web.{modname}, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.{modname} подтянул webapp: {out}"


def test_brand_logo_keeps_the_multipart_cap_check():
    """`_forbidden_cap(roles.CAP_SETTINGS)` уехал вместе с ручкой: multipart проходит мимо
    _caps_mw, и без этой строки загрузка логотипа открылась бы оператору."""
    src = io.open(REPO / "app" / "web" / "studio.py", encoding="utf-8").read()
    assert "_forbidden_cap(roles.CAP_SETTINGS)" in src


def test_orderlist_paths_keep_their_rights():
    """Права путей гигиены заморожены: зачистка тестовых и удаление заказа оператору НЕ
    отдаются (закрыто по умолчанию), а «отметить все просмотренными» — производственная
    кнопка, она у оператора ЕСТЬ явной строкой CAP_ORDERS."""
    from app import roles, webguard
    routes = dict(getattr(webguard, "_OPERATOR_ROUTES", ()))
    for p in ("/api/admin/orders/clean_test", "/api/admin/order/delete"):
        assert p not in routes, f"{p} появился в _OPERATOR_ROUTES"
    assert routes.get("/api/admin/orders/mark_all_seen") == roles.CAP_ORDERS


@pytest.mark.parametrize("mod", [studio, orderlist])
def test_modules_hold_no_bot(mod):
    for name in ("_bot", "set_bot", "_storage", "_models"):
        assert not hasattr(mod, name)
