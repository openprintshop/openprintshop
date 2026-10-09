"""Интеграция с принтером Elegoo (Centauri Carbon) по его MQTT-протоколу (c5f8ec5f).

Принтер держит MQTT-брокер на :1883 с авторизацией (user=ELEGOO_MQTT_USER, pass=ELEGOO_ACCESS_CODE).
Топики (выяснено разведкой):
  elegoo/<SN>/api_status                         — телеметрия, JSON-RPC {"method":6000,"result":{...}}
  elegoo/<SN>/<client>/api_request | api_response — команды/ответы ({"type":"PING"} → {"type":"PONG"})

Здесь реализовано БЕЗОПАСНОЕ ТОЛЬКО-ЧТЕНИЕ статуса: подключаемся, подписываемся, собираем
телеметрию, отключаемся. Команды печати НЕ шлём (отправка появится отдельно, после захвата
реального обмена ElegooSlicer и только по явной команде пользователя).

Гейт: нужны ELEGOO_PRINTER_IP и ELEGOO_ACCESS_CODE (иначе enabled()=False, кнопка скрыта).
MQTT 3.1.1 реализован минимально (без сторонних зависимостей); блокирующий — вызывать через
asyncio.to_thread. Каждый recv ограничен таймаутом, поэтому не зависает.

АДРЕСАТ ВЫЗОВА. Каждая функция, ходящая в сеть, принимает необязательный `ep: Endpoint` —
адрес, код доступа, серийник и параметры камеры КОНКРЕТНОГО принтера. `ep=None` означает
«принтер по умолчанию» и собирается из .env ровно теми же значениями, что читались раньше.
Состояние живого соединения собрано в `ElegooSession` (сокет подписчика, привязка, снимок
телеметрии, пассивный сбор, снимок граммов). Модульные функции — фасад над сессией принтера по
умолчанию, `session()`. Сессия в процессе пока ОДНА, поэтому второй принтер сегодня получает
только разовые соединения (`_request_transient`), а телеметрии и таймлапсов у него нет: чтобы
их дать, нужен реестр сессий по адресу плюс ключевание ретранслятора камеры и printwatch.
"""
from __future__ import annotations

import hashlib
import http.client
import itertools
import json
import logging
import os
import queue
import re
import socket
import struct
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace

from . import config

log = logging.getLogger(__name__)

MQTT_PORT = 1883
MQTT_USER_DEFAULT = "elegoo"      # логин брокера CC2 (переопределяется ELEGOO_MQTT_USER)
CAMERA_PORT_DEFAULT = "8080"      # камера SDCP: http://<ip>:8080/mjpeg (ELEGOO_CAMERA_PORT/PATH)
CAMERA_PATH_DEFAULT = "/mjpeg"
STATUS_METHOD = 6000
CLIENT_ID = "newbor_bot"          # старый transient-путь request() (фолбэк)
# «Привязка» разведана 20.07 (docs/captures): клиент публикует в elegoo/{SN}/api_register свой
# client_id, принтер отвечает register_response и с этого момента шлёт ему unicast-ответы на
# elegoo/{SN}/{client_id}/api_response. Так работает ElegooSlicer; теперь так же — бот, через
# постоянное соединение фонового подписчика. CHANNEL_ID стабилен: повторный api_register даёт
# «already registered» — это тоже успех. Формат как у эфемерных каналов слайсера (0cli + hex).
CHANNEL_ID = "0clinbbot01"
METHOD_LIST_FILES = 1044
METHOD_START_PRINT = 1020
METHOD_JOB_HISTORY = 1036          # история заданий + ссылки на таймлапс (docs/printer-integration.md)
METHOD_TIMELAPSE_EXPORT = 1051     # «GetTimeLapseVideoList» — на деле СБОРКА mp4 из кадров задания
                                   # по его time_lapse_video_url; в ответе url готового файла
                                   # (разведано по lan_service_web ElegooSlicer, задача 4c6cd9e8)

# Команды управления печатью (пауза/продолжить/стоп/свет). В отличие от 1020/1044, которые мы
# подсмотрели захватом обмена ElegooSlicer, реальные method-коды этих команд у Centauri Carbon 2
# (MQTT) НЕ разведаны и публично не документированы (домыслы запрещены, см. docs/printer-
# integration.md). Поэтому коды берём из окружения; пока они не заданы — command_method()=None,
# а команда вернёт честную ошибку «не разведана», а не выстрелит наугад в живой принтер. Захватить
# их можно одной сессией ElegooSlicer (Пауза/Стоп/Свет) и прописать ELEGOO_METHOD_* в .env.


def _method_env(name: str) -> int | None:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("неверный %s=%r — ожидался целочисленный method-код", name, raw)
        return None


# action → (env-переменная с method-кодом, человеко-понятное имя для ошибки)
_CONTROL_ACTIONS = {
    "pause": ("ELEGOO_METHOD_PAUSE", "Пауза"),
    "resume": ("ELEGOO_METHOD_RESUME", "Продолжить"),
    "stop": ("ELEGOO_METHOD_STOP", "Стоп"),
    "light": ("ELEGOO_METHOD_LIGHT", "Свет"),
}
_req_counter = itertools.count(1000)


# ─── Справочники стокового протокола из официального SDK Elegoo ─────────────────────────────
# Источник — `elegooofficial/elegoo-link` (Apache-2.0), адаптер CC2:
# src/lan/adapters/elegoo_fdm_cc2/elegoo_fdm_cc2_message_adapter.cpp. Это не догадки по
# захватам, а таблицы самого вендора: коды ошибок ответа (convertRequestErrorToElegooError) и
# разбор machine_status.status / sub_status. Разбор фич сообщества — docs/Перспективные
# разработки/cc2-community-features-2026-10-03.md.

# error_code ответа api_response → что сказать мастеру. Раньше мастер видел «error_code=1009»
# и шёл гуглить; принтер при этом говорил вполне конкретное «я занят».
_ERROR_TEXT = {
    109: "закончился филамент",
    1000: "принтер не принял код доступа",
    1001: "принтер не знает такой команды",
    1002: "принтер не смог открыть папку",
    1003: "принтер отклонил параметры команды",
    1004: "не удалось записать файл на принтер",
    1005: "принтер не смог обновить токен",
    1006: "внутренняя ошибка принтера: команда не дошла до платы управления",
    1007: "принтер не смог удалить файл",
    1008: "принтер вернул пустой ответ",
    1009: "принтер занят",
    1010: "принтер сейчас не печатает",
    1011: "принтер не смог скопировать файл",
    1012: "задание не найдено",
    1013: "ошибка базы данных принтера",
    1021: "файл для печати не найден на принтере",
    1026: "нет данных выравнивания стола: откалибруйте стол или включите калибровку перед печатью",
    9000: "смещение файла не совпало с тем, что уже лежит на принтере",
    9001: "принтер не смог открыть файл на запись",
    9002: "принтер не смог записать файл",
    9003: "принтер не смог перейти к нужному месту файла",
    9004: "контрольная сумма файла не сошлась",
    9005: "отменять нечего: передача шла не от этого клиента",
    9006: "принтер не смог отменить передачу",
    9007: "на принтере нет папки для загрузки",
    9008: "контрольная сумма не сошлась (системная ошибка принтера)",
    9009: "контрольная сумма не сошлась (принтер не прочитал файл)",
    9999: "неизвестная ошибка принтера",
}


def error_text(code) -> str:
    """error_code ответа → строка для тоста. Код остаётся в скобках: по нему ищут в логе."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return f"Принтер вернул ошибку ({code})"
    text = _ERROR_TEXT.get(c)
    if not text:
        return f"Принтер вернул ошибку (код {c})"
    return f"{text[0].upper()}{text[1:]} (код {c})"


# machine_status.status → состояние машины. 2 = «идёт задание», подробности — в sub_status.
_MACHINE_STATE = {
    0: "Запускается", 1: "Свободен", 2: "Печать", 3: "Работа с филаментом",
    4: "Работа с филаментом", 5: "Автовыравнивание стола", 6: "Калибровка PID",
    7: "Тест резонанса", 8: "Самопроверка", 9: "Обновление прошивки", 10: "Парковка осей",
    11: "Передача файла", 12: "Сборка таймлапса", 13: "Работа экструдера",
    14: "Аварийная остановка", 15: "Восстановление после сбоя питания",
}

# sub_status → уточнение. Коды общие для нескольких состояний (2901 — выравнивание и в печати,
# и в самопроверке), поэтому таблица одна. Значения — ровно из разбора вендора.
_SUB_STATE = {
    1041: "Подготовка", 1045: "Прогрев сопла", 1096: "Прогрев сопла",
    1405: "Прогрев стола", 1906: "Прогрев стола",
    1061: "Загрузка в экструдер", 1063: "Загрузка в экструдер завершена",
    1062: "Выгрузка из экструдера", 1064: "Выгрузка из экструдера завершена",
    1081: "Загрузка файла", 1082: "Загрузка файла", 1086: "Загрузка файла",
    1133: "Загрузка филамента", 1134: "Загрузка филамента", 1135: "Загрузка филамента",
    1136: "Филамент загружен", 1144: "Выгрузка филамента", 1145: "Филамент выгружен",
    1503: "Калибровка PID", 1504: "Калибровка PID", 1505: "Калибровка PID завершена",
    1506: "Калибровка PID не удалась",
    2061: "Обновление", 2071: "Обновление", 2072: "Обновление", 2073: "Обновление",
    2074: "Обновление завершено",
    2075: "Печать", 2077: "Печать завершена",
    2401: "Продолжение печати", 2402: "Печать продолжена",
    2501: "Ставится на паузу", 2502: "Пауза", 2505: "Пауза",
    2503: "Остановка", 2504: "Остановлено",
    2801: "Парковка осей", 2802: "Оси запаркованы", 2803: "Парковка не удалась",
    2901: "Автовыравнивание стола", 2902: "Выравнивание завершено",
    3000: "Приём файла", 3001: "Файл принят",
    5934: "Тест резонанса", 5935: "Тест резонанса завершён", 5936: "Тест резонанса не удался",
}

# Подстатусы паузы. До этой таблицы бот паузы не видел вовсе: print_stats.state достраивается
# из остатка времени и на паузе остаётся «printing» (см. JobCard в деске).
_PAUSED_SUBS = frozenset({2501, 2502, 2505})

# Режим скорости печати (method 1031, телеметрия gcode_move.speed_mode) — значения из SDK:
# «Silent: 0, Balanced: 1, Sport: 2, Ludicrous: 3».
SPEED_MODES = {0: "Тихий", 1: "Баланс", 2: "Спорт", 3: "Безумный"}
METHOD_SPEED_MODE = 1031
METHOD_AUTO_REFILL = 2004          # {"auto_refill": bool}; состояние — canvas_info.auto_refill


def machine_phase(state: dict) -> dict | None:
    """machine_status → {status, sub_status, label, detail, paused} или None, если принтер его
    ещё не прислал. label — состояние машины, detail — уточнение по sub_status (может быть
    пустым: неизвестный вендору подстатус честно остаётся без подписи)."""
    ms = (state or {}).get("machine_status")
    if not isinstance(ms, dict) or not _is_num(ms.get("status")):
        return None
    status = int(ms["status"])
    sub = int(ms["sub_status"]) if _is_num(ms.get("sub_status")) else 0
    label = _MACHINE_STATE.get(status, f"Состояние {status}")
    detail = _SUB_STATE.get(sub, "") if sub else ""
    if detail == label:
        detail = ""
    return {"status": status, "sub_status": sub, "label": label, "detail": detail,
            "paused": status == 2 and sub in _PAUSED_SUBS}


# ─── Кому адресован вызов: Endpoint ──────────────────────────────────────────────────────────
# Раньше каждая сетевая точка модуля читала .env сама. Пока принтер в мастерской один, это
# работало; но карточка ВТОРОГО принтера с собственным адресом при этом молча управляла бы
# ПЕРВЫМ — её «стоп» остановил бы боевую печать и отрапортовал бы об успехе. Endpoint делает
# адресата явным параметром вызова. `ep=None` по-прежнему означает «принтер из .env», поэтому
# для всех сегодняшних вызовов не меняется ровно ничего.


@dataclass(frozen=True)
class Endpoint:
    """Один принтер: куда стучаться (ip), чем авторизоваться (code/mqtt_user), в чьи темы
    публиковать (serial), где искать камеру (camera_port/camera_path).

    frozen — чтобы эндпоинт нельзя было доправить на полпути. Адрес попадает в сокет, а
    серийник — в тему MQTT, и разъезд между ними означает «команда ушла на правильный принтер
    в тему чужой машины»: она не выполнится, а вызов отчитается об успехе. Такое не ловится
    ничем, кроме жалобы «команды молча не работают»."""

    ip: str = ""
    code: str = ""
    serial: str = ""
    mqtt_user: str = MQTT_USER_DEFAULT
    camera_port: str = CAMERA_PORT_DEFAULT
    camera_path: str = CAMERA_PATH_DEFAULT


def _ep(ep: Endpoint | None = None) -> Endpoint:
    """Эндпоинт вызова: явный — как есть; None — собранный из настроек принтера по умолчанию.

    Настройки читаются на КАЖДЫЙ вызов, а не один раз при импорте: подписчик перечитывает адрес
    при каждом переподключении, а админка и тесты подменяют config на лету. Порт и путь камеры
    жили `os.getenv` прямо в теле camera_url — теперь это поля эндпоинта, иначе камера второго
    принтера бралась бы с параметров первого."""
    if ep is not None:
        return ep
    return Endpoint(
        ip=(config.ELEGOO_PRINTER_IP or "").strip(),
        code=(config.ELEGOO_ACCESS_CODE or "").strip(),
        serial=(config.ELEGOO_SERIAL or "").strip(),
        mqtt_user=(config.ELEGOO_MQTT_USER or "").strip(),
        camera_port=(os.getenv("ELEGOO_CAMERA_PORT", CAMERA_PORT_DEFAULT).strip()
                     or CAMERA_PORT_DEFAULT),
        camera_path=(os.getenv("ELEGOO_CAMERA_PATH", CAMERA_PATH_DEFAULT).strip()
                     or CAMERA_PATH_DEFAULT),
    )


def _configured(e: Endpoint) -> bool:
    """Есть ли куда и чем идти. Тот же гейт, что `enabled()`, но по УЖЕ собранному эндпоинту:
    примитивы держат его в руках и не должны собирать второй раз."""
    return bool(e.ip and e.code)


def enabled(ep: Endpoint | None = None) -> bool:
    """Настроен ли принтер (есть адрес и код доступа). Без ep — принтер по умолчанию, как раньше."""
    return _configured(_ep(ep))


def norm_host(addr: str) -> str:
    """Адрес к сравнимому виду — только для СРАВНЕНИЯ и для ключа реестра сессий; в сокет уходит
    исходная строка.

    Поле адреса в карточке принтера подписано как перекрывающее .env, и владелец вправе вписать
    туда свой принтер в любой привычной форме. Побайтовое сравнение считало бы «http://192.168.0.10»,
    «192.168.0.10:1883» и «192.168.0.10/» ТРЕМЯ РАЗНЫМИ принтерами — а это три подписчика на один
    брокер с одинаковым client_id, то есть бесконечная война реконнектов (брокер обязан рвать
    предыдущую сессию), снаружи видная как «принтер мигает онлайн/оффлайн».

    Порт срезаем только у форм с явной схемой или единственным двоеточием: у IPv6 двоеточий
    много, и резать по последнему значило бы ломать адрес."""
    s = (addr or "").strip().lower().rstrip("/")
    for scheme in ("http://", "https://", "mqtt://"):
        if s.startswith(scheme):
            s = s[len(scheme):]
            break
    s = s.rstrip("/")
    if s.count(":") == 1:                     # host:port, но не IPv6
        s = s.split(":", 1)[0]
    return s


def card_endpoint(address: str = "", code: str = "", serial: str = "") -> Endpoint:
    """Эндпоинт КАРТОЧКИ принтера: её три поля поверх принтера по умолчанию.

    Базой берём эндпоинт по умолчанию и заменяем ровно то, что есть в карточке. Собирать
    конструктором заново значило бы молча потерять ELEGOO_MQTT_USER и ELEGOO_CAMERA_PORT/PATH
    у тех, кто их задал. Пустое поле карточки = «как в .env» (так работает боевая установка,
    где карточка про адрес ничего не знает).

    Адрес и серийник обязаны ехать ВМЕСТЕ, и поэтому .env-серийник достаётся ТОЛЬКО карточке,
    ведущей на .env-машину. Отдай его карточке с ЧУЖИМ адресом — тело уйдёт на правильный
    принтер в тему ЧУЖОЙ машины: команда не выполнится нигде, а вызов отчитается об успехе
    (`unconfirmed`). Хуже того, подписчик такого адреса не привяжется никогда: ветка
    автоопределения серийника из топика срабатывает только при ПУСТОМ серийнике, а значит нет ни
    подтверждённых ответов, ни self-poll (свет, вентиляторы, позиция, слоты), и серийник первого
    принтера показывается в карточке второго как свой.

    Пустой адрес в .env означает «принтер описан только карточкой» (white-label): путать не с
    чем, фолбэк работает как раньше."""
    base = _ep()
    ip = (address or base.ip).strip()
    same_machine = not norm_host(base.ip) or norm_host(ip) == norm_host(base.ip)
    return replace(base,
                   ip=ip,
                   code=(code or base.code).strip(),
                   serial=(serial or (base.serial if same_machine else "")).strip())


def camera_url(ep: Endpoint | None = None) -> str:
    """URL MJPEG-потока камеры принтера для проксирования в админ-панель.

    Приоритет — явный config.PRINTER_CAMERA_URL (для нестандартных камер/RTSP-мостов), но
    только для принтера по умолчанию: это настройка ЕГО камеры. Эндпоинту с ДРУГИМ адресом
    её отдавать нельзя — мастер смотрел бы на первый принтер, думая, что видит второй.

    «По умолчанию» здесь означает то же, что и в предохранителе драйвера: пустой адрес в .env
    значит «принтер описан только карточкой» (установка white-label), и мост тогда принадлежит
    ей. Без этой оговорки покупатель с камерой через RTSP-мост и адресом в карточке молча
    получал бы http://<ip>:8080/mjpeg и «камера не отвечает» без объяснения.
    Иначе выводим из IP принтера: Centauri Carbon отдаёт камеру по SDCP как
    http://<ip>:8080/mjpeg (порт/путь можно переопределить ELEGOO_CAMERA_PORT/PATH).
    Камеру нужно один раз включить на экране принтера (SDCP Cmd 386). Пусто → камеры нет."""
    e = _ep(ep)
    default_ip = (config.ELEGOO_PRINTER_IP or "").strip()
    if config.PRINTER_CAMERA_URL and (not default_ip or e.ip == default_ip):
        return config.PRINTER_CAMERA_URL
    if not e.ip:
        return ""
    path = e.camera_path if e.camera_path.startswith("/") else "/" + e.camera_path
    return f"http://{e.ip}:{e.camera_port}{path}"


# ---- минимальный MQTT 3.1.1 (только то, что нужно) ----
def _rem_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def _mqtt_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _connect_pkt(client_id: str, user: str, password: str) -> bytes:
    # variable header: "MQTT", level 4, flags=user+pass+clean(0xC2), keepalive 60
    vh = _mqtt_str("MQTT") + bytes([0x04, 0xC2]) + struct.pack(">H", 60)
    payload = _mqtt_str(client_id) + _mqtt_str(user) + _mqtt_str(password)
    rem = vh + payload
    return bytes([0x10]) + _rem_len(len(rem)) + rem


def _subscribe_pkt(topic: str, packet_id: int = 1) -> bytes:
    payload = struct.pack(">H", packet_id) + _mqtt_str(topic) + bytes([0x00])  # QoS 0
    return bytes([0x82]) + _rem_len(len(payload)) + payload


def _publish_pkt(topic: str, payload: bytes) -> bytes:
    rem = _mqtt_str(topic) + payload          # QoS 0 → без packet id
    return bytes([0x30]) + _rem_len(len(rem)) + rem


def _read_rem_len(sock: socket.socket):
    mult, val = 1, 0
    while True:
        b = sock.recv(1)
        if not b:
            return None
        b = b[0]
        val += (b & 0x7f) * mult
        if not (b & 0x80):
            break
        mult *= 128
    return val


def _recv_pkt(sock: socket.socket):
    head = sock.recv(1)
    if not head:
        return None, None
    rem = _read_rem_len(sock)
    if rem is None:
        return None, None
    data = b""
    while len(data) < rem:
        chunk = sock.recv(rem - len(data))
        if not chunk:
            break
        data += chunk
    return head[0], data


def _parse_publish(ptype: int, data: bytes):
    """(topic, obj|None) из PUBLISH-пакета."""
    tlen = struct.unpack(">H", data[:2])[0]
    topic = data[2:2 + tlen].decode("utf-8", "ignore")
    off = 2 + tlen + (2 if ((ptype & 0x06) >> 1) > 0 else 0)   # QoS>0 → есть packet id
    try:
        obj = json.loads(data[off:].decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        obj = None
    return topic, obj


def _deep_merge(dst: dict, src: dict) -> dict:
    """Накладываем частичный статус на накопленный (телеметрия приходит дельтами)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _has_snapshot(state: dict) -> bool:
    """Накопили достаточно телеметрии, чтобы отвечать, не дожидаясь всего окна?

    Centauri рассылает api_status дельтами и не всегда шлёт heater_bed в каждом снимке,
    поэтому жёсткое условие «extruder И heater_bed» раньше держало опрос на полный таймаут.
    Достаточно температуры сопла плюс любого «живого» блока (стол, либо статус печати с
    прогрессом/слоем, либо обдув) — это покрывает и idle, и активную печать."""
    if "extruder" not in state:
        return False
    return any(k in state for k in ("heater_bed", "heatbed", "bed", "print_status", "fans"))


def get_status(timeout: float = 4.0, ep: Endpoint | None = None) -> dict:
    """Только-чтение: собрать телеметрию принтера за timeout секунд. {ok, serial, state} | {ok:False}.

    РАЗОВОЕ подключение — для скриптов/диагностики. Вебапп сюда НЕ ходит: он читает только
    кэш фонового подписчика (cached_status), иначе каждый опрос страницы превращается в
    connect/disconnect по MQTT и досит принтер (фидбэк 20.07).

    Возвращаемся раньше, как только есть полезный снимок (_has_snapshot), чтобы экран статуса
    не ждал весь таймаут. Чтение идёт короткими шагами (recv-таймаут < окна) — застрявший
    пакет не съедает весь бюджет, цикл просто повторяет приём до общего дедлайна."""
    dst = _ep(ep)
    if not _configured(dst):
        return {"ok": False, "error": "Принтер Elegoo не настроен"}
    try:
        sock = socket.create_connection((dst.ip, MQTT_PORT), timeout=5)
    except OSError as e:
        return {"ok": False, "error": f"Нет связи с принтером: {e}"}
    # короткий per-recv таймаут: телеметрия Centauri сыпется чаще, держать recv на всё окно незачем
    sock.settimeout(min(1.5, timeout))
    try:
        sock.sendall(_connect_pkt("newbor-status", dst.mqtt_user, dst.code))
        ptype, data = _recv_pkt(sock)
        if ptype != 0x20 or len(data) < 2 or data[1] != 0:
            rc = data[1] if (ptype == 0x20 and len(data) >= 2) else "?"
            return {"ok": False, "error": f"Принтер отклонил подключение (код {rc})"}
        sock.sendall(_subscribe_pkt("elegoo/#"))
        serial = dst.serial
        state: dict = {}
        end = time.time() + timeout
        grace = None                       # после первого снимка добираем дельты совсем недолго
        while time.time() < end:
            try:
                ptype, data = _recv_pkt(sock)
            except socket.timeout:
                if grace is not None:      # есть снимок, новых пакетов нет — выходим
                    break
                continue                   # пакетов пока нет — ждём до общего дедлайна
            if ptype is None:
                break
            if (ptype & 0xF0) != 0x30:        # не PUBLISH
                continue
            topic, obj = _parse_publish(ptype, data)
            parts = topic.split("/")
            if len(parts) >= 2 and parts[0] == "elegoo" and not serial:
                serial = parts[1]
            if obj and topic.endswith("/api_status") and isinstance(obj.get("result"), dict):
                _deep_merge(state, obj["result"])
                if grace is None and _has_snapshot(state):
                    grace = time.time() + 0.4   # короткое окно склейки соседних дельт
            if grace is not None and time.time() >= grace:
                break
        return {"ok": True, "serial": serial, "state": state, "online": True}
    except Exception as e:  # noqa: BLE001
        log.warning("elegoo get_status failed", exc_info=True)
        return {"ok": False, "error": f"Ошибка связи: {e}"}
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


# ─── Сессия: одно живое соединение с ОДНИМ принтером ─────────────────────────────────────
# Держим ОДНО постоянное соединение и копим последний снимок телеметрии в памяти. Экран
# статуса читает кэш мгновенно (cached_status) — без переподключения на каждый опрос. Это и
# быстрее (нет рукопожатия MQTT раз в ~2.5 с), и ЛЕГЧЕ для принтера, чем reconnect-на-опрос.
# ВАЖНО (фидбэк «дудосим принтер»): connected отражает живость самого соединения. Пока оно
# живо, кэш считается АКТУАЛЬНЫМ независимо от возраста последнего кадра — в простое Centauri
# вещает редко (тишина = «ничего не менялось»), и это не повод переподключаться или ходить
# разовым get_status() на каждый опрос страницы.

# Как часто привязанный подписчик сам опрашивает полный статус (1002) и слоты (2005).
# Печать: чаще (данные быстро меняются). Простой: реже (экономно, но экран всё равно полон).
STATUS_POLL_PRINTING = 3.0
STATUS_POLL_IDLE = 15.0
SLOTS_POLL_SEC = 30.0


class ElegooSession:
    """Состояние ОДНОГО живого соединения с принтером Elegoo, собранное в объект.

    Раньше это были десять модульных глобалей, и «подписчик в процессе один» было не решением,
    а следствием: второй карточке принтера просто некуда было положить свой сокет, свою привязку
    и свой снимок телеметрии — она получала показания первого принтера. Сессия убирает именно это
    ограничение; сколько сессий поднимать и по какому ключу — решает уже реестр, а не файл.

    Ключ реестра будет НОРМАЛИЗОВАННЫЙ АДРЕС, а не id карточки: соединение физически одно на
    брокер, а `client_id` зашит константой (CHANNEL_ID). Две карточки на один адрес (мастер завёл
    вторую под другое сопло) при ключе-по-id подняли бы двух подписчиков с одинаковым client_id,
    и брокер обязан рвать предыдущую сессию — бесконечная война реконнектов, которая снаружи
    выглядит как «принтер мигает онлайн/оффлайн».

    Эндпоинт хранится ПРОВАЙДЕРОМ, а не значением: подписчик перечитывает адрес на каждом заходе
    цикла, потому что его могли поменять в настройках, пока мы висели в reconnect-паузе.

    Мутабельные поля (`live`, `harvest`, `pending`, `file_grams_snap`) никогда не переприсваиваются
    целиком — только правятся под своим мьютексом: на них держат ссылки и цикл подписчика, и
    вызовы из других потоков."""

    def __init__(self, ep_provider: Callable[[], Endpoint] | None = None) -> None:
        self._ep_provider = ep_provider or _ep
        self.live: dict = {"state": {}, "ts": 0.0, "serial": "", "connected": False}
        self.live_lock = threading.Lock()
        # «Привязанный» запрос-ответ поверх постоянного соединения подписчика (разведано 20.07).
        # request() публикует на наш канал через ЭТОТ сокет и ждёт ответ, который ловит цикл
        # подписчика и раздаёт по id в pending. Send из нескольких потоков (heartbeat +
        # to_thread-вызовы request) сериализуется send_lock; recv — только в потоке подписчика.
        self.sock: socket.socket | None = None
        self.send_lock = threading.Lock()
        self.bound = False                      # зарегистрированы ли на текущем соединении
        self.pending: dict[int, queue.Queue] = {}
        self.pending_lock = threading.Lock()
        self.harvest: dict = {"files": {}, "history": {}, "ts": 0.0}
        self.harvest_lock = threading.Lock()
        self.file_grams_snap: dict[str, float] = {}   # имя файла → граммы (снимок list_files)
        self.file_grams_probe_ts: float = 0.0         # когда реально ходили в активный 1044
        self.file_grams_lock = threading.Lock()
        self._started = False
        self._stop = threading.Event()

    def endpoint(self) -> Endpoint:
        """Куда ходит эта сессия ПРЯМО СЕЙЧАС (настройки могли поменять на ходу)."""
        return self._ep_provider()

    def rebind(self, ep: Endpoint) -> None:
        """Прибить сессию к конкретному эндпоинту (карточке принтера).

        Адрес при этом обязан оставаться тем же — он ключ реестра; меняются код доступа и
        серийник, которые правят в админке. Сессию принтера по умолчанию так не трогают: её
        эндпоинт читается из настроек провайдером, и подмена его константой сломала бы
        подхват смены адреса."""
        self._ep_provider = lambda: ep

    def _enabled(self) -> bool:
        """Настроен ли принтер ЭТОЙ сессии.

        Именно свой, а не «принтер из .env»: раньше цикл спрашивал модульный `enabled()`, и для
        второй сессии это значило бы «стою, пока не настроен первый принтер» — и наоборот,
        «ломлюсь в сеть, хотя мой собственный адрес пуст»."""
        return _configured(self.endpoint())

    def is_bound(self) -> bool:
        """Привязан ли бот к принтеру сейчас (принтер шлёт нам unicast-ответы, как слайсеру)."""
        with self.live_lock:
            return self.bound and self.live["connected"]

    def subscriber_endpoint(self) -> tuple[str, str, bool]:
        """Куда ведёт живой фоновый подписчик этой сессии: (ip, serial, connected).

        Спрашивают это не из любопытства. Пока сессия в процессе одна, карточка принтера с
        собственным адресом управляла бы НЕ своим принтером: её «пауза» ушла бы по сокету
        подписчика на первый CC2. Драйверу нужен способ это заметить и честно сказать «пульта
        нет», пока у карточки нет собственной сессии.

        ip берём из настроек, а не из состояния подписчика: подписчик читает его там же при
        каждом переподключении (_loop), а состояние показывало бы «никуда» те секунды, пока
        соединение переустанавливается — и пульт мигал бы вместе с реконнектом. serial — тот, что
        реально прилетел в топике (он же автоопределяется), с фолбэком на настройки. connected
        описывает живость соединения и на выбор принтера не влияет — это диагностика для UI."""
        dflt = self.endpoint()
        with self.live_lock:
            serial = str(self.live.get("serial") or "").strip()
            connected = bool(self.live.get("connected"))
        return dflt.ip, serial or dflt.serial, connected

    def is_subscriber_endpoint(self, e: Endpoint) -> bool:
        """Тот ли это принтер, с которым уже говорит подписчик ЭТОЙ сессии.

        Сравниваем ТОЛЬКО адрес. Серийник в сравнение не входит намеренно: у боевой установки он
        автоопределяется из топика и в .env может быть пуст, так что расхождение по нему выкинуло
        бы каждую команду мастера в разовое соединение — то есть в лишние connect/disconnect к
        принтеру (больная тема проекта) и в ответы «без подтверждения». Пустой адрес подписчика
        означает, что идти ему некуда и путать не с чем."""
        sub_ip, _sub_serial, _connected = self.subscriber_endpoint()
        return not sub_ip or e.ip == sub_ip

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._enabled():
                self._stop.wait(10.0)
                continue
            # эндпоинт перечитывается на каждом заходе цикла: адрес принтера в настройках могли
            # поменять, пока мы висели в reconnect-паузе
            dst = self.endpoint()
            sock = None
            try:
                sock = socket.create_connection((dst.ip, MQTT_PORT), timeout=5)
                sock.settimeout(2.0)
                sock.sendall(_connect_pkt("newbor-live", dst.mqtt_user, dst.code))
                ptype, data = _recv_pkt(sock)
                if ptype != 0x20 or len(data) < 2 or data[1] != 0:
                    raise OSError("CONNACK отклонён")
                sock.sendall(_subscribe_pkt("elegoo/#"))
                backoff = 1.0
                serial = dst.serial
                last_ping = time.time()
                last_hb = 0.0                              # heartbeat на наш канал (держит привязку)
                last_1002 = 0.0                            # self-poll полного статуса (см. ниже)
                last_2005 = 0.0                            # self-poll слотов CANVAS
                with self.live_lock:
                    self.live["connected"] = True
                with self.send_lock:
                    self.sock = sock
                    self.bound = False
                if serial:                                 # регистрируемся как «привязанный» клиент
                    self._register(sock, serial)
                while self._enabled() and not self._stop.is_set():
                    # heartbeat: PING на наш api_request-канал каждые ~8 c — как ElegooSlicer; без
                    # него привязка со временем протухает, и принтер снова замолкает на unicast.
                    now = time.time()
                    if serial and now - last_hb > 8:
                        try:
                            with self.send_lock:
                                sock.sendall(_publish_pkt(
                                    f"elegoo/{serial}/{CHANNEL_ID}/api_request", b'{"type":"PING"}'))
                        except OSError:
                            break
                        last_hb = now
                    # self-poll полного статуса: раз привязаны — сами запрашиваем 1002 (свет/
                    # вентиляторы/позиция/датчик прутка/print_status) и реже 2005 (слоты). Вещание
                    # api_status в простое скудное (часто только heater_bed) — а так экран «Печать»/
                    # «Пульт» ВСЕГДА имеет полный снимок. Одно соединение, редкие запросы → не дудос.
                    # Ответы ловит тот же цикл recv и мержит в кэш (merge_response_status). Чаще в
                    # печати, реже в простое.
                    if serial and self.bound:
                        printing = ((self.live["state"].get("print_status") or {})
                                    .get("state") == "printing")
                        every = STATUS_POLL_PRINTING if printing else STATUS_POLL_IDLE
                        if now - last_1002 > every:
                            self._fire(sock, serial, 1002)
                            last_1002 = now
                        if now - last_2005 > SLOTS_POLL_SEC:
                            self._fire(sock, serial, 2005)
                            last_2005 = now
                    try:
                        ptype, data = _recv_pkt(sock)
                    except socket.timeout:
                        if time.time() - last_ping > 30:   # keepalive PINGREQ (connect keepalive=60)
                            with self.send_lock:
                                sock.sendall(b"\xc0\x00")
                            last_ping = time.time()
                        continue
                    if ptype is None:
                        break
                    if (ptype & 0xF0) != 0x30:              # не PUBLISH
                        continue
                    topic, obj = _parse_publish(ptype, data)
                    parts = topic.split("/")
                    if len(parts) >= 2 and parts[0] == "elegoo" and not serial:
                        serial = parts[1]
                        self._register(sock, serial)   # serial узнали из вещания — регистрируемся
                    if not obj:
                        continue
                    if topic.endswith("/api_status") and isinstance(obj.get("result"), dict):
                        with self.live_lock:
                            _deep_merge(self.live["state"], obj["result"])
                            self.live["ts"] = time.time()
                            if serial:
                                self.live["serial"] = serial
                    elif topic.endswith("/register_response") and f"/{CHANNEL_ID}/" in topic:
                        with self.live_lock:
                            self.bound = True          # принтер нас знает → шлёт unicast-ответы
                        log.info("Elegoo: привязка подтверждена (%s)", obj.get("error"))
                    elif topic.endswith("/api_response"):
                        # ответ на наш запрос → разбудить ожидающий request() по id
                        if f"/{CHANNEL_ID}/" in topic and _is_num(obj.get("id")):
                            with self.pending_lock:
                                q = self.pending.get(obj["id"])
                            if q is not None:
                                try:
                                    q.put_nowait(obj)
                                except queue.Full:
                                    pass
                        try:            # ответы (наши и слайсеру) со статусом → живой кэш: 1002/2005
                            self.merge_response_status(obj)  # слоты/свет/вентиляторы/позиция
                        except Exception:  # noqa: BLE001
                            log.debug("response status merge failed", exc_info=True)
                        if config.ELEGOO_PASSIVE_HARVEST:
                            try:                        # Путь A: ловим ответы 1044/1036 из потока
                                self.harvest_api_response(topic, obj)
                            except Exception:  # noqa: BLE001
                                log.debug("passive harvest failed", exc_info=True)
            except Exception:  # noqa: BLE001
                log.debug("elegoo subscriber reconnect", exc_info=True)
            finally:
                with self.live_lock:
                    self.live["connected"] = False
                    self.bound = False
                with self.send_lock:
                    self.sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._stop.wait(backoff)
            backoff = min(backoff * 2.0, 30.0)


    def _register(self, sock: socket.socket, serial: str) -> None:
        """Опубликовать api_register нашим CHANNEL_ID — заявка на привязку. Подтверждение
        (register_response) ловит цикл подписчика и ставит bound. Идемпотентно («already
        registered»)."""
        try:
            body = json.dumps({"request_id": CHANNEL_ID, "client_id": CHANNEL_ID}).encode()
            with self.send_lock:
                sock.sendall(_publish_pkt(f"elegoo/{serial}/api_register", body))
        except OSError:
            log.debug("elegoo register publish failed", exc_info=True)

    def _fire(self, sock: socket.socket, serial: str, method: int) -> None:
        """Отправить read-запрос на наш канал БЕЗ ожидания — ответ придёт в общий цикл recv и
        сам смержится в кэш (merge_response_status). Для self-poll статуса/слотов."""
        try:
            body = json.dumps({"method": method, "id": next(_req_counter)}).encode()
            with self.send_lock:
                sock.sendall(_publish_pkt(f"elegoo/{serial}/{CHANNEL_ID}/api_request", body))
        except OSError:
            log.debug("elegoo self-poll %s failed", method, exc_info=True)

    def start(self) -> None:
        """Запустить фоновый MQTT-подписчик этой сессии (демон-поток). Идемпотентно; если принтер
        не настроен, поток простаивает и сам подхватит принтер, когда тот станет доступен.

        Идемпотентность здесь не вежливость, а защита: два подписчика к одному брокеру приходят
        с одинаковым client_id (CHANNEL_ID зашит константой), и брокер обязан рвать предыдущую
        сессию — получается бесконечная война реконнектов."""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        name = f"elegoo-mqtt-sub-{norm_host(self.endpoint().ip) or 'default'}"
        threading.Thread(target=self._loop, name=name, daemon=True).start()
        log.info("Elegoo MQTT-подписчик запущен (%s)", name)

    def stop(self) -> None:
        """Погасить подписчика этой сессии. Нужен, когда карточку второго принтера удалили или
        сменили ей адрес: иначе поток вечно ломится в машину, которой больше нет.

        Сокет закрываем принудительно — иначе поток заметит остановку только на таймауте recv."""
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
                log.debug("elegoo session socket close failed", exc_info=True)

    def cached_status(self, max_age: float = 6.0) -> dict | None:
        """Снимок от фонового подписчика. Возвращаем КОПИЮ state (вызывающий дополняет её enrich).

        Пока соединение подписчика ЖИВО — кэш актуален независимо от возраста кадра: в простое
        принтер вещает редко, тишина означает «ничего не менялось», а не «данные протухли».
        max_age работает только как грация на время реконнекта (соединение упало, но кадры были
        недавно). Ни при каком исходе НЕ инициируем новых подключений — это единственная защита
        принтера от шторма connect/disconnect (фидбэк «дудосим принтер, вырубается камера»)."""
        with self.live_lock:
            ts, connected = self.live["ts"], self.live["connected"]
            fresh = ts and (time.time() - ts) <= max_age
            if (connected or fresh) and self.live["state"]:
                return {"ok": True, "serial": self.live["serial"], "online": True, "cached": True,
                        "connected": bool(connected),
                        "age": round(time.time() - ts, 1) if ts else None,
                        "state": json.loads(json.dumps(self.live["state"]))}
            if connected:
                # соединение живо, но принтер ещё ничего не вещал (первые секунды после старта) —
                # честный «онлайн без данных», а не None (иначе вызывающие начнут стучаться сами)
                return {"ok": True, "serial": self.live["serial"], "online": True, "cached": True,
                        "connected": True, "age": None, "state": {}}
        return None

    def request(self, method: int, params: dict | None = None, timeout: float = 8.0,
                *, wait_data: bool = False, command: bool = False,
                ep: Endpoint | None = None) -> dict:
        """Запрос-ответ по сокету ЭТОЙ сессии, с фолбэком на разовое соединение. См. request()."""
        dst = _ep(ep)
        if not _configured(dst):
            return {"ok": False, "error": "Принтер Elegoo не настроен"}
        sn = dst.serial
        if not sn:
            return {"ok": False, "error": "Не задан серийный номер принтера (ELEGOO_SERIAL)"}

        # Быстрый путь идёт по сокету ПОДПИСЧИКА этой сессии. Чужому эндпоинту он не годится ни на
        # секунду: тело ушло бы по соединению ЧУЖОГО принтера, а при command=True вызов вернул бы
        # {ok: True, unconfirmed: True} — «успех», не покинувший первой машины. Не совпал адрес —
        # уходим в _request_transient, он откроет своё соединение.
        sock = self.sock
        if self.is_bound() and sock is not None and self.is_subscriber_endpoint(dst):
            rid = next(_req_counter)
            q: queue.Queue = queue.Queue(maxsize=1)
            with self.pending_lock:
                self.pending[rid] = q
            try:
                body = json.dumps({"id": rid, "method": method, "params": params or {}}).encode()
                with self.send_lock:
                    sock.sendall(_publish_pkt(f"elegoo/{sn}/{CHANNEL_ID}/api_request", body))
                deadline = time.time() + timeout
                while True:
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    try:
                        obj = q.get(timeout=left)
                    except queue.Empty:
                        break
                    result = obj.get("result") or {}
                    err = result.get("error_code", 0)
                    if err:
                        return {"ok": False, "err_code": err, "error": error_text(err)}
                    meaningful = any(k != "error_code" for k in result)
                    if meaningful or not wait_data:         # команда с error_code=0 = подтверждена
                        return {"ok": True, "result": result, "method": obj.get("method")}
                # привязка есть, а ответа нет вовремя — редко; не гадаем, честная ошибка/фолбэк-флаг
                if command:
                    return {"ok": True, "unconfirmed": True, "result": {}}
                return {"ok": False, "error": "Принтер не ответил вовремя"}
            except OSError as e:
                log.debug("elegoo bound request send failed: %s", e)  # соединение отвалилось → фолбэк
            finally:
                with self.pending_lock:
                    self.pending.pop(rid, None)
        return _request_transient(method, params, timeout, wait_data=wait_data,
                                  command=command, ep=dst)

    def merge_response_status(self, obj: dict) -> None:
        """result из api_response 1002/2005 → в живой кэш подписчика (как кадр api_status)."""
        if not isinstance(obj, dict) or obj.get("method") not in _STATUS_RESPONSE_METHODS:
            return
        result = obj.get("result")
        if not isinstance(result, dict) or result.get("error_code") not in (0, None):
            return
        payload = {k: v for k, v in result.items() if k != "error_code"}
        if not payload:
            return
        with self.live_lock:
            _deep_merge(self.live["state"], payload)
            self.live["ts"] = time.time()

    def harvest_api_response(self, topic: str, obj: dict) -> None:
        """Из api_response с method 1044/1036 обновить кэш пассивного сбора (по имени файла)."""
        if not isinstance(obj, dict) or not (topic or "").endswith("/api_response"):
            return
        result = obj.get("result")
        if not isinstance(result, dict):
            return
        method = obj.get("method")
        if method == METHOD_LIST_FILES:
            files = {f["name"]: f for f in _parse_file_list(result) if f.get("name")}
            if files:
                with self.harvest_lock:
                    self.harvest["files"].update(files)
                    self.harvest["ts"] = time.time()
        elif method == METHOD_JOB_HISTORY:
            hist = {j["name"]: j for j in _parse_history(result) if j.get("name")}
            if hist:
                with self.harvest_lock:
                    self.harvest["history"].update(hist)
                    self.harvest["ts"] = time.time()

    def harvested_info(self, filename: str) -> dict | None:
        """Пассивно собранные данные по имени gcode-файла. См. harvested_info()."""
        name = (filename or "").strip()
        if not name:
            return None
        with self.harvest_lock:
            f = self.harvest["files"].get(name)
            h = self.harvest["history"].get(name)
        if not f and not h:
            return None
        out: dict = {}
        if f:
            out.update({"grams": f.get("grams"), "print_time": f.get("print_time"),
                        "colors": f.get("colors")})
        if h:
            out.update({"duration_sec": h.get("duration_sec"), "status": h.get("status"),
                        "ok": h.get("ok")})
        return out

    def harvested_files(self) -> dict | None:
        """Пассивно собранный список файлов принтера. См. harvested_files()."""
        with self.harvest_lock:
            files = list(self.harvest["files"].values())
            ts = self.harvest["ts"]
        if not files:
            return None
        return {"ok": True, "files": files, "harvested": True,
                "age": round(time.time() - ts, 1) if ts else None}

    def file_grams(self, filename: str) -> float | None:
        """Граммы gcode-файла ЭТОГО принтера: пассивный сбор → снимок list_files под троттлингом.

        Эндпоинта в аргументах нет намеренно: сессия и есть «чей это принтер». Раньше здесь была
        развилка «свой — кэшем, чужой — мимо кэшей», потому что кэш был один на процесс и отдать
        чужому принтеру граммы одноимённого файла с первой машины значило соврать в стоимости
        заказа. Теперь кэш свой у каждого, и обход не нужен — а вместе с ним не теряется и
        троттлинг (экран «Печать» поллит статус каждые ~3 с, без кэша это шторм 1044)."""
        name = (filename or "").strip()
        if not name:
            return None
        hv = self.harvested_info(name)               # (1) пассивный сбор — самый дешёвый и главный
        if hv and (hv.get("grams") or 0) > 0:
            return float(hv["grams"])
        now = time.time()
        with self.file_grams_lock:
            cached = self.file_grams_snap.get(name)
            fresh = (now - self.file_grams_probe_ts) < _FILE_GRAMS_TTL
        if cached is not None:
            return cached                            # граммы файла неизменны → отдаём из кэша всегда
        if fresh:
            return None                              # недавно опрашивали, файла нет — не досим снова
        r = list_files(timeout=8.0, ep=self.endpoint())   # (2) блокирующий сетевой запрос к принтеру
        with self.file_grams_lock:
            self.file_grams_probe_ts = time.time()   # отметка даже при неудаче: не хлещем битый 1044
            if r.get("ok"):
                self.file_grams_snap.clear()
                for f in r.get("files") or []:
                    g = f.get("grams") or 0
                    if f.get("name") and g > 0:
                        self.file_grams_snap[f["name"]] = float(g)
            return self.file_grams_snap.get(name)


# ─── Реестр сессий ───────────────────────────────────────────────────────────────────────
# Сессия принтера по умолчанию — того, что описан в .env. Она особенная: её эндпоинт читается
# из настроек на каждом заходе цикла, поэтому смена адреса в админке подхватывается сама, и
# гасить её нельзя. Остальные сессии заводятся по карточкам принтеров.
#
# КЛЮЧ РЕЕСТРА — нормализованный АДРЕС, а не id карточки. Причина не стилистическая: соединение
# физически одно на брокер, а client_id зашит константой (CHANNEL_ID). Две карточки на один адрес
# (мастер завёл вторую под другое сопло) при ключе-по-id подняли бы двух подписчиков с одинаковым
# client_id, и брокер обязан рвать предыдущую сессию → бесконечная война реконнектов, снаружи
# видная как «принтер мигает онлайн/оффлайн».
_DEFAULT_SESSION = ElegooSession(_ep)
_sessions: dict[str, ElegooSession] = {}
_sessions_lock = threading.Lock()


def session() -> ElegooSession:
    """Сессия принтера по умолчанию. Модульные функции ниже — фасад над ней."""
    return _DEFAULT_SESSION


def session_for(ep: Endpoint | None = None) -> ElegooSession:
    """Сессия, обслуживающая ЭТОТ адрес. Совпал с принтером по умолчанию — она и есть.

    Совпадение считается по `norm_host`: «http://192.168.0.10», «192.168.0.10:1883» и
    «192.168.0.10/» — один и тот же брокер, и второй сессии к нему быть не должно.

    Реквизиты (код доступа, серийник) у найденной сессии обновляются на месте: карточку правят
    из админки, и держать сессию со старым паролем значило бы «принтер отвалился после смены
    кода» без единой строчки в логе."""
    dst = _ep(ep)
    key = norm_host(dst.ip)
    if not key or key == norm_host(_ep().ip):
        return _DEFAULT_SESSION
    with _sessions_lock:
        ses = _sessions.get(key)
        if ses is None:
            ses = ElegooSession()
            _sessions[key] = ses
        ses.rebind(dst)
        return ses


def sessions_sync(endpoints: list[Endpoint]) -> list[str]:
    """Привести реестр в соответствие со списком принтеров: поднять недостающих подписчиков,
    погасить лишних. Возвращает ключи живых сессий (без принтера по умолчанию) — для лога.

    Звать после любой правки карточек принтеров. Не позовёшь — удалённая карточка навсегда
    оставит поток, вечно ломящийся в машину, которой больше нет, а новая не получит телеметрии
    до перезапуска бота.

    Принтер по умолчанию сюда не попадает никогда: его сессией управляет `start_subscriber()`,
    и погасить её означало бы обезглавить всю автоматику мастерской."""
    default_key = norm_host(_ep().ip)
    want: dict[str, Endpoint] = {}
    for e in endpoints:
        if not _configured(e):
            continue                          # нет адреса или кода — идти некуда, поток не нужен
        key = norm_host(e.ip)
        if not key or key == default_key:
            continue
        want[key] = e
    with _sessions_lock:
        gone = [_sessions.pop(k) for k in list(_sessions) if k not in want]
        live = []
        for key, e in want.items():
            ses = _sessions.get(key)
            if ses is None:
                ses = ElegooSession()
                _sessions[key] = ses
            ses.rebind(e)
            live.append(ses)
    for ses in gone:                          # вне лока: stop() закрывает сокет
        ses.stop()
    for ses in live:
        ses.start()
    return sorted(want)


def sessions_stop_all() -> None:
    """Погасить подписчиков всех КАРТОЧЕК. Сессию принтера по умолчанию не трогаем — она живёт
    столько же, сколько процесс. Нужно на остановке бота и в тестах, чтобы поток не пережил
    того, кто его завёл."""
    with _sessions_lock:
        gone = list(_sessions.values())
        _sessions.clear()
    for ses in gone:
        ses.stop()


def is_bound() -> bool:
    """Привязан ли бот к принтеру сейчас (принтер шлёт нам unicast-ответы, как слайсеру)."""
    return session().is_bound()


def subscriber_endpoint() -> tuple[str, str, bool]:
    """Куда ведёт живой фоновый подписчик: (ip, serial, connected). См. ElegooSession."""
    return session().subscriber_endpoint()


def start_subscriber() -> None:
    """Запустить фоновый MQTT-подписчик принтера ПО УМОЛЧАНИЮ. Идемпотентно.

    Подписчиков в процессе столько, сколько адресов у активных карточек: этот — для принтера из
    .env, остальных поднимает `sessions_sync()` (её зовёт `db.refresh_printers` после любой
    правки карточек). Разделены они намеренно: сессия по умолчанию живёт столько же, сколько
    процесс, и гасить её нельзя — на ней держится вся автоматика мастерской."""
    session().start()


def cached_status(max_age: float = 6.0, ep: Endpoint | None = None) -> dict | None:
    """Снимок от фонового подписчика. Без ep — принтер по умолчанию.

    Маршрутизация «эндпоинт → сессия» живёт ЗДЕСЬ, а не у вызывающих: подмены в тестах и
    половина ручек ставятся на модульные функции, и драйвер, зовущий сессию напрямую, молча
    проходил бы мимо них — вплоть до похода в сеть к боевому принтеру."""
    return session_for(ep).cached_status(max_age)


def summarize(state: dict) -> dict:
    """Достаём знакомые поля телеметрии (имена — как в api_status: extruder.temperature и т.п.)."""
    ex = state.get("extruder") or {}
    bed = state.get("heater_bed") or state.get("heatbed") or state.get("bed") or {}
    return {
        "extruder_temp": ex.get("temperature"),
        "extruder_target": ex.get("target") or ex.get("targetTemperature"),
        "bed_temp": bed.get("temperature"),
        "bed_target": bed.get("target") or bed.get("targetTemperature"),
    }


# Вентиляторы в телеметрии: ключ прошивки → короткое имя для UI. Прошивка непоследовательна в
# шкале: пользовательские (model/aux/box) вещаются долей 0..1, системные (controller/heater) —
# в абсолюте 0..255 (разведано живьём 20.07: controller_fan.speed=255 при работе). Нормализуем
# порогом в _fan_pct, а не слепым ×100 (иначе 255 → 25500%).
_FAN_KEYS = (("fan", "model"), ("aux_fan", "aux"), ("box_fan", "box"),
             ("controller_fan", "controller"), ("heater_fan", "heater"))


def _fan_pct(sp) -> int | None:
    """Скорость вентилятора → проценты, учитывая обе шкалы прошивки (0..1 и 0..255)."""
    if not _is_num(sp):
        return None
    v = float(sp)
    return round(v / 255 * 100) if v > 1.5 else round(v * 100)


def extras(state: dict) -> dict:
    """Дополнительная телеметрия для настольной админки: всё полезное, что вещание api_status
    приносит сверх температур/прогресса. Ключи и смысл полей разведаны захватами обмена
    ElegooSlicer↔принтер (docs/captures/): led.status — реальное состояние подсветки,
    machine_status.exception_status — ошибки принтера, ztemperature_sensor — температура в
    камере, canvas_info — слоты CANVAS (что заряжено), active_tray_id — какой слот подаёт
    филамент (−1 = никакой). Ничего не выдумываем: ключей нет в снимке — их нет и в ответе
    (в простое принтер вещает мало, по ходу печати дельты доносят остальное)."""
    out: dict = {}
    led = state.get("led") or {}
    if _is_num(led.get("status")):
        out["light"] = 1 if led["status"] else 0
    ex = state.get("extruder") or {}
    if _is_num(ex.get("filament_detected")):
        out["filament_detected"] = 1 if ex["filament_detected"] else 0
    if _is_num(ex.get("filament_detect_enable")):
        out["filament_sensor_on"] = 1 if ex["filament_detect_enable"] else 0
    zt = state.get("ztemperature_sensor") or {}
    if _is_num(zt.get("temperature")):
        out["chamber_temp"] = zt["temperature"]
    fans = {}
    for key, label in _FAN_KEYS:
        pct = _fan_pct(((state.get("fans") or {}).get(key) or {}).get("speed"))
        if pct is not None:
            fans[label] = pct
    if fans:
        out["fans"] = fans
    gm = state.get("gcode_move") or {}
    pos = {a: gm[a] for a in ("x", "y", "z") if _is_num(gm.get(a))}
    if pos:
        out["position"] = pos
    if _is_num(gm.get("speed_mode")) and int(gm["speed_mode"]) in SPEED_MODES:
        out["speed_mode"] = int(gm["speed_mode"])
    ms = state.get("machine_status") or {}
    if isinstance(ms.get("exception_status"), list):
        out["exceptions"] = ms["exception_status"]
    phase = machine_phase(state)
    if phase:
        out["machine"] = phase
    th = state.get("tool_head") or {}
    if isinstance(th.get("homed_axes"), str) and th["homed_axes"]:
        out["homed_axes"] = th["homed_axes"]
    ed = state.get("external_device") or {}
    if isinstance(ed.get("u_disk"), bool):
        out["u_disk"] = ed["u_disk"]
    ci = state.get("canvas_info") or {}
    slots = []
    for canvas in ci.get("canvas_list") or []:
        for t in (canvas or {}).get("tray_list") or []:
            if not isinstance(t, dict) or not _is_num(t.get("tray_id")):
                continue
            slots.append({
                # UI-слот = tray_id+1 — та же нумерация, что у кнопок выгрузки (unload_filament)
                "slot": int(t["tray_id"]) + 1,
                "brand": t.get("brand") or "",
                "name": t.get("filament_name") or "",
                "type": t.get("filament_type") or "",
                "color": t.get("filament_color") or "",
                "temp_min": t.get("min_nozzle_temp"),
                "temp_max": t.get("max_nozzle_temp"),
                "status": t.get("status"),
            })
    if slots:
        out["slots"] = sorted(slots, key=lambda s: s["slot"])
    at = ci.get("active_tray_id")
    if _is_num(at):
        out["active_slot"] = int(at) + 1 if at >= 0 else 0   # 0 = ничего не подаётся
    if isinstance(ci.get("auto_refill"), bool):
        out["auto_refill"] = ci["auto_refill"]
    return out


def _fmt_dur(sec) -> str:
    """Секунды → «2 ч 3 мин» / «4 мин» (как в мини-аппе)."""
    sec = int(sec or 0)
    hh, mm = sec // 3600, (sec % 3600) // 60
    return (f"{hh} ч " if hh else "") + f"{mm} мин"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# Слова print_status.state, означающие «задание идёт» (пауза и прогрев — тоже задание). Тот же
# набор, что BUSY_WORDS драйверов (printerdrv/base.py); импортировать его сюда нельзя — круг.
_JOB_WORDS = ("printing", "paused", "pausing", "resuming", "heating", "preheat", "busy")
MACHINE_PRINTING = 2                # machine_status.status «Печать» (SDK Elegoo)
# Ниже этой доли прогресса остаток по нему не экстраполируем: первые проценты — прогрев,
# выравнивание и юбка, и «прошло × (1 − p) / p» врёт там в разы.
_EXTRAPOLATE_FROM = 0.05


def job_running(state: dict) -> bool:
    """Идёт ли задание по СЫРОЙ телеметрии CC2: слово print_status.state, а если прошивка его
    не прислала — machine_status.status == 2. Остаток времени признаком НЕ служит: у файлов
    Orca прошивка шлёт remaining_time_sec = 0 всю печать (оценку она берёт из заголовка
    ElegooSlicer)."""
    ps = (state or {}).get("print_status") or {}
    word = str(ps.get("state") or "").strip().lower()
    if word:
        return word.startswith(_JOB_WORDS)
    ms = (state or {}).get("machine_status") or {}
    return _is_num(ms.get("status")) and int(ms["status"]) == MACHINE_PRINTING


def _job_progress(state: dict, dur, rem, running: bool) -> float | None:
    """Доля 0..1 или None. Порядок — от точного к общему:

      1. сырой virtual_sdcard.progress — прогресс самого Klipper; его кладёт драйвер из службы
         прошивки сообщества (cc2community.merge_job), стоковая прошивка такого ключа не шлёт;
      2. machine_status.progress (0..100) — ровно его официальный SDK Elegoo показывает
         прогрессом печати (elegoo-link, elegoo_fdm_cc2_message_adapter.cpp);
      3. по времени: прошло / (прошло + осталось) — только при НЕНУЛЕВОМ остатке. С нулевым
         остатком идущее задание выходило «готово на 100 %» с первой минуты (экран TURZX,
         06.10.2026), а printwatch считал печать заказа законченной сразу после старта.
    Для законченного задания остаётся п. 3 как был: 500 / (500 + 0) = 1.0."""
    if running:
        vs = (state.get("virtual_sdcard") or {}).get("progress")
        if _is_num(vs) and 0 <= vs <= 1:
            return float(vs)
        mp = (state.get("machine_status") or {}).get("progress")
        if _is_num(mp) and 0 < mp <= 100:
            return float(mp) / 100.0
    if _is_num(dur) and _is_num(rem) and (rem > 0 or not running) and (dur + rem) > 0:
        return max(0.0, min(1.0, dur / (dur + rem)))
    return None


def enrich_status(state: dict, now: float | None = None) -> dict:
    """Аддитивная нормализация телеметрии Elegoo в форму, которую читает мини-аппа.

    Прошивка Centauri отдаёт прогресс/слой/время под своими ключами (print_status.*,
    machine_status.progress, fans.fan.speed), а фронт ждёт привычные Klipper-имена
    (virtual_sdcard.progress, info.current_layer, top-level elapsed/remaining/eta_at/fan).
    Достраиваем ТОЛЬКО то, что реально есть в телеметрии — ничего не выдумываем (стол/всего-
    слоёв/скорость принтер в этом снимке не присылает, оставляем их пустыми → в UI «—»).
    Единственная оценка — остаток по прогрессу, когда прошивка остатка не знает (см. ниже).
    Возвращает overlay для _deep_merge.
    """
    ps = state.get("print_status") or {}
    overlay: dict = {}
    dur = ps.get("print_duration")          # фактическое время печати, сек
    total = ps.get("total_duration")        # время с запуска задания (с прогревом), сек
    rem = ps.get("remaining_time_sec")      # оценка оставшегося, сек
    running = job_running(state)

    prog = _job_progress(state, dur, rem, running)
    if prog is not None:
        overlay["virtual_sdcard"] = {"progress": prog}

    # Остаток: прошивки, а если она его не знает (файл не из ElegooSlicer) — по прогрессу.
    # remaining_sec — число для экранов, которым нужны минуты (LCD), строки — для мини-аппы.
    left = float(rem) if _is_num(rem) and rem > 0 else None
    if (left is None and running and prog is not None and prog >= _EXTRAPOLATE_FROM
            and prog < 1 and _is_num(dur) and dur > 0):
        left = dur * (1 - prog) / prog

    # Текущий слой (всего слоёв — только если принтер его прислал).
    cl = ps.get("current_layer")
    if _is_num(cl):
        info = {"current_layer": int(cl)}
        tl = ps.get("total_layer") or ps.get("total_layers")
        if _is_num(tl) and tl:
            info["total_layer"] = int(tl)
        overlay["info"] = info

    wall = total if _is_num(total) and total else dur   # «прошло» — по настенным часам, если есть
    if _is_num(wall) and wall > 0:
        overlay["elapsed"] = _fmt_dur(wall)
        overlay["started_at"] = time.strftime("%d.%m · %H:%M",
                                              time.localtime((now or time.time()) - wall))
    if left is not None:
        overlay["remaining_sec"] = round(left)
        overlay["remaining"] = _fmt_dur(left)
        overlay["eta_at"] = time.strftime("%d.%m · %H:%M",
                                          time.localtime((now or time.time()) + left))

    fan = ((state.get("fans") or {}).get("fan") or {}).get("speed")
    if _is_num(fan):
        overlay["fan"] = round(fan)

    # Klipper-совместимый флаг печати: мини-аппа (карточка принтера на админ-хабе) смотрит
    # print_stats.state === 'printing'. Centauri такого поля не шлёт (только print_status.*),
    # поэтому карточка ВСЕГДА показывала «в простое», даже когда принтер печатает. Выводим
    # состояние из слова прошивки (job_running) и «живых» признаков задания — остаток времени
    # или незавершённый прогресс. Нет признаков → поле не добавляем, UI честно покажет
    # «в простое»/«нет связи». Одних «живых» признаков мало: у файла Orca остаток нулевой, и
    # задание с именем файла пропадало из всех экранов.
    active = running or (_is_num(rem) and rem > 0) or (prog is not None and 0.0 < prog < 1.0)
    if active:
        ps_overlay = {"state": "printing"}
        job = ps.get("filename") or ps.get("task_name") or ps.get("taskName") or ps.get("name")
        if isinstance(job, str) and job.strip():
            ps_overlay["filename"] = job.strip()
        overlay["print_stats"] = ps_overlay
    return overlay


# ---- JSON-RPC поверх MQTT: запрос → ответ (формат выяснен захватом обмена ElegooSlicer) ----
def _open(timeout: float, ep: Endpoint | None = None) -> socket.socket:
    """Подключиться к MQTT принтера и авторизоваться. Бросает RuntimeError при отказе."""
    dst = _ep(ep)
    sock = socket.create_connection((dst.ip, MQTT_PORT), timeout=5)
    sock.settimeout(timeout)
    sock.sendall(_connect_pkt(CLIENT_ID, dst.mqtt_user, dst.code))
    ptype, data = _recv_pkt(sock)
    if ptype != 0x20 or len(data) < 2 or data[1] != 0:
        rc = data[1] if (ptype == 0x20 and len(data) >= 2) else "?"
        sock.close()
        raise RuntimeError(f"Принтер отклонил подключение (код {rc})")
    return sock


def request(method: int, params: dict | None = None, timeout: float = 8.0,
            *, wait_data: bool = False, command: bool = False,
            ep: Endpoint | None = None) -> dict:
    """Послать method/params и ждать ответ. {ok, result} | {ok:False, error} | {ok, unconfirmed}.

    Основной путь (разведка 20.07): бот — «привязанный» клиент (см. CHANNEL_ID), принтер шлёт
    ему unicast-ответы на любой запрос — свет/пауза/файлы/история приходят С ПОДТВЕРЖДЕНИЕМ,
    как у ElegooSlicer, через постоянное соединение подписчика. Если привязки сейчас нет
    (подписчик переподключается / принтер только что включился) — фолбэк на разовое соединение
    (_request_transient), где команды уходят «вслепую» (unconfirmed).

    ep — кому адресован запрос; None = принтер по умолчанию (из .env)."""
    return session_for(ep).request(method, params, timeout, wait_data=wait_data,
                                   command=command, ep=ep)


def _request_transient(method: int, params: dict | None = None, timeout: float = 8.0,
                       *, wait_data: bool = False, command: bool = False,
                       ep: Endpoint | None = None) -> dict:
    """Разовое соединение connect→subscribe→request→disconnect — ФОЛБЭК, когда привязки нет.
    Команды уходят «вслепую»: без привязки принтер их выполняет, но unicast-подтверждение не шлёт
    (→ unconfirmed). Эхо api_request отфильтровано по топику (фидбэк edde64e4)."""
    dst = _ep(ep)
    sn = dst.serial
    try:
        sock = _open(timeout, dst)
    except OSError as e:
        return {"ok": False, "error": f"Нет связи с принтером: {e}"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    try:
        sock.sendall(_subscribe_pkt(f"elegoo/{sn}/#"))     # широко — ответ ловим по id
        sub_deadline = time.time() + 3
        while time.time() < sub_deadline:                  # ждём SUBACK, чтобы не упустить ответ
            try:
                ptype, _d = _recv_pkt(sock)
            except socket.timeout:
                break
            if ptype is not None and (ptype & 0xF0) == 0x90:   # SUBACK
                break
        rid = next(_req_counter)
        body = json.dumps({"id": rid, "method": method, "params": params or {}}).encode()
        sock.sendall(_publish_pkt(f"elegoo/{sn}/{CLIENT_ID}/api_request", body))
        ack = None   # пустой ACK при wait_data — фолбэк, если данные так и не пришли
        end = time.time() + timeout
        while time.time() < end:
            try:
                ptype, data = _recv_pkt(sock)
            except socket.timeout:
                break
            if ptype is None:
                break
            if (ptype & 0xF0) != 0x30:
                continue
            topic, obj = _parse_publish(ptype, data)
            if not obj:
                continue
            if topic and topic.endswith("/api_request"):
                continue   # эхо запросов (наших и чужих клиентов) — это НЕ ответ принтера
            # данные выборки могут прийти отдельным сообщением с тем же method, но другим id
            if obj.get("id") != rid and not (wait_data and obj.get("method") == method):
                continue
            result = obj.get("result") or {}
            err = result.get("error_code", 0)
            if err:
                return {"ok": False, "err_code": err, "error": error_text(err)}
            meaningful = any(k != "error_code" for k in result)
            if meaningful or not wait_data:
                return {"ok": True, "result": result, "method": obj.get("method")}
            ack = {"ok": True, "result": result, "method": obj.get("method")}
        if ack is not None:
            return ack             # дождались только ACK — история/список реально пусты
        if command:
            # команда ушла в брокер; CC2 выполняет её, но unicast-подтверждение шлёт только
            # привязанному клиенту-слайсеру (см. докстринг) — честно говорим «без подтверждения»
            return {"ok": True, "unconfirmed": True, "result": {}}
        return {"ok": False, "error": "Принтер не ответил вовремя (unicast-ответы он шлёт только "
                                      "привязанному клиенту-слайсеру)"}
    except Exception as e:  # noqa: BLE001
        log.warning("elegoo request method=%s failed", method, exc_info=True)
        return {"ok": False, "error": f"Ошибка связи: {e}"}
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


def _parse_file_list(result: dict) -> list[dict]:
    """Разбор ответа method 1044 в список файлов — общий для активного запроса и пассивного сбора.
    Поля разведаны захватом обмена ElegooSlicer↔принтер 12.07 (inventory_master_plan.md §5.1.1):
    total_filament_used — граммы (оценка слайсера, зашитая в файл; у принтера нет весов),
    color_map — [{color:#hex, name:материал, t:экструдер}] (мультицвет = несколько), print_time —
    оценка печати в секундах."""
    raw = (result or {}).get("file_list", [])
    return [{"name": f.get("filename"), "size": f.get("size", 0),
             "print_time": f.get("print_time", 0), "layer": f.get("layer", 0),
             "grams": f.get("total_filament_used", 0) or 0,
             # unix-время появления файла на принтере — для сортировки «сначала новые»
             "created": f.get("create_time", 0) or 0,
             "colors": [{"hex": c.get("color", ""), "type": c.get("name", ""),
                         "extruder": c.get("t")} for c in (f.get("color_map") or [])]}
            for f in raw if f.get("type") == "file" and f.get("filename")]


# Где лежит файл: внутренняя память или флешка. Имена носителей — из SDK (START_PRINT шлёт
# storage_media «local» / «u-disk» / «sd-card»); у CC2 слота SD нет, флешка — USB-порт сзади.
STORAGES = ("local", "u-disk")


def _storage(storage: str | None) -> str:
    s = (storage or "local").strip().lower()
    return s if s in STORAGES else "local"


def list_files(timeout: float = 8.0, ep: Endpoint | None = None, *,
               storage: str = "local") -> dict:
    """Список gcode-файлов принтера (method 1044): внутренняя память или флешка. Только чтение."""
    r = request(METHOD_LIST_FILES, {"storage_media": _storage(storage), "offset": 0, "limit": 100},
                timeout, wait_data=True, ep=ep)
    if not r.get("ok"):
        return r
    return {"ok": True, "files": _parse_file_list(r.get("result") or {})}


def normalize_job_name(name: str | None) -> str:
    """Каноничный ключ имени задания печати — ЕДИНЫЙ для сравнения «журнальный label ↔ task_name
    истории принтера (1036) ↔ имя gcode-файла (1044)» и для ключа авто-кэша таймлапсов.

    Зачем: имя задания приходит из РАЗНЫХ источников — телеметрия `print_status.filename` (из неё
    складывается label записи журнала), история заданий `task_name`, список файлов `filename`.
    Байт-в-байт сравнение хрупко: захваты (docs/captures) показывают, что history.task_name несёт
    расширение `.gcode` (напр. «ECC2_0.4_Body_Elegoo PLA Matte _0.12_3h49m.gcode»), но другой
    источник/прошивка легко отдаст то же имя без расширения, с путём-префиксом, с иными пробелами
    или в другом регистре — и тогда таймлапс «Овечки Тимми» никогда не сматчится (2bc57e3e).

    Нормализуем консервативно, СОХРАНЯЯ существующие точные совпадения (равные строки → равные
    ключи): basename (срезаем путь и обратные слэши Windows) → без хвоста `.gcode` → NFC (имена с
    не-ASCII: китайские иероглифы приходят и из ФС, и из БД истории — разная Unicode-нормализация)
    → схлопнутые пробелы → casefold (регистронезависимо). Пусто → пустая строка (не матчим
    «ничего» с «ничем»)."""
    s = (name or "").strip()
    if not s:
        return ""
    s = s.replace("\\", "/").rsplit("/", 1)[-1]       # basename: убираем возможный путь
    if s.lower().endswith(".gcode"):
        s = s[:-len(".gcode")]
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"\s+", " ", s).strip()                # любые пробелы → один, края обрезаны
    return s.casefold()


def _find_timelapse_url(d: dict) -> str:
    """Достать URL таймлапса из записи истории заданий. Предпочитаем задокументированный ключ
    time_lapse_video_url (снят захватом обмена ElegooSlicer); если прошивка назвала иначе —
    толерантно ищем http-ссылку в ключе со словом lapse/video. Домыслов о протоколе нет: метод
    1036 и наличие ссылки подтверждены в docs/printer-integration.md, здесь лишь устойчивый разбор."""
    for k in ("time_lapse_video_url", "timeLapseVideoUrl", "timelapse_url", "video_url"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for k, v in d.items():
        if isinstance(v, str) and v.startswith("http") and ("lapse" in str(k).lower() or "video" in str(k).lower()):
            return v.strip()
    return ""


def _parse_history(result: dict) -> list[dict]:
    """Разбор ответа method 1036 в список заданий — общий для активного запроса и пассивного сбора.
    history_task_list — реальный ключ-обёртка (разведан захватом 12.07); begin/end_time → реальная
    длительность (сек), task_status 1=успех/2=стоп-брак."""
    raw = None
    for k in ("history_task_list", "history_list", "task_list", "file_list", "list", "data",
              "records", "tasks"):
        if isinstance((result or {}).get(k), list):
            raw = result[k]
            break
    if raw is None:                                   # имя ключа-обёртки не разведано — берём первый список
        raw = next((v for v in (result or {}).values() if isinstance(v, list)), [])
    jobs = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        begin = it.get("begin_time") or it.get("beginTime") or it.get("start_time") or 0
        end = it.get("end_time") or it.get("endTime") or it.get("finish_time") or 0
        st = it.get("task_status")                    # 1=успех, 2=стоп/брак (разведано 12.07)
        # Статус видео таймлапса (enum lan_service_web): 0=кадры не снимались, 1=кадры есть, но mp4
        # ЕЩЁ НЕ СОБРАН (нужен METHOD_TIMELAPSE_EXPORT), 2=mp4 готов и скачивается, 3=сборка
        # провалилась. Без него бот качал несуществующие mp4 несобранных роликов, а принтер на
        # неверное имя файла молчит вечно (кейс «журнал без таймлапсов», 4c6cd9e8).
        tl_st = it.get("time_lapse_video_status")
        if tl_st is None:
            tl_st = it.get("timeLapseVideoStatus")
        jobs.append({
            "name": it.get("task_name") or it.get("filename") or it.get("name") or "",
            "time_lapse_video_url": _find_timelapse_url(it),
            "time_lapse_video_status": tl_st if isinstance(tl_st, int) else None,
            "begin_time": begin,
            "end_time": end,
            # реальная длительность печати (сек) — end−begin; для сравнения оценка/факт (print_samples)
            "duration_sec": int(end - begin) if (begin and end and end >= begin) else 0,
            "status": st,
            "ok": (st == 1) if st is not None else None,   # None = статус неизвестен
        })
    return jobs


def job_history(timeout: float = 8.0, ep: Endpoint | None = None) -> dict:
    """История заданий печати (method 1036) с ссылками на таймлапс. Только чтение; best-effort —
    при неподдержке/ошибке вернёт {ok:False}."""
    r = request(METHOD_JOB_HISTORY, {"storage_media": "local", "offset": 0, "limit": 50}, timeout,
                wait_data=True, ep=ep)
    if not r.get("ok"):
        return r
    return {"ok": True, "jobs": _parse_history(r.get("result") or {})}


def generate_timelapse(url: str, timeout: float = 150.0, ep: Endpoint | None = None) -> dict:
    """Собрать mp4 таймлапса из кадров задания (method 1051, params {"url": time_lapse_video_url
    записи истории}). Принтер рендерит видео (минуты) и отвечает, когда готово; в ответе — путь
    готового файла для /download. Схема снята с lan_service_web ElegooSlicer: сам слайсер зовёт
    ровно это перед скачиванием «несобранного» ролика (задача 4c6cd9e8). Возвращает {ok, url?};
    пустой url при ok — сборка ушла, но готовый путь узнаём повторным job_history."""
    v = (url or "").strip()
    if not v:
        return {"ok": False, "error": "пустой url таймлапса"}
    r = request(METHOD_TIMELAPSE_EXPORT, {"url": v}, timeout, wait_data=True, ep=ep)
    if not r.get("ok"):
        return r
    res = r.get("result") or {}
    out = ""
    for k in ("url", "Url", "video_url", "time_lapse_video_url"):
        if isinstance(res.get(k), str) and res[k].strip():
            out = res[k].strip()
            break
    return {"ok": True, "url": out}


# ─── Пассивный сбор данных принтера (Путь A мастер-плана учёта) ──────────────────────────────
# Фоновый подписчик уже слушает elegoo/#, поэтому видит ответы принтера на 1044/1036, когда мастер
# открыл экран принтера в ElegooSlicer. При config.ELEGOO_PASSIVE_HARVEST кэшируем их (имя файла →
# граммы/время/статус) для обогащения журнала печатей реальными данными. Read-only, в память,
# best-effort; ключ разведан захватом 12.07 (inventory_master_plan.md §5.1.1).
# Ответы принтера со «статусной» начинкой: 1002 — полный статус (свет/вентиляторы/позиция/
# датчик прутка), 2005 — слоты CANVAS. Пока свой запрос-ответ нам недоступен (привязка клиента
# не разведана, см. docs/captures/README), подбираем эти ответы пассивно из обмена
# ElegooSlicer↔принтер: мастер открыл вкладку принтера в слайсере → наш живой кэш и экран
# «Печать» получают слоты и полный статус. Мержится всегда (read-only, та же форма, что
# api_status), в отличие от харвеста файлов/истории, который гейтится ELEGOO_PASSIVE_HARVEST.
_STATUS_RESPONSE_METHODS = (1002, 2005)


def harvested_info(filename: str, ep: Endpoint | None = None) -> dict | None:
    """Пассивно собранные данные принтера по имени gcode-файла: что известно из 1044 (grams/
    print_time/colors) и 1036 (duration_sec/status/ok). None — если по файлу ничего не собрано."""
    return session_for(ep).harvested_info(filename)


def harvested_files(ep: Endpoint | None = None) -> dict | None:
    """Пассивно собранный список файлов принтера — фолбэк, когда активный 1044 не отвечает
    (принтер шлёт unicast только привязанному слайсеру). None — ничего не собрано."""
    return session_for(ep).harvested_files()


# Троттлинг «live-граммов текущей печати»: граммы файла — это «Filament Consumption» из Web UI
# принтера (оценка слайсера, зашитая в gcode). Они НЕ меняются, поэтому кэшируем агрессивно, а
# дорогой сетевой list_files (1044, до 10 с, может досить принтер — больная тема проекта) зовём не
# чаще раза в _FILE_GRAMS_TTL. Экран «Печать» поллит статус каждые ~3 с, а грамм на файл нужен
# именно там — без троттлинга это был бы шторм 1044 на каждый кадр.
_FILE_GRAMS_TTL = 90.0


def file_grams(filename: str, ep: Endpoint | None = None) -> float | None:
    """Граммы («Filament Consumption» из Web UI принтера) для gcode-файла по имени. Источники от
    дешёвого к дорогому:
      1) пассивный сбор (harvested_info) — бесплатно, из фонового MQTT-подписчика (когда мастер
         держит вкладку принтера в ElegooSlicer);
      2) собственный снимок активного list_files под ТРОТТЛИНГОМ: сетевой 1044 зовём не чаще раза
         в _FILE_GRAMS_TTL, весь ответ держим в кэше {имя→граммы}. В пределах TTL отвечаем из кэша
         (файла в снимке нет → None, повторно принтер НЕ дёргаем).
    None — если ни один источник граммов не дал. Блокирующая (сеть) — звать в asyncio.to_thread."""
    return session_for(ep).file_grams(filename)


def latest_timelapse_url(job_name: str | None = None, timeout: float = 8.0,
                         ep: Endpoint | None = None) -> dict:
    """Best-effort URL таймлапса последней (или совпадающей по имени) печати. {ok,url,name}|{ok:False}.
    Ролики с несобранным mp4 (time_lapse_video_status≠2) пропускаем: их файла ещё НЕТ, а принтер
    на несуществующее имя молчит до таймаута — вызывающие зря бы висели (4c6cd9e8). Их соберёт и
    привяжет свип printwatch (generate_timelapse), клиенту дошлёт deliver_late_timelapses."""
    r = job_history(timeout, ep)
    if not r.get("ok"):
        return r
    jobs = [j for j in r.get("jobs", []) if j.get("time_lapse_video_url")
            and j.get("time_lapse_video_status") in (2, None)]
    if not jobs:
        return {"ok": False, "error": "Таймлапс не найден в истории заданий принтера"}
    chosen = None
    if job_name:
        nm = normalize_job_name(job_name)             # устойчивое сравнение (см. normalize_job_name)
        chosen = next((j for j in jobs if nm and normalize_job_name(j.get("name")) == nm), None)
    if chosen is None:
        chosen = max(jobs, key=lambda j: j.get("end_time") or 0)    # самая свежая
    return {"ok": True, "url": chosen["time_lapse_video_url"], "name": chosen.get("name", "")}


def start_print(filename: str, *, tray_id: int = 1, filament_t: int = 3,
                canvas_id: int = 0, bedlevel: bool = False, plate: str = "A",
                slot_map: list | None = None, printer_check: bool = True,
                storage: str = "local",
                timeout: float = 8.0, ep: Endpoint | None = None) -> dict:
    """СТАРТ ПЕЧАТИ файла на принтере (method 1020). Вызывать ТОЛЬКО по явному действию админа.
    Config точно повторяет то, что шлёт ElegooSlicer — проверено ЖИВЫМ захватом реального старта
    20.07 (docs/captures): `{"bedlevel_force":false,"delay_video":true,"print_layout":"A",
    "printer_check":true,"slot_map":[{"canvas_id":0,"t":2,"tray_id":2}]}` для PETG со слота 3.

    printer_check=True — как у слайсера. В SDK вендора это поле называется autoBedLeveling, а
    bedlevel_force — bedLevelForce (обязательная полная калибровка).
    bedlevel — форс auto-leveling (в gcode CC2 и так есть BED_MESH_CALIBRATE, слайсер шлёт False).
    plate — пластина (print_layout: "A" — гладкая сторона, "B" — текстурная; у каждой своя
    сохранённая сетка). slot_map — карта «филамент gcode → слот хаба» [{canvas_id, t, tray_id}]
    (t — индекс филамента в gcode, tray_id — слот−1); иначе из tray_id/filament_t (моноцвет).
    storage — где лежит файл: "local" или "u-disk" (флешка)."""
    if not filename:
        return {"ok": False, "error": "Не указан файл"}
    sm = slot_map or [{"canvas_id": canvas_id, "t": filament_t, "tray_id": tray_id}]
    cfg = {"bedlevel_force": bool(bedlevel), "delay_video": True, "print_layout": plate or "A",
           "printer_check": bool(printer_check), "slot_map": sm}
    return request(METHOD_START_PRINT,
                   {"config": cfg, "filename": filename, "storage_media": _storage(storage)},
                   min(timeout, 4.0), command=True, ep=ep)


def set_speed_mode(mode: int, timeout: float = 4.0, ep: Endpoint | None = None) -> dict:
    """Режим скорости печати (method 1031, {"mode": 0..3}) — стоковый аналог «скорость %».
    Только по явному действию мастера; текущий режим принтер вещает в gcode_move.speed_mode."""
    try:
        m = int(mode)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный режим скорости"}
    if m not in SPEED_MODES:
        return {"ok": False, "error": "режим скорости должен быть 0..3"}
    return request(METHOD_SPEED_MODE, {"mode": m}, min(timeout, 3.0), command=True, ep=ep)


def set_auto_refill(on: bool, timeout: float = 4.0, ep: Endpoint | None = None) -> dict:
    """Автодозаправка CANVAS (method 2004, {"auto_refill": bool}): кончилась катушка — хаб
    сам переключается на слот с тем же материалом и цветом. Состояние — canvas_info.auto_refill."""
    return request(METHOD_AUTO_REFILL, {"auto_refill": bool(on)}, min(timeout, 3.0),
                   command=True, ep=ep)


def control_method(action: str) -> int | None:
    """Method-код команды управления (pause/resume/stop/light) или None, если он не задан в .env."""
    env = (_CONTROL_ACTIONS.get(action) or (None,))[0]
    return _method_env(env) if env else None


def control(action: str, *, on: bool = True, timeout: float = 8.0,
            ep: Endpoint | None = None) -> dict:
    """Разовая команда управления печатью по явному действию админа: pause|resume|stop|light.

    Method-коды берутся из окружения (ELEGOO_METHOD_PAUSE=1021/RESUME=1023/STOP=1022/LIGHT=1029 —
    сняты захватом обмена ElegooSlicer 2026-06-04). Пока код не задан — НЕ шлём ничего наугад,
    а возвращаем понятную ошибку. Свет передаёт params {"power": 1|0} (как ElegooSlicer); пауза/
    продолжить/стоп — без params (пустой объект)."""
    meta = _CONTROL_ACTIONS.get(action)
    if not meta:
        return {"ok": False, "error": f"Неизвестная команда: {action}"}
    env_name, label = meta
    method = _method_env(env_name)
    if method is None:
        return {"ok": False, "error": (
            f"Команда «{label}» ещё не разведана для этого принтера. Задайте {env_name} "
            "в .env (method-код из захвата ElegooSlicer), затем перезапустите бота.")}
    params: dict = {}
    if action == "light":
        params = {"power": 1 if on else 0}
    return request(method, params, min(timeout, 3.0), command=True, ep=ep)


def unload_filament(slot: int, timeout: float = 12.0, ep: Endpoint | None = None) -> dict:
    """Выгрузка филамента из слота (1..4) хаба. Только по явному действию админа.

    Method-код берётся из .env ELEGOO_METHOD_UNLOAD (2002 — снят захватом ElegooSlicer 2026-06-04).
    ВАЖНО: UI-слот и tray_id сдвинуты — `tray_id = slot − 1` (слот 1 → tray_id 0 … слот 4 → 3),
    canvas_id=0 (один хаб). Подтверждено ответом 2005 (tray_id 0 = филамент в слоте 1)."""
    method = _method_env("ELEGOO_METHOD_UNLOAD")
    if method is None:
        return {"ok": False, "error": ("Команда «Выгрузка» ещё не разведана для этого принтера. "
                                       "Задайте ELEGOO_METHOD_UNLOAD в .env, затем перезапустите бота.")}
    try:
        tray = int(slot) - 1
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный слот"}
    if not (0 <= tray <= 3):
        return {"ok": False, "error": "слот вне диапазона 1..4"}
    return request(method, {"canvas_id": 0, "tray_id": tray}, min(timeout, 3.0), command=True, ep=ep)


# ─── Прогрев / оси / вентиляторы (коды разведаны захватом 20.07, docs/captures/) ──────────────
# Все команды подтверждены живьём error_code:0. Требуют привязки (request сам выберет путь).
METHOD_SET_TEMP = 1028             # {"extruder": T} | {"heater_bed": T}; T в °C, 0 = выключить
METHOD_HOME = 1026                 # {"homed_axes": "xyz"} (или подмножество осей)
METHOD_MOVE = 1027                 # {"axes": "x|y|z", "distance": ±мм}
METHOD_FAN = 1030                  # {"fan"|"aux_fan"|"box_fan": 0..255}

# Разумные потолки target-температур (защита от опечатки/переброса). CC2: сопло до 320, стол до 110.
_TEMP_MAX = {"extruder": 320, "heater_bed": 110}
# Пользовательские вентиляторы, которыми управляет UI (системные controller/heater не трогаем).
_FAN_UI = {"model": "fan", "aux": "aux_fan", "box": "box_fan"}


def set_temperature(target: str, value: int, timeout: float = 4.0,
                    ep: Endpoint | None = None) -> dict:
    """Задать целевую температуру: target='extruder'|'heater_bed', value °C (0 = выключить нагрев).
    Method 1028 (разведан 20.07). Для «Подготовить к печати» и ручного прогрева."""
    if target not in _TEMP_MAX:
        return {"ok": False, "error": "target должен быть 'extruder' или 'heater_bed'"}
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверная температура"}
    if not (0 <= v <= _TEMP_MAX[target]):
        return {"ok": False, "error": f"температура вне диапазона 0..{_TEMP_MAX[target]}°"}
    return request(METHOD_SET_TEMP, {target: v}, min(timeout, 3.0), command=True, ep=ep)


def home(axes: str = "xyz", timeout: float = 30.0, ep: Endpoint | None = None) -> dict:
    """Парковка осей (method 1026). axes — подмножество 'xyz' (по умолчанию все)."""
    ax = "".join(c for c in (axes or "").lower() if c in "xyz") or "xyz"
    return request(METHOD_HOME, {"homed_axes": ax}, min(timeout, 30.0), command=True, ep=ep)


def move_axis(axis: str, distance: float, timeout: float = 8.0,
              ep: Endpoint | None = None) -> dict:
    """Переместить одну ось на distance мм (method 1027). axis='x'|'y'|'z', distance ±.
    Перед перемещением оси должны быть запаркованы (иначе принтер отклонит — это на нём)."""
    ax = (axis or "").lower()
    if ax not in ("x", "y", "z"):
        return {"ok": False, "error": "ось должна быть x, y или z"}
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверное расстояние"}
    if not (-300 <= d <= 300) or d == 0:
        return {"ok": False, "error": "расстояние вне диапазона ±300 мм"}
    return request(METHOD_MOVE, {"axes": ax, "distance": d}, min(timeout, 5.0), command=True, ep=ep)


def set_fan(which: str, pct: int, timeout: float = 4.0, ep: Endpoint | None = None) -> dict:
    """Скорость пользовательского вентилятора в % (method 1030). which='model'|'aux'|'box'.
    UI-проценты 0..100 переводим в шкалу прошивки 0..255 (разведано: 100%→255, 50%→127.5)."""
    key = _FAN_UI.get(which)
    if not key:
        return {"ok": False, "error": "вентилятор должен быть 'model', 'aux' или 'box'"}
    try:
        p = max(0, min(100, int(round(float(pct)))))
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный процент"}
    return request(METHOD_FAN, {key: round(p * 255 / 100, 1)}, min(timeout, 3.0), command=True,
                   ep=ep)


# ─── Заливка gcode на принтер (HTTP, разведана 20.07 — docs/captures/upload-protocol.md) ────
# Единственное, чего бот раньше не умел: файл приходилось класть слайсером вручную.
# Протокол: последовательные PUT одного и того же пути, чанк за чанком, смещение в Content-Range.
# НЕ multipart и не POST. Чанки строго по порядку — сервер ведёт свой курсор (ошибка 9000 =
# FileOffsetMismatch), параллелить нельзя. Отдельного «commit» нет: последний чанк завершает
# заливку, принтер сам сверяет X-File-MD5 (ошибка 9004 = MD5CheckFailed).
UPLOAD_PATHS = {"local": "/upload", "u-disk": "/upload/udisk", "sd-card": "/upload/sdcard"}
UPLOAD_CHUNK = 1024 * 1024          # 1 МиБ — как в веб-интерфейсе слайсера
UPLOAD_RETRIES = 3                  # повторов на чанк (слайсер делает столько же, пауза 0.2 с)
# ВАЖНО (проверено живьём 20.07): все чанки идут по ОДНОМУ keep-alive соединению. Если открывать
# новое TCP на каждый чанк, прошивка (libhv) с четвёртого отвечает 429 Too Many Requests — и это
# не лечится паузами (пробовал до 1.5 с). Браузер шлёт fetch по одному соединению, поэтому в
# веб-интерфейсе проблемы нет. С keep-alive 6 МБ уходят за ~1 с без единого отказа.
UPLOAD_THROTTLE_WAIT = 2.0          # пауза перед повтором, если 429 всё-таки прилетел
# Коды ошибок заливки из прошивки (enum веб-интерфейса) — расшифровываем мастеру по-человечески.
UPLOAD_ERRORS = {
    1000: "принтер отверг код доступа (X-Token)",
    9000: "рассинхрон смещения — чанки ушли не по порядку",
    9001: "принтер не смог открыть файл на запись",
    9002: "ошибка записи на принтере",
    9003: "ошибка позиционирования в файле",
    9004: "не сошлась контрольная сумма MD5",
    9007: "путь загрузки не существует",
    9010: "не удалось удалить одноимённый файл",
    9999: "неизвестная ошибка принтера",
}


def _upload_chunk(conn, endpoint: str, headers: dict, body: bytes) -> tuple[int, dict]:
    """Один PUT-чанк в УЖЕ открытое соединение → (http_status, json-ответ). Тело читаем всегда:
    принтер кладёт в него error_code, а на троттлинг отвечает 429 обычным HTML."""
    conn.request("PUT", endpoint, body=body, headers=headers)
    resp = conn.getresponse()
    status, raw = resp.status, resp.read()
    try:
        return status, (json.loads(raw.decode("utf-8", "replace")) or {})
    except (ValueError, AttributeError):
        return status, {}


def upload_gcode(path, *, name: str | None = None, storage: str = "local",
                 chunk: int = UPLOAD_CHUNK, timeout: float = 60.0, progress=None,
                 ep: Endpoint | None = None) -> dict:
    """Залить .gcode на принтер. {ok:True, name} | {ok:False, error}. Блокирующая (звать в to_thread).

    name — имя файла НА ПРИНТЕРЕ; по умолчанию имя исходного файла. Нужен, когда заливаем из
    временной копии (иначе на принтер уехало бы служебное имя вида upload_<таймштамп>_…).
    progress — необязательный колбэк(отправлено_байт, всего_байт) для отображения прогресса."""
    dst = _ep(ep)
    if not _configured(dst):
        return {"ok": False, "error": "Принтер Elegoo не настроен"}
    ip, code = dst.ip, dst.code
    endpoint = UPLOAD_PATHS.get(storage)
    if not endpoint:
        return {"ok": False, "error": f"Неизвестное хранилище: {storage}"}
    try:
        data = open(path, "rb").read()          # noqa: SIM115 — читаем целиком, файлы до ~50 МБ
    except OSError as e:
        return {"ok": False, "error": f"Файл не читается: {e}"}
    total = len(data)
    if not total:
        return {"ok": False, "error": "Файл пуст"}
    name = os.path.basename(str(name or path))
    if not name.lower().endswith(".gcode"):
        # прошивка принимает только gcode (веб-интерфейс проверяет тем же условием)
        return {"ok": False, "error": "Принтер принимает только .gcode"}
    md5 = hashlib.md5(data).hexdigest()          # noqa: S324 — контрольная сумма протокола, не крипто
    quoted = urllib.parse.quote(name, safe="")
    conn = http.client.HTTPConnection(ip, 80, timeout=timeout)
    sent = 0
    try:
        while sent < total:
            end = min(sent + chunk, total)
            body = data[sent:end]
            headers = {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {sent}-{end - 1}/{total}",
                "X-Token": code,
                "X-File-Name": quoted,
                "X-File-MD5": md5,
            }
            last_err = ""
            for attempt in range(UPLOAD_RETRIES + 1):
                try:
                    status, res = _upload_chunk(conn, endpoint, headers, body)
                    ec = res.get("error_code", 0)
                    if status == 200 and ec == 0:
                        last_err = ""
                        break
                    if status == 429:            # соединение всё же затроттлили — ждём и повторяем
                        last_err = "принтер занят (429)"
                        conn.close()
                        time.sleep(UPLOAD_THROTTLE_WAIT * (attempt + 1))
                        conn = http.client.HTTPConnection(ip, 80, timeout=timeout)
                        continue
                    last_err = UPLOAD_ERRORS.get(ec, f"HTTP {status}, код {ec}")
                    if ec in (1000, 9004, 9007):  # смысла повторять нет — токен/сумма/путь
                        return {"ok": False, "error": f"Принтер отклонил загрузку: {last_err}"}
                except (http.client.HTTPException, OSError) as e:
                    last_err = f"{type(e).__name__}"
                    conn.close()                 # соединение испорчено — переоткрываем
                    conn = http.client.HTTPConnection(ip, 80, timeout=timeout)
                if attempt < UPLOAD_RETRIES:
                    time.sleep(0.2)
            if last_err:
                return {"ok": False, "error": f"Загрузка прервалась на {sent}/{total} Б: {last_err}"}
            sent = end
            if progress:
                try:
                    progress(sent, total)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("Elegoo: файл «%s» залит на принтер (%d Б)", name, total)
    return {"ok": True, "name": name, "size": total}


def load_filament(slot: int, timeout: float = 12.0, ep: Endpoint | None = None) -> dict:
    """Загрузка филамента в слот (1..4) хаба — method 2001, пара к 2002 (разведано 20.07).
    Раньше числилась «код НЕ захвачен»: в UI была только выгрузка."""
    try:
        tray = int(slot) - 1
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный слот"}
    if not (0 <= tray <= 3):
        return {"ok": False, "error": "слот вне диапазона 1..4"}
    return request(2001, {"canvas_id": 0, "tray_id": tray}, min(timeout, 3.0), command=True, ep=ep)


def delete_file(filename, storage: str = "local", timeout: float = 6.0,
                ep: Endpoint | None = None) -> dict:
    """Удалить файл(ы) в памяти принтера — method 1047.

    ВАЖНО: file_path — МАССИВ имён, даже для одного файла (строка → error_code 1003
    InvalidParameter; разведано по коду веб-интерфейса 20.07). Имя берётся ровно таким, каким
    пришло в списке 1044 — без каталога и ведущего слеша."""
    names = [filename] if isinstance(filename, str) else list(filename or [])
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names:
        return {"ok": False, "error": "не указан файл"}
    return request(1047, {"storage_media": _storage(storage), "file_path": names}, timeout,
                   command=True, ep=ep)


def file_info(filename: str, timeout: float = 6.0, ep: Endpoint | None = None, *,
              storage: str = "local") -> dict:
    """Детали одного gcode-файла по имени (method 1046): color_map/print_time/грамм/слои.
    Для карточки файла перед стартом печати. Требует привязки."""
    if not (filename or "").strip():
        return {"ok": False, "error": "не указан файл"}
    r = request(1046, {"storage_media": _storage(storage), "filename": filename.strip()}, timeout,
                wait_data=True, ep=ep)
    if not r.get("ok"):
        return r
    res = r.get("result") or {}
    return {"ok": True, "file": {
        "name": res.get("filename"), "size": res.get("size", 0),
        "print_time": res.get("print_time", 0), "layer": res.get("layer", 0),
        "grams": res.get("total_filament_used", 0) or 0,
        "colors": [{"hex": c.get("color", ""), "type": c.get("name", ""), "extruder": c.get("t")}
                   for c in (res.get("color_map") or [])]}}
