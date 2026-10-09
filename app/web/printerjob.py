"""Печать как работа: статус машины, сетевой старт, привязка печати к заказу, история.

Один предмет: жизненный цикл ПЕЧАТИ ЗАКАЗА на машине — живой статус с «богатой шапкой» и
привязкой, старт файла (в т.ч. с картой слотов), привязка/отвязка текущей печати, история
заданий и прикрепление выбранной печати к заказу (таймлапс + запись журнала + скрин стола).

Выделено из app/webapp.py волной 11 endgame-плана (после волн 1a, 2, 4 и 10: медиа заказа —
в ordermedia, драйвер и указатель активной печати — в printerhub, кадры/скрин — в
printerlive, качалка таймлапсов — в printmedia).

ДВА СОЗНАТЕЛЬНЫХ отличия от дословного переноса, оба — про подмены в тестах:
  * ссылки на printmedia (`_TIMELAPSE_DIR`, `cached_timelapse_path`, `_download_timelapse`,
    `_deliver_timelapse`) квалифицированы модулем: связывание по значению заморозило бы
    объект на момент импорта, и подмена каталога таймлапсов в тестах молча промахивалась бы —
    тесты писали бы в БОЕВОЙ data/timelapses;
  * `copy_journal_photo_to_order` — тем же правилом через printerlive.
Остальные тела перенесены ДОСЛОВНО. Помощники printerhub связаны по значению — подмены на
них в тестах ставятся во ВСЕ модули (образец tests/test_capability_gates.py).

Регистрация маршрутов осталась в make_app строками на фасадные имена.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from aiohttp import web

from .. import config, db, elegoo, notify, printmedia, reprint, texts
from ..webauth import _admin_body, _admin_forbidden, _verify_admin
from ..websign import _file_ver
from . import printerlive
from .common import _ago, _bad, _photo_url, _toint, signed_url
from .ordermedia import _timelapse_url
from .printerext import _clean_slot_map
from .printerhub import (
    _active_print_header,
    _active_print_key,
    _driver_printer_id,
    _no_printer,
    _printer_driver,
    _printer_ep,
    _printer_id_arg,
    _set_active_print,
)

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev

async def _api_admin_printer_status(request: web.Request) -> web.Response:
    """Живой статус принтера по его протоколу (c5f8ec5f; драйверы — 0613014e)."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    pid = _printer_id_arg(request)
    drv = await _printer_driver(pid)
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.status:
        # «Не умею телеметрию» и «нет связи» — разные ответы. Без этой ветки принтер без
        # телеметрии отрисовывался бы как СЛОМАННЫЙ, и мастер искал бы неисправность там,
        # где её нет (цикл 3, волна 3).
        return web.json_response({"ok": False, "driver": drv.describe(),
                                  "error": f"{drv.title}: телеметрию этот принтер не отдаёт"})
    # ТОЛЬКО кэш фонового MQTT-подписчика. Раньше при устаревшем кэше делался разовый
    # get_status (connect→subscribe→disconnect до 8 с) — а кэш «устаревал» в простое постоянно,
    # т.к. принтер вещает редко. Итог: страница поллит раз в 2.5–3 с → шторм переподключений,
    # который досил принтер (и, похоже, ронял его камеру). Теперь: соединение подписчика живо →
    # кэш актуален; соединения нет → честный оффлайн (подписчик сам переподключается с бэкофом
    # ≤30 с, стучаться из хендлера незачем). max_age=30 — грация на время реконнекта.
    st = await asyncio.to_thread(drv.status, 30.0) or {"ok": False,
                                                       "error": "Нет связи с принтером"}
    if st.get("ok"):
        state = st.get("state") or {}
        state.update(drv.enrich(state))             # аддитивно: ключи в форме мини-аппы (#printerview)
        st["state"] = state
        # summarize/extras — разбор ИМЕННО SDCP-телеметрии (слоты CANVAS, коды ошибок,
        # вентиляторы). У принтера другого вендора этих полей нет, и выдумывать их нельзя.
        if drv.kind == "elegoo":
            st["summary"] = elegoo.summarize(state)
            st["extras"] = elegoo.extras(state)
        else:
            st.setdefault("summary", {})
            st.setdefault("extras", {})
        # «Богатая шапка» (OpenTodo 09e9ee22): если печатаем и знаем заказ — отдаём его данные
        # (миниатюра, пластик, граммы). order_id/plastic/filename кладём туда, где их уже читает UI.
        if (state.get("print_stats") or {}).get("state") == "printing":
            job = await _active_print_header(pid)
            if job:
                state["order_id"] = job["order_id"]
                state["plastic"] = job["plastic"]
                state.setdefault("print_stats", {})["filename"] = job["label"]
                st["job"] = job
        # Привязка печать→заказ (OpenTodo 70b5a590) — для секции «Заказ печати» в UI.
        b = await db.get_print_binding(pid)
        rr = await db.get_request(b["request_id"]) if b and b.get("request_id") else None
        if rr:
            # миниатюра заказа — для карточки принтера на «Пульте»/«Печати» (как в списке
            # заказов): рендер /reqprev, иначе фото результата, иначе пусто (фронт даст иконку)
            preview = (signed_url("req", rr.id, {"v": _file_ver(rr.preview_path)})
                       if rr.preview_path and Path(rr.preview_path).exists()
                       else (_photo_url("order", rr.id, rr.done_photo) or ""))
            st["bound"] = {"rid": rr.id, "label": rr.model_label,
                           "status_label": db.REQ_STATUS_LABELS.get(rr.status, rr.status),
                           "finished": int(b.get("finished") or 0), "preview": preview}
        # «Пластик не указан» при живой печати — враньё, когда печать стартовали не из бота:
        # пластик знает привязанный заказ, а без него — активный слот CANVAS (принтер сам
        # сообщает, какая катушка подаётся). Заполняем той же цепочкой, что и «богатая шапка».
        if (state.get("print_stats") or {}).get("state") == "printing" and not state.get("plastic"):
            plastic = (rr.plastic_name or "") if rr else ""
            if not plastic:
                ex = st.get("extras") or {}
                slot = next((s for s in (ex.get("slots") or [])
                             if s.get("slot") == ex.get("active_slot")), None)
                if slot:
                    plastic = f"{slot.get('brand') or ''} {slot.get('name') or ''}".strip() \
                        or (slot.get("type") or "")
            if plastic:
                state["plastic"] = plastic
    # Камеру отдаём как свой HTTPS-прокси (/api/admin/camera), а не прямой http-URL принтера:
    # mjpeg по http на https-странице Telegram блокируется как mixed-content. Прокси = тот же origin.
    # Спросили конкретный принтер — адрес несёт его id: иначе фронт, который умеет выбирать
    # машину, просил бы поток «вообще» и получал стол принтера по умолчанию.
    st["camera_url"] = ("" if not drv.camera_url() else
                        f"/api/admin/camera?printer_id={pid}" if pid else "/api/admin/camera")
    # Способности принтера (0613014e): по ним обе админки прячут кнопки, которых у этого
    # принтера нет. Раньше кнопка была всегда, а «не умеет» выяснялось нажатием.
    st["driver"] = drv.describe()
    return web.json_response(st)

async def _api_admin_printer_print(request: web.Request) -> web.Response:
    """СТАРТ печати файла на принтере (method 1020). Только по явному действию админа."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    pid = _toint(body.get("printer_id")) or None
    drv = await _printer_driver(pid)
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.start:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
    filename = (body.get("filename") or "").strip()
    if not filename:
        return web.json_response({"ok": False, "error": "Не указан файл"})
    # Опциональные параметры старта (по умолчанию — прежнее поведение): auto-leveling, пластина,
    # карта слотов. slot_map = [{canvas_id,t,tray_id}] — «филамент gcode → слот хаба».
    kw: dict = {}
    if "bedlevel" in body:
        kw["bedlevel"] = bool(body.get("bedlevel"))
    if body.get("plate"):
        plate = str(body.get("plate")).strip().upper()
        if plate not in ("A", "B"):
            return web.json_response({"ok": False, "error": "Сторона пластины — A или B"})
        kw["plate"] = plate
    storage = (body.get("storage") or "local").strip()
    if storage != "local":
        if not drv.caps.usb:
            return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
        kw["storage"] = storage
    if isinstance(body.get("slot_map"), list) and body["slot_map"]:
        sm, why = _clean_slot_map(body["slot_map"])
        if why:
            return web.json_response({"ok": False, "error": why})
        # Повторная проверка по самому файлу (идея CC2 Control сообщества): каждый филамент
        # файла должен получить слот. Диалог мог открыться на устаревшем списке, а принтер
        # молча подал бы на «забытый» инструмент что попало. Нет ответа 1046 — не блокируем:
        # старт важнее, а карта уже проверена по форме.
        info = await asyncio.to_thread(drv.file_info, filename, storage=storage)
        need = {int(c.get("extruder")) for c in ((info.get("file") or {}).get("colors") or [])
                if isinstance(c.get("extruder"), int)} if info.get("ok") else set()
        missing = sorted(need - {m["t"] for m in sm})
        if missing:
            return web.json_response({"ok": False, "error": (
                "Не всем филаментам файла назначен слот: "
                + ", ".join(f"T{t}" for t in missing))})
        kw["slot_map"] = sm
    else:
        if body.get("tray_id") is not None:
            try:
                kw["tray_id"] = int(body["tray_id"])
            except (TypeError, ValueError):
                pass
        if body.get("filament_t") is not None:
            try:
                kw["filament_t"] = int(body["filament_t"])
            except (TypeError, ValueError):
                pass
    res = await asyncio.to_thread(lambda: drv.start(filename, **kw))
    # Чем и КАК отправили (OpenTodo 8e8d4873): параметры старта — пластина, слоты хаба,
    # автокалибровка стола — в gcode не записаны, их знаем только мы и только сейчас.
    if res.get("ok"):
        # Файл с флешки CC2 Control печатает из своей копии в памяти, под другим именем: под
        # ним задание и придёт в журнал.
        printed = str(res.get("filename") or filename)
        try:
            await db.save_print_settings(
                printed, start={k: v for k, v in kw.items()}, started=True,
                norm_name=elegoo.normalize_job_name(printed),
                printer_id=_driver_printer_id(drv))
        except Exception:  # noqa: BLE001
            log.debug("не записать параметры старта печати", exc_info=True)
        # «Богатая шапка»: заказ, отправленный на печать ЭТОЙ ручкой, до сих пор в неё не
        # попадал — указатель ставил только путь Moonraker и ручная привязка. У владельца
        # печать идёт именно отсюда (Elegoo, method 1020), поэтому шапка с превью, пластиком
        # и весом зажигалась только после ручной привязки заказа.
        rid = _toint(body.get("request_id"))
        if rid:
            _set_active_print(rid, await _active_print_key(pid))
    return web.json_response(res)

async def _api_admin_reprint_parts(request: web.Request) -> web.Response:
    """Детали текущего задания для диалога «Допечатать без детали» (Путь A, app/reprint.py).

    Недоступность — не ошибка, а ответ `{available: false, reason: …}`: интерфейс показывает
    ПРИЧИНУ вместо кнопки, а не молча прячет действие."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    pid = _printer_id_arg(request)
    drv = await _printer_driver(pid)
    return web.json_response(await reprint.availability(pid, drv=drv))


async def _api_admin_reprint(request: web.Request) -> web.Response:
    """ШАГ 1 допечатки: стоп → переслайс → заливка. БЕЗ старта (app/reprint.py).

    Останавливает печать — то есть уничтожает уже напечатанное. Зовётся только по явному
    подтверждению мастера в диалоге; никакой автоматики за этой ручкой нет. Старт — отдельная
    ручка `_api_admin_reprint_start`: новое задание печатается с первого слоя и в новой
    раскладке, а на столе в этот момент лежат недопечатанные детали."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    pid = _toint(body.get("printer_id")) or None
    drop: list[int] = []
    for v in body.get("drop") or []:
        i = _toint(v)
        if i is not None:
            drop.append(int(i))
    expect = {}
    for k, v in (body.get("expect") or {}).items():
        i = _toint(k)
        if i is not None:
            expect[int(i)] = str(v or "")
    drv = await _printer_driver(pid)
    # Отказы ДО стопа (гейты, устаревший список деталей, пустой выбор) приходят сюда
    # исключением и печать не трогали; всё, что случилось ПОСЛЕ стопа, ядро возвращает
    # ответом с `stopped: true` — мастер обязан узнать, что печать убита.
    try:
        res = await reprint.reprint_without(drv, pid, drop, uid=uid, expect_ids=expect)
    except reprint.ReprintUnavailable as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.warning("допечатка: ручка не отработала", exc_info=True)
        return web.json_response({"ok": False, "error": f"Допечатка не вышла: {exc}"})
    return web.json_response(res)


async def _api_admin_reprint_start(request: web.Request) -> web.Response:
    """ШАГ 2 допечатки: запустить уже залитое задание (app/reprint.py::start_reprint).

    Отдельная ручка, отдельная кнопка и отдельное подтверждение — потому что печать пойдёт с
    ПЕРВОГО СЛОЯ в новой раскладке, и стол к этому моменту должен быть пуст. Автоматический
    старт сразу после нарезки означал бы заход соплом в лежащий на столе пластик."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    pid = _toint(body.get("printer_id")) or None
    drv = await _printer_driver(pid)
    try:
        res = await reprint.start_reprint(drv, pid, str(body.get("filename") or ""), uid=uid)
    except reprint.ReprintUnavailable as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.warning("допечатка: старт не отработал", exc_info=True)
        return web.json_response({"ok": False, "error": f"Не запустить допечатку: {exc}"})
    return web.json_response(res)


async def _api_admin_printer_bind(request: web.Request) -> web.Response:
    """Привязать текущую печать к заказу (OpenTodo 70b5a590) — кнопка в меню просмотра печати."""
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
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    pid = _toint(body.get("printer_id")) or None
    job = ""                                       # имя текущего задания — для сопоставления таймлапса
    try:
        drv = await _printer_driver(pid)
        cs = await asyncio.to_thread(drv.status, 30.0)
        ps = ((cs or {}).get("state") or {}).get("print_status") or {}
        job = ps.get("filename") or ps.get("task_name") or ps.get("name") or ""
    except Exception:  # noqa: BLE001
        job = ""
    await db.set_print_binding(rid, job, pid)
    # ключ тот же, что у привязки строкой выше: разъедься они — шапка не зажглась бы
    _set_active_print(rid, await _active_print_key(pid))
    return web.json_response({"ok": True})

async def _api_admin_printer_unbind(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    await db.clear_print_binding(_toint(body.get("printer_id")) or None)
    return web.json_response({"ok": True})

async def _api_admin_printer_history(request: web.Request) -> web.Response:
    """Последние печати с принтера (method 1036) — для выбора, какая печать относится к заказу
    (OpenTodo c83a2a9e). Отдаём имя, время завершения и есть ли таймлапс."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    drv = await _printer_driver()
    if not (drv.enabled() and drv.caps.history):
        return web.json_response({"ok": False, "error": "Принтер Elegoo не настроен"})
    res = await asyncio.to_thread(drv.history, 8.0)
    if not res.get("ok"):
        return web.json_response({"ok": False, "error": res.get("error") or "История недоступна"})
    jobs = sorted(res.get("jobs", []), key=lambda j: j.get("end_time") or 0, reverse=True)[:20]
    return web.json_response({"ok": True, "jobs": [
        {"name": j.get("name") or "", "end_time": j.get("end_time") or 0,
         "ago": _ago(int(j.get("end_time") or 0)),
         "has_timelapse": bool(j.get("time_lapse_video_url"))} for j in jobs]})

async def _link_attached_print_journal(rid: int, job_name: str) -> bool:
    """Найти запись журнала печати по имени задания и привязать к заказу (OpenTodo 71523f3f):
    ручная привязка из истории принтера раньше давала заказу только файл видео, а сама печать
    (запись журнала со скрином стола, граммами, временем) оставалась неприкаянной — и медиа
    заказа (_order_prints_media) её не видели. Матчим канонично (elegoo.normalize_job_name);
    кандидаты — свободные записи (request_id пуст) и уже наша (идемпотентность), из нескольких
    берём свежайшую; привязанную к ДРУГОМУ заказу не крадём. Заодно даём заказу скрин стола.
    Best-effort: видео уже привязано, неудача матчинга не должна ронять ручку."""
    try:
        key = elegoo.normalize_job_name(job_name)
        if not key:
            return False
        best = None
        for e in await db.list_print_journal(200):        # свежие первыми (ORDER BY id DESC)
            if elegoo.normalize_job_name(e.get("label")) != key:
                continue
            cur = int(e.get("request_id") or 0)
            if cur and cur != int(rid):
                continue                                  # чужая привязка — не трогаем
            best = e                                      # первая подходящая = самая свежая
            break
        if not best:
            return False
        if int(best.get("request_id") or 0) != int(rid):
            await db.set_print_journal_fields(int(best["id"]), request_id=int(rid))
        await printerlive.copy_journal_photo_to_order(int(best["id"]), int(rid))
        return True
    except Exception:  # noqa: BLE001
        log.debug("link attached print journal failed", exc_info=True)
        return False

async def _api_admin_order_attach_print(request: web.Request) -> web.Response:
    """Прикрепить ВЫБРАННУЮ печать из истории принтера к заказу (OpenTodo c83a2a9e + 6b2eb433):
    скачиваем таймлапс этой печати, сохраняем у заказа (виден в карточке заказа у клиента и
    админа), по флагу send отправляем клиенту видео сразу. Плюс привязываем САМУ печать (запись
    журнала) и её скрин стола к заказу (71523f3f) — заказ получает все данные печати."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    drv = await _printer_driver()
    if not (drv.enabled() and drv.caps.history):
        return web.json_response({"ok": False, "error": "Принтер Elegoo не настроен"})
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    job_name = (body.get("job_name") or "").strip()
    if not job_name:
        return web.json_response({"ok": False, "error": "Выберите печать из истории"})
    # авто-кэш с финиша печати (c084f065) — в подпапке машины, которая печатала
    cached = printmedia.cached_timelapse_path(job_name, _driver_printer_id(await _printer_driver()))
    if cached:
        dest = printmedia._TIMELAPSE_DIR / f"{rid}.mp4"
        await asyncio.to_thread(shutil.copyfile, cached, dest)
        await db.update_request(rid, timelapse=str(dest))
        linked = await _link_attached_print_journal(rid, job_name)
        r = await db.get_request(rid)
        if body.get("send"):
            await printmedia._deliver_timelapse(r, dest)
        await db.log_event(None, "привязал печать", f"#{rid} ← «{job_name}» (из кэша)")
        return web.json_response({"ok": True, "timelapse": _timelapse_url(r),
                                  "journal_linked": linked})
    res = await asyncio.to_thread(drv.history, 8.0)
    if not res.get("ok"):
        return web.json_response({"ok": False, "error": res.get("error") or "История недоступна"})
    job = next((j for j in res.get("jobs", []) if (j.get("name") or "").strip() == job_name), None)
    if not job:
        return web.json_response({"ok": False, "error": "Печать не найдена в истории принтера"})
    tl_url = job.get("time_lapse_video_url")
    if not tl_url:
        return web.json_response({"ok": False, "error": "У этой печати нет таймлапса"})
    try:
        dest = await printmedia._download_timelapse(tl_url, rid, _printer_ep(drv))
    except Exception:  # noqa: BLE001
        log.warning("attach chosen timelapse failed", exc_info=True)
        dest = None
    if not dest:
        return web.json_response({"ok": False, "error": "Не удалось скачать таймлапс с принтера"})
    await db.update_request(rid, timelapse=str(dest))
    linked = await _link_attached_print_journal(rid, job_name)
    r = await db.get_request(rid)
    if body.get("send"):
        await printmedia._deliver_timelapse(r, dest)
    await db.log_event(None, "привязал печать", f"#{rid} ← «{job_name}»")
    return web.json_response({"ok": True, "timelapse": _timelapse_url(r), "journal_linked": linked})


async def _api_admin_queue(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.queue_items()
    for it in items:
        it["status_label"] = db.REQ_STATUS_LABELS.get(it["status"], it["status"])
        it["eta"] = db.eta_date(it["done_minutes"])
        # Миниатюра строки очереди: рендер модели, а у заказов мастера/индивидуальных без
        # рендера — фото результата. Иначе строка показывала бы пустой тёмный квадрат.
        pp = it.pop("preview_path", None)
        dp = it.pop("done_photo", None)
        it["preview"] = (
            signed_url("req", it["id"], {"v": _file_ver(pp)})
            if pp else (_photo_url("order", it["id"], dp) or ""))
    return web.json_response({"ok": True, "currency": config.CURRENCY, "queue": items})

async def _api_admin_queue_move(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    delta = -1 if str(body.get("dir")) == "up" else 1
    ok = await db.queue_move(_toint(body.get("id")), delta)
    return web.json_response({"ok": bool(ok)})

async def _api_admin_self_print(request: web.Request) -> web.Response:
    """«Печать для себя»: внутренняя печать админа — списывает филамент и копит наработку ТО,
    но НЕ попадает в клиентскую аналитику (хранится отдельно от requests)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    pid = _toint(body.get("plastic_id"))
    p = await db.get_plastic(pid) if pid else None
    if not p:
        return web.json_response({"ok": False, "error": "Выберите пластик"})
    try:
        grams = float(str(body.get("grams") or 0).replace(",", "."))
    except (TypeError, ValueError):
        grams = 0.0
    if grams <= 0:
        return web.json_response({"ok": False, "error": "Граммы — число больше 0"})
    try:
        minutes = float(str(body.get("minutes") or 0).replace(",", "."))
    except (TypeError, ValueError):
        minutes = 0.0
    label = (body.get("label") or "Печать для себя").strip()[:120]
    color = (body.get("color") or "").strip() or None
    await db.add_self_print(p.name, grams, minutes, label)
    try:        # списываем филамент (как при обычном заказе)
        new_stock, low = await db.deduct_color_stock(pid, color, grams)
        if low and _bot is not None:
            await notify.alert(f"Низкий остаток филамента: «{p.name}» — осталось {new_stock:.0f} г",
                               key=f"lowstock_{pid}", throttle_sec=3600)
    except Exception:  # noqa: BLE001
        log.debug("self-print stock deduct failed", exc_info=True)
    try:        # наработка ТО как от обычной печати (одна печать)
        crossed = await db.accrue_service(minutes, 1)
        for it in (crossed or []):
            await db.mark_service_notified(it.id, 1)
            if _bot is not None:
                # ТО принтера делает тот, кто у станка, — оператору это нужнее владельца.
                await notify.notify_admins(_bot, texts.service_due_alert(it),
                                           audience=notify.ALL_STAFF)
    except Exception:  # noqa: BLE001
        log.debug("self-print service accrual failed", exc_info=True)
    return web.json_response({"ok": True})

async def _api_admin_service(request: web.Request) -> web.Response:
    """Список пунктов ТО со статусом-светофором (фаза 2 docs/maintenance-reminders.md)."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    now = int(time.time())
    warn = float((await db.get_settings()).get("service_warn_pct", 85) or 85) / 100.0
    out = []
    for it in await db.list_service_items(active_only=False):
        cur, thr = it.progress(now)
        ratio = (cur / thr) if thr > 0 else 0.0
        status = "red" if ratio >= 1 else ("amber" if ratio >= warn else "green")
        out.append({"id": it.id, "label": it.label, "trigger": it.trigger_type,
                    "threshold": thr, "current": round(cur, 1), "ratio": round(ratio, 3),
                    "status": status, "is_active": bool(it.is_active)})
    return web.json_response({"ok": True, "items": out, "warn_pct": round(warn * 100)})

async def _api_admin_service_action(request: web.Request) -> web.Response:
    """Действия над пунктом ТО: обслужено / порог / тип триггера / вкл-выкл / добавить / удалить."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    iid = _toint(body.get("id"))
    if action == "done":
        await db.mark_serviced(iid, (body.get("note") or "").strip()[:200])
        return web.json_response({"ok": True})
    if action == "threshold":
        try:
            thr = float(str(body.get("threshold")).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Порог — число"})
        await db.update_service_item(iid, threshold=max(0.0, thr))
        return web.json_response({"ok": True})
    if action == "type":
        tt = (body.get("trigger_type") or "").strip()
        if tt not in ("hours", "prints", "days"):
            return web.json_response({"ok": False, "error": "Неизвестный тип"})
        await db.update_service_item(iid, trigger_type=tt)
        return web.json_response({"ok": True})
    if action == "toggle":
        it = await db.get_service_item(iid)
        if it:
            await db.update_service_item(iid, is_active=0 if it.is_active else 1)
        return web.json_response({"ok": True})
    if action == "add":
        label = (body.get("label") or "").strip()
        if not label:
            return web.json_response({"ok": False, "error": "Введите название"})
        tt = (body.get("trigger_type") or "hours").strip()
        if tt not in ("hours", "prints", "days"):
            tt = "hours"
        try:
            thr = float(str(body.get("threshold") or 0).replace(",", "."))
        except (TypeError, ValueError):
            thr = 0.0
        nid = await db.add_service_item(label, tt, max(0.0, thr))
        return web.json_response({"ok": True, "id": nid})
    if action == "del":
        await db.delete_service_item(iid)
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})
