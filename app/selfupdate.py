"""Автообновления бота (OpenTodo 644367fb).

Модель обновления повторяет ручной путь владельца (`restart_bot.ps1 -Update`), но изнутри
бота: `git fetch` → `git merge --ff-only origin/<текущая ветка>` → `pip install -r
requirements.txt` → выход процесса. Поднимает бота обратно СТОРОЖ (watchdog.py, задача
Планировщика `pcb_watchdog`) — тот же механизм, что лечит падения, поэтому отдельного
«перезапускателя» не нужно; простой — до пары минут (FAIL_LIMIT × CHECK_EVERY сторожа).

Зачем: бот превращается в коммерческий продукт (print-studio-commercial-product) — на машине
клиента нет разработчика, обновления должны приезжать сами. На машине владельца режим notify
безопасен: локальные коммиты (ahead > 0) обновление честно блокируют, ff-only ничего не теряет.

Режимы (text_settings.selfupdate_mode, правится из админки без перезапуска):
- off    — не проверять вовсе;
- notify — проверять раз в selfupdate_interval_h часов и слать админам уведомление (по умолчанию);
- auto   — применять самостоятельно и перезапускаться.

Все git-операции — subprocess в to_thread (git может ходить в сеть десятки секунд).
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from html import escape

from . import db, license, notify, version

log = logging.getLogger(__name__)

MODES = ("off", "notify", "auto")
DEFAULT_MODE = "notify"
DEFAULT_INTERVAL_H = 6.0
_CHECK_TICK_S = 300          # шаг цикла; сам интервал проверки — настройка мастера
_GIT_TIMEOUT_S = 120
_PIP_TIMEOUT_S = 900

_last: dict = {}             # последний результат check() — для экрана «Статус»
_last_notified = ""          # sha origin, о котором уже уведомляли (не спамить каждый тик)
_applying = False            # защита от двойного «Обновить» из двух админок
_last_lic_warned = ""        # sha, о недоступности которого по лицензии уже писали


def _git(*args: str, timeout: float = _GIT_TIMEOUT_S) -> tuple[int, str, str]:
    """(rc, stdout, stderr) git-команды в корне репозитория. Блокирующее — звать в to_thread."""
    try:
        p = subprocess.run(["git", "-C", str(version.BASE_DIR), *args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "git не найден в PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)}: таймаут {timeout:g} с"


def _check_sync() -> dict:
    """Свериться с origin: сколько коммитов мы позади/впереди своей ветки."""
    branch = version.info()["branch"]
    if not branch:
        return {"ok": False, "error": "нет git-ветки (detached HEAD или нет .git)"}
    rc, _, err = _git("fetch", "--quiet", "origin", branch)
    if rc != 0:
        return {"ok": False, "branch": branch, "error": err or f"git fetch: код {rc}"}
    rc, out, err = _git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}")
    if rc != 0:
        return {"ok": False, "branch": branch, "error": err or f"git rev-list: код {rc}"}
    try:
        ahead, behind = (int(x) for x in out.split())
    except ValueError:
        return {"ok": False, "branch": branch, "error": f"git rev-list: не разобрал «{out}»"}
    commits: list[str] = []
    remote_sha = ""
    if behind:
        _, log_out, _ = _git("log", "--oneline", "--no-decorate", "-15", f"HEAD..origin/{branch}")
        commits = [ln for ln in log_out.splitlines() if ln.strip()]
        _, remote_sha, _ = _git("rev-parse", "--short", f"origin/{branch}")
    return {"ok": True, "branch": branch, "behind": behind, "ahead": ahead,
            "commits": commits, "remote_sha": remote_sha, "checked_at": int(time.time())}


async def check() -> dict:
    """Проверка обновлений; результат запоминается для экрана «Статус»."""
    global _last
    res = await asyncio.to_thread(_check_sync)
    _last = res
    return res


def _requirement_files() -> list:
    """Файлы зависимостей, которые надо переустановить после обновления кода.

    Всегда ядро; дополнительно — файлы ВКЛЮЧЁННЫХ модулей. Без этого обновление молча
    ломало бы студию, которая включила модуль: новые пакеты из requirements-ai.txt к ней
    бы не приехали, и фича отвалилась бы «сама собой» после очередного апдейта.

    Ориентируемся на выключатели в .env, а не на наличие пакетов: выключенный модуль не
    должен подтягиваться обратно только потому, что когда-то был установлен.
    """
    from . import config
    files = [version.BASE_DIR / "requirements.txt"]
    if config.AVATAR_GPU or config.SEMSEARCH_ENABLED:
        files.append(version.BASE_DIR / "requirements-ai.txt")
    if config.MW_BROWSER_ENABLED:
        files.append(version.BASE_DIR / "requirements-makerworld.txt")
    return [f for f in files if f.exists()]


def _apply_sync() -> dict:
    """Применить обновление: ff-only merge + pip install. Блокирующее — звать в to_thread."""
    branch = version.info()["branch"]
    # Лицензия — ПЕРВЫМ шагом и именно здесь: _apply_sync() — единственная воронка обоих
    # путей обновления (кнопка в админке и фоновый режим auto из loop()), а middleware
    # редакции фоновый цикл не проходит вовсе. Формат отказа тот же, что у остальных
    # предохранителей: {'ok': False, 'error': ...} уезжает в тост админки как есть.
    allowed, why = license.update_allowed()
    if not allowed:
        return {"ok": False, "error": why, "license": license.status().get("state", "")}
    rc, out, _ = _git("status", "--porcelain", "--untracked-files=no")
    if rc != 0 or out:
        return {"ok": False, "error": "в рабочем дереве есть незакоммиченные правки — "
                                      "обновление отменено, чтобы их не потерять"}
    res = _check_sync()
    if not res.get("ok"):
        return res
    if not res.get("behind"):
        return {"ok": False, "error": "обновлений нет — уже на последней версии"}
    if res.get("ahead"):
        return {"ok": False, "error": f"локальных коммитов впереди origin: {res['ahead']} — "
                                      "нужен ручной merge/push, автообновление не рискует"}
    rc, _, err = _git("merge", "--ff-only", f"origin/{branch}")
    if rc != 0:
        return {"ok": False, "error": f"git merge --ff-only: {err or rc}"}
    # зависимости могли смениться вместе с кодом; при неизменённом requirements.txt pip
    # отрабатывает за секунды («already satisfied»)
    for req in _requirement_files():
        try:
            p = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=_PIP_TIMEOUT_S)
            if p.returncode != 0:
                # код уже обновлён — честно сообщаем, но не откатываем: сторож поднимет бота,
                # а несостыковка зависимостей видна в selfcheck и логах
                log.warning("pip install -r %s при автообновлении вернул %s: %s",
                            req.name, p.returncode, (p.stderr or "")[-400:])
        except Exception:  # noqa: BLE001
            log.warning("pip install -r %s при автообновлении не отработал", req.name, exc_info=True)
    return {"ok": True, "updated_to": res.get("remote_sha", ""), "count": res.get("behind", 0)}


def _restart_soon() -> None:
    """Выйти из процесса через секунду (ответ API успевает уйти) — сторож поднимет бота
    с новым кодом. Перед выходом гасим детей: без этого cpupool-дети сиротеют и держат
    VRAM (кейс print-cost-bot-vram-orphan-leak).

    Тонкость, из-за которой намерение выше не исполнялось (разбор 11.08): os._exit(0)
    пропускает ВСЕ atexit-хуки, а именно на них держится штатное убийство детей —
    и рассылка сентинелов воркерам пула (concurrent.futures._python_exit), и убийство
    daemon-процессов multiprocessing. Поэтому shutdown зовём с wait=True (дождаться смерти
    воркеров по-настоящему), а воркер аватарок гасим явно: он daemon=True и на автоматику
    рассчитывать здесь нельзя. Иначе автообновление штатно оставляет ОС процесс на гигабайты
    с дохлым родителем — а такой воркер не увидит EOF никогда."""
    def _die() -> None:
        try:
            from . import cpupool
            cpupool.shutdown(wait=True)
        except Exception:  # noqa: BLE001
            log.warning("автообновление: пул воркеров не погас чисто", exc_info=True)
        try:
            from . import avatars_gpu
            avatars_gpu.unload("автообновление")
        except Exception:  # noqa: BLE001
            log.warning("автообновление: воркер аватарок не погас чисто", exc_info=True)
        log.info("Автообновление: выхожу — сторож перезапустит бота с новым кодом")
        logging.shutdown()
        os._exit(0)
    asyncio.get_running_loop().call_later(1.5, _die)


async def apply() -> dict:
    """Обновиться и перезапуститься. Возвращает {ok} ДО перезапуска (он через ~1.5 с)."""
    global _applying
    if _applying:
        return {"ok": False, "error": "обновление уже идёт"}
    _applying = True
    try:
        res = await asyncio.to_thread(_apply_sync)
        if not res.get("ok"):
            return res
        log.info("Автообновление: код обновлён до %s (%s коммитов), перезапускаюсь",
                 res.get("updated_to"), res.get("count"))
        _restart_soon()
        return res
    finally:
        # на успехе процесс всё равно умрёт через ~1.5 с; повторный клик до этого упрётся
        # в «обновлений нет» — гонки не страшны
        _applying = False


async def mode() -> str:
    m = (await db.get_text_settings()).get("selfupdate_mode", "").strip().lower()
    return m if m in MODES else DEFAULT_MODE


async def interval_h() -> float:
    v = (await db.get_settings()).get("selfupdate_interval_h")
    try:
        return max(0.25, float(v)) if v is not None else DEFAULT_INTERVAL_H
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_H


def status() -> dict:
    """Последняя проверка — для сводки «Статус» (без похода в git)."""
    return dict(_last)


async def _notify_update(bot, res: dict) -> None:
    global _last_notified
    sha = res.get("remote_sha") or ""
    if sha and sha == _last_notified:
        return                       # об этой версии уже писали
    _last_notified = sha
    lines = "\n".join("• " + escape(c) for c in (res.get("commits") or [])[:10])
    await notify.notify_admins(
        bot,
        (f"⬆️ <b>Доступно обновление</b> ({res.get('behind')} коммит(ов), "
         f"ветка <code>{escape(res.get('branch') or '')}</code> → <code>{escape(sha)}</code>)\n"
         f"{lines}\n\n"
         "Обновить: админка → Статус → «Обновить сейчас» (бот перезапустится сам)."))


async def loop(bot) -> None:
    """Фоновая проверка обновлений. Интервал и режим — настройки мастера, действуют без
    перезапуска. Первый заход — через 10 минут после старта (не мешать подъёму бота)."""
    next_at = time.time() + 600
    while True:
        await asyncio.sleep(_CHECK_TICK_S)
        if time.time() < next_at:
            continue
        try:
            m = await mode()
            next_at = time.time() + (await interval_h()) * 3600
            if m == "off":
                continue
            res = await check()
            if not res.get("ok"):
                log.debug("проверка обновлений: %s", res.get("error"))
                continue
            if not res.get("behind"):
                continue
            if m == "auto":
                # Проверяем лицензию ДО обещания «применяю»: иначе владелец получает
                # сообщение о перезапуске, которого не будет, и узнаёт правду только из лога.
                allowed, why = license.update_allowed()
                if not allowed:
                    global _last_lic_warned
                    log.warning("Автообновление не применяется: %s", why)
                    sha = res.get("remote_sha") or ""
                    if sha != _last_lic_warned:
                        _last_lic_warned = sha
                        try:
                            await notify.notify_admins(
                                bot, "⚠️ <b>Обновление есть, но не применяется</b>\n"
                                     + escape(why))
                        except Exception:  # noqa: BLE001
                            log.debug("selfupdate notify failed", exc_info=True)
                    continue
                log.info("Автообновление (auto): позади origin на %s коммит(ов) — применяю",
                         res.get("behind"))
                try:
                    await notify.notify_admins(
                        bot, f"⬆️ <b>Автообновление</b>: применяю {res.get('behind')} "
                             f"коммит(ов) (<code>{escape(res.get('remote_sha') or '')}</code>), "
                             "бот перезапустится в течение пары минут.")
                except Exception:  # noqa: BLE001
                    log.debug("selfupdate notify failed", exc_info=True)
                out = await apply()
                if not out.get("ok"):
                    log.warning("Автообновление не применилось: %s", out.get("error"))
            else:
                await _notify_update(bot, res)
        except Exception:  # noqa: BLE001  — цикл не должен умирать от разовой ошибки
            log.debug("цикл автообновления: ошибка", exc_info=True)
