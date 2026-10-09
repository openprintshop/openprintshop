"""Клиентский конвейер мини-апки: от загрузки модели до заказа и его карточки.

Один кусок — потому что это один живой поток данных через реестр web/registry (`_models`):
загрузка и варианты набора, параметрика, столы и превью, 3D-файл вьюера, смета (мгновенная
оценка -> честное уточнение фоном), быстрая смета по ссылке из чата, пересчёт при выборе
опций, оформление (заказ/без слайса/сейчас/из чата/корзина), клиентская карточка заказа с
перепиской и «свои модели». Правило endgame-плана: пары «обёртка/нутро» (_refine_calc/
_refine_calc_inner, _api_calc/_api_calc_inner) не разрезаются — нутро держит состояние.

Выделено из app/webapp.py волной 15 endgame-плана. Правила дома:
* модуль НЕ имеет права импортировать app.webapp (разрез односторонний; проверяется тестом
  в отдельном процессе). Всё берётся из настоящих домов: реестр — web/registry (волна 14),
  счётчики — web/quotecalc, 3D — web/mesh3d, медиа заказа — web/ordermedia, дефолты —
  web/orderdefaults, порог превью — web/storefront;
* `_REFINE_MAX_BYTES` — плоская константа: подменять в тестах ЗДЕСЬ, а не через webapp;
* бота ставит webapp.set_bot веером _BOT_FAN (волна 12, вариант «б»): ветки отправки
  закрыты `if _bot is None` и у студии с выключенной мини-аппой молчат, как и раньше;
* регистрация маршрутов осталась в make_app строками на фасадные имена; подписные пути
  не менялись ни на символ;
* тела перенесены ДОСЛОВНО (сверка против git show HEAD:app/webapp.py) с двумя
  отличиями, оба закреплены tests/test_clientflow_facade.py: (1) ленивый `from .db
  import ...` внутри карточки заказа поднялся на `from ..db import ...` — модуль теперь
  в app/web/; (2) обращения к mesh3d.* и ordermedia._save_chat_image КВАЛИФИЦИРОВАНЫ
  модулем — тесты подменяют их в mesh3d/ordermedia, и копия по значению оставила бы
  подмену недосягаемой (образец — printmedia в printerjob).

quick_link_quote и place_order_from_chat зовутся из handlers/user_flow.py через фасад
webapp.* — это публичные точки входа из Telegram-чата, их имена не трогать.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from .. import (
    cache,
    calcservice,
    config,
    cpupool,
    db,
    deferred,
    delivery,
    downloader,
    elegoo,
    fastcalc,
    inspect3mf,
    keyboards,
    materials,
    notify,
    orderchat,
    parametric,
    payments,
    preview,
    pricing,
    ratelimit,
    texts,
)

# place_from_token живёт в app/orders.py; исторический псевдоним _place_one сохранён —
# его знают и тела ниже, и tests (inspect.getsource(webapp._place_one)).
from ..bedspec import DEFAULT_BED, BedSpec
from ..orders import _embedded_thumb, _order_stock_plan
from ..orders import place_from_token as _place_one
from ..printmedia import _file_size
from ..webauth import _verify_init_data, _verify_user
from ..websign import _file_ver

# mesh3d и _save_chat_image — КВАЛИФИЦИРОВАНЫ модулем (не by-value): тесты подменяют их
# в webapp и mesh3d/ordermedia (образец — printmedia в printerjob), копия по значению
# оставила бы подмену недосягаемой, а тест — на настоящем разборе меша/записи фото.
from . import mesh3d, ordermedia
from .common import _bad, _img_headers, _maybe_webp, _toint, check_sig, signed_url
from .orderdefaults import _apply_default_delivery, _delivery_note
from .ordermedia import _msgs_json, _order_prints_media, _slicer_url, _timelapse_url
from .quotecalc import _apply_dfm, _parse_plates_sel, _result_payload
from .registry import _make_token, _models, _register_full, _register_model, _remember_and_respond
from .storefront import _THUMB_MAX

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`

# Порог «тяжёлого» меша: выше него мгновенную ≈оценку НЕ уточняем честным слайсом в фоне —
# слайс/рендер такого файла (koala-класс, 100+ МБ) уходят в память/коллизию на десятки минут,
# результат — та же оценка (goal 16.07). Оценка остаётся, мастер уточняет цену вручную.
_REFINE_MAX_BYTES = 50 * 1024 * 1024


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


def _blocked_payload(exc, url: str) -> dict:
    """Ответ при анти-бот блоке MakerWorld: честный текст + запрос для поиска той же модели на
    Printables (имя берём из слага ссылки — без сети). Фронт покажет кнопку «Найти на Printables»."""
    return {"ok": False, "error": str(exc), "blocked": True,
            "search": downloader.label_from_url(url) if url else ""}

async def _api_model(request: web.Request) -> web.Response:
    ct = request.content_type or ""
    src_url = ""
    if ct.startswith("multipart/"):
        init_data, filename, data = "", "", b""
        reader = await request.multipart()
        async for field in reader:
            if field.name == "initData":
                init_data = (await field.read()).decode("utf-8", "ignore")
            elif field.name == "file":
                filename = field.filename or "model"
                data = await field.read()
        uid = _verify_init_data(init_data)
        if not uid:
            return web.json_response({"ok": False, "error": "auth"}, status=403)
        if not ratelimit.allow(f"upload:{uid}", config.RATE_UPLOADS_PER_MIN, 60):
            return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
        try:
            meshes, label = await downloader.save_web_upload(filename, data)
        except downloader.DownloadError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _bad()
        uid = _verify_init_data(body.get("initData", ""))
        if not uid:
            return web.json_response({"ok": False, "error": "auth"}, status=403)
        if not ratelimit.allow(f"upload:{uid}", config.RATE_UPLOADS_PER_MIN, 60):
            return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
        url = (body.get("url") or "").strip()
        if not downloader.is_http_url(url):
            return web.json_response({"ok": False, "error": "Это не похоже на ссылку"})
        src_url = url
        # #14: у модели MakerWorld может быть несколько вариантов (профилей) — спросим выбор
        mw = await downloader.fetch_makerworld_variants(url)
        if mw and mw.get("variants"):
            # вариант приходит как {id,title,thumb}; дублируем id в iid, чтобы работали и старые
            # клиенты, читающие variants[].iid (иначе /api/variant получает пустой iid → 400)
            variants = [{**v, "iid": v.get("id"), "id": v.get("id")} for v in mw["variants"]]
            return web.json_response({"ok": True, "variants": variants,
                                      "default_iid": mw.get("default_iid"),
                                      "mw_title": mw["title"], "mw_url": url})
        if mw and mw.get("single"):
            meshes, label = mw["single"]      # один вариант — уже скачан в одной сессии
        else:
            try:
                meshes, label = await downloader.fetch_from_url(url)
            except downloader.MakerWorldBlocked as exc:
                return web.json_response(_blocked_payload(exc, url))
            except downloader.DownloadError as exc:
                return web.json_response({"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.warning("web fetch failed", exc_info=True)
                return web.json_response({"ok": False, "error": f"Не удалось скачать: {exc}"})

    paths = [str(p) for p in meshes]
    info = await cpupool.run(inspect3mf.inspect_model, paths)
    return await _remember_and_respond(paths, label, uid, src_url, info)

async def _api_parametric_models(request: web.Request) -> web.Response:
    """Список параметрических моделей + их параметры для формы (OpenTodo c1cc840a)."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    models = [{"id": m["id"], "name": m["name"], "desc": m.get("desc", ""),
               "params": parametric.public_params(m)}
              for m in parametric.MODELS if not m.get("hidden")]   # табличка живёт в кастомайзере
    return web.json_response({"ok": True, "available": parametric.openscad_available(), "models": models})

async def _api_parametric_model(request: web.Request) -> web.Response:
    """Сгенерировать параметрическую модель (OpenSCAD → STL) и отдать её как ОБЫЧНУЮ загруженную
    модель (токен) — дальше тот же конвейер расчёта/заказа (OpenTodo c1cc840a)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"param:{uid}", config.RATE_UPLOADS_PER_MIN, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    model = parametric.get_model(body.get("model_id"))
    if not model:
        return web.json_response({"ok": False, "error": "Модель не найдена"})
    if not model.get("qr") and not parametric.openscad_available():   # QR-брелок не требует OpenSCAD
        return web.json_response({"ok": False, "error": "Параметрические модели сейчас недоступны"})
    values = parametric.prepare_values(model, body.get("params") or {})
    if model.get("autosize"):
        values = await parametric.autosize_keychain(model, values)
    try:
        stl = await parametric.render(model, values)
    except parametric.ParametricError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("parametric render failed")
        return web.json_response({"ok": False, "error": f"Ошибка генерации: {exc}"})
    paths = [str(stl)]
    info = await cpupool.run(inspect3mf.inspect_model, paths)
    await db.log_event(uid, "параметрическая модель", parametric.label_for(model, values))
    return await _remember_and_respond(paths, parametric.label_for(model, values), uid, "", info)

def _hex_to_rgb(s):
    """#rrggbb / #rgb → (r,g,b) в 0..1, иначе None."""
    if not s:
        return None
    s = str(s).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0)
    except ValueError:
        return None

async def _api_parametric_preview2c(request: web.Request) -> web.Response:
    """Двухцветный 3D-рендер параметрической модели прямо на экране настройки: OpenSCAD рендерит
    две части (корпус Part=1 / текст+рамка Part=2), софт-растеризатор красит их в Цвет 1 / Цвет 2
    и отдаёт JPEG как data-URL. STL и JPEG кэшируются по сигнатуре; цену считает обычный слайс."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"p2c:{uid}", 120, 60):
        return web.json_response({"ok": False, "error": "Слишком часто"})
    model = parametric.get_model(body.get("model_id"))
    if not model:
        return web.json_response({"ok": False, "error": "Модель не найдена"})
    if not parametric.openscad_available():
        return web.json_response({"ok": False, "error": "Параметрические модели сейчас недоступны"})
    values = parametric.prepare_values(model, body.get("params") or {})
    if model.get("autosize"):
        values = await parametric.autosize_keychain(model, values)
    c1 = _hex_to_rgb(body.get("color1")) or (0.91, 0.93, 0.95)   # корпус (по умолч. белый)
    c2 = _hex_to_rgb(body.get("color2")) or (0.1, 0.1, 0.1)      # текст + рамка (чёрный)
    c3 = _hex_to_rgb(body.get("color3")) or (0.18, 0.43, 0.84)   # полоса-евробанд (синий)
    try:
        body_stl, ink_stl, euro_stl = await parametric.render_parts(model, values)
    except parametric.ParametricError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("parametric 2c render failed")
        return web.json_response({"ok": False, "error": f"Ошибка генерации: {exc}"})
    key = hashlib.md5(
        f"{model['id']}|{json.dumps(values, sort_keys=True, ensure_ascii=False)}|{c1}|{c2}|{c3}".encode("utf-8")
    ).hexdigest()[:16]
    parts = [([str(body_stl)], c1)]
    if euro_stl:
        parts.append(([str(euro_stl)], c3))
    if ink_stl:
        parts.append(([str(ink_stl)], c2))
    img = await cpupool.run_render(preview.render_colored_parts, parts, key)
    if not img or not Path(img).exists():
        return web.json_response({"ok": False, "error": "Не удалось отрисовать"})
    data = base64.b64encode(Path(img).read_bytes()).decode("ascii")
    return web.json_response({"ok": True, "image": "data:image/jpeg;base64," + data})

async def _api_variant(request: web.Request) -> web.Response:
    """#14: скачать выбранный вариант (instance) модели MakerWorld → токен расчёта (как /api/model)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"upload:{uid}", config.RATE_UPLOADS_PER_MIN, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    url = (body.get("url") or "").strip()
    iid = body.get("iid")
    if not (downloader.is_http_url(url) and iid):
        return _bad()
    try:
        meshes, label = await downloader.download_makerworld_instance(url, iid)
    except downloader.MakerWorldBlocked as exc:
        return web.json_response(_blocked_payload(exc, url))
    except downloader.DownloadError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.warning("variant download failed", exc_info=True)
        return web.json_response({"ok": False, "error": f"Не удалось скачать вариант: {exc}"})
    paths = [str(p) for p in meshes]
    info = await cpupool.run(inspect3mf.inspect_model, paths)
    return await _remember_and_respond(paths, label, uid, url, info)

async def _api_file_variant(request: web.Request) -> web.Response:
    """Смена ВАРИАНТА НАБОРА карточки (OpenTodo 87eb9cf3): карточка несёт одну модель
    несколькими комплектами файлов (полный .3mf / россыпь STL / second .3mf) — клиент выбрал
    другой комплект. Пересобираем токен из ПОЛНОГО набора (m["all_paths"]) под выбранный key:
    ответ — та же форма, что у /api/model, фронт прогоняет его через applyModel и тихо
    пересчитывает смету. Старый токен остаётся жить (ссылки/смета на него не ломаются)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    m = _models.get(body.get("token", ""))
    if not m or m.get("uid") != uid:
        return web.json_response({"ok": False, "expired": True,
                                  "error": "Модель не найдена — откройте карточку заново"}, status=404)
    variants = m.get("file_variants") or []
    key = str(body.get("key") or "")
    if not variants or key not in {v["key"] for v in variants}:
        return web.json_response({"ok": False, "error": "У модели нет такого варианта"})
    res = await _register_full(m.get("all_paths") or m["paths"], m["label"], uid,
                               m.get("src_url", ""), sel_key=key, log=False)
    await db.log_event(uid, "выбрал вариант набора", f'{m["label"]} → {key}')
    return web.json_response(res)

async def _api_plate(request: web.Request) -> web.StreamResponse:
    """#03a5727e: картинка одного стола (Metadata/plate_N.png) для многостольной модели.

    Если автор .3mf не вшил картинки столов — рендерим их сами из геометрии
    (OpenTodo 314e175e): один тяжёлый проход на модель (под per-token замком от дублей,
    список путей мемоизируется в токене), дальше отдаём готовые файлы.

    ?c=<имя цвета> (OpenTodo 65aa1a35): перерисовать стол в цвете выбранного пластика —
    цветной запрос всегда идёт через НАШ рендер (вшитая авторская картинка цвета не знает)."""
    m = _models.get(request.match_info.get("token", ""))
    if not m:
        raise web.HTTPNotFound()
    thumbs = m.get("plate_thumbs") or []
    try:
        idx = int(request.match_info.get("idx", "0"))
    except (TypeError, ValueError):
        raise web.HTTPNotFound() from None
    color = (request.query.get("c") or "").strip()
    if not color and 0 <= idx < len(thumbs) and Path(thumbs[idx]).exists():
        return web.FileResponse(thumbs[idx], headers={"Cache-Control": "max-age=3600"})
    if not (0 <= idx < int(m.get("plates", 1) or 1)):
        raise web.HTTPNotFound()
    rgbs = None
    if color:
        try:
            rgbs = materials.color_to_rgbs(color, await db.get_color_overrides()) or None
        except Exception:  # noqa: BLE001
            rgbs = None
    ckey = "mono" if rgbs is None else hashlib.sha1(repr(rgbs).encode()).hexdigest()[:8]
    store = m.setdefault("plate_render", {})
    if store.get(ckey) is None:
        lock = m.setdefault("_plate_lock", asyncio.Lock())
        async with lock:                       # параллельные тайлы не дублируют тяжёлый рендер
            if store.get(ckey) is None:
                store[ckey] = await cpupool.run_render(preview.render_plate_previews, m["paths"], rgbs)
    rendered = store.get(ckey) or []
    p = rendered[idx] if 0 <= idx < len(rendered) else ""
    if p and Path(p).exists():
        return web.FileResponse(p, headers={"Cache-Control": "max-age=3600"})
    raise web.HTTPNotFound()

async def _api_plates_info(request: web.Request) -> web.Response:
    """Сводка по столам мультиплейт-модели (OpenTodo eb4424d9 + d7518198): габариты и признак
    многоцвета на каждый стол. Габариты считаются лениво (тяжёлая геометрия в cpupool, дисковый
    кэш по модели) и мемоизируются в токене."""
    m = _models.get(request.match_info.get("token", ""))
    if not m:
        return web.json_response({"ok": False, "error": "Модель не найдена"}, status=404)
    n = int(m.get("plates", 1) or 1)
    if n <= 1:
        return web.json_response({"ok": True, "plates": []})
    if m.get("plate_dims") is None:
        lock = m.setdefault("_plate_lock", asyncio.Lock())
        async with lock:
            if m.get("plate_dims") is None:
                m["plate_dims"] = await cpupool.run(preview.plates_dims, m["paths"])
    dims = m.get("plate_dims") or []
    mc = m.get("plate_mc") or []
    return web.json_response({"ok": True, "plates": [
        {"idx": i, "dims": (dims[i] if i < len(dims) else ""),
         "mc": bool(mc[i]) if i < len(mc) else False} for i in range(n)]})

def _bed_of_printer(printer) -> BedSpec:
    """Стол карточки принтера; принтера нет — прежнее поведение (DEFAULT_BED)."""
    return BedSpec.from_machine_profile(getattr(printer, "machine_profile", None))


def _memo_preview(m: dict, bed: BedSpec) -> str | None:
    """Мемо превью из записи токена — только если оно нарисовано ТЕМ ЖЕ столом.

    Картинка помнит стол, которым её дедуплицировали (m['preview_bed']): на широком столе
    наложенные копии схлопываются, на узком остаются. Отдать заказу/PDF картинку от чужого
    принтера значит показать деталей больше или меньше, чем в смете (хвост B1)."""
    png = m.get("preview")
    return png if png and m.get("preview_bed") == bed else None


async def _bed_of_model(m: dict) -> BedSpec:
    """Стол принтера ЭТОГО расчёта — один на превью, 3D-вьюер и смету токена.

    Принтер в записи токена лежит ИМЕНЕМ (m['last']['printer'], кладётся каждым путём расчёта),
    а не готовым BedSpec: профиль принтера могли поменять между расчётом и открытием вьюера,
    и держать протухший снимок стола хуже, чем сходить в БД (список принтеров короткий и
    кэшируется на уровне db). До первого расчёта имени ещё нет — берём принтер по умолчанию:
    именно на нём мини-апп и покажет первую смету. Ничего не нашлось — DEFAULT_BED, то есть
    ровно прежнее поведение."""
    try:
        name = str(((m.get("last") or {}).get("printer")) or "")
        printer = next((x for x in await db.list_printers(active_only=False)
                        if x.name == name), None) if name else None
        if printer is None:
            printer = await db.get_default_printer()
        if printer is not None:
            return BedSpec.from_machine_profile(getattr(printer, "machine_profile", None))
    except Exception:  # noqa: BLE001 — стол не восстановился: считаем как раньше, а не падаем
        log.debug("bed lookup for model token failed", exc_info=True)
    return DEFAULT_BED


async def _api_mpreview(request: web.Request) -> web.StreamResponse:
    m = _models.get(request.match_info.get("token", ""))
    if not m:
        raise web.HTTPNotFound()
    bed = await _bed_of_model(m)
    png = m.get("preview") if m.get("preview_bed") == bed else None
    if not (png and Path(png).exists()):
        # мемо превью в токене переживает смену принтера в той же сессии — держим рядом стол,
        # которым оно нарисовано, иначе клиент, переключивший принтер, увидит чужую картинку
        png = await cpupool.run_render(preview.render_preview, m["paths"], bed=bed)
        if png and Path(png).exists():
            m["preview"], m["preview_bed"] = str(png), bed
    if png and Path(png).exists():
        return web.FileResponse(png, headers={"Cache-Control": "no-cache"})
    raise web.HTTPNotFound()

async def _api_cpreview(request: web.Request) -> web.StreamResponse:
    m = _models.get(request.match_info.get("token", ""))
    if m and m.get("cpreview") and Path(m["cpreview"]).exists():
        return web.FileResponse(m["cpreview"], headers={"Cache-Control": "no-cache"})
    raise web.HTTPNotFound()

async def _api_modelfile(request: web.Request) -> web.StreamResponse:
    """Сырой файл модели (m['paths'][0]) для клиентского three.js-вьюера (B1, 3D-просмотр).

    Отдаёт ОРИГИНАЛ авторского файла (может быть с MakerWorld/src_url), поэтому авторизация
    обязательна и строже, чем у PNG-превью (/api/mpreview): токен — неугадываемый uuid4
    (capability), И плюс initData→uid должен совпасть с владельцем расчёта m['uid'].
      • 403 — подпись невалидна ИЛИ токен принадлежит другому пользователю;
      • 404 — токен вытеснен из _models (_MODELS_MAX) / файла нет на диске / формат не отдаём.
    При 403/404 мини-апп тихо откатывается на существующий PNG — сбой 3D не блокирует заказ."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        raise web.HTTPForbidden()
    m = _models.get(request.match_info.get("token", ""))
    if not m:
        raise web.HTTPNotFound()                       # токен вытеснен/протух → фолбэк на PNG
    if int(m.get("uid") or 0) != uid:
        raise web.HTTPForbidden()                      # чужой расчёт — оригинал не отдаём
    paths = m.get("paths") or []
    if not paths:
        raise web.HTTPNotFound()
    paths = inspect3mf.drop_redundant_plate_stls(list(paths))   # тот же фильтр, что у меты (24.07)
    p = Path(str(paths[0]))
    ext = p.suffix.lower()
    # .step/.stp three.js не парсит без OpenCASCADE → 3D для них не предлагаем и файл не отдаём
    # (гейт _MODEL3D_EXTS = тот же, что у model_viewable): вьюер такого токена и не откроется.
    if ext not in mesh3d._MODEL3D_EXTS or not p.is_file():
        raise web.HTTPNotFound()
    total = 0
    for fp in paths:                                   # GLB собирается из ВСЕГО набора файлов
        try:
            total += Path(fp).stat().st_size
        except OSError:
            pass
    if total > mesh3d._MODEL3D_MAX_SRC_BYTES:                 # страховка: неподъёмный исходник не разбираем
        raise web.HTTPNotFound()
    # Отдаём НЕ сырой файл, а извлечённый сервером бинарный GLB со СЦЕНОЙ из деталей (part0..N):
    # так .3mf (в т.ч. Bambu/MakerWorld, на которых клиентский 3MFLoader спотыкался) и .obj рисуются
    # одним надёжным GLTFLoader'ом, а детали сохранены — клиент красит каждую в свой цвет (мультицвет)
    # в реальном времени. Тяжёлый разбор делает сервер (trimesh, в отдельном потоке).
    data = await asyncio.to_thread(mesh3d._model3d_glb_bytes, paths, await _bed_of_model(m))
    if not data:
        raise web.HTTPNotFound()                       # меш не разобрался → мини-апп остаётся на PNG
    headers = dict(_img_headers(request, private=True))
    headers["Content-Type"] = "model/gltf-binary"
    return web.Response(body=data, headers=headers)

def _live_progress():
    """Доля 0..1 текущей печати из кэш-телеметрии (живой ETA, idea #8). None — не печатает/нет данных."""
    try:
        cs = elegoo.cached_status(max_age=180.0)
        if cs and cs.get("ok"):
            ov = elegoo.enrich_status(cs.get("state") or {})
            if (ov.get("print_stats") or {}).get("state") == "printing":
                pg = (ov.get("virtual_sdcard") or {}).get("progress")
                if isinstance(pg, (int, float)):
                    return float(pg)
    except Exception:  # noqa: BLE001
        pass
    return None

async def _api_calc(request: web.Request) -> web.Response:
    """Обёртка под shield (OpenTodo ae4c6c13): если клиент нажал «Уведомить, когда посчитается»
    и закрыл приложение, aiohttp отменяет хендлер — но слайс доживает в отдельной задаче и сам
    шлёт уведомление с ценой (конец _api_calc_inner).

    Вышел, НЕ нажав кнопку (закрыл мини-апп во время слайса), — подписываем автоматически:
    разрыв соединения виден здесь как CancelledError, ставим m["notify_calc"] — доживающий
    слайс дошлёт смету (или ошибку) тем же путём, что и кнопка. Тихие расчёты (карточка
    товара, пакет ссылок, кастомайзер — фронт шлёт quiet) не подписываем: юзер цену не ждал."""
    try:
        body = await request.json()   # payload кэшируется — _api_calc_inner прочитает его же
    except Exception:  # noqa: BLE001
        body = {}
    task = asyncio.create_task(_api_calc_inner(request))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # дожить; ошибки — в лог
        if isinstance(body, dict) and not body.get("quiet") and not task.done():
            m = _models.get(str(body.get("token") or ""))
            uid = _verify_init_data(body.get("initData", "")) if m else None
            if m and uid and m.get("uid") == uid:
                m["notify_calc"] = uid
        raise

async def _mw_estimate_for(m: dict) -> dict | None:
    """MW-оценка (вес+время профиля) для зарегистрированной модели, с кэшем в её записи.
    False-маркер = «узнавали, данных нет» — второй раз API не дёргаем."""
    if "mw_est" in m:
        return m["mw_est"] or None
    est = None
    try:
        if m.get("src_url"):
            est = await downloader.fetch_makerworld_estimate(m["src_url"])
    except Exception:  # noqa: BLE001
        est = None
    m["mw_est"] = est or False
    return est

async def _mw_snapshot(token: str, m: dict, est: dict, *, uid: int, printer, plastic,
                       quality, strength, color: str, colors_sel: list, qty: int,
                       plates: int, promo_pct: float = 0.0, urgency_factor: float = 1.0) -> dict:
    """Смета из ДАННЫХ MakerWorld (97a91eee): цена ручной формулой (как заказ за клиента),
    снимок m[last]/m[pdf]/last_payload с approx=True + refining=True (фронт поллит quote_state,
    пока фоновый слайс не уточнит цифры). Возвращает payload."""
    settings = await db.get_settings()
    discount = await calcservice.effective_discount(uid, qty, promo_pct)
    grams, minutes = float(est["weight_g"]), float(est["minutes"])
    bd = pricing.compute_price_manual(
        grams, minutes, plastic.price_per_gram * 1000.0, settings, qty=qty,
        discount_pct=discount, failure_rate_pct=plastic.failure_rate_pct,
        volume_cm3_unit=(grams / plastic.density) if plastic.density else None,
        urgency_factor=urgency_factor)
    # метка многоцвета для подписей (на цену не влияет — граммы MW уже финальные, с продувкой)
    bd.extra_colors = max(0, int(est.get("colors_cnt") or 1) - 1)
    pricing.apply_options(bd, m.get("options"), _m_nparts(m))   # B4: опции поверх цены
    pdisp = f"{plastic.name} · {color}" if color else plastic.name
    if not m.get("cpreview"):
        m["cpreview"] = await _fetch_mw_cover(est.get("cover") or "", est.get("iid"))
    m["last"] = {"plastic_name": pdisp, "printer": printer.name, "qty": qty,
                 "quality": quality.label, "strength": strength.label,
                 "dims": "", "grams_total": bd.grams_total,
                 "minutes_total": bd.minutes_total, "total": bd.total,
                 "plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
                 "color": color, "colors_sel": colors_sel, "color_grams": [],
                 "plastic_grams": [], "scale_pct": 100,
                 "volume_cm3_unit": (bd.grams_unit / plastic.density) if plastic.density else 0.0,
                 "minutes_unit": bd.minutes_unit, "extra_colors": bd.extra_colors,
                 "mixed": False, "approx": True}
    m["pdf"] = {"bd": bd, "settings": settings, "model_label": m["label"],
                "plastic_name": pdisp, "printer_name": printer.name, "qty": qty,
                "quality_label": quality.label, "strength_label": strength.label,
                "plates": plates, "preview_path": m.get("cpreview")}
    m["calc_plates"] = int(plates or 1)
    payload = _result_payload(bd, settings, config.CURRENCY, plates, False)
    payload["approx"] = True
    payload["refining"] = True    # фоновый слайс идёт — фронт поллит и обновит цифры
    if m.get("cpreview"):
        payload["cpreview"] = f"/api/cpreview/{token}"
    payload["dims"] = ""
    payload["scale_pct"] = 100
    payload["queue"] = await db.queue_load(_live_progress())
    payload["eta"] = db.eta_date(payload["queue"].get("minutes", 0) + bd.minutes_total)
    payload["urgency"] = m.get("urgency") or "normal"           # B3: срочность
    # B3: срок по времени печати заказа (bd.minutes_total) — «срочно» всегда завтра, «обычно» —
    # завтра/послезавтра относительно порога term_split_hours.
    payload["term"] = db.promised_date(bd.minutes_total, m.get("urgency"), settings)
    # B2 (DFM): быстрый снимок ещё без габаритов (dims='') — вердикт тихий 'ok'; поля всё равно
    # ставим, чтобы фронт сбросил возможный прошлый вердикт. Фоновый слайс уточнит его в _refine_calc.
    _apply_dfm(payload, dims="", printer=printer, meas=None, settings=settings)
    m["last_payload"] = payload
    return payload

def _time_factor(printer) -> float:
    """Коэффициент времени машины (B1): для мгновенных fastcalc-смет, идущих мимо
    calcservice.compute (там множитель применяет сам compute)."""
    try:
        return float(getattr(printer, "time_factor", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0


async def _snap_calc(uid: int, action: str, detail: str, *, source: str, m: dict, payload: dict,
                     bd=None, printer=None, plastic=None, quality=None, strength=None,
                     color: str = "", qty: int = 1, preview: str = "") -> int | None:
    """Событие ленты + СНИМОК сметы, которую клиент только что увидел (26.08).

    Раньше в ленте оставалась одна строка текста, а сама смета жила только в памяти процесса
    (registry._models, до рестарта) — «рассчитал цену» в ленте было нечем открыть, хотя мастеру
    нужно ровно это: какую цену человек увидел и из чего она сложилась.

    Цифры берём из PriceBreakdown — в payload они уже отформатированы в строки («9 ч 36 мин»).
    Сам payload кладём как есть, минус живая очередь и ссылка на превью по токену: и то и другое
    протухнет к моменту, когда мастер откроет снимок. Снимок необязателен — store глушит свои
    ошибки и возвращает None, событие ленты уходит в любом случае."""
    bd = bd if bd is not None else (m.get("pdf") or {}).get("bd")
    snap = {k: v for k, v in (payload or {}).items() if k not in ("queue", "cpreview")}
    disc_amount = float(getattr(bd, "discount_amount", 0.0) or 0.0)
    total = float(getattr(bd, "total", 0.0) or 0.0)
    cid = await db.add_calc_snapshot(
        uid, action=action, source=source, model_label=(m or {}).get("label", "") or "",
        printer=getattr(printer, "name", "") or "",
        plastic_name=getattr(plastic, "name", "") or "", color=color or "",
        quality=getattr(quality, "label", "") or "",
        strength=getattr(strength, "label", "") or "", qty=int(qty or 1),
        grams_total=float(getattr(bd, "grams_total", 0.0) or 0.0),
        minutes_total=float(getattr(bd, "minutes_total", 0.0) or 0.0),
        total=total, total_before=total + disc_amount,
        discount_pct=float(getattr(bd, "discount_pct", 0.0) or 0.0),
        approx=bool(snap.get("approx")), estimated=bool(snap.get("estimated")),
        preview_path=preview or (m or {}).get("cpreview") or "", payload=snap)
    if m is not None:
        m["calc_snap_id"] = cid    # фоновый слайс допишет сюда уточнённые цифры (_refine_calc)
    await db.log_event(uid, action, detail, calc_id=cid)
    return cid


async def _cache_snapshot(token: str, m: dict, cc: dict, *, uid: int, printer, plastic, quality,
                          strength, color: str, colors_sel: list, qty: int, extra_colors: int,
                          promo_pct: float = 0.0, urgency_factor: float = 1.0) -> dict:
    """Мгновенная смета из calc_cache (OpenTodo ae038968): грамм/время берём из результата
    ПОСЛЕДНЕГО честного слайса ЭТОЙ модели тем же принтером/качеством/прочностью (масштаб 100%,
    без выбора столов/своего пластика на столе) — эти цифры зависят от профиля печати, а не от
    пластика (та же логика, что у быстрого пересчёта пластика без переслайса, 050aff56). Цена —
    штатной формулой pricing.compute_price. approx=True + refining=True, как у MW-пути
    (_mw_snapshot) — фон честно досчитает слайсом и обновит смету (_refine_calc)."""
    settings = await db.get_settings()
    discount = await calcservice.effective_discount(uid, qty, promo_pct)
    volume = float(cc.get("volume_cm3") or 0.0)
    minutes = float(cc.get("minutes") or 0.0)
    plates = int(cc.get("plates") or 1)
    # plates и в цену (plate_fee×N — прошлый расчёт эти столы уже тарифицировал), и в payload
    bd = pricing.compute_price(volume, minutes, qty, plastic, settings, discount_pct=discount,
                               extra_colors=extra_colors, plates=plates,
                               urgency_factor=urgency_factor)
    pricing.apply_options(bd, m.get("options"), _m_nparts(m))   # B4: опции поверх цены
    m["calc_plates"] = plates
    pdisp = f"{plastic.name} · {color}" if color else plastic.name
    m["last"] = {"plastic_name": pdisp, "printer": printer.name, "qty": qty,
                 "quality": quality.label, "strength": strength.label,
                 "dims": "", "grams_total": bd.grams_total,
                 "minutes_total": bd.minutes_total, "total": bd.total,
                 "plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
                 "color": color, "colors_sel": colors_sel, "color_grams": [],
                 "plastic_grams": [], "scale_pct": 100,
                 "volume_cm3_unit": volume, "minutes_unit": minutes, "extra_colors": extra_colors,
                 "mixed": False, "approx": True}
    m["pdf"] = {"bd": bd, "settings": settings, "model_label": m["label"],
                "plastic_name": pdisp, "printer_name": printer.name, "qty": qty,
                "quality_label": quality.label, "strength_label": strength.label,
                "plates": plates, "preview_path": m.get("cpreview")}
    m["calc_plates"] = int(plates or 1)
    payload = _result_payload(bd, settings, config.CURRENCY, plates, False)
    payload["approx"] = True
    payload["refining"] = True    # фоновый слайс идёт — фронт поллит и обновит цифры
    if m.get("cpreview"):
        payload["cpreview"] = f"/api/cpreview/{token}"
    payload["dims"] = ""
    payload["scale_pct"] = 100
    payload["queue"] = await db.queue_load(_live_progress())
    payload["eta"] = db.eta_date(payload["queue"].get("minutes", 0) + bd.minutes_total)
    payload["urgency"] = m.get("urgency") or "normal"           # B3: срочность
    # B3: срок по времени печати заказа (bd.minutes_total) — «срочно» всегда завтра, «обычно» —
    # завтра/послезавтра относительно порога term_split_hours.
    payload["term"] = db.promised_date(bd.minutes_total, m.get("urgency"), settings)
    # B2 (DFM): быстрый снимок ещё без габаритов (dims='') — вердикт тихий 'ok'; поля всё равно
    # ставим, чтобы фронт сбросил возможный прошлый вердикт. Фоновый слайс уточнит его в _refine_calc.
    _apply_dfm(payload, dims="", printer=printer, meas=None, settings=settings)
    m["last_payload"] = payload
    return payload

async def _remember_calc_cache(m: dict, res: dict, plastic, printer, quality, strength, *,
                               plates_only: list | None, plate_plastics: dict | None) -> None:
    """Запомнить результат честного слайса в calc_cache (OpenTodo ae038968) — после КАЖДОГО
    успешного calcservice.compute() в calc-путях мини-апки. Объём/время единицы БЕРЁМ из
    bd.grams_unit/bd.minutes_unit — это «сырые» цифры слайса ДО умножения на тираж и ДО
    надбавки за мультицвет (compute_price домножает их дальше), поэтому калькулятор кэша
    честно переиспользуется под любой пластик/тираж/скидку/доп.цвета при попадании в кэш.
    Мультиплейт с выбором столов/своими пластиками на столах НЕ кэшируем: там объём частичный
    (только выбранные столы) или смешанный по нескольким пластикам — «пластик не важен» не
    работает, а полная модель кэшу не соответствует. Аналитическую ≈оценку (слайс лёг) тоже НЕ
    кэшируем: это не результат слайса, кэш мгновенной цены им травить нельзя."""
    if plates_only or plate_plastics or res.get("estimated") or not getattr(plastic, "density", 0):
        return
    try:
        bd = res["bd"]
        volume = bd.grams_unit / plastic.density
        minutes = bd.minutes_unit
        if volume <= 0 or minutes <= 0:
            return
        mhash = cache.model_hash(m["paths"])
        await db.put_calc_cache(mhash, printer.id, quality.id, strength.id,
                                int(res.get("scale_pct") or 100), volume, minutes,
                                int(res.get("plates") or m.get("plates") or 1))
    except Exception:  # noqa: BLE001
        log.debug("calc_cache save failed", exc_info=True)

async def _bg_render_cpreview(token: str, m: dict, color: str) -> None:
    """Цветной рендер «гиганта» (>_REFINE_MAX_BYTES) ФОНОМ. Тяжёлый путь compute (он и рендерит)
    для таких моделей не запускается, и смета оставалась вообще без рендера — «ни 3д, ни рендера»
    (кейс «Флекси Дарт Вейдер», OpenTodo c23def0d; 3D чинится капом mesh3d._MODEL3D_MAX_SRC_BYTES).
    Рендер идёт в изолированном воркере (cpupool.run_render, память капнута) и кэшируется по
    хэш+цвет (pc7) — повтор бесплатный. Готовое превью попадает в m/квоту/PDF/заказ; павший
    воркер — не беда, остаёмся на базовом PNG модели."""
    try:
        overrides = await db.get_color_overrides()
        rgbs = materials.color_to_rgbs(color, overrides)
        rpaths = inspect3mf.drop_redundant_plate_stls(list(m["paths"]))   # без дублей (24.07)
        bed = await _bed_of_model(m)          # тот же стол, что у сметы этого токена
        cp = None
        if m.get("multicolor"):
            # мультицвет-гигант: те же по-детальные слоты, что у обычного пути (18ddd6c8)
            try:
                sel = (m.get("last") or {}).get("colors_sel") or []
                srgb = [materials.color_to_rgb(c, overrides) or (0.6, 0.64, 0.68) for c in sel]
                cp = await cpupool.run_render(preview.render_multicolor_preview, rpaths,
                                              srgb or None, bed=bed)
            except Exception:  # noqa: BLE001
                cp = None
        if not (cp and Path(cp).exists()):
            cp = await cpupool.run_render(preview.render_preview, rpaths, rgbs or None, bed=bed)
        if not (cp and Path(cp).exists()):
            return
        m2 = _models.get(token)
        if m2 is not m:
            return                                     # токен вытеснен — некому отдавать
        m["cpreview"] = str(cp)
        lp = m.get("last_payload")
        if lp is not None:
            lp["cpreview"] = f"/api/cpreview/{token}"
    except Exception:  # noqa: BLE001
        log.debug("bg cpreview render failed", exc_info=True)

async def _refine_estimate(token: str, seq: int, *, uid: int, printer, plastic, quality,
                           strength, color: str, colors_sel: list, qty: int,
                           extra_colors: int, promo_pct: float, urgency_factor: float = 1.0) -> None:
    """Фоновое уточнение ≈оценки ТОЧНЫМ замером геометрии (fastcalc wait_precise) для гигантов,
    которым честный слайс не светит (>_REFINE_MAX_BYTES): мгновенный ответ был bbox-прикидкой,
    здесь молча пересчитываем смету по точному замеру (кэшируется по хэшу модели — следующие
    расчёты мгновенно точные) и обновляем quote_state, как _refine_calc. Смета остаётся
    approx/estimated: это всё ещё аналитика, а не слайс."""
    m = _models.get(token)
    if not m:
        return
    try:
        fest = await fastcalc.estimate(
            m["paths"], plastic=plastic, quality=quality, strength=strength, qty=qty,
            extra_colors=extra_colors, plates=int(m.get("plates", 1) or 1), scale=1.0,
            printer=printer, wait_precise=True)
        m2 = _models.get(token)
        if m2 is m and int(m.get("calc_seq") or 0) == seq \
                and fest and float(fest.get("volume_cm3") or 0.0) > 0 \
                and fest.get("measure") != "bbox":
            cc_like = {"volume_cm3": fest["volume_cm3"],
                       # сырые минуты fastcalc × коэффициент времени машины (B1); в calc_cache
                       # минуты лежат уже умноженными (bd.minutes_unit), там не домножаем
                       "minutes": fest["minutes_unit"] * _time_factor(printer),
                       # точный замер знает раскладку набора по столам (defe5dc0)
                       "plates": int(fest.get("est_plates") or m.get("plates", 1) or 1)}
            payload = await _cache_snapshot(
                token, m, cc_like, uid=uid, printer=printer, plastic=plastic, quality=quality,
                strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                extra_colors=extra_colors, promo_pct=promo_pct, urgency_factor=urgency_factor)
            m["last"]["estimated"] = True
            payload["estimated"] = True
    except Exception:  # noqa: BLE001
        log.warning("фоновое уточнение ≈оценки упало — смета остаётся bbox-прикидкой",
                    exc_info=True)
    finally:
        m2 = _models.get(token)
        if m2 is m and int(m.get("calc_seq") or 0) == seq and m.get("last_payload"):
            m["last_payload"].pop("refining", None)
            nuid = m.pop("notify_calc", None)   # юзер ушёл, не дождавшись — дошлём итог
            if nuid and _bot is not None:
                try:
                    await notify.notify_user(
                        _bot, nuid,
                        texts.calc_ready_note(m.get("order_label") or m["label"], m["last_payload"]),
                        reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="quote",
                                                        token=token, text="Открыть смету"))
                except Exception:  # noqa: BLE001
                    log.debug("refine estimate-ready notify failed", exc_info=True)

async def _refine_calc(token: str, seq: int, *, uid: int, printer, plastic, quality,
                       strength, color: str, colors_sel: list, qty: int,
                       extra_colors: int, multicolor: bool, promo_pct: float,
                       plates_only: list | None = None, scale_pct: int = 100,
                       urgency_factor: float = 1.0) -> None:
    """Фоновый честный слайс после ЛЮБОЙ мгновенной приближённой цены (OpenTodo ae038968: MW-
    данные, calc_cache последнего слайса, быстрый пересчёт под другой пластик — «на фоне слайсить
    можно и цену потом просто обновить»): считаем честно и молча обновляем смету — фронт поллит
    quote_state, пока payload.refining. Юзер успел пересчитать с другими параметрами (calc_seq
    ушёл) → результат выбрасываем; слайс упал → смета остаётся быстрой (approx), но refining
    снимаем (обобщено из _mw_refine_calc, 97a91eee)."""
    m = _models.get(token)
    if not m:
        return
    try:
        await _refine_calc_inner(token, seq, m, uid=uid, printer=printer, plastic=plastic,
                                 quality=quality, strength=strength, color=color,
                                 colors_sel=colors_sel, qty=qty, extra_colors=extra_colors,
                                 multicolor=multicolor, promo_pct=promo_pct,
                                 plates_only=plates_only, scale_pct=scale_pct,
                                 urgency_factor=urgency_factor)
    except Exception:  # noqa: BLE001
        log.warning("фоновое уточнение слайсом упало — смета остаётся быстрой", exc_info=True)
    finally:
        # что бы ни случилось — фронт не должен поллить вечно
        m2 = _models.get(token)
        if m2 is m and int(m.get("calc_seq") or 0) == seq and m.get("last_payload"):
            m["last_payload"].pop("refining", None)
            # Юзер вышел, не дождавшись даже мгновенной сметы (подписка авто/кнопкой висит, а
            # мгновенные пути _api_calc_inner до своей отправки уведомления не доходят) — дошлём
            # итог отсюда: уточнённую цену, а при упавшем уточнении — быструю (approx).
            nuid = m.pop("notify_calc", None)
            if nuid and _bot is not None:
                try:
                    await notify.notify_user(
                        _bot, nuid,
                        texts.calc_ready_note(m.get("order_label") or m["label"], m["last_payload"]),
                        reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="quote",
                                                        token=token, text="Открыть смету"))
                except Exception:  # noqa: BLE001
                    log.debug("refine calc-ready notify failed", exc_info=True)

async def _refine_calc_inner(token: str, seq: int, m: dict, *, uid: int, printer, plastic,
                             quality, strength, color: str, colors_sel: list, qty: int,
                             extra_colors: int, multicolor: bool, promo_pct: float,
                             plates_only: list | None = None, scale_pct: int = 100,
                             urgency_factor: float = 1.0) -> None:
    try:
        res = await calcservice.compute(
            user_id=uid, model_paths=m["paths"], model_label=m["label"], printer=printer,
            plastic=plastic, color=color, qty=qty, quality=quality, strength=strength,
            extra_colors=extra_colors, plates=m["plates"], is_multicolor=multicolor,
            base_preview=_memo_preview(m, _bed_of_printer(printer)), promo_pct=promo_pct,
            plates_only=plates_only,
            scale_pct=scale_pct, urgency_factor=urgency_factor, colors_sel=colors_sel)
    except calcservice.CalcError as exc:
        log.info("фоновое уточнение слайсом не удалось (%s) — смета остаётся быстрой", exc)
        return
    m2 = _models.get(token)
    if m2 is not m or int(m.get("calc_seq") or 0) != seq:
        return
    await _remember_calc_cache(m, res, plastic, printer, quality, strength,
                               plates_only=plates_only, plate_plastics=None)
    bd = res["bd"]
    pricing.apply_options(bd, m.get("options"), _m_nparts(m, res))   # B4: опции поверх цены
    m["cpreview"] = res.get("preview_path") or m.get("cpreview")
    m["last"] = {"plastic_name": res["pdisp"], "printer": printer.name, "qty": qty,
                 "quality": quality.label, "strength": strength.label,
                 "dims": res.get("dims", ""), "grams_total": bd.grams_total,
                 "minutes_total": bd.minutes_total, "total": bd.total,
                 "plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
                 "color": color, "colors_sel": colors_sel, "color_grams": res.get("color_grams") or [],
                 "plastic_grams": [], "scale_pct": 100,
                 "volume_cm3_unit": (bd.grams_unit / plastic.density) if plastic.density else 0.0,
                 "minutes_unit": bd.minutes_unit, "extra_colors": bd.extra_colors,
                 # слайс упал и compute отдал аналитическую ≈оценку → цена по-прежнему приблизительна
                 "mixed": False, "approx": bool(res.get("estimated")), "estimated": bool(res.get("estimated"))}
    m["calc_plates"] = int(len(plates_only or []) or res.get("plates") or m.get("plates") or 1)
    m["pdf"] = {"bd": bd, "settings": res["settings"], "model_label": m["label"],
                "plastic_name": res["pdisp"], "printer_name": printer.name, "qty": qty,
                "quality_label": quality.label, "strength_label": strength.label,
                "plates": m["calc_plates"], "preview_path": res.get("preview_path")}
    payload = _result_payload(bd, res["settings"], config.CURRENCY, m["calc_plates"], res["cached"])
    payload["cpreview"] = f"/api/cpreview/{token}"
    payload["dims"] = res.get("dims", "")
    payload["scale_pct"] = 100
    payload["queue"] = await db.queue_load(_live_progress())
    payload["eta"] = db.eta_date(payload["queue"].get("minutes", 0) + bd.minutes_total)
    payload["urgency"] = m.get("urgency") or "normal"                    # B3: срочность
    # B3: срок по времени печати заказа (bd.minutes_total) — см. правило в db.promised_date.
    payload["term"] = db.promised_date(bd.minutes_total, m.get("urgency"), res["settings"])
    # B2 (DFM): фоновый слайс уточнил габариты — пересчитываем вердикт и кэшируем замер на m,
    # чтобы быстрая смена пластика (requote) переиспользовала его без нового замера.
    m["dfm_meas"] = res.get("dfm")
    _apply_dfm(payload, dims=res.get("dims", ""), printer=printer,
               meas=res.get("dfm"), settings=res["settings"])
    if res.get("estimated"):     # уточнение слайсом не удалось (слайсер лёг) — остаёмся на ≈оценке
        payload["approx"] = True
        payload["estimated"] = True
    m["last_payload"] = payload
    # Заказ оформлен во время слайса (order_now) на БЫСТРОМ пути (MW-данные / кэш слайса): там
    # _api_calc_inner отдаёт approx и уходит в return ДО calc-хвоста, а честный слайс досчитывается
    # уже здесь, фоном — значит и цену пендинг-заказу проставляем отсюда (f1e80d67). Без этого
    # заявка, оформленная во время MW/кэш-расчёта, осталась бы навсегда unsliced=1/price=0.
    poid = m.pop("pending_order_id", None)
    if poid is not None:
        try:
            await _finalize_pending_order(poid, m, uid)
        except Exception:  # noqa: BLE001
            log.warning("refine: финализация заказа #%s не удалась", poid, exc_info=True)
    # чат-путь: отредактировать caption сметного сообщения точной ценой
    ref = m.pop("mw_refine_msg", None)
    if ref and _bot is not None:
        try:
            # сохраняем кнопку заказа из чата (идея 3) с уже уточнённой ценой; если клиент успел
            # заказать до уточнения — кнопку не возвращаем (только смета).
            order_text = None if m.get("ordered_rid") else ("⚡ Заказать · " + str(payload.get("total") or ""))
            kb = keyboards.quote_actions(config.WEBAPP_PUBLIC_URL, token, order_text=order_text)
            caption = texts.calc_ready_note(m["label"], payload) + \
                "\n<i>Цена уточнена по слайсу.</i>"
            if ref[0] == "photo":
                await _bot.edit_message_caption(chat_id=ref[1], message_id=ref[2],
                                                caption=caption, reply_markup=kb)
            else:
                await _bot.edit_message_text(caption, chat_id=ref[1], message_id=ref[2],
                                             reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.debug("mw refine message edit failed", exc_info=True)
    # Дописываем уточнённые цифры в снимок сметы (26.08): мгновенная ветка показала клиенту
    # приблизительную цену, честный слайс её поправил — разница между показанным и уточнённым
    # и есть прямой ответ мастеру, нормальную ли цену человеку назвали. Исходную не затираем.
    # Стоит В САМОМ КОНЦЕ: это наша бухгалтерия, и ни клиент, ни его смета её ждать не должны.
    if not res.get("estimated"):
        await db.mark_calc_refined(m.get("calc_snap_id"), total=bd.total, grams=bd.grams_total,
                                   minutes=bd.minutes_total)


async def _resolve_options(raw) -> list[dict]:
    """Опции постобработки (B4): id-шники из тела /api/calc → снимок активного каталога
    [{id, name, price}]. Снимаем имя+цену СЕЙЧАС (не на момент заказа) один раз, чтобы дальше
    apply_options на всех путях расчёта работал с одними и теми же цифрами, а при оформлении
    заказа снимок уехал в requests.breakdown и request_options. Кривые/неактивные id молча
    отбрасываем. Порядок — как в каталоге (sort)."""
    if not isinstance(raw, list) or not raw:
        return []
    want = {_toint(x) for x in raw if _toint(x)}
    if not want:
        return []
    # У «за-детальных» услуг (accee7a9) price каталога — ставка ЗА ДЕТАЛЬ; в снимок кладём её как
    # rate, а price пересчитает apply_options (ставка × детали × тираж) на каждом пути расчёта.
    return [{"id": o.id, "name": o.name, "price": float(o.price),
             **({"per_part": 1, "rate": float(o.price)} if o.per_part else {})}
            for o in await db.list_postproc_options(active_only=True) if o.id in want]

# ---- выбор клиента для расчёта: ОДНИ правила для /api/calc, order_now и очереди досчёта ----
# Очередь досчёта (Э0, app/deferred.py) пересчитывает заказ order_now, если живой расчёт до него
# не дожил (рестарт, слайс дольше придержки, пересчёт на том же токене). Её цена обязана быть
# той же, что дал бы живой /api/calc (review:e0#0): разъедутся правила — клиенту на
# подтверждение уйдёт сумма без надбавки за многоцвет или за всю модель вместо выбранных столов.
# Поэтому правила разбора выбора — здесь, функциями, а не строками внутри _api_calc_inner.

def _multicolor_choice(model_mc: bool, num_colors, mixed_plates: bool, want: bool,
                       req_colors: int) -> tuple[bool, int]:
    """(многоцвет, доп. цветов для надбавок multicolor_*). Для .3mf с распознанными цветами
    (model_mc) число цветов знает инспекция; у STL/параметрики инспекция цвета НЕ видит —
    доверяем числу от клиента (colors = разные пластики основа/текст/полоса), иначе
    мультицветный брелок считался бы одноцветным (баг ревью #1). Цвета по РАЗНЫМ столам
    (каждый стол одноцветный) печатаются без продувки/смен — надбавку не берём (OpenTodo
    0bcdd728; mixed_plates — из инспекции)."""
    if model_mc:
        mc = bool(want)
        return mc, (max(0, int(num_colors or 1) - 1) if mc and mixed_plates else 0)
    if want and int(req_colors or 0) > 1:
        return True, min(max(int(req_colors), 2), 8) - 1     # кламп: 2..8 цветов → 1..7 доп.
    return False, 0


def _pick_colors_sel(raw, plastic) -> list[str]:
    """Слоты цветов мультицвета (a197bb93): имя на каждый слот, только из палитры пластика."""
    if not isinstance(raw, list):
        return []
    seen = [str(c or "").strip() for c in raw[:8]]
    pal = plastic.color_list or []
    return [c for c in seen if c and c in pal]


async def _resolve_plate_plastics(raw, nplates: int, plastic) -> dict[int, object]:
    """Свой пластик на стол (OpenTodo 70525c99): {индекс стола: plastic_id} → {индекс: Plastic}.
    Невалидные id/индексы, неактивные пластики и совпадающие с основным молча пропускаем."""
    out: dict[int, object] = {}
    if int(nplates or 1) > 1 and isinstance(raw, dict):
        for k, v in raw.items():
            i, pid = _toint(k, -1), _toint(v)
            if 0 <= i < int(nplates) and pid and pid != plastic.id:
                pobj = await db.get_plastic(pid)
                if pobj and pobj.is_active:
                    out[i] = pobj
    return out


def _valid_plate_colors(raw, nplates: int, plastic, plate_plastics: dict) -> dict[int, str]:
    """Цвета по столам {индекс: имя цвета}: валидируем по палитре ЭФФЕКТИВНОГО пластика стола
    (свой пластик стола или общий, OpenTodo 70525c99); стол со своим пластиком без цвета —
    первый цвет его палитры. Не назначенные столы — базовый цвет (в словарь не попадают)."""
    if int(nplates or 1) <= 1:
        return {}
    pc_raw = raw if isinstance(raw, dict) else {}
    out: dict[int, str] = {}
    for k, v in pc_raw.items():
        i = _toint(k, -1)
        v = str(v or "").strip()
        if 0 <= i < int(nplates) and v:
            vc = set((plate_plastics.get(i) or plastic).color_list or [])
            if not vc or v in vc:
                out[i] = v
    for i, pobj in plate_plastics.items():
        if i not in out:
            pcl = pobj.color_list or []
            if pcl:
                out[i] = pcl[0]
    return out


def _plates_material_label(plastic, color: str, plate_plastics: dict, plate_colors: dict,
                           sel) -> str | None:
    """Честная подпись материала заказа при разных цветах/пластиках на столах («Elegoo PLA ·
    стол 1: Красный, стол 3: PETG Чёрный»); None — все выбранные столы одним пластиком и цветом."""
    def _eff(i):
        return plate_plastics.get(i) or plastic
    if not any(i in plate_plastics or plate_colors.get(i, color) != color for i in sel):
        return None

    def _tag(i):
        pn = f"{_eff(i).name} " if i in plate_plastics else ""
        return f"стол {i + 1}: {pn}{plate_colors.get(i, color) or '—'}".rstrip()
    mix = ", ".join(_tag(i) for i in sel)
    base = plastic.name if not plate_plastics else " + ".join(dict.fromkeys(_eff(i).name for i in sel))
    return f"{base} · {mix}"


def _plastic_grams_last(res: dict, plate_colors: dict, color: str) -> list[dict]:
    """Смешанные пластики столов: «пластик → граммы юнита + цвет его столов» для
    по-пластикового плана склада (OpenTodo 70525c99)."""
    out = []
    for g in (res.get("plastic_grams") or []):
        own = [plate_colors[i] for i in g.get("plates", []) if plate_colors.get(i)]
        out.append({"plastic_id": g["plastic_id"], "name": g["name"],
                    "grams_unit": g["grams_unit"], "color": own[0] if own else (color or None)})
    return out


def _order_label(m: dict, plates_sel, scale_pct) -> str:
    """Ярлык заказа с пометками «· стол(ы) 1,3 · масштаб 150% · комплект: …» — мастер видит,
    ЧТО и КАК печатать; "" — пометок нет (история/коллекция хранят чистый ярлык модели)."""
    tags = ([f"стол(ы) {','.join(str(i + 1) for i in plates_sel)}"] if plates_sel else []) \
        + ([f"масштаб {int(scale_pct)}%"] if scale_pct and int(scale_pct) != 100 else []) \
        + ([f"комплект: {m['variant_label']}"] if m.get("variant_label") else [])
    return f"{m['label']} · {' · '.join(tags)}" if tags else ""


def _m_nparts(m: dict, res: dict | None = None) -> int | None:
    """Число деталей модели для «за-детальных» опций постобработки (accee7a9): из свежего замера
    res["dfm"] (fastmeasure), иначе из закэшированного на токене m["dfm_meas"] (замер прошлого
    расчёта той же модели — деталей от пластика/качества не прибавляется). None — деталей никто
    не считал (MW/кэш-снимок до первого замера): apply_options возьмёт parts из снимка опции или 1."""
    for src in ((res or {}).get("dfm"), m.get("dfm_meas")):
        try:
            v = int((src or {}).get("n_parts") or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    return None

async def _api_calc_inner(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"calc:{uid}", config.RATE_CALCS_PER_MIN, 60):
        return web.json_response({"ok": False, "error": "Слишком много расчётов — подождите минуту"})
    m = _models.get(body.get("token", ""))
    if not m or m.get("uid") != uid:
        return web.json_response({"ok": False, "expired": True,
                                  "error": "Модель не найдена — загрузите заново"}, status=404)
    m.pop("notify_calc", None)   # новый расчёт сбрасывает прошлую подписку на уведомление
    # ...и привязку к заказу, оформленному во время ПРОШЛОГО слайса. Цену тому заказу живой
    # расчёт уже не проставит (параметры другие) — его задание в очереди досчёта (Э0) созревает
    # сразу, а не через придержку: заказ посчитает сборщик по выбору, с которым его оформили.
    _lost = m.pop("pending_order_id", None)
    if _lost is not None:
        await deferred.release_live(_lost)
    m.pop("ordered_now", None); m.pop("ordered_now_priced", None)   # новый расчёт = можно заказать заново
    printer = (await db.get_printer(_toint(body.get("printer_id")))) if body.get("printer_id") \
        else await db.get_default_printer()
    plastic = await db.get_plastic(_toint(body.get("plastic_id")))
    quality = await db.get_quality(_toint(body.get("quality_id")))
    strength = await db.get_strength(_toint(body.get("strength_id")))
    if not (printer and plastic and quality and strength):
        return web.json_response({"ok": False, "error": "Проверьте выбор параметров"})
    # Срочность (B3): клиент выбирает 'normal'/'rush'. Множитель наценки берём из настроек один раз
    # и прокидываем во ВСЕ пути расчёта (MW-снимок, кэш-снимок, фон-слайс, полный слайс) — иначе
    # срочный заказ молча посчитался бы обычной ценой в пропущенном пути. Неизвестное/отсутствующее
    # значение = обычная печать (старые кэшированные клиенты без поля работают без изменений).
    urgency = "rush" if str(body.get("urgency") or "").strip().lower() == "rush" else "normal"
    urgency_factor = pricing.urgency_factor_from(urgency, await db.get_settings())
    m["urgency"] = urgency
    m["urgency_factor"] = urgency_factor
    # Опции постобработки (B4): резолвим выбранные id в снимок каталога ОДИН раз и кладём в m —
    # apply_options на КАЖДОМ пути расчёта (MW-снимок, кэш-снимок, фон-слайс, полный слайс,
    # requote) читает m["options"], иначе услуги молча пропали бы в пропущенном пути (утечка
    # выручки). Пустой выбор → []; снимок уедет в заказ при оформлении (данные-сейф).
    m["options"] = await _resolve_options(body.get("options"))
    color = (body.get("color") or "").strip()
    qty = max(1, min(1000, _toint(body.get("qty"), 1)))
    # Масштаб модели в % (по умолчанию 100). Верхнюю границу против СТОЛА проверяет
    # calcservice.effective_scale — тут только санитайз диапазона от кривого клиента.
    scale_pct = max(10, min(1000, _toint(body.get("scale"), 100)))
    # Масштаб идущего расчёта: заказ, оформленный во время слайса (order_now — масштаб в теле
    # не шлёт), иначе не знает, в каком размере клиент хотел модель.
    m["calc_scale_pct"] = scale_pct
    # Число цветов → надбавки multicolor_*. Для .3mf берём из инспекции (m["num_colors"]); для STL
    # (в т.ч. параметрических из кастомайзера) инспекция цвета НЕ видит → доверяем числу от клиента
    # (colors = разные пластики основа/текст/полоса). Иначе мультицветный брелок считался как
    # одноцветный и надбавки за продувку/смены цвета терялись (баг ревью #1).
    req_colors = _toint(body.get("colors"), 0)
    multicolor, extra_colors = _multicolor_choice(
        bool(m["multicolor"]), m.get("num_colors"), m.get("mixed_plates", True),
        bool(body.get("multicolor")), req_colors)
    # Слоты цветов мультицвета (a197bb93): фронт шлёт colors_sel=[имя на каждый слот] —
    # склеиваем через « - », ровно как Telegram-флоу (превью станет градиентным, заказ и
    # склад увидят все цвета). Наценка НЕ зависит от выбора: extra_colors уже посчитан выше.
    colors_sel: list[str] = _pick_colors_sel(body.get("colors_sel"), plastic) if multicolor else []
    if colors_sel:
        color = " - ".join(colors_sel)
    # Выбор столов мультиплейт-модели (OpenTodo 77dc9b01): фронт шлёт plates=[0-базные индексы].
    # Валидный НЕПОЛНЫЙ набор → режем только выбранные; полный/кривой → обычный расчёт целиком.
    nplates = int(m.get("plates", 1) or 1)
    plates_sel = _parse_plates_sel(body.get("plates"), nplates)
    # Свой пластик на стол (OpenTodo 70525c99): фронт шлёт plate_plastics={idx: plastic_id}.
    plate_plastics = await _resolve_plate_plastics(body.get("plate_plastics"), nplates, plastic)
    plate_colors_sel = _valid_plate_colors(body.get("plate_colors"), nplates, plastic, plate_plastics)
    promo_code = (body.get("promo") or "").strip().upper()
    promo_pct = await db.validate_promo(promo_code) if promo_code else None
    m["promo_code"] = promo_code if promo_pct else ""   # для списания при оформлении (только валидный)
    m["promo_typed"] = promo_code                       # что ввёл клиент — пишем в заказ даже если не сработал
    m["promo_ok"] = 1 if promo_pct else 0
    # Выбор ИДУЩЕГО расчёта целиком, идентификаторами (review:e0#0): заказ, оформленный во время
    # слайса (order_now), кладёт его в задание очереди досчёта — и если живой расчёт до заказа
    # не доживёт, сборщик посчитает ровно то же, что считается сейчас. Старый фронт шлёт в
    # order_now только принтер/пластик/пресеты/тираж/цвет — остальное заказ берёт отсюда.
    m["calc_choice"] = {"multicolor": bool(body.get("multicolor")), "colors": req_colors,
                        "colors_sel": list(colors_sel), "plates": list(plates_sel or []),
                        "plate_plastics": {str(i): p.id for i, p in plate_plastics.items()},
                        "plate_colors": {str(i): c for i, c in plate_colors_sel.items()},
                        "scale": scale_pct, "options": [o["id"] for o in m["options"]],
                        "promo": promo_code}
    # Мгновенная цена из данных MakerWorld (97a91eee): вес+время профиля уже посчитаны автором —
    # смета отдаётся сразу (approx), а честный слайс бежит ФОНОМ и сам обновляет её (фронт
    # поллит quote_state, пока refining). Масштаб/выбор столов/свой пластик стола требуют
    # пересборки геометрии — MW-данные их не покрывают, там обычный путь со слайсом.
    if scale_pct == 100 and not plates_sel and not plate_plastics:
        est = await _mw_estimate_for(m)
        if est:
            token = body.get("token", "")
            payload = await _mw_snapshot(
                token, m, est, uid=uid, printer=printer, plastic=plastic, quality=quality,
                strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                plates=int(m.get("plates", 1) or 1), promo_pct=promo_pct or 0.0,
                urgency_factor=urgency_factor)
            if promo_pct:
                payload["promo"] = {"code": promo_code, "pct": promo_pct}
            elif promo_code:
                payload["promo_invalid"] = True
            m["calc_seq"] = seq = int(m.get("calc_seq") or 0) + 1
            # снимок ЭТОЙ сметы ещё не записан (его пишет _snap_calc ниже): фоновый слайс,
            # успевший финишировать раньше, не должен дописать уточнение в ПРОШЛЫЙ снимок
            m["calc_snap_id"] = None
            asyncio.create_task(_refine_calc(
                token, seq, uid=uid, printer=printer, plastic=plastic, quality=quality,
                strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                extra_colors=extra_colors, multicolor=multicolor, promo_pct=promo_pct or 0.0,
                urgency_factor=urgency_factor))
            await _snap_calc(uid, "рассчитал цену",
                             f'{m["label"]} · {plastic.name} ×{qty} · MW-данные', source="mw",
                             m=m, payload=payload, printer=printer, plastic=plastic,
                             quality=quality, strength=strength, color=color, qty=qty)
            return web.json_response({"ok": True, "result": payload})
        # OpenTodo ae038968: MW-данных нет — пробуем кэш ПОСЛЕДНЕГО слайса этой же модели тем же
        # принтером/качеством/прочностью (масштаб 100%): грамм/время от пластика не зависят, цена
        # считается штатной формулой мгновенно, а честный слайс уточняет её в фоне (как MW-путь).
        model_hash = cache.model_hash(m["paths"])
        cc = await db.get_calc_cache(model_hash, printer.id, quality.id, strength.id, 100)
        if cc:
            token = body.get("token", "")
            payload = await _cache_snapshot(
                token, m, cc, uid=uid, printer=printer, plastic=plastic, quality=quality,
                strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                extra_colors=extra_colors, promo_pct=promo_pct or 0.0,
                urgency_factor=urgency_factor)
            if promo_pct:
                payload["promo"] = {"code": promo_code, "pct": promo_pct}
            elif promo_code:
                payload["promo_invalid"] = True
            m["calc_seq"] = seq = int(m.get("calc_seq") or 0) + 1
            # снимок ЭТОЙ сметы ещё не записан (его пишет _snap_calc ниже): фоновый слайс,
            # успевший финишировать раньше, не должен дописать уточнение в ПРОШЛЫЙ снимок
            m["calc_snap_id"] = None
            asyncio.create_task(_refine_calc(
                token, seq, uid=uid, printer=printer, plastic=plastic, quality=quality,
                strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                extra_colors=extra_colors, multicolor=multicolor, promo_pct=promo_pct or 0.0,
                urgency_factor=urgency_factor))
            await _snap_calc(uid, "рассчитал цену",
                             f'{m["label"]} · {plastic.name} ×{qty} · кэш слайса', source="cache",
                             m=m, payload=payload, printer=printer, plastic=plastic,
                             quality=quality, strength=strength, color=color, qty=qty)
            return web.json_response({"ok": True, "result": payload})
        # Мгновенная аналитическая ≈оценка (Рычаг Б, app.fastcalc): НОВАЯ модель без MW-данных и без
        # кэша слайса — НЕ блокируем клиента слайсом. Считаем ≈цену из геометрии меша за <1 c и
        # отдаём СРАЗУ (approx + refining), а честный слайс уточняет её в ФОНЕ (_refine_calc) — как
        # MW/кэш-пути. Слайсер лёг (VRAM) → фон вернёт ту же оценку (compute сам фолбэчит), не тупик.
        try:
            fest = await fastcalc.estimate(
                m["paths"], plastic=plastic, quality=quality, strength=strength, qty=qty,
                extra_colors=extra_colors, plates=int(m.get("plates", 1) or 1), scale=1.0,
                printer=printer)
        except Exception:  # noqa: BLE001
            fest = None
        if fest and float(fest.get("volume_cm3") or 0.0) > 0:
            token = body.get("token", "")
            cc_like = {"volume_cm3": fest["volume_cm3"],
                       "minutes": fest["minutes_unit"] * _time_factor(printer),   # B1, см. выше
                       # точный замер знает раскладку набора по столам (defe5dc0)
                       "plates": int(fest.get("est_plates") or m.get("plates", 1) or 1)}
            payload = await _cache_snapshot(
                token, m, cc_like, uid=uid, printer=printer, plastic=plastic, quality=quality,
                strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                extra_colors=extra_colors, promo_pct=promo_pct or 0.0,
                urgency_factor=urgency_factor)
            m["last"]["estimated"] = True       # грубая аналитика, а не кэш реального слайса
            payload["estimated"] = True
            if promo_pct:
                payload["promo"] = {"code": promo_code, "pct": promo_pct}
            elif promo_code:
                payload["promo_invalid"] = True
            # Огромный меш (koala-класс, 100+ МБ): честный слайс всё равно упрётся в память/коллизию
            # и рендер превью на десятки минут (OOM-прон). Слайс НЕ гоняем, но если геометрия
            # посчиталась лишь bbox-прикидкой (точный замер не успел), фоном доводим ЕЁ до
            # точного замера (fastcalc wait_precise + кэш) и молча обновляем смету — та же
            # механика quote_state, что у фонового слайса. Обычные модели — честный слайс в фоне.
            total_bytes = 0
            for p in m["paths"]:
                try:
                    total_bytes += os.path.getsize(p)
                except OSError:
                    pass
            # Гигант мимо compute → рендера не будет НИОТКУДА: докрашиваем фоном (c23def0d).
            # Один запуск на токен+цвет: кэш pc7 делает повтор дешёвым, но не бесплатным (хэш массива
            # файлов) — не плодим задачи на каждый тап пересчёта.
            if total_bytes > _REFINE_MAX_BYTES and not m.get("cpreview") \
                    and m.get("_render_color") != color:
                m["_render_color"] = color
                asyncio.create_task(_bg_render_cpreview(body.get("token", ""), m, color))
            if total_bytes <= _REFINE_MAX_BYTES:
                m["calc_seq"] = seq = int(m.get("calc_seq") or 0) + 1
                # снимок ЭТОЙ сметы ещё не записан (его пишет _snap_calc ниже): фоновый слайс,
                # успевший финишировать раньше, не должен дописать уточнение в ПРОШЛЫЙ снимок
                m["calc_snap_id"] = None
                asyncio.create_task(_refine_calc(
                    token, seq, uid=uid, printer=printer, plastic=plastic, quality=quality,
                    strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                    extra_colors=extra_colors, multicolor=multicolor, promo_pct=promo_pct or 0.0,
                    urgency_factor=urgency_factor))
            elif fest.get("measure") == "bbox":
                m["calc_seq"] = seq = int(m.get("calc_seq") or 0) + 1
                # снимок ЭТОЙ сметы ещё не записан (его пишет _snap_calc ниже): фоновый слайс,
                # успевший финишировать раньше, не должен дописать уточнение в ПРОШЛЫЙ снимок
                m["calc_snap_id"] = None
                asyncio.create_task(_refine_estimate(
                    token, seq, uid=uid, printer=printer, plastic=plastic, quality=quality,
                    strength=strength, color=color, colors_sel=colors_sel, qty=qty,
                    extra_colors=extra_colors, promo_pct=promo_pct or 0.0,
                    urgency_factor=urgency_factor))
            else:
                payload.pop("refining", None)   # замер уже точный, слайса не будет → не поллим
                log.info("≈оценка (точный замер) без фонового слайса: меш %d МБ тяжёл для слайса",
                         total_bytes // (1024 * 1024))
            await _snap_calc(uid, "рассчитал цену",
                             f'{m["label"]} · {plastic.name} ×{qty} · ≈оценка', source="estimate",
                             m=m, payload=payload, printer=printer, plastic=plastic,
                             quality=quality, strength=strength, color=color, qty=qty)
            return web.json_response({"ok": True, "result": payload})
    try:
        res = await calcservice.compute(
            user_id=uid, model_paths=m["paths"], model_label=m["label"], printer=printer,
            plastic=plastic, color=color, qty=qty, quality=quality, strength=strength,
            extra_colors=extra_colors, plates=m["plates"], is_multicolor=multicolor,
            base_preview=_memo_preview(m, _bed_of_printer(printer)),
            promo_pct=promo_pct or 0.0, plates_only=plates_sel,
            plate_plastics=plate_plastics or None, scale_pct=scale_pct,
            urgency_factor=urgency_factor, colors_sel=colors_sel)
    except calcservice.CalcError as exc:
        nuid = m.pop("notify_calc", None)   # подписан на уведомление → честно сообщить о неудаче
        if nuid and _bot is not None:
            try:
                await notify.notify_user(_bot, nuid, "⚠️ Не удалось посчитать "
                                         f"«{texts.esc(m['label'])}»: {texts.esc(str(exc))}")
            except Exception:  # noqa: BLE001
                log.debug("calc-fail notify failed", exc_info=True)
        # Тупик клиента (OpenTodo 55a1cf8f): слайс не удался — но заказ всё равно можно оформить,
        # мастер посмотрит модель сам и назначит цену вручную («Изменить цену» → по граммам/
        # времени, уже готовый инструмент). Запоминаем параметры, с которыми считали, — они
        # понадобятся /api/order_unsliced, чтобы записать в заказ то, что клиент реально выбрал.
        # scale_pct — ЗАПРОШЕННЫЙ масштаб: подгонка под стол живёт внутри упавшего расчёта.
        m["unsliced_ctx"] = {"printer": printer, "plastic": plastic, "quality": quality,
                             "strength": strength, "color": color, "qty": qty, "error": str(exc),
                             "scale_pct": scale_pct}
        # Заказ оформили во время слайса, а слайс упал. Живой хвост цену уже не проставит —
        # задание очереди досчёта (Э0) созревает сразу: сборщик попробует ещё раз и, если
        # модель и правда не считается, отдаст заказ мастеру с алертом и честным сообщением
        # клиенту (до Э0 заявка молча оставалась «оценит мастер» без единого уведомления).
        _lost = m.pop("pending_order_id", None)
        if _lost is not None:
            await deferred.release_live(_lost)
        return web.json_response({"ok": False, "error": str(exc), "can_order_unsliced": True})
    await _remember_calc_cache(m, res, plastic, printer, quality, strength,
                               plates_only=plates_sel, plate_plastics=plate_plastics or None)
    bd = res["bd"]
    pricing.apply_options(bd, m.get("options"), _m_nparts(m, res))   # B4: опции поверх цены
    scale_pct = int(res.get("scale_pct", 100) or 100)   # фактический (после клампа к столу)
    # Подмножество столов и масштаб ≠ 100% → помечаем заказ в ярлыке («· столы 1,3 · масштаб
    # 150%»), чтобы мастер видел, ЧТО и КАК печатать; история/коллекция хранят чистый ярлык
    # полной модели (для повторного использования).
    m["plates_sel"] = plates_sel
    m["order_label"] = _order_label(m, plates_sel, scale_pct)
    m["cpreview"] = res["preview_path"]
    if int(m.get("plates", 1) or 1) > 1:
        # Мультиплейт: вместо общего рендера — КОЛЛАЖ из столов (только выбранных), каждый в
        # СВОЁМ цвете (OpenTodo eb4424d9 + 65aa1a35); он же идёт в заказ/уведомления/PDF.
        # Любой сбой — остаёмся на обычном превью.
        # цвета по столам (_valid_plate_colors выше): не назначенные — базовый цвет
        plate_colors = plate_colors_sel
        m["plate_colors"] = plate_colors
        sel = plates_sel if plates_sel else list(range(int(m["plates"])))
        try:
            overrides = await db.get_color_overrides()
            by_color: dict = {}
            for i in sel:
                by_color.setdefault(plate_colors.get(i, color), []).append(i)
            imgs_by_idx: dict = {}
            for cname, idxs in by_color.items():
                rgbs = materials.color_to_rgbs(cname, overrides) if cname else None
                imgs = await cpupool.run_render(preview.render_plate_previews, m["paths"], rgbs or None)
                for i in idxs:
                    if 0 <= i < len(imgs):
                        imgs_by_idx[i] = imgs[i]
            chosen = [imgs_by_idx.get(i, "") for i in sel]
            collage = await asyncio.to_thread(preview.compose_plate_collage, chosen)
            if collage:
                m["cpreview"] = str(collage)
                res["preview_path"] = str(collage)
            elif not res.get("preview_path"):
                # Коллаж не собрался (тяжёлый многостольник — плейт-рендеры пустые). Берём
                # встроенную в .3mf миниатюру, чтобы заказ не остался без картинки (c6d9cebc).
                tb = await asyncio.to_thread(preview.extract_3mf_thumbnail, m["paths"])
                if tb:
                    m["cpreview"] = str(tb)
                    res["preview_path"] = str(tb)
        except Exception:  # noqa: BLE001
            log.debug("plate collage failed", exc_info=True)
        # разные цвета/пластики на столах → честная подпись материала в заказе/смете (от
        # рендера не зависит: тот же ярлык ставит и сборщик очереди досчёта)
        mix_label = _plates_material_label(plastic, color, plate_plastics, plate_colors, sel)
        if mix_label:
            res["pdisp"] = mix_label
    # Смешанные пластики столов: снимок «пластик → граммы юнита + цвет его столов» для
    # по-пластикового списания склада при оформлении заказа (OpenTodo 70525c99).
    pg_last = _plastic_grams_last(res, m.get("plate_colors") or {}, color)
    m["last"] = {"plastic_name": res["pdisp"], "printer": printer.name, "qty": qty,
                 "quality": quality.label, "strength": strength.label, "dims": res.get("dims", ""),
                 "grams_total": bd.grams_total, "minutes_total": bd.minutes_total, "total": bd.total,
                 "plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
                 "color": color, "colors_sel": colors_sel, "color_grams": res.get("color_grams") or [],
                 "plastic_grams": pg_last, "scale_pct": scale_pct,
                 # для быстрого пересчёта под другой пластик без переслайса (OpenTodo 050aff56):
                 # объём и минуты зависят от профиля печати, не от пластика
                 "volume_cm3_unit": (bd.grams_unit / plastic.density) if plastic.density else 0.0,
                 "minutes_unit": bd.minutes_unit, "extra_colors": bd.extra_colors,
                 # estimated=True → слайс лёг, цена от аналитической ≈оценки (Рычаг Б): помечаем
                 # approx, чтобы UI/уведомления показали «~» и «мастер уточнит» (тот же механизм,
                 # что у быстрого пересчёта). approx уводит и заказ в ручную проверку мастером.
                 "mixed": bool(pg_last), "approx": bool(res.get("estimated"))}
    # Столов в расчёте: выбранное подмножество, иначе — из результата compute (для набора деталей
    # он больше m["plates"]: раскладка/цветовые группы, OpenTodo defe5dc0), иначе — модельные.
    m["calc_plates"] = int(len(plates_sel) if plates_sel
                           else (res.get("plates") or m.get("plates") or 1))
    m["pdf"] = {"bd": bd, "settings": res["settings"], "model_label": m.get("order_label") or m["label"],
                "plastic_name": res["pdisp"], "printer_name": printer.name, "qty": qty,
                "quality_label": quality.label, "strength_label": strength.label,
                "plates": m["calc_plates"],
                "preview_path": res["preview_path"]}
    payload = _result_payload(bd, res["settings"], config.CURRENCY,
                              m["calc_plates"], res["cached"])
    if res.get("estimated"):
        payload["approx"] = True    # ≈оценка вместо слайса: «~цена», «мастер уточнит» (без refining)
    if plates_sel:
        payload["plates_sel"] = plates_sel
    payload["cpreview"] = f"/api/cpreview/{body['token']}"
    payload["dims"] = res.get("dims", "")
    payload["scale_pct"] = scale_pct
    if res.get("scale_clamped"):
        payload["scale_clamped"] = True                       # фронт покажет тост про кламп
        payload["scale_max_pct"] = res.get("scale_max_pct")
    if promo_pct:
        payload["promo"] = {"code": promo_code, "pct": promo_pct}
    elif promo_code:
        payload["promo_invalid"] = True
    payload["queue"] = await db.queue_load(_live_progress())   # очередь с поправкой на живую печать (#8)
    payload["eta"] = db.eta_date(payload["queue"].get("minutes", 0) + bd.minutes_total)   # #515
    payload["urgency"] = urgency                                       # B3: срочность
    # B3: срок по времени печати заказа (bd.minutes_total) — см. правило в db.promised_date.
    payload["term"] = db.promised_date(bd.minutes_total, urgency, res["settings"])
    # B2 (DFM): структурированный вердикт печатопригодности вместо прежней голой пилюли-варнинга
    # (5715a757 убрал текст «габарит больше стола», который пугал на наборах). Теперь гейт одной
    # детали + по-детальный замер отделяют реальную проблему от валидного набора мелочи. scale_max_pct
    # отдаём всегда — фронт предлагает рычаг «уменьшить до N%» при 'wont_fit'. Замер кэшируем на m
    # для быстрого requote без переизмерения.
    payload["scale_max_pct"] = res.get("scale_max_pct")
    m["dfm_meas"] = res.get("dfm")
    _apply_dfm(payload, dims=res.get("dims", ""), printer=printer,
               meas=res.get("dfm"), settings=res["settings"])
    await _snap_calc(uid, "рассчитал цену", f'{m["label"]} · {plastic.name} ×{qty}',
                     source="slice", m=m, payload=payload, bd=bd, printer=printer,
                     plastic=plastic, quality=quality, strength=strength, color=color, qty=qty,
                     preview=res.get("preview_path") or "")
    m["last_payload"] = payload   # смета по токену для deep-link из уведомления (ae4c6c13)
    poid = m.pop("pending_order_id", None)   # заказ оформлен во время слайса (order_now) → цена готова
    if poid is not None:
        await _finalize_pending_order(poid, m, uid)
    nuid = m.pop("notify_calc", None)
    if nuid and _bot is not None:
        try:
            await notify.notify_user(
                _bot, nuid, texts.calc_ready_note(m.get("order_label") or m["label"], payload),
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="quote",
                                                token=body.get("token"), text="Открыть смету"))
        except Exception:  # noqa: BLE001
            log.debug("calc-ready notify failed", exc_info=True)
    return web.json_response({"ok": True, "result": payload})

async def _api_calc_notify(request: web.Request) -> web.Response:
    """«Уведомить, когда посчитается» (OpenTodo ae4c6c13): юзер не ждёт анимацию — бот пришлёт
    цену сообщением с кнопкой на смету. Подписка живёт в записи модели; шлёт её конец
    _api_calc_inner (он переживает закрытие приложения благодаря shield-обёртке)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    m = _models.get(body.get("token", ""))
    if not m or m.get("uid") != uid:
        return web.json_response({"ok": False, "error": "Модель не найдена"}, status=404)
    m["notify_calc"] = uid
    return web.json_response({"ok": True})

async def _quick_calc_plastic():
    """Пластик быстрого расчёта — ВСЕГДА Elegoo PLA Matte (решение владельца, 2026-07-04).
    Ищем по имени среди активных; если катушку переименовали/выключили — дефолтный (лучше
    посчитать дефолтным, чем молча умереть), нет дефолтного — первый активный."""
    ps = await db.list_plastics(active_only=True)
    for p in ps:
        n = (p.name or "").lower()
        if "elegoo" in n and ("matte" in n or "матов" in n):
            return p
    return next((p for p in ps if p.is_default), ps[0] if ps else None)

async def _quick_calc_defaults():
    """Жёсткие дефолты быстрого расчёта (решения владельца 2026-07-04): принтер по умолчанию,
    Elegoo PLA Matte, СТАНДАРТНАЯ высота слоя (★-пресет качества), прочность — золотая середина
    списка. Один источник для быстрой сметы по ссылке в чате, MW-сметы и очереди досчёта
    (app/deferred.py, задания с quick=true): разъедутся дефолты — клиент получит из очереди
    одну цену, а из чата за ту же ссылку другую. None — пластики/пресеты не настроены."""
    printer = await db.get_default_printer()
    plastic = await _quick_calc_plastic()
    quals = await db.list_quality(active_only=True)
    strs = await db.list_strength(active_only=True)
    quality = next((q for q in quals if q.is_default), quals[0] if quals else None)
    strength = strs[len(strs) // 2] if strs else None
    if not (printer and plastic and quality and strength):
        return None
    return printer, plastic, quality, strength

async def _quick_link_download(url: str) -> tuple[list[str], str]:
    """Скачать модель по ссылке так же, как быстрый расчёт: у MakerWorld — вариант по
    умолчанию (решение владельца 2026-07-04: качаем default), иначе общий загрузчик.
    Общий для quick_link_quote и очереди досчёта: ссылка, принятая приёмной, пока ПК был
    выключен, должна скачаться ровно тем же путём, что и присланная при включённом."""
    mw = None
    try:                       # варианты MakerWorld (#14): для быстрого пути берём дефолтный
        mw = await downloader.fetch_makerworld_variants(url)
    except Exception:  # noqa: BLE001
        mw = None
    if mw and mw.get("variants"):
        iid = mw.get("default_iid") or (mw["variants"][0] or {}).get("id")
        meshes, label = await downloader.download_makerworld_instance(url, iid)
    elif mw and mw.get("single"):
        meshes, label = mw["single"]
    else:
        meshes, label = await downloader.fetch_from_url(url)
    return [str(p) for p in meshes], label

async def _fetch_mw_cover(cover_url: str, iid) -> str | None:
    """Обложка профиля MW → локальный файл: CDN площадок у владельца режется (27f25b91),
    поэтому картинку выкачиваем на сервере — ей отвечаем в чат и её же отдаёт /api/cpreview."""
    if not cover_url:
        return None
    dest = config.CACHE_DIR / "mwcover" / f"{iid}.jpg"
    if dest.exists():
        return str(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as sess, \
                sess.get(cover_url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            data = await r.read()
        if not (200 < len(data) <= _THUMB_MAX):
            return None
        dest.write_bytes(data)
        return str(dest)
    except Exception:  # noqa: BLE001
        log.debug("mw cover fetch failed", exc_info=True)
        return None

async def _quick_mw_quote(chat_id: int, uid: int, url: str, est: dict,
                          ack_msg_id: int | None) -> bool:
    """Быстрый расчёт из ДАННЫХ MakerWorld (задача владельца): у профиля печати MW уже есть
    вес (г) и время — цена считается мгновенно ручной формулой (как заказ за клиента), слайс
    не запускается вовсе. Ack-сообщение редактируется ценой сразу; затем фоном скачивается
    файл (нужен для заказа/пересчёта) и приходит обычная смета с кнопкой, помеченная approx —
    у админа на заказе будет штатная приписка «проверьте кнопкой Пересчитать по слайсеру».
    True — смета отправлена; False/сбой — вызывающий уходит на прежний путь со слайсом."""
    from aiogram.types import FSInputFile
    try:
        defaults = await _quick_calc_defaults()
        if defaults is None:
            return False
        printer, plastic, quality, strength = defaults
        settings = await db.get_settings()
        # Скидки — как в обычном расчёте (тираж=1, персональная). НЕ дубль общей
        # calcservice.effective_discount и не «забытый промокод»: промокод живёт только в памяти
        # токена расчёта мини-аппы (m['promo_code']), а быстрый расчёт по ссылке в чате токена
        # не имеет — брать его тут неоткуда. Тираж жёстко 1, потому что весь расчёт идёт qty=1.
        discount = max(await db.best_discount(1), await db.get_user_discount(uid))
        grams, minutes = float(est["weight_g"]), float(est["minutes"])
        bd = pricing.compute_price_manual(
            grams, minutes, plastic.price_per_gram * 1000.0, settings, qty=1,
            discount_pct=discount, failure_rate_pct=plastic.failure_rate_pct,
            volume_cm3_unit=(grams / plastic.density) if plastic.density else None)
        label = est.get("title") or downloader.label_from_url(url)
        note = texts.mw_instant_note(label, {
            "total": texts.fmt_money(bd.total, config.CURRENCY),
            "grams_total": f"{bd.grams_total:.1f}",
            "time_total": texts.fmt_minutes(bd.minutes_total)})
        if ack_msg_id:   # мгновенная цена вместо «обычно 1–3 минуты»
            try:
                await _bot.edit_message_text(note, chat_id=chat_id, message_id=ack_msg_id)
            except Exception:  # noqa: BLE001
                await _bot.send_message(chat_id, note)
        else:
            await _bot.send_message(chat_id, note)

        # файл всё равно нужен: заказ (paths/hash), «Повторить», пересчёт админа по слайсеру
        meshes, dl_label = await downloader.download_makerworld_instance(url, est["iid"])
        label = dl_label or label
        paths = [str(p) for p in meshes]
        info = await cpupool.run(inspect3mf.inspect_model, paths)
        token = await _register_model(paths, label, uid, url, info)
        m = _models[token]
        m["mw_est"] = est                       # кэш для пересчётов из мини-аппа

        color = (plastic.color_list or [""])[0]
        payload = await _mw_snapshot(token, m, est, uid=uid, printer=printer, plastic=plastic,
                                     quality=quality, strength=strength, color=color,
                                     colors_sel=[], qty=1, plates=info.plates)
        await _snap_calc(uid, "быстрый расчёт по ссылке",
                         f"{label} · {plastic.name} · MW-данные", source="mw", m=m,
                         payload=payload, printer=printer, plastic=plastic, quality=quality,
                         strength=strength, color=color, qty=1)

        caption = texts.calc_ready_note(label, payload)
        # Идея 3 (ux-paths-next.md): «Заказать · ~цена» прямо из чата + «Открыть смету».
        kb = keyboards.quote_actions(config.WEBAPP_PUBLIC_URL, token,
                                     order_text="⚡ Заказать · " + str(payload.get("total") or ""))
        pv = m.get("cpreview")
        if pv and Path(pv).exists():
            msg = await _bot.send_photo(chat_id, FSInputFile(pv), caption=caption, reply_markup=kb)
            m["mw_refine_msg"] = ("photo", chat_id, msg.message_id)
        else:
            msg = await _bot.send_message(chat_id, caption, reply_markup=kb)
            m["mw_refine_msg"] = ("text", chat_id, msg.message_id)
        # честный слайс фоном — обновит смету и caption сообщения точной ценой (97a91eee)
        m["calc_seq"] = seq = int(m.get("calc_seq") or 0) + 1
        # calc_snap_id здесь НЕ сбрасываем (в отличие от путей мини-аппа): снимок этой сметы
        # записан выше, и уточнять слайсу нужно именно его
        extra_colors = (max(0, int(info.num_colors or 1) - 1)
                        if info.is_multicolor and info.mixed_plates else 0)
        asyncio.create_task(_refine_calc(
            token, seq, uid=uid, printer=printer, plastic=plastic, quality=quality,
            strength=strength, color=color, colors_sel=[], qty=1,
            extra_colors=extra_colors, multicolor=bool(info.is_multicolor), promo_pct=0.0))
        return True
    except Exception:  # noqa: BLE001
        log.warning("MW instant quote failed for %s — обычный путь со слайсом", url, exc_info=True)
        return False

async def quick_link_quote(chat_id: int, uid: int, url: str, ack_msg_id: int | None = None) -> None:
    """Быстрый расчёт по ссылке, кинутой прямо в чат бота (без мастера). Дефолты — решения
    владельца (2026-07-04), без вариаций: ПЕРВЫЙ вариант модели (= основная), пластик Elegoo
    PLA Matte, СТАНДАРТНАЯ высота слоя (★-пресет качества), 1 шт, прочность — золотая середина
    списка пресетов. Результат: рендер + цена + кнопка «Открыть смету». Токен модели
    регистрируется как при загрузке из мини-апки, поэтому кнопка открывает ПОЛНОЦЕННУЮ смету
    (deep-link go=quote&t=…, ae4c6c13): там можно сменить пластик/цвет/масштаб и сразу оформить
    заказ. Ack «начал расчёт» шлёт хендлер до запуска этой корутины — она бежит фоном.

    Для MakerWorld с данными профиля (вес+время) слайс не запускается вовсе — цена приходит
    мгновенно из этих данных (_quick_mw_quote); сюда возвращаемся только при сбое MW-пути."""
    from aiogram.types import FSInputFile
    if _bot is None:
        return
    try:
        est = None
        try:                       # быстрый расчёт из данных MW: вес+время уже посчитаны
            est = await downloader.fetch_makerworld_estimate(url)
        except Exception:  # noqa: BLE001
            est = None
        if est and await _quick_mw_quote(chat_id, uid, url, est, ack_msg_id):
            return
        paths, label = await _quick_link_download(url)
        info = await cpupool.run(inspect3mf.inspect_model, paths)
        token = await _register_model(paths, label, uid, url, info)
        m = _models[token]

        # стандартная высота слоя = ★-пресет качества; прочность = золотая середина списка
        defaults = await _quick_calc_defaults()
        if defaults is None:
            raise calcservice.CalcError("не настроены пластики или пресеты печати")
        printer, plastic, quality, strength = defaults
        color = (plastic.color_list or [""])[0]
        extra_colors = (max(0, int(info.num_colors or 1) - 1)
                        if info.is_multicolor and info.mixed_plates else 0)

        res = await calcservice.compute(
            user_id=uid, model_paths=paths, model_label=label, printer=printer, plastic=plastic,
            color=color, qty=1, quality=quality, strength=strength, extra_colors=extra_colors,
            plates=info.plates, is_multicolor=bool(info.is_multicolor))
        await _remember_calc_cache(m, res, plastic, printer, quality, strength,
                                   plates_only=None, plate_plastics=None)
        bd = res["bd"]
        settings = res["settings"]
        m["cpreview"] = res["preview_path"]
        # тот же снимок, что кладёт _api_calc_inner: кнопка «Оформить» на смете идёт через
        # _place_one/last, PDF — через pdf; быстрый путь = один пластик, все столы, масштаб 100.
        m["last"] = {"plastic_name": res["pdisp"], "printer": printer.name, "qty": 1,
                     "quality": quality.label, "strength": strength.label,
                     "dims": res.get("dims", ""), "grams_total": bd.grams_total,
                     "minutes_total": bd.minutes_total, "total": bd.total,
                     "plastic_id": plastic.id, "quality_id": quality.id, "strength_id": strength.id,
                     "color": color, "color_grams": res.get("color_grams") or [],
                     "plastic_grams": [], "scale_pct": 100,
                     "volume_cm3_unit": (bd.grams_unit / plastic.density) if plastic.density else 0.0,
                     "minutes_unit": bd.minutes_unit, "extra_colors": bd.extra_colors,
                     "mixed": False, "approx": False}
        m["pdf"] = {"bd": bd, "settings": settings, "model_label": label,
                    "plastic_name": res["pdisp"], "printer_name": printer.name, "qty": 1,
                    "quality_label": quality.label, "strength_label": strength.label,
                    "plates": info.plates, "preview_path": res["preview_path"]}
        payload = _result_payload(bd, settings, config.CURRENCY, info.plates, res["cached"])
        payload["cpreview"] = f"/api/cpreview/{token}"
        payload["dims"] = res.get("dims", "")
        payload["scale_pct"] = 100
        m["dfm_meas"] = res.get("dfm")   # B2 (DFM): замер для вердикта + для requote со сметы
        _apply_dfm(payload, dims=res.get("dims", ""), printer=printer,
                   meas=res.get("dfm"), settings=settings)
        payload["queue"] = await db.queue_load(_live_progress())
        payload["eta"] = db.eta_date(payload["queue"].get("minutes", 0) + bd.minutes_total)
        m["last_payload"] = payload   # смета по токену — deep-link go=quote&t=… (ae4c6c13)
        await _snap_calc(uid, "быстрый расчёт по ссылке", f"{label} · {plastic.name}",
                         source="link", m=m, payload=payload, bd=bd, printer=printer,
                         plastic=plastic, quality=quality, strength=strength, color=color,
                         qty=1, preview=res.get("preview_path") or "")

        caption = texts.calc_ready_note(label, payload)
        # Идея 3 (ux-paths-next.md): «Заказать · ~цена» прямо из чата + «Открыть смету».
        kb = keyboards.quote_actions(config.WEBAPP_PUBLIC_URL, token,
                                     order_text="⚡ Заказать · " + texts.fmt_money(bd.total, config.CURRENCY))
        pv = res.get("preview_path")
        if pv and Path(pv).exists():
            await _bot.send_photo(chat_id, FSInputFile(pv), caption=caption, reply_markup=kb)
        else:
            await _bot.send_message(chat_id, caption, reply_markup=kb)
    except downloader.DownloadError as exc:
        await _quick_calc_fail(chat_id, f"Не удалось скачать модель: {exc}")
    except calcservice.CalcError as exc:
        await _quick_calc_fail(chat_id, f"Не удалось посчитать: {exc}")
    except Exception:  # noqa: BLE001
        log.exception("quick link quote failed for %s", url)
        await _quick_calc_fail(chat_id, "Что-то пошло не так. Попробуйте через мастер расчёта "
                                        "(«Рассчитать стоимость» в меню).")

async def _quick_calc_fail(chat_id: int, msg: str) -> None:
    if _bot is None:
        return
    try:
        await _bot.send_message(chat_id, f"⚠️ {texts.esc(msg)}")
    except Exception:  # noqa: BLE001
        log.debug("quick calc fail notify failed", exc_info=True)

async def _api_quote_state(request: web.Request) -> web.Response:
    """Смета по токену — для deep-link go=quote&t=… из уведомления «расчёт готов» (ae4c6c13)."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    m = _models.get(request.query.get("token", ""))
    if not m or m.get("uid") != uid or not m.get("last_payload"):
        return web.json_response({"ok": False, "error": "Расчёт устарел — посчитайте заново"})
    tok = request.query.get("token", "")
    return web.json_response({"ok": True,
        "model": {"token": tok, "label": m.get("label") or "Модель",
                  "plates": m.get("plates", 1), "multicolor": int(m.get("multicolor") or 0),
                  "num_colors": int(m.get("num_colors") or 1), "preview": "",
                  "mc_colors": m.get("mc_colors") or [],
                  # варианты набора (87eb9cf3): смета из deep-link тоже даёт переключатель
                  "file_variants": [{"key": v["key"], "label": v["label"], "files": len(v["paths"])}
                                    for v in (m.get("file_variants") or [])],
                  "variant_sel": m.get("variant_sel") or "",
                  # B1 (3D-просмотр): смета из deep-link «расчёт готов» тоже предлагает 3D
                  "model_url": f"/api/modelfile/{tok}", **mesh3d._model3d_meta(m.get("paths") or [])},
        "result": m["last_payload"]})

async def _api_requote(request: web.Request) -> web.Response:
    """Быстрый пересчёт под другой пластик БЕЗ переслайса (OpenTodo 050aff56): объём и время
    берём из последнего слайса (они зависят от профиля печати, не от пластика), цена — той же
    формулой pricing.compute_price. Это приближение (минуты считались с профилем исходного
    пластика) — расчёт честно помечается approx, при оформлении заказа клиент и мастер увидят
    пометку «быстрый расчёт», а у мастера в карточке уже есть «Пересчитать по слайсеру»."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"requote:{uid}", 40, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    m = _models.get(body.get("token", ""))
    if not m or m.get("uid") != uid or not m.get("last"):
        return web.json_response({"ok": False, "expired": True,
                                  "error": "Сначала сделайте расчёт"}, status=404)
    last = m["last"]
    if last.get("mixed"):
        return web.json_response({"ok": False, "error": "У модели свои пластики на столах — "
                                                        "меняйте их через «Параметры»"})
    vol, minutes = float(last.get("volume_cm3_unit") or 0), float(last.get("minutes_unit") or 0)
    if vol <= 0 or minutes <= 0:
        return web.json_response({"ok": False, "error": "Пересчитайте модель заново"})
    plastic = await db.get_plastic(_toint(body.get("plastic_id")))
    if not plastic or not plastic.is_active:
        return web.json_response({"ok": False, "error": "Пластик недоступен"})
    qty = max(1, min(1000, _toint(body.get("qty"), int(last.get("qty") or 1))))
    settings = await db.get_settings()
    promo_pct = (await db.validate_promo(m.get("promo_code"))) if m.get("promo_code") else None
    discount = await calcservice.effective_discount(uid, qty, promo_pct)
    # Срочность (B3): множитель прошлого расчёта этого токена сохраняем и при быстрой смене пластика —
    # иначе срочная наценка молча слетела бы при requote (это тоже путь построения цены).
    urgency_factor = float(m.get("urgency_factor", 1.0) or 1.0)
    bd = pricing.compute_price(vol, minutes, qty, plastic, settings, discount_pct=discount,
                               extra_colors=int(last.get("extra_colors") or 0),
                               urgency_factor=urgency_factor)
    # B4: опции постобработки прошлого расчёта этого токена сохраняем и при быстрой смене пластика —
    # иначе услуги молча слетели бы при requote (это тоже путь построения цены).
    pricing.apply_options(bd, m.get("options"), _m_nparts(m))
    # выбранный цвет (шарики на смете, fb552c3a); чужой/пустой → первый из палитры пластика
    color = (body.get("color") or "").strip()
    if color and color not in (plastic.color_list or []):
        color = ""
    if not color:
        color = (plastic.color_list or [""])[0]
    pdisp = f"{plastic.name} · {color}" if color else plastic.name
    # обновляем снимок расчёта: оформление заказа пойдёт уже с новым пластиком; разбивки по
    # цветам/пластикам прежнего расчёта больше не верны — склад спишется общим объёмом
    last.update({"plastic_name": pdisp, "qty": qty, "grams_total": bd.grams_total,
                 "minutes_total": bd.minutes_total, "total": bd.total, "plastic_id": plastic.id,
                 "color": color, "color_grams": [], "plastic_grams": [], "approx": True})
    if m.get("pdf"):
        m["pdf"].update({"bd": bd, "plastic_name": pdisp, "qty": qty})
    # живая перекраска превью под выбранный цвет (OpenTodo 0c67414b): тот же рендер, что в
    # полном расчёте; мультиплейт-коллажи не трогаем (пер-стольные цвета меняются в «Параметрах»).
    # ?cv= — cache-bust, иначе WebView не перезагрузит картинку по прежнему URL.
    cprev = None
    if int(m.get("plates", 1) or 1) <= 1 and m.get("paths"):
        try:
            overrides = await db.get_color_overrides()
            rgbs = materials.color_to_rgbs(color, overrides) if color else None
            cp = await cpupool.run_render(preview.render_preview,
                                          [Path(p) for p in m["paths"]], rgbs or None,
                                          bed=await _bed_of_model(m))
            if cp and Path(cp).exists():
                m["cpreview"] = str(cp)
                if m.get("pdf"):
                    m["pdf"]["preview_path"] = str(cp)
                cprev = f"/api/cpreview/{body.get('token')}?cv={int(time.time())}"
        except Exception:  # noqa: BLE001
            log.debug("requote recolor preview failed", exc_info=True)
    payload = _result_payload(bd, settings, config.CURRENCY,
                              len(m.get("plates_sel") or []) or m.get("plates", 1), cached=True)
    payload["cpreview"] = cprev or f"/api/cpreview/{body.get('token')}"
    payload["dims"] = last.get("dims", "")
    payload["queue"] = await db.queue_load(_live_progress())
    payload["eta"] = db.eta_date(payload["queue"].get("minutes", 0) + bd.minutes_total)
    payload["urgency"] = m.get("urgency") or "normal"               # B3: срочность прежнего расчёта
    # B3: срок по времени печати заказа (bd.minutes_total) — см. правило в db.promised_date.
    payload["term"] = db.promised_date(bd.minutes_total, m.get("urgency"), settings)
    payload["approx"] = True
    payload["plastic_id"] = plastic.id
    # OpenTodo ae038968: «умножаем на коэффициент» из задачи владельца, только честнее — граммы
    # уже пересчитаны по ПЛОТНОСТИ нового пластика (тот же объём, что дал прошлый слайс), а не
    # просто домножены. Время слайса профилем СТАРОГО пластика могло чуть отличаться от нового
    # (разные скорости печати материала) — честный слайс фоном уточняет обе цифры молча, как у
    # MW-пути/calc_cache (_refine_calc); без квалити/принтера/файлов — остаёмся на approx.
    quality = await db.get_quality(_toint(last.get("quality_id")))
    strength = await db.get_strength(_toint(last.get("strength_id")))
    printer = next((p for p in await db.list_printers(active_only=False)
                    if p.name == last.get("printer")), None) or await db.get_default_printer()
    # B2 (DFM): смена пластика геометрию не меняет — берём кэшированный замер модели (m['dfm_meas'])
    # и тот же стол принтера. Фоновый слайс (_refine_calc ниже) всё равно пересчитает вердикт заново.
    _apply_dfm(payload, dims=last.get("dims", ""), printer=printer,
               meas=m.get("dfm_meas"), settings=settings)
    if quality and strength and printer and m.get("paths"):
        payload["refining"] = True
        m["last_payload"] = payload
        m["calc_seq"] = seq = int(m.get("calc_seq") or 0) + 1
        # снимок ЭТОЙ сметы ещё не записан (его пишет _snap_calc ниже): фоновый слайс,
        # успевший финишировать раньше, не должен дописать уточнение в ПРОШЛЫЙ снимок
        m["calc_snap_id"] = None
        extra_colors = int(last.get("extra_colors") or 0)
        asyncio.create_task(_refine_calc(
            body.get("token", ""), seq, uid=uid, printer=printer, plastic=plastic,
            quality=quality, strength=strength, color=color, colors_sel=last.get("colors_sel") or [],
            qty=qty, extra_colors=extra_colors, multicolor=bool(extra_colors), promo_pct=promo_pct or 0.0,
            plates_only=m.get("plates_sel") or None, scale_pct=int(last.get("scale_pct") or 100),
            urgency_factor=urgency_factor))
    else:
        m["last_payload"] = payload   # смета по токену остаётся актуальной (ae4c6c13)
    await _snap_calc(uid, "сменил пластик в расчёте", f'{m["label"]} → {plastic.name}',
                     source="requote", m=m, payload=payload, bd=bd, printer=printer,
                     plastic=plastic, quality=quality, strength=strength, color=color, qty=qty)
    return web.json_response({"ok": True, "result": payload})

async def _api_colors_sel(request: web.Request) -> web.Response:
    """Синхронизация ВЫБРАННЫХ ЦВЕТОВ мультицвета с серверным снимком расчёта (OpenTodo
    2f7b597f). Слоты «Цвета печати» на смете красят 3D мгновенно и ЛОКАЛЬНО, но заказ
    строится из серверного m["last"] (_place_one) — без этого вызова смена цвета жила
    только на экране, пока клиент не пересчитает через «изменить параметры» (ровно та
    жалоба). Цена от ИМЁН цветов не зависит (число разных цветов меняет продувку — тогда
    фронт делает полный /api/calc), поэтому здесь только снимок: color/colors_sel/
    plastic_name/pdf + перерисовка цветного превью по слотам (оно уедет в заказ картинкой)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"colorsel:{uid}", 60, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    m = _models.get(body.get("token", ""))
    if not m or m.get("uid") != uid or not m.get("last"):
        return web.json_response({"ok": False, "expired": True,
                                  "error": "Сначала сделайте расчёт"}, status=404)
    last = m["last"]
    plastic = await db.get_plastic(_toint(last.get("plastic_id")))
    if not plastic:
        return web.json_response({"ok": False, "error": "Пересчитайте модель заново"})
    raw = body.get("colors_sel") if isinstance(body.get("colors_sel"), list) else []
    # та же валидация, что у /api/calc: чужие/пустые имена отбрасываем (позиции слотов на
    # карточке всегда заполнены целиком — applyModel предвыбирает каждый слот)
    sel = [c for c in (str(x or "").strip() for x in raw[:8]) if c and c in (plastic.color_list or [])]
    if not sel:
        return web.json_response({"ok": False, "error": "Выберите цвета из палитры"})
    color = " - ".join(sel)
    pdisp = f"{plastic.name} · {color}"
    last.update({"color": color, "colors_sel": sel, "plastic_name": pdisp})
    if m.get("pdf"):
        m["pdf"]["plastic_name"] = pdisp
    # живое превью по выбранным слотам — та же картинка уедет в заказ (OpenTodo 18ddd6c8)
    cprev = None
    if m.get("paths"):
        try:
            overrides = await db.get_color_overrides()
            rgbs = [materials.color_to_rgb(c, overrides) or (0.6, 0.64, 0.68) for c in sel]
            cp = await cpupool.run_render(preview.render_multicolor_preview,
                                          [Path(p) for p in m["paths"]], rgbs,
                                          bed=await _bed_of_model(m))
            if cp and Path(cp).exists():
                m["cpreview"] = str(cp)
                if m.get("pdf"):
                    m["pdf"]["preview_path"] = str(cp)
                cprev = f"/api/cpreview/{body.get('token')}?cv={int(time.time())}"
        except Exception:  # noqa: BLE001
            log.debug("colors_sel recolor preview failed", exc_info=True)
    await db.log_event(uid, "сменил цвета в расчёте", f'{m["label"]} → {color}')
    return web.json_response({"ok": True, "cpreview": cprev})

async def _api_order(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    m = _models.get(body.get("token", ""))
    if not m or not m.get("last"):
        return web.json_response({"ok": False, "error": "Сначала сделайте расчёт"})
    # Опции постобработки (B4-инстант): на смете услуги переключаются ЛОКАЛЬНО (плоская цена,
    # без переслайса/спиннера), поэтому серверный снимок m["options"]/m["last"]["total"] мог
    # отстать от актуального выбора. Клиент присылает выбор в теле заказа — переприкладываем его
    # к уже посчитанной bd БЕЗ переслайса: apply_options идемпотентна (снимает прошлый набор и
    # кладёт новый), итог/разбивка/тег услуг снова сходятся. Меняем options и цену ТОЛЬКО вместе
    # (при наличии живого снимка pdf), иначе не трогаем — старый клиент без поля работает как раньше.
    if "options" in body:
        _pdf = m.get("pdf") or {}
        _bd = _pdf.get("bd")
        if _bd is not None:
            m["options"] = await _resolve_options(body.get("options"))
            pricing.apply_options(_bd, m["options"], _m_nparts(m))
            m["last"]["total"] = _bd.total
    # комментарий-пожелание клиента (d929c054): попадает мастеру в карточку заказа
    rid = await _place_one(u, m, comment=(body.get("comment") or "").strip()[:500])
    if rid is None:
        return web.json_response({"ok": False, "error": "Сначала сделайте расчёт"})
    # Доставка по умолчанию из профиля (A2): у заказа в один тап шага «куда везти» нет.
    await _apply_default_delivery(int(u["id"]), rid, float(m["last"].get("total") or 0))
    approx = bool(m["last"].get("approx"))   # быстрый пересчёт без переслайса (050aff56)
    await _notify_new_order(u, m, rid, approx=approx)
    return web.json_response({"ok": True})

async def _api_order_unsliced(request: web.Request) -> web.Response:
    """Заказ БЕЗ сметы (OpenTodo 55a1cf8f): клиент нажал «Отправить мастеру на оценку» после
    неудачного /api/calc. Цена = 0, статус 'new', unsliced=1 — мастер видит файл модели
    (кнопки «Открыть в слайсере»/«Открыть папку» в карточке заказа уже работают от file_path)
    и назначает цену инструментом «Изменить цену» → «По граммам и времени» (calcservice.
    reprice_request, OpenTodo 8c1cab08) — тот же путь подтверждения клиентом, что и у обычной
    смены цены. Требует контекст последнего неудачного /api/calc этого токена (m["unsliced_ctx"]) —
    без него понятно, какие принтер/пластик/качество/прочность/тираж вписывать в заказ, неоткуда."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    m = _models.get(body.get("token", ""))
    if not m or m.get("uid") != int(u["id"]):
        return web.json_response({"ok": False, "expired": True,
                                  "error": "Модель не найдена — загрузите заново"}, status=404)
    ctx = m.pop("unsliced_ctx", None)
    if not ctx:
        return web.json_response({"ok": False, "error": "Сначала попробуйте посчитать стоимость"})
    printer, plastic, quality, strength = ctx["printer"], ctx["plastic"], ctx["quality"], ctx["strength"]
    color = ctx.get("color") or ""
    qty = max(1, min(1000, int(ctx.get("qty") or 1)))
    pdisp = f"{plastic.name} · {color}" if color else plastic.name
    uname = ("@" + u["username"]) if u.get("username") else \
        (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {u['id']}")
    wish = (body.get("comment") or "").strip()[:500]
    note = (f"⚠️ Автоматический расчёт не удался: {ctx.get('error') or 'ошибка слайсера'}\n\n"
            "Цену назначит мастер после осмотра модели.")
    rid = await db.add_request(
        user_id=int(u["id"]), user_name=uname,
        model_label=m.get("order_label") or m["label"], printer=printer.name,
        plastic_name=pdisp, quantity=qty, quality=quality.label, strength=strength.label,
        grams_total=0.0, minutes_total=0.0, price=0.0,
        preview_path=(_memo_preview(m, _bed_of_printer(printer)) or m.get("cpreview")
                      or await _embedded_thumb(m.get("paths"))),
        multicolor=int(m.get("multicolor", 0)), plates=int(m.get("plates", 1)),
        hash=cache.model_hash(m["paths"]), src_url=m.get("src_url", ""),
        file_path=(m["paths"][0] if m.get("paths") else ""),
        quality_id=quality.id, strength_id=strength.id,
        # Сметы нет, но масштаб клиента — в снимок: мастер видит его в карточке, а пересчёт и
        # допечатка слайсят в нём. Снимок из одного масштаба сметой не притворяется
        # (pricing.breakdown_from_dict → None).
        breakdown={"scale_pct": int(ctx.get("scale_pct") or 100)},
        description=note + (f"\n\n💬 {wish}" if wish else ""), unsliced=1)
    # B4: опции сохраняем даже без цены — мастер учтёт ручную работу при осмотре модели.
    try:
        await db.set_request_options(rid, m.get("options"))
    except Exception:  # noqa: BLE001
        log.debug("set request options (unsliced) failed", exc_info=True)
    await db.log_event(int(u["id"]), "оставил заказ без расчёта (слайс не удался)",
                       f'#{rid} «{m["label"]}»', user_name=uname)
    # Доставка по умолчанию (A2). Цены ещё нет — порог «бесплатно от суммы» посчитается
    # по нулю (то есть доставка платная); как только мастер назначит цену, он увидит
    # доставку в карточке и сможет пересчитать её кнопкой по тарифу.
    await _apply_default_delivery(int(u["id"]), rid, 0.0)
    if _bot is not None:
        try:
            r = await db.get_request(rid)
            from aiogram.types import FSInputFile
            pv = m.get("preview") or m.get("cpreview")
            admin_text = texts.admin_new_order(r, config.CURRENCY) + "\n\n" + texts.UNSLICED_ADMIN_NOTE
            await notify.notify_admins(
                _bot, admin_text,
                photo=FSInputFile(pv) if pv and Path(pv).exists() else None,
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                                text="Открыть заказ в приложении"))
            await _bot.send_message(int(u["id"]),
                                    texts.UNSLICED_CLIENT_NOTE + await _delivery_note(rid))
        except Exception:  # noqa: BLE001
            log.debug("unsliced order notify failed", exc_info=True)
    return web.json_response({"ok": True})

# Части выбора, которые /api/calc берёт из тела запроса помимо принтера/пластика/пресетов/
# тиража/цвета (m["calc_choice"] хранит их в том же виде, что тело).
_CHOICE_KEYS = ("multicolor", "colors", "colors_sel", "plates", "plate_plastics", "plate_colors",
                "scale", "options", "promo")


async def _order_now_choice(body: dict, m: dict, plastic) -> dict:
    """Выбор заказа order_now целиком — тем же разбором, что у /api/calc (review:e0#0).

    Источник — тело «Оставить заказ» (новый фронт шлёт весь выбор, с которым ушёл расчёт), а
    чего в нём нет (старый закэшированный фронт шлёт только принтер/пластик/пресеты/тираж/цвет/
    многоцвет/слоты) — выбор ИДУЩЕГО расчёта этого токена (m["calc_choice"]). Без него живой
    хвост проставил бы цену многоцвета/выбранных столов, а очередь досчёта после рестарта —
    цену всей модели одним пластиком."""
    src = {**(m.get("calc_choice") or {}), **{k: body[k] for k in _CHOICE_KEYS if k in body}}
    nplates = int(m.get("plates", 1) or 1)
    req_colors = _toint(src.get("colors"), 0)
    multicolor, _extra = _multicolor_choice(bool(m.get("multicolor")), m.get("num_colors"),
                                            m.get("mixed_plates", True), bool(src.get("multicolor")),
                                            req_colors)
    plate_plastics = await _resolve_plate_plastics(src.get("plate_plastics"), nplates, plastic)
    if src.get("scale") is not None:
        scale = max(10, min(1000, _toint(src.get("scale"), 100)))
    else:   # масштаб идущего расчёта; расчёта в этом процессе не было — не выдумываем
        scale = int(m["calc_scale_pct"]) if m.get("calc_scale_pct") else None
    if "promo" in src:
        code = str(src.get("promo") or "").strip().upper()
        promo = code if code and await db.validate_promo(code) else ""
    else:
        promo = m.get("promo_code") or ""
    return {"multicolor": multicolor, "colors": req_colors,
            "colors_sel": _pick_colors_sel(src.get("colors_sel"), plastic) if multicolor else [],
            "plates_sel": _parse_plates_sel(src.get("plates"), nplates),
            "plate_plastics": plate_plastics,
            "plate_colors": _valid_plate_colors(src.get("plate_colors"), nplates, plastic, plate_plastics),
            "scale": scale, "promo": promo,
            "options": (await _resolve_options(src["options"]) if "options" in src
                        else (m.get("options") or []))}


async def _api_order_now(request: web.Request) -> web.Response:
    """Заказ ПРЯМО ВО ВРЕМЯ слайса (OpenTodo f1e80d67): клиент нажал «Оставить заказ», пока
    слайсер ещё считает. Заказ создаётся МГНОВЕННО (не ждём слайс) из живого выбора клиента —
    мастер видит заявку сразу. Если расчёт этого токена уже успел долететь — оформляем обычный
    заказ с ценой. Иначе — заявка без цены (unsliced=1), а идущий фоном /api/calc сам проставит
    цену через _finalize_pending_order, когда досчитается (слайс упал — заказ остаётся у мастера
    на ручную оценку, как order_unsliced)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    m = _models.get(body.get("token", ""))
    if not m or m.get("uid") != int(u["id"]):
        return web.json_response({"ok": False, "expired": True,
                                  "error": "Модель не найдена — загрузите заново"}, status=404)
    # Идемпотентность (двойной тап / гонка двух запросов): один токен = один заказ order_now.
    # Повторный вызов возвращает уже созданный заказ, а не плодит дубль (и двойной резерв склада/
    # погашение промо в priced-ветке). Проверка+установка `_order_now_busy` синхронны (без await
    # между ними) — второй одновременный запрос гарантированно увидит флаг и не создаст второй заказ.
    if m.get("ordered_now"):
        return web.json_response({"ok": True, "order_id": m["ordered_now"],
                                  "priced": bool(m.get("ordered_now_priced")), "dedup": True})
    if m.get("_order_now_busy"):
        return web.json_response({"ok": False, "error": "Заказ уже оформляется — секунду."})
    m["_order_now_busy"] = True
    try:
        comment = (body.get("comment") or "").strip()[:500]
        # Срочность (B3): заказ во время слайса несёт свой выбор срочности. Обновляем m["urgency"]
        # из тела (если пришло) — приоритетный путь _place_one прочитает его; неценовая ветка ниже
        # пишет срочность и обещанный срок прямо в заявку.
        if "urgency" in body:
            m["urgency"] = "rush" if str(body.get("urgency") or "").strip().lower() == "rush" else "normal"
        # Расчёт уже долетел (успел между показом слайса и тапом) → обычный заказ с ценой.
        if m.get("last"):
            rid = await _place_one(u, m, comment=comment)
            if rid is not None:
                m.pop("pending_order_id", None)
                m["ordered_now"] = rid
                m["ordered_now_priced"] = True
                await _notify_new_order(u, m, rid, approx=bool(m["last"].get("approx")))
                return web.json_response({"ok": True, "order_id": rid, "priced": True})
        # Иначе — мгновенная заявка без цены из выбора клиента. Досчёт цены — фоном (calc-хвост).
        printer = (await db.get_printer(_toint(body.get("printer_id")))) if body.get("printer_id") \
            else await db.get_default_printer()
        plastic = await db.get_plastic(_toint(body.get("plastic_id")))
        quality = await db.get_quality(_toint(body.get("quality_id")))
        strength = await db.get_strength(_toint(body.get("strength_id")))
        if not (printer and plastic and quality and strength):
            return web.json_response({"ok": False, "error": "Проверьте выбор параметров"})
        color = (body.get("color") or "").strip()
        qty = max(1, min(1000, _toint(body.get("qty"), 1)))
        # Весь выбор, а не только пластик/пресеты (review:e0#0): заказ досчитает очередь, если
        # живой расчёт до него не доживёт, — и считать она должна ровно то, что считается сейчас.
        ch = await _order_now_choice(body, m, plastic)
        if ch["colors_sel"]:
            color = " - ".join(ch["colors_sel"])
        nplates = int(m.get("plates", 1) or 1)
        pdisp = ((_plates_material_label(plastic, color, ch["plate_plastics"], ch["plate_colors"],
                                         ch["plates_sel"] or list(range(nplates)))
                  if nplates > 1 else None)
                 or (f"{plastic.name} · {color}" if color else plastic.name))
        label = _order_label(m, ch["plates_sel"], ch["scale"]) or m["label"]
        uname = ("@" + u["username"]) if u.get("username") else \
            (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {u['id']}")
        note = ("⏳ Заказ оформлен во время расчёта — цена появится, как только слайсер досчитает "
                "(или назначит мастер).")
        # Срочность (B3): фиксируем выбор и обещанный срок сразу — досчёт цены (calc-хвост) на них
        # не влияет, а мастер видит флаг «срочно» ещё до появления цены.
        _urg = "rush" if (m.get("urgency") == "rush") else ""
        # Цена ещё не досчитана (минуты неизвестны, в заявку уходит minutes_total=0.0) → срок считаем
        # от 0 мин: «срочно» всё равно завтра, «обычно» с нулём минут — тоже завтра (короткий заказ).
        _promised = db.promised_date(0.0, "rush" if _urg == "rush" else "normal", await db.get_settings())
        rid = await db.add_request(
            user_id=int(u["id"]), user_name=uname,
            model_label=label, printer=printer.name,
            plastic_name=pdisp, quantity=qty, quality=quality.label, strength=strength.label,
            grams_total=0.0, minutes_total=0.0, price=0.0,
            preview_path=(m.get("cpreview") or _memo_preview(m, _bed_of_printer(printer))
                          or await _embedded_thumb(m.get("paths"))),
            # столов — выбранные (как у оформления с готовой сметой), иначе все модельные
            multicolor=int(m.get("multicolor", 0)),
            plates=int(len(ch["plates_sel"]) if ch["plates_sel"] else m.get("plates", 1)),
            hash=cache.model_hash(m["paths"]), src_url=m.get("src_url", ""),
            file_path=(m["paths"][0] if m.get("paths") else ""),
            quality_id=quality.id, strength_id=strength.id,
            # масштаб идущего слайса — сразу в снимок (как у order_unsliced): если слайс упадёт,
            # другого следа выбора клиента не останется; досчёт перепишет снимок полностью
            breakdown=({"scale_pct": int(ch["scale"])} if ch["scale"] else None),
            description=note + (f"\n\n💬 {comment}" if comment else ""), unsliced=1,
            urgency=_urg, promised_date=_promised, price_pending=1, commit=False)
        # Очередь досчёта (Э0): связь «заказ ↔ досчёт» больше не только в памяти токена. Заказ и
        # задание — ОДНИМ commit-ом (enqueue_deferred коммитит и строку заказа выше). Задание
        # придержано на время живого расчёта (deferred.LIVE_HOLD_SEC): цену сейчас досчитывает
        # он, и второй такой же тяжёлый расчёт параллельно ни к чему. Процесс перезапустится до
        # хвоста — при старте сборщик снимет придержку и досчитает сам (клиенту придёт цена на
        # подтверждение). Параметры — идентификаторами: имена пластиков/пресетов переименуют.
        await db.enqueue_deferred(
            rid, source=deferred.pc_model_source(m["paths"], label),
            params=deferred.params_from_choice(
                printer=printer, plastic=plastic, quality=quality, strength=strength, color=color,
                qty=qty, scale_pct=ch["scale"], urgency=m.get("urgency"),
                options=ch["options"], promo=ch["promo"], comment=comment,
                multicolor=ch["multicolor"], colors=ch["colors"], colors_sel=ch["colors_sel"],
                plates_sel=ch["plates_sel"], plate_plastics=ch["plate_plastics"],
                plate_colors=ch["plate_colors"],
                model={"multicolor": bool(m.get("multicolor")), "num_colors": m.get("num_colors"),
                       "mixed_plates": m.get("mixed_plates", True), "plates": nplates}),
            client_req_id=f"now:{rid}", delay_sec=deferred.LIVE_HOLD_SEC)
        # B4: заказ во время слайса — опции уже в m (выбраны на расчёте); линкуем сразу, досчёт
        # цены (_finalize_pending_order) снимок разбивки с bd.options положит отдельно.
        try:
            await db.set_request_options(rid, ch["options"])
        except Exception:  # noqa: BLE001
            log.debug("set request options (order_now) failed", exc_info=True)
        m["pending_order_id"] = rid
        m["ordered_now"] = rid            # заказ создан — повторный order_now вернёт его, не дубль
        await db.log_event(int(u["id"]), "оставил заказ во время расчёта", f'#{rid} «{m["label"]}»',
                           user_name=uname)
        # Доставка по умолчанию (A2). Цены ещё нет — стоимость пересчитается по порогу
        # «бесплатно от суммы» в _finalize_pending_order, когда слайсер досчитает.
        await _apply_default_delivery(int(u["id"]), rid, 0.0)
        # Гонка с calc-хвостом: расчёт мог долететь и САМ финализировать заявку, пока мы писали id
        # и лог. Если m["last"] уже есть — цена либо будет проставлена нами (мы первыми сняли id),
        # либо уже проставлена calc-хвостом (id снят им). В обоих случаях «в обработке» НЕ шлём.
        if m.get("last"):
            if m.pop("pending_order_id", None) == rid:
                await _finalize_pending_order(rid, m, int(u["id"]))
            m["ordered_now_priced"] = True
            return web.json_response({"ok": True, "order_id": rid, "priced": True})
        # Заявка действительно ждёт расчёта — уведомляем клиента/мастера «в обработке».
        if _bot is not None:
            try:
                r = await db.get_request(rid)
                from aiogram.types import FSInputFile
                pv = m.get("cpreview") or m.get("preview")
                admin_text = (texts.admin_new_order(r, config.CURRENCY)
                              + "\n\n⏳ Оформлен во время расчёта — цена досчитается автоматически "
                                "(если слайс не пройдёт — оцените вручную).")
                await notify.notify_admins(
                    _bot, admin_text,
                    photo=FSInputFile(pv) if pv and Path(pv).exists() else None,
                    reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                                    text="Открыть заказ в приложении"))
                await _bot.send_message(
                    int(u["id"]), "🧾 Заказ принят! Считаю цену — пришлю, как только слайсер досчитает."
                    + await _delivery_note(rid))
            except Exception:  # noqa: BLE001
                log.debug("order_now notify failed", exc_info=True)
        return web.json_response({"ok": True, "order_id": rid, "priced": False})
    finally:
        m["_order_now_busy"] = False

async def _notify_new_order(u: dict, m: dict, rid: int, approx: bool = False) -> None:
    """Уведомления по только что созданному ПОЛНОМУ заказу (цена есть): мастеру карточка с фото,
    клиенту — подтверждение. Общий хвост _api_order и _api_order_now (ветка «расчёт уже готов»)."""
    if _bot is None:
        return
    try:
        r = await db.get_request(rid)
        from aiogram.types import FSInputFile
        pv = m.get("cpreview")
        admin_text = texts.admin_new_order(r, config.CURRENCY)
        if approx:
            admin_text += ("\n\n⚡ Расчёт быстрый (смена пластика без переслайса) — "
                           "проверьте кнопкой «Пересчитать по слайсеру» в карточке.")
        await notify.notify_admins(
            _bot, admin_text,
            photo=FSInputFile(pv) if pv and Path(pv).exists() else None,
            # Новый заказ — событие операционное, оно нужно и оператору. Но текст денежный,
            # поэтому оператору уходит вариант без цены (роли не обходим через Telegram).
            audience=notify.ALL_STAFF, operator_text=texts.admin_new_order_operator(r),
            reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                            text="Открыть заказ в приложении"))
        client_text = (texts.ORDER_PLACED + (("\n\n" + texts.FAST_QUOTE_NOTE) if approx else "")
                       + await _delivery_note(rid))
        await _bot.send_message(int(u["id"]), client_text)
    except Exception:  # noqa: BLE001
        log.debug("web order notify failed", exc_info=True)

async def place_order_from_chat(user: dict, token: str) -> tuple[bool, str]:
    """Оформить заказ прямо из чата по кнопке под быстрой сметой (ux-paths-next.md, идея 3) —
    без открытия мини-аппа. user — {id, username, first_name, last_name} из Telegram.
    Идемпотентно: повторный тап по той же модели не плодит заказы. Возвращает (ок, текст ответа).
    Уведомления мастеру/клиенту шлёт _notify_new_order (как обычный веб-заказ)."""
    m = _models.get(token)
    if not m or int(m.get("uid") or 0) != int(user["id"]) or not m.get("last"):
        return False, "Расчёт устарел — пришлите ссылку заново, и я посчитаю снова."
    if m.get("ordered_rid"):
        return True, texts.ORDER_PLACED     # уже оформлен (двойной тап) — не плодим заказ
    rid = await _place_one(user, m)
    if rid is None:
        return False, "Расчёт устарел — пришлите ссылку заново, и я посчитаю снова."
    m["ordered_rid"] = rid
    # Доставка по умолчанию из профиля (A2) — в чате спросить адрес тем более негде.
    await _apply_default_delivery(int(user["id"]), rid, float(m["last"].get("total") or 0))
    await _notify_new_order(user, m, rid, approx=bool(m["last"].get("approx")))
    return True, texts.ORDER_PLACED

async def _finalize_pending_order(rid: int, m: dict, uid: int) -> None:
    """Заказ, оформленный во время слайса (order_now), досчитался: проставляем реальную цену/
    граммы/время + план резерва склада, снимаем флаг «без сметы» и уведомляем клиента и мастера.
    Слайс упал — сюда не заходим: задание очереди досчёта сборщик попробует сам (и при неудаче
    отдаст заказ мастеру с алертом).

    Применяем ТОЛЬКО если цену заказа ещё никто не решил (db.apply_live_price): его могла
    досчитать очередь (после рестарта клиенту уже ушло «подтвердите цену»), назвать мастер,
    пересчитать клиент в «Изменить», или он отменён — тогда живой хвост не пишет ничего и не
    шлёт второе сообщение. Мастер, взявший заказ в работу / отложивший его, НЕ дожидаясь цены,
    цену не решал — такому заказу цена пишется, как до Э0 (review:regress#0).

    Запись — одна store-функция: цена, граммы, время, снимок, план склада и отмена задания
    очереди — одним условным UPDATE и одним commit-ом (review:e0#2). Упали до commit — заказ
    всё ещё ждёт ПК, а его задание созревает сразу: досчитает сборщик."""
    last = m.get("last")
    if not last:
        return
    pdf = m.get("pdf") or {}
    breakdown = (pricing.breakdown_to_dict(pdf["bd"], pdf["settings"])
                 if pdf.get("bd") is not None and pdf.get("settings") is not None else {})
    # Масштаб пишем и без снимка цены: иначе снимок без масштаба стёр бы масштаб, положенный в
    # заявку при оформлении (фактический, после подгонки под стол — из last).
    breakdown["scale_pct"] = int(last.get("scale_pct", 100) or 100)
    try:
        # preview_path обновляем ТОЛЬКО если есть чем: заказ во время расчёта уже создан с
        # превью, а к финалу цветного (cpreview) могло и не появиться — флекси-осьминог.
        # Пустой cpreview затёр бы уже показанную клиенту картинку → «заказ без картинки»
        # (OpenTodo c6d9cebc). Цепочка отката — цветной → серый → встроенная в .3mf миниатюра.
        # Всё, что требует await, — ДО записи: между захватом и записью не должно быть окна.
        pv = (m.get("cpreview") or _memo_preview(m, await _bed_of_model(m))
              or await _embedded_thumb(m.get("paths")))
        applied = await db.apply_live_price(rid, {
            "price": last["total"], "grams_total": last["grams_total"],
            "minutes_total": last["minutes_total"], "breakdown": breakdown,
            "plastic_name": last["plastic_name"], "quality": last["quality"],
            "strength": last["strength"], "quality_id": last.get("quality_id"),
            "strength_id": last.get("strength_id"), "dims": last.get("dims", ""),
            "preview_path": pv or None, "stock_plan": _order_stock_plan(last) or None})
    except Exception:  # noqa: BLE001
        log.warning("finalize pending order #%s: запись цены не удалась — заказ досчитает очередь",
                    rid, exc_info=True)
        # ничего не записано: заказ всё ещё ждёт ПК — задание созревает сейчас, а не через
        # придержку (живого расчёта для этого заказа больше не будет)
        await deferred.release_live(rid)
        return
    if not applied:
        log.info("finalize pending order #%s: цену заказа уже решили — живой хвост ничего не пишет", rid)
        return
    # Доставка (A2): цена наконец известна — пересчитываем стоимость под порог «бесплатно
    # от суммы». Вызов идемпотентный: если шапки нет, ничего не создастся сверх умолчаний.
    await _apply_default_delivery(uid, rid, float(last.get("total") or 0))
    if _bot is not None:
        try:
            cur = config.CURRENCY
            r = await db.get_request(rid)
            taken = r is not None and r.status != "new"   # мастер уже взял, не дождавшись цены
            await notify.notify_user(
                _bot, uid, texts.live_price_ready(rid, last["total"], cur, taken=taken)
                + await _delivery_note(rid),
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders"))
            await notify.notify_admins(
                _bot, f"💰 Заказ #{rid} (оформлен во время расчёта) досчитан: "
                      f"{texts.fmt_money(last['total'], cur)} · {last.get('grams_total', 0):.0f} г.",
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                                text="Открыть заказ"))
        except Exception:  # noqa: BLE001
            log.debug("finalize pending order notify failed", exc_info=True)

async def _api_order_cart(request: web.Request) -> web.Response:
    """Оформление корзины ОДНИМ заказом (B5): список токенов → по заявке requests на каждый с общим
    order_group + строка-сущность order_groups (шапка: доставка/комментарий/снимок итога). requests
    остаётся источником истины по позиции (статусы/журнал/очередь пер-позиционны); группа лишь
    сворачивает выдачу в клиентском и мастерском списках. Уведомление админам — ОДНА сводка."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    tokens = body.get("tokens") or []
    if not isinstance(tokens, list) or not tokens:
        return web.json_response({"ok": False, "error": "Корзина пуста"})
    batch = bool(body.get("batch"))   # клиент попросил собрать партией на один стол (idea #14)
    # Доставка (A2): способ (pickup/delivery) + адрес + тип доставки. Стоимость СЧИТАЕТ
    # СЕРВЕР по выбранному типу (delivery.type_cost) — цену от клиента не принимаем; итог
    # заказа = сумма позиций + доставка (см. ниже). Тип и адрес не прислали — берём умолчания
    # клиента из профиля (он мог задать их один раз и не повторять на каждом заказе).
    dmethod = (body.get("delivery_method") or "").strip().lower()
    if dmethod not in ("pickup", "delivery"):
        dmethod = ""
    daddr = (body.get("delivery_address") or "").strip()[:300] if dmethod == "delivery" else ""
    dtype = None
    if dmethod == "delivery":
        tid = _toint(body.get("delivery_type_id"))
        if not tid:                                    # клиент не выбрал → умолчание из профиля
            tid = int((await db.get_user_prefs(int(u["id"]))).get("delivery_type_id") or 0)
        if tid:
            t = await db.get_delivery_type(tid)
            dtype = t if (t and int(t["is_active"] or 0)) else None   # выключенный тип игнорируем
    gcomment = (body.get("comment") or "").strip()[:500]
    # Заметка мастеру на каждую позицию партии: совпадающие материал+цвет печатать одним столом дешевле.
    note = ("🧩 Печать партией: собрать позиции с одинаковым материалом и цветом на один стол "
            "(меньше подготовки) и пересчитать дешевле." if batch else "")
    # Опции постобработки на позициях корзины (B4-инстант): выбор мог переключаться локально на
    # смете, поэтому клиент присылает карту token→[id услуг]; переприкладываем на каждый токен ДО
    # оформления (apply_options идемпотентна). Нет карты / нет токена в ней → позицию не трогаем.
    opts_map = body.get("opts") if isinstance(body.get("opts"), dict) else {}
    group = uuid.uuid4().hex[:12]
    placed = []
    for tok in tokens[:20]:
        m = _models.get(tok)
        if m and m.get("uid") == int(u["id"]) and m.get("last"):
            if tok in opts_map:
                _pdf = m.get("pdf") or {}
                _bd = _pdf.get("bd")
                if _bd is not None:
                    m["options"] = await _resolve_options(opts_map.get(tok))
                    pricing.apply_options(_bd, m["options"], _m_nparts(m))
                    m["last"]["total"] = _bd.total
            rid = await _place_one(u, m, order_group=group, comment=note)
            if rid:
                placed.append(rid)
    if not placed:
        return web.json_response({"ok": False, "expired": True,
                                  "error": "Позиции корзины устарели — посчитайте заново"})
    # Позиции для сводки/итога + снимок суммы. Итог ДЕРИВИРУЕМ из цен позиций (не из клиента).
    rows = [await db.get_request(rid) for rid in placed]
    items = [{"label": r.model_label, "qty": r.quantity, "price": r.price} for r in rows if r]
    total = round(sum(float(r.price or 0) for r in rows if r), 2)
    # Доставка считается ПОСЛЕ позиций: порог «бесплатно от суммы» смотрит на итог по позициям.
    dcost = delivery.type_cost(dtype, total)
    try:
        await db.create_order_group(group, int(u["id"]), comment=gcomment,
                                    delivery_method=dmethod, delivery_address=daddr,
                                    total_snapshot=round(total + dcost, 2), batch=int(batch),
                                    delivery_type_id=(int(dtype["id"]) if dtype else None),
                                    delivery_type_name=((dtype or {}).get("name") or ""),
                                    delivery_cost=dcost)
    except Exception:  # noqa: BLE001
        log.debug("create order group failed", exc_info=True)   # шапки нет → свёртка на fallback
    if _bot is not None:
        try:
            # ОДНА сводка админам (а не N уведомлений _notify_new_order per-позицию).
            await notify.notify_admins(
                _bot, texts.admin_cart_order(
                    first_name=u.get("first_name", ""), gid=group, count=len(placed),
                    total=total, items=items, cur=config.CURRENCY, batch=batch,
                    delivery_method=dmethod, delivery_address=daddr, comment=gcomment,
                    delivery_cost=dcost, delivery_type=((dtype or {}).get("name") or "")),
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder",
                                                oid=placed[0], text="Открыть заказ в приложении"))
            await _bot.send_message(
                int(u["id"]),
                texts.ORDER_PLACED + texts.user_cart_order_note(
                    count=len(placed), total=total, cur=config.CURRENCY,
                    delivery_method=dmethod, delivery_address=daddr,
                    delivery_cost=dcost, delivery_type=((dtype or {}).get("name") or "")))
        except Exception:  # noqa: BLE001
            log.debug("cart order notify failed", exc_info=True)
    await db.clear_user_cart(int(u["id"]))   # оформлено → серверная копия корзины не нужна (#10)
    return web.json_response({"ok": True, "count": len(placed), "group": group})

async def _api_cart_sync(request: web.Request) -> web.Response:
    """Серверная копия корзины для нуджа о брошенной корзине (idea #10)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False}, status=400)
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    items = body.get("items") if isinstance(body.get("items"), list) else []
    await db.save_user_cart(uid, items[:50], str(body.get("total") or ""))
    return web.json_response({"ok": True})

def _order_json(r, pay_on: bool = False) -> dict:
    """pay_on — онлайн-оплата настроена (payments.enabled()); флаг считает вызывающий один
    раз на запрос, а не по разу на заказ (это поход в text_settings)."""
    from ..db import REQ_CANCELLABLE, REQ_STATUS_LABELS
    cur = config.CURRENCY
    unsliced = int(getattr(r, "unsliced", 0) or 0)
    # Слайс не удался при оформлении (OpenTodo 55a1cf8f): пока мастер не назначил цену (заказ
    # ещё «new» и по нему нет ни цены, ни ожидающей подтверждения) — «0,00 ₽» вводило бы клиента
    # в заблуждение. Как только цена появится (клиент пересчитал сам через «Изменить» или мастер
    # прислал цену на подтверждение) — показываем её как обычно.
    unsliced_wait = unsliced and r.status == "new" and (r.price or 0) <= 0
    # Цену считает ПК сам (очередь досчёта, Э0): это не «оценит мастер» — клиент ждёт сообщения
    # от бота, а не мастера. Только в «Новом»: ушёл из него — флаг смысла не имеет.
    pending_calc = bool(int(getattr(r, "price_pending", 0) or 0)) and r.status == "new"
    return {"id": r.id, "label": r.model_label, "plastic": r.plastic_name, "qty": r.quantity,
            "group": (getattr(r, "order_group", "") or ""),   # непустой = позиция заказа-корзины (B5)
            "status": r.status, "status_label": REQ_STATUS_LABELS.get(r.status, r.status),
            "price": (texts.PRICE_PENDING_SHORT if pending_calc
                      else "Оценит мастер" if unsliced_wait else texts.fmt_money(r.price, cur)),
            "price_pending": pending_calc,
            "price_note": texts.PRICE_PENDING_NOTE if pending_calc else "",
            "pending": texts.fmt_money(r.pending_price, cur) if r.pending_price is not None else None,
            # сумма к подтверждению в копейках — кнопка «Принять» шлёт её обратно (expected),
            # и сервер не примет цену, которую клиент не видел (её успели поменять)
            "pending_minor": (payments.amount_minor(r.pending_price)
                              if r.pending_price is not None else None),
            # первая цена заказа (досчитал ПК / назвал мастер) — не «изменил цену»: было 0
            "first_price": (r.price or 0) <= 0,
            "comment": getattr(r, "price_comment", None) or "",
            "dims": getattr(r, "dims", "") or "",
            "mc": int(getattr(r, "multicolor", 0) or 0), "plates": int(getattr(r, "plates", 1) or 1),
            "grams": f"{r.grams_total:.0f} г", "time": texts.fmt_minutes(r.minutes_total),
            "preview": (signed_url("req", r.id, {"v": _file_ver(r.preview_path)})
                        if r.preview_path and Path(r.preview_path).exists() else ""),
            "needs_response": r.status in ("price_changed", "invoiced") and r.pending_price is not None,
            "cancellable": r.status in REQ_CANCELLABLE,
            # онлайн-оплата (A1): бейдж «оплачено» и кнопка «Оплатить» в карточке заказа
            "paid": bool(int(getattr(r, "paid", 0) or 0)),
            "payable": payments.is_payable(r, pay_on),
            "due": getattr(r, "due_date", "") or "",
            "custom": int(getattr(r, "is_custom", 0) or 0),
            "unsliced": unsliced,
            "description": getattr(r, "description", "") or "",
            "dev_fee": float(getattr(r, "dev_fee", 0.0) or 0.0),
            "slicer_shot": _slicer_url(r),
            "timelapse": _timelapse_url(r),
            "breakdown": texts.order_breakdown(r, cur),
            "repeatable": bool(getattr(r, "hash", "")),
            "quality_id": getattr(r, "quality_id", None), "strength_id": getattr(r, "strength_id", None),
            "editable": (r.status == "new" and not int(getattr(r, "is_custom", 0) or 0)
                         and bool(getattr(r, "file_path", "") and Path(r.file_path).exists()))}

async def _api_orders(request: web.Request) -> web.Response:
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    return web.json_response({"ok": True, "orders": await build_user_orders(uid)})


async def build_user_orders(uid: int, limit: int = 40) -> list:
    """«Мои заказы» клиента uid (тело /api/orders) — карточки, свёрнутые корзины, трекер.

    Вынесено из ручки для снимка приёмной (Э2, app/relaylink.py): пока ПК выключен, мини-апп
    на домене показывает список из снимка — тот же самый, что отдала бы эта ручка."""
    items = await db.list_user_requests(uid, limit=limit)
    # ждущие реакции клиента — наверх (стабильная сортировка сохраняет порядок id DESC):
    # сперва подтверждение цены, следом неоплаченные «Ждёт оплаты» (фидбэк 31.07)
    def _prio(r):
        if r.status == "price_changed" and r.pending_price is not None:
            return 0
        if r.status == "awaiting_payment" and not int(getattr(r, "paid", 0) or 0):
            return 1
        return 2
    items.sort(key=_prio)
    from ..db import REQ_STATUS_LABELS
    qmap = {it["id"]: it for it in await db.queue_items()}   # позиция в очереди печати (d79b54bb)
    umap = await db.orders_unread_user(uid)                  # непрочитанные ответы админа (9d222a19)
    # статус-трекер «как у доставки» (ux-simplification.md, идея 5): заказ, который печатается
    # прямо сейчас, — по привязке печати (печать видели активной и финиш ещё не зафиксирован)
    # печатается прямо сейчас — по привязке ЛЮБОЙ машины: при одновременной печати «текущая
    # привязка» одной из них скрыла бы трекер у заказа, который печатается на второй
    printing_rids = {b["request_id"] for b in await db.list_print_bindings()
                     if b.get("was_active") and not b.get("finished")}
    cur = config.CURRENCY
    pay_on = await payments.enabled()   # один поход в настройки на весь список (A1)
    out = []
    groups: dict[str, dict] = {}   # gid → карточка-группа, держит позицию первой позиции группы
    for r in items:
        j = _order_json(r, pay_on=pay_on)
        # таймлапсы и скрины стола всех печатей заказа (многостольные заказы печатаются в
        # несколько заходов); одиночный r.timelapse — копия журнального видео — гасим как дубль
        prints, tl_sizes = await _order_prints_media(r.id)
        j["prints"] = prints
        if j.get("timelapse") and _file_size(getattr(r, "timelapse", "")) in tl_sizes:
            j["timelapse"] = ""
        hist = await db.get_status_history(r.id)
        j["timeline"] = [{"label": REQ_STATUS_LABELS.get(s, s),
                          "at": time.strftime("%d.%m %H:%M", time.localtime(at))} for s, at in hist]
        qi = qmap.get(r.id)
        if qi:
            j["qpos"] = qi["pos"]
            j["qtotal"] = len(qmap)
            j["qeta"] = db.eta_date(qi["done_minutes"])
        j["unread"] = umap.get(r.id, 0)
        j["printing"] = r.id in printing_rids
        # Свёртка заказа-корзины (B5): позиции с ОДНИМ НЕПУСТЫМ order_group идут в одну карточку.
        # Фильтр gid != '' обязателен — иначе NULL-группа (одиночные заказы) схлопнулась бы в одну.
        gid = (getattr(r, "order_group", "") or "").strip()
        if gid:
            g = groups.get(gid)
            if g is None:
                g = {"group": True, "gid": gid, "items": [], "total_raw": 0.0,
                     "preview": j.get("preview") or "", "created_at": r.created_at}
                groups[gid] = g
                out.append(g)                     # позиция группы = место первой её позиции
            g["items"].append(j)
            g["total_raw"] += float(r.price or 0)
        else:
            out.append(j)
    # Группа из ОДНОЙ позиции — это не «корзина», а обычный заказ (так выглядит и одиночный
    # заказ с доставкой из умолчаний, и корзина, из которой оформили одну модель). Разворачиваем
    # её обратно в простую карточку и переносим доставку с трек-номером прямо в позицию —
    # иначе клиент видел бы «🧺 Заказ · 1 позиция» с задвоенными ценой, превью и статусом.
    for gid, g in list(groups.items()):
        if len(g["items"]) != 1:
            continue
        item = g["items"][0]
        ent = await db.get_order_group(gid)
        dcost = delivery.group_delivery_cost(ent)
        item["group"] = ""                    # маркер «позиция корзины» снимаем — она одна
        item["delivery_method"] = (ent or {}).get("delivery_method") or ""
        item["delivery_address"] = (ent or {}).get("delivery_address") or ""
        item["delivery_type"] = (ent or {}).get("delivery_type_name") or ""
        item["delivery_cost"] = texts.fmt_money(dcost, cur) if dcost > 0 else ""
        item["items_total"] = item.get("price") if dcost > 0 else ""
        if dcost > 0:      # total_raw группы из одной позиции = цена самой позиции
            item["total_with_delivery"] = texts.fmt_money(
                round(float(g.get("total_raw") or 0) + dcost, 2), cur)
        item["track"] = delivery.group_track(ent)
        out[out.index(g)] = item
        groups.pop(gid, None)
    # Дозаполняем карточки-группы: общий итог (сумма позиций — авторитет), доставка/комментарий из
    # шапки order_groups (может ОТСУТСТВОВАТЬ у старых cart-заказов → fallback: пусто), смешанные
    # статусы rollup'ом (не единым лживым), непрочитанное — сумма по позициям.
    for g in groups.values():
        ent = await db.get_order_group(g["gid"])
        g["count"] = len(g["items"])
        # Итог группы (A2) = позиции + доставка. items_total показываем отдельной строкой,
        # только когда доставка платная — иначе клиент видит одну сумму, как и раньше.
        items_total = round(g["total_raw"], 2)
        dcost = delivery.group_delivery_cost(ent)
        g["total"] = texts.fmt_money(round(items_total + dcost, 2), cur)
        g["items_total"] = texts.fmt_money(items_total, cur) if dcost > 0 else ""
        g["delivery_cost"] = texts.fmt_money(dcost, cur) if dcost > 0 else ""
        g.pop("total_raw", None)
        g["delivery_method"] = (ent or {}).get("delivery_method") or ""
        g["delivery_address"] = (ent or {}).get("delivery_address") or ""
        g["delivery_type"] = (ent or {}).get("delivery_type_name") or ""
        g["track"] = delivery.group_track(ent)      # отправление: перевозчик, номер, ссылка
        g["comment"] = (ent or {}).get("comment") or ""
        g["unread"] = sum(int(it.get("unread") or 0) for it in g["items"])
        seen, statuses = set(), []
        for it in g["items"]:
            if it["status"] not in seen:
                seen.add(it["status"])
                statuses.append({"status": it["status"], "label": it["status_label"]})
        g["statuses"] = statuses
    return out

async def _api_order_respond(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    rid = _toint(body.get("order_id"))
    r = await db.get_request(rid)
    if not r or r.user_id != uid:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if r.status not in ("price_changed", "invoiced") or r.pending_price is None:
        return web.json_response({"ok": False, "error": "Нечего подтверждать"})
    is_invoice = r.status == "invoiced"        # счёт по индивидуальному заказу vs смена цены
    accept = bool(body.get("accept"))
    # expected — сумма в копейках, которую клиент видел на кнопке (pending_minor карточки).
    # Цену успели поменять (мастер назвал другую, ПК пересчитал) — не принимаем молча, а
    # отдаём актуальную: фронт перерисует карточку. Без поля — как раньше (старый фронт).
    if accept and body.get("expected") is not None:
        exp = _toint(body.get("expected"), -1)
        if exp != payments.amount_minor(r.pending_price):
            return web.json_response({"ok": False, "stale": True,
                                      "pending": texts.fmt_money(r.pending_price, config.CURRENCY),
                                      "pending_minor": payments.amount_minor(r.pending_price),
                                      "error": texts.price_stale_alert(r.pending_price, config.CURRENCY)})
    if accept:
        # Принятая сумма при включённой оплате идёт этапом «Ждёт оплаты» (фидбэк 31.07):
        # клиенту тут же уходит инвойс, оплата сама переведёт «В работе». Без онлайн-оплаты —
        # прежнее поведение (сразу в работу, деньги при получении).
        pay_on = (await payments.enabled()
                  and payments.amount_minor(r.pending_price) >= payments.MIN_AMOUNT_MINOR)
        new_status = "awaiting_payment" if pay_on else "in_progress"
        await db.update_request(rid, price=r.pending_price, pending_price=None, status=new_status)
        # Согласованная цена стала боевой (счёт по индивидуальному заказу / новая цена от
        # мастера) → пересчитываем порог «бесплатно от суммы» ДО send_invoice ниже (волна 5).
        await delivery.recalc_group_delivery(rid)
        what = "счёт" if is_invoice else "новую цену"
        msg = (f"✅ Клиент принял {what} по заказу #{rid} "
               f"({texts.fmt_money(r.pending_price, config.CURRENCY)})."
               + (" Заказ ждёт оплаты — счёт отправлен." if pay_on else ""))
    else:
        await db.update_request(rid, pending_price=None, status="rejected")
        what = "счёта" if is_invoice else "новой цены"
        msg = f"❌ Клиент отказался от {what} по заказу #{rid}."
    if _bot is not None:
        try:
            await notify.notify_admins(_bot, msg)
        except Exception:  # noqa: BLE001
            log.debug("order respond notify failed", exc_info=True)
    await db.log_event(uid, "принял цену" if accept else "отклонил цену", f"#{rid}")
    # Сумма согласована → сразу счёт на оплату в чат (A1). Best-effort: выключенная оплата
    # или отказ провайдера не должны ломать само принятие цены.
    if accept and _bot is not None:
        try:
            await payments.send_invoice(_bot, await db.get_request(rid))
        except Exception:  # noqa: BLE001
            log.debug("post-accept invoice failed", exc_info=True)
    return web.json_response({"ok": True})

async def _api_order_pay_link(request: web.Request) -> web.Response:
    """Ссылка-счёт Telegram Payments для мини-аппа (A1): фронт открывает её через
    WebApp.openInvoice — клиент платит, не выходя из приложения. Сам факт оплаты придёт
    боту отдельным successful_payment (handlers/payments.py)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    r = await db.get_request(_toint(body.get("order_id")))
    if not r or r.user_id != uid:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if not await payments.enabled():
        return web.json_response({"ok": False, "error": "Онлайн-оплата не настроена"})
    if int(getattr(r, "paid", 0) or 0):
        return web.json_response({"ok": False, "error": "Заказ уже оплачен"})
    if _bot is None:
        return web.json_response({"ok": False, "error": "Бот недоступен"})
    link = await payments.create_link(_bot, r)
    if not link:
        return web.json_response(
            {"ok": False, "error": "Не удалось создать счёт — попробуйте позже "
                                   "или напишите мастеру"})
    return web.json_response({"ok": True, "link": link})

async def _api_order_cancel(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    r = await db.get_request(_toint(body.get("order_id")))
    if not r or r.user_id != uid:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    from ..db import REQ_CANCELLABLE
    if r.status not in REQ_CANCELLABLE:
        return web.json_response({"ok": False, "error": "Этот заказ уже нельзя отменить"})
    await db.update_request(r.id, status="cancelled", pending_price=None)
    if _bot is not None:
        try:
            # оплаченный заказ отменять можно, но мастер должен увидеть, что деньги уже
            # получены: возврат делается руками в кабинете провайдера (A1)
            paid_note = ("\n⚠️ Заказ был ОПЛАЧЕН — не забудьте оформить возврат "
                         "в кабинете платёжного провайдера."
                         if int(getattr(r, "paid", 0) or 0) else "")
            await notify.notify_admins(
                _bot, f"🗑 Клиент отменил заказ #{r.id} «{texts.esc(r.model_label)}».{paid_note}")
        except Exception:  # noqa: BLE001
            log.debug("cancel notify failed", exc_info=True)
    await db.log_event(uid, "отменил заказ", f"#{r.id} «{r.model_label}»")
    return web.json_response({"ok": True})

async def _api_order_edit(request: web.Request) -> web.Response:
    """Клиент меняет параметры заказа (пластик/цвет/качество/прочность/тираж) ДО взятия в
    работу (OpenTodo 42ce4724). Пересчитывает и обновляет существующий заказ + уведомляет мастера."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"calc:{uid}", config.RATE_CALCS_PER_MIN, 60):
        return web.json_response({"ok": False, "error": "Слишком много пересчётов — подождите минуту"})
    r = await db.get_request(_toint(body.get("order_id")))
    if not r or r.user_id != uid:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if int(getattr(r, "is_custom", 0) or 0) or r.status != "new":
        return web.json_response({"ok": False, "error": "Этот заказ уже нельзя изменить"})
    try:
        out = await calcservice.recompute_request(
            r, plastic_id=_toint(body.get("plastic_id")), quality_id=_toint(body.get("quality_id")),
            strength_id=_toint(body.get("strength_id")), qty=_toint(body.get("qty"), 1),
            color=(body.get("color") or ""))
    except calcservice.CalcError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    # Пока шёл пересчёт, заказ мог уйти: очередь досчёта (Э0) прислала цену на подтверждение,
    # мастер назвал свою. Писать поверх нельзя — клиент «принял» бы цену, посчитанную по старому
    # выбору, при новых параметрах в карточке. Проверка «ещё new» и запись — ОДИН условный
    # UPDATE (review:e0#3): отдельное чтение перед записью оставляло окно для результата
    # сборщика. Тем же commit-ом снимается ожидание ПК и отменяется задание очереди — цену
    # назвал сам клиент, иначе сборщик позже переписал бы выбранное им.
    bd = out["bd"]
    if not await db.apply_client_edit(
            r.id, plastic_name=out["pdisp"], quality=out["quality"].label,
            strength=out["strength"].label, quantity=out["qty"], quality_id=out["quality"].id,
            strength_id=out["strength"].id, grams_total=bd.grams_total,
            minutes_total=bd.minutes_total, price=bd.total, dims=out["dims"],
            preview_path=out["preview_path"], breakdown=pricing.breakdown_to_dict(bd, out["settings"])):
        return web.json_response({"ok": False, "error": "Заказ уже изменился — обновите список"})
    await db.log_event(uid, "изменил заказ", f'#{r.id} → {out["pdisp"]} ×{out["qty"]}')
    if _bot is not None:
        try:
            await notify.notify_admins(
                _bot, f"✏️ Клиент изменил параметры заказа #{r.id} «{texts.esc(r.model_label)}»: "
                      f"{texts.esc(out['pdisp'])}, ×{out['qty']} — новая цена "
                      f"{texts.fmt_money(bd.total, config.CURRENCY)}.",
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=r.id,
                                                text="Открыть заказ в приложении"))
        except Exception:  # noqa: BLE001
            log.debug("order edit notify failed", exc_info=True)
    return web.json_response({"ok": True, "price": texts.fmt_money(bd.total, config.CURRENCY)})

async def _api_order_messages(request: web.Request) -> web.Response:
    """Тред заказа для клиента (9d222a19): только свой заказ; помечаем прочитанным."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    r = await db.get_request(_toint(request.query.get("id")))
    if not r or r.user_id != uid:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    rows = await db.list_order_messages(r.id)
    await db.mark_order_read(r.id, by_admin=False)
    return web.json_response({"ok": True, "label": r.model_label,
                              "messages": _msgs_json(rows, mine_is_admin=False)})

async def _api_order_message(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    r = await db.get_request(_toint(body.get("order_id")))
    if not r or r.user_id != uid:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    text = (body.get("text") or "").strip()[:2000]
    image = body.get("image") or ""
    if not text and not image:
        return web.json_response({"ok": False, "error": "Пустое сообщение"})
    # Вложение = загрузка файла от клиента, поэтому частоту ограничиваем: на текстовые
    # сообщения лимита нет и не нужно, а фото без него позволяет забить диск мастерской.
    if image and not ratelimit.allow(f"chatphoto:{uid}", 10, 300):
        return web.json_response({"ok": False,
                                  "error": "Слишком много фото подряд — попробуйте через минуту"})
    mid = await db.add_order_message(r.id, from_admin=False, text=text)
    photo_path = await ordermedia._save_chat_image(mid, image) if image else ""
    if _bot is not None:
        # Текст уведомления собирает app/orderchat.py — тем же кодом, которым разбирает цитату,
        # когда мастер отвечает на него реплаем прямо в Telegram. Соберёте строку здесь руками —
        # ответ мастера перестанет находить свой заказ, и узнается это не в тестах, а у клиента.
        await orderchat.notify_staff(_bot, orderchat.Target(r.id, uid, False),
                                     r.user_name or str(uid), text, photo_path, msg_id=mid)
    await db.log_event(uid, "написал в чат", f"#{r.id}: {text[:80]}")
    rows = await db.list_order_messages(r.id)
    return web.json_response({"ok": True, "messages": _msgs_json(rows, mine_is_admin=False)})

def _saved_model_json(sm) -> dict:
    """Карточка сохранённой модели пользователя (форма для /api/mymodels)."""
    return {"id": sm.id, "label": sm.label,
            "mc": int(getattr(sm, "multicolor", 0) or 0), "plates": int(getattr(sm, "plates", 1) or 1),
            "preview": (signed_url("sm", sm.id, {"v": _file_ver(sm.preview_path)})
                        if sm.preview_path and Path(sm.preview_path).exists() else "")}

async def _api_mymodels(request: web.Request) -> web.Response:
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    items = await db.list_saved_models(uid, limit=40)
    return web.json_response({"ok": True, "models": [_saved_model_json(sm) for sm in items]})

async def _saved_preview(request: web.Request) -> web.StreamResponse:
    sid = request.match_info.get("sid", "")
    if not check_sig(request, "sm", sid):
        raise web.HTTPNotFound()
    sm = await db.get_saved_model(_toint(sid))
    if sm and sm.preview_path and Path(sm.preview_path).exists():
        served = await _maybe_webp(request, Path(sm.preview_path))
        return web.FileResponse(served, headers=_img_headers(request, private=True))
    raise web.HTTPNotFound()

def _presel(sm) -> dict:
    """Параметры из сохранённой модели для предвыбора в конфиге (true-повтор заказа)."""
    return {"plastic_id": sm.plastic_id, "quality_id": sm.quality_id,
            "strength_id": sm.strength_id, "qty": sm.quantity or 1}

async def _api_use(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    kind, oid = body.get("kind"), _toint(body.get("id"))
    src, presel = None, None
    if kind == "saved":
        sm = await db.get_saved_model(oid)
        if sm:
            src, presel = (sm.paths, sm.label), _presel(sm)
    elif kind == "collection":
        it = await db.get_collection_item(oid)
        src = (it.paths, it.label) if it else None
    elif kind == "order":   # «Повторить заказ»: заказ → модель пользователя по хэшу + те же параметры
        req = await db.get_request(oid)
        sm = (await db.get_saved_model_by_hash(uid, req.hash)
              if req and req.user_id == uid and req.hash else None)
        if sm:
            src, presel = (sm.paths, sm.label), _presel(sm)
    if not src:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    # B7 (источник лида): kind ручки и есть происхождение модели будущего заказа
    origin = {"saved": "mine", "collection": "catalog", "order": "repeat"}.get(kind, "")
    res = await _make_token(uid, src[0], src[1], presel=presel, origin=origin)
    if not res:
        return web.json_response({"ok": False, "error": "Файлы модели недоступны"})
    kinds = {"saved": "открыл свою модель", "collection": "взял из коллекции", "order": "повторил заказ"}
    await db.log_event(uid, kinds.get(kind, "открыл модель"), str(src[1] or ""))
    return web.json_response(res)
