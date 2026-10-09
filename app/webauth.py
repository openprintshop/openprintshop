"""Аутентификация веб-слоя: подпись Telegram initData, токен ПК-админки, локальность запроса.

Выделено из app/webapp.py волной 4 цикла 2. Тела функций перенесены ДОСЛОВНО, кроме одного
осознанного слияния — см. `is_local_request` ниже. Модуль зависит только от app/config.py и
aiohttp: webapp он не импортирует, поэтому цикла нет.

ПОЧЕМУ `_REQ_IS_LOCAL` ЖИВЁТ ИМЕННО ЗДЕСЬ И ТОЛЬКО В ОДНОМ ЭКЗЕМПЛЯРЕ. Это contextvars.ContextVar:
`_local_mw` в него ПИШЕТ на входе каждого запроса, `_verify_user` из него ЧИТАЕТ, решая, принимать
ли токен ПК-админки. Переехать должен САМ ОБЪЕКТ, а не значение: заведи здесь вторую переменную —
и запись пойдёт в одну, чтение из другой. Отказ будет БЕЗМОЛВНЫМ (ни исключения, ни строчки в
логе): настольная админка просто перестанет пускать владельца, отдавая 403 на всё подряд.
app/webapp.py реэкспортирует имя, а не копирует значение, — см. фасад там же.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import time
import urllib.parse

from aiohttp import web

from . import config, desklogin

_AUTH_TTL = 24 * 3600   # initData считаем валидным сутки

# Запрос пришёл с этой же машины (ставит _local_mw). Токен ПК-админки принимаем ТОЛЬКО
# для таких запросов: cloudflared-тоннель проксирует внешний трафик на localhost, поэтому
# сам по себе request.remote == 127.0.0.1 ничего не доказывает — но тоннель всегда добавляет
# заголовки прокси (Cf-Connecting-Ip / X-Forwarded-For), а у честного локального запроса их
# нет. Дефолт False: вне HTTP-запроса (или без middleware) токен не работает.
_REQ_IS_LOCAL: contextvars.ContextVar[bool] = contextvars.ContextVar("req_is_local", default=False)


def is_local_request(request) -> bool:
    """Запрос сделан С ЭТОЙ машины напрямую — единственная проверка локальности веб-слоя.

    Слито волной 4 из двух прежних предикатов по правилу «и то, и другое»: `_request_is_local`
    смотрел peer + заголовки прокси, `_local_request` — peer + Host, и каждый пропускал то, что
    ловил другой. Теперь обязательны все три условия сразу:
      * peer — петлевой адрес: отсекает обычного внешнего клиента;
      * нет заголовков прокси: cloudflared/tailscale коннектятся С 127.0.0.1, но добавляют
        Cf-Connecting-Ip / X-Forwarded-For;
      * Host — локальный: туннель мог и не проставить заголовок прокси, а публичное имя в Host
        остаётся (`curl -H "Host: example.com" http://127.0.0.1:8081/...` — так и вскрыли дыру
        в /api/lcd/data).

    Штатные лаунчеры ужесточение не отрезает — оба ходят на `http://127.0.0.1:8081/`, то есть
    несут Host «127.0.0.1»: admin_panel.py:23 (URL_BASE) и desktop_admin/shell.py:32,37,38
    (BASE_URL, /api/admin/events, /api/admin/hub).

    Атрибуты запроса читаются напрямую, БЕЗ getattr-заглушек (кроме `remote`, как было
    исторически): у настоящего aiohttp-запроса host и headers есть всегда, а фейк, где их нет,
    обязан падать громко, а не тихо получать False от предиката безопасности.
    """
    host = (request.host or "").split(":")[0].lower()
    return (getattr(request, "remote", None) in ("127.0.0.1", "::1")
            and "X-Forwarded-For" not in request.headers
            and "Cf-Connecting-Ip" not in request.headers
            and host in ("127.0.0.1", "localhost", "::1"))


def _request_is_local(request) -> bool:
    """Прежнее имя (app/webapp.py:601 до волны 4) — однострочный псевдоним is_local_request."""
    return is_local_request(request)


def _local_request(request: web.Request) -> bool:
    """Прежнее имя (app/webapp.py:968 до волны 4) — однострочный псевдоним is_local_request."""
    return is_local_request(request)


def _verify_user(init_data: str) -> dict | None:
    """Проверка подписи Telegram initData токеном бота → словарь user (или None).

    Три источника личности, все — в одном поле `init_data`, потому что фронт возит его одинаково:

    1. ЛИЧНАЯ СЕССИЯ настольной админки (app/desklogin.py) — сотрудник вошёл кодом из бота, и
       запрос идёт ОТ НЕГО, со своей ролью. Проверяется первой: у неё есть конкретный человек,
       а у .env-токена — только «кто-то за этим компьютером».
    2. ПК-режим админки (OpenTodo e075a6f3): секретный токен из .env вместо initData —
       приложение в обычном браузере работает как аккаунт ПЕРВОГО админа (лаунчер
       admin_panel.py, токен фронт хранит в localStorage). Это и есть та дверь, из-за которой
       роли в деске были декоративны, поэтому настройка `desk_login_mode = personal` её
       закрывает; по умолчанию она открыта — обновление кода не имеет права запереть владельца.
    3. Подпись Telegram — единственный способ снаружи.

    Оба локальных способа действуют ТОЛЬКО для запросов с этой же машины (_REQ_IS_LOCAL)."""
    if init_data and _REQ_IS_LOCAL.get():
        uid = desklogin.user_for_token(str(init_data))
        if uid:
            name = desklogin.name_of(uid)
            return {"id": int(uid), "username": "desk", "first_name": name or "Сотрудник"}
    tok = config.ADMIN_PANEL_TOKEN
    if (tok and init_data and config.ADMIN_IDS
            and _REQ_IS_LOCAL.get()
            and desklogin.mode() != "personal"
            and hmac.compare_digest(str(init_data), tok)):
        return {"id": int(config.ADMIN_IDS[0]), "username": "admin_pc",
                "first_name": "Мастер (ПК)"}
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash", "")
        if not received or not config.BOT_TOKEN:
            return None
        check = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
        secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if int(time.time()) - int(parsed.get("auth_date", "0")) > _AUTH_TTL:
            return None
        u = json.loads(parsed.get("user", "{}"))
        return u if u.get("id") else None
    except Exception:  # noqa: BLE001
        return None


def _verify_init_data(init_data: str) -> int | None:
    u = _verify_user(init_data)
    return int(u["id"]) if u else None


def _verify_admin(init_data: str) -> int | None:
    """uid, если подпись валидна И пользователь — админ; иначе None (для админ-эндпоинтов)."""
    uid = _verify_init_data(init_data)
    return uid if (uid and config.is_admin(uid)) else None


def _admin_body(body: dict):
    """(uid, error_response|None) для POST-админ-эндпоинтов: проверка подписи + прав админа."""
    uid = _verify_admin(body.get("initData", ""))
    if not uid:
        return None, web.json_response({"ok": False, "error": "Доступ только администратору"}, status=403)
    return uid, None


def _admin_forbidden() -> web.Response:
    return web.json_response({"ok": False, "error": "Доступ только администратору"}, status=403)


@web.middleware
async def _local_mw(request: web.Request, handler):
    """Помечает запросы с этой же машины для токен-доступа ПК-админки (_REQ_IS_LOCAL):
    см. комментарий у contextvar. Первый в цепочке — метка нужна любому хендлеру."""
    _REQ_IS_LOCAL.set(_request_is_local(request))
    return await handler(request)
