"""Тест безопасного булева вычитания вычитающих деталей (#3ca1fe86 / #329fc86b)."""
import zipfile
from pathlib import Path

import pytest
import trimesh

from app import geometry


def test_rests_on_plate_detects_floating():
    """OpenTodo 9d0c10f5: разброс низов экземпляров > стола = «парящая» (нефизичная) раскладка."""
    base = trimesh.creation.box((10, 10, 10))

    def inst(zmin):
        m = base.copy()
        m.apply_translation([0, 0, zmin - m.bounds[0][2]])
        return (m, "h", (0, 0, 0), 17.0, "1")

    assert geometry._rests_on_plate([inst(0), inst(5)]) is True        # оба у стола
    assert geometry._rests_on_plate([inst(0), inst(400)]) is False     # один парит на 400 мм


def test_drop_to_plate_centers_and_grounds():
    """OpenTodo 9d0c10f5: деталь опускается на стол (низ→0) и центрируется по XY."""
    m = trimesh.creation.box((20, 20, 20))
    m.apply_translation([100, 50, 300])
    geometry._drop_to_plate([m])
    b = m.bounds
    assert abs(b[0][2]) < 1e-6                                          # низ на столе
    assert abs((b[0][0] + b[1][0]) / 2) < 1e-6 and abs((b[0][1] + b[1][1]) / 2) < 1e-6


def test_unique_native_geoms_uses_raw_not_scaled():
    """OpenTodo 9d0c10f5: при битых трансформах берём СЫРУЮ геометрию (правильный размер),
    по одной на уникальный хэш — а не раздутый мировой меш (как у Donald Duck Urban: ×5.95)."""
    sc = trimesh.Scene()
    base = trimesh.creation.box((20, 30, 10))
    sc.add_geometry(base, geom_name="g1")
    big = base.copy()
    big.apply_scale(5.95)                                              # «битый» мировой меш
    insts = [(big, "H", (0, 0, 0), 1.0, "g1"), (big.copy(), "H", (9, 9, 9), 1.0, "g1")]
    out = geometry._unique_native_geoms(sc, insts)
    assert len(out) == 1                                              # один на уникальный хэш
    ext = out[0].bounds[1] - out[0].bounds[0]
    assert abs(ext[0] - 20) < 1e-3 and abs(ext[2] - 10) < 1e-3        # сырой g1, не ×5.95


def test_subtract_distinct_cutter():
    base = trimesh.creation.box((20, 20, 20))      # 8000 mm3
    cutter = trimesh.creation.box((10, 10, 30))    # пересечение 10×10×20 = 2000
    res = geometry._subtract_negatives([base], [cutter])
    assert 5900 < float(res[0].volume) < 6100      # дырка вырезана


def test_subtract_keeps_when_cutter_not_watertight():
    base = trimesh.creation.box((20, 20, 20))
    bad = trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
    res = geometry._subtract_negatives([base], [bad])   # негерметичный вырез — не режем
    assert abs(float(res[0].volume) - 8000) < 1         # база без изменений (безопасно)


def _inst(box_size, at):
    m = trimesh.creation.box(box_size)
    m.apply_translation(at)
    return (m, "h", None, None, "k")           # формат _scene_instances: важен только [0].bounds


def test_footprint_exceeds_bed_multiplate():
    # детали разнесены на 400 мм по X → раскладка крупнее стола → мультиплейт
    insts = [_inst((20, 20, 20), (0, 0, 0)), _inst((20, 20, 20), (400, 0, 0))]
    assert geometry._footprint_exceeds_bed(insts) is True


def test_footprint_within_bed_single_plate():
    # детали рядом (в пределах стола) → дедуп «наложенных копий» должен работать как раньше
    insts = [_inst((20, 20, 20), (0, 0, 0)), _inst((20, 20, 20), (30, 0, 0))]
    assert geometry._footprint_exceeds_bed(insts) is False


# --- защита от OOM на тираже инстансов тяжёлого меша («100 mini ducks»: 1.07M×101 = 108M граней) ---

def _instanced_faces(scene):
    """Суммарно граней с учётом инстансов (как считает _cap_scene_faces)."""
    counts = {}
    for node in scene.graph.nodes_geometry:
        _, g = scene.graph[node]
        counts[g] = counts.get(g, 0) + 1
    return sum(len(scene.geometry[g].faces) * c for g, c in counts.items() if g in scene.geometry)


def test_cap_scene_faces_reduces_instanced_high_poly_under_budget():
    base = trimesh.creation.icosphere(subdivisions=5)     # ~20480 граней
    scene = trimesh.Scene()
    for i in range(30):                                    # 30 инстансов → ~600k граней
        scene.add_geometry(base.copy(), node_name=f"n{i}",
                           transform=trimesh.transformations.translation_matrix([i * 60.0, 0, 0]))
    before = _instanced_faces(scene)
    budget = 120_000
    assert before > budget                                 # сценарий действительно превышает бюджет
    geometry._cap_scene_faces(scene, budget)
    after = _instanced_faces(scene)
    assert after < before                                  # уникальные меши проредились
    assert after <= budget * 1.4                           # суммарно сведено под бюджет (с допуском дециатора)


def test_cap_scene_faces_leaves_small_model_untouched():
    base = trimesh.creation.box((10, 10, 10))             # 12 граней — заведомо в бюджете
    scene = trimesh.Scene()
    scene.add_geometry(base, node_name="n0")
    before = _instanced_faces(scene)
    geometry._cap_scene_faces(scene, 2_000_000)
    assert _instanced_faces(scene) == before              # ничего не трогаем, если уже в бюджете


# --- модель-ТИРАЖ: режем одну единицу ×count вместо материализации N×тяжёлого меша ----------

def _single_cube_3mf(path):
    """Минимальный .3mf: один объект-куб, один build-item (НЕ тираж)."""
    s = 10.0
    pts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    tris = [(0, 3, 1), (1, 3, 2), (0, 1, 4), (1, 5, 4), (1, 2, 5), (2, 6, 5),
            (2, 3, 6), (3, 7, 6), (3, 0, 7), (0, 4, 7), (4, 5, 7), (5, 6, 7)]
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in pts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             f'<object id="1" type="model"><mesh><vertices>{v}</vertices><triangles>{t}</triangles>'
             '</mesh></object></resources><build>'
             '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build></model>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
          'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.'
          'openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType='
          '"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model"'
            ' Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            '</Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    return path


def test_unit_groups_none_for_non_tiled(tmp_path):
    # одиночная модель (1 экземпляр) — не тираж → обычный путь (None)
    p = _single_cube_3mf(tmp_path / "single.3mf")
    assert geometry.unit_groups([p]) is None


def test_unit_groups_real_ducks_tiled():
    # «100 mini ducks» = 1 уникальный меш ×101 инстанс. unit_groups должен это распознать и
    # вернуть ОДИН меш (полной геометрии, без дециации) + count=101 → слайс режет 1 ед. ×101.
    p = (Path(__file__).resolve().parents[1]
         / "data/downloads/dl_e5c8973e/100 mini ducks.3mf")
    if not p.exists():
        pytest.skip("real '100 mini ducks' .3mf not present in data/downloads")
    groups = geometry.unit_groups([p])
    assert groups is not None and len(groups) == 1        # один уникальный меш-утка
    assert groups[0][1] == 101                            # 101 инстанс
    assert len(groups[0][0].faces) > 1_000_000            # полная геометрия (не проредили)
