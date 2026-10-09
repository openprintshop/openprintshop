"""Plate-aware разбор мультиплейт .3mf (3811f970 / a6452eaf).

КРИТИЧЕСКИЙ ПУТЬ: одноплейтные модели (подавляющее большинство) обязаны грузиться
ТОЧНО как раньше — гейт нового пути строго на «столов > 1». Здесь:
(a) регресс-тест: одноплейтная сборка отдаёт ТОТ ЖЕ набор мешей, что load_print_meshes;
(b) мультиплейт .3mf бьётся на N столов, геометрия сохраняется (сумма граней = старой),
    а пер-стол счёт деталей отличается от старого «лумпа» в один --arrange.

Фикстура — настоящий маленький .3mf (zip): объекты с inline-мешем + model_settings.config
с <plate>. Детерминированно и быстро, без слайсера.
"""
import zipfile
from pathlib import Path

import pytest
import trimesh

from app import geometry, inspect3mf

# --- сборка синтетического .3mf --------------------------------------------------------
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


def _cube(ox, oy, oz, s=10.0):
    pts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
           (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    pts = [(x + ox, y + oy, z + oz) for x, y, z in pts]
    tris = [(0, 3, 1), (1, 3, 2), (0, 1, 4), (1, 5, 4), (1, 2, 5), (2, 6, 5),
            (2, 3, 6), (3, 7, 6), (3, 0, 7), (0, 4, 7), (4, 5, 7), (5, 6, 7)]
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in pts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return v, t


def _model(object_offsets):
    """3dmodel.model с одним мешем-кубом на каждый object id (id == gname в trimesh)."""
    objs, items = [], []
    for oid, off in object_offsets.items():
        v, t = _cube(*off)
        objs.append(f'<object id="{oid}" type="model"><mesh>'
                    f"<vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>")
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 '
                     f'{off[0]} {off[1]} {off[2]}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources>{"".join(objs)}</resources>'
        f'<build>{"".join(items)}</build></model>'
    )


def _settings(plates, parts):
    """model_settings.config: object id == part id == gname (упрощение для теста).
    plates: список списков object_id; parts: набор object_id (для <object>/<part>)."""
    obj_blocks = "".join(
        f'<object id="{o}"><part id="{o}" subtype="normal_part"/></object>' for o in parts
    )
    plate_blocks = ""
    for i, objs in enumerate(plates, 1):
        insts = "".join(
            f'<model_instance><metadata key="object_id" value="{o}"/></model_instance>'
            for o in objs
        )
        plate_blocks += f'<plate><metadata key="plater_id" value="{i}"/>{insts}</plate>'
    return f"<config>{obj_blocks}{plate_blocks}</config>".encode()


def _make_3mf(tmp_path, name, object_offsets, plates, *, with_settings=True):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", _model(object_offsets))
        if with_settings:
            z.writestr("Metadata/model_settings.config",
                       _settings(plates, sorted(object_offsets)))
    return p


# --- (a) РЕГРЕСС критического пути: одноплейтная модель не меняется ---------------------

def test_single_plate_loads_same_as_before(tmp_path):
    # одна плита с двумя деталями рядом → новый путь НЕ активируется, старый отдаёт всё
    offs = {1: (0, 0, 0), 2: (30, 0, 0)}
    p = _make_3mf(tmp_path, "single.3mf", offs, plates=[[1, 2]])

    assert geometry.load_print_meshes_by_plate([p]) == []   # гейт: не мультиплейт
    meshes = geometry.load_print_meshes([p])
    assert len(meshes) == 2                                   # обе детали (поведение как раньше)


def test_no_model_settings_falls_back(tmp_path):
    # нет model_settings.config → нельзя узнать столы → старый путь (критический путь цел)
    offs = {1: (0, 0, 0), 2: (30, 0, 0)}
    p = _make_3mf(tmp_path, "nocfg.3mf", offs, plates=[], with_settings=False)
    assert geometry.load_print_meshes_by_plate([p]) == []
    assert len(geometry.load_print_meshes([p])) == 2


def test_multi_input_not_treated_as_multiplate(tmp_path):
    # несколько входных файлов → мультиплейт-детект отключён (только для одиночного .3mf)
    offs = {1: (0, 0, 0), 2: (300, 0, 0)}
    p = _make_3mf(tmp_path, "mp.3mf", offs, plates=[[1], [2]])
    stl = tmp_path / "extra.stl"
    trimesh.creation.box((10, 10, 10)).export(str(stl))
    assert geometry.load_print_meshes_by_plate([p, stl]) == []


# --- (b) МУЛЬТИПЛЕЙТ: бьётся на N столов; геометрия сохраняется; счёт отличается --------

def test_multiplate_splits_into_n_plates(tmp_path):
    # 3 стола: [1], [2,3], [4]; детали разнесены по X так, что общий след > стола
    offs = {1: (0, 0, 0), 2: (300, 0, 0), 3: (330, 0, 0), 4: (600, 0, 0)}
    p = _make_3mf(tmp_path, "mp3.3mf", offs, plates=[[1], [2, 3], [4]])

    plates = geometry.load_print_meshes_by_plate([p])
    assert len(plates) == 3
    assert [len(g) for g in plates] == [1, 2, 1]             # пер-стол счёт деталей


def test_multiplate_conserves_geometry_vs_lumped(tmp_path):
    # honest sum: суммарная геометрия по столам == старому «лумпу» (ничего не потеряно/удвоено)
    offs = {1: (0, 0, 0), 2: (300, 0, 0), 3: (330, 0, 0), 4: (600, 0, 0)}
    p = _make_3mf(tmp_path, "mp4.3mf", offs, plates=[[1], [2, 3], [4]])

    plates = geometry.load_print_meshes_by_plate([p])
    per_plate_faces = sum(sum(len(m.faces) for m in g) for g in plates)
    lumped = geometry.load_print_meshes([p])
    lumped_faces = sum(len(m.faces) for m in lumped)
    assert per_plate_faces == lumped_faces                   # геометрия сохранена

    # и при этом пер-стол РАЗБИЕНИЕ отличается от единого списка (старого лумпа):
    assert len(plates) == 3 and len(lumped) == 4             # 3 стол-группы ≠ 4 детали одним списком


def test_multiplate_keeps_identical_copies_on_a_plate(tmp_path):
    # КРИТИЧНО (баг card shuffler): три ОДИНАКОВЫЕ детали, разложенные кучно на ОДНОМ столе —
    # это три отдельные детали, их нельзя схлопывать в одну (иначе вес занижается втрое).
    # Внутри стола дедуп «наложенных копий» НЕ применяется — каждый инстанс учитывается.
    p = tmp_path / "copies.3mf"
    v, t = _cube(0, 0, 0)
    items = "".join(
        f'<item objectid="1" transform="1 0 0 0 1 0 0 0 1 {dx} 0 0"/>' for dx in (0, 15, 30)
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model"><mesh>'
        f"<vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>"
        f"</resources><build>{items}</build></model>"
    )
    # один стол со ВСЕМИ тремя инстансами + второй стол (чтобы это считалось мультиплейтом)
    settings = (
        b'<config>'
        b'<object id="1"><part id="1" subtype="normal_part"/></object>'
        b'<object id="2"><part id="2" subtype="normal_part"/></object>'
        b'<plate><model_instance><metadata key="object_id" value="1"/></model_instance></plate>'
        b'<plate><model_instance><metadata key="object_id" value="2"/></model_instance></plate>'
        b'</config>'
    )
    # нужен и объект 2 в .model, иначе trimesh его не разместит — добавим его вторым items'ом
    v2, t2 = _cube(500, 0, 0)
    model = model.replace(
        "</resources>",
        f'<object id="2" type="model"><mesh><vertices>{v2}</vertices>'
        f"<triangles>{t2}</triangles></mesh></object></resources>",
    ).replace("</build>", '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 500 0 0"/></build>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", settings)

    obj = trimesh.load(str(p))
    assert isinstance(obj, trimesh.Scene)
    n_obj1 = sum(1 for n in obj.graph.nodes_geometry if str(obj.graph[n][1]).split("_")[0] == "1")
    if n_obj1 < 3:
        pytest.skip("trimesh collapsed duplicate instances at load; nothing to guard")

    plates = geometry.load_print_meshes_by_plate([p])
    assert len(plates) == 2
    assert len(plates[0]) == 3       # ВСЕ три кучных копии сохранены (не схлопнуты в 1)
    assert len(plates[1]) == 1


def test_multiplate_shared_part_distributed_once_each(tmp_path):
    # один меш (gname) на двух столах → trimesh даёт 2 инстанса; каждый учитывается на СВОЁМ
    # столе ровно один раз (честная сумма 2 копий, без двойного счёта и без потери) — кейс
    # зеркальной детали Mark 85 (part 3 на столах 7 и 8).
    # два инстанса одного объекта: строим .model с двумя <item> на тот же objectid
    p = tmp_path / "shared.3mf"
    v, t = _cube(0, 0, 0)
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model"><mesh>'
        f"<vertices>{v}</vertices><triangles>{t}</triangles></mesh></object></resources>"
        '<build>'
        '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 300 0 0"/>'
        "</build></model>"
    )
    settings = (
        b'<config>'
        b'<object id="1"><part id="1" subtype="normal_part"/></object>'
        b'<plate><model_instance><metadata key="object_id" value="1"/></model_instance></plate>'
        b'<plate><model_instance><metadata key="object_id" value="1"/></model_instance></plate>'
        b'</config>'
    )
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", settings)

    # sanity: парсер видит 2 стола, оба заявляют part "1"
    assert inspect3mf.plate_part_ids(p) == [{"1"}, {"1"}]

    obj = trimesh.load(str(p))
    assert isinstance(obj, trimesh.Scene)
    n_instances = len(list(obj.graph.nodes_geometry))
    if n_instances < 2:
        # некоторые сборки trimesh схлопывают одинаковые объекты — пропускаем (нечего раздавать)
        pytest.skip("trimesh collapsed duplicate instances; nothing to distribute")

    plates = geometry.load_print_meshes_by_plate([p])
    assert len(plates) == 2
    assert [len(g) for g in plates] == [1, 1]                # по одному инстансу на каждый стол


# --- (c) object_id != part_id (НАСТОЯЩАЯ раскладка Bambu) — закрываем разрыв ревью ---------
# Mark 85 и Card Shuffler в реале имеют object_id != part_id; прежние фикстуры это НЕ покрывали
# (object==part). Здесь два кейса: (1) обёртка <object><component> → trimesh даёт ключ = part-id
# (основной путь); (2) геометрия адресована по object_id → срабатывает запасной object-fallback.

def _make_wrapper_3mf(tmp_path, name, leaves, wrappers, plates):
    """leaves: {part_id: offset} (реальные меши), wrappers: {object_id: part_id} (обёртки с
    <component>), plates: список списков object_id. Это РОВНО раскладка Bambu (object != part)."""
    objs = []
    for pid, off in leaves.items():
        v, t = _cube(*off)
        objs.append(f'<object id="{pid}" type="model"><mesh>'
                    f"<vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>")
    for oid, pid in wrappers.items():
        objs.append(f'<object id="{oid}" type="model"><components>'
                    f'<component objectid="{pid}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
                    f"</components></object>")
    items = "".join(f'<item objectid="{o}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
                    for grp in plates for o in grp)
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources>{"".join(objs)}</resources><build>{items}</build></model>')
    obj_blocks = "".join(f'<object id="{o}"><part id="{p}" subtype="normal_part"/></object>'
                         for o, p in wrappers.items())
    plate_blocks = ""
    for i, grp in enumerate(plates, 1):
        insts = "".join(f'<model_instance><metadata key="object_id" value="{o}"/></model_instance>'
                        for o in grp)
        plate_blocks += f'<plate><metadata key="plater_id" value="{i}"/>{insts}</plate>'
    settings = f"<config>{obj_blocks}{plate_blocks}</config>".encode()
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", settings)
    return p


def test_multiplate_wrapper_component_object_neq_part(tmp_path):
    # настоящая Bambu-раскладка: обёртки 7→part6, 9→part8; trimesh ключует по part-id.
    p = _make_wrapper_3mf(tmp_path, "wrap.3mf",
                          leaves={6: (0, 0, 0), 8: (300, 0, 0)},
                          wrappers={7: 6, 9: 8},
                          plates=[[7], [9]])
    # парсер разворачивает object_id → part-id
    assert inspect3mf.plate_part_ids(p) == [{"6"}, {"8"}]
    assert inspect3mf.plate_object_ids(p) == [{"7"}, {"9"}]
    obj = trimesh.load(str(p))
    keys = sorted(str(obj.graph[n][1]).split("_")[0] for n in obj.graph.nodes_geometry)
    assert keys == ["6", "8"]                                # trimesh ключует по part-id
    plates = geometry.load_print_meshes_by_plate([p])
    assert len(plates) == 2 and [len(g) for g in plates] == [1, 1]   # НЕ схлопнулось в orphans


def test_multiplate_object_keyed_geometry_uses_object_fallback(tmp_path):
    # граничный кейс из ревью: геометрия адресована по OBJECT-id (ключ != part-id).
    # Плоские объекты 7/9 (ключ trimesh = 7/9), но model_settings: object7→part6, object9→part8.
    # Сопоставление по part-id промахивается → должен сработать запасной object-fallback.
    # Плоские объекты-меши с id == object_id (7, 9) → trimesh ключует геометрию по object_id.
    v7, t7 = _cube(0, 0, 0)
    v9, t9 = _cube(300, 0, 0)
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             f'<object id="7" type="model"><mesh><vertices>{v7}</vertices>'
             f"<triangles>{t7}</triangles></mesh></object>"
             f'<object id="9" type="model"><mesh><vertices>{v9}</vertices>'
             f"<triangles>{t9}</triangles></mesh></object></resources>"
             '<build><item objectid="7" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
             '<item objectid="9" transform="1 0 0 0 1 0 0 0 1 300 0 0"/></build></model>')
    settings = (b'<config>'
                b'<object id="7"><part id="6" subtype="normal_part"/></object>'
                b'<object id="9"><part id="8" subtype="normal_part"/></object>'
                b'<plate><metadata key="plater_id" value="1"/>'
                b'<model_instance><metadata key="object_id" value="7"/></model_instance></plate>'
                b'<plate><metadata key="plater_id" value="2"/>'
                b'<model_instance><metadata key="object_id" value="9"/></model_instance></plate>'
                b'</config>')
    p = tmp_path / "objkeyed.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", settings)

    assert inspect3mf.plate_part_ids(p) == [{"6"}, {"8"}]    # part-id (для основного пути)
    assert inspect3mf.plate_object_ids(p) == [{"7"}, {"9"}]  # object-id (для запасного)
    obj = trimesh.load(str(p))
    keys = sorted(str(obj.graph[n][1]).split("_")[0] for n in obj.graph.nodes_geometry)
    assert keys == ["7", "9"]                                # ключ геометрии = OBJECT-id
    plates = geometry.load_print_meshes_by_plate([p])
    # без object-fallback тут было бы [[], [], [orphans=2]] (баг ревью); с ним — честный сплит
    assert len(plates) == 2 and [len(g) for g in plates] == [1, 1]


def test_real_mark85_splits_into_nine_plates():
    # smoke на НАСТОЯЩЕМ Mark 85 (если файл на месте): 9 столов, без orphan-коллапса.
    p = Path(__file__).resolve().parents[1] / (
        "data/downloads/dl_d0b9dac5/Mark 85 Helmet with Detailing and Motorization.3mf")
    if not p.exists():
        pytest.skip("real Mark 85 .3mf not present in data/downloads")
    assert len(inspect3mf.plate_part_ids(p)) == 9            # 9 столов
    plates = geometry.load_print_meshes_by_plate([p])
    assert len(plates) == 9                                  # ровно 9 групп → НЕТ orphan-группы
    assert sum(len(g) for g in plates) == 14                 # все 14 инстансов разложены


# --- (в) ГАБАРИТЫ ПО СТОЛАМ: общий bbox раскладки всегда больше стола и пугает клиента -----

def test_dims_go_per_plate_for_multiplate(tmp_path, monkeypatch):
    """Мультиплейт: строка габаритов — по столам, а не общий bbox раскладки. Каждый стол по
    любой оси меньше общего bbox, иначе смысл правки теряется."""
    from app import config, preview
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    offs = {1: (0, 0, 0), 2: (300, 0, 0), 3: (330, 0, 0), 4: (600, 0, 0)}
    p = _make_3mf(tmp_path, "mp_dims.3mf", offs, plates=[[1], [2, 3], [4]])

    by_plates = preview.dims_by_plates([p])
    parts = by_plates.replace(" мм", "").split(" · ")
    assert len(parts) == 3                                   # ровно по столу на группу
    assert preview.model_dims([p])[0] == by_plates           # смета берёт ту же строку
    assert preview.plates_dims([p]) == parts                 # плитки столов — из того же замера

    whole = trimesh.util.concatenate(
        [m for g in geometry.load_print_meshes_by_plate([p]) for m in g])
    gx, gy, _gz = whole.bounds[1] - whole.bounds[0]
    for part in parts:                                       # стол заведомо уже всей раскладки
        x, y, _z = (float(v) for v in part.split("×"))
        assert x <= gx and y <= gy
        assert x < gx or y < gy


def test_single_plate_dims_unchanged(tmp_path, monkeypatch):
    """Один стол — прежняя строка «X×Y×Z мм» без перечисления (критический путь не трогаем)."""
    from app import config, preview
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    p = _make_3mf(tmp_path, "single_dims.3mf", {1: (0, 0, 0)}, plates=[[1]])
    dims, size = preview.model_dims([p])
    assert " · " not in dims and dims.endswith(" мм") and size > 0
    assert preview.dims_by_plates([p]) == ""


def test_many_plates_label_is_truncated(tmp_path, monkeypatch):
    """Столов больше четырёх — подпись не растёт бесконечно: четыре и счётчик остальных."""
    from app import preview
    monkeypatch.setattr(preview, "plate_extents",
                        lambda paths: [(10.0, 20.0, 30.0)] * 6)
    s = preview.dims_by_plates(["x.3mf"])
    assert s.count(" · ") == 4 and s.endswith("ещё 2 стола")
    monkeypatch.setattr(preview, "plate_extents",
                        lambda paths: [(10.0, 20.0, 30.0)] * 9)
    assert preview.dims_by_plates(["x.3mf"]).endswith("ещё 5 столов")


def test_empty_plate_keeps_its_slot(tmp_path, monkeypatch):
    """Пустой стол держит место в plates_dims: подписи плиток адресуются индексом стола."""
    from app import preview
    monkeypatch.setattr(preview, "plate_extents",
                        lambda paths: [(10.0, 20.0, 30.0), None, (5.0, 5.0, 5.0)])
    assert preview.plates_dims(["x.3mf"]) == ["10×20×30", "", "5×5×5"]
    assert preview.dims_by_plates(["x.3mf"]) == "10×20×30 · 5×5×5 мм"


# --- (г) ПРЕДОХРАНИТЕЛЬ ПО ОЗУ: разбор .3mf стоит ×25 от распакованного XML ---------------

def test_source_mesh_mb_reads_zip_directory(tmp_path):
    """Вес геометрии берётся из оглавления архива (file_size), а не из размера файла на диске:
    .3mf сжат в разы, и по нему пик памяти не предскажешь."""
    from app import geometry as g
    p = _make_3mf(tmp_path, "w.3mf", {1: (0, 0, 0), 2: (30, 0, 0)}, plates=[[1, 2]])
    with zipfile.ZipFile(p) as z:
        raw = sum(i.file_size for i in z.infolist() if i.filename.endswith(".model"))
    assert abs(g.source_mesh_mb([p]) - raw / 1e6) < 1e-9
    # соседние записи архива (model_settings, rels) в вес геометрии НЕ входят
    with zipfile.ZipFile(p) as z:
        assert raw < sum(i.file_size for i in z.infolist())


def test_heavy_model_is_refused_not_loaded(tmp_path, monkeypatch):
    """Потолок сработал → грузить НЕ пытаемся (иначе смысл предохранителя теряется) и отдаём
    пустой набор: вызывающие уже умеют падать на вшитую в .3mf картинку."""
    from app import geometry as g
    p = _make_3mf(tmp_path, "heavy.3mf", {1: (0, 0, 0), 2: (300, 0, 0)}, plates=[[1], [2]])
    monkeypatch.setattr(g, "MESH_SRC_LIMIT_MB", 0.000001)

    def boom(*_a, **_kw):                                       # trimesh звать не должны вовсе
        raise AssertionError("тяжёлую модель начали грузить в память")

    monkeypatch.setattr(g.trimesh, "load", boom)
    assert "потолка" in g.too_heavy_to_load([p])
    assert g.load_print_meshes([p]) == []
    assert g.load_print_meshes_by_plate([p]) == []


def test_free_ram_share_gates_the_load(tmp_path, monkeypatch):
    """Вторая крутилка — доля СВОБОДНОЙ памяти: та же модель проходит на свободной машине и
    не проходит на занятой, потолок при этом не меняется."""
    from app import geometry as g
    p = _make_3mf(tmp_path, "ram.3mf", {1: (0, 0, 0)}, plates=[[1]])
    monkeypatch.setattr(g, "MESH_SRC_LIMIT_MB", 1000.0)
    monkeypatch.setattr(g, "MESH_RAM_FACTOR", 25.0)
    monkeypatch.setattr(g, "MESH_RAM_SHARE", 0.5)
    assert g.too_heavy_to_load([p]) == ""                       # мелочь проходит всегда
    monkeypatch.setattr(g, "MESH_RAM_FACTOR", 25_000_000.0)     # «модель» стала неподъёмной
    assert "свободной ОЗУ" in g.too_heavy_to_load([p])
