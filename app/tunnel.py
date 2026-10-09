"""Вход наружу для мини-апки: выбор провайдера публичного HTTPS-адреса (C3).

Режим задаётся ключом TUNNEL_MODE в .env:

* ``quick``  — прежнее поведение: Cloudflare Quick Tunnel без аккаунта
  (``cloudflared tunnel --url http://localhost:PORT``). Адрес вида
  https://*.trycloudflare.com выцепляется из вывода и МЕНЯЕТСЯ при каждом
  перезапуске бота — для продажи это дефект (см. docs/Перспективные
  разработки/public-entry-2026-08-22.md), поэтому режим остаётся дефолтом
  только ради совместимости с уже стоящими установками.
* ``named`` — именованный туннель Cloudflare по токену студии
  (``cloudflared tunnel run --token <CF_TUNNEL_TOKEN>``). Постоянный адрес
  студии берётся ТОЛЬКО из WEBAPP_PUBLIC_URL: в этом режиме cloudflared
  публичного адреса в вывод не печатает вовсе. Нет токена или адреса —
  честная ошибка в лог и в selfcheck, БЕЗ молчаливого отката на quick
  (иначе студия думает, что адрес постоянный, а он меняется).
* ``none``  — туннель не поднимаем: адрес снаружи обеспечивает сам владелец
  (Tailscale Funnel, свой обратный прокси, белый IP). Берём WEBAPP_PUBLIC_URL.

Во всех режимах WEBAPP_PUBLIC_URL остаётся приоритетным источником адреса:
единственная запись в ``config.WEBAPP_PUBLIC_URL`` — ветка quick, когда адрес
в .env не задан (на этой мутации держатся все потребители адреса).

За процессом cloudflared следит сторож: падение = мини-апка недоступна
клиентам, поэтому процесс поднимается заново с нарастающей паузой, а в режиме
quick при перезапуске сбрасывается запомненный адрес (иначе бот навсегда
залипнет на мёртвом) и вызывается колбэк ``on_url`` — им run.py переставляет
кнопку-меню ☰.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from . import config

log = logging.getLogger(__name__)

_DOWNLOAD_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
                 "cloudflared-windows-amd64.exe")
_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

MODES = ("quick", "named", "none")
# Паузы перед перезапуском упавшего cloudflared; последняя повторяется дальше.
_BACKOFF = (5.0, 10.0, 30.0, 60.0)

_proc: asyncio.subprocess.Process | None = None
_public_url: str | None = None
_mode: str = "quick"
_reader: asyncio.Task[None] | None = None      # держим ссылки: иначе задачи съест GC
_watchdog: asyncio.Task[None] | None = None
_stopping: bool = False
_error: str | None = None
_on_url: Callable[[str], Awaitable[None] | None] | None = None


def mode() -> str:
    """Режим входа наружу из .env; неизвестное значение = quick (с руганью в лог)."""
    m = (config.TUNNEL_MODE or "quick").strip().lower()
    if m not in MODES:
        log.warning("TUNNEL_MODE=%r не распознан (ожидается %s) — работаю как quick",
                    config.TUNNEL_MODE, "/".join(MODES))
        return "quick"
    return m


def public_url() -> str | None:
    return _public_url


def mask_token(token: str | None) -> str:
    """Токен туннеля — секрет с полным контролем над туннелем студии: в лог и в статус
    уходит только признак/огрызок, никогда значение целиком."""
    if not token:
        return ""
    return f"{token[:4]}…{token[-4:]}" if len(token) > 16 else "задан"


def status() -> dict[str, object]:
    """Снимок для админ-статуса и selfcheck (без секретов)."""
    return {
        # Режим берём живой из .env, а не запомненный в start(): иначе до первого
        # старта (или в процессе, где старта не было вовсе) статус врал бы «quick».
        "mode": mode(),
        "public_url": config.WEBAPP_PUBLIC_URL or "",
        "running": bool(_proc is not None and _proc.returncode is None),
        "token_set": bool(config.CF_TUNNEL_TOKEN),
        "error": _error or "",
    }


def _looks_valid(p: Path) -> bool:
    """Похоже на полный .exe: PE-заголовок 'MZ' и не подозрительно маленький размер.

    Порог сознательно НЕ равен реальному размеру релиза (≈70 МБ): Cloudflare может
    выпустить бинарь меньше, и слишком высокая планка заставила бы бота перекачивать
    70 МБ при КАЖДОМ старте. Задача проверки — отсечь обрезанную загрузку и html-заглушку
    прокси, а не сверять версию."""
    try:
        if not p.exists() or p.stat().st_size < 5_000_000:
            return False
        with open(p, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


async def _ensure_binary() -> Path | None:
    dest = config.BASE_DIR / "tools" / "cloudflared.exe"
    if _looks_valid(dest):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / "cloudflared.exe.part"   # качаем во временный → атомарный rename
    log.info("Скачиваю cloudflared (официальный бинарь Cloudflare)…")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as c:
            async with c.stream("GET", _DOWNLOAD_URL) as r:
                r.raise_for_status()
                expected = int(r.headers.get("Content-Length", "0"))
                written = 0
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                        written += len(chunk)
        if expected and written < expected:
            raise OSError(f"обрыв загрузки {written}/{expected} байт")
        # Обрыв ловится сверкой с Content-Length (тогда файл в dest не переезжает вовсе);
        # если сервер длину не прислал, сверять не с чем — требуем размер живого релиза.
        floor = 5_000_000 if expected else 40_000_000
        if written < floor:
            raise OSError(f"слишком маленький файл: {written} байт")
        os.replace(tmp, dest)
        log.info("cloudflared сохранён: %s (%.1f МБ)", dest, dest.stat().st_size / 1e6)
        return dest
    except Exception:  # noqa: BLE001
        log.warning("Не удалось скачать cloudflared", exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None


async def _notify(url: str) -> None:
    """Колбэк «адрес появился/сменился» (run.py переставляет кнопку ☰)."""
    if _on_url is None:
        return
    try:
        res = _on_url(url)
        if asyncio.iscoroutine(res):
            await res
    except Exception:  # noqa: BLE001
        log.warning("Колбэк смены публичного адреса упал", exc_info=True)


async def _read_url(proc: asyncio.subprocess.Process) -> None:
    """Читает вывод cloudflared. В quick — выцепляет адрес; в named адреса в выводе нет,
    но stdout всё равно надо вычитывать, иначе труба переполнится и процесс встанет."""
    global _public_url
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        if _mode != "quick":
            continue
        m = _URL_RE.search(line.decode(errors="ignore"))
        if m and not _public_url:
            _public_url = m.group(0)
            config.WEBAPP_PUBLIC_URL = _public_url
            log.info("Cloudflare-туннель готов: %s", _public_url)
            await _notify(_public_url)


async def _spawn(port: int, exe: Path) -> asyncio.subprocess.Process:
    if _mode == "named":
        token = config.CF_TUNNEL_TOKEN or ""
        log.info("Запускаю именованный Cloudflare-туннель (токен %s)", mask_token(token))
        args = [str(exe), "tunnel", "--no-autoupdate", "run", "--token", token]
    else:
        args = [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]
    return await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)


async def _supervise(port: int, exe: Path) -> None:
    """Сторож: пожинает упавший cloudflared и поднимает заново с бэкоффом."""
    global _proc, _public_url, _reader, _error
    attempt = 0
    while not _stopping:
        proc = _proc
        if proc is None:
            return
        started = asyncio.get_running_loop().time()
        rc = await proc.wait()
        if _stopping:
            return
        # Долго проживший процесс = прошлая серия падений вылечена: иначе счётчик только
        # растёт, и через четыре падения (пусть даже разнесённые на дни) пауза навсегда
        # становится максимальной — минута недоступности мини-апки на ровном месте.
        if asyncio.get_running_loop().time() - started > _BACKOFF[-1]:
            attempt = 0
        delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
        _error = f"cloudflared остановился (код {rc})"
        log.error("cloudflared остановился (код %s) — вход снаружи потерян, "
                  "перезапуск через %.0f c", rc, delay)
        await asyncio.sleep(delay)
        if _stopping:
            return
        if _mode == "quick":
            # Без сброса перезапущенный туннель выдаст НОВЫЙ адрес, а бот продолжит
            # раздавать мёртвый старый (условие «запомнить только первый раз»).
            _public_url = None
        try:
            _proc = await _spawn(port, exe)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось перезапустить cloudflared", exc_info=True)
            attempt += 1
            continue
        _reader = asyncio.create_task(_read_url(_proc))
        _error = None
        attempt += 1


async def start(port: int, on_url: Callable[[str], Awaitable[None] | None] | None = None) -> str | None:
    """Поднимает вход наружу по TUNNEL_MODE, возвращает публичный https-URL (или None).

    on_url зовётся, когда адрес появился или сменился (только режим quick)."""
    global _proc, _public_url, _mode, _reader, _watchdog, _stopping, _error, _on_url
    _stopping = False
    _on_url = on_url
    _error = None
    # Состояние прошлого запуска чистим здесь, а не в stop(): повторный start() в том же
    # процессе («перезапустить туннель без рестарта бота») иначе мгновенно вернул бы
    # СТАРЫЙ, уже мёртвый quick-адрес.
    _public_url = None
    _mode = mode()
    url = config.WEBAPP_PUBLIC_URL

    if _mode == "none":
        if not url:
            _error = ("TUNNEL_MODE=none, но WEBAPP_PUBLIC_URL не задан — "
                      "адреса снаружи нет вовсе")
            log.error("Вход снаружи не настроен: %s", _error)
            return None
        log.info("Вход снаружи: адрес владельца (TUNNEL_MODE=none): %s", url)
        return url

    if _mode == "named":
        if not config.CF_TUNNEL_TOKEN:
            _error = "TUNNEL_MODE=named, но CF_TUNNEL_TOKEN не задан"
            log.error("Именованный туннель не поднят: %s", _error)
            return None
        if not url:
            # Молчаливый откат на quick запрещён: студия считала бы адрес постоянным.
            _error = ("TUNNEL_MODE=named, но WEBAPP_PUBLIC_URL не задан — "
                      "постоянный адрес студии брать неоткуда")
            log.error("Именованный туннель не поднят: %s", _error)
            return None
    elif url:
        # quick + заданный адрес: как и раньше, лишний cloudflared рядом со своим
        # Funnel/прокси не поднимаем.
        log.info("Мини-апка: стабильный адрес из .env: %s (туннель не поднимаем)", url)
        return url

    exe = await _ensure_binary()
    if not exe:
        _error = "cloudflared не найден и не скачался"
        log.warning("Вход снаружи не поднят: %s", _error)
        return None
    _proc = await _spawn(port, exe)
    _reader = asyncio.create_task(_read_url(_proc))
    _watchdog = asyncio.create_task(_supervise(port, exe))

    if _mode == "named":
        # Адрес здесь ПОСТОЯННЫЙ и известен из .env, поэтому возвращаем его независимо
        # от того, пережил ли cloudflared первую секунду: иначе разовое падение на старте
        # навсегда (до рестарта бота) снимало бы кнопку-меню ☰ — сторож процесс поднимет,
        # а колбэка on_url в named нет вовсе. Здоровье процесса едет отдельно —
        # в status()['running']/['error'] и в selfcheck.
        for _ in range(4):
            if _proc.returncode is not None:
                _error = f"cloudflared сразу завершился (код {_proc.returncode})"
                log.error("Именованный туннель упал на старте: %s "
                          "(адрес %s из .env, поднимаю заново сторожем)", _error, url)
                break
            await asyncio.sleep(0.25)
        else:
            log.info("Именованный Cloudflare-туннель запущен, публичный адрес: %s", url)
        return url

    for _ in range(60):                      # ждём URL до ~30 c
        if _public_url:
            return _public_url
        if _proc.returncode is not None:     # процесс упал — дальше им займётся сторож
            # Адрес мог быть УЖЕ напечатан в трубу, а задача-читатель просто не успела
            # его разобрать (под нагрузкой процесс завершается раньше чтения). Дочитываем
            # хвост, иначе start() вернёт None при живом, разобранном чуть позже адресе.
            if _reader is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(_reader), 1)
            if _public_url:
                return _public_url
            break
        await asyncio.sleep(0.5)
    if not _public_url:
        _error = "cloudflared не выдал адрес"
        log.warning("cloudflared не выдал URL — мини-апка будет недоступна, останется список.")
    return _public_url


async def stop() -> None:
    global _proc, _reader, _watchdog, _stopping
    _stopping = True
    for t in (_watchdog, _reader):
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
    _watchdog = _reader = None
    if _proc is not None and _proc.returncode is None:
        try:
            _proc.terminate()
        except ProcessLookupError:
            pass
        except Exception:  # noqa: BLE001
            log.debug("tunnel stop failed", exc_info=True)
        else:
            # Пожинаем процесс, иначе остаётся зомби и предупреждение asyncio при выходе.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(_proc.wait(), 5)
    _proc = None
