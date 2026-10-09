"""Температура CPU на экране TURZX — максимум по ядрам из дерева LibreHardwareMonitor.

Было (до 24.09.2026): один «приоритетный» датчик (`CPU Package` / `Core (Tctl/Tdie)`), иначе
первый попавшийся. Горячее ядро или кристалл прятались за пакетным датчиком, а «первым» мог
оказаться датчик материнки с «CPU» в имени. Теперь — максимум по датчикам самого камня.

Деревья ниже повторяют формат `data.json` LHM: корень `Sensor` → машина → железо → группы →
датчики (`SensorId`/`Type` у LHM ≥ 0.9; у старых сборок и OpenHardwareMonitor их нет).
"""
from app.web.screens import _lcd_find_cpu_temp


def _s(text, value, sid=""):
    node = {"Text": text, "Min": "", "Value": value, "Max": "", "Children": []}
    if sid:
        node.update(SensorId=sid, Type="Temperature")
    return node


def _g(text, *children):
    return {"Text": text, "Min": "", "Value": "", "Max": "", "Children": list(children)}


def _root(*hardware):
    # у корня LHM Value — буквально заголовок колонки «Value»; он не должен ничего ломать
    return {"Text": "Sensor", "Min": "Min", "Value": "Value", "Max": "Max",
            "Children": [_g("DESKTOP-KSA", *hardware)]}


_BOARD = _g("ASUS ROG STRIX X670E-E", _g("Nuvoton NCT6799D", _g(
    "Temperatures",
    _s("CPU", "88.0 °C", "/lpc/nct6799d/0/temperature/0"),
    _s("CPU Socket", "91.0 °C", "/lpc/nct6799d/0/temperature/1"))))
_GPU = _g("NVIDIA GeForce RTX 5070", _g(
    "Temperatures", _s("GPU Core", "95.0 °C", "/gpu-nvidia/0/temperature/0")))


def test_ryzen_7000_takes_hottest_ccd():
    """7950X: поядерных датчиков у Ryzen нет — самое горячее место из Tctl/Tdie и CCD."""
    cpu = _g("AMD Ryzen 9 7950X", _g(
        "Temperatures",
        _s("Core (Tctl/Tdie)", "61.4 °C", "/amdcpu/0/temperature/2"),
        _s("CCD1 (Tdie)", "58.9 °C", "/amdcpu/0/temperature/3"),
        _s("CCD2 (Tdie)", "73.1 °C", "/amdcpu/0/temperature/4"),
        _s("CCDs Average (Tdie)", "66.0 °C", "/amdcpu/0/temperature/6")),
        _g("Load", {**_s("CPU Total", "12.3 %"), "SensorId": "/amdcpu/0/load/0", "Type": "Load"}))
    # старая логика вернула бы 61.4 (приоритет Core (Tctl/Tdie)); датчики материнки и GPU
    # горячее, но это не камень
    assert _lcd_find_cpu_temp(_root(_BOARD, cpu, _GPU)) == 73.1


def test_intel_hottest_core_not_distance_to_tjmax():
    """Intel: максимум по ядрам; «Distance to TjMax» — запас до троттлинга, не температура."""
    cpu = _g("12th Gen Intel Core i7-12700K", _g(
        "Temperatures",
        _s("P-Core #1", "41.0 °C", "/intelcpu/0/temperature/0"),
        _s("P-Core #2", "55.0 °C", "/intelcpu/0/temperature/1"),
        _s("E-Core #9", "44.0 °C", "/intelcpu/0/temperature/8"),
        _s("CPU Package", "48.0 °C", "/intelcpu/0/temperature/12"),
        _s("P-Core #1 Distance to TjMax", "59.0 °C", "/intelcpu/0/temperature/13"),
        _s("Core Average", "46.7 °C", "/intelcpu/0/temperature/20")))
    assert _lcd_find_cpu_temp(_root(cpu, _BOARD)) == 55.0


def test_ryzen_tctl_offset_is_ignored_when_tdie_present():
    """2700X: Tctl = Tdie + 10° (смещение для кулеров) — показываем настоящий Tdie."""
    cpu = _g("AMD Ryzen 7 2700X", _g(
        "Temperatures",
        _s("Core (Tctl)", "70.0 °C", "/amdcpu/0/temperature/0"),
        _s("Core (Tdie)", "60.0 °C", "/amdcpu/0/temperature/1")))
    assert _lcd_find_cpu_temp(_root(cpu)) == 60.0


def test_legacy_tree_without_sensor_id():
    """Старый LHM/OHM без SensorId: CPU — по имени ЖЕЛЕЗА. Датчик «CPU» материнки в ветку CPU
    не попадает (раньше его имя само открывало ветку, и максимум стал бы 90°)."""
    cpu = _g("AMD Ryzen 7 5800X", _g(
        "Temperatures", _s("Core (Tctl/Tdie)", "65,5 °C"), _s("CCD1 (Tdie)", "63,0 °C")))
    board = _g("MSI MAG B550", _g("Nuvoton NCT6798D", _g("Temperatures", _s("CPU", "90.0 °C"))))
    assert _lcd_find_cpu_temp(_root(board, cpu)) == 65.5


def test_fahrenheit_is_converted():
    cpu = _g("AMD Ryzen 9 7950X", _g(
        "Temperatures", _s("Core (Tctl/Tdie)", "150.8 °F", "/amdcpu/0/temperature/2")))
    assert round(_lcd_find_cpu_temp(_root(cpu)), 1) == 66.0


def test_no_cpu_temperature_is_none():
    """Нет градусов у камня (LHM без прав администратора, виртуалка) → None, фронт покажет «—»."""
    cpu = _g("AMD Ryzen 9 7950X", _g("Load", _s("CPU Total", "12.3 %")))
    assert _lcd_find_cpu_temp(_root(cpu, _GPU)) is None
    assert _lcd_find_cpu_temp({}) is None
