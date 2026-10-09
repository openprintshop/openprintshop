"""Пул ПРОЦЕССОВ для CPU-тяжёлой работы (растеризация превью, дециация/загрузка мешей).

Зачем: растеризатор превью и упрощение мешей — это плотные питоновские циклы по десяткам
тысяч граней, которые удерживают GIL надолго. Если выполнять их через ``asyncio.to_thread``,
они не дают работать общему event loop'у (бот и мини-апка живут в одном процессе) — пока
один клиент считает/рендерит, у других «виснет интерфейс». Вынос в отдельные процессы даёт
каждому свой GIL: основной цикл остаётся отзывчивым, расчёты идут по-настоящему параллельно.

Надёжность важнее ускорения: при ЛЮБОЙ проблеме с пулом (не поднялся, воркер умер,
объект не пиклится) — безопасно деградируем на ``asyncio.to_thread`` для этого вызова,
чтобы пул НИКОГДА не уронил расчёт. Функции, которые сюда передаём, должны быть модульного
уровня и принимать/возвращать пиклящиеся данные (пути-строки, dataclass'ы, кортежи).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor

from . import config

log = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_broken = False
_pool_breaks = 0        # поломок пула подряд; успешная задача обнуляет
_render_breaks = 0
POOL_BREAK_LIMIT = 3    # после стольких поломок подряд сдаёмся и уходим в потоки насовсем

# Отдельный одиночный РЕНДЕР-воркер (OpenTodo 745aa243): GPU-растеризация живёт ТОЛЬКО в нём.
# Почему не в общем пуле: там 4 воркера с ресайклингом (max_tasks_per_child) — каждый тащил бы
# свой torch (~1 ГБ RAM) и свой CUDA-контекст (VRAM), а после ресайкла импортировал бы всё
# заново — «кушается много ОЗУ». Один воркер: torch и CUDA-контекст один раз на RENDER_POOL_MAX_TASKS
# рендеров (совсем без ресайклинга он не отдавал память ОС никогда — см. _get_render_pool).
_render_pool: ProcessPoolExecutor | None = None
_render_broken = False


def _noop() -> bool:
    return True


def _init_worker() -> None:
    """Инициализатор процесса-воркера: настраивает логирование.

    Воркеры пула — отдельные процессы (на Windows spawn) и НЕ наследуют logging-конфиг главного
    процесса (RotatingFileHandler из run.py). Без этого log.info/warning из geometry и подготовки
    мешей — в т.ч. предупреждение об обрезке деталей по MAX_MESH_FILES — молча пропадали: занижение
    расхода из-за лимита деталей не оставляло следа (как было с ветряком 61→40 дет.). Ставим
    StreamHandler на унаследованный stderr (его ловит лаунчер в bot_manual.err) — диагностика
    воркеров снова видна, без риска многопроцессной ротации одного bot.log."""
    try:
        root = logging.getLogger()
        if root.handlers:
            return
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [worker]: %(message)s"))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001 — логирование не должно мешать запуску воркера
        pass


def _get_pool() -> ProcessPoolExecutor | None:
    """Лениво создаёт пул. Если воркеров 0 или создать не удалось — возвращает None (→ поток)."""
    global _pool, _broken
    if _broken or config.CPU_POOL_WORKERS <= 0:
        return None
    if _pool is None:
        try:
            kwargs = {"max_workers": config.CPU_POOL_WORKERS, "initializer": _init_worker}
            if config.CPU_POOL_MAX_TASKS > 0:        # перезапуск воркера после N задач — отдаёт RAM ОС
                kwargs["max_tasks_per_child"] = config.CPU_POOL_MAX_TASKS
            try:
                _pool = ProcessPoolExecutor(**kwargs)
            except TypeError:                        # max_tasks_per_child только с Python 3.11+
                kwargs.pop("max_tasks_per_child", None)
                _pool = ProcessPoolExecutor(**kwargs)
        except Exception:  # noqa: BLE001
            log.warning("CPU process pool unavailable — fallback to threads", exc_info=True)
            _broken = True
            return None
    return _pool


async def run(func, *args, **kwargs):
    """Выполнить CPU-функцию в процессе-воркере; при любом сбое пула — в потоке (фолбэк).

    Функции тут идемпотентны/чисты (превью/инспект/подготовка мешей), поэтому повторный
    запуск в потоке после сбоя пула безопасен и просто гарантирует результат.

    kwargs: run_in_executor их не принимает, поэтому склеиваем functools.partial — он пикуется
    вместе с module-level функцией и её аргументами и спокойно уезжает в процесс-воркер.
    Заведено, чтобы стол принтера (bed=…) доезжал до превью/геометрии именем, а не позицией:
    позиционная передача через промежуточный max_faces ломается при первой же смене порядка
    параметров, причём молча — картинкой от чужого стола."""
    if kwargs:
        func, args = functools.partial(func, *args, **kwargs), ()
    global _pool, _broken, _pool_breaks
    pool = _get_pool()
    if pool is not None:
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(pool, func, *args)
            _pool_breaks = 0          # успешная задача — серия поломок прервана
            return res
        except BrokenExecutor:
            # Сломался САМ ПУЛ (воркер убит извне, OOM). Пул пересоздастся при следующем
            # вызове; глушим насовсем только после POOL_BREAK_LIMIT поломок подряд — иначе
            # разовая беда лишает бота процессов до перезапуска.
            _pool_breaks += 1
            _pool = None
            if _pool_breaks >= POOL_BREAK_LIMIT:
                _broken = True
                log.error("CPU pool ломается подряд %d раз — перехожу на потоки до перезапуска",
                          _pool_breaks)
            else:
                log.warning("CPU pool сломан (%s) — пересоздам на следующем вызове", func,
                            exc_info=True)
        except Exception:  # noqa: BLE001 — упала ЗАДАЧА (битый меш, ошибка пиклинга)
            # РАНЬШЕ здесь тоже стоял `_broken = True`, и обычный ValueError на одной кривой
            # модели НАВСЕГДА уводил всю тяжёлую геометрию и растеризацию в процесс бота —
            # без пула, без ресайклинга и без возврата памяти ОС. Сброса флага не было нигде.
            # Пул тут ни при чём: повторяем в потоке (функции идемпотентны), пул оставляем жить.
            log.warning("CPU pool task failed (%s) — переключаюсь на поток", func, exc_info=True)
    return await asyncio.to_thread(func, *args)


def _init_render_worker() -> None:
    """Инициализатор рендер-воркера: логирование + флаг, разрешающий GPU-растеризацию.
    Только процесс с этим флагом импортирует torch (см. preview._rasterize_gpu)."""
    _init_worker()
    os.environ["PCB_RENDER_WORKER"] = "1"


def _get_render_pool() -> ProcessPoolExecutor | None:
    global _render_pool, _render_broken
    if _render_broken or config.CPU_POOL_WORKERS <= 0:
        return None
    if _render_pool is None:
        try:
            # Жить долго — да (torch+CUDA грузятся один раз), но НЕ ВЕЧНО. До 11.08 здесь не
            # было max_tasks_per_child вовсе, и это делало рендер-воркер единственным процессом
            # бота, который не возвращал память ОС никогда: кэш аллокатора torch и разовые
            # огромные меши копились до перезапуска (наблюдалось 15.87 ГБ против 264 МБ у
            # свежего). Редкий ресайкл возвращает кучу ОС ценой ~4 с на перезагрузку torch.
            kwargs = {"max_workers": 1, "initializer": _init_render_worker}
            if config.RENDER_POOL_MAX_TASKS > 0:
                kwargs["max_tasks_per_child"] = config.RENDER_POOL_MAX_TASKS
            try:
                _render_pool = ProcessPoolExecutor(**kwargs)
            except TypeError:                        # max_tasks_per_child только с Python 3.11+
                kwargs.pop("max_tasks_per_child", None)
                _render_pool = ProcessPoolExecutor(**kwargs)
        except Exception:  # noqa: BLE001
            log.warning("render pool unavailable — рендер в общем пуле", exc_info=True)
            _render_broken = True
            return None
    return _render_pool


async def run_render(func, *args, **kwargs):
    """Выполнить РЕНДЕР (растеризацию превью) в выделенном воркере с GPU; при сбое —
    в общем пуле/потоке (там GPU выключен флагом, посчитает numpy).

    kwargs — как в run(): склеиваются в functools.partial (см. там же, почему по имени)."""
    if kwargs:
        func, args = functools.partial(func, *args, **kwargs), ()
    global _render_pool, _render_broken, _render_breaks
    pool = _get_render_pool()
    if pool is not None:
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(pool, func, *args)
            _render_breaks = 0
            return res
        except BrokenExecutor:
            # Ресайкл воркера по max_tasks_per_child штатный и НЕ ломает пул, но если воркер
            # убили извне — пересоздаём. Насовсем в numpy-фолбэк уходим только после серии.
            _render_breaks += 1
            _render_pool = None
            if _render_breaks >= POOL_BREAK_LIMIT:
                _render_broken = True
                log.error("Рендер-воркер ломается подряд %d раз — GPU-растеризация выключена",
                          _render_breaks)
            else:
                log.warning("Рендер-воркер сломан (%s) — пересоздам", func, exc_info=True)
        except Exception:  # noqa: BLE001 — упала задача, а не воркер: пул не трогаем
            log.warning("render pool task failed (%s) — фолбэк в общий пул", func, exc_info=True)
    return await run(func, *args)


async def warmup() -> None:
    """Прогрев: поднять воркеры заранее (импорт trimesh/numpy/PIL в них ~секунды), чтобы
    первый реальный расчёт не ждал холодный старт пула. Сбой прогрева не критичен."""
    try:
        await run(_noop)
        log.info("CPU pool прогрет (%d воркеров)", config.CPU_POOL_WORKERS)
    except Exception:  # noqa: BLE001
        log.debug("CPU pool warmup skipped", exc_info=True)
    try:
        from . import preview
        ok, info = await run_render(preview.gpu_selfcheck)
        log.info("Рендер-воркер прогрет: GPU-растеризация %s (%s)",
                 "АКТИВНА" if ok else "выключена", info)
    except Exception:  # noqa: BLE001
        log.debug("render pool warmup skipped", exc_info=True)


def shutdown(wait: bool = False) -> None:
    """Погасить пулы. wait=True — ДОЖДАТЬСЯ реальной смерти воркеров.

    Дефолт False сохраняет прежнее поведение обычной остановки (там следом всё равно умирает
    сам процесс, и воркеров добивает taskkill /T). Но при автообновлении следом идёт
    os._exit(0), который пропускает atexit-хук concurrent.futures — а именно этот хук штатно
    рассылает воркерам сентинелы. Без wait=True shutdown лишь выставляет флаг, и воркеры
    остаются висеть с дохлым родителем: EOF они не увидят никогда, потому что multiprocessing
    отдаёт ребёнку и читающий, и ПИШУЩИЙ конец очереди. Ровно так и появляется процесс на
    гигабайты, который простаивает и которого никто не убьёт (разбор 11.08)."""
    global _pool, _render_pool
    if _pool is not None:
        try:
            _pool.shutdown(wait=wait, cancel_futures=True)
        except Exception:  # noqa: BLE001
            log.debug("shutdown pool failed", exc_info=True)
        _pool = None
    if _render_pool is not None:
        try:
            _render_pool.shutdown(wait=wait, cancel_futures=True)
        except Exception:  # noqa: BLE001
            log.debug("shutdown render pool failed", exc_info=True)
        _render_pool = None
