"""Тесты санации проект-3mf из BambuStudio под ElegooSlicer (app.bambu3mf) на синтетических zip.

Живой слайс покрыт разведкой вручную (Коала «Tiny Flexi Koala»): исходный бамбу-3mf не
режется (версионный гейт + битый model_settings), санированный — режется, 4 цвета сохраняются.
Здесь проверяем логику переписи БЕЗ вызова слайсера."""
import json
import zipfile

import pytest

from app import bambu3mf, calcservice, db

# --- синтетический проект-3mf «как MakerLab»: бамбу-принтер, -1-сентинелы, 4 филамента,
#     пустой <assemble>, мусорная <metadata face_count>, версия 02.04.00.70. -----------------
_PROJECT = {
    "version": "02.04.00.70",
    "printer_settings_id": "Bambu Lab X1E 0.4 nozzle",
    "printer_model": "Bambu Lab X1E",
    "print_settings_id": "0.20mm Standard @BBL X1C",
    "curr_bed_type": "Cool Plate",
    "filament_type": ["PLA", "PLA", "PETG", "PLA"],
    "filament_colour": ["#9A9996FF", "#D0CBC3FF", "#4F4D49FF", "#7C7A76FF"],
    "tree_support_wall_count": "-1",
    "wall_filament": "0",
    "nozzle_diameter": ["0.4"],
}
_MODEL = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">\n'
    ' <metadata name="Application">BambuStudio-02.04.00.70</metadata>\n'
    ' <resources><object id="2" p:UUID="00000001-aaaa" type="model"><components>'
    '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
    '</components></object></resources>\n'
    ' <build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 128 128 16.24" printable="1"/></build>\n'
    '</model>\n'
)
_MODEL_SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
    '  <object id="2">\n'
    '    <metadata key="name" value="quant_1"/>\n'
    '    <metadata key="enable_support" value="1"/>\n'
    '    <metadata key="extruder" value="1"/>\n'
    '    <metadata key="support_type" value="tree(auto)"/>\n'
    '    <metadata face_count="500000"/>\n'
    '    <part id="1" subtype="normal_part">\n'
    '      <metadata key="name" value="quant_1"/>\n'
    '      <mesh_stat face_count="500000" edges_fixed="0"/>\n'
    '    </part>\n'
    '  </object>\n'
    '  <plate>\n'
    '    <metadata key="plater_id" value="1"/>\n'
    '    <metadata key="gcode_file" value=""/>\n'
    '    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>\n'
    '    <model_instance>\n'
    '      <metadata key="object_id" value="2"/>\n'
    '      <metadata key="instance_id" value="0"/>\n'
    '      <metadata key="identify_id" value="37"/>\n'
    '    </model_instance>\n'
    '  </plate>\n'
    '  <assemble>\n  </assemble>\n'
    '</config>\n'
)


def _mk_bambu(tmp_path, name="koala.3mf", project=None):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/project_settings.config",
                   json.dumps(project or _PROJECT).encode("utf-8"))
        z.writestr("3D/3dmodel.model", _MODEL)
        z.writestr("3D/Objects/object_1.model", b"<model><resources></resources></model>")
        z.writestr("Metadata/model_settings.config", _MODEL_SETTINGS)
        z.writestr("Metadata/plate_1.png", b"\x89PNG dummy")
    return p


def _cfg(p):
    return json.loads(zipfile.ZipFile(p).read("Metadata/project_settings.config").decode("utf-8"))


def _ms(p):
    return zipfile.ZipFile(p).read("Metadata/model_settings.config").decode("utf-8")


def _no_profiles(_material):
    """Резолвер без профилей → build_config берёт дефолты шаблона (не зависим от установки слайсера)."""
    return ""


async def _fake_resolver():
    """Подмена calcservice._bambu_filament_resolver — не ходим в базу за профилями пластиков."""
    return _no_profiles


# ---------------------------------------------------------------- детект --------------------

def test_is_bambu_by_printer(tmp_path):
    assert bambu3mf.is_bambu_project(_mk_bambu(tmp_path)) is True


def test_is_bambu_by_application(tmp_path):
    proj = dict(_PROJECT, printer_settings_id="Something 0.4", printer_model="Something",
                tree_support_wall_count="0")
    assert bambu3mf.is_bambu_project(_mk_bambu(tmp_path, project=proj)) is True   # ловит по Application


def test_not_bambu_plain_elegoo(tmp_path):
    proj = dict(_PROJECT, printer_settings_id="Elegoo Centauri Carbon 2 0.4 nozzle",
                printer_model="Elegoo Centauri Carbon 2", tree_support_wall_count="0")
    p = tmp_path / "e.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(proj).encode())
        z.writestr("3D/3dmodel.model", '<model><metadata name="Application">ElegooSlicer-1.5.2.2</metadata></model>')
    assert bambu3mf.is_bambu_project(p) is False


def test_not_bambu_stl(tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"solid")
    assert bambu3mf.is_bambu_project(stl) is False


# ---------------------------------------------------------------- санация -------------------

def test_sanitize_swaps_printer_and_neutralizes_version(tmp_path):
    p = _mk_bambu(tmp_path)
    assert bambu3mf.sanitize(p, resolver=_no_profiles) is True
    c = _cfg(p)
    assert c["printer_settings_id"] == "B Elegoo Centauri Carbon 2 0.4 nozzle"
    assert c["printer_model"] == "Elegoo Centauri Carbon 2"
    # версионный гейт снят: Application больше не BambuStudio
    model = zipfile.ZipFile(p).read("3D/3dmodel.model").decode("utf-8")
    assert "BambuStudio" not in model
    assert "ElegooSlicer" in model


def test_sanitize_fixes_out_of_range_sentinels(tmp_path):
    p = _mk_bambu(tmp_path)
    bambu3mf.sanitize(p, resolver=_no_profiles)
    c = _cfg(p)
    assert c["tree_support_wall_count"] in ("0", "1", "2")
    assert c["wall_filament"] == "1" and c["solid_infill_filament"] == "1"


def test_sanitize_bed_by_material(tmp_path):
    # доминирующий материал PLA (3 из 4) → сторона Б
    p = _mk_bambu(tmp_path)
    bambu3mf.sanitize(p, resolver=_no_profiles)
    assert _cfg(p)["curr_bed_type"] == bambu3mf.BED_PLA

    proj = dict(_PROJECT, filament_type=["PETG", "PETG"], filament_colour=["#000000", "#FFFFFF"])
    q = _mk_bambu(tmp_path, name="petg.3mf", project=proj)
    bambu3mf.sanitize(q, resolver=_no_profiles)
    assert _cfg(q)["curr_bed_type"] == bambu3mf.BED_OTHER


def test_sanitize_filament_arrays_consistent_length(tmp_path):
    p = _mk_bambu(tmp_path)
    bambu3mf.sanitize(p, resolver=_no_profiles)
    c = _cfg(p)
    n = len(c["filament_type"])
    assert n == 4
    assert len(c["filament_colour"]) == n
    assert c["filament_colour"][0] == "#9A9996"          # альфа срезана до #RRGGBB
    # все пофиламентные массивы одной длины N (иначе движок падает на «vector of different length»)
    for key in bambu3mf._PER_FILAMENT_KEYS:
        v = c.get(key)
        if isinstance(v, list):
            assert len(v) == n, f"{key} длиной {len(v)} != {n}"
    # промывочные массивы согласованы: вектор 2N, матрица N*N
    assert len(c["flush_volumes_vector"]) == 2 * n
    assert len(c["flush_volumes_matrix"]) == n * n


def test_sanitize_uses_db_resolver_profiles(tmp_path):
    def resolver(material):
        return {"PLA": r"C:\x\My Tuned PLA.json", "PETG": r"C:\x\My PETG.json"}.get(material.upper(), "")
    p = _mk_bambu(tmp_path)
    bambu3mf.sanitize(p, resolver=resolver)
    ids = _cfg(p)["filament_settings_id"]
    assert ids == ["My Tuned PLA", "My Tuned PLA", "My PETG", "My Tuned PLA"]


def test_sanitize_rebuilds_model_settings(tmp_path):
    p = _mk_bambu(tmp_path)
    bambu3mf.sanitize(p, resolver=_no_profiles)
    ms = _ms(p)
    # <assemble> больше не пустой — есть assemble_item с трансформом объекта
    assert "<assemble_item" in ms
    assert 'transform="1 0 0 0 1 0 0 0 1 128 128 16.24"' in ms
    # мусорная <metadata face_count> и пустой gcode_file выброшены
    assert "face_count" not in ms
    assert 'key="gcode_file"' not in ms
    # авторская настройка поддержки сохранена
    assert 'key="support_type" value="tree(auto)"' in ms
    # инстанс на плите сохранён
    assert 'key="object_id" value="2"' in ms


def test_sanitize_makes_backup(tmp_path):
    p = _mk_bambu(tmp_path)
    orig = p.read_bytes()
    bambu3mf.sanitize(p, resolver=_no_profiles)
    bak = p.with_name(p.name + ".bambu.bak")
    assert bak.exists()
    assert bak.read_bytes() == orig


def test_sanitize_idempotent(tmp_path):
    p = _mk_bambu(tmp_path)
    assert bambu3mf.sanitize(p, resolver=_no_profiles) is True
    # после санации это уже валидный Elegoo-проект → повторный вызов ничего не делает
    assert bambu3mf.is_bambu_project(p) is False
    assert bambu3mf.sanitize(p, resolver=_no_profiles) is False


def test_sanitize_no_backup_when_disabled(tmp_path):
    p = _mk_bambu(tmp_path)
    bambu3mf.sanitize(p, resolver=_no_profiles, backup=False)
    assert not p.with_name(p.name + ".bambu.bak").exists()


def test_sanitize_returns_false_for_non_bambu(tmp_path):
    stl = tmp_path / "x.stl"
    stl.write_bytes(b"solid")
    assert bambu3mf.sanitize(stl, resolver=_no_profiles) is False


# ------------------------- настройка sanitize_3mf (OpenTodo 67dfaeaf) -------------------------
# Санация правит .3mf НА МЕСТЕ и без бэкапа, поэтому мастер может её выключить: тогда файл
# автора остаётся байт-в-байт исходным (слайсинг всё равно отключён, чинить его не для кого).

@pytest.mark.asyncio
async def test_calc_leaves_3mf_untouched_when_setting_off(tmp_path):
    """sanitize_3mf выключена (дефолт) → файл не переписывается, это всё ещё бамбу-проект."""
    p = _mk_bambu(tmp_path)
    before = p.read_bytes()
    await calcservice._sanitize_bambu_models([p], enabled=False)
    assert p.read_bytes() == before
    assert bambu3mf.is_bambu_project(p) is True


@pytest.mark.asyncio
async def test_calc_sanitizes_3mf_when_setting_on(tmp_path, monkeypatch):
    """sanitize_3mf включена → прежнее поведение: проект чинится под ElegooSlicer."""
    monkeypatch.setattr(calcservice, "_bambu_filament_resolver",
                        _fake_resolver, raising=True)
    p = _mk_bambu(tmp_path)
    before = p.read_bytes()
    await calcservice._sanitize_bambu_models([p], enabled=True)
    assert p.read_bytes() != before
    assert bambu3mf.is_bambu_project(p) is False


def test_default_setting_keeps_3mf_original():
    """Дефолт настройки — «не трогать файл»; get_settings приводит его к 0.0 (не к truthy '0')."""
    assert db.DEFAULT_SETTINGS["sanitize_3mf"] == "0"
    assert bool(float(db.DEFAULT_SETTINGS["sanitize_3mf"])) is False
    assert "sanitize_3mf" in db.SETTING_LABELS
    assert "sanitize_3mf" in db.SETTINGS_ORDER


def test_template_asset_present():
    assert bambu3mf._TEMPLATE.exists(), "шаблон B-профиля должен лежать в app/assets"
    tmpl = json.loads(bambu3mf._TEMPLATE.read_text(encoding="utf-8"))
    assert tmpl["printer_settings_id"] == "B Elegoo Centauri Carbon 2 0.4 nozzle"
