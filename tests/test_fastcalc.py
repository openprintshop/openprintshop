"""Тесты быстрой аналитической ≈оценки v2 (app.fastcalc + app.fastmeasure) и её встраивания
фолбэком в calcservice.compute, когда слайс падает/лежит.

Проверяем: точный замер и вменяемые граммы/минуты для мелкого STL-куба и мелкого .3mf;
масштаб и монотонность; None на мусоре/неизвестном формате; персистентный кэш замеров;
быстрый ответ на тяжёлой коале (bbox или кэш, но НЕ синхронный точный замер); и что
calcservice.compute при крэше слайсера НЕ падает, а отдаёт ≈оценку с флагом estimated."""
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, fastcalc, fastmeasure

_PLA = SimpleNamespace(density=1.24)
_Q = SimpleNamespace(layer_height=0.20, process="0.20mm Standard @Elegoo CC2 0.4 nozzle.json")
_S = SimpleNamespace(infill=20, walls=3)     # для совместимости API (слайс идёт профилем)

# Куб 10 мм inline-.3mf (как в test_mesh_volume): объём тела 1.0 см³.
_CUBE_V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
           (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_CUBE_T = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
           (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _cube_3mf(path: Path) -> Path:
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % t for t in _CUBE_V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tr for tr in _CUBE_T)
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>%s</vertices>'
            '<triangles>%s</triangles></mesh></object></resources>'
            '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build></model>'
            % (v, t))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", core)
    return path


def _cube_stl(path: Path, side: float) -> Path:
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(side, side, side)).export(str(path))
    return path


@pytest.fixture()
def _tmp_measure_cache(tmp_path, monkeypatch):
    """Кэш замеров — во временную папку (не трогаем продовый data/cache)."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    return tmp_path


async def _est(paths, **kw):
    kw.setdefault("allow_background", False)
    return await fastcalc.estimate(paths, plastic=_PLA, quality=_Q, strength=_S, qty=1,
                                   settings=None, **kw)


# ─────────────────────────── измеритель fastmeasure ───────────────────────────

def test_measure_cube_3mf(tmp_path):
    m3 = _cube_3mf(tmp_path / "cube10.3mf")
    r = fastmeasure.measure_model([m3])
    assert r is not None
    assert r["v_mm3"] == pytest.approx(1000.0, rel=0.01)          # 10 мм куб
    assert r["v_union_mm3"] == pytest.approx(1000.0, rel=0.06)    # z-колонки — выборка
    b = r["buckets"]
    assert b[:, :, 0].sum() == pytest.approx(600.0, rel=0.01)     # площадь 6 см² = 600 мм²
    # бок 4 грани → латеральная составляющая 400; верх+низ → проекционная 200
    assert b[:, :, 2].sum() == pytest.approx(400.0, rel=0.01)
    assert b[:, :, 1].sum() == pytest.approx(200.0, rel=0.01)
    assert r["h_mm"] == pytest.approx(10.0, abs=0.01)
    assert not r["painted"]


def test_measure_painted_flag(tmp_path):
    """Покраска граней (mmu_segmentation) детектится по байтам — гейт продувки."""
    m3 = tmp_path / "painted.3mf"
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % t for t in _CUBE_V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d" mmu_segmentation="4"/>' % tr
                for tr in _CUBE_T)
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>%s</vertices>'
            '<triangles>%s</triangles></mesh></object></resources>'
            '<build><item objectid="1"/></build></model>' % (v, t))
    with zipfile.ZipFile(m3, "w") as z:
        z.writestr("3D/3dmodel.model", core)
    r = fastmeasure.measure_model([m3])
    assert r is not None
    assert r["painted"]                                           # атрибуты вычищены, объём цел
    assert r["v_mm3"] == pytest.approx(1000.0, rel=0.01)


def test_measure_piled_copies_dedup(tmp_path):
    """Наложенные копии (2 item в одном месте) в режиме пресета дедупятся, в нативном — нет."""
    m3 = tmp_path / "piled.3mf"
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % t for t in _CUBE_V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tr for tr in _CUBE_T)
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>%s</vertices>'
            '<triangles>%s</triangles></mesh></object></resources>'
            '<build><item objectid="1"/><item objectid="1" '
            'transform="1 0 0 0 1 0 0 0 1 1 1 0"/></build></model>' % (v, t))
    with zipfile.ZipFile(m3, "w") as z:
        z.writestr("3D/3dmodel.model", core)
    preset = fastmeasure.measure_model([m3], replicate_geometry=True)
    native = fastmeasure.measure_model([m3], replicate_geometry=False)
    assert preset["n_parts"] == 1 and preset["v_mm3"] == pytest.approx(1000.0, rel=0.01)
    assert native["n_parts"] == 2 and native["v_mm3"] == pytest.approx(2000.0, rel=0.01)


def test_measure_binary_stl(tmp_path):
    stl = _cube_stl(tmp_path / "cube20.stl", 20.0)
    r = fastmeasure.measure_model([stl])
    assert r is not None
    assert r["v_mm3"] == pytest.approx(8000.0, rel=0.01)
    assert r["buckets"][:, :, 0].sum() == pytest.approx(2400.0, rel=0.01)


# ─────────────────────────── оценщик fastcalc ───────────────────────────

@pytest.mark.asyncio
async def test_estimate_small_stl_fast(tmp_path, _tmp_measure_cache):
    stl = _cube_stl(tmp_path / "cube20.stl", 20.0)
    t0 = time.perf_counter()
    r = await _est([stl])
    dt = time.perf_counter() - t0
    assert dt < 2.0
    assert r is not None
    assert r["measure"] == "precise"
    assert r["v_solid_cm3"] == pytest.approx(8.0, rel=0.06)   # 20 мм куб = 8 см³ (союз-объём)
    assert 0 < r["grams_unit"] < 8.0 * _PLA.density           # меньше сплошного (есть инфилл)
    assert 0.15 <= r["fill_factor"] <= 1.0
    assert r["minutes_unit"] > 0
    assert r["grams_unit"] == pytest.approx(r["volume_cm3"] * _PLA.density, rel=1e-6)


@pytest.mark.asyncio
async def test_estimate_small_3mf(tmp_path, _tmp_measure_cache):
    m3 = _cube_3mf(tmp_path / "cube10.3mf")
    r = await _est([m3])
    assert r is not None
    assert r["v_solid_cm3"] == pytest.approx(1.0, rel=0.06)
    assert r["grams_unit"] > 0 and r["minutes_unit"] > 0


@pytest.mark.asyncio
async def test_measure_cache_reused(tmp_path, _tmp_measure_cache):
    stl = _cube_stl(tmp_path / "cube20.stl", 20.0)
    r1 = await _est([stl])
    r2 = await _est([stl])
    assert r1["measure"] == "precise"
    assert r2["measure"] == "cache"                           # второй раз — из кэша замеров
    assert r2["volume_cm3"] == pytest.approx(r1["volume_cm3"], rel=1e-9)


@pytest.mark.asyncio
async def test_bigger_volume_more_grams(tmp_path, _tmp_measure_cache):
    small = await _est([_cube_stl(tmp_path / "c20.stl", 20.0)])
    big = await _est([_cube_stl(tmp_path / "c40.stl", 40.0)])
    assert big["grams_unit"] > small["grams_unit"]            # 64 см³ >> 8 см³
    assert big["v_solid_cm3"] > small["v_solid_cm3"]
    # доля филамента у крупного НИЖЕ (инфилл разбавляет оболочку)
    assert big["fill_factor"] < small["fill_factor"]


@pytest.mark.asyncio
async def test_scale_increases_estimate(tmp_path, _tmp_measure_cache):
    stl = _cube_stl(tmp_path / "cube20.stl", 20.0)
    base = await _est([stl])
    scaled = await _est([stl], scale=2.0)
    assert scaled["v_solid_cm3"] == pytest.approx(base["v_solid_cm3"] * 8.0, rel=0.02)
    assert scaled["height_mm"] == pytest.approx(base["height_mm"] * 2.0, rel=0.02)


@pytest.mark.asyncio
async def test_estimate_none_on_garbage(tmp_path, _tmp_measure_cache):
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    assert await _est([bad]) is None
    assert await _est([tmp_path / "missing.3mf"]) is None
    assert await _est([tmp_path / "model.step"]) is None       # формат без меша — оценить нельзя


@pytest.mark.asyncio
async def test_estimate_none_when_density_zero(tmp_path, _tmp_measure_cache):
    stl = _cube_stl(tmp_path / "cube.stl", 20.0)
    r = await fastcalc.estimate([stl], plastic=SimpleNamespace(density=0.0), quality=_Q,
                                strength=_S, qty=1, settings=None, allow_background=False)
    assert r is None


# ─────── тяжёлая коала (573 МБ меша): быстрый ответ без синхронного точного замера ───────
_KOALA = config.DATA_DIR / "vk_catalog" / "55350717" / "7111" / "коала.3mf"


@pytest.mark.asyncio
@pytest.mark.skipif(not _KOALA.exists(), reason="коала.3mf отсутствует (продовый файл)")
async def test_koala_fast_answer(_tmp_measure_cache):
    """Гигант отвечает быстро: bbox-фолбэк (точный замер уйдёт в фон при allow_background),
    а НЕ синхронный точный замер на десятки секунд."""
    t0 = time.perf_counter()
    r = await _est([_KOALA])
    dt = time.perf_counter() - t0
    assert dt < 3.0                              # хэш 84-МБ файла + bbox-префикс, не 16+ c замера
    assert r is not None
    assert r["measure"] == "bbox"
    assert r["grams_unit"] > 0 and r["minutes_unit"] > 0


# ─────────────────── встраивание: compute падает на слайсе → отдаёт ≈оценку ───────────────────

@pytest.mark.asyncio
async def test_compute_falls_back_to_estimate_on_crash(tmp_db, monkeypatch, tmp_path):
    """Крэш слайсера (STATUS_STACK_OVERFLOW и т.п.) НЕ роняет расчёт: compute отдаёт цену от
    аналитической ≈оценки с флагом estimated=True (а не бросает CalcError)."""
    from app import calcservice, slicer

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    printer = await tmp_db.get_default_printer()
    plastic = (await tmp_db.list_plastics(active_only=True))[0]
    quality = (await tmp_db.list_quality(active_only=True))[0]
    strength = (await tmp_db.list_strength(active_only=True))[0]
    stl = _cube_stl(tmp_path / "cube20.stl", 20.0)

    async def _crash(*a, **k):
        raise slicer.SlicerError("Слайсер не справился с этой моделью (STATUS_STACK_OVERFLOW).")

    async def _run(func, *args, **kw):                # cpupool → синхронно в тесте
        return func(*args, **kw)

    async def _run_render(func, *args, **kw):
        return None                             # превью не рисуем

    monkeypatch.setattr(calcservice.slicer, "slice_unit", _crash)
    monkeypatch.setattr(calcservice.cpupool, "run", _run)
    monkeypatch.setattr(calcservice.cpupool, "run_render", _run_render)

    res = await calcservice.compute(
        user_id=1, model_paths=[str(stl)], model_label="cube", printer=printer, plastic=plastic,
        color="", qty=2, quality=quality, strength=strength, save=False)

    assert res["estimated"] is True
    bd = res["bd"]
    assert bd.total > 0
    assert bd.grams_unit > 0 and bd.minutes_unit > 0
    assert bd.qty == 2 and bd.grams_total == pytest.approx(bd.grams_unit * 2, rel=1e-6)


@pytest.mark.asyncio
async def test_compute_still_raises_when_estimate_impossible(tmp_db, monkeypatch, tmp_path):
    """Слайс упал И оценить нельзя (формат без меша) → честный CalcError (заказ без сметы),
    а не выдуманная цена."""
    from app import calcservice, slicer

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    printer = await tmp_db.get_default_printer()
    plastic = (await tmp_db.list_plastics(active_only=True))[0]
    quality = (await tmp_db.list_quality(active_only=True))[0]
    strength = (await tmp_db.list_strength(active_only=True))[0]
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")            # не меш — fastcalc вернёт None

    async def _crash(*a, **k):
        raise slicer.SlicerError("нет геометрии для слайсинга.")

    async def _run(func, *args, **kw):
        return func(*args, **kw)

    async def _run_render(func, *args, **kw):
        return None

    monkeypatch.setattr(calcservice.slicer, "slice_unit", _crash)
    monkeypatch.setattr(calcservice.cpupool, "run", _run)
    monkeypatch.setattr(calcservice.cpupool, "run_render", _run_render)

    with pytest.raises(calcservice.CalcError):
        await calcservice.compute(
            user_id=1, model_paths=[str(step)], model_label="part", printer=printer,
            plastic=plastic, color="", qty=1, quality=quality, strength=strength, save=False)


# ─────────────── ползунок высоты слоя (03.08.2026): явный выбор клиента ───────────────

def test_quality_explicit_rule():
    """Явность выводится из объектов: не-дефолтное качество = клиент двигал ползунок;
    best_quality_id пластика побеждает глобальный дефолт; стаб без атрибута — дефолт."""
    exp = fastcalc._quality_is_explicit
    assert exp(None, None) is False
    assert exp(_PLA, _Q) is False                       # стаб без is_default → дефолт
    assert exp(_PLA, SimpleNamespace(id=3, is_default=1, layer_height=0.2)) is False
    assert exp(_PLA, SimpleNamespace(id=6, is_default=0, layer_height=0.3)) is True
    best = SimpleNamespace(density=1.24, best_quality_id=6)
    assert exp(best, SimpleNamespace(id=6, is_default=0, layer_height=0.3)) is False
    assert exp(best, SimpleNamespace(id=3, is_default=1, layer_height=0.2)) is True


def _cube_project_3mf(path: Path, layer: float = 0.12) -> Path:
    """Куб + project_settings.config автора (слой 0.12): вход «проект со своими настройками»."""
    import json

    _cube_3mf(path)
    cfg = {"layer_height": str(layer), "wall_loops": "2", "sparse_infill_density": "15",
           "top_shell_layers": "5", "bottom_shell_layers": "3",
           "outer_wall_line_width": "0.42", "inner_wall_line_width": "0.45",
           "enable_support": "0", "filament_colour": ["#FFFFFF"],
           "sparse_infill_pattern": "rectilinear"}
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/project_settings.config", json.dumps(cfg))
    return path


@pytest.mark.asyncio
async def test_explicit_quality_overrides_project_layer(tmp_path, _tmp_measure_cache):
    """ЯВНОЕ качество перекрывает слой автора проект-3mf, дефолтное — нет: при явных 0.36 мм
    минуты заметно меньше, чем по авторским 0.12 (накладные на слои падают втрое)."""
    p = _cube_project_3mf(tmp_path / "cube_proj.3mf", layer=0.12)
    q36 = SimpleNamespace(id=7, is_default=0, layer_height=0.36,
                          process="0.36mm Draft @Elegoo CC2 0.6 nozzle.json")
    r_auth = await fastcalc.estimate([p], plastic=_PLA, quality=_Q, strength=_S, qty=1,
                                     settings=None, allow_background=False)
    r_expl = await fastcalc.estimate([p], plastic=_PLA, quality=q36, strength=_S, qty=1,
                                     settings=None, allow_background=False)
    assert r_auth is not None and r_expl is not None
    assert r_auth["grams_unit"] > 0 and r_expl["grams_unit"] > 0
    assert r_auth["minutes_unit"] > r_expl["minutes_unit"] * 1.3
