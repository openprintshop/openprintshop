"""Точная привязка деталь→цвет в 3D-вьюере (продолжение b1f9ace).

Раньше клиент красил детали мультицветной сборки ПО КРУГУ (part[i] = colors[i % n]) — для
моделей с назначенными экструдерами цвет мог лечь не на ту деталь. Теперь:
  • geometry.load_print_meshes_with_ids отдаёт (mesh, part_id, src_path), НЕ меняя набор/порядок
    мешей load_print_meshes (тот кормит pricing/slicing — регрессы недопустимы, поэтому парность
    проверяем по каждой ветке, до которой дёшево дотянуться);
  • webapp._model3d_glb_bytes зашивает в имя узла GLB слот цвета детали (part{i}_e{ext}_s{slot}),
    слот = порядковый номер занятого экструдера по возрастанию — ровно порядок
    inspect3mf.ModelInfo.used_colours (mc_colors), которым клиент строит colors_sel.
"""
import re
import zipfile

import pytest

trimesh = pytest.importorskip("trimesh")

from app import geometry, webapp  # noqa: E402

# --- сборка синтетического .3mf (паттерн test_geometry_plate) --------------------------
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


def _cube_xml(ox, oy, oz, s=10.0):
    pts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
           (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    pts = [(x + ox, y + oy, z + oz) for x, y, z in pts]
    tris = [(0, 3, 1), (1, 3, 2), (0, 1, 4), (1, 5, 4), (1, 2, 5), (2, 6, 5),
            (2, 3, 6), (3, 7, 6), (3, 0, 7), (0, 4, 7), (4, 5, 7), (5, 6, 7)]
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in pts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return v, t


def _model_xml(object_offsets, items):
    """3dmodel.model: меш-куб на каждый object id; items = [(oid, 'tx ty tz')]."""
    objs = []
    for oid, off in object_offsets.items():
        v, t = _cube_xml(*off)
        objs.append(f'<object id="{oid}" type="model"><mesh>'
                    f"<vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>")
    its = "".join(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {tr}"/>'
                  for oid, tr in items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources>{"".join(objs)}</resources>'
        f"<build>{its}</build></model>"
    )


def _settings_xml(part_extruder):
    """model_settings.config: object id == part id == gname; экструдер на <part> (или без)."""
    blocks = []
    for pid, ext in part_extruder.items():
        meta = f'<metadata key="extruder" value="{ext}"/>' if ext else ""
        blocks.append(f'<object id="{pid}">'
                      f'<part id="{pid}" subtype="normal_part">{meta}</part></object>')
    return f"<config>{''.join(blocks)}</config>".encode()


def _make_3mf(tmp_path, name, object_offsets, part_extruder=None, filament_colours=None):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model",
                   _model_xml(object_offsets,
                              [(oid, f"{off[0]} {off[1]} {off[2]}")
                               for oid, off in object_offsets.items()]))
        if part_extruder is not None:
            z.writestr("Metadata/model_settings.config", _settings_xml(part_extruder))
        if filament_colours is not None:
            z.writestr("Metadata/project_settings.config",
                       ('{"filament_colour": ['
                        + ",".join(f'"{c}"' for c in filament_colours) + "]}").encode())
    return p


# --- geometry.load_print_meshes_with_ids: парность со старой функцией + сами id ---------


def _sig(m):
    return (len(m.faces), round(float(m.vertices.sum()), 4))


def test_with_ids_matches_load_print_meshes_and_carries_ids(tmp_path):
    p = _make_3mf(tmp_path, "two.3mf", {1: (0, 0, 0), 2: (30, 0, 0)}, part_extruder={1: 2, 2: 5})
    old = geometry.load_print_meshes([p])
    new = geometry.load_print_meshes_with_ids([p])
    assert [_sig(m) for m in old] == [_sig(m) for m, _pid, _src in new]   # тот же набор и порядок
    assert sorted(pid for _m, pid, _src in new) == ["1", "2"]
    assert all(str(src) == str(p) for _m, _pid, src in new)


def test_with_ids_dedup_piled_keeps_id(tmp_path):
    # две НАЛОЖЕННЫЕ копии одной детали (два item одного объекта в одной точке) → дедуп до одной,
    # part-id при этом сохраняется (раньше _dedup_piled отдавал голые меши)
    p = tmp_path / "piled.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model",
                   _model_xml({1: (0, 0, 0)}, [(1, "0 0 0"), (1, "0 0 0")]))
        z.writestr("Metadata/model_settings.config", _settings_xml({1: 1}))
    old = geometry.load_print_meshes([p])
    new = geometry.load_print_meshes_with_ids([p])
    assert len(old) == len(new) == 1
    assert new[0][1] == "1"


def test_with_ids_stl_has_no_part_id(tmp_path):
    f = tmp_path / "solo.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(str(f))
    new = geometry.load_print_meshes_with_ids([f])
    assert len(new) == 1
    assert new[0][1] is None and str(new[0][2]) == str(f)
    assert _sig(geometry.load_print_meshes([f])[0]) == _sig(new[0][0])


# --- production-3mf: пары (mesh, part_id) и парность обёртки (паттерн test_production_3mf) ---

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
 <build>
  <item objectid="5" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>
 </build>
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


def test_production_items_with_ids_pairs(tmp_path):
    p = tmp_path / "prod.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", _PROD_CORE)
        z.writestr("3D/Objects/object_9.model", _PROD_SUB)
    (oid, pairs), = geometry.production_items_with_ids(p)
    assert oid == "5"
    assert [pid for _m, pid in pairs] == ["1", "2"]
    # обёртка production_items — те же меши в том же порядке
    (_oid2, parts), = geometry.production_items(p)
    assert [_sig(m) for m in parts] == [_sig(m) for m, _pid in pairs]


# --- webapp._model3d_glb_bytes: слот цвета в имени узла GLB ------------------------------


def _glb_names(tmp_path_cache, monkeypatch, model_path):
    from app import config
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path_cache)
    data = webapp._model3d_glb_bytes([str(model_path)])
    assert data and data[:4] == b"glTF"
    loaded = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb")
    return data, dict(loaded.geometry)


def test_glb_node_names_carry_slots_matching_mc_colors_order(tmp_path, monkeypatch):
    # экструдеры 2 и 5 (с «дырами» в нумерации) + палитра из 5 филаментов:
    # used_colours (mc_colors) = [цвет №2, цвет №5] → слоты: экструдер 2 → s0, экструдер 5 → s1
    p = _make_3mf(tmp_path, "mc.3mf", {1: (0, 0, 0), 2: (30, 0, 0)},
                  part_extruder={1: 2, 2: 5},
                  filament_colours=["#FF0000FF", "#00FF00FF", "#0000FFFF",
                                    "#FFFF00FF", "#00FFFFFF"])
    _data, geoms = _glb_names(tmp_path / "cache", monkeypatch, p)
    es = {}
    for name, g in geoms.items():
        m = re.fullmatch(r"part\d+_e(\d+)_s(\d+)", name)
        assert m, f"имя узла без слота: {name}"
        es[int(m.group(1))] = (int(m.group(2)), g)
    assert set(es) == {2, 5}
    assert es[2][0] == 0 and es[5][0] == 1
    # слот привязан к ПРАВИЛЬНОЙ детали: деталь экструдера 2 — куб у нуля, экструдера 5 — сдвинутый
    assert float(es[2][1].bounds[1][0]) < 30.0 <= float(es[5][1].bounds[0][0])


def test_glb_node_names_slots_without_palette(tmp_path, monkeypatch):
    # палитры в .3mf нет (mc_colors пуст) → слоты всё равно по возрастанию экструдеров
    p = _make_3mf(tmp_path, "nopal.3mf", {1: (0, 0, 0), 2: (30, 0, 0)},
                  part_extruder={1: 2, 2: 5})
    _data, geoms = _glb_names(tmp_path / "cache", monkeypatch, p)
    assert sorted(n[n.index("_e"):] for n in geoms) == ["_e2_s0", "_e5_s1"]


def test_glb_node_names_plain_without_extruders(tmp_path, monkeypatch):
    # деталей с экструдерами нет → прежние имена part0..N (клиент красит по индексу, как раньше)
    p = _make_3mf(tmp_path, "plain.3mf", {1: (0, 0, 0), 2: (30, 0, 0)},
                  part_extruder={1: None, 2: None})
    _data, geoms = _glb_names(tmp_path / "cache", monkeypatch, p)
    assert sorted(geoms) == ["part0", "part1"]


def test_glb_cache_key_bumped(tmp_path, monkeypatch):
    # смена схемы имён узлов/привязки обязана сменить ключ кэша GLB — иначе старые файлы (без
    # слотов / с мусорной привязкой из дублей part-id) отдавались бы вечно. v6 = детектор
    # мис-ассембли production-3mf ловит дубли компонентов и при заявленном face_count (заказ #136:
    # верхняя панель лежала в кэше нарисованной ДВАЖДЫ разными цветами); v5 = общий кап граней
    # сцены (много-детальные StlAggregator-наборы больше не пухнут, «Сова Zippy»); v4 = разрезание
    # крашеных деталей по paint_color + слоты по группам имён STL (24.07, вторая волна);
    # v3 = фикс part_extruders для Bambu-обёрток («Тимми»); v2 = слоты в именах узлов.
    p = _make_3mf(tmp_path, "cached.3mf", {1: (0, 0, 0)}, part_extruder={1: 1})
    cache_dir = tmp_path / "cache"
    _glb_names(cache_dir, monkeypatch, p)
    files = list((cache_dir / "geo").glob("glb_*.glb"))
    assert files and all("_v6.glb" in f.name for f in files)
