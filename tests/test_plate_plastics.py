"""Свой пластик на каждый стол (OpenTodo 70525c99): группировка столов, раздельный слайс
своим филамент-профилем, средневзвешенная цена по граммам групп."""
from types import SimpleNamespace

from app import calcservice, db, slicer
from app.calcservice import _plastic_groups


def _plastic(pid, name, price_kg, density, mat="PLA"):
    return db.Plastic(id=pid, name=name, price_per_kg=price_kg, density=density,
                      is_active=1, material_type=mat, colors="Белый,Чёрный")


PLA = _plastic(1, "Test PLA", 1000.0, 1.24, "PLA")
PETG = _plastic(2, "Test PETG", 2000.0, 1.27, "PETG")


def test_plastic_groups_grouping():
    # столы 0,2 — основной PLA; стол 1 — PETG
    g = _plastic_groups(PLA, {1: PETG}, 3, None)
    assert [(p.id, idxs) for p, idxs in g] == [(1, [0, 2]), (2, [1])]
    # выбранное подмножество столов уважается
    g = _plastic_groups(PLA, {1: PETG}, 3, [1, 2])
    assert [(p.id, idxs) for p, idxs in g] == [(2, [1]), (1, [2])]
    # все столы на одном (не основном) пластике → одна группа PETG
    g = _plastic_groups(PLA, {0: PETG, 1: PETG}, 2, None)
    assert len(g) == 1 and g[0][0].id == 2 and g[0][1] == [0, 1]
    # без пер-стольных назначений → одна группа основного
    g = _plastic_groups(PLA, None, 4, None)
    assert len(g) == 1 and g[0][0].id == 1 and g[0][1] == [0, 1, 2, 3]


async def test_compute_mixed_plate_plastics(tmp_db, tmp_path, monkeypatch):
    """Смешанные пластики: слайс на группу своим профилем, цена = Σ грамм×цена (взвешенно)."""
    await tmp_db.set_setting("slicing_enabled", 1)   # тест именно слайс-пути (17.07 дефолт 0)
    model = tmp_path / "m.3mf"
    model.write_bytes(b"fake")
    calls = []

    async def fake_slice_unit(mp, *, filament_profile, plates_only=None, **kw):
        calls.append((filament_profile, plates_only))
        # PLA-группа (2 стола): 10 cm3 / 60 мин; PETG-группа (1 стол): 5 cm3 / 30 мин
        if plates_only == [1]:
            return slicer.SliceResult(volume_cm3=5.0, minutes=30.0, cached=False)
        return slicer.SliceResult(volume_cm3=10.0, minutes=60.0, cached=False)

    monkeypatch.setattr(calcservice.slicer, "slice_unit", fake_slice_unit)
    monkeypatch.setattr(calcservice.materials, "resolve_filament",
                        lambda _dir, mat, fallback="", prefer="": f"fil-{mat}")

    async def fake_run_render(fn, *a, **kw):
        raise RuntimeError("no render in tests")

    async def fake_run(fn, *a, **kw):
        return "", 0.0   # model_dims

    monkeypatch.setattr(calcservice.cpupool, "run_render", fake_run_render)
    monkeypatch.setattr(calcservice.cpupool, "run", fake_run)

    printer = SimpleNamespace(id=1, name="P", filament_dir="fd", machine_profile="mp",
                              process_dir="pd")
    quality = SimpleNamespace(id=1, label="q", process="proc", layer_height=0.2)
    strength = SimpleNamespace(id=1, label="s", infill=15, walls=3)

    res = await calcservice.compute(
        user_id=1, model_paths=[str(model)], model_label="M", printer=printer,
        plastic=PLA, color="Белый", qty=1, quality=quality, strength=strength,
        plates=3, save=False, plate_plastics={1: PETG})

    # две группы → два слайса разными профилями и разными подмножествами столов
    assert sorted(calls) == [("fil-PETG", [1]), ("fil-PLA", [0, 2])]
    bd = res["bd"]
    g_pla, g_petg = 10.0 * PLA.density, 5.0 * PETG.density
    assert abs(bd.grams_total - (g_pla + g_petg)) < 1e-6
    assert abs(bd.minutes_total - 90.0) < 1e-6
    # филамент в цене = Σ грамм × своя цена (средневзвешенная цена/кг даёт ровно это)
    expected_filament = g_pla * PLA.price_per_gram + g_petg * PETG.price_per_gram
    assert abs(bd.filament_cost - expected_filament) < 1e-6
    # подпись материала честно перечисляет оба пластика
    assert "Test PLA" in res["pdisp"] and "Test PETG" in res["pdisp"]
    # разбивка по пластикам для склада
    pg = {g["plastic_id"]: g for g in res["plastic_grams"]}
    assert abs(pg[1]["grams_unit"] - g_pla) < 1e-6 and pg[1]["plates"] == [0, 2]
    assert abs(pg[2]["grams_unit"] - g_petg) < 1e-6 and pg[2]["plates"] == [1]


async def test_compute_single_custom_plastic_uses_it_everywhere(tmp_db, tmp_path, monkeypatch):
    """Все столы на одном НЕ основном пластике → один слайс его профилем и его цена."""
    await tmp_db.set_setting("slicing_enabled", 1)   # тест именно слайс-пути
    model = tmp_path / "m.3mf"
    model.write_bytes(b"fake")
    calls = []

    async def fake_slice_unit(mp, *, filament_profile, plates_only=None, **kw):
        calls.append((filament_profile, plates_only))
        return slicer.SliceResult(volume_cm3=10.0, minutes=60.0, cached=False)

    monkeypatch.setattr(calcservice.slicer, "slice_unit", fake_slice_unit)
    monkeypatch.setattr(calcservice.materials, "resolve_filament",
                        lambda _dir, mat, fallback="", prefer="": f"fil-{mat}")

    async def fake_run_render(fn, *a, **kw):
        raise RuntimeError("no render in tests")

    async def fake_run(fn, *a, **kw):
        return "", 0.0

    monkeypatch.setattr(calcservice.cpupool, "run_render", fake_run_render)
    monkeypatch.setattr(calcservice.cpupool, "run", fake_run)

    printer = SimpleNamespace(id=1, name="P", filament_dir="fd", machine_profile="mp",
                              process_dir="pd")
    quality = SimpleNamespace(id=1, label="q", process="proc", layer_height=0.2)
    strength = SimpleNamespace(id=1, label="s", infill=15, walls=3)

    res = await calcservice.compute(
        user_id=1, model_paths=[str(model)], model_label="M", printer=printer,
        plastic=PLA, color="", qty=1, quality=quality, strength=strength,
        plates=2, save=False, plate_plastics={0: PETG, 1: PETG})

    assert calls == [("fil-PETG", None)]            # один слайс, профиль PETG, без подмножества
    bd = res["bd"]
    assert abs(bd.grams_total - 10.0 * PETG.density) < 1e-6
    expected_filament = 10.0 * PETG.density * PETG.price_per_gram
    assert abs(bd.filament_cost - expected_filament) < 1e-6
    assert res["pdisp"] == "Test PETG"              # подпись материала — фактический пластик
    assert res["plastic_grams"] == []               # не mixed → разбивка не нужна
