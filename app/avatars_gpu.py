"""Локальная GPU-генерация аватарок (OpenTodo a327643f): диффузионная модель на видеокарте.

Дополняет процедурный генератор (avatars.py): если включено (AVATAR_GPU=1) и CUDA доступна —
картинку рисует диффузионная модель на RTX; любой сбой → None, и вызывающий откатывается на
процедурный рендер (аватарка появляется ВСЕГДА). Пайплайн грузится лениво и один раз
(потокобезопасно); при первом использовании модель скачивается с HuggingFace (кэш HF).

Поддержаны два семейства (по AVATAR_GPU_MODEL):
- SD-turbo/SDXL-Turbo: 512×512, 1–4 шага, ~0.25–2 с — лёгкий режим, уживается с играми;
  пайплайн живёт В ЭТОМ процессе (маленький, грузится за секунды);
- Z-Image-Turbo (OpenTodo 2a00eac6): 6B DiT, 1024×1024, 9 шагов, ~19 с на RTX 5070 —
  максимальное качество/следование промту. DiT грузим в GGUF Q6_K (5.9 ГБ, визуально = Q8),
  текст-энкодер Qwen (8 ГБ bf16) живёт ТОЛЬКО на CPU (offload-хук снят — его фаза была пиком
  VRAM), эмбеддинги считаются на CPU ~3 с; VAE — тайловый декод; воркер закапен 7 ГиБ VRAM
  (set_per_process_memory_fraction, замер 21.07: реальная потребность ~6.2 ГиБ). Строго bf16
  (fp16 → NaN).
  Генерация идёт в ОТДЕЛЬНОМ spawn-процессе-воркере (как cpupool._render_pool): пайплайн с
  offload-копиями держит ~20+ ГБ ОПЕРАТИВКИ, а внутрипроцессная выгрузка её НЕ возвращает
  (замер 2026-07-08: после unload() у бота остались 21.6 ГБ WS / 43 ГБ commit — хуки offload
  и аллокатор держат кучу). Смерть воркера по простою возвращает ОС всё гарантированно.

Цепочка тиров (фидбэк 21.07 «почему процедурный вместо ИИ», ступень «lite» — 02.09.2026):
настроенная модель (Z-Image Q6_K, 1024², порог 7 ГиБ свободной VRAM) → та же Z-Image в
облегчённом кванте Q4_K_M на 768² (порог ~5.5 ГиБ: рабочий стол с браузером и слайсером
держит 5–6 ГБ из 12, и раньше это сразу роняло на sd-turbo) → лёгкий запасной sd-turbo
(когда и облегчённая не влезает или воркер сломался) → None (процедурный рендер у
вызывающего). generate_png отдаёт (png, имя движка) — UI честно говорит, чем нарисовано:
"z-image-turbo" / "z-image-turbo-lite" / "sd-turbo".

Промт RU→EN (02.09.2026): gtx-эндпоинт Google → офлайн MarianMT на CPU → оригинал, успешные
переводы кэшируются. До этого перевод был только gtx без кэша: три клика «↻ Заменить» подряд
получили 429, русский промт уехал в CLIP sd-turbo, и тот нарисовал город и барочную раму
вместо маскота. Русский промт в CLIP-семейство больше не отдаём — честнее процедурный рендер.
Неполный стек (в .venv пропал accelerate → Z-Image падал в воркере на ImportError) виден в
status().stack_missing и в логе старта, а не через 10 с после каждого спавна воркера.

scene=True (release notes) — стиль «сцена с маскотами» и кадр 4:3 вместо портрета-иконки.

«Настроенная модель» с 22.07 — выбор мастера из админки (set_model, каталог MODELS,
хранится в text_settings поверх .env) либо, пока выбора не было, AVATAR_GPU_MODEL из .env.

Всё синхронное — звать через asyncio.to_thread (как и avatars.generate).
"""
from __future__ import annotations

import io
import logging
import re
import threading

from . import config

log = logging.getLogger(__name__)

_PIPE = None            # in-process пайплайн (sd/sdxl-turbo — настроенный ИЛИ лёгкий запасной)
_PIPE_MODEL = ""        # какая модель сидит в _PIPE (настроенная vs запасная)
_WORKER = None          # (Process, Connection, lite: bool) — воркер Z-Image; lite = облегчённый квант
_LOCK = threading.Lock()
_STACK_WARNED = False   # предупреждение о неполном стеке ИИ — один раз за жизнь процесса, не на каждый клик

# Перевод промта RU→EN (02.09.2026): кэш успешных переводов + офлайн-запас к gtx.
_TR_CACHE: dict[str, str] = {}
_TR_CACHE_MAX = 256
_OFFLINE_TR_MODEL = "Helsinki-NLP/opus-mt-ru-en"   # MarianMT: ~300 МБ на диске/в ОЗУ, CPU 0.1–0.3 с
_OFFLINE_TR = None                                  # (tokenizer, model) — резидентно после первой загрузки
_OFFLINE_TR_FAILED = False                          # загрузка не удалась (нет пакетов/сети) — не ретраим на каждый клик
_OFFLINE_TR_LOCK = threading.Lock()

# Бюджеты VRAM Z-Image (порог гейта = жёсткий кап воркера, см. _need_mb).
_NEED_ZIMAGE = 7168        # Q6_K @1024²: замер 21.07 — реальная потребность ~6.2–6.9 ГиБ
_NEED_ZIMAGE_LITE = 5632   # Q4_K_M @768²: файл на 0.9 ГБ меньше, активации ~на 40% меньше (пик — в логе «пик VRAM»)
_LOAD_FAILED = False    # фатальный сбой загрузки in-process пути — не ретраим на каждый клик
_LOAD_FAILED_AT = 0.0   # когда взвели — после _ZIMAGE_RETRY_SEC даём загрузке ещё шанс (см. _load_failed_now)
_ZIMAGE_FAILED = False  # сбой загрузки воркера Z-Image: тир пропускаем, запасной живёт
_ZIMAGE_FAILED_AT = 0.0  # когда взвели флаг — после _ZIMAGE_RETRY_SEC даём Z-Image ещё шанс
_ZIMAGE_RETRY_SEC = 900.0  # 15 мин: транзиентный сбой загрузки (разовый OOM/сеть) не должен НАВСЕГДА
_LAST_USED = 0.0        # время ПОЛНОЙ загрузки/последней генерации — от него считаем простой (a2e67f96)
_BUSY = False           # идёт загрузка модели или генерация: простой НЕ начался, выгружать нельзя
_WORKER_LOAD_TIMEOUT = 240.0   # холодная загрузка Q8-DiT + Qwen с диска ≈ 40 c, с запасом
_WORKER_GEN_TIMEOUT = 120.0    # одна генерация 1024² ≈ 19 c, с запасом

# Лёгкий запасной ИИ (фидбэк 21.07 «почему процедурный вместо ИИ»): когда Z-Image не может
# (мало VRAM под загрузку / воркер сломался) — рисуем sd-turbo (~2.8 ГБ VRAM, 0.25 с), а не
# процедурную заглушку. Хуже Z-Image, но это настоящая диффузия, и она влезает почти всегда.
_FALLBACK_MODEL = "stabilityai/sd-turbo"

# Каталог моделей для выбора из админки («Статус» → карточка ИИ). Ключ — repo id HuggingFace;
# steps — рецепт семейства (turbo-модели обучены на 1–4 шагах, Z-Image — на ~9), поэтому шаги
# едут вместе с моделью, а не берутся из общего AVATAR_GPU_STEPS. Выбор мастера хранится в
# text_settings.avatar_gpu_model (поверх .env, как порог автовыгрузки) и применяется без
# перезапуска: webapp дёргает set_model, run._warmup_avatar_gpu применяет сохранённое на старте.
MODELS: dict[str, dict] = {
    "Tongyi-MAI/Z-Image-Turbo": {"label": "Z-Image-Turbo", "steps": 9,
        "desc": "лучшее качество и следование промту · 1024² · ~19 с · ~7 ГБ VRAM "
                "(мало свободной VRAM → она же в облегчённом кванте, ~5.5 ГБ)"},
    "stabilityai/sdxl-turbo": {"label": "SDXL-Turbo", "steps": 2,
        "desc": "лучше слушается промта, чем SD-Turbo · 512² · ~2 с · ~7 ГБ VRAM"},
    "stabilityai/sd-turbo": {"label": "SD-Turbo", "steps": 4,
        "desc": "самая быстрая и лёгкая · 512² · ~0.3 с · ~3 ГБ VRAM"},
}
_MODEL_OVERRIDE: str | None = None   # выбор мастера из админки; None = модель из .env

# Рантайм-выключатель (OpenTodo ac6c7e0d): файл-флаг выключает ИИ-генерацию БЕЗ перезапуска —
# админ дёргает ярлык на рабочем столе (хочет поиграть → освободить VRAM), юзеры тем временем
# получают процедурные аватарки. Файл — единственный источник истины (переживает рестарты).
_OFF_FLAG = config.DATA_DIR / "avatar_gpu.off"

# Стиль приложения: милый маскот-«напечатанная фигурка» с тёплым оранжевым акцентом.
_STYLE = ("{p}, cute mascot character, 3d printed figurine look with visible print layers, "
          "centered portrait, app avatar, warm orange rim light, dark studio background, "
          "high quality, detailed, vibrant")
# Для SD2-семейства (sd-turbo) слова «high quality, detailed» провоцируют вотермарки/текст
# в углах (артефакт обучающей выборки) — стиль без них, с «product photo» (живой тест 2026-06-10).
_STYLE_SD = ("{p}, cute mascot character, 3d printed figurine, centered portrait, app avatar, "
             "warm orange rim light, dark studio background, smooth product photo, vibrant colors")
# Иллюстрация к release note (02.09.2026) — это СЦЕНА («робот раскладывает детали по ячейкам
# у принтера»), а не портрет для иконки: слова «centered portrait, app avatar» тянули картинку
# к одному персонажу крупным планом и обрезали сюжет. Здесь — общий план и мягкий студийный свет.
_STYLE_SCENE = ("{p}, cute 3d printed mascot characters with visible print layers, storytelling scene, "
                "wide shot, soft studio lighting with warm orange accent light, dark background, "
                "high quality, detailed, vibrant")
_STYLE_SD_SCENE = ("{p}, cute 3d printed mascot figurines, scene, wide shot, warm orange rim light, "
                   "dark studio background, smooth product photo, vibrant colors")


def _final_prompt(en_prompt: str, model: str, styled: bool, scene: bool = False) -> str:
    """Итоговый промт для модели: фирменный стиль маскота-фигурки или сырой промт как есть
    (styled=False — панель «Проба генерации» в админке: сравнивать модели без наслоений).
    Z-Image и SDXL держат длинный стиль, SD2-семейство — укороченный без вотермарк-слов.
    scene=True — стиль сцены для иллюстрации к release note вместо портрета-иконки."""
    if not styled:
        return en_prompt
    m = model.lower()
    big = "z-image" in m or "xl" in m
    if scene:
        return (_STYLE_SCENE if big else _STYLE_SD_SCENE).format(p=en_prompt)
    return (_STYLE if big else _STYLE_SD).format(p=en_prompt)


def _zimage_size(lite: bool, scene: bool) -> tuple[int, int]:
    """Размер кадра Z-Image: аватарка — квадрат, сцена — 4:3 (пост в Telegram смотрится лучше
    горизонтальным). Облегчённый квант рисует на ~768-классе: меньше активаций → влезает в
    ~5.5 ГиБ; аватарка всё равно уезжает в 512², а сцену Telegram ужмёт до 1280 по ширине.
    Стороны — кратные 16 (патч 2 × VAE 8)."""
    if scene:
        return (896, 672) if lite else (1152, 864)
    return (768, 768) if lite else (1024, 1024)


def _gguf_label(url: str) -> str:
    """«Q6_K» / «Q4_K_M» из имени GGUF-файла — для честной строки в логе; пусто → bf16."""
    m = re.search(r"[-_](Q\d[\w]*)\.gguf$", (url or "").strip(), flags=re.I)
    return m.group(1) if m else "bf16"


def current_model() -> str:
    """Действующая модель генерации: выбор мастера из админки поверх .env."""
    return _MODEL_OVERRIDE or config.AVATAR_GPU_MODEL


def _steps_for(model: str) -> int:
    """Шаги генерации для конкретной модели: рецепт из каталога. Ручной AVATAR_GPU_STEPS из
    .env уважаем, только пока мастер не выбрал модель в админке — иначе 9 шагов Z-Image
    достались бы sd-turbo (мыло и втрое дольше). Запасной sd-turbo всегда идёт по каталогу.

    ВАЖНО (жалоба «Z-Image деградирует»): для СЕМЕЙСТВА Z-Image рецепт каталога (9 шагов)
    авторитетнее ручного AVATAR_GPU_STEPS. Иначе .env-путь без явного AVATAR_GPU_STEPS (его
    дефолт — 4, под turbo-модели) молча рисовал бы Z-Image в 4 шага = мыло. У Z-Image малый
    шаг ломает картинку, поэтому его шаги из .env не берём никогда."""
    recipe = MODELS.get(model, {}).get("steps")
    if recipe and "z-image" in model.lower():
        return int(recipe)
    if _MODEL_OVERRIDE is None and model == config.AVATAR_GPU_MODEL:
        return config.AVATAR_GPU_STEPS
    return int(recipe if recipe is not None else config.AVATAR_GPU_STEPS)


def set_model(model: str | None) -> bool:
    """Сменить модель генерации из админки без перезапуска (поверх .env). None/"" — вернуться
    к модели из .env. Неизвестный repo id → False (модель из .env принимаем любую: мастер мог
    вписать туда что-то своё). Смена выгружает текущий пайплайн/воркер (в 12 ГБ два не живут)
    и снимает пометки сбоев — у новой модели свой шанс загрузиться. Может подождать _LOCK,
    пока дорисуется идущая генерация."""
    global _MODEL_OVERRIDE, _LOAD_FAILED, _LOAD_FAILED_AT, _ZIMAGE_FAILED, _ZIMAGE_FAILED_AT
    model = (model or "").strip() or None
    if model is not None and model not in MODELS and model != config.AVATAR_GPU_MODEL:
        return False
    prev = current_model()
    _MODEL_OVERRIDE = model
    if current_model() != prev:
        _LOAD_FAILED = _ZIMAGE_FAILED = False
        _LOAD_FAILED_AT = _ZIMAGE_FAILED_AT = 0.0
        unload(reason=f"смена модели на {current_model()}")
        log.info("Модель генерации аватарок: %s (выбор из админки)", current_model())
    return True


def _is_zimage() -> bool:
    return "z-image" in current_model().lower()


def _load_failed_now() -> bool:
    """_LOAD_FAILED, но с кулдауном — ровно как у _ZIMAGE_FAILED (см. _zimage_png).

    Флаг гасит ВСЮ GPU-генерацию (его читает available(), а generate_png начинается с
    `if not available(): return None`), а взводится он одним неудачным `_load()`. Без кулдауна
    разовый OOM при загрузке запасного sd-turbo сажал всех клиентов на процедурный рисунок
    ДО ПЕРЕЗАПУСКА бота: тумблер «включить ИИ» в админке флаг не снимает, снимает только смена
    модели. Тот же довод уже записан у Z-Image — здесь он забылся."""
    global _LOAD_FAILED
    if not _LOAD_FAILED:
        return False
    import time as _t
    if _LOAD_FAILED_AT and (_t.time() - _LOAD_FAILED_AT) >= _ZIMAGE_RETRY_SEC:
        _LOAD_FAILED = False
        log.info("Кулдаун загрузки GPU-генератора истёк — пробую снова")
        return False
    return True


def available() -> bool:
    """GPU-генерация включена и реально возможна (torch стоит, CUDA видит карту)."""
    if not config.AVATAR_GPU or _load_failed_now() or _OFF_FLAG.exists():
        return False
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def _worker_alive() -> bool:
    return _WORKER is not None and _WORKER[0].is_alive()


def _stop_worker() -> None:
    """Погасить воркер Z-Image (мягко через None, затем kill). Вызывать под _LOCK."""
    global _WORKER
    w, _WORKER = _WORKER, None
    if not w:
        return
    proc, conn = w[0], w[1]
    try:
        conn.send(None)
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    proc.join(timeout=10)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=5)
    if proc.is_alive():
        # Ссылку _WORKER мы уже обнулили выше, значит найти этот процесс изнутри бота больше
        # нечем: _worker_alive() отдаст False, автовыгрузка по простою решит, что гасить нечего,
        # и живой воркер с моделью останется висеть до перезапуска. Молчать об этом нельзя —
        # именно так и выглядит «простаивающий python-процесс на 15 ГБ» в диспетчере задач.
        log.warning("Воркер Z-Image PID %s не умер после kill — возможна сирота с моделью в ОЗУ",
                    proc.pid)


def unload(reason: str = "выключен ярлыком") -> bool:
    """Выгрузить пайплайн из VRAM/RAM (ярлык «выключить ИИ» ac6c7e0d / простой a2e67f96).
    True — выгрузили. reason — для честной строки в логе."""
    global _PIPE, _PIPE_MODEL
    with _LOCK:
        had = _PIPE is not None or _WORKER is not None
        if not had:
            return False
        _PIPE, _PIPE_MODEL = None, ""
        _stop_worker()
    try:
        import gc
        gc.collect()
        import torch
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    log.info("GPU-генератор аватарок выгружен из VRAM (%s)", reason)
    return True


def refresh_from_flag() -> bool:
    """Перечитать файл-флаг: выключен → выгрузить VRAM сейчас. Возвращает 'включено?'."""
    if _OFF_FLAG.exists():
        unload()
        return False
    return available()


def set_enabled(on: bool) -> bool:
    """Вкл/выкл ИИ-генерацию из админки (OpenTodo ee064f93) — тем же файлом-флагом, что
    ярлык на рабочем столе (ac6c7e0d): источник истины один, состояния не расходятся.
    Выключение сразу освобождает VRAM. False — флаг не переключился (ошибка ФС)."""
    try:
        if on:
            _OFF_FLAG.unlink(missing_ok=True)
        else:
            _OFF_FLAG.touch()
    except OSError:
        log.warning("не удалось переключить файл-флаг ИИ-генерации", exc_info=True)
        return False
    refresh_from_flag()
    return True


def status() -> dict:
    """Статус ИИ-генератора для экрана «Статус системы» (OpenTodo 689d72bb). models — каталог
    для переключателя модели в админке; модель из .env вне каталога тоже попадает в список,
    чтобы после экспериментов было куда вернуться."""
    import time
    _w = _WORKER            # см. "zimage_lite" ниже: глобал читаем один раз, без гонки
    cur = current_model()
    models = [{"id": mid, "label": m["label"], "desc": m["desc"], "current": mid == cur}
              for mid, m in MODELS.items()]
    if config.AVATAR_GPU_MODEL not in MODELS:
        models.append({"id": config.AVATAR_GPU_MODEL,
                       "label": config.AVATAR_GPU_MODEL.split("/")[-1],
                       "desc": "модель из .env", "current": config.AVATAR_GPU_MODEL == cur})
    return {"enabled": bool(config.AVATAR_GPU and not _OFF_FLAG.exists()),
            "loaded": _PIPE is not None or _worker_alive(),
            "model": cur,
            "model_label": str(MODELS.get(cur, {}).get("label") or cur.split("/")[-1]),
            "model_from_env": _MODEL_OVERRIDE is None,
            "models": models,
            # простой: 0 пока грузимся/генерим (_BUSY), иначе минуты от последней готовности
            "idle_min": 0.0 if _BUSY else (round((time.time() - _LAST_USED) / 60, 1) if _LAST_USED else None),
            "off_by_flag": _OFF_FLAG.exists(), "load_failed": _LOAD_FAILED,
            "zimage_failed": _ZIMAGE_FAILED,
            # Облегчённый квант сейчас в работе (воркер жив и поднят как lite). Кортеж снимаем
            # в ЛОКАЛЬНУЮ переменную: `_worker_alive() and _WORKER[2]` читает глобал дважды, а
            # между чтениями стоит proc.is_alive() — системный вызов, на время которого GIL
            # отпущен. Успевший в это окно _stop_worker() (выгрузка по простою из фонового цикла
            # run.py, кнопка «Выгрузить», таймаут генерации) обнуляет _WORKER, и второе чтение
            # даёт TypeError — а status() зовётся ПРЯМО в event loop без try/except, то есть
            # мастер получает 500 на весь экран «Статус системы».
            "zimage_lite": bool(_w and _w[0].is_alive() and _w[2]),
            # каких пакетов ИИ-стека нет — причина, по которой Z-Image не поднимется (02.09)
            "stack_missing": stack_missing()}


_STACK_MODULES = ("torch", "diffusers", "accelerate", "gguf")


def stack_missing() -> list[str]:
    """Каких пакетов ИИ-стека нет в окружении. 02.09.2026: из .venv пропал accelerate — Z-Image
    падал в воркере на ImportError через ~10 с после КАЖДОГО спавна (и так каждые 15 минут
    кулдауна), а картинки молча рисовал sd-turbo. Проверка через find_spec ничего не импортирует
    (миллисекунды), поэтому её можно звать на каждую генерацию. gguf нужен только под квант."""
    import importlib.util
    need = [m for m in _STACK_MODULES
            if m != "gguf" or config.AVATAR_GPU_GGUF or config.AVATAR_GPU_GGUF_LITE]
    return [m for m in need if importlib.util.find_spec(m) is None]


def maybe_idle_unload(idle_minutes: float) -> bool:
    """Выгрузить модель из VRAM/RAM, если ею не пользовались дольше idle_minutes (a2e67f96).
    True — выгрузили. Следующая генерация загрузит заново — фронт покажет ИИ-анимацию.

    Простой отсчитываем от МОМЕНТА ПОЛНОЙ загрузки/последней генерации (_LAST_USED), а не от
    начала загрузки: пока модель грузится (может тянуться до _WORKER_LOAD_TIMEOUT) или идёт
    генерация — _BUSY=True, и тогда это НЕ простой (иначе долгая загрузка «съедала» окно
    простоя и модель выгружалась почти сразу после готовности)."""
    import time
    if _BUSY:                                              # грузимся/генерим — простой ещё не начался
        return False
    if (_PIPE is None and not _worker_alive()) or idle_minutes <= 0:
        return False
    if time.time() - _LAST_USED < idle_minutes * 60:
        return False
    return unload(reason=f"простой {idle_minutes:g} мин")


def _load_zimage(model: str, gguf: str | None = None) -> None:
    """Z-Image-Turbo (2a00eac6): DiT из GGUF-кванта + enable_model_cpu_offload.
    Строго bf16 — fp16 у Z-Image даёт NaN в латентах (чёрные картинки, issue #14).
    .to('cuda') НЕ зовём: целиком пайплайн (~20 ГБ) в 12 ГБ не влезает, offload
    держит на карте только работающий модуль (пик ~8–9 ГБ с Q8_0).
    gguf — какой квант грузить (основной или облегчённый); None = AVATAR_GPU_GGUF из .env."""
    global _PIPE
    import torch
    from diffusers import GGUFQuantizationConfig, ZImagePipeline, ZImageTransformer2DModel
    gguf = config.AVATAR_GPU_GGUF if gguf is None else gguf
    transformer = None
    if gguf:
        transformer = ZImageTransformer2DModel.from_single_file(
            gguf,
            quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
            torch_dtype=torch.bfloat16,
            config=model, subfolder="transformer")
    pipe = ZImagePipeline.from_pretrained(
        model, transformer=transformer, torch_dtype=torch.bfloat16)
    pipe.enable_model_cpu_offload()
    # Снимаем offload-хук с текст-энкодера: иначе его forward утащит 8 ГБ bf16-весов Qwen
    # на карту — именно эта фаза и была пиком VRAM (замер 21.07: 7.9 ГиБ, кванты DiT не
    # помогали). Энкодер навсегда остаётся в RAM, эмбеддинги считаются на CPU (~3 с).
    from accelerate.hooks import remove_hook_from_module
    remove_hook_from_module(pipe.text_encoder, recurse=True)
    # VAE-декод 1024² одним куском — заметный пик; тайловый декод срезает его бесплатно
    try:
        pipe.enable_vae_tiling()
    except AttributeError:
        pipe.vae.enable_tiling()
    pipe.set_progress_bar_config(disable=True)
    _PIPE = pipe


def _load(model: str | None = None, gguf: str | None = None) -> None:
    """Однократная загрузка пайплайна на GPU (под _LOCK). Может качать модель с HF.
    model — какой пайплайн грузить: настроенный (по умолчанию) или лёгкий запасной;
    gguf — квант DiT для Z-Image (None = из .env)."""
    global _PIPE, _PIPE_MODEL
    model = model or current_model()
    import torch
    log.info("Загружаю %s на GPU (первый раз может скачивать модель с HuggingFace)", model)
    # Скорость (OpenTodo 0afd7b48): TF32-матмулы + channels_last у UNet. ВАЖНО: cudnn.benchmark
    # НЕ включаем — его автоперебор алгоритмов на первом прогоне стоил ~25–30 с (живой замер
    # 2026-06-11 на 5070/Blackwell), а выигрыш для turbo-моделей нулевой (0.15 с и так).
    torch.set_float32_matmul_precision("high")
    if "z-image" in model.lower():
        _load_zimage(model, gguf)
    else:
        from diffusers import AutoPipelineForText2Image
        pipe = AutoPipelineForText2Image.from_pretrained(
            model, torch_dtype=torch.float16, variant="fp16")
        pipe.to("cuda")
        try:
            pipe.unet.to(memory_format=torch.channels_last)
        except Exception:  # noqa: BLE001
            pass
        pipe.set_progress_bar_config(disable=True)
        _PIPE, _PIPE_MODEL = pipe, model
    log.info("GPU-генератор аватарок готов: %s на %s", model, torch.cuda.get_device_name(0))


def _worker_main(conn, model: str, gguf: str | None = None, need_mb: int | None = None) -> None:
    """Тело spawn-процесса-воркера Z-Image: загрузить пайплайн, отвечать на запросы
    (styled_prompt, seed, steps, size) → ("ok", png_bytes, peak_mb) | ("err", msg). size —
    int (квадрат) или (w, h). Живёт до None/обрыва канала. Вся оперативка возвращается ОС
    со смертью процесса. model/gguf/need_mb приходят аргументами: выбор модели и решение
    «основной или облегчённый квант» живут в родителе, spawn-процесс видит только .env."""
    try:
        # Жёсткий бюджет VRAM (переход на лёгкий квант, 21.07): кэш-аллокатор torch иначе
        # съедает ВСЁ свободное (замер: пик 11.4 из 12 ГБ при реальной потребности ~6.2),
        # душа рабочий стол и игры. Кап = порог гейта _need_mb: обещали столько — столько и
        # берём. Превышение = OOM → ("err") → снаружи сработает запасной sd-turbo.
        import torch
        total_mb = torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
        torch.cuda.set_per_process_memory_fraction(
            min(1.0, (need_mb or _need_mb(model)) / total_mb))
        _load(model, gguf)
    except Exception as exc:  # noqa: BLE001
        try:
            conn.send(("load_failed", repr(exc)))
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        conn.send(("ready", None))
    except Exception:  # noqa: BLE001
        return
    import torch
    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            break
        if req is None:
            break
        styled, seed, steps, size = req
        w, h = (int(size[0]), int(size[1])) if isinstance(size, (tuple, list)) else (int(size), int(size))
        try:
            # Эмбеддинги — на CPU (энкодер там и живёт, см. _load_zimage); на карте только
            # DiT+VAE: ≤6.2 ГиБ на Q6_K@1024 — укладывается в кап 7 ГиБ (замер 21.07)
            torch.cuda.reset_peak_memory_stats()
            emb, _neg = _PIPE.encode_prompt(styled, device=torch.device("cpu"),
                                            do_classifier_free_guidance=False)
            emb = [e.to("cuda", torch.bfloat16) for e in emb]
            gen = torch.Generator("cuda").manual_seed(seed % (2 ** 31))
            image = _PIPE(prompt_embeds=emb, num_inference_steps=steps, guidance_scale=0.0,
                          width=w, height=h, generator=gen).images[0]
            buf = io.BytesIO()
            image.convert("RGB").save(buf, "PNG", optimize=True)
            # пик аллокатора за генерацию — родитель пишет его в лог: по нему калибруются
            # бюджеты _NEED_ZIMAGE/_NEED_ZIMAGE_LITE (снаружи nvidia-smi врёт — кэш-аллокатор)
            peak_mb = int(torch.cuda.max_memory_allocated() // (1024 * 1024))
            conn.send(("ok", buf.getvalue(), peak_mb))
        except Exception as exc:  # noqa: BLE001
            try:
                conn.send(("err", repr(exc)))
            except Exception:  # noqa: BLE001
                break
        finally:
            # Чистим кэш-аллокатор между генерациями: под жёстким капом 7 ГиБ фрагментация
            # копится и рано или поздно даёт OOM на генерации → тихий фолбэк на sd-turbo (часть
            # жалобы «Z-Image деградирует»). Стоит ~мс, качество/детерминизм не трогает.
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass


def _prefetch_gguf(url: str) -> None:
    """Скачать GGUF в кэш HF ДО спавна воркера. Внутри воркера первое скачивание (~6 ГБ)
    упиралось в _WORKER_LOAD_TIMEOUT: воркер убивали на середине, кэш оставался пустым, и
    КАЖДАЯ следующая попытка качала с нуля — Z-Image не поднимался никогда (замер 21.07).
    В родителе таймаута нет; уже скачанный файл hf_hub_download отдаёт мгновенно."""
    url = (url or "").strip()
    m = re.match(r"https://huggingface\.co/([^/]+/[^/]+)/(?:blob|resolve)/[^/]+/(.+)$", url)
    if not m:
        return
    from huggingface_hub import hf_hub_download
    hf_hub_download(m.group(1), m.group(2))


def _ensure_worker(lite: bool = False) -> bool:
    """Поднять воркер Z-Image, если ещё не жив (под _LOCK). False — не получилось
    (сбой загрузки помечает _ZIMAGE_FAILED; запасной sd-turbo при этом продолжает работать).
    lite — облегчённый квант (AVATAR_GPU_GGUF_LITE) с меньшим капом VRAM, когда основной
    не проходит гейт по свободной памяти (02.09.2026)."""
    global _WORKER, _ZIMAGE_FAILED, _ZIMAGE_FAILED_AT
    if _worker_alive():
        return True
    _stop_worker()   # прибрать труп, если процесс умер сам
    model = current_model()
    gguf = config.AVATAR_GPU_GGUF_LITE if lite else config.AVATAR_GPU_GGUF
    need = _need_mb(model, lite)
    try:
        _prefetch_gguf(gguf)
    except Exception:  # noqa: BLE001
        log.warning("Префетч GGUF не удался — воркер попробует скачать сам", exc_info=True)
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_worker_main, args=(child, model, gguf, need),
                       daemon=True, name="zimage-avatar")
    log.info("Поднимаю воркер Z-Image (квант %s, кап %d МБ; загрузка модели ~40 c; RAM живёт "
             "в его процессе)", _gguf_label(gguf), need)
    proc.start()
    child.close()
    kind, payload = "timeout", None
    if parent.poll(_WORKER_LOAD_TIMEOUT):
        try:
            kind, payload = parent.recv()
        except (EOFError, OSError):
            kind = "dead"
    if kind == "ready":
        _WORKER = (proc, parent, lite)
        return True
    if kind == "load_failed":
        import time as _t
        _ZIMAGE_FAILED, _ZIMAGE_FAILED_AT = True, _t.time()
        log.warning("Не удалось загрузить Z-Image в воркере — ~%d мин рисует запасной %s "
                    "(потом ещё попытка): %s", int(_ZIMAGE_RETRY_SEC // 60), _FALLBACK_MODEL, payload)
    else:
        # Кулдаун нужен и здесь, а не только при load_failed. Иначе КАЖДЫЙ следующий клик по
        # «Аватару» заново спавнит воркер и ждёт до _WORKER_LOAD_TIMEOUT (240 c): мастер видит
        # четыре минуты «думаю», и так до бесконечности. Через _ZIMAGE_RETRY_SEC тир Z-Image
        # снова получит шанс — транзиентный сбой не блокирует его навсегда.
        import time as _t
        _ZIMAGE_FAILED, _ZIMAGE_FAILED_AT = True, _t.time()
        log.warning("Воркер Z-Image не поднялся (%s) — ~%d мин рисует запасной %s",
                    kind, int(_ZIMAGE_RETRY_SEC // 60), _FALLBACK_MODEL)
    try:
        parent.close()
    except Exception:  # noqa: BLE001
        pass
    proc.kill()
    proc.join(timeout=5)
    if proc.is_alive():
        log.warning("Воркер Z-Image PID %s пережил kill при неудачном подъёме", proc.pid)
    return False


def _gpu_snapshot() -> tuple[float, float] | None:
    """(утилизация %, свободно МБ) из nvidia-smi. None — smi нет/сломан → считаем карту
    свободной (поведение как раньше). Зовётся один раз на генерацию, только перед ЗАГРУЗКОЙ."""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        util, used, total = (float(x) for x in out.stdout.strip().split("\n")[0].split(","))
        return util, total - used
    except Exception:  # noqa: BLE001
        return None


def _need_mb(model: str, lite: bool = False) -> int:
    """Бюджет VRAM модели: и порог свободной памяти для гейта, и жёсткий кап воркера
    (set_per_process_memory_fraction). Z-Image на Q6_K + VAE-тайлинг укладывается в 7 ГиБ
    (замер 21.07: потребность ~6.9 ГиБ; переход с Q8/9000 — рабочий стол держит 3–4 ГБ
    из 12, и прежний порог отсекал машину всегда). lite — облегчённый квант Q4_K_M на 768²
    (02.09: рабочий стол со слайсером и браузером держал 5.9–6.3 ГБ, и 7168 отсекал всегда).
    sdxl ~7 ГБ, sd-turbo ~3 ГБ."""
    m = model.lower()
    if "z-image" in m:
        return _NEED_ZIMAGE_LITE if lite else _NEED_ZIMAGE
    return 7168 if "xl" in m else 3500


def _has_cyrillic(s: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in s)


def _gtx_translate(prompt: str) -> str | None:
    """RU→EN бесплатным gtx-эндпоинтом Google (как modelsearch.translate_query). None — сбой:
    429 (квота — 02.09 хватило трёх одинаковых запросов за 4 минуты), нет сети, пустой ответ."""
    try:
        import httpx
        r = httpx.get("https://translate.googleapis.com/translate_a/single",
                      params={"client": "gtx", "sl": "ru", "tl": "en", "dt": "t", "q": prompt},
                      timeout=5.0)
        r.raise_for_status()
        parts = [seg[0] for seg in (r.json()[0] or []) if seg and seg[0]]
        return "".join(parts).strip() or None
    except Exception as exc:  # noqa: BLE001
        # раньше это был debug — и 429 в логе был виден только строкой httpx; сбой перевода
        # меняет картинку, поэтому пишем явно
        log.info("gtx-перевод промта не удался (%s) — пробую офлайн-переводчик", exc)
        return None


def _offline_translate(prompt: str) -> str | None:
    """Офлайн RU→EN на CPU (MarianMT opus-mt-ru-en) — страховка от 429/отсутствия сети у gtx.
    Замер 02.09: перевод 0.1–0.3 с, загрузка с диска ~1–2 с (первый раз — скачивание ~300 МБ),
    ~300 МБ ОЗУ резидентно. Качество ниже Google (диминутивы: «ёжик» → «yeast»), но CLIP
    получает осмысленный английский вместо кириллицы. NLLB-200-600M проверен и отвергнут:
    1–2.6 с, 2.4 ГБ и ошибок не меньше («наушники» → «earrings»).
    None — пакетов нет (sentencepiece/transformers — модуль requirements-ai) или загрузка не удалась;
    после первого провала не ретраим до перезапуска."""
    global _OFFLINE_TR, _OFFLINE_TR_FAILED
    if _OFFLINE_TR_FAILED:
        return None
    try:
        import torch
        with _OFFLINE_TR_LOCK:
            if _OFFLINE_TR is None:
                from transformers import MarianMTModel, MarianTokenizer
                tok = MarianTokenizer.from_pretrained(_OFFLINE_TR_MODEL)
                mdl = MarianMTModel.from_pretrained(_OFFLINE_TR_MODEL).eval()
                _OFFLINE_TR = (tok, mdl)
                log.info("Офлайн-переводчик промтов загружен: %s", _OFFLINE_TR_MODEL)
            tok, mdl = _OFFLINE_TR
            with torch.inference_mode():
                out = mdl.generate(**tok([prompt], return_tensors="pt"), num_beams=2, max_new_tokens=96)
            text = tok.decode(out[0], skip_special_tokens=True).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        _OFFLINE_TR_FAILED = True
        log.warning("Офлайн-переводчик промтов недоступен (%r) — при сбое gtx промт уйдёт в модель "
                    "как есть (до перезапуска)", exc)
        return None


def _to_english(prompt: str) -> str:
    """RU→EN для диффузионной модели: gtx (Google, точнее) → офлайн MarianMT → оригинал.
    Успешные переводы кэшируются: кнопка «↻ Заменить» и панель пробы шлют ОДИН промт много раз
    подряд — именно так 02.09 и словили 429 (плюс поиск моделей ходит в тот же эндпоинт).
    Неудачу не кэшируем — следующий клик попробует снова. Латиница/пусто — как есть."""
    key = (prompt or "").strip()
    if not key or not _has_cyrillic(key):
        return prompt
    hit = _TR_CACHE.get(key)
    if hit:
        return hit
    en = _gtx_translate(key) or _offline_translate(key)
    if not en:
        log.warning("Промт не переведён (gtx и офлайн-переводчик недоступны) — модель получит "
                    "русский текст: %r", key)
        return prompt
    if len(_TR_CACHE) > _TR_CACHE_MAX:
        _TR_CACHE.clear()
    _TR_CACHE[key] = en
    return en


def _zimage_png(en_prompt: str, seed_int: int, snap: tuple[float, float] | None,
                styled: bool = True, scene: bool = False) -> tuple[bytes, str] | None:
    """Тир Z-Image (воркер-процесс; под _LOCK) → (png, "z-image-turbo" | "z-image-turbo-lite").
    None — тир не смог: мало VRAM даже под облегчённый квант, воркер не поднялся или сорвал
    генерацию. Вызывающий уходит на запасной sd-turbo.

    Выбор кванта (02.09.2026) — по свободной VRAM ПЕРЕД загрузкой: хватает на Q6_K → основной;
    хватает только на Q4_K_M → облегчённый (та же модель, чуть мягче детали, 768-класс);
    иначе None. Живой воркер рисует тем квантом, с которым поднялся, до выгрузки по простою —
    после неё выбор повторится уже по новой картине памяти."""
    global _ZIMAGE_FAILED
    if _ZIMAGE_FAILED:
        # Не латчим навсегда: после кулдауна даём Z-Image ещё попытку — разовый сбой загрузки
        # (транзиентный OOM/сеть) иначе НАВСЕГДА (до рестарта) сажал бы на sd-turbo, и мастер
        # видел бы стабильно худшую картинку («деградировала»). Кулдаун не прошёл → запасной.
        import time as _t
        if _ZIMAGE_FAILED_AT and (_t.time() - _ZIMAGE_FAILED_AT) >= _ZIMAGE_RETRY_SEC:
            _ZIMAGE_FAILED = False
            log.info("Кулдаун Z-Image истёк — пробую загрузить снова")
        else:
            return None
    model = current_model()
    if not _worker_alive():
        lite = False
        if snap and snap[1] < _need_mb(model):
            if config.AVATAR_GPU_GGUF_LITE and snap[1] >= _need_mb(model, lite=True):
                lite = True
                log.info("Свободной VRAM %.0f МБ < %d — Z-Image в облегчённом кванте %s (порог %d)",
                         snap[1], _need_mb(model), _gguf_label(config.AVATAR_GPU_GGUF_LITE),
                         _need_mb(model, lite=True))
            else:
                floor = _need_mb(model, lite=True) if config.AVATAR_GPU_GGUF_LITE else _need_mb(model)
                log.info("Мало свободной VRAM (%.0f МБ < %d) даже для облегчённого Z-Image — "
                         "пробую запасной %s", snap[1], floor, _FALLBACK_MODEL)
                return None
        if not _ensure_worker(lite):
            return None
    _proc, conn, lite = _WORKER
    engine = "z-image-turbo-lite" if lite else "z-image-turbo"
    prompt = _final_prompt(en_prompt, model, styled, scene)
    steps = max(4, min(12, _steps_for(model)))
    size = _zimage_size(lite, scene)
    import time as _t
    t0 = _t.monotonic()
    try:
        conn.send((prompt, seed_int, steps, size))
        if not conn.poll(_WORKER_GEN_TIMEOUT):
            log.warning("Воркер Z-Image молчит >%.0f с — глушу, пробую запасной %s",
                        _WORKER_GEN_TIMEOUT, _FALLBACK_MODEL)
            _stop_worker()
            return None
        msg = conn.recv()
    except (EOFError, OSError, BrokenPipeError):
        log.warning("Канал воркера Z-Image оборвался — пробую запасной %s", _FALLBACK_MODEL)
        _stop_worker()
        return None
    kind, payload = msg[0], msg[1:]
    if kind == "ok":
        peak = int(payload[1]) if len(payload) > 1 else 0
        log.info("Z-Image %s %d×%d: %.1f с, пик VRAM %d МБ (кап %d)",
                 _gguf_label(config.AVATAR_GPU_GGUF_LITE if lite else config.AVATAR_GPU_GGUF),
                 size[0], size[1], _t.monotonic() - t0, peak, _need_mb(model, lite))
        return payload[0], engine
    log.warning("Воркер Z-Image не смог сгенерировать: %s — пробую запасной %s",
                payload[0] if payload else "?", _FALLBACK_MODEL)
    return None


def _inprocess_png(en_prompt: str, seed_int: int, snap: tuple[float, float] | None,
                   model: str, styled: bool = True, scene: bool = False) -> bytes | None:
    """Тир sd/sdxl-turbo в ЭТОМ процессе (под _LOCK): настроенная лёгкая модель или запасной.
    None — не смог (мало VRAM / загрузка сломана / сбой генерации / промт не переведён)
    → процедурный рендер."""
    global _PIPE, _PIPE_MODEL, _LOAD_FAILED, _LOAD_FAILED_AT
    if _load_failed_now():
        return None
    if _has_cyrillic(en_prompt):
        # CLIP у sd/sdxl знает только английский: русский промт для него шум — так 02.09 вышли
        # «город» и «барочная рама» вместо маскота. Честнее процедурный рендер: он хотя бы читает
        # промт (уши/клюв/шапка). Z-Image сюда не попадает — его Qwen-энкодер многоязычный.
        log.info("Промт остался по-русски (переводчики недоступны) — %s его не поймёт, рисую процедурно",
                 model)
        return None
    if _PIPE is not None and _PIPE_MODEL != model:   # в 12 ГБ два пайплайна не держим
        _PIPE, _PIPE_MODEL = None, ""
    if _PIPE is None:
        if snap and snap[1] < _need_mb(model):
            log.info("Мало свободной VRAM (%.0f МБ < %d) для загрузки %s — рисую процедурно",
                     snap[1], _need_mb(model), model)
            return None
        try:
            _load(model)
        except Exception:  # noqa: BLE001
            import time as _t
            _LOAD_FAILED, _LOAD_FAILED_AT = True, _t.time()
            log.warning("Не удалось загрузить GPU-генератор аватарок — %d мин рисуем процедурно "
                        "(потом ещё попытка)", int(_ZIMAGE_RETRY_SEC // 60), exc_info=True)
            return None
    prompt = _final_prompt(en_prompt, model, styled, scene)
    try:
        import torch
        gen = torch.Generator("cuda").manual_seed(seed_int % (2 ** 31))
        image = _PIPE(prompt=prompt,
                      num_inference_steps=max(1, min(8, _steps_for(model))),
                      guidance_scale=0.0,            # так обучены turbo-модели (без CFG)
                      width=512, height=512, generator=gen).images[0]
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        log.warning("GPU-генерация аватарки не удалась — откат на процедурный рендер",
                    exc_info=True)
        return None


def generate_png(prompt: str, seed_int: int, styled: bool = True,
                 scene: bool = False) -> tuple[bytes, str] | None:
    """(PNG, имя движка) по весёлому промту. None при любом сбое — вызывающий откатится на
    процедурный. Цепочка тиров (фидбэк 21.07 «почему процедурный вместо ИИ», ступень lite —
    02.09): настроенная модель (Z-Image в воркере: основной квант, при нехватке VRAM —
    облегчённый) → лёгкий запасной sd-turbo → None. Сбой ЗАГРУЗКИ помечает свой тир
    сломанным до перезапуска; сбой одной генерации — нет.
    styled=False — промт уходит в модель без фирменного стиля маскота (панель «Проба
    генерации» в админке), перевод RU→EN сохраняется. scene=True — иллюстрация к release
    note: стиль сцены и кадр 4:3 вместо портрета-иконки.

    Защита от зависаний (655318e9): GPU занята игрой → сразу None (процедурный фолбэк);
    параллельная генерация уже идёт дольше 10 с → тоже None, не строим очередь на минуты."""
    global _LAST_USED, _BUSY, _STACK_WARNED
    if not available():
        return None
    # Перевод — ДО лока и до снимка GPU: это сеть (до 5 с) или CPU-модель (~0.3 с), и держать
    # под ними GPU-лок означало бы блокировать параллельную генерацию ради перевода.
    en = _to_english(prompt)
    # снимок nvidia-smi (~0.5–1 с) нужен только перед ЗАГРУЗКОЙ модели: загрузка при занятой
    # карте уходит в своп на минуты. Уже загруженная модель генерит и под нагрузкой.
    snap = None
    if _PIPE is None and not _worker_alive():
        snap = _gpu_snapshot()
        if snap and snap[0] >= 70:
            log.info("GPU занята другим процессом (util %.0f%%) — аватарка процедурно", snap[0])
            return None
    if not _LOCK.acquire(timeout=10):
        log.info("GPU-генератор занят другой генерацией >10 с — аватарка процедурно")
        return None
    import time as _time
    # НЕ ставим _LAST_USED в НАЧАЛЕ (до загрузки): иначе отсчёт простоя стартовал бы с момента
    # НАЧАЛА загрузки, и холодная модель (~40 c, до 240 c) выгружалась бы почти сразу после
    # готовности. Пока грузимся/генерим — _BUSY гасит выгрузку; _LAST_USED выставляем в finally,
    # когда модель уже ПОЛНОСТЬЮ загружена и отработала (это и есть точка отсчёта простоя).
    _BUSY = True
    # Модель читаем ОДИН раз, уже под _LOCK. Раньше её читали дважды — _is_zimage() на входе и
    # current_model() перед вызовом, — а set_model пишет _MODEL_OVERRIDE (:134) ДО того, как
    # сам заблокируется на _LOCK внутри unload() (:138). В окно между двумя чтениями
    # переключение админкой на Z-Image уводило генерацию в _inprocess_png С Z-IMAGE, то есть
    # грузило 6B-пайплайн с offload-копиями В ПРОЦЕСС БОТА — ровно то, что запрещает шапка
    # модуля: оттуда эта память (20+ ГБ) не возвращается ОС даже после unload().
    model = current_model()
    try:
        if "z-image" in model.lower():
            miss = stack_missing()
            if miss:
                # Без accelerate/gguf воркер упадёт на ImportError через ~10 с после спавна — и так
                # каждые 15 минут кулдауна (02.09). Не спавним вовсе; предупреждаем один раз.
                if not _STACK_WARNED:
                    _STACK_WARNED = True
                    log.warning("Стек ИИ неполный — нет %s: Z-Image пропускаю, рисует запасной %s. "
                                "Лечение: pip install -r requirements-ai.txt", ", ".join(miss),
                                _FALLBACK_MODEL)
            else:
                res = _zimage_png(en, seed_int, snap, styled, scene)
                if res is not None:
                    return res
            # Z-Image не смог — лёгкий запасной вместо процедурной заглушки (фидбэк 21.07).
            # Снимка здесь может не быть: выше (:snap) его берут только когда воркера НЕТ. А сбой
            # самой ГЕНЕРАЦИИ воркер не гасит — он остаётся жив и держит карту, и запасной пошёл бы
            # грузиться вслепую: гейт свободной VRAM в _inprocess_png при snap=None молчит. На
            # тесной карте (ступень lite: кап 5632 из 12 ГБ, свободного остатка ~0.4 ГБ) это ровно
            # OOM при загрузке — а он взводит _LOAD_FAILED. Берём снимок здесь: гейт честно
            # откажет, вернётся процедурный рисунок, и ИИ-генерация остальным не ломается.
            if snap is None:
                snap = _gpu_snapshot()
            png = _inprocess_png(en, seed_int, snap, _FALLBACK_MODEL, styled, scene)
            return (png, _FALLBACK_MODEL.split("/")[-1]) if png is not None else None
        png = _inprocess_png(en, seed_int, snap, model, styled, scene)
        return (png, model.split("/")[-1]) if png is not None else None
    finally:
        # Точка отсчёта простоя: модель загружена и только что отработала. Ставим ДО снятия _BUSY,
        # чтобы фоновый цикл, увидев _BUSY=False, читал уже свежую метку (порядок важен).
        _LAST_USED = _time.time()
        _BUSY = False
        _LOCK.release()
