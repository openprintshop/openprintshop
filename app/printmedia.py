"""Медиа печати: скачивание таймлапсов с принтера, авто-кэш и доставка их клиенту.

Выделено из app/webapp.py волной 3 цикла 2 БЕЗ единой правки тел функций — это первый разрез
двенадцатитысячного модуля, и он сознательно взят самым безопасным: ни один маршрут не двигается,
webapp реэкспортирует все перенесённые имена (668 якорей тестов и весь остальной код продолжают
находить их там же, где находили).

Модуль намеренно НЕ импортирует webapp: доставка (deliver_late_timelapses → _deliver_timelapses)
уехала сюда целиком именно ради этого — иначе цикл, ради разрыва которого затевался разрез,
оказался бы спрятан в отложенном импорте, а не разорван. keyboards/notify/texts webapp не
импортируют, поэтому цикла нет.

Бот сюда ставится извне (run.wire_bot), как в app/notify.py: все ветки доставки закрыты
`if _bot is None`, и забытая проводка даёт тишину без единой строки в логе.
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, db, elegoo, keyboards, notify, texts

log = logging.getLogger(__name__)

_bot = None                 # экземпляр Bot; ставится из run.wire_bot() — как в app/notify.py


def set_bot(bot):
    """Поставить бота, вернув ПРЕЖНЕГО — как webapp.set_bot.

    Возврат нужен не здесь, а у вызывающего: фикстуры подменяют бота на время теста и
    восстанавливают из возврата. Разная сигнатура у двух почти одинаковых функций — готовые
    грабли: следующий автор напишет `prev = printmedia.set_bot(x)` по аналогии и восстановит
    None, то есть тихо отключит доставку до конца прогона."""
    global _bot
    prev, _bot = _bot, bot
    return prev


def _file_size(path) -> int:
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0


_TIMELAPSE_DIR = config.DATA_DIR / "timelapses"
_TG_UPLOAD_LIMIT = 48 * 1024 * 1024     # запас под лимит загрузки Bot API (~50 МБ)


def _http_get_bytes(url: str, timeout: float = 120.0) -> bytes:
    """Блокирующее скачивание по HTTP через urllib (вызывать в to_thread).

    Почему НЕ aiohttp: сервер принтера (libhv) отдаёт ответ одновременно с Content-Length И
    Transfer-Encoding: chunked — это нарушение RFC, и aiohttp такой ответ отвергает
    («Transfer-Encoding can't be present with Content-Length»), из-за чего таймлапсы не
    качались вообще. urllib (как и браузер) читает его нормально — проверено живьём.
    ProxyHandler({}) — игнорируем HTTP(S)_PROXY из окружения: принтер в локальной сети,
    через VPN-прокси он недостижим. Таймаут обязателен: при неверном токене/имени файла
    принтер не отвечает 401/404, а молча держит соединение.
    """
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(url, timeout=timeout) as r:
        if getattr(r, "status", 200) != 200:
            raise OSError(f"HTTP {r.status}")
        return r.read()


async def _download_to(url: str, dest: Path, timeout: float = 120.0) -> Path | None:
    """Скачать файл (URL в локальной сети принтера) в dest. None при неудаче.
    Любая сетевая ошибка (в т.ч. кривой URL) — это None, а не исключение наружу: вызывающий
    показывает мастеру понятную причину, а не стектрейс. timeout — для фоновой авто-докачки его
    делают коротким: битую загрузку принтер держит открытой ~75 с до RemoteDisconnected, а
    соединение всё это время конкурирует с камерой (кейс 22.07) — лучше отвалиться быстро."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = await asyncio.to_thread(_http_get_bytes, url, timeout)
    except Exception as e:  # noqa: BLE001
        log.info("таймлапс: не скачался (%s): %s", type(e).__name__, url)
        return None
    if not data:
        return None
    dest.write_bytes(data)
    return dest


# Скачивание таймлапса с принтера (протокол разведан 20.07, docs/captures/timelapse-download.md).
# В истории заданий (MQTT 1036) поле time_lapse_video_url приходит ОТНОСИТЕЛЬНЫМ и с
# ОБМАНЧИВЫМ префиксом: "picture/<имя>.gcode<штамп>" или "video/<имя>.gcode<штамп>.mp4".
# Реальный файл ВСЕГДА лежит в video/ и ВСЕГДА с .mp4 — префикс picture/ не значит «видео нет»
# (проверено: такой ролик выкачивается целиком). Отдаётся единственным роутом принтера:
#   GET http://<ip>/download?X-Token=<ACCESS_CODE>&file_name=video/<имя>.mp4
# Пустой time_lapse_video_url — вот это и есть «видео не записано».
def _timelapse_has_video(url: str) -> bool:
    """Записан ли таймлапс у задания (пустое поле = не записан)."""
    return bool((url or "").strip())


def _timelapse_file_name(raw: str) -> str:
    """Значение из 1036 → имя файла для /download: всегда video/<имя>.mp4."""
    v = (raw or "").strip()
    if not v or v.startswith(("http://", "https://")):
        return ""
    name = v.split("/", 1)[1] if "/" in v else v      # отбрасываем префикс picture/ | video/
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return "video/" + name


def _timelapse_download_url(file_name: str, ep: elegoo.Endpoint | None = None) -> str:
    """file_name (как его ждёт роут /download принтера) → абсолютный URL скачивания.

    Единственная ручка принтера, которую строит сам вебапп, а не app/elegoo.py, — поэтому
    адрес и код сюда обязаны приезжать ТЕМ ЖЕ эндпоинтом, что и во все остальные вызовы.
    ep=None — принтер по умолчанию, как было до появления эндпоинтов."""
    e = elegoo._ep(ep)
    if not (file_name and e.ip and e.code):
        return ""
    return (f"http://{e.ip}/download?X-Token={urllib.parse.quote(e.code, safe='')}"
            f"&file_name={urllib.parse.quote(file_name, safe='')}")


def _timelapse_abs_url(url: str, ep: elegoo.Endpoint | None = None) -> str:
    """Относительный путь принтера → абсолютный URL скачивания. Пусто, если построить нельзя."""
    v = (url or "").strip()
    if not v:
        return ""
    if v.startswith(("http://", "https://")):
        return v
    return _timelapse_download_url(_timelapse_file_name(v), ep)


def _timelapse_candidate_urls(url: str, ep: elegoo.Endpoint | None = None) -> list[str]:
    """Все осмысленные URL скачивания для значения time_lapse_video_url — по порядку попыток.
    Сначала СЫРОЕ имя как есть (ровно так качает lan_service_web ElegooSlicer у ГОТОВЫХ видео),
    затем каноничное video/<имя>.mp4 из docs/captures/timelapse-download.md (подтверждено живьём
    для picture/-префикса). На неверное имя принтер молчит до таймаута, поэтому кандидатов
    немного и без повторов (задача 4c6cd9e8)."""
    v = (url or "").strip()
    if not v:
        return []
    if v.startswith(("http://", "https://")):
        return [v]
    out = [_timelapse_download_url(v, ep), _timelapse_abs_url(v, ep)]
    return [u for i, u in enumerate(out) if u and u not in out[:i]]


async def _download_timelapse(url: str, rid: int, ep: elegoo.Endpoint | None = None) -> Path | None:
    """Скачать видео таймлапса в data/timelapses/<rid>.mp4. None — если видео нет или не отдалось."""
    if not _timelapse_has_video(url):
        log.info("таймлапс: у задания нет записи видео")
        return None
    cands = _timelapse_candidate_urls(url, ep)
    if not cands:
        log.info("таймлапс: не из чего построить URL (нет IP/кода принтера?)")
        return None
    for cand in cands:
        got = await _download_to(cand, _TIMELAPSE_DIR / f"{rid}.mp4")
        if got:
            return got
    return None


# ── автовыгрузка таймлапса сразу после печати (OpenTodo c084f065) ──
# Принтер хранит таймлапсы у себя и может их затереть; качаем на ПК, как только печать
# завершилась. Кэш по имени задания; attach-флоу берёт файл отсюда, если он есть.
_TIMELAPSE_AUTO_DIR = _TIMELAPSE_DIR / "auto"


def _safe_job_name(name: str) -> str:
    # Имя файла авто-кэша таймлапса. Каноникализируем через elegoo.normalize_job_name, чтобы один и
    # тот же ролик писался и читался по ОДНОМУ ключу из всех источников имени (label записи журнала,
    # task_name истории принтера, job_name привязки) — иначе видео, выгруженное под одним написанием
    # имени, не находилось под другим (кейс «Овечка Тимми», 2bc57e3e). Затем — ФС-безопасно.
    #
    return re.sub(r"[^\w\-.]+", "_", elegoo.normalize_job_name(name))[:80] or "job"


def _timelapse_dir(printer_id: int | None = None) -> Path:
    """Папка авто-кэша роликов. Своя ПОДПАПКА на машину (цикл 3, волна 5).

    Две машины с `plate_1.gcode` — обычное дело: слайсер называет пластины одинаково, и без
    разделения они перезаписали бы видео друг друга, а ролик уходит клиенту в результат заказа.

    Именно подпапка, а не префикс в имени: префикс вида `p1_` столкнулся бы с настоящим
    заданием, которое так и называется. Ролики, скачанные до этой правки, остаются в корне и
    продолжают находиться — см. фолбэк ниже."""
    return (_TIMELAPSE_AUTO_DIR / f"p{int(printer_id)}") if printer_id else _TIMELAPSE_AUTO_DIR


def cached_timelapse_path(job_name: str, printer_id: int | None = None) -> Path | None:
    """Путь к готовому ролику. Сначала ролик СВОЕЙ машины, потом общий (снятый до волны 5)."""
    nm = _safe_job_name(job_name)
    if printer_id:
        p = _timelapse_dir(printer_id) / f"{nm}.mp4"
        if p.exists() and p.stat().st_size > 0:
            return p
    p = _TIMELAPSE_AUTO_DIR / f"{nm}.mp4"
    return p if (p.exists() and p.stat().st_size > 0) else None


def timelapse_dest(job_name: str, printer_id: int | None = None) -> Path:
    """Куда КЛАСТЬ ролик этого задания. Пара к `cached_timelapse_path`, которая его ищет.

    Нужна драйверам, качающим не по HTTP: у Bambu файл лежит на FTPS принтера, и общий
    `cache_timelapse_from_url` ему не подходит — а имя и папка обязаны быть теми же, иначе свип
    скачает ролик и не найдёт его на следующем проходе."""
    d = _timelapse_dir(printer_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe_job_name(job_name)}.mp4"


async def cache_timelapse_from_url(job_name: str, url: str,
                                   ep: elegoo.Endpoint | None = None,
                                   printer_id: int | None = None) -> Path | None:
    """Скачать таймлапс задания в авто-кэш по ГОТОВОМУ url из истории принтера. Один заход,
    без ретраев и без сна — ретраит вызывающий (свип printwatch._tick_timelapse_sweep, он же
    заменил одноразовую фоновую задачу, погибавшую при рестарте бота — кейс «Овечка Тимми»,
    OpenTodo 2bc57e3e). Уже скачан → путь сразу."""
    if not job_name or not url:
        return None
    cached = cached_timelapse_path(job_name, printer_id)
    if cached:
        return cached
    dest = _timelapse_dir(printer_id) / f"{_safe_job_name(job_name)}.mp4"
    got = None
    # короткий таймаут: это фоновая авто-докачка свипа. Настоящий таймлапс (несколько МБ по
    # локалке) качается за секунды; если принтер не отдаёт за 30 с — файла под таким именем нет
    # (на неверное имя он молчит вечно) либо он битый/занят, и держать соединение открытым дольше
    # значит мешать камере (кейс 22.07). Ручная привязка мастером
    # (_attach_and_deliver_timelapse) остаётся на полном таймауте — там ждёт человек.
    for cand in _timelapse_candidate_urls(url, ep):
        try:
            got = await _download_to(cand, dest, timeout=30.0)
        except Exception:  # noqa: BLE001
            log.debug("timelapse download failed", exc_info=True)
            return None
        if got:
            break
    if got:
        log.info("таймлапс «%s» выгружен на ПК: %s", job_name, got)
    return got


async def _heal_journal_timelapse(entry: dict) -> str:
    """Живой путь таймлапса записи журнала: поле записи, а если оно пустое или файл удалён —
    авто-кэш по имени задания (label). Найденное в кэше сразу чиним в записи, чтобы дальше не
    искать. Пусто — видео нет локально. Единая точка для журнала, медиа заказа и доставки
    клиенту (OpenTodo df00e8ff, 2bc57e3e)."""
    # Мастер удалил таймлапс (1faab6cb): запись «вылечивать» нельзя, иначе видео воскреснет
    # из авто-кэша при первом же показе журнала/заказа.
    if int(entry.get("tl_deleted") or 0):
        return ""
    tl = entry.get("timelapse")
    if tl and Path(tl).exists():
        return str(tl)
    # Ролик лежит в подпапке СВОЕЙ машины (волна 5). Спрашивать без printer_id значит искать
    # только в корне — там остались лишь ролики, снятые до разделения.
    cached = cached_timelapse_path(entry.get("label") or "", entry.get("printer_id"))
    if cached:
        entry["timelapse"] = str(cached)
        await db.set_print_journal_fields(entry["id"], timelapse=str(cached))
        return str(cached)
    return ""


async def deliver_late_timelapses(entries: list[dict]) -> None:
    """Дослать клиенту таймлапсы, которые СВИП привязал к записям журнала УЖЕ подтверждённого
    («напечатано») заказа. Кейс «Овечка Тимми» (2bc57e3e): заказ подтверждён и альбом клиенту ушёл
    ЕЩЁ ДО того, как принтер дорисовал видео (он рендерит их минуты-часы после финиша), поэтому в
    Telegram видео не попало. В приложении они появляются сами (карточка заказа лечит таймлапс из
    авто-кэша), но проактивно дослать в чат — то, чего ждал владелец.

    Идемпотентно: свип привязывает видео к записи РОВНО ОДИН РАЗ (записи с готовым видео он далее
    пропускает), поэтому дослыл не повторяется. Группируем по заказу → один альбом на заказ, без
    спама. Только клиентские заказы в статусе «напечатано»: незавершённым таймлапсы дошлёт сам
    confirm_print при подтверждении (а свой заказ мастеру дослать незачем)."""
    if _bot is None or not entries:
        return
    by_req: dict[int, list[Path]] = {}
    shots: dict[int, list[Path]] = {}
    for e in entries:
        rid, tl = e.get("request_id"), e.get("timelapse")
        if not (rid and tl and Path(tl).exists()):
            continue
        by_req.setdefault(int(rid), []).append(Path(tl))
        # Скрин берём ТОЛЬКО у той печати, чьё видео дослылаем сейчас: печатей у заказа
        # бывает много, и общий сбор по заказу слал бы уже отправленные фото повторно.
        ph = e.get("done_photo")
        if ph and Path(ph).exists() and float(e.get("fail_pct") or 0) < 100:
            shots.setdefault(int(rid), []).append(Path(ph))
    for rid, vids in by_req.items():
        try:
            r = await db.get_request(rid)
            if (not r or r.status != "printed" or db.is_self_order(r)
                    or not getattr(r, "user_id", None)):
                continue
            # одно видео — сообщением, несколько — альбомом; скрины стола — только тех
            # печатей, чьи видео едут этим же дослылом (71523f3f)
            await _deliver_timelapses(r, vids, shots.get(rid, []))
        except Exception:  # noqa: BLE001
            log.debug("late timelapse deliver failed", exc_info=True)


async def _deliver_timelapses(r, vids: list[Path], photos: list[Path] | None = None) -> None:
    """Отправить клиенту таймлапсы печатей его заказа: одно видео — как раньше, несколько —
    альбомом (медиагруппой), не влезшие в лимит Bot API — сообщением со ссылкой в мини-апп
    (там всё отдаётся из карточки заказа). Скрины стола (photos) едут В ТОМ ЖЕ альбоме
    (Bot API позволяет мешать фото и видео): видео приоритетнее, фото добираются остатком
    до лимита 10; без видео фото не шлём (вне задачи 71523f3f)."""
    if _bot is None or not vids:
        return
    from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo
    cap = texts.user_timelapse_caption(r, len(vids))
    small = [p for p in vids if _file_size(p) <= _TG_UPLOAD_LIMIT]
    # фото — только вдогонку к отправляемым видео и только в свободные слоты альбома
    photos = list(photos or [])[:max(0, 10 - len(small[:10]))] if small else []
    try:
        if len(small) == 1 and not photos:
            await _bot.send_video(r.user_id, FSInputFile(str(small[0])), caption=cap)
        elif small:
            items = [(InputMediaVideo, p) for p in small[:10]]  # лимит альбома Bot API — 10
            items += [(InputMediaPhoto, p) for p in photos]
            media = [cls(media=FSInputFile(str(p)), caption=cap if i == 0 else None)
                     for i, (cls, p) in enumerate(items)]       # подпись — на первом элементе
            await _bot.send_media_group(r.user_id, media)
        if len(small) < len(vids) or len(small) > 10:       # большие/лишние — ссылкой в мини-аппу
            kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders", oid=r.id)
            await notify.notify_user(
                _bot, r.user_id,
                cap + ("\n\nВидео большое — посмотрите его в приложении." if not small
                       else "\n\nЧасть видео не влезла в Telegram — все таймлапсы в приложении, "
                            "в карточке заказа."),
                reply_markup=kb)
    except Exception:  # noqa: BLE001
        log.debug("timelapse deliver failed", exc_info=True)


async def _deliver_timelapse(r, dest: Path) -> None:
    """Одиночная доставка (ручная привязка печати из истории принтера)."""
    await _deliver_timelapses(r, [dest])
