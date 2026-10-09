"""Личный вход сотрудника в настольную админку.

ЗАЧЕМ. До этого вход в деск был один на машину: `ADMIN_PANEL_TOKEN` из `.env` резолвился в
`config.ADMIN_IDS[0]`, то есть КТО БЫ ни сел за компьютер студии, он работал правами первого
владельца. Роли (`app/roles.py`) в настольной админке из-за этого были декоративны: наёмный
оператор видел выручку, цены и кнопку удаления заказа. Для одной мастерской это удобство, для
продажи чужой студии — дыра, из-за которой не имеют смысла ни роли, ни лицензии.

КАК УСТРОЕН ВХОД. Личность берётся оттуда же, откуда её берёт вся остальная система, — из
Telegram: сотрудника заводит владелец на экране «Команда» по его user_id, и бот его уже знает.
Поэтому пароля здесь нет вовсе (лишний секрет, который придётся хранить и восстанавливать), а
есть одноразовый код:

    1. сотрудник пишет боту /desk — бот отвечает шестизначным кодом на 5 минут;
    2. сотрудник вводит код в настольной админке;
    3. сервер выдаёт токен сессии, дальше он едет в том же поле, что и прежний токен ПК.

Код бот отдаёт ТОЛЬКО сотруднику (`config.is_admin`), а обменять его на сессию можно ТОЛЬКО
запросом с этой машины — как и прежний токен. Значит перехваченный в переписке код чужому
человеку бесполезен: он не сидит за компьютером мастерской.

ПОЧЕМУ КЭШ В ПАМЯТИ. `webauth._verify_user` — синхронная функция на горячем пути каждого
запроса, а база у нас асинхронная. Ходить в неё оттуда нельзя, поэтому сессии живут словарём в
памяти (как роли в `config.STAFF_ROLES`), а таблица нужна, чтобы вход переживал перезапуск.
Расхождение невозможно по построению: пишем всегда в оба места и только через этот модуль.

ЧЕГО ЗДЕСЬ НЕТ НАМЕРЕННО. Токен не хранится ни в базе, ни в логах — только его sha256. Утечка
файла базы не даёт войти. И сам токен не заменяет `.env`-токен, а живёт рядом: режим входа
переключает настройка `desk_login_mode`, и по умолчанию она оставляет прежнее поведение —
иначе обновление кода заперло бы владельца снаружи его собственной мастерской.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time

from . import config, db

log = logging.getLogger(__name__)

CODE_TTL_SEC = 5 * 60          # код живёт 5 минут: успеть перепечатать с телефона, не больше
SESSION_TTL_SEC = 30 * 86400   # сессия — месяц; каждое обращение продлевает её
_CODE_ATTEMPT_LIMIT = 10       # неудачных попыток за минуту (6 цифр перебирать нечем, но всё же)
_CODE_ATTEMPT_WINDOW = 60

# sha256(токен) → (user_id, expires_at). Сам токен не хранится нигде.
_SESSIONS: dict[str, tuple[int, int]] = {}
# user_id → как показывать человека в интерфейсе (из строки staff; у корневых владельцев пусто)
_NAMES: dict[int, str] = {}
_attempts: list[float] = []


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def mode() -> str:
    """'token' — прежний вход по .env-токену разрешён, 'personal' — только личный вход.

    Читается из кэша настроек (`config.DESK_LOGIN_MODE`), потому что спрашивают её из
    синхронного `_verify_user`. Обновляется `refresh()`."""
    return config.DESK_LOGIN_MODE or "token"


def user_for_token(token: str) -> int | None:
    """user_id по токену сессии — или None. Синхронно: зовётся из `webauth._verify_user`.

    Просроченную сессию из памяти выкидываем сразу, из базы её уберёт `prune()`: держать
    протухший токен «до уборки» значило бы пускать по нему."""
    if not token:
        return None
    h = _hash(token)
    rec = _SESSIONS.get(h)
    if not rec:
        return None
    uid, exp = rec
    if exp and exp < int(time.time()):
        _SESSIONS.pop(h, None)
        return None
    return uid


def name_of(user_id: int) -> str:
    return _NAMES.get(int(user_id), "")


async def refresh() -> None:
    """Поднять сессии и имена из базы в память. Зовётся на старте и после правок команды."""
    try:
        rows = await db.list_desk_sessions()
    except Exception:  # noqa: BLE001
        log.warning("не удалось прочитать сессии настольной админки", exc_info=True)
        return
    now = int(time.time())
    _SESSIONS.clear()
    for r in rows:
        if int(r["expires_at"]) > now:
            _SESSIONS[r["token"]] = (int(r["user_id"]), int(r["expires_at"]))
    _NAMES.clear()
    for s in await db.list_staff():
        _NAMES[int(s["user_id"])] = (s.get("name") or "").strip()
    config.DESK_LOGIN_MODE = (await db.get_text_setting("desk_login_mode")) or "token"


async def issue_code(user_id: int) -> str:
    """Одноразовый код для входа. Прежние коды этого человека гасим: их всё равно не помнят,
    а живой второй код — это лишняя дверь."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = int(time.time())
    await db.drop_desk_codes(int(user_id))
    await db.add_desk_code(code, int(user_id), now, now + CODE_TTL_SEC)
    return code


def _rate_limited() -> bool:
    now = time.time()
    _attempts[:] = [t for t in _attempts if now - t < _CODE_ATTEMPT_WINDOW]
    return len(_attempts) >= _CODE_ATTEMPT_LIMIT


async def redeem_code(code: str, *, device: str = "") -> tuple[str, int] | None:
    """Обменять код на токен сессии: (токен, user_id) или None.

    Код одноразовый и удаляется в момент обмена — повторный ввод того же кода уже ничего не
    откроет. Проверка прав НЕ здесь: человек мог быть сотрудником в момент выдачи кода и
    перестать им быть — за это отвечает `roles.can()` на каждом запросе."""
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6 or _rate_limited():
        _attempts.append(time.time())
        return None
    row = await db.take_desk_code(code, int(time.time()))
    if not row:
        _attempts.append(time.time())
        return None
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    exp = now + SESSION_TTL_SEC
    uid = int(row["user_id"])
    await db.add_desk_session(_hash(token), uid, (device or "")[:80], now, exp)
    _SESSIONS[_hash(token)] = (uid, exp)
    log.info("настольная админка: вход сотрудника id %s", uid)
    return token, uid


async def touch(token: str) -> None:
    """Продлить сессию (раз в сутки, не на каждый запрос — иначе запись в базу на каждый клик)."""
    h = _hash(token)
    rec = _SESSIONS.get(h)
    if not rec:
        return
    uid, exp = rec
    now = int(time.time())
    if exp - now > SESSION_TTL_SEC - 86400:
        return
    new_exp = now + SESSION_TTL_SEC
    _SESSIONS[h] = (uid, new_exp)
    await db.touch_desk_session(h, now, new_exp)


async def revoke(token: str) -> None:
    h = _hash(token)
    _SESSIONS.pop(h, None)
    await db.drop_desk_session(h)


async def revoke_user(user_id: int) -> int:
    """Выкинуть все сессии человека — при снятии с должности или по кнопке «Выйти везде».

    Это ЕДИНСТВЕННЫЙ способ отобрать доступ у уволенного: снять роль мало, если у него на
    машине студии осталась живая сессия."""
    uid = int(user_id)
    for h, (u, _e) in list(_SESSIONS.items()):
        if u == uid:
            _SESSIONS.pop(h, None)
    return await db.drop_desk_sessions_of(uid)


def sessions_of(user_id: int) -> int:
    uid = int(user_id)
    return sum(1 for u, _e in _SESSIONS.values() if u == uid)


async def prune() -> None:
    """Убрать протухшие сессии и коды из базы. Память чистится сама в `user_for_token`."""
    now = int(time.time())
    await db.prune_desk_auth(now)
    for h, (_u, exp) in list(_SESSIONS.items()):
        if exp < now:
            _SESSIONS.pop(h, None)
