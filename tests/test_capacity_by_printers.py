"""Ёмкость мастерской в сроках = часы в сутки × число печатающих машин (цикл 3).

Срок клиенту считался от ОДНОЙ константы «печатных часов в сутки» на всю студию. При
одновременной печати на двух машинах (решение владельца 11.08) мастерская успевает вдвое
больше, а обещание клиенту осталось бы прежним — то есть главный выигрыш от второго принтера
до него бы не дошёл.

Ключевое свойство правки: с ОДНОЙ машиной формула та же, что была. Ничего не меняется сегодня
и становится верным само, когда вторую карточку включат.
"""
import datetime

from app import config, db


def _as_date(stamp: str) -> datetime.date:
    """«DD.MM» → настоящая дата.

    Строки сравнивать НЕЛЬЗЯ: на переломе месяца «30.08» лексикографически больше
    «01.09», хотя календарно раньше, и проверка «второй принтер сократил срок» падала бы
    ровно в последние дни месяца (поймано 28.08.2026 — тест до этого спал весь год).
    Год восстанавливаем по ближайшему будущему: eta_date всегда смотрит вперёд.
    """
    today = datetime.date.today()
    day, month = (int(part) for part in stamp.split("."))
    year = today.year if (month, day) >= (today.month, today.day) else today.year + 1
    return datetime.date(year, month, day)


def test_one_printer_keeps_the_old_formula(monkeypatch):
    """Регресс: пока принтер один, срок обязан совпадать с прежним."""
    monkeypatch.setattr(config, "PRINT_HOURS_PER_DAY", 12.0)
    monkeypatch.setattr(config, "ACTIVE_PRINTERS", 1)
    one = db.eta_date(12 * 60 * 3)                       # ровно три дня печати
    monkeypatch.setattr(config, "ACTIVE_PRINTERS", 1)
    assert db.eta_date(12 * 60 * 3) == one


def test_two_printers_halve_the_queue(monkeypatch):
    """Две машины печатают одновременно — очередь расходится вдвое быстрее."""
    monkeypatch.setattr(config, "PRINT_HOURS_PER_DAY", 12.0)
    monkeypatch.setattr(config, "ACTIVE_PRINTERS", 1)
    slow = db.eta_date(12 * 60 * 4)                      # 4 дня на одной машине
    monkeypatch.setattr(config, "ACTIVE_PRINTERS", 2)
    fast = db.eta_date(12 * 60 * 4)                      # те же работы на двух
    assert _as_date(fast) < _as_date(slow), f"вторая машина не сократила срок: {slow} → {fast}"


def test_capacity_never_drops_below_one_machine(monkeypatch):
    """Нулевой или битый кэш не должен растягивать срок до бесконечности."""
    monkeypatch.setattr(config, "PRINT_HOURS_PER_DAY", 12.0)
    monkeypatch.setattr(config, "ACTIVE_PRINTERS", 0)
    assert db.eta_date(60) == db.eta_date(60)
    monkeypatch.setattr(config, "ACTIVE_PRINTERS", 1)
    zero_is_one = db.eta_date(12 * 60 * 2)
    monkeypatch.setattr(config, "ACTIVE_PRINTERS", 0)
    assert db.eta_date(12 * 60 * 2) == zero_is_one


async def test_cache_follows_the_printer_cards(tmp_db):
    """Кэш обновляется САМИМИ функциями изменения принтеров — забыть его в новой ручке нельзя."""
    await db.refresh_active_printers()
    assert config.ACTIVE_PRINTERS == 1

    second = await db.add_printer("Bambu Lab P1S", "m.json", "p", "f", is_active=1)
    assert config.ACTIVE_PRINTERS == 2, "заведение активной карточки не обновило ёмкость"

    await db.update_printer(second, is_active=0)
    assert config.ACTIVE_PRINTERS == 1, "выключение карточки не вернуло ёмкость"

    await db.update_printer(second, is_active=1)
    await db.delete_printer(second)
    assert config.ACTIVE_PRINTERS == 1, "удаление карточки не обновило ёмкость"


async def test_inactive_card_does_not_promise_speed(tmp_db):
    """Заведённая, но НЕ включённая карточка ёмкости не добавляет: она ещё не печатает."""
    await db.add_printer("Bambu Lab P1S", "m.json", "p", "f")     # по умолчанию неактивна
    assert config.ACTIVE_PRINTERS == 1
