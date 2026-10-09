"""База знаний владельца студии (OpenTodo c356387a): статьи kb_articles.

Маленький, но самостоятельный домен: своя таблица, свой экран, черновики (published=0,
видны только автору в редакторе) и ручная сортировка. С витриной (store/products.py) его
не сливали намеренно — между ними нет ни одной ссылки.

Выделено из app/db.py волной 6 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ. Из внешнего мира нужны только core._conn и
stdlib time (_now_txt — единственный местный хелпер, человекочитаемая отметка времени).
"""
from __future__ import annotations

import time

from .core import _conn


# ---- База знаний владельца студии (OpenTodo c356387a) ----
def _now_txt() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


async def kb_list(published_only: bool = True) -> list[dict]:
    """Статьи базы знаний. Черновики (published=0) видит только автор в редакторе;
    published_only=True — то, что показывается на самом экране базы знаний."""
    where = "WHERE published=1 " if published_only else ""
    cur = await _conn().execute(
        "SELECT id, title, body, sort, published, created_at, updated_at FROM kb_articles "
        f"{where}ORDER BY sort, id")
    return [dict(r) for r in await cur.fetchall()]


async def kb_get(aid: int) -> dict | None:
    cur = await _conn().execute(
        "SELECT id, title, body, sort, published, created_at, updated_at FROM kb_articles "
        "WHERE id=?", (int(aid or 0),))
    r = await cur.fetchone()
    return dict(r) if r else None


async def kb_save(aid: int | None, title: str, body: str, sort: int = 0,
                  published: bool = False) -> int:
    """Создать (aid=None/0) или обновить статью. Возвращает id."""
    now = _now_txt()
    args = (str(title or "").strip()[:200], str(body or ""), int(sort or 0),
            1 if published else 0)
    if aid:
        await _conn().execute(
            "UPDATE kb_articles SET title=?, body=?, sort=?, published=?, updated_at=? WHERE id=?",
            (*args, now, int(aid)))
        await _conn().commit()
        return int(aid)
    cur = await _conn().execute(
        "INSERT INTO kb_articles(title, body, sort, published, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?)", (*args, now, now))
    await _conn().commit()
    return cur.lastrowid


async def kb_delete(aid: int) -> None:
    await _conn().execute("DELETE FROM kb_articles WHERE id=?", (int(aid or 0),))
    await _conn().commit()
