"""Стол принтера доходит до ПРЕВЬЮ и 3D-вьюера — не только до сметы (B1, хвосты).

Пробел, который закрывают эти тесты: geometry решает «дедупить ли наложенные копии» по порогу
стола (BedSpec.fit_xy_mm), смета звала загрузчик со столом РЕАЛЬНОГО принтера, а превью и GLB —
без стола, то есть по дефолтному CC2. На широком столе смета копии схлопывала, а вьюер показывал
все; привязка цветов к деталям идёт по ПОРЯДКУ деталей, поэтому расходились и цвета.

Все модели тут синтетические и крошечные (кубы по 12 граней) — тяжёлых .3mf в тестах не гоняем.
"""
from __future__ import annotations

import dataclasses
import zipfile

import pytest

from app import fastmeasure, geometry, preview
from app.bedspec import DEFAULT_BED, BedSpec, bed_key_suffix, bed_tag
from app.web import mesh3d

# Столы развилки: узкий — сегодняшний CC2 (порог «умещается» 250 мм), широкий — 406 мм
# (порог 400 мм). Общий след модели ниже — 306 мм, ровно между ними.
NARROW = DEFAULT_BED
WIDE = BedSpec(xy_mm=406.0)


def _cube_mesh_xml(side: float) -> str:
    v = [(0, 0, 0), (side, 0, 0), (side, side, 0), (0, side, 0),
         (0, 0, side), (side, 0, side), (side, side, side), (0, side, side)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    vs = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return f"<mesh><vertices>{vs}</vertices><triangles>{ts}</triangles></mesh>"


def _piled_copies_3mf(path):
    """Две РАЗНЫЕ геометрии (кубы 40 и 41 мм) по 2 НАЛОЖЕННЫЕ копии, разнесённые на 260 мм
    (общий след 306 мм).

    Узкий стол (порог 250) → 306 > 250 → раскладка считается мультиплейтом, дедуп выключен:
    остаются все 4 куба. Широкий (порог 400) → дедуп работает, каждая пара схлопывается в свою
    копию → остаются 2. Кубы намеренно РАЗНОГО размера: geometry группирует экземпляры по
    content-hash геометрии, и одинаковые кубы попали бы в одну группу с разбросом 265 мм —
    там не сработал бы уже сам дедуп, и тест был бы «зелёным» на обоих столах по ошибке."""
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources>'
            f'<object id="1" type="model">{_cube_mesh_xml(40)}</object>'
            f'<object id="2" type="model">{_cube_mesh_xml(41)}</object>'
            '</resources><build>'
            '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 5 0 0"/>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 260 0 0"/>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 265 0 0"/>'
            '</build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", core)
    return path


@pytest.fixture
def piled(tmp_path, monkeypatch):
    """Модель-развилка + изолированные каталоги кэша (боевые data/ тесты не трогают)."""
    monkeypatch.setattr(preview.config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev")
    (tmp_path / "prev").mkdir()
    return [_piled_copies_3mf(tmp_path / "piled.3mf")]


# ─────────────────────── (а) решение о копиях едет за столом ───────────────────────

def test_load_dedup_follows_bed(piled):
    """Узкий и широкий стол дают РАЗНУЮ геометрию превью: 4 куба против 2. До правки обе
    ветки считали по DEFAULT_BED, и на широком столе картинка расходилась со сметой."""
    narrow = preview._load_dedup(piled, NARROW)
    wide = preview._load_dedup(piled, WIDE)
    assert narrow is not None and wide is not None
    assert len(narrow.faces) == 2 * len(wide.faces), "широкий стол обязан схлопнуть копии"


def test_load_print_meshes_parts_follow_bed(piled):
    """Тот же разрыв на уровне ДЕТАЛЕЙ (их число и порядок задают слоты цветов в GLB)."""
    assert len(geometry.load_print_meshes(piled, NARROW)) == 4
    assert len(geometry.load_print_meshes(piled, WIDE)) == 2


def test_unit_groups_accepts_bed_and_stays_bed_independent(piled):
    """unit_groups стол ПРИНИМАЕТ (единообразие цепочки), но решений от него не принимает:
    его гейты — число инстансов и бюджет граней. Тест страхует от соблазна начать судить
    тираж по столу молча, без переписывания докстроки."""
    assert geometry.unit_groups(piled, NARROW) == geometry.unit_groups(piled, WIDE)
    assert geometry.unit_groups(piled) == geometry.unit_groups(piled, WIDE)


# ─────────────────────── (в) старые вызовы работают как раньше ───────────────────────

def test_calls_without_bed_behave_as_before(piled):
    """Стол — параметр СО ЗНАЧЕНИЕМ ПО УМОЛЧАНИЮ: вызов без него = вызов с DEFAULT_BED."""
    a = preview._load_dedup(piled)
    b = preview._load_dedup(piled, DEFAULT_BED)
    assert len(a.faces) == len(b.faces)
    assert preview.model_dims(piled) == preview.model_dims(piled, 1.0, DEFAULT_BED)


# ─────────────────────── (б) ключи кэша различают столы ───────────────────────

def test_bed_key_suffix_empty_for_default_bed():
    """ГАРД неизменности: у дефолтного стола хвост ключа ПУСТОЙ, значит имена файлов кэша
    превью/геометрии/GLB на живом CC2 остались прежними — прогретый кэш не стух, и картинка
    при одном принтере считается ровно как до правки."""
    assert bed_key_suffix(DEFAULT_BED) == ""
    assert bed_key_suffix(BedSpec()) == ""
    assert bed_key_suffix(WIDE).startswith("_b")


def test_bed_tag_covers_every_bed_field():
    """В отпечаток входит ВЕСЬ BedSpec, а не только читаемые сегодня поля: лишний промах
    кэша дешевле молча отданной чужой картинки (тот же принцип, что у кэша замеров)."""
    base = bed_tag(DEFAULT_BED)
    for field, value in (("xy_mm", 300.0), ("z_mm", 400.0), ("pile_ratio", 0.5),
                         ("arrange_gap_mm", 5.0), ("arrange_margin_mm", 12.0)):
        assert bed_tag(dataclasses.replace(DEFAULT_BED, **{field: value})) != base, field


def test_bed_tag_is_one_format_for_the_whole_project():
    """fastcalc._bed_tag остался именем, но формат теперь ОДИН (app.bedspec.bed_tag) —
    иначе ключи замеров и ключи превью разъедутся при первой же правке формата."""
    from app import fastcalc
    assert fastcalc._bed_tag(WIDE) == bed_tag(WIDE)


def test_geo_cache_key_differs_per_bed(piled):
    """Дисковый кэш геометрии превью: два стола → два РАЗНЫХ файла. Без стола в имени меш,
    дедуплицированный по CC2, отдавался бы принтеру с 400-мм столом."""
    narrow = preview._geo_cache_path(piled, preview._MAX_FACES, NARROW)
    wide = preview._geo_cache_path(piled, preview._MAX_FACES, WIDE)
    assert narrow is not None and wide is not None
    assert narrow != wide
    assert narrow.parent == wide.parent               # различаются ИМЕНЕМ, а не каталогом
    assert bed_tag(WIDE) in wide.name
    # у дефолтного стола имя РОВНО прежнее — прогретый кэш CC2 не стух
    assert narrow == preview._geo_cache_path(piled, preview._MAX_FACES)
    assert narrow.name.endswith("_v11.stl")


def test_dims_memo_keyed_by_bed(piled):
    """Процессный мемо габаритов ключуется (хэш, СТОЛ): на одном лишь хэше второй принтер
    получил бы габариты, посчитанные по чужому столу — молча, без ошибки в логе. А эта строка
    уходит в смету, PDF и каталог."""
    preview._dims_memo.clear()
    assert preview.model_dims(piled, 1.0, NARROW)[0]
    assert preview.model_dims(piled, 1.0, WIDE)[0]
    beds = {k[1] for k in preview._dims_memo}
    assert beds == {NARROW, WIDE}, preview._dims_memo.keys()
    # повтор берётся из мемо СВОЕГО стола, а не соседнего
    assert preview.model_dims(piled, 1.0, WIDE) == preview.model_dims(piled, 1.0, WIDE)


def test_preview_cache_key_differs_per_bed(piled, tmp_path):
    """Готовая картинка тоже ключуется столом (иначе превью одного принтера прилипнет
    к другому — грабля матрицы слайсеров 12.08, где движок забыли в ключе кэша)."""
    a = preview.render_preview(piled, None, 4000, NARROW)
    b = preview.render_preview(piled, None, 4000, WIDE)
    assert a is not None and b is not None and a != b
    assert bed_key_suffix(NARROW) == "" and a.name.endswith("_4000.jpg")
    assert bed_tag(WIDE) in b.name


def test_glb_cache_key_differs_per_bed(piled, tmp_path, monkeypatch):
    """3D-вьюер: сцена GLB зависит от стола (число и ПОРЯДОК узлов = порядок деталей,
    а клиентский фолбэк красит colors[i] по порядку) → стол обязан быть в ключе."""
    monkeypatch.setattr(mesh3d.config, "CACHE_DIR", tmp_path / "cache")
    assert mesh3d._model3d_glb_bytes(piled, NARROW)
    assert mesh3d._model3d_glb_bytes(piled, WIDE)
    files = sorted(p.name for p in (tmp_path / "cache" / "geo").glob("glb_*.glb"))
    assert len(files) == 2, files
    assert any(bed_tag(WIDE) in n for n in files)


# ─────────────────────── (г) смета и картинка согласованы ───────────────────────

@pytest.mark.parametrize("bed, parts", [(NARROW, 4), (WIDE, 2)])
def test_estimate_and_picture_agree_on_part_count(piled, bed, parts):
    """На ОДНОМ столе смета (fastmeasure) и картинка (geometry) видят одинаковое число
    деталей. Это и есть смысл всей правки: раньше правая часть равенства всегда считалась
    по CC2, чем бы ни считала левая."""
    # config у fastmeasure и preview — ОДИН модуль, кэш уже уведён в tmp фикстурой piled
    meas = fastmeasure.measure_model(piled, True, bed=bed)
    assert meas is not None
    assert meas["n_parts"] == parts
    assert len(geometry.load_print_meshes(piled, bed)) == parts
