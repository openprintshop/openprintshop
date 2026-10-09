"""Финиш печати не теряется при сбое БД (04.10.2026, разбор перед разделением на VPS).

Было: _tick_journal выкидывал задание из _cur ДО записи в журнал, а цикл глотал ошибку на
debug. «database is locked» в минуту финиша (автобэкап держит БД) терял печать навсегда: ни
строки журнала, ни вопроса мастеру «заказ или для себя», ни списания пластика по ней.
Стало: задание ждёт в очереди записи (_unjournaled), пока запись не удастся, повтор идёт
следующим тиком, и строка журнала при этом не задваивается.
"""
import logging
import sqlite3

import pytest

from app import db, printerdrv, printwatch


class _Bot:
    async def send_message(self, chat_id, text, **kw):
        return None


@pytest.fixture
def asks(monkeypatch):
    """Вопросы мастеру «заказ или для себя»: на каждую запись журнала — ровно один."""
    sent = []

    async def fake_notify(bot, text, **kw):
        sent.append(text)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    monkeypatch.setattr(printwatch, "_notify_print_started", _noop)
    monkeypatch.setattr(printwatch, "_capture_bed_shot", _noop)
    # машину ведёт симулятор — фолбэк на кэш подписчика .env-принтера здесь лишний
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)
    return sent


async def _own(fake_printer) -> None:
    printerdrv.use_driver(fake_printer, printer_id=1)
    fake_printer.printer = await db.get_printer(1)


def _flaky_journal(monkeypatch, broken: list[bool], *, insert_first: bool = False) -> list:
    """add_print_journal падает «database is locked», пока broken[0] истинно.

    insert_first — худший случай: INSERT прошёл, а commit упал. Строка уже в общей транзакции
    соединения и уедет в БД с чужим commit-ом; повтор обязан её узнать, а не вставить вторую."""
    real = db.add_print_journal
    calls = []

    async def flaky(**kw):
        calls.append(kw)
        if broken[0]:
            if insert_first:
                await real(**kw)
            raise sqlite3.OperationalError("database is locked")
        return await real(**kw)

    monkeypatch.setattr(db, "add_print_journal", flaky)
    return calls


async def _print_and_finish(fake_printer, job: str, minutes: float) -> None:
    """Печать от старта до последнего активного тика; finish() — на совести теста."""
    fake_printer.begin(job, minutes=minutes, grams=12)
    await printwatch._tick_journal(_Bot())
    fake_printer.advance(minutes)
    await printwatch._tick_journal(_Bot())
    fake_printer.finish()


def _not_written(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "НЕ записана" in r.getMessage()]


async def test_failed_journal_write_is_retried_next_tick_exactly_once(
        tmp_db, fake_printer, asks, monkeypatch, caplog):
    await _own(fake_printer)
    broken = [False]
    calls = _flaky_journal(monkeypatch, broken)
    await _print_and_finish(fake_printer, "dragon.gcode", 30)

    broken[0] = True                                    # БД заперта ровно в минуту финиша
    with caplog.at_level(logging.WARNING, logger=printwatch.log.name):
        await printwatch._tick_journal(_Bot())
    assert await db.list_print_journal() == [] and asks == []
    assert len(printwatch._unjournaled) == 1, "печать потеряна: в очереди записи её нет"
    assert len(_not_written(caplog)) == 1, "сбой записи журнала обязан быть виден в логе"

    broken[0] = False                                   # следующий тик — БД ожила
    await printwatch._tick_journal(_Bot())
    rows = await db.list_print_journal()
    assert len(rows) == 1 and rows[0]["label"] == "dragon.gcode"
    assert rows[0]["printer_id"] == 1 and rows[0]["graded"] == 0
    assert len(asks) == 1 and printwatch._unjournaled == {}

    await printwatch._tick_journal(_Bot())              # дальше — ни дублей, ни новых вопросов
    assert len(await db.list_print_journal()) == 1 and len(asks) == 1
    assert len(calls) == 2


async def test_retry_after_insert_without_commit_does_not_duplicate(
        tmp_db, fake_printer, asks, monkeypatch):
    await _own(fake_printer)
    broken = [False]
    _flaky_journal(monkeypatch, broken, insert_first=True)
    await _print_and_finish(fake_printer, "dragon.gcode", 30)

    broken[0] = True
    await printwatch._tick_journal(_Bot())
    first = await db.list_print_journal()
    assert len(first) == 1 and asks == []               # строка есть, но финиш ещё не объявлен

    broken[0] = False
    await printwatch._tick_journal(_Bot())
    rows = await db.list_print_journal()
    assert [r["id"] for r in rows] == [first[0]["id"]], "повтор вставил вторую строку той же печати"
    assert len(asks) == 1 and printwatch._unjournaled == {}


async def test_next_print_does_not_merge_into_unwritten_one(
        tmp_db, fake_printer, asks, monkeypatch, caplog):
    """Мастер запустил следующую печать, а БД ещё не ожила: печати остаются РАЗНЫМИ записями."""
    await _own(fake_printer)
    broken = [False]
    _flaky_journal(monkeypatch, broken)
    await _print_and_finish(fake_printer, "a.gcode", 30)

    broken[0] = True
    with caplog.at_level(logging.WARNING, logger=printwatch.log.name):
        await printwatch._tick_journal(_Bot())          # финиш A: запись упала
        fake_printer.begin("b.gcode", minutes=20, grams=5)
        await printwatch._tick_journal(_Bot())          # A снова не записана, B пошла
    assert len(_not_written(caplog)) == 1, "WARNING по одной печати — не чаще раза в 10 минут"

    broken[0] = False
    fake_printer.advance(20)
    await printwatch._tick_journal(_Bot())              # A легла в журнал, B ещё печатается
    assert [r["label"] for r in await db.list_print_journal()] == ["a.gcode"]

    fake_printer.finish()
    await printwatch._tick_journal(_Bot())
    rows = {r["label"]: r for r in await db.list_print_journal()}
    assert set(rows) == {"a.gcode", "b.gcode"} and len(asks) == 2
    assert rows["a.gcode"]["minutes"] > rows["b.gcode"]["minutes"], "минуты печатей перемешались"
