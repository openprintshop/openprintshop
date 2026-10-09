"""Волна 0 разреза db.py: соединение живёт в app/store/core, db.py — фасад и владелец цикла.

Контракты, на которых стоит вся будущая программа (refactoring-db-plan-2026-08-21):
_conn один на всех (db._conn ЕСТЬ core._conn), состояние подменяется только в core,
доменные модули пакета store не смеют импортировать app.db.
"""
import pathlib
import subprocess
import sys

import pytest

from app import db
from app.store import core

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_conn_is_the_same_object():
    assert db._conn is core._conn, "db._conn разошёлся с core._conn — фасад сломан"


async def test_init_wires_core_and_conn_works(tmp_db):
    """После init/connect_existing соединение стоит в core и _conn отдаёт именно его."""
    assert core.db_or_none() is not None
    assert core._conn() is core.db_or_none()
    cur = await db._conn().execute("SELECT 1")
    assert (await cur.fetchone())[0] == 1


def test_conn_raises_without_db(monkeypatch):
    monkeypatch.setattr(core, "_db", None)
    with pytest.raises(RuntimeError, match="init"):
        core._conn()


def test_set_db_returns_previous():
    """Возврат прежнего значения — контракт для тестов (образец webapp.set_bot)."""
    sentinel = object()
    prev = core.set_db(sentinel)
    try:
        assert core.db_or_none() is sentinel
    finally:
        core.set_db(prev)


def test_store_does_not_import_db():
    """Разрез односторонний: пакет store не тянет app.db (иначе цикл, ради разрыва
    которого программа и затевается)."""
    code = "import app.store.core, sys; print(int('app.db' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.store.core подтянул app.db: {out}"


def test_no_module_level_db_left_in_dbpy():
    """В db.py не должно остаться МОДУЛЬНОГО _db: вернись он — init стал бы писать в одну
    копию, а _conn читать из другой, и «БД не инициализирована» всплыла бы у живого бота."""
    import ast
    src = (REPO / "app" / "db.py").read_text(encoding="utf-8")
    assigns = {t.id for n in ast.parse(src).body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    ann = {n.target.id for n in ast.parse(src).body
           if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    assert "_db" not in (assigns | ann)
