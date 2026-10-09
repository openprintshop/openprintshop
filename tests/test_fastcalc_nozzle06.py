"""Модель времени fastcalc знает про сопло 0.6 (перефит 21.08.2026 по боевым моделям).

Старые константы 0.4 завышали время крупных моделей на 0.30/0.36-пресетах на 100–180%
(медианная ошибка 62% против слайсера); свои константы 0.6 дают 25%. Выбор — по полю
nozzle настроек: его несёт пресет качества (f71f4973), авторские настройки проект-3mf
сопла не знают и честно считаются константами 0.4.
"""
from app import fastcalc


def test_time_consts_selection():
    assert fastcalc._time_consts({"nozzle": "0.6"}) == (
        fastcalc.MVS_WALL_06_MM3_S, fastcalc.MVS_BULK_06_MM3_S, fastcalc.SEC_PER_LAYER_06)
    for s in ({}, {"nozzle": ""}, {"nozzle": "0.4"}):
        assert fastcalc._time_consts(s) == (
            fastcalc.MVS_WALL_MM3_S, fastcalc.MVS_BULK_MM3_S, fastcalc.SEC_PER_LAYER)


def test_06_is_faster_on_bulk():
    """Физика перефита: у 0.6 эффективные объёмные скорости выше — то же тело печатается
    быстрее, чем считали константы 0.4 (именно это и было жалобой: время завышено)."""
    fil = {"shell": 3000.0, "infill": 9000.0, "support": 0.0, "flush": 0.0}
    meas = {"part_heights": [30.0], "h_mm": 30.0}
    t04 = fastcalc.print_minutes(fil, meas, {"layer_height": 0.3}, 1.0)
    t06 = fastcalc.print_minutes(fil, meas, {"layer_height": 0.3, "nozzle": "0.6"}, 1.0)
    assert t06 < t04


def test_process_settings_carry_nozzle(tmp_path):
    """Сопло берётся из ПРЕСЕТА и переживает мемо-кэш профиля (второй вызов с другим
    пресетом на тот же файл обязан отдать ЕГО сопло, а не запомненное)."""
    class _P:
        process_dir = str(tmp_path)   # файла нет — ветка раннего возврата

    class _Q04:
        layer_height, process, nozzle = 0.2, "x.json", "0.4"

    class _Q06:
        layer_height, process, nozzle = 0.3, "x.json", "0.6"

    assert fastcalc._process_print_settings(_Q04(), _P())["nozzle"] == "0.4"
    assert fastcalc._process_print_settings(_Q06(), _P())["nozzle"] == "0.6"


def test_real_06_process_settings(tmp_path):
    """На настоящем профиле 0.30@0.6 (если слайсер установлен): и сопло, и ширины 0.62."""
    import pathlib
    proc_dir = pathlib.Path(r"C:\Program Files\ElegooSlicer\resources\profiles\Elegoo\process\ECC2")
    if not (proc_dir / "0.30mm Standard @Elegoo CC2 0.6 nozzle.json").exists():
        import pytest
        pytest.skip("профилей ElegooSlicer на машине нет")

    class _P:
        process_dir = str(proc_dir)

    class _Q:
        layer_height, nozzle = 0.3, "0.6"
        process = "0.30mm Standard @Elegoo CC2 0.6 nozzle.json"

    s = fastcalc._process_print_settings(_Q(), _P())
    assert s["nozzle"] == "0.6" and abs(s["w_outer"] - 0.62) < 0.05
