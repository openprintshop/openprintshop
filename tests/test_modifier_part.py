"""Непечатаемые детали .3mf: modifier_part / support_enforcer / support_blocker.

Модификатор — это область с ДРУГИМИ НАСТРОЙКАМИ внутри обычной детали; сам он материалом
не печатается. Но геометрия у него настоящая, и без фильтра trimesh/fastmeasure грузили его
как обычное тело: чехол MakerWorld «iPhone 12/13/14/15 open-design» (модификатор
iPhone17e_修改器.STL внутри чехла) мерился как 56.7 см³ вместо 23.4 — быстрый расчёт врал
+118% по граммам.

Вычитающие детали (negative_part) — отдельная история: их мало выкинуть, ими ещё РЕЖУТ тело,
поэтому список держится отдельно. Здесь же регресс на то, что эта обработка не поехала.
"""
import zipfile

import pytest

from app import fastmeasure, geometry, inspect3mf

# --- сборка синтетического .3mf ---------------------------------------------------------

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)

CUBE_MM3 = 1000.0        # куб 10×10×10 — объём одной детали фикстуры


def _cube_object(oid, ox, s=10.0):
    pts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
           (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    pts = [(x + ox, y, z) for x, y, z in pts]
    tris = [(0, 3, 1), (1, 3, 2), (0, 1, 4), (1, 5, 4), (1, 2, 5), (2, 6, 5),
            (2, 3, 6), (3, 7, 6), (3, 0, 7), (0, 4, 7), (4, 5, 7), (5, 6, 7)]
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in pts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return (f'<object id="{oid}" type="model"><mesh>'
            f"<vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>")


def _make_3mf(tmp_path, subtypes, name="mod.3mf"):
    """.3mf из куба на каждый subtype: object id == part id == ключ геометрии trimesh
    (упрощение, как в test_geometry_plate). Кубы разнесены по X — не «наложенные копии»."""
    objs, items, parts = [], [], []
    for i, sub in enumerate(subtypes, 1):
        objs.append(_cube_object(i, i * 30.0))
        items.append(f'<item objectid="{i}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
        parts.append(f'<object id="{i}"><part id="{i}" subtype="{sub}"/></object>')
    core = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            f'<resources>{"".join(objs)}</resources>'
            f'<build>{"".join(items)}</build></model>')
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", core)
        z.writestr("Metadata/model_settings.config",
                   f"<config>{''.join(parts)}</config>")
    return p


# --- разбор subtype'ов -------------------------------------------------------------------

def _cfg(tmp_path, subtypes):
    """Голый .3mf только с model_settings — для тестов самого разбора id."""
    parts = "".join(f'<object id="{i}"><part id="{i}" subtype="{s}"/></object>'
                    for i, s in enumerate(subtypes, 1))
    p = tmp_path / "cfg.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/model_settings.config", f"<config>{parts}</config>")
        z.writestr("3D/3dmodel.model", "<model/>")
    return p


def test_nonprinting_ids_cover_modifier_and_supports(tmp_path):
    p = _cfg(tmp_path, ["normal_part", "modifier_part", "support_enforcer",
                        "support_blocker", "negative_part"])
    assert inspect3mf.nonprinting_part_ids(p) == {"2", "3", "4"}


def test_negative_ids_unchanged_by_modifier(tmp_path):
    # РЕГРЕСС: вычитающие детали живут отдельно — модификатор в их список попасть не должен
    # (их геометрией РЕЖУТ тело, а модификатор вычитать нельзя — он лежит ВНУТРИ детали).
    p = _cfg(tmp_path, ["normal_part", "modifier_part", "negative_part"])
    assert inspect3mf.negative_part_ids(p) == {"3"}


def test_nonprinting_ids_none_for_plain_model(tmp_path):
    assert inspect3mf.nonprinting_part_ids(_cfg(tmp_path, ["normal_part"])) == set()


def test_nonprinting_ids_non_3mf(tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"solid")
    assert inspect3mf.nonprinting_part_ids(stl) == set()


# --- быстрый замер (баг чехла) ------------------------------------------------------------

def test_fastmeasure_skips_modifier_part(tmp_path):
    # 2 куба, второй — модификатор: меряется ровно один (как считает слайсер)
    p = _make_3mf(tmp_path, ["normal_part", "modifier_part"])
    r = fastmeasure.measure_3mf(p)
    assert r is not None
    assert r["n_parts"] == 1
    assert r["v_mm3"] == pytest.approx(CUBE_MM3, rel=1e-6)


def test_fastmeasure_skips_supports_and_negative(tmp_path):
    p = _make_3mf(tmp_path, ["normal_part", "support_enforcer", "support_blocker",
                             "negative_part"])
    r = fastmeasure.measure_3mf(p)
    assert r["n_parts"] == 1
    assert r["v_mm3"] == pytest.approx(CUBE_MM3, rel=1e-6)


def test_fastmeasure_two_normal_parts_unchanged(tmp_path):
    # РЕГРЕСС критического пути: обычная модель меряется целиком, как раньше
    p = _make_3mf(tmp_path, ["normal_part", "normal_part"])
    r = fastmeasure.measure_3mf(p)
    assert r["n_parts"] == 2
    assert r["v_mm3"] == pytest.approx(2 * CUBE_MM3, rel=1e-6)


def test_fastmeasure_all_nonprinting_keeps_all(tmp_path):
    # Страховка: если ВСЁ помечено непечатаемым — разметке не доверяем и меряем всё
    # (иначе оценка молча схлопнется в ноль и уйдёт в грубый bbox-фолбэк).
    p = _make_3mf(tmp_path, ["modifier_part", "modifier_part"])
    r = fastmeasure.measure_3mf(p)
    assert r is not None
    assert r["v_mm3"] == pytest.approx(2 * CUBE_MM3, rel=1e-6)


# --- геометрия (превью / габариты / STL для пресетного слайса) -----------------------------

def test_geometry_drops_modifier_part(tmp_path):
    # load_print_meshes кормит не только превью: _3mf_to_stls экспортирует эти меши
    # слайсеру — модификатор ушёл бы в печать сплошным телом.
    p = _make_3mf(tmp_path, ["normal_part", "modifier_part"])
    meshes = geometry.load_print_meshes([p])
    assert len(meshes) == 1
    assert float(meshes[0].volume) == pytest.approx(CUBE_MM3, rel=1e-6)


def test_geometry_two_normal_parts_unchanged(tmp_path):
    p = _make_3mf(tmp_path, ["normal_part", "normal_part"])
    assert len(geometry.load_print_meshes([p])) == 2


def test_geometry_all_nonprinting_keeps_all(tmp_path):
    p = _make_3mf(tmp_path, ["modifier_part", "support_blocker"])
    assert len(geometry.load_print_meshes([p])) == 2


# --- ручная сборка production-3mf ---------------------------------------------------------

_PROD_CORE = """<?xml version='1.0' encoding='UTF-8'?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">
 <resources>
  <object id="5" type="model">
   <components>
    <component p:path="/3D/Objects/object_9.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
    <component p:path="/3D/Objects/object_9.model" objectid="2" transform="1 0 0 0 1 0 0 0 1 5 0 0"/>
   </components>
  </object>
 </resources>
 <build><item objectid="5" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/></build>
</model>"""

_PROD_SUB = """<?xml version='1.0' encoding='UTF-8'?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model"><mesh>
   <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>
   <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
  </mesh></object>
  <object id="2" type="model"><mesh>
   <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="0" z="1"/></vertices>
   <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
  </mesh></object>
 </resources>
 <build/>
</model>"""


def _make_prod_3mf(tmp_path, sub2):
    """Структура настоящего чехла: верхний объект из двух компонентов, второй — sub2."""
    settings = ('<?xml version="1.0" encoding="UTF-8"?><config><object id="5">'
                '<part id="1" subtype="normal_part"/>'
                f'<part id="2" subtype="{sub2}"/></object></config>')
    p = tmp_path / "prod.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", _PROD_CORE)
        z.writestr("3D/Objects/object_9.model", _PROD_SUB)
        z.writestr("Metadata/model_settings.config", settings)
    return p


def test_production_items_skips_modifier(tmp_path):
    (_oid, parts), = geometry.production_items(_make_prod_3mf(tmp_path, "modifier_part"))
    assert len(parts) == 1                       # компонент-модификатор не собран


def test_production_items_keeps_normal_parts(tmp_path):
    (_oid, parts), = geometry.production_items(_make_prod_3mf(tmp_path, "normal_part"))
    assert len(parts) == 2
