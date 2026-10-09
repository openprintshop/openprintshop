"""Защита загрузок по ссылкам клиента от SSRF: бот не ходит во внутреннюю сеть.

Зачем. Ссылку на модель присылает клиент (мини-апка `/api/model`, голая ссылка в чат), и до
04.10.2026 загрузчик качал по ней что угодно и шёл за редиректами куда угодно. То есть любой
клиент мог заставить бота сходить на принтер в локальной сети (192.168.x), на собственный
aiohttp бота на 127.0.0.1:8081 (часть админских ручек доверяет локальным запросам), в Tailscale
(100.64/10) или на link-local метаданные. Найдено в разборе схемы разделения на VPS.

Как. Два слоя, и оба нужны:

1. Хук запроса httpx (`_check_hop`) — перед КАЖДЫМ запросом, в том числе перед каждым шагом
   редиректа (httpx зовёт хуки "request" внутри своего цикла редиректов): имя резолвится, и
   если хоть один адрес не публичный — запрос не уходит. Работает и через прокси из окружения
   (на машине владельца дочерние процессы наследуют HTTP(S)_PROXY туннеля): проверяется
   логический адрес из ссылки, а не адрес прокси.
2. Сетевой бэкенд httpcore (`_PublicOnlyBackend`) — при КАЖДОМ TCP-соединении резолвит имя
   сам, проверяет все адреса и соединяется именно с проверенным IP. Без этого слоя хватило
   бы DNS rebinding: имя с TTL 0 на первый запрос отвечает публичным адресом (проверка
   пройдена), а на второй, уже из httpx, — 127.0.0.1. Сервисы вроде rbndr.us делают это за
   минуту, без своего DNS. TLS не страдает: SNI и проверка сертификата идут по имени из
   ссылки, а не по IP (httpcore берёт server_hostname из origin, а не из connect_tcp).

Через прокси второй слой не ставится (адрес резолвит прокси, а соединяемся мы с ним самим),
там остаётся первый. Остаточный риск — rebinding именно на стороне прокси; закрывать его
подменой хоста на IP нельзя: сломается маршрутизация туннеля по доменам.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.request

import httpcore
import httpx

log = logging.getLogger(__name__)

# Сколько редиректов подряд терпим. У httpx по умолчанию 20; честным площадкам хватает 2–3
# (Printables → files.printables.com, Thingiverse → CDN, Dropbox/Google Drive — до 4–5).
MAX_REDIRECTS = 10
# getaddrinfo на Windows уходит в поток и на несуществующем имени может висеть десятки секунд
RESOLVE_TIMEOUT = 10.0

# NAT64 (RFC 6052): шлюз превращает 64:ff9b::a.b.c.d в обращение к a.b.c.d — значит, решает
# вложенный IPv4. ::/96 — устаревшие «IPv4-совместимые» адреса: публичных хостов на них нет,
# а ipaddress считает их глобальными. fec0::/10 — устаревший site-local (аналог 10/8), его
# ipaddress тоже считает глобальным.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_V4_COMPAT = ipaddress.IPv6Network("::/96")
_SITE_LOCAL = ipaddress.IPv6Network("fec0::/10")


class BlockedAddress(Exception):
    """Адрес, куда по ссылке клиента ходить нельзя. str(exc) — честный текст для клиента
    (без резолвнутого IP: незачем рассказывать, что за имя живёт во внутренней сети),
    `detail` — подробности для лога владельца."""

    def __init__(self, host: str, detail: str, text: str | None = None):
        self.host = host
        self.detail = detail
        super().__init__(text or (
            f"Ссылка ведёт (напрямую или через перенаправление) на внутренний адрес «{host}» — "
            "в локальную сеть или на сам сервер бота. Такие ссылки бот не открывает из "
            "соображений безопасности. Пришлите ссылку на файл на публичном сайте или "
            "загрузите сам файл (.stl, .3mf, .obj, .zip)."))


class ResolveError(Exception):
    """Имя из ссылки не резолвится. Отказываем сразу (fail closed): проверить такой адрес
    нечем, а без проверки его пришлось бы отдать на откуп прокси или повторному резолву."""

    def __init__(self, host: str):
        self.host = host
        super().__init__(f"Не удалось найти сайт «{host}» — проверьте ссылку.")


def is_public_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Публичный ли адрес. False — loopback, RFC1918, link-local, CGNAT 100.64/10 (Tailscale),
    multicast, unspecified, зарезервированные и документационные, IPv6 ::1/fc00::/7/fe80::/10,
    а также IPv6-обёртки над такими IPv4 (::ffff:127.0.0.1, 6to4, NAT64)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:        # ::ffff:a.b.c.d — тот же IPv4 в обёртке
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:         # 2002:aabb:ccdd:: — 6to4 ведёт на a.b.c.d
            addr = addr.sixtofour
        elif addr in _NAT64:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        elif addr in _V4_COMPAT or addr in _SITE_LOCAL:
            return False
    # is_global уже исключает private/loopback/link-local/100.64/10/unspecified/reserved,
    # а multicast (224/4, ff00::/8) ipaddress числит глобальным — отсекаем отдельно
    return addr.is_global and not addr.is_multicast


async def _getaddrinfo(host: str, port: int) -> list[str]:
    """Все адреса имени (точка подмены для тестов). Литерал IP getaddrinfo возвращает как
    есть, поэтому IP в ссылке проходит ту же проверку, что и имя."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def resolve_public(host: str, port: int, timeout: float | None = None) -> list[str]:
    """Резолвит имя и возвращает его адреса, только если ВСЕ они публичные.

    Именно все, а не «хоть один»: дальше соединение может пойти на любой из них, и запись
    вида «публичный + 127.0.0.1» иначе стала бы лотереей в пользу атакующего."""
    host = host.strip("[]")
    try:
        async with asyncio.timeout(min(timeout or RESOLVE_TIMEOUT, RESOLVE_TIMEOUT)):
            addrs = await _getaddrinfo(host, port)
    except (OSError, UnicodeError, TimeoutError) as exc:
        raise ResolveError(host) from exc
    if not addrs:
        raise ResolveError(host)
    bad = [a for a in addrs if not is_public_ip(a.split("%", 1)[0])]
    if bad:
        raise BlockedAddress(host, f"{host}:{port} -> {', '.join(bad)}")
    return addrs


async def check_url(url: str | httpx.URL) -> None:
    """Можно ли идти по этой ссылке: только http/https и только публичные адреса.
    Бросает BlockedAddress / ResolveError."""
    u = url if isinstance(url, httpx.URL) else httpx.URL(url)
    host = u.raw_host.decode("ascii")         # IDNA-форма: «пример.рф» → xn--e1afmkfd.xn--p1ai
    if u.scheme not in ("http", "https"):
        raise BlockedAddress(host, f"схема {u.scheme!r}",
                             text=f"Ссылка ведёт на неподдерживаемый адрес ({u.scheme}://…) — "
                                  "бот качает только по http и https.")
    if not host:
        raise BlockedAddress(host, "пустой хост", text="В ссылке нет адреса сайта.")
    await resolve_public(host, u.port or (443 if u.scheme == "https" else 80))


async def _check_hop(request: httpx.Request) -> None:
    """Хук httpx: проверка перед каждым запросом, включая каждый шаг редиректа (слой 1)."""
    await check_url(request.url)


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Сетевой бэкенд httpcore: соединяется только с проверенным публичным IP (слой 2).

    Резолв и соединение — в одном месте, поэтому подменить адрес между проверкой и
    connect уже нельзя. Адреса пробуем по очереди, как это делает обычный клиент: у CDN
    часто есть и IPv6, который на машине без IPv6 не соединится."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        try:
            addrs = await resolve_public(host, port, timeout)
        except ResolveError as exc:
            # так же, как упал бы сам httpcore на нерезолвящемся имени → httpx.ConnectError
            raise httpcore.ConnectError(str(exc)) from exc
        last: Exception | None = None
        for ip in addrs:
            try:
                return await self._inner.connect_tcp(ip, port, timeout=timeout,
                                                     local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last  # type: ignore[misc]   # addrs не пуст — resolve_public это гарантирует

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedAddress(path, "unix socket", text="Ссылка ведёт на локальный сокет.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _public_transport() -> httpx.AsyncHTTPTransport:
    """Транспорт httpx с бэкендом слоя 2. Публичного параметра network_backend у
    httpx.AsyncHTTPTransport нет, поэтому меняем бэкенд у его пула — до первого соединения,
    пул передаёт бэкенд каждому новому соединению. Если httpx однажды поменяет внутренности,
    загрузки не ломаем: остаётся слой 1, а tests/test_downloader_ssrf.py упадёт и скажет."""
    transport = httpx.AsyncHTTPTransport()
    pool = getattr(transport, "_pool", None)
    inner = getattr(pool, "_network_backend", None)
    if isinstance(pool, httpcore.AsyncConnectionPool) and inner is not None:
        pool._network_backend = _PublicOnlyBackend(inner)
    else:
        log.warning("netguard: у httpx нет ожидаемого пула — защита от DNS rebinding не "
                    "поставлена, работает только проверка перед запросом")
    return transport


def public_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient для ссылок, пришедших от клиента (или найденных по ним).

    Слой 2 ставится маунтом "all://", а не transport=: transport= выключил бы прокси из
    окружения, а маунты прокси ("http://", "https://") специфичнее "all://" и по-прежнему
    выигрывают. Исключение — ALL_PROXY: его маунт тоже "all://", и наш его бы затёр, —
    поэтому при нём слой 2 не ставим, как и при любом прокси. Хосты из NO_PROXY httpx шлёт
    штатным транспортом (маунт None), им остаётся слой 1 — у владельца там только LAN-адреса,
    которые слой 1 и так отбивает."""
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [_check_hop, *hooks.get("request", [])]
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("max_redirects", MAX_REDIRECTS)
    mounts = dict(kwargs.pop("mounts", None) or {})
    if not (kwargs.get("trust_env", True) and urllib.request.getproxies().get("all")):
        mounts.setdefault("all://", _public_transport())
    return httpx.AsyncClient(event_hooks=hooks, mounts=mounts, **kwargs)
