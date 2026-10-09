"""Гигиена списка заказов: снять тестовые, отметить все просмотренными, удалить заказ.

Один предмет: разметка и НЕОБРАТИМОЕ удаление СПИСКА заказов (в отличие от orderdesk —
чтения и orderbench — работы с файлом конкретного заказа). Права разные и заморожены
тестом: зачистка тестовых и удаление заказа держатся на «закрыто по умолчанию» (наёмному
оператору их не отдаём), а «отметить все просмотренными» — производственная кнопка, она у
оператора ЕСТЬ явной строкой CAP_ORDERS в webguard.

Выделено из app/webapp.py волной 7 endgame-плана (вторая половина, app/web/orderlist.py по
плану). Регистрация маршрутов осталась в make_app строками на фасадные имена. Тела
перенесены ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import logging

from aiohttp import web

from .. import config, db
from ..webauth import _admin_body
from .common import _bad, _toint

log = logging.getLogger(__name__)

def _cleanup_request_files(r) -> None:
    """Подчистить файлы, принадлежащие ТОЛЬКО этому заказу: фото результата и фото
    индивидуального заказа. Модель/обычное превью не трогаем — они общие с коллекцией.
    Зеркало handlers.admin._cleanup_request_files (логика одна на обе админки)."""
    paths: list[str] = []
    if getattr(r, "done_photo", None):
        paths.append(r.done_photo)
    if getattr(r, "slicer_shot", None):          # скрин из слайсера к счёту (OpenTodo 6aaa6a19)
        paths.append(r.slicer_shot)
    if getattr(r, "timelapse", None):            # таймлапс печати (OpenTodo 70b5a590)
        paths.append(r.timelapse)
    if getattr(r, "is_custom", 0):
        paths += list(getattr(r, "photos", []) or [])
    for p in paths:
        try:
            # C2: значение из БД разворачиваем в абсолютное — относительный путь иначе
            # разрешился бы от каталога процесса (корня репозитория) и удалил бы
            # постороннее. Именно abs_data и есть предохранитель: относительное значение
            # всегда указывает ВНУТРЬ DATA_DIR и никуда больше.
            config.abs_data(p).unlink(missing_ok=True)
        except OSError:
            log.debug("cleanup request file failed: %s", p, exc_info=True)

async def _api_admin_orders_clean_test(request: web.Request) -> web.Response:
    """Массовое удаление тестовых заказов (без цены и с 0 г / 0 мин) — зеркало a:reqcleanc."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    deleted = await db.delete_test_requests()
    for r in deleted:
        _cleanup_request_files(r)
    return web.json_response({"ok": True, "deleted": len(deleted)})

async def _api_admin_orders_mark_all_seen(request: web.Request) -> web.Response:
    """Сбросить все непросмотренные заказы в «просмотрено» (OpenTodo 040525a2) — гасит счётчик
    непросмотренных на LCD-дашборде одной кнопкой из админки."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    marked = await db.mark_all_requests_seen()
    return web.json_response({"ok": True, "marked": marked})

async def _api_admin_order_delete(request: web.Request) -> web.Response:
    """Необратимое удаление одного заказа с подчисткой файлов — зеркало a:rdelc."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    r = await db.delete_request(_toint(body.get("id")))
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"}, status=404)
    _cleanup_request_files(r)
    return web.json_response({"ok": True})
