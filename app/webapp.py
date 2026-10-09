"""Мини-апка Telegram: коллекция моделей как прокручиваемая веб-страница.

aiohttp-сервер живёт в одном процессе с ботом. Страница берёт текущую коллекцию из БД и
рисует сетку карточек (preview + имя + габариты). Есть поиск по названию (на клиенте).

Выбор модели уходит на POST /pick вместе с Telegram initData (подписан токеном бота).
Сервер проверяет подпись, достаёт user_id и запускает расчёт через бота — это работает при
ЛЮБОМ способе запуска мини-апки (кнопка-меню ☰, inline-кнопка, reply-кнопка).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

from aiohttp import web

from . import (
    avatars,
    backup,
    cc2spools,
    config,
    db,
    delivery,
    desklogin,
    edition,
    errorlog,
    gamify,
    keyboards,
    license,
    materials,
    notify,
    optional,
    orderchat,
    preview,
    pricing,
    ratelimit,
    roles,
    selfcheck,
    selfupdate,
    texts,
    tuning,
    tunnel,
    version,
    weblog,
)

log = logging.getLogger(__name__)

# Пул аккаунтов MakerWorld — личный модуль: в открытой версии проекта его нет (app/optional.py).
mwtoken = optional.load("mwtoken")


def _mw_status() -> dict:
    """Сводка пула MakerWorld для админки; без модуля — та же форма, что у пустого пула."""
    if mwtoken is None:
        return {"configured": False, "total": 0, "ready": 0, "usable": 0}
    return mwtoken.status()


_bot = None        # экземпляр Bot (для запуска расчёта)
_storage = None    # FSM-хранилище диспетчера (общее)
# _AUTH_TTL уехал в app/webauth.py вместе с _verify_user (волна 4 цикла 2) — см. фасад ниже.

# Сессии загруженных моделей (_models) и регистрация токенов уехали в app/web/registry.py
# (волна 14 endgame) — реэкспорт в фасадном блоке registry ниже по файлу.
# Порог «тяжёлого» меша (_REFINE_MAX_BYTES) уехал в app/web/clientflow.py (волна 15
# endgame) — реэкспорт в фасадном блоке clientflow ниже по файлу.

# 3D-просмотр модели в мини-аппе (B1): константы бюджета уехали в app/web/mesh3d.py
# (волна 9 endgame) — реэкспорт в фасадном блоке mesh3d ниже по файлу; историю порога
# (30 МБ эры сырого файла → 384 МБ после «Флекси Дарт Вейдера») см. в докстрингах там же.
# Мёртвые _MODEL3D_MIME/_model3d_stl_bytes/_nozzle_of_process удалены 21.08 по санкции
# владельца (вопрос 2 endgame-плана закрыт).

# Указатель активной печати (_active_print и его функции) уехал в app/web/printerhub.py
# (волна 10 endgame) — реэкспорт в фасадном блоке ниже по файлу.

# Веер держателей бота (волна 12 endgame, вариант «б» вопроса №1): модули app/web/*, чьи
# ручки шлют сообщения клиенту, держат СВОЙ модульный `_bot` — фасадом его реэкспортировать
# нельзя (имя пере-присваивается через global, вторая привязка застыла бы на None). Бота им
# раздаёт set_bot ниже, а зовёт его только webapp.start() под WEBAPP_ENABLED — поэтому
# run.wire_bot() этих модулей НЕ касается и поведение студии с выключенной мини-аппой
# сохраняется байт в байт. Полноту веера сторожит tests/test_bot_wiring.py: модуль app.web.*
# с модульным `_bot`, забытый в этом кортеже, роняет тест, а не тишину у клиента.
# Наполняется ПЕРЕД make_app (ниже по файлу): члены веера появляются фасадными импортами
# по местам вырезов, и здесь их имён ещё нет. set_bot читает кортеж в момент вызова.
_BOT_FAN: tuple = ()


def set_bot(bot):
    """Поставить экземпляр Bot веб-слою (как notify.set_bot) и вернуть ПРЕДЫДУЩИЙ.

    Возврат прошлого значения нужен тестам: подменили бота на заглушку — по прежнему
    значению состояние восстанавливается, и следующий тест не получает чужую заглушку.
    Раздаёт бота и всем модулям веера _BOT_FAN — прежнее значение у них то же самое:
    кроме этой функции, их `_bot` не ставит никто."""
    global _bot
    prev, _bot = _bot, bot
    for _m in _BOT_FAN:
        _m.set_bot(bot)
    return prev


# Подпись ссылок уехала в app/websign.py (волна 4 цикла 2). Здесь остаётся ФАСАД — ЯВНОЕ
# перечисление, по строке на имя: `from .websign import *` приватные имена не берёт, а `__all__`
# в этом модуле нет вовсе. Реэкспорт держит весь код и тесты, которые знают эти имена как
# webapp.*; забытое имя отказывает не на старте, а в момент вызова у живого клиента.
# `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ (автофикс молча сносит реэкспорт) — гасим
# построчно через noqa. Секрет и алгоритм подписи не трогать: ссылки разосланы клиентам.
from .websign import _file_ver  # noqa: E402,F401,I001
from .websign import _sign  # noqa: E402,F401,I001

# Общие помощники веб-слоя уехали в app/web/common.py (волна 4 цикла 2) — ФАСАД по тем же
# правилам, что у websign выше: явное перечисление, по строке на имя, noqa построчно.
# Вместе с ними там появились check_sig/signed_url/sig — единственное место, где префикс
# подписи стоит рядом с путём ручки. Префиксы не переписывать: ссылки уже у клиентов.
from .web.common import _ago  # noqa: E402,F401,I001
from .web.common import _avatar_url  # noqa: E402,F401,I001
from .web.common import _bad  # noqa: E402,F401,I001
from .web.common import _color_css  # noqa: E402,F401,I001
from .web.common import _fnum  # noqa: E402,F401,I001
from .web.common import _img_headers  # noqa: E402,F401,I001
from .web.common import _maybe_webp  # noqa: E402,F401,I001
from .web.common import _num  # noqa: E402,F401,I001
from .web.common import _photo_url  # noqa: E402,F401,I001
from .web.common import _print_settings_view  # noqa: E402,F401,I001
from .web.common import _product_photo_url  # noqa: E402,F401,I001
from .web.common import _served_img  # noqa: E402,F401,I001
from .web.common import _spool_photo_path  # noqa: E402,F401,I001
from .web.common import _to_webp  # noqa: E402,F401,I001
from .web.common import _toint  # noqa: E402,F401,I001
from .web.common import check_sig  # noqa: E402,F401,I001
from .web.common import sig  # noqa: E402,F401,I001
from .web.common import signed_url  # noqa: E402,F401,I001


# _THUMB_MAX и _thumb_proxy уехали в app/web/storefront.py (волна 8 endgame) — фасад ниже.

# Указатель активной печати уехал в app/web/printerhub.py (волна 10 endgame): словарь
# _active_print реэкспортируется КАК ОБЪЕКТ (мутируется, не пере-присваивается), функции —
# фасадом. ФАСАД по общим правилам; автофикс ruff не запускать.
from .web.printerhub import _active_print  # noqa: E402,F401,I001
from .web.printerhub import _active_print_header  # noqa: E402,F401,I001
from .web.printerhub import _active_print_key  # noqa: E402,F401,I001
from .web.printerhub import _request_printer_id  # noqa: E402,F401,I001
from .web.printerhub import _set_active_print  # noqa: E402,F401,I001


# Витрина (страницы коллекции/каталога, картинки) уехала в app/web/storefront.py
# (волна 8 endgame) — фасад в блоке storefront ниже по файлу.


async def _pick_plastic(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    user_id = _verify_init_data(body.get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    try:
        pid = int(body.get("plastic_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad id"}, status=400)
    p = await db.get_plastic(pid)
    if not p or not p.is_active:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if _bot is None or _storage is None:
        return web.json_response({"ok": False, "error": "bot offline"}, status=503)
    from .handlers.user_flow import select_plastic_from_webapp
    try:
        status = await select_plastic_from_webapp(_bot, _storage, user_id, p)
    except Exception:  # noqa: BLE001
        log.warning("webapp pickplastic failed", exc_info=True)
        return web.json_response({"ok": False, "error": "internal"}, status=500)
    return web.json_response({"ok": status == "ok", "status": status})


# Аутентификация веб-слоя уехала в app/webauth.py (волна 4 цикла 2). Здесь остаётся ФАСАД —
# ЯВНОЕ перечисление, по строке на имя: `from .webauth import *` приватные имена не берёт, а
# `__all__` в этом модуле нет вовсе. Реэкспорт держит и весь код ниже, и ~130 подмен в тестах,
# которые знают эти имена как webapp.*; забытое имя откажет не на старте, а в момент вызова
# у живого клиента. `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ (автофикс молча сносит
# реэкспорт) — гасим построчно через noqa.
#
# _REQ_IS_LOCAL реэкспортируется как САМ ОБЪЕКТ ContextVar, а не как снятое с него значение:
# _local_mw в него пишет, _verify_user читает. Второй экземпляр переменной означал бы, что
# настольная админка МОЛЧА (без исключения и без строки в логе) перестала пускать владельца.
#
# _request_is_local и _local_request после слияния предикатов — псевдонимы одной функции
# webauth.is_local_request (loopback И без заголовков прокси И локальный Host).
from .webauth import _admin_body  # noqa: E402,F401,I001
from .webauth import _admin_forbidden  # noqa: E402,F401,I001
from .webauth import _AUTH_TTL  # noqa: E402,F401,I001
from .webauth import _local_mw  # noqa: E402,F401,I001
from .webauth import _local_request  # noqa: E402,F401,I001
from .webauth import _REQ_IS_LOCAL  # noqa: E402,F401,I001
from .webauth import _request_is_local  # noqa: E402,F401,I001
from .webauth import _verify_admin  # noqa: E402,F401,I001
from .webauth import _verify_init_data  # noqa: E402,F401,I001
from .webauth import _verify_user  # noqa: E402,F401,I001
from .webauth import is_local_request  # noqa: E402,F401,I001


async def _pick(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    user_id = _verify_init_data(body.get("initData", ""))
    if not user_id:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    try:
        cid = int(body.get("collection_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad id"}, status=400)
    it = await db.get_collection_item(cid)
    if not it:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if _bot is None or _storage is None:
        return web.json_response({"ok": False, "error": "bot offline"}, status=503)
    from .handlers.user_flow import start_collection_from_webapp
    try:
        ok = await start_collection_from_webapp(_bot, _storage, user_id, it)
    except Exception:  # noqa: BLE001
        log.warning("webapp pick failed", exc_info=True)
        return web.json_response({"ok": False, "error": "internal"}, status=500)
    if not ok:
        return web.json_response({"ok": False, "error": "файлы недоступны"})
    return web.json_response({"ok": True})


async def _health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# Экраны студии (LCD-дашборд + TURZX) уехали в app/web/screens.py (волна 3 endgame-плана,
# после волны 2: данные принтера идут через printerhub._live_view). Здесь ФАСАД по общим
# правилам: явное перечисление, noqa построчно; автофикс ruff на блоке не запускать.
# Словари состояния (_lcd_cache, _lcd_prn) реэкспортируются КАК ОБЪЕКТЫ — они мутируются и
# никогда не пере-присваиваются, поэтому тесты чистят через фасад настоящее состояние.
# Плоские константы (_LCD_KEY и пр.) НЕ реэкспортируются намеренно: подмена значения через
# webapp перепривязала бы копию — тесты подменяют их в самом app/web/screens.py.
# Регистрация маршрутов осталась в make_app строками на фасадные имена: забытая строка
# фасада падает громко на старте, а не молча у клиента.
from .web import screens  # noqa: E402,F401,I001
from .web.screens import _api_admin_screen_power  # noqa: E402,F401,I001
from .web.screens import _api_lcd_data  # noqa: E402,F401,I001
from .web.screens import _api_screen_power  # noqa: E402,F401,I001
from .web.screens import _api_screen_status  # noqa: E402,F401,I001
from .web.screens import _first_hex  # noqa: E402,F401,I001
from .web.screens import _lcd_cache  # noqa: E402,F401,I001
from .web.screens import _lcd_cpu_temp  # noqa: E402,F401,I001
from .web.screens import _lcd_file  # noqa: E402,F401,I001
from .web.screens import _lcd_find_cpu_temp  # noqa: E402,F401,I001
from .web.screens import _lcd_gpu  # noqa: E402,F401,I001
from .web.screens import _lcd_prn  # noqa: E402,F401,I001
from .web.screens import _lcd_prn_for  # noqa: E402,F401,I001
from .web.screens import _lcd_queue  # noqa: E402,F401,I001
from .web.screens import _turzx_enabled  # noqa: E402,F401,I001
from .web.screens import lcd_print_handled  # noqa: E402,F401,I001
from .web.screens import reap_process  # noqa: E402,F401,I001

# ===================== Полный расчёт как мини-апка (/app) =====================
# Реестр сессий расчёта (_models и регистрация токенов) уехал в app/web/registry.py
# (волна 14 endgame). ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс
# ruff на блоке не запускать. Словарь _models реэкспортируется КАК ОБЪЕКТ: он мутируется и
# никогда не пере-присваивается (global здесь объявлен только у _bot/_runner/_storage),
# поэтому оставшиеся читатели (_api_use, _api_pdf, конвейер сметы) и conftest.clean_models
# работают с ТЕМ ЖЕ словарём. _MODELS_MAX — плоская константа: подменять в тестах в самом
# registry (setattr на webapp перепривязал бы копию).
from .web import registry  # noqa: E402,F401,I001
from .web.registry import _MODELS_MAX  # noqa: E402,F401,I001
from .web.registry import _make_token  # noqa: E402,F401,I001
from .web.registry import _models  # noqa: E402,F401,I001
from .web.registry import _register_full  # noqa: E402,F401,I001
from .web.registry import _register_model  # noqa: E402,F401,I001
from .web.registry import _remember_and_respond  # noqa: E402,F401,I001
from .web.registry import _remember_model  # noqa: E402,F401,I001


# 3D-артефакты (мета вьюера, сборка GLB, меш заказа) уехали в app/web/mesh3d.py (волна 9
# endgame-плана) вместе с константами бюджета — их читают оставшийся здесь /api/modelfile
# (уедет волной 15) и web/registry (_register_full). ФАСАД по общим правилам;
# ПОДМЕНА В ТЕСТАХ — в оба модуля (плоские константы перепривязываются по месту).
from .web import mesh3d  # noqa: E402,F401,I001
from .web.mesh3d import _GLB_FACE_CAP  # noqa: E402,F401,I001
from .web.mesh3d import _MODEL3D_EXTS  # noqa: E402,F401,I001
from .web.mesh3d import _MODEL3D_FACES  # noqa: E402,F401,I001
from .web.mesh3d import _MODEL3D_MAX_SRC_BYTES  # noqa: E402,F401,I001
from .web.mesh3d import _api_admin_orderfile  # noqa: E402,F401,I001
from .web.mesh3d import _model3d_glb_bytes  # noqa: E402,F401,I001
from .web.mesh3d import _model3d_meta  # noqa: E402,F401,I001


def _default_mw_plastic_color(plastics: list) -> tuple[int | None, str | None]:
    """Дефолтный пластик/цвет для открытия карточки товара: Elegoo Matte, цвет БЕЛЫЙ (OpenTodo
    b2507a91, решение владельца 24.07; прежний стандарт Тёмно-синий ae038968 — второй кандидат,
    если белого в палитре нет). Ищем по имени/цвету БЕЗ хардкода id — если катушку переименуют
    или удалят, фронт молча откатится на самый дешёвый пластик (cheapestPlastic, 050aff56).
    (None, None) — подходящей катушки/цвета нет."""
    def _norm(s: str) -> str:
        return (s or "").strip().lower().replace("ё", "е")
    cand = next((p for p in plastics
                if p.is_active and "elegoo" in _norm(p.name) and "matte" in _norm(p.name)), None)
    if not cand:
        return None, None
    def _match(*subs: str) -> str | None:
        for c in cand.color_list:
            nc = _norm(c)
            if any(s in nc for s in subs):
                return c
        return None
    white = _match("бел", "white")
    if white:
        return cand.id, white
    dark_blue = _match("темно-син", "темно син", "dark blue", "navy")
    if dark_blue:
        return cand.id, dark_blue
    return None, None


async def _delivery_options(uid: int) -> dict:
    """Блок доставки для мини-аппа: включена ли она, прайс типов и умолчания клиента.

    Один источник правды для трёх экранов (корзина, смета, профиль): проверять «а есть ли
    вообще доставка» каждому отдельным запросом — три лишних round-trip на старте. Ни одного
    ВКЛЮЧЁННОГО типа → enabled=false, и все три экрана молчат про доставку: мастерская
    работает на самовывоз, вопрос «куда везти» там лишний."""
    types = await db.list_delivery_types(active_only=True)
    prefs = await db.get_user_prefs(uid)
    tid = int(prefs.get("delivery_type_id") or 0)
    if tid and not any(int(t["id"]) == tid for t in types):
        tid = 0                      # тип выключили после того, как клиент его выбрал
    return {"enabled": bool(types),
            "types": [delivery.type_json(t) for t in types],
            "default_type_id": tid,
            "default_address": prefs.get("delivery_address") or ""}


async def _api_options(request: web.Request) -> web.Response:
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    return web.json_response(await build_options(uid))


async def build_options(uid: int) -> dict:
    """Опции мини-аппа для клиента uid (тело /api/options).

    Вынесено из ручки для снимка приёмной (Э2, app/relaylink.py): пока ПК выключен, мини-апп
    на домене берёт опции из снимка — без них он не рисуется вовсе («Загружаю…» навсегда).
    Для снимка зовётся с uid=0: у такого «клиента» нет ни заказов, ни избранного, ни роли —
    получаются обезличенные опции, одинаковые для всех."""
    printers = await db.list_printers(active_only=True)
    plastics = await db.list_plastics(active_only=True)
    quals = await db.list_quality(active_only=True)
    strs = await db.list_strength(active_only=True)
    pending = await db.pending_price_count(uid)
    models_count = await db.count_saved_models(uid)
    collection_count = await db.count_collection()
    orders_count = await db.count_user_requests(uid)
    overrides = await db.get_color_overrides()
    bot_username = ""
    if _bot is not None:
        try:
            bot_username = (await _bot.me()).username or ""
        except Exception:  # noqa: BLE001
            bot_username = ""
    referrals = await db.count_referrals(uid)
    # Остатки по цветам → клиенту отдаём только ДОСТУПНОСТЬ (не граммы): цвет с заведённым складом
    # на нуле СКРЫВАЕТСЯ из палитры (решение владельца 21.08: шарик должен ПРОПАДАТЬ, а не
    # гаситься — «погашенный» на экране сметы читался как доступный), 0<остаток≤порог = на
    # исходе. Незаведённые цвета — доступны: учёт по цветам может не вестись.
    # Единый остаток по цвету = legacy plastic_color_stock ∪ агрегат катушек (Фаза A мастер-плана):
    # раньше склад брался ТОЛЬКО из plastic_color_stock, поэтому пластик без пер-цветовых строк
    # (напр. id=11 Elegoo PLA Matte) ВСЕГДА показывался «в наличии». Теперь сигнал единый и
    # работает, как только по пластику заводят катушки. Возвращаем имена ИЗ ПАЛИТРЫ (чтобы фронт
    # сопоставил чипы), сравнивая по canon-цвету (регистр не важен).
    availmap = {p.id: await db.color_availability(p.id) for p in plastics}

    def _avail(p):
        a = availmap.get(p.id, {})
        out, low = [], []
        for c in p.color_list:
            info = a.get(db._canon_color(c))
            if not info:
                continue
            st = info.get("stock") or 0
            if st <= 0:
                out.append(c)
            elif st <= (info.get("low") or 0):
                low.append(c)
        return out, low

    availcache = {p.id: _avail(p) for p in plastics}
    # Видимая клиенту палитра: без цветов «на нуле». Пластик, у которого цвета заведены, но
    # доступных не осталось, из выдачи исчезает целиком — заказать его всё равно нечем.
    _vis = {p.id: [c for c in p.color_list if c not in set(availcache[p.id][0])]
            for p in plastics}
    plastics = [p for p in plastics if _vis[p.id] or not p.color_list]
    # «Повторить прошлый заказ» одной кнопкой на хабе (ux-simplification.md, идея 1):
    # последний заказ с моделью, которую реально можно перезапустить (хэш + сохранённая модель)
    last_order = None
    for r in await db.list_user_requests(uid, limit=10):
        if getattr(r, "hash", "") and not int(getattr(r, "is_custom", 0) or 0):
            sm = await db.get_saved_model_by_hash(uid, r.hash)
            if sm:
                last_order = {"id": r.id, "label": r.model_label}
                break
    # Дефолтный пластик/цвет карточки товара: Elegoo Matte, Белый (b2507a91; прежний стандарт
    # Тёмно-синий ae038968 — фолбэк). Не найден → None/None, фронт откатится на самый дешёвый (050aff56).
    def_plastic_id, def_color = _default_mw_plastic_color(plastics)
    # Срочность (B3): множитель для подписи «×K» на чипе и обещанные сроки (обычный/срочный) как
    # готовые «DD.MM» — фронт рисует их в чипах «к DD.MM» без клиентского счёта дат. urgent_k=0,
    # если наценка выключена (rush_surcharge_pct=0) → чип «Срочно» без числа (graceful degradation).
    _urg_settings = await db.get_settings()
    _urgent_k = pricing.urgency_factor_from("rush", _urg_settings)
    # Любимый цвет клиента (ux-paths-next.md, идея 4): последний выбранный цвет — первым в
    # палитре. Фронт применит его, только если у дефолтного пластика такой цвет есть (иначе —
    # первый цвет), поэтому подмена безопасна.
    try:
        _fav_color = await db.get_user_last_color(uid)
        if _fav_color:
            def_color = _fav_color
    except Exception:  # noqa: BLE001
        log.debug("fav color lookup failed", exc_info=True)
    return {
        "ok": True, "currency": config.CURRENCY, "pending_orders": pending,
        "orders_count": orders_count, "bot_username": bot_username, "referrals": referrals,
        "is_admin": config.is_admin(uid), "last_order": last_order,
        # Роль и права сотрудника (A4): по ним админка мини-аппа прячет разделы, которые
        # оператору недоступны. Для обычного клиента — пустая роль и пустой список прав.
        "me": roles.whoami(uid),
        # Доставка (A2): включена ли она вообще + прайс типов и умолчания клиента. Ни одного
        # типа не заведено → enabled=false, и клиент НЕ ВИДИТ ни выбора получения в корзине,
        # ни настройки адреса в профиле: мастерская работает только на самовывоз, и лишний
        # вопрос «куда везти» там только мешает. Цены здесь без привязки к сумме заказа —
        # точную стоимость под конкретную корзину даёт /api/delivery_types?total=…
        "delivery": await _delivery_options(uid),
        "models_count": models_count, "collection_count": collection_count,
        "default_plastic_id": def_plastic_id, "default_color": def_color,
        # Срочность (B3): только коэффициент подписи «×K». Конкретную дату «к DD.MM» на CONFIG-
        # экране больше НЕ показываем — срок теперь зависит от времени печати, а оно до расчёта
        # неизвестно (точный срок появляется в смете как result.term после расчёта).
        "urgent_k": round(_urgent_k, 2) if _urgent_k > 1.0 else 0,
        "rush_pct": _urg_settings.get("rush_surcharge_pct", 0.0),
        # Избранное (сердечки): фронт держит один набор id и рисует сердечки на любых
        # плитках (разделы каталога, поиск, лента) без пер-карточных флагов с сервера.
        "fav_ids": await db.list_favorite_ids(uid),
        # Возможности сборки (f98aa1ef): фронт не показывает то, чего в этой сборке нет.
        # Разделы, требующие личных инструментов владельца (поиск по каталогам, параметрика),
        # в билде студии просто не рисуются — иначе кнопка вела бы в 404.
        "features": {**edition.flags(),
                     "model_search": edition.enabled("model_search") and config.MODEL_SEARCH_ENABLED},
        "printers": [{"id": p.id, "name": p.name} for p in printers],
        "plastics": [{"id": p.id, "name": p.name, "type": p.material_type,
                      # клиенту — цена с коэффициентом сложности (сам коэффициент не светим)
                      "price": p.eff_price_per_kg, "density": p.density, "is_default": bool(p.is_default),
                      "description": p.description,
                      # категория витрины (3af25fc2); пустая (до бэкфилла) → «Обычный»
                      "category": p.category or materials.category_hint(p.material_type, p.name),
                      "best_quality_id": p.best_quality_id,   # лучший профиль печати (94cc52c7)
                      "colors": _vis[p.id], "color_css": [_color_css(c, overrides) for c in _vis[p.id]],
                      # цвета «на нуле» уже скрыты из colors; поле оставлено пустым для
                      # совместимости фронта (он умеет «гасить» — теперь гасить нечего)
                      "out_of_stock": [], "low_stock": availcache[p.id][1],
                      "photo": bool(p.photo_path and Path(p.photo_path).exists()),
                      # фото катушек по цветам (OpenTodo eb9a9efe): {цвет: url} только для
                      # цветов, у которых файл реально лежит в data/spool_photos/{id}/
                      "spools": {c: f"/spool/{p.id}?c={urllib.parse.quote(c)}&v={_file_ver(str(sp))}"
                                 for c in _vis[p.id]
                                 if (sp := _spool_photo_path(p.id, c)) is not None},
                      "pver": _file_ver(p.photo_path)} for p in plastics],
        # layer_height/nozzle — для ползунка высоты слоя (03.08.2026): фронт строит стопы
        # по активным пресетам, 0.30/0.36 помечаются соплом 0.6
        "qualities": [{"id": q.id, "label": q.label, "layer_height": q.layer_height,
                       "nozzle": db.nozzle_of(q),
                       "is_default": bool(q.is_default)} for q in quals],
        "strengths": [{"id": s.id, "label": s.label, "infill": s.infill, "walls": s.walls,
                       "is_default": bool(s.is_default)} for s in strs],
        # Опции постобработки (B4): активный каталог платных услуг мастера; пустой список —
        # владелец ничего не завёл, фронт секцию не рисует. per_part=1 (accee7a9): price — ставка
        # ЗА ДЕТАЛЬ, итог на смете клиент множит на детали модели (result.parts) × тираж.
        "options": [{"id": o.id, "name": o.name, "price": o.price, "per_part": int(o.per_part)}
                    for o in await db.list_postproc_options(active_only=True)],
    }


# _avatar_url уехал в app/web/common.py (волна 1b endgame-плана) — реэкспорт в фасадном
# блоке common в шапке файла.


async def _avatar_file(request: web.Request) -> web.StreamResponse:
    uid = request.match_info.get("uid", "")
    if not check_sig(request, "av", uid):
        raise web.HTTPNotFound()
    p = config.AVATAR_DIR / f"{_toint(uid)}.png"
    if p.exists():
        return web.FileResponse(p, headers=_img_headers(request, private=True))
    raise web.HTTPNotFound()


async def _api_profile(request: web.Request) -> web.Response:
    """Профиль клиента: уровень, XP, достижения (OpenTodo 7a5be39c) + аватарка (fca4246e).

    Профиль v2: + xp_floor/xp_next, реф-карточка, лента XP, признак «новых» достижений.
    `?seen=1` (шлёт экран профиля, но не чип в шапке) — отдать ответ и пометить все
    разблокированные достижения просмотренными: точка на чипе и теги NEW гаснут.
    """
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    stats = await db.user_profile_stats(uid)
    av = await db.get_user_avatar(uid)
    profile = gamify.build_profile(stats)
    seen = await db.seen_achievements(uid)
    unlocked_keys = [a["key"] for a in profile["achievements"] if a["unlocked"]]
    for a in profile["achievements"]:
        a["is_new"] = a["unlocked"] and a["key"] not in seen
    profile["unseen"] = sum(1 for a in profile["achievements"] if a["is_new"])
    xp_log = []
    for e in await db.xp_events(uid, 4):
        entry = gamify.xp_log_entry(e["kind"], e["ref"])
        if entry:
            xp_log.append({"reason": entry[0], "amount": entry[1], "at": _ago(e["at"])})
    bot_username = ""
    if _bot is not None:
        try:
            bot_username = (await _bot.me()).username or ""
        except Exception:  # noqa: BLE001
            bot_username = ""
    extra = {}
    if bot_username:
        extra["referral_link"] = f"https://t.me/{bot_username}?start=ref{uid}"
        extra["referral_reward"] = f"{config.REF_REWARD_AMOUNT:g} {config.CURRENCY} на счёт"
    extra["family_bonus"] = f"{config.FAMILY_OPENING_BONUS:g} {config.CURRENCY}"
    # Мастеру вместо баланса показываем «было бы потрачено» — сумма его самопечатей (мастер-план).
    would_spend = await db.admin_would_spend(uid) if config.is_admin(uid) else None
    # Семейный счёт (2026-07-16): общий баланс + история с подписями «чей заказ и номер».
    family = await db.get_family(uid)
    if family:
        family["tx"] = await db.list_family_tx(family["id"], 15)
    resp = web.json_response({"ok": True, "currency": config.CURRENCY,
                              "avatar_url": _avatar_url(uid),
                              "avatar_prompt": (av or {}).get("prompt") or "",
                              "balance": await db.get_balance(uid),
                              "balance_tx": await db.list_balance_tx(uid, 10),
                              "prefs": await db.get_user_prefs(uid),
                              "family": family,
                              "would_spend": would_spend,
                              "xp_log": xp_log, **extra, **profile})
    if request.query.get("seen") == "1":
        await db.mark_achievements_seen(uid, unlocked_keys)
    return resp


async def _local_avatar_gpu_refresh(request: web.Request) -> web.Response:
    """Перечитать файл-флаг ИИ-генерации аватарок (OpenTodo ac6c7e0d) — дёргают ярлыки
    «вкл/выкл ИИ» с рабочего стола после правки data/avatar_gpu.off. Без auth: состояние
    задаёт ТОЛЬКО файл на диске, эндпоинт лишь применяет его (выгрузка VRAM/прогрев);
    посторонний вызов ничего не меняет."""
    from . import avatars_gpu
    on = await asyncio.to_thread(avatars_gpu.refresh_from_flag)
    if on:
        asyncio.get_running_loop().create_task(
            asyncio.to_thread(avatars_gpu.generate_png, "warmup", 1))   # греем в фоне
    return web.json_response({"ok": True, "gpu_on": on})


# Очередь расчётов («мастер посчитает вручную») уехала в app/web/quotecalc.py (волна 13
# endgame) к счётчикам сметы. Бота модулю раздаёт webapp.set_bot веером _BOT_FAN
# (волна 12). ФАСАД по общим правилам, noqa построчно.
from .web.quotecalc import _api_admin_calc_queue  # noqa: E402,F401,I001
from .web.quotecalc import _api_admin_calc_queue_resolve  # noqa: E402,F401,I001
from .web.quotecalc import _api_calc_queue  # noqa: E402,F401,I001


async def _api_admin_journal(request: web.Request) -> web.Response:
    """Журнал печатей мастера (OpenTodo ed1518ed): список + сводка по пластикам/браку."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.list_print_journal()
    for it in items:
        it["ago"] = _ago(it["created_at"])
        # полный опрос (0f848906): фото результата + таймлапс печати у записи журнала
        it["photo"] = (_photo_url("journal", it["id"], it.get("done_photo"))
                       if it.get("done_photo") else "")
        # запись без таймлапса лечится из авто-кэша по имени задания (OpenTodo df00e8ff)
        tl = await _heal_journal_timelapse(it)
        it["timelapse_url"] = (signed_url("jtl", it["id"]) if tl else "")
        it["settings"] = await _print_settings_view(it)
    return web.json_response({"ok": True, "items": items,
                              "stats": await db.print_journal_stats()})


# _print_settings_view уехал в app/web/common.py (волна 4 цикла 2) — по той же причине, что
# и _photo_url ниже: его зовут и журнал печатей отсюда, и карточка печати кампании из
# app/web/calib.py, а общий дом обязан не импортировать ни того, ни другого.


async def _jtl_file(request: web.Request) -> web.StreamResponse:
    """Отдать таймлапс записи журнала по подписанному URL (0f848906)."""
    jid = _toint(request.match_info.get("jid"))
    if not check_sig(request, "jtl", jid):
        return web.Response(status=403)
    e = await db.get_print_journal(jid)
    path = e.get("timelapse") if e else None
    if not path or not Path(path).exists():
        return web.Response(status=404)
    return web.FileResponse(path)


async def _api_admin_journal_add(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    label = (body.get("label") or "").strip()
    if not label:
        return web.json_response({"ok": False, "error": "Что печатали?"})
    plastic = await db.get_plastic(_toint(body.get("plastic_id"))) if body.get("plastic_id") else None
    # ползунок брака 0–100 (60e0b2a8); старый бинарный result принимаем как фолбэк
    fail_pct = _num(body, "fail_pct", None)
    if fail_pct is None:
        fail_pct = 100.0 if (body.get("result") or "") == "fail" else 0.0
    # Катушка печати (Фаза B): если мастер выбрал катушку — списываем именно её (точный учёт), иначе
    # прежний путь по пластику. add_print_journal при spool_id пометит запись учтённой, поэтому
    # _consume_journal_stock ниже станет no-op (без двойного списания).
    spool_id = _toint(body.get("spool_id")) or None
    jid = await db.add_print_journal(
        label=label, plastic_id=plastic.id if plastic else None,
        plastic_name=plastic.name if plastic else (body.get("plastic_name") or ""),
        grams=_num(body, "grams", 0.0) or 0.0, minutes=_num(body, "minutes", 0.0) or 0.0,
        fail_pct=fail_pct, fail_reason=(body.get("fail_reason") or ""),
        note=(body.get("note") or ""), spool_id=spool_id)
    await _consume_journal_stock(jid)    # печать вне заказа списывает свой пластик (05806f15)
    return web.json_response({"ok": True, "id": jid})


async def _api_admin_journal_update(request: web.Request) -> web.Response:
    """Правка прошлой записи журнала печатей (OpenTodo 60e0b2a8)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    # Оценка из журнала приложения = печать обработана: раньше LCD-экран сбрасывал чат-опрос
    # качества, но он выпилен из Telegram (b02bc4c6) — сбрасываем здесь.
    lcd_print_handled()
    label = (body.get("label") or "").strip()
    if not label:
        return web.json_response({"ok": False, "error": "Что печатали?"})
    plastic = await db.get_plastic(_toint(body.get("plastic_id"))) if body.get("plastic_id") else None
    jid = _toint(body.get("id"))
    ok = await db.update_print_journal(
        jid, label=label, plastic_id=plastic.id if plastic else None,
        plastic_name=plastic.name if plastic else (body.get("plastic_name") or ""),
        grams=_num(body, "grams", 0.0) or 0.0, minutes=_num(body, "minutes", 0.0) or 0.0,
        fail_pct=_num(body, "fail_pct", 0.0) or 0.0,
        fail_reason=(body.get("fail_reason") or ""), note=(body.get("note") or ""))
    if not ok:
        return web.json_response({"ok": False, "error": "Запись не найдена"})
    # привязка к заказу (полный опрос, 0f848906): «вне заказа» = 0/пусто → отвязать
    if "request_id" in body:
        rid = _toint(body.get("request_id"))
        await db.set_print_journal_fields(jid, request_id=rid or None)
    # после фиксации пластика/граммов/заказа — печать вне заказа списывает свой пластик (05806f15)
    await _consume_journal_stock(jid)
    return web.json_response({"ok": True})


async def _api_admin_journal_delete(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    await db.delete_print_journal(_toint(body.get("id")))
    return web.json_response({"ok": True})


def _unlink_data_file(path) -> None:
    """Удалить файл, но ТОЛЬКО внутри data/ — чужой (внезапно подменённый) путь удалять
    нельзя. Значение из БД сперва разворачиваем в абсолютное (C2: в базе путь
    ОТНОСИТЕЛЬНЫЙ, и голый resolve() увёл бы его от текущего каталога процесса). Ошибки ФС
    молчаливо глотаем: поле в БД уже очищено, а осиротевший файл безвреден."""
    try:
        p = config.abs_data(path)
        if p.is_file() and config.under_data_dir(p):
            p.unlink()
    except OSError:
        log.debug("media unlink failed", exc_info=True)


async def _api_admin_journal_media_delete(request: web.Request) -> web.Response:
    """Удалить медиа печати (OpenTodo 1faab6cb): kind='photo' — скрин стола записи журнала,
    kind='timelapse' — её таймлапс. Таймлапс удаляется С СЛЕДОМ (tl_deleted=1) и вместе с
    авто-кэшем по имени задания — иначе самолечение (_heal_journal_timelapse) или свип
    printwatch воскресят видео. Копия таймлапса у привязанного заказа (requests.timelapse
    исторически дублирует журнальное видео, сверка по размеру файла — как в
    _order_prints_media) гасится заодно, чтобы клиент не продолжал его видеть."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    jid, kind = _toint(body.get("id")), (body.get("kind") or "").strip()
    e = await db.get_print_journal(jid)
    if not e:
        return web.json_response({"ok": False, "error": "Запись не найдена"})
    if kind == "photo":
        path = e.get("done_photo")
        await db.set_print_journal_fields(jid, done_photo=None)
        if path:
            _unlink_data_file(path)
        return web.json_response({"ok": True})
    if kind != "timelapse":
        return web.json_response({"ok": False, "error": "kind: photo|timelapse"})
    path = e.get("timelapse")
    size = _file_size(path)
    await db.set_print_journal_fields(jid, timelapse=None, tl_deleted=1)
    if path:
        _unlink_data_file(path)
    # авто-кэш живёт отдельным файлом, в подпапке своей машины (волна 5): без printer_id
    # удаление ролика мастером не нашло бы файл, и свип докачал бы его обратно
    cached = cached_timelapse_path(e.get("label") or "", e.get("printer_id"))
    if cached:
        _unlink_data_file(cached)
    rid = _toint(e.get("request_id"))
    if rid and size:
        r = await db.get_request(rid)
        rtl = getattr(r, "timelapse", "") if r else ""
        if rtl and _file_size(rtl) == size:
            await db.update_request(rid, timelapse=None)
            _unlink_data_file(rtl)
    return web.json_response({"ok": True})


async def _api_admin_order_media_delete(request: web.Request) -> web.Response:
    """Удалить медиа самого заказа (OpenTodo 1faab6cb): kind='photo' — фото результата
    (requests.done_photo, куда финиш печати копирует скрин стола), kind='timelapse' —
    привязанный к заказу таймлапс. Файл заказа всегда его собственная копия
    (done_<rid>.jpg / timelapses/<rid>.mp4), поэтому удаляем и файл; авто-кэш журнала
    не трогаем — им владеют записи журнала."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid, kind = _toint(body.get("id")), (body.get("kind") or "").strip()
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    if kind == "photo":
        path = getattr(r, "done_photo", "")
        await db.update_request(rid, done_photo=None)
        if path:
            _unlink_data_file(path)
        return web.json_response({"ok": True})
    if kind != "timelapse":
        return web.json_response({"ok": False, "error": "kind: photo|timelapse"})
    path = getattr(r, "timelapse", "")
    await db.update_request(rid, timelapse=None)
    # у заказа файл — копия в data/timelapses/<rid>.mp4; общий авто-кэш (timelapses/auto/)
    # оставляем журналу, иначе удаление копии заказа выбило бы видео у записи журнала
    if path:
        try:
            if Path(path).resolve().parent == _TIMELAPSE_DIR.resolve():
                _unlink_data_file(path)
        except OSError:
            pass
    return web.json_response({"ok": True})


# ── Калибровка филамента ───────────────────────────────────────────────────────────────────
# Блок уехал в app/web/calib.py (волна 4 цикла 2) целиком: 24 ручки, страница образца по QR
# и доменные помощники экрана. Маршруты объявлены там же, рядом с обработчиками, и включаются
# в make_app одной строкой `app.add_routes(calib.routes)` — потерять ручку при переносе нечем.
#
# Ниже — ФАСАД по тем же правилам, что у websign/web.common/webauth/webguard выше: ЯВНОЕ
# перечисление, по строке на имя. `from .web.calib import *` приватные имена не берёт, а
# `__all__` в этом модуле нет вовсе. Реэкспорт держит тесты и весь код, который знает эти
# имена как webapp.*; забытая строка отказывает не на старте, а в момент вызова у клиента.
# `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ — автофикс молча сносит реэкспорт.
from .web import calib  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_backfill  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_drift  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_export  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_labels  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_link  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_measure_add  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_measure_delete  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_passport_apply  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_passport_delete  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_passport_save  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_photo  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_plate_save  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_preset_history  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_preset_restore  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_presets  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_presets_snapshot  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_run  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_run_delete  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_run_save  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_sample_delete  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_sample_save  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_step_apply  # noqa: E402,F401,I001
from .web.calib import _api_admin_calib_step_save  # noqa: E402,F401,I001
from .web.calib import _calib_journal_view  # noqa: E402,F401,I001
from .web.calib import _calib_params  # noqa: E402,F401,I001
from .web.calib import _calib_preset_path  # noqa: E402,F401,I001
from .web.calib import _calib_refresh_passport  # noqa: E402,F401,I001
from .web.calib import _calib_step_view  # noqa: E402,F401,I001
from .web.calib import _calib_test_dicts  # noqa: E402,F401,I001
from .web.calib import _csample_page  # noqa: E402,F401,I001
from .web.calib import _sample_url  # noqa: E402,F401,I001


# Центр уведомлений админки и семейные счета уехали в app/web/notifhub.py (волна 13
# endgame). ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс ruff
# на блоке не запускать. Бота модулю раздаёт webapp.set_bot веером _BOT_FAN (волна 12).
from .web import notifhub  # noqa: E402,F401,I001
from .web.notifhub import _api_admin_families  # noqa: E402,F401,I001
from .web.notifhub import _api_admin_family_add  # noqa: E402,F401,I001
from .web.notifhub import _api_admin_family_create  # noqa: E402,F401,I001
from .web.notifhub import _api_admin_family_remove  # noqa: E402,F401,I001
from .web.notifhub import _api_admin_notification_action  # noqa: E402,F401,I001
from .web.notifhub import _api_admin_notifications  # noqa: E402,F401,I001
from .web.notifhub import _api_family_pick_partner  # noqa: E402,F401,I001
from .web.notifhub import _api_family_request  # noqa: E402,F401,I001
from .web.notifhub import _cust_display  # noqa: E402,F401,I001


async def _api_avatar_generate(request: web.Request) -> web.Response:
    """Сгенерировать новую аватарку профиля (локально): весёлый промт + картинка в стиле приложения."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"avatar:{uid}", 12, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    prev = ((await db.get_user_avatar(uid)) or {}).get("prompt") or ""   # анти-повтор существа
    try:
        # 120 с — потолок на ВСЮ генерацию (655318e9: при занятой игрой видеокарте запрос висел
        # 12–17 минут с вечным спиннером). Внутри есть свои защиты (guard занятости GPU, лок 10 с),
        # таймаут — последний рубеж: честная ошибка вместо бесконечной загрузки.
        res = await asyncio.wait_for(asyncio.to_thread(avatars.generate, uid, None, prev), 120)
    except TimeoutError:
        log.warning("Генерация аватарки превысила 120 с — отвечаю ошибкой (uid=%s)", uid)
        return web.json_response({"ok": False, "error": "ИИ сейчас занят (видеокарта загружена) — "
                                                        "попробуйте чуть позже"})
    if not res.get("ok"):
        return web.json_response({"ok": False, "error": res.get("error") or "Не удалось"})
    await db.set_user_avatar(uid, res["prompt"], res["seed"])
    await db.log_event(uid, "сгенерировал аватарку", f'«{res["prompt"][:80]}»')
    return web.json_response({"ok": True, "url": _avatar_url(uid), "prompt": res["prompt"]})


# Клиентский конвейер (загрузка/варианты/параметрика -> смета -> оформление -> карточка
# заказа -> свои модели) уехал в app/web/clientflow.py ОДНИМ КУСКОМ (волна 15 endgame;
# пары «обёртка/нутро» _refine_calc/_refine_calc_inner и _api_calc/_api_calc_inner не
# разрезаются). ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс ruff
# на блоке не запускать. _REFINE_MAX_BYTES — плоская константа: подменять в тестах в самом
# clientflow. Бота модулю раздаёт webapp.set_bot веером _BOT_FAN (волна 12).
# quick_link_quote и place_order_from_chat зовут handlers/user_flow.py как webapp.* —
# фасад для них не роскошь, а публичный вход из Telegram.
from .web import clientflow  # noqa: E402,F401,I001
from .web.clientflow import _REFINE_MAX_BYTES  # noqa: E402,F401,I001
from .web.clientflow import _api_calc  # noqa: E402,F401,I001
from .web.clientflow import _api_calc_inner  # noqa: E402,F401,I001
from .web.clientflow import _api_calc_notify  # noqa: E402,F401,I001
from .web.clientflow import _api_cart_sync  # noqa: E402,F401,I001
from .web.clientflow import _api_colors_sel  # noqa: E402,F401,I001
from .web.clientflow import _api_cpreview  # noqa: E402,F401,I001
from .web.clientflow import _api_file_variant  # noqa: E402,F401,I001
from .web.clientflow import _api_model  # noqa: E402,F401,I001
from .web.clientflow import _api_modelfile  # noqa: E402,F401,I001
from .web.clientflow import _api_mpreview  # noqa: E402,F401,I001
from .web.clientflow import _api_mymodels  # noqa: E402,F401,I001
from .web.clientflow import _api_order  # noqa: E402,F401,I001
from .web.clientflow import _api_order_cancel  # noqa: E402,F401,I001
from .web.clientflow import _api_order_cart  # noqa: E402,F401,I001
from .web.clientflow import _api_order_edit  # noqa: E402,F401,I001
from .web.clientflow import _api_order_message  # noqa: E402,F401,I001
from .web.clientflow import _api_order_messages  # noqa: E402,F401,I001
from .web.clientflow import _api_order_now  # noqa: E402,F401,I001
from .web.clientflow import _api_order_pay_link  # noqa: E402,F401,I001
from .web.clientflow import _api_order_respond  # noqa: E402,F401,I001
from .web.clientflow import _api_order_unsliced  # noqa: E402,F401,I001
from .web.clientflow import _api_orders  # noqa: E402,F401,I001
from .web.clientflow import _api_parametric_model  # noqa: E402,F401,I001
from .web.clientflow import _api_parametric_models  # noqa: E402,F401,I001
from .web.clientflow import _api_parametric_preview2c  # noqa: E402,F401,I001
from .web.clientflow import _api_plate  # noqa: E402,F401,I001
from .web.clientflow import _api_plates_info  # noqa: E402,F401,I001
from .web.clientflow import _api_quote_state  # noqa: E402,F401,I001
from .web.clientflow import _api_requote  # noqa: E402,F401,I001
from .web.clientflow import _api_use  # noqa: E402,F401,I001
from .web.clientflow import _api_variant  # noqa: E402,F401,I001
from .web.clientflow import _bg_render_cpreview  # noqa: E402,F401,I001
from .web.clientflow import _blocked_payload  # noqa: E402,F401,I001
from .web.clientflow import _cache_snapshot  # noqa: E402,F401,I001
from .web.clientflow import _fetch_mw_cover  # noqa: E402,F401,I001
from .web.clientflow import _finalize_pending_order  # noqa: E402,F401,I001
from .web.clientflow import _hex_to_rgb  # noqa: E402,F401,I001
from .web.clientflow import _live_progress  # noqa: E402,F401,I001
from .web.clientflow import _m_nparts  # noqa: E402,F401,I001
from .web.clientflow import _mw_estimate_for  # noqa: E402,F401,I001
from .web.clientflow import _mw_snapshot  # noqa: E402,F401,I001
from .web.clientflow import _notify_new_order  # noqa: E402,F401,I001
from .web.clientflow import _order_json  # noqa: E402,F401,I001
from .web.clientflow import _presel  # noqa: E402,F401,I001
from .web.clientflow import _quick_calc_fail  # noqa: E402,F401,I001
from .web.clientflow import _quick_calc_plastic  # noqa: E402,F401,I001
from .web.clientflow import _quick_mw_quote  # noqa: E402,F401,I001
from .web.clientflow import _refine_calc  # noqa: E402,F401,I001
from .web.clientflow import _refine_calc_inner  # noqa: E402,F401,I001
from .web.clientflow import _refine_estimate  # noqa: E402,F401,I001
from .web.clientflow import _remember_calc_cache  # noqa: E402,F401,I001
from .web.clientflow import _resolve_options  # noqa: E402,F401,I001
from .web.clientflow import _saved_model_json  # noqa: E402,F401,I001
from .web.clientflow import _saved_preview  # noqa: E402,F401,I001
from .web.clientflow import place_order_from_chat  # noqa: E402,F401,I001
from .web.clientflow import quick_link_quote  # noqa: E402,F401,I001


# Счётчики сметы уехали в app/web/quotecalc.py (волна 9 endgame-плана) — ФАСАД по общим
# правилам: явное перечисление, noqa построчно; автофикс ruff не запускать.
from .web import quotecalc  # noqa: E402,F401,I001
from .web.quotecalc import _apply_dfm  # noqa: E402,F401,I001
from .web.quotecalc import _bed_of  # noqa: E402,F401,I001
from .web.quotecalc import _bed_xy  # noqa: E402,F401,I001
from .web.quotecalc import _parse_plates_sel  # noqa: E402,F401,I001
from .web.quotecalc import _result_payload  # noqa: E402,F401,I001


# Доставка по умолчанию и приписка о ней уехали в app/web/orderdefaults.py (волна 1c
# endgame-плана) — ФАСАД по общим правилам: явное перечисление, noqa построчно, автофикс
# ruff на блоке не запускать.
from .web.orderdefaults import _apply_default_delivery  # noqa: E402,F401,I001
from .web.orderdefaults import _delivery_note  # noqa: E402,F401,I001


# ─────────────────────── Готовые изделия / витрина (OpenTodo bd896aa6) ───────────────────────
# Мастер добавляет готовый товар (фото, фикс-цена, наличие), клиент покупает СРАЗУ — без загрузки
# модели, слайса и расчёта. Покупка списывает наличие атомарно и заводит обычный заказ (product_id),
# чтобы товар прошёл по тому же конвейеру статусов/переписки/выдачи, что и печатные заказы.
# _product_photo_url уехал в app/web/common.py (волна 1b endgame-плана) — реэкспорт в
# фасадном блоке common в шапке файла.


# Готовые товары и индивидуальный заказ уехали в app/web/products.py (волна 13 endgame).
# ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс ruff на блоке не
# запускать. _CUSTOM_DIR читает и оставшийся здесь _save_slicer_shot (уедет волной 16).
# Бота модулю раздаёт webapp.set_bot веером _BOT_FAN (волна 12).
from .web import products  # noqa: E402,F401,I001
from .web.products import _CUSTOM_DIR  # noqa: E402,F401,I001
from .web.products import _CUSTOM_MAX_BYTES  # noqa: E402,F401,I001
from .web.products import _CUSTOM_MAX_PHOTOS  # noqa: E402,F401,I001
from .web.products import _api_admin_product_delete  # noqa: E402,F401,I001
from .web.products import _api_admin_product_save  # noqa: E402,F401,I001
from .web.products import _api_admin_products  # noqa: E402,F401,I001
from .web.products import _api_custom_order  # noqa: E402,F401,I001
from .web.products import _api_product_order  # noqa: E402,F401,I001
from .web.products import _api_products  # noqa: E402,F401,I001
from .web.products import _custom_photo  # noqa: E402,F401,I001
from .web.products import _product_json  # noqa: E402,F401,I001
from .web.products import _product_photo_file  # noqa: E402,F401,I001


# Конструктор заказа уехал в app/orders.py (волна 5 цикла 2, шаг 4) вместе с двумя своими
# помощниками: `_order_stock_plan` (план резерва пластика) и `_embedded_thumb` (последний
# рубеж превью). Уехали ЦЕЛИКОМ и без правки тел — иначе orders.py пришлось бы импортировать
# webapp, то есть спрятать цикл в отложенном импорте вместо того, чтобы его разорвать.
#
# Ниже — ФАСАД по тем же правилам, что у websign/web.common/webauth/webguard/web.calib выше:
# ЯВНОЕ перечисление, по строке на имя. `from .orders import *` приватные имена не берёт, а
# `__all__` там нет вовсе. Реэкспорт держит четыре вызова `_place_one` в этом файле, три
# вызова `_embedded_thumb`, `_order_stock_plan` из досчёта заказа во время слайса и тесты,
# которые знают эти имена как webapp.* — включая inspect.getsource(webapp._place_one) в
# tests/test_b2_intersection_final.py. `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ:
# автофикс молча сносит реэкспорт, а отказ придёт не на старте, а у живого клиента.
from .orders import _embedded_thumb  # noqa: E402,F401,I001
from .orders import _order_stock_plan  # noqa: E402,F401,I001
from .orders import place_from_token as _place_one  # noqa: E402,F401,I001


# _apply_default_delivery уехал в app/web/orderdefaults.py (волна 1c endgame-плана) —
# реэкспорт в фасадном блоке orderdefaults выше по файлу.


# Деньги заказа уехали в app/web/ordermoney.py (волна 16 endgame — последняя волна полной
# программы). ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс ruff
# на блоке не запускать. _maybe_referral_reward зовут отсюда confirm_print и
# handlers/payments.py:88 — потеря его строки выключает реферальную награду молча.
# Бота модулю раздаёт webapp.set_bot веером _BOT_FAN (волна 12).
from .web import ordermoney  # noqa: E402,F401,I001
from .web.ordermoney import _accrue_service_on_print  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_balance  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_balance_comment  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_bill  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_create  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_invoice  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_paid  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_price  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_print_net  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_recalc  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_recalc_apply  # noqa: E402,F401,I001
from .web.ordermoney import _api_admin_order_status  # noqa: E402,F401,I001
from .web.ordermoney import _apply_price_or_ask  # noqa: E402,F401,I001
from .web.ordermoney import _capture_print_sample  # noqa: E402,F401,I001
from .web.ordermoney import _consume_journal_stock  # noqa: E402,F401,I001
from .web.ordermoney import _consume_order_stock  # noqa: E402,F401,I001
from .web.ordermoney import _maybe_referral_reward  # noqa: E402,F401,I001
from .web.ordermoney import _sane_actual  # noqa: E402,F401,I001
from .web.ordermoney import _save_slicer_shot  # noqa: E402,F401,I001
from .web.ordermoney import _telemetry_actual_minutes  # noqa: E402,F401,I001


# Медиа и переписка заказа уехали в app/web/ordermedia.py (волна 1a endgame-плана: общий дом,
# без которого не режется домен заказов, — долг назван в app/web/orderbench.py:29-31). Здесь
# ФАСАД по тем же правилам, что у websign/common выше: явное перечисление, по строке на имя,
# noqa построчно; `ruff check --fix` на блоке не запускать. Маршруты /reqprev/{rid},
# /slicershot/{rid} и /timelapse/{rid} регистрирует make_app одной строкой
# app.add_routes(ordermedia.routes) — там же, где стояли прежние add_get.
from .web import ordermedia  # noqa: E402,F401,I001
from .web.ordermedia import _chat_photo_url  # noqa: E402,F401,I001
from .web.ordermedia import _msgs_json  # noqa: E402,F401,I001
from .web.ordermedia import _order_prints_media  # noqa: E402,F401,I001
from .web.ordermedia import _request_preview  # noqa: E402,F401,I001
from .web.ordermedia import _save_chat_image  # noqa: E402,F401,I001
from .web.ordermedia import _slicer_shot_file  # noqa: E402,F401,I001
from .web.ordermedia import _slicer_url  # noqa: E402,F401,I001
from .web.ordermedia import _timelapse_file  # noqa: E402,F401,I001
from .web.ordermedia import _timelapse_url  # noqa: E402,F401,I001


# _CUSTOM_DIR/_CUSTOM_MAX_* уехали в app/web/products.py (волна 13 endgame) — реэкспорт
# в фасадном блоке products ниже (их читает и _save_slicer_shot, уедет волной 16).


# _chat_photo_url, _msgs_json и _save_chat_image уехали в app/web/ordermedia.py (волна 1a
# endgame-плана) — реэкспорт в общем фасадном блоке ordermedia выше по файлу.


# ───────── База знаний владельца студии (OpenTodo c356387a) ─────────
# Статьи для ВЛАДЕЛЬЦА и его операторов (как настроить, откалибровать, что делать при
# типовой проблеме) — живут только в админке. Клиенту мастерской база знаний не
# показывается: у него есть чат с мастером, а сообщество делается для владельцев студий
# и живёт в Telegram-группе, ссылка на которую лежит в настройках (owners_chat_url).


# Студия владельца (база знаний, каталог, настройки/бренд, команда, ошибки, оттенки, пул
# MakerWorld) уехала в app/web/studio.py (волна 7 endgame-плана). ФАСАД по общим правилам:
# явное перечисление, noqa построчно; автофикс ruff не запускать. Регистрация маршрутов
# осталась в make_app строками на фасадные имена — забытая строка фасада падает громко.
from .web import studio  # noqa: E402,F401,I001
from .web.studio import _api_admin_brand_logo  # noqa: E402,F401,I001
from .web.studio import _api_admin_collection  # noqa: E402,F401,I001
from .web.studio import _api_admin_collection_edit  # noqa: E402,F401,I001
from .web.studio import _api_admin_errors  # noqa: E402,F401,I001
from .web.studio import _api_admin_errors_seen  # noqa: E402,F401,I001
from .web.studio import _api_admin_feature  # noqa: E402,F401,I001
from .web.studio import _api_admin_kb  # noqa: E402,F401,I001
from .web.studio import _api_admin_kb_delete  # noqa: E402,F401,I001
from .web.studio import _api_admin_kb_save  # noqa: E402,F401,I001
from .web.studio import _api_admin_mw_account  # noqa: E402,F401,I001
from .web.studio import _api_admin_mw_accounts  # noqa: E402,F401,I001
from .web.studio import _api_admin_setting  # noqa: E402,F401,I001
from .web.studio import _api_admin_settings  # noqa: E402,F401,I001
from .web.studio import _api_admin_shade  # noqa: E402,F401,I001
from .web.studio import _api_admin_shades  # noqa: E402,F401,I001
from .web.studio import _api_admin_staff  # noqa: E402,F401,I001
from .web.studio import _api_admin_staff_save  # noqa: E402,F401,I001
from .web.studio import _brand_logo_file  # noqa: E402,F401,I001


# Раздача файлов мини-аппа и деска (/app/…, /favicon.ico) уехала в app/web/uifiles.py
# (вопрос 5 endgame-плана, решение владельца 22.08.2026). Раньше обе ручки были
# замыканиями внутри make_app; теперь — модульные, регистрация ниже на фасадные имена.
from .web import uifiles  # noqa: E402,F401,I001
from .web.uifiles import _favicon  # noqa: E402,F401,I001
from .web.uifiles import _ui_file  # noqa: E402,F401,I001


# Идеи клиентов уехали в app/web/storefront.py (волна 13 endgame) к клиентской витрине.
# Бота модулю раздаёт webapp.set_bot веером _BOT_FAN (волна 12). ФАСАД по общим правилам.
from .web.storefront import _api_idea  # noqa: E402,F401,I001


# Клиентская витрина и каталог уехали в app/web/storefront.py (волна 8 endgame-плана):
# HTML-страницы коллекции/каталога с картинками, прокси внешних превью (_thumb_proxy/
# _api_thumb), поиск по площадкам и клиентские API каталога (коллекция, избранное,
# рекомендации, лента F4, поиск по коллекции, настройки клиента). НЕ уехали _pick и
# _pick_plastic — единственные читатели _storage (см. «что не выносится» endgame-плана).
# ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс ruff не запускать.
from .web import storefront  # noqa: E402,F401,I001
from .web.storefront import _THUMB_MAX  # noqa: E402,F401,I001
from .web.storefront import _api_collection  # noqa: E402,F401,I001
from .web.storefront import _api_collection_search  # noqa: E402,F401,I001
from .web.storefront import _api_favorite  # noqa: E402,F401,I001
from .web.storefront import _api_feed  # noqa: E402,F401,I001
# «Мастерская органайзеров» (Gridfinity) уехала из storefront в app/web/gridfinity.py:
# витрина осталась витриной, а конфигуратор дорос до проектов, спецификации деталей и
# частичного заказа — с собственной таблицей маршрутов (как calib/prices/equipment).
# Карточка конфигуратора в ленте (_GF_FEED_SHOWN) осталась в storefront: это логика ЛЕНТЫ.
# ФАСАД по общим правилам, noqa построчно; `ruff check --fix` на блоке запускать НЕЛЬЗЯ.
from .web import gridfinity  # noqa: E402,F401,I001
from .web.gridfinity import _api_admin_gf_part_printed  # noqa: E402,F401,I001
from .web.gridfinity import _api_admin_gf_release  # noqa: E402,F401,I001
from .web.gridfinity import _api_admin_gridfinity_config  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_measure_add  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_order  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_part_patch  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_parts_select  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_preflight  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_preset_add  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_presets  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_project  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_project_add  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_project_patch  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_project_save  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_projects  # noqa: E402,F401,I001
from .web.gridfinity import _api_gf_template  # noqa: E402,F401,I001
from .web.gridfinity import _api_gridfinity_config  # noqa: E402,F401,I001
from .web.gridfinity import _api_gridfinity_config_set  # noqa: E402,F401,I001
from .web.gridfinity import _api_gridfinity_order  # noqa: E402,F401,I001
from .web.gridfinity import _gf_auto_name  # noqa: E402,F401,I001
from .web.gridfinity import _gf_batch_summary  # noqa: E402,F401,I001
from .web.gridfinity import _gf_batches  # noqa: E402,F401,I001
from .web.gridfinity import _gf_beds  # noqa: E402,F401,I001
from .web.gridfinity import _gf_body  # noqa: E402,F401,I001
from .web.gridfinity import _gf_cfg_of  # noqa: E402,F401,I001
from .web.gridfinity import _gf_color_need  # noqa: E402,F401,I001
from .web.gridfinity import _gf_counts  # noqa: E402,F401,I001
from .web.gridfinity import _gf_dims  # noqa: E402,F401,I001
from .web.gridfinity import _gf_actor  # noqa: E402,F401,I001
from .web.gridfinity import _gf_denied  # noqa: E402,F401,I001
from .web.gridfinity import _gf_desk_html  # noqa: E402,F401,I001
from .web.gridfinity import _gf_desk_info  # noqa: E402,F401,I001
from .web.gridfinity import _gf_desk_notify  # noqa: E402,F401,I001
from .web.gridfinity import _gf_desk_page  # noqa: E402,F401,I001
from .web.gridfinity import _gf_desk_url  # noqa: E402,F401,I001
from .web.gridfinity import _gf_dump  # noqa: E402,F401,I001
from .web.gridfinity import _gf_discount  # noqa: E402,F401,I001
from .web.gridfinity import _gf_estimate  # noqa: E402,F401,I001
from .web.gridfinity import _gf_f  # noqa: E402,F401,I001
from .web.gridfinity import _gf_filament  # noqa: E402,F401,I001
from .web.gridfinity import _gf_frozen  # noqa: E402,F401,I001
from .web.gridfinity import _gf_free  # noqa: E402,F401,I001
from .web.gridfinity import _gf_grid  # noqa: E402,F401,I001
from .web.gridfinity import _gf_head_lines  # noqa: E402,F401,I001
from .web.gridfinity import _gf_i  # noqa: E402,F401,I001
from .web.gridfinity import _gf_inner_h  # noqa: E402,F401,I001
from .web.gridfinity import _gf_live_state  # noqa: E402,F401,I001
from .web.gridfinity import _gf_minutes  # noqa: E402,F401,I001
from .web.gridfinity import _gf_migrate_draft  # noqa: E402,F401,I001
from .web.gridfinity import _gf_mod_line  # noqa: E402,F401,I001
from .web.gridfinity import _gf_modules  # noqa: E402,F401,I001
from .web.gridfinity import _gf_money  # noqa: E402,F401,I001
from .web.gridfinity import _gf_notify_change  # noqa: E402,F401,I001
from .web.gridfinity import _gf_notify_order  # noqa: E402,F401,I001
from .web.gridfinity import _gf_oversize  # noqa: E402,F401,I001
from .web.gridfinity import _gf_owned  # noqa: E402,F401,I001
from .web.gridfinity import _gf_part_view  # noqa: E402,F401,I001
from .web.gridfinity import _gf_parts_view  # noqa: E402,F401,I001
from .web.gridfinity import _gf_preflight  # noqa: E402,F401,I001
from .web.gridfinity import _gf_preview  # noqa: E402,F401,I001
from .web.gridfinity import _gf_price_ctx  # noqa: E402,F401,I001
from .web.gridfinity import _gf_reconcile  # noqa: E402,F401,I001
from .web.gridfinity import _gf_share_html  # noqa: E402,F401,I001
from .web.gridfinity import _gf_script  # noqa: E402,F401,I001
from .web.gridfinity import _gf_share_page  # noqa: E402,F401,I001
from .web.gridfinity import _gf_sig  # noqa: E402,F401,I001
from .web.gridfinity import _gf_settings  # noqa: E402,F401,I001
from .web.gridfinity import _gf_statuses  # noqa: E402,F401,I001
from .web.gridfinity import _gf_std_discount  # noqa: E402,F401,I001
from .web.gridfinity import _gf_std_text  # noqa: E402,F401,I001
from .web.gridfinity import _gf_summary  # noqa: E402,F401,I001
from .web.gridfinity import _gf_too_big  # noqa: E402,F401,I001
from .web.gridfinity import _gf_uname  # noqa: E402,F401,I001
from .web.gridfinity import _gf_units  # noqa: E402,F401,I001
from .web.gridfinity import _gf_valid  # noqa: E402,F401,I001
from .web.storefront import _api_model_search  # noqa: E402,F401,I001
from .web.storefront import _api_prefs  # noqa: E402,F401,I001
from .web.storefront import _api_recommend  # noqa: E402,F401,I001
from .web.storefront import _api_thumb  # noqa: E402,F401,I001
from .web.storefront import _card  # noqa: E402,F401,I001
from .web.storefront import _catalog_page  # noqa: E402,F401,I001
from .web.storefront import _collection_item_json  # noqa: E402,F401,I001
from .web.storefront import _collection_page  # noqa: E402,F401,I001
from .web.storefront import _image  # noqa: E402,F401,I001
from .web.storefront import _plastic_card  # noqa: E402,F401,I001
from .web.storefront import _plastic_image  # noqa: E402,F401,I001
from .web.storefront import _spool_image  # noqa: E402,F401,I001
from .web.storefront import _thumb_proxy  # noqa: E402,F401,I001
# Сборщики карточек каталога и ленты — общие с пакетом каталога приёмной (app/relaybundle.py).
from .web.storefront import build_catalog_categories  # noqa: E402,F401,I001
from .web.storefront import feed_item_json  # noqa: E402,F401,I001


# PDF-документы (смета по токену, паспорт заказа) уехали в app/web/ordermedia.py (волна 13
# endgame) к остальному представлению заказа наружу. Бота модулю раздаёт webapp.set_bot
# веером _BOT_FAN (волна 12). ФАСАД по общим правилам, noqa построчно.
from .web.ordermedia import _api_order_pdf  # noqa: E402,F401,I001
from .web.ordermedia import _api_pdf  # noqa: E402,F401,I001


async def _api_version(request: web.Request) -> web.Response:
    """Текущая версия фронта мини-апки. Страница сверяет её со своей (вшитой в index.html) и,
    если расходится, сама перезагружается — лечит залипший кеш Telegram WebView.

    Отдаём ещё и канал выпуска (stable/test, OpenTodo 31485676), чтобы мини-аппа показывала
    бейдж «TEST», когда запущена тестовая версия — иначе её визуально не отличить от боевой."""
    vi = version.info()
    return web.json_response(
        {"v": config.webui_version(), "channel": vi["channel"], "rev": vi["sha"],
         "is_test": vi["is_test"]},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


async def _api_desk_login(request: web.Request) -> web.Response:
    """Обмен одноразового кода из бота на личную сессию настольной админки.

    Единственная ручка проекта БЕЗ аутентификации — она и есть дверь. Защищают её три вещи:
    запрос обязан быть С ЭТОЙ машины (файлы деска и так отдаются только локально), код живёт
    пять минут и сгорает при обмене, а частота попыток ограничена в desklogin. Отвечать на
    неверный код чем-то подробнее «код не подошёл» нельзя: разница в ответах превратила бы
    ручку в оракул «такой код существует»."""
    if not _request_is_local(request):
        raise web.HTTPNotFound()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    got = await desklogin.redeem_code(str(body.get("code") or ""),
                                      device=str(body.get("device") or ""))
    if not got:
        return web.json_response({"ok": False, "error": "Код не подошёл или истёк"})
    token, uid = got
    if not config.is_admin(uid):
        # Человека сняли с должности между выдачей кода и входом. Сессию сразу же и отзываем:
        # роль проверяется на каждом запросе, но пускать в пустую админку незачем.
        await desklogin.revoke(token)
        return web.json_response({"ok": False, "error": "Вы больше не в команде мастерской"})
    await db.log_event(uid, "вошёл в настольную админку", "")
    return web.json_response({"ok": True, "token": token, "me": roles.whoami(uid)})


async def _api_desk_logout(request: web.Request) -> web.Response:
    """Выйти на этом устройстве. Токен приезжает тем же полем, что и всегда."""
    if not _request_is_local(request):
        raise web.HTTPNotFound()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    await desklogin.revoke(str(body.get("initData") or ""))
    return web.json_response({"ok": True})


async def _api_desk_whoami(request: web.Request) -> web.Response:
    """Кто я и как устроен вход. Нужна экрану входа ДО всякой авторизации: по ней он решает,
    показывать форму кода или пускать сразу (в режиме `token` вход прежний)."""
    if not _request_is_local(request):
        raise web.HTTPNotFound()
    uid = _verify_init_data(request.query.get("initData", ""))
    return web.json_response({"ok": True, "mode": desklogin.mode(),
                              "authed": bool(uid and config.is_admin(uid)),
                              "me": roles.whoami(uid) if uid else None})


# Три сквозные middleware (_error_mw, _compress_mw, _usage_mw) уехали в app/webguard.py
# (волна 8 endgame) к _caps_mw/_edition_mw — реэкспорт в фасадном блоке webguard ниже по
# файлу. Порядок middleware в make_app НЕ менялся.


# ===================== Админ-панель в приложении (#432bca3a) =====================
# _admin_forbidden уехал в app/webauth.py (волна 4 цикла 2), имя доступно через фасад выше.


# ───────────────────── Права команды на админских ручках (A4) ─────────────────────
# Карты прав (_OPERATOR_ROUTES / _OPERATOR_PREFIXES / _OWNER_ROUTES), _route_cap, _forbidden_cap,
# вырезание денег из ответов и обе middleware (_edition_mw, _caps_mw) уехали в app/webguard.py
# (волна 4 цикла 2). Здесь остаётся ФАСАД — ЯВНОЕ перечисление, по строке на имя:
# `from .webguard import *` приватные имена не берёт, а `__all__` в этом модуле нет вовсе.
# Реэкспорт держит и код ниже (_money_visible/_hide_money в шести хендлерах, _forbidden_cap
# в _api_admin_brand_logo, обе middleware в make_app), и обращения из тестов, которые знают эти
# имена как webapp.*; забытое имя откажет не на старте, а в момент вызова у живого клиента.
# `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ (автофикс молча сносит реэкспорт) — гасим
# построчно через noqa.
#
# Карты реэкспортируются как САМИ объекты (dict/tuple), а не как копии: правка карты видна
# с обеих сторон, второго списка прав в проекте не появилось.
#
# Порядок middleware в make_app НЕ менялся: _local_mw по-прежнему первый (метка локальности
# нужна любому хендлеру), _edition_mw и _caps_mw идут последними в прежнем порядке.
from .webguard import _caps_mw  # noqa: E402,F401,I001
from .webguard import _compress_mw  # noqa: E402,F401,I001
from .webguard import _edition_mw  # noqa: E402,F401,I001
from .webguard import _error_mw  # noqa: E402,F401,I001
from .webguard import _forbidden_cap  # noqa: E402,F401,I001
from .webguard import _hide_money  # noqa: E402,F401,I001
from .webguard import _money_visible  # noqa: E402,F401,I001
from .webguard import _OPERATOR_PREFIXES  # noqa: E402,F401,I001
from .webguard import _OPERATOR_ROUTES  # noqa: E402,F401,I001
from .webguard import _usage_mw  # noqa: E402,F401,I001
from .webguard import _OWNER_ROUTES  # noqa: E402,F401,I001
from .webguard import _route_cap  # noqa: E402,F401,I001


# Списки заказов и карточка мастера уехали в app/web/orderdesk.py (волна 5 endgame-плана,
# после волны 1a: карточка держится за общий дом медиа/переписки ordermedia). Здесь ФАСАД
# по общим правилам: явное перечисление, noqa построчно; автофикс ruff не запускать.
# Регистрация маршрутов осталась в make_app строками на фасадные имена.
from .web import orderdesk  # noqa: E402,F401,I001
from .web.orderdesk import _api_admin_order  # noqa: E402,F401,I001
from .web.orderdesk import _api_admin_orders  # noqa: E402,F401,I001
from .web.orderdesk import _api_admin_orders_csv  # noqa: E402,F401,I001
from .web.orderdesk import _api_admin_self_orders  # noqa: E402,F401,I001
from .web.orderdesk import _delivery_recalc_hint  # noqa: E402,F401,I001
from .web.orderdesk import _order_scale_pct  # noqa: E402,F401,I001

# ════════════════ CRM: клиенты, сегменты, рассылки, задачи (crm-roadmap.md) ════════════════
# CRM (клиенты, карточка, скидка, сплошной чат, события, поиск, задачи) уехала в
# app/web/crm.py (волна 6 endgame-плана). Домен разрезан по держателю бота: ниже остаются
# _api_admin_client_message и _api_admin_broadcast — обе читают _bot (волны 12/13).
# ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс ruff не запускать.
from .web import crm  # noqa: E402,F401,I001
from .web.crm import _api_admin_calc  # noqa: E402,F401,I001
from .web.crm import _api_admin_chat_search  # noqa: E402,F401,I001
from .web.crm import _api_admin_client  # noqa: E402,F401,I001
from .web.crm import _api_admin_client_chat  # noqa: E402,F401,I001
from .web.crm import _api_admin_client_discount  # noqa: E402,F401,I001
from .web.crm import _api_admin_client_save  # noqa: E402,F401,I001
from .web.crm import _api_admin_clients  # noqa: E402,F401,I001
from .web.crm import _api_admin_clients_csv  # noqa: E402,F401,I001
from .web.crm import _api_admin_events  # noqa: E402,F401,I001
from .web.crm import _api_admin_task  # noqa: E402,F401,I001
from .web.crm import _api_admin_tasks  # noqa: E402,F401,I001
from .web.crm import _parse_task_due  # noqa: E402,F401,I001

# Хвост CRM (написать клиенту, рассылка) уехал в app/web/crm.py (волна 13 endgame): домен,
# разрезанный волной 6 пополам по держателю бота, собран обратно — бота модулю раздаёт
# webapp.set_bot веером _BOT_FAN (волна 12). ФАСАД по общим правилам, noqa построчно.
from .web.crm import _api_admin_broadcast  # noqa: E402,F401,I001
from .web.crm import _api_admin_client_message  # noqa: E402,F401,I001


# Сплошной чат, события и поиск уехали в app/web/crm.py (волна 6) — фасад выше.


# Задачи-напоминания уехали в app/web/crm.py (волна 6) — фасад выше.


# _delivery_recalc_hint и _api_admin_order уехали в app/web/orderdesk.py (волна 5) — фасад выше.


# ── Заказ на верстаке ──────────────────────────────────────────────────────────────────────
# Блок уехал в app/web/orderbench.py (волна «Заказ на верстаке» цикла 2): материал заказа,
# слайс модели по ссылке, открыть в слайсере и открыть папку на ПК бота. Маршруты объявлены
# там же, рядом с обработчиками, и включаются в make_app одной строкой
# `app.add_routes(orderbench.routes)` — потерять ручку при переносе нечем.
#
# Ниже — ФАСАД по тем же правилам, что у calib/prices/equipment выше: ЯВНОЕ перечисление, по
# строке на имя. `from .web.orderbench import *` приватные имена не берёт, а `__all__` в том
# модуле нет вовсе. `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ — автофикс молча снесёт
# реэкспорт, и откажет он не на старте, а у живого клиента.
#
# `import subprocess` и `reslice` из шапки файла УДАЛЕНЫ той же волной: их единственным
# пользователем было уехавшее тело _api_admin_order_open_slicer. Прочие вхождения subprocess
# в файле — это `asyncio.subprocess.PIPE/DEVNULL`, атрибут пакета asyncio, верхнеуровневый
# импорт ему не нужен. Вернёшь «на всякий случай» — получишь красный ruff (F401).
from .web import orderbench  # noqa: E402,F401,I001
from .web.orderbench import _api_admin_order_material  # noqa: E402,F401,I001
from .web.orderbench import _api_admin_order_open_folder  # noqa: E402,F401,I001
from .web.orderbench import _api_admin_order_open_slicer  # noqa: E402,F401,I001
from .web.orderbench import _api_admin_order_slice  # noqa: E402,F401,I001
from .web.orderbench import _printer_filament_names  # noqa: E402,F401,I001


async def _api_admin_order_message(request: web.Request) -> web.Response:
    """Админ пишет клиенту в тред заказа (9d222a19) и уведомляет его."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"}, status=404)
    text = (body.get("text") or "").strip()[:2000]
    image = body.get("image") or ""
    if not text and not image:
        return web.json_response({"ok": False, "error": "Пустое сообщение"})
    mid = await db.add_order_message(rid, from_admin=True, text=text, user_id=r.user_id)
    photo_path = await _save_chat_image(mid, image) if image else ""
    if _bot is not None:
        try:
            # Текст — общий с реплаем в Telegram (app/orderchat.py): по этой же шапке клиент
            # отвечает прямо из чата с ботом, не открывая приложение.
            await orderchat.notify_client(_bot, orderchat.Target(rid, r.user_id, True),
                                          text, photo_path, msg_id=mid)
        except Exception:  # noqa: BLE001
            log.debug("admin order message notify failed", exc_info=True)
    rows = await db.list_order_messages(rid)
    return web.json_response({"ok": True, "messages": _msgs_json(rows, mine_is_admin=True)})


# Денежные ключи сводки дашборда: у оператора (A4) их в ответе нет — выручку мастерской
# наёмный сотрудник не видит ни цифрой, ни графиком.
# мёртв с 08.08.2026: дашборд сужен до права финансов, удалить после промоушена в stable
_DASH_MONEY_KEYS = ("revenue_printed", "revenue_all", "avg_order", "revenue_7d", "revenue_30d",
                    "orev_7d", "orev_30d")


async def _api_admin_dashboard(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    stats = await db.dashboard_stats()
    money = _money_visible(request)
    # мёртв с 08.08.2026: дашборд сужен до права финансов, удалить после промоушена в stable
    # (без CAP_FINANCE до хендлера уже не доходят — _caps_mw отвечает 403)
    if not money:
        _hide_money(stats, _DASH_MONEY_KEYS)
        for row in stats.get("series14") or []:      # ряд «выручка по дням» — обнуляем сумму
            if isinstance(row, dict):
                row["revenue"] = None
    return web.json_response({"ok": True, "currency": config.CURRENCY, "money": money,
                              "stats": stats,
                              # воронка по периодам + причины отказов (crm-roadmap этап 6)
                              "crm": (await db.crm_dashboard()) if money else {}})


async def _gpu_snapshot() -> dict:
    """Снимок видеокарты через nvidia-smi (OpenTodo 689d72bb): VRAM/загрузка/температура.
    Best-effort: нет nvidia-smi/таймаут → пустой dict (карточка просто не покажется)."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        parts = [p.strip() for p in out.decode("utf-8", "ignore").strip().split(",")]
        if len(parts) >= 5:
            return {"name": parts[0], "vram_used_mb": int(parts[1]), "vram_total_mb": int(parts[2]),
                    "util_pct": int(parts[3]), "temp_c": int(parts[4])}
    except Exception:  # noqa: BLE001
        pass
    finally:
        await reap_process(proc)
    return {}


async def ai_idle_min() -> float:
    """Действующий порог автовыгрузки ИИ из VRAM (минуты простоя): настройка мастера из
    «Статуса» (settings.avatar_gpu_idle_min) поверх дефолта из .env (AVATAR_GPU_IDLE_MIN).
    0 — по простою не выгружать. Читает и цикл run._avatar_gpu_idle_loop."""
    s = await db.get_settings()
    v = s.get("avatar_gpu_idle_min")
    return float(v) if v is not None else float(config.AVATAR_GPU_IDLE_MIN)


async def _api_admin_status(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    from . import avatars_gpu, relaylink
    q = await db.queue_load()
    running, cap = selfcheck.slicer_load()
    printers = len(await db.list_printers(active_only=True))
    plastics = len(await db.list_plastics(active_only=True))
    return web.json_response({"ok": True, "status": {
        "uptime": selfcheck.uptime_str(), "disk_gb": round(selfcheck.disk_free_gb(), 1),
        "queue_count": q["count"], "queue_minutes": q["minutes"],
        "slices_running": running, "slices_cap": cap,
        # mw_token — сводка по ПУЛУ аккаунтов MakerWorld в прежней форме (её читают обе админки),
        # mw_accounts — состояние каждого аккаунта отдельно (кулдаун 418, срок, ошибка)
        "printers": printers, "plastics": plastics, "mw_token": _mw_status(),
        "mw_accounts": mwtoken.status_all() if mwtoken else [],
        "turzx": {"enabled": await _turzx_enabled()},   # питание TURZX-экрана (тумблер в админке)
        # ИИ и железо (OpenTodo 689d72bb); idle_unload_min — порог автовыгрузки (ee064f93)
        "ai": {**avatars_gpu.status(), "idle_unload_min": await ai_idle_min()},
        "gpu": await _gpu_snapshot(),
        "preview_gpu": bool(config.PREVIEW_GPU),
        # Фактический каталог данных (C2): задаётся DATA_DIR в .env, и владелец должен видеть,
        # КУДА бот на самом деле пишет базу и фото — иначе перенос data/ проверить нечем.
        "data_dir": str(config.DATA_DIR),
        # Вход наружу (C3): каким способом студия видна клиентам и по какому адресу.
        # Адрес публичный по определению, а вот CF_TUNNEL_TOKEN не отдаём даже маской —
        # только признак «задан».
        # Живое состояние процесса тоже отдаём: при неверном токене cloudflared умирает и
        # вечно перезапускается сторожем, а по одним ключам .env всё выглядит настроенным.
        "tunnel_mode": str(tunnel.status()["mode"]),
        "public_url": str(tunnel.status()["public_url"]),
        "tunnel_token_set": bool(tunnel.status()["token_set"]),
        "tunnel_running": bool(tunnel.status()["running"]),
        "tunnel_error": str(tunnel.status()["error"]),
        "calc_queue": await db.count_calc_queue(),
        # автообновления (644367fb): версия, режим и последняя проверка — для блока «Обновления»
        "version": version.info(),
        "update": {**selfupdate.status(), "mode": await selfupdate.mode()},
        # Лицензия студии (трек C, п.4): отдаём РАСШИФРОВАННЫЕ поля (кому выдана, план,
        # срок) — это не секрет и именно их владелец должен видеть. Сам ключ и подпись
        # не отдаём никогда. Вложенным объектом, чтобы не менять сетку «Показателей».
        "license": license.status(),
        # Этап 0 автотюнинга: сколько исходов печати уже накоплено (и сколько с оценкой
        # клиента). Вложенным объектом — плоский ключ сетка «Показателей» деска вывалила бы
        # ячейкой с сырым английским именем. Только агрегаты: ручку поллят раз в 20 секунд,
        # сборке датасета (tuning.build_dataset) тут делать нечего.
        "tuning": await tuning.stats(),
        # Откуда бот слушает Telegram (Э1): сам или через приёмную на VPS, и как она. Вложенным
        # объектом по той же причине, что tuning; ключа подписи тут нет и быть не может.
        "relay": relaylink.status(),
        "issues": await selfcheck.run_checks()}})


async def _api_admin_update_check(request: web.Request) -> web.Response:
    """Проверить обновления кода вручную (644367fb): fetch origin + сколько коммитов позади."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    res = await selfupdate.check()
    return web.json_response({"ok": True, "update": {**res, "mode": await selfupdate.mode()},
                              "version": version.info()})


async def _api_admin_update_apply(request: web.Request) -> web.Response:
    """Применить обновление (644367fb): ff-only pull + pip install, затем выход процесса —
    сторож поднимет бота с новым кодом (~1–2 мин). Ответ уходит ДО перезапуска."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    res = await selfupdate.apply()
    if res.get("ok"):
        await db.log_event(uid, "обновление бота",
                           f"→ {res.get('updated_to')} ({res.get('count')} коммит(ов))")
    return web.json_response(res)


async def _api_admin_update_mode(request: web.Request) -> web.Response:
    """Режим автообновлений (644367fb): off / notify / auto — живёт в text_settings."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    m = str(body.get("mode") or "").strip().lower()
    if m not in selfupdate.MODES:
        return web.json_response({"ok": False, "error": "Режим: off, notify или auto"})
    await db.set_text_setting("selfupdate_mode", m)
    await db.log_event(uid, "автообновления",
                       {"off": "выключил", "notify": "режим «уведомлять»",
                        "auto": "режим «обновляться самому»"}[m])
    return web.json_response({"ok": True, "mode": m})


async def _api_admin_backup_status(request: web.Request) -> web.Response:
    """Состояние обоих контуров бэкапа (b557e341): журнал попыток + что реально лежит в каталогах.

    Каталоги листаем в to_thread: сотня файлов на сетевом/спящем диске не должна вешать loop."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    snap = await asyncio.to_thread(backup.scan)
    runs = await db.list_backup_runs(20)
    return web.json_response({"ok": True, "last": runs[0] if runs else None, "runs": runs, **snap})


async def _api_admin_backup_now(request: web.Request) -> web.Response:
    """Снимок БД по кнопке (b557e341): та же процедура, что и по расписанию."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    run = await backup.backup_once()
    await db.log_event(uid, "бэкап БД", (Path(run.get("path") or "").name or "—")
                       if run.get("ok") else ("не удался: " + (run.get("error") or "")))
    # ok отражает исход самой копии: фронт разворачивает {ok:false} в понятную ошибку
    return web.json_response({"ok": bool(run.get("ok")), "run": run,
                              "error": run.get("error") or ""})


async def _api_admin_ai_action(request: web.Request) -> web.Response:
    """Управление ИИ-генератором с экрана «Статус» (OpenTodo ee064f93). Body {action, ...}:
    toggle {on} — вкл/выкл генерацию (тот же файл-флаг, что ярлык на рабочем столе, выключение
    сразу освобождает VRAM); unload — выгрузить модель из VRAM/RAM прямо сейчас; idle {minutes}
    — порог автовыгрузки по простою (0 — не выгружать), живёт в settings поверх .env."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    from . import avatars_gpu
    action = str(body.get("action") or "")
    if action == "toggle":
        on = bool(body.get("on"))
        if not await asyncio.to_thread(avatars_gpu.set_enabled, on):
            return web.json_response({"ok": False, "error": "Не удалось переключить файл-флаг ИИ"})
        await db.log_event(_uid, "ИИ-генерация", "включил" if on else "выключил (VRAM освобождена)")
    elif action == "unload":
        if not await asyncio.to_thread(avatars_gpu.unload, "вручную из «Статуса»"):
            return web.json_response({"ok": False, "error": "Модель и так не загружена"})
        await db.log_event(_uid, "ИИ-генерация", "выгрузил модель из VRAM")
    elif action == "idle":
        try:
            minutes = float(body.get("minutes"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Не разобрал число минут"})
        if not 0 <= minutes <= 1440:
            return web.json_response({"ok": False, "error": "Порог — от 0 (не выгружать) до 1440 минут"})
        await db.set_setting("avatar_gpu_idle_min", minutes)
        await db.log_event(_uid, "ИИ-генерация",
                           f"автовыгрузка: {minutes:g} мин" if minutes else "автовыгрузка выключена")
    elif action == "model":
        # Выбор модели генерации из каталога avatars_gpu.MODELS (пусто = вернуть модель из
        # .env). Применяется сразу — set_model выгружает старый пайплайн, поэтому to_thread:
        # под _LOCK можно прождать до конца идущей генерации. Переживает рестарт через
        # text_settings — run._warmup_avatar_gpu применяет сохранённое на старте.
        model = str(body.get("model") or "").strip()
        if not await asyncio.to_thread(avatars_gpu.set_model, model or None):
            return web.json_response({"ok": False, "error": "Неизвестная модель"})
        await db.set_text_setting("avatar_gpu_model", model)
        label = str(avatars_gpu.MODELS.get(model, {}).get("label") or model or "из .env")
        await db.log_event(_uid, "ИИ-генерация", f"модель: {label}")
    else:
        return web.json_response({"ok": False, "error": "Неизвестное действие"})
    return web.json_response({"ok": True,
        "ai": {**avatars_gpu.status(), "idle_unload_min": await ai_idle_min()}})


async def _api_admin_ai_test(request: web.Request) -> web.Response:
    """Проба генерации картинок с экрана «Статус» (22.07): промт мастера → PNG выбранной
    моделью. Картинка не сохраняется (base64 прямо в ответе — админка локальная, мегабайт
    не страшен) и процедурного фолбэка нет: панель сравнивает ИИ-модели, честный отказ
    полезнее заглушки. Каждый вызов — новый случайный seed; styled=False шлёт промт в
    модель без фирменного стиля маскота (перевод RU→EN остаётся)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    from . import avatars_gpu
    prompt = str(body.get("prompt") or "").strip()[:300]
    if not prompt:
        return web.json_response({"ok": False, "error": "Опишите, что нарисовать"})
    if not await asyncio.to_thread(avatars_gpu.available):
        return web.json_response({"ok": False,
            "error": "ИИ-генерация выключена или GPU недоступна — тумблер в карточке «ИИ и видеокарта»"})
    styled = bool(body.get("styled", True))
    seed = int.from_bytes(os.urandom(6), "big")
    t0 = time.monotonic()
    res = await asyncio.to_thread(avatars_gpu.generate_png, prompt, seed, styled)
    if res is None:
        return web.json_response({"ok": False,
            "error": "Не получилось: видеокарта занята или модель не смогла — детали в логе бота"})
    png, engine = res
    return web.json_response({"ok": True,
        "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "engine": engine, "seconds": round(time.monotonic() - t0, 1), "seed": seed})


async def _api_admin_hub(request: web.Request) -> web.Response:
    """Агрегат для стартового экрана админ-хаба (Вариант A «Умные плитки»): счётчики блока
    «требует внимания», свёрнутый дашборд (выручка/заказы) и числа по разделам. Один round-trip;
    принтер опрашивается отдельно через /api/admin/printer_status. Все цифры — существующими
    хелперами (db.dashboard_stats / queue_load / списки разделов), без доработки SQL."""
    huid = _verify_admin(request.query.get("initData", ""))
    if not huid:
        return _admin_forbidden()
    stats = await db.dashboard_stats()
    status_mix = stats.get("status_mix") or {}
    series = stats.get("series14") or []
    q = await db.queue_load()
    discounts = len(await db.list_discounts()) + len(await db.list_user_discounts())
    presets = len(await db.list_quality(active_only=False)) + len(await db.list_strength(active_only=False))
    money = _money_visible(request)   # оператор (A4) видит «требует внимания», но не выручку
    return web.json_response({"ok": True, "currency": config.CURRENCY, "money": money,
        # Роль и права сотрудника: настольная админка и так опрашивает хаб — по нему она
        # прячет разделы, закрытые оператору, без отдельного запроса.
        "me": roles.whoami(huid),
        "attention": {
            "new_orders": status_mix.get("new", 0),
            # По клиентам, а не по заказам: считаем и сообщения вне заказа (2026-07-19)
            "unread": sum((await db.clients_unread_admin()).values()),
            "price_wait": status_mix.get("price_changed", 0),
        },
        "revenue": {
            "today": (series[-1]["revenue"] if series else 0.0) if money else None,
            "week": stats.get("orev_7d", 0) if money else None,
            "orders_week": stats.get("orders_7d", 0),
            "avg_order": stats.get("avg_order", 0) if money else None,
            "orders_total": stats.get("orders", 0),
        },
        "counts": {
            "queue": q["count"],
            "plastics": len(await db.list_plastics(active_only=True)),
            "discounts": discounts,
            "presets": presets,
            "shades": len(await db.list_color_shades()),
            "collection": await db.count_collection(),
            "products": await db.count_products(active_only=False),   # готовые изделия (bd896aa6)
            # центр уведомлений и семейные заявки — отдельные плитки (2026-07-17)
            "notifications": await db.count_new_admin_notifications(exclude_kind="family_request"),
            "family_requests": await db.count_new_admin_notifications(kind="family_request"),
            # открытые задачи мастера — бейдж раздела «Задачи» в сайдбаре настольной админки
            "tasks": len(await db.list_crm_tasks()),
            # непрочитанные сбои — бейдж раздела «Ошибки» (телеметрия, f4de8627)
            "errors": await db.count_new_errors(),
        }})


# Общий дом драйвера принтера уехал в app/web/printerhub.py (волна 2 endgame-плана):
# `_printer_driver` — самый горячий узел файла, двадцать вызывающих из трёх доменов. Здесь
# ФАСАД по общим правилам: явное перечисление, по строке на имя, noqa построчно; автофикс
# ruff на блоке не запускать. printwatch с этой волны берёт `_printer_ep` из printerhub
# напрямую — фасад держит остальных читателей и тесты.
from .web import printerhub  # noqa: E402,F401,I001
from .web.printerhub import _camera_url  # noqa: E402,F401,I001
from .web.printerhub import _driver_printer_id  # noqa: E402,F401,I001
from .web.printerhub import _live_view  # noqa: E402,F401,I001
from .web.printerhub import _no_printer  # noqa: E402,F401,I001
from .web.printerhub import _printer_driver  # noqa: E402,F401,I001
from .web.printerhub import _printer_ep  # noqa: E402,F401,I001
from .web.printerhub import _printer_id_arg  # noqa: E402,F401,I001


# Печать как работа (статус, сетевой старт, привязка, история, прикрепление печати) уехала
# в app/web/printerjob.py (волна 11 endgame-плана — последняя волна свободной программы).
# ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс ruff не запускать.
from .web import printerjob  # noqa: E402,F401,I001
# «Инструменты» настольной админки (допечатка со слоя, Путь В): маршруты таблицей модуля,
# фасадных имён нет — ручки зовутся только по HTTP.
from .web import tools as webtools  # noqa: E402,F401,I001
# Пульт, вторая очередь (фичи прошивки сообщества CC2, сделанные своими): таблица модуля,
# фасадных имён нет.
from .web import printerext  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_order_attach_print  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_printer_bind  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_printer_history  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_printer_print  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_printer_status  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_reprint  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_reprint_parts  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_reprint_start  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_printer_unbind  # noqa: E402,F401,I001
from .web.printerjob import _link_attached_print_journal  # noqa: E402,F401,I001


# Живой принтер (камера + ретранслятор, скрин стола, файлы/заливка, ручной пульт) уехал в
# app/web/printerlive.py (волна 4 endgame-плана, после волны 2). Остались здесь и НЕ уехали:
# _api_admin_printer_print (пишет _active_print — волна 10) и _api_admin_printer_status
# (читает _active_print_header — волна 11). Здесь ФАСАД по общим правилам: явное
# перечисление, noqa построчно; автофикс ruff на блоке не запускать. Реестр ретрансляторов
# _cam_relays реэкспортируется КАК ОБЪЕКТ (мутируется, не пере-присваивается); плоские
# константы (_CAM_LINGER_S, _UPLOAD_MAX_BYTES, _PRINTER_ACTIONS) — намеренно нет.
# Регистрация маршрутов осталась в make_app строками на фасадные имена.
from .web import printerlive  # noqa: E402,F401,I001
from .web.printerlive import _CamRelay  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_camera  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_camera_check  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_control  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_delete  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_fan  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_files  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_heat  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_home  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_load  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_move  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_unload  # noqa: E402,F401,I001
from .web.printerlive import _api_admin_printer_upload  # noqa: E402,F401,I001
from .web.printerlive import _cam_relay_for  # noqa: E402,F401,I001
from .web.printerlive import _cam_relays  # noqa: E402,F401,I001
from .web.printerlive import _grab_camera_frame  # noqa: E402,F401,I001
from .web.printerlive import _printer_cmd_endpoint  # noqa: E402,F401,I001
from .web.printerlive import _printer_printing  # noqa: E402,F401,I001
from .web.printerlive import _remember_gcode_settings  # noqa: E402,F401,I001
from .web.printerlive import capture_bed_shot  # noqa: E402,F401,I001
from .web.printerlive import copy_journal_photo_to_order  # noqa: E402,F401,I001

# Файлы принтера и заливка уехали в app/web/printerlive.py (волна 4) — фасад выше по файлу.


# Ручной пульт уехал в app/web/printerlive.py (волна 4) — фасад выше по файлу.


# ───────── Привязка печати к заказу + подтверждение + таймлапс (OpenTodo 70b5a590) ─────────
# Пласт медиа таймлапсов уехал в app/printmedia.py (волна 3 цикла 2): скачивание с принтера,
# авто-кэш по имени задания, самолечение записей журнала и доставка клиенту. Здесь остаётся
# ФАСАД — ЯВНОЕ перечисление, по одной строке на имя: `from .printmedia import *` приватные
# имена не берёт, а `__all__` в этом модуле нет вовсе. Реэкспорт держит весь код и тесты,
# которые знают эти имена как webapp.*; забытое имя отказывает не на старте, а в момент вызова
# у живого клиента. ruff помечает каждую строку фасада как fixable (F401) — `ruff check --fix`
# на этом блоке запускать НЕЛЬЗЯ, автофикс молча снесёт реэкспорт; гасим построчно через noqa.
#
# Модульный `_bot` и `set_bot` у printmedia СВОИ и сюда НЕ реэкспортируются (иначе затёрли бы
# одноимённые webapp'а); ставит их run.wire_bot().
from .printmedia import _TIMELAPSE_DIR  # noqa: E402,F401,I001
from .printmedia import _TG_UPLOAD_LIMIT  # noqa: E402,F401,I001
from .printmedia import _http_get_bytes  # noqa: E402,F401,I001
from .printmedia import _download_to  # noqa: E402,F401,I001
from .printmedia import _timelapse_has_video  # noqa: E402,F401,I001
from .printmedia import _timelapse_file_name  # noqa: E402,F401,I001
from .printmedia import _timelapse_download_url  # noqa: E402,F401,I001
from .printmedia import _timelapse_abs_url  # noqa: E402,F401,I001
from .printmedia import _timelapse_candidate_urls  # noqa: E402,F401,I001
from .printmedia import _download_timelapse  # noqa: E402,F401,I001
from .printmedia import _TIMELAPSE_AUTO_DIR  # noqa: E402,F401,I001
from .printmedia import _safe_job_name  # noqa: E402,F401,I001
from .printmedia import cached_timelapse_path  # noqa: E402,F401,I001
from .printmedia import cache_timelapse_from_url  # noqa: E402,F401,I001
from .printmedia import _heal_journal_timelapse  # noqa: E402,F401,I001
from .printmedia import deliver_late_timelapses  # noqa: E402,F401,I001
from .printmedia import _deliver_timelapses  # noqa: E402,F401,I001
from .printmedia import _deliver_timelapse  # noqa: E402,F401,I001
from .printmedia import _file_size  # noqa: E402,F401,I001


async def _collect_order_bed_photos(rid: int) -> list[Path]:
    """Скрины стола печатей заказа для альбома клиенту (OpenTodo 71523f3f): тот же фильтр, что в
    _order_prints_media для клиента — без полностью бракованных печатей (их перепечатывают),
    только живые файлы (сознательно удалённое мастером не воскрешаем)."""
    photos: list[Path] = []
    for e in await db.list_print_journal_for_request(int(rid)):
        if float(e.get("fail_pct") or 0) >= 100:
            continue
        ph = e.get("done_photo")
        if ph and Path(ph).exists():
            photos.append(Path(ph))
    return photos


async def _collect_order_timelapses(r) -> tuple[list[Path], set[int]]:
    """Все локальные видео печатей заказа из журнала (per-печать, без полностью бракованных) +
    их размеры. Дедуп по размеру файла: копии одного видео (авто-кэш vs скачанный под заказ)
    различаются только путём."""
    vids, sizes = [], set()
    for e in await db.list_print_journal_for_request(r.id):
        if float(e.get("fail_pct") or 0) >= 100:
            continue
        p = await _heal_journal_timelapse(e)     # пустое поле лечится из авто-кэша (2bc57e3e)
        sz = _file_size(p) if p else 0
        if sz and sz not in sizes:
            sizes.add(sz)
            vids.append(Path(p))
    return vids, sizes


async def _attach_and_deliver_timelapse(r) -> bool:
    """Best-effort: собрать ВСЕ таймлапсы печатей заказа (журнальные, per-печать — многостольный
    заказ печатается в несколько заходов) плюс свежайшую печать с принтера (её журнальная запись
    могла ещё не получить видео — принтер дорисовывает его минуты), привязать к заказу и отправить
    клиенту. Любая ошибка некритична — печать уже отмечена напечатанной, таймлапсы просто не
    приложатся (домыслов по SDCP нет, всё под guard)."""
    try:
        vids, sizes = await _collect_order_timelapses(r)
        latest = None
        drv = await _printer_driver()
        if drv.enabled() and drv.caps.timelapse:
            b = next((x for x in await db.list_print_bindings()
                      if x.get("request_id") == r.id), None)
            job_name = (b or {}).get("job_name") or None
            # сначала локальный авто-кэш (c084f065) — мгновенно и не зависит от принтера
            cached = cached_timelapse_path(job_name or "", _driver_printer_id(drv))
            if cached:
                latest = _TIMELAPSE_DIR / f"{r.id}.mp4"
                latest.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copyfile, cached, latest)
            else:
                info = await asyncio.to_thread(drv.latest_timelapse_url, job_name)
                if info.get("ok"):
                    latest = await _download_timelapse(info["url"], r.id, _printer_ep(drv))
        if latest and _file_size(latest) not in sizes:
            vids.append(latest)
        if not vids:
            return False
        # одиночное поле заказа заполняем всегда (совместимость: старые карточки, чистка при
        # удалении заказа); копия — журнальный файл живёт своей жизнью в кэше авто-выгрузки
        if not latest:
            latest = _TIMELAPSE_DIR / f"{r.id}.mp4"
            latest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, vids[-1], latest)
        await db.update_request(r.id, timelapse=str(latest))
        # «юзер видит все таймлапсы» + скрины стола печатей тем же альбомом (71523f3f)
        await _deliver_timelapses(r, vids, await _collect_order_bed_photos(r.id))
        return True
    except Exception:  # noqa: BLE001
        log.warning("attach timelapse failed", exc_info=True)
        return False


async def confirm_print(rid: int, ok: bool) -> dict:
    """Подтверждение мастером завершённой печати (OpenTodo 70b5a590). ok=True → заказ «напечатано»
    (как обычный перевод: сэмпл времени, начисление ТО, уведомление+оценка клиенту) и best-effort
    привязка/отправка таймлапса. Вызывается из мини-аппы и из Telegram-кнопки уведомления."""
    r = await db.get_request(rid)
    if not r:
        return {"ok": False, "error": "Заказ не найден"}
    if not ok:
        await db.clear_print_binding_for_request(rid)
        return {"ok": True, "confirmed": False}
    if r.status != "printed":
        await db.update_request(rid, status="printed")
        r = await db.get_request(rid)
        await _capture_print_sample(r, {})       # факт времени из телеметрии (ручного ввода нет)
        await tuning.capture_features(r)         # этап 0 автотюнинга: заморозить ручки печати
        await _accrue_service_on_print(r)
        await _consume_order_stock(r)            # резерв → реальное списание склада (a89586a2)
        await _maybe_referral_reward(r.user_id)  # рефералка: напечатан (+оплачен?) → бонус
        if _bot is not None and not db.is_self_order(r):   # своему же заказу «оцените» не шлём
            try:
                await notify.notify_user(_bot, r.user_id, texts.user_status_note(r, config.CURRENCY),
                                         reply_markup=keyboards.rate_request(rid))
            except Exception:  # noqa: BLE001
                log.debug("confirm-print notify failed", exc_info=True)
    await _attach_and_deliver_timelapse(r)
    await db.clear_print_binding_for_request(rid)
    # «модель на столе» гасим у машины, где печатался ИМЕННО этот заказ: разбор печати на
    # одной машине не должен гасить экран второй
    lcd_print_handled(await _request_printer_id(rid))
    return {"ok": True, "confirmed": True}


async def _api_admin_print_classify(request: web.Request) -> web.Response:
    """Данные для экрана «чья печать?» (OpenTodo 4effbc1b): запись журнала + активные
    клиентские заказы с картинками — мастер выбирает «по красоте», а не вводит номер."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    jid = _toint(request.query.get("jid"))
    entry = await db.get_print_journal(jid) if jid else None
    if not entry:
        return web.json_response({"ok": False, "error": "Запись журнала не найдена"})
    # Клиентские заказы ПЛЮС заказы самого мастера (user_id ∈ ADMIN_IDS): они живут в отдельном
    # списке kind='self' и раньше сюда не попадали — печать по заказу, оставленному админом,
    # было не к чему привязать (OpenTodo 90c4ac36). Дедуп по id, свежие сверху — как в BindCard.
    rows = (await db.list_requests(40, kind="client")) + (await db.list_requests(40, kind="self"))
    seen: set[int] = set()
    orders = []
    for r in sorted(rows, key=lambda x: -x.id):
        if r.id in seen or r.status not in ("new", "awaiting_payment", "in_progress",
                                            "price_changed", "invoiced"):
            continue
        seen.add(r.id)
        orders.append({"id": r.id, "label": r.model_label, "user": r.user_name,
                       "status": r.status,
                       "status_label": db.REQ_STATUS_LABELS.get(r.status, r.status),
                       "qty": r.quantity, "price": r.price,
                       "preview": (signed_url("req", r.id, {"v": _file_ver(r.preview_path)})
                                   if r.preview_path else "")})
    return web.json_response({"ok": True, "currency": config.CURRENCY, "print": {
        "jid": jid, "label": entry.get("label") or "печать",
        "grams": entry.get("grams") or 0, "minutes": entry.get("minutes") or 0,
        "plastic": entry.get("plastic_name") or "",
        "request_id": entry.get("request_id")}, "orders": orders})


async def _api_admin_print_classify_pick(request: web.Request) -> web.Response:
    """Выбор на экране «чья печать?»: {jid, action: 'order'|'self', id}. Делает РОВНО то же,
    что pe:-кнопки в чате (прикрепить к заказу / учесть самопечатью), затем гасит дубль-
    уведомление раздела и шлёт мастеру в чат обычную оценку результата печати."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    jid = _toint(body.get("jid"))
    action = str(body.get("action") or "")
    entry = await db.get_print_journal(jid) if jid else None
    if not entry:
        return web.json_response({"ok": False, "error": "Запись журнала не найдена"})
    from .handlers import user_flow  # локальный импорт: webapp ↔ handlers циклически
    name = entry.get("label") or "печать"
    if action == "order":
        ok, msg = await user_flow._attach_print_to_order(jid, _toint(body.get("id")))
        if not ok:
            return web.json_response({"ok": False, "error": re.sub(r"<[^>]+>", "", msg)})
    elif action == "self":
        # Идемпотентность: повторный «Для себя» создал бы ВТОРОЙ заказ-самопечать с полной
        # ценой (старый остался бы сиротой с self_print=1, и admin_would_spend просуммировал
        # бы оба). Признак «уже разобрано» — не сам request_id (printwatch проставляет его при
        # создании записи для печати по заказу), а то, что текущий заказ записи помечен
        # self_print. Легко нарваться: карточка в «Уведомлениях» переживала разбор из чата и
        # держала живую кнопку выбора заказа.
        if await db.is_self_print_order(_toint(entry.get("request_id"))):
            await user_flow._dismiss_print_classify_notif(jid)   # заодно убираем зависшую карточку
            return web.json_response({"ok": False, "error": "Эта печать уже учтена как самопечать"})
        msg = await user_flow._register_self_print(uid, jid, entry, name)
    else:
        return web.json_response({"ok": False, "error": "Неизвестное действие"})
    await db.clear_print_binding(entry.get("printer_id"))
    # гасим дубль в «Уведомлениях» приложения (создан printwatch-ом при финише печати)
    await user_flow._dismiss_print_classify_notif(jid)
    lcd_print_handled(entry.get("printer_id"))   # печать разобрана → LCD-экран: done → idle
    # Опрос качества в Telegram выпилен (OpenTodo b02bc4c6): оценка (успех/брак) ставится
    # в «Журнале печатей» приложения — неоценённая запись и так ждёт мастера там.
    return web.json_response({"ok": True, "message": re.sub(r"<[^>]+>", "", msg)})


# Пакетный расчёт ссылок (OpenTodo 89251db6) уехал в app/web/linkbatch.py (волна 13
# endgame) — реэкспорт в фасадном блоке linkbatch ниже.


# Пакетный расчёт ссылок уехал в app/web/linkbatch.py (волна 13 endgame). ФАСАД по общим
# правилам: явное перечисление, noqa построчно; автофикс ruff на блоке не запускать.
# Словарь _batches реэкспортируется КАК ОБЪЕКТ (мутируется, не пере-присваивается);
# _BATCH_MAX_LINKS/_BATCH_KEEP — плоские константы: подменять в тестах в самом linkbatch.
# Бота модулю раздаёт webapp.set_bot веером _BOT_FAN (волна 12).
from .web import linkbatch  # noqa: E402,F401,I001
from .web.linkbatch import _BATCH_KEEP  # noqa: E402,F401,I001
from .web.linkbatch import _BATCH_MAX_LINKS  # noqa: E402,F401,I001
from .web.linkbatch import _api_admin_batch_calc  # noqa: E402,F401,I001
from .web.linkbatch import _api_admin_batch_cancel  # noqa: E402,F401,I001
from .web.linkbatch import _api_admin_batch_last  # noqa: E402,F401,I001
from .web.linkbatch import _api_admin_batch_send  # noqa: E402,F401,I001
from .web.linkbatch import _api_admin_batch_status  # noqa: E402,F401,I001
from .web.linkbatch import _batch_defaults  # noqa: E402,F401,I001
from .web.linkbatch import _batch_json  # noqa: E402,F401,I001
from .web.linkbatch import _batch_preview  # noqa: E402,F401,I001
from .web.linkbatch import _batches  # noqa: E402,F401,I001
from .web.linkbatch import _extract_links  # noqa: E402,F401,I001
from .web.linkbatch import _run_batch  # noqa: E402,F401,I001


async def _api_admin_print_confirm(request: web.Request) -> web.Response:
    """Мастер подтверждает завершённую печать из мини-аппы (то же делает Telegram-кнопка)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    return web.json_response(await confirm_print(_toint(body.get("id")), bool(body.get("ok"))))


# Гигиена списка заказов (тестовые/просмотрено/удаление) уехала в app/web/orderlist.py
# (волна 7 endgame-плана). ФАСАД по общим правилам; права путей держатся на «закрыто по
# умолчанию» — в _OPERATOR_ROUTES их не вписывать.
from .web import orderlist  # noqa: E402,F401,I001
from .web.orderlist import _api_admin_order_delete  # noqa: E402,F401,I001
from .web.orderlist import _api_admin_orders_clean_test  # noqa: E402,F401,I001
from .web.orderlist import _api_admin_orders_mark_all_seen  # noqa: E402,F401,I001
from .web.orderlist import _cleanup_request_files  # noqa: E402,F401,I001


# Очередь печати, самопечать и обслуживание уехали в app/web/printerjob.py (волна 13
# endgame) к остальным принтерным операциям мастера. Бота модулю раздаёт webapp.set_bot
# веером _BOT_FAN (волна 12). ФАСАД по общим правилам, noqa построчно.
from .web.printerjob import _api_admin_queue  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_queue_move  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_self_print  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_service  # noqa: E402,F401,I001
from .web.printerjob import _api_admin_service_action  # noqa: E402,F401,I001


# ---- Фаза 2: каталог (пластики / промокоды / коллекция) ----


async def _api_admin_plastics(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    ps = await db.list_plastics(active_only=False)
    cstock = {p.id: await db.get_color_stock(p.id) for p in ps}
    # Цвета, чей остаток ведёт принтер (катушки CC2 Control): экраны показывают их остаток только
    # для чтения — его пересчитывает сверка. '' — общий склад без цвета.
    owned = {p.id: await db.cc2_owned_colors(p.id) for p in ps}
    overrides = await db.get_color_overrides()
    reserved = await db.reserved_grams_by_plastic()   # резерв под активные заказы (a89586a2)
    # Оператору (A4) справочник нужен для смены материала в заказе, но закупочная цена
    # килограмма и коэффициент сложности — это прайс: их в ответе не будет.
    money = _money_visible(request)
    payload = {"ok": True, "currency": config.CURRENCY, "money": money,
        "material_types": list(materials.MATERIAL_TYPES),
        "plastic_categories": list(materials.PLASTIC_CATEGORIES),
        "plastics": [{"id": p.id, "name": p.name, "price": p.price_per_kg, "density": p.density,
                      "material": p.material_type, "description": p.description, "colors": p.color_list,
                      "category": p.category or materials.category_hint(p.material_type, p.name),
                      "is_active": bool(p.is_active), "is_default": bool(p.is_default),
                      "stock": p.stock_grams, "low": p.low_threshold, "uses": p.uses,
                      "reserved": round(reserved.get(p.id, 0.0), 1),   # синий сегмент на полосе
                      "color_stock": cstock.get(p.id, {}),
                      "cc2_colors": list(dict.fromkeys(
                          c for c in (list(cstock.get(p.id, {})) + p.color_list)
                          if db._canon_color(c) in owned[p.id])),
                      "cc2_stock": "" in owned[p.id],
                      "color_css": {c: _color_css(c, overrides) for c in p.color_list},
                      "failure": p.failure_rate_pct,
                      "factor": p.price_factor,   # коэффициент сложности (×цена; клиент не видит)
                      # тара катушки: остаток = показание весов − это число (клиенту не идёт)
                      "spool_weight": p.spool_weight_g, "spool_note": p.spool_note,
                      "best_quality_id": p.best_quality_id,   # лучший профиль печати (94cc52c7)
                      "photo": _photo_url("plastic", p.id, p.photo_path),
                      # фото катушек по цветам (OpenTodo eb9a9efe): {цвет: url} — только у тех
                      # цветов, для которых файл реально лежит в data/spool_photos/{id}/
                      "spools": {c: f"/spool/{p.id}?c={urllib.parse.quote(c)}&v={_file_ver(str(sp))}"
                                 for c in p.color_list
                                 if (sp := _spool_photo_path(p.id, c)) is not None}} for p in ps],
        "failure_default": float((await db.get_settings()).get("failure_rate_pct", 0.0))}
    if not money:
        for p in payload["plastics"]:
            _hide_money(p, ("price", "factor"))
    return web.json_response(payload)


async def _api_admin_plastic(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    pid = _toint(body.get("id"))
    if action == "save":
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"ok": False, "error": "Введите название"})
        try:
            price = float(str(body.get("price")).replace(",", "."))
            density = float(str(body.get("density")).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Цена и плотность — числа"})
        material = (body.get("material") or "PLA").strip()
        desc = (body.get("description") or "").strip()
        cval = body.get("colors")
        colors = ", ".join(c.strip() for c in cval if c and c.strip()) if isinstance(cval, list) \
            else (cval or "").strip()
        # Индивид. % брака: пусто → NULL (берётся общий из настроек); иначе число.
        fraw = body.get("failure")
        failure = None
        if fraw not in (None, ""):
            try:
                failure = float(str(fraw).replace(",", "."))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "% брака — число или пусто"})
        # Коэффициент сложности работы с материалом: пусто → 1 (обычный). Клиент коэффициента
        # не видит — витрины показывают цену уже умноженной (Plastic.eff_price_per_kg).
        fcraw = body.get("factor")
        factor = 1.0
        if fcraw not in (None, ""):
            try:
                factor = float(str(fcraw).replace(",", "."))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "Коэффициент — число (например 1,2) или пусто"})
            if not 0.1 <= factor <= 10:
                return web.json_response({"ok": False, "error": "Коэффициент — от 0,1 до 10"})
        # лучший профиль печати этого пластика (94cc52c7): id пресета качества или NULL
        bq = _toint(body.get("best_quality_id")) or None
        # категория витрины (3af25fc2): не из списка/пусто → авто-подсказка по типу/названию
        category = (body.get("category") or "").strip()
        if category not in materials.PLASTIC_CATEGORIES:
            category = materials.category_hint(material, name)
        # Тара катушки и оговорки к ней. Ключей может НЕ БЫТЬ вовсе — старая версия фронта их не
        # шлёт, а 'save' перезаписывает поля разом: подставь мы здесь 0/'' по умолчанию, замер
        # тары стёрло бы первое же сохранение карточки из необновлённого приложения.
        extra: dict[str, object] = {}
        if "spool_weight" in body:
            try:
                extra["spool_weight_g"] = max(0.0, float(str(body.get("spool_weight") or 0).replace(",", ".")))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "Вес катушки — число или пусто"})
        if "spool_note" in body:
            extra["spool_note"] = (body.get("spool_note") or "").strip()
        if pid:
            await db.update_plastic(pid, name=name, price_per_kg=price, density=density,
                                    material_type=material, description=desc, colors=colors,
                                    failure_rate_pct=failure, best_quality_id=bq,
                                    price_factor=factor, category=category, **extra)
        else:
            pid = await db.add_plastic(name, price, density, material)
            await db.update_plastic(pid, description=desc, colors=colors, failure_rate_pct=failure,
                                    best_quality_id=bq, price_factor=factor, category=category,
                                    **extra)
        return web.json_response({"ok": True, "id": pid})
    if action == "stock":
        try:
            stock = float(str(body.get("stock") or 0).replace(",", "."))
            low = float(str(body.get("low") or 0).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Остаток и порог — числа"})
        if "" in await db.cc2_owned_colors(pid):      # общий склад ведёт принтер — только порог
            await db.update_plastic(pid, low_threshold=low)
            return web.json_response({"ok": True, "cc2": True})
        await db.update_plastic(pid, stock_grams=stock, low_threshold=low)
        return web.json_response({"ok": True})
    if action == "color_stock":
        color = (body.get("color") or "").strip()
        if not color:
            return web.json_response({"ok": False, "error": "Не указан цвет"})
        try:
            stock = float(str(body.get("stock") or 0).replace(",", "."))
            low = float(str(body.get("low") or 0).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Остаток и порог — числа"})
        # Остаток цвета, который ведёт принтер (катушки CC2 Control), — сумма катушек: его
        # пересчитывает сверка, и ручное число затёрлось бы ближайшей. Порог — наш, его пишем.
        if db._canon_color(color) in await db.cc2_owned_colors(pid):
            await db.set_color_low(pid, color, low)
            return web.json_response({"ok": True, "cc2": True})
        await db.set_color_stock(pid, color, stock, low)
        return web.json_response({"ok": True})
    if action == "toggle":
        p = await db.get_plastic(pid)
        if p:
            await db.update_plastic(pid, is_active=0 if p.is_active else 1)
        return web.json_response({"ok": True})
    if action == "default":
        await db.set_default_plastic(pid)
        return web.json_response({"ok": True})
    if action == "delete":
        await db.delete_plastic(pid)
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})


def _admin_num(v, dflt):
    """Число из строки админ-формы ('12,5'→12.5); пусто/мусор → dflt."""
    if v in (None, ""):
        return dflt
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return dflt


async def _api_admin_spools(request: web.Request) -> web.Response:
    """Катушки пластика (Фаза A мастер-плана): по plastic_id (query) или все. Для каждой —
    остаток/номинал/цвет/статус/цена/% + последние движения; плюс агрегат остатка по цвету.

    Есть принтер с учётом катушек CC2 Control — блок `cc2` (связь, вопросы лотков, катушки
    принтера без пары) и живые поля катушек принтера (cc2spools.decorate). Снимок берётся по
    ревизии, а не опросом на каждый запрос: принтер слабый."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    pid = _toint(request.query.get("plastic_id")) or None
    money = _money_visible(request)   # оператору (A4) остаток нужен, закупочная цена — нет
    src = await cc2spools.source()
    cc2 = await cc2spools.view(src) if src else None
    snap = cc2spools.snapshot(src.host)[0] if src else None
    out = []
    for s in await db.list_spools(pid):
        led = (await db.spool_ledger(s["id"]))[-8:]
        pct = round(100 * s["remaining_g"] / s["net_weight_g"]) if s["net_weight_g"] > 0 else None
        row = cc2spools.decorate({**s, "pct": pct, "ledger": led}, snap)
        out.append(row if money else _hide_money(row, ("cost", "supplier")))
    by_color = await db.spool_stock_by_color(pid) if pid else {}
    return web.json_response({"ok": True, "currency": config.CURRENCY, "money": money,
                              "spools": out, "by_color": by_color, "cc2": cc2})


async def _api_admin_spool(request: web.Request) -> web.Response:
    """Приход катушки (add), инвентаризация (correction — ввод факт. веса), взвешивание
    (weigh — вес на весах минус тара), ручное движение (move — продувка/списание/приход) и
    статус (Фаза A мастер-плана учёта).

    Есть принтер с учётом катушек CC2 Control — приход и всё, что касается его катушек, уходит
    в его службу (cc2spools.handle), плюс действия cc2_* (связать, взять в учёт, ответить на
    вопрос лотка, включить учёт). Своё, непринтерное, — как раньше."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    res = await cc2spools.handle(await cc2spools.source(), body)
    if res is not None:
        return web.json_response(res)
    if action == "add":                                   # приход катушки
        pid = _toint(body.get("plastic_id"))
        if not pid or not await db.get_plastic(pid):
            return web.json_response({"ok": False, "error": "Не выбран пластик"})
        net = _admin_num(body.get("net_weight"), 0.0)
        if net <= 0:
            return web.json_response({"ok": False, "error": "Вес нетто — число больше нуля"})
        sid = await db.add_spool(
            pid, (body.get("color") or "").strip(), net,
            low_threshold=max(0.0, _admin_num(body.get("low"), 0.0)),
            cost=_admin_num(body.get("cost"), None), supplier=(body.get("supplier") or "").strip() or None,
            note=(body.get("note") or "").strip() or None)
        return web.json_response({"ok": True, "id": sid})
    sid = _toint(body.get("spool_id"))
    if not sid or not await db.get_spool(sid):
        return web.json_response({"ok": False, "error": "Катушка не найдена"})
    if action == "correction":                            # инвентаризация: ввод фактического веса
        actual = _admin_num(body.get("actual"), None)
        if actual is None:
            return web.json_response({"ok": False, "error": "Фактический вес — число"})
        sp = await db.get_spool(sid)
        rem = await db.add_stock_ledger(sid, max(0.0, actual) - sp["remaining_g"], "correction",
                                        note=(body.get("note") or "").strip() or None)
        return web.json_response({"ok": True, "remaining_g": rem})
    if action == "weigh":                                 # катушка на весах целиком − тара пластика
        gross = _admin_num(body.get("gross"), None)
        sp = await db.get_spool(sid)
        p = await db.get_plastic(sp["plastic_id"])
        tare = _admin_num(body.get("tare"), float(p.spool_weight_g or 0) if p else 0.0)
        if gross is None or gross < 0:
            return web.json_response({"ok": False, "error": "Вес на весах — число"})
        if not tare or tare <= 0:
            return web.json_response({"ok": False, "error": "Укажите вес пустой катушки в карточке пластика"})
        if gross < tare:
            return web.json_response({"ok": False, "error": f"На весах меньше тары ({tare:g} г)"})
        rem = await db.add_stock_ledger(sid, (gross - tare) - sp["remaining_g"], "correction",
                                        note=f"взвешено: {gross:g} г − тара {tare:g} г")
        return web.json_response({"ok": True, "remaining_g": rem})
    if action == "move":                                  # ручное движение (+ приход, − расход/продувка)
        delta = _admin_num(body.get("delta"), None)
        if delta is None:
            return web.json_response({"ok": False, "error": "Дельта — число (+ приход, − расход)"})
        reason = (body.get("reason") or "correction").strip()
        if reason not in ("purchase", "correction", "purge", "writeoff", "print"):
            reason = "correction"
        rem = await db.add_stock_ledger(sid, delta, reason,
                                        note=(body.get("note") or "").strip() or None)
        return web.json_response({"ok": True, "remaining_g": rem})
    if action == "status":                                # active | empty | archived
        await db.set_spool_status(sid, (body.get("status") or "").strip())
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})


# ---- Фаза 3: настройки (ценообразование/реквизиты/скидки/оттенки/пресеты/принтеры) ----


async def _api_admin_bigdata(request: web.Request) -> web.Response:
    """Собранные данные для алгоритмов (OpenTodo 7507fba0): точность модели времени
    (оценка vs факт), расход материалов, переиспользование моделей, покрытие замерами.
    + журнал печатей мастера (брак по пластикам, OpenTodo cacfae14/ed1518ed)."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    return web.json_response({"ok": True, **await db.bigdata_stats(),
                              "journal": await db.print_journal_stats()})


async def _api_admin_activity(request: web.Request) -> web.Response:
    """Лента действий пользователей для админа (OpenTodo f0602c66): только просмотр, без
    уведомлений. ?user_id=N — отфильтровать по конкретному клиенту."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    user_id = _toint(request.query.get("user_id")) or None
    rows = await db.list_activity(limit=150, user_id=user_id)
    # админов помечаем и не показываем голым id (OpenTodo a5db500c)
    for r in rows:
        if r.get("user_id") in config.ADMIN_IDS:
            r["admin"] = 1
            r["name"] = r.get("name") or "Мастер"
    return web.json_response({"ok": True, "events": rows})


# ── Цены и скидки: промокоды, тиражные и персональные скидки, опции постобработки, калькулятор ──
# Блок уехал в app/web/prices.py (волна «Цены и скидки» цикла 2) целиком. Маршруты объявлены
# там же, рядом с обработчиками, и включаются в make_app одной строкой
# `app.add_routes(prices.routes)` — потерять ручку при переносе нечем.
#
# Ниже — ФАСАД по тем же правилам, что у printmedia/websign/web.common/webauth/webguard/calib/
# equipment выше: ЯВНОЕ перечисление, по строке на имя. `from .web.prices import *` приватные
# имена не берёт, а `__all__` в этом модуле нет вовсе. Реэкспорт держит тесты и весь код,
# который знает эти имена как webapp.*; забытая строка отказывает не на старте, а в момент
# вызова у живого клиента. `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ — автофикс молча
# снесёт реэкспорт; гасим построчно через noqa.
from .web import prices  # noqa: E402,F401,I001
from .web.prices import _api_admin_discount  # noqa: E402,F401,I001
from .web.prices import _api_admin_discounts  # noqa: E402,F401,I001
from .web.prices import _api_admin_option  # noqa: E402,F401,I001
from .web.prices import _api_admin_options  # noqa: E402,F401,I001
from .web.prices import _api_admin_price_calc  # noqa: E402,F401,I001
from .web.prices import _api_admin_promo  # noqa: E402,F401,I001
from .web.prices import _api_admin_promos  # noqa: E402,F401,I001


# ═══════════════════════════ Доставка: тарифы и отправления (A2) ═══════════════════════════
# Доставка (справочник типов, доставка и отгрузка заказа) уехала в app/web/shipping.py
# (волна 13 endgame). ФАСАД по общим правилам: явное перечисление, noqa построчно; автофикс
# ruff на блоке не запускать. Бота модулю раздаёт webapp.set_bot веером _BOT_FAN (волна 12).
from .web import shipping  # noqa: E402,F401,I001
from .web.shipping import _api_admin_delivery  # noqa: E402,F401,I001
from .web.shipping import _api_admin_delivery_type  # noqa: E402,F401,I001
from .web.shipping import _api_admin_order_delivery  # noqa: E402,F401,I001
from .web.shipping import _api_admin_order_shipment  # noqa: E402,F401,I001
from .web.shipping import _api_delivery_types  # noqa: E402,F401,I001
from .web.shipping import _group_or_error  # noqa: E402,F401,I001


# ═══════════════════════════ Команда мастерской: роли и права (A4) ═══════════════════════════


# ═══════════════ Центр уведомлений админа + семейные счета (2026-07-16) ═══════════════
# Пришло на смену «Объединить счета» (merge_user_accounts): слияние уничтожало историю
# второго клиента. Семейный счёт = общий ТОЛЬКО баланс, участники равноправны, число любое.


async def _api_admin_usage(request: web.Request) -> web.Response:
    """Usage-агрегат обращений к эндпоинтам за N суток (аудит 07.2026, реком. 6): замена
    ротируемым текстовым access-логам. ?days=7 по умолчанию."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    days = _toint(request.query.get("days")) or 7
    return web.json_response({"ok": True, "days": days, "rows": await db.usage_stats(days)})


# ───────────────────── Телеметрия ошибок (OpenTodo f4de8627) ─────────────────────


async def _api_jserr(request: web.Request) -> web.Response:
    """Ошибка JS мини-аппа: фронт падает молча, и без этой ручки о белом экране у клиента
    узнать неоткуда. Жёсткий лимит (3 за 5 минут на пользователя) — цикл вида «ошибка в
    обработчике ошибок» иначе завалил бы телеметрию за секунды."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"jserr:{uid}", 3, 300):
        return web.json_response({"ok": False, "error": "too many"})
    text = str(body.get("text") or "").strip()[:500]
    if not text:
        return web.json_response({"ok": False, "error": "empty"})
    errorlog.capture("js", text, context=f"uid {uid}")
    return web.json_response({"ok": True})


# ── Оборудование: карточки принтеров и пресеты печати ──────────────────────────────────────
# Блок уехал в app/web/equipment.py (волна «Оборудование» цикла 2) целиком: пресеты качества
# и прочности, карточки принтеров с соплами, протоколом связи и выбором слайсера. Маршруты
# объявлены там же, рядом с обработчиками, и включаются в make_app одной строкой
# `app.add_routes(equipment.routes)` — потерять ручку при переносе нечем.
#
# Ниже — ФАСАД по тем же правилам, что у printmedia/websign/web.common/webauth/webguard/calib
# выше: ЯВНОЕ перечисление, по строке на имя. `from .web.equipment import *` приватные имена
# не берёт, а `__all__` в этом модуле нет вовсе. Реэкспорт держит тесты и весь код, который
# знает эти имена как webapp.*; забытая строка отказывает не на старте, а в момент вызова у
# живого клиента. `ruff check --fix` на этом блоке запускать НЕЛЬЗЯ — автофикс молча снесёт
# реэкспорт; гасим построчно через noqa.
from .web import equipment  # noqa: E402,F401,I001
from .web.equipment import _api_admin_preset  # noqa: E402,F401,I001
from .web.equipment import _api_admin_presets  # noqa: E402,F401,I001
from .web.equipment import _api_admin_printer  # noqa: E402,F401,I001
from .web.equipment import _api_admin_printers  # noqa: E402,F401,I001


# _photo_url уехал в app/web/common.py (волна 4 цикла 2): его зовёт и этот файл, и блок
# калибровки, а общий дом обоим нужен такой, который никого из них не импортирует.


async def _api_admin_photo(request: web.Request) -> web.Response:
    """Загрузка фото катушки/печати прямо из приложения (42f90942). data — base64 (dataURL)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    kind = body.get("kind", "")
    pid = _toint(body.get("id"))
    if kind not in ("plastic", "collection", "order", "journal", "product", "notif", "spool",
                    "calib"):
        return web.json_response({"ok": False, "error": "Неизвестный тип"})
    data = body.get("data") or ""
    if "," in data and data[:5].lower() == "data:":
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001
        raw = b""
    if not raw or len(raw) > 12 * 1024 * 1024:
        return web.json_response({"ok": False, "error": "Некорректное изображение"})
    # Фото катушки конкретного цвета: лежит файлом на диске (data/spool_photos/{id}/{цвет}.jpg),
    # своего поля в БД нет — читается по факту наличия файла (_spool_photo_path). Отдельная
    # ветка, потому что путь зависит от цвета, а не только от id.
    if kind == "spool":
        if not await db.get_plastic(pid):
            return web.json_response({"ok": False, "error": "Пластик не найден"}, status=404)
        color = str(body.get("color") or "").strip()
        if not color:
            return web.json_response({"ok": False, "error": "Не указан цвет катушки"})
        d = config.DATA_DIR / "spool_photos" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / (re.sub(r"[^\w\-]+", "_", color) + ".jpg")
        try:
            dest.write_bytes(raw)
            await asyncio.to_thread(preview.optimize_photo, dest)
        except Exception:  # noqa: BLE001
            log.warning("save spool photo failed", exc_info=True)
            return web.json_response({"ok": False, "error": "Не удалось сохранить"})
        return web.json_response({"ok": True,
            "url": f"/spool/{pid}?c={urllib.parse.quote(color)}&v={_file_ver(str(dest))}"})
    if kind == "plastic" and not await db.get_plastic(pid):
        return web.json_response({"ok": False, "error": "Пластик не найден"}, status=404)
    if kind == "collection" and not await db.get_collection_item(pid):
        return web.json_response({"ok": False, "error": "Не найдено"}, status=404)
    if kind == "order" and not await db.get_request(pid):
        return web.json_response({"ok": False, "error": "Заказ не найден"}, status=404)
    if kind == "journal" and not await db.get_print_journal(pid):
        return web.json_response({"ok": False, "error": "Запись журнала не найдена"}, status=404)
    if kind == "product" and not await db.get_product(pid):
        return web.json_response({"ok": False, "error": "Товар не найден"}, status=404)
    if kind == "notif" and not await db.get_admin_notification(pid):
        return web.json_response({"ok": False, "error": "Уведомление не найдено"}, status=404)
    if kind == "calib" and not await db.get_calib_sample(pid):
        return web.json_response({"ok": False, "error": "Образец не найден"}, status=404)
    base_dir = config.NOTIF_IMG_DIR if kind == "notif" else config.COLLECTION_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    dest = base_dir / {
        "plastic": f"plastic_{pid}.jpg", "collection": f"col_{pid}.jpg",
        "journal": f"jphoto_{pid}.jpg", "product": f"product_{pid}.jpg",
        "notif": f"notif_{pid}.jpg", "calib": f"calib_{pid}.jpg"}.get(kind, f"done_{pid}.jpg")
    try:
        dest.write_bytes(raw)
        await asyncio.to_thread(preview.optimize_photo, dest)   # ресайз+сжатие вне цикла
    except Exception:  # noqa: BLE001
        log.warning("save admin photo failed", exc_info=True)
        return web.json_response({"ok": False, "error": "Не удалось сохранить"})
    if kind == "plastic":
        await db.update_plastic(pid, photo_path=str(dest))
    elif kind == "order":
        await db.update_request(pid, done_photo=str(dest))
    elif kind == "journal":
        await db.set_print_journal_fields(pid, done_photo=str(dest))
    elif kind == "product":
        await db.update_product(pid, photo_path=str(dest))
    elif kind == "notif":
        await db.update_admin_notification(pid, image_path=str(dest))
    elif kind == "calib":
        await db.set_calib_sample_photo(pid, str(dest))
    else:
        await db.set_collection_photo(pid, str(dest))
    # фото товара видно клиентам — отдаём публичный подписанный URL (не admin-signed)
    url = _product_photo_url(await db.get_product(pid)) if kind == "product" \
        else _photo_url(kind, pid, str(dest))
    resp = {"ok": True, "url": url}
    if kind == "order" and body.get("to_collection"):
        resp["collection"] = await _attach_done_to_collection(pid, dest)
    return web.json_response(resp)


async def _attach_done_to_collection(rid: int, photo: Path) -> bool:
    """Привязать фото готовой печати заказа к карточке коллекции по хэшу модели (OpenTodo
    3fe0b3fa). Файл КОПИРУЕМ в собственный путь коллекции (col_<cid>.jpg) — чтобы удаление
    заказа (которое чистит done_-фото) не забрало картинку из общей галереи. Возвращает True,
    если карточка коллекции для этой модели нашлась."""
    try:
        r = await db.get_request(rid)
        cid = await db.get_collection_id_by_hash(getattr(r, "hash", "") if r else "")
        if not cid:
            return False
        col_dest = config.COLLECTION_DIR / f"col_{cid}.jpg"
        col_dest.write_bytes(photo.read_bytes())
        await db.set_collection_photo(cid, str(col_dest))
        # отмеченная админом печать попадает в раздел «Напечатанное» каталога (c9c42789)
        it = await db.get_collection_item(cid)
        if it and not (it.category or "").strip():
            await db.set_collection_category(cid, "Напечатанное")
        return True
    except Exception:  # noqa: BLE001
        log.warning("attach done photo to collection failed", exc_info=True)
        return False


async def _admin_photo_file(request: web.Request) -> web.StreamResponse:
    """Отдать фото катушки/печати/результата заказа по подписанному URL (берём путь из БД)."""
    kind = request.match_info.get("kind", "")
    pid = _toint(request.match_info.get("id"))
    if kind not in ("plastic", "collection", "order", "journal", "notif", "chat", "calib", "calc") \
            or not check_sig(request, "aphoto", f"{kind}{pid}"):
        return web.Response(status=403)
    if kind == "calib":
        obj = await db.get_calib_sample(pid)
        path = obj.get("photo") if obj else None
        if not path or not Path(path).exists():
            return web.Response(status=404)
        return web.FileResponse(path)
    if kind == "calc":                       # превью модели из снимка сметы (26.08)
        obj = await db.get_calc_snapshot(pid)
        path = obj.get("preview_path") if obj else None
    elif kind == "chat":
        obj = await db.get_message(pid)
        path = obj.get("image_path") if obj else None
    elif kind == "plastic":
        obj = await db.get_plastic(pid)
        path = getattr(obj, "photo_path", None) if obj else None
    elif kind == "order":
        obj = await db.get_request(pid)
        path = getattr(obj, "done_photo", None) if obj else None
    elif kind == "journal":
        obj = await db.get_print_journal(pid)
        path = obj.get("done_photo") if obj else None
    elif kind == "notif":
        obj = await db.get_admin_notification(pid)
        path = obj.get("image_path") if obj else None
    else:
        obj = await db.get_collection_item(pid)
        path = getattr(obj, "photo_path", None) if obj else None
    if not path or not Path(path).exists():
        return web.Response(status=404)
    return web.FileResponse(path)


# Веер держателей бота (волна 12, вариант «б»): модули app/web/*, чьи ручки шлют клиенту.
# Кортеж собирается ЗДЕСЬ, после всех фасадных импортов; set_bot (шапка файла) читает его
# в момент вызова. Новый модуль с модульным `_bot` обязан появиться и тут, и в
# conftest._BOT_MODULES — полноту сторожит tests/test_bot_wiring.py.
_BOT_FAN = (clientflow, crm, gridfinity, linkbatch, notifhub, ordermedia, ordermoney,
            printerjob, products, quotecalc, shipping, storefront)


def make_app() -> web.Application:
    # явный лимит размера запроса под загрузку моделей (.3mf бывают десятки МБ)
    app = web.Application(client_max_size=config.MAX_DOWNLOAD_BYTES + 8 * 1024 * 1024,
                          middlewares=[_local_mw, _usage_mw, _error_mw, _compress_mw,
                                       _edition_mw, _caps_mw])
    app.router.add_get("/", _collection_page)
    app.router.add_get("/img/{cid}", _image)
    app.router.add_post("/pick", _pick)
    app.router.add_get("/catalog", _catalog_page)
    app.router.add_get("/pimg/{pid}", _plastic_image)
    app.router.add_get("/spool/{pid}", _spool_image)   # фото катушки цвета (eb9a9efe)
    app.router.add_post("/pickplastic", _pick_plastic)
    # Статика мини-аппа и деска — app/web/uifiles.py (ручки были замыканиями здесь).
    app.router.add_get("/app", _ui_file)            # /app        → index.html
    app.router.add_get("/app/", _ui_file)           # /app/       → index.html
    app.router.add_get("/app/{path:.*}", _ui_file)  # /app/app.css, /app/app.js, …
    app.router.add_get("/favicon.ico", _favicon)    # иконка с корня домена (аудит 07.2026)
    # логотип мастерской — вне /app, чтобы ссылка была короткой и одинаковой в мини-аппе,
    # настольной админке и в шапке страницы каталога
    app.router.add_get("/brand/logo", _brand_logo_file)
    app.router.add_get("/api/version", _api_version)
    # Вход в настольную админку: только с этой машины, без авторизации (это и есть дверь).
    app.router.add_post("/api/desk/login", _api_desk_login)
    app.router.add_post("/api/desk/logout", _api_desk_logout)
    app.router.add_get("/api/desk/whoami", _api_desk_whoami)
    app.router.add_get("/api/options", _api_options)
    app.router.add_get("/api/profile", _api_profile)
    app.router.add_post("/api/avatar/generate", _api_avatar_generate)
    app.router.add_get("/avatar/{uid}", _avatar_file)
    app.router.add_post("/api/model", _api_model)
    app.router.add_get("/api/parametric/models", _api_parametric_models)   # параметрические (c1cc840a)
    app.router.add_post("/api/parametric/model", _api_parametric_model)
    app.router.add_post("/api/parametric/preview2c", _api_parametric_preview2c)   # 2-цветный рендер
    app.router.add_post("/api/variant", _api_variant)
    app.router.add_post("/api/file_variant", _api_file_variant)   # комплект файлов карточки (87eb9cf3)
    app.router.add_get("/api/plate/{token}/{idx}", _api_plate)
    app.router.add_get("/api/plates/{token}", _api_plates_info)   # размеры+многоцвет столов (eb4424d9)
    app.router.add_get("/api/mpreview/{token}", _api_mpreview)
    app.router.add_get("/api/modelfile/{token}", _api_modelfile)   # сырой меш для 3D-вьюера (B1)
    app.router.add_post("/api/calc", _api_calc)
    app.router.add_post("/api/requote", _api_requote)   # смена пластика без переслайса (050aff56)
    app.router.add_post("/api/colors_sel", _api_colors_sel)   # цвета слотов → снимок заказа (2f7b597f)
    app.router.add_post("/api/calc_notify", _api_calc_notify)   # «уведомить, когда посчитается» (ae4c6c13)
    app.router.add_get("/api/quote_state", _api_quote_state)    # смета по токену из уведомления
    app.router.add_get("/api/cpreview/{token}", _api_cpreview)
    app.router.add_post("/api/order", _api_order)
    app.router.add_post("/api/order_unsliced", _api_order_unsliced)   # заказ без сметы (55a1cf8f)
    app.router.add_post("/api/order_now", _api_order_now)   # заказ во время слайса (f1e80d67)
    # Готовые изделия / витрина (bd896aa6)
    app.router.add_get("/api/products", _api_products)
    app.router.add_post("/api/product_order", _api_product_order)
    app.router.add_get("/api/admin/products", _api_admin_products)
    app.router.add_post("/api/admin/product/save", _api_admin_product_save)
    app.router.add_post("/api/admin/product/delete", _api_admin_product_delete)
    app.router.add_get("/prodphoto/{id}", _product_photo_file)
    app.router.add_post("/api/order_cart", _api_order_cart)
    app.router.add_post("/api/cart_sync", _api_cart_sync)   # серверная копия корзины (#10)
    app.router.add_get("/api/orders", _api_orders)
    app.router.add_post("/api/order/respond", _api_order_respond)
    app.router.add_post("/api/order/pay_link", _api_order_pay_link)   # онлайн-оплата (A1)
    app.router.add_post("/api/order/cancel", _api_order_cancel)
    app.router.add_post("/api/order/edit", _api_order_edit)
    app.router.add_get("/api/order/messages", _api_order_messages)
    app.router.add_post("/api/order/message", _api_order_message)
    app.router.add_post("/api/idea", _api_idea)
    app.router.add_post("/api/jserr", _api_jserr)   # ошибки JS мини-аппа → телеметрия (f4de8627)
    app.router.add_get("/api/model_search", _api_model_search)
    app.router.add_post("/api/calc_queue", _api_calc_queue)   # очередь разбора (26196ac1)
    app.router.add_get("/local/avatar_gpu/refresh", _local_avatar_gpu_refresh)   # ярлыки ИИ (ac6c7e0d)
    app.router.add_get("/api/admin/calc_queue", _api_admin_calc_queue)
    app.router.add_post("/api/admin/calc_queue_resolve", _api_admin_calc_queue_resolve)
    # журнал печатей мастера (ed1518ed) + заявка на семейный счёт (d5689a4b)
    app.router.add_get("/api/admin/journal", _api_admin_journal)
    app.router.add_post("/api/admin/journal_add", _api_admin_journal_add)
    app.router.add_post("/api/admin/journal_update", _api_admin_journal_update)   # правка (60e0b2a8)
    app.router.add_post("/api/admin/journal_delete", _api_admin_journal_delete)
    # удаление медиа печатей/заказов — скрины стола и таймлапсы (OpenTodo 1faab6cb)
    app.router.add_post("/api/admin/journal_media_delete", _api_admin_journal_media_delete)
    app.router.add_post("/api/admin/order_media_delete", _api_admin_order_media_delete)
    # Калибровка филамента: кампании, шаги, замеры, образцы, паспорта, снимки пресетов,
    # плюс страница образца по QR с этикетки (/csample/{sid}) — подписанная, без логина.
    # Пути и порядок — в таблице routes рядом с самими обработчиками (см. блок выше).
    app.add_routes(calib.routes)
    app.router.add_post("/api/family_request", _api_family_request)
    app.router.add_get("/api/collection_search", _api_collection_search)   # семантика (b56041c9)
    app.router.add_get("/api/recommend", _api_recommend)                   # лента 70/30 (df0c9674)
    app.router.add_get("/api/feed", _api_feed)                             # масонри-лента F4 (cursor/next)
    # «Мастерская органайзеров» (Gridfinity): проекты, спецификация деталей, частичный
    # заказ, мерки, заготовки, шаблон дна и ссылка «показать» — таблицей модуля одной
    # строкой (порядок внутри блока задаёт сам app/web/gridfinity.py).
    app.add_routes(gridfinity.routes)
    # «Инструменты» настольной админки: допечатка со слоя без сорвавшихся деталей (Путь В) —
    # таблицей модуля app/web/tools.py.
    app.add_routes(webtools.routes)
    # Пульт, вторая очередь: режим скорости, автодозаправка, выключатели, профили прогрева и
    # прошивка сообщества CC2 — таблицей модуля app/web/printerext.py.
    app.add_routes(printerext.routes)
    app.router.add_post("/api/favorite", _api_favorite)                    # сердечко «Избранное»
    app.router.add_post("/api/prefs", _api_prefs)                          # настройки клиента (0d11db78)
    app.router.add_get("/api/thumb", _api_thumb)
    app.router.add_get("/api/mymodels", _api_mymodels)
    app.router.add_get("/smprev/{sid}", _saved_preview)
    # Подписные медиа заказа (/reqprev, /slicershot, /timelapse): пути и порядок — в таблице
    # routes рядом с самими обработчиками (app/web/ordermedia.py).
    app.add_routes(ordermedia.routes)
    app.router.add_get("/jtl/{jid}", _jtl_file)   # таймлапс записи журнала (0f848906)
    app.router.add_post("/api/custom_order", _api_custom_order)
    app.router.add_get("/custphoto/{rid}/{idx}", _custom_photo)
    app.router.add_post("/api/use", _api_use)
    app.router.add_get("/api/collection", _api_collection)
    app.router.add_post("/api/pdf", _api_pdf)
    app.router.add_post("/api/order/pdf", _api_order_pdf)
    app.router.add_get("/api/admin/orders", _api_admin_orders)
    app.router.add_get("/api/admin/self_orders", _api_admin_self_orders)
    app.router.add_get("/api/admin/orders/export.csv", _api_admin_orders_csv)
    app.router.add_post("/api/admin/orders/clean_test", _api_admin_orders_clean_test)
    app.router.add_post("/api/admin/orders/mark_all_seen", _api_admin_orders_mark_all_seen)
    app.router.add_get("/api/admin/order/{id}", _api_admin_order)
    app.router.add_get("/api/admin/orderfile", _api_admin_orderfile)   # меш заказа (STL) для 3D в карточке мастера (B1)
    app.router.add_post("/api/admin/order/status", _api_admin_order_status)
    # Заказ на верстаке: материал заказа, слайс модели по ссылке, открыть в слайсере и
    # открыть папку на ПК бота. Пути и порядок — в таблице routes рядом с самими
    # обработчиками (см. блок выше).
    app.add_routes(orderbench.routes)
    app.router.add_post("/api/admin/order/price", _api_admin_order_price)
    app.router.add_post("/api/admin/order/recalc", _api_admin_order_recalc)
    app.router.add_post("/api/admin/order/recalc_apply", _api_admin_order_recalc_apply)
    app.router.add_post("/api/admin/order/invoice", _api_admin_order_invoice)
    app.router.add_post("/api/admin/order/create", _api_admin_order_create)
    app.router.add_post("/api/admin/balance", _api_admin_balance)
    app.router.add_post("/api/admin/balance_comment", _api_admin_balance_comment)
    app.router.add_post("/api/admin/order/message", _api_admin_order_message)
    app.router.add_post("/api/admin/order/print_net", _api_admin_order_print_net)
    app.router.add_post("/api/admin/order/delete", _api_admin_order_delete)
    app.router.add_get("/api/admin/printer_status", _api_admin_printer_status)
    app.router.add_get("/api/admin/camera", _api_admin_camera)
    app.router.add_get("/api/admin/camera_check", _api_admin_camera_check)
    app.router.add_get("/api/admin/printer_files", _api_admin_printer_files)
    app.router.add_get("/api/admin/reprint_parts", _api_admin_reprint_parts)
    app.router.add_post("/api/admin/reprint", _api_admin_reprint)
    app.router.add_post("/api/admin/reprint_start", _api_admin_reprint_start)
    app.router.add_post("/api/admin/printer_print", _api_admin_printer_print)
    app.router.add_post("/api/admin/printer_control", _api_admin_printer_control)
    app.router.add_post("/api/admin/printer_unload", _api_admin_printer_unload)
    app.router.add_post("/api/admin/printer_upload", _api_admin_printer_upload)
    app.router.add_post("/api/admin/printer_delete", _api_admin_printer_delete)
    app.router.add_post("/api/admin/printer_load", _api_admin_printer_load)
    app.router.add_post("/api/admin/printer_heat", _api_admin_printer_heat)
    app.router.add_post("/api/admin/printer_fan", _api_admin_printer_fan)
    app.router.add_post("/api/admin/printer_home", _api_admin_printer_home)
    app.router.add_post("/api/admin/printer_move", _api_admin_printer_move)
    app.router.add_post("/api/admin/order/paid", _api_admin_order_paid)   # «оплачен» (7f257a33)
    app.router.add_post("/api/admin/order/bill", _api_admin_order_bill)   # «Ждёт оплаты» (31.07)
    app.router.add_post("/api/admin/printer_bind", _api_admin_printer_bind)
    app.router.add_post("/api/admin/printer_unbind", _api_admin_printer_unbind)
    app.router.add_post("/api/admin/print_confirm", _api_admin_print_confirm)
    # выбор печати из истории + прикрепление таймлапса к заказу (OpenTodo c83a2a9e + 6b2eb433)
    app.router.add_get("/api/admin/printer_history", _api_admin_printer_history)
    app.router.add_post("/api/admin/order/attach_print", _api_admin_order_attach_print)
    # пакетный расчёт ссылок для клиента (OpenTodo 89251db6 + UX fcf355eb)
    app.router.add_post("/api/admin/batch_calc", _api_admin_batch_calc)
    app.router.add_get("/api/admin/batch_status", _api_admin_batch_status)
    app.router.add_get("/api/admin/batch_last", _api_admin_batch_last)
    app.router.add_post("/api/admin/batch_send", _api_admin_batch_send)
    app.router.add_post("/api/admin/batch_cancel", _api_admin_batch_cancel)
    app.router.add_get("/batchprev/{bid}/{idx}", _batch_preview)
    app.router.add_post("/api/admin/photo", _api_admin_photo)
    app.router.add_get("/admin/photo/{kind}/{id}", _admin_photo_file)
    app.router.add_get("/api/admin/dashboard", _api_admin_dashboard)
    app.router.add_get("/api/admin/usage", _api_admin_usage)
    # телеметрия ошибок (f4de8627) — только владельцу (в _OPERATOR_ROUTES не вносим)
    app.router.add_get("/api/admin/errors", _api_admin_errors)
    app.router.add_post("/api/admin/errors/seen", _api_admin_errors_seen)
    # база знаний владельца студии (c356387a) — владельческие ручки
    app.router.add_get("/api/admin/kb", _api_admin_kb)
    app.router.add_post("/api/admin/kb/save", _api_admin_kb_save)
    app.router.add_post("/api/admin/kb/delete", _api_admin_kb_delete)
    app.router.add_get("/api/admin/hub", _api_admin_hub)
    app.router.add_get("/api/admin/status", _api_admin_status)
    app.router.add_get("/api/admin/queue", _api_admin_queue)
    app.router.add_post("/api/admin/queue/move", _api_admin_queue_move)
    app.router.add_get("/api/admin/plastics", _api_admin_plastics)
    app.router.add_post("/api/admin/plastic", _api_admin_plastic)
    app.router.add_get("/api/admin/spools", _api_admin_spools)      # катушки (Фаза A мастер-плана)
    app.router.add_post("/api/admin/spool", _api_admin_spool)
    app.router.add_get("/api/admin/service", _api_admin_service)
    app.router.add_post("/api/admin/service", _api_admin_service_action)
    app.router.add_post("/api/admin/self_print", _api_admin_self_print)
    # Цены и скидки: промокоды, скидки, опции постобработки, калькулятор сметы. Пути и
    # порядок — в таблице routes рядом с самими обработчиками (см. блок выше).
    app.add_routes(prices.routes)
    app.router.add_get("/api/admin/collection", _api_admin_collection)
    app.router.add_post("/api/admin/collection", _api_admin_collection_edit)
    app.router.add_get("/api/admin/settings", _api_admin_settings)
    app.router.add_post("/api/admin/setting", _api_admin_setting)
    app.router.add_post("/api/admin/brand_logo", _api_admin_brand_logo)   # белый лейбл (A3)
    app.router.add_get("/api/admin/activity", _api_admin_activity)
    app.router.add_get("/api/admin/bigdata", _api_admin_bigdata)
    app.router.add_get("/api/admin/notifications", _api_admin_notifications)
    app.router.add_get("/api/admin/print_classify", _api_admin_print_classify)
    app.router.add_post("/api/admin/print_classify", _api_admin_print_classify_pick)
    app.router.add_get("/api/admin/families", _api_admin_families)
    app.router.add_post("/api/admin/notification_action", _api_admin_notification_action)
    app.router.add_post("/api/family_pick_partner", _api_family_pick_partner)
    app.router.add_post("/api/admin/family_create", _api_admin_family_create)
    app.router.add_post("/api/admin/family_add", _api_admin_family_add)
    app.router.add_post("/api/admin/family_remove", _api_admin_family_remove)
    app.router.add_get("/api/admin/shades", _api_admin_shades)
    app.router.add_post("/api/admin/shade", _api_admin_shade)
    # Оборудование: пресеты печати (качество/прочность) и карточки принтеров с соплами,
    # протоколом связи и выбором слайсера. Пути и порядок — в таблице routes рядом с
    # самими обработчиками (см. блок выше).
    app.add_routes(equipment.routes)
    app.router.add_post("/api/admin/feature", _api_admin_feature)   # возможности сборки (f98aa1ef)
    app.router.add_get("/api/admin/mw_accounts", _api_admin_mw_accounts)
    app.router.add_post("/api/admin/mw_account", _api_admin_mw_account)
    # CRM: клиенты/сегменты/рассылки/задачи (crm-roadmap.md, OpenTodo a8b591cf)
    app.router.add_get("/api/admin/clients", _api_admin_clients)
    app.router.add_get("/api/admin/clients/export.csv", _api_admin_clients_csv)
    app.router.add_get("/api/admin/client", _api_admin_client)
    # смета, которую видел клиент — открывается кликом по плитке ленты событий (26.08)
    app.router.add_get("/api/admin/calc", _api_admin_calc)
    app.router.add_post("/api/admin/client_save", _api_admin_client_save)
    app.router.add_post("/api/admin/client_discount", _api_admin_client_discount)
    app.router.add_post("/api/admin/client_message", _api_admin_client_message)
    # Сплошной чат клиента и поиск по переписке — настольная админка (2026-07-19)
    app.router.add_get("/api/admin/client_chat", _api_admin_client_chat)
    app.router.add_get("/api/admin/chat_search", _api_admin_chat_search)
    # Windows-тосты настольной админки (этап 3) — только локально
    app.router.add_get("/api/admin/events", _api_admin_events)
    app.router.add_post("/api/admin/broadcast", _api_admin_broadcast)
    app.router.add_get("/api/admin/tasks", _api_admin_tasks)
    app.router.add_post("/api/admin/task", _api_admin_task)
    # Доставка (A2): тарифы клиенту, прайс и отправления мастеру
    app.router.add_get("/api/delivery_types", _api_delivery_types)
    app.router.add_get("/api/admin/delivery", _api_admin_delivery)
    app.router.add_post("/api/admin/delivery_type", _api_admin_delivery_type)
    app.router.add_post("/api/admin/order/delivery", _api_admin_order_delivery)
    app.router.add_post("/api/admin/order/shipment", _api_admin_order_shipment)
    # Команда мастерской (A4): роли и права
    app.router.add_get("/api/admin/staff", _api_admin_staff)
    app.router.add_post("/api/admin/staff", _api_admin_staff_save)
    app.router.add_get("/health", _health)
    app.router.add_get("/api/screen-status", _api_screen_status)   # TURZX-экран (fbe5e463)
    app.router.add_get("/api/screen-power", _api_screen_power)      # флаг питания TURZX для рендерера
    app.router.add_post("/api/admin/screen_power", _api_admin_screen_power)   # тумблер вкл/выкл из админки
    app.router.add_post("/api/admin/ai", _api_admin_ai_action)   # ИИ: вкл/выкл, выгрузка, автовыгрузка (ee064f93)
    app.router.add_post("/api/admin/ai_test", _api_admin_ai_test)   # проба генерации по промту (22.07)
    # автообновления кода (644367fb): проверить / применить / режим — владельческие ручки
    app.router.add_post("/api/admin/update_check", _api_admin_update_check)
    app.router.add_post("/api/admin/update_apply", _api_admin_update_apply)
    app.router.add_post("/api/admin/update_mode", _api_admin_update_mode)
    # резервные копии БД (b557e341) — только владельцу (в _OPERATOR_ROUTES не вносим)
    app.router.add_get("/api/admin/backup_status", _api_admin_backup_status)
    app.router.add_post("/api/admin/backup_now", _api_admin_backup_now)
    # LCD-дашборд в корпусе ПК (design_handoff_lcd): статика + данные
    app.router.add_get("/lcd", _lcd_file)
    app.router.add_get("/lcd/", _lcd_file)
    app.router.add_get("/lcd/{path:.*}", _lcd_file)
    app.router.add_get("/api/lcd/data", _api_lcd_data)
    return app


_runner: web.AppRunner | None = None


async def start(port: int, bot=None, storage=None) -> None:
    global _runner, _storage
    set_bot(bot)
    _storage = storage
    # access_log_class — НЕ косметика: штатный AccessLogger пишет в лог `request.path_qs`
    # целиком, а фронт возит авторизацию в query, поэтому в bot.log ложились живые initData,
    # токен ПК-админки и личные сессии деска. Подробности и способ — в app/weblog.py.
    _runner = web.AppRunner(make_app(), access_log_class=weblog.MaskingAccessLogger)
    await _runner.setup()
    await web.TCPSite(_runner, "127.0.0.1", port).start()
    log.info("Мини-апка слушает 127.0.0.1:%d", port)


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
