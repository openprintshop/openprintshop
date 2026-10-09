"""Раздача файлов мини-аппа и настольной админки: /app/…, /favicon.ico.

Один предмет: статика фронта из app/webui — index.html с серверной подстановкой бренда,
версии и возможностей сборки (A3, #cachebust, f98aa1ef), остальные файлы с no-cache,
иконка для корня домена. Деск (webui/desk) отдаётся только локальным запросам.

Выделено из app/webapp.py по вопросу 5 endgame-плана (решение владельца 22.08.2026:
«можно расплетать»). До этого обе ручки были ЗАМЫКАНИЯМИ внутри make_app — единственная
правка тел: константы `_WEBUI`/`_DESK`/`_FAVICON` стали модульными, тела сдвинуты на уровень
влево. Регистрация маршрутов осталась в make_app строками на фасадные имена.
"""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path

from aiohttp import web

from .. import brand, config, db, edition
from ..webauth import _request_is_local

log = logging.getLogger(__name__)

_WEBUI = Path(__file__).parent.parent / "webui"
_DESK = (_WEBUI / "desk").resolve()   # настольная админка (этап 2 редизайна ПК-админки)


async def _ui_file(request: web.Request) -> web.StreamResponse:
    rel = request.match_info.get("path", "") or "index.html"
    full = (_WEBUI / rel).resolve()
    # защита от выхода за пределы папки
    if not str(full).startswith(str(_WEBUI.resolve())):
        raise web.HTTPNotFound()
    # каталог → index.html внутри него (/app/desk/ → webui/desk/index.html)
    if full.is_dir():
        full = full / "index.html"
    if not full.is_file():
        raise web.HTTPNotFound()
    # Настольная админка — только с этой машины. Порт 8081 опубликован наружу туннелем,
    # а её токен и так принимается лишь локально (_verify_user): наружу светить нечего.
    if str(full).startswith(str(_DESK)) and not _request_is_local(request):
        raise web.HTTPNotFound()
    # Telegram-вебвью (особенно desktop/WebView2) агрессивно кэширует мини-апп: без этих
    # заголовков клиент держит старый index.html и не видит обновлений (#cachebust).
    nocache = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
               "Pragma": "no-cache", "Expires": "0"}
    # index.html отдаём с инжектом текущей версии фронта: даже копия из кеша знает свою
    # версию и сверяет её с /api/version → сама перезагружается, если устарела.
    if full.name == "index.html":
        # Бренд подставляем НА СЕРВЕРЕ, а не запросом из мини-аппа: иначе первый кадр
        # успевал бы показать наше имя и оранжевый акцент, и клиент видел бы моргание
        # чужого бренда поверх своего (A3, white-label).
        try:
            ts = await db.get_text_settings()
        except Exception:  # noqa: BLE001
            # Отдать страницу важнее, чем отдать её с чужим брендом: на секундном
            # сбое БД мини-апп обязан открыться (пусть и с нашими дефолтами), а не
            # встретить клиента пятисоткой.
            log.warning("brand: настройки недоступны, отдаём дефолтный бренд", exc_info=True)
            ts = {}
        b = brand.resolve(ts)
        # Возможности сборки (f98aa1ef) вшиваем той же подстановкой и по той же причине:
        # сайдбар рисуется до первого запроса, и раздел выключенной фичи иначе мигнул бы.
        # Флаги — по ВСЕМУ реестру: личного модуля нет в сборке → явный False, а не пропуск.
        feats = json.dumps(edition.flags(),
                           ensure_ascii=False).replace("<", "\u003c")
        page = (full.read_text(encoding="utf-8")
                .replace("%%VER%%", config.webui_version())
                .replace("%%BRAND_JSON%%", brand.to_script_json(b))
                .replace("%%FEATURES_JSON%%", feats)
                .replace("%%BRAND_TITLE%%", html.escape(brand.page_title(b)))
                .replace("%%BRAND_NAME%%", html.escape(b["title"])))
        return web.Response(text=page, content_type="text/html", headers=nocache)
    resp = web.FileResponse(full)
    for _k, _v in nocache.items():
        resp.headers[_k] = _v
    return resp


# favicon: браузеры дёргают /favicon.ico от корня домена, минуя /app (аудит 07.2026:
# 53×404 за 40 ч). Отдаём иконку из webui/, чтобы убрать шум в access-логе.
_FAVICON = _WEBUI / "favicon.ico"


async def _favicon(request: web.Request) -> web.StreamResponse:
    if _FAVICON.is_file():
        return web.FileResponse(_FAVICON, headers={"Cache-Control": "max-age=604800"})
    raise web.HTTPNotFound()
