"""Реестр драйверов принтеров (OpenTodo 0613014e).

`for_printer(printer)` — единственная точка, где решается, каким протоколом мы разговариваем
с конкретным принтером. Всё остальное (webapp, printwatch) работает с драйвером и спрашивает
у него способности, а не проверяет вендора if-ами.

Совместимость с сегодняшним днём важнее чистоты: у принтера в боевой БД поле `kind` пустое,
и для него драйвер выбирается ровно так, как код вёл себя раньше — Elegoo, если в .env есть
его адрес; иначе Moonraker, если задан его URL; иначе «только профиль слайсера».
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .. import config
from .bambu_drv import BambuDriver
from .base import Caps, NoneDriver, PrinterDriver
from .elegoo_drv import ElegooDriver
from .fake import FakePrinter
from .moonraker_drv import MoonrakerDriver

log = logging.getLogger(__name__)

DRIVERS: dict[str, type[PrinterDriver]] = {
    "elegoo": ElegooDriver,
    "bambu": BambuDriver,
    "moonraker": MoonrakerDriver,
    "none": NoneDriver,
    "fake": FakePrinter,
}

# Что показывать в карточке принтера в админке. «fake» наружу не предлагаем: это тестовое
# окружение, а не режим работы мастерской.
KINDS: tuple[tuple[str, str], ...] = (
    ("", "определить автоматически"),
    ("elegoo", "Elegoo (SDCP)"),
    ("bambu", "Bambu Lab (связь — с приездом машины)"),
    ("moonraker", "Klipper (Moonraker)"),
    ("none", "только профиль слайсера"),
)

# Шов подмены драйверов (только для тестов, см. use_driver / use_driver_factory). Форм три,
# потому что одной глобальной было мало: пока подмена одна на процесс, сценарий «две карточки —
# два разных принтера» невыразим в принципе, а именно его проверяет работа по второму принтеру.
_override: PrinterDriver | None = None                          # «на все карточки» (старая форма)
_override_by_id: dict[int, PrinterDriver] = {}                  # карточке с этим id — свой драйвер
_override_factory: Callable[[Any], PrinterDriver | None] | None = None   # выбор по полям карточки


def autodetect_kind() -> str:
    """Как код вёл себя до появления драйверов: Elegoo из .env, иначе Moonraker, иначе никак."""
    if (config.ELEGOO_PRINTER_IP or "").strip():
        return "elegoo"
    if (config.MOONRAKER_URL or "").strip():
        return "moonraker"
    return "none"


def _overridden(printer) -> PrinterDriver | None:
    """Подмена для этой карточки — от самой точной формы к самой общей.

    Порядок (карточка → фабрика → «все карточки») выбран так, чтобы точечную подмену можно было
    поставить ПОВЕРХ общей: тест ставит симулятор всем принтерам сразу, а одной карточке — свой,
    и она получает именно его. Обратный порядок сделал бы точечную форму бесполезной рядом
    со старой."""
    pid = getattr(printer, "id", None)
    if _override_by_id and isinstance(pid, int):
        drv = _override_by_id.get(pid)
        if drv is not None:
            return drv
    if _override_factory is not None:
        drv = _override_factory(printer)
        if drv is not None:      # None от фабрики = «эту карточку не подменяю», выбор идёт дальше
            return drv
    return _override


def for_printer(printer=None) -> PrinterDriver:
    """Драйвер для карточки принтера (или для «принтера по умолчанию», если карточки нет)."""
    drv = _overridden(printer)
    if drv is not None:
        return drv
    kind = (getattr(printer, "kind", "") or "").strip().lower() or autodetect_kind()
    cls = DRIVERS.get(kind)
    if cls is None:
        log.warning("Неизвестный тип принтера «%s» — работаем как «только профиль слайсера»", kind)
        cls = NoneDriver
    return cls(printer)


def use_driver(driver: PrinterDriver | None, *, printer_id: int | None = None) -> None:
    """Подменить драйвер (тесты: симулятор вместо железа).

    Без `printer_id` — как раньше, во ВСЁМ приложении: `for_printer` вернёт этот драйвер любой
    карточке. С `printer_id` — только карточке с таким id; остальные выбирают драйвер обычным
    путём, по своему `kind`. Так описывается мастерская с двумя разными принтерами.

    Отдельной функцией, а не monkeypatch-ем модуля: подмена одна на процесс и снимается
    вызовом use_driver(None) — иначе симулятор протёк бы из теста в тест. Поэтому use_driver(None)
    без `printer_id` гасит шов ЦЕЛИКОМ, вместе с точечными подменами и фабрикой: именно на этот
    вызов завязана autouse-фикстура-страховка, и оставить ей половину шва означало бы вернуть ту
    самую протечку, от которой она защищает."""
    global _override, _override_factory
    if printer_id is not None:
        if driver is None:
            _override_by_id.pop(int(printer_id), None)
        else:
            _override_by_id[int(printer_id)] = driver
        return
    _override = driver
    if driver is None:
        _override_by_id.clear()
        _override_factory = None


def use_driver_factory(factory: Callable[[Any], PrinterDriver | None] | None) -> None:
    """Подмена, которая смотрит на карточку: `factory(printer)` → драйвер или None.

    Нужна там, где принтеры различает не id (в тестах он может быть ещё не присвоен), а поля
    карточки — kind или адрес. Вернув None, фабрика говорит «эту карточку не подменяю», и выбор
    драйвера идёт дальше обычным путём: так можно накрыть один принтер, не ослепляя проверки
    выбора у остальных. Снимается use_driver_factory(None) либо общим use_driver(None)."""
    global _override_factory
    _override_factory = factory


__all__ = ["Caps", "PrinterDriver", "NoneDriver", "ElegooDriver", "BambuDriver",
           "MoonrakerDriver", "FakePrinter", "DRIVERS", "KINDS", "for_printer", "use_driver",
           "use_driver_factory", "autodetect_kind"]
