"""Четыре домена с ботом после выноса волной 13 endgame (вариант «б» вопроса №1).

app/web/linkbatch.py — пакетный расчёт ссылок; app/web/shipping.py — доставка;
app/web/notifhub.py — центр уведомлений и семьи; app/web/products.py — товары и
индивидуальный заказ. Все четыре держат СВОЙ `_bot` (веер webapp._BOT_FAN — его полноту
сторожит test_bot_wiring, здесь только фасады и герметичность).
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import linkbatch, notifhub, products, shipping

REPO = pathlib.Path(__file__).resolve().parents[1]

LINKBATCH = ("_extract_links", "_batch_defaults", "_run_batch", "_batch_json",
             "_api_admin_batch_calc", "_api_admin_batch_status", "_api_admin_batch_last",
             "_api_admin_batch_cancel", "_batch_preview", "_api_admin_batch_send")
SHIPPING = ("_api_delivery_types", "_api_admin_delivery", "_api_admin_delivery_type",
            "_group_or_error", "_api_admin_order_delivery", "_api_admin_order_shipment")
NOTIFHUB = ("_api_family_pick_partner", "_api_family_request", "_cust_display",
            "_api_admin_notifications", "_api_admin_families", "_api_admin_family_create",
            "_api_admin_family_add", "_api_admin_family_remove",
            "_api_admin_notification_action")
PRODUCTS = ("_product_json", "_product_photo_file", "_api_products", "_api_product_order",
            "_api_admin_products", "_api_admin_product_save", "_api_admin_product_delete",
            "_api_custom_order", "_custom_photo")
MODULES = {linkbatch: LINKBATCH, shipping: SHIPPING, notifhub: NOTIFHUB, products: PRODUCTS}


@pytest.mark.parametrize("mod,names", MODULES.items(), ids=lambda v: getattr(v, "__name__", ""))
def test_facade_reexports(mod, names):
    for name in names:
        assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
        assert getattr(webapp, name) is getattr(mod, name), name


def test_state_and_constants():
    # словарь пакетов — ТОТ ЖЕ объект (мутируется, не пере-присваивается)
    assert webapp._batches is linkbatch._batches
    assert webapp._BATCH_MAX_LINKS == linkbatch._BATCH_MAX_LINKS
    assert webapp._BATCH_KEEP == linkbatch._BATCH_KEEP
    # каталог индивидуальных заказов читает и денежный блок webapp (_save_slicer_shot)
    assert webapp._CUSTOM_DIR is products._CUSTOM_DIR
    assert webapp._CUSTOM_MAX_BYTES == products._CUSTOM_MAX_BYTES
    assert webapp._CUSTOM_MAX_PHOTOS == products._CUSTOM_MAX_PHOTOS


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    moved = {n for names in MODULES.values() for n in names}
    tree = ast.parse(src)
    own = {n.name for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in moved}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"
    assigns = {t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    ann = {n.target.id for n in tree.body
           if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    dup = (assigns | ann) & {"_batches", "_BATCH_MAX_LINKS", "_BATCH_KEEP",
                             "_CUSTOM_DIR", "_CUSTOM_MAX_BYTES", "_CUSTOM_MAX_PHOTOS"}
    assert not dup, f"состояние определено в webapp повторно: {sorted(dup)}"


@pytest.mark.parametrize("modname", ["linkbatch", "shipping", "notifhub", "products"])
def test_modules_do_not_import_webapp(modname):
    code = f"import app.web.{modname}, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.{modname} подтянул webapp: {out}"


@pytest.mark.parametrize("mod", MODULES, ids=lambda m: m.__name__)
def test_modules_hold_a_bot(mod):
    """Каждый модуль — держатель бота: ветки отправки закрыты `if _bot is None`,
    а бота ставит webapp.set_bot веером (полноту веера сторожит test_bot_wiring)."""
    assert hasattr(mod, "_bot") and callable(mod.set_bot)


def test_notifhub_lazy_avatars_import_climbed_one_level():
    """Единственное отличие от дословного переноса: ленивый `from . import avatars`
    внутри _api_admin_notification_action поднялся на `from .. import avatars`."""
    src = io.open(REPO / "app" / "web" / "notifhub.py", encoding="utf-8").read()
    assert "from .. import avatars" in src
    code_lines = [ln for ln in src.splitlines() if ln.lstrip().startswith("from . import")]
    assert not code_lines, code_lines
