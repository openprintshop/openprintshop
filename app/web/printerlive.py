"""Живой принтер в веб-слое: ретранслятор камеры, скрин стола, файлы и ручной пульт.

Один предмет: то, чем мастер СМОТРИТ на машину и ТРОГАЕТ её руками — прокси камеры поверх
общего ретранслятора, кадр для скрина стола, список/заливка/удаление gcode и ручные команды
(пауза/стоп/свет, филамент, прогрев, оси, вентиляторы). Все пути — CAP_PRINTER.

Выделено из app/webapp.py волной 4 endgame-плана (после волны 2: драйвер берётся из
app/web/printerhub.py). НЕ ВЗЯТЫ соседи по прежнему расположению, и это не забывчивость:
  * `_api_admin_printer_print` пишет указатель активной печати (`_set_active_print` →
    `_active_print`) — состояние webapp до волны 10; уехав, писал бы в чужой словарь, и
    «богатая шапка» экрана принтера гасла бы для сетевой печати МОЛЧА.
  * `_api_admin_printer_status` читает `_active_print_header` — то же состояние (волна 11).
  * привязка печати, confirm_print и разбор печати держатся за `_bot` и доставку — волны 11/13.

МОДУЛЬНОЕ СОСТОЯНИЕ ПЕРЕЕХАЛО СЮДА: реестр ретрансляторов `_cam_relays` (ключ — URL камеры).
Словарь только мутируется и не пере-присваивается, поэтому фасад webapp реэкспортирует ТОТ ЖЕ
объект — тесты чистят его через фасад настоящим. Плоские константы (`_CAM_LINGER_S`,
`_UPLOAD_MAX_BYTES`) фасадом не реэкспортируются (подмена через webapp перепривязала бы копию).

app/printwatch.py с этой волны берёт `capture_bed_shot` ОТСЮДА — у наблюдателя остаётся
ровно один отложенный `from . import webapp` (_active_print_header, волна 10).

Регистрация маршрутов осталась в make_app строками на фасадные имена: забытая строка фасада
падает громко на старте, а не молча у клиента.

Тела перенесены ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .. import config, db, elegoo, gcodemeta, preview
from ..webauth import _admin_body, _admin_forbidden, _verify_admin
from .common import _bad
from .printerext import _move_out_of_bounds
from .printerhub import _camera_url, _no_printer, _printer_driver, _printer_id_arg

log = logging.getLogger(__name__)


# ── Ретранслятор камеры: ОДНО подключение НА КАМЕРУ на всех зрителей ─────────────────────────
# Раньше каждый зритель потока держал СВОЁ соединение к mjpeg-серверу принтера, а snap-фолбэк
# мобильной админки открывал НОВОЕ соединение под каждый кадр (раз в 2.5 с!). Несколько
# экранов + авто-реконнекты = шторм подключений к :8080, от которого камера принтера
# отваливалась (фидбэк 20.07). Апстрим ровно один: фоновая задача читает поток, режет
# на JPEG-кадры (SOI/EOI) и раздаёт подписанным очередям; snap берёт последний кадр бесплатно.
# Апстрим живёт, пока есть зрители (+линджер под snap-опрос), и гаснет сам, когда все ушли.
#
# «Один» до волны 6 означало «один на процесс»: состояние лежало в четырёх модульных
# переменных, URL перечитывался на каждом заходе цикла, и смена принтера по умолчанию молча
# переводила текущий ретранслятор на другую камеру — старые зрители получали ЧУЖОЙ стол. Тем же
# путём чужой кадр попадал в скрин стола, а оттуда — в фото результата ЗАКАЗА клиента.
#
# Теперь ретранслятор — объект, а ключ реестра — URL КАМЕРЫ, не id карточки. Ключ по карточке
# поднял бы два апстрима к одной камере, если на один принтер заведены две карточки (например
# под разные сопла) — то есть вернул бы ровно тот шторм, ради защиты от которого всё написано.
_CAM_LINGER_S = 15.0
# Сколько ждём, пока зритель заберёт из сокета ОДИН кадр. Больше линджера смысла не имеет:
# зритель, который столько не читает, уже не зритель. См. разбор в _api_admin_camera.
_CAM_WRITE_TIMEOUT_S = 12.0


class _CamRelay:
    """Ретранслятор ОДНОЙ камеры: фоновый апстрим, последний кадр и очереди зрителей."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.clients: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.last: tuple[float, bytes] | None = None   # (unix-время, jpeg) — последний кадр
        self.seen: float = 0.0                         # monotonic последнего интереса зрителей

    def wanted(self) -> bool:
        return bool(self.clients) or (time.monotonic() - self.seen) < _CAM_LINGER_S

    def touch(self) -> None:
        """Отметить интерес зрителя и поднять апстрим, если он не крутится."""
        self.seen = time.monotonic()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._relay())

    def fresh_frame(self, max_age: float = 5.0) -> bytes | None:
        """Последний кадр, если он не старше max_age. Кадр бесплатный: апстрим уже смотрит."""
        if self.last and time.time() - self.last[0] < max_age:
            return self.last[1]
        return None

    def feed(self, buf: bytes) -> bytes:
        """Вырезать из накопленного потока все целые JPEG (SOI..EOI), раздать зрителям; вернуть
        неразобранный хвост. Отстающий зритель теряет старые кадры, а не копит лаг."""
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                return buf[-1:]                 # возможен пол-маркера на границе чанка
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                return buf[start:]
            frame, buf = buf[start:end + 2], buf[end + 2:]
            self.last = (time.time(), frame)
            for q in list(self.clients):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    pass

    async def _relay(self) -> None:
        """Фоновый апстрим: держит одно соединение, при обрыве переподключается с бэкофом,
        завершается сам, когда зрителей нет дольше линджера.

        URL прибит к объекту и НЕ перечитывается: сменился адрес принтера — это другая камера,
        и ей полагается свой ретранслятор со своими зрителями, а не тихая подмена картинки под
        уже открытыми экранами."""
        backoff = 0.5
        try:
            while self.wanted():
                try:
                    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=12)
                    async with aiohttp.ClientSession(timeout=timeout) as s, s.get(self.url) as r:
                        if r.status != 200:
                            raise OSError(f"камера ответила {r.status}")
                        buf = b""
                        async for chunk in r.content.iter_chunked(8192):
                            if not self.wanted():
                                return
                            buf = self.feed(buf + chunk)
                            backoff = 0.5
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.debug("camera relay upstream hiccup", exc_info=True)
                if self.wanted():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 5.0)
        finally:
            self.task = None
            _cam_relays.pop(self.url, None)      # погасший ретранслятор в реестре не держим


_cam_relays: dict[str, _CamRelay] = {}


def _cam_relay_for(url: str) -> _CamRelay:
    """Ретранслятор этой камеры — общий на всех её зрителей."""
    ses = _cam_relays.get(url)
    if ses is None:
        ses = _CamRelay(url)
        _cam_relays[url] = ses
    return ses


async def _grab_camera_frame(printer_id: int | None = None) -> bytes | None:
    """Один JPEG-кадр с камеры принтера (для скрина стола по итогам печати). None — не вышло."""
    url = await _camera_url(printer_id)
    if not url:
        return None
    frame = _cam_relay_for(url).fresh_frame()
    if frame is not None:
        return frame                            # ретранслятор уже смотрит — кадр бесплатный
    timeout = aiohttp.ClientTimeout(total=10, sock_connect=4, sock_read=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s, s.get(url) as r:
            if r.status != 200:
                return None
            buf = b""
            async for chunk in r.content.iter_chunked(8192):
                buf += chunk
                start = buf.find(b"\xff\xd8")                 # JPEG SOI
                if start >= 0:
                    end = buf.find(b"\xff\xd9", start + 2)     # JPEG EOI
                    if end >= 0:
                        return buf[start:end + 2]
                if len(buf) > 4_000_000:
                    break
    except Exception:  # noqa: BLE001
        return None
    return None


async def copy_journal_photo_to_order(jid: int, rid: int) -> bool:
    """Скопировать скрин стола печати (print_journal.done_photo) в фото результата заказа
    (requests.done_photo): копия done_<rid>.jpg + пережим. Только если файл СУЩЕСТВУЕТ (сознательно
    удалённое мастером не воскрешаем) и у заказа фото ещё нет (ручное не затираем). Единая точка
    для финиша печати (capture_bed_shot) и обеих привязок печати к заказу (OpenTodo 71523f3f).
    Never-raise: косметика медиа не должна ронять привязку."""
    try:
        e = await db.get_print_journal(int(jid))
        src = (e or {}).get("done_photo") or ""
        if not src or not Path(src).exists():
            return False
        r = await db.get_request(int(rid))
        if not r or (getattr(r, "done_photo", "") and Path(r.done_photo).exists()):
            return False
        config.COLLECTION_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.COLLECTION_DIR / f"done_{int(rid)}.jpg"
        await asyncio.to_thread(shutil.copyfile, src, dest)
        await asyncio.to_thread(preview.optimize_photo, dest)
        await db.update_request(int(rid), done_photo=str(dest))
        return True
    except Exception:  # noqa: BLE001
        log.debug("copy journal photo to order failed", exc_info=True)
        return False


async def capture_bed_shot(jid: int, request_id: int | None = None,
                           printer_id: int | None = None) -> bool:
    """Скрин стола по итогам печати (OpenTodo): один кадр камеры → фото записи журнала, а если
    печать привязана к заказу — и фото результата заказа. Нужен, чтобы у печати всегда была
    хоть какая-то картинка, даже когда таймлапс ещё не готов, а мастер фото не приложил.
    Уже загруженное вручную фото не затираем.

    `printer_id` — чья это была печать. Самый дорогой путь ошибки во всём ретрансляторе: кадр
    отсюда уезжает в фото РЕЗУЛЬТАТА заказа, то есть клиенту. Взять кадр не с той машины значит
    показать покупателю чужую деталь."""
    raw = await _grab_camera_frame(printer_id)
    if not raw:
        return False
    try:
        config.COLLECTION_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.COLLECTION_DIR / f"jphoto_{jid}.jpg"
        dest.write_bytes(raw)
        await asyncio.to_thread(preview.optimize_photo, dest)
        await db.set_print_journal_fields(jid, done_photo=str(dest))
        if request_id:
            await copy_journal_photo_to_order(jid, request_id)
        return True
    except Exception:  # noqa: BLE001
        log.warning("capture bed shot failed", exc_info=True)
        return False


async def _api_admin_camera(request: web.Request) -> web.StreamResponse:
    """HTTPS-прокси камеры принтера (обходит mixed-content в Telegram WebView) поверх общего
    ретранслятора (_cam_relay): сколько бы экранов ни смотрело, к принтеру идёт ОДНО
    соединение. По умолчанию — multipart-поток кадров; ?mode=snap → один JPEG (фолбэк опросом,
    отдаётся из последнего кадра ретранслятора без похода к принтеру). Обрывы апстрима чинит
    сам ретранслятор — зритель их не видит.

    Зритель, переставший читать сокет (не закрыв его), отцепляется по таймауту записи — см.
    разбор в теле; иначе обработчик висит до обрыва соединения по ОС."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    url = await _camera_url(_printer_id_arg(request))
    if not url:
        raise web.HTTPNotFound()
    relay = _cam_relay_for(url)
    relay.touch()
    if request.query.get("mode") == "snap":
        # ждём кадр от ретранслятора (он мог только что подняться); линджер 15 c больше цикла
        # ожидания, поэтому повторно touch внутри цикла не нужен
        for _ in range(80):                                    # ≤ 8 с
            frame = relay.fresh_frame()
            if frame is not None:
                return web.Response(body=frame, content_type="image/jpeg",
                                    headers={"Cache-Control": "no-store"})
            await asyncio.sleep(0.1)
        raise web.HTTPBadGateway(text="кадр не получен")
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    relay.clients.add(q)
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=pcbframe",
        "Cache-Control": "no-store, no-cache, must-revalidate"})
    loop = asyncio.get_running_loop()
    try:
        await resp.prepare(request)
        deadline = loop.time() + 1800                          # потолок 30 мин, как раньше
        while loop.time() < deadline:
            try:
                frame = await asyncio.wait_for(q.get(), timeout=25.0)
            except asyncio.TimeoutError:
                break              # кадров долго нет — отпускаем клиента, <img> переоткроется
            try:
                await asyncio.wait_for(
                    resp.write(b"--pcbframe\r\nContent-Type: image/jpeg\r\n"
                               b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                               + frame + b"\r\n"),
                    timeout=_CAM_WRITE_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Зритель ЖИВ по TCP, но кадры из сокета не забирает: замёрзшее окно WebView
                # деска, свёрнутая вкладка. Окно приёма схлопывается, aiohttp засыпает в drain
                # — и БЕЗ таймаута спит вечно: потолок `deadline` проверяется между итерациями
                # и до висящей записи не доходит. Разбор 10.09: за сутки девять таких
                # обработчиков провисели по 7216 c (пока ОС не оборвала сокет сама), каждый
                # всё это время держал очередь в relay.clients и кончился 500 с алертом.
                #
                # Рвём соединение САМИ. Не close(): тот ждёт слива буфера, которого зритель не
                # читает, — то есть снова вечный сон. abort() буфер выбрасывает, а sleep(0)
                # пропускает вперёд connection_lost: без него уже отменённый drain-waiter
                # достался бы закрытию ответа (write_eof) и уснул бы на том же месте.
                tr = request.transport
                if tr is not None:
                    tr.abort()
                    await asyncio.sleep(0)
                break              # зритель не читает — <img> переоткроет поток сам
    except (ConnectionError, asyncio.CancelledError):
        # ConnectionError, а не ConnectionResetError: обрыв на записи прилетает из
        # base_protocol как ГОЛЫЙ ConnectionError("Connection lost") — узкий except его
        # пропускал, и обычный уход зрителя всплывал 500-й в _error_mw с алертом админам.
        pass                       # клиент закрыл вкладку — это норма
    finally:
        relay.clients.discard(q)
        relay.seen = time.monotonic()         # линджер отсчитываем от ухода последнего зрителя
    return resp


async def _api_admin_camera_check(request: web.Request) -> web.Response:
    """Диагностика камеры: пробуем достучаться до потока и возвращаем причину сбоя.
    Так «видео не работает» превращается в конкретику: какой URL и что ответил (или почему недоступен)."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    url = await _camera_url(_printer_id_arg(request))
    if not url:
        return web.json_response({"ok": False, "url": "",
                                  "error": "URL камеры не задан: пусты и адрес принтера "
                                           "(карточка либо ELEGOO_PRINTER_IP), и PRINTER_CAMERA_URL"})
    try:
        timeout = aiohttp.ClientTimeout(total=6, sock_connect=4, sock_read=4)
        async with aiohttp.ClientSession(timeout=timeout) as s, s.get(url) as r:
            ct = r.headers.get("Content-Type", "")
            head = b""
            try:
                head = await r.content.read(512)   # реально ли отдаёт байты потока
            except Exception:  # noqa: BLE001
                pass
            ok = r.status == 200 and bool(head)
            return web.json_response({"ok": ok, "url": url, "status": r.status, "content_type": ct,
                                      "error": None if ok else f"ответ {r.status}, тип «{ct or '—'}»"})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "url": url, "error": f"нет связи: {e}"})


async def _api_admin_printer_files(request: web.Request) -> web.Response:
    """Список gcode-файлов на принтере Elegoo (read-only). Активный 1044 отвечает только
    «привязанному» клиенту-слайсеру; если ответа нет — отдаём пассивно собранный список
    (подписчик подбирает его из обмена ElegooSlicer↔принтер), это лучше честной ошибки:
    старт печати (1020) принтер выполняет и без привязки."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    drv = await _printer_driver()
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.files:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
    storage = (request.query.get("storage") or "local").strip()
    if storage != "local":
        # Флешка — отдельный носитель со своей способностью (`usb`), снимков настроек у её
        # файлов нет: на неё бот ничего не заливает.
        if not drv.caps.usb:
            return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
        return web.json_response(await asyncio.to_thread(drv.files_on, storage, 10.0))
    r = await asyncio.to_thread(drv.files, 10.0)
    # Чем резали каждый файл (OpenTodo 8e8d4873): у принтера этих данных нет — снимок наш,
    # сделанный при заливке. Файлам без снимка (залиты слайсером напрямую) поле пустое.
    for f in (r.get("files") or []):
        name = str(f.get("name") or f.get("filename") or "")
        snap = await db.find_print_settings(name, elegoo.normalize_job_name(name),
                                            printer_id=getattr(getattr(drv, "printer", None), "id", None))
        f["settings"] = gcodemeta.summary({"values": snap.get("values") or {}}) if snap else ""
    return web.json_response(r)


async def _remember_gcode_settings(name: str, data: bytes) -> int:
    """Разобрать конфиг-блок gcode и запомнить его под именем файла (OpenTodo 8e8d4873).

    Разбор в потоке: хвост в полмегабайта и сотня регулярок на event loop бота — лишнее.
    Любая неудача разбора молча пропускается: это справка, а не условие заливки."""
    try:
        meta = await asyncio.to_thread(gcodemeta.parse_bytes, data)
        if not meta:
            return 0
        return await db.save_print_settings(
            name, values=meta.get("values") or {}, slicer_name=meta.get("slicer") or "",
            grams=meta.get("grams"), minutes=meta.get("minutes"), layers=meta.get("layers"),
            uploaded=True, norm_name=elegoo.normalize_job_name(name))
    except Exception:  # noqa: BLE001
        log.debug("не разобрать настройки gcode %s", name, exc_info=True)
        return 0


_UPLOAD_MAX_BYTES = 200 * 1024 * 1024      # предохранитель: gcode крупнее 200 МБ не бывает


async def _api_admin_printer_upload(request: web.Request) -> web.Response:
    """Залить .gcode в память принтера (HTTP-протокол разведан 20.07 — elegoo.upload_gcode).
    multipart: initData, file. Это то, что раньше приходилось делать слайсером вручную."""
    if not (request.content_type or "").startswith("multipart/"):
        return _bad()
    init_data, fname, data = "", "", b""
    reader = await request.multipart()
    async for field in reader:
        if field.name == "initData":
            init_data = (await field.read()).decode("utf-8", "ignore")
        elif field.name == "file":
            fname = field.filename or ""
            data = await field.read()
    if not _verify_admin(init_data):
        return _admin_forbidden()
    drv = await _printer_driver()
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.upload:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
    if not data or not fname:
        return web.json_response({"ok": False, "error": "Файл не приложен"})
    if len(data) > _UPLOAD_MAX_BYTES:
        return web.json_response({"ok": False, "error": "Файл слишком большой"})
    # Расширения спрашиваем У ДРАЙВЕРА: это свойство протокола, а не общее правило. Centauri
    # ест .gcode, Bambu — .3mf. Раньше «.gcode» было зашито здесь, и второй вендор упирался бы
    # в проверку, к нему не относящуюся (цикл 3, волна 3).
    exts = tuple(getattr(drv, "upload_exts", (".gcode",)))
    if not fname.lower().endswith(exts):
        return web.json_response({"ok": False,
                                  "error": f"{drv.title} принимает только {', '.join(exts)}"})
    tmp = config.DATA_DIR / "cache" / f"upload_{int(time.time())}_{Path(fname).name}"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        # имя на принтере — исходное, а не служебное имя временной копии
        res = await drv.upload(tmp, name=Path(fname).name)
        # Снимок настроек задания (OpenTodo 8e8d4873): единственный момент, когда gcode у нас
        # в руках. Дальше файл живёт на принтере, и «чем это резали» спросить было бы не у кого.
        if res.get("ok"):
            await _remember_gcode_settings(Path(fname).name, data)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    return web.json_response(res)


async def _api_admin_printer_delete(request: web.Request) -> web.Response:
    """Удалить файл из памяти принтера (method 1047). Только по явному действию админа.

    Гейт по СВОЕЙ способности `delete`, а не по `files`: показать список и удалить из него —
    разные умения, и принтер, который умеет первое и не умеет второе, раньше пропускал кнопку
    и отвечал отказом (цикл 3, волна 3)."""
    body = await _printer_cmd_endpoint(request, None, need="delete")
    if isinstance(body, web.Response):
        return body
    drv = await _printer_driver()
    # Несколько файлов и флешка (разбор фич сообщества, 03.10): {names: [...], storage}. 1047
    # принимает массив имён, поэтому у Elegoo это одна команда, а не N.
    storage = (body.get("storage") or "local").strip()
    names = body.get("names")
    if isinstance(names, list) or storage != "local":
        if storage != "local" and not drv.caps.usb:
            return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
        lst = [str(n).strip() for n in (names if isinstance(names, list) else [body.get("name")])
               if str(n or "").strip()]
        if not lst:
            return web.json_response({"ok": False, "error": "Не указан файл"})
        res = await asyncio.to_thread(drv.delete_files, lst, storage=storage)
        return web.json_response(res)
    name = (body.get("name") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "Не указан файл"})
    res = await asyncio.to_thread(drv.delete_file, name)
    return web.json_response(res)


_PRINTER_ACTIONS = {"pause", "resume", "stop", "light"}


async def _api_admin_printer_control(request: web.Request) -> web.Response:
    """Управление текущей печатью на принтере Elegoo: пауза/продолжить/стоп/свет.
    Только по явному действию админа. Method-коды команд читаются из .env (см. elegoo.control)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    drv = await _printer_driver()
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.control:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
    action = (body.get("action") or "").strip()
    if action not in _PRINTER_ACTIONS:
        return web.json_response({"ok": False, "error": "Неизвестная команда"})
    on = bool(body.get("on", True))
    res = await asyncio.to_thread(drv.control, action, on=on)
    return web.json_response(res)


async def _api_admin_printer_unload(request: web.Request) -> web.Response:
    """Выгрузка филамента из слота (1..4) хаба. Только по явному действию админа.
    Method-код (2002) и сдвиг tray_id=слот−1 — см. elegoo.unload_filament."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    drv = await _printer_driver()
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.filament:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
    try:
        slot = int(body.get("slot"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "неверный слот"})
    res = await asyncio.to_thread(drv.unload_filament, slot)
    return web.json_response(res)


def _printer_printing() -> bool:
    """Идёт ли сейчас печать (по живому кэшу). Гард для ручного управления — прогрев/оси/
    вентиляторы во время печати сбивают задание, поэтому такие команды блокируем."""
    cs = elegoo.cached_status(30.0) or {}
    state = cs.get("state") or {}
    return (elegoo.enrich_status(state).get("print_stats") or {}).get("state") == "printing"


async def _printer_cmd_endpoint(request: web.Request, fn, *args, block_while_printing: bool = True,
                                need: str = ""):
    """Общая обёртка ручных команд принтера: проверка админа/настройки/печати + вызов fn(*args).

    need — требуемая способность драйвера (0613014e). Отказ здесь честнее, чем в протоколе:
    у принтера без ручного управления кнопки не будет вовсе, а если её всё же нажали
    (прямой запрос) — ответ объясняет, что дело в принтере, а не в сбое."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    drv = await _printer_driver()
    if not drv.enabled():
        return _no_printer(drv)
    if need and not getattr(drv.caps, need, False):
        return web.json_response(
            {"ok": False, "error": f"Принтер ({drv.title}) так не умеет"})
    if block_while_printing and _printer_printing():
        return web.json_response({"ok": False, "error": "Недоступно во время печати"})
    return body


async def _api_admin_printer_load(request: web.Request) -> web.Response:
    """Загрузка филамента в слот (method 2001) — пара к выгрузке, разведана 20.07."""
    body = await _printer_cmd_endpoint(request, None, need="filament")
    if isinstance(body, web.Response):
        return body
    try:
        slot = int(body.get("slot"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "неверный слот"})
    drv = await _printer_driver()
    res = await asyncio.to_thread(drv.load_filament, slot)
    return web.json_response(res)


async def _api_admin_printer_heat(request: web.Request) -> web.Response:
    """Прогрев: задать целевую температуру сопла/стола (method 1028). {target, value}."""
    body = await _printer_cmd_endpoint(request, None, need="move")
    if isinstance(body, web.Response):
        return body
    target = (body.get("target") or "").strip()
    value = body.get("value", 0)
    drv = await _printer_driver()
    res = await asyncio.to_thread(drv.set_temperature, target, value)
    return web.json_response(res)


async def _api_admin_printer_fan(request: web.Request) -> web.Response:
    """Скорость пользовательского вентилятора в % (method 1030). {which, pct}."""
    body = await _printer_cmd_endpoint(request, None, need="move")
    if isinstance(body, web.Response):
        return body
    which = (body.get("which") or "").strip()
    pct = body.get("pct", 0)
    drv = await _printer_driver()
    res = await asyncio.to_thread(drv.set_fan, which, pct)
    return web.json_response(res)


async def _api_admin_printer_home(request: web.Request) -> web.Response:
    """Парковка осей (method 1026). {axes?} — по умолчанию xyz."""
    body = await _printer_cmd_endpoint(request, None, need="move")
    if isinstance(body, web.Response):
        return body
    axes = (body.get("axes") or "xyz").strip()
    drv = await _printer_driver()
    res = await asyncio.to_thread(drv.home, axes)
    return web.json_response(res)


async def _api_admin_printer_move(request: web.Request) -> web.Response:
    """Переместить ось на distance мм (method 1027). {axis, distance}."""
    body = await _printer_cmd_endpoint(request, None, need="move")
    if isinstance(body, web.Response):
        return body
    axis = (body.get("axis") or "").strip()
    distance = body.get("distance", 0)
    drv = await _printer_driver()
    why = await asyncio.to_thread(_move_out_of_bounds, drv, axis, distance)
    if why:
        return web.json_response({"ok": False, "error": why})
    res = await asyncio.to_thread(drv.move_axis, axis, distance)
    return web.json_response(res)
