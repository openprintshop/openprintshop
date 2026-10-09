"""Мастер расчёта: модель → (принтер) → пластик → количество → качество → прочность → расчёт."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, FSInputFile, Message, ReplyKeyboardRemove

from .. import (
    calcservice,
    config,
    cpupool,
    db,
    delivery,
    downloader,
    inspect3mf,
    keyboards,
    notify,
    orders,
    payments,
    preview,
    pricing,
    quote,
    ratelimit,
    texts,
)
from ..bedspec import DEFAULT_BED, BedSpec
from ..states import Calc, Feedback, Idea, PrintStat
from ..ui import consume, render

log = logging.getLogger(__name__)
router = Router()


# ---------- запуск ----------
@router.callback_query(F.data == "m:calc")
async def start_calc(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(screen_id=cb.message.message_id)
    await state.set_state(Calc.model)
    has_saved = await db.count_saved_models(cb.from_user.id) > 0
    await render(cb.bot, state, cb.message.chat.id, texts.MODEL_PROMPT,
                 keyboards.model_step(has_saved, can_search=config.MODEL_SEARCH_ENABLED))
    await cb.answer()


# ---------- шаг: модель ----------
async def _drop_screen(bot, state: FSMContext, chat_id: int) -> None:
    data = await state.get_data()
    mid = data.get("screen_id")
    if mid:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:  # noqa: BLE001
            pass
    await state.update_data(screen_id=None)


# превью лёгкой модели успевает отрисоваться за это время — показываем фото и мастер под ним;
# тяжёлая модель (млн+ граней) не успевает → не блокируем мастер, фото дошлём, когда готово.
_PREVIEW_FG_TIMEOUT = 10


def _preview_caption(label: str, info) -> str:
    return (texts.PREVIEW_CAPTION.format(label=texts.esc(label))
            + texts.model_badges(info.plates, info.is_multicolor, info.num_colors))


async def _accept_model(bot, state: FSMContext, chat_id: int, meshes: list[Path], label: str,
                        src_url: str = "") -> None:
    paths = [str(p) for p in meshes]
    await state.update_data(model_paths=paths, model_label=label, multicolor=False,
                            extra_colors=0, preview_path=None, model_src_url=src_url)
    await render(bot, state, chat_id, "⏳ Анализирую модель…", None)

    # анализ .3mf (быстро): несколько столов / многоцветная печать
    info = await cpupool.run(inspect3mf.inspect_model, paths)
    await state.update_data(model_plates=info.plates, model_numcolors=info.num_colors,
                            model_is_multicolor=info.is_multicolor,
                            model_mixed_plates=bool(info.mixed_plates))

    # рендер превью отдельной задачей; ждём недолго — лёгкая успеет, тяжёлая уйдёт в фон.
    # Стол берём у принтера ПО УМОЛЧАНИЮ: на этом шаге клиент принтер ещё не выбирал
    # (_go_printer/choose_printer ставят printer_id позже), а стол решает дедуп наложенных
    # копий. Выберут принтер с другим столом — картинка пересоберётся сама: стол теперь
    # входит в ключ кэша превью.
    try:
        dprn = await db.get_default_printer()
        bed = (BedSpec.from_machine_profile(getattr(dprn, "machine_profile", None))
               if dprn else DEFAULT_BED)
    except Exception:  # noqa: BLE001 — принтер не прочитался: считаем как раньше
        bed = DEFAULT_BED
    render_task = asyncio.create_task(
        cpupool.run_render(preview.render_preview, [Path(p) for p in paths], bed=bed))
    png = None
    try:
        png = await asyncio.wait_for(asyncio.shield(render_task), timeout=_PREVIEW_FG_TIMEOUT)
    except Exception:  # noqa: BLE001  (TimeoutError тоже сюда — тяжёлая модель уходит в фон)
        png = None

    if png and Path(png).exists():
        # быстрый путь: фото, затем свежий экран мастера под ним
        await state.update_data(preview_path=str(png))
        await _drop_screen(bot, state, chat_id)
        try:
            await bot.send_photo(chat_id, FSInputFile(str(png)), caption=_preview_caption(label, info))
        except Exception:  # noqa: BLE001
            log.debug("send preview failed", exc_info=True)
    else:
        # тяжёлая модель: уходим в мастер сразу, превью дошлём фоном, когда дорисуется
        asyncio.create_task(_bg_finish_preview(bot, state, chat_id, paths, label, info, render_task))

    if info.plates >= 2:    # #03a5727e: отдельная картинка на каждый стол (встроенные в .3mf)
        try:
            thumbs = await asyncio.to_thread(preview.extract_plate_thumbnails, paths)
            for i, th in enumerate(thumbs[:8], 1):
                await bot.send_photo(chat_id, FSInputFile(str(th)), caption=f"🧩 Стол {i}")
        except Exception:  # noqa: BLE001
            log.debug("send plate thumbnails failed", exc_info=True)

    await _go_after_model(bot, state, chat_id)


async def _bg_finish_preview(bot, state: FSMContext, chat_id: int, paths: list[str],
                             label: str, info, render_task) -> None:
    """Дожидается фонового рендера и шлёт фото. FSM не пишем (гонка с кликами юзера),
    только проверяем актуальность модели; цветное превью результата рисуется отдельно."""
    try:
        png = await render_task
        if not (png and Path(png).exists()):
            return
        data = await state.get_data()
        if data.get("model_paths") != paths:   # юзер уже сменил модель — не шлём устаревшее
            return
        await bot.send_photo(chat_id, FSInputFile(str(png)), caption=_preview_caption(label, info))
    except Exception:  # noqa: BLE001
        log.debug("bg preview failed", exc_info=True)


async def _go_after_model(bot, state: FSMContext, chat_id: int) -> None:
    """Многоцветную модель — на выбор моно/многоцвет; иначе сразу к принтеру."""
    data = await state.get_data()
    if data.get("model_is_multicolor"):
        await state.set_state(Calc.coloring)
        await render(bot, state, chat_id,
                     texts.coloring_prompt(data.get("model_numcolors", 2), data.get("model_plates", 1)),
                     keyboards.coloring())
        return
    await _go_printer(bot, state, chat_id)


@router.callback_query(Calc.coloring, F.data.startswith("mc:"))
async def choose_coloring(cb: CallbackQuery, state: FSMContext) -> None:
    mode = cb.data.split(":")[1]
    data = await state.get_data()
    if mode == "multi":
        nc = int(data.get("model_numcolors", 2) or 2)
        # Цвета по разным столам (каждый стол одноцветный) — без продувки/смен цвета,
        # надбавку multicolor_* не берём (OpenTodo 0bcdd728).
        mixed = bool(data.get("model_mixed_plates", True))
        await state.update_data(multicolor=True,
                                extra_colors=max(1, nc - 1) if mixed else 0)
    else:
        await state.update_data(multicolor=False, extra_colors=0)
    await cb.answer()
    await _go_printer(cb.bot, state, cb.message.chat.id)


@router.message(Calc.model, F.document)
async def model_document(message: Message, state: FSMContext) -> None:
    try:
        meshes, label = await downloader.handle_upload(message.bot, message.document)
    except downloader.DownloadError as exc:
        await consume(message)
        await render(message.bot, state, message.chat.id,
                     f"⚠️ {texts.esc(str(exc))}\n\n{texts.MODEL_PROMPT}", keyboards.cancel_only())
        return
    await consume(message)
    await _accept_model(message.bot, state, message.chat.id, meshes, label)


@router.message(Calc.model, F.text)
async def model_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await consume(message)
    has_saved = await db.count_saved_models(message.from_user.id) > 0
    if downloader.is_http_url(text):
        await render(message.bot, state, message.chat.id, texts.MODEL_DOWNLOADING, None)
        try:                       # #14: несколько вариантов модели на MakerWorld → выбор
            mw = await downloader.fetch_makerworld_variants(text)
        except Exception:  # noqa: BLE001
            mw = None
        if mw and mw.get("variants"):
            await state.update_data(mw_url=text, mw_variants=mw["variants"])
            await state.set_state(Calc.variant)
            await render(message.bot, state, message.chat.id, texts.VARIANTS_PROMPT,
                         keyboards.mw_variants(mw["variants"]))
            return
        if mw and mw.get("single"):
            meshes, label = mw["single"]       # один вариант — уже скачан в одной сессии
        else:
            try:
                meshes, label = await downloader.fetch_from_url(text)
            except downloader.DownloadError as exc:
                await render(message.bot, state, message.chat.id,
                             f"⚠️ {texts.esc(str(exc))}\n\n{texts.MODEL_PROMPT}",
                             keyboards.model_step(has_saved, can_search=config.MODEL_SEARCH_ENABLED))
                return
        await _accept_model(message.bot, state, message.chat.id, meshes, label, src_url=text)
    else:
        await render(message.bot, state, message.chat.id, texts.MODEL_BAD_INPUT,
                     keyboards.model_step(has_saved, can_search=config.MODEL_SEARCH_ENABLED))


# ---------- быстрый расчёт: ссылка прямо в чат, БЕЗ мастера ----------
def _bare_url(message: Message) -> bool:
    """Сообщение = одна голая ссылка (без лишних слов) и никакой FSM-шаг не активен —
    трактуем как «посчитай мне это». Тексты со ссылкой внутри не перехватываем."""
    t = (message.text or "").strip()
    return bool(t) and downloader.is_http_url(t) and not any(ch.isspace() for ch in t)


@router.message(StateFilter(None), _bare_url)
async def quick_link(message: Message, state: FSMContext) -> None:
    """Кинул ссылку в чат → мгновенный ответ «начал расчёт», а скачивание + слайс с дефолтами
    (пластик Elegoo Matte) бегут фоном; результат придёт рендером с ценой и кнопкой «Открыть
    смету» в приложении (webapp.quick_link_quote)."""
    if not ratelimit.allow(f"calc:{message.from_user.id}", config.RATE_CALCS_PER_MIN, 60):
        await message.answer("⏳ Слишком много расчётов — подождите минуту.")
        return
    ack = await message.answer(texts.QUICK_CALC_STARTED)
    from .. import webapp  # локальный импорт: на уровне модулей webapp ↔ handlers циклически
    # id ack-сообщения — чтобы MW-путь отредактировал его мгновенной ценой из данных профиля
    asyncio.create_task(webapp.quick_link_quote(message.chat.id, message.from_user.id,
                                                message.text.strip(),
                                                ack_msg_id=ack.message_id))


@router.callback_query(F.data.startswith("qorder:"))
async def quick_order(cb: CallbackQuery) -> None:
    """Заказ прямо из чата по кнопке под быстрой сметой (ux-paths-next.md, идея 3): оформляем
    заказ по токену модели без открытия мини-аппа. После успеха убираем кнопку заказа (чтобы не
    тапнули повторно) и оставляем «Мои заказы»."""
    token = (cb.data or "").split(":", 1)[1]
    u = {"id": cb.from_user.id, "username": cb.from_user.username,
         "first_name": cb.from_user.first_name, "last_name": cb.from_user.last_name}
    from .. import webapp  # локальный импорт: webapp ↔ handlers циклически
    ok, msg = await webapp.place_order_from_chat(u, token)
    if ok:
        try:   # снимаем кнопку «Заказать», оставляем ссылку на «Мои заказы»
            await cb.message.edit_reply_markup(reply_markup=keyboards.open_app(
                config.WEBAPP_PUBLIC_URL, go="orders", text="🧾 Мои заказы"))
        except Exception:  # noqa: BLE001
            pass
        await cb.answer("Заказ оформлен ✅")
        await cb.message.answer(msg)
    else:
        await cb.answer(msg, show_alert=True)


# ---------- шаг: поиск модели по запросу (опц., см. docs/model-search.md §4.2) ----------
@router.callback_query(Calc.model, F.data == "m:search")
async def search_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id, search_hits={})
    await state.set_state(Calc.search_results)
    await render(cb.bot, state, cb.message.chat.id, texts.SEARCH_PROMPT, keyboards.cancel_only())
    await cb.answer()


@router.message(Calc.search_results, F.text)
async def search_query(message: Message, state: FSMContext) -> None:
    """Свободный текст в режиме поиска = поисковый запрос (НЕ перехват общего ввода — попадаем
    сюда только из явной кнопки «Найти модель»). Если прислали ссылку — грузим как ссылку."""
    from .. import modelsearch
    query = message.text.strip()
    await consume(message)
    if downloader.is_http_url(query):       # вставили ссылку в поиск → обычный путь по ссылке
        await state.set_state(Calc.model)
        try:
            meshes, label = await downloader.fetch_from_url(query)
        except (downloader.DownloadError, Exception) as exc:  # noqa: BLE001
            await render(message.bot, state, message.chat.id,
                         f"⚠️ {texts.esc(str(exc))}\n\n{texts.SEARCH_PROMPT}", keyboards.cancel_only())
            await state.set_state(Calc.search_results)
            return
        await _accept_model(message.bot, state, message.chat.id, meshes, label, src_url=query)
        return
    await render(message.bot, state, message.chat.id, texts.SEARCH_SEARCHING, None)
    try:
        hits, _sources = await modelsearch.search(query)
    except Exception:  # noqa: BLE001
        log.warning("bot model search failed", exc_info=True)
        hits = []
    if not hits:
        await render(message.bot, state, message.chat.id, texts.SEARCH_EMPTY, keyboards.cancel_only())
        return
    hits = hits[:12]                        # короткий список кнопок в боте
    # page_url не влезает в callback (64 байта) — храним карту "source:ext_id" → page_url в FSM
    hmap = {f"{h['source']}:{h['ext_id']}": h["page_url"] for h in hits}
    await state.update_data(search_hits=hmap)
    await render(message.bot, state, message.chat.id,
                 texts.search_results_text(query, len(hits)), keyboards.search_results(hits))


@router.callback_query(Calc.search_results, F.data.startswith("find:"))
async def search_pick(cb: CallbackQuery, state: FSMContext) -> None:
    """Тап по найденной модели → восстановить page_url и пойти обычным путём загрузки."""
    key = cb.data.split(":", 1)[1]          # "<source>:<ext_id>"
    data = await state.get_data()
    page_url = (data.get("search_hits") or {}).get(key)
    if not page_url:
        await cb.answer("Результат устарел — повторите поиск", show_alert=True)
        return
    await cb.answer("Скачиваю модель…")
    await state.set_state(Calc.model)       # дальше — стандартный мастер расчёта
    await render(cb.bot, state, cb.message.chat.id, texts.MODEL_DOWNLOADING, None)
    try:                                    # у MakerWorld бывает несколько вариантов — спросим
        mw = await downloader.fetch_makerworld_variants(page_url)
    except Exception:  # noqa: BLE001
        mw = None
    if mw and mw.get("variants"):
        await state.update_data(mw_url=page_url, mw_variants=mw["variants"])
        await state.set_state(Calc.variant)
        await render(cb.bot, state, cb.message.chat.id, texts.VARIANTS_PROMPT,
                     keyboards.mw_variants(mw["variants"]))
        return
    if mw and mw.get("single"):
        meshes, label = mw["single"]
    else:
        try:
            meshes, label = await downloader.fetch_from_url(page_url)
        except (downloader.DownloadError, Exception) as exc:  # noqa: BLE001
            await render(cb.bot, state, cb.message.chat.id,
                         f"⚠️ {texts.esc(str(exc))}\n\n{texts.MODEL_PROMPT}", keyboards.cancel_only())
            return
    await _accept_model(cb.bot, state, cb.message.chat.id, meshes, label, src_url=page_url)


@router.callback_query(Calc.variant, F.data.startswith("mwv:"))
async def choose_variant(cb: CallbackQuery, state: FSMContext) -> None:
    """#14: скачать выбранный вариант модели MakerWorld и продолжить как обычно."""
    iid = cb.data.split(":")[1]
    data = await state.get_data()
    url = data.get("mw_url")
    if not url:
        await cb.answer("Ссылка устарела — пришлите модель заново", show_alert=True)
        return
    await cb.answer("Скачиваю выбранный вариант…")
    await render(cb.bot, state, cb.message.chat.id, texts.MODEL_DOWNLOADING, None)
    try:
        meshes, label = await downloader.download_makerworld_instance(url, int(iid))
    except (downloader.DownloadError, ValueError) as exc:
        await render(cb.bot, state, cb.message.chat.id,
                     f"⚠️ {texts.esc(str(exc))}\n\n{texts.MODEL_PROMPT}", keyboards.cancel_only())
        return
    await _accept_model(cb.bot, state, cb.message.chat.id, meshes, label, src_url=url)


# ---------- шаг: принтер ----------
async def _ask_printer(printers: list) -> bool:
    """Спрашивать ли КЛИЕНТА, на какой машине печатать.

    Раньше условием было «активных карточек больше одной», и заведение второго принтера молча
    добавляло покупателю шаг с названиями моделей на кнопках. Теперь это переключатель мастера
    (settings.printer_choice_enabled, по умолчанию выключен): студия сама решает, на чём
    печатать, а клиент видит цену и сроки. Разбор 11.08."""
    if len(printers) <= 1:
        return False
    s = await db.get_settings()
    return bool(float(s.get("printer_choice_enabled") or 0))


async def _go_printer(bot, state: FSMContext, chat_id: int) -> None:
    printers = await db.list_printers(active_only=True)
    if not await _ask_printer(printers):
        if printers:
            await state.update_data(printer_id=printers[0].id)
        await _go_material(bot, state, chat_id)
        return
    await state.set_state(Calc.printer)
    await render(bot, state, chat_id, texts.PRINTER_PROMPT, keyboards.printers_user(printers))


@router.callback_query(Calc.printer, F.data.startswith("prn:"))
async def choose_printer(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(printer_id=int(cb.data.split(":")[1]))
    await _go_material(cb.bot, state, cb.message.chat.id)
    await cb.answer()


# ---------- шаг: пластик ----------
async def _default_combo() -> str | None:
    """Строка «пластик · качество · прочность» из админских дефолтов — только если заданы все три.
    Иначе None (кнопка быстрого расчёта не показывается)."""
    dp = await db.get_default_plastic()
    if not (dp and dp.is_default and dp.is_active):
        return None
    dq = next((q for q in await db.list_quality(active_only=True) if q.is_default), None)
    ds = next((s for s in await db.list_strength(active_only=True) if s.is_default), None)
    if not (dq and ds):
        return None
    name = dp.name if len(dp.name) <= 22 else dp.name[:21] + "…"
    return f"{name} · {dq.label} · {ds.label}"


async def _go_material(bot, state: FSMContext, chat_id: int) -> None:
    # быстрый список: недавние пластики юзера, иначе самые популярные (до 5)
    quick = await db.recent_plastics_for_user(chat_id, 5) or await db.popular_plastics(5)
    if not quick:
        await state.set_state(None)
        await render(bot, state, chat_id, texts.NO_PLASTICS, keyboards.back_home())
        return
    await state.update_data(quickmode=False)   # сброс быстрого режима при (пере)показе экрана
    combo = await _default_combo()
    await state.set_state(Calc.material)
    data = await state.get_data()
    await render(bot, state, chat_id, texts.material_prompt(data.get("model_label", "модель")),
                 keyboards.materials(quick, webapp_url=config.WEBAPP_PUBLIC_URL, default_combo=combo))


@router.callback_query(Calc.material, F.data.startswith("mat:"))
async def choose_material(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":")[1]
    if val == "catalog":
        await _open_catalog(cb, state)
        return
    if val == "quick":
        await _quick_defaults(cb, state)
        return
    plastic = await db.get_plastic(int(val))
    if not plastic or not plastic.is_active:
        await cb.answer("Пластик недоступен", show_alert=True)
        return
    await state.update_data(quickmode=False)
    await cb.answer()
    await _after_plastic(cb.bot, state, cb.message.chat.id, plastic)


async def _quick_defaults(cb: CallbackQuery, state: FSMContext) -> None:
    """Быстрый расчёт: берём админские дефолты пластика/качества/прочности и пропускаем эти шаги
    (цвет/количество — по-прежнему спрашиваем, это выбор под конкретный заказ)."""
    plastic = await db.get_default_plastic()
    quality = next((q for q in await db.list_quality(active_only=True) if q.is_default), None)
    strength = next((s for s in await db.list_strength(active_only=True) if s.is_default), None)
    if not (plastic and plastic.is_default and plastic.is_active and quality and strength):
        await cb.answer("Значения по умолчанию не настроены", show_alert=True)
        return
    await state.update_data(quality_id=quality.id, strength_id=strength.id, quickmode=True)
    await cb.answer(f"По умолчанию: {plastic.name}")
    await _after_plastic(cb.bot, state, cb.message.chat.id, plastic)


async def _after_plastic(bot, state: FSMContext, chat_id: int, plastic) -> None:
    """После выбора пластика: если цветов >1 — шаг выбора цвета, иначе сразу количество."""
    await state.update_data(plastic_id=plastic.id, color="", colors_sel=[])
    cols = plastic.color_list
    if len(cols) > 1:
        sel = [cols[0]]      # один цвет предвыбран — можно добавить ещё или сразу «Готово»
        await state.update_data(colors_sel=sel, color=cols[0])
        await state.set_state(Calc.color)
        await render(bot, state, chat_id, texts.COLOR_PROMPT, keyboards.colors(plastic, sel))
    else:
        if cols:
            await state.update_data(color=cols[0])
        await state.set_state(Calc.quantity)
        await render(bot, state, chat_id, texts.QUANTITY_PROMPT, keyboards.quantity())


@router.callback_query(Calc.color, F.data == "clr:done")
async def colors_done(cb: CallbackQuery, state: FSMContext) -> None:
    """Подтвердить набор цветов и перейти к количеству (несколько цветов → переливание)."""
    data = await state.get_data()
    sel = data.get("colors_sel") or []
    await state.update_data(color=" - ".join(sel))
    await state.set_state(Calc.quantity)
    await render(cb.bot, state, cb.message.chat.id, texts.QUANTITY_PROMPT, keyboards.quantity())
    await cb.answer()


@router.callback_query(Calc.color, F.data.startswith("clr:"))
async def choose_color(cb: CallbackQuery, state: FSMContext) -> None:
    """Тоггл цвета в наборе (мультивыбор). Минимум один цвет всегда остаётся выбранным."""
    idx = int(cb.data.split(":")[1])
    data = await state.get_data()
    plastic = await db.get_plastic(data["plastic_id"])
    cols = plastic.color_list if plastic else []
    if idx >= len(cols):
        await cb.answer()
        return
    sel = list(data.get("colors_sel") or [])
    name = cols[idx]
    max_c = max(1, int(data.get("model_numcolors", 1) or 1))   # #7332: лимит = число цветов модели
    if name in sel:
        if len(sel) > 1:        # последний цвет снять нельзя — хотя бы один нужен
            sel.remove(name)
    elif max_c <= 1:
        sel = [name]            # одноцветная модель — заменяем выбор
    elif len(sel) < max_c:
        sel.append(name)
    else:
        await cb.answer(f"У этой модели максимум {max_c} цв.", show_alert=True)
        return
    await state.update_data(colors_sel=sel, color=" - ".join(sel))
    await render(cb.bot, state, cb.message.chat.id, texts.COLOR_PROMPT, keyboards.colors(plastic, sel))
    await cb.answer()


# ---------- каталог пластиков (карусель) ----------
async def _show_catalog_card(bot, state: FSMContext, chat_id: int) -> None:
    data = await state.get_data()
    ids = data.get("cat_ids", [])
    if not ids:
        return
    idx = data.get("cat_idx", 0) % len(ids)
    await state.update_data(cat_idx=idx)
    p = await db.get_plastic(ids[idx])
    if not p:
        return
    caption = texts.catalog_card(p, config.CURRENCY)
    kb = keyboards.catalog_card(p.id, idx, len(ids))
    sid = data.get("screen_id")
    if sid:
        try:
            await bot.delete_message(chat_id, sid)
        except Exception:  # noqa: BLE001
            pass
    if p.photo_path and Path(p.photo_path).exists():
        msg = await bot.send_photo(chat_id, FSInputFile(p.photo_path), caption=caption, reply_markup=kb)
    else:
        msg = await bot.send_message(chat_id, caption, reply_markup=kb)
    await state.update_data(screen_id=msg.message_id)


async def _open_catalog(cb: CallbackQuery, state: FSMContext) -> None:
    plastics = await db.list_plastics(active_only=True)
    if not plastics:
        await cb.answer("Каталог пуст", show_alert=True)
        return
    await state.update_data(cat_ids=[p.id for p in plastics], cat_idx=0,
                            screen_id=cb.message.message_id)
    await _show_catalog_card(cb.bot, state, cb.message.chat.id)
    await cb.answer()


@router.callback_query(F.data == "cat:next")
async def cat_next(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(cat_idx=data.get("cat_idx", 0) + 1, screen_id=cb.message.message_id)
    await _show_catalog_card(cb.bot, state, cb.message.chat.id)
    await cb.answer()


@router.callback_query(F.data == "cat:prev")
async def cat_prev(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(cat_idx=data.get("cat_idx", 0) - 1, screen_id=cb.message.message_id)
    await _show_catalog_card(cb.bot, state, cb.message.chat.id)
    await cb.answer()


@router.callback_query(F.data == "cat:noop")
async def cat_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data.startswith("cat:pick:"))
async def cat_pick(cb: CallbackQuery, state: FSMContext) -> None:
    pid = int(cb.data.split(":")[2])
    plastic = await db.get_plastic(pid)
    if not plastic or not plastic.is_active:
        await cb.answer("Пластик недоступен", show_alert=True)
        return
    chat_id = cb.message.chat.id
    try:
        await cb.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await state.update_data(screen_id=None, quickmode=False)
    await cb.answer()
    await _after_plastic(cb.bot, state, chat_id, plastic)


# ---------- шаг: количество ----------
@router.callback_query(Calc.quantity, F.data.startswith("qty:"))
async def choose_quantity(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":")[1]
    if val == "custom":
        await state.set_state(Calc.quantity_custom)
        await render(cb.bot, state, cb.message.chat.id, texts.QUANTITY_CUSTOM_PROMPT, keyboards.cancel_only())
        await cb.answer()
        return
    await state.update_data(quantity=int(val))
    await _after_quantity(cb.bot, state, cb.message.chat.id)
    await cb.answer()


@router.message(Calc.quantity_custom, F.text)
async def quantity_custom(message: Message, state: FSMContext) -> None:
    await consume(message)
    raw = message.text.strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 1000):
        await render(message.bot, state, message.chat.id, texts.QUANTITY_BAD, keyboards.cancel_only())
        return
    await state.update_data(quantity=int(raw))
    await _after_quantity(message.bot, state, message.chat.id)


async def _apply_default_presets(state: FSMContext) -> bool:
    """Качество/прочность клиент больше не выбирает (8c8199d1): пресет качества — лучший для
    пластика (best_quality_id) или дефолтный, прочность — дефолтная. False — пресетов нет."""
    data = await state.get_data()
    quals = await db.list_quality(active_only=True)
    strs = await db.list_strength(active_only=True)
    if not quals or not strs:
        return False
    quality = None
    if data.get("plastic_id"):
        p = await db.get_plastic(data["plastic_id"])
        bq = int(getattr(p, "best_quality_id", 0) or 0) if p else 0
        quality = next((q for q in quals if q.id == bq), None)
    quality = quality or next((q for q in quals if q.is_default), quals[0])
    strength = next((s for s in strs if s.is_default), strs[0])
    await state.update_data(quality_id=quality.id, strength_id=strength.id)
    return True


async def _after_quantity(bot, state: FSMContext, chat_id: int) -> None:
    """После количества: качество/прочность подставляются дефолтами мастера, у клиента вместо
    них шаг «особые пожелания» (8c8199d1); в быстром режиме — сразу подтверждение."""
    data = await state.get_data()
    if not await _apply_default_presets(state):
        await render(bot, state, chat_id,
                     texts.slice_error("Пресеты печати не настроены — обратитесь к мастеру."),
                     keyboards.back_home())
        return
    if data.get("quickmode"):
        await state.set_state(Calc.confirm)
        await _show_confirm(bot, state, chat_id)
    else:
        await _go_wishes(bot, state, chat_id)


# ---------- шаг: особые пожелания ----------
async def _go_wishes(bot, state: FSMContext, chat_id: int) -> None:
    await state.set_state(Calc.wishes)
    await render(bot, state, chat_id, texts.WISHES_PROMPT, keyboards.wishes())


@router.callback_query(Calc.wishes, F.data == "wish:skip")
async def wishes_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(wishes="", screen_id=cb.message.message_id)
    await state.set_state(Calc.confirm)
    await _show_confirm(cb.bot, state, cb.message.chat.id)
    await cb.answer()


@router.message(Calc.wishes, F.text)
async def wishes_text(message: Message, state: FSMContext) -> None:
    await consume(message)
    await state.update_data(wishes=message.text.strip()[:500])
    await state.set_state(Calc.confirm)
    await _show_confirm(message.bot, state, message.chat.id)


async def _show_confirm(bot, state: FSMContext, chat_id: int) -> None:
    data = await state.get_data()
    plastic = await db.get_plastic(data["plastic_id"])
    printer = await db.get_printer(data.get("printer_id")) if data.get("printer_id") else None
    printer = printer or await db.get_default_printer()
    color = data.get("color", "")
    pdisp = f"{plastic.name} · {color}" if color else plastic.name
    text = texts.confirm_text(
        model_label=data["model_label"], plastic_name=pdisp,
        printer_name=printer.name if printer else "—", qty=data["quantity"],
        wishes=data.get("wishes", ""),
        multicolor=bool(data.get("multicolor")), extra_colors=int(data.get("extra_colors", 0)),
        plates=int(data.get("model_plates", 1)),
    )
    await render(bot, state, chat_id, text, keyboards.confirm())


# ---------- расчёт ----------
@router.callback_query(Calc.confirm, F.data == "go")
async def run_calc(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await _perform_calc(cb.bot, state, cb.message.chat.id, cb.from_user)


async def _perform_calc(bot, state: FSMContext, chat_id: int, user) -> None:
    data = await state.get_data()
    await render(bot, state, chat_id, texts.PROCESSING, None)

    printer = await db.get_printer(data.get("printer_id")) if data.get("printer_id") else None
    printer = printer or await db.get_default_printer()
    quality = await db.get_quality(data["quality_id"])
    strength = await db.get_strength(data["strength_id"])
    plastic = await db.get_plastic(data["plastic_id"])
    if not (printer and quality and strength and plastic):
        await state.set_state(None)
        await render(bot, state, chat_id, texts.slice_error("Настройки изменились — начните заново."),
                     keyboards.back_home())
        return

    color = data.get("color", "")
    qty = data["quantity"]
    plates = int(data.get("model_plates", 1))
    extra_colors = int(data.get("extra_colors", 0)) if data.get("multicolor") else 0

    try:
        res = await calcservice.compute(
            user_id=user.id, model_paths=data["model_paths"], model_label=data["model_label"],
            printer=printer, plastic=plastic, color=color, qty=qty, quality=quality,
            strength=strength, extra_colors=extra_colors, plates=plates,
            is_multicolor=bool(data.get("model_is_multicolor")),
            base_preview=data.get("preview_path"),
            colors_sel=list(data.get("colors_sel") or []))
    except calcservice.CalcError as exc:
        log.warning("Calc error: %s", exc)
        await state.set_state(None)
        await render(bot, state, chat_id, texts.slice_error(str(exc)), keyboards.back_home())
        return

    bd, settings = res["bd"], res["settings"]
    pv_path, pdisp = res["preview_path"], res["pdisp"]

    # данные для PDF
    await state.update_data(last_result={
        "model_label": data["model_label"], "plastic_name": pdisp, "printer_name": printer.name,
        "qty": qty, "quality_label": quality.label, "strength_label": strength.label,
        "bd": bd, "settings": settings, "preview_path": pv_path, "plates": plates,
        "dims": res.get("dims", ""), "multicolor": int(bool(data.get("model_is_multicolor"))),
    })
    # пер-цветовые граммы (по экструдерам) для разбивки списания склада при оформлении заказа
    await state.update_data(color_grams=res.get("color_grams") or [])

    text = texts.result_text(
        model_label=data["model_label"], plastic_name=pdisp, printer_name=printer.name,
        qty=qty, bd=bd, settings=settings, cur=config.CURRENCY, cached=res["cached"], plates=plates)
    queue = await db.queue_load()       # #515: ориентир по сроку + #9724: печатопригодность
    text += f"\n\n📅 Ориентировочно готово к <b>{db.eta_date(queue.get('minutes', 0) + bd.minutes_total)}</b>"
    if queue.get("count", 0) > 0:
        text += f"  ·  🕒 в очереди {queue['count']} зак."
    # #9724 + B2: печатопригодность. Прокидываем по-детальный замер (res['dfm']) для ГЕЙТА ОДНОЙ
    # ДЕТАЛИ — иначе валидный набор мелочи ловил бы ложные предупреждения про тонкие стенки.
    _dfm = res.get("dfm") or {}
    for w in texts.printability_warnings(
            res.get("dims", ""), config.BED_SIZE_MM,
            n_parts=_dfm.get("n_parts"), min_extent_mm=float(_dfm.get("min_extent_mm") or 0.0),
            max_part_mm=float(_dfm.get("max_part_mm") or 0.0),
            wall_proxy_mm=float(_dfm.get("wall_proxy_mm") or 0.0),
            min_wall_mm=float(settings.get("dfm_min_wall_mm") or 0.8)):
        text += f"\n⚠️ {texts.esc(w)}"
    await state.set_state(None)
    # результат: цветное превью + текст
    await _drop_screen(bot, state, chat_id)
    if pv_path and Path(pv_path).exists():
        try:
            await bot.send_photo(chat_id, FSInputFile(pv_path),
                                 caption=texts.PREVIEW_CAPTION.format(label=texts.esc(data["model_label"])))
        except Exception:  # noqa: BLE001
            pass
    msg = await bot.send_message(chat_id, text, reply_markup=keyboards.result())
    await state.update_data(screen_id=msg.message_id)

    # уведомление администраторам (с превью, если есть)
    try:
        await notify.notify_admins(
            bot,
            texts.admin_notification(
                user=user, model_label=data["model_label"], printer_name=printer.name,
                plastic_name=pdisp, qty=qty, quality_label=quality.label,
                strength_label=strength.label, bd=bd, cur=config.CURRENCY, plates=plates),
            exclude=user.id,
            photo=FSInputFile(pv_path) if pv_path and Path(pv_path).exists() else None)
    except Exception:  # noqa: BLE001
        log.debug("admin notify failed", exc_info=True)


# ---------- история моделей ----------
@router.callback_query(F.data == "m:mymodels")
async def my_models(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(screen_id=cb.message.message_id)
    items = await db.list_saved_models(cb.from_user.id, limit=30)
    if not items:
        await render(cb.bot, state, cb.message.chat.id, texts.MY_MODELS_EMPTY, keyboards.back_home())
    else:
        await render(cb.bot, state, cb.message.chat.id, texts.MY_MODELS_TITLE, keyboards.saved_models(items))
    await cb.answer()


@router.callback_query(F.data.startswith("smdel:"))
async def saved_delete(cb: CallbackQuery, state: FSMContext) -> None:
    await db.delete_saved_model(int(cb.data.split(":")[1]))
    await my_models(cb, state)


@router.callback_query(F.data.startswith("sm:"))
async def saved_pick(cb: CallbackQuery, state: FSMContext) -> None:
    sid = int(cb.data.split(":")[1])
    sm = await db.get_saved_model(sid)
    chat_id = cb.message.chat.id
    if not sm:
        await cb.answer("Модель не найдена", show_alert=True)
        await my_models(cb, state)
        return
    existing = [p for p in sm.paths if Path(p).exists()]
    if not existing:
        await state.update_data(screen_id=cb.message.message_id)
        await render(cb.bot, state, chat_id, texts.MODEL_MISSING, keyboards.model_step(True))
        await state.set_state(Calc.model)
        await cb.answer()
        return

    # восстановим выбор пользователя в FSM
    await state.update_data(
        screen_id=cb.message.message_id, model_paths=existing, model_label=sm.label,
        preview_path=sm.preview_path, printer_id=sm.printer_id, plastic_id=sm.plastic_id,
        quality_id=sm.quality_id, strength_id=sm.strength_id, quantity=sm.quantity,
        multicolor=False, extra_colors=0)
    # пере-анализ модели (столы/многоцвет) — по умолчанию печатаем одноцветно
    info = await cpupool.run(inspect3mf.inspect_model, existing)
    await state.update_data(model_plates=info.plates, model_numcolors=info.num_colors,
                            model_is_multicolor=info.is_multicolor,
                            model_mixed_plates=bool(info.mixed_plates))

    # если что-то из прошлых настроек больше недоступно — сразу на изменение параметров
    plastic = await db.get_plastic(sm.plastic_id) if sm.plastic_id else None
    quality = await db.get_quality(sm.quality_id) if sm.quality_id else None
    strength = await db.get_strength(sm.strength_id) if sm.strength_id else None
    printer = await db.get_printer(sm.printer_id) if sm.printer_id else await db.get_default_printer()
    if not (plastic and plastic.is_active and quality and quality.is_active
            and strength and strength.is_active and sm.quantity):
        await cb.answer()
        await _drop_screen(cb.bot, state, chat_id)
        if sm.preview_path and Path(sm.preview_path).exists():
            try:
                await cb.bot.send_photo(chat_id, FSInputFile(sm.preview_path),
                                        caption=texts.PREVIEW_CAPTION.format(label=texts.esc(sm.label)))
            except Exception:  # noqa: BLE001
                pass
        await _go_after_model(cb.bot, state, chat_id)
        return

    # показать превью + экран «повторить с теми же / изменить»
    await _drop_screen(cb.bot, state, chat_id)
    if sm.preview_path and Path(sm.preview_path).exists():
        try:
            await cb.bot.send_photo(chat_id, FSInputFile(sm.preview_path),
                                    caption=texts.PREVIEW_CAPTION.format(label=texts.esc(sm.label)))
        except Exception:  # noqa: BLE001
            pass
    await state.set_state(Calc.reuse)
    text = texts.reuse_text(
        label=sm.label, printer_name=printer.name if printer else "—", plastic_name=plastic.name,
        qty=sm.quantity)
    await render(cb.bot, state, chat_id, text, keyboards.reuse_settings(sid))
    await cb.answer()


@router.callback_query(Calc.reuse, F.data == "rmgo")
async def reuse_go(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await _perform_calc(cb.bot, state, cb.message.chat.id, cb.from_user)


@router.callback_query(Calc.reuse, F.data == "rmedit")
async def reuse_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id)
    await _go_printer(cb.bot, state, cb.message.chat.id)
    await cb.answer()


# ---------- коллекция моделей (кросс-пользовательская) ----------
@router.callback_query(F.data == "m:collection")
async def collection_browse(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    chat_id = cb.message.chat.id
    items = await db.list_collection(limit=30)
    try:
        await cb.message.delete()
    except Exception:  # noqa: BLE001
        pass
    # мини-апка: если поднят публичный URL — вверху списка кнопка «открыть страницей»
    if items:
        msg = await cb.bot.send_message(
            chat_id, texts.COLLECTION_TITLE,
            reply_markup=keyboards.collection_list(items, webapp_url=config.WEBAPP_PUBLIC_URL))
    else:
        msg = await cb.bot.send_message(chat_id, texts.COLLECTION_EMPTY, reply_markup=keyboards.back_home())
    await state.update_data(screen_id=msg.message_id)
    await cb.answer()


@router.callback_query(F.data.startswith("col:"))
async def collection_open(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[1])
    it = await db.get_collection_item(cid)
    chat_id = cb.message.chat.id
    if not it:
        await cb.answer("Не найдено", show_alert=True)
        return
    has_photo = bool(it.photo_path and Path(it.photo_path).exists())
    img = it.photo_path if has_photo else it.preview_path
    caption = texts.collection_caption(it.label, it.uses, has_photo)
    try:
        await cb.message.delete()
    except Exception:  # noqa: BLE001
        pass
    if img and Path(img).exists():
        msg = await cb.bot.send_photo(chat_id, FSInputFile(img), caption=caption,
                                      reply_markup=keyboards.collection_item(cid))
    else:
        msg = await cb.bot.send_message(chat_id, caption, reply_markup=keyboards.collection_item(cid))
    await state.update_data(screen_id=msg.message_id)
    await cb.answer()


async def _start_collection_model(bot, state: FSMContext, chat_id: int, it) -> bool:
    """Готовит FSM по модели из коллекции и уводит в мастер (принтер/цвет). False — файлов нет."""
    existing = [p for p in it.paths if Path(p).exists()]
    if not existing:
        return False
    await state.clear()
    await state.update_data(model_paths=existing, model_label=it.label, preview_path=it.preview_path,
                            multicolor=False, extra_colors=0)
    info = await cpupool.run(inspect3mf.inspect_model, existing)
    await state.update_data(model_plates=info.plates, model_numcolors=info.num_colors,
                            model_is_multicolor=info.is_multicolor,
                            model_mixed_plates=bool(info.mixed_plates), screen_id=None)
    await _go_after_model(bot, state, chat_id)
    return True


@router.callback_query(F.data.startswith("colgo:"))
async def collection_go(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[1])
    it = await db.get_collection_item(cid)
    chat_id = cb.message.chat.id
    if not it:
        await cb.answer("Не найдено", show_alert=True)
        return
    try:
        await cb.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await cb.answer()
    if not await _start_collection_model(cb.bot, state, chat_id, it):
        await cb.bot.send_message(chat_id, "Файлы модели недоступны — попросите прислать заново.")


async def start_collection_from_webapp(bot, storage, user_id: int, it) -> bool:
    """Запуск расчёта по модели из мини-апки — вызывает веб-сервер после проверки initData.
    Строим FSMContext по ключу пользователя (хранилище общее с диспетчером) и ведём в мастер."""
    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    try:
        await bot.send_message(user_id, f"✅ Из коллекции: <b>{texts.esc(it.label)}</b>",
                               reply_markup=ReplyKeyboardRemove())
    except Exception:  # noqa: BLE001
        pass
    return await _start_collection_model(bot, state, user_id, it)


async def select_plastic_from_webapp(bot, storage, user_id: int, plastic) -> str:
    """Выбор пластика из каталога-мини-апки. Действует только когда юзер на шаге материала
    (иначе каталог — просто для просмотра). Возвращает 'ok' | 'not_calc'."""
    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    cur = await state.get_state()
    if cur not in (Calc.material.state, Calc.color.state):
        return "not_calc"
    await _after_plastic(bot, state, user_id, plastic)
    return "ok"


# ---------- PDF-смета ----------
@router.callback_query(F.data == "result:pdf")
async def make_pdf(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lr = data.get("last_result")
    if not lr:
        await cb.answer("Сначала сделайте расчёт", show_alert=True)
        return
    await cb.answer("Готовлю PDF…")
    texts_db = await db.get_text_settings()
    preview_path = Path(lr["preview_path"]) if lr.get("preview_path") else None
    try:
        pdf = await asyncio.to_thread(
            quote.build_quote,
            business_name=texts_db.get("business_name", ""), business_contact=texts_db.get("business_contact", ""),
            model_label=lr["model_label"], printer_name=lr["printer_name"], plastic_name=lr["plastic_name"],
            qty=lr["qty"], quality_label=lr["quality_label"], strength_label=lr["strength_label"],
            bd=lr["bd"], cur=config.CURRENCY, settings=lr["settings"], preview_path=preview_path,
            plates=lr.get("plates", 1))
        await cb.bot.send_document(cb.message.chat.id, FSInputFile(str(pdf)), caption="📄 Смета на 3D-печать")
    except Exception as exc:  # noqa: BLE001
        log.exception("PDF build failed")
        await cb.bot.send_message(cb.message.chat.id, f"Не удалось сформировать PDF: {texts.esc(str(exc))}")


# ---------- заказы ----------
@router.callback_query(F.data == "order:place")
async def place_order(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lr = data.get("last_result")
    if not lr:
        await cb.answer("Сначала сделайте расчёт", show_alert=True)
        return
    bd = lr["bd"]
    user = cb.from_user
    # ОДИН КОНСТРУКТОР ЗАКАЗА (волна 5 цикла 2). Бот собирает ТОТ ЖЕ снимок расчёта, каким
    # мини-аппа держит модель в токене, и отдаёт его orders.place_from_token. Раньше здесь
    # лежал свой вызов db.add_request с другим набором полей — бот-заказ и веб-заказ приезжали
    # мастеру разными строками requests (пустой срок, разная детализация активности).
    #
    # СКЛАД: бот-заказ РЕЗЕРВИРУЕТ пластик, а не списывает его при оформлении (волна 5, шаг 5).
    # `plastic_id`/`colors_sel`/`color_grams` в снимке — ровно те три поля, из которых
    # orders._order_stock_plan строит план резерва; списывает по плану db.apply_order_stock при
    # «напечатано», проставляя движению ref_type='request'/ref_id. Раньше бот списывал склад
    # прямо здесь, без ссылки на заказ, и compute_request_cogs не находил расхода — cogs_actual
    # у ВСЕХ бот-заказов был нулевым, отчёты по марже завышены.
    #
    # СНЯТИЕ НЕМЕДЛЕННОГО СПИСАНИЯ И ПЛАН РЕЗЕРВА — ОДНО НЕДЕЛИМОЕ ИЗМЕНЕНИЕ. План в дополнение
    # к немедленному списанию дал бы ВТОРОЕ списание при «напечатано»; снятое списание без плана
    # оставило бы заказ, за который склад не спишется никогда. tests/test_bot_order_flow.py
    # (test_place_order_reserves_stock_and_deducts_on_printed) проверяет обе половины сразу.
    #
    # `last["color"]` по-прежнему НЕ кладём: плану он не нужен (тот берёт `colors_sel`), а
    # единственный оставшийся его читатель в конструкторе — память «любимый цвет клиента»,
    # которую читает только мини-аппа (webapp: db.get_user_last_color). Включать её для
    # бот-пути — отдельная фича, а не часть починки склада.
    m = {
        "label": lr["model_label"],
        "paths": data.get("model_paths", []),
        "preview": lr.get("preview_path"),
        "src_url": data.get("model_src_url", ""),
        "multicolor": int(lr.get("multicolor", 0)),
        "plates": int(lr.get("plates", 1)),
        # снимок расчётника цены — из него конструктор соберёт requests.breakdown
        "pdf": {"bd": bd, "settings": lr["settings"]},
        "last": {
            "printer": lr["printer_name"], "plastic_name": lr["plastic_name"],
            "qty": lr["qty"], "quality": lr["quality_label"], "strength": lr["strength_label"],
            "grams_total": bd.grams_total, "minutes_total": bd.minutes_total,
            "total": bd.total, "dims": lr.get("dims", ""),
            "quality_id": data.get("quality_id"), "strength_id": data.get("strength_id"),
            # план резерва склада: те же три поля, что раньше уходили в deduct_color_stock_split
            "plastic_id": data.get("plastic_id"), "colors_sel": data.get("colors_sel") or [],
            "color_grams": data.get("color_grams"),
        },
    }
    # Отдаём first_name/last_name, а не готовый full_name: конструктор склеивает их пробелом
    # ровно так же, как aiogram в User.full_name, — подпись заказа не меняется.
    # m["last"] непустой всегда → rid не None (None бывает только на пустом снимке).
    rid = await orders.place_from_token(
        {"id": user.id, "username": user.username,
         "first_name": user.first_name, "last_name": user.last_name},
        m, comment=(data.get("wishes") or "").strip())    # особые пожелания (8c8199d1)
    # Немедленного списания склада здесь БОЛЬШЕ НЕТ — заказ резервирует (см. комментарий к
    # снимку выше). Предупреждение «низкий остаток» не потеряно: его шлёт webapp
    # `_consume_order_stock` по алертам db.apply_order_stock — там же, где списание, и теперь
    # одинаково для заказа из бота и из мини-аппы.
    # Рефералка больше НЕ начисляется при оформлении (OpenTodo 7f257a33): бонус на счёт обоим
    # выдаётся после первого НАПЕЧАТАННОГО И ОПЛАЧЕННОГО заказа (webapp._maybe_referral_reward).
    await cb.answer("Заказ оставлен ✅")
    # Запись «оставил заказ» в активности делает сам конструктор — второй строки здесь больше
    # нет, иначе на каждый бот-заказ в журнале появлялось бы две.
    await state.update_data(screen_id=cb.message.message_id)
    await render(cb.bot, state, cb.message.chat.id, texts.ORDER_PLACED, keyboards.after_order())
    try:
        r = await db.get_request(rid)
        pv = lr.get("preview_path")
        await notify.notify_admins(
            cb.bot, texts.admin_new_order(r, config.CURRENCY),
            photo=FSInputFile(pv) if pv and Path(pv).exists() else None,
            reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                            text="Открыть заказ в приложении"))
    except Exception:  # noqa: BLE001
        log.debug("notify admins (new order) failed", exc_info=True)


# ---------- отзыв/оценка после печати ----------
def _stars(n) -> str:
    """Шкала оценки 1–10 (bdcc18e4); старые оценки 1–5 смигрированы ×2 в db._migrate."""
    n = max(0, min(10, int(n or 0)))
    return "★" * n + "☆" * (10 - n)


async def _notify_admins_feedback(bot, rid: int, *, rating: int | None = None,
                                  review: str | None = None) -> None:
    """Сообщить админам, что клиент оценил заказ / оставил отзыв (OpenTodo e6ef5ffb).
    Раньше оценка и отзыв молча писались в БД — мастер о них никак не узнавал."""
    try:
        r = await db.get_request(rid)
        if r is None:
            return
        if db.is_self_order(r):
            return          # клиент и админ — одно лицо: рапортовать себе о своей оценке незачем
        head = f"#{rid} «{r.model_label}» · {r.user_name or r.user_id}"
        if review is not None:
            rstr = f"{_stars(r.rating)}  {int(r.rating)}/10\n" if r.rating else ""
            body = f"📝 Отзыв клиента по заказу {head}\n{rstr}\n«{review}»"
        else:
            body = f"⭐ Клиент оценил заказ {head}\n{_stars(rating)}  {int(rating or 0)}/10"
        await notify.notify_admins(
            bot, body, audience=notify.ALL_STAFF,   # оценка качества печати — про работу, не про деньги
            reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                            text="Открыть заказ в приложении"))
    except Exception:  # noqa: BLE001
        log.debug("feedback notify failed", exc_info=True)


@router.callback_query(F.data.startswith("rate:"))
async def rate_order(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    rid, n = int(parts[1]), max(1, min(10, int(parts[2])))   # кламп: callback можно подделать
    await db.update_request(rid, rating=n)
    await db.log_event(cb.from_user.id, "оценил заказ", f"#{rid} · {n}/10", user_name=cb.from_user.full_name)
    await cb.answer("Спасибо за оценку! ⭐")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)   # убираем звёзды
    except Exception:  # noqa: BLE001
        pass
    await _notify_admins_feedback(cb.bot, rid, rating=n)
    # Отзыв просим только у настоящего клиента: у своего же заказа мастера просить отзыв о
    # самом себе бессмысленно (кнопок оценки для таких заказов и не приходит — это страховка).
    r = await db.get_request(rid)
    if db.is_self_order(r):
        return
    if await state.get_state() is None:   # не перебиваем активный мастер расчёта
        await state.set_state(Feedback.review)
        m = await cb.message.answer("Хотите оставить отзыв? Напишите его сообщением или нажмите «Пропустить».",
                                    reply_markup=keyboards.skip_review())
        # запоминаем это сообщение, чтобы убрать кнопку «Пропустить», когда отзыв пришлют текстом
        await state.update_data(review_rid=rid, review_prompt_chat=m.chat.id, review_prompt_msg=m.message_id)


@router.callback_query(F.data == "review:skip")
async def review_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await cb.answer("Ок, спасибо!")


@router.message(Feedback.review, F.text)
async def save_review(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = data.get("review_rid")
    if rid:
        review = message.text.strip()[:500]
        await db.update_request(rid, review=review)
        await db.log_event(message.from_user.id, "оставил отзыв", f"#{rid}", user_name=message.from_user.full_name)
        await _notify_admins_feedback(message.bot, rid, review=review)
    # отзыв получен — убираем кнопку «Пропустить» с исходного сообщения, чтобы её нельзя было нажать
    chat_id, msg_id = data.get("review_prompt_chat"), data.get("review_prompt_msg")
    if chat_id and msg_id:
        try:
            await message.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
    await state.set_state(None)
    await message.answer("Спасибо за отзыв! 🙌")


# ---------- идея по улучшению продукта ----------
@router.callback_query(F.data == "m:idea")
async def start_idea(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Idea.text)
    await render(cb.bot, state, cb.message.chat.id, texts.IDEA_PROMPT, keyboards.cancel_only())
    await cb.answer()


@router.message(Idea.text, F.text)
async def save_idea(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await consume(message)
    if len(text) < 5:
        await render(message.bot, state, message.chat.id, texts.IDEA_TOO_SHORT, keyboards.cancel_only())
        return
    await state.set_state(None)
    user = message.from_user
    uname = ("@" + user.username) if user.username else (user.full_name or f"id {user.id}")
    await db.add_idea(user.id, uname, text[:1000])
    await message.answer(texts.IDEA_THANKS, reply_markup=keyboards.back_home())
    try:
        await notify.notify_admins(message.bot, texts.admin_idea(user, text[:1000]))
    except Exception:  # noqa: BLE001
        log.debug("notify admins (idea) failed", exc_info=True)


@router.callback_query(F.data == "m:myorders")
async def my_orders(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(screen_id=cb.message.message_id)
    items = await db.list_user_requests(cb.from_user.id, limit=20)
    if not items:
        await render(cb.bot, state, cb.message.chat.id, texts.MY_ORDERS_EMPTY, keyboards.back_home())
    else:
        await render(cb.bot, state, cb.message.chat.id, texts.MY_ORDERS_TITLE, keyboards.user_requests(items))
    await cb.answer()


@router.callback_query(F.data.startswith("ureq:"))
async def user_request_open(cb: CallbackQuery, state: FSMContext) -> None:
    r = await db.get_request(int(cb.data.split(":")[1]))
    if not r or r.user_id != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return
    await state.update_data(screen_id=cb.message.message_id)
    await render(cb.bot, state, cb.message.chat.id, texts.request_detail(r, config.CURRENCY),
                keyboards.user_request_item(r, pay_on=await payments.enabled()))
    await cb.answer()


async def _refresh_request_msg(cb, r) -> None:
    try:
        await cb.message.edit_text(texts.request_detail(r, config.CURRENCY),
                                   reply_markup=keyboards.user_request_item(
                                       r, pay_on=await payments.enabled()))
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("uacc:"))
async def user_accept_price(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    rid = int(parts[1])
    # uacc:<rid>:<копейки> — кнопка помнит сумму, которую видел клиент (Э0). Старые кнопки
    # uacc:<rid> без суммы лежат в истории чатов и работают как раньше.
    expected = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    r = await db.get_request(rid)
    if not r or r.user_id != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return
    if r.status in ("price_changed", "invoiced") and r.pending_price is not None:
        if expected is not None and payments.amount_minor(r.pending_price) != expected:
            # Цену поменяли после того, как клиент её увидел (мастер назвал другую, ПК
            # пересчитал): принять молча нельзя — он соглашался на другие деньги. Показываем
            # актуальную и даём новые кнопки уже под неё.
            await cb.answer(texts.price_stale_alert(r.pending_price, config.CURRENCY), show_alert=True)
            await _show_actual_price(cb, r)
            return
        what = "счёт" if r.status == "invoiced" else "новую цену"
        # при включённой оплате принятая сумма идёт этапом «Ждёт оплаты» (фидбэк 31.07) —
        # зеркало мини-апп пути (_api_order_respond): инвойс уйдёт ниже, оплата сама
        # переведёт заказ «В работе»
        pay_on = (await payments.enabled()
                  and payments.amount_minor(r.pending_price) >= payments.MIN_AMOUNT_MINOR)
        new_status = "awaiting_payment" if pay_on else "in_progress"
        await db.update_request(rid, price=r.pending_price, pending_price=None, status=new_status)
        # порог «бесплатно от суммы» под согласованную цену — ДО send_invoice ниже; зеркало
        # мини-апп пути (_api_order_respond), волна 5
        await delivery.recalc_group_delivery(rid)
        await notify.notify_admins(
            cb.bot, f"✅ Клиент принял {what} по заказу #{rid} "
                    f"({texts.fmt_money(r.pending_price, config.CURRENCY)})."
                    + (" Заказ ждёт оплаты — счёт отправлен." if pay_on else ""))
        # то же событие, что и в мини-апп пути (_api_order_respond) — иначе реакция клиента
        # через inline-кнопки бота не попадала в activity_log (аудит 07.2026)
        await db.log_event(cb.from_user.id, "принял цену", f"#{rid}")
        await cb.answer("Принято ✅")
        # Сумма согласована — сразу предлагаем оплатить онлайн (A1). Best-effort: если
        # оплата не настроена/провайдер отказал, send_invoice вернёт False и молчим.
        await payments.send_invoice(cb.bot, await db.get_request(rid))
    else:
        await cb.answer("Действие недоступно")
    await _refresh_request_msg(cb, await db.get_request(rid))


async def _show_actual_price(cb: CallbackQuery, r) -> None:
    """Перерисовать сообщение под кнопкой актуальной ценой с новыми кнопками; не вышло
    (сообщение с фото, слишком старое) — прислать то же новым сообщением."""
    text = texts.request_detail(r, config.CURRENCY)
    kb = keyboards.price_confirm(r.id, r.pending_price)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001
        try:
            await cb.bot.send_message(cb.from_user.id, text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.debug("actual price resend failed", exc_info=True)


@router.callback_query(F.data.startswith("udec:"))
async def user_decline_price(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r.user_id != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return
    if r.status in ("price_changed", "invoiced"):
        what = "счёта" if r.status == "invoiced" else "новой цены"
        await db.update_request(rid, pending_price=None, status="rejected")
        await notify.notify_admins(cb.bot, f"❌ Клиент отказался от {what} по заказу #{rid}.")
        await db.log_event(cb.from_user.id, "отклонил цену", f"#{rid}")
        await cb.answer("Отказ зафиксирован")
    else:
        await cb.answer("Действие недоступно")
    await _refresh_request_msg(cb, await db.get_request(rid))


@router.callback_query(F.data.startswith("pok:"))
async def admin_print_confirm_ok(cb: CallbackQuery, state: FSMContext) -> None:
    """Мастер подтверждает завершённую печать (OpenTodo 70b5a590): заказ → «напечатано»,
    клиенту best-effort уходит таймлапс. Та же логика, что у кнопки в мини-аппе."""
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Только для мастера", show_alert=True)
        return
    rid = int(cb.data.split(":")[1])
    from .. import webapp
    res = await webapp.confirm_print(rid, True)
    await cb.answer("Готово ✅ — заказ напечатан" if res.get("ok") else (res.get("error") or "Ошибка"))
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("pnok:"))
async def admin_print_confirm_no(cb: CallbackQuery, state: FSMContext) -> None:
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Только для мастера", show_alert=True)
        return
    rid = int(cb.data.split(":")[1])
    from .. import webapp
    await webapp.confirm_print(rid, False)
    await cb.answer("Отмечено: печать не ок")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


# ---------- журнал печатей: оценка авто-занесённой печати (OpenTodo 5ac0ae9f) ----------
# Печать УЖЕ в журнале (printwatch заносит сразу, graded=0). Кнопки опроса просто проставляют
# оценку существующей записи по её id — истечения нет, запись не теряется.
def _lcd_handled() -> None:
    from .. import webapp
    webapp.lcd_print_handled()   # мастер обработал печать → LCD-экран: done → idle


async def _pj_drop_buttons(msg) -> None:
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("pj:"))
async def admin_print_journal_survey(cb: CallbackQuery, state: FSMContext) -> None:
    """Оценка результата печати, уже занесённой в журнал (по id записи; без истечения)."""
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Только для мастера", show_alert=True)
        return
    _, action, sjid = cb.data.split(":", 2)
    jid = int(sjid) if sjid.isdigit() else 0
    entry = await db.get_print_journal(jid) if jid else None
    if not entry:
        await cb.answer("Запись не найдена (удалена?). Оцените через «Журнал печатей».",
                        show_alert=True)
        await _pj_drop_buttons(cb.message)
        return
    name = entry.get("label") or "печать"
    if action == "later":
        _lcd_handled()
        await state.set_state(None)          # выйти из шага обязательного фото, если были в нём
        await _pj_drop_buttons(cb.message)
        await cb.answer("Ок — оцените позже в «Журнале печатей»")
        return
    # ЛЮБАЯ оценка (успех/частичный/полный) сначала требует ФОТО результата (OpenTodo 7bb76b35):
    # запоминаем намерение, просим фото, и только после него фиксируем оценку.
    await state.update_data(pj_jid=jid, pj_name=name, pj_action=action,
                            pj_prompt_chat=cb.message.chat.id, pj_prompt_msg=cb.message.message_id)
    await state.set_state(PrintStat.photo)
    await _pj_drop_buttons(cb.message)
    await cb.message.answer("📸 Пришлите <b>фото готовой печати</b> — оно обязательно для оценки.",
                            reply_markup=keyboards.pj_later(jid))
    await cb.answer()


@router.message(PrintStat.photo, F.photo)
async def printstat_photo(message: Message, state: FSMContext) -> None:
    """Обязательное фото результата печати (OpenTodo 7bb76b35): сохраняем и продолжаем оценку."""
    data = await state.get_data()
    jid = int(data.get("pj_jid") or 0)
    if not jid or not await db.get_print_journal(jid):
        await state.set_state(None)
        await message.answer("Запись не найдена — оцените печать через «Журнал печатей».")
        return
    config.COLLECTION_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.COLLECTION_DIR / f"jphoto_{jid}.jpg"
    try:
        await message.bot.download(message.photo[-1], destination=str(dest))
        await asyncio.to_thread(preview.optimize_photo, dest)
    except Exception:  # noqa: BLE001
        log.warning("journal photo save failed", exc_info=True)
        await message.answer("Не получилось сохранить фото — пришлите ещё раз.")
        return
    await db.set_print_journal_fields(jid, done_photo=str(dest))
    name = data.get("pj_name") or "печать"
    action = data.get("pj_action")
    if action == "ok":
        await db.grade_print_journal(jid, 0)
        _lcd_handled()
        await state.set_state(None)
        await message.answer(f"✅ Печать «{texts.esc(name)}» — успех. Фото и оценка в журнале.")
    elif action == "part":
        await state.set_state(PrintStat.pct)
        await message.answer("Фото принято. Сколько процентов печати ушло в брак? Напишите число 1–99.")
    else:                                    # полный брак
        await state.update_data(pj_pct=100)
        await state.set_state(PrintStat.reason)
        await message.answer("Фото принято. Причина брака? Одним сообщением (или «-», если без причины).")


@router.message(PrintStat.photo)
async def printstat_photo_required(message: Message, state: FSMContext) -> None:
    """В шаге обязательного фото любое НЕ-фото → напоминаем (фото нельзя пропустить)."""
    data = await state.get_data()
    await message.answer("Нужно именно <b>фото</b> результата печати — пришлите фото "
                         "(без сжатия как файл тоже не подойдёт). Или «🕒 Оценю позже».",
                         reply_markup=keyboards.pj_later(int(data.get("pj_jid") or 0)))


@router.message(PrintStat.pct, F.text)
async def printstat_pct(message: Message, state: FSMContext) -> None:
    try:
        pct = float(message.text.strip().replace("%", "").replace(",", "."))
    except ValueError:
        pct = -1.0
    if not (0 < pct < 100):
        await message.answer("Нужно число от 1 до 99 — сколько процентов печати ушло в брак.")
        return
    await state.update_data(pj_pct=pct)
    await state.set_state(PrintStat.reason)
    await message.answer("Причина брака? Одним сообщением (или «-», если без причины).")


@router.message(PrintStat.reason, F.text)
async def printstat_reason(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(None)
    jid = int(data.get("pj_jid") or 0)
    if not jid or not await db.get_print_journal(jid):
        await message.answer("Запись не найдена — оцените печать через «Журнал печатей».")
        return
    reason = message.text.strip()
    if reason in ("-", "—"):
        reason = ""
    pct = float(data.get("pj_pct") or 100)
    await db.grade_print_journal(jid, pct, reason)
    _lcd_handled()
    chat_id, msg_id = data.get("pj_prompt_chat"), data.get("pj_prompt_msg")
    if chat_id and msg_id:
        try:
            await message.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id,
                                                        reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
    pct_str = str(int(pct)) if pct == int(pct) else f"{pct:g}"
    await message.answer(f"⚠️ Оценено: «{texts.esc(data.get('pj_name') or 'печать')}», брак {pct_str}%"
                         + (f", причина: {texts.esc(reason)}" if reason else "") + ".")


# ---------- классификация печати: заказ / для себя (мастер-план, Фаза B) ----------
# Опрос качества (успех/брак) после классификации в Telegram выпилен (OpenTodo b02bc4c6):
# оценка ставится в «Журнале печатей» приложения — неоценённая запись и так ждёт мастера там.
# Кнопки СТАРЫХ чат-опросов (pj:) продолжают работать — их обработчики выше не тронуты.
async def _attach_print_to_order(jid: int, rid: int) -> tuple[bool, str]:
    """Прикрепить печать (запись журнала jid) к заказу rid БЕЗ смены статуса (на заказ бывает
    несколько печатей — мастер переводит в «Напечатано» сам). Возвращает (ok, текст-ответ)."""
    r = await db.get_request(rid)
    if not r:
        return (False, f"Заказ #{rid} не найден — проверь номер.")
    await db.set_print_journal_fields(jid, request_id=rid)
    from .. import webapp  # локальный импорт: webapp ↔ handlers циклически
    # заказу — и скрин стола этой печати, если своего фото ещё нет (OpenTodo 71523f3f)
    await webapp.copy_journal_photo_to_order(jid, rid)
    log.info("печать jid=%s прикреплена к заказу #%s (статус не меняем)", jid, rid)
    return (True, f"✅ Печать прикреплена к заказу <b>#{rid}</b> «{texts.esc(r.model_label)}».\n"
                  "Статус <b>не меняю</b> — переведёшь в «Напечатано» вручную, когда все печати "
                  "заказа будут готовы.")


async def _register_self_print(admin_id: int, jid: int, entry: dict, name: str) -> str:
    """Печать «для себя» → заказ-самопечать (слоистость 0, status='self'): цена = во сколько
    обошлась бы заказом (граммы+время по прайсу). Возвращает текст-ответ мастеру."""
    grams = float(entry.get("grams") or 0)
    minutes = float(entry.get("minutes") or 0)
    plastic = None
    if entry.get("plastic_id"):
        plastic = await db.get_plastic(int(entry["plastic_id"]))
    if not plastic and entry.get("plastic_name"):
        plastic = await db.get_plastic_by_name(entry["plastic_name"])
    if not plastic:
        plastic = await db.get_default_plastic()
    price, bd_dict = 0.0, None
    if plastic and grams > 0:
        settings = await db.get_settings()
        # Цена материала — как на всех остальных путях: price_per_gram × 1000, то есть С УЧЁТОМ
        # коэффициента сложности материала (Plastic.price_factor). Раньше здесь стоял «сырой»
        # price_per_kg — коэффициент терялся, и «было бы потрачено» занижалось на сложных
        # материалах. У материала без коэффициента (1.0) цифра не меняется.
        bd = pricing.compute_price_manual(
            grams, minutes, plastic.price_per_gram * 1000.0, settings, qty=1,
            failure_rate_pct=plastic.failure_rate_pct,
            volume_cm3_unit=(grams / plastic.density if plastic.density else None))
        price = round(bd.total, 2)
        bd_dict = pricing.breakdown_to_dict(bd, settings)
    await db.create_self_print_order(
        admin_id, plastic_name=(plastic.name if plastic else (entry.get("plastic_name") or "")),
        grams=grams, minutes=minutes, price=price, label=name, breakdown=bd_dict, jid=jid)
    total = await db.admin_would_spend(admin_id)
    cur = config.CURRENCY
    if price > 0:
        return (f"🏠 Учёл как <b>печать для себя</b>. Было бы потрачено заказом: "
                f"<b>{price:g} {cur}</b>.\nВсего «было бы потрачено»: <b>{total:g} {cur}</b> "
                "(в твоём профиле вместо баланса).")
    return ("🏠 Учёл как <b>печать для себя</b>. Цену не посчитал (нет граммов/пластика) — "
            "запись есть в журнале.")


async def _dismiss_print_classify_notif(jid: int) -> None:
    """Погасить карточку «чья печать?» в разделе «Уведомления» приложения.

    printwatch заводит её на КАЖДУЮ завершённую печать (дубль чат-пинга на случай, если пинг
    утонет), а гасил её раньше только экран в мини-аппе. Классификация из чата — основной и
    быстрый путь, и она карточку не трогала: бейдж «Уведомления» рос на 1 за печать и никогда
    не обнулялся, карточка держала живую кнопку «Выбрать заказ» на уже разобранной печати, а
    накопившись за LIMIT 100 в list_admin_notifications — вытесняла из выборки family_request
    и ломала дедуп заявок на семейный счёт. Ошибку глушим: разбор печати важнее косметики."""
    if not jid:
        return
    try:
        for n in await db.list_admin_notifications(kind="print_classify"):
            if int((n.get("payload") or {}).get("jid") or 0) == int(jid):
                await db.update_admin_notification(n["id"], status="done")
    except Exception:  # noqa: BLE001
        log.debug("classify notif done-mark failed", exc_info=True)


@router.callback_query(F.data.startswith("pe:"))
async def admin_print_classify(cb: CallbackQuery, state: FSMContext) -> None:
    """Классификация завершившейся печати: заказ / для себя (мастер-план, Фаза B).
    Оценка результата — в «Журнале печатей» приложения (чат-опрос выпилен, b02bc4c6)."""
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Только для мастера", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    jid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    entry = await db.get_print_journal(jid) if jid else None
    if not entry:
        await cb.answer("Запись не найдена (удалена?). Оцените через «Журнал печатей».", show_alert=True)
        await _pj_drop_buttons(cb.message)
        return
    name = entry.get("label") or "печать"
    await _pj_drop_buttons(cb.message)
    if action == "oth":                                  # спросим номер заказа
        await state.update_data(pe_jid=jid, pe_name=name)
        await state.set_state(PrintStat.order_no)
        await cb.message.answer("Введите <b>номер заказа</b>, к которому прикрепить эту печать "
                                "(например 42):")
        await cb.answer()
        return
    if action == "ord":                                  # подтверждение уже привязанного заказа
        rid = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        _, m = await _attach_print_to_order(jid, rid)
        await cb.message.answer(m)
        # привязку снимаем у ТОЙ машины, что печатала: с двумя принтерами «текущая привязка»
        # без машины сняла бы чужую, и чужой заказ остался бы без своей печати
        await db.clear_print_binding(entry.get("printer_id"))
        await _dismiss_print_classify_notif(jid)         # печать разобрана — карточку в приложении гасим
        _lcd_handled()
        await cb.answer()
        return
    if action == "self":                                 # печать для себя → заказ-самопечать
        m = await _register_self_print(cb.from_user.id, jid, entry, name)
        await cb.message.answer(m)
        await db.clear_print_binding(entry.get("printer_id"))
        await _dismiss_print_classify_notif(jid)
        _lcd_handled()
        await cb.answer()
        return
    await cb.answer()


@router.message(PrintStat.order_no, F.text)
async def admin_print_order_no(message: Message, state: FSMContext) -> None:
    """Ввод номера заказа для привязки печати (ветка «Другой заказ» классификации)."""
    data = await state.get_data()
    jid = int(data.get("pe_jid") or 0)
    entry = await db.get_print_journal(jid) if jid else None
    if not entry:
        await state.set_state(None)
        await message.answer("Запись не найдена — оцените печать через «Журнал печатей».")
        return
    try:
        rid = int(message.text.strip().lstrip("#").strip())
    except ValueError:
        await message.answer("Нужен номер заказа числом (например 42). Попробуйте ещё раз:")
        return
    ok, m = await _attach_print_to_order(jid, rid)
    await message.answer(m)
    if not ok:
        return                                           # остаёмся в состоянии — можно переввести
    await state.set_state(None)
    await db.clear_print_binding(entry.get("printer_id"))
    await _dismiss_print_classify_notif(jid)
    _lcd_handled()


# ---------- Семейный счёт: нативный выбор партнёра (OpenTodo 0528ba47) ----------
@router.message(F.users_shared)
async def family_partner_shared(message: Message) -> None:
    """Клиент выбрал близкого нативным пикером Telegram (кнопка из мини-аппа,
    keyboards.family_pick_partner): заводим заявку на семейный счёт с ТОЧНЫМ user_id
    партнёра — мастеру в разделе «Семейные счета» партнёр предвыбран без гаданий по имени."""
    us = message.users_shared
    if not us or us.request_id != keyboards.FAMILY_PICK_REQUEST_ID:
        return
    uid = message.from_user.id
    shared = (us.users or [None])[0]
    if shared is None:
        await message.answer("Не удалось получить контакт — попробуйте ещё раз.",
                             reply_markup=ReplyKeyboardRemove())
        return
    pname = ("@" + shared.username) if getattr(shared, "username", None) else         (" ".join(x for x in [getattr(shared, "first_name", "") or "",
                              getattr(shared, "last_name", "") or ""] if x)
         or f"id {shared.user_id}")
    if shared.user_id == uid:
        await message.answer("Это вы сами 🙂 Выберите близкого, с кем открыть общий счёт.",
                             reply_markup=ReplyKeyboardRemove())
        return
    uname = ("@" + message.from_user.username) if message.from_user.username else         (message.from_user.full_name or f"id {uid}")
    if await db.family_of(uid) is not None:
        await message.answer("У вас уже есть семейный счёт.", reply_markup=ReplyKeyboardRemove())
        return
    if not ratelimit.allow(f"family:{uid}", 3, 3600):
        await message.answer("Заявка уже отправлена — мастер свяжется.",
                             reply_markup=ReplyKeyboardRemove())
        return
    await db.log_event(uid, "запросил семейный счёт", f"{pname} (выбран из контактов)",
                       user_name=uname)
    body = f"От {uname}, партнёр: {pname} (выбран из контактов)"
    payload = {"from_id": uid, "from_name": uname, "partner": pname,
               "partner_id": shared.user_id}
    dup = [n for n in await db.list_admin_notifications(kind="family_request")
           if n["payload"].get("from_id") == uid]
    if dup:
        # Клиент перевыбрал близкого, пока прошлая заявка ещё висит. Раньше новый выбор
        # молча ВЫБРАСЫВАЛСЯ, а клиенту всё равно уходило «заявка принята» с именем НОВОГО
        # партнёра — у мастера же оставался старый. Если старая карточка пришла из текстового
        # пути, partner_id в ней нет вообще, фронт падает в подстрочный матч по имени, и
        # «Аня» резолвится в «Ваня» — а подтверждение УДАЛЯЕТ баланс не того человека и
        # вливает его деньги в чужую семью. Поэтому уточняем висящую карточку: пикер знает
        # точный user_id и всегда точнее того, что там лежало.
        await db.update_admin_notification(dup[0]["id"], body=body, payload=payload)
    else:
        await db.add_admin_notification(
            "family_request", title="Заявка на семейный счёт", body=body, payload=payload)
    # Пинг шлём и на уточнение тоже: у мастера в чате уже лежит сообщение со СТАРЫМ партнёром,
    # и без второго сообщения он подтвердит по нему.
    try:
        head = "Заявка на семейный счёт уточнена" if dup else "Заявка на семейный счёт"
        await notify.notify_admins(
            message.bot,
            f"👨‍👩‍👧 <b>{head}</b>\nОт: {texts.esc(uname)} (id {uid})\n"
            f"Партнёр: {texts.esc(pname)} (id {shared.user_id}, выбран из контактов)",
            reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminfamilies",
                                            text="Открыть заявки"))
    except Exception:  # noqa: BLE001
        log.debug("family pick notify failed", exc_info=True)
    await message.answer(
        f"Заявка на семейный счёт с <b>{texts.esc(pname)}</b> у мастера — он подтвердит "
        "открытие, и общий баланс заработает. 👨‍👩‍👧",
        reply_markup=ReplyKeyboardRemove())


# ---------- CRM: отписка от рассылок + задачи мастера (crm-roadmap.md) ----------
@router.callback_query(F.data == "unsub")
async def broadcast_unsubscribe(cb: CallbackQuery) -> None:
    """Кнопка «Отписаться» в рассылке (этап 5): больше не слать этому клиенту."""
    await db.upsert_customer(cb.from_user.id, no_broadcast=1)
    await cb.answer("Отписали — рассылок больше не будет", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("ct:done:"))
async def crm_task_done_cb(cb: CallbackQuery) -> None:
    """«Сделано» под напоминанием о задаче мастера (этап 4)."""
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Только для мастера", show_alert=True)
        return
    ok = await db.set_crm_task_done(int(cb.data.split(":")[2]))
    await cb.answer("Задача закрыта ✅" if ok else "Задача не найдена")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("ucancel:"))
async def user_cancel_order(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r.user_id != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return
    if r.status in db.REQ_CANCELLABLE:
        await db.update_request(rid, status="cancelled", pending_price=None)
        # оплаченный заказ: деньги уже у провайдера — мастеру нужно оформить возврат (A1)
        paid_note = ("\n⚠️ Заказ был ОПЛАЧЕН — не забудьте оформить возврат "
                     "в кабинете платёжного провайдера."
                     if int(getattr(r, "paid", 0) or 0) else "")
        await notify.notify_admins(
            cb.bot, f"🗑 Клиент отменил заказ #{rid} «{texts.esc(r.model_label)}».{paid_note}")
        await cb.answer("Заказ отменён")
    else:
        await cb.answer("Этот заказ уже нельзя отменить", show_alert=True)
    await _refresh_request_msg(cb, await db.get_request(rid))


# ---------- навигация «назад» ----------
@router.callback_query(F.data == "nav:model")
async def nav_model(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id)
    await state.set_state(Calc.model)
    has_saved = await db.count_saved_models(cb.from_user.id) > 0
    await render(cb.bot, state, cb.message.chat.id, texts.MODEL_PROMPT,
                 keyboards.model_step(has_saved, can_search=config.MODEL_SEARCH_ENABLED))
    await cb.answer()


@router.callback_query(F.data == "nav:printer")
async def nav_printer(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id)
    printers = await db.list_printers(active_only=True)
    if not await _ask_printer(printers):
        await state.set_state(Calc.model)
        await render(cb.bot, state, cb.message.chat.id, texts.MODEL_PROMPT, keyboards.cancel_only())
    else:
        await state.set_state(Calc.printer)
        await render(cb.bot, state, cb.message.chat.id, texts.PRINTER_PROMPT, keyboards.printers_user(printers))
    await cb.answer()


@router.callback_query(F.data == "nav:material")
async def nav_material(cb: CallbackQuery, state: FSMContext) -> None:
    try:
        await cb.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await state.update_data(screen_id=None)
    await _go_material(cb.bot, state, cb.message.chat.id)
    await cb.answer()


@router.callback_query(F.data == "nav:qty")
async def nav_qty(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id)
    await state.set_state(Calc.quantity)
    await render(cb.bot, state, cb.message.chat.id, texts.QUANTITY_PROMPT, keyboards.quantity())
    await cb.answer()


@router.callback_query(F.data == "nav:wish")
async def nav_wish(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(screen_id=cb.message.message_id)
    await _go_wishes(cb.bot, state, cb.message.chat.id)
    await cb.answer()


# ---------- мусор ----------
# Calc.wishes тоже здесь: текст съедает wishes_text (зарегистрирован раньше), а стикеры/фото
# в шаге пожеланий глотаем, как и в остальных шагах мастера.
@router.message(StateFilter(Calc.variant, Calc.printer, Calc.material, Calc.color, Calc.coloring,
                            Calc.quantity, Calc.quantity_custom, Calc.wishes,
                            Calc.confirm, Calc.reuse))
async def drop_stray(message: Message, state: FSMContext) -> None:
    await consume(message)


# ---------- быстрый старт из чата: кинул ссылку/файл вне мастера — сразу запускаем расчёт ----------
# (OpenTodo 81d0f7ec) Не нужно жать «Новый расчёт»: ссылка или .stl/.3mf, присланные в обычном чате,
# поднимают тот же мастер расчёта с уже загруженной моделью (полный pipeline: превью, цвета, цена).
async def _boot_chat_calc(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Calc.model)
    await state.update_data(screen_id=None)


def _chatcalc_allowed(uid: int) -> bool:
    return ratelimit.allow(f"chatcalc:{uid}", config.RATE_UPLOADS_PER_MIN, 60)


@router.message(StateFilter(None), F.document)
async def chat_doc_calc(message: Message, state: FSMContext) -> None:
    """Файл .stl/.3mf, присланный вне мастера → сразу расчёт."""
    if not _chatcalc_allowed(message.from_user.id):
        await message.answer("Секунду — слишком часто. Пришлите файл ещё раз чуть позже.")
        return
    await _boot_chat_calc(message, state)
    await model_document(message, state)


@router.message(StateFilter(None), F.text)
async def chat_text_calc(message: Message, state: FSMContext) -> None:
    """Ссылка на модель, присланная вне мастера → сразу расчёт. Прочий текст → подсказка-меню."""
    text = (message.text or "").strip()
    if text.startswith("/") or not downloader.is_http_url(text):
        await message.answer(
            "Нажмите /start, чтобы открыть меню.\n\nИли пришлите <b>ссылку на модель</b> "
            "(MakerWorld / Printables / Thingiverse или прямую на .stl/.3mf) — посчитаю сразу.")
        return
    if not _chatcalc_allowed(message.from_user.id):
        await message.answer("Секунду — слишком часто. Пришлите ссылку ещё раз чуть позже.")
        return
    await _boot_chat_calc(message, state)
    await model_text(message, state)


@router.message(StateFilter(None))
async def fallback(message: Message) -> None:
    await message.answer("Нажмите /start, чтобы открыть меню.")
