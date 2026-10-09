"""Клиент приёмной на ПК (Э1, docs/Перспективные разработки/vps-relay-2026-10-04.md §3).

При TG_SOURCE=relay бот сам Telegram НЕ опрашивает: единственный поллер — «приёмная» psb-relay
на VPS. Она пишет каждый апдейт в свой журнал и отдаёт его ПК по long-poll `POST /_r/poll`
(направление ПК→VPS: ни Funnel, ни белый IP для этого не нужны). Отсюда и разделение труда:

* `RelayLink.run` — главный цикл вместо `dp.start_polling`: забрать пачку, раздать апдейты
  диспетчеру ОТДЕЛЬНЫМИ задачами (как это делает aiogram в polling), подтвердить сделанное в
  следующем запросе (`ack`). Подтверждение — только после обработки: если ПК умрёт посреди
  пачки, приёмная через 600 с отдаст её снова, а повтор отсечёт `tg_seen` («дубль хуже
  пропуска»: апдейт, по которому хендлер уже начал работу, второй раз не обрабатывается);
* события приёмной (`deferred_order` — заказ, оставленный, пока ПК был выключен) уходят в
  очередь досчёта (`deferred.import_relay_order`) и подтверждаются только после успеха;
* снимок для офлайна (`/_r/snapshot`) — счета к оплате, заказы клиентов, бренд: по нему
  приёмная отвечает, пока ПК выключен. Раз в минуту и только если что-то изменилось;
* аварийный прямой режим: приёмная молчит дольше TG_FALLBACK_AFTER — бот опрашивает Telegram
  сам (с тем же дедупом, без сброса очереди) и возвращается, как только приёмная дважды подряд
  ответит на проверку или Telegram скажет 409 и внеочередная проверка пройдёт (409 значит лишь
  «приёмная опрашивает Telegram», а не «ПК до неё достаёт»);
* сторож poll-режима (`guard_poll_mode`): бот в TG_SOURCE=poll при настроенной приёмной
  выключает её опрос, если тот остался включён (откат, сделанный при недоступном VPS).

Всё это — фоновая работа: ни /health, ни деск, ни LCD её не ждут (грабли разреза: сторож не
должен ронять ПК из-за недоступного VPS). Подпись — HMAC-SHA256, свой маленький код: пакет
relay/ живёт на VPS отдельно и в app не импортируется.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import TelegramMethod
from aiogram.types import MenuButtonDefault, MenuButtonWebApp, WebAppInfo

from . import config, db, deferred, notify, payments, relaybundle, replica, version

log = logging.getLogger(__name__)

#: Версия протокола ПК↔приёмная. Приёмная принимает PROTO и PROTO-1 (relay/__init__.py).
PROTO = 1
CLIENT_VERSION = "1.0.0"

H_TS = "X-PSB-Ts"
H_NONCE = "X-PSB-Nonce"
H_SIG = "X-PSB-Sig"
H_PROTO = "X-PSB-Proto"

#: Ключ короче — relay-режим не стартует (контракт §2: подпись не должна подбираться).
MIN_KEY_BYTES = 32

POLL_WAIT = 25.0            # long-poll /_r/poll (приёмная держит до 30 с)
POLL_MAX = 100
CONNECT_TIMEOUT = 5.0
READ_EXTRA = 15.0           # чтение ответа = wait + столько
#: Соединение живёт дольше цикла опроса: иначе каждый опрос платил бы новый TLS до VPS.
KEEPALIVE = 60.0
#: Сколько хендлеров СООБЩЕНИЙ одновременно (см. _needs_slot). aiogram в polling не ограничивает
#: вовсе; здесь потолок, чтобы флуд моделями и ссылками не раздул память, но с запасом:
#: загрузка и расчёт держат слот минутами.
HANDLER_LIMIT = 32
#: Сколько апдейтов держим у себя (в работе + ждут слота). Больше — просим у приёмной по одному:
#: опрос не останавливаем никогда, иначе через 45 с она сочтёт ПК выключенным.
BACKLOG_LIMIT = 200
ACK_CHUNK = 500             # приёмная принимает до 1000 id в списке
BACKOFF = (1.0, 15.0)       # пауза между неудачными опросами: от и до
#: Пустой ответ быстрее этого при wait>0 — аномалия (приёмная гасится, лишние опросы):
#: не крутимся вхолостую, а выдерживаем паузу.
MIN_POLL_GAP = 1.0
PROBE_EVERY = 60.0          # прямой режим: проверка /_r/health
PROBE_STREAK = 2            # столько удачных проверок подряд — и назад к приёмной
#: Опрос Telegram у приёмной в этих состояниях — рабочий: можно возвращаться. conflict — это
#: 409 от нашего же прямого опроса, starting — первые секунды после её рестарта.
POLLING_WORKS = ("ok", "conflict", "starting")
PROXY_STICKY = 600.0        # прокси помог — столько им и пользуемся, не тратя 5 с на прямой
SNAPSHOT_EVERY = 60.0
SNAPSHOT_FORCE = 600.0      # без изменений снимок всё равно освежаем раз в 10 мин
#: Копия базы на VPS (app/replica.py, контракт §7): раз в минуту, если база изменилась.
REPLICA_EVERY = 60.0
#: Пакет каталога для мини-аппа без ПК (app/relaybundle.py): раз в 5 мин, если изменился,
#: и раз в час в любом случае (вдруг у приёмной его стёрли).
BUNDLE_EVERY = 300.0
BUNDLE_FORCE = 3600.0
#: Выгрузки копии и пакета больше обычных ручек — читаем ответ дольше.
UPLOAD_READ = 60.0
#: Прогрев картинок каталога в кэше nginx на VPS: параллельно не больше стольких.
WARM_PARALLEL = 2
PRUNE_FIRST = 300.0
PRUNE_EVERY = 86400.0
DRAIN_SEC = 5.0             # остановка: столько ждём хендлеры, потом снимаем
RELAY_STALL_ALERT = 600.0   # приёмная доступна, но Telegram не опрашивает столько — алерт
ALERT_THROTTLE = 1800
#: Сторож poll-режима: первая проверка приёмной после старта и дальше раз в столько секунд.
GUARD_FIRST = 30.0
GUARD_EVERY = 300.0
#: /_r/control off ждёт, пока приёмная подтвердит Telegram offset (до 35 с), — читаем дольше.
CONTROL_READ = 50.0
MENU_TEXT = "📱 Приложение"   # тот же текст ☰, что ставит run._bringup_webapp

# Снимок заказов для «Мои заказы» в офлайне.
ORDERS_DAYS = 90
ORDERS_PER_USER = 10
#: Потолок строк одного запроса снимка: защита от разросшейся базы, не рабочий предел.
SNAPSHOT_ROWS = 5000
#: Закрытые и «для себя» в «Мои заказы» не показываем — там только то, о чём клиент ещё думает.
_ORDERS_SKIP = ("cancelled", "rejected", "handed", "self")

# Снимок для мини-аппа на домене (Э2): обезличенные опции и «Мои заказы» по клиентам — их
# отдаёт приёмная, пока ПК выключен (relay/web.py). Формат — ровно ответы /api/options и
# /api/orders: фронт рисует их теми же экранами.
WEB_ORDERS_DAYS = 90
WEB_ORDERS_PER_USER = 15
WEB_USERS_MAX = 300
#: Весь раздел web — не больше этого: приёмная принимает снимок до 2 МиБ целиком.
WEB_BUDGET = 1_200_000
#: Карточки клиента пересобираем, когда менялись его заказы, но не реже раза в столько: в
#: карточке есть и то, что от строки заказа не зависит (очередь печати, «печатается сейчас»,
#: непрочитанные ответы мастера).
WEB_ORDERS_TTL = 600.0
#: Персональные поля /api/options, которые в обезличенном снимке обнуляются (uid=0 их и так
#: не имеет — список здесь как гарантия: что бы ни добавили в build_options, личное не уедет).
_WEB_PERSONAL = {"pending_orders": 0, "orders_count": 0, "referrals": 0, "models_count": 0,
                 "is_admin": False, "last_order": None, "fav_ids": []}
#: Файл заявки из мини-аппа (relay_file): приёмная принимает до 50 МБ, запас сверху.
RELAY_FILE_LIMIT = 64 * 1024 * 1024
_REF = re.compile(r"[0-9a-f]{32}")

_EVENT_ID = re.compile(r"[0-9a-f]{32}")
_HEX = re.compile(r"[0-9a-fA-F]+")


class RelayConfigError(Exception):
    """TG_SOURCE=relay, а настройки не годятся: бот не стартует в этом режиме и говорит почему."""


class RelayError(Exception):
    """Приёмная недоступна или отвергла запрос. Для цикла всё это — «приёмная офлайн».

    kind: net (сеть/таймаут), http (5xx, мусор вместо JSON), auth (404 — подпись не принята),
    proto (409 — версии протокола разошлись), bad (400/413 — запрос ПК кривой)."""

    def __init__(self, reason: str, kind: str = "net") -> None:
        super().__init__(reason)
        self.kind = kind


# ── подпись запросов (HMAC-SHA256, общий ключ ПК и приёмной) ──────────────────────

def signature(key: str, method: str, path: str, ts: str | int, nonce: str, body: bytes) -> str:
    """hex(HMAC-SHA256(key, "METHOD\\nPATH\\nTS\\nNONCE\\nsha256hex(body)")). Ключ — строка как
    есть (64 hex-символа — это и есть ключ, без раскодирования), так же у приёмной."""
    canonical = "\n".join([method.upper(), path, str(ts), nonce, hashlib.sha256(body).hexdigest()])
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_headers(key: str, method: str, path: str, body: bytes, *, ts: int | None = None,
                 nonce: str | None = None) -> dict[str, str]:
    """Заголовки подписанного запроса. Nonce одноразовый (приёмная помнит его 600 с), поэтому
    каждую попытку — в том числе запасную через прокси — подписываем заново."""
    ts = int(time.time()) if ts is None else ts
    nonce = secrets.token_hex(16) if nonce is None else nonce
    return {H_TS: str(ts), H_NONCE: nonce, H_SIG: signature(key, method, path, ts, nonce, body),
            H_PROTO: str(PROTO)}


def check_key(text: str) -> str:
    """Ключ → строка для HMAC. Те же правила, что у приёмной (relay/config.check_key): hex —
    не короче 64 знаков (32 hex — это лишь 16 байт случайности), иначе — не меньше 32 байт."""
    key = (text or "").strip()
    if not key:
        raise RelayConfigError("ключ подписи пуст")
    if _HEX.fullmatch(key):
        if len(key) < 2 * MIN_KEY_BYTES:
            raise RelayConfigError(f"ключ подписи — {len(key)} hex-символов, нужно не меньше {2 * MIN_KEY_BYTES}")
    elif len(key.encode("utf-8")) < MIN_KEY_BYTES:
        raise RelayConfigError(f"ключ подписи короче {MIN_KEY_BYTES} байт")
    return key


def load_key(path: Path) -> str:
    """utf-8-sig: Windows PowerShell 5.1 пишет файлы с BOM, и он остался бы в ключе — подпись
    не сходилась бы ни разу, а причина была бы невидима."""
    try:
        text = Path(path).read_text("utf-8-sig")
    except FileNotFoundError:
        raise RelayConfigError(f"нет файла ключа {path} (его кладёт tools/edge/edge.ps1 -Action setup — "
                               f"по пути RELAY_KEY_FILE из .env, а без него в DATA_DIR\\relay.key)") from None
    except OSError as exc:
        raise RelayConfigError(f"ключ {path} не читается: {exc}") from None
    return check_key(text)


def config_problem() -> str | None:
    """Что мешает relay-режиму, или None. Зовёт run.py ДО подъёма чего бы то ни было: в relay-
    режиме без приёмной бот глух, и узнать об этом надо громко и сразу, а не через сутки."""
    url = config.RELAY_URL
    if not url:
        return "не задан RELAY_URL (адрес приёмной, например https://203.0.113.10:8450)"
    if not url.lower().startswith(("https://", "http://")):
        return f"RELAY_URL должен начинаться с https:// — сейчас {url!r}"
    try:
        load_key(config.RELAY_KEY_FILE)
    except RelayConfigError as exc:
        return str(exc)
    return None


def env_proxy(url: str) -> str | None:
    """Прокси из переменных окружения для адреса url (HTTPS_PROXY/HTTP_PROXY, NO_PROXY).

    Только окружение, не реестр Windows. Это может быть локальный прокси (например,
    127.0.0.1:10809), который ходит на тот же VPS — запасная дорога, если прямой маршрут
    до VPS провайдер уронил."""
    parts = urlsplit(url)
    proxies = urllib.request.getproxies_environment()
    if not proxies or urllib.request.proxy_bypass_environment(parts.hostname or "", proxies):
        return None
    proxy = (proxies.get(parts.scheme) or "").strip()
    if not proxy:
        return None
    return proxy if "://" in proxy else "http://" + proxy


# ── слоты хендлеров ──────────────────────────────────────────────────────────

def _message_needs_slot(msg: dict) -> bool:
    if isinstance(msg.get("successful_payment"), dict):
        return False        # оплата: деньги клиента уже списаны — записать её без очереди
    text = msg.get("text")
    return not (isinstance(text, str) and text.startswith("/"))    # команды — выход для клиента


def _needs_slot(raw: dict) -> bool:
    """Ждёт ли апдейт общего слота (HANDLER_LIMIT).

    Слот — защита памяти от флуда моделями и ссылками, а качать и считать минутами могут
    только сообщения (документ, ссылка, шаг мастера расчёта). Остальное — без очереди, как в
    poll (aiogram там хендлеры не ограничивает): pre_checkout (у Telegram 10 с на ответ),
    оплата, кнопки, команды. Раньше слот был общим: 32 повисших загрузки (встал cpupool,
    сервер по ссылке отдаёт байт в минуту) глушили в relay-режиме всё, включая оплату, а
    приёмная, видя ПК онлайн, сама не отвечала."""
    for key in ("message", "edited_message"):
        msg = raw.get(key)
        if isinstance(msg, dict):
            return _message_needs_slot(msg)
    return False


def _update_needs_slot(update) -> bool:
    """То же для разобранного aiogram Update (прямой режим)."""
    msg = update.message or update.edited_message
    if msg is None:
        return False
    return _message_needs_slot({"successful_payment": {} if msg.successful_payment else None,
                                "text": msg.text})


# ── снимок для офлайна ───────────────────────────────────────────────────────

async def _snapshot_rows(since: float) -> list[SimpleNamespace]:
    """Одним SELECT'ом — всё, что нужно снимку: неоплаченные заказы в оплачиваемых статусах
    (без ограничения по давности — ПК принял бы и такой счёт) и активные заказы за ORDERS_DAYS.
    Только нужные колонки: полные строки заказов (breakdown, превью…) снимку ни к чему, а
    сотни клиентов не превращаются в сотни запросов. Чтение — тот же приём, что у
    maintenance/tuning: прямой SELECT через соединение фасада db."""
    pay = payments.PAYABLE_STATUSES
    sql = ("SELECT id, user_id, status, price, pending_price, paid, model_label, created_at, order_group "
           "FROM requests "
           f"WHERE (status IN ({','.join('?' * len(pay))}) AND COALESCE(paid, 0) = 0) "
           f"OR (created_at >= ? AND status NOT IN ({','.join('?' * len(_ORDERS_SKIP))})) "
           "ORDER BY id DESC LIMIT ?")
    cur = await db._conn().execute(sql, (*pay, int(since), *_ORDERS_SKIP, SNAPSHOT_ROWS))
    return [SimpleNamespace(**dict(r)) for r in await cur.fetchall()]


async def build_snapshot(now: float | None = None) -> dict:
    """Снимок для приёмной (`/_r/snapshot`, формат — relay/payments.normalize_snapshot).

    payable — то, что приёмная вправе принять в pre_checkout, пока ПК выключен: правила
    payments.is_payable, а сумма — payments.order_total_minor (её же сверяет
    precheckout_error; с доставкой корзины). orders — «Мои заказы» по клиентам: активные за
    ORDERS_DAYS, не больше ORDERS_PER_USER свежих на клиента."""
    now = time.time() if now is None else now
    _token, currency = await payments.pay_settings()
    ts = await db.get_text_settings()
    since = now - ORDERS_DAYS * 86400
    payable: list[dict] = []
    orders: dict[str, list] = {}
    for r in await _snapshot_rows(since):
        if not r.user_id:
            continue
        if payments.is_payable(r):
            payable.append({"rid": int(r.id), "uid": int(r.user_id),
                            "total_minor": int(await payments.order_total_minor(r)), "paid": False})
        if r.status in _ORDERS_SKIP or int(r.created_at or 0) < since:
            continue
        mine = orders.setdefault(str(int(r.user_id)), [])
        if len(mine) >= ORDERS_PER_USER:
            continue
        # Цена, о которой клиент сейчас думает: при «изменена цена»/«счёт выставлен» это новая
        # сумма на подтверждении, а не старая. Цена ещё считается — 0 (приёмная так и напишет).
        price = r.pending_price if r.status in ("price_changed", "invoiced") and r.pending_price else r.price
        mine.append({"id": int(r.id), "label": str(r.model_label or "")[:200], "status": str(r.status or ""),
                     "price": round(float(price or 0), 2)})
    return {
        "generated_at": now,
        "currency": currency,
        "payable": payable,
        "orders": orders,
        # Из текстов приёмная читает только бренд (relay/service.Relay.brand) — его и шлём.
        "texts": {"brand_title": str(ts.get("brand_title") or "")},
        # Копии сообщений и заявки из офлайна — всей команде: это операционное, не деньги.
        "admin_ids": notify.recipients(notify.ALL_STAFF),
        # Мини-апп на домене без ПК (Э2). Сбой сборки раздела не должен лишить приёмную
        # остального снимка (оплаты в офлайне важнее экрана «Мои заказы»).
        "web": await _web_section_safe(now),
    }


_web_cache: dict[int, tuple[tuple, float, list]] = {}   # uid → (подпись заказов, когда собрано, карточки)


async def _web_section_safe(now: float) -> dict:
    try:
        return await build_web_section(now)
    except Exception:  # noqa: BLE001
        log.warning("снимок для мини-аппа без ПК не собрался — уйдёт без него", exc_info=True)
        return {}


async def _web_users(since: float) -> list[tuple[int, tuple]]:
    """Клиенты с заказами, менявшимися за WEB_ORDERS_DAYS, и «подпись» их заказов (сколько и
    когда менялись последний раз) — по ней видно, что карточки пора пересобрать. Свежие —
    первыми: упрёмся в бюджет — без снимка останутся самые давние."""
    cur = await db._conn().execute(
        "SELECT user_id, COUNT(*) AS n, MAX(COALESCE(updated_at, created_at)) AS last FROM requests "
        "WHERE user_id IS NOT NULL AND COALESCE(updated_at, created_at) >= ? "
        "GROUP BY user_id ORDER BY last DESC LIMIT ?", (int(since), WEB_USERS_MAX))
    return [(int(r["user_id"]), (int(r["n"]), int(r["last"] or 0))) for r in await cur.fetchall()]


async def build_web_section(now: float) -> dict:
    """Раздел web снимка: опции мини-аппа для «никого» (uid=0) и «Мои заказы» по клиентам —
    теми же функциями, что отвечают ручки /api/options и /api/orders (webapp.build_options,
    clientflow.build_user_orders). Карточки пересобираются только у тех, чьи заказы менялись."""
    from . import webapp
    from .web import clientflow

    opts = await webapp.build_options(0)
    opts.update(_WEB_PERSONAL)
    orders: dict[str, list] = {}
    used = 0
    alive = set()
    for uid, sig in await _web_users(now - WEB_ORDERS_DAYS * 86400):
        alive.add(uid)
        cached = _web_cache.get(uid)
        if cached is not None and cached[0] == sig and now - cached[1] < WEB_ORDERS_TTL:
            cards = cached[2]
        else:
            try:
                cards = await clientflow.build_user_orders(uid, limit=WEB_ORDERS_PER_USER)
            except Exception:  # noqa: BLE001 — один кривой заказ не лишает снимка остальных
                log.warning("снимок мини-аппа: заказы клиента %s не собрались", uid, exc_info=True)
                continue
            _web_cache[uid] = (sig, now, cards)
        size = len(json.dumps(cards, ensure_ascii=False))
        if used + size > WEB_BUDGET:
            break
        used += size
        if cards:
            orders[str(uid)] = cards
    for uid in [u for u in _web_cache if u not in alive]:
        del _web_cache[uid]
    return {"options": opts, "orders": orders}


async def fetch_relay_file(ref: str, *, size: int | None = None, sha256: str | None = None,
                           limit: int = RELAY_FILE_LIMIT) -> bytes:
    """Файл модели из заявки мини-аппа (Э2), принятый приёмной, пока ПК был выключен.

    Зовёт сборщик досчёта (app/deferred.py) — в любом режиме бота, в том числе после отката на
    poll, поэтому без живого RelayLink: свой короткий сеанс, подпись тем же ключом, сначала
    напрямую, при отказе соединения — через прокси окружения. RelayError.kind:
    gone — файла у приёмной уже нет (повтор не поможет), net/http — сеть (повторить позже)."""
    if not _REF.fullmatch(ref or ""):
        raise RelayError("номер файла заявки кривой", "bad")
    problem = config_problem()
    if problem:
        raise RelayError(problem, "config")
    key = load_key(config.RELAY_KEY_FILE)
    url = config.RELAY_URL
    path = f"/_r/file/{ref}"
    proxy = env_proxy(url)
    routes: list[str | None] = [None, proxy] if proxy else [None]
    timeout = aiohttp.ClientTimeout(total=300, connect=CONNECT_TIMEOUT, sock_connect=CONNECT_TIMEOUT,
                                    sock_read=60)
    buf = bytearray()
    async with aiohttp.ClientSession(trust_env=False) as session:
        for i, via in enumerate(routes):
            buf = bytearray()
            try:
                async with session.get(url + path, headers=sign_headers(key, "GET", path, b""), proxy=via,
                                       timeout=timeout) as resp:
                    if resp.status == 404:
                        raw = await resp.read()
                        if b"unknown file" in raw:
                            raise RelayError("файла заявки у приёмной уже нет", "gone")
                        raise RelayError("приёмная ответила 404 — подпись не принята", "auth")
                    if resp.status != 200:
                        raise RelayError(f"приёмная ответила {resp.status}", "http")
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        buf += chunk
                        if len(buf) > limit:
                            raise RelayError("файл заявки больше лимита", "bad")
            except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
                if i + 1 < len(routes):
                    continue
                raise RelayError(f"нет соединения ({type(exc).__name__})") from None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise RelayError(f"обрыв ({type(exc).__name__})") from None
            break
    data = bytes(buf)
    if size is not None and len(data) != int(size):
        raise RelayError(f"файл заявки пришёл не целиком ({len(data)} из {size} байт)")
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise RelayError("файл заявки пришёл повреждённым (sha256 не сошёлся)")
    return data


def snapshot_digest(snap: dict) -> str:
    """Хэш содержимого без времени генерации — по нему «изменился ли снимок»."""
    body = {k: v for k, v in snap.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ── модульное состояние: ☰ и статус ─────────────────────────────────────────

#: Адрес ☰, который положил run._bringup_webapp: None — ещё неизвестен (в hello не шлём, и
#: приёмная кнопку не трогает), "" — мини-апки нет (приёмная ставит обычное меню).
_menu_url: str | None = None
_current: RelayLink | None = None


def set_menu_url(url: str | None) -> None:
    """run.py: в relay-режиме ☰ ставит приёмная — ей уходит адрес в hello. В аварийном прямом
    режиме кнопку ставит сам ПК, и тогда новый адрес применяется сразу."""
    global _menu_url
    _menu_url = (url or "").strip()
    link = _current
    if link is not None:
        link.menu_changed()


def menu_url() -> str | None:
    return _menu_url


def _direct_web(on: bool) -> bool:
    """Э2: WEBAPP_PUBLIC_URL — домен мини-апки на VPS, а приёмная недоступна (VPS, скорее всего,
    лежит вместе с доменом): новые кнопки ведут на прямой вход ПК (WEBAPP_DIRECT_URL), пока она
    не вернётся. True — адрес сменился."""
    relay_url, direct = config.WEBAPP_RELAY_URL, config.WEBAPP_DIRECT_URL
    if not relay_url or not direct or relay_url.rstrip("/") == direct:
        return False
    want = direct if on else relay_url
    if config.WEBAPP_PUBLIC_URL == want:
        return False
    config.WEBAPP_PUBLIC_URL = want
    return True


def _direct_menu_url(url: str) -> str:
    """Адрес ☰ для прямого режима: путь на домене VPS → тот же путь на прямом входе ПК."""
    relay_url, direct = (config.WEBAPP_RELAY_URL or "").rstrip("/"), config.WEBAPP_DIRECT_URL
    if relay_url and direct and url.startswith(relay_url + "/"):
        return direct + url[len(relay_url):]
    return url


def status() -> dict:
    """Для админки и пульта: откуда бот берёт апдейты и как дела с приёмной."""
    if config.TG_SOURCE != "relay":
        return {"source": config.TG_SOURCE, "mode": "poll"}
    link = _current
    if link is None:
        return {"source": "relay", "mode": "starting", "url": config.RELAY_URL}
    return link.status()


async def run(bot, dp) -> None:
    """Вместо dp.start_polling (run.py при TG_SOURCE=relay). Возвращается только отменой."""
    await RelayLink(bot, dp).run()


async def guard_poll_mode(*, first: float = GUARD_FIRST, every: float = GUARD_EVERY) -> None:
    """Сторож poll-режима (run.serve при TG_SOURCE=poll и заданном RELAY_URL).

    Бот опрашивает Telegram сам, а приёмная настроена — так бывает после отката. Если откат
    делали, пока приёмная была недоступна, `control off` до неё не дошёл, и, когда VPS вернётся,
    Telegram будут опрашивать двое: 409 у обоих, а апдейты, которые успела забрать приёмная,
    ПК не увидит никогда (для неё ПК офлайн — клиенту «мастерская офлайн», оплата застрянет в её
    журнале), и ни пульт, ни лог бота этого не покажут. Поэтому бот сам раз в GUARD_EVERY
    спрашивает /_r/health и выключает включённый опрос. Покупатель без приёмной (RELAY_URL пуст)
    сюда не попадает — у него всё как было; нет ключа — сторожить нечем, молча выходим."""
    if not config.RELAY_URL.lower().startswith(("https://", "http://")):
        return
    try:
        link = RelayLink(None, None, fallback=False)
    except RelayConfigError as exc:
        log.info("сторож приёмной в режиме poll не запущен: %s", exc)
        return
    link._session = aiohttp.ClientSession(trust_env=False, connector=aiohttp.TCPConnector(limit=2))
    try:
        await asyncio.sleep(first)
        while True:
            try:
                await link.guard_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — сторож вторичен и не должен ронять бота
                log.warning("сторож приёмной: сбой проверки", exc_info=True)
            await asyncio.sleep(every)
    finally:
        await link._session.close()


# ── клиент ───────────────────────────────────────────────────────────────────

class RelayLink:
    def __init__(self, bot, dp, *, url: str | None = None, key: str | None = None,
                 wait: float = POLL_WAIT, fallback: bool | None = None,
                 fallback_after: float | None = None, probe_every: float = PROBE_EVERY,
                 direct_wait: float = POLL_WAIT, snapshot_every: float = SNAPSHOT_EVERY,
                 backoff: tuple[float, float] = BACKOFF, proxy=env_proxy,
                 replicate: bool | None = None) -> None:
        self.bot = bot
        self.dp = dp
        self.url = (config.RELAY_URL if url is None else url).rstrip("/")
        self.key = load_key(config.RELAY_KEY_FILE) if key is None else check_key(key)
        self.wait = float(wait)
        self.fallback = config.TG_FALLBACK if fallback is None else bool(fallback)
        self.fallback_after = config.TG_FALLBACK_AFTER if fallback_after is None else float(fallback_after)
        self.probe_every = float(probe_every)
        self.direct_wait = float(direct_wait)
        self.snapshot_every = float(snapshot_every)
        #: Слать ли копию базы и пакет каталога (контракт §7; тесты клиента их выключают).
        self.replica_on = config.RELAY_REPLICA if replicate is None else bool(replicate)
        self.backoff_min, self.backoff_max = backoff
        self._proxy_for = proxy or (lambda _url: None)

        self.mode = "starting"
        self.mode_since = time.time()
        self._session: aiohttp.ClientSession | None = None
        self._allowed: list[str] = []
        self._kw: dict = {}
        self._sem = asyncio.Semaphore(HANDLER_LIMIT)
        self._ev_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._inflight: set[int] = set()
        self._ev_inflight: set[str] = set()
        # Подтверждения копятся здесь и уезжают со следующим опросом; словарь — упорядоченное
        # множество (порядок не важен приёмной, но удобен глазам в отладке).
        self._acks: dict[int, None] = {}
        self._ack_events: dict[str, None] = {}
        self._online = asyncio.Event()
        self._backoff = 0.0
        self._fail_since: float | None = None
        self._last_ok_at: float | None = None
        self._last_error: str | None = None
        self._last_error_at: float | None = None
        self._relay_info: dict = {}
        self._relay_bad_since: float | None = None
        self._prefer_proxy_until = 0.0
        self._via_proxy = False
        self._direct_offset: int | None = None
        self._probe_error: str | None = None
        self._snap_hash: str | None = None
        self._snap_sent_at = 0.0
        self._snap_error: str | None = None
        # Копия базы и пакет каталога (контракт §7).
        self._replica = replica.Replicator(config.DB_PATH, config.DATA_DIR / "replica")
        self._bundle_hash: str | None = None
        self._bundle_sent_at = 0.0
        self._bundle_error: str | None = None
        self._warmed: set[str] = set()
        self._said: dict[str, float] = {}
        self.counters = {"updates": 0, "duplicates": 0, "events": 0, "fallbacks": 0}
        vi = version.info()
        self._rev = str(vi.get("sha") or "")
        self._channel = str(vi.get("channel") or "")

    # ── жизнь ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        global _current
        _current = self
        self._session = aiohttp.ClientSession(
            trust_env=False,   # прокси окружения — только явной запасной попыткой (см. _call)
            connector=aiohttp.TCPConnector(limit=8, keepalive_timeout=KEEPALIVE))
        background: list[asyncio.Task] = []
        try:
            self._allowed = list(self.dp.resolve_used_update_types())
            # Тот же контекст хендлеров и те же события старта/остановки диспетчера, что даёт
            # start_polling: dispatcher, bots и workflow_data (без bot — он идёт отдельно).
            self._kw = {"dispatcher": self.dp, "bots": (self.bot,), **self.dp.workflow_data}
            self._kw.pop("bot", None)
            await self.dp.emit_startup(bot=self.bot, **self._kw)
            self._set_mode("relay")
            if self.url.lower().startswith("http://"):
                log.warning("RELAY_URL без https (%s): снимок с данными клиентов пойдёт открытым текстом", self.url)
            log.info("Бот слушает Telegram через приёмную %s; прямой опрос — %s", self.url,
                     f"если она молчит дольше {self.fallback_after:.0f} с" if self.fallback
                     else "выключен (TG_FALLBACK=0)")
            background = [asyncio.create_task(self._snapshot_loop(), name="relaylink-snapshot"),
                          asyncio.create_task(self._prune_loop(), name="relaylink-prune")]
            if self.replica_on:
                background += [
                    asyncio.create_task(self._periodic("replica", REPLICA_EVERY, self.replica_tick),
                                        name="relaylink-replica"),
                    asyncio.create_task(self._periodic("bundle", BUNDLE_EVERY, self.bundle_tick),
                                        name="relaylink-bundle")]
            while True:
                try:
                    if self.mode == "direct":
                        await self._direct_loop()
                    else:
                        await self._relay_step()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — сбой цикла не должен оглушить бота до рестарта
                    # aiogram в polling тоже не роняет опрос ни при каком сбое: здесь — наш
                    # собственный дефект, и лучше живой бот с трассировкой в логе, чем мёртвый.
                    self._log_every("loop", 60, logging.ERROR, "цикл приёмной: сбой", exc_info=True)
                    await asyncio.sleep(self.backoff_max)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await self._drain()
            await self._session.close()
            try:
                await self.dp.emit_shutdown(bot=self.bot, **self._kw)
            except Exception:  # noqa: BLE001 — остановка не должна падать из-за хвостов
                log.debug("emit_shutdown failed", exc_info=True)
            self._set_mode("stopped")
            if _current is self:
                _current = None

    async def _drain(self) -> None:
        """Остановка: хендлерам — несколько секунд дописать ответ, потом снимаем. Неподтверждённое
        приёмная отдаст снова, а уже начатое отсечёт tg_seen."""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _done, still = await asyncio.wait(pending, timeout=DRAIN_SEC)
            for task in still:
                task.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.mode_since = time.time()

    def menu_changed(self) -> None:
        if self.mode == "direct":
            self._spawn(self._direct_menu())

    # ── опрос приёмной ────────────────────────────────────────────────────

    def _hello(self) -> dict:
        hello = {"rev": self._rev, "channel": self._channel, "version": CLIENT_VERSION,
                 "allowed_updates": self._allowed, "mode": "relay"}
        if _menu_url is not None:
            hello["menu_url"] = _menu_url
        return hello

    def _room(self) -> int:
        return max(1, min(POLL_MAX, BACKLOG_LIMIT - len(self._inflight)))

    async def _relay_step(self) -> None:
        acks = list(self._acks)[:ACK_CHUNK]
        ack_events = list(self._ack_events)[:ACK_CHUNK]
        body = {"hello": self._hello(), "ack": acks, "ack_events": ack_events,
                "wait": self.wait, "max": self._room()}
        started = time.monotonic()
        try:
            data = await self._call("POST", "/_r/poll", body, read_timeout=self.wait + READ_EXTRA)
        except RelayError as exc:
            if exc.kind == "bad":
                # 400 — сам запрос кривой: с теми же подтверждениями он будет кривым вечно.
                # Бросаем их: приёмная передоставит строки через 600 с, и tg_seen подтвердит их снова.
                self._forget_acks(acks, ack_events)
            await self._on_relay_fail(exc)
            return
        self._forget_acks(acks, ack_events)
        self._on_relay_ok(data)
        updates = [u for u in data.get("updates") or []
                   if isinstance(u, dict) and type(u.get("update_id")) is int and isinstance(u.get("raw"), dict)]
        events = [e for e in data.get("events") or [] if isinstance(e, dict)]
        for item in updates:
            self._submit_raw(item["update_id"], item["raw"])
        for ev in events:
            self._submit_event(ev)
        gap = min(MIN_POLL_GAP, self.wait / 2)
        if not updates and not events and time.monotonic() - started < gap:
            await asyncio.sleep(gap)

    def _forget_acks(self, acks: list[int], ack_events: list[str]) -> None:
        for uid in acks:
            self._acks.pop(uid, None)
        for eid in ack_events:
            self._ack_events.pop(eid, None)

    def _on_relay_ok(self, data: dict) -> None:
        now = time.time()
        if self._fail_since is not None:
            log.info("Приёмная снова на связи (не отвечала %d с)", now - self._fail_since)
        self._fail_since = None
        self._backoff = 0.0
        self._last_ok_at = now
        self._online.set()
        info = data.get("relay")
        if isinstance(info, dict):
            self._relay_info = info
        polling = self._relay_info.get("polling")
        state = polling.get("state") if isinstance(polling, dict) else None
        if state == "ok":
            if self._relay_bad_since is not None:
                log.info("Приёмная снова опрашивает Telegram")
            self._relay_bad_since = None
        elif self._relay_bad_since is None:
            self._relay_bad_since = now
            log.warning("Приёмная на связи, но Telegram не опрашивает (%s) — пока она не включит опрос "
                        "(/_r/control), бот ничего не услышит", state)
        elif now - self._relay_bad_since >= RELAY_STALL_ALERT:
            self._spawn(self._alert(
                f"Приёмная на VPS доступна, но не опрашивает Telegram уже {int((now - self._relay_bad_since) // 60)} мин "
                f"(состояние: {state}). Клиенты сейчас не получают ответов — проверьте "
                "tools/edge/edge.ps1 -Action status.", key="relaylink:relaypoll"))

    async def _on_relay_fail(self, exc: RelayError) -> None:
        now = time.time()
        first = self._fail_since is None
        if first:
            self._fail_since = now
        self._last_error = str(exc)
        self._last_error_at = now
        down = now - self._fail_since
        if first:
            log.warning("Приёмная недоступна: %s", exc)
        else:
            self._log_every("down", 300, logging.WARNING, "Приёмная недоступна уже %d с: %s", int(down), exc)
        if exc.kind in ("auth", "proto"):
            # Сама это не пройдёт: ключ/часы/версия. Говорим сразу, не дожидаясь прямого режима.
            await self._alert(f"Приёмная на VPS отвергает запросы бота: {exc}", key=f"relaylink:{exc.kind}",
                              throttle=3600)
        if down >= self.fallback_after:
            if self.fallback:
                await self._enter_direct(exc, down)
                return
            await self._alert(f"Приёмная на VPS не отвечает {int(down // 60)} мин ({exc}). Прямой опрос "
                              "выключен (TG_FALLBACK=0) — бот сейчас не получает сообщений.", key="relaylink:down")
        self._backoff = min(max(self._backoff * 2, self.backoff_min), self.backoff_max)
        pause = self._backoff
        if self.fallback:
            pause = min(pause, self.fallback_after - down)   # к прямому режиму — без опоздания
        await asyncio.sleep(max(0.0, pause))

    # ── апдейты и события ────────────────────────────────────────────────

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _submit_raw(self, update_id: int, raw: dict) -> None:
        if update_id in self._inflight:
            return          # тот же апдейт уже у нас в работе — подтвердит та задача
        self._inflight.add(update_id)
        self._spawn(self._handle(update_id, lambda: self._feed_raw(update_id, raw), ack=True,
                                 gated=_needs_slot(raw)))

    def _submit_update(self, update) -> None:
        """Прямой режим: апдейт уже разобран aiogram (bot.get_updates)."""
        if update.update_id in self._inflight:
            return
        self._inflight.add(update.update_id)
        self._spawn(self._handle(update.update_id, lambda: self._feed_update(update), ack=False,
                                 gated=_update_needs_slot(update)))

    async def _handle(self, update_id: int, feed, *, ack: bool, gated: bool = True) -> None:
        """Отметка tg_seen — уже ВНУТРИ слота, перед самой обработкой: апдейт, ждавший слота,
        при остановке ПК не помечается увиденным и после рестарта не потеряется. gated=False —
        без слота (оплата, кнопки, команды: см. _needs_slot)."""
        try:
            async with self._sem if gated else contextlib.nullcontext():
                try:
                    fresh = await db.tg_seen_mark(update_id)
                except Exception:  # noqa: BLE001 — без отметки не обрабатываем и не подтверждаем
                    self._log_every("seen", 60, logging.ERROR, "апдейт %s: отметка tg_seen не записалась — "
                                    "не подтверждаю, приёмная передоставит", update_id, exc_info=True)
                    return
                if fresh:
                    self.counters["updates"] += 1
                    await feed()
                else:
                    self.counters["duplicates"] += 1
                    log.info("апдейт %s уже обрабатывался — только подтверждаю", update_id)
                if ack:
                    # И после исключения хендлера: aiogram его уже записал, повтор того же
                    # апдейта не поможет, а висеть в журнале приёмной ему незачем.
                    self._acks[update_id] = None
        finally:
            self._inflight.discard(update_id)

    async def _feed_raw(self, update_id: int, raw: dict) -> None:
        try:
            response = await self.dp.feed_raw_update(self.bot, raw, **self._kw)
        except Exception as exc:  # noqa: BLE001 — трассировку хендлера aiogram уже записал
            log.warning("апдейт %s не обработан (%s: %s) — подтверждаю, повтор не поможет",
                        update_id, type(exc).__name__, exc)
            return
        await self._answer(response)

    async def _feed_update(self, update) -> None:
        try:
            response = await self.dp.feed_update(self.bot, update, **self._kw)
        except Exception:  # noqa: BLE001 — как aiogram в polling: в лог и дальше
            log.exception("апдейт %s не обработан", update.update_id)
            return
        await self._answer(response)

    async def _answer(self, response) -> None:
        # Хендлер может вернуть метод вместо вызова — polling aiogram его исполняет, и мы тоже.
        if isinstance(response, TelegramMethod):
            await self.dp.silent_call_request(bot=self.bot, result=response)

    def _submit_event(self, ev: dict) -> None:
        eid = ev.get("id")
        if not isinstance(eid, str) or not _EVENT_ID.fullmatch(eid):
            self._log_every("evid", 600, logging.WARNING, "приёмная прислала событие с кривым id %r — пропускаю", eid)
            return
        if eid in self._ev_inflight:
            return
        self._ev_inflight.add(eid)
        self._spawn(self._handle_event(eid, ev.get("kind"), ev.get("payload")))

    async def _handle_event(self, eid: str, kind, payload) -> None:
        try:
            async with self._ev_lock:
                try:
                    if kind != "deferred_order":
                        raise ValueError(f"неизвестный вид события {kind!r} — эта версия бота его не знает")
                    rid = await deferred.import_relay_order(eid, payload)
                except ValueError as exc:
                    # Битое по схеме: повтор не поможет. Подтверждаем, чтобы приёмная не крутила его
                    # вечно, но мастеру — громко: за событием стоит заявка клиента.
                    log.error("событие приёмной %s (%s) не принято: %s — подтверждаю", eid, kind, exc)
                    await self._alert(f"Приёмная прислала заявку, которую бот не смог принять ({kind}, "
                                      f"{eid[:8]}): {exc}. Заказ не заведён — загляните в журнал приёмной.",
                                      key="relaylink:badevent", throttle=600)
                    self._ack_events[eid] = None
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — БД, сеть: без ack, приёмная передоставит
                    log.warning("событие приёмной %s (%s): %s — не подтверждаю, приёмная передоставит",
                                eid, kind, exc, exc_info=True)
                    return
                self.counters["events"] += 1
                self._ack_events[eid] = None
                if rid:
                    log.info("событие приёмной %s (%s) принято: заказ #%s", eid, kind, rid)
                else:
                    log.info("событие приёмной %s (%s) уже было принято раньше — только подтверждаю", eid, kind)
        finally:
            self._ev_inflight.discard(eid)

    # ── аварийный прямой режим ────────────────────────────────────────────

    async def _enter_direct(self, exc: RelayError, down: float) -> None:
        self._set_mode("direct")
        self.counters["fallbacks"] += 1
        self._direct_offset = None
        web = _direct_web(True)
        log.warning("Приёмная молчит %d с (%s) — перехожу на прямой опрос Telegram%s", down, exc,
                    f"; мини-апка — на прямом адресе {config.WEBAPP_PUBLIC_URL}" if web else "")
        await self._alert(f"Приёмная на VPS не отвечает {int(down)} с ({exc}). Бот перешёл на прямой опрос "
                          "Telegram и работает как раньше; к приёмной вернётся сам, как только она ответит."
                          + (" Кнопки мини-апки пока ведут на прямой адрес ПК, а не на домен." if web else ""),
                          key="relaylink:direct")

    async def _back_to_relay(self, reason: str) -> None:
        self._set_mode("relay")
        self._fail_since = None
        self._backoff = 0.0
        if _direct_web(False):
            log.info("Мини-апка снова на домене: %s", config.WEBAPP_PUBLIC_URL)
        log.warning("Возвращаюсь к приёмной: %s", reason)
        await self._alert(f"Бот снова слушает Telegram через приёмную: {reason}.", key="relaylink:relay")

    async def _direct_loop(self) -> None:
        await self._direct_menu()
        offset: int | None = None
        streak = 0
        backoff = 0.0
        next_probe = time.monotonic() + self.probe_every
        while self.mode == "direct":
            if time.monotonic() >= next_probe:
                next_probe = time.monotonic() + self.probe_every
                streak = streak + 1 if await self._probe() else 0
                if streak >= PROBE_STREAK:
                    await self._leave_direct(offset)
                    return
            try:
                updates = await self.bot.get_updates(
                    offset=offset, timeout=int(self.direct_wait), allowed_updates=self._allowed,
                    request_timeout=int(self.direct_wait + READ_EXTRA))
            except TelegramConflictError:
                # 409: этим токеном опрашивает кто-то ещё — скорее всего, приёмная. Но «жива для
                # Telegram» не значит «доступна ПК»: истёк сертификат, лёг nginx, разные ключи,
                # провайдер режет порт. Вернуться вслепую значило оглохнуть ещё на fallback_after
                # (сброшенный отсчёт) — и так по кругу, ~60% времени, пока клиентам отвечает её
                # «мастерская офлайн». Поэтому сначала внеочередная проба.
                if await self._probe():
                    await self._leave_direct(offset, "Telegram ответил 409, и приёмная отвечает на проверку")
                    return
                streak = 0
                self._last_error = (f"Telegram ответил 409 — приёмная опрашивает его, но ПК её не достаёт "
                                    f"({self._probe_error})")
                self._last_error_at = time.time()
                self._log_every("direct-409", 300, logging.WARNING, "прямой опрос: %s — остаюсь на прямом опросе",
                                self._last_error)
                # Перехватить опрос обратно: на 409 приёмная сама встанет на паузу. Короткая пауза —
                # чтобы при чём-то неожиданном не долбить Telegram без остановки.
                await asyncio.sleep(self.backoff_min)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — сеть до Telegram: подождать и повторить
                backoff = min(max(backoff * 2, self.backoff_min), self.backoff_max)
                self._log_every("direct-net", 300, logging.WARNING, "прямой опрос Telegram: %s: %s",
                                type(exc).__name__, exc)
                await asyncio.sleep(max(0.0, min(backoff, next_probe - time.monotonic())))
                continue
            backoff = 0.0
            for upd in updates:
                offset = upd.update_id + 1
                self._submit_update(upd)
            self._direct_offset = offset

    async def _probe(self) -> bool:
        """Проверка «приёмная снова в строю»: подписанный /_r/health отвечает, опрос Telegram у
        неё включён И работает (POLLING_WORKS). Иначе, вернувшись, бот оглох бы: 409, который
        вернул бы его в прямой режим, от неопрашивающей приёмной не придёт, а в relay-режиме
        прямого опроса при доступной приёмной нет. Не опрашивает она, например, когда на диске
        VPS меньше резерва или в relay.env старый токен (state=error)."""
        try:
            data = await self._call("GET", "/_r/health", None, read_timeout=READ_EXTRA)
        except RelayError as exc:
            self._probe_error = str(exc)
            self._last_error, self._last_error_at = str(exc), time.time()
            log.info("проверка приёмной: %s", exc)
            return False
        polling = data.get("polling") if isinstance(data.get("polling"), dict) else {}
        if not polling.get("enabled"):
            self._probe_error = "опрос Telegram у неё выключен"
            log.info("приёмная отвечает, но опрос Telegram у неё выключен — остаюсь на прямом опросе")
            return False
        if polling.get("state") not in POLLING_WORKS:
            self._probe_error = f"Telegram она не опрашивает: {polling.get('state')} — {polling.get('last_error')}"
            log.info("приёмная отвечает, но Telegram не опрашивает (%s: %s) — остаюсь на прямом опросе",
                     polling.get("state"), polling.get("last_error"))
            return False
        self._probe_error = None
        return True

    async def _leave_direct(self, offset: int | None,
                            reason: str = "приёмная дважды подряд ответила на проверку") -> None:
        """Подтвердить Telegram всё, что бот уже взял (getUpdates с offset = последний + 1), иначе
        приёмная получит эти апдейты снова (их отсёк бы tg_seen, но зачем). Пришедшее в ответ —
        тоже в работу и тоже подтвердить, пока ответ не станет пустым."""
        for _ in range(10):
            if offset is None:
                break
            try:
                updates = await self.bot.get_updates(offset=offset, timeout=0, allowed_updates=self._allowed,
                                                     request_timeout=int(CONNECT_TIMEOUT + READ_EXTRA))
            except TelegramConflictError:
                break       # приёмная уже опрашивает сама
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — не беда: хвост отсечёт tg_seen
                log.info("не смог подтвердить offset %s: %s", offset, exc)
                break
            if not updates:
                break
            for upd in updates:
                offset = upd.update_id + 1
                self._submit_update(upd)
        self._direct_offset = offset
        await self._back_to_relay(reason)

    async def _direct_menu(self) -> None:
        """Прямой режим: ☰ ставит сам ПК — как в режиме poll (приёмная, может быть, лежит)."""
        if self.mode != "direct" or _menu_url is None:
            return
        try:
            if _menu_url:
                await self.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text=MENU_TEXT, web_app=WebAppInfo(url=_direct_menu_url(_menu_url))))
            else:
                await self.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        except Exception:  # noqa: BLE001
            log.warning("прямой режим: кнопка ☰ не встала", exc_info=True)

    # ── сторож poll-режима ───────────────────────────────────────────────

    async def guard_tick(self) -> bool:
        """Одна проверка сторожа (guard_poll_mode). True — опрос приёмной был включён, и бот его
        выключил. Приёмная недоступна — молчим: это не новость для poll-режима, а пульт её в
        poll не спрашивает."""
        try:
            data = await self._call("GET", "/_r/health", None, read_timeout=READ_EXTRA)
        except RelayError as exc:
            self._log_every("guard", 3600, logging.INFO, "сторож приёмной: %s", exc)
            return False
        polling = data.get("polling") if isinstance(data.get("polling"), dict) else {}
        if not polling.get("enabled"):
            return False
        log.warning("Приёмная %s опрашивает Telegram, хотя бот слушает его сам (TG_SOURCE=poll) — "
                    "выключаю её опрос", self.url)
        try:
            await self._call("POST", "/_r/control", {"polling": "off"}, read_timeout=CONTROL_READ)
        except RelayError as exc:
            log.warning("сторож приёмной: опрос не выключился: %s", exc)
            await self._alert(f"Приёмная на VPS опрашивает Telegram, хотя бот слушает его сам (TG_SOURCE=poll), "
                              f"а выключить её опрос не вышло ({exc}). Два поллера — часть сообщений уходит "
                              "мимо бота. Выполните: tools/edge/edge.ps1 -Action control -Polling off",
                              key="relaylink:guard-fail")
            return False
        inbox = data.get("inbox") if isinstance(data.get("inbox"), dict) else {}
        left = int(inbox.get("new") or 0) + int(inbox.get("claimed") or 0)
        await self._alert("Приёмная на VPS опрашивала Telegram, хотя бот слушает его сам (TG_SOURCE=poll) — "
                          "так бывает, если откат делали, пока VPS был недоступен. Бот выключил её опрос. "
                          + (f"В её журнале осталось {left} апдейт(ов), которые бот не получит — загляните в "
                             "tools/edge/edge.ps1 -Action status." if left else ""),
                          key="relaylink:guard")
        return True

    # ── HTTP ─────────────────────────────────────────────────────────────

    async def _call(self, method: str, path: str, payload: dict | None, *, read_timeout: float) -> dict:
        """Подписанный запрос к приёмной. Сначала напрямую (trust_env=False), при ОТКАЗЕ
        соединения — та же попытка через прокси окружения. Помог прокси — им и ходим
        PROXY_STICKY секунд: иначе каждый опрос платил бы 5 с таймаута на мёртвом пути."""
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False,
                                                      separators=(",", ":")).encode("utf-8")
        return await self._send(method, path, body, "application/json" if payload is not None else None,
                                read_timeout=read_timeout)

    async def _send(self, method: str, path: str, body: bytes, content_type: str | None, *,
                    read_timeout: float) -> dict:
        """Подписанный запрос с готовым телом (JSON или двоичная выгрузка копии базы и пакета)."""
        proxy = self._proxy_for(self.url)
        routes: list[str | None] = [None]
        if proxy:
            routes = [proxy, None] if time.monotonic() < self._prefer_proxy_until else [None, proxy]
        timeout = aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT, sock_connect=CONNECT_TIMEOUT,
                                        sock_read=read_timeout)
        for i, via in enumerate(routes):
            headers = sign_headers(self.key, method, path, body)
            if content_type:
                headers["Content-Type"] = content_type
            try:
                async with self._session.request(method, self.url + path, data=body or None, headers=headers,
                                                 proxy=via, timeout=timeout) as resp:
                    status = resp.status
                    raw = await resp.read()
            except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
                if i + 1 < len(routes):
                    log.debug("приёмная %s: %s — пробую %s", "через прокси" if via else "напрямую",
                              type(exc).__name__, "напрямую" if via else "через прокси")
                    continue
                raise RelayError(f"нет соединения ({type(exc).__name__})") from None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise RelayError(f"обрыв ({type(exc).__name__})") from None
            if via:
                self._prefer_proxy_until = time.monotonic() + PROXY_STICKY
            self._via_proxy = bool(via)
            return self._parse(status, raw)
        raise RelayError("нет маршрута до приёмной")    # сюда не дойти: последний маршрут бросает

    @staticmethod
    def _parse(status: int, raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            data = None
        if status == 200 and isinstance(data, dict):
            return data
        if status == 404:
            raise RelayError("приёмная ответила 404 — подпись не принята: ключи на VPS и ПК разные, "
                             "часы расходятся больше чем на 5 мин или nginx не пропускает /_r/", "auth")
        if status == 409:
            need = data.get("need") if isinstance(data, dict) else None
            raise RelayError(f"версии протокола разошлись: у бота {PROTO}, приёмной нужна {need} — "
                             "обновите отстающую сторону", "proto")
        if status in (400, 413):
            err = data.get("error") if isinstance(data, dict) else ""
            raise RelayError(f"приёмная не приняла запрос ({status}: {err})", "bad")
        if status == 200:
            raise RelayError("приёмная ответила не JSON", "http")
        raise RelayError(f"приёмная ответила {status}", "http")

    # ── фон: снимок и уборка ─────────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        while True:
            await self._online.wait()
            if self.mode == "relay" and self._fail_since is None:
                try:
                    await self.snapshot_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — снимок вторичен, цикл живёт
                    self._snap_error = f"{type(exc).__name__}: {exc}"
                    self._log_every("snap", 600, logging.WARNING, "снимок для приёмной не ушёл: %s", self._snap_error)
            await asyncio.sleep(self.snapshot_every)

    async def snapshot_tick(self, now: float | None = None) -> bool:
        """Собрать снимок и отправить, если изменился (или прошло SNAPSHOT_FORCE). True — ушёл."""
        now = time.time() if now is None else now
        snap = await build_snapshot(now)
        digest = snapshot_digest(snap)
        if digest == self._snap_hash and now - self._snap_sent_at < SNAPSHOT_FORCE:
            return False
        await self._call("POST", "/_r/snapshot", snap, read_timeout=READ_EXTRA)
        self._snap_hash, self._snap_sent_at, self._snap_error = digest, now, None
        return True

    # ── фон: копия базы и пакет каталога (контракт §7) ───────────────────

    async def _periodic(self, name: str, every: float, tick) -> None:
        """Общий цикл фоновых выгрузок: только когда бот ходит в приёмную и она отвечает."""
        while True:
            await self._online.wait()
            if self.mode == "relay" and self._fail_since is None:
                try:
                    await tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — выгрузка вторична, цикл живёт
                    err = f"{type(exc).__name__}: {exc}"
                    if name == "replica":
                        self._replica.error = err
                    else:
                        self._bundle_error = err
                    self._log_every(name, 600, logging.WARNING, "%s для приёмной не ушёл: %s",
                                    "копия базы" if name == "replica" else "пакет каталога", err)
            await asyncio.sleep(every)

    async def replica_tick(self, now: float | None = None) -> bool:
        """Снимок базы → приёмной (страницами или целиком). True — ушёл. Не та база у приёмной —
        сразу же целиком, не дожидаясь следующей минуты."""
        for _ in range(2):
            prepared = await asyncio.to_thread(self._replica.prepare)
            if prepared is None:
                return False
            body, header, data = prepared
            res = await self._send("POST", "/_r/replica", body, "application/octet-stream",
                                   read_timeout=UPLOAD_READ)
            if res.get("ok"):
                self._replica.accepted(data, header, len(body), time.time() if now is None else now)
                return True
            self._replica.reset()
            self._replica.error = str(res.get("error") or "приёмная попросила копию целиком")
            if header["mode"] == "full":
                break          # и целиком не приняла — ждём следующей минуты, без кругов
        return False

    async def bundle_tick(self, now: float | None = None) -> bool:
        """Пакет каталога → приёмной, если изменился (или прошёл BUNDLE_FORCE). True — ушёл."""
        now = time.time() if now is None else now
        bundle = await relaybundle.build_bundle(now)
        digest = await asyncio.to_thread(relaybundle.digest, bundle)
        if digest == self._bundle_hash and now - self._bundle_sent_at < BUNDLE_FORCE:
            return False
        body = await asyncio.to_thread(relaybundle.pack, bundle)
        await self._send("POST", "/_r/bundle", body, "application/octet-stream", read_timeout=UPLOAD_READ)
        self._bundle_hash, self._bundle_sent_at, self._bundle_error = digest, now, None
        self._spawn(self._warm(relaybundle.warm_urls(bundle)))
        return True

    async def _warm(self, urls: list[str]) -> int:
        """Прогнать картинки через сайт мини-аппа на VPS, чтобы они легли в кэш nginx и были
        видны без ПК. Только когда кнопки уже ведут на VPS (webcutover сделан). → сколько
        картинок прогрето."""
        base = (config.WEBAPP_PUBLIC_URL or "").rstrip("/")
        direct = (config.WEBAPP_DIRECT_URL or "").rstrip("/")
        if not base or not direct or base == direct or self._session is None:
            return 0
        todo = [u for u in urls if u and u not in self._warmed]
        sem = asyncio.Semaphore(WARM_PARALLEL)
        timeout = aiohttp.ClientTimeout(total=60, connect=CONNECT_TIMEOUT)
        done = 0

        async def one(url: str) -> None:
            nonlocal done
            async with sem:
                try:
                    async with self._session.get(base + url, timeout=timeout,
                                                 headers={"Accept": "image/webp,image/*,*/*;q=0.8"}) as resp:
                        await resp.read()
                        if resp.status == 200:
                            self._warmed.add(url)
                            done += 1
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass

        await asyncio.gather(*(one(u) for u in todo))
        if todo:
            log.info("кэш картинок на VPS: прогрето %d из %d", done, len(todo))
        return done

    async def _prune_loop(self) -> None:
        await asyncio.sleep(PRUNE_FIRST)
        while True:
            try:
                n = await db.prune_tg_seen()
                if n:
                    log.info("tg_seen: забыто %d давних апдейтов", n)
            except Exception:  # noqa: BLE001
                log.warning("tg_seen: уборка не удалась", exc_info=True)
            await asyncio.sleep(PRUNE_EVERY)

    # ── служебное ────────────────────────────────────────────────────────

    async def _alert(self, text: str, *, key: str, throttle: int = ALERT_THROTTLE) -> None:
        try:
            await notify.alert(text, key=key, throttle_sec=throttle)
        except Exception:  # noqa: BLE001
            log.debug("relaylink alert failed", exc_info=True)

    def _log_every(self, key: str, every: float, level: int, msg: str, *args, **kw) -> None:
        now = time.monotonic()
        if now - self._said.get(key, -1e9) < every:
            return
        self._said[key] = now
        log.log(level, msg, *args, **kw)

    def status(self) -> dict:
        now = time.time()
        online = self.mode == "relay" and self._fail_since is None and self._last_ok_at is not None \
            and now - self._last_ok_at < self.wait + READ_EXTRA + 30
        polling = self._relay_info.get("polling") if isinstance(self._relay_info.get("polling"), dict) else {}
        return {
            "source": "relay",
            "mode": self.mode,                 # relay | direct | starting | stopped
            "mode_since": self.mode_since,
            "url": self.url,
            "online": online,
            "last_ok_at": self._last_ok_at,
            "fail_since": self._fail_since,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "via_proxy": self._via_proxy,
            "relay_polling": polling.get("state"),
            "fallback": {"enabled": self.fallback, "after": self.fallback_after},
            "direct_offset": self._direct_offset if self.mode == "direct" else None,
            "inflight": len(self._inflight),
            "acks_pending": len(self._acks),
            "events_pending": len(self._ack_events),
            "counters": dict(self.counters),
            "relay": self._relay_info,
            "snapshot": {"sent_at": self._snap_sent_at or None, "error": self._snap_error},
            "replica": self._replica.status(),
            "bundle": {"sent_at": self._bundle_sent_at or None, "error": self._bundle_error,
                       "warmed": len(self._warmed)},
        }
