"""Симулятор принтера Bambu Lab: настоящий MQTT-брокер поверх TLS на localhost.

Зачем. Волна 7б написана до приезда машины, и проверять её скриптованным сокетом — значит
проверять свои же представления о протоколе. Симулятор говорит по-настоящему: слушает TCP,
поднимает TLS, разбирает CONNECT/SUBSCRIBE/PUBLISH/PINGREQ, отвечает CONNACK и SUBACK, принимает
`pushall` и шлёт отчёт — полный, а потом дельтами. Через него проходит весь путь: сессия →
драйвер → наблюдатель → журнал.

Это ровно та же идея, что `printerdrv/fake.py` для Elegoo (волна 1): тестовое окружение вместо
железа, где печать начинается, идёт и заканчивается по команде теста.

**ЧЕГО СИМУЛЯТОР НЕ ДОКАЗЫВАЕТ.** Он говорит так, как мы ПОНЯЛИ протокол по опубликованному
формату. Совпадение с настоящей прошивкой он не проверяет и проверить не может: имена полей,
единицы, коды ошибок и порядок кадров сверяются только записями с живой машины
(`tools/bambu_sniff.py`). Симулятор ловит другое — ошибки НАШЕЙ стороны: сломанный хендшейк,
потерянную подписку, забытый `pushall`, замену состояния вместо слияния, зависший реконнект.

Сертификат самоподписанный и делается на лету через `openssl` (в сборке для разработчика он
есть; нет — симулятор честно скажет об этом, а не поднимется «без TLS» втихаря). Это тот же
случай, что у настоящего принтера: проверка имени не проходит, доверяем каналу, а не бумажке.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import subprocess
import threading
from pathlib import Path

from . import elegoo

log = logging.getLogger(__name__)

IDLE_REPORT = {
    "gcode_state": "IDLE",
    "nozzle_temper": 29.0, "nozzle_target_temper": 0.0,
    "bed_temper": 26.0, "bed_target_temper": 0.0,
    "chamber_temper": 25.0,
    "mc_percent": 0, "mc_remaining_time": 0,
    "cooling_fan_speed": "0",
    "stg_cur": 0,
}


def make_cert(dirpath: Path) -> tuple[Path, Path]:
    """Самоподписанный сертификат для localhost. Требует openssl в PATH.

    Отдельной функцией, потому что это единственное внешнее требование симулятора: не нашёлся
    openssl — тест обязан пропуститься с внятной причиной, а не молча проверить полпути."""
    key, crt = dirpath / "sim.key", dirpath / "sim.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(crt)],
        check=True, capture_output=True)
    return key, crt


class BambuSimulator:
    """Мини-брокер, изображающий принтер Bambu. Запускается на 127.0.0.1 со случайным портом."""

    def __init__(self, serial: str = "01P00SIM", code: str = "SIMCODE",
                 certfile: Path | None = None, keyfile: Path | None = None) -> None:
        self.serial, self.code = serial, code
        self.state = dict(IDLE_REPORT)
        self.requests: list[dict] = []     # что нам прислал бот — тесты это проверяют
        self.connects = 0                  # сколько раз к нам подключались (проверка реконнекта)
        self._clients: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self._srv.settimeout(0.3)
        self.port = self._srv.getsockname()[1]
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if certfile and keyfile:
            self._ctx.load_cert_chain(str(certfile), str(keyfile))
        threading.Thread(target=self._serve, daemon=True, name="bambu-sim").start()

    # ── управление печатью из теста ────────────────────────────────────────────────────
    def begin(self, name: str = "plate_1.gcode", minutes: int = 60) -> None:
        self.publish({"gcode_state": "RUNNING", "subtask_name": name,
                      "mc_percent": 0, "mc_remaining_time": minutes,
                      "nozzle_target_temper": 220.0, "bed_target_temper": 60.0,
                      "layer_num": 0, "total_layer_num": 300})

    def advance(self, percent: int, layer: int, remaining_min: int) -> None:
        """Дельта — ровно так же, как шлёт прошивка: без имени задания и без температур."""
        self.publish({"mc_percent": percent, "layer_num": layer,
                      "mc_remaining_time": remaining_min})

    def finish(self) -> None:
        self.publish({"gcode_state": "FINISH", "mc_percent": 100, "mc_remaining_time": 0,
                      "nozzle_target_temper": 0.0, "bed_target_temper": 0.0})

    def publish(self, delta: dict) -> None:
        """Отправить отчёт-дельту всем подписчикам и запомнить его в своём состоянии."""
        with self._lock:
            self.state.update(delta)
            clients = list(self._clients)
        pkt = elegoo._publish_pkt(f"device/{self.serial}/report", json.dumps({"print": delta}).encode())
        for c in clients:
            try:
                c.sendall(pkt)
            except OSError:
                pass

    def drop_clients(self) -> None:
        """Оборвать соединения — проверка реконнекта."""
        with self._lock:
            clients, self._clients = list(self._clients), []
        for c in clients:
            try:
                c.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.drop_clients()
        try:
            self._srv.close()
        except OSError:
            pass

    # ── сервер ─────────────────────────────────────────────────────────────────────────
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _addr = self._srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(raw,), daemon=True).start()

    def _client(self, raw: socket.socket) -> None:
        try:
            sock = self._ctx.wrap_socket(raw, server_side=True)
        except Exception:  # noqa: BLE001
            log.debug("sim: TLS-рукопожатие не удалось", exc_info=True)
            raw.close()
            return
        sock.settimeout(1.0)
        try:
            ptype, data = elegoo._recv_pkt(sock)
            if ptype is None or (ptype & 0xF0) != 0x10:      # ждём CONNECT
                sock.close()
                return
            # пароль — последнее поле переменной части CONNECT; проверяем ЧЕСТНО, чтобы неверный
            # код доступа давал отказ, а не молчаливое соединение без данных
            if self.code.encode() not in data:
                sock.sendall(b"\x20\x02\x00\x05")            # CONNACK: not authorized
                sock.close()
                return
            sock.sendall(b"\x20\x02\x00\x00")                # CONNACK ok
            with self._lock:
                self.connects += 1
            while not self._stop.is_set():
                try:
                    ptype, data = elegoo._recv_pkt(sock)
                except (TimeoutError, socket.timeout):
                    continue
                if ptype is None:
                    break
                kind = ptype & 0xF0
                if kind == 0x80:                             # SUBSCRIBE
                    pid = data[:2]
                    sock.sendall(b"\x90\x03" + pid + b"\x00")
                    with self._lock:
                        self._clients.append(sock)
                    continue
                if kind == 0xC0:                             # PINGREQ
                    sock.sendall(b"\xd0\x00")
                    continue
                if kind == 0x30:                             # PUBLISH от бота
                    _topic, obj = elegoo._parse_publish(ptype, data)
                    with self._lock:
                        self.requests.append(obj or {})
                    if ((obj or {}).get("pushing") or {}).get("command") == "pushall":
                        with self._lock:
                            full = dict(self.state)
                        sock.sendall(elegoo._publish_pkt(
                            f"device/{self.serial}/report", json.dumps({"print": full}).encode()))
                    continue
        except OSError:
            pass
        finally:
            with self._lock:
                if sock in self._clients:
                    self._clients.remove(sock)
            try:
                sock.close()
            except OSError:
                pass
