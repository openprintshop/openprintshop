"""Цикл 3, волна 4: «чья это печать». У этой волны СРОК ГОДНОСТИ.

Разметить историю «всё это Centauri» можно ровно до первой печати на второй машине — после
разделить будет нечем: ни журнал, ни телеметрия, ни заказы машину не записывали.

Отдельная ловушка, из-за которой волна и понадобилась: колонка `print_journal.printer_id`
существовала в схеме с прошлой миграции и НЕ ЗАПОЛНЯЛАСЬ НИ ОДНОЙ СТРОКОЙ — ни INSERT, ни
белый список правки её не знали, а бэкфилл тянул значение из таблицы, где оно тоже никогда не
писалось. Колонка выглядела готовой и была пуста; увидев её в схеме, легко решить, что
миграция уже сделана.
"""
import pytest

from app import db, printerdrv, printwatch


class _Bot:
    async def send_message(self, chat_id, text, **kw):
        return None


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(printwatch, "_notify_print_started", _noop)
    monkeypatch.setattr(printwatch, "_capture_bed_shot", _noop)
    printwatch._cur.clear()
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)


# ---------- колонки заполняются ----------

async def test_journal_records_which_printer(tmp_db, fake_printer):
    """Запись журнала обязана знать машину. Раньше поле было в схеме и всегда пустым."""
    printerdrv.use_driver(fake_printer, printer_id=1)
    fake_printer.printer = await db.get_printer(1)

    fake_printer.begin("plate.gcode", minutes=30, grams=12)
    await printwatch._tick_journal(_Bot())
    fake_printer.advance(30)
    await printwatch._tick_journal(_Bot())
    fake_printer.finish()
    await printwatch._tick_journal(_Bot())

    rows = await db.list_print_journal(limit=5)
    assert rows and rows[0]["printer_id"] == 1


async def test_journal_keeps_the_printer_that_actually_printed(tmp_db, fake_printer):
    """Машина печати — та, что печатала, а не «основная» на момент финиша.

    Чья это печать, запоминается ПОКА ОНА ИДЁТ: на финише телеметрии уже нет, а вместе с ней
    исчез бы и ответ."""
    printerdrv.use_driver(fake_printer, printer_id=1)
    fake_printer.printer = await db.get_printer(1)
    fake_printer.begin("plate.gcode", minutes=30, grams=12)
    await printwatch._tick_journal(_Bot())
    fake_printer.advance(30)
    await printwatch._tick_journal(_Bot())

    # владелец переключил основной принтер прямо во время печати
    second = await db.add_printer("Bambu Lab P1S", "m.json", "p", "f", is_active=1)
    await db.set_default_printer(second)

    fake_printer.finish()
    await printwatch._tick_journal(_Bot())
    rows = await db.list_print_journal(limit=5)
    assert rows and rows[0]["printer_id"] == 1, "печать приписана не той машине"


async def test_telemetry_records_which_printer(tmp_db, fake_printer):
    printerdrv.use_driver(fake_printer, printer_id=1)
    fake_printer.printer = await db.get_printer(1)
    fake_printer.begin("plate.gcode", minutes=10)
    await printwatch._tick_telemetry()
    rows = await db.list_printer_telemetry(since=0)
    assert rows and rows[-1]["printer_id"] == 1


async def test_no_card_means_no_guess(tmp_db, fake_printer):
    """У драйвера без карточки машины действительно нет — и выдумывать её нельзя.

    Иначе телеметрия одного принтера легла бы под номером другого."""
    printerdrv.use_driver(fake_printer)          # без привязки к карточке
    fake_printer.printer = None
    assert printwatch._printer_id(fake_printer) is None


async def test_order_learns_its_printer_when_printed(tmp_db):
    """Заказ узнаёт свою машину в момент печати, а не при оформлении.

    При оформлении машины ещё нет: клиент её не выбирает, мастерская решает у стола."""
    rid = await db.add_request(user_id=1, user_name="@t", model_label="M", printer="CC2",
                               plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                               grams_total=5, minutes_total=25, price=100, preview_path=None)
    await db._conn().execute("UPDATE requests SET printer_id = NULL WHERE id = ?", (rid,))
    await db._conn().commit()

    await db.add_print_journal(label="j.gcode", plastic_id=None, plastic_name="PLA",
                               grams=5, minutes=25, request_id=rid, printer_id=2)
    r = await db._conn().execute("SELECT printer_id FROM requests WHERE id = ?", (rid,))
    assert (await r.fetchone())["printer_id"] == 2


async def test_reprint_does_not_rewrite_the_first_machine(tmp_db):
    """Допечатка на другой машине не переписывает историю: заказ был сделан ТАМ."""
    rid = await db.add_request(user_id=1, user_name="@t", model_label="M", printer="CC2",
                               plastic_name="PLA", quantity=1, quality="0.2", strength="Ст",
                               grams_total=5, minutes_total=25, price=100, preview_path=None)
    await db._conn().execute("UPDATE requests SET printer_id = NULL WHERE id = ?", (rid,))
    await db._conn().commit()
    await db.add_print_journal(label="a.gcode", plastic_id=None, plastic_name="PLA",
                               grams=5, minutes=25, request_id=rid, printer_id=1)
    await db.add_print_journal(label="b.gcode", plastic_id=None, plastic_name="PLA",
                               grams=5, minutes=25, request_id=rid, printer_id=2)
    r = await db._conn().execute("SELECT printer_id FROM requests WHERE id = ?", (rid,))
    assert (await r.fetchone())["printer_id"] == 1


# ---------- разовая разметка истории ----------

async def test_backfill_marks_all_history_once(tmp_db):
    """Прошлая история размечается одним принтером и ровно один раз.

    Повторный прогон не смеет приписывать первому принтеру записи, которые к тому времени уже
    честно принадлежат второму, — поэтому флаг в settings, а не «где NULL»."""
    # запись «из прошлого»: машины не знает, как вся история до этой волны
    jid = await db.add_print_journal(label="старая.gcode", plastic_id=None, plastic_name="PLA",
                                     grams=10, minutes=30)
    assert (await db.get_print_journal(jid))["printer_id"] is None

    await _rerun_backfill()
    assert (await db.get_print_journal(jid))["printer_id"] == 1, "бэкфилл не разметил историю"

    # а теперь эта печать честно принадлежит второй машине — повтор не смеет её переписать
    await db._conn().execute("UPDATE print_journal SET printer_id = 2 WHERE id = ?", (jid,))
    await db._conn().commit()
    await db._backfill_printer_ownership(db._conn())          # флаг стоит → ничего не делаем
    assert (await db.get_print_journal(jid))["printer_id"] == 2, "повтор переписал чужую печать"


async def _rerun_backfill() -> None:
    """Снять флаг и прогнать разметку заново — имитация «мигрируем впервые»."""
    await db._conn().execute("DELETE FROM settings WHERE key='printer_ownership_backfilled'")
    await db._conn().commit()
    await db._backfill_printer_ownership(db._conn())


async def test_backfill_is_idempotent(tmp_db):
    """Второй вызов при уже стоящем флаге не делает ничего — флаг ставится миграцией."""
    cur = await db._conn().execute(
        "SELECT value FROM settings WHERE key='printer_ownership_backfilled'")
    assert (await cur.fetchone())["value"] == "1", "миграция не отметила разметку выполненной"
    await db._backfill_printer_ownership(db._conn())          # не должно бросить и ничего менять
