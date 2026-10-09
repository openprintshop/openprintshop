"""Волна 0 цикла 3: что ломалось в момент, когда карточку второго принтера просто заведут.

Разведка 11.08 нашла мину, которая срабатывает ДО всякого драйвера и без единой ошибки в
логе. Мастер нажимает «Добавить принтер» — карточка создавалась сразу активной, а протокол
связи и адрес выставляются отдельным действием. В ту же секунду:

  • бот начинал показывать КЛИЕНТУ шаг «на каком принтере печатаем?» с названиями машин на
    кнопках — потому что условием было просто «активных карточек больше одной»;
  • selfcheck начинал жаловаться при каждом старте на отсутствующие слайс-профили;
  • «первый принтер» расходился: список сортировался по ИМЕНИ, а get_default_printer по ID.

Здесь проверяется, что каждая из мин обезврежена.
"""
import pytest

from app import db
from app.handlers import user_flow


async def _mk(name: str, **kw) -> int:
    return await db.add_printer(name, "m.json", "proc", "fil", **kw)


async def _clear_default() -> None:
    """Снять пометку «основной» со всех карточек.

    Достижимое состояние, а не искусственное: флаг ставит единственная функция
    set_default_printer, и в белый список update_printer он не входит."""
    await db._conn().execute("UPDATE printers SET is_default = 0")
    await db._conn().commit()


# В tmp_db уже засеяна боевая карточка CC2 (id=1, активна, основная) — как в проде.

# ---------- 1. карточка заводится выключенной ----------

async def test_new_printer_card_is_inactive(tmp_db):
    """Между «завёл» и «настроил» карточка не должна быть боевой."""
    before = {p.id for p in await db.list_printers(active_only=True)}
    pid = await _mk("Bambu Lab P1S")
    assert (await db.get_printer(pid)).is_active == 0, "новая карточка активна — мина вернулась"
    assert {p.id for p in await db.list_printers(active_only=True)} == before


async def test_explicit_activation_still_possible(tmp_db):
    """Первый принтер студии (сид/миграция) заводится активным явным флагом."""
    pid = await _mk("Bambu Lab P1S", is_active=1)
    assert (await db.get_printer(pid)).is_active == 1
    assert pid in {p.id for p in await db.list_printers(active_only=True)}


# ---------- 2. клиента не спрашивают про принтер без явного решения мастера ----------

async def test_client_is_not_asked_by_default(tmp_db):
    """Две активные карточки САМИ ПО СЕБЕ не включают клиенту выбор машины.

    Это и была продуктовая регрессия: покупателю показывали «Elegoo Centauri Carbon 2» и
    «Bambu Lab P1S» кнопками, хотя выбирать машину — дело мастерской."""
    await _mk("Bambu Lab P1S", is_active=1)
    printers = await db.list_printers(active_only=True)
    assert len(printers) >= 2
    assert await user_flow._ask_printer(printers) is False


async def test_client_is_asked_when_master_turned_it_on(tmp_db):
    """Мастер может включить выбор осознанно — тогда шаг появляется."""
    await _mk("Bambu Lab P1S", is_active=1)
    await db.set_setting("printer_choice_enabled", 1)
    printers = await db.list_printers(active_only=True)
    assert await user_flow._ask_printer(printers) is True


async def test_single_printer_never_asks(tmp_db):
    """Одна машина — спрашивать нечего, даже если переключатель включён."""
    await db.set_setting("printer_choice_enabled", 1)
    printers = await db.list_printers(active_only=True)
    assert len(printers) == 1, "в засеянной базе ожидается ровно одна активная карточка"
    assert await user_flow._ask_printer(printers) is False


async def test_setting_survives_float_storage(tmp_db):
    """Известная ловушка проекта: get_settings отдаёт ВСЁ как float, поэтому '0' приезжает
    как 0.0. Проверяем, что выключенное действительно выключено."""
    await _mk("Bambu Lab P1S", is_active=1)
    await db.set_setting("printer_choice_enabled", 0)
    s = await db.get_settings()
    assert s["printer_choice_enabled"] == 0.0
    assert await user_flow._ask_printer(await db.list_printers(active_only=True)) is False


# ---------- 3. «первый принтер» один и тот же во всех путях ----------

@pytest.mark.parametrize("name", ["Bambu Lab P1S", "AAA Первый по алфавиту"])
async def test_first_printer_is_the_same_everywhere(tmp_db, name):
    """Список и get_default_printer обязаны сходиться, даже когда основной не помечен.

    Раньше список сортировался по ИМЕНИ, а default — по ID. При снятом is_default мини-аппа
    брала первого по алфавиту (расчёт клиенту уезжал на чужой профиль и стол), а автоматика и
    пульт — первого по id. Расхождение молчаливое. Имя во втором случае специально такое,
    чтобы новая карточка обгоняла засеянную по алфавиту, но не по id."""
    await _mk(name, is_active=1)
    await _clear_default()   # достижимое состояние: флаг ставит только set_default_printer
    lst = await db.list_printers(active_only=True)
    default = await db.get_default_printer()
    assert lst[0].id == default.id, "первый принтер разъехался между путями"


async def test_default_flag_wins_over_order(tmp_db):
    """Помеченный основным идёт первым в обоих путях, независимо от id и имени."""
    second = await _mk("Bambu Lab P1S", is_active=1)
    await db.set_default_printer(second)
    assert (await db.list_printers(active_only=True))[0].id == second
    assert (await db.get_default_printer()).id == second
