"""Команда мастерской: роли сотрудников (A4) и личный вход в настольную админку.

* РОЛИ (таблица staff, A4): приглашённые сотрудники с ролью/именем/заметкой. Корневые
  владельцы живут в .env (ADMIN_IDS) и сюда не попадают — см. app/roles.py.
* ЛИЧНЫЙ ВХОД В ДЕСК (desk_sessions + одноразовые desk_login_codes): здесь ТОЛЬКО SQL,
  вся логика — в app/desklogin.py. Токен нигде не хранится, только его sha256.
  take_desk_code — «прочитал и сразу удалил» условным DELETE с проверкой срока: две
  одновременные попытки не могут открыть сессию по одному коду дважды.

Выделено из app/db.py волной 6 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ. Замыкания внутри модуля нет вовсе — ни одна из
тринадцати функций не зовёт другую; из внешнего мира нужны только core._conn и stdlib time.

Хвостовая get_text_setting, физически лежавшая в конце секции деск-входа, сюда НЕ поехала:
её домен — настройки, и она дописана в app/store/settings.py (иначе домен настроек навсегда
расщепился бы по двум модулям ради одной константы DEFAULT_TEXT_SETTINGS).
"""
from __future__ import annotations

import time

from .core import _conn


# ─────────────────────────── Команда мастерской: роли (A4) ───────────────────────────
async def list_staff() -> list[dict]:
    """Приглашённые сотрудники (без корневых ADMIN_IDS — те живут в .env, см. app/roles.py)."""
    cur = await _conn().execute("SELECT * FROM staff ORDER BY created_at ASC")
    return [dict(r) for r in await cur.fetchall()]


async def get_staff(user_id: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM staff WHERE user_id = ?", (int(user_id),))
    r = await cur.fetchone()
    return dict(r) if r else None


async def set_staff(user_id: int, role: str, *, name: str = "", note: str = "",
                    added_by: int | None = None) -> None:
    """Добавить сотрудника или сменить ему роль. UPSERT: повторный вызов меняет роль/имя,
    created_at и added_by у существующей строки не переписываются."""
    now = int(time.time())
    await _conn().execute(
        "INSERT INTO staff(user_id, role, name, note, added_by, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
        "role = excluded.role, name = excluded.name, note = excluded.note, updated_at = excluded.updated_at",
        (int(user_id), role, name or "", note or "", added_by, now, now))
    await _conn().commit()


async def remove_staff(user_id: int) -> None:
    await _conn().execute("DELETE FROM staff WHERE user_id = ?", (int(user_id),))
    await _conn().commit()


# ───────────── Личный вход в настольную админку (app/desklogin.py) ─────────────
# Хранилище; вся логика — там, здесь только SQL. Токен нигде не хранится, только его sha256.
async def list_desk_sessions() -> list[dict]:
    cur = await _conn().execute("SELECT * FROM desk_sessions")
    return [dict(r) for r in await cur.fetchall()]


async def add_desk_session(token_hash: str, user_id: int, device: str,
                           created_at: int, expires_at: int) -> None:
    await _conn().execute(
        "INSERT OR REPLACE INTO desk_sessions(token, user_id, device, created_at, last_seen, "
        "expires_at) VALUES(?,?,?,?,?,?)",
        (token_hash, int(user_id), device or "", int(created_at), int(created_at), int(expires_at)))
    await _conn().commit()


async def touch_desk_session(token_hash: str, seen_at: int, expires_at: int) -> None:
    await _conn().execute(
        "UPDATE desk_sessions SET last_seen = ?, expires_at = ? WHERE token = ?",
        (int(seen_at), int(expires_at), token_hash))
    await _conn().commit()


async def drop_desk_session(token_hash: str) -> None:
    await _conn().execute("DELETE FROM desk_sessions WHERE token = ?", (token_hash,))
    await _conn().commit()


async def drop_desk_sessions_of(user_id: int) -> int:
    """Все сессии человека. Возвращает, сколько выкинули, — мастеру это и показываем."""
    cur = await _conn().execute("DELETE FROM desk_sessions WHERE user_id = ?", (int(user_id),))
    await _conn().commit()
    return int(cur.rowcount or 0)


async def add_desk_code(code: str, user_id: int, created_at: int, expires_at: int) -> None:
    await _conn().execute(
        "INSERT OR REPLACE INTO desk_login_codes(code, user_id, created_at, expires_at) "
        "VALUES(?,?,?,?)", (code, int(user_id), int(created_at), int(expires_at)))
    await _conn().commit()


async def drop_desk_codes(user_id: int) -> None:
    await _conn().execute("DELETE FROM desk_login_codes WHERE user_id = ?", (int(user_id),))
    await _conn().commit()


async def take_desk_code(code: str, now: int) -> dict | None:
    """Прочитать и СРАЗУ удалить код: он одноразовый, повторный ввод не должен открывать ничего.

    Удаляем условным DELETE с проверкой срока — тогда «прочитал, проверил, удалил» не может
    разъехаться между двумя одновременными попытками."""
    cur = await _conn().execute(
        "SELECT * FROM desk_login_codes WHERE code = ? AND expires_at > ?", (code, int(now)))
    row = await cur.fetchone()
    if not row:
        return None
    cur = await _conn().execute("DELETE FROM desk_login_codes WHERE code = ?", (code,))
    await _conn().commit()
    return dict(row) if cur.rowcount else None


async def prune_desk_auth(now: int) -> None:
    await _conn().execute("DELETE FROM desk_sessions WHERE expires_at < ?", (int(now),))
    await _conn().execute("DELETE FROM desk_login_codes WHERE expires_at < ?", (int(now),))
    await _conn().commit()
