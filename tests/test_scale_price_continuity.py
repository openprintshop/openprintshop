"""Масштаб не должен ДОРОЖИТЬ модель: 92% обязаны стоить дешевле 100%.

Тикет клиента 26.07.2026: «Large Display Shelf w/ 3 Tiers» на 92% давала 70+ руб, а на 100% —
55 руб. Причина: настройки печати автора (project_settings.config — у полки 5% gyroid) читались
ТОЛЬКО на нативном пути слайсера, а он включается ровно при масштабе 100%. Любой другой масштаб
(и выбор столов) переводил расчёт на процесс-профиль бота с 15% заполнения — плюс ~75% пластика,
и цена рвалась на границе 100%.

Проверяем инвариант: у проекта-3mf со СВОИМИ настройками граммы монотонно растут по масштабу и
источник настроек остаётся авторским при любом масштабе."""
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, fastcalc, slicer

_PLA = SimpleNamespace(density=1.24)
_Q = SimpleNamespace(layer_height=0.20, process="0.20mm Standard @Elegoo CC2 0.4 nozzle.json")
_S = SimpleNamespace(infill=20, walls=3)

# Куб 100 мм: тело 1000 см³ — на таком объёме разница «5% против 15%» видна в разы
# (у мелкого куба всё съедает оболочка, и подмена настроек не проявляется).
_SIDE = 100.0
_V = [(0, 0, 0), (_SIDE, 0, 0), (_SIDE, _SIDE, 0), (0, _SIDE, 0),
      (0, 0, _SIDE), (_SIDE, 0, _SIDE), (_SIDE, _SIDE, _SIDE), (0, _SIDE, _SIDE)]
_T = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
      (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]

# Настройки «автора»: заполнение ВТРОЕ меньше пресетного дефолта бота (15%) — как у полки.
_PROJECT_CFG = {"layer_height": "0.2", "wall_loops": "3", "sparse_infill_density": "5%",
                "sparse_infill_pattern": "gyroid", "top_shell_layers": "7",
                "bottom_shell_layers": "3", "outer_wall_line_width": "0.42",
                "inner_wall_line_width": "0.45", "enable_support": "0",
                "filament_colour": ["#FFFFFF"]}


def _project_3mf(path: Path, cfg: dict | None = None) -> Path:
    """Проект-3mf: геометрия + Metadata/project_settings.config (родные настройки автора)."""
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % t for t in _V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tr for tr in _T)
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>%s</vertices>'
            '<triangles>%s</triangles></mesh></object></resources>'
            '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build></model>'
            % (v, t))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", core)
        z.writestr("Metadata/project_settings.config", json.dumps(cfg or _PROJECT_CFG))
    return path


@pytest.fixture()
def _tmp_measure_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    return tmp_path


async def _est(paths, **kw):
    kw.setdefault("allow_background", False)
    return await fastcalc.estimate(paths, plastic=_PLA, quality=_Q, strength=_S, qty=1,
                                   settings=None, **kw)


@pytest.mark.asyncio
async def test_scale_down_never_costs_more(tmp_path, _tmp_measure_cache):
    """Главный инвариант тикета: уменьшение масштаба не увеличивает граммы."""
    m3 = _project_3mf(tmp_path / "shelf.3mf")
    grams = {}
    for pct in (100, 99, 95, 92, 80, 50):
        r = await _est([m3], scale=pct / 100.0)
        assert r is not None
        grams[pct] = r["grams_unit"]
    order = [100, 99, 95, 92, 80, 50]
    for a, b in zip(order, order[1:]):
        assert grams[b] < grams[a], f"{b}% ({grams[b]:.1f} г) не дешевле {a}% ({grams[a]:.1f} г)"
    # разрыв ровно на границе 100% (симптом бага) — соседний масштаб отличается на единицы %
    assert grams[99] == pytest.approx(grams[100], rel=0.05)


@pytest.mark.asyncio
async def test_project_settings_survive_scale(tmp_path, _tmp_measure_cache):
    """Настройки автора читаются при любом масштабе, а не только на нативном пути (100%)."""
    m3 = _project_3mf(tmp_path / "shelf.3mf")
    at100 = await _est([m3])
    at92 = await _est([m3], scale=0.92)
    assert at100["settings_src"] == "native"        # проект целиком как есть
    assert at92["settings_src"] == "project"        # те же настройки автора на масштабе
    # заполнение осталось авторским (5%), а не пресетным (15%): доля филамента почти не изменилась
    assert at92["fill_factor"] == pytest.approx(at100["fill_factor"], rel=0.15)


@pytest.mark.asyncio
async def test_plate_selection_keeps_project_settings(tmp_path, _tmp_measure_cache):
    """Выбор столов тоже отключал нативный путь — настройки автора должны выжить и там."""
    m3 = _project_3mf(tmp_path / "shelf.3mf")
    r = await _est([m3], plates_only=[0])
    assert r is not None and r["settings_src"] == "project"


@pytest.mark.asyncio
async def test_stl_still_uses_preset(tmp_path, _tmp_measure_cache):
    """У обычного меша своих настроек нет — путь пресетный, как и раньше."""
    trimesh = pytest.importorskip("trimesh")
    stl = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(stl))
    r = await _est([stl], scale=0.92)
    assert r is not None and r["settings_src"] == "preset"


def test_slicer_carries_project_settings_into_process(tmp_path):
    """Слайсер при масштабе/выборе столов подставляет настройки автора в процесс-профиль —
    иначе фон-слайс вернул бы ту же задранную цену, что и до фикса."""
    m3 = _project_3mf(tmp_path / "shelf.3mf")
    ov = slicer._project_overrides([m3])
    assert ov["sparse_infill_density"] == "5%"
    assert ov["wall_loops"] == "3" and ov["top_shell_layers"] == "7"

    base = tmp_path / "process.json"
    base.write_text(json.dumps({"name": "base", "sparse_infill_density": "15%",
                                "wall_loops": "2", "top_shell_layers": "5"}), encoding="utf-8")
    out = tmp_path / "out.json"
    slicer._build_process_json(base, out, "ECC2", ov)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["sparse_infill_density"] == "5%"     # автор перекрыл профиль бота
    assert data["wall_loops"] == "3"
    assert data["compatible_printers"] == ["ECC2"]


def test_slicer_overrides_empty_for_plain_mesh(tmp_path):
    """У STL и у 3mf без project_settings перекрывать нечем — профиль идёт КАК ЕСТЬ (16.07)."""
    stl = tmp_path / "cube.stl"
    stl.write_bytes(b"solid x\nendsolid x\n")
    assert slicer._project_overrides([stl]) == {}
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    assert slicer._project_overrides([plain]) == {}
