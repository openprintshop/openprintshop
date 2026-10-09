"""Режим «только быстрый расчёт» (решение владельца 17.07.2026): slicing_enabled=0 (дефолт) —
клиентские расчёты идут БЕЗ слайсера через fastcalc; выбор столов уважается per-plate замером;
явные админ-инструменты (force_slice) слайсят всегда; настройка 1 возвращает прежний слайс."""
import zipfile

import pytest

from app import calcservice, fastcalc, slicer

_CUBE_V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
           (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_CUBE_T = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
           (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _cube_mesh_xml(side: float) -> str:
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % (x * side / 10, y * side / 10, z * side / 10)
                for x, y, z in _CUBE_V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tr for tr in _CUBE_T)
    return f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>"


def _cube_stl(path, side: float):
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(side, side, side)).export(str(path))
    return path


def _two_plate_3mf(path):
    """Два стола: на первом куб 10 мм (1 см³), на втором куб 20 мм (8 см³)."""
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources>'
            f'<object id="1" type="model">{_cube_mesh_xml(10)}</object>'
            f'<object id="2" type="model">{_cube_mesh_xml(20)}</object>'
            '</resources><build>'
            '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 0 0"/>'
            '</build></model>')
    ms = ('<?xml version="1.0"?><config>'
          '<object id="1"><metadata key="extruder" value="1"/></object>'
          '<object id="2"><metadata key="extruder" value="1"/></object>'
          '<plate><metadata key="plater_id" value="1"/>'
          '<model_instance><metadata key="object_id" value="1"/>'
          '<metadata key="instance_id" value="0"/></model_instance></plate>'
          '<plate><metadata key="plater_id" value="2"/>'
          '<model_instance><metadata key="object_id" value="2"/>'
          '<metadata key="instance_id" value="0"/></model_instance></plate>'
          '</config>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", core)
        z.writestr("Metadata/model_settings.config", ms)
    return path


async def _presets(tmp_db):
    printer = await tmp_db.get_default_printer()
    plastic = (await tmp_db.list_plastics(active_only=True))[0]
    quality = (await tmp_db.list_quality(active_only=True))[0]
    strength = (await tmp_db.list_strength(active_only=True))[0]
    return printer, plastic, quality, strength


def _no_slice(monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("slice_unit не должен вызываться при slicing_enabled=0")
    monkeypatch.setattr(calcservice.slicer, "slice_unit", _boom)


def _sync_pools(monkeypatch):
    async def _run(func, *args, **kw):
        return func(*args, **kw)

    async def _run_render(func, *args, **kw):
        return None
    monkeypatch.setattr(calcservice.cpupool, "run", _run)
    monkeypatch.setattr(calcservice.cpupool, "run_render", _run_render)


@pytest.mark.asyncio
async def test_compute_uses_fastcalc_only_by_default(tmp_db, monkeypatch, tmp_path, _no_bg=None):
    """Дефолт (slicing_enabled=0): compute НЕ трогает слайсер, отдаёт ≈оценку estimated=True."""
    monkeypatch.setattr(calcservice.config, "CACHE_DIR", tmp_path / "cache", raising=False)
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _no_slice(monkeypatch)
    _sync_pools(monkeypatch)
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = _cube_stl(tmp_path / "cube20.stl", 20.0)

    res = await calcservice.compute(
        user_id=1, model_paths=[str(stl)], model_label="cube", printer=printer, plastic=plastic,
        color="", qty=1, quality=quality, strength=strength, save=False)
    assert res["estimated"] is True
    assert res["bd"].total > 0 and res["bd"].grams_unit > 0


@pytest.mark.asyncio
async def test_plates_only_measures_selected_plates(tmp_db, monkeypatch, tmp_path):
    """Выбор столов: оценка меряет ТОЛЬКО выбранные столы (куб 1 см³ vs куб 8 см³)."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    m3 = _two_plate_3mf(tmp_path / "plates.3mf")
    _, plastic, quality, _ = await _presets(tmp_db)

    async def est(sel):
        r = await fastcalc.estimate([str(m3)], plastic=plastic, quality=quality, strength=None,
                                    allow_background=False, plates_only=sel)
        assert r is not None
        return r["v_solid_cm3"]

    v0 = await est([0])
    v1 = await est([1])
    vall = await est(None)
    assert v0 == pytest.approx(1.0, rel=0.08)
    assert v1 == pytest.approx(8.0, rel=0.08)
    assert vall == pytest.approx(9.0, rel=0.08)


@pytest.mark.asyncio
async def test_compute_plates_only_prices_selection(tmp_db, monkeypatch, tmp_path):
    """compute при выключенном слайсе ценит ВЫБРАННЫЕ столы, а не всю модель."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _no_slice(monkeypatch)
    _sync_pools(monkeypatch)
    printer, plastic, quality, strength = await _presets(tmp_db)
    m3 = _two_plate_3mf(tmp_path / "plates.3mf")

    small = await calcservice.compute(
        user_id=1, model_paths=[str(m3)], model_label="p0", printer=printer, plastic=plastic,
        color="", qty=1, quality=quality, strength=strength, save=False,
        plates=2, plates_only=[0])
    full = await calcservice.compute(
        user_id=1, model_paths=[str(m3)], model_label="all", printer=printer, plastic=plastic,
        color="", qty=1, quality=quality, strength=strength, save=False, plates=2)
    assert small["estimated"] and full["estimated"]
    assert small["bd"].grams_unit < full["bd"].grams_unit * 0.5   # 1 см³ против 9 см³


@pytest.mark.asyncio
async def test_force_slice_bypasses_flag(tmp_db, monkeypatch, tmp_path):
    """Явный админ-инструмент (force_slice=True) слайсит даже при slicing_enabled=0."""
    _sync_pools(monkeypatch)
    called = []

    async def fake_slice(*a, **k):
        called.append(1)
        return slicer.SliceResult(volume_cm3=7.0, minutes=42.0, cached=False)
    monkeypatch.setattr(calcservice.slicer, "slice_unit", fake_slice)
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = _cube_stl(tmp_path / "cube.stl", 20.0)

    res = await calcservice.compute(
        user_id=1, model_paths=[str(stl)], model_label="cube", printer=printer, plastic=plastic,
        color="", qty=1, quality=quality, strength=strength, save=False, force_slice=True)
    assert called, "слайсер должен был вызваться"
    assert res["estimated"] is False
    assert res["bd"].grams_unit == pytest.approx(7.0 * plastic.density, rel=1e-6)


@pytest.mark.asyncio
async def test_force_slice_still_sanitizes_bambu(tmp_db, monkeypatch, tmp_path):
    """Санация бамбу-3mf гейтится настройкой sanitize_3mf (дефолт 0 — файл автора не трогаем),
    но force_slice обязан её запрашивать: это явный админ-инструмент, которому нужен НАСТОЯЩИЙ
    слайс. Иначе несанированный бамбу-проект уходит в слайсер, тот падает, SlicerError глотается
    и мастеру молча отдаётся ≈оценка, неотличимая от настоящего слайса."""
    _sync_pools(monkeypatch)
    seen = []

    async def spy(paths, *, enabled, printer=None):
        seen.append(enabled)
    monkeypatch.setattr(calcservice, "_sanitize_bambu_models", spy)

    async def fake_slice(*a, **k):
        return slicer.SliceResult(volume_cm3=7.0, minutes=42.0, cached=False)
    monkeypatch.setattr(calcservice.slicer, "slice_unit", fake_slice)
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = _cube_stl(tmp_path / "cube.stl", 20.0)
    kw = dict(user_id=1, model_paths=[str(stl)], model_label="cube", printer=printer,
              plastic=plastic, color="", qty=1, quality=quality, strength=strength, save=False)

    await calcservice.compute(force_slice=True, **kw)
    assert seen == [True], "sanitize_3mf=0, но force_slice обязан просить санацию"

    seen.clear()
    _no_slice(monkeypatch)
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    await calcservice.compute(**kw)
    assert seen == [False], "обычный клиентский расчёт файл автора не трогает"


@pytest.mark.asyncio
async def test_setting_reenables_slicing(tmp_db, monkeypatch, tmp_path):
    """slicing_enabled=1 возвращает прежний слайс-путь без force_slice."""
    await tmp_db.set_setting("slicing_enabled", 1)
    _sync_pools(monkeypatch)
    called = []

    async def fake_slice(*a, **k):
        called.append(1)
        return slicer.SliceResult(volume_cm3=3.0, minutes=10.0, cached=False)
    monkeypatch.setattr(calcservice.slicer, "slice_unit", fake_slice)
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = _cube_stl(tmp_path / "cube.stl", 20.0)

    res = await calcservice.compute(
        user_id=1, model_paths=[str(stl)], model_label="cube", printer=printer, plastic=plastic,
        color="", qty=1, quality=quality, strength=strength, save=False)
    assert called and res["estimated"] is False


@pytest.mark.asyncio
async def test_recalc_request_without_slicer(tmp_db, monkeypatch, tmp_path):
    """Админ-«Пересчитать» при выключенном слайсе работает через быстрый расчёт."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _no_slice(monkeypatch)
    printer = await tmp_db.get_default_printer()
    plastic = (await tmp_db.list_plastics(active_only=True))[0]
    quality = (await tmp_db.list_quality(active_only=True))[0]
    strength = (await tmp_db.list_strength(active_only=True))[0]
    stl = _cube_stl(tmp_path / "cube.stl", 20.0)
    rid = await tmp_db.add_request(
        user_id=5, user_name="c", model_label="Куб", printer=printer.name,
        plastic_name=plastic.name, quantity=1, quality=quality.label, strength=strength.label,
        grams_total=10, minutes_total=30, price=5, preview_path=None,
        file_path=str(stl), quality_id=quality.id, strength_id=strength.id)
    r = await tmp_db.get_request(rid)

    res = await calcservice.recalc_request(r)
    assert res["bd"].total > 0 and res["bd"].grams_unit > 0
