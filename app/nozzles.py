"""Сопла принтера как отдельная сущность (OpenTodo f71f4973).

Раньше сопло было НЕЯВНЫМ и жило в двух местах сразу:

* у принтера один `machine_profile` — в нём зашит `nozzle_diameter` (0.4);
* у пресета качества сопло угадывалось из ИМЕНИ процесс-файла («…0.6 nozzle.json»)
  регуляркой в webapp — только чтобы нарисовать клиенту бейдж «сопло 0.6».

Пока сопло было одно, расхождения не было. С приходом 0.6 (03.08.2026, кампания и паспорт
в калибровке) вылезло: клиент тянет ползунок на 0.30, слайсер берёт ПРОЦЕСС сопла 0.6, а
машинный профиль остаётся от 0.4 — ширины линий и расход считаются по чужому соплу, а
`selfcheck` даже не знает, что второй профиль вообще должен существовать.

Здесь — доменная часть: нормализация диаметра и чтение его из профилей слайсера/имён.
Хранилище (таблица `printer_nozzles`, столбец `quality_presets.nozzle`) — в app/db.py,
выбор профиля под конкретный слайс — `db.machine_profile_for`.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT = "0.4"                      # исторический дефолт проекта (CC2 приехал с 0.4)

# «0.6 nozzle», «0,6 nozzle», «nozzle 0.6» — в именах профилей ElegooSlicer встречается и то и то.
_NAME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:mm\s*)?nozzle|nozzle[\s_-]*(\d+(?:[.,]\d+)?)", re.I)


def norm(value) -> str:
    """Диаметр к каноничному виду: 0.6 / «0,60» / « .6 » → «0.6». Не число → «»."""
    if value is None:
        return ""
    txt = str(value).strip().replace(",", ".")
    if not txt:
        return ""
    try:
        num = float(txt)
    except (TypeError, ValueError):
        return ""
    if num <= 0 or num > 5:          # 5 мм — заведомо не FDM-сопло: явная ошибка ввода
        return ""
    # Каноничная запись — без хвостовых нулей: «0.60» и «0.6» это одно сопло, а ключи
    # калибровки (паспорт: пластик × принтер × сопло) сравниваются строкой.
    return f"{num:g}"


def from_process_name(name: str) -> str:
    """Сопло из имени процесс-профиля («0.30mm Standard @Elegoo CC2 0.6 nozzle.json» → «0.6»).

    Фолбэк для пресетов качества, у которых сопло ещё не проставлено явно (столбец пуст):
    ровно та логика, что жила регуляркой в webapp до этой задачи."""
    m = _NAME_RE.search(str(name or ""))
    if not m:
        return ""
    return norm(m.group(1) or m.group(2))


def from_machine_profile(path: str | Path) -> str:
    """Сопло из machine-профиля слайсера: ключ `nozzle_diameter` (бывает списком).

    Значение может лежать в родителе цепочки `inherits`, поэтому профиль расплющиваем тем же
    кодом, что и слайсер. Профиль не читается — пробуем имя файла, иначе «»."""
    p = Path(path)
    data: dict = {}
    try:
        from . import slicer  # локальный импорт: nozzles не тянет слайсер
        data = slicer._flatten_profile(p)         # noqa: SLF001 — единственный разбор `inherits`
    except Exception:  # noqa: BLE001
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    raw = data.get("nozzle_diameter")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    val = norm(raw)
    return val or from_process_name(p.stem)


def label(nozzle: str) -> str:
    """«0.6» → «сопло 0.6 мм» для интерфейса и уведомлений."""
    n = norm(nozzle) or DEFAULT
    return f"сопло {n} мм"


def matches(a, b) -> bool:
    """Одно ли это сопло. Пустое значение считаем дефолтом — записи до миграции сравнимы."""
    return (norm(a) or DEFAULT) == (norm(b) or DEFAULT)
