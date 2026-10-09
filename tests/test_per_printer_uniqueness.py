"""Цикл 3, волна 5: одинаковые имена файлов на двух машинах.

`plate_1.gcode` есть у обеих машин — это не редкость, а норма: слайсер называет пластины
одинаково. Пока уникальность была «имя файла на всю студию», вторая машина молча перезаписывала
ответ на вопрос «чем это резали», а её таймлапс — видео первой. Оба ответа единственные в своём
роде: снимок настроек кормит калибровку и разбор брака, ролик уходит клиенту в результат заказа.

Здесь проверяется, что ключом стала ПАРА (машина, имя) — и что записи, сделанные до этой волны
(машина неизвестна), продолжают находиться.
"""
import pytest

from app import db, printmedia


async def _snap(name: str, printer_id=None, slicer="ElegooSlicer") -> int:
    return await db.save_print_settings(name, values={"slicer": slicer}, slicer_name=slicer,
                                        printer_id=printer_id)


# ---------- снимок «чем резали» ----------

async def test_same_filename_on_two_printers_keeps_both(tmp_db):
    """Две машины, один и тот же `plate_1.gcode` — два РАЗНЫХ снимка."""
    a = await _snap("plate_1.gcode", printer_id=1, slicer="ElegooSlicer")
    b = await _snap("plate_1.gcode", printer_id=2, slicer="BambuStudio")
    assert a != b, "снимок второй машины перезаписал первый"

    assert (await db.find_print_settings("plate_1.gcode", printer_id=1))["slicer"] == "ElegooSlicer"
    assert (await db.find_print_settings("plate_1.gcode", printer_id=2))["slicer"] == "BambuStudio"


async def test_second_save_updates_its_own_snapshot(tmp_db):
    """Повторная заливка на ту же машину дополняет свой снимок, а не плодит новый."""
    a = await _snap("plate_1.gcode", printer_id=1)
    again = await db.save_print_settings("plate_1.gcode", grams=42.0, printer_id=1)
    assert again == a
    assert (await db.find_print_settings("plate_1.gcode", printer_id=1))["grams"] == 42.0


async def test_old_snapshots_without_printer_are_still_found(tmp_db):
    """Записи до волны 5 машины не знают — и терять их нельзя.

    Ищем сначала у своей машины, не нашли — берём общую. Иначе весь накопленный ответ «чем
    резали» пропал бы в день миграции."""
    await _snap("legacy.gcode", printer_id=None, slicer="OrcaSlicer")
    found = await db.find_print_settings("legacy.gcode", printer_id=2)
    assert found and found["slicer"] == "OrcaSlicer"


async def test_own_snapshot_wins_over_the_shared_one(tmp_db):
    """Если есть и свой, и общий — отдаём СВОЙ: чужие настройки в разборе брака хуже, чем ничего."""
    await _snap("mix.gcode", printer_id=None, slicer="Общий")
    await _snap("mix.gcode", printer_id=2, slicer="Свой")
    assert (await db.find_print_settings("mix.gcode", printer_id=2))["slicer"] == "Свой"


async def test_unique_index_is_now_a_pair(tmp_db):
    """Прибито схемой: старый индекс по одному имени снят, новый — по паре."""
    cur = await db._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='print_settings'")
    names = {r["name"] for r in await cur.fetchall()}
    assert "idx_print_settings_name" not in names, "вернулась уникальность по одному имени"
    assert "idx_print_settings_printer_name" in names


# ---------- авто-кэш таймлапсов ----------

def test_timelapse_cache_separates_printers_by_folder():
    """Две машины с одинаковым именем задания не перезаписывают видео друг друга.

    Именно подпапка, а не префикс в имени: префикс вида `p1_` столкнулся бы с настоящим
    заданием, которое так и называется (поймано этим же тестом при первой реализации)."""
    a = printmedia._timelapse_dir(1)
    b = printmedia._timelapse_dir(2)
    assert a != b and a.name == "p1" and b.name == "p2"
    assert printmedia._safe_job_name("plate_1.gcode") == "plate_1"   # имя не изменилось


def test_timelapse_dir_unchanged_when_printer_unknown():
    """Без машины путь прежний — иначе все ролики, снятые до правки, перестали бы находиться."""
    assert printmedia._timelapse_dir() == printmedia._TIMELAPSE_AUTO_DIR
    assert printmedia._timelapse_dir(None) == printmedia._TIMELAPSE_AUTO_DIR


@pytest.mark.parametrize("pid", [None, 1])
def test_cached_timelapse_falls_back_to_shared(tmp_path, monkeypatch, pid):
    """Ролик, скачанный до волны 5 (без машины), продолжает находиться и после неё."""
    monkeypatch.setattr(printmedia, "_TIMELAPSE_AUTO_DIR", tmp_path)
    shared = tmp_path / f"{printmedia._safe_job_name('plate_1.gcode')}.mp4"
    shared.write_bytes(b"video")
    assert printmedia.cached_timelapse_path("plate_1.gcode", pid) == shared


def test_own_timelapse_wins_over_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(printmedia, "_TIMELAPSE_AUTO_DIR", tmp_path)
    (tmp_path / f"{printmedia._safe_job_name('plate_1.gcode')}.mp4").write_bytes(b"old")
    own = tmp_path / "p2"
    own.mkdir()
    mine = own / f"{printmedia._safe_job_name('plate_1.gcode')}.mp4"
    mine.write_bytes(b"mine")
    assert printmedia.cached_timelapse_path("plate_1.gcode", 2) == mine


# ---------- наработка ТО: своя у каждой машины ----------

async def test_service_counters_are_per_printer(tmp_db):
    """Обслуживание одной машины не смеет обнулять счётчик другой.

    Раньше `key` был уникален на всю студию: две записи «смазка направляющих» просто не могли
    существовать, и вторая машина тихо ездила бы на просроченном ТО."""
    second = await db.add_printer("Bambu Lab P1S", "m.json", "p", "f", is_active=1)

    await db.accrue_service(600.0, 5, printer_id=1)          # 10 часов на первой
    await db.accrue_service(60.0, 1, printer_id=second)      # 1 час на второй

    first_items = await db.list_service_items(printer_id=1)
    second_items = await db.list_service_items(printer_id=second)
    assert first_items and second_items
    assert {i.key for i in first_items} == {i.key for i in second_items}, "наборы узлов совпадают"
    assert first_items[0].hours_since == 10.0
    assert second_items[0].hours_since == 1.0

    # обслужили узел на ВТОРОЙ машине — первая не должна этого заметить
    await db.mark_serviced(second_items[0].id, note="тест")
    assert (await db.get_service_item(second_items[0].id)).hours_since == 0.0
    assert (await db.get_service_item(first_items[0].id)).hours_since == 10.0


async def test_new_printer_gets_its_own_service_items(tmp_db):
    """У только что заведённой машины пунктов ТО ещё нет — они заводятся при первом начислении."""
    second = await db.add_printer("Bambu Lab P1S", "m.json", "p", "f", is_active=1)
    assert await db.list_service_items(printer_id=second) == []
    await db.accrue_service(30.0, 1, printer_id=second)
    assert await db.list_service_items(printer_id=second), "пункты ТО второй машины не завелись"


async def test_accrue_defaults_to_the_main_printer(tmp_db):
    """Аргумент со значением по умолчанию — намеренно.

    У функции две точки вызова, обе в молчаливом except: обязательный аргумент означал бы, что
    забытый вызов даёт TypeError, которого никто не увидит, и начисление ТО просто перестало бы
    происходить."""
    await db.accrue_service(120.0, 2)                        # без printer_id
    items = await db.list_service_items(printer_id=1)
    assert items and items[0].hours_since == 2.0


def test_accrue_signature_keeps_a_default():
    """Регресс на ту же мысль: если однажды сделают аргумент обязательным — тест упадёт."""
    import inspect
    p = inspect.signature(db.accrue_service).parameters["printer_id"]
    assert p.default is None, "printer_id стал обязательным — забытый вызов умрёт молча"
