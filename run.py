"""Точка входа: python run.py"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, MenuButtonDefault, MenuButtonWebApp, WebAppInfo

from app import (
    abandonednudge,
    backup,
    config,
    cpupool,
    crmtasks,
    db,
    deferred,
    desklogin,
    edition,
    elegoo,
    errorlog,
    jobarchive,
    license,
    maintenance,
    notify,
    optional,
    printmedia,
    printwatch,
    relaylink,
    roles,
    selfcheck,
    selfupdate,
    tunnel,
    version,
    webapp,
    winasyncio,
)
from app.handlers import chatreply, common, user_flow
from app.handlers import payments as payments_handlers


def _claim_single_instance() -> None:
    """Не дать боту жить вторым экземпляром и оставить якорь для сторожа.

    Зачем (инцидент 10.08, разбор 11.08). Сторож перезапустил бота, не убедившись, что старый
    умер: kill() убил только процесс-обёртку, порт остался занят, и новый экземпляр поднялся
    БЕЗ веб-сервера — исключение bind'а глушилось как «мини-апка не запустилась». Такой
    экземпляр не может ответить на /health НИКОГДА, сторож считает его мёртвым и поднимает
    следующий. Каждый лишний экземпляр приносит свой пул воркеров и свой torch-рендер-воркер:
    петля сама себя разгоняет, пока не кончится память. В логе от этого остаётся только
    OSError 10048 в bot.log — и больше ничего.

    Порт занимаем ДО инициализации чего бы то ни было (БД, пул, torch), чтобы дубликат умирал
    дёшево и громко. Проверка — на том же адресе, что и рабочий сервер.

    Заодно пишем <DATA_DIR>/bot.pid (каталог данных задаётся DATA_DIR в .env, C2). Файл читали и restart_bot.ps1, и документация, а не писал его
    НИКТО — ветка поиска бота по pid-файлу была мёртвой, и оставался лишь поиск по имени
    процесса, который на этой машине слеп (см. комментарий в watchdog._procs)."""
    if config.WEBAPP_ENABLED:
        # Несколько попыток, а не одна: при перезапуске порт освобождается не мгновенно, и
        # умереть от секундной задержки было бы хуже болезни — мастерская осталась бы вообще
        # без бота. Отказываемся, только если порт держат УСТОЙЧИВО.
        last: OSError | None = None
        for attempt in range(6):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", int(config.WEBAPP_PORT)))
                last = None
                break
            except OSError as e:
                last = e
                if attempt:
                    logging.warning("Порт %s занят, жду освобождения (%d/6)…",
                                    config.WEBAPP_PORT, attempt + 1)
                time.sleep(1.0)
            finally:
                probe.close()
        if last is not None:
            raise SystemExit(
                f"Порт {config.WEBAPP_PORT} занят уже 6 с ({last}). Похоже, бот уже запущен — "
                f"второй экземпляр не поднимаю: он остался бы без мини-апки и без /health, а "
                f"сторож плодил бы следующие. Остановите старый: .\\restart_bot.ps1 -Channel stable"
            ) from last
    try:
        pid_file = config.DATA_DIR / "bot.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logging.debug("bot.pid не записан", exc_info=True)


async def _startup_selfcheck() -> None:
    issues = await selfcheck.run_checks()
    if issues:
        logging.warning("Самодиагностика: %s", "; ".join(issues))
        await notify.alert("Самодиагностика при старте:\n— " + "\n— ".join(issues),
                           key="selfcheck", throttle_sec=1)
    else:
        logging.info("Самодиагностика: окружение в порядке")


async def _announce_channel(bot: Bot) -> None:
    """Когда запущена ТЕСТОВАЯ версия (OpenTodo 31485676) — явно предупредить админов в Telegram,
    иначе боевую и тестовую не различить. На stable молчим (лишний шум на каждом рестарте не нужен)."""
    if not version.is_test():
        return
    vi = version.info()
    await notify.notify_admins(
        bot,
        ("🧪 <b>Запущена ТЕСТОВАЯ версия</b>\n"
         f"канал: <code>{vi['channel']}</code> · ветка <code>{vi['branch']}</code> · "
         f"<code>{vi['sha']}</code>\n\n"
         "Это не боевой бот, а тест на согласовании. Вернуть боевую версию:\n"
         "<code>.\\restart_bot.ps1 -Channel stable</code>\n"
         "Одобрить тест и влить в stable:\n"
         "<code>.\\restart_bot.ps1 -Promote</code>"))


async def _warmup_avatar_gpu() -> None:
    """Проверка доступности GPU-генератора аватарок БЕЗ загрузки модели в VRAM.

    Раньше прогрев грузил SDXL при каждом старте бота — модель висела в VRAM ~7 ГБ, даже
    если аватарки никому не нужны (фидбэк 69d0b9a4/655318e9: «не выгружается», мешает играм).
    Теперь ленивá: грузится при первом клике «Аватар» (фронт показывает анимацию «будим
    мощнейший ИИ»), а после AVATAR_GPU_IDLE_MIN минут простоя выгружается."""
    from app import avatars_gpu
    try:
        # Выбор модели из админки переживает рестарт: text_settings поверх .env (22.07).
        saved = (await db.get_text_settings()).get("avatar_gpu_model", "").strip()
        if saved and not await asyncio.to_thread(avatars_gpu.set_model, saved):
            logging.warning("Сохранённая модель аватарок %r не в каталоге — рисует модель из .env", saved)
    except Exception:  # noqa: BLE001
        logging.debug("apply saved avatar model failed", exc_info=True)
    try:
        ok = await asyncio.to_thread(avatars_gpu.available)
        st = avatars_gpu.status()
        why = "выключен ярлыком на рабочем столе" if st.get("off_by_flag") else \
            ("выключен в конфиге" if not st.get("enabled") else "нет CUDA/torch")
        logging.info("GPU-генератор аватарок: %s",
                     "доступен (модель грузится по первому клику)" if ok
                     else f"недоступен — {why}, аватарки процедурно")
        # Неполный стек видно на старте, а не через 40 с внутри воркера (02.09.2026: из .venv
        # пропал accelerate — Z-Image падал на ImportError, а картинки рисовал sd-turbo).
        if ok and st.get("stack_missing"):
            logging.warning("Стек ИИ-генерации неполный — нет %s: Z-Image не поднимется, рисует "
                            "запасной. Лечение: .venv\\Scripts\\pip install -r requirements-ai.txt",
                            ", ".join(st["stack_missing"]))
    except Exception:  # noqa: BLE001
        logging.debug("avatar gpu availability check failed", exc_info=True)


async def _avatar_gpu_idle_loop() -> None:
    """Выгрузка ИИ-модели из VRAM после простоя (OpenTodo a2e67f96): ИИ не хранится в памяти
    зря — через N минут без генераций освобождаем ~7 ГБ. Следующий клик загрузит модель заново
    (фронт покажет ИИ-анимацию загрузки). Порог берём через webapp.ai_idle_min(): настройка
    мастера с экрана «Статус» (ee064f93) поверх дефолта .env, действует без перезапуска."""
    from app import avatars_gpu, webapp
    while True:
        await asyncio.sleep(60)
        try:
            idle = await webapp.ai_idle_min()
            if idle > 0 and await asyncio.to_thread(avatars_gpu.maybe_idle_unload, idle):
                logging.info("ИИ-модель выгружена из VRAM после %g мин простоя", idle)
        except Exception:  # noqa: BLE001
            logging.debug("avatar idle unload failed", exc_info=True)


async def _on_aiogram_error(event: ErrorEvent) -> bool:
    """Единственный перехват необработанных исключений хендлеров бота (телеметрия f4de8627).

    Раньше их не ловил никто: aiogram печатал трейсбек в лог, и на этом след обрывался.
    Теперь сбой едет в error_log с контекстом (тип апдейта + id пользователя, если достижим),
    а в bot.log остаётся строка с notelemetry — иначе TelemetryHandler записал бы её вторично.
    Возвращаем True: апдейт считается обработанным, polling продолжается."""
    upd = event.update
    kind, uid = "update", None
    try:
        kind = getattr(upd, "event_type", None) or "update"
        ev = getattr(upd, "event", None)
        frm = getattr(ev, "from_user", None)
        uid = getattr(frm, "id", None)
    except Exception:  # noqa: BLE001
        pass
    ctx = f"{kind}" + (f" uid {uid}" if uid else "")
    exc = event.exception
    errorlog.capture("tg", f"{type(exc).__name__}: {exc}", exc, context=ctx)
    logging.warning("Сбой обработчика бота (%s): %s", ctx, exc,
                    exc_info=exc, extra={"notelemetry": True})
    return True


def wire_bot(bot: Bot) -> None:
    """Раздать экземпляр Bot всем модулям, которые держат его в модульной переменной `_bot`.

    Одна точка вместо россыпи set_bot по коду старта: каждый такой модуль закрывает свои ветки
    отправки проверкой `if _bot is None`, поэтому ЗАБЫТАЯ проводка не падает и даже не пишет
    в лог — клиент просто перестаёт получать сообщения, и заметно это становится через сутки.
    Список держит tests/test_bot_wiring.py: он обходит уже импортированные app.* и требует
    бота у КАЖДОГО модуля с модульным `_bot`, то есть ловит следующий разрез webapp.py,
    который заведёт себе такой же модульный бот и забудет добавить строчку сюда.

    webapp сюда СОЗНАТЕЛЬНО не входит, хотя тоже держит модульный `_bot`. Его ставит
    webapp.start(), а тот запускается только при config.WEBAPP_ENABLED. Поставить бота отсюда
    значило бы включить у студии с выключенной мини-аппой ветки, которые год молчали: быструю
    смету по ссылке из чата (quick_link_quote), «оцените печать» и доставку таймлапса при
    подтверждении (confirm_print), заказ из чата, реферальную награду — всё это достижимо
    из Telegram без веб-сервера и закрыто ровно проверкой `_bot is None`. Это может быть
    и желаемым поведением, но это ПРОДУКТОВОЕ решение, а не побочный эффект переноса кода."""
    notify.set_bot(bot)       # алерты админам о сбоях из любого модуля
    printmedia.set_bot(bot)   # доставка таймлапсов клиенту (свип printwatch → Telegram)


def build_dispatcher() -> Dispatcher:
    """Собирает диспетчер: роутеры и перехват ошибок в строго заданном порядке.

    Вынесено из main() отдельной функцией, чтобы собранный диспетчер можно было получить
    без запуска бота — порядок роутеров проверяется по dp.sub_routers, а не по тексту
    этого файла. Возвращает готовый к polling диспетчер, а не заготовку."""
    dp = Dispatcher(storage=MemoryStorage())
    # Платёжный роутер — СТРОГО ПЕРВЫМ: в user_flow есть catch-all message-фолбэк
    # (StateFilter(None) → «Нажмите /start»), который иначе съедает successful_payment —
    # деньги списаны, а заказ не помечен оплаченным (реальный инцидент 31.07, заказ #87).
    dp.include_router(payments_handlers.router)   # онлайн-оплата: pre_checkout/successful_payment (A1)
    # Ответ реплаем в чат заказа — ДО common и user_flow по той же причине: иначе реплай
    # достаётся либо шагу мастера расчёта (клиент отвечает, не закрыв мастер), либо тому же
    # catch-all фолбэку, который и отвечал клиенту «Нажмите /start» вместо доставки ответа
    # мастеру. Чужие апдейты роутер не трогает: гейт стоит в фильтре (chatreply._reply_target).
    dp.include_router(chatreply.router)           # реплай на сообщение бота → чат заказа
    dp.include_router(common.router)
    dp.include_router(user_flow.router)
    dp.errors.register(_on_aiogram_error)   # телеметрия сбоев хендлеров бота (f4de8627)
    return dp


async def _bringup_webapp(bot: Bot, dp: Dispatcher) -> None:
    """Поднимает веб-сервер мини-апки + Cloudflare-туннель и ставит кнопку-меню ☰ на коллекцию.
    В фоне, чтобы не задерживать polling; если туннеля нет — сбрасываем кнопку в дефолт.

    При TG_SOURCE=relay кнопку ☰ ставит приёмная на VPS (по состоянию ПК: мини-апка живёт
    здесь, и в офлайне кнопка вела бы в никуда) — сюда только кладём адрес, который уедет к
    ней в hello. Ставь её ПК сам — приёмная и ПК перетягивали бы кнопку друг у друга."""
    relay = config.TG_SOURCE == "relay"
    try:
        await webapp.start(config.WEBAPP_PORT, bot, dp.storage)

        last_menu_url: list[str] = []

        async def _set_menu(url: str) -> None:
            """Кнопка ☰ хранится на стороне Telegram, поэтому её надо переставлять при
            КАЖДОЙ смене адреса (quick-туннель после перезапуска выдаёт новый), а не
            только при первом поднятии. По ТОМУ ЖЕ адресу дважды не ходим: в quick адрес
            приезжает колбэком ещё до возврата tunnel.start(), и повторный вызов ниже
            стоил бы лишнего запроса Bot API на каждом старте."""
            if last_menu_url and last_menu_url[-1] == url:
                return
            last_menu_url.append(url)
            app_url = url.rstrip("/") + "/app/?v=" + config.webui_version()
            if relay:
                relaylink.set_menu_url(app_url)
                logging.info("Мини-апка: %s (кнопку ☰ поставит приёмная, версия фронта %s)",
                             url, config.webui_version())
                return
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📱 Приложение", web_app=WebAppInfo(url=app_url)))
            logging.info("Мини-апка: %s (кнопка ☰ установлена, версия фронта %s)", url, config.webui_version())

        # Приоритет и режим входа наружу решает tunnel.start (C3, TUNNEL_MODE в .env):
        # quick — временный Cloudflare-туннель, named — именованный по токену студии,
        # none — адрес владельца из WEBAPP_PUBLIC_URL. Адрес из .env приоритетен везде.
        url = await tunnel.start(config.WEBAPP_PORT, on_url=_set_menu)
        if url:
            await _set_menu(url)
        else:
            if relay:
                relaylink.set_menu_url("")     # приёмная поставит обычное меню
            else:
                await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
            logging.warning("Вход снаружи не поднялся (режим %s) — коллекция останется "
                            "списком в чате.", tunnel.mode())
    except Exception:  # noqa: BLE001
        logging.warning("Мини-апка не запустилась", exc_info=True)


async def main() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:   # файл в UTF-8 (без cp1251-кракозябр консоли) с ротацией 5МБ × 5
        _handlers.append(RotatingFileHandler(
            config.LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    # Телеметрия ошибок (f4de8627): всё уровня ERROR+ дублируется в БД, а не только в файл,
    # который ротируется за часы. Пишет в буфер — БД сюда ещё не поднята, сливает flush_loop.
    _handlers.append(errorlog.TelemetryHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=_handlers,
    )
    winasyncio.install()   # WinError 10022: 99.7% строк ERROR в логе и брошенные сокеты
    config.ensure_dirs()
    _claim_single_instance()   # дубликат бота — умереть сразу, до БД/пула/torch (разбор 11.08)
    logging.info("Канал выпуска: %s (PID %s)", version.label(), os.getpid())   # stable (master) / test — см. docs/release-channels.md

    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните.")
    _check_tg_source()
    if not config.ADMIN_IDS:
        logging.warning("ADMIN_IDS пуст — админ-панель будет недоступна.")
    if not config.SLICER_PATH.exists():
        logging.warning("Слайсер не найден по пути %s — расчёт работать не будет.", config.SLICER_PATH)

    await db.init()
    # Команда мастерской (A4): роли из БД в кэш config.STAFF_ROLES — до старта бота, иначе
    # первые апдейты оператор получит как обычный клиент (без админки).
    await roles.refresh()
    # Личные сессии настольной админки (app/desklogin.py) — тем же приёмом и по той же причине:
    # их спрашивают из синхронного webauth._verify_user. Без этого вызова сотрудник, вошедший
    # вчера, после перезапуска бота оказался бы у двери с ключом, который никто не узнаёт.
    await desklogin.refresh()
    await desklogin.prune()
    # Сколько машин печатает → ёмкость мастерской в сроках (db.eta_date). Кэш, потому что
    # eta_date синхронна; наполнять надо до первого расчёта, иначе первый клиент получит срок
    # по одной машине. Тем же вызовом поднимаются фоновые подписчики карточек принтеров с
    # собственным адресом (у установки с одним принтером не поднимается ничего).
    await db.refresh_printers()
    # Возможности сборки (f98aa1ef): личные инструменты против продуктовых. Переключатели
    # владельца читаем один раз при старте — реестр спрашивают на каждом запросе.
    edition.apply_settings(await db.get_settings())
    logging.info("Редакция сборки: %s", edition.edition().upper())
    # Пробный период (решение владельца 23.08.2026): при первой установке отметка о старте
    # ставится САМА — в БД и файлом в каталоге данных. Await, а не задача: стартовая
    # самодиагностика ниже пущена create_task'ом и без отметок соврала бы владельцу
    # «лицензионный ключ не задан». На машине разработчика (enforcement 'off') не делает ничего.
    await license.ensure_trial()

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    wire_bot(bot)   # бот — всем модулям с модульным _bot (алерты notify, доставка printmedia, веб-слой)
    dp = build_dispatcher()

    # Мини-апка коллекции: локальный веб-сервер + Cloudflare-туннель для публичного HTTPS.
    # В фоне, чтобы не задерживать старт бота; кнопка-меню появится, как только будет готов URL.
    asyncio.create_task(backup.backup_loop())   # резервные копии БД (старт + по расписанию)
    asyncio.create_task(errorlog.flush_loop())   # буфер телеметрии ошибок → БД (f4de8627)
    asyncio.create_task(maintenance.maintenance_loop())   # чистка диска + компактизация БД (раз в сутки)
    asyncio.create_task(maintenance.cart_nudge_loop(bot))   # нудж о брошенной корзине (idea #10)
    asyncio.create_task(maintenance.calib_snapshot_loop())   # снимки пресетов калибровки в БД
    asyncio.create_task(maintenance.memory_watch_loop())   # память бота и воркеров в лог (разбор 11.08)
    asyncio.create_task(abandonednudge.loop(bot))   # догон брошенных расчётов (A5, по умолчанию выкл.)
    mwtoken = optional.load("mwtoken")   # пул MakerWorld — личный модуль, в открытой версии его нет
    if mwtoken is not None:
        asyncio.create_task(mwtoken.maintain_loop())   # авто-обновление токена MakerWorld до истечения
    asyncio.create_task(_startup_selfcheck())   # проверка окружения + алерт админу при проблемах
    asyncio.create_task(_announce_channel(bot))   # если запущена test-версия — предупредить админов
    asyncio.create_task(cpupool.warmup())   # прогреваем пул процессов (превью/геометрия) заранее
    asyncio.create_task(_warmup_avatar_gpu())   # проверка GPU-генератора (модель грузим лениво, 69d0b9a4)
    asyncio.create_task(_avatar_gpu_idle_loop())   # …и выгрузка после простоя (a2e67f96)
    elegoo.start_subscriber()   # фоновый MQTT-подписчик принтера → мгновенный статус из кэша
    asyncio.create_task(printwatch.loop(bot))   # финиш привязанной печати → подтверждение мастеру (70b5a590)
    asyncio.create_task(jobarchive.loop(bot))   # архив отправленных заданий — исходник для допечатки со слоя
    asyncio.create_task(crmtasks.loop(bot))   # напоминания о задачах мастера (CRM этап 4, a8b591cf)
    asyncio.create_task(selfupdate.loop(bot))   # проверка/применение обновлений кода (644367fb)
    asyncio.create_task(deferred.loop(bot))   # очередь досчёта: заказ без цены переживает рестарт (Э0)

    if config.WEBAPP_ENABLED:
        asyncio.create_task(_bringup_webapp(bot, dp))
    elif config.TG_SOURCE == "relay":
        relaylink.set_menu_url("")     # мини-апки нет: обычное меню поставит приёмная
    else:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        except Exception:  # noqa: BLE001
            pass

    await serve(bot, dp)


def _check_tg_source() -> None:
    """Откуда слушать Telegram — до подъёма БД, пула и туннеля: relay-режим без годного адреса
    и ключа оставил бы бота глухим, и узнать это надо из первой же строки лога, а не через сутки
    тишины. Мусор в TG_SOURCE config уже свёл к poll — здесь только говорим об этом вслух."""
    raw = (config.TG_SOURCE_RAW or "").strip().strip('"').strip("'").strip().lower()
    if raw and raw not in config.TG_SOURCES:
        logging.warning("TG_SOURCE=%r не понят (ожидалось poll или relay) — бот сам опрашивает "
                        "Telegram, как обычно", config.TG_SOURCE_RAW)
    if config.TG_SOURCE != "relay":
        return
    problem = relaylink.config_problem()
    if problem:
        raise SystemExit(f"TG_SOURCE=relay, но приёмной пользоваться нельзя: {problem}. Исправьте .env "
                         f"или верните TG_SOURCE=poll (откат — docs/Перспективные разработки/"
                         f"vps-relay-2026-10-04.md §4).")
    logging.info("Источник апдейтов: приёмная %s (TG_SOURCE=relay)", config.RELAY_URL)


async def serve(bot: Bot, dp: Dispatcher) -> None:
    """Слушать Telegram до остановки, затем погасить всё (shutdown).

    poll  — как всегда: delete_webhook + start_polling. Сброс накопившегося — по TG_DROP_PENDING
            (по умолчанию да, как было; владелец с приёмной ставит 0 — иначе откат с relay на
            poll выбросил бы сообщения, пришедшие за время рестарта).
    relay — Telegram опрашивает приёмная на VPS: ни delete_webhook (он сбросил бы её очередь),
            ни start_polling (два поллера одного токена — 409 у обоих). Апдейты забирает
            relaylink; события старта/остановки диспетчера он шлёт сам, как start_polling."""
    if config.TG_SOURCE == "relay":
        logging.info("Бот запущен. Слушаю Telegram через приёмную…")
        try:
            await relaylink.run(bot, dp)
        finally:
            await shutdown(bot)
        return
    # Приёмная настроена (RELAY_URL есть — так после отката), а бот опрашивает сам: сторож
    # выключит её опрос, если тот остался включён (откат при недоступном VPS). Без RELAY_URL —
    # у покупателей — ни задачи, ни запросов: всё как было.
    guard = asyncio.create_task(relaylink.guard_poll_mode(), name="relay-guard") if config.RELAY_URL else None
    await bot.delete_webhook(drop_pending_updates=config.TG_DROP_PENDING)
    logging.info("Бот запущен. Ожидаю сообщения…")
    try:
        await dp.start_polling(bot)
    finally:
        if guard is not None:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        await shutdown(bot)


async def shutdown(bot: Bot) -> None:
    """Гасит всё, что подняла main(): туннель, веб-сервер, телеметрию, БД, сессию, пул.

    Живёт рядом со своим единственным вызовом — из main() эту последовательность иначе
    не проверить, до неё не добраться, не запустив polling бота."""
    await tunnel.stop()
    await webapp.stop()
    # Хвост буфера телеметрии — ДО закрытия БД: ошибка, случившаяся за секунды до
    # остановки, обычно и есть причина остановки, а flush_loop сливает раз в 5 с.
    try:
        await errorlog.flush_once()
    except Exception:  # noqa: BLE001
        logging.debug("errorlog final flush failed", exc_info=True)
    await db.close()
    await bot.session.close()
    cpupool.shutdown()
    # Воркер аватарок здесь не гасился вовсе. При taskkill /T это неважно (дерево валят
    # снаружи), но при штатной остановке — единственное, что его убирало, — EOF пайпа.
    try:
        from app import avatars_gpu
        avatars_gpu.unload("остановка бота")
    except Exception:  # noqa: BLE001
        logging.debug("avatar worker stop failed", exc_info=True)
    # Убираем ТОЛЬКО СВОЙ pid-файл. Наивный unlink стирал чужой: файл — якорь, по которому
    # бота находит restart_bot.ps1, и снести метку ЖИВОГО процесса нельзя. Поймано на живой
    # машине 11.08 — tests/test_errorlog.py зовёт run.shutdown() без подмены DATA_DIR и удалил
    # bot.pid работающего боевого бота. Тот же случай возможен и в бою: любой второй процесс,
    # дошедший до shutdown, не должен разоружать первый.
    try:
        pid_file = config.DATA_DIR / "bot.pid"
        if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    asyncio.run(main())
