"""3D-артефакты и счётчики сметы после выноса в app/web/mesh3d.py и quotecalc.py.

Волна 9 endgame-плана. Особенность: константы бюджета 3D читают И функции mesh3d, И
оставшиеся в webapp клиентские пути (/api/modelfile, _register_full — волны 14/15), поэтому
они реэкспортируются фасадом; подмена в тестах обязана идти В ОБА модуля.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp
from app.web import mesh3d, quotecalc

REPO = pathlib.Path(__file__).resolve().parents[1]

MESH3D = ("_api_admin_orderfile", "_model3d_glb_bytes", "_model3d_meta")
MESH3D_CONSTS = ("_MODEL3D_MAX_SRC_BYTES", "_MODEL3D_FACES", "_GLB_FACE_CAP", "_MODEL3D_EXTS")
QUOTECALC = ("_apply_dfm", "_bed_of", "_bed_xy", "_parse_plates_sel", "_result_payload",
             # волна 13: очередь расчётов «мастер посчитает вручную»
             "_api_calc_queue", "_api_admin_calc_queue", "_api_admin_calc_queue_resolve")


@pytest.mark.parametrize("name", MESH3D + MESH3D_CONSTS)
def test_mesh3d_facade_reexports(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(mesh3d, name) or \
        getattr(webapp, name) == getattr(mesh3d, name)


@pytest.mark.parametrize("name", QUOTECALC)
def test_quotecalc_facade_reexports(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(quotecalc, name)


def test_no_leftover_definitions_in_webapp():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    both = set(MESH3D) | set(QUOTECALC)
    tree = ast.parse(src)
    own = {n.name for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in both}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"
    # мёртвые _model3d_stl_bytes/_nozzle_of_process УДАЛЕНЫ 21.08 по санкции владельца
    # (вопрос 2 плана закрыт) — возвращение любого из них означает случайный откат
    for ghost in ("_model3d_stl_bytes", "_nozzle_of_process"):
        back = {n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == ghost}
        assert not back, f"{ghost} вернулся в webapp — он был удалён по санкции 21.08"


@pytest.mark.parametrize("modname", ["mesh3d", "quotecalc"])
def test_modules_do_not_import_webapp(modname):
    code = f"import app.web.{modname}, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.{modname} подтянул webapp: {out}"


def test_lazy_geometry_import_climbed_one_level():
    """Единственное отличие от дословного переноса: ленивый `from . import cache, geometry`
    внутри _model3d_glb_bytes поднялся на `from .. import` — модуль теперь в app/web/."""
    src = io.open(REPO / "app" / "web" / "mesh3d.py", encoding="utf-8").read()
    assert "from .. import cache, geometry" in src
    # однотчечный вариант не должен остаться В КОДЕ (в докстринге он упомянут историей)
    code_lines = [ln for ln in src.splitlines() if ln.lstrip().startswith("from . import")]
    assert not code_lines, code_lines


def test_mesh3d_holds_no_bot():
    for name in ("_bot", "set_bot", "_storage", "_models", "_active_print"):
        assert not hasattr(mesh3d, name)


def test_quotecalc_holds_a_bot_but_no_other_state():
    """Волна 13: quotecalc — держатель бота (очередь расчётов пишет клиенту и мастеру);
    ставит его webapp.set_bot веером _BOT_FAN (полноту сторожит test_bot_wiring)."""
    assert hasattr(quotecalc, "_bot") and callable(quotecalc.set_bot)
    for name in ("_storage", "_models", "_active_print"):
        assert not hasattr(quotecalc, name)
