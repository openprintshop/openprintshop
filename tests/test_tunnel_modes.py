"""Вход наружу (C3): режимы TUNNEL_MODE, приоритет WEBAPP_PUBLIC_URL, сторож-перезапуск.

Настоящий cloudflared не запускается и наружу ничего не публикуется: вместо бинаря
подставляется скрипт-заглушка на python, печатающий такие же строки, как настоящий.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app import config, selfcheck, tunnel

# Заглушка вместо cloudflared: печатает баннер с адресом (номер запуска берём из счётчика,
# чтобы перезапуск сторожем давал ДРУГОЙ адрес — как у настоящего quick-туннеля) и живёт
# заданное число секунд.
_STUB = '''
import sys, time
from pathlib import Path
counter = Path(sys.argv[1])
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
print("2026-08-22T10:00:00Z INF +----------------------------------------+")
print("2026-08-22T10:00:00Z INF |  https://stub-tunnel-%d.trycloudflare.com  |" % n)
print("2026-08-22T10:00:00Z INF +----------------------------------------+")
sys.stdout.flush()
time.sleep(float(sys.argv[2]))
'''


@pytest.fixture(autouse=True)
async def _clean_tunnel():
    yield
    await tunnel.stop()
    tunnel._public_url = None
    tunnel._error = None
    tunnel._mode = "quick"


def _env(monkeypatch, mode="quick", url=None, token=None):
    monkeypatch.setattr(config, "TUNNEL_MODE", mode)
    monkeypatch.setattr(config, "WEBAPP_PUBLIC_URL", url)
    monkeypatch.setattr(config, "CF_TUNNEL_TOKEN", token)


def _stub(monkeypatch, tmp_path: Path, lifetime: str = "30") -> list[list[str]]:
    """Подменяет и поиск бинаря, и запуск процесса: cloudflared никогда не берётся с диска."""
    script = tmp_path / "stub_cloudflared.py"
    script.write_text(_STUB, encoding="utf-8")
    counter = tmp_path / "runs.txt"
    calls: list[list[str]] = []

    async def fake_ensure() -> Path:
        return tmp_path / "cloudflared.exe"

    async def fake_spawn(port: int, exe: Path):
        calls.append([str(exe), str(port)])
        return await asyncio.create_subprocess_exec(
            sys.executable, str(script), str(counter), lifetime,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    monkeypatch.setattr(tunnel, "_ensure_binary", fake_ensure)
    monkeypatch.setattr(tunnel, "_spawn", fake_spawn)
    return calls


def test_url_regex_matches_real_cloudflared_line():
    line = ("2026-08-22T10:00:00Z INF |  https://brave-fox-1234-ab.trycloudflare.com   |")
    m = tunnel._URL_RE.search(line)
    assert m and m.group(0) == "https://brave-fox-1234-ab.trycloudflare.com"


def test_mode_parsing(monkeypatch):
    for raw, want in [("quick", "quick"), ("NAMED", "named"), (" none ", "none"),
                      ("", "quick"), ("чепуха", "quick")]:
        monkeypatch.setattr(config, "TUNNEL_MODE", raw)
        assert tunnel.mode() == want, raw


def test_token_is_masked():
    masked = tunnel.mask_token("eyJhIjoiZmFrZS10b2tlbi12YWx1ZSJ9")
    assert "IjoiZmFrZS" not in masked and masked.startswith("eyJh")
    assert tunnel.mask_token("") == ""


async def test_quick_with_env_url_does_not_spawn(monkeypatch, tmp_path):
    """Установка владельца: режим quick по умолчанию, но адрес задан (Funnel/свой домен) —
    лишний cloudflared рядом подниматься не должен."""
    _env(monkeypatch, "quick", "https://ksa.example.ts.net")
    calls = _stub(monkeypatch, tmp_path)
    assert await tunnel.start(8081) == "https://ksa.example.ts.net"
    assert calls == []
    assert config.WEBAPP_PUBLIC_URL == "https://ksa.example.ts.net"


async def test_none_returns_env_url_without_process(monkeypatch, tmp_path):
    _env(monkeypatch, "none", "https://app.studio.ru")
    calls = _stub(monkeypatch, tmp_path)
    assert await tunnel.start(8081) == "https://app.studio.ru"
    assert calls == []


async def test_none_without_url_is_error(monkeypatch, tmp_path):
    _env(monkeypatch, "none", None)
    calls = _stub(monkeypatch, tmp_path)
    assert await tunnel.start(8081) is None
    assert calls == []
    assert "WEBAPP_PUBLIC_URL" in str(tunnel.status()["error"])


async def test_named_without_url_does_not_fall_back_to_quick(monkeypatch, tmp_path):
    """Молчаливый откат на quick запрещён: студия считала бы адрес постоянным."""
    _env(monkeypatch, "named", None, token="tok-123")
    calls = _stub(monkeypatch, tmp_path)
    assert await tunnel.start(8081) is None
    assert calls == [], "cloudflared не должен подниматься без адреса"
    assert config.WEBAPP_PUBLIC_URL is None
    assert "WEBAPP_PUBLIC_URL" in str(tunnel.status()["error"])


async def test_named_without_token_is_error(monkeypatch, tmp_path):
    _env(monkeypatch, "named", "https://app.studio.ru", token=None)
    calls = _stub(monkeypatch, tmp_path)
    assert await tunnel.start(8081) is None
    assert calls == []
    assert "CF_TUNNEL_TOKEN" in str(tunnel.status()["error"])


async def test_named_keeps_env_url_even_if_process_prints_quick_url(monkeypatch, tmp_path):
    """В named публичного адреса в выводе нет вовсе — адрес берётся только из .env,
    и никакая строка вывода не имеет права его перезаписать."""
    _env(monkeypatch, "named", "https://app.studio.ru", token="tok-123")
    _stub(monkeypatch, tmp_path)
    assert await tunnel.start(8081) == "https://app.studio.ru"
    await asyncio.sleep(0.3)
    assert config.WEBAPP_PUBLIC_URL == "https://app.studio.ru"
    st = tunnel.status()
    assert st["mode"] == "named" and st["running"] is True and st["token_set"] is True


async def test_quick_reads_url_from_output_and_publishes_it(monkeypatch, tmp_path):
    _env(monkeypatch, "quick", None)
    _stub(monkeypatch, tmp_path)
    seen: list[str] = []
    url = await tunnel.start(8081, on_url=lambda u: seen.append(u))
    assert url == "https://stub-tunnel-1.trycloudflare.com"
    assert config.WEBAPP_PUBLIC_URL == url, "адрес обязан уехать в config (его читают все потребители)"
    assert seen == [url], "колбэк смены адреса не позвали — кнопка ☰ осталась бы мёртвой"


async def test_watchdog_restarts_and_picks_up_new_url(monkeypatch, tmp_path):
    """Падение туннеля = мини-апка недоступна: сторож поднимает заново, и НОВЫЙ адрес
    должен вытеснить мёртвый старый."""
    _env(monkeypatch, "quick", None)
    monkeypatch.setattr(tunnel, "_BACKOFF", (0.05,))
    _stub(monkeypatch, tmp_path, lifetime="0")
    seen: list[str] = []
    # Заглушка живёт 0 c, поэтому под нагрузкой первый процесс успевает умереть раньше,
    # чем start() вернётся: истина здесь не в возвращённом значении, а в том, что адрес
    # публикуется и СМЕНЯЕТСЯ после перезапуска (иначе бот залип бы на мёртвом туннеле).
    await tunnel.start(8081, on_url=lambda u: seen.append(u))
    for _ in range(200):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0.1)
    assert len(seen) >= 2, "сторож не перезапустил туннель или новый адрес не опубликован"
    assert seen[0] != seen[-1]
    assert str(config.WEBAPP_PUBLIC_URL) == seen[-1]
    assert str(config.WEBAPP_PUBLIC_URL).endswith(".trycloudflare.com")


async def test_stop_reaps_process(monkeypatch, tmp_path):
    _env(monkeypatch, "quick", None)
    _stub(monkeypatch, tmp_path)
    await tunnel.start(8081)
    assert tunnel.status()["running"] is True
    await tunnel.stop()
    assert tunnel.status()["running"] is False


# ---- selfcheck ----------------------------------------------------------------------

def test_selfcheck_named_without_token_and_url(monkeypatch):
    _env(monkeypatch, "named", None, token=None)
    out = " | ".join(selfcheck.entry_checks())
    assert "CF_TUNNEL_TOKEN" in out and "WEBAPP_PUBLIC_URL" in out


def test_selfcheck_none_without_url(monkeypatch):
    _env(monkeypatch, "none", None)
    assert any("TUNNEL_MODE=none" in i for i in selfcheck.entry_checks())


def test_selfcheck_quick_hints_about_changing_address(monkeypatch):
    _env(monkeypatch, "quick", None)
    assert any("меняется" in i for i in selfcheck.entry_checks())


def test_selfcheck_quiet_when_address_is_stable(monkeypatch):
    """У владельца режим формально quick, но адрес задан — ругаться не на что."""
    _env(monkeypatch, "quick", "https://ksa.example.ts.net")
    assert selfcheck.entry_checks() == []


def test_selfcheck_rejects_http_and_ip(monkeypatch):
    _env(monkeypatch, "none", "http://app.studio.ru")
    assert any("не https" in i for i in selfcheck.entry_checks())
    _env(monkeypatch, "none", "https://203.0.113.7")
    assert any("непубличный IP" in i for i in selfcheck.entry_checks())
    _env(monkeypatch, "none", "https://192.168.1.10:8443")
    assert any("непубличный IP" in i for i in selfcheck.entry_checks())


def test_selfcheck_accepts_public_ip(monkeypatch):
    """Публичный IP с сертификатом Let's Encrypt на IP — как мини-апп на VPS: Telegram такие
    кнопки открывает, ругаться не на что."""
    _env(monkeypatch, "none", "https://8.8.8.8:8451")
    assert selfcheck.entry_checks() == []


async def test_quick_returns_url_even_if_process_died_right_after_printing(monkeypatch, tmp_path):
    """Гонка старта: адрес уже напечатан в трубу, но процесс умер раньше, чем его успел
    разобрать читатель. start() обязан дочитать хвост, а не вернуть None при живом адресе."""
    _env(monkeypatch, "quick", None)
    monkeypatch.setattr(tunnel, "_BACKOFF", (30.0,))   # сторож не должен вмешаться
    _stub(monkeypatch, tmp_path, lifetime="0")
    inner = tunnel._spawn

    async def spawn_already_dead(port: int, exe: Path):
        """Отдаёт start() процесс, который УЖЕ завершился, напечатав адрес в трубу, —
        детерминированное воспроизведение гонки, которая под нагрузкой случайна."""
        proc = await inner(port, exe)
        await proc.wait()
        return proc

    monkeypatch.setattr(tunnel, "_spawn", spawn_already_dead)
    url = await tunnel.start(8081)
    assert url == "https://stub-tunnel-1.trycloudflare.com"
    assert config.WEBAPP_PUBLIC_URL == url


def test_status_mode_is_live_from_env_without_start(monkeypatch):
    """До первого start() статус деска обязан показывать НАСТОЯЩИЙ режим из .env,
    иначе владелец видит «quick» при named и считает адрес непостоянным."""
    _env(monkeypatch, "named", "https://app.studio.ru", token="t" * 40)
    assert tunnel.status()["mode"] == "named"
    assert tunnel.status()["token_set"] is True


def test_selfcheck_reports_running_tunnel_failure(monkeypatch):
    """Крашлуп cloudflared (например неверный токен) обязан быть виден в самодиагностике:
    по одним ключам .env всё выглядит настроенным, а мини-апки у клиентов нет."""
    _env(monkeypatch, "named", "https://app.studio.ru", token="t" * 40)
    assert selfcheck.entry_checks() == []
    monkeypatch.setattr(tunnel, "_error", "cloudflared остановился (код 1)")
    assert any("cloudflared остановился" in i for i in selfcheck.entry_checks())


async def test_named_returns_stable_url_even_if_process_dies_at_start(monkeypatch, tmp_path):
    """Разовое падение cloudflared в первую секунду НЕ должно снимать кнопку-меню ☰:
    адрес в named постоянный и берётся из .env, а процесс поднимет сторож. Раньше
    start() отдавал None, run.py ставил MenuButtonDefault, и колбэка on_url в named нет
    вовсе — кнопка не возвращалась до перезапуска бота при живом туннеле."""
    _env(monkeypatch, "named", "https://app.studio.ru", token="tok-123")
    calls = _stub(monkeypatch, tmp_path, lifetime="0")   # заглушка умирает сразу
    assert await tunnel.start(8081) == "https://app.studio.ru"
    assert calls, "процесс всё же запускался"
    await asyncio.sleep(0.2)
    # Молчаливой зелени нет: беда видна в статусе и в самодиагностике.
    assert str(tunnel.status()["error"])
    assert any("вход снаружи" in i for i in selfcheck.entry_checks())


def test_selfcheck_still_hints_about_quick_after_tunnel_published_address(monkeypatch):
    """quick-туннель сам пишет свой адрес в config.WEBAPP_PUBLIC_URL, и раньше намёк
    «адрес меняется» замолкал через секунду после старта — то есть ровно тогда, когда
    владелец открывает экран «Статус» и зовёт самодиагностику."""
    _env(monkeypatch, "quick", "https://stub-tunnel-1.trycloudflare.com")
    monkeypatch.setattr(tunnel, "_public_url", "https://stub-tunnel-1.trycloudflare.com")
    assert any("меняется" in i for i in selfcheck.entry_checks())


async def test_start_forgets_address_of_previous_run(monkeypatch, tmp_path):
    """Повторный start() в том же процессе не имеет права отдать мёртвый прошлый адрес."""
    monkeypatch.setattr(tunnel, "_public_url", "https://dead-tunnel.trycloudflare.com")
    _env(monkeypatch, "none", "https://app.studio.ru")
    assert await tunnel.start(8081) == "https://app.studio.ru"
    assert tunnel.public_url() is None
