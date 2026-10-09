"""Ручная сборка production-3mf (OpenTodo 041be6d4): трансформы, состав, детектор раздува."""
import zipfile

import numpy as np

from app import geometry, inspect3mf

_CORE = """<?xml version='1.0' encoding='UTF-8'?>
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
  <item objectid="5" transform="0 1 0 -1 0 0 0 0 1 100 200 0" printable="1"/>
  <item objectid="5" transform="1 0 0 0 1 0 0 0 1 300 0 0"/>
  <item objectid="5" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="0"/>
 </build>
</model>"""

# два мини-объекта: один треугольник у (0,0,0) и у (1,0,0)
_SUB = """<?xml version='1.0' encoding='UTF-8'?>
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

_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="5"><metadata key="name" value="x"/><metadata face_count="2"/></object>
</config>"""


def _make_3mf(tmp_path):
    p = tmp_path / "prod.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", _CORE)
        z.writestr("3D/Objects/object_9.model", _SUB)
        z.writestr("Metadata/model_settings.config", _SETTINGS)
    return p


def test_mat_from_3mf_translation_and_rotation():
    T = geometry._mat_from_3mf("1 0 0 0 1 0 0 0 1 10 20 30")
    v = T @ np.array([1.0, 1.0, 1.0, 1.0])
    assert np.allclose(v[:3], [11, 21, 31])
    # поворот на 90° (row-vector convention 3MF): x→y
    R = geometry._mat_from_3mf("0 1 0 -1 0 0 0 0 1 0 0 0")
    v2 = R @ np.array([1.0, 0.0, 0.0, 1.0])
    assert np.allclose(v2[:3], [0, 1, 0], atol=1e-9)
    # пустой/битый transform — identity
    assert np.allclose(geometry._mat_from_3mf(None), np.eye(4))
    assert np.allclose(geometry._mat_from_3mf("мусор"), np.eye(4))


def test_production_items_compose(tmp_path):
    p = _make_3mf(tmp_path)
    items = geometry.production_items(p)
    assert len(items) == 2                              # printable="0" пропущен
    (oid1, parts1), (oid2, parts2) = items
    assert oid1 == "5" and len(parts1) == 2 and len(parts2) == 2
    # item 2: чистая трансляция +300 по x; компонент 2 ещё +5 локально
    c1 = parts2[0].vertices.mean(axis=0)
    c2 = parts2[1].vertices.mean(axis=0)
    assert abs((c2 - c1)[0] - 5.0) < 1e-6
    assert abs(c1[0] - (300 + 1 / 3)) < 1e-6            # центр треугольника (1/3, 1/3, 0) + 300
    # item 1: поворот 90° CCW + перенос (100,200). Точные мировые центроиды:
    # part0: локально (1/3,1/3,0) → (-1/3,1/3,0) + (100,200,0)
    # part1: локально (1/3,0,1/3)+5x → (16/3,0,1/3) → (0,16/3,1/3) + (100,200,0)
    r0 = items[0][1][0].vertices.mean(axis=0)
    r1 = items[0][1][1].vertices.mean(axis=0)
    assert np.allclose(r0, [100 - 1 / 3, 200 + 1 / 3, 0], atol=1e-6)
    assert np.allclose(r1, [100, 200 + 16 / 3, 1 / 3], atol=1e-6)


# ElegooSlicer-стиль: face_count НЕ пишется, только <mesh_stat> без числа граней (044a4bcd)
_SETTINGS_ELEGOO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="5"><metadata key="name" value="x"/>
    <part id="1" subtype="normal_part">
      <mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0"/>
    </part>
  </object>
</config>"""


def _make_3mf_elegoo(tmp_path):
    p = tmp_path / "prod_elegoo.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", _CORE)
        z.writestr("3D/Objects/object_9.model", _SUB)
        z.writestr("Metadata/model_settings.config", _SETTINGS_ELEGOO)
    return p


def test_elegoo_no_face_count_detected_by_dup_geoms(tmp_path):
    """«Пивные открывашка» (044a4bcd): Elegoo не заявляет face_count → детектор сверки слеп.
    Ловим по сигнатуре бага: production-3mf + дубли содержимого в уникальных геометриях."""
    import trimesh
    p = _make_3mf_elegoo(tmp_path)
    assert inspect3mf.declared_face_total(p) == 0
    assert inspect3mf.has_production_components(p) is True

    def tri(dx=0.0):
        return trimesh.Trimesh(vertices=np.eye(3) + [dx, 0, 0], faces=[[0, 1, 2]], process=False)

    # trimesh положил ОДИН слитый меш в два ключа geometry → мис-ассембли
    dup = trimesh.Scene()
    dup.add_geometry(tri(), geom_name="1")
    dup.add_geometry(tri(), geom_name="2")
    assert geometry._trimesh_misassembled(p, dup) is True
    # здоровая сцена: геометрии разные → доверяем trimesh
    ok = trimesh.Scene()
    ok.add_geometry(tri(), geom_name="1")
    ok.add_geometry(tri(7.0), geom_name="2")
    assert geometry._trimesh_misassembled(p, ok) is False
    # обычный (не production) 3mf с дублями — НЕ триггерим ручную сборку
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("3D/3dmodel.model", _SUB)
        z.writestr("Metadata/model_settings.config", _SETTINGS_ELEGOO)
    assert inspect3mf.has_production_components(plain) is False
    assert geometry._trimesh_misassembled(plain, dup) is False


def test_declared_face_total_and_detector(tmp_path):
    p = _make_3mf(tmp_path)
    assert inspect3mf.declared_face_total(p) == 2
    # сцена, раздутая кратно (как делает trimesh у kitty) → детектор срабатывает
    import trimesh
    big = trimesh.Trimesh(vertices=np.random.rand(12, 3),
                          faces=np.array([[0, 1, 2]] * 9), process=False)
    scene = trimesh.Scene()
    scene.add_geometry(big, geom_name="g1")
    assert geometry._trimesh_misassembled(p, scene) is True    # 9 граней > 2×1.5
    small = trimesh.Scene()
    small.add_geometry(trimesh.Trimesh(vertices=np.eye(3), faces=[[0, 1, 2]], process=False),
                       geom_name="g2")
    assert geometry._trimesh_misassembled(p, small) is False   # 1 ≤ 3


# Bambu заводит ОТДЕЛЬНЫЙ <object> на каждое размещение (6 стержней = 6 объектов по 914 граней),
# а уникальная геометрия у trimesh на них одна → declared завышен, и раздув уходит под допуск ×1.5
_SETTINGS_REPEATED = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="5"><metadata key="name" value="x"/><metadata face_count="2"/></object>
  <object id="6"><metadata key="name" value="x"/><metadata face_count="2"/></object>
  <object id="7"><metadata key="name" value="x"/><metadata face_count="2"/></object>
</config>"""


def test_dup_geoms_detected_even_when_face_count_declared(tmp_path):
    """CATAN card holder box (заказ #136): face_count заявлен, но повторы размещений разбавили
    сверку (35594 против «заявленных» 24678 = ×1.44 < 1.5) — верхняя панель рисовалась ДВАЖДЫ,
    вторым цветом. Сигнатуру дублей проверяем и при известном declared."""
    import trimesh
    p = tmp_path / "prod_repeated.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", _CORE)
        z.writestr("3D/Objects/object_9.model", _SUB)
        z.writestr("Metadata/model_settings.config", _SETTINGS_REPEATED)
    assert inspect3mf.declared_face_total(p) == 6        # 3 размещения × 2 грани

    def quad(dx=0.0):
        return trimesh.Trimesh(vertices=np.vstack([np.eye(3), [1, 1, 1]]) + [dx, 0, 0],
                               faces=[[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], process=False)

    # 8 граней уникальных геометрий ≤ 6×1.5 — сверка молчит, но содержимое ключей одинаковое
    dup = trimesh.Scene()
    dup.add_geometry(quad(), geom_name="9")
    dup.add_geometry(quad(), geom_name="10")
    assert geometry._trimesh_misassembled(p, dup) is True
    # здоровая сцена того же размера: ключи с РАЗНЫМ содержимым → trimesh не трогаем
    ok = trimesh.Scene()
    ok.add_geometry(quad(), geom_name="9")
    ok.add_geometry(quad(7.0), geom_name="10")
    assert geometry._trimesh_misassembled(p, ok) is False


# --- trimesh перечитывает sub-model на КАЖДЫЙ компонент (скорпион 09.10.2026) -------------
# 4 компонента на один файл: trimesh разберёт его 4 раза (×2+ к весу архива), ручная сборка — раз
_CORE_REREAD = """<?xml version='1.0' encoding='UTF-8'?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">
 <resources>
  <object id="5" type="model">
   <components>
    <component p:path="/3D/Objects/object_9.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
    <component p:path="/3D/Objects/object_9.model" objectid="2" transform="1 0 0 0 1 0 0 0 1 5 0 0"/>
    <component p:path="/3D/Objects/object_9.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 10 0 0"/>
    <component p:path="/3D/Objects/object_9.model" objectid="2" transform="1 0 0 0 1 0 0 0 1 15 0 0"/>
   </components>
  </object>
 </resources>
 <build>
  <item objectid="5" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>
 </build>
</model>"""

_SETTINGS_NEG = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="5"><metadata key="name" value="x"/>
    <part id="2" subtype="negative_part"><metadata key="name" value="cut"/></part>
  </object>
</config>"""


def _make_reread_3mf(tmp_path, settings=_SETTINGS, name="reread.3mf"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", _CORE_REREAD)
        z.writestr("3D/Objects/object_9.model", _SUB)
        z.writestr("Metadata/model_settings.config", settings)
    return p


def _no_trimesh(monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("раздутый production-3mf отдали trimesh")
    monkeypatch.setattr(geometry.trimesh, "load", boom)


def test_xml_mb_counts_trimesh_rereads(tmp_path):
    p = _make_reread_3mf(tmp_path)
    core, sub = len(_CORE_REREAD.encode()), len(_SUB.encode())
    real, via_trimesh = geometry._3mf_xml_mb(p)
    assert abs(real - (core + sub) / 1e6) < 1e-9
    assert abs(via_trimesh - (core + 4 * sub) / 1e6) < 1e-9      # 3 лишних разбора sub-model
    # путь с ручной сборкой платит только за сами файлы, trimesh — за все перечитывания
    assert abs(geometry.source_mesh_mb([p]) - real) < 1e-9
    assert abs(geometry.source_mesh_mb([p], via_trimesh=True) - via_trimesh) < 1e-9
    # обычный production без повторов — цена та же, что и раньше
    plain = _make_3mf(tmp_path)
    r2, t2 = geometry._3mf_xml_mb(plain)
    assert geometry._manual_first(plain) is False and t2 <= r2 * geometry._REPARSE_TOL


def test_inflated_production_never_reaches_trimesh(tmp_path, monkeypatch):
    """Скорпион: ×38 повторных разборов, trimesh 3 минуты и 9.2 ГБ, а результат всё равно ушёл
    в ручную сборку. Теперь ручная сборка сразу — и в обычной загрузке, и в проверке тиража."""
    p = _make_reread_3mf(tmp_path)
    real, via_trimesh = geometry._3mf_xml_mb(p)
    assert geometry._manual_first(p) is True
    # потолок между ценой ручной сборки и ценой trimesh — ровно случай скорпиона
    monkeypatch.setattr(geometry, "MESH_SRC_LIMIT_MB", (real + via_trimesh) / 2)
    _no_trimesh(monkeypatch)
    assert geometry.too_heavy_to_load([p]) == ""
    assert "потолка" in geometry.too_heavy_to_load([p], via_trimesh=True)
    assert geometry.unit_groups([p]) is None                    # тираж trimesh-ом не проверяем
    trip = geometry.load_print_meshes_with_ids([p])
    assert [pid for _m, pid, _src in trip] == ["1", "2", "1", "2"]
    xs = sorted(round(float(m.vertices[:, 0].min()), 6) for m, _pid, _src in trip)
    assert xs == [0.0, 5.0, 10.0, 15.0]                         # 4 детали на своих местах


def test_manual_failure_still_guards_trimesh(tmp_path, monkeypatch):
    """Ручная сборка не вышла → прежний путь через trimesh, но под предохранителем ПО ЦЕНЕ
    trimesh: на него бомба и рассчитана."""
    p = _make_reread_3mf(tmp_path)
    real, via_trimesh = geometry._3mf_xml_mb(p)
    monkeypatch.setattr(geometry, "MESH_SRC_LIMIT_MB", (real + via_trimesh) / 2)
    monkeypatch.setattr(geometry, "production_items_with_ids", lambda _p: [])
    _no_trimesh(monkeypatch)
    assert geometry.load_print_meshes([p]) == []


def test_negative_parts_keep_trimesh_path_and_its_price(tmp_path, monkeypatch):
    """С вычитающими деталями ручная сборка не годится — грузит trimesh, и предохранитель
    берёт с такого файла полную цену trimesh."""
    p = _make_reread_3mf(tmp_path, _SETTINGS_NEG, "reread_neg.3mf")
    real, via_trimesh = geometry._3mf_xml_mb(p)
    assert geometry._manual_first(p) is False
    assert abs(geometry.source_mesh_mb([p]) - via_trimesh) < 1e-9
    monkeypatch.setattr(geometry, "MESH_SRC_LIMIT_MB", (real + via_trimesh) / 2)
    _no_trimesh(monkeypatch)
    assert geometry.load_print_meshes([p]) == []
