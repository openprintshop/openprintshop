"""Тесты детекта многоцвета/плит в .3mf (на синтетических zip)."""
import zipfile

from app import inspect3mf
from app.inspect3mf import ModelInfo


def _mk(tmp_path, settings=b"<config></config>", model=b"<model></model>", name="m.3mf"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/model_settings.config", settings)
        z.writestr("3D/3dmodel.model", model)
    return p


def test_modelinfo_properties():
    assert ModelInfo().is_multicolor is False
    assert ModelInfo(painted=True).is_multicolor is True
    assert ModelInfo(extruders={1, 2}).is_multicolor is True
    assert ModelInfo(extruders={3}).is_multicolor is False        # один экструдер = одноцвет
    assert ModelInfo(plates=2).is_multiplate is True
    assert ModelInfo(painted=True).num_colors == 2
    assert ModelInfo(extruders={1, 2, 3}).num_colors == 3


def test_used_colours_by_extruders(tmp_path):
    # цвета РЕАЛЬНО занятых экструдеров в порядке номеров; альфа-канал Bambu (#RRGGBBAA) срезается
    cfg = (b'<config><object><metadata key="extruder" value="1"/>'
           b'<metadata key="extruder" value="3"/></object></config>')
    p = tmp_path / "mc.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/model_settings.config", cfg)
        z.writestr("3D/3dmodel.model", b"<model></model>")
        z.writestr("Metadata/project_settings.config",
                   b'{"filament_colour": ["#FF0000FF", "#00FF00", "#0000FF"]}')
    assert inspect3mf.inspect_model([p]).used_colours == ["#ff0000", "#0000ff"]


def test_used_colours_properties():
    # крашеная модель без разметки экструдеров → вся палитра проекта
    assert ModelInfo(painted=True, filament_colours=["#AA0000", "#00BB00"]).used_colours == ["#aa0000", "#00bb00"]
    # одноцветная без разметки → первый филамент
    assert ModelInfo(filament_colours=["#AA0000", "#00BB00"]).used_colours == ["#aa0000"]
    # один размеченный экструдер → именно его цвет (не первый)
    assert ModelInfo(extruders={2}, filament_colours=["#AA0000", "#00BB00"]).used_colours == ["#00bb00"]
    # битая запись палитры НЕ сдвигает индексы остальных
    assert ModelInfo(extruders={1, 2}, filament_colours=["мусор", "#00BB00"]).used_colours == ["#00bb00"]
    # нет цветов в .3mf → пусто
    assert ModelInfo().used_colours == []
    assert ModelInfo(filament_colours=["мусор"]).used_colours == []


def test_multicolor_by_extruders(tmp_path):
    cfg = (b'<config><object><metadata key="extruder" value="1"/>'
           b'<metadata key="extruder" value="2"/></object></config>')
    info = inspect3mf.inspect_model([_mk(tmp_path, cfg)])
    assert info.is_multicolor is True
    assert info.distinct_extruders == {1, 2}


def test_multicolor_by_paint(tmp_path):
    info = inspect3mf.inspect_model([_mk(tmp_path, model=b'<model><triangle paint_color="4"/></model>')])
    assert info.painted is True
    assert info.is_multicolor is True


def test_monocolor(tmp_path):
    cfg = b'<config><metadata key="extruder" value="1"/></config>'
    info = inspect3mf.inspect_model([_mk(tmp_path, cfg, b'<model><triangle/></model>')])
    assert info.is_multicolor is False


def test_paint_seam_is_not_multicolor(tmp_path):
    # paint_seam — это шов, НЕ цвет. Не должен помечать модель многоцветной.
    info = inspect3mf.inspect_model([_mk(tmp_path, model=b'<model><triangle paint_seam="1"/></model>')])
    assert info.is_multicolor is False


def test_plates_count(tmp_path):
    cfg = b'<config><plate></plate><plate></plate></config>'
    info = inspect3mf.inspect_model([_mk(tmp_path, cfg)])
    assert info.plates == 2
    assert info.is_multiplate is True


def test_non_3mf_ignored(tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"solid")
    info = inspect3mf.inspect_model([stl])
    assert info.plates == 1 and info.is_multicolor is False


def test_negative_part_detected(tmp_path):
    # #8: subtype="negative_part" — вычитающая деталь, её id должен попасть в выдачу.
    cfg = (b'<config><object id="3">'
           b'<part id="1" subtype="normal_part"><metadata key="name" value="Body"/></part>'
           b'<part id="2" subtype="negative_part"><metadata key="name" value="Hole"/></part>'
           b'</object></config>')
    ids = inspect3mf.negative_part_ids(_mk(tmp_path, cfg))
    assert ids == {"2"}


def test_no_negative_parts(tmp_path):
    cfg = (b'<config><object id="3">'
           b'<part id="1" subtype="normal_part"></part></object></config>')
    assert inspect3mf.negative_part_ids(_mk(tmp_path, cfg)) == set()


def test_negative_part_non_3mf(tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"solid")
    assert inspect3mf.negative_part_ids(stl) == set()


# ---- plate-aware: разбор <plate> → object_id → part_id (3811f970 / a6452eaf) ----

# Объект Bambu: <object id=O> содержит <part id=P>; <plate> ссылается на object_id.
_MULTIPLATE = (
    b'<config>'
    b'<object id="10"><part id="1" subtype="normal_part"/></object>'
    b'<object id="20"><part id="2" subtype="normal_part"/></object>'
    b'<object id="30"><part id="3" subtype="normal_part"/></object>'
    b'<plate><metadata key="plater_id" value="1"/>'
    b'<model_instance><metadata key="object_id" value="10"/></model_instance></plate>'
    b'<plate><metadata key="plater_id" value="2"/>'
    b'<model_instance><metadata key="object_id" value="20"/></model_instance>'
    b'<model_instance><metadata key="object_id" value="30"/></model_instance></plate>'
    b'</config>'
)


def test_plate_part_ids_splits_into_plates(tmp_path):
    plates = inspect3mf.plate_part_ids(_mk(tmp_path, _MULTIPLATE))
    assert len(plates) == 2
    assert plates[0] == {"1"}                 # стол 1 → object 10 → part 1
    assert plates[1] == {"2", "3"}            # стол 2 → объекты 20,30 → части 2,3


def test_plate_part_ids_preserves_order(tmp_path):
    # порядок столов должен сохраняться (пользователь хочет «Стол 1, Стол 2, …»)
    plates = inspect3mf.plate_part_ids(_mk(tmp_path, _MULTIPLATE))
    assert [sorted(s) for s in plates] == [["1"], ["2", "3"]]


def test_plate_part_ids_single_plate_returns_empty(tmp_path):
    # один стол → [] → вызывающий идёт прежним (одноплейтным) путём
    cfg = (b'<config><object id="10"><part id="1" subtype="normal_part"/></object>'
           b'<plate><model_instance><metadata key="object_id" value="10"/>'
           b'</model_instance></plate></config>')
    assert inspect3mf.plate_part_ids(_mk(tmp_path, cfg)) == []


def test_plate_part_ids_no_plates_returns_empty(tmp_path):
    cfg = b'<config><object id="10"><part id="1" subtype="normal_part"/></object></config>'
    assert inspect3mf.plate_part_ids(_mk(tmp_path, cfg)) == []


def test_plate_part_ids_non_3mf(tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"solid")
    assert inspect3mf.plate_part_ids(stl) == []


def test_plate_part_ids_shared_part_on_two_plates(tmp_path):
    # зеркальная деталь (Mark 85): два объекта-инстанса ссылаются на ОДИН part → деталь на 2 столах
    cfg = (b'<config>'
           b'<object id="40"><part id="3" subtype="normal_part"/></object>'
           b'<object id="50"><part id="3" subtype="normal_part"/></object>'
           b'<plate><model_instance><metadata key="object_id" value="40"/></model_instance></plate>'
           b'<plate><model_instance><metadata key="object_id" value="50"/></model_instance></plate>'
           b'</config>')
    plates = inspect3mf.plate_part_ids(_mk(tmp_path, cfg))
    assert plates == [{"3"}, {"3"}]           # part 3 заявлен обоими столами


def test_part_extruders_object_and_part_level(tmp_path):
    # экструдер на уровне <object> наследуется part-ом; на <part> — переопределяет.
    # Карта теперь несёт и object-id (10→2, 20→4): пространств адресации у сцены несколько.
    cfg = (b'<config>'
           b'<object id="10"><metadata key="extruder" value="2"/>'
           b'<part id="1" subtype="normal_part"/></object>'
           b'<object id="20"><metadata key="extruder" value="3"/>'
           b'<part id="2" subtype="normal_part">'
           b'<metadata key="extruder" value="4"/></part></object>'
           b'</config>')
    ext = inspect3mf.part_extruders(_mk(tmp_path, cfg))
    assert ext["1"] == 2 and ext["2"] == 4    # part1 наследует 2 от объекта; part2 переопределён на 4
    assert ext["10"] == 2 and ext["20"] == 3  # object-id — своё пространство: у объекта СВОЙ экструдер


def test_part_extruders_bambu_wrappers_duplicate_part_ids(tmp_path):
    """Bambu-стиль (кейс «Овечка Тимми», фидбэк 24.07): у КАЖДОГО объекта <part id="1"> —
    старая карта «по part-id» схлопывалась в один мусорный ключ и шерсть красилась чёрным.
    Теперь: дублирующиеся part-id выброшены, объектные id на месте, а через <component> главного
    .model карта бриджится в пространство id мешей сцены (2→1, 4→3)."""
    cfg = (b'<config>'
           b'<object id="2"><metadata key="name" value="Body"/>'
           b'<metadata key="extruder" value="1"/>'
           b'<part id="1" subtype="normal_part"/></object>'
           b'<object id="4"><metadata key="name" value="Head"/>'
           b'<metadata key="extruder" value="2"/>'
           b'<part id="1" subtype="normal_part"/></object>'
           b'</config>')
    model = (b'<model><resources>'
             b'<object id="1" type="model"><mesh/></object>'
             b'<object id="2" type="model"><components>'
             b'<component objectid="1"/></components></object>'
             b'<object id="3" type="model"><mesh/></object>'
             b'<object id="4" type="model"><components>'
             b'<component objectid="3"/></components></object>'
             b'</resources><build><item objectid="2"/><item objectid="4"/></build></model>')
    ext = inspect3mf.part_extruders(_mk(tmp_path, cfg, model=model))
    assert ext.get("2") == 1 and ext.get("4") == 2      # объектные id model_settings
    assert ext.get("1") == 1 and ext.get("3") == 2      # мост component→object: id мешей сцены
    # дублирующийся part-id "1" НЕ должен был перебить мостовую запись тела (1 → белый),
    # иначе Тимми снова почернел бы; ключ "1" обязан прийти из моста (экструдер 1), не из part-скопа
    assert ext["1"] == 1


# ─────────── mixed_plates: смешение цветов НА СТОЛЕ (OpenTodo 0bcdd728) ───────────

_PLATE1 = (b'<plate><metadata key="plater_id" value="1"/>'
           b'<model_instance><metadata key="object_id" value="1"/></model_instance></plate>')
_PLATE2 = (b'<plate><metadata key="plater_id" value="2"/>'
           b'<model_instance><metadata key="object_id" value="2"/></model_instance></plate>')


def test_colors_split_by_plates_not_mixed(tmp_path):
    """Разные цвета по РАЗНЫМ столам (каждый стол одноцветный) — многоцвет есть,
    но смешения нет → надбавка за мультицвет не положена."""
    cfg = (b'<config>'
           b'<object id="1"><metadata key="extruder" value="1"/>'
           b'<part id="p1"><metadata key="extruder" value="1"/></part></object>'
           b'<object id="2"><metadata key="extruder" value="2"/>'
           b'<part id="p2"><metadata key="extruder" value="2"/></part></object>'
           + _PLATE1 + _PLATE2 + b'</config>')
    info = inspect3mf.inspect_model([_mk(tmp_path, cfg)])
    assert info.is_multicolor is True
    assert info.mixed_plates is False


def test_colors_mixed_on_one_plate(tmp_path):
    """Два экструдера у объектов ОДНОГО стола — реальное смешение, надбавка остаётся."""
    both = (b'<plate><metadata key="plater_id" value="1"/>'
            b'<model_instance><metadata key="object_id" value="1"/></model_instance>'
            b'<model_instance><metadata key="object_id" value="2"/></model_instance></plate>')
    cfg = (b'<config>'
           b'<object id="1"><metadata key="extruder" value="1"/>'
           b'<part id="p1"><metadata key="extruder" value="1"/></part></object>'
           b'<object id="2"><metadata key="extruder" value="2"/>'
           b'<part id="p2"><metadata key="extruder" value="2"/></part></object>'
           + both + _PLATE2 + b'</config>')
    info = inspect3mf.inspect_model([_mk(tmp_path, cfg)])
    assert info.mixed_plates is True


def test_single_plate_multicolor_is_mixed(tmp_path):
    """Один стол, два экструдера: данных по столам нет ([]), консервативно смешение."""
    cfg = (b'<config><object id="1"><metadata key="extruder" value="1"/>'
           b'<metadata key="extruder" value="2"/></object></config>')
    info = inspect3mf.inspect_model([_mk(tmp_path, cfg)])
    assert info.is_multicolor is True
    assert info.mixed_plates is True


def test_painted_is_mixed(tmp_path):
    info = inspect3mf.inspect_model(
        [_mk(tmp_path, model=b'<model><triangle paint_color="4"/></model>')])
    assert info.mixed_plates is True


def test_monocolor_not_mixed(tmp_path):
    cfg = b'<config><metadata key="extruder" value="1"/></config>'
    info = inspect3mf.inspect_model([_mk(tmp_path, cfg, b'<model><triangle/></model>')])
    assert info.mixed_plates is False
