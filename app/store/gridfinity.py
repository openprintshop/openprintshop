"""Мастерская органайзеров: проекты Gridfinity, спецификация деталей и всё вокруг них.

Хранилище одного домена — конфигуратора Gridfinity, выросшего из «одного черновика на
юзера» в мини-приложение с долгой жизнью проекта:

* ПРОЕКТ (gridfinity_projects) — физический ящик клиента. Раскладка лежит ЦЕЛИКОМ
  строкой JSON движка ({v, base, modules, seq}); сервер её не пересчитывает.
* ДЕТАЛЬ (gridfinity_parts) — проекция модуля раскладки (`mod_id`) плюс то, чего в
  геометрии нет: имя, заметка «что кладём», количество, состояние заказа, номер заявки,
  версия. `spec_json` — ЗАМОРОЖЕННЫЙ снимок геометрии на момент заказа: живая геометрия
  всегда читается из раскладки проекта, а печатали именно то, что в снимке.
* МЕРКИ (gridfinity_measures) — сохранённые замеры мебели клиента: новый проект стартует
  без рулетки.
* ВЕРСИИ (gridfinity_versions) — снимки раскладки для отката (храним последние N).
* ЗАГОТОВКИ (gridfinity_presets) — свои модули, переносимые между проектами.
* ССЫЛКА «ПОКАЗАТЬ» (gridfinity_shares) — токен на чтение проекта без авторизации.

Плюс два старожила, переехавшие сюда из store/profile.py вместе с доменом:
черновик `gridfinity_configs` (совместимость со старым фронтом) и снимок заявки
`gridfinity_order_configs`.

Модуль — ТОЛЬКО SQL: правила состояний, сверка раскладки со спецификацией и оценка —
в app/web/gridfinity.py. Рёбер к соседям по store нет ни одного.
"""
from __future__ import annotations

import time

from .core import _conn


# ---- Черновик и снимок заявки (старый контур, совместимость) ----
# Черновик хранится строкой JSON как приехал с фронта (формат движка {v, base, modules,
# seq}) — сервер его не разбирает, только ограничивает размер на ручке. Один черновик на
# юзера: до появления проектов конфигуратор был «рабочим столом», а не библиотекой.
async def gridfinity_config_get(user_id: int) -> str | None:
    cur = await _conn().execute(
        "SELECT config FROM gridfinity_configs WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return row["config"] if row else None


async def gridfinity_config_set(user_id: int, config: str) -> None:
    await _conn().execute(
        "INSERT INTO gridfinity_configs(user_id, config, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET config=excluded.config, "
        "updated_at=excluded.updated_at",
        (user_id, config, int(time.time())))
    await _conn().commit()


async def gridfinity_order_config_add(request_id: int, user_id: int, config: str) -> None:
    """Снимок конфигурации, из которой оформлена заявка (request_id = requests.id)."""
    await _conn().execute(
        "INSERT OR REPLACE INTO gridfinity_order_configs(request_id, user_id, config, "
        "created_at) VALUES(?,?,?,?)",
        (request_id, user_id, config, int(time.time())))
    await _conn().commit()


async def gridfinity_order_config_get(request_id: int) -> str | None:
    """Конфигурация заявки — раскладка в карточке заказа у мастера."""
    cur = await _conn().execute(
        "SELECT config FROM gridfinity_order_configs WHERE request_id = ?", (request_id,))
    row = await cur.fetchone()
    return row["config"] if row else None


# ---- Проекты ----
async def gf_projects(user_id: int, archived: int = 0) -> list[dict]:
    """Проекты юзера, свежие сверху. `archived=0` — активные, 1 — архив, -1 — все."""
    sql = ("SELECT id, user_id, title, config, created_at, updated_at, archived_at "
           "FROM gridfinity_projects WHERE user_id = ?")
    args: list = [user_id]
    if archived >= 0:
        sql += " AND archived_at " + ("> 0" if archived else "= 0")
    sql += " ORDER BY updated_at DESC, id DESC"
    cur = await _conn().execute(sql, args)
    return [dict(r) for r in await cur.fetchall()]


async def gf_project(project_id: int) -> dict | None:
    cur = await _conn().execute(
        "SELECT id, user_id, title, config, created_at, updated_at, archived_at "
        "FROM gridfinity_projects WHERE id = ?", (project_id,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def gf_project_count(user_id: int, archived: int = 0) -> int:
    """Сколько проектов у юзера — потолок активных проверяется до вставки."""
    sql = "SELECT COUNT(*) AS n FROM gridfinity_projects WHERE user_id = ?"
    if archived >= 0:
        sql += " AND archived_at " + ("> 0" if archived else "= 0")
    cur = await _conn().execute(sql, (user_id,))
    r = await cur.fetchone()
    return int(r["n"]) if r else 0


async def gf_project_add(user_id: int, title: str, config: str) -> int:
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO gridfinity_projects(user_id, title, config, created_at, updated_at, "
        "archived_at) VALUES(?,?,?,?,?,0)", (user_id, title, config, now, now))
    await _conn().commit()
    return int(cur.lastrowid)


async def gf_project_set_config(project_id: int, config: str) -> None:
    await _conn().execute(
        "UPDATE gridfinity_projects SET config = ?, updated_at = ? WHERE id = ?",
        (config, int(time.time()), project_id))
    await _conn().commit()


async def gf_project_rename(project_id: int, title: str) -> None:
    await _conn().execute(
        "UPDATE gridfinity_projects SET title = ?, updated_at = ? WHERE id = ?",
        (title, int(time.time()), project_id))
    await _conn().commit()


async def gf_project_archive(project_id: int, archived: bool) -> None:
    await _conn().execute(
        "UPDATE gridfinity_projects SET archived_at = ?, updated_at = ? WHERE id = ?",
        (int(time.time()) if archived else 0, int(time.time()), project_id))
    await _conn().commit()


async def gf_drop_legacy_draft(user_id: int) -> None:
    """Снести зеркало старого черновика. Зовётся, когда у юзера не осталось проектов:
    иначе миграция воскресила бы только что удалённый ящик при следующем заходе."""
    await _conn().execute("DELETE FROM gridfinity_configs WHERE user_id = ?", (user_id,))
    await _conn().commit()


async def gf_project_delete(project_id: int) -> None:
    """Удаление проекта уносит его детали, версии, заготовки-снимки и ссылку.

    Одним commit-ом: осиротевшие детали — это заявки, ссылающиеся в пустоту."""
    for sql in ("DELETE FROM gridfinity_parts WHERE project_id = ?",
                "DELETE FROM gridfinity_versions WHERE project_id = ?",
                "DELETE FROM gridfinity_shares WHERE project_id = ?",
                "DELETE FROM gridfinity_projects WHERE id = ?"):
        await _conn().execute(sql, (project_id,))
    await _conn().commit()


# ---- Детали спецификации ----
_PART_COLS = ("id, project_id, mod_id, name, note, qty, state, sel, request_id, version, "
              "parent_id, color_idx, sig, vol_cm3, spec_json, changed_at, printed_at, "
              "created_at, updated_at")


async def gf_parts(project_id: int) -> list[dict]:
    cur = await _conn().execute(
        f"SELECT {_PART_COLS} FROM gridfinity_parts WHERE project_id = ? ORDER BY id",
        (project_id,))
    return [dict(r) for r in await cur.fetchall()]


async def gf_part(part_id: int) -> dict | None:
    cur = await _conn().execute(
        f"SELECT {_PART_COLS} FROM gridfinity_parts WHERE id = ?", (part_id,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def gf_request_statuses(ids: list[int]) -> dict:
    """{request_id: status} одним запросом. Цикл get_request на каждую деталь давал N+1 на
    каждом открытии проекта и на каждой галочке в спецификации."""
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    cur = await _conn().execute(
        f"SELECT id, status FROM requests WHERE id IN ({q})", tuple(ids))
    return {int(r["id"]): str(r["status"] or "") for r in await cur.fetchall()}


async def gf_parts_by_request(request_id: int) -> list[dict]:
    """Детали одной заявки — печать-лист мастера и отметка «готово»."""
    cur = await _conn().execute(
        f"SELECT {_PART_COLS} FROM gridfinity_parts WHERE request_id = ? ORDER BY id",
        (request_id,))
    return [dict(r) for r in await cur.fetchall()]


async def gf_part_add(*, project_id: int, mod_id: int, name: str = "", note: str = "",
                      qty: int = 1, state: str = "draft", color_idx: int = 0,
                      sig: str = "", vol_cm3: float = 0.0, version: int = 1,
                      parent_id: int = 0) -> int:
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO gridfinity_parts(project_id, mod_id, name, note, qty, state, sel, "
        "request_id, version, parent_id, color_idx, sig, vol_cm3, spec_json, changed_at, "
        "printed_at, created_at, updated_at) VALUES(?,?,?,?,?,?,0,0,?,?,?,?,?,'',0,0,?,?)",
        (project_id, mod_id, name, note, qty, state, version, parent_id, color_idx, sig,
         vol_cm3, now, now))
    await _conn().commit()
    return int(cur.lastrowid)


_PART_PATCH = {"mod_id", "name", "note", "qty", "state", "sel", "request_id", "color_idx",
               "sig", "vol_cm3", "spec_json", "changed_at", "printed_at"}


async def gf_part_update(part_id: int, **fields) -> None:
    """Точечная правка детали. Ключи вне белого списка игнорируются молча — вызывающий
    код собирает патч из клиентского JSON, и опечатка не должна ронять всю ручку."""
    patch = {k: v for k, v in fields.items() if k in _PART_PATCH}
    if not patch:
        return
    sets = ", ".join(f"{k} = ?" for k in patch)
    await _conn().execute(
        f"UPDATE gridfinity_parts SET {sets}, updated_at = ? WHERE id = ?",
        (*patch.values(), int(time.time()), part_id))
    await _conn().commit()


async def gf_part_delete(part_id: int) -> None:
    await _conn().execute("DELETE FROM gridfinity_parts WHERE id = ?", (part_id,))
    await _conn().commit()


async def gf_parts_select(project_id: int, ids: list[int], sel: int, free: bool = False) -> None:
    """Отметить/снять детали «в партию» ОДНИМ запросом (групповые действия спецификации).

    free=True — заодно вернуть деталь в черновик и отвязать от умершей заявки: клиент явно
    сказал, что печатаем это сейчас."""
    if not ids:
        return
    q = ",".join("?" * len(ids))
    extra = ", state = 'draft', request_id = 0" if free else ""
    await _conn().execute(
        f"UPDATE gridfinity_parts SET sel = ?{extra}, updated_at = ? "
        f"WHERE project_id = ? AND id IN ({q})",
        (int(sel), int(time.time()), project_id, *ids))
    await _conn().commit()


async def gf_parts_order(project_id: int, ids: list[int], request_id: int,
                         specs: dict[int, str]) -> int:
    """Оформление партии: детали уезжают в заявку одним commit-ом. Возвращает число
    ЗАХВАЧЕННЫХ деталей.

    Захват УСЛОВНЫЙ (`state IN ('draft','hold')`): два параллельных оформления — ретрай по
    таймауту, второй открытый мини-апп, повтор с другого устройства — иначе создали бы две
    заявки на один комплект, а вторая перетёрла бы request_id первой, оставив мастеру
    пустой печать-лист. Одной транзакцией: полу-оформленная партия хуже любой ошибки."""
    now, taken = int(time.time()), 0
    for pid in ids:
        cur = await _conn().execute(
            "UPDATE gridfinity_parts SET state = 'ordered', sel = 0, request_id = ?, "
            "spec_json = ?, changed_at = 0, updated_at = ? "
            "WHERE id = ? AND project_id = ? AND state IN ('draft', 'hold')",
            (request_id, specs.get(pid, ""), now, pid, project_id))
        taken += int(cur.rowcount or 0)
    await _conn().commit()
    return taken


async def gf_part_set_printed(part_id: int, printed: bool) -> None:
    now = int(time.time())
    await _conn().execute(
        "UPDATE gridfinity_parts SET state = ?, printed_at = ?, updated_at = ? WHERE id = ?",
        ("printed" if printed else "ordered", now if printed else 0, now, part_id))
    await _conn().commit()


async def gf_parts_release_request(request_id: int) -> int:
    """Заявка отменена/отклонена — детали возвращаются в черновик.

    Возвращает число освобождённых деталей. Напечатанные не трогаем: отказ по остатку
    заявки не отменяет того, что уже лежит у клиента на столе."""
    cur = await _conn().execute(
        "UPDATE gridfinity_parts SET state = 'draft', request_id = 0, updated_at = ? "
        "WHERE request_id = ? AND state = 'ordered'", (int(time.time()), request_id))
    await _conn().commit()
    return int(cur.rowcount or 0)


# ---- Мерки мебели ----
async def gf_measures(user_id: int) -> list[dict]:
    cur = await _conn().execute(
        "SELECT id, user_id, title, btype, w, d, h, series, created_at "
        "FROM gridfinity_measures WHERE user_id = ? ORDER BY id DESC", (user_id,))
    return [dict(r) for r in await cur.fetchall()]


async def gf_measure_add(user_id: int, title: str, btype: str, w: float, d: float,
                         h: float, series: str = "") -> int:
    cur = await _conn().execute(
        "INSERT INTO gridfinity_measures(user_id, title, btype, w, d, h, series, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user_id, title, btype, w, d, h, series, int(time.time())))
    await _conn().commit()
    return int(cur.lastrowid)


async def gf_measure_delete(measure_id: int, user_id: int) -> None:
    await _conn().execute(
        "DELETE FROM gridfinity_measures WHERE id = ? AND user_id = ?", (measure_id, user_id))
    await _conn().commit()


# ---- Версии раскладки ----
async def gf_version_add(project_id: int, config: str, note: str = "", keep: int = 10) -> int:
    """Снимок раскладки + обрезка истории до `keep` штук одним commit-ом."""
    cur = await _conn().execute(
        "INSERT INTO gridfinity_versions(project_id, config, note, created_at) VALUES(?,?,?,?)",
        (project_id, config, note, int(time.time())))
    vid = int(cur.lastrowid)
    await _conn().execute(
        "DELETE FROM gridfinity_versions WHERE project_id = ? AND id NOT IN "
        "(SELECT id FROM gridfinity_versions WHERE project_id = ? ORDER BY id DESC LIMIT ?)",
        (project_id, project_id, keep))
    await _conn().commit()
    return vid


async def gf_versions(project_id: int) -> list[dict]:
    cur = await _conn().execute(
        "SELECT id, project_id, note, created_at FROM gridfinity_versions "
        "WHERE project_id = ? ORDER BY id DESC", (project_id,))
    return [dict(r) for r in await cur.fetchall()]


async def gf_version_get(version_id: int) -> dict | None:
    cur = await _conn().execute(
        "SELECT id, project_id, config, note, created_at FROM gridfinity_versions "
        "WHERE id = ?", (version_id,))
    r = await cur.fetchone()
    return dict(r) if r else None


# ---- Заготовки деталей ----
async def gf_presets(user_id: int) -> list[dict]:
    cur = await _conn().execute(
        "SELECT id, user_id, title, mod_json, created_at FROM gridfinity_presets "
        "WHERE user_id = ? ORDER BY id DESC", (user_id,))
    return [dict(r) for r in await cur.fetchall()]


async def gf_preset_add(user_id: int, title: str, mod_json: str) -> int:
    cur = await _conn().execute(
        "INSERT INTO gridfinity_presets(user_id, title, mod_json, created_at) VALUES(?,?,?,?)",
        (user_id, title, mod_json, int(time.time())))
    await _conn().commit()
    return int(cur.lastrowid)


async def gf_preset_delete(preset_id: int, user_id: int) -> None:
    await _conn().execute(
        "DELETE FROM gridfinity_presets WHERE id = ? AND user_id = ?", (preset_id, user_id))
    await _conn().commit()


# ---- Ссылка «показать» ----
async def gf_share_set(project_id: int, user_id: int, token: str) -> None:
    """Токен на проект — один; повторный вызов перевыпускает ссылку (старая умирает)."""
    await _conn().execute("DELETE FROM gridfinity_shares WHERE project_id = ?", (project_id,))
    await _conn().execute(
        "INSERT INTO gridfinity_shares(token, project_id, user_id, created_at) VALUES(?,?,?,?)",
        (token, project_id, user_id, int(time.time())))
    await _conn().commit()


async def gf_share_get(token: str) -> dict | None:
    cur = await _conn().execute(
        "SELECT token, project_id, user_id, created_at FROM gridfinity_shares WHERE token = ?",
        (token,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def gf_share_of(project_id: int) -> str | None:
    cur = await _conn().execute(
        "SELECT token FROM gridfinity_shares WHERE project_id = ?", (project_id,))
    r = await cur.fetchone()
    return r["token"] if r else None


async def gf_share_drop(project_id: int) -> None:
    await _conn().execute("DELETE FROM gridfinity_shares WHERE project_id = ?", (project_id,))
    await _conn().commit()


# ─────────────────── сессия «продолжить на компьютере» ───────────────────
#
# Ссылка на ПРАВКУ одного проекта в обычном браузере, без Telegram. Хранится отдельно от
# `gridfinity_shares` (та — только чтение): смешать их значило бы однажды выдать право
# писать тому, кому дали посмотреть. Токен на проект ОДИН — перевыпуск гасит прежний.

async def gf_desk_set(project_id: int, user_id: int, token: str, ttl: int) -> int:
    """Выпустить ссылку и вернуть её срок (unix). Прежняя ссылка проекта умирает."""
    now = int(time.time())
    exp = now + max(60, int(ttl))
    await _conn().execute("DELETE FROM gridfinity_desk WHERE project_id = ?", (project_id,))
    await _conn().execute(
        "INSERT INTO gridfinity_desk(token, project_id, user_id, created_at, expires_at) "
        "VALUES(?,?,?,?,?)", (token, project_id, user_id, now, exp))
    await _conn().commit()
    return exp


async def gf_desk_get(token: str) -> dict | None:
    """Живая сессия по токену. Просроченную не отдаём и тут же убираем из базы."""
    cur = await _conn().execute(
        "SELECT token, project_id, user_id, created_at, expires_at, used_at "
        "FROM gridfinity_desk WHERE token = ?", (token,))
    r = await cur.fetchone()
    if not r:
        return None
    if int(r["expires_at"]) <= int(time.time()):
        await _conn().execute("DELETE FROM gridfinity_desk WHERE token = ?", (token,))
        await _conn().commit()
        return None
    return dict(r)


async def gf_desk_of(project_id: int) -> dict | None:
    """Живая сессия проекта — для кнопки в мастерской (показать/погасить)."""
    cur = await _conn().execute(
        "SELECT token, expires_at, used_at FROM gridfinity_desk WHERE project_id = ?",
        (project_id,))
    r = await cur.fetchone()
    if not r or int(r["expires_at"]) <= int(time.time()):
        return None
    return dict(r)


async def gf_desk_touch(token: str) -> None:
    """Отметка «ссылкой пользовались» — по ней клиент видит, что сессия жива не зря."""
    await _conn().execute("UPDATE gridfinity_desk SET used_at = ? WHERE token = ?",
                          (int(time.time()), token))
    await _conn().commit()


async def gf_desk_drop(project_id: int) -> None:
    await _conn().execute("DELETE FROM gridfinity_desk WHERE project_id = ?", (project_id,))
    await _conn().commit()


async def gf_user_name(user_id: int) -> str:
    """Подпись клиента по его прошлым заявкам — для заказа из сессии ПК.

    Там нет initData, то есть нет ни имени, ни username: взять их неоткуда, кроме как из
    того, чем клиент уже подписывался. Пусто — вызывающий подставит «id N»."""
    cur = await _conn().execute(
        "SELECT user_name FROM requests WHERE user_id = ? AND user_name IS NOT NULL "
        "AND user_name != '' ORDER BY id DESC LIMIT 1", (user_id,))
    r = await cur.fetchone()
    return str(r["user_name"]) if r else ""
