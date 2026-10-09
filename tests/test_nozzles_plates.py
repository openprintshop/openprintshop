"""Сопла принтера (OpenTodo f71f4973) и пластины стола с Z-offset (OpenTodo 1028ab67).

Обе фичи защищают от ТИХИХ потерь:

* сопло — слайс пресетом сопла 0.6 по машинному профилю 0.4 не падает, он просто считает
  ширины линий и расход по чужому диаметру. Поэтому тесты фиксируют, что профиль выбирается
  ПО СОПЛУ пресета, а при отсутствии такого сопла честно откатывается к профилю принтера;
* Z-offset — единственное значение калибровки, которого НЕТ в пресетах слайсера (там он
  всегда 0): он живёт в принтере. Тесты фиксируют, что он сохраняется, наследуется паспортом
  от пластины и не путает «не мерил» с «ровно 0».
"""
import json

import pytest

from app import nozzles


# ── нормализация диаметра ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("0.6", "0.6"), ("0,6", "0.6"), ("0.60", "0.6"), (0.6, "0.6"), (" 0.4 ", "0.4"),
    ("0.40", "0.4"), (0.8, "0.8"), ("1", "1"),
    ("", ""), (None, ""), ("сопло", ""), ("-0.4", ""), ("0", ""), ("42", ""),
])
def test_norm(raw, expect):
    assert nozzles.norm(raw) == expect


@pytest.mark.parametrize("name,expect", [
    ("0.30mm Standard @Elegoo CC2 0.6 nozzle.json", "0.6"),
    ("0.20mm Standard @Elegoo CC2 0.4 nozzle.json", "0.4"),
    ("mb 0.30mm Standard @Elegoo CC2 0.6 nozzle", "0.6"),
    ("0.16mm Optimal @Elegoo CC2 0,6 nozzle.json", "0.6"),
    ("какой-то свой профиль.json", ""),
    ("", ""),
])
def test_from_process_name(name, expect):
    assert nozzles.from_process_name(name) == expect


def test_matches_treats_empty_as_default():
    """Записи до миграции сопла не несут — сравниваются как 0.4, иначе история разъехалась бы."""
    assert nozzles.matches("", "0.4")
    assert nozzles.matches("0.40", 0.4)
    assert not nozzles.matches("0.6", "0.4")


def test_from_machine_profile_reads_nozzle_diameter(tmp_path):
    p = tmp_path / "ECC2 0.6.json"
    p.write_text(json.dumps({"name": "Elegoo CC2 0.6", "nozzle_diameter": ["0.6"]}),
                 encoding="utf-8")
    assert nozzles.from_machine_profile(p) == "0.6"


def test_from_machine_profile_falls_back_to_name(tmp_path):
    """Профиль нечитаем — диаметр всё равно достаём из имени, а не молчим."""
    p = tmp_path / "Elegoo CC2 0.8 nozzle.json"
    p.write_text("{битый json", encoding="utf-8")
    assert nozzles.from_machine_profile(p) == "0.8"


# ── реестр сопел принтера ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_migration_gives_every_printer_a_nozzle(tmp_db):
    """После миграции у принтера есть запись сопла с его текущим machine-профилем."""
    printers = await tmp_db.list_printers()
    assert printers
    for p in printers:
        nz = await tmp_db.list_printer_nozzles(p.id)
        assert nz, f"у принтера {p.name} нет ни одного сопла"
        assert nz[0].machine_profile == p.machine_profile
        assert nz[0].is_default


@pytest.mark.asyncio
async def test_machine_profile_follows_quality_nozzle(tmp_db, tmp_path):
    """Сердце задачи: пресет сопла 0.6 режется профилем 0.6, а не профилем принтера."""
    printer = (await tmp_db.list_printers())[0]
    prof06 = tmp_path / "ECC2 0.6.json"
    prof06.write_text(json.dumps({"nozzle_diameter": ["0.6"]}), encoding="utf-8")
    await tmp_db.add_printer_nozzle(printer.id, "0.6", str(prof06))

    qid = await tmp_db.add_quality("Черновое — 0.30 мм", 0.30,
                                   "0.30mm Standard @Elegoo CC2 0.6 nozzle.json")
    q = await tmp_db.get_quality(qid)
    assert tmp_db.nozzle_of(q) == "0.6"
    assert await tmp_db.machine_profile_for(printer, tmp_db.nozzle_of(q)) == str(prof06)

    # Пресет 0.4 остаётся на профиле принтера — прежнее поведение не меняется.
    q04 = await tmp_db.get_quality(
        await tmp_db.add_quality("Стандарт", 0.20, "0.20mm Standard @Elegoo CC2 0.4 nozzle.json"))
    assert await tmp_db.machine_profile_for(printer, tmp_db.nozzle_of(q04)) == printer.machine_profile


@pytest.mark.asyncio
async def test_unknown_nozzle_falls_back_to_printer_profile(tmp_db):
    """Сопла 0.8 в реестре нет → считаем профилем принтера, а не падаем."""
    printer = (await tmp_db.list_printers())[0]
    assert await tmp_db.machine_profile_for(printer, "0.8") == printer.machine_profile


@pytest.mark.asyncio
async def test_default_nozzle_switches_printer_profile(tmp_db, tmp_path):
    """«Это сопло стоит сейчас» подменяет machine_profile принтера: старый код (стол,
    selfcheck, «Открыть в слайсере») читает именно его."""
    printer = (await tmp_db.list_printers())[0]
    prof06 = tmp_path / "ECC2 0.6.json"
    prof06.write_text(json.dumps({"nozzle_diameter": ["0.6"]}), encoding="utf-8")
    nid = await tmp_db.add_printer_nozzle(printer.id, "0.6", str(prof06))
    await tmp_db.set_default_printer_nozzle(nid)

    again = await tmp_db.get_printer(printer.id)
    assert again.machine_profile == str(prof06)
    nz = {n.nozzle: n.is_default for n in await tmp_db.list_printer_nozzles(printer.id)}
    assert nz["0.6"] == 1 and nz["0.4"] == 0


@pytest.mark.asyncio
async def test_default_nozzle_cannot_be_deleted(tmp_db):
    """Удалить основное сопло — значит оставить принтер без машинного профиля."""
    printer = (await tmp_db.list_printers())[0]
    n = (await tmp_db.list_printer_nozzles(printer.id))[0]
    with pytest.raises(ValueError):
        await tmp_db.delete_printer_nozzle(n.id)


@pytest.mark.asyncio
async def test_adding_same_nozzle_twice_is_an_edit(tmp_db, tmp_path):
    """Повторное добавление того же диаметра правит путь и НЕ снимает признак основного."""
    printer = (await tmp_db.list_printers())[0]
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    await tmp_db.add_printer_nozzle(printer.id, "0.4", str(other))
    nz = await tmp_db.list_printer_nozzles(printer.id)
    assert len(nz) == 1
    assert nz[0].machine_profile == str(other)
    assert nz[0].is_default


@pytest.mark.asyncio
async def test_known_nozzles_merges_registry_and_presets(tmp_db, tmp_path):
    prof06 = tmp_path / "ECC2 0.6.json"
    prof06.write_text("{}", encoding="utf-8")
    printer = (await tmp_db.list_printers())[0]
    await tmp_db.add_printer_nozzle(printer.id, "0.6", str(prof06))
    await tmp_db.add_quality("Крупное — 0.36 мм", 0.36,
                             "0.36mm Extra Draft @Elegoo CC2 0.8 nozzle.json")
    known = await tmp_db.known_nozzles()
    assert known == sorted(known, key=float)
    assert {"0.4", "0.6", "0.8"} <= set(known)


# ── пластины стола и Z-offset ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_plate_roundtrip(tmp_db):
    pid = await tmp_db.save_calib_plate(name="Текстурированная PEI", kind="textured",
                                        z_offset=-0.04, note="родная")
    plate = await tmp_db.get_calib_plate(pid)
    assert plate["name"] == "Текстурированная PEI"
    assert plate["z_offset"] == -0.04
    assert plate["is_default"] == 1        # первая пластина = та, что на столе


@pytest.mark.asyncio
async def test_plate_name_is_the_key(tmp_db):
    """Пластина с тем же именем — правка, а не дубль: иначе список зарос бы копиями."""
    first = await tmp_db.save_calib_plate(name="Гладкая PEI", z_offset=-0.02)
    again = await tmp_db.save_calib_plate(name="Гладкая PEI", z_offset=-0.05)
    assert first == again
    assert len(await tmp_db.list_calib_plates()) == 1
    assert (await tmp_db.get_calib_plate(first))["z_offset"] == -0.05


@pytest.mark.asyncio
async def test_plate_requires_name(tmp_db):
    with pytest.raises(ValueError):
        await tmp_db.save_calib_plate(name="   ")


@pytest.mark.asyncio
async def test_unmeasured_offset_is_not_zero(tmp_db):
    """«Ещё не мерил» и «ровно 0» — разные состояния: ноль означал бы откалиброванный ноль."""
    pid = await tmp_db.save_calib_plate(name="Новая пластина")
    assert (await tmp_db.get_calib_plate(pid))["z_offset"] is None


@pytest.mark.asyncio
async def test_passport_keeps_plate_and_own_offset(tmp_db):
    """Паспорт помнит пластину и СВОЙ вылет (у PETG на той же плите он другой)."""
    plate = await tmp_db.save_calib_plate(name="Текстурированная PEI", z_offset=-0.04)
    plastic = (await tmp_db.list_plastics(active_only=False))[0]
    await tmp_db.save_calib_passport(plastic_id=plastic.id, printer_id=None, nozzle="0.6",
                                     values={"flow": 1.04}, plate_id=plate, z_offset=-0.07)
    p = (await tmp_db.list_calib_passports())[0]
    assert p["plate"] == "Текстурированная PEI"
    assert p["plate_z"] == -0.04          # базовый вылет пластины
    assert p["z_offset"] == -0.07         # уточнение этого филамента


@pytest.mark.asyncio
async def test_passport_without_own_offset_inherits_plate(tmp_db):
    plate = await tmp_db.save_calib_plate(name="Холодная", z_offset=-0.01)
    plastic = (await tmp_db.list_plastics(active_only=False))[0]
    await tmp_db.save_calib_passport(plastic_id=plastic.id, printer_id=None, nozzle="0.4",
                                     plate_id=plate)
    p = (await tmp_db.list_calib_passports())[0]
    assert p["z_offset"] is None and p["plate_z"] == -0.01


@pytest.mark.asyncio
async def test_deleting_plate_keeps_passport(tmp_db):
    """Пластину выбросили — паспорт остаётся, просто без пластины."""
    plate = await tmp_db.save_calib_plate(name="Убитая PEI", z_offset=-0.03)
    plastic = (await tmp_db.list_plastics(active_only=False))[0]
    await tmp_db.save_calib_passport(plastic_id=plastic.id, printer_id=None, nozzle="0.4",
                                     plate_id=plate)
    await tmp_db.delete_calib_plate(plate)
    p = (await tmp_db.list_calib_passports())[0]
    assert p["plate_id"] is None and p["plate"] is None
