"""Протокол Bambu Lab (X1/P1/A1): телеметрия по MQTT/TLS и файлы по FTPS.

Три слоя, все написаны ДО приезда машины и покрыты тестами:

1. **Разбор отчёта** — как поля Bambu ложатся в форму, которую по всему проекту ждут экран,
   LCD, журнал и наблюдатель (`enrich_status`, `summarize`, `extras`).
2. **Подписчик** (`BambuSession`) — MQTT поверх TLS на :8883, логин `bblp`, пароль — код доступа
   из меню принтера. Темы `device/<serial>/report` и `device/<serial>/request`. Полный снимок
   приходит только на `pushall`, дальше ЧАСТИЧНЫЕ обновления, поэтому состояние КОПИТСЯ слиянием.
3. **Файлы** (`list_files`/`upload`/`delete_file`/`timelapses`/`fetch_timelapse`) — FTPS с
   implicit TLS на :990. Каталоги `/cache`, `/model`, `/timelapse`.

Порядок «сначала код, потом железо» взят из опыта Elegoo: там первый час у принтера ушёл на
отладку разбора вместо ЗАПИСИ обмена, и активную ветку `enrich_status` до сих пор нечем покрыть —
все захваты в `docs/captures/` сняты в простое.

**ЧЕСТНО ПРО ГОТОВНОСТЬ.** Ни один байт сетевой части не проверен на живом принтере: MQTT-слой
прогнан через симулятор (`app/bambusim.py`, настоящий TLS), файловый — на подменённом транспорте.
Поэтому драйвер объявляет способности ПО ФАКТУ: пока сессия не приняла ни одного отчёта,
`describe()` отдаёт пустой набор. Ответит машина — интерфейс оживёт сам.

**Чего здесь НЕТ и почему.** Истории заданий: в локальном режиме Bambu её не отдаёт вовсе, она
живёт в облаке, а собирать её из списка файлов нельзя — свип таймлапсов сверяет ИМЕНА заданий, и
чужой источник привязал бы к печати чужое видео. Камеры: у Bambu это не MJPEG, а свой поток
chamber image по TCP, ретранслятор проекта такого не умеет. Слотов AMS: привязку цвета к лотку
нельзя выдумывать вслепую — она уезжает в заказ клиента.

**Про зависимость.** `paho-mqtt` не понадобился: пакеты MQTT 3.1.1 собираются примитивами из
`app/elegoo.py` (чистый протокол, без елегувских особенностей), TLS и FTPS даёт стандартная
библиотека. Если на живой машине всплывут keepalive/QoS, которых нет в самописном клиенте, —
вот тогда её и стоит завести.
"""
from __future__ import annotations

import calendar
import ftplib
import itertools
import json
import logging
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import elegoo

log = logging.getLogger(__name__)

MQTT_PORT = 8883
MQTT_USER = "bblp"
FTPS_PORT = 990
CAMERA_PORT = 6000


@dataclass(frozen=True)
class Endpoint:
    """Один принтер Bambu: адрес, код доступа из меню принтера и серийник (он в теме MQTT).

    frozen по той же причине, что у Elegoo: адрес попадает в сокет, а серийник — в тему, и
    разъезд между ними означает «команда ушла на правильный принтер в тему чужой машины». Она не
    выполнится, а вызов отчитается об успехе — такое не ловится ничем, кроме жалобы."""

    ip: str = ""
    code: str = ""
    serial: str = ""


def topic_report(serial: str) -> str:
    return f"device/{(serial or '').strip()}/report"


def topic_request(serial: str) -> str:
    return f"device/{(serial or '').strip()}/request"


# Стадии печати Bambu (`stg_cur`), которые означают «машина занята, но не кладёт пластик»:
# калибровка стола, прогрев, смена филамента. Для нас это ЗАНЯТОСТЬ — ровно как пауза и прогрев
# у Elegoo (см. PrinterDriver.job_state): гард «по сети не трогаем» и детектор финиша спрашивают
# «машина занята заданием», а не «сопло движется прямо сейчас».
_BUSY_STAGES = {
    1: "auto_bed_leveling",
    2: "heatbed_preheating",
    3: "sweeping_xy_mech_mode",
    4: "changing_filament",
    5: "m400_pause",
    6: "paused_filament_runout",
    7: "heating_hotend",
    8: "calibrating_extrusion",
    9: "scanning_bed_surface",
    10: "inspecting_first_layer",
    11: "identifying_build_plate_type",
    12: "calibrating_micro_lidar",
    13: "homing_toolhead",
    14: "cleaning_nozzle_tip",
    15: "checking_extruder_temperature",
}

# gcode_state прошивки → слово, которое понимает автоматика проекта. «PAUSE» и «PREPARE» —
# занятость (BUSY_WORDS в printerdrv/base), «FAILED» — финиш: задание встало.
_GCODE_STATE = {
    "IDLE": "standby",
    "PREPARE": "heating",
    "RUNNING": "printing",
    "PAUSE": "paused",
    "FINISH": "complete",
    "FAILED": "error",
    "SLICING": "heating",
}


def _num(v):
    """Числа Bambu приходят и строками («220»), и числами. None — поля нет или оно не число."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def report_payload(msg: dict) -> dict:
    """Полезная часть отчёта: `{"print": {...}}` → `{...}`. Не наш формат — пустой словарь.

    Принтер шлёт в ту же тему и другие конверты (`info`, `system`, `camera`), и разбирать их как
    телеметрию значило бы затирать состояние пустотой."""
    if not isinstance(msg, dict):
        return {}
    p = msg.get("print")
    return p if isinstance(p, dict) else {}


def enrich_status(state: dict) -> dict:
    """Отчёт Bambu → OVERLAY в форме проекта (контракт `PrinterDriver.enrich`).

    Возвращается ДОБАВКА для слияния поверх сырого состояния, а не полная картина: так устроен
    контракт, и «читатель возврата» вместо мержа — та самая ошибка, из-за которой у Elegoo три
    копии выражения разъехались с источником данных (разбор 11.08).

    Поля отчёта (опубликованный формат X1/P1):
      gcode_state, mc_percent, mc_remaining_time (минуты!), layer_num, total_layer_num,
      nozzle_temper/nozzle_target_temper, bed_temper/bed_target_temper, chamber_temper,
      subtask_name / gcode_file, stg_cur, cooling_fan_speed (0..15 «ступеней», не проценты).

    Отдельно про время: `mc_remaining_time` у Bambu в МИНУТАХ, а весь остальной проект считает
    остаток в СЕКУНДАХ (`print_status.remaining_time_sec`). Перепутать эти двое значило бы
    показать клиенту срок в шестьдесят раз меньше — поэтому пересчёт здесь, а не у потребителя."""
    p = state if isinstance(state, dict) else {}
    if not p:
        return {}
    out: dict = {}

    raw_state = str(p.get("gcode_state") or "").strip().upper()
    stage = _num(p.get("stg_cur"))
    phase = _GCODE_STATE.get(raw_state, "")
    if not phase and stage is not None and int(stage) in _BUSY_STAGES:
        phase = "heating"                    # стадия подготовки без gcode_state — тоже занятость
    if phase:
        out["print_stats"] = {"state": phase}

    name = str(p.get("subtask_name") or p.get("gcode_file") or "").strip()
    if name:
        out.setdefault("print_stats", {})["filename"] = name

    pct = _num(p.get("mc_percent"))
    if pct is not None:
        out["virtual_sdcard"] = {"progress": max(0.0, min(1.0, pct / 100.0))}

    info: dict = {}
    layer = _num(p.get("layer_num"))
    total = _num(p.get("total_layer_num"))
    if layer is not None:
        info["current_layer"] = int(layer)
    if total is not None:
        info["total_layer"] = int(total)
    if info:
        out["info"] = info

    # Остаток: минуты прошивки → секунды проекта. Ключ тот же, что у Elegoo, чтобы экран, LCD и
    # свип не заводили себе третью ветку разбора.
    rem_min = _num(p.get("mc_remaining_time"))
    ps: dict = {}
    if rem_min is not None and rem_min >= 0:
        ps["remaining_time_sec"] = int(rem_min * 60)
    if name:
        ps["filename"] = name
    if info.get("current_layer") is not None:
        ps["current_layer"] = info["current_layer"]
    if stage is not None:
        ps["stage"] = _BUSY_STAGES.get(int(stage), "")
    if ps:
        out["print_status"] = ps

    nozzle, nozzle_t = _num(p.get("nozzle_temper")), _num(p.get("nozzle_target_temper"))
    if nozzle is not None or nozzle_t is not None:
        out["extruder"] = {"temperature": nozzle, "target": nozzle_t}
    bed, bed_t = _num(p.get("bed_temper")), _num(p.get("bed_target_temper"))
    if bed is not None or bed_t is not None:
        out["heater_bed"] = {"temperature": bed, "target": bed_t}
    cham = _num(p.get("chamber_temper"))
    if cham is not None:
        out["ztemperature_sensor"] = {"temperature": cham}

    # Обдув: у Bambu 0..15 ступеней, а не проценты. Отдаём долей 0..1 — так же, как прошивка
    # Centauri отдаёт пользовательские вентиляторы (см. elegoo._fan_pct).
    fan = _num(p.get("cooling_fan_speed"))
    if fan is not None:
        out["fans"] = {"fan": {"speed": max(0.0, min(1.0, fan / 15.0))}}
    return out


def summarize(state: dict) -> dict:
    """Температуры в той же форме, что у Elegoo (`summarize`) — для истории телеметрии."""
    o = enrich_status(state)
    ex, bed = o.get("extruder") or {}, o.get("heater_bed") or {}
    return {"extruder_temp": ex.get("temperature"), "extruder_target": ex.get("target"),
            "bed_temp": bed.get("temperature"), "bed_target": bed.get("target")}


def extras(state: dict) -> dict:
    """Дополнительные показания: камера-температура, обдув, слоты AMS.

    Слоты пока НЕ разбираются: форма `ams.ams[i].tray[j]` известна, но сопоставлять её с нашими
    слотами вслепую — значит выдумать привязку цвета к лотку и записать её в заказ. Это работа
    у железа (волна 7б), и лучше пусто, чем неверно."""
    o = enrich_status(state)
    out: dict = {}
    cham = (o.get("ztemperature_sensor") or {}).get("temperature")
    if cham is not None:
        out["chamber_temp"] = cham
    fan = ((o.get("fans") or {}).get("fan") or {}).get("speed")
    if fan is not None:
        out["fans"] = {"model": round(fan * 100)}
    return out


# ─── Сетевая часть (волна 7б): TLS-подписчик отчётов ─────────────────────────────────────
# Написана ДО приезда машины и покрыта тестами на скриптованном сокете. План цикла требует
# планировать 7б как «написали → переписали»: у железа сверяются поля, коды ошибок и хендшейк,
# а не пишется с нуля — так первый час у машины уходит на записи, а не на отладку.
#
# ЧЕСТНО ПРО ГОТОВНОСТЬ: ни один байт этого кода не проверен на живом принтере. Поэтому драйвер
# объявляет способности не по вере, а ПО ФАКТУ: пока сессия не приняла ни одного отчёта,
# способности пустые (см. BambuDriver.describe). Ответит машина — интерфейс оживёт сам.
#
# Пакеты MQTT собираются примитивами из app/elegoo.py: это чистый MQTT 3.1.1 без единой
# елегувской особенности, и второй экземпляр той же арифметики был бы просто копией.

CONNECT_TIMEOUT = 8.0
RECV_TIMEOUT = 2.0
PING_EVERY = 30.0
PUSHALL_EVERY = 300.0        # полный снимок раз в 5 минут: между ними идут только дельты


def _tls_context() -> ssl.SSLContext:
    """TLS для самоподписанного сертификата принтера.

    Проверка имени и цепочки отключена ОСОЗНАННО и с оговоркой: сертификат Bambu самоподписанный,
    выписан на внутреннее имя и меняется прошивкой — сверять его не с чем. Это локальная сеть
    мастерской, а код доступа всё равно уходит в шифрованный канал. Молча поставить CERT_NONE
    было бы хуже: следующий читатель счёл бы это недосмотром."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class BambuSession:
    """Живое соединение с ОДНИМ принтером Bambu: подписчик отчётов и отправка команд.

    Устроена как `elegoo.ElegooSession` и по тем же причинам: состояние в объекте, эндпоинт
    провайдером, реестр по нормализованному адресу. Отличий протокола три:

    * состояние КОПИТСЯ слиянием: полный снимок приходит только на `pushall`, дальше идут
      дельты, и замена целиком стирала бы половину показаний на каждом кадре;
    * у команд есть `sequence_id`, а отвечает принтер в ту же тему отчётов — отдельного канала
      ответов нет, подтверждение ловит общий цикл;
    * `seen_report` — признак «машина реально ответила». На нём висят способности драйвера:
      пока отчёта не было, обещать нечего."""

    def __init__(self, ep_provider=None) -> None:
        self._ep_provider = ep_provider or (lambda: Endpoint())
        self.live: dict = {"state": {}, "ts": 0.0, "serial": "", "connected": False}
        self.live_lock = threading.Lock()
        self.sock = None
        self.send_lock = threading.Lock()
        self.seen_report = False     # принят ли хоть один отчёт — источник честных способностей
        self._seq = itertools.count(1)
        self._started = False
        self._stop = threading.Event()

    def endpoint(self) -> Endpoint:
        return self._ep_provider()

    def rebind(self, ep: Endpoint) -> None:
        self._ep_provider = lambda: ep

    def _enabled(self) -> bool:
        """Есть ли куда идти. Серийник обязателен: он в ТЕМЕ, а не в теле запроса."""
        e = self.endpoint()
        return bool(e.ip and e.code and e.serial)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        threading.Thread(target=self._loop, daemon=True,
                         name=f"bambu-mqtt-sub-{self.endpoint().ip or 'none'}").start()
        log.info("Bambu MQTT-подписчик запущен (%s)", self.endpoint().ip)

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._started = False
        with self.send_lock:
            sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                log.debug("bambu session socket close failed", exc_info=True)

    def cached_status(self, max_age: float = 6.0) -> dict | None:
        """Снимок отчётов в форме проекта. Возвращаем КОПИЮ state, как у Elegoo.

        None, пока отчётов не было: словарь с `ok=True` и пустым состоянием наблюдатель прочитал
        бы как «на связи и простаивает», и сторож связи не поднял бы тревогу никогда."""
        with self.live_lock:
            ts, connected = self.live["ts"], self.live["connected"]
            fresh = ts and (time.time() - ts) <= max_age
            if (connected or fresh) and self.live["state"]:
                return {"ok": True, "serial": self.live["serial"], "online": True, "cached": True,
                        "connected": bool(connected),
                        "age": round(time.time() - ts, 1) if ts else None,
                        "state": json.loads(json.dumps(self.live["state"]))}
        return None

    def next_seq(self) -> str:
        return str(next(self._seq))

    def send(self, body: dict) -> dict:
        """Отправить команду в `device/<sn>/request`. Нет соединения — честный отказ.

        Подтверждения не ждём и не выдаём за него отсутствие ошибки: принтер отвечает в общую
        тему отчётов. Возвращаем `unconfirmed` — ровно так же, как Elegoo без привязки, и
        вызывающие уже умеют это показывать."""
        e = self.endpoint()
        with self.send_lock:
            sock = self.sock
        if sock is None:
            return {"ok": False, "error": "Нет соединения с принтером Bambu"}
        try:
            with self.send_lock:
                sock.sendall(elegoo._publish_pkt(topic_request(e.serial),
                                                 json.dumps(body).encode()))
        except OSError as exc:
            return {"ok": False, "error": f"Команда не ушла: {exc}"}
        return {"ok": True, "unconfirmed": True, "result": {}}

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._enabled():
                self._stop.wait(10.0)
                continue
            dst = self.endpoint()
            sock = None
            try:
                sock = self._connect(dst)
                with self.live_lock:
                    self.live["connected"] = True
                    self.live["serial"] = dst.serial
                with self.send_lock:
                    self.sock = sock
                backoff = 1.0
                self._pushall(sock, dst.serial)
                last_ping = last_push = time.time()
                while self._enabled() and not self._stop.is_set():
                    now = time.time()
                    if now - last_push > PUSHALL_EVERY:
                        self._pushall(sock, dst.serial)   # страховка от потерянных дельт
                        last_push = now
                    try:
                        ptype, data = elegoo._recv_pkt(sock)
                    except socket.timeout:
                        if now - last_ping > PING_EVERY:
                            with self.send_lock:
                                sock.sendall(b"\xc0\x00")
                            last_ping = now
                        continue
                    if ptype is None:
                        break
                    if (ptype & 0xF0) != 0x30:            # не PUBLISH
                        continue
                    _topic, obj = elegoo._parse_publish(ptype, data)
                    payload = report_payload(obj or {})
                    if not payload:
                        continue
                    with self.live_lock:
                        # именно СЛИЯНИЕ: полный снимок — только на pushall, дальше дельты
                        elegoo._deep_merge(self.live["state"], payload)
                        elegoo._deep_merge(self.live["state"], enrich_status(payload))
                        self.live["ts"] = time.time()
                    self.seen_report = True
            except Exception:  # noqa: BLE001
                log.debug("bambu subscriber reconnect", exc_info=True)
            finally:
                with self.live_lock:
                    self.live["connected"] = False
                with self.send_lock:
                    self.sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._stop.wait(backoff)
            backoff = min(backoff * 2.0, 30.0)

    def _connect(self, dst: Endpoint):
        raw = socket.create_connection((dst.ip, MQTT_PORT), timeout=CONNECT_TIMEOUT)
        sock = _tls_context().wrap_socket(raw, server_hostname=dst.ip)
        sock.settimeout(RECV_TIMEOUT)
        sock.sendall(elegoo._connect_pkt("newbor-bambu", MQTT_USER, dst.code))
        ptype, data = elegoo._recv_pkt(sock)
        if ptype != 0x20 or len(data) < 2 or data[1] != 0:
            raise OSError("CONNACK отклонён — проверьте код доступа принтера")
        sock.sendall(elegoo._subscribe_pkt(topic_report(dst.serial)))
        return sock

    def _pushall(self, sock, serial: str) -> None:
        """Попросить ПОЛНЫЙ снимок. Без него в кэше окажутся одни дельты — половина показаний
        не придёт вовсе, и экран покажет принтер полупустым."""
        try:
            body = json.dumps({"pushing": {"sequence_id": self.next_seq(),
                                           "command": "pushall"}}).encode()
            with self.send_lock:
                sock.sendall(elegoo._publish_pkt(topic_request(serial), body))
        except OSError:
            log.debug("bambu pushall failed", exc_info=True)


# ─── Реестр сессий: ключ — нормализованный адрес, как у Elegoo ───────────────────────────
_sessions: dict[str, BambuSession] = {}
_sessions_lock = threading.Lock()


def session_for(ep: Endpoint) -> BambuSession:
    """Сессия этого адреса. Второй подписчик к тому же брокеру пришёл бы с тем же client_id, а
    брокер обязан рвать предыдущую сессию — та же война реконнектов, что у Elegoo."""
    key = elegoo.norm_host(ep.ip)
    with _sessions_lock:
        ses = _sessions.get(key)
        if ses is None:
            ses = BambuSession()
            _sessions[key] = ses
        ses.rebind(ep)
        return ses


def sessions_sync(endpoints: list[Endpoint]) -> list[str]:
    """Поднять подписчиков под список карточек Bambu и погасить лишних."""
    want: dict[str, Endpoint] = {}
    for e in endpoints:
        if e.ip and e.code and e.serial:
            want[elegoo.norm_host(e.ip)] = e
    with _sessions_lock:
        gone = [_sessions.pop(k) for k in list(_sessions) if k not in want]
        live = []
        for key, e in want.items():
            ses = _sessions.get(key) or BambuSession()
            _sessions[key] = ses
            ses.rebind(e)
            live.append(ses)
    for ses in gone:
        ses.stop()
    for ses in live:
        ses.start()
    return sorted(want)


def sessions_stop_all() -> None:
    """Погасить всех подписчиков Bambu (остановка бота, уборка в тестах)."""
    with _sessions_lock:
        gone = list(_sessions.values())
        _sessions.clear()
    for ses in gone:
        ses.stop()


# ─── Файловая часть: FTPS с implicit TLS ─────────────────────────────────────────────────
# Файлы у Bambu живут не в протоколе, а на FTPS-сервере самого принтера: :990, implicit TLS,
# логин тот же `bblp` с кодом доступа. «Implicit» означает, что TLS поднимается СРАЗУ при
# подключении, до всякой команды — обычный `FTP_TLS` из стандартной библиотеки так не умеет
# (он делает explicit: сначала открытый канал, потом AUTH TLS). Отсюда подкласс ниже; сторонний
# пакет не нужен.
#
# Раскладка на принтере (разведана по документации LAN-режима):
#   /cache/      — задания, отправленные на печать (gcode/3mf)
#   /model/      — модели, положенные пользователем
#   /timelapse/  — mp4 таймлапсов, /timelapse/thumbnail/ — их превью
#
# ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Истории заданий у Bambu в LAN-режиме нет вовсе: она живёт в облаке.
# Выдумывать её из списка файлов нельзя — журнал печатей и свип таймлапсов сверяют ИМЕНА заданий,
# и «история», собранная из чужого источника, привязала бы к печати чужое видео. Поэтому
# `history()` у драйвера честно молчит, а таймлапс ищется по имени файла напрямую.

FTPS_USER = "bblp"
DIR_CACHE = "/cache"
DIR_MODEL = "/model"
DIR_TIMELAPSE = "/timelapse"
PRINTABLE_EXTS = (".gcode", ".3mf", ".gcode.3mf")


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS с implicit TLS: канал шифруется с первого байта, а не по команде AUTH.

    Стандартный `FTP_TLS` рассчитан на explicit (:21 → AUTH TLS). У Bambu сервер на :990 ждёт
    рукопожатия сразу, и explicit-клиент на нём просто виснет: сервер молчит, потому что ждёт
    ClientHello, а клиент ждёт приветствие. Подменяем сокет на обёрнутый в момент установки."""

    def __init__(self, *a, **kw):
        self._sock = None
        super().__init__(*a, **kw)

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value


def _ftp_connect(ep: Endpoint, timeout: float = 20.0) -> _ImplicitFTP_TLS:
    """Подключиться к FTPS принтера и войти. Бросает на любой сбой — вызывающий переводит в
    отказ с человеческим текстом.

    Сертификат самоподписанный, как и у MQTT: проверять его не с чем (см. _tls_context)."""
    ftp = _ImplicitFTP_TLS(context=_tls_context(), timeout=timeout)
    ftp.connect(host=ep.ip, port=FTPS_PORT, timeout=timeout)
    ftp.login(user=FTPS_USER, passwd=ep.code)
    ftp.prot_p()                 # шифруем и канал ДАННЫХ, иначе сервер откажет в передаче
    return ftp


def _entries(ftp: _ImplicitFTP_TLS, path: str) -> list[dict]:
    """Список файлов каталога: [{name, size, ts}]. Каталоги и точки пропускаем.

    Читаем через MLSD (машинный формат со `size`/`modify`), а при отказе — через NLST: у
    прошивок разных поколений набор команд отличается, и падать из-за этого нельзя."""
    out: list[dict] = []
    try:
        for name, facts in ftp.mlsd(path, facts=["type", "size", "modify"]):
            if facts.get("type") != "file" or name in (".", ".."):
                continue
            out.append({"name": name, "size": int(facts.get("size") or 0),
                        "ts": _mlsd_time(facts.get("modify"))})
        return out
    except (ftplib.all_errors, ValueError):
        log.debug("bambu: MLSD не поддержан, пробую NLST", exc_info=True)
    try:
        for raw in ftp.nlst(path):
            name = raw.rsplit("/", 1)[-1]
            if name and name not in (".", ".."):
                out.append({"name": name, "size": 0, "ts": 0})
    except ftplib.all_errors:
        log.debug("bambu: NLST не удался для %s", path, exc_info=True)
    return out


def _mlsd_time(value: str | None) -> int:
    """`modify` из MLSD (YYYYMMDDHHMMSS, UTC) → unix-время. Не разобралось — 0, а не «сейчас»:
    выдуманное свежее время поставило бы чужой файл первым в выдаче «последний таймлапс»."""
    s = (value or "").strip()
    if len(s) < 14 or not s[:14].isdigit():
        return 0
    try:
        return int(calendar.timegm(time.strptime(s[:14], "%Y%m%d%H%M%S")))
    except ValueError:
        return 0


def list_files(ep: Endpoint, timeout: float = 20.0) -> dict:
    """Печатаемые файлы принтера: /cache и /model. Форма ответа — как у Elegoo `list_files`.

    Граммов и времени печати здесь нет и взяться неоткуда: FTP отдаёт только имя, размер и дату,
    а «Filament Consumption» Bambu держит внутри 3mf. Ставить 0 честнее выдуманного числа —
    вызывающий (`file_grams`) трактует 0 как «неизвестно» и не пишет его в стоимость."""
    try:
        ftp = _ftp_connect(ep, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"FTPS принтера недоступен: {exc}"}
    try:
        seen: dict[str, dict] = {}
        for d in (DIR_CACHE, DIR_MODEL):
            for e in _entries(ftp, d):
                if not e["name"].lower().endswith(PRINTABLE_EXTS):
                    continue
                # одно имя в двух каталогах — берём то, что крупнее нуля и свежее
                old = seen.get(e["name"])
                if old is None or (e["ts"], e["size"]) > (old["ts"], old["size"]):
                    # ts кладём В ЗАПИСЬ, а не держим отдельно: сравнение читает его же, и
                    # «отдельный словарь дат» — лишний повод им разъехаться
                    seen[e["name"]] = {"name": e["name"], "size": e["size"], "dir": d,
                                       "ts": e["ts"], "print_time": 0, "layer": 0,
                                       "grams": 0, "colors": []}
        return {"ok": True, "files": sorted(seen.values(), key=lambda f: f["name"])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"не прочитать список файлов: {exc}"}
    finally:
        _quit(ftp)


def upload(ep: Endpoint, path, name: str = "", timeout: float = 300.0) -> dict:
    """Залить файл в /cache — туда, откуда принтер печатает задания."""
    src = Path(path)
    if not src.exists():
        return {"ok": False, "error": f"файл не найден: {src}"}
    dest = (name or src.name).strip()
    try:
        ftp = _ftp_connect(ep, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"FTPS принтера недоступен: {exc}"}
    try:
        with src.open("rb") as fh:
            ftp.storbinary(f"STOR {DIR_CACHE}/{dest}", fh)
        return {"ok": True, "name": dest}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"заливка не удалась: {exc}"}
    finally:
        _quit(ftp)


def delete_file(ep: Endpoint, name: str, timeout: float = 20.0) -> dict:
    """Удалить файл задания. Ищем в обоих каталогах: имя приходит из списка, а он их объединяет."""
    nm = (name or "").strip().rsplit("/", 1)[-1]
    if not nm:
        return {"ok": False, "error": "не указано имя файла"}
    try:
        ftp = _ftp_connect(ep, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"FTPS принтера недоступен: {exc}"}
    try:
        last = None
        for d in (DIR_CACHE, DIR_MODEL):
            try:
                ftp.delete(f"{d}/{nm}")
                return {"ok": True}
            except ftplib.all_errors as exc:
                last = exc
        return {"ok": False, "error": f"файл не удалён: {last}"}
    finally:
        _quit(ftp)


def timelapses(ep: Endpoint, timeout: float = 20.0) -> dict:
    """Список роликов из /timelapse, свежие первыми."""
    try:
        ftp = _ftp_connect(ep, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"FTPS принтера недоступен: {exc}"}
    try:
        vids = [e for e in _entries(ftp, DIR_TIMELAPSE)
                if e["name"].lower().endswith((".mp4", ".avi"))]
        vids.sort(key=lambda e: (e["ts"], e["name"]), reverse=True)
        return {"ok": True, "files": vids}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"не прочитать таймлапсы: {exc}"}
    finally:
        _quit(ftp)


def fetch_timelapse(ep: Endpoint, name: str, dest, timeout: float = 300.0) -> dict:
    """Скачать ролик в `dest`. Пишем во ВРЕМЕННЫЙ файл и переименовываем в конце.

    Иначе оборванная закачка оставит куцый .mp4, который и свип, и клиент примут за готовое
    видео: у Elegoo это уже ловилось — недокачанный файл «есть», и повторно его никто не берёт."""
    nm = (name or "").strip().rsplit("/", 1)[-1]
    dest = Path(dest)
    if not nm:
        return {"ok": False, "error": "не указано имя ролика"}
    try:
        ftp = _ftp_connect(ep, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"FTPS принтера недоступен: {exc}"}
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as fh:
            ftp.retrbinary(f"RETR {DIR_TIMELAPSE}/{nm}", fh.write)
        if tmp.stat().st_size <= 0:
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": "принтер отдал пустой файл"}
        tmp.replace(dest)
        return {"ok": True, "path": str(dest), "size": dest.stat().st_size}
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": f"скачивание не удалось: {exc}"}
    finally:
        _quit(ftp)


def _quit(ftp) -> None:
    """Закрыть соединение, не роняя вызывающего: FTPS-сервер принтера любит рвать связь сам."""
    try:
        ftp.quit()
    except Exception:  # noqa: BLE001
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass
