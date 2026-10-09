"""Стол принтера → ЦЕНА: сквозные тесты волны 6, шага 2.

ЗАЧЕМ. Шаг 1 завёл BedSpec и протащил стол ПАРАМЕТРОМ, но реальный стол принтера никто вниз
не передавал: любой принтер считался зашитым столом CC2. Шаг 2 подставил в расчёт настоящий
профиль машины — и это ДВИГАЕТ ЦЕНУ по двум независимым каналам:

  • раскладка набора по столам (fastmeasure.estimate_plates) — столов больше/меньше, а за
    каждый стол берётся settings["plate_fee"];
  • дедуп наложенных копий (fastmeasure._aggregate) — на широком столе раскладка перестаёт
    считаться мультиплейтом, копии схлопываются, объём (а с ним граммы) падает вдвое.

Сквозного теста «стол → цена» до этого шага не было вовсе, поэтому обе цепочки проверяются
через настоящий calcservice.compute, а не через кусочки внутри fastmeasure.

Плюс ключ кэша замеров: раньше он про стол не знал, и два принтера с разными столами делили
одну запись — второй получал цифры первого, то есть чужую цену.
"""
import dataclasses
import json
import zipfile

import pytest

from app import calcservice, fastcalc, fastmeasure
from app.bedspec import DEFAULT_BED, BedSpec

# ─────────────────────────── синтетика ───────────────────────────

_CUBE_V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
           (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_CUBE_T = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
           (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _cube_mesh_xml(side: float) -> str:
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % (x * side / 10, y * side / 10, z * side / 10)
                for x, y, z in _CUBE_V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tr for tr in _CUBE_T)
    return f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>"


def _machine(tmp_path, name: str, side: float):
    """Синтетический профиль машины с квадратным столом side×side (как их читает slicer.bed_size)."""
    p = tmp_path / name
    p.write_text(json.dumps({
        "printable_area": ["0x0", f"{side:g}x0", f"{side:g}x{side:g}", f"0x{side:g}"],
        "printable_height": str(side)}), encoding="utf-8")
    return p


def _box_stl(path, w: float, d: float, h: float):
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(w, d, h)).export(str(path))
    return str(path)


def _piled_copies_3mf(path):
    """Две геометрии по 2 НАЛОЖЕННЫЕ копии, разнесённые на 260 мм (общий след 305 мм).

    Стол CC2 (порог «умещается» 250) → 305 > 250 → раскладка считается мультиплейтом, дедуп
    выключен → меряются все 4 куба. Стол 406 мм (порог 400) → дедуп работает → остаются 2.
    Ровно та развилка, из-за которой один и тот же файл весит разное на разных принтерах."""
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources>'
            f'<object id="1" type="model">{_cube_mesh_xml(40)}</object>'
            f'<object id="2" type="model">{_cube_mesh_xml(40)}</object>'
            '</resources><build>'
            '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 5 0 0"/>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 260 0 0"/>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 265 0 0"/>'
            '</build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", core)
    return str(path)


async def _presets(tmp_db):
    printer = await tmp_db.get_default_printer()
    plastic = (await tmp_db.list_plastics(active_only=True))[0]
    quality = (await tmp_db.list_quality(active_only=True))[0]
    strength = (await tmp_db.list_strength(active_only=True))[0]
    return printer, plastic, quality, strength


def _sync_pools(monkeypatch):
    async def _run(func, *args, **kw):
        return func(*args, **kw)

    async def _run_render(func, *args, **kw):
        return None
    monkeypatch.setattr(calcservice.cpupool, "run", _run)
    monkeypatch.setattr(calcservice.cpupool, "run_render", _run_render)


def _no_slice(monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("слайсер в этих тестах вызываться не должен (slicing_enabled=0)")
    monkeypatch.setattr(calcservice.slicer, "slice_unit", _boom)


# ─────────────────────── стол принтера → число столов → цена ───────────────────────

async def test_printer_bed_changes_plates_and_price(tmp_db, monkeypatch, tmp_path):
    """ОДИН И ТОТ ЖЕ набор, два принтера: узкий стол (256) кладёт его на 2 стола, широкий (400)
    на 1 — и цена расходится на доплату за стол (settings["plate_fee"]).

    Пять плиток 110×110 мм. Рабочая зона = стол − 2×10 мм отступа, деталь занимает 118 мм с
    зазором: на 236 мм помещается 2×2 плитки, пятая уезжает на второй стол; на 380 мм — 3+2 на
    одном. До этого шага оба принтера считались столом CC2 и второй переплачивал за лишний стол."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _no_slice(monkeypatch)
    _sync_pools(monkeypatch)
    printer, plastic, quality, strength = await _presets(tmp_db)
    paths = [_box_stl(tmp_path / f"tile{i}.stl", 110.0, 110.0, 4.0) for i in range(5)]

    async def calc(bed_side: float):
        pr = dataclasses.replace(
            printer, machine_profile=str(_machine(tmp_path, f"m{bed_side:g}.json", bed_side)))
        return await calcservice.compute(
            user_id=1, model_paths=paths, model_label="tiles", printer=pr, plastic=plastic,
            color="", qty=1, quality=quality, strength=strength, save=False)

    narrow = await calc(256.0)
    wide = await calc(400.0)

    assert (narrow["plates"], wide["plates"]) == (2, 1), "стол не доехал до раскладки по столам"
    fee = float((await tmp_db.get_settings())["plate_fee"])
    assert fee > 0, "тест бессмыслен при нулевой доплате за стол"
    assert narrow["bd"].plate_fee == pytest.approx(fee * 2)
    assert wide["bd"].plate_fee == pytest.approx(fee * 1)
    assert narrow["bd"].total > wide["bd"].total      # ← ЦЕНА, а не только диагностика
    # граммы тут не при чём: разошлась именно раскладка
    assert narrow["bd"].grams_unit == pytest.approx(wide["bd"].grams_unit)


# ─────────────────────── стол принтера → дедуп копий → граммы → цена ───────────────────────

async def test_printer_bed_changes_dedup_grams_and_price(tmp_db, monkeypatch, tmp_path):
    """Тот же .3mf с наложенными копиями на двух принтерах: на широком столе копии
    схлопываются, и модель весит ВДВОЕ меньше — это самый крупный ценовой сдвиг волны."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _no_slice(monkeypatch)
    _sync_pools(monkeypatch)
    printer, plastic, quality, strength = await _presets(tmp_db)
    m3 = _piled_copies_3mf(tmp_path / "piled.3mf")

    async def calc(bed_side: float):
        pr = dataclasses.replace(
            printer, machine_profile=str(_machine(tmp_path, f"m{bed_side:g}.json", bed_side)))
        return await calcservice.compute(
            user_id=1, model_paths=[m3], model_label="piled", printer=pr, plastic=plastic,
            color="", qty=1, quality=quality, strength=strength, save=False)

    narrow = await calc(256.0)     # порог 250 < след 305 → мультиплейт, дедупа нет: 4 куба
    wide = await calc(406.0)       # порог 400 > след 305 → копии схлопнуты: 2 куба

    assert narrow["plates"] == wide["plates"] == 1, "раскладка не должна мешать замеру дедупа"
    assert narrow["bd"].grams_unit == pytest.approx(2.0 * wide["bd"].grams_unit, rel=0.02)
    assert narrow["bd"].total > wide["bd"].total


async def test_default_profile_keeps_todays_price(tmp_db, monkeypatch, tmp_path):
    """ГАРД: у принтера с непрочитанным профилем стол = DEFAULT_BED, то есть цена ровно та же,
    что до волны. Живой CC2 читается как 256×256 — тот же DEFAULT_BED, поэтому боевые расчёты
    этой волной не сдвинутся; двигаются только принтеры с ДРУГИМ столом."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _no_slice(monkeypatch)
    _sync_pools(monkeypatch)
    printer, plastic, quality, strength = await _presets(tmp_db)
    m3 = _piled_copies_3mf(tmp_path / "piled.3mf")

    async def calc(profile: str):
        pr = dataclasses.replace(printer, machine_profile=profile)
        return await calcservice.compute(
            user_id=1, model_paths=[m3], model_label="piled", printer=pr, plastic=plastic,
            color="", qty=1, quality=quality, strength=strength, save=False)

    cc2 = await calc(str(_machine(tmp_path, "cc2.json", DEFAULT_BED.xy_mm)))
    broken = await calc(str(tmp_path / "нет.json"))
    assert cc2["bd"].total == pytest.approx(broken["bd"].total)
    assert cc2["bd"].grams_unit == pytest.approx(broken["bd"].grams_unit)


# ─────────────────────── ключ кэша замеров знает про стол ───────────────────────

def test_measure_cache_key_differs_per_bed(tmp_path, monkeypatch):
    """Одна модель, два стола → РАЗНЫЕ файлы кэша. Без этого второй принтер молча получил бы
    замер первого (то есть его граммы и его цену) — ради чего шаг 2 и делается одним куском."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    model = [_box_stl(tmp_path / "box.stl", 20.0, 20.0, 20.0)]

    a = fastcalc._measure_cache_path(model, True, None, DEFAULT_BED)
    b = fastcalc._measure_cache_path(model, True, None, BedSpec(xy_mm=400.0))
    assert a != b
    assert a.parent == b.parent          # различаются именно ИМЕНЕМ, а не каталогом
    # тот же стол — тот же файл (иначе кэш не работал бы вовсе)
    assert a == fastcalc._measure_cache_path(model, True, None, BedSpec())


def test_measure_cache_key_covers_every_bed_field(tmp_path, monkeypatch):
    """В ключ входит ВЕСЬ BedSpec, а не только те поля, что замер читает сегодня: лишний
    промах кэша дешевле молча отданной чужой сметы."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    model = [_box_stl(tmp_path / "box.stl", 20.0, 20.0, 20.0)]
    base = fastcalc._measure_cache_path(model, True, None, DEFAULT_BED)
    for field, value in (("xy_mm", 300.0), ("z_mm", 400.0), ("pile_ratio", 0.5),
                         ("arrange_gap_mm", 5.0), ("arrange_margin_mm", 12.0)):
        other = dataclasses.replace(DEFAULT_BED, **{field: value})
        assert fastcalc._measure_cache_path(model, True, None, other) != base, field


def test_measure_version_bumped_for_bed_dependent_cache(tmp_path, monkeypatch):
    """Версия замеров поднята вместе с добавлением стола в ключ. Прежние записи (fm5) считались
    столом CC2 при любом железе — они обязаны перестать читаться, иначе кэш пережил бы правку
    и продолжил отдавать цифры чужого стола."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    assert fastcalc.MEASURE_VERSION != "fm5"
    name = fastcalc._measure_cache_path([_box_stl(tmp_path / "b.stl", 10, 10, 10)], True).name
    assert name.endswith(f"-{fastcalc.MEASURE_VERSION}.json")


# ─────────────────────── стол доезжает от принтера до замера ───────────────────────

async def test_estimate_reads_bed_from_printer_profile(tmp_db, monkeypatch, tmp_path):
    """fastcalc.estimate без явного bed берёт стол из printer.machine_profile (раньше брала
    зашитые 256 для дедупа и отдельно звала slicer.bed_size для раскладки — два числа могли
    разъехаться)."""
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _, plastic, quality, _ = await _presets(tmp_db)
    paths = [_box_stl(tmp_path / f"tile{i}.stl", 110.0, 110.0, 4.0) for i in range(5)]

    async def est(bed_side, **kw):
        pr = type("P", (), {"machine_profile": str(_machine(tmp_path, f"m{bed_side:g}.json",
                                                            bed_side)), "process_dir": ""})()
        r = await fastcalc.estimate(paths, plastic=plastic, quality=quality, strength=None,
                                    printer=pr, allow_background=False, **kw)
        assert r is not None
        return r["est_plates"]

    assert await est(256.0) == 2
    assert await est(400.0) == 1
    # явный bed сильнее профиля принтера (так его передаёт calcservice.compute)
    assert await est(256.0, bed=BedSpec(xy_mm=400.0)) == 1


def test_estimate_plates_matches_the_bed_used_for_measuring():
    """Раскладка и замер судят об ОДНОМ столе: обе цепочки читают один BedSpec."""
    fps = [(110.0, 110.0)] * 5
    assert fastmeasure.estimate_plates(fps, bed=BedSpec(xy_mm=256.0)) == 2
    assert fastmeasure.estimate_plates(fps, bed=BedSpec(xy_mm=400.0)) == 1
