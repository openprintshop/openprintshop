"""Сборщик очереди досчёта цены (Э0, docs/Перспективные разработки/vps-relay-2026-10-04.md §1).

Заказ без цены — уже не «связь в памяти токена», а строка deferred_calc в БД (логика очереди —
app/store/deferred.py). Этот модуль её исполняет: берёт созревшие задания по одному, достаёт
файлы модели из источника, считает цену тем же calcservice.compute, что и мини-аппа, и
применяет результат одной транзакцией. Клиенту — «✅ Заказ #N посчитан: X. Подтвердите цену»
с кнопками, мастеру — «💰 досчитан». Работает при любом режиме бота (ROLE/TG_SOURCE): ему
неважно, откуда пришёл заказ.

Откуда задания:
* order_now (мини-аппа, «Оставить заказ» во время слайса) — источник pc_model, задание
  придержано на время живого расчёта (LIVE_HOLD_SEC); рестарт процесса придержку снимает;
* приёмная на VPS (Э1), пока ПК был выключен, — import_relay_order: ссылка, файл Telegram
  (file_id), модель каталога, своя модель, «повторить заказ»; выбор клиента — дефолты
  быстрого расчёта (quick=true).

Правила исполнения:
* ОДНО тяжёлое задание за раз (замок `heavy`): расчёт — это меш в памяти и рендер превью,
  два параллельно на большом наборе — это своп. Всё тяжёлое — внутри calcservice/cpupool, как
  у живого расчёта: event loop не блокируется;
* ошибка, которую повтор не исправит (модель битая/не найдена, CalcError, файл больше лимита
  Telegram), — сразу к мастеру; всё прочее (сеть, 418 MakerWorld, getFile, нехватка памяти)
  — повтор с экспонентой, после MAX_ATTEMPTS — тоже к мастеру;
* уведомления — только после commit и только тому, кому apply_deferred_result вернул True;
* упала сама store-операция (занятая БД, I/O) — задание не висит 'running' до рестарта:
  release_claim возвращает его в очередь с паузой, а не вышло и это — loop() возвращает
  сирот на следующем проходе (review:e0#1);
* выбор клиента в задании — полный (многоцвет, столы, пластик на стол, масштаб, срочность,
  опции, промо) и разбирается теми же функциями clientflow, что у живого /api/calc: цена
  очереди не должна расходиться с ценой живого расчёта (review:e0#0).

Модульного `_bot` и set_bot здесь нет намеренно: бота сборщику передаёт run.py в loop(bot),
а прибор tests/test_bot_wiring.py считает держателем бота каждый модуль с `_bot`+set_bot и
требует его в run.wire_bot — правка которого в этот этап не входит. До старта loop()
(тесты, приём события раньше сборщика) берём бота у notify — его ставит wire_bot.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from . import config, db, keyboards, notify, texts

log = logging.getLogger(__name__)

POLL_SEC = 30.0            # раз в столько сборщик смотрит в очередь и без wake()
# Сколько задание order_now ждёт живой расчёт мини-аппы. Живой хвост обычно приходит за
# секунды–минуты (fastcalc; гигант — фоновый точный замер). Если он так и не пришёл (клиент
# пересчитал с другими параметрами, вкладку закрыли посреди слайса) — сборщик досчитает сам.
LIVE_HOLD_SEC = 15 * 60

_runner_bot = None         # бот, переданный в loop(); см. докстринг — не `_bot` намеренно


class JobFailed(Exception):
    """Повтор не поможет: модели нет, источник невалиден, параметры заказа больше не
    существуют. Текст — честный и для клиента (без внутренностей), он уходит ему в причине."""


# ---- примитивы, привязанные к текущему циклу событий ----
# Модульные asyncio.Lock/Event прибились бы к первому циклу, где их ждали, а у pytest цикл
# свой на каждый тест. Держим их по циклу: в проде цикл один, разницы нет.
_prims: dict[str, tuple[object, object]] = {}


def _prim(name: str, factory):
    loop = asyncio.get_running_loop()
    cur = _prims.get(name)
    if cur is None or cur[0] is not loop:
        cur = (loop, factory())
        _prims[name] = cur
    return cur[1]


def _bot():
    return _runner_bot if _runner_bot is not None else notify._bot


def wake() -> None:
    """Разбудить сборщик сейчас, не дожидаясь POLL_SEC (новое задание, снятая придержка)."""
    cur = _prims.get("wake")
    if cur is not None:
        try:
            cur[0].call_soon_threadsafe(cur[1].set)
        except RuntimeError:       # цикл уже закрыт (тест кончился) — будить некого
            pass


async def release_live(rid: int) -> None:
    """Живой расчёт мини-аппы отказался от заказа (слайс упал / клиент пересчитал с другими
    параметрами): задание order_now созревает сразу. Никогда не роняет вызывающего — он
    отвечает клиенту на /api/calc, и сбой очереди не должен превращаться в ошибку сметы."""
    try:
        if await db.release_deferred_hold(int(rid)):
            wake()
    except Exception:  # noqa: BLE001
        log.warning("очередь досчёта: не снял придержку заказа #%s", rid, exc_info=True)


# ---- что кладём в задание ----
def pc_model_source(paths, label: str) -> dict:
    """Источник «модель уже на диске ПК» (order_now). Пути — относительно DATA_DIR, как всё в
    БД (C2): база переедет с каталогом данных — задание останется валидным."""
    return {"type": "pc_model", "paths": [config.rel_data(str(p)) for p in (paths or [])],
            "label": str(label or "")[:200]}


def params_from_choice(*, printer, plastic, quality, strength, color: str = "", qty: int = 1,
                       scale_pct=None, urgency=None, options=None, promo=None,
                       comment: str = "", multicolor=None, colors: int = 0, colors_sel=None,
                       plates_sel=None, plate_plastics=None, plate_colors=None,
                       model: dict | None = None) -> dict:
    """Выбор клиента ИДЕНТИФИКАТОРАМИ (имена пластиков и пресетов мастер переименует, id — нет).

    Задание обязано нести ВЕСЬ выбор, который считал бы живой /api/calc (review:e0#0):
    многоцвет и слоты цветов, число цветов STL (colors), выбранные столы (plates_sel, 0-базные),
    свой пластик на стол ({стол: plastic_id}), цвета столов, масштаб, срочность, опции, промо.
    model — снимок флагов модели с токена мини-аппы (multicolor/num_colors/mixed_plates/plates):
    по ним живой расчёт решает надбавку за многоцвет, и сборщик решает так же, а не по
    повторной инспекции (у STL-набора с цветами в именах файлов она многоцвета не видит).

    multicolor=None — старый вызов: многоцвет восстанавливается из строки цвета «Белый -
    Красный» (так её склеивает и /api/calc, и мастер в боте)."""
    color = (color or "").strip()
    if multicolor is None:
        sel = [c.strip() for c in color.split(" - ") if c.strip()] if " - " in color else []
        mc = len(sel) > 1
    else:
        mc = bool(multicolor)
        sel = [str(c).strip()[:60] for c in (colors_sel or []) if str(c or "").strip()][:8] if mc else []
    out = {"printer_id": getattr(printer, "id", None), "plastic_id": getattr(plastic, "id", None),
           "quality_id": getattr(quality, "id", None), "strength_id": getattr(strength, "id", None),
           "color": color, "colors_sel": sel, "multicolor": mc,
           "colors": max(0, min(8, int(colors or 0))),
           "qty": max(1, min(1000, int(qty or 1))),
           "scale_pct": max(10, min(1000, int(scale_pct or 100))),
           "urgency": "rush" if urgency == "rush" else "normal",
           "options": [o for o in (options or []) if isinstance(o, dict)],
           "promo": str(promo or "")[:40], "comment": str(comment or "")[:500], "quick": False,
           "plates_sel": sorted({int(i) for i in (plates_sel or [])}),
           "plate_plastics": {str(int(i)): int(getattr(p, "id", p)) for i, p in (plate_plastics or {}).items()},
           "plate_colors": {str(int(i)): str(c)[:60] for i, c in (plate_colors or {}).items() if c}}
    if model:
        out["model"] = {"multicolor": bool(model.get("multicolor")),
                        "num_colors": max(1, int(model.get("num_colors") or 1)),
                        "mixed_plates": bool(model.get("mixed_plates", True)),
                        "plates": max(1, int(model.get("plates") or 1))}
    return out


# ---- сборщик ----
async def loop(bot) -> None:
    """Фоновая задача run.py: при старте — сироты 'running' назад в очередь (и снятие придержек
    живого расчёта, который умер вместе с прошлым процессом); дальше — разбор очереди раз в
    POLL_SEC и по wake()."""
    global _runner_bot
    _runner_bot = bot
    ev = _prim("wake", asyncio.Event)
    try:
        n = await db.requeue_running()
        if n:
            log.info("очередь досчёта: %d заданий прерванного процесса возвращены в очередь", n)
    except Exception:  # noqa: BLE001
        log.warning("очередь досчёта: возврат 'running' при старте не удался", exc_info=True)
    orphans = False
    while True:
        ev.clear()
        try:
            if orphans:
                # Прошлый проход упал мимо release_claim (БД лежала и на возврате задания,
                # или упал сам захват после UPDATE). Между проходами этот процесс не исполняет
                # НИЧЕГО (сборщик один, задания строго по одному), значит всё 'running' сейчас —
                # наши сироты: назад в очередь, как при старте. Придержки order_now не трогаем —
                # их живой расчёт жив (review:e0#1).
                await db.requeue_running(release_holds=False)
                orphans = False
            await drain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сборщик не имеет права умереть
            log.exception("очередь досчёта: сбой разбора очереди")
            orphans = True
        try:
            await asyncio.wait_for(ev.wait(), POLL_SEC)
        except TimeoutError:
            pass


async def drain() -> int:
    """Разобрать все созревшие задания по одному. Возвращает число взятых в работу.

    Исполнение задания, упавшее исключением мимо классификации (сама store-операция: apply/
    retry/fail_deferred на занятой БД, I/O), не оставляет задание 'running' до рестарта
    (review:e0#1): оно возвращается в очередь с паузой (release_claim), а проход прерывается —
    при лежащей БД гнать очередь вхолостую незачем. Не вернулось и это — исключение уходит в
    loop(), и тот вернёт сирот на следующем проходе."""
    n = 0
    while True:
        job = await db.claim_next_deferred()
        if job is None:
            return n
        n += 1
        try:
            await run_one(job)
        except asyncio.CancelledError:
            raise       # остановка процесса: 'running' вернёт в очередь следующий старт
        except Exception as exc:  # noqa: BLE001
            text = f"сбой исполнения: {str(exc) or exc.__class__.__name__}"
            log.exception("очередь досчёта: заказ #%s — %s, задание вернётся в очередь", job["rid"], text)
            await db.release_claim(job["id"], job["claim_token"], text)
            return n


async def run_one(job: dict) -> str:
    """Исполнить захваченное задание. Исход: 'done' | 'retry' | 'failed' | 'lost' (заказ
    ушёл, пока считали, — молчим)."""
    async with _prim("heavy", asyncio.Lock):
        return await _run_one(job)


async def _run_one(job: dict) -> str:
    attempts = int(job.get("attempts") or 0)
    if attempts > db.deferred.MAX_ATTEMPTS:
        # Захватов больше, чем попыток: процесс умирал ПОСРЕДИ расчёта этой модели (рестарт
        # возвращает 'running' в очередь, ошибки не бывает) — дальше пробовать значит ронять ПК.
        return await _give_up(job, f"расчёт прерывался {attempts - 1} раз — похоже, модель "
                                   "слишком тяжела для автоматического расчёта", "")
    try:
        result, r = await _compute(job)
        prefs = await db.get_user_prefs(r.user_id)
        # Тот же смысл, что ordermoney._apply_price_or_ask: при прежней цене 0 автоприём бывает
        # ТОЛЬКО у клиента с «принимать любые изменения» (price_auto_accept не считает порог в
        # процентах от нуля) — кто автоприём не включал, первую цену подтверждает всегда.
        auto = (not db.is_self_order(r)) and db.price_auto_accept(prefs, r.price or 0.0, result["price"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — классификация ниже
        return await _on_error(job, exc)
    if not await db.apply_deferred_result(job["id"], job["claim_token"], result, auto_accept=auto):
        log.info("очередь досчёта: заказ #%s ушёл, пока шёл расчёт — результат не применён", job["rid"])
        return "lost"
    await _notify_applied(int(job["rid"]), result, auto)
    return "done"


def _is_permanent(exc: Exception) -> bool:
    from . import calcservice, downloader
    if isinstance(exc, (JobFailed, calcservice.CalcError, downloader.ModelFileError)):
        return True
    # getFile на файле больше лимита: Telegram скажет «file is too big» сколько ни проси
    return "file is too big" in str(exc).lower()


async def _on_error(job: dict, exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    if not _is_permanent(exc):
        if await db.retry_deferred(job["id"], job["claim_token"], text):
            log.warning("очередь досчёта: заказ #%s — временный сбой (%s), повтор позже",
                        job["rid"], text, exc_info=not isinstance(exc, OSError))
            return "retry"
        text = f"{text} (попыток: {int(job.get('attempts') or 0)})"
    log.warning("очередь досчёта: заказ #%s не посчитан: %s", job["rid"], text)
    client_reason = str(exc) if isinstance(exc, JobFailed) else ""
    from . import downloader
    if isinstance(exc, downloader.ModelFileError):
        client_reason = str(exc)       # наши честные тексты про формат/лимит — их можно клиенту
    return await _give_up(job, text, client_reason)


async def _give_up(job: dict, error: str, client_reason: str) -> str:
    if not await db.fail_deferred(job["id"], job["claim_token"], error):
        return "lost"
    bot = _bot()
    if bot is None:
        return "failed"
    rid = int(job["rid"])
    try:
        r = await db.get_request(rid)
        if r is not None and not db.is_self_order(r):
            await notify.notify_user(bot, r.user_id, texts.deferred_client_failed(rid, client_reason),
                                     reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders"))
        await notify.notify_admins(
            bot, texts.deferred_admin_failed(rid, error),
            reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                            text="Открыть заказ"))
    except Exception:  # noqa: BLE001
        log.warning("очередь досчёта: уведомление об отказе по #%s не ушло", rid, exc_info=True)
    return "failed"


async def _notify_applied(rid: int, result: dict, auto: bool) -> None:
    """Цена применена (commit уже сделан, применили именно мы): клиенту — на подтверждение
    (или «принято автоматически»), мастеру — «досчитан». Сбой отправки цену не откатывает:
    заказ уже в «Мои заказы» с кнопками, сообщение — лишь доставка."""
    from . import delivery
    if auto:
        try:            # цена сразу боевая → порог «бесплатно от суммы» под неё (как в автоприёме)
            await delivery.recalc_group_delivery(rid)
        except Exception:  # noqa: BLE001
            log.debug("deferred: пересчёт доставки не удался", exc_info=True)
    bot = _bot()
    if bot is None:
        return
    cur = config.CURRENCY
    price = float(result["price"])
    try:
        r = await db.get_request(rid)
        if r is not None and not db.is_self_order(r):
            if auto:
                text = texts.deferred_price_auto(r, price, cur)
                kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders")
            else:
                text = texts.deferred_price_ready(r, price, cur)
                kb = keyboards.price_confirm(rid, price, webapp_url=config.WEBAPP_PUBLIC_URL,
                                             accept_text="✅ Принять цену")
            pv = r.preview_path if r.preview_path and Path(r.preview_path).exists() else None
            sent = await notify.notify_user(bot, r.user_id, text, reply_markup=kb, photo=pv)
            if not sent and pv:   # фото не ушло (битый файл) — цена важнее картинки
                await notify.notify_user(bot, r.user_id, text, reply_markup=kb)
        await notify.notify_admins(
            bot, texts.deferred_admin_ready(rid, price, float(result.get("grams_total") or 0), cur, auto=auto),
            reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                            text="Открыть заказ"))
    except Exception:  # noqa: BLE001
        log.warning("очередь досчёта: уведомление о цене по #%s не ушло", rid, exc_info=True)


# ---- расчёт ----
def _existing(paths) -> list[str]:
    out = []
    for p in paths or []:
        try:
            ap = config.abs_data(p)
            if ap.exists():
                out.append(str(ap))
        except (OSError, TypeError, ValueError):
            continue
    return out


async def _materialize(src: dict, uid: int) -> tuple[list[str], str, bool]:
    """Источник задания → (файлы модели на диске, подпись, подпись «настоящая» — её можно
    записать в заказ вместо временной из события приёмной)."""
    typ = (src or {}).get("type")
    if typ == "pc_model":
        paths = _existing(src.get("paths"))
        if not paths:
            raise JobFailed("Файлы модели не найдены на компьютере мастерской.")
        return paths, str(src.get("label") or "Модель"), False
    if typ == "url":
        from . import downloader
        from .web import clientflow
        url = str(src.get("url") or "").strip()
        if not downloader.is_http_url(url):
            raise JobFailed("Ссылка на модель не распознана.")
        paths, label = await clientflow._quick_link_download(url)
        if not paths:
            raise JobFailed("По ссылке не нашлось файлов модели.")
        return paths, label or downloader.label_from_url(url), True
    if typ == "tg_file":
        from . import downloader
        size = src.get("file_size")
        if size is not None and int(size or 0) > downloader.TG_FILE_LIMIT:
            raise JobFailed(downloader.TG_TOO_BIG_MSG)
        bot = _bot()
        if bot is None:
            raise RuntimeError("бот ещё не поднят — файл из Telegram скачать нечем")
        meshes, name = await downloader.download_tg_file(
            bot, str(src.get("file_id") or ""), file_name=src.get("file_name"),
            file_size=size, unique_id=src.get("file_unique_id"))
        return [str(p) for p in meshes], Path(name).stem or "Модель", True
    if typ == "relay_file":
        # Файл, принятый приёмной из мини-аппа, пока ПК был выключен (Э2): скачиваем по
        # номеру подписанным запросом и разбираем тем же путём, что загрузку из мини-аппа
        # (zip, STEP, сетки). Файла у приёмной уже нет — повтор не поможет, это к мастеру;
        # сеть — обычный повтор очереди.
        from . import downloader, relaylink
        try:
            data = await relaylink.fetch_relay_file(str(src.get("ref") or ""), size=src.get("file_size"),
                                                    sha256=src.get("sha256") or None)
        except relaylink.RelayError as exc:
            if exc.kind in ("gone", "bad", "config", "auth"):
                raise JobFailed("Файл модели из заявки недоступен — пришлите его, пожалуйста, ещё раз.") from exc
            raise
        meshes, name = await downloader.save_web_upload(
            downloader.safe_upload_name(src.get("file_name"), "model.stl"), data)
        return [str(p) for p in meshes], Path(name).stem or "Модель", True
    if typ == "collection":
        it = await db.get_collection_item(int(src.get("id") or 0))
        if it is None:
            raise JobFailed("Модель из каталога больше недоступна.")
        paths = _existing(it.paths)
        if not paths:
            raise JobFailed("Файлы модели из каталога недоступны.")
        return paths, it.label, True
    if typ == "saved_model":
        sm = await db.get_saved_model(int(src.get("id") or 0))
        if sm is None or int(sm.user_id or 0) != int(uid):
            raise JobFailed("Сохранённая модель не найдена.")
        paths = _existing(sm.paths)
        if not paths:
            raise JobFailed("Файлы сохранённой модели недоступны.")
        return paths, sm.label, True
    if typ == "request":
        # «Повторить заказ» — как /api/use kind=order: модель клиента по хэшу заказа, запасной
        # путь — файл самого заказа
        req = await db.get_request(int(src.get("id") or 0))
        if req is None or int(req.user_id or 0) != int(uid):
            raise JobFailed("Заказ для повтора не найден.")
        sm = await db.get_saved_model_by_hash(uid, req.hash) if req.hash else None
        paths = _existing(sm.paths if sm else [req.file_path])
        if not paths:
            raise JobFailed("Файлы модели повторяемого заказа недоступны.")
        return paths, (sm.label if sm else req.model_label), True
    raise JobFailed("Неизвестный источник модели.")


async def _choice(params: dict, info) -> dict:
    """Параметры задания → объекты для calcservice.compute. quick=true — дефолты быстрого
    расчёта, выбранные ЗДЕСЬ, в момент расчёта (общий помощник с быстрой сметой по ссылке)."""
    from . import pricing
    from .web import clientflow
    # Флаги модели — со снимка токена мини-аппы (order_now), иначе — из инспекции: так же, как
    # их видел живой расчёт (у STL-набора с цветами в именах регистрация ставит многоцвет сама).
    mdl = params.get("model") if isinstance(params.get("model"), dict) else {}
    is_mc = bool(mdl["multicolor"]) if "multicolor" in mdl else bool(getattr(info, "is_multicolor", False))
    num_colors = int(mdl.get("num_colors") or getattr(info, "num_colors", 1) or 1)
    mixed = bool(mdl["mixed_plates"]) if "mixed_plates" in mdl else bool(getattr(info, "mixed_plates", True))
    nplates = int(mdl.get("plates") or getattr(info, "plates", 1) or 1)
    settings = await db.get_settings()
    if params.get("quick"):
        defaults = await clientflow._quick_calc_defaults()
        if defaults is None:
            raise JobFailed("В мастерской не настроены пластики или пресеты печати.")
        printer, plastic, quality, strength = defaults
        return {"printer": printer, "plastic": plastic, "quality": quality, "strength": strength,
                "color": (plastic.color_list or [""])[0], "colors_sel": [], "qty": 1,
                "multicolor": is_mc, "extra_colors": max(0, num_colors - 1) if is_mc and mixed else 0,
                "model_mc": is_mc, "plates": nplates, "plates_sel": None, "plate_plastics": {},
                "plate_colors": {}, "scale_pct": 100, "urgency_factor": 1.0, "options": [],
                "promo_pct": 0.0}
    pid = params.get("printer_id")
    printer = (await db.get_printer(int(pid))) if pid else await db.get_default_printer()
    plastic = await db.get_plastic(int(params.get("plastic_id") or 0))
    quality = await db.get_quality(int(params.get("quality_id") or 0))
    strength = await db.get_strength(int(params.get("strength_id") or 0))
    if not (printer and plastic and quality and strength):
        raise JobFailed("Выбранные для заказа принтер, пластик или пресет печати больше недоступны.")
    # Дальше — ТЕ ЖЕ функции разбора, что у /api/calc (review:e0#0): многоцвет и надбавка,
    # слоты цветов из палитры, столы, пластик и цвет на стол.
    sel_raw = [c for c in (params.get("colors_sel") or []) if isinstance(c, str)]
    # число цветов STL; у заданий до ревью его нет — тогда по числу слотов, как считалось раньше
    req_colors = int(params.get("colors") or 0) if "colors" in params else len(sel_raw)
    multicolor, extra = clientflow._multicolor_choice(
        is_mc, num_colors, mixed, bool(params.get("multicolor")), req_colors)
    colors_sel = clientflow._pick_colors_sel(sel_raw, plastic) if multicolor else []
    color = str(params.get("color") or "").strip()
    if colors_sel:
        color = " - ".join(colors_sel)
    plates_sel = clientflow._parse_plates_sel(params.get("plates_sel"), nplates)
    plate_plastics = await clientflow._resolve_plate_plastics(params.get("plate_plastics"), nplates, plastic)
    plate_colors = clientflow._valid_plate_colors(params.get("plate_colors"), nplates, plastic,
                                                  plate_plastics)
    promo = str(params.get("promo") or "").strip().upper()
    promo_pct = (await db.validate_promo(promo) or 0.0) if promo else 0.0
    return {"printer": printer, "plastic": plastic, "quality": quality, "strength": strength,
            "color": color, "colors_sel": colors_sel,
            "qty": max(1, min(1000, int(params.get("qty") or 1))),
            "multicolor": multicolor, "extra_colors": extra, "model_mc": is_mc,
            "plates": nplates, "plates_sel": plates_sel, "plate_plastics": plate_plastics,
            "plate_colors": plate_colors,
            "scale_pct": max(10, min(1000, int(params.get("scale_pct") or 100))),
            "urgency_factor": pricing.urgency_factor_from(params.get("urgency") or "normal", settings),
            "options": [o for o in (params.get("options") or []) if isinstance(o, dict)],
            "promo_pct": float(promo_pct or 0.0)}


async def _compute(job: dict):
    """Задание → (результат для apply_deferred_result, заказ). Тяжёлое — внутри calcservice
    и cpupool, как у живого расчёта мини-аппы."""
    from . import cache, calcservice, cpupool, inspect3mf, pricing
    from .orders import _embedded_thumb, _order_stock_plan
    from .web import clientflow
    r = await db.get_request(int(job["rid"]))
    if r is None:
        raise JobFailed("Заказ не найден.")
    src = job.get("source") or {}
    paths, label, real_label = await _materialize(src, r.user_id)
    info = await cpupool.run(inspect3mf.inspect_model, paths)
    ch = await _choice(job.get("params") or {}, info)
    printer, plastic, quality, strength = ch["printer"], ch["plastic"], ch["quality"], ch["strength"]
    res = await calcservice.compute(
        user_id=int(r.user_id), model_paths=paths, model_label=label, printer=printer,
        plastic=plastic, color=ch["color"], qty=ch["qty"], quality=quality, strength=strength,
        extra_colors=ch["extra_colors"], plates=ch["plates"],
        is_multicolor=ch["multicolor"], promo_pct=ch["promo_pct"], scale_pct=ch["scale_pct"],
        urgency_factor=ch["urgency_factor"], colors_sel=ch["colors_sel"],
        plates_only=ch["plates_sel"], plate_plastics=ch["plate_plastics"] or None)
    bd = res["bd"]
    try:
        nparts = int((res.get("dfm") or {}).get("n_parts") or 0) or None
    except (TypeError, ValueError):
        nparts = None
    if ch["options"]:
        pricing.apply_options(bd, ch["options"], nparts)     # B4: опции поверх цены, как в смете
    breakdown = pricing.breakdown_to_dict(bd, res["settings"])
    breakdown["scale_pct"] = int(res.get("scale_pct", 100) or 100)
    preview = res.get("preview_path") or await _embedded_thumb(paths)
    if not preview and r.preview_path and Path(r.preview_path).exists():
        preview = r.preview_path
    # Подпись материала и план склада — как у живого расчёта: разные пластики/цвета на столах
    # видны в заказе, а склад резервируется по каждому пластику своих столов.
    pdisp = res["pdisp"]
    if ch["plates"] > 1:
        sel = ch["plates_sel"] or list(range(ch["plates"]))
        pdisp = clientflow._plates_material_label(plastic, ch["color"], ch["plate_plastics"],
                                                  ch["plate_colors"], sel) or pdisp
    last = {"plastic_id": plastic.id, "plastic_name": pdisp, "grams_total": bd.grams_total,
            "color": ch["color"], "colors_sel": ch["colors_sel"],
            "color_grams": res.get("color_grams") or [],
            "plastic_grams": clientflow._plastic_grams_last(res, ch["plate_colors"], ch["color"])}
    result = {"price": bd.total, "grams_total": bd.grams_total, "minutes_total": bd.minutes_total,
              "breakdown": breakdown, "plastic_name": pdisp, "printer": printer.name,
              "quality": quality.label, "strength": strength.label, "quality_id": quality.id,
              "strength_id": strength.id, "quantity": ch["qty"], "dims": res.get("dims", "") or "",
              "preview_path": preview or None, "file_path": paths[0], "hash": cache.model_hash(paths),
              # столов в заказе — как у оформления из мини-аппы (orders.place_from_token):
              # выбранные, иначе — раскладка расчёта
              "plates": (len(ch["plates_sel"]) if ch["plates_sel"]
                         else int(res.get("plates") or ch["plates"])),
              # флаг МОДЕЛИ, как пишет его оформление из мини-аппы, а не «выбрал ли клиент
              # многоцвет»: иначе досчёт перетирал бы requests.multicolor заказа order_now
              "multicolor": int(bool(ch["model_mc"])), "stock_plan": _order_stock_plan(last)}
    if real_label and label:
        result["model_label"] = str(label)[:200]
    return result, r


# ---- приём заказа из приёмной (Э1) ----
_EVENT_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_SRC_TYPES = {"url", "tg_file", "relay_file", "collection", "saved_model", "request"}
_LEAD_SOURCE = {"url": "link", "tg_file": "file", "relay_file": "file", "collection": "catalog",
                "saved_model": "mine", "request": "repeat"}


def _clean_src(raw) -> dict:
    """Источник из события приёмной — недоверенные данные: только известные типы и поля,
    длины и числа проверены. Путей ПК отсюда не принимаем никогда (pc_model — только свой)."""
    if not isinstance(raw, dict) or raw.get("type") not in _SRC_TYPES:
        raise ValueError("deferred_order: неизвестный источник модели")
    typ = raw["type"]
    if typ == "url":
        url = str(raw.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")) or len(url) > 2000:
            raise ValueError("deferred_order: ссылка не http(s)")
        return {"type": "url", "url": url}
    if typ == "relay_file":
        # Файл модели, принятый приёмной из мини-аппа, пока ПК был выключен (Э2): ПК скачает
        # его подписанным запросом по номеру. Имя — только для подписи и расширения.
        ref = str(raw.get("ref") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", ref):
            raise ValueError("deferred_order: кривой номер файла приёмной")
        try:
            size = int(raw["file_size"]) if raw.get("file_size") is not None else None
        except (TypeError, ValueError):
            size = None
        sha = str(raw.get("sha256") or "").strip().lower()
        return {"type": "relay_file", "ref": ref, "file_name": str(raw.get("file_name") or "")[:255],
                "file_size": size, "sha256": sha if re.fullmatch(r"[0-9a-f]{64}", sha) else ""}
    if typ == "tg_file":
        fid = str(raw.get("file_id") or "").strip()
        if not fid or len(fid) > 256:
            raise ValueError("deferred_order: нет file_id")
        try:
            size = int(raw["file_size"]) if raw.get("file_size") is not None else None
        except (TypeError, ValueError):
            size = None
        return {"type": "tg_file", "file_id": fid,
                "file_unique_id": str(raw.get("file_unique_id") or "")[:128],
                "file_name": str(raw.get("file_name") or "")[:255], "file_size": size}
    try:
        oid = int(raw.get("id") or 0)
    except (TypeError, ValueError):
        oid = 0
    if oid <= 0:
        raise ValueError("deferred_order: нет id модели")
    return {"type": typ, "id": oid}


def _clean_params(raw) -> dict:
    """Выбор клиента из события (Э2: мини-аппа на VPS сможет его прислать). Нет его или он
    кривой — дефолты быстрого расчёта (как у ссылки, кинутой в чат)."""
    if not isinstance(raw, dict) or raw.get("quick", False) or not raw.get("plastic_id"):
        # Быстрый заказ (из бота или мини-аппа без ПК): выбор — дефолты, но пожелание клиента
        # («белый, 2 шт») — его единственный голос в заявке, его держим.
        comment = str(raw.get("comment") or "")[:500] if isinstance(raw, dict) else ""
        return {"quick": True, "comment": comment} if comment else {"quick": True}

    def _int(k):
        try:
            v = int(raw.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        return v if v > 0 else None
    color = str(raw.get("color") or "")[:120]
    sel = [str(c)[:60] for c in (raw.get("colors_sel") or []) if isinstance(c, str)][:8]
    return {"printer_id": _int("printer_id"), "plastic_id": _int("plastic_id"),
            "quality_id": _int("quality_id"), "strength_id": _int("strength_id"),
            "color": color, "colors_sel": sel, "multicolor": bool(raw.get("multicolor")) and len(sel) > 1,
            "qty": max(1, min(1000, _int("qty") or 1)),
            "scale_pct": max(10, min(1000, _int("scale_pct") or 100)),
            "urgency": "normal", "options": [], "promo": "",
            "comment": str(raw.get("comment") or "")[:500], "quick": False}


def _user_name(user: dict, uid: int) -> str:
    """Подпись клиента — та же формула, что у заказа из мини-аппы (orders.place_from_token)."""
    u = user if isinstance(user, dict) else {}
    un = str(u.get("username") or "").strip()[:64]
    if un:
        return "@" + un
    full = " ".join(str(u.get(k) or "").strip()[:64] for k in ("first_name", "last_name")
                    if str(u.get(k) or "").strip())
    return full or f"id {uid}"


async def _provisional_label(src: dict, uid: int) -> str:
    """Временная подпись заказа до расчёта: мастер в карточке сразу видит, ЧТО заказали.
    После расчёта её заменит настоящее имя модели (у ссылки — название с площадки)."""
    typ = src["type"]
    try:
        if typ == "url":
            from . import downloader
            return downloader.label_from_url(src["url"])
        if typ in ("tg_file", "relay_file"):
            from . import downloader
            fallback = "Файл из Telegram" if typ == "tg_file" else "Файл из приложения"
            return Path(downloader.safe_upload_name(src.get("file_name"), fallback)).stem
        if typ == "collection":
            it = await db.get_collection_item(src["id"])
            return it.label if it else f"Модель каталога #{src['id']}"
        if typ == "saved_model":
            sm = await db.get_saved_model(src["id"])
            return sm.label if sm and int(sm.user_id or 0) == uid else "Своя модель"
        if typ == "request":
            req = await db.get_request(src["id"])
            return req.model_label if req and int(req.user_id or 0) == uid else f"Повтор заказа #{src['id']}"
    except Exception:  # noqa: BLE001 — подпись не повод терять заказ
        log.debug("relay label failed", exc_info=True)
    return "Модель"


async def _display_choice(params: dict) -> dict:
    """Имена принтера/пластика/пресетов для карточки до расчёта (best-effort): при quick —
    сегодняшние дефолты быстрого расчёта. Авторитетный выбор делает сборщик в момент расчёта."""
    out = {"printer": "", "plastic_name": "", "quality": "", "strength": "",
           "quality_id": None, "strength_id": None, "quantity": int(params.get("qty") or 1)}
    try:
        if params.get("quick"):
            from .web import clientflow
            d = await clientflow._quick_calc_defaults()
            if d:
                printer, plastic, quality, strength = d
                out.update(printer=printer.name, plastic_name=plastic.name, quality=quality.label,
                           strength=strength.label, quality_id=quality.id, strength_id=strength.id,
                           quantity=1)
            return out
        printer = (await db.get_printer(params["printer_id"])) if params.get("printer_id") \
            else await db.get_default_printer()
        plastic = await db.get_plastic(params.get("plastic_id") or 0)
        quality = await db.get_quality(params.get("quality_id") or 0)
        strength = await db.get_strength(params.get("strength_id") or 0)
        color = params.get("color") or ""
        out.update(printer=getattr(printer, "name", "") or "",
                   plastic_name=(f"{plastic.name} · {color}" if plastic and color
                                 else getattr(plastic, "name", "") or ""),
                   quality=getattr(quality, "label", "") or "", strength=getattr(strength, "label", "") or "",
                   quality_id=getattr(quality, "id", None), strength_id=getattr(strength, "id", None))
    except Exception:  # noqa: BLE001
        log.debug("relay display choice failed", exc_info=True)
    return out


async def import_relay_order(event_id: str, payload: dict) -> int:
    """Заказ, принятый приёмной (VPS), пока ПК был выключен (событие deferred_order, Э1).

    Идемпотентно: ключ client_req_id='rly:<event_id>' — повторная доставка события (ack
    потерялся, приёмная передоставила) возвращает уже заведённый заказ и ничего не шлёт.
    Заказ и задание появляются ОДНИМ commit-ом; затем отметка relay_events_seen, доставка по
    умолчанию из профиля, мастеру — «🌙 принят приёмной», сборщику — wake(). Клиенту на этом
    шаге НЕ пишем: приёмная уже ответила ему «заявка принята», следующее его сообщение — цена.

    payload: {"offer_id", "uid", "chat_id", "user": {id, username, first_name, last_name},
              "src": {"type": url|tg_file|collection|saved_model|request, …}, "created_at",
              необязательно "params": {выбор клиента идентификаторами}}.
    Возвращает rid; 0 — событие уже обрабатывалось, а заказ по нему не найден (подтвердить
    приёмной и не создавать заново). ValueError — событие битое по схеме: повтор не поможет."""
    eid = str(event_id or "").strip()
    if not _EVENT_ID_RE.fullmatch(eid):
        raise ValueError("deferred_order: некорректный id события")
    if not isinstance(payload, dict):
        raise ValueError("deferred_order: тело события не объект")
    try:
        uid = int(payload.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid <= 0:
        raise ValueError("deferred_order: нет uid клиента")
    src = _clean_src(payload.get("src"))
    params = _clean_params(payload.get("params"))
    crid = "rly:" + eid
    # Замок: два одновременных приёма одного события (повтор пришёл, пока первый ещё пишет)
    # без него оба не нашли бы задание и завели бы по заказу.
    async with _prim("import", asyncio.Lock):
        job = await db.deferred_by_client_req(crid)
        if job is not None:
            await db.relay_event_seen_mark(eid)
            return int(job["rid"])
        if await db.relay_event_was_seen(eid):
            return 0
        user_name = _user_name(payload.get("user"), uid)
        label = (await _provisional_label(src, uid))[:200] or "Модель"
        disp = await _display_choice(params)
        note = texts.RELAY_ORDER_NOTE
        if src["type"] in ("tg_file", "relay_file") and src.get("file_name"):
            size = src.get("file_size")
            note += f"\n\n📎 Файл: {src['file_name'][:120]}" + (
                f" ({int(size) / 1048576:.1f} МБ)" if size else "")
        if params.get("comment"):
            note += f"\n\n💬 {params['comment']}"
        rid = await db.add_request(
            user_id=uid, user_name=user_name, model_label=label, printer=disp["printer"],
            plastic_name=disp["plastic_name"], quantity=disp["quantity"], quality=disp["quality"],
            strength=disp["strength"], grams_total=0.0, minutes_total=0.0, price=0.0,
            preview_path=None, src_url=(src["url"] if src["type"] == "url" else ""),
            quality_id=disp["quality_id"], strength_id=disp["strength_id"],
            description=note, unsliced=1, price_pending=1,
            source=_LEAD_SOURCE.get(src["type"], ""), commit=False)
        await db.enqueue_deferred(rid, source=src, params=params, client_req_id=crid)
        await db.relay_event_seen_mark(eid)
    # Дальше — побочные эффекты, которым нельзя ронять уже принятый заказ.
    try:
        from .web.orderdefaults import _apply_default_delivery
        await _apply_default_delivery(uid, rid, 0.0)   # цены нет — порог пересчитается с ценой
    except Exception:  # noqa: BLE001
        log.debug("relay order default delivery failed", exc_info=True)
    try:
        await db.log_event(uid, "оставил заказ через приёмную (ПК был выключен)", f"#{rid} «{label}»",
                           user_name=user_name)
    except Exception:  # noqa: BLE001
        log.debug("relay order log_event failed", exc_info=True)
    bot = _bot()
    if bot is not None:
        try:
            await notify.notify_admins(
                bot, texts.relay_order_admin_note(rid, label, user_name),
                audience=notify.ALL_STAFF,
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                                text="Открыть заказ"))
        except Exception:  # noqa: BLE001
            log.warning("relay order admin notify failed", exc_info=True)
    wake()
    log.info("очередь досчёта: заказ #%s принят из приёмной (событие %s, источник %s)",
             rid, eid, src["type"])
    return rid
