"""Клиентский конвейер после выноса в app/web/clientflow.py ОДНИМ КУСКОМ (волна 15).

Пары «обёртка/нутро» (_refine_calc/_refine_calc_inner, _api_calc/_api_calc_inner) уехали
целиком — нутро держит состояние. quick_link_quote и place_order_from_chat зовутся из
handlers/user_flow.py как webapp.* — потеря их фасадной строки означает отказ входа из
Telegram-чата, а не мини-апки.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import clientflow, registry

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_blocked_payload", "_api_model", "_api_parametric_models", "_api_parametric_model",
          "_hex_to_rgb", "_api_parametric_preview2c", "_api_variant", "_api_file_variant",
          "_api_plate", "_api_plates_info", "_api_mpreview", "_api_cpreview", "_api_modelfile",
          "_live_progress", "_api_calc", "_mw_estimate_for", "_mw_snapshot", "_cache_snapshot",
          "_remember_calc_cache", "_bg_render_cpreview", "_refine_estimate", "_refine_calc",
          "_refine_calc_inner", "_resolve_options", "_m_nparts", "_api_calc_inner",
          "_api_calc_notify", "_quick_calc_plastic", "_fetch_mw_cover", "_quick_mw_quote",
          "quick_link_quote", "_quick_calc_fail", "_api_quote_state", "_api_requote",
          "_api_colors_sel", "_api_order", "_api_order_unsliced", "_api_order_now",
          "_notify_new_order", "place_order_from_chat", "_finalize_pending_order",
          "_api_order_cart", "_api_cart_sync", "_order_json", "_api_orders",
          "_api_order_respond", "_api_order_pay_link", "_api_order_cancel", "_api_order_edit",
          "_api_order_messages", "_api_order_message", "_saved_model_json", "_api_mymodels",
          "_saved_preview", "_presel", "_api_use")


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(clientflow, name)


def test_refine_max_bytes_reexported():
    assert webapp._REFINE_MAX_BYTES == clientflow._REFINE_MAX_BYTES


def test_models_is_registry_dict():
    """Конвейер пишет в ТОТ ЖЕ реестр (волна 14), что и webapp-фасад: вторая копия
    словаря — это две разные сметы у одного клиента."""
    assert clientflow._models is registry._models is webapp._models


def test_place_one_alias_survived():
    """Исторический псевдоним _place_one (= orders.place_from_token) жив в обоих модулях:
    его знают tests и вызовы оформления."""
    from app import orders
    assert clientflow._place_one is orders.place_from_token
    assert webapp._place_one is orders.place_from_token


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    tree = ast.parse(src)
    own = {n.name for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"
    assigns = {t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    assert "_REFINE_MAX_BYTES" not in assigns, "_REFINE_MAX_BYTES определён в webapp повторно"


def test_wrapper_inner_pairs_moved_together():
    """Границу пар не рвать: обе половины каждой пары — в clientflow."""
    for a, b in (("_refine_calc", "_refine_calc_inner"), ("_api_calc", "_api_calc_inner")):
        assert hasattr(clientflow, a) and hasattr(clientflow, b)


def test_lazy_db_import_climbed_one_level():
    """Отличие тел №1: ленивый `from .db import ...` поднялся на `from ..db import ...`.
    Ловим только ЛЕНИВЫЕ (с отступом, внутри функций) однотчечные импорты: модульный
    `from . import mesh3d, ordermedia` наверху — намеренная квалификация (отличие №2)."""
    src = io.open(REPO / "app" / "web" / "clientflow.py", encoding="utf-8").read()
    assert "from ..db import" in src
    bad = [ln for ln in src.splitlines()
           if ln.startswith((" ", "\t"))
           and ln.lstrip().startswith(("from .db ", "from . import"))]
    assert not bad, bad


def test_mesh3d_and_chat_image_are_module_qualified():
    """Отличие тел №2: подмены в тестах идут в mesh3d/ordermedia (webapp — фасадом),
    поэтому clientflow обязан читать эти имена ЧЕРЕЗ МОДУЛЬ, а не держать копию."""
    from app.web import mesh3d, ordermedia
    assert clientflow.mesh3d is mesh3d
    assert clientflow.ordermedia is ordermedia
    src = io.open(REPO / "app" / "web" / "clientflow.py", encoding="utf-8").read()
    code = [ln.partition("#")[0] for ln in src.splitlines()]
    for name in ("_model3d_glb_bytes", "_MODEL3D_MAX_SRC_BYTES"):
        assert not any(f" {name}" in ln or ln.startswith(name) for ln in code), name
    assert any("mesh3d._model3d_glb_bytes" in ln for ln in code)
    assert any("ordermedia._save_chat_image" in ln for ln in code)


def test_clientflow_does_not_import_webapp():
    code = "import app.web.clientflow, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.clientflow подтянул webapp: {out}"


def test_module_holds_a_bot():
    assert hasattr(clientflow, "_bot") and callable(clientflow.set_bot)
    assert not hasattr(clientflow, "_storage")
