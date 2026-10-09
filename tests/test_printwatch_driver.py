"""Цикл 3, волна 2: автоматика мастерской ведётся ДРАЙВЕРОМ, а не прямыми вызовами Elegoo.

Чем этот файл отличается от tests/test_printflow.py. Там печать изображается подменами:
`elegoo.cached_status` возвращает нарисованный словарь, а `_active_and_progress` — заданный
кортеж. Такой тест проверяет подменённое, а не работающее: пока printwatch ходил в elegoo
напрямую, ни одна мультипринтерная правка им не ловилась, а Klipper-карточка не получала
журнала НИКОГДА — и тесты этого не видели.

Здесь печать ведёт симулятор: `begin → advance → finish`, и весь путь (статус → признак
печати → журнал) идёт через настоящий драйвер. Ни одной подмены внутренностей printwatch.
"""
import pytest

from app import db, printerdrv, printwatch
from app.printerdrv import Caps


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """Гасим внешние эффекты финиша: уведомления, скрин стола, снимок настроек."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(printwatch, "_notify_print_started", _noop)
    monkeypatch.setattr(printwatch, "_capture_bed_shot", _noop)
    printwatch._cur.clear()


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


async def _run_print(job: str, minutes: float, printer) -> None:
    """Полный прогон печати через тики printwatch — как в жизни, но время идёт по команде."""
    printer.begin(job, minutes=minutes, grams=12.0)
    await printwatch._tick_journal(_Bot())          # печать началась
    printer.advance(minutes)
    await printwatch._tick_journal(_Bot())          # печать идёт
    printer.finish()
    await printwatch._tick_journal(_Bot())          # финиш — запись в журнал


async def test_journal_is_driven_by_the_driver(tmp_db, fake_printer, monkeypatch):
    """Сквозной путь: симулятор напечатал — в журнале появилась запись.

    Раньше этот путь на симуляторе не работал вовсе: его состояние не содержало полей, по
    которым код решает, что печать активна, а история отдавалась под чужим ключом."""
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)  # кэша нет
    await _run_print("ECC2_0.4_Кот_PLA_0.2.gcode", 30.0, fake_printer)

    rows = await db.list_print_journal(limit=10)
    assert rows, "журнал пуст — автоматика не увидела печать симулятора"
    assert rows[0]["label"].startswith("ECC2")
    assert float(rows[0]["minutes"]) >= 29.0


async def test_pause_is_not_a_finish(tmp_db, fake_printer, monkeypatch):
    """Пауза не должна выглядеть как «печать закончилась» и заводить запись в журнал.

    Это и есть причина, по которой active включает паузу: иначе мастер, поставивший печать на
    паузу пообедать, получал бы опрос «занести в журнал?» и запись на половину печати."""
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)
    fake_printer.begin("plate.gcode", minutes=40, grams=10)
    await printwatch._tick_journal(_Bot())
    fake_printer.advance(20)
    await printwatch._tick_journal(_Bot())
    fake_printer.pause()
    await printwatch._tick_journal(_Bot())

    assert await db.list_print_journal(limit=10) == [], "пауза записалась как законченная печать"

    fake_printer.resume()
    fake_printer.advance(20)
    await printwatch._tick_journal(_Bot())   # тик В ПЕЧАТИ: цикл копит длительность на ходу
    fake_printer.finish()
    await printwatch._tick_journal(_Bot())
    rows = await db.list_print_journal(limit=10)
    assert len(rows) == 1 and float(rows[0]["minutes"]) >= 39.0


async def test_printer_without_telemetry_does_not_break_the_loop(tmp_db, monkeypatch):
    """Бедный принтер (нет телеметрии) — не повод падать: тик просто ничего не делает."""
    poor = printerdrv.FakePrinter(caps=Caps(upload=True, start=True))
    printerdrv.use_driver(poor)
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)
    await printwatch._tick_journal(_Bot())
    await printwatch._tick_telemetry()
    await printwatch._tick_offline(_Bot())
    assert await db.list_print_journal(limit=10) == []


async def test_falls_back_to_subscriber_cache_and_says_so(tmp_db, monkeypatch, caplog):
    """Основной карточкой пометили принтер без пульта — журнал обязан продолжить работать.

    Без фолбэка это был бы новый способ молча всё потерять: драйвер отдаёт None, и вместе с ним
    замолкают журнал, привязка к заказу, скрин стола, свип и автофиниш. Пока журнал не знает,
    чья это печать (колонка появится волной 4), честнее вести его по телеметрии живого принтера
    — но СКАЗАТЬ об этом вслух."""
    printerdrv.use_driver(printerdrv.NoneDriver())
    st = {"print_status": {"filename": "dragon.gcode", "print_duration": 600,
                           "remaining_time_sec": 600}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": st})
    printwatch._fallback_warned.clear()

    with caplog.at_level("WARNING"):
        cs, drv = await printwatch._status()
    assert cs and cs.get("ok"), "фолбэк не сработал — автоматика замолчала бы"
    assert any("основной" in r.message.lower() or "основног" in r.message.lower()
               for r in caplog.records), "фолбэк сработал МОЛЧА — так и было до правки"


async def test_two_cards_two_printers(tmp_db, fake_printers, monkeypatch):
    """Две карточки — два РАЗНЫХ принтера: печать первой не видна как печать второй.

    Мастерская с одним принтером скрывает целый класс ошибок: код, который берёт «принтер»
    вместо «принтер этой карточки», на одном принтере работает всегда."""
    a, b = fake_printers[1], fake_printers[2]
    a.begin("first.gcode", minutes=10)
    da = printerdrv.for_printer(await db.get_printer(1))
    assert da.job_state(da.status()["state"])["active"] is True
    assert da.job_state(da.status()["state"])["name"] == "first.gcode"
    db_ = printerdrv.for_printer(type("P", (), {"id": 2, "kind": "fake"})())
    assert db_.job_state(db_.status()["state"])["active"] is False
    assert b.st.printing is False
