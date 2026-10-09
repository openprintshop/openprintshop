"""Масштабирование модели: подготовка мешей, лимит против стола, габариты, снимок заказа."""
import json
from types import SimpleNamespace

import trimesh

from app import calcservice, geometry, preview, slicer


def _box_stl(tmp_path, name, extents):
    p = tmp_path / name
    trimesh.creation.box(extents=extents).export(str(p))
    return p


# ---- slicer.bed_size -------------------------------------------------------------------

def test_bed_size_parses_machine_profile(tmp_path):
    prof = tmp_path / "machine.json"
    prof.write_text(json.dumps({
        "name": "Test CC2",
        "printable_area": ["0x0", "256x0", "256x256", "0x256"],
        "printable_height": "250",
    }), encoding="utf-8")
    assert slicer.bed_size(prof) == (256.0, 250.0)


def test_bed_size_fallback_on_garbage(tmp_path):
    prof = tmp_path / "machine.json"
    prof.write_text("не json", encoding="utf-8")
    xy, z = slicer.bed_size(prof)
    assert xy > 0 and z > 0          # консервативный дефолт, не падение


# ---- geometry.scale_limit --------------------------------------------------------------

def test_scale_limit_single_box(tmp_path):
    p = _box_stl(tmp_path, "box.stl", (100, 50, 25))
    lim = geometry.scale_limit([p], 256.0, 256.0)
    assert abs(lim - 2.56) < 1e-6    # лимитирует самая длинная сторона (X=100)


def test_scale_limit_takes_largest_part(tmp_path):
    a = _box_stl(tmp_path, "big.stl", (200, 10, 10))
    b = _box_stl(tmp_path, "small.stl", (20, 20, 20))
    lim = geometry.scale_limit([a, b], 256.0, 256.0)
    assert abs(lim - 1.28) < 1e-6    # 256/200 — большая деталь, а не общий bbox

    # высота тоже ограничивает: низкий стол по Z
    lim_z = geometry.scale_limit([a, b], 256.0, 15.0)
    assert abs(lim_z - 0.75) < 1e-6  # 15/20 у кубика


def test_scale_limit_unreadable_is_inf(tmp_path):
    p = tmp_path / "model.step"
    p.write_text("no mesh here", encoding="utf-8")
    assert geometry.scale_limit([p], 256.0, 256.0) == float("inf")


# ---- slicer._prepare_models (масштабированная копия STL) --------------------------------

def test_prepare_models_scale_makes_scaled_copy(tmp_path):
    p = _box_stl(tmp_path, "box.stl", (10, 20, 30))
    out = slicer._prepare_models([p], scale=1.5)
    assert len(out) == 1 and out[0] != p and out[0].name == "box_s150.stl"
    m = trimesh.load(str(out[0]), force="mesh")
    e = m.bounds[1] - m.bounds[0]
    assert abs(e[0] - 15) < 1e-6 and abs(e[1] - 30) < 1e-6 and abs(e[2] - 45) < 1e-6
    # оригинал не тронут
    e0 = trimesh.load(str(p), force="mesh").bounds
    assert abs((e0[1] - e0[0])[0] - 10) < 1e-6


def test_prepare_models_scale_1_passthrough(tmp_path):
    p = _box_stl(tmp_path, "box.stl", (10, 20, 30))
    assert slicer._prepare_models([p], scale=1.0) == [p]


def test_prepare_models_unscalable_returns_original(tmp_path):
    """Файл, который trimesh не читает: при масштабе возвращается ОРИГИНАЛ (маркер для честного
    отказа в slice_unit) — но БЕЗ исключения (оно бы «сломало» cpupool)."""
    p = tmp_path / "model.step"
    p.write_text("not a mesh", encoding="utf-8")
    assert slicer._prepare_models([p], scale=1.5) == [p]


# ---- preview.model_dims со scale --------------------------------------------------------

def test_model_dims_scales_linearly(tmp_path):
    p = _box_stl(tmp_path, "box.stl", (10, 20, 30))
    s1, v1 = preview.model_dims([p])
    s2, v2 = preview.model_dims([p], scale=2.0)
    assert s1 == "10×20×30 мм" and s2 == "20×40×60 мм"
    assert abs(v2 - v1 * 8) < 1e-6   # объём габаритов — куб масштаба


# ---- calcservice: кламп и снимок заказа --------------------------------------------------

async def test_effective_scale_clamps_to_bed(tmp_path, monkeypatch):
    p = _box_stl(tmp_path, "box.stl", (100, 50, 25))
    prof = tmp_path / "machine.json"
    prof.write_text(json.dumps({"printable_area": ["0x0", "256x0", "256x256", "0x256"],
                                "printable_height": "256"}), encoding="utf-8")
    printer = SimpleNamespace(machine_profile=str(prof))
    # 100% не трогаем никогда (даже без запроса масштаба геометрию не грузим)
    assert await calcservice.effective_scale([p], printer, 100) == (100, False, None)
    # в пределах лимита (256/100 = 2.56 → максимум 256%)
    assert await calcservice.effective_scale([p], printer, 200) == (200, False, 256)
    # больше лимита → кламп до максимально возможного + флаг
    assert await calcservice.effective_scale([p], printer, 500) == (256, True, 256)
    # уменьшение всегда можно
    assert await calcservice.effective_scale([p], printer, 50) == (50, False, 256)


async def test_effective_scale_unknown_geometry_no_clamp(tmp_path):
    p = tmp_path / "model.step"
    p.write_text("no mesh", encoding="utf-8")
    printer = SimpleNamespace(machine_profile=str(tmp_path / "нет.json"))
    assert await calcservice.effective_scale([p], printer, 300) == (300, False, None)


def test_snapshot_scale_pct():
    assert calcservice._snapshot_scale_pct(SimpleNamespace(breakdown={"scale_pct": 150})) == 150
    assert calcservice._snapshot_scale_pct(SimpleNamespace(breakdown={})) == 100
    assert calcservice._snapshot_scale_pct(SimpleNamespace(breakdown=None)) == 100
    assert calcservice._snapshot_scale_pct(SimpleNamespace(breakdown={"scale_pct": "мусор"})) == 100
