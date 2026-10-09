"""Загрузка по ссылке клиента не ходит во внутреннюю сеть (SSRF, app/netguard.py).

До 04.10.2026 _download_direct качал что угодно и шёл за редиректами куда угодно: ссылкой
можно было достать принтер в LAN, собственный aiohttp бота на 127.0.0.1:8081, Tailscale.

Сеть в тестах ненастоящая. DNS подменён (netguard._getaddrinfo), «публичный сайт» — локальный
сервер aiohttp, которому фейковый DNS выдаёт имя, а проверку адреса для 127.0.0.1 тест
разрешает явно (фикстура loopback_is_public). Сторож no_real_network валит тест на любой
попытке соединиться не с петлёй: сломанная защита иначе пошла бы на настоящий роутер.
"""
import ipaddress
import socket

import httpcore
import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from httpcore._backends.auto import AutoBackend

from app import deferred, downloader, netguard

STL = b"solid t\nendsolid t\n"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Каждое реальное TCP-соединение httpcore — в список; не петля → тест падает."""
    real = AutoBackend.connect_tcp
    dialed: list[tuple[str, int]] = []

    async def guard(self, host, port, *a, **kw):
        dialed.append((host, port))
        if host not in ("127.0.0.1", "::1"):
            raise AssertionError(f"тест пошёл в сеть на {host}:{port} — защита не сработала")
        return await real(self, host, port, *a, **kw)

    monkeypatch.setattr(AutoBackend, "connect_tcp", guard)
    return dialed


@pytest.fixture(autouse=True)
def no_proxies(monkeypatch, tmp_path):
    """Прокси из окружения/реестра в тестах выключены: из шелла Claude прогон наследует
    HTTPS_PROXY туннеля, и запросы уехали бы в настоящий прокси. Тест прокси включает свой."""
    monkeypatch.setattr(httpx._utils, "getproxies", lambda: {})
    monkeypatch.setattr(netguard.urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_CACHE_DIR", tmp_path / "cache")


class FakeDNS:
    def __init__(self):
        self.table: dict[str, list] = {}
        self.calls: list[str] = []

    async def __call__(self, host, port):
        self.calls.append(host)
        try:
            ipaddress.ip_address(host)
            return [host]                     # литерал IP getaddrinfo отдаёт как есть
        except ValueError:
            pass
        ans = self.table.get(host)
        if ans is None:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return list(ans.pop(0) if ans and isinstance(ans[0], list) else ans)


@pytest.fixture
def dns(monkeypatch):
    fake = FakeDNS()
    monkeypatch.setattr(netguard, "_getaddrinfo", fake)
    return fake


@pytest.fixture
def loopback_is_public(monkeypatch):
    """Локальный тест-сервер изображает публичный сайт: 127.0.0.1 считаем публичным,
    всё остальное — по настоящим правилам."""
    real = netguard.is_public_ip
    monkeypatch.setattr(netguard, "is_public_ip", lambda ip: str(ip) == "127.0.0.1" or real(ip))


@pytest.fixture
async def site():
    """«Публичный сайт»: файл модели, редиректы наружу и внутрь, петля редиректов."""
    hits: list[tuple[str, str]] = []

    async def model(req):
        hits.append((req.path, req.headers.get("Host", "")))
        return web.Response(body=STL)

    def redirect(target):
        async def h(req):
            hits.append((req.path, req.headers.get("Host", "")))
            raise web.HTTPFound(target.replace("{port}", str(req.url.port or "")))
        return h

    app = web.Application()
    app.router.add_get("/model.stl", model)
    app.router.add_get("/to-private.stl", redirect("http://192.168.1.1/admin.stl"))
    app.router.add_get("/to-intranet.stl", redirect("http://intranet.evil.test/x.stl"))
    app.router.add_get("/to-v6-loopback.stl", redirect("http://[::1]:8081/api/admin.stl"))
    app.router.add_get("/to-cdn.stl", redirect("http://cdn.example.test:{port}/model.stl"))
    app.router.add_get("/loop.stl", redirect("/loop.stl"))
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    server.hits = hits
    try:
        yield server
    finally:
        await server.close()


# ---------- правила адресов ----------
@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.8.8", "10.1.2.3", "172.16.0.1", "192.168.1.1",   # loopback, RFC1918
    "169.254.169.254",                                                   # link-local, метаданные
    "100.64.0.1", "100.100.100.100",                                     # CGNAT / Tailscale
    "224.0.0.1", "239.255.255.250", "0.0.0.0", "255.255.255.255", "240.0.0.1",
    "::1", "::", "fc00::1", "fd7a:115c:a1e0::1", "fe80::1", "fe80::1%12", "fec0::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:192.168.1.1",                            # IPv4 в IPv6-обёртке
    "2002:c0a8:101::1",                                                  # 6to4 → 192.168.1.1
    "64:ff9b::7f00:1",                                                   # NAT64 → 127.0.0.1
    "::127.0.0.1",                                                       # IPv4-совместимый
    "не адрес",
])
def test_internal_addresses_rejected(ip):
    assert not netguard.is_public_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "104.18.2.3", "2606:4700::1111", "::ffff:8.8.8.8",
                                "64:ff9b::808:808"])
def test_public_addresses_allowed(ip):
    assert netguard.is_public_ip(ip)


# ---------- сам загрузчик ----------
async def test_direct_private_ip_blocked(dns, no_real_network):
    with pytest.raises(downloader.UnsafeLinkError) as ei:
        await downloader._download_direct("http://192.168.1.1/model.stl")
    assert "внутренний адрес" in str(ei.value)
    assert no_real_network == []              # отказ ДО соединения, к роутеру не ходили


async def test_hostname_resolving_to_private_blocked(dns, no_real_network):
    dns.table["printer.evil.test"] = ["93.184.216.34", "192.168.1.50"]   # хоть один внутренний
    with pytest.raises(downloader.UnsafeLinkError) as ei:
        await downloader._download_direct("http://printer.evil.test/model.stl")
    assert "printer.evil.test" in str(ei.value)
    assert "192.168.1.50" not in str(ei.value)   # что живёт во внутренней сети — не рассказываем
    assert no_real_network == []


async def test_ipv6_loopback_blocked(dns, no_real_network):
    for url in ("http://[::1]:8081/api/admin.stl", "http://[::ffff:127.0.0.1]:8081/x.stl"):
        with pytest.raises(downloader.UnsafeLinkError):
            await downloader._download_direct(url)
    assert no_real_network == []


async def test_public_url_downloads(dns, loopback_is_public, site):
    """Обычная публичная ссылка качается. Имя files.example.test знает только фейковый DNS —
    значит, соединение пошло именно на проверенный IP (слой 2), а Host остался исходным."""
    dns.table["files.example.test"] = ["127.0.0.1"]
    paths, name = await downloader._download_direct(
        f"http://files.example.test:{site.port}/model.stl")
    assert name == "model.stl" and paths[0].read_bytes() == STL
    assert site.hits == [("/model.stl", f"files.example.test:{site.port}")]


async def test_public_to_public_redirect_followed(dns, loopback_is_public, site):
    dns.table["files.example.test"] = ["127.0.0.1"]
    dns.table["cdn.example.test"] = ["127.0.0.1"]
    paths, _ = await downloader._download_direct(
        f"http://files.example.test:{site.port}/to-cdn.stl")
    assert paths[0].read_bytes() == STL
    assert [h for _, h in site.hits] == [f"files.example.test:{site.port}",
                                         f"cdn.example.test:{site.port}"]


@pytest.mark.parametrize("path", ["/to-private.stl", "/to-intranet.stl", "/to-v6-loopback.stl"])
async def test_public_to_private_redirect_blocked(dns, loopback_is_public, site,
                                                  no_real_network, path):
    """Публичная ссылка, которая редиректом уводит во внутреннюю сеть: первый шаг честно
    сходил на «сайт», второй не ушёл никуда."""
    dns.table["files.example.test"] = ["127.0.0.1"]
    dns.table["intranet.evil.test"] = ["10.0.0.7"]
    with pytest.raises(downloader.UnsafeLinkError):
        await downloader._download_direct(f"http://files.example.test:{site.port}{path}")
    assert [p for p, _ in site.hits] == [path]
    assert all(port == site.port for _, port in no_real_network)


async def test_dns_rebinding_blocked_at_connect(dns, no_real_network):
    """Имя на проверке отвечает публичным адресом, а к моменту соединения — петлёй. Ловит
    второй слой: соединение идёт только на адрес, проверенный в момент connect."""
    dns.table["rebind.evil.test"] = [["93.184.216.34"], ["127.0.0.1"]]
    with pytest.raises(downloader.UnsafeLinkError):
        await downloader._download_direct("http://rebind.evil.test/model.stl")
    assert dns.calls == ["rebind.evil.test", "rebind.evil.test"]
    assert no_real_network == []


async def test_redirect_loop_honest_error(dns, loopback_is_public, site):
    dns.table["files.example.test"] = ["127.0.0.1"]
    with pytest.raises(downloader.DownloadError, match="перенаправляет слишком много раз"):
        await downloader._download_direct(f"http://files.example.test:{site.port}/loop.stl")
    assert len(site.hits) == netguard.MAX_REDIRECTS + 1


async def test_unresolvable_host_is_not_a_security_block(dns):
    with pytest.raises(downloader.DownloadError) as ei:
        await downloader._download_direct("http://no-such-host.test/model.stl")
    assert not isinstance(ei.value, downloader.UnsafeLinkError)
    assert "Не удалось найти сайт" in str(ei.value)


async def test_fetch_from_url_blocks_and_caches_nothing(dns, tmp_path):
    with pytest.raises(downloader.UnsafeLinkError):
        await downloader.fetch_from_url("http://127.0.0.1:8081/api/admin/backup.zip")
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").iterdir())


async def test_thingiverse_file_link_to_private_blocked(dns, monkeypatch, no_real_network):
    """API у Thingiverse свой, но download_url приходит из ответа — и тоже проверяется."""
    dns.table["api.thingiverse.com"] = ["93.184.216.34"]
    monkeypatch.setattr(downloader.config, "THINGIVERSE_TOKEN", "tok")

    def api(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[{"name": "x.stl",
                                              "download_url": "http://169.254.169.254/x.stl"}])
        return httpx.Response(200, json={"name": "Thing"})

    real = netguard.public_client
    monkeypatch.setattr(netguard, "public_client", lambda **kw: real(
        mounts={"https://api.thingiverse.com": httpx.MockTransport(api)}, **kw))
    with pytest.raises(downloader.UnsafeLinkError):
        await downloader._download_thingiverse("https://www.thingiverse.com/thing:123")
    assert no_real_network == []


async def test_env_proxy_still_used_and_guarded(dns, monkeypatch, site, no_real_network):
    """Прокси из окружения (туннель владельца) по-прежнему работает: публичное имя уходит
    через него, а внутренний адрес отбивается ещё до прокси."""
    proxy = {"http": f"http://127.0.0.1:{site.port}"}
    monkeypatch.setattr(httpx._utils, "getproxies", lambda: proxy)
    monkeypatch.setattr(netguard.urllib.request, "getproxies", lambda: proxy)
    dns.table["files.example.test"] = ["93.184.216.34"]   # сами туда не ходим — ходит прокси
    paths, _ = await downloader._download_direct("http://files.example.test/model.stl")
    assert paths[0].read_bytes() == STL
    with pytest.raises(downloader.UnsafeLinkError):
        await downloader._download_direct("http://10.99.0.2/model.stl")
    assert len(site.hits) == 1 and no_real_network == [("127.0.0.1", site.port)]


def test_rebinding_guard_installed():
    """Канарейка для обновлений httpx: слой 2 ставится через внутренности пула
    (публичного network_backend у транспорта нет). Упало — смотри netguard._public_transport."""
    client = netguard.public_client()
    transport = client._mounts[httpx._utils.URLPattern("all://")]
    assert isinstance(transport._pool, httpcore.AsyncConnectionPool)
    assert isinstance(transport._pool._network_backend, netguard._PublicOnlyBackend)


def test_unsafe_link_is_permanent_for_deferred_queue():
    """Очередь досчёта не повторяет такую ссылку шесть раз и отдаёт клиенту наш текст."""
    assert deferred._is_permanent(downloader.UnsafeLinkError("x"))
