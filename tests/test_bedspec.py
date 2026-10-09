"""BedSpec — параметры стола принтера одним объектом (app/bedspec.py).

ЗАЧЕМ ЭТО ВООБЩЕ ТЕСТИРУЕТСЯ. Порог «размер стола» решает не только «мультиплейт ли
раскладка», но и ДЕДУПИТЬ ЛИ наложенные копии: один и тот же набор при разном столе даёт
разный объём и разное число деталей, то есть разные граммы и разную цену. Пока стол зашит
константой CC2, принтер с другим столом считается по чужому числу.

Волна 6, шаг 1: BedSpec заведён и протащен ПАРАМЕТРОМ со значением по умолчанию, поведение
не меняется. Здесь проверяем ровно это: (а) числа по умолчанию — те же, что стояли
константами; (б) стол читается из профиля машины; (в) параметр реально влияет на дедуп,
когда его передают явно.
"""
import json

import numpy as np
import pytest

from app import fastmeasure, geometry, slicer
from app.bedspec import DEFAULT_BED, BedSpec

# ─────────────────────────── числа по умолчанию ───────────────────────────

def test_defaults_are_todays_constants():
    """Дефолты BedSpec — РОВНО те числа, что стояли константами в geometry/fastmeasure/slicer.
    Любая правка этих значений двигает цену на всех расчётах — она должна ронять этот тест."""
    b = BedSpec()
    assert b.xy_mm == 256.0            # slicer._BED_FULL_MM / фолбэк bed_size / fastcalc / texts
    assert b.z_mm == 256.0             # фолбэк printable_height
    assert b.pile_ratio == 0.75        # geometry._PILE_RATIO == fastmeasure._PILE_RATIO
    assert b.arrange_gap_mm == 8.0     # estimate_plates: зазор между деталями
    assert b.arrange_margin_mm == 10.0  # estimate_plates: отступ от краёв
    assert b == DEFAULT_BED


def test_fit_xy_is_exactly_the_old_250():
    """fit_xy_mm — прежний _BED_MM: «умещается на столе», стол минус запас на края.
    На дефолтном столе ровно 250.0 (без плавучей погрешности — сравнение точное)."""
    assert DEFAULT_BED.fit_xy_mm == 250.0


def test_fit_xy_follows_the_bed():
    """Порог едет за столом принтера, а не остаётся числом CC2 на любом железе."""
    assert BedSpec(xy_mm=300.0).fit_xy_mm == 294.0
    assert BedSpec(xy_mm=350.0).fit_xy_mm == 344.0
    # вырожденный стол не даёт отрицательного/нулевого порога
    assert BedSpec(xy_mm=1.0).fit_xy_mm == 1.0


def test_spec_is_frozen_hashable_picklable():
    """Уезжает в cpupool-воркеры (подготовка геометрии) и в мемо-ключи — значит должен
    пикуться и хэшироваться, а поменять его на лету нельзя."""
    import pickle

    b = BedSpec(xy_mm=300.0)
    with pytest.raises(Exception):
        b.xy_mm = 400.0            # type: ignore[misc]
    assert pickle.loads(pickle.dumps(b)) == b
    assert hash(b) == hash(BedSpec(xy_mm=300.0))
    assert {b, BedSpec(xy_mm=300.0)} == {b}


# ─────────────────────────── чтение из профиля машины ───────────────────────────

def _machine(tmp_path, name, area, height):
    p = tmp_path / name
    p.write_text(json.dumps({"printable_area": area, "printable_height": str(height)}),
                 encoding="utf-8")
    return p


def test_from_machine_profile_reads_bed(tmp_path):
    p = _machine(tmp_path, "big.json", ["0x0", "300x0", "300x300", "0x300"], 400)
    b = BedSpec.from_machine_profile(p)
    assert (b.xy_mm, b.z_mm) == (300.0, 400.0)
    assert b.fit_xy_mm == 294.0
    # остальное — общие для всех принтеров калибровки, из профиля не читаются
    assert (b.pile_ratio, b.arrange_gap_mm, b.arrange_margin_mm) == (0.75, 8.0, 10.0)


def test_from_machine_profile_matches_bed_size(tmp_path):
    """Единственный источник размера стола — существующая slicer.bed_size (в т.ч. её правило
    «прямоугольный стол → меньшая сторона»), BedSpec её не дублирует."""
    p = _machine(tmp_path, "rect.json", ["0x0", "350x0", "350x300", "0x300"], 325)
    b = BedSpec.from_machine_profile(p)
    assert (b.xy_mm, b.z_mm) == slicer.bed_size(p)
    assert b.xy_mm == 300.0            # меньшая сторона: масштаб должен влезть по обеим


def test_from_machine_profile_falls_back_to_defaults(tmp_path):
    """Профиль не задан / не найден / битый → консервативный CC2, то есть DEFAULT_BED:
    расчёт не падает и считает как раньше."""
    assert BedSpec.from_machine_profile(None) == DEFAULT_BED
    assert BedSpec.from_machine_profile("") == DEFAULT_BED
    assert BedSpec.from_machine_profile(tmp_path / "нет.json") == DEFAULT_BED
    broken = tmp_path / "broken.json"
    broken.write_text("{не json", encoding="utf-8")
    assert BedSpec.from_machine_profile(broken) == DEFAULT_BED


def test_from_machine_profile_overrides(tmp_path):
    p = _machine(tmp_path, "big.json", ["0x0", "300x0", "300x300", "0x300"], 400)
    b = BedSpec.from_machine_profile(p, pile_ratio=0.5, arrange_gap_mm=5.0)
    assert (b.xy_mm, b.pile_ratio, b.arrange_gap_mm) == (300.0, 0.5, 5.0)
    assert b.arrange_margin_mm == 10.0


# ─────────────── дефолт == прежнее поведение в местах, куда стол протащен ───────────────

def test_bed_size_fallback_is_the_default_spec(tmp_path):
    assert slicer.bed_size(tmp_path / "нет.json") == (DEFAULT_BED.xy_mm, DEFAULT_BED.z_mm)


def test_estimate_plates_default_spec_equals_old_call():
    """estimate_plates(fps, 256.0) — как её зовёт fastcalc — совпадает с расчётом по
    DEFAULT_BED: значит зазор 8 и отступ 10 переехали в BedSpec без сдвига."""
    fps = [(108.6, 84.3), (100.0, 100.0), (100.0, 100.0), (100.0, 100.0),
           (100.0, 100.0), (49.4, 39.7)]
    assert fastmeasure.estimate_plates(fps, 256.0) == fastmeasure.estimate_plates(fps)
    assert fastmeasure.estimate_plates(fps, 256.0) == fastmeasure.estimate_plates(fps, bed=DEFAULT_BED)


def test_estimate_plates_bigger_bed_needs_fewer_plates():
    """Стол больше — столов меньше. Явное подтверждение, что параметр доехал до укладки."""
    fps = [(100.0, 100.0)] * 5
    assert fastmeasure.estimate_plates(fps, bed=DEFAULT_BED) == 2
    assert fastmeasure.estimate_plates(fps, bed=BedSpec(xy_mm=400.0)) == 1


def test_pack_into_beds_default_spec_equals_old_threshold(tmp_path):
    """slicer._pack_into_beds без bed_mm берёт fit_xy_mm — прежние 250.0."""
    sized = [(tmp_path / f"p{i}.stl", 120.0, 120.0) for i in range(4)]
    assert slicer._pack_into_beds(sized) == slicer._pack_into_beds(sized, 250.0)


# ─────────────────────────── стол двигает дедуп копий (а значит цену) ───────────────────────────

def _placed(x0: float, fp: tuple, size: float = 40.0) -> dict:
    """Один размещённый экземпляр для fastmeasure._aggregate: куб size мм с началом в x0."""
    lo = np.array([x0, 0.0, 0.0])
    return {"v": 1000.0, "v_union": 1000.0, "exposure": 1.0, "air": 0.0,
            "buckets": np.ones((2, fastmeasure.NB, 3)),
            "bbox": np.stack([lo, lo + size]), "fp": fp}


def test_aggregate_bed_decides_dedup_and_volume():
    """Две пары наложенных копий, разнесённые на 260 мм.

    На дефолтном столе общий след (305 мм) больше порога 250 → раскладка считается
    мультиплейтом, дедуп ОТКЛЮЧЁН → 4 детали и 4000 мм³. На столе пошире тот же набор
    порога не превышает → копии схлопываются → 2 детали и 2000 мм³. Это и есть тот самый
    «разный стол — разные граммы и разная цена», ради которого стол стал параметром."""
    placed = [_placed(0.0, ("e", "1")), _placed(5.0, ("e", "1")),
              _placed(260.0, ("e", "2")), _placed(265.0, ("e", "2"))]

    small = fastmeasure._aggregate(placed, True)                    # DEFAULT_BED, fit 250
    assert (small["n_parts"], small["v_mm3"]) == (4, 4000.0)

    wide = fastmeasure._aggregate(placed, True, BedSpec(xy_mm=406.0))  # fit 400
    assert (wide["n_parts"], wide["v_mm3"]) == (2, 2000.0)


def test_dedup_piled_respects_pile_ratio():
    """pile_ratio тоже приехал из BedSpec: тот же набор при более строгом пороге не схлопывается."""
    import trimesh

    def inst(x, name):
        m = trimesh.creation.box(extents=(40, 40, 40))
        m.apply_translation([x, 0, 0])
        return (m, "same-hash", np.array([x, 0.0, 0.0]), 69.28, name)

    insts = [inst(0.0, "a"), inst(40.0, "b")]      # разброс 40 мм при диагонали ~69 мм
    assert len(geometry._dedup_piled(insts)) == 1                              # 40 < 0.75×69
    assert len(geometry._dedup_piled(insts, BedSpec(pile_ratio=0.5))) == 2     # 40 > 0.50×69
