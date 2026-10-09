"""Реестр сессий расчёта после выноса в app/web/registry.py (волна 14 endgame).

Главный контракт — ОБЪЕКТНАЯ идентичность `_models`: словарь мутируется по месту из
регистрации, конвейера сметы и оформления заказа, и вторая копия означала бы две разные
сметы у одного клиента. Фасад обязан реэкспортировать ТОТ ЖЕ словарь, а autouse-фикстура
conftest.clean_models — чистить его через фасад по-настоящему.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import registry

REPO = pathlib.Path(__file__).resolve().parents[1]

FUNCS = ("_remember_model", "_register_model", "_register_full",
         "_remember_and_respond", "_make_token")


@pytest.mark.parametrize("name", FUNCS)
def test_registry_facade_reexports(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(registry, name)


def test_models_is_the_same_object():
    assert webapp._models is registry._models, \
        "webapp._models разошёлся с registry._models: две копии реестра = две разные сметы"
    assert webapp._MODELS_MAX == registry._MODELS_MAX


def test_clean_models_fixture_really_cleans(clean_models):
    """Фикстура чистит настоящий словарь: пишем через registry, видим пустоту через webapp."""
    registry._models["tok"] = {"label": "x"}
    assert webapp._models["tok"]["label"] == "x"
    webapp._models.clear()
    assert not registry._models


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    tree = ast.parse(src)
    own = {n.name for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCS}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"
    # и само состояние не должно определяться в webapp второй раз
    assigns = {t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    ann = {n.target.id for n in tree.body
           if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    assert "_models" not in (assigns | ann), "_models определён в webapp повторно"


def test_registry_does_not_import_webapp():
    code = "import app.web.registry, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.registry подтянул webapp: {out}"


def test_registry_holds_no_bot():
    for name in ("_bot", "set_bot", "_storage", "_active_print"):
        assert not hasattr(registry, name)
