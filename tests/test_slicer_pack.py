"""Юнит-тест укладки деталей по столам (bin-packing) для слайсинга крупных сборок.
Чистая функция — без слайсера, сети и trimesh (размеры передаём синтетически)."""
from pathlib import Path

import pytest

from app import slicer


def test_pack_into_beds_groups_small_and_isolates_large():
    bed = 100.0                       # площадь стола 10000, бюджет укладки = 6000 (0.6)
    sized = [
        (Path("a"), 50, 50),          # 2500
        (Path("b"), 50, 50),          # 2500  → a+b = 5000 ≤ 6000: один стол
        (Path("c"), 50, 50),          # 2500  → +2500 > 6000: следующий стол
        (Path("big"), 130, 40),       # сторона 130 > стол 100 → отдельный стол
    ]
    bins = slicer._pack_into_beds(sized, bed)
    flat = [p for b in bins for p in b]

    assert len(flat) == 4 and len(set(flat)) == 4     # все детали учтены, без дублей и потерь
    assert [Path("big")] in bins                       # крупная деталь — на отдельном столе
    assert any(len(b) >= 2 for b in bins)              # мелкие детали сгруппированы вместе
    assert len(bins) >= 2                              # не всё свалено на один стол


def test_pack_into_beds_single_bed_when_everything_fits():
    bed = 100.0
    sized = [(Path("a"), 20, 20), (Path("b"), 20, 20), (Path("c"), 20, 20)]  # 400×3 = 1200 ≤ 6000
    bins = slicer._pack_into_beds(sized, bed)
    assert len(bins) == 1 and len(bins[0]) == 3        # всё на одном столе — как при обычной нарезке


def test_pack_into_beds_unknown_size_does_not_crash():
    bed = 100.0
    sized = [(Path("a"), 0.0, 0.0), (Path("b"), 0.0, 0.0)]   # размер неизвестен → площадь 0
    bins = slicer._pack_into_beds(sized, bed)
    assert sorted(str(p) for b in bins for p in b) == ["a", "b"]


# ---- рекурсивное под-разбиение «неукладываемой» плиты (_slice_group) ----
# Имитирует card shuffler: набор крашит ElegooSlicer (exit -101), если на одном столе оказались
# обе крупные детали; разбиение через одну разводит их по половинам, и всё досчитывается.
_BIG = {Path("big1"), Path("big2")}


async def test_slice_group_bisects_unsliceable_combo_and_sums():
    calls: list[list[Path]] = []

    async def fake_run(parts, _outdir):
        calls.append(list(parts))
        if _BIG.issubset(set(parts)):
            raise slicer.SlicerError("exit -101: набор не разложился на стол")
        return float(len(parts)), float(len(parts) * 10)   # объём=кол-во, время=кол-во×10

    parts = [Path("big1"), Path("big2"), Path("s1"), Path("s2")]   # [::2] разведёт big1/big2
    vol, mins = await slicer._slice_group(parts, Path("out"), fake_run)

    assert (vol, mins) == (4.0, 40.0)                  # все 4 детали учтены (сумма по половинам)
    assert any(_BIG.issubset(set(c)) for c in calls)   # сперва пробовали целиком — и упали
    ok = [c for c in calls if not _BIG.issubset(set(c))]
    assert all(len(c) >= 1 for c in ok) and len(ok) == 2   # ровно две успешные половины


async def test_slice_group_no_split_when_first_try_succeeds():
    calls: list[list[Path]] = []

    async def ok(parts, _outdir):
        calls.append(list(parts))
        return 5.0, 50.0

    vol, mins = await slicer._slice_group([Path("a"), Path("b")], Path("out"), ok)
    assert (vol, mins) == (5.0, 50.0) and len(calls) == 1   # один проход, без разбиения


async def test_slice_group_single_part_failure_propagates():
    async def always_fail(_parts, _outdir):
        raise slicer.SlicerError("деталь не режется")

    with pytest.raises(slicer.SlicerError):
        await slicer._slice_group([Path("only")], Path("out"), always_fail)


# ---- ремонт одиночной детали через децимированную копию-сиблинг (баг Mark 85, плита 4) ----
# Слайсер отвергает некоторые сложные меши (вырожденные слайверы валят чекер gcode-path-conflict,
# exit -100). Восстановить из экспортированного STL нельзя → при экспорте рядом кладётся
# <stem>.repair.stl из чистого in-memory меша; _slice_group берёт её фолбэком.

def test_repair_sibling_path(tmp_path):
    assert slicer._repair_sibling(tmp_path / "p4_merged.stl") == tmp_path / "p4_merged.repair.stl"


async def test_slice_group_single_part_retries_repair_sibling(tmp_path):
    part = tmp_path / "p0_merged.stl"
    part.write_bytes(b"solid")
    repair = tmp_path / "p0_merged.repair.stl"          # «ремонтная» копия лежит рядом
    repair.write_bytes(b"solid-decimated")
    calls: list[list[Path]] = []

    async def run(parts, _outdir):
        calls.append(list(parts))
        if parts[0] == part:
            raise slicer.SlicerError("exit -100: чекер коллизий упал на детали")
        return 7.0, 70.0                                 # ремонтная копия режется

    vol, mins = await slicer._slice_group([part], tmp_path / "out", run)
    assert (vol, mins) == (7.0, 70.0)                    # досчитано по ремонтной копии
    assert calls[0] == [part] and calls[1] == [repair]   # сперва оригинал, затем .repair.stl


async def test_slice_group_single_part_no_sibling_propagates(tmp_path, monkeypatch):
    part = tmp_path / "p0_merged.stl"                    # ни ремонтной копии, ни центрирования
    part.write_bytes(b"solid")

    async def no_center(_func, *_a):                     # _center_on_bed «не смог» → None
        return None
    monkeypatch.setattr(slicer.cpupool, "run", no_center)

    async def always_fail(_parts, _outdir, arrange="1"):
        raise slicer.SlicerError("exit -100")

    with pytest.raises(slicer.SlicerError):              # честный отказ, а не молчаливый ноль
        await slicer._slice_group([part], tmp_path / "out", always_fail)


async def test_slice_group_centers_oversized_part_when_arrange_fails(tmp_path, monkeypatch):
    # деталь ~со стол: --arrange 1 → -50; центрируем (_center_on_bed) и режем с --arrange 0
    part = tmp_path / "p3_0.stl"; part.write_bytes(b"x")  # без .repair.stl

    async def fake_center(_func, *_a):
        return str(tmp_path / "centered.stl")            # _center_on_bed «успешно»
    monkeypatch.setattr(slicer.cpupool, "run", fake_center)

    calls: list[tuple[str, str]] = []

    async def run(parts, _outdir, arrange="1"):
        calls.append((Path(parts[0]).name, arrange))
        if arrange == "1":
            raise slicer.SlicerError("exit -50: no suitable objects")
        return 9.0, 90.0                                 # центрированная + --arrange 0 режется

    vol, mins = await slicer._slice_group([part], tmp_path / "out", run)
    assert (vol, mins) == (9.0, 90.0)
    assert calls[0] == ("p3_0.stl", "1")                 # сперва как есть, --arrange 1 → -50
    assert calls[1] == ("centered.stl", "0")             # затем по центру, --arrange 0 → ок


# ---- мультиплейт: сумма объёма/времени по столам (_slice_plates_sum) (3811f970 / a6452eaf) ----
# Каждый стол слайсится отдельно и суммируется — НЕ лумпится в один --arrange (баг Mark 85).

async def test_slice_plates_sum_adds_per_plate():
    calls: list[list[Path]] = []

    async def fake_run(parts, _outdir):
        calls.append(list(parts))
        return float(len(parts)), float(len(parts) * 10)   # объём=кол-во, время=кол-во×10

    groups = [[Path("p1a")], [Path("p2a"), Path("p2b")], [Path("p3a")]]   # 1+2+1 = 4 детали
    vol, mins = await slicer._slice_plates_sum(groups, Path("work"), fake_run)
    assert (vol, mins) == (4.0, 40.0)                  # сумма по столам
    assert len(calls) == 3                              # каждый стол — свой запуск


async def test_slice_plates_sum_skips_empty_plates():
    async def fake_run(parts, _outdir):
        return float(len(parts)), float(len(parts) * 10)

    groups = [[Path("a")], [], [Path("b")]]            # стол 2 пуст → пропускаем, не падаем
    vol, mins = await slicer._slice_plates_sum(groups, Path("work"), fake_run)
    assert (vol, mins) == (2.0, 20.0)


async def test_slice_plates_sum_all_empty_raises():
    async def fake_run(_parts, _outdir):
        return 1.0, 10.0

    with pytest.raises(slicer.SlicerError):
        await slicer._slice_plates_sum([[], []], Path("work"), fake_run)


async def test_slice_plates_sum_bisects_unsliceable_plate():
    # стол с «неукладываемой» комбинацией падает целиком → _slice_group делит его и досчитывает
    big = {Path("big1"), Path("big2")}

    async def fake_run(parts, _outdir):
        if big.issubset(set(parts)):
            raise slicer.SlicerError("exit -101")
        return float(len(parts)), float(len(parts) * 10)

    groups = [[Path("big1"), Path("big2"), Path("s1"), Path("s2")]]   # один стол, но не лёг целиком
    vol, mins = await slicer._slice_plates_sum(groups, Path("work"), fake_run)
    assert (vol, mins) == (4.0, 40.0)                  # стол досчитан через деление


async def test_slice_plates_sum_propagates_failed_plate(tmp_path):
    # один стол не режется (и нет ремонтной копии) → весь расчёт честно падает, НЕ занижается молча
    bad = tmp_path / "bad"; bad.write_bytes(b"x")

    async def run(parts, _outdir):
        if Path(parts[0]).name == "bad":
            raise slicer.SlicerError("exit -100")
        return 1.0, 10.0

    groups = [[tmp_path / "good"], [bad]]
    with pytest.raises(slicer.SlicerError):
        await slicer._slice_plates_sum(groups, tmp_path / "work", run)


def test_prepare_plates_single_input_non_3mf_returns_empty():
    # не .3mf → мультиплейт-детект отключён → [] (старый путь)
    assert slicer._prepare_plates([Path("model.stl")]) == []


def test_prepare_plates_multi_input_returns_empty(tmp_path):
    # несколько входов → мультиплейт-детект только для одиночного .3mf → []
    a = tmp_path / "a.3mf"
    b = tmp_path / "b.stl"
    a.write_bytes(b"PK")
    b.write_bytes(b"solid")
    assert slicer._prepare_plates([a, b]) == []


# ---- пер-филаментный объём из gcode (источник разбивки склада по цветам) ----
def test_parse_filament_cm3_list_per_extruder(tmp_path):
    """`filament used [cm3] = a, b, c` → [a, b, c] по экструдерам (последняя строка = итог)."""
    (tmp_path / "plate_1.gcode").write_text(
        "; filament used [cm3] = 1.0, 2.0\n; filament used [cm3] = 10.5, 20.5, 5.0\n",
        encoding="utf-8")
    assert slicer._parse_filament_cm3_list(tmp_path) == [10.5, 20.5, 5.0]


def test_parse_filament_cm3_list_sums_slots_across_plates(tmp_path):
    """Несколько плит → одноимённые слоты экструдеров суммируются поэлементно."""
    (tmp_path / "plate_1.gcode").write_text("; filament used [cm3] = 10, 20\n", encoding="utf-8")
    (tmp_path / "plate_2.gcode").write_text("; filament used [cm3] = 1, 2, 3\n", encoding="utf-8")
    assert slicer._parse_filament_cm3_list(tmp_path) == [11.0, 22.0, 3.0]


def test_parse_filament_cm3_list_empty_when_no_data(tmp_path):
    """Нет строк про филамент → пусто (вызывающий тогда не делит склад — фолбэк)."""
    (tmp_path / "plate_1.gcode").write_text("; nothing useful here\n", encoding="utf-8")
    assert slicer._parse_filament_cm3_list(tmp_path) == []
    assert slicer._parse_filament_cm3_list(tmp_path / "missing") == []


def test_parse_gcode_dir_time_fallback_without_normal_mode(tmp_path):
    """Время печати без «(normal mode)» (иной формат слайсера) тоже разбирается (фолбэк)."""
    (tmp_path / "plate_1.gcode").write_text(
        "; filament used [cm3] = 12.5\n; estimated printing time = 2h 0m 0s\n", encoding="utf-8")
    vol, mins = slicer._parse_gcode_dir(tmp_path)
    assert abs(vol - 12.5) < 1e-6 and abs(mins - 120) < 1e-6


def test_parse_gcode_dir_raises_without_summary(tmp_path):
    """gcode есть, но без сводки филамент/время → честная ошибка (а не молчаливый ноль)."""
    (tmp_path / "plate_1.gcode").write_text("; just a header\nG1 X0 Y0\n", encoding="utf-8")
    with pytest.raises(slicer.SlicerError):
        slicer._parse_gcode_dir(tmp_path)
