"""Конфигурация бота: читается из .env (см. .env.example).

КАТАЛОГ ДАННЫХ (C2). ``DATA_DIR`` — корень всего изменяемого состояния студии: база
``bot.db``, кэши моделей и превью, фото, таймлапсы, логи, бэкапы. Берётся из переменной
``DATA_DIR`` в .env/окружении (абсолютный путь); пусто — прежнее ``<папка бота>/data``.
Сам .env ОБЯЗАН оставаться в BASE_DIR: из него читается DATA_DIR, внутрь него его не
положить (курица-яйцо). .env грузится с ``override=True``, то есть ПОБЕЖДАЕТ окружение —
поэтому в .env.example образец лежит закомментированным: пустая строка ``DATA_DIR=``
затёрла бы значение, переданное переменной окружения, и бот молча уехал бы в BASE_DIR.

``.resolve()`` на DATA_DIR НЕ делаем сознательно: data/ бывает junction на другой
диск, и резолв дал бы префикс вида ``D:\\Data\\…``, с которым перестали бы совпадать
строки, унаследованные в БД. Поэтому сравнения префиксов ведём и по сырому, и по
резолвнутому виду (см. ``rel_data``/``abs_data``).

ПРАВИЛО ХРАНЕНИЯ ПУТЕЙ: в БД, JSON-полях и настройках лежат ТОЛЬКО ОТНОСИТЕЛЬНЫЕ пути
внутри DATA_DIR в POSIX-форме (``previews/x.png``). Всё, что пишется в базу, проходит
через ``rel_data()``; всё, что из базы читается и превращается в Path/open/FileResponse/
FSInputFile — через ``abs_data()``. Пути ВНЕ DATA_DIR (профили слайсера, офсайтные
бэкапы) остаются абсолютными и обеими функциями не трогаются.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Грузим .env проекта по явному пути (не зависит от текущей директории) и override=True,
# чтобы .env побеждал, даже если в окружении остался старый BOT_TOKEN.
load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)


def _data_dir_from_env() -> Path:
    """DATA_DIR из окружения/.env; пусто — прежний BASE_DIR/'data'.

    Значение нормализуем аккуратно: снимаем кавычки и завершающий разделитель (иначе
    префиксные сравнения и миграция промахнутся), но НЕ резолвим (junction).
    """
    raw = (os.getenv("DATA_DIR") or "").strip().strip('"').strip("'")
    if not raw:
        return BASE_DIR / "data"
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError(f"DATA_DIR должен быть абсолютным путём, получено: {raw!r}")
    return Path(os.path.normpath(str(p)))


# ---- Пути проекта ----
DATA_DIR = _data_dir_from_env()
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_CACHE_DIR = DOWNLOAD_DIR / "cache"   # кэш файлов, скачанных по ссылке (хранится N дней)
SLICE_DIR = DATA_DIR / "slices"
PREVIEW_DIR = DATA_DIR / "previews"
QUOTE_DIR = DATA_DIR / "quotes"
CACHE_DIR = DATA_DIR / "cache"
COLLECTION_DIR = DATA_DIR / "collection_photos"
DB_PATH = DATA_DIR / "bot.db"
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))          # сколько копий хранить
BACKUP_INTERVAL_HOURS = float(os.getenv("BACKUP_INTERVAL_HOURS", "6"))
RATE_UPLOADS_PER_MIN = int(os.getenv("RATE_UPLOADS_PER_MIN", "12"))   # лимит загрузок моделей/мин
RATE_CALCS_PER_MIN = int(os.getenv("RATE_CALCS_PER_MIN", "20"))       # лимит расчётов/мин
RATE_SEARCH_PER_MIN = int(os.getenv("RATE_SEARCH_PER_MIN", "20"))     # лимит текстовых поисков моделей/мин
# Рефералка (OpenTodo 7f257a33): БОНУС НА СЧЁТ обоим (пригласившему и приглашённому), один раз,
# после первого НАПЕЧАТАННОГО И ОПЛАЧЕННОГО заказа приглашённого. Скидок в %% больше не даёт.
# Внутри одной семьи (family_accounts) рефералка НЕ начисляется — вместо неё разовый бонус
# на общий счёт при его открытии (FAMILY_OPENING_BONUS, редизайн 2026-07-16).
REF_REWARD_AMOUNT = float(os.getenv("REF_REWARD_AMOUNT", "5"))        # бонус на счёт, в валюте бота
FAMILY_OPENING_BONUS = float(os.getenv("FAMILY_OPENING_BONUS", "5"))  # бонус на общий счёт при открытии
NOTIF_IMG_DIR = DATA_DIR / "notif_images"    # картинки release notes (центр уведомлений админа)

# Известные подкаталоги внутри DATA_DIR. Часть объявлена выше, часть создаётся по месту
# (spool_photos, custom_orders, timelapses, calibration, brand, mw_profile(s)), а vk_catalog
# наполняет отдельный инструмент tools/vk_board_import.py — но в БД он самый частый сегмент.
# Список нужен ТОЛЬКО страховке abs_data(): восстановленная на другой машине БД с чужими
# абсолютными путями перекореняется на текущий DATA_DIR по этим именам.
DATA_SUBDIRS: tuple[str, ...] = (
    "downloads", "slices", "previews", "quotes", "cache", "collection_photos", "spool_photos",
    "custom_orders", "notif_images", "backups", "avatars", "parametric", "timelapses", "logs",
    "calibration", "brand", "mw_profile", "mw_profiles", "vk_catalog",
)
_DATA_SUBDIRS_CF = frozenset(s.casefold() for s in DATA_SUBDIRS)


def _norm_prefixes() -> tuple[str, ...]:
    """Префиксы DATA_DIR для сравнения: сырой И резолвнутый (junction), в POSIX-виде.

    DATA_DIR читаем на КАЖДОМ вызове (модульная глобаль), чтобы monkeypatch в тестах и
    подмена окружения работали — «запекать» значение в константу нельзя.
    """
    out = []
    for base in (DATA_DIR, _safe_resolve(DATA_DIR)):
        s = str(base).replace("\\", "/").rstrip("/")
        if s and s not in out:
            out.append(s)
    return tuple(out)


def _safe_resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def rel_data(p: str | Path | None) -> str | None:
    """Путь под DATA_DIR → относительная POSIX-строка; всё прочее — как есть.

    Применяется на ЗАПИСИ в БД/JSON/настройки. None и пустая строка сохраняются как есть
    (колонки nullable, и «очистить поле» должно остаться очисткой). Идемпотентна:
    относительное значение остаётся относительным, путь вне DATA_DIR — дословным.
    """
    if p is None:
        return None
    s = str(p)
    if not s:
        return s
    norm = s.replace("\\", "/")
    low = norm.casefold()
    for pref in _norm_prefixes():
        pl = pref.casefold()
        if low == pl:
            return ""
        if low.startswith(pl + "/"):
            return norm[len(pref) + 1:]
    return s


def abs_data(p: str | Path | None) -> Path:
    """Значение из БД → абсолютный Path для файловых операций.

    * относительное → DATA_DIR / p;
    * абсолютное — как есть; но если такого файла НЕТ, а в пути есть известный сегмент
      данных (DATA_SUBDIRS) и перекоренённый на DATA_DIR путь СУЩЕСТВУЕТ — отдаём его
      (страховка для БД, восстановленной на другой машине без миграции). Условие
      «существует» обязательно: иначе чужой временный путь с именем вроде
      ``collection_photos`` увело бы в боевой data/.
    """
    if p is None or not str(p):
        return Path(str(p or ""))
    path = Path(p)
    # Проверяем ЯКОРЬ, а не is_absolute(): на Windows у "/tmp/a.jpg" нет диска, то есть
    # is_absolute() ложь, но приклеивать такой путь к DATA_DIR нельзя — Path его корнем
    # просто съест ("C:/data" / "/tmp/a" = "C:/tmp/a"). Свои значения всегда без якоря.
    if not path.anchor:
        return DATA_DIR / path
    rel = rel_data(path)
    if rel and not Path(rel).anchor:
        return DATA_DIR / rel          # свой же путь, записанный абсолютом
    try:
        if path.exists():
            return path
    except OSError:
        return path
    parts = path.parts
    for i in range(1, len(parts)):
        if parts[i].casefold() in _DATA_SUBDIRS_CF:
            cand = DATA_DIR.joinpath(*parts[i:])
            try:
                if cand.exists():
                    return cand
            except OSError:
                pass
            break
    return path


def under_data_dir(p: str | Path | None) -> bool:
    """Лежит ли путь внутри DATA_DIR — сверяем И по сырому, И по резолвнутому префиксу.

    Предохранитель удаления файлов. Если data/ — junction, путь файла резолвится
    в другой том (например, D:/Data/…), а сам DATA_DIR остаётся сырым. Проверка по одному
    префиксу дала бы False, и медиа перестали бы удаляться — молча.
    """
    if p is None or not str(p):
        return False
    cand = [Path(p)]
    try:
        cand.append(cand[0].resolve())
    except OSError:
        pass
    for c in cand:
        if not c.is_absolute():
            continue
        for base in (DATA_DIR, _safe_resolve(DATA_DIR)):
            try:
                if c.is_relative_to(base):
                    return True
            except (OSError, ValueError):
                continue
    return False


def abs_str(p: str | Path | None) -> str | None:
    """abs_data() для мест, где нужна строка и важно сохранить None/'' (ряды БД, JSON).

    Если разворачивать нечего (путь и так абсолютный и никуда не перекорёнен), отдаём
    ИСХОДНУЮ строку байт-в-байт: чужие значения вроде "/tmp/a.jpg" не должны менять вид
    от одного лишь прохода через Path.
    """
    if p is None:
        return None
    s = str(p)
    if not s:
        return s
    a = abs_data(s)
    return s if a == Path(s) else str(a)


def _ints(raw: str) -> list[int]:
    return [int(x) for x in re.split(r"[,\s]+", (raw or "").strip()) if x]


# ---- Telegram ----
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS: list[int] = _ints(os.getenv("ADMIN_IDS", ""))
# ПК-режим админки (OpenTodo e075a6f3): секретный токен вместо Telegram initData —
# мини-апп открывается в обычном браузере (admin_panel.py) от имени ПЕРВОГО админа.
# Пусто (дефолт) — режим выключен. Токен = полный админ-доступ: держать в .env, не светить.
ADMIN_PANEL_TOKEN = os.getenv("ADMIN_PANEL_TOKEN", "").strip()

# ---- Параметрические модели (OpenSCAD, OpenTodo c1cc840a) ----
# .scad-модели с Customizer-параметрами рендерим в STL через OpenSCAD CLI → дальше обычный слайс.
OPENSCAD_PATH = Path(os.getenv("OPENSCAD_PATH", r"C:\Program Files\OpenSCAD\openscad.exe"))
PARAMETRIC_DIR = BASE_DIR / "app" / "parametric_models"   # бандл .scad-моделей в репозитории
PARAM_RENDER_DIR = DATA_DIR / "parametric"                # кэш отрендеренных STL по (модель+параметры)

# ---- Слайсер ----
SLICER_PATH = Path(os.getenv("SLICER_PATH", r"C:\Program Files\ElegooSlicer\elegoo-slicer.exe"))
# B6: freecadcmd.exe для конвертации STEP→STL (app/stepconv.py). Пусто — пробуем типовое
# место этой машины (слайсеры/freecad); нет нигде — .step честно отклоняется на приёме.
FREECAD_CMD = os.getenv("FREECAD_CMD", "").strip()
PROFILES_DIR = Path(os.getenv("PROFILES_DIR", r"C:\Program Files\ElegooSlicer\resources\profiles\Elegoo"))
_MACHINE_NAME = os.getenv("MACHINE_PROFILE_NAME", "Elegoo Centauri Carbon 2 0.4 nozzle.json")
_FILAMENT_NAME = os.getenv("FILAMENT_PROFILE_NAME", "Elegoo PLA @ECC2.json")

MACHINE_PROFILE = PROFILES_DIR / "machine" / "ECC2" / _MACHINE_NAME
DEFAULT_FILAMENT_PROFILE = PROFILES_DIR / "filament" / "ECC2" / _FILAMENT_NAME
PROCESS_DIR = PROFILES_DIR / "process" / "ECC2"
FILAMENT_DIR = PROFILES_DIR / "filament" / "ECC2"

# Пользовательские пресеты слайсера — там лежит РЕЗУЛЬТАТ калибровки мастера (три листа-дельты:
# филамент/принтер/процесс). Каталог не в репозитории и не в бэкапе проекта, а обновление
# слайсера возвращает пресеты к стоку — поэтому app/calibration.py снимает с них снимки в БД.
SLICER_USER_DIR = Path(os.getenv(
    "SLICER_USER_DIR", str(Path(os.getenv("APPDATA", "")) / "ElegooSlicer" / "user" / "default")))

SLICE_TIMEOUT = int(os.getenv("SLICE_TIMEOUT", "240"))
# Глобальный потолок ОДНОВРЕМЕННЫХ процессов слайсера (на ВСЕ запросы и плиты разом). Семафор
# держится вокруг самого subprocess'а elegoo-slicer, а не вокруг всего расчёта — поэтому плиты
# одной мультиплейт-модели режутся параллельно (см. slicer._slice_plates_sum), упираясь в этот
# же лимит. Каждый процесс слайсера сам многопоточный и ест ~1–1.5 ГБ на пике; 4 даёт хороший
# параллелизм на многоядерной машине без риска её положить (16 ядер / 63 ГБ здесь — запас велик).
# 6 на Ryzen 7950X (16c/32t, 63 ГБ): слайсы столов одной модели идут параллельно, ~1.5 ГБ RAM
# на процесс слайсера (OpenTodo 0afd7b48 «ускорить слайс нагрузкой на CPU»). Помни: .env
# переопределяет (load_dotenv override=True) — менять надо ОБА места.
MAX_CONCURRENT_SLICES = int(os.getenv("MAX_CONCURRENT_SLICES", "6"))
# Воркеров в пуле ПРОЦЕССОВ для CPU-тяжёлой геометрии/превью. Софт-растеризатор и дециация
# мешей — плотные питоновские циклы, держащие GIL; в потоке они «вешают» общий event loop
# (бот + webapp), из-за чего расчёт одного клиента подвешивал интерфейс другим. Свой процесс —
# свой GIL, основной цикл остаётся отзывчивым. 0 → отключить пул (везде вернётся to_thread).
CPU_POOL_WORKERS = int(os.getenv("CPU_POOL_WORKERS", "4"))   # 4 на 16 ядрах: подготовка
# мешей/превью столов идёт параллельно слайсам (OpenTodo 0afd7b48)
# Перезапуск воркера пула после N задач — возвращает ОС память, накопленную trimesh/numpy на
# тяжёлых мешах (иначе RSS воркера только растёт от расчёта к расчёту). 0 → не перезапускать.
CPU_POOL_MAX_TASKS = int(os.getenv("CPU_POOL_MAX_TASKS", "30"))
# То же для ОДИНОЧНОГО рендер-воркера, но реже. Он был единственным пулом БЕЗ ресайклинга —
# «torch грузится один раз» — и потому единственным процессом, чья память не возвращалась ОС
# никогда: кэширующий аллокатор torch плюс разовые монстры (меш на 13 млн граней) копились до
# перезапуска бота. Замер 11.08: свежий воркер — 264 МБ, наблюдался — 15.87 ГБ. Порог редкий,
# потому что перезагрузка torch стоит ~4 с; раз в 200 рендеров это ничто. 0 → как было.
RENDER_POOL_MAX_TASKS = int(os.getenv("RENDER_POOL_MAX_TASKS", "200"))
# Порог тревоги по памяти процесса-воркера (ГБ): выше — WARNING в лог с разбором по процессам.
# До 11.08 у проекта НЕ БЫЛО ни одного измерения собственной памяти, и раздувшийся воркер
# обнаружился только глазами владельца в диспетчере задач.
PROC_RSS_WARN_GB = float(os.getenv("PROC_RSS_WARN_GB", "6"))
# Ориентировочный срок готовности: эффективных печатных часов в сутки НА ОДНУ МАШИНУ
PRINT_HOURS_PER_DAY = float(os.getenv("PRINT_HOURS_PER_DAY", "12"))
# Сколько машин реально печатает. Ёмкость мастерской = часы × машины: две машины, печатающие
# одновременно (решение владельца 11.08), дают вдвое больше печатных часов в сутки — иначе срок
# клиенту остался бы прежним, и главный выигрыш от второго принтера он бы не увидел.
# Кэш, как STAFF_ROLES: eta_date синхронна и ходить в базу из неё нельзя. Наполняется
# db.refresh_active_printers() на старте и после любой правки карточек. 1 = как было всегда.
ACTIVE_PRINTERS = 1


def set_active_printers(n: int) -> None:
    global ACTIVE_PRINTERS
    ACTIVE_PRINTERS = max(1, int(n or 1))
# Размер стола принтера, мм — для предупреждения «не влезает на стол» (Centauri Carbon 2 ≈ 256)
BED_SIZE_MM = float(os.getenv("BED_SIZE_MM", "256"))
# Прямая отправка на печать по сети через Moonraker/Klipper (c6e4f855). Пусто = выключено
# (кнопка скрыта). Пример: MOONRAKER_URL=http://192.168.0.50:7125 [+ MOONRAKER_API_KEY]
MOONRAKER_URL = os.getenv("MOONRAKER_URL", "").strip()
MOONRAKER_API_KEY = os.getenv("MOONRAKER_API_KEY", "").strip()
# Принтер Elegoo (Centauri Carbon) по его MQTT-протоколу (c5f8ec5f). Нужны IP и код доступа
# (с экрана принтера). Пусто = выключено. ELEGOO_SERIAL необязателен (автоопределяется из топика).
ELEGOO_PRINTER_IP = os.getenv("ELEGOO_PRINTER_IP", "").strip()
ELEGOO_ACCESS_CODE = os.getenv("ELEGOO_ACCESS_CODE", "").strip()
ELEGOO_MQTT_USER = os.getenv("ELEGOO_MQTT_USER", "elegoo").strip()
# Пассивный сбор данных принтера (Путь A мастер-плана учёта): фоновый подписчик и так слушает
# elegoo/#, поэтому видит ответы принтера на 1044/1036, когда мастер открыл экран в ElegooSlicer.
# При ON — парсим их в кэш (имя файла → граммы/время/статус) для обогащения журнала печатей.
# Read-only, в память, best-effort; по умолчанию ВЫКЛ (не меняем поведение до явного согласия).
ELEGOO_PASSIVE_HARVEST = os.getenv("ELEGOO_PASSIVE_HARVEST", "0").strip().lower() in ("1", "true", "yes", "on")
# Автосборка таймлапсов свипом printwatch (method 1051). Рендер mp4 грузит принтер минутами:
# 08.10.2026 сборка одного ролика дала на CC2 load 24 и 14 МБ свободной памяти, а в очереди
# стояли ещё четыре печати по шесть часов. Выключено — свип только докачивает ГОТОВЫЕ ролики,
# собрать видео можно кнопкой «Создать видео» в истории принтера (CC2 Control) или слайсером.
TIMELAPSE_AUTOBUILD = os.getenv("TIMELAPSE_AUTOBUILD", "1").strip().lower() in ("1", "true", "yes", "on")
ELEGOO_SERIAL = os.getenv("ELEGOO_SERIAL", "").strip()
# URL живого кадра/потока камеры принтера (MJPEG или snapshot) для экрана «Статус принтера».
# Пусто = панель камеры показывает аккуратную заглушку «Камера не настроена».
PRINTER_CAMERA_URL = os.getenv("PRINTER_CAMERA_URL", "").strip()

# ---- Генератор аватарок профиля (OpenTodo fca4246e) ----
# Локально: «весёлые промты» — через Ollama, если он запущен (модели уже стоят в системе),
# иначе встроенный словарь-фолбэк. Картинка — процедурно (Pillow, фирменный стиль) ИЛИ локальным
# diffusion-сервером, если задан AVATAR_AI_URL (A1111-совместимый /sdapi/v1/txt2img, GPU).
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").strip()
AVATAR_PROMPT_MODEL = os.getenv("AVATAR_PROMPT_MODEL", "qwen2.5-coder:7b").strip()
AVATAR_AI_URL = os.getenv("AVATAR_AI_URL", "").strip()      # пусто = только процедурный рендер
AVATAR_DIR = DATA_DIR / "avatars"
# GPU-генерация аватарок прямо на видеокарте (OpenTodo a327643f) через diffusers.
# AVATAR_GPU=0 выключает; при недоступной CUDA/сбое всегда тихий откат на процедурный рендер.
# Дефолт — лёгкий sd-turbo (фидбэк 2026-06-10): ~2.8 ГБ VRAM против ~7 у sdxl-turbo,
# холодный путь «клик → аватарка» ≈3.5 с вместо ≈13 (загрузка 3 с + генерация 0.25 с);
# уживается с играми. SDXL лучше следует промту (шляпы/предметы) — вернуть можно одной
# строкой: AVATAR_GPU_MODEL=stabilityai/sdxl-turbo (+ AVATAR_GPU_STEPS=2) в .env.
AVATAR_GPU = os.getenv("AVATAR_GPU", "1").strip() not in ("0", "false", "no", "")
AVATAR_GPU_MODEL = os.getenv("AVATAR_GPU_MODEL", "stabilityai/sd-turbo").strip()
AVATAR_GPU_STEPS = int(os.getenv("AVATAR_GPU_STEPS", "4"))   # turbo-модели: 1–4 шага без CFG;
# sd-turbo на 4 шагах заметно чище, чем на 2 (и всё равно 0.25 с)
# Z-Image-Turbo (OpenTodo 2a00eac6): топовая локальная модель, влезающая в 12 ГБ —
# AVATAR_GPU_MODEL=Tongyi-MAI/Z-Image-Turbo (+ AVATAR_GPU_STEPS=9 в .env). DiT берём в
# GGUF-кванте: bf16-трансформер один занимает 12.3 ГБ VRAM. С 21.07 квант — Q6_K (5.9 ГБ,
# визуально неотличим от Q8): вместе с VAE-тайлингом и капом воркера генерация укладывается
# в бюджет 7 ГБ VRAM и проходит гейт даже при занятом рабочем столе (Q8 требовал 8.5+ и
# вечно откатывался на запасной). Пусто = грузить bf16 из основного репо (карта на 24 ГБ).
AVATAR_GPU_GGUF = os.getenv(
    "AVATAR_GPU_GGUF",
    "https://huggingface.co/vantagewithai/Z-Image-Turbo-GGUF/blob/main/z_image_turbo-Q6_K.gguf",
).strip()
# Облегчённый квант ТОЙ ЖЕ Z-Image для тесной видеокарты (02.09.2026). Рабочий стол владельца
# в этот день держал 5.9–6.3 ГБ из 12 — Q6_K не проходил гейт 7 ГиБ и картинку рисовал слабый
# sd-turbo (чужие случайные сюжеты). Теперь между ними есть ступень: Q4_K_M (5.0 ГБ) на 768²
# укладывается в ~5.5 ГиБ — чуть мягче деталями, но это та же модель и то же понимание промта.
# Пусто = ступени нет (сразу запасной sd-turbo).
AVATAR_GPU_GGUF_LITE = os.getenv(
    "AVATAR_GPU_GGUF_LITE",
    "https://huggingface.co/vantagewithai/Z-Image-Turbo-GGUF/blob/main/z_image_turbo-Q4_K_M.gguf",
).strip()
# GPU-растеризация превью моделей (OpenTodo 9b62b4f8): кадр считается на CUDA вместо
# python-цикла по граням (секунды → десятки мс). При занятой карте/сбое — numpy-фолбэк.
PREVIEW_GPU = os.getenv("PREVIEW_GPU", "1").strip() not in ("0", "false", "no", "")
# Выгрузка SDXL из VRAM после простоя, минут (OpenTodo a2e67f96). 0 = держать всегда.
AVATAR_GPU_IDLE_MIN = float(os.getenv("AVATAR_GPU_IDLE_MIN", "5"))   # 10→5 мин (69d0b9a4)

# ---- Прочее ----
CURRENCY = os.getenv("CURRENCY", "руб.")  # бел. рубль (BYN); можно "Br" или "BYN"
MAKERWORLD_COOKIE = os.getenv("MAKERWORLD_COOKIE", "").strip()
MAKERWORLD_TOKEN = os.getenv("MAKERWORLD_TOKEN", "").strip()  # Bearer-токен авторизованной сессии
MAX_MESH_FILES = int(os.getenv("MAX_MESH_FILES", "256"))  # потолок деталей на слайс (сборки бывают 50–100+)
# Потолок СУММАРНЫХ граней геометрии (с учётом инстансов). Модель вроде «100 mini ducks» — это
# 1 меш на ~1.07M граней ×101 инстанс = 108M граней → воркер уходит в OOM (BrokenProcessPool),
# бот падает и сторож его перезапускает по кругу. Если бюджет превышен — прорежаем УНИКАЛЬНЫЕ
# меши сцены ОДИН раз (объём/смета почти не меняются, память и слайс перестают взрываться).
MAX_TOTAL_FACES = int(os.getenv("MAX_TOTAL_FACES", "2000000"))
# Прямые ссылки на файлы и дружелюбные площадки
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "200")) * 1024 * 1024
# Срок кэша скачанных по ссылке моделей, дней; 0 = БЕССРОЧНО — «всё, что хоть раз скачал,
# остаётся» (OpenTodo bdcfcda4). Throwaway-папки dl_* чистятся по 30-дневному сроку всегда.
DOWNLOAD_CACHE_AGE_DAYS = int(os.getenv("DOWNLOAD_CACHE_AGE_DAYS", "0"))
THINGIVERSE_TOKEN = os.getenv("THINGIVERSE_TOKEN", "").strip()  # бесплатно: thingiverse.com/developers


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "")


# ---- Текстовый поиск моделей по площадкам (см. docs/model-search.md) ----
# Агрегатор Printables (анонимно) + Thingiverse (по THINGIVERSE_TOKEN) + MakerWorld (по
# MAKERWORLD_TOKEN). Источник без токена просто прячется из выдачи (graceful degradation).
MODEL_SEARCH_ENABLED = _flag("MODEL_SEARCH_ENABLED", "1")
MODEL_SEARCH_LIMIT = int(os.getenv("MODEL_SEARCH_LIMIT", "24"))   # сколько карточек отдаём суммарно
MODEL_SEARCH_TIMEOUT = float(os.getenv("MODEL_SEARCH_TIMEOUT", "8"))  # таймаут на один источник, сек


# ---- Загрузка с MakerWorld через реальный браузер (Playwright) ----
# Серверный httpx блокируется Cloudflare; реальный браузер с разовым логином — работает.
MW_BROWSER_ENABLED = _flag("MW_BROWSER_ENABLED", "1")
# Умный поиск по каталогу (app/semsearch.py): «дракон» находит «Яйцо дракона» без совпадения
# букв. Тянет torch+transformers (~1.3 ГБ) — модуль опциональный, ставится галочкой
# установщика. Флаг нужен, чтобы «выключено» было ВИДНО в настройках: без него единственным
# признаком было отсутствие пакета, и владелец не мог отличить «не поставили» от «сломалось».
SEMSEARCH_ENABLED = _flag("SEMSEARCH_ENABLED", "1")
MW_HEADLESS = _flag("MW_HEADLESS", "1")
MW_CHANNEL = os.getenv("MW_CHANNEL", "chrome").strip()  # системный Chrome; фолбэк — chromium
MW_PROFILE_DIR = Path(os.getenv("MW_PROFILE_DIR", str(DATA_DIR / "mw_profile")))
MW_DOWNLOAD_TIMEOUT = int(os.getenv("MW_DOWNLOAD_TIMEOUT", "90"))
# После анти-бот 418 выводим АККАУНТ из ротации на N минут (долбёжка продлевает блок).
# 90, а не прежние 15: замеренная длительность блока MakerWorld — 90+ минут, и ранний повтор
# просто тратил запрос и продлевал бан. С пулом аккаунтов ждать дешевле — запрос уходит с
# соседнего аккаунта, а этот спокойно досиживает свой бан (см. docs/…/makerworld-account-pool.md).
MW_DOWNLOAD_COOLDOWN_MIN = int(os.getenv("MW_DOWNLOAD_COOLDOWN_MIN", "90"))


# ---- Мини-апка коллекции (веб-страница в Telegram) ----
WEBAPP_ENABLED = _flag("WEBAPP_ENABLED", "1")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8081"))
# Публичный HTTPS-URL мини-апки. Заполняется в рантайме (Cloudflare-туннель) или из .env,
# если у вас стабильный адрес. Кнопка строится по текущему значению.
WEBAPP_PUBLIC_URL: str | None = os.getenv("WEBAPP_PUBLIC_URL", "").strip() or None
# Вход наружу (C3): чем студия показывает мини-апку клиентам.
#   quick — временный Cloudflare-туннель без аккаунта (адрес меняется при перезапуске);
#   named — именованный туннель Cloudflare по токену студии (постоянный адрес из
#           WEBAPP_PUBLIC_URL); none — адрес обеспечивает владелец сам (Funnel/свой прокси).
TUNNEL_MODE = (os.getenv("TUNNEL_MODE", "quick").strip().lower() or "quick")
# Токен именованного туннеля — СЕКРЕТ (даёт полный контроль над туннелем студии):
# только из .env, в логи и в админ-статус уходит маска, а не значение.
CF_TUNNEL_TOKEN: str | None = os.getenv("CF_TUNNEL_TOKEN", "").strip() or None


# ---- Откуда бот берёт апдейты Telegram (Э1, docs/Перспективные разработки/vps-relay-2026-10-04.md §3) ----
# poll  — бот сам опрашивает Telegram (как всегда; режим покупателей по умолчанию, байт в байт);
# relay — Telegram опрашивает «приёмная» на VPS, а бот забирает у неё журнал апдейтов
#         (app/relaylink.py): пока ПК выключен, клиенты получают честные ответы и могут
#         оставить заказ без цены.
# Неизвестное значение — НЕ relay: опечатка в .env не должна оставить бота глухим (в relay-режиме
# без приёмной он ничего не услышит). Что значение было мусорным, run.py пишет в лог —
# здесь логирование ещё не настроено.
TG_SOURCES = ("poll", "relay")


def parse_tg_source(raw: str | None) -> str:
    """Значение TG_SOURCE из .env → 'poll' | 'relay'. Всё прочее (пусто, опечатка) — 'poll'."""
    value = (raw or "").strip().strip('"').strip("'").strip().lower()
    return value if value in TG_SOURCES else "poll"


def _flag_or(name: str, default: bool) -> bool:
    """Флаг, у которого ПУСТОЕ значение значит «по умолчанию», а не «выключено» (в отличие от
    _flag): `TG_DROP_PENDING=` в .env не должен молча менять поведение старта."""
    raw = os.getenv(name, "").strip().strip('"').strip("'").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _seconds(raw: str | None, default: float, low: float) -> float:
    try:
        value = float((raw or "").strip() or default)
    except ValueError:
        return default
    return max(low, value) if value == value else default      # NaN → по умолчанию


TG_SOURCE_RAW = os.getenv("TG_SOURCE", "").strip()
TG_SOURCE = parse_tg_source(TG_SOURCE_RAW)
# Адрес приёмной (например, https://<IP вашего VPS>:8450) и файл ключа подписи. Ключ — файлом
# в каталоге данных, а не строкой в .env: .env показывают пульт и скриншоты, файл — нет.
RELAY_URL = os.getenv("RELAY_URL", "").strip().strip('"').strip("'").rstrip("/")
RELAY_KEY_FILE = Path(os.getenv("RELAY_KEY_FILE", "").strip().strip('"').strip("'")
                      or (DATA_DIR / "relay.key"))
# Выбросить ли при старте апдейты, накопившиеся, пока бот лежал (delete_webhook в режиме poll).
# 1 — как было всегда; владелец с приёмной ставит 0, чтобы при откате не терять сообщения.
TG_DROP_PENDING = _flag_or("TG_DROP_PENDING", True)
# Аварийный прямой режим: приёмная недоступна дольше TG_FALLBACK_AFTER секунд — бот сам
# опрашивает Telegram, пока приёмная не ответит дважды подряд (relaylink). Меньше 10 с не
# бывает: обычная пауза между long-poll'ами не должна выглядеть аварией.
TG_FALLBACK = _flag_or("TG_FALLBACK", True)
TG_FALLBACK_AFTER = _seconds(os.getenv("TG_FALLBACK_AFTER"), 180.0, 10.0)
# Копия базы на VPS и пакет каталога для мини-аппа без ПК (app/replica.py, app/relaybundle.py;
# контракт §7). Только в TG_SOURCE=relay; 0 — не отправлять (приёмная работает как раньше).
RELAY_REPLICA = _flag_or("RELAY_REPLICA", True)
# Прямой вход ПК (Tailscale Funnel / туннель студии), когда WEBAPP_PUBLIC_URL — домен мини-апки
# на VPS (Э2, docs/Перспективные разработки/vps-relay-2026-10-04.md §5; оба адреса пишет
# edge.ps1 -Action webcutover). Что ПК включён, домен узнаёт только по его опросам приёмной: бот
# в poll к ней не ходит, и домен считал бы мастерскую выключенной, — поэтому в poll кнопки и ☰
# сразу ведут на прямой вход, а в relay — пока приёмная недоступна (аварийный прямой режим,
# app/relaylink.py: VPS, скорее всего, лежит вместе с доменом).
WEBAPP_DIRECT_URL: str | None = (os.getenv("WEBAPP_DIRECT_URL", "").strip().strip('"').strip("'").rstrip("/")
                                 or None)
#: WEBAPP_PUBLIC_URL из .env как есть: к нему relaylink возвращается после аварийного режима.
WEBAPP_RELAY_URL: str | None = WEBAPP_PUBLIC_URL


def effective_webapp_url(public: str | None, direct: str | None, tg_source: str) -> str | None:
    """Адрес мини-апки для кнопок и ☰ на старте: в poll при заданном прямом входе — он."""
    return direct if tg_source != "relay" and direct else public


WEBAPP_PUBLIC_URL = effective_webapp_url(WEBAPP_PUBLIC_URL, WEBAPP_DIRECT_URL, TG_SOURCE)


# ---- Редакция сборки (OpenTodo f98aa1ef) ----
# dev — билд разработчика (всё, включая личные инструменты: пул MakerWorld, OpenSCAD, GPU,
# дисплей TURZX, самообновление из git); studio — билд для покупателя, где личные фичи по
# умолчанию скрыты. Пусто = определить самим по наличию .git (см. app/edition.py): на машине
# владельца это рабочая копия, у покупателя — установленный дистрибутив. Реестр возможностей
# и переключатели владельца живут в app/edition.py, здесь только сырое значение из .env.
EDITION = os.getenv("EDITION", "").strip().lower()


# ---- Лицензия студии (трек C, п.4; app/license.py) ----
# Подписанный офлайн-ключ покупателя: строкой сюда либо файлом <DATA_DIR>/license.key.
# Не секрет в смысле пароля (ключ ничего не открывает, только подтверждает покупку), но
# в вывод установщика и в логи не печатается — незачем.
LICENSE_KEY = os.getenv("LICENSE_KEY", "").strip()
# Насколько строго спрашивать лицензию: off — не спрашивать вовсе, update — без
# действующей лицензии не применять обновления кода. Пусто = решает
# app/license.enforcement_level() по ОДНОМУ признаку: вшит ли в сборку публичный ключ
# выпуска (вшит — 'update', пуст — 'off'). Редакцию сборки здесь не спрашиваем: она
# определяется по наличию .git и у покупателя (установка = git clone) даёт 'dev'.
# ЕДИНСТВЕННАЯ ручка политики: ужесточение делается новым уровнем здесь, а не проверками
# по всему коду. На машине разработчика — off (или EDITION=dev) в личном .env.
LICENSE_ENFORCEMENT = os.getenv("LICENSE_ENFORCEMENT", "").strip().lower()
# Пробный период при первой установке (app/license.py): сколько дней продукт работает с
# обновлениями без ключа. Пусто/мусор = умолчание license.TRIAL_DAYS (90). Служебная ручка:
# покупателю не отдаём, нужна для тестов и особых договорённостей.
try:
    TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "").strip() or 0)
except ValueError:
    TRIAL_DAYS = 0


def ensure_dirs() -> None:
    for d in (DATA_DIR, DOWNLOAD_DIR, DOWNLOAD_CACHE_DIR, SLICE_DIR, PREVIEW_DIR, QUOTE_DIR, CACHE_DIR,
              COLLECTION_DIR, BACKUP_DIR, LOG_DIR, AVATAR_DIR, PARAM_RENDER_DIR, NOTIF_IMG_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---- Роли команды (A4) ----
# Раньше доступ был плоским: ADMIN_IDS из .env — и всё, каждый админ всемогущ. Теперь ролей
# две (owner/operator, матрица прав — в app/roles.py), а список сотрудников живёт в БД
# (таблица staff) и кэшируется здесь: is_admin() зовут из синхронного кода (клавиатуры,
# хендлеры), ходить за ролью в базу оттуда нельзя.
#
# ADMIN_IDS = КОРНЕВЫЕ владельцы: всегда owner, из приложения не понижаются и не удаляются.
# Это защита от «сам себя запер»: даже если в staff кто-то напортил, владелец из .env войдёт.
ROLE_OWNER = "owner"
ROLE_OPERATOR = "operator"
STAFF_ROLES: dict[int, str] = {}     # uid → роль; наполняет roles.refresh() из БД
# Как входят в настольную админку: 'token' (прежний .env-токен) или 'personal' (личный вход
# по коду из бота, app/desklogin.py). Кэш той же природы, что и STAFF_ROLES: значение спрашивают
# из синхронного webauth._verify_user, а настройка живёт в базе. Наполняет desklogin.refresh().
DESK_LOGIN_MODE = "token"


def set_staff_roles(mapping: dict[int, str]) -> None:
    """Заменить кэш ролей целиком (вызывает app/roles.py после любой правки команды)."""
    STAFF_ROLES.clear()
    STAFF_ROLES.update({int(k): str(v) for k, v in (mapping or {}).items()})


def role_of(user_id: int | None) -> str | None:
    """Роль сотрудника или None, если это обычный клиент."""
    if user_id is None:
        return None
    uid = int(user_id)
    if uid in ADMIN_IDS:
        return ROLE_OWNER
    return STAFF_ROLES.get(uid)


def is_admin(user_id: int | None) -> bool:
    """Сотрудник мастерской (владелец ИЛИ оператор) — то есть у человека есть админка.
    Что именно ему в ней доступно, решает матрица прав roles.can()."""
    return role_of(user_id) is not None


def is_owner(user_id: int | None) -> bool:
    return role_of(user_id) == ROLE_OWNER


def staff_ids() -> list[int]:
    """Все сотрудники: корневые владельцы из .env + приглашённые из БД."""
    return sorted({int(x) for x in ADMIN_IDS} | set(STAFF_ROLES))


# ---- Версия фронта мини-апки (авто-сброс кеша Telegram WebView) ----
_WEBUI_DIR = BASE_DIR / "app" / "webui"
_DESK_DIR = _WEBUI_DIR / "desk"        # настольная админка: в версию мини-аппа не входит
_ver_memo: dict = {"v": "", "exp": 0.0}


def webui_version() -> str:
    """Короткая версия фронта = хэш (имя+размер+mtime) всех файлов app/webui. Меняется при ЛЮБОЙ
    правке мини-апки → страница сверяет её с /api/version и сама сбрасывает кеш WebView. Никаких
    ручных бампов ?v=. Мемо на 1 с: index.html и следующий /api/version отдают ОДИН хэш (без
    ложных перезагрузок, если файл изменится в зазоре между запросами)."""
    now = time.monotonic()
    if _ver_memo["v"] and now < _ver_memo["exp"]:
        return _ver_memo["v"]
    h = hashlib.sha1()
    try:
        for p in sorted(_WEBUI_DIR.rglob("*")):
            # Настольная админка (app/webui/desk) в версию мини-аппа НЕ входит: иначе каждая
            # правка десктопа принудительно перезагружала бы мини-апп у всех клиентов Telegram.
            if p.is_file() and _DESK_DIR not in p.parents:
                st = p.stat()
                h.update(f"{p.name}:{st.st_size}:{st.st_mtime_ns}".encode())
        v = h.hexdigest()[:8]
    except OSError:
        v = "dev"
    _ver_memo["v"] = v
    _ver_memo["exp"] = now + 1.0
    return v
