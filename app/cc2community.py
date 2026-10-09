"""Прошивка сообщества для Centauri Carbon 2: автоопределение и клиент её API.

Проект `Neme77/centauri-carbon-2-community` (GPL-3.0) ставит на принтер рут и свою службу
CC2 Control (порт 8081). Она говорит с внутренним сокетом Klipper `/tmp/elegoo_uds`, которого
снаружи нет, и поэтому умеет то, чего стоковый MQTT не даёт вовсе: снять деталь на ходу, карта
стола, замер винтов, живой Z-offset, скорость и поток в %, консоль G-code, правка слота хаба.
Разбор фич: docs/Перспективные разработки/cc2-community-features-2026-10-03.md.

Их код сюда НЕ скопирован ни строкой: GPL сделал бы производной весь бот. Мы ходим к их службе
по HTTP, как к любой другой машине в сети, — это обычное взаимодействие программ, а не
заимствование. Отсюда же правило модуля: всё, что их служба проверяет сама (пределы ходов,
«только в простое», «только во время печати»), мы повторяем у себя лишь там, где без этого
интерфейс соврал бы мастеру. Последнее слово всегда за принтером.

Автоопределение — `GET :8081/api/v1/system/info` («discovery API v1», cc2-control/docs/API.md
их репозитория). Ответ кэшируется на адрес; проба идёт в фоновом потоке, чтобы опрос статуса
раз в три секунды не ждал сетевого таймаута, когда прошивки нет. Стоковый принтер на 8081 не
слушает вовсе: соединение отбивается ядром мгновенно, никакая служба принтера его не видит.

Сеть — голый http.client, а не urllib: дочерние процессы бывают с HTTP(S)_PROXY локального
туннеля в окружении, и urllib честно пустил бы запрос к принтеру через чужой сервер.
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import statistics
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PORT = 8081
TIMEOUT = 3.0
PROBE_OK_TTL = 300.0        # прошивка найдена — перепроверяем раз в 5 минут (её могли снести)
PROBE_FAIL_TTL = 600.0      # не найдена — раз в 10 минут (её могли поставить)
# Выключатель автопробы. Тесты гасят его автофикстурой: фоновый поток, стучащийся в выдуманный
# адрес карточки, пережил бы тест.
AUTO_PROBE = True

PLATFORM = "cc2-community"


@dataclass(frozen=True)
class Info:
    """Что ответила служба сообщества о себе."""
    firmware: str = ""
    control: str = ""
    api_version: int = 0
    caps: dict = field(default_factory=dict)
    port: int = PORT

    def as_dict(self) -> dict:
        return {"firmware": self.firmware, "control": self.control,
                "api_version": self.api_version, "port": self.port,
                "caps": dict(self.caps)}


_lock = threading.Lock()
_cache: dict[str, tuple[float, Info | None]] = {}
_probing: set[str] = set()
# Сдвиг Z за сессию — по адресу. Их служба сама его не помнит (в их интерфейсе он живёт до
# перезагрузки страницы), а откатить «всё, что накрутили», без суммы нельзя. Живёт до
# перезапуска бота — ровно как SET_GCODE_OFFSET живёт до перезапуска Klipper.
_zoffset: dict[str, float] = {}


def _host(host: str) -> str:
    h = (host or "").strip()
    if "://" in h:
        h = h.split("://", 1)[1]
    return h.split("/", 1)[0].split(":", 1)[0]


# ─── HTTP ────────────────────────────────────────────────────────────────────────────────────

class CommunityError(Exception):
    """Служба ответила отказом или недоступна — текст годится в тост."""


class CommunityUnreachable(CommunityError):
    """Соединение со службой не установилось: запрос до неё точно не дошёл, и сделать то же
    самое стоковым путём можно без риска сделать это дважды."""


def _http(host: str, method: str, path: str, body: str | None = None,
          timeout: float = TIMEOUT, port: int = PORT):
    h = _host(host)
    if not h:
        raise CommunityError("не задан адрес принтера")
    conn = http.client.HTTPConnection(h, port, timeout=timeout)
    try:
        headers = {"Connection": "close"}
        data = None
        if body is not None:
            data = body.encode("utf-8")
            # Их служба принимает действия голой строкой (text/plain), а не JSON.
            headers["Content-Type"] = "text/plain;charset=UTF-8"
            # Защита от подделки запросов из браузера (их http_guard.h, ветка после 1.1.31):
            # изменяющий запрос без этого заголовка отклоняется. Старым версиям он не мешает.
            # Host http.client ставит сам — «адрес:порт», ровно то, что служба признаёт своим.
            headers["X-CC2-Request"] = "1"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    except (OSError, http.client.HTTPException) as e:
        raise CommunityError(f"прошивка сообщества недоступна: {e}") from e
    finally:
        conn.close()
    text = raw.decode("utf-8", "replace")
    try:
        obj = json.loads(text) if text.strip() else {}
    except ValueError:
        obj = {"text": text}
    if status >= 400:
        msg = (obj.get("error") if isinstance(obj, dict) else "") or text.strip() or f"HTTP {status}"
        raise CommunityError(str(msg))
    return obj


def _get(host: str, path: str, timeout: float = TIMEOUT):
    return _http(host, "GET", path, timeout=timeout)


def _post(host: str, path: str, body: str = "", timeout: float = TIMEOUT):
    return _http(host, "POST", path, body, timeout=timeout)


def _raw(host: str, path: str, body: str, timeout: float = TIMEOUT,
         port: int = PORT) -> tuple[int, str, bytes]:
    """POST с ответом-байтами (превью G-code — PNG/JPEG): (статус, Content-Type, тело). Отдельно
    от _http, который разбирает текст и JSON: картинку через decode пропускать нельзя.
    Соединение — отдельным шагом: его отказ (CommunityUnreachable) значит, что запрос не ушёл."""
    h = _host(host)
    if not h:
        raise CommunityError("не задан адрес принтера")
    conn = http.client.HTTPConnection(h, port, timeout=timeout)
    try:
        try:
            conn.connect()
        except OSError as e:
            raise CommunityUnreachable(f"прошивка сообщества недоступна: {e}") from e
        conn.request("POST", path, body=body.encode("utf-8"),
                     headers={"Connection": "close", "Content-Type": "text/plain;charset=UTF-8",
                              "X-CC2-Request": "1"})
        resp = conn.getresponse()
        return resp.status, (resp.getheader("Content-Type") or ""), resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise CommunityError(f"прошивка сообщества недоступна: {e}") from e
    finally:
        conn.close()


# ─── файлы на принтере (их file_manager / gcode_thumbnails) ──────────────────────────────────
# Их служба читает файловую систему принтера сама: видит память и флешку, вложенные папки и
# файлы, залитые из Orca мимо вендорской базы. Стоковый 1044 отвечает только «привязанному»
# слайсеру, отсюда наш пассивный сбор — с их службой он для списка не нужен.

STORAGES = {"local": "internal", "u-disk": "usb"}       # наш носитель → их

# Их отказы по файлам — по-английски; мастеру нужны слова.
_FILE_ERRORS = {
    "Printer must be idle and connected": "Файл из папки удаляется, только пока принтер простаивает",
    "Storage unavailable or not writable": "Носитель недоступен или защищён от записи",
    "Cannot delete this file": "Принтер не дал удалить этот файл",
}


def _file_error(e: CommunityError) -> CommunityError:
    msg = str(e).strip()
    return CommunityError(_FILE_ERRORS.get(msg) or msg)


def gcode_files(host: str, storage: str = "local") -> dict:
    """Файлы носителя: {ok, files:[{name, path, size, created}], truncated, total, source}.
    name = path относительно корня носителя (во вложенной папке — с папкой): им же и стартуют."""
    key = STORAGES.get(storage)
    if not key:
        raise CommunityError("неизвестный носитель")
    obj = _get(host, "/api/gcode-files", timeout=8.0)
    st = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(st, dict):
        raise CommunityError("служба не отдала список файлов")
    if not st.get("available"):
        return {"ok": False, "error": "флешка не вставлена" if key == "usb" else "память принтера недоступна"}
    files = []
    for f in st.get("files") or []:
        path = str(f.get("path") or "") if isinstance(f, dict) else ""
        if not path:
            continue
        files.append({"name": path, "path": path, "size": int(f.get("size") or 0),
                      "created": int(f.get("modified") or 0)})
    return {"ok": True, "files": files, "truncated": bool(st.get("truncated")),
            "total": int(st.get("total") or len(files)), "source": "cc2"}


def gcode_thumbnail(host: str, storage: str, path: str) -> tuple[bytes, str] | None:
    """Крупнейшее превью, зашитое в G-code: (байты, Content-Type) или None — превью нет."""
    key = STORAGES.get(storage)
    if not key or not path or "\n" in path:
        raise CommunityError("неверный файл")
    status, ctype, raw = _raw(host, "/api/gcode-files/thumbnail", f"{key}\n{path}", timeout=10.0)
    if status in (404, 415, 422):
        return None
    if status >= 400 or not ctype.startswith("image/"):
        raise CommunityError(f"превью не получено (HTTP {status})")
    return raw, ctype.split(";")[0].strip()


def gcode_inspect(host: str, storage: str, path: str) -> dict:
    """Инструменты файла и их филамент (их /inspect: читают сам G-code, без вендорской базы) в
    форме нашего file_info: {ok, file:{name, colors:[{hex, type, extruder, mm}], ...}}.
    Читают файл целиком (кэш у них по stat файла) — звать по делу, а не на каждый кадр."""
    key = STORAGES.get(storage)
    if not key or not path or "\n" in path:
        raise CommunityError("неверный файл")
    obj = _http(host, "POST", "/api/gcode-files/inspect", f"{key}\n{path}", timeout=60.0)
    if not isinstance(obj, dict) or not isinstance(obj.get("filaments"), list):
        raise CommunityError("служба не разобрала файл")
    colors = []
    for f in obj["filaments"]:
        if not isinstance(f, dict) or not isinstance(f.get("tool"), int):
            continue
        hexv = str(f.get("color") or "")[:7]          # «#RRGGBBAA» слайсера → #RRGGBB
        colors.append({"hex": hexv if hexv.startswith("#") else "",
                       "type": str(f.get("material") or ""), "extruder": f["tool"],
                       **({"mm": float(f["mm"])} if isinstance(f.get("mm"), (int, float)) else {})})
    # tools — инструменты, которые файл реально вызывает: по ним же их служба сверяет карту
    # слотов при старте.
    tools = [t for t in obj.get("tools") or [] if isinstance(t, int) and not isinstance(t, bool)]
    return {"ok": True, "source": "cc2", "file": {
        "name": path, "colors": colors, "tools": tools or [c["extruder"] for c in colors],
        "multicolour": bool(obj.get("multicolour")), "adaptive_mesh": bool(obj.get("adaptive_mesh"))}}


def gcode_delete(host: str, storage: str, path: str) -> None:
    """Удалить файл (их /delete; только в простое). Файла уже нет — тоже успех: его и хотели
    убрать, а при выборе нескольких один исчезнувший не должен останавливать остальные."""
    key = STORAGES.get(storage)
    if not key or not path or "\n" in path:
        raise CommunityError("неверный файл")
    try:
        _post(host, "/api/gcode-files/delete", f"{key}\n{path}", timeout=8.0)
    except CommunityError as e:
        if str(e).strip() == "G-code file not found or unsafe":
            return
        raise _file_error(e) from e


# ─── старт печати (их /api/gcode-files/print) ────────────────────────────────────────────────
# Их старт сверяет карту слотов с самим файлом, флешку сперва копирует в память принтера (1020
# надёжно берёт только её) и калибрует стол по-настоящему: стоковый 1020 bedlevel_force
# принимает, но подготовку не выполняет (их docs/GCODE-PROTOCOL.md). Режимы стола: saved —
# сохранённая сетка стороны листа (нет её — служба сама снимет полную), calibrate — по границам
# модели, если слайсер их записал, иначе полная, adaptive — только по границам, full — полная.
LEVELING = ("saved", "calibrate", "adaptive", "full")

_PRINT_ERRORS = {
    "Invalid print request": "Служба не приняла запрос старта",
    "File is unavailable or unsafe": "Файла нет на принтере — обновите список",
    "Invalid build plate side": "Сторона листа — A или B",
    "Invalid Canvas slot mapping": "Неверная карта слотов",
    "Cannot inspect bed leveling commands": "CC2 Control не смог прочитать файл",
    "Canvas mapping does not match this G-code":
        "Карта слотов не совпадает с филаментами файла — откройте старт заново",
    "Printer MQTT is not ready": "CC2 Control сейчас не на связи с принтером — повторите через минуту",
    "Starting a file requires an idle printer": "Принтер занят — старт только в простое",
    "Cannot import the USB file into internal memory":
        "Файл с флешки не скопировался в память принтера — хватает ли места?",
    "Filename is incompatible with calibrated printing":
        "С калибровкой стола файл с кавычками или обратной косой чертой в имени не запустить",
    "Cannot send the printer start request": "Принтер не принял старт — повторите",
    "Plate verification is still running": "CC2 Control ещё проверяет пластину стола — повторите через минуту",
}


def print_error_text(msg: str) -> str:
    msg = (msg or "").strip()
    return _PRINT_ERRORS.get(msg) or msg or "CC2 Control не запустил печать"


def gcode_print(host: str, storage: str, path: str, *, slot_map: list[tuple[int, int]],
                plate: str = "A", leveling: str = "saved", timelapse: bool = True) -> dict:
    """Старт файла: 202 → их ответ {accepted, filename, imported_from_usb, leveling, timelapse}.

    slot_map — пары (инструмент файла, лоток 0…3). Отказ службы — CommunityError с её текстом
    дословно: по нему вызывающий решает, что дальше (print_error_text переводит). Ответ
    потерялся уже после отправки — CommunityError «не ответил»: повторять вслепую нельзя."""
    key = STORAGES.get(storage)
    if not key or not path or "\n" in path:
        raise CommunityError("неверный файл")
    if plate not in ("A", "B") or leveling not in LEVELING:
        raise CommunityError("неверные параметры старта")
    mapping = ",".join(f"{int(t)}:{int(tray)}" for t, tray in slot_map)
    body = f"{key}\n{path}\n{mapping}\n{plate}\n{leveling}\n{1 if timelapse else 0}"
    try:
        # Флешку служба сперва копирует в память, калиброванный старт ждёт ответа принтера на
        # 1019 — отсюда запас по времени.
        status, _ctype, raw = _raw(host, "/api/gcode-files/print", body,
                                   timeout=120.0 if key == "usb" else 30.0)
    except CommunityUnreachable:
        raise
    except CommunityError as e:
        raise CommunityError("CC2 Control не ответил на старт — посмотрите на принтер, "
                             "прежде чем запускать снова") from e
    try:
        obj = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    if status >= 400 or not obj.get("accepted"):
        raise CommunityError(str(obj.get("error") or f"HTTP {status}"))
    return obj


# ─── автоопределение ─────────────────────────────────────────────────────────────────────────

def parse_info(obj) -> Info | None:
    """Ответ discovery API → Info, или None, если на порту не их служба."""
    if not isinstance(obj, dict) or obj.get("platform") != PLATFORM:
        return None
    caps = obj.get("capabilities") if isinstance(obj.get("capabilities"), dict) else {}
    services = obj.get("services") if isinstance(obj.get("services"), dict) else {}
    try:
        port = int(services.get("cc2_control") or PORT)
    except (TypeError, ValueError):
        port = PORT
    try:
        api = int(obj.get("api_version") or 0)
    except (TypeError, ValueError):
        api = 0
    return Info(firmware=str(obj.get("community_firmware") or ""),
                control=str(obj.get("cc2_control") or ""), api_version=api,
                caps={k: bool(v) for k, v in caps.items()}, port=port)


def probe(host: str) -> Info | None:
    """Синхронная проба (сеть). Результат кладётся в кэш — и удачный, и «нет прошивки»."""
    h = _host(host)
    info = None
    if h:
        try:
            info = parse_info(_get(h, "/api/v1/system/info", timeout=2.0))
        except CommunityError:
            info = None
    with _lock:
        prev = _cache.get(h)
        _cache[h] = (time.time(), info)
    # Пишем в лог только перемену: перепроверка идёт раз в 5 минут, и строка «найдена» на
    # каждый круг засоряла bot.log. Нашлась, пропала, сменилась версия — это события.
    before = prev[1] if prev else None
    if info and info != before:
        log.info("CC2 %s: найдена прошивка сообщества %s (CC2 Control %s)",
                 h, info.firmware, info.control)
    elif before and not info:
        log.info("CC2 %s: прошивка сообщества больше не отвечает", h)
    return info


def cached(host: str) -> Info | None:
    """Последний известный ответ без похода в сеть."""
    with _lock:
        hit = _cache.get(_host(host))
    return hit[1] if hit else None


def ensure_probe(host: str) -> None:
    """Если кэш устарел — перепроверить в фоне. Зовётся из опроса статуса; сам не ждёт сети."""
    h = _host(host)
    if not h or not AUTO_PROBE:
        return
    now = time.time()
    with _lock:
        hit = _cache.get(h)
        if hit is not None:
            ttl = PROBE_OK_TTL if hit[1] else PROBE_FAIL_TTL
            if now - hit[0] < ttl:
                return
        if h in _probing:
            return
        _probing.add(h)

    def _run():
        try:
            probe(h)
        except Exception:  # noqa: BLE001 — фоновая проба не должна ронять поток
            log.debug("cc2community: проба %s упала", h, exc_info=True)
        finally:
            with _lock:
                _probing.discard(h)

    threading.Thread(target=_run, name=f"cc2community-probe-{h}", daemon=True).start()


def forget(host: str | None = None) -> None:
    """Сбросить кэш (тесты, смена адреса карточки)."""
    with _lock:
        if host is None:
            _cache.clear()
            _zoffset.clear()
            _job.clear()
            _spools_summary.clear()
            _report.clear()
        else:
            _cache.pop(_host(host), None)
            _zoffset.pop(_host(host), None)
            _job.pop(_host(host), None)
            _spools_summary.pop(_host(host), None)
            _report.pop(_host(host), None)


# ─── живое задание ───────────────────────────────────────────────────────────────────────────
# Стоковый MQTT о задании из Orca знает мало: remaining_time_sec = 0 всю печать (оценку прошивка
# берёт из заголовка ElegooSlicer), total_layer не шлёт вовсе. Их служба в GET /api/printer
# досчитывает остаток из оценки слайсера в самом G-code (docs/PRINT_TIME_ESTIMATE.md их
# репозитория), всего слоёв — разбором файла, прогресс — из virtual_sdcard Klipper по сокету.
# Отсюда берём ровно то, чего нет в MQTT; MQTT остаётся основой (температуры, слой, состояние).

JOB_REFRESH = 5.0       # не чаще раза в 5 с: их страница сама опрашивает /api/printer, а хост
                        # CC2 слаб (03.10.2026 печать упала от перегрузки: 2 ядра, loadavg 16)
JOB_MAX_AGE = 20.0      # снимок старше — не используем: задание могло смениться
_JOB_STATES = ("printing", "paused")
_job: dict[str, tuple[float, dict | None]] = {}
_job_busy: set[str] = set()
# Сводка склада катушек из того же ответа /api/printer ({enabled, revision, questions}): по смене
# ревизии app/cc2spools.py перечитывает тяжёлый /api/spools, не опрашивая его вслепую.
_spools_summary: dict[str, tuple[float, dict]] = {}


def parse_spools_summary(obj) -> dict | None:
    """`spools` из ответа /api/printer → {enabled, revision, questions, printing, messages};
    None — склада нет (старая служба без учёта катушек или их библиотека не загружена).

    printing — машина печатает (machine.status 2): живой остаток катушки тогда меняется без
    смены ревизии. messages — счётчик MQTT-сообщений их службы с её старта: он сбросился —
    служба перезапускалась, и ревизия начала счёт заново (совпасть с нашей она может случайно)."""
    s = obj.get("spools") if isinstance(obj, dict) else None
    if not isinstance(s, dict) or not isinstance(s.get("revision"), int) \
            or isinstance(s.get("revision"), bool):
        return None
    qs = s.get("questions") if isinstance(s.get("questions"), list) else []
    m = obj.get("machine") if isinstance(obj.get("machine"), dict) else {}
    msgs = obj.get("messages")
    return {"enabled": s.get("enabled") is True, "revision": s["revision"],
            "questions": [q for q in qs if isinstance(q, int) and not isinstance(q, bool)],
            "printing": m.get("status") == 2,
            "messages": msgs if isinstance(msgs, int) and not isinstance(msgs, bool) else -1}


def remember_spools_summary(host: str, obj) -> dict | None:
    """Разобрать и запомнить сводку склада из ответа /api/printer этого адреса."""
    summ = parse_spools_summary(obj)
    if summ is not None:
        with _lock:
            _spools_summary[_host(host)] = (time.time(), summ)
    return summ


def spools_summary(host: str) -> tuple[float, dict] | None:
    """(когда, сводка) последнего увиденного склада катушек — без сети."""
    with _lock:
        return _spools_summary.get(_host(host))


# Отчёт прошивки (`printer_report` в /api/printer): последнее событие vendor-потока Klipper —
# WARNING 1, CRITICAL 2, RESUME 3 (их API.md, «Native printer reports»). Стоковый MQTT таких
# событий не даёт; приходят тем же опросом задания, отдельного запроса нет.
REPORT_LEVELS = {1: "предупреждение", 2: "критично", 3: "возобновление"}
_report: dict[str, tuple[float, dict]] = {}


def parse_report(obj) -> dict | None:
    """`printer_report` из ответа /api/printer → {event_id, code, level, message, age}."""
    r = obj.get("printer_report") if isinstance(obj, dict) else None
    if not isinstance(r, dict) or not isinstance(r.get("event_id"), str) or not r["event_id"]:
        return None

    def i(v) -> int:
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    msg = r.get("message")
    return {"event_id": r["event_id"][:64], "code": i(r.get("code")), "level": i(r.get("level")),
            "message": msg.strip()[:500] if isinstance(msg, str) else "", "age": i(r.get("age"))}


def remember_report(host: str, obj) -> dict | None:
    rep = parse_report(obj)
    if rep is not None:
        with _lock:
            _report[_host(host)] = (time.time(), rep)
    return rep


def report(host: str) -> tuple[float, dict] | None:
    """(когда получен, отчёт) последнего события прошивки — без сети."""
    with _lock:
        return _report.get(_host(host))


def parse_job(obj) -> dict | None:
    """Ответ /api/printer → поля задания; None, если задания нет или служба без связи с
    принтером. Ключи — как в стоковом print_status, чтобы слияние было переносом, а не
    переводом: filename, current_layer, total_layer, remaining_time_sec, print_duration; плюс
    progress 0..1 и remaining_source («mqtt» / «gcode»)."""
    if not isinstance(obj, dict) or not obj.get("connected"):
        return None
    age = obj.get("last_message_age")
    if isinstance(age, (int, float)) and age > 15:      # их служба сама давно не слышала принтер
        return None
    m = obj.get("machine") if isinstance(obj.get("machine"), dict) else {}
    p = obj.get("print") if isinstance(obj.get("print"), dict) else {}
    if m.get("status") != 2 or str(p.get("state") or "").lower() not in _JOB_STATES:
        return None
    out: dict = {"filename": str(p.get("filename") or "").strip(),
                 "state": str(p.get("state")).lower()}

    def num(v) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    if (v := num(p.get("current_layer"))) is not None and v >= 0:
        out["current_layer"] = int(v)
    if (v := num(p.get("total_layers"))) is not None and v > 0:
        out["total_layer"] = int(v)
    if (v := num(p.get("duration"))) is not None and v >= 0:
        out["print_duration"] = int(v)
    if (v := num(p.get("remaining"))) is not None and v > 0:
        out["remaining_time_sec"] = int(v)
        out["remaining_source"] = str(p.get("remaining_source") or "")
    if (v := num(m.get("progress"))) is not None and 0 <= v <= 100:
        out["progress"] = v / 100.0
    return out


def fetch_job(host: str) -> dict | None:
    """Синхронно (сеть): снимок задания в кэш. Ошибка связи — «задания не знаем» (None).
    Попутно запоминает сводку склада катушек и отчёт прошивки из того же ответа."""
    h = _host(host)
    try:
        obj = _get(h, "/api/printer", timeout=2.0)
        job = parse_job(obj)
        remember_spools_summary(h, obj)
        remember_report(h, obj)
    except CommunityError:
        job = None
    with _lock:
        _job[h] = (time.time(), job)
    return job


def job(host: str) -> dict | None:
    """Последний снимок задания без ожидания сети; устаревший — освежается в фоне.

    Ходит к принтеру, только если на нём найдена прошивка сообщества: стоковый CC2 на 8081 не
    слушает, и каждый опрос был бы лишним отказом в соединении."""
    h = _host(host)
    if not h or not cached(h):
        return None
    now = time.time()
    with _lock:
        ts, snap = _job.get(h, (0.0, None))
        stale = now - ts >= JOB_REFRESH
        start = stale and AUTO_PROBE and h not in _job_busy
        if start:
            _job_busy.add(h)
    if start:
        def _run():
            try:
                fetch_job(h)
            except Exception:  # noqa: BLE001 — фоновый опрос не должен ронять поток
                log.debug("cc2community: снимок задания %s не получен", h, exc_info=True)
            finally:
                with _lock:
                    _job_busy.discard(h)

        threading.Thread(target=_run, name=f"cc2community-job-{h}", daemon=True).start()
    return snap if now - ts <= JOB_MAX_AGE else None


def merge_job(state: dict, snap: dict | None) -> dict:
    """Дописать в СЫРОЕ состояние MQTT то, чего стоковая телеметрия о задании не знает.

    Мутирует и возвращает `state` — звать на копии (cached_status её и отдаёт). Ничего из MQTT
    не перетирает, кроме нулевого остатка: имя и всего слоёв — только если их нет; прогресс
    кладётся как virtual_sdcard.progress, то есть под тем именем, под каким его отдаёт Klipper,
    и elegoo.enrich_status берёт его первым. Снимок другого задания (имя не совпало) не трогаем."""
    if not snap:
        return state
    ps = state.get("print_status")
    ps = dict(ps) if isinstance(ps, dict) else {}
    mine = str(ps.get("filename") or "").strip()
    if mine and snap.get("filename") and mine != snap["filename"]:
        return state
    if not mine and snap.get("filename"):
        ps["filename"] = snap["filename"]
    tl = ps.get("total_layer")
    if not (isinstance(tl, (int, float)) and tl > 0) and snap.get("total_layer"):
        ps["total_layer"] = snap["total_layer"]
    rem = ps.get("remaining_time_sec")
    if not (isinstance(rem, (int, float)) and rem > 0) and snap.get("remaining_time_sec"):
        ps["remaining_time_sec"] = snap["remaining_time_sec"]
    state["print_status"] = ps
    if isinstance(snap.get("progress"), float):
        state["virtual_sdcard"] = {"progress": snap["progress"]}
    return state


def driver_caps(info: Info | None) -> dict:
    """Способности драйвера, которые даёт найденная прошивка (поля Caps)."""
    if not info:
        return {}
    c = info.caps
    return {
        "exclude_object": bool(c.get("object_exclusion")),
        # Карта стола и действия /api/control (винты, Z-offset, скорость/поток) — часть самой
        # службы с V4.0; отдельного флага в discovery у них нет.
        "bed_mesh": info.api_version >= 1,
        "live_tune": info.api_version >= 1,
        "gcode": bool(c.get("gcode_console")),
        "slot_edit": bool(c.get("canvas")),
        "thumbs": bool(c.get("file_manager")) and bool(c.get("gcode_thumbnails")),
    }


# ─── действия ────────────────────────────────────────────────────────────────────────────────

def _result_status(obj) -> dict:
    """Ответ Klipper objects/query, как его пересылает их служба: {result:{status:{...}}}."""
    if not isinstance(obj, dict):
        return {}
    res = obj.get("result") if isinstance(obj.get("result"), dict) else obj
    st = res.get("status") if isinstance(res.get("status"), dict) else res
    return st if isinstance(st, dict) else {}


_OBJECT_NAME = re.compile(r"^[A-Za-z0-9_.\- ]{1,80}$")   # ровно то, что их служба пропустит


def valid_object_name(name: str) -> bool:
    return bool(_OBJECT_NAME.match(name or ""))


def objects(host: str) -> dict:
    """Детали текущего задания: {objects:[{name, center, polygon}], excluded, current}."""
    eo = _result_status(_get(host, "/api/exclude-objects")).get("exclude_object")
    if not isinstance(eo, dict):
        raise CommunityError("принтер не сообщил детали задания")
    out = []
    for o in eo.get("objects") or []:
        if isinstance(o, dict):
            name = str(o.get("name") or "")
            center = o.get("center") if isinstance(o.get("center"), list) else None
            poly = o.get("polygon") if isinstance(o.get("polygon"), list) else []
        else:
            name, center, poly = str(o), None, []
        if name:
            out.append({"name": name, "center": center, "polygon": poly})
    excluded = [str(x) for x in (eo.get("excluded_objects") or [])]
    cur = eo.get("current_object")
    return {"objects": out, "excluded": excluded, "current": str(cur) if cur else ""}


def action(host: str, act: str) -> dict:
    """Одно защищённое действие их службы (POST /api/control, тело — строка действия)."""
    _post(host, "/api/control", act)
    return {"ok": True}


def exclude(host: str, name: str) -> dict:
    if not valid_object_name(name):
        raise CommunityError("такое имя детали прошивка не примет")
    return action(host, f"object:exclude:{name}")


def _mesh_profile(root: dict) -> dict | None:
    matrix = root.get("probed_matrix") or root.get("mesh_matrix") or root.get("points")
    if not (isinstance(matrix, list) and matrix and isinstance(matrix[0], list)):
        return None
    rows = []
    for r in matrix:
        rows.append([float(v) if isinstance(v, (int, float)) else None for v in (r or [])])
    mn = root.get("mesh_min") if isinstance(root.get("mesh_min"), list) else None
    mx = root.get("mesh_max") if isinstance(root.get("mesh_max"), list) else None
    mp = root.get("mesh_params") if isinstance(root.get("mesh_params"), dict) else {}
    if mn is None and mp:
        mn = [mp.get("min_x"), mp.get("min_y")]
        mx = [mp.get("max_x"), mp.get("max_y")]
    vals = [v for r in rows for v in r if v is not None]
    if not vals:
        return None
    return {"min": mn, "max": mx, "matrix": rows,
            "lo": min(vals), "hi": max(vals), "range": max(vals) - min(vals),
            "mean": sum(vals) / len(vals)}


# Профили сохранённых сеток CC2: «default» — сторона A листа, «default1» — сторона B (их
# разведка, GCODE-PROTOCOL.md). Подписи — как у ручки старта печати (print_layout A/B).
MESH_PROFILES = {"default": "A", "default1": "B"}


def mesh(host: str) -> dict:
    """Карта стола: активная сетка и сохранённые профили сторон A/B."""
    bm = _result_status(_get(host, "/api/mesh")).get("bed_mesh")
    if not isinstance(bm, dict):
        raise CommunityError("прошивка не отдала карту стола")
    active = _mesh_profile(bm)
    profiles = {}
    for name, p in (bm.get("profiles") or {}).items():
        if isinstance(p, dict):
            prof = _mesh_profile(p)
            if prof:
                prof["side"] = MESH_PROFILES.get(name, "")
                profiles[name] = prof
    return {"active": active, "active_name": str(bm.get("profile_name") or ""),
            "profiles": profiles}


def console(host: str) -> dict:
    """Состояние консоли их службы: {busy, completed, success, command, output}."""
    c = _get(host, "/api/console")
    if not isinstance(c, dict):
        raise CommunityError("консоль не ответила")
    return {"busy": bool(c.get("busy")), "completed": bool(c.get("completed")),
            "success": bool(c.get("success")), "command": str(c.get("command") or ""),
            "output": str(c.get("output") or ""), "generation": c.get("generation")}


def console_send(host: str, command: str) -> dict:
    cmd = (command or "").strip()
    if not cmd or "\n" in cmd or "\r" in cmd:
        raise CommunityError("одна команда за раз, без переводов строки")
    if len(cmd) > 200:
        raise CommunityError("команда слишком длинная")
    _post(host, "/api/console/command", cmd)
    return {"ok": True}


def console_clear(host: str) -> dict:
    _post(host, "/api/console/clear", "")
    return {"ok": True}


def health(host: str) -> dict:
    """Ресурсы принтера: аптайм, память, загрузка — читает /proc на самом принтере."""
    h = _get(host, "/api/health")
    if not isinstance(h, dict):
        raise CommunityError("служба не сообщила о себе")
    load = str(h.get("loadavg") or "").split()
    return {"uptime_s": h.get("uptime_seconds"), "mem_total_kb": h.get("mem_total_kb"),
            "mem_available_kb": h.get("mem_available_kb"),
            "load1": float(load[0]) if load and _is_float(load[0]) else None,
            "mqtt": bool(h.get("mqtt_connected")) and bool(h.get("mqtt_registered"))}


def tuning(host: str) -> dict:
    """Фактические скорость и поток (%) по телеметрии Klipper — то, что принтер ПРИНЯЛ."""
    p = _get(host, "/api/printer")
    t = (p.get("tuning") if isinstance(p, dict) else None) or {}
    return {"speed_percent": t.get("speed_percent"), "flow_percent": t.get("flow_percent"),
            "live_velocity": t.get("live_velocity")}


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ─── Z-offset за сессию ──────────────────────────────────────────────────────────────────────

ZOFFSET_STEPS = (0.01, 0.05)
ZOFFSET_SESSION_MAX = 0.5


def zoffset_session(host: str) -> float:
    with _lock:
        return round(_zoffset.get(_host(host), 0.0), 3)


def zoffset_adjust(host: str, delta: float) -> dict:
    """Сдвинуть Z на ±0.01/±0.05 мм. Их служба режет шаг сама; сумму за сессию держим мы,
    чтобы её можно было откатить одним действием и чтобы она не ушла за ±0.5 мм."""
    d = round(float(delta), 3)
    if abs(d) not in ZOFFSET_STEPS:
        raise CommunityError("шаг Z-offset — 0.01 или 0.05 мм")
    h = _host(host)
    total = zoffset_session(h) + d
    if abs(total) > ZOFFSET_SESSION_MAX + 1e-9:
        raise CommunityError(f"за сессию Z-offset сдвигается не больше ±{ZOFFSET_SESSION_MAX} мм")
    action(h, f"zoffset:adjust:{d:+.3f}")
    with _lock:
        _zoffset[h] = round(total, 3)
    return {"ok": True, "session": round(total, 3)}


def zoffset_undo(host: str) -> dict:
    h = _host(host)
    total = zoffset_session(h)
    if abs(total) < 0.0005:
        return {"ok": True, "session": 0.0}
    action(h, f"zoffset:undo:{-total:+.3f}")
    with _lock:
        _zoffset[h] = 0.0
    return {"ok": True, "session": 0.0}


# ─── винты стола ─────────────────────────────────────────────────────────────────────────────
# Их служба по действию `screws:measure` греет стол до 60°, чистит сопло и трижды касается
# стола тензодатчиком сопла в четырёх точках у винтов. Результаты Klipper печатает в консоль
# строками «probe at X,Y is z=Z» — оттуда их и берём. Расчёт поправок свой.

SCREWS = (("ПЛ", "передний левый", 30, 30), ("ПП", "передний правый", 230, 30),
          ("ЗП", "задний правый", 230, 225), ("ЗЛ", "задний левый", 30, 225))
_PROBE_LINE = re.compile(r"probe at\s+(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\s+is z=(-?\d+(?:\.\d+)?)",
                         re.IGNORECASE)


def _corner(x: float, y: float) -> str:
    """Угол стола 256×256 по координатам касания: перед — малый Y, лево — малый X."""
    front, left = y < 128, x < 128
    return {(True, True): "ПЛ", (True, False): "ПП", (False, False): "ЗП", (False, True): "ЗЛ"}[(front, left)]


def screws_from_console(text: str) -> dict | None:
    """Медиана трёх последних касаний у каждого из четырёх винтов; None — замер не полон.

    Касания раскладываются по УГЛАМ, а не по точным координатам: у разных версий службы точки
    разные (1.1.31 — X35/X225, Y30/Y225 по холодному столу; ветка разработки — X30/X230 с
    прогревом до 60 °C), а винт у каждого угла один."""
    groups: dict[str, list[float]] = {}
    for m in _PROBE_LINE.finditer(text or ""):
        code = _corner(float(m.group(1)), float(m.group(2)))
        groups.setdefault(code, []).append(float(m.group(3)))
    out = {}
    for code, _name, _x, _y in SCREWS:
        vals = groups.get(code) or []
        if len(vals) < 3:
            return None
        out[code] = statistics.median(vals[-3:])
    return out


def screws_plan(values: dict) -> dict:
    """Поправки винтов, мкм. Два плана, мастер выбирает:

    * «от переднего левого» — ПЛ не трогаем, остальные три подгоняем под него (так делает
      SCREWS_TILT_CALCULATE у Klipper);
    * «минимум кручения» — опорная высота в середине размаха всех четырёх: крутятся все винты,
      но самый большой поворот вдвое меньше. Полезно, когда один угол сильно ушёл.

    Положительное отклонение = угол ВЫШЕ опоры: касание случилось раньше, стол там ближе к
    соплу, винт надо ОПУСТИТЬ. Направление вращения зависит от резьбы и подписано словами."""
    codes = [c for c, *_ in SCREWS]
    ref = values[codes[0]]
    rel = {c: values[c] - ref for c in codes}
    mid = (max(rel.values()) + min(rel.values())) / 2

    def plan(offset: float) -> list[dict]:
        rows = []
        for code, name, x, y in SCREWS:
            dev = rel[code] - offset
            um = round(dev * 1000)
            rows.append({"code": code, "name": name, "x": x, "y": y, "um": um,
                         "action": ("ок" if abs(um) <= 20 else ("опустить" if um > 0 else "поднять"))})
        return rows

    by_fl = plan(0.0)
    minimal = plan(mid)
    return {"z": {c: round(values[c], 4) for c in codes},
            "from_front_left": by_fl, "minimal": minimal,
            "peak_um": max(abs(r["um"]) for r in by_fl),
            "peak_minimal_um": max(abs(r["um"]) for r in minimal),
            "spread_um": round((max(rel.values()) - min(rel.values())) * 1000)}
