"""Периодическое обслуживание: чистка временных файлов + компактизация SQLite.

Запускается фоновой задачей из run.py раз в сутки. Чистит превью/рендеры/webp/geo-кэш,
на которые никто не ссылается и которые старше CLEAN_AGE_DAYS; делает WAL-checkpoint и
(best-effort) VACUUM, чтобы файл БД и -wal не пухли.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)

CLEAN_AGE_DAYS = 14
_PREVIEW_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


async def _referenced_paths() -> set[str]:
    """Абсолютные пути файлов, на которые ссылается БД (их не удаляем).

    C2, КРИТИЧНО: в БД пути лежат ОТНОСИТЕЛЬНО DATA_DIR, а обход каталогов ниже сравнивает
    их со str(файл) — абсолютным. Без config.abs_data() ни одна ссылка не совпала бы, и
    суточная чистка снесла бы живые превью коллекции. Читаем БД напрямую (мимо store), так
    что нормализация обязана стоять именно здесь."""
    refs: set[str] = set()
    c = db._conn()
    queries = (
        "SELECT preview_path FROM collection WHERE preview_path IS NOT NULL",
        "SELECT photo_path FROM collection WHERE photo_path IS NOT NULL",
        "SELECT preview_path FROM saved_models WHERE preview_path IS NOT NULL",
        "SELECT preview_path FROM requests WHERE preview_path IS NOT NULL",
        "SELECT photo_path FROM plastics WHERE photo_path IS NOT NULL AND photo_path != ''",
    )
    for sql in queries:
        cur = await c.execute(sql)
        for r in await cur.fetchall():
            if r[0]:
                refs.add(str(config.abs_data(r[0])))
    return refs


async def _referenced_model_paths() -> set[str]:
    """Пути файлов моделей из коллекции и сохранённых моделей — их кэш загрузок не чистим
    (чтобы модель из коллекции не пришлось качать заново).

    Любая ошибка, кроме «нет колонки», ВЫЛЕТАЕТ наружу. Раньше глоталась любая, и «database is
    locked» в момент суточной чистки давал пустое множество, то есть «защищать нечего»: чистка
    снесла бы папки моделей, на которые ссылается живая коллекция. Пустой ответ здесь обязан
    значить «ссылок нет», а не «прочитать не вышло»."""
    refs: set[str] = set()
    c = db._conn()
    for sql in ("SELECT paths FROM collection WHERE paths IS NOT NULL",
                "SELECT paths FROM saved_models WHERE paths IS NOT NULL"):
        try:
            cur = await c.execute(sql)
            rows = await cur.fetchall()
        except sqlite3.OperationalError as e:
            if "no such column" not in str(e).lower():
                raise
            continue   # старая БД без колонки: эта таблица ссылок на модели хранить не может
        for r in rows:
            if not r[0]:
                continue
            try:
                for p in json.loads(r[0]):
                    refs.add(str(config.abs_data(p)))
            except (ValueError, TypeError):
                pass
    return refs


def _cleanup_dir(folder: Path, refs: set[str], exts: set[str], age_sec: float,
                 ignore_refs: bool = False) -> int:
    if not folder.exists():
        return 0
    cutoff = time.time() - age_sec
    removed = 0
    for f in folder.iterdir():
        try:
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            if f.stat().st_mtime > cutoff:
                continue
            sp = str(f)
            base = sp[:-5] if sp.lower().endswith(".webp") else sp  # foo.jpg.webp → foo.jpg
            if not ignore_refs and (sp in refs or base in refs):
                continue
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _prune_download_dirs(root: Path, refs: set[str], age_sec: float, prefix: str | None = None) -> int:
    """Удаляет подпапки старше age_sec (по самому свежему файлу внутри), кроме тех, чьи файлы ещё
    нужны коллекции/сохранённым моделям. Для кэша загрузок cache/<key> и старых throwaway dl_* папок."""
    if not root.exists():
        return 0
    cutoff = time.time() - age_sec
    removed = 0
    for d in root.iterdir():
        try:
            if not d.is_dir() or (prefix is not None and not d.name.startswith(prefix)):
                continue
            files = [f for f in d.rglob("*") if f.is_file()]
            if any(str(f) in refs for f in files):
                continue                       # ещё используется коллекцией — не трогаем
            newest = max((f.stat().st_mtime for f in files), default=0.0)
            if newest > cutoff:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


async def cleanup_files() -> dict:
    refs = await _referenced_paths()
    try:
        model_refs: set[str] | None = await _referenced_model_paths()
    except Exception:  # noqa: BLE001
        # Ссылки коллекции не прочитались — папки загрузок сегодня НЕ трогаем вовсе: без ссылок
        # чистка не отличит живую модель коллекции от мусора. Превью и geo-кэш от этих ссылок
        # не зависят и чистятся как обычно; загрузки подождут до завтра.
        log.warning("Чистка загрузок пропущена: ссылки коллекции на модели не прочитаны",
                    exc_info=True)
        model_refs = None
    age = CLEAN_AGE_DAYS * 86400
    # throwaway dl_* всегда по 30-дневному сроку; кэш cache/<key> — только если задан срок >0
    # (DOWNLOAD_CACHE_AGE_DAYS=0 — кэш бессрочный, OpenTodo bdcfcda4)
    dl_age = (config.DOWNLOAD_CACHE_AGE_DAYS or 30) * 86400
    prev = await asyncio.to_thread(_cleanup_dir, config.PREVIEW_DIR, refs, _PREVIEW_EXTS, age)
    # .part — временное имя атомарной записи кэша геометрии (preview._atomic_save): при жёстком
    # убийстве процесса на середине export файл остаётся сиротой, и без него в фильтре копился бы вечно.
    # .glb в фильтре не было до 27.08.2026, хотя лежит он в том же каталоге и живёт по тем же
    # правилам: за три бампа версии ключа (v1→v6) в geo накопилось 98 файлов на 520 МиБ, которые
    # уже никто не мог прочесть. Чистка по возрасту без оглядки на ссылки (ignore_refs) для GLB
    # верна ровно так же, как для STL: это производное от модели, пересобирается по запросу.
    geo = await asyncio.to_thread(_cleanup_dir, config.CACHE_DIR / "geo", set(),
                                  {".stl", ".part", ".glb"}, age, True)
    dl_cache = dl_old = 0
    if model_refs is not None:
        if config.DOWNLOAD_CACHE_AGE_DAYS > 0:
            dl_cache = await asyncio.to_thread(
                _prune_download_dirs, config.DOWNLOAD_CACHE_DIR, model_refs, dl_age)
        dl_old = await asyncio.to_thread(_prune_download_dirs, config.DOWNLOAD_DIR, model_refs,
                                         dl_age, "dl_")
    dl = dl_cache + dl_old
    if prev or geo or dl:
        log.info("Чистка диска: previews=%d, geo-кэш=%d, загрузки=%d", prev, geo, dl)
    return {"previews": prev, "geo": geo, "downloads": dl}


def _vacuum_sync(path: str) -> None:
    con = sqlite3.connect(path, isolation_level=None, timeout=10)  # autocommit — VACUUM нельзя в транзакции
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
    finally:
        con.close()


async def sqlite_maintenance() -> None:
    try:                       # checkpoint на рабочем соединении — главное (усекает -wal)
        await db._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await db._conn().commit()
    except Exception:  # noqa: BLE001
        log.debug("wal_checkpoint failed", exc_info=True)
    try:                       # VACUUM на отдельном соединении, best-effort
        await asyncio.to_thread(_vacuum_sync, str(config.DB_PATH))
        log.info("SQLite: checkpoint + VACUUM ok")
    except Exception:  # noqa: BLE001
        log.debug("VACUUM skipped (БД занята)", exc_info=True)


async def backfill_missing_previews(limit: int = 500) -> int:
    """Проставить превью заказам, у которых его нет, а модель — .3mf с встроенной слайсером
    миниатюрой (OpenTodo c6d9cebc: 7-стольный флекси-осьминог остался без картинки, потому
    что коллаж плейт-рендеров не собрался, а полный рендер такого меша висит минутами).

    Берём готовую миниатюру из .3mf — это распаковка одного PNG, без загрузки меша, мгновенно.
    Идемпотентно: обрабатываем только заказы с preview_path IS NULL и существующим .3mf.
    Возвращает число вылеченных заказов."""
    from . import preview
    c = db._conn()
    try:
        cur = await c.execute(
            "SELECT id, file_path FROM requests "
            "WHERE (preview_path IS NULL OR preview_path = '') "
            "AND file_path IS NOT NULL AND file_path LIKE '%.3mf' "
            "ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
    except Exception:  # noqa: BLE001
        log.debug("backfill previews: select failed", exc_info=True)
        return 0
    fixed = 0
    for r in rows:
        fp = str(config.abs_data(r["file_path"])) if r["file_path"] else ""
        try:
            if not (fp and Path(fp).exists()):
                continue
            thumb = await asyncio.to_thread(preview.extract_3mf_thumbnail, [fp])
            if thumb:
                await db.update_request(int(r["id"]), preview_path=str(thumb))
                fixed += 1
        except Exception:  # noqa: BLE001
            log.debug("backfill preview failed for #%s", r["id"], exc_info=True)
    if fixed:
        log.info("Бэкфилл превью: вылечено заказов без картинки — %d", fixed)
    return fixed


async def maintenance_loop(interval_hours: float = 24) -> None:
    """Раз в сутки (первый прогон — после паузы, чтобы не мешать старту)."""
    # Разовый прогон бэкфилла превью на старте (без паузы): дёшево — распаковка PNG из .3mf,
    # но сразу возвращает картинки заказам, оставшимся без них (флекси-осьминог, c6d9cebc).
    try:
        await backfill_missing_previews()
    except Exception:  # noqa: BLE001
        log.warning("startup preview backfill error", exc_info=True)
    # Добор замороженных параметров печати (этап 0 автотюнинга) — тоже РАЗОВО на старте, а не
    # только внутри цикла: иначе после деплоя витрина владельца сутки показывала бы «с
    # замороженными параметрами — 0» при уже готовых к добору заказах. Функция never-raise и
    # самоограничена (берёт только строки, где признаки ещё пусты), повторный проход — 0 строк.
    from . import tuning
    await tuning.backfill_features()
    while True:
        await asyncio.sleep(max(0.5, interval_hours) * 3600)
        try:
            await cleanup_files()
            await sqlite_maintenance()
            await backfill_missing_previews()
            # Этап 0 автотюнинга (docs/ai-auto-tuning.md): идемпотентный добор замороженных
            # параметров печати по историческим заказам. По расписанию, а не на горячем
            # пути клиента; сама функция never-raise.
            await tuning.backfill_features()
            # разобранные записи телеметрии старше 30 суток (f4de8627) — best-effort:
            # purge_old_errors сама never-raise, но и от неё не должен зависеть остальной цикл
            gone = await db.purge_old_errors(30)
            if gone:
                log.info("Телеметрия ошибок: убрано %d прочитанных записей старше 30 дней", gone)
            # Карта «сообщение переписки ↔ сообщение в Telegram» (реакции и ответы, 13.08.2026):
            # растёт по строке на каждого получателя каждого сообщения чата и нужна ровно
            # столько, сколько на сообщение реально могут ответить.
            links = await db.prune_tg_links(180)
            if links:
                log.info("Связи сообщений Telegram: убрано %d старше 180 дней", links)
        except Exception:  # noqa: BLE001
            log.warning("maintenance loop error", exc_info=True)


def memory_report() -> dict:
    """Сколько памяти держит сам бот и каждый его дочерний процесс.

    До 11.08 проект не измерял собственную память ВООБЩЕ — единственным числом было
    общесистемное psutil.virtual_memory() на экране состояния. Поэтому раздувшийся до 15.87 ГБ
    рендер-воркер владелец обнаружил глазами в диспетчере задач, а разбирать пришлось задним
    числом, когда улики уже стёр перезапуск. Здесь — поимённо: свой RSS, RSS каждого ребёнка
    и доля занятой памяти машины."""
    import psutil
    me = psutil.Process()
    kids: list[dict] = []
    for ch in me.children(recursive=True):
        try:
            kids.append({"pid": ch.pid, "name": ch.name(),
                         "gb": round(ch.memory_info().rss / 2 ** 30, 2)})
        except psutil.Error:          # ребёнок умер между перечислением и опросом — норма
            continue
    kids.sort(key=lambda k: k["gb"], reverse=True)
    return {"self_gb": round(me.memory_info().rss / 2 ** 30, 2), "children": kids,
            "system_percent": psutil.virtual_memory().percent}


async def memory_watch_loop(interval_min: float = 10) -> None:
    """Следит за памятью бота и его воркеров; кричит, когда кто-то распух.

    Раз в interval_min пишет одну строку в лог — чтобы рост было видно ИСТОРИЕЙ, а не разовым
    снимком. Превышение config.PROC_RSS_WARN_GB любым процессом или >92% памяти машины —
    WARNING с поимённой разбивкой и алерт мастеру (notify.alert сам throttl'ит повторы)."""
    from . import notify
    while True:
        await asyncio.sleep(max(1.0, interval_min) * 60)
        try:
            rep = await asyncio.to_thread(memory_report)
        except Exception:  # noqa: BLE001 — наблюдение не должно ронять бота
            log.debug("memory report failed", exc_info=True)
            continue
        kids = rep["children"]
        worst = max([k["gb"] for k in kids], default=0.0)
        log.info("Память: бот %.2f ГБ, воркеров %d (макс %.2f ГБ), система %.0f%%",
                 rep["self_gb"], len(kids), worst, rep["system_percent"])
        limit = float(config.PROC_RSS_WARN_GB)
        heavy = [k for k in kids if k["gb"] >= limit]
        if rep["self_gb"] >= limit:
            heavy.insert(0, {"pid": "сам", "name": "run.py", "gb": rep["self_gb"]})
        if not heavy and rep["system_percent"] < 92:
            continue
        detail = ", ".join(f"{k['name']} PID {k['pid']} — {k['gb']:.2f} ГБ" for k in heavy)
        log.warning("Память на пределе: система %.0f%%, крупные процессы: %s",
                    rep["system_percent"], detail or "нет")
        try:
            await notify.alert(
                f"⚠️ Память на пределе: занято {rep['system_percent']:.0f}%.\n"
                f"{detail or 'крупных процессов нет — память съел кто-то снаружи'}",
                key="memory_pressure", throttle_sec=3600)
        except Exception:  # noqa: BLE001
            log.debug("memory alert failed", exc_info=True)


async def calib_snapshot_loop(interval_min: float = 30) -> None:
    """Сторож пользовательских пресетов слайсера: снимок при каждой правке.

    Результат калибровки живёт в трёх файлах в %APPDATA% — вне репозитория и вне бэкапа
    проекта, и обновление слайсера возвращает их к стоку. Снимок пишется ТОЛЬКО когда
    изменился sha, поэтому таблица calib_presets — это история правок: видно, когда MVS стал
    13, а юбка — одной петлёй. Первый прогон сразу на старте: если пресет успели сбросить до
    первого тика, снимать будет уже нечего.
    """
    from . import calibration, db
    first = True
    while True:
        if not first:
            await asyncio.sleep(max(1.0, interval_min) * 60)
        first = False
        try:
            items = await asyncio.to_thread(calibration.snapshot_user_presets)
            res = await db.snapshot_calib_presets(items)
            if res["new"] or res["changed"] or res["gone"]:
                log.info("Снимки пресетов калибровки: новых %d, изменённых %d, пропало %d",
                         res["new"], res["changed"], res["gone"])
        except Exception:  # noqa: BLE001
            log.warning("calib snapshot loop error", exc_info=True)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


async def cart_nudge_loop(bot, interval_min: float = 30) -> None:
    """Нудж о брошенной корзине (idea #10): корзины давностью 3–48 ч → ОДНО напоминание в Telegram."""
    from . import notify
    while True:
        await asyncio.sleep(max(5, interval_min) * 60)
        try:
            carts = await db.stale_carts(min_age_s=3 * 3600, max_age_s=48 * 3600)
        except Exception:  # noqa: BLE001
            continue
        for c in carts:
            try:
                n = int(c["count"])
                word = _plural(n, "модель", "модели", "моделей")
                txt = (f"🛒 В вашей корзине {n} {word}"
                       + (f" на {c['total']}" if c.get("total") else "")
                       + ". Оформить заказ? Откройте мини-приложение, чтобы продолжить.")
                await notify.notify_user(bot, c["user_id"], txt)
                await db.mark_cart_nudged(c["user_id"])
            except Exception:  # noqa: BLE001
                log.debug("cart nudge failed for %s", c.get("user_id"), exc_info=True)
