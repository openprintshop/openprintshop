"""Держатель соединения БД — волна 0 разреза app/db.py (21.08.2026).

Единственное состояние всего слоя данных: aiosqlite-соединение `_db`. Оно
ПЕРЕ-ПРИСВАИВАЕТСЯ (init/connect_existing/close), поэтому фасадом его реэкспортировать
нельзя — та же механика, что у webapp._bot (волна 12 endgame): вторая привязка застыла бы
на None. Владеет жизненным циклом по-прежнему app/db.py (init/схема/миграции/close) —
он ставит соединение сюда через set_db(); доменные модули app/store/* читают ТОЛЬКО
через _conn() и не имеют права импортировать app.db.

Волной 2 сюда же переехал _jload — разбор JSON-колонки. Он не домен, а инфраструктура:
его зовут и склад (store/plastics), и одиннадцать мест самого db.py (заказы, журнал
печатей, витрина, корзина, коллекция) — класть его в доменный модуль значило бы
заставить будущие домены импортировать джейсон-хелпер у склада.

Тестам, которым нужна «упавшая БД», подменять _db ЗДЕСЬ (monkeypatch.setattr(core,
"_db", None)) — подмена db._db больше ни до чего не достаёт.
"""
from __future__ import annotations

import json

import aiosqlite

from ..config import abs_str, rel_data

_db: aiosqlite.Connection | None = None


def set_db(conn: aiosqlite.Connection | None) -> aiosqlite.Connection | None:
    """Поставить соединение (зовёт db.init/connect_existing/close); возврат прежнего —
    для тестов, по образцу webapp.set_bot."""
    global _db
    prev, _db = _db, conn
    return prev


def db_or_none() -> aiosqlite.Connection | None:
    """Соединение без исключения: для close() и веток «БД ещё/уже нет»."""
    return _db


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("DB не инициализирована — вызовите db.init()")
    return _db


def _jload(raw, default):
    """Разобрать JSON из колонки БД; битое/пустое значение → default.

    Через мапперы (_to_request, _to_collection, _to_saved) проходит КАЖДАЯ строка списочной
    выдачи, поэтому голый json.loads означает: одна испорченная ячейка роняет весь список."""
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


# ---- пути под DATA_DIR (C2) ----
# Правило проекта: в БД лежат ОТНОСИТЕЛЬНЫЕ пути внутри DATA_DIR, наружу (в dataclass'ы и
# словари, которые дальше уходят в Path/open/FileResponse/FSInputFile) store отдаёт
# АБСОЛЮТНЫЕ. Поэтому нормализация живёт в choke-point'ах этого слоя: rel_data на записи,
# _abs/_abs_list на сборке строки. Тогда десятки read-сайтов в handlers/web/webapp
# продолжают видеть привычный абсолютный путь и правки не требуют.
_rel = rel_data
_abs = abs_str


def _rel_list(paths) -> str:
    """JSON-список путей для колонки БД: каждый элемент через rel_data."""
    return json.dumps([_rel(str(p)) for p in (paths or [])])


def _abs_list(raw) -> list:
    """JSON-колонка со списком путей → список АБСОЛЮТНЫХ путей (битое значение → [])."""
    items = _jload(raw, []) if isinstance(raw, (str, bytes, bytearray)) or raw is None else raw
    if not isinstance(items, list):
        return []
    return [_abs(x) if isinstance(x, str) else x for x in items]
