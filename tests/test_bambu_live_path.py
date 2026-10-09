"""Волна 7б через СИМУЛЯТОР: настоящий TLS, настоящий MQTT, настоящий путь до журнала.

Чем это отличается от tests/test_bambu_report.py. Там разбор проверяется на рукописных payload'ах
и скриптованном сокете — то есть проверяется наше представление о формате. Здесь поднимается
`app/bambusim.BambuSimulator`: он слушает TCP, поднимает TLS, отвечает CONNACK и SUBACK, принимает
`pushall` и шлёт отчёт — полный, а дальше дельтами, как прошивка. Сессия ходит к нему по-честному.

**ЧЕГО ЭТИ ТЕСТЫ НЕ ДОКАЗЫВАЮТ.** Симулятор говорит так, как мы ПОНЯЛИ протокол по
опубликованному формату. Совпадение с настоящей прошивкой Bambu он не проверяет и не может:
имена полей, единицы и коды ошибок сверяются только записями с живой машины
(`tools/bambu_sniff.py`, порядок работ — раздел 12 плана цикла 3). Ловится тут другое — ошибки
НАШЕЙ стороны: сломанный хендшейк, потерянная подписка, забытый `pushall`, замена состояния
вместо слияния, зависший реконнект, команда, ушедшая не в ту тему.
"""
import shutil
import time

import pytest

from app import bambu, printerdrv

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None,
                                reason="нет openssl — симулятору нечем поднять TLS")


def _wait(cond, timeout: float = 8.0, step: float = 0.05) -> bool:
    """Дождаться условия. Сессия живёт в фоновом потоке, и sleep фиксированной длины сделал бы
    тест либо медленным, либо хлопающим."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def sim(tmp_path):
    from app import bambusim

    key, crt = bambusim.make_cert(tmp_path)
    s = bambusim.BambuSimulator(certfile=crt, keyfile=key)
    try:
        yield s
    finally:
        s.close()
        bambu.sessions_stop_all()


class _Card:
    def __init__(self, port: int, serial: str, code: str):
        self.id, self.name, self.kind = 2, "Bambu (симулятор)", "bambu"
        # адрес с портом — norm_host срежет его для КЛЮЧА реестра, но в сокет уйдёт как есть
        self.address, self.access_code, self.serial = f"127.0.0.1:{port}", code, serial
        self.slicer = self.slicer_path = ""


def _session_to(sim, monkeypatch):
    """Сессия, ходящая в симулятор: подменён только ПОРТ, всё остальное настоящее."""
    monkeypatch.setattr(bambu, "MQTT_PORT", sim.port)
    ses = bambu.session_for(bambu.Endpoint(ip="127.0.0.1", code=sim.code, serial=sim.serial))
    ses.start()
    return ses


def test_handshake_subscribe_and_full_snapshot(sim, monkeypatch):
    """Подключились, подписались, попросили полный снимок — и получили состояние.

    Без `pushall` в кэше оказались бы одни дельты: прошивка полный снимок сама не шлёт, и экран
    показал бы принтер полупустым."""
    ses = _session_to(sim, monkeypatch)
    assert _wait(lambda: ses.seen_report), "сессия не приняла ни одного отчёта"

    assert any((r.get("pushing") or {}).get("command") == "pushall" for r in sim.requests)
    cs = ses.cached_status(30.0)
    assert cs and cs["ok"] and cs["connected"] is True
    assert cs["state"]["extruder"]["temperature"] == 29.0
    assert cs["state"]["heater_bed"]["temperature"] == 26.0
    assert cs["state"]["print_stats"]["state"] == "standby"


def test_wrong_access_code_never_connects(sim, monkeypatch):
    """Неверный код доступа — отказ, а не тихое соединение без данных."""
    monkeypatch.setattr(bambu, "MQTT_PORT", sim.port)
    ses = bambu.BambuSession(
        lambda: bambu.Endpoint(ip="127.0.0.1", code="НЕВЕРНЫЙ", serial=sim.serial))
    ses.start()
    try:
        assert not _wait(lambda: ses.seen_report, timeout=2.0)
        assert ses.cached_status(30.0) is None
    finally:
        ses.stop()


def test_deltas_are_merged_over_the_full_snapshot(sim, monkeypatch):
    """Прошивка шлёт дельты. Замена состояния целиком стирала бы половину показаний на каждом
    кадре: в дельте прогресса нет ни температур, ни имени задания."""
    ses = _session_to(sim, monkeypatch)
    assert _wait(lambda: ses.seen_report)

    sim.begin("plate_1.gcode", minutes=60)
    assert _wait(lambda: (ses.cached_status(30.0) or {}).get("state", {})
                 .get("print_stats", {}).get("state") == "printing")

    sim.advance(percent=42, layer=120, remaining_min=35)     # ДЕЛЬТА без температур и имени
    assert _wait(lambda: (ses.cached_status(30.0) or {})["state"]["virtual_sdcard"]["progress"] == 0.42)

    st = ses.cached_status(30.0)["state"]
    assert st["print_stats"]["filename"] == "plate_1.gcode"  # имя пережило дельту
    assert st["extruder"]["target"] == 220.0                 # цели тоже
    assert st["info"]["current_layer"] == 120
    assert st["print_status"]["remaining_time_sec"] == 35 * 60


def test_driver_sees_the_print_through_the_whole_stack(sim, monkeypatch):
    """Сквозной путь: симулятор → сессия → драйвер → `job_state`, которым живёт вся автоматика."""
    ses = _session_to(sim, monkeypatch)
    assert _wait(lambda: ses.seen_report)
    monkeypatch.setattr(bambu, "MQTT_PORT", sim.port)

    drv = printerdrv.for_printer(_Card(sim.port, sim.serial, sim.code))
    assert drv.enabled() is True
    # способности появились САМИ — по факту первого отчёта, а не по вере в код
    assert drv.describe()["caps"] == printerdrv.BambuDriver.caps.as_dict()

    sim.begin("cat.gcode", minutes=30)
    assert _wait(lambda: drv.job_state((drv.status(30.0) or {}).get("state") or {})["active"])

    js = drv.job_state(drv.status(30.0)["state"])
    assert js["name"] == "cat.gcode" and js["phase"] == "printing"

    sim.finish()
    assert _wait(lambda: not drv.job_state((drv.status(30.0) or {}).get("state") or {})["active"])


def test_command_reaches_the_printer_in_its_own_topic(sim, monkeypatch):
    """Команда обязана уйти в `device/<sn>/request` ЭТОГО принтера.

    Уедет адрес без серийника — тело придёт на правильную машину в чужую тему, потеряется молча,
    а вызов отчитается об успехе."""
    ses = _session_to(sim, monkeypatch)
    assert _wait(lambda: ses.seen_report)
    monkeypatch.setattr(bambu, "MQTT_PORT", sim.port)

    drv = printerdrv.for_printer(_Card(sim.port, sim.serial, sim.code))
    r = drv.control("pause")
    assert r["ok"] and r.get("unconfirmed")      # подтверждение придёт отдельным отчётом

    assert _wait(lambda: any((x.get("print") or {}).get("command") == "pause"
                             for x in sim.requests))
    # неизвестную команду не изображаем выполненной
    assert drv.control("light")["ok"] is False


def test_session_reconnects_after_the_printer_drops_it(sim, monkeypatch):
    """Принтер разорвал соединение (перезагрузка, Wi-Fi) — подписчик обязан вернуться сам и
    заново попросить полный снимок, иначе после обрыва в кэше останутся одни старые дельты."""
    ses = _session_to(sim, monkeypatch)
    assert _wait(lambda: ses.seen_report)
    first = sim.connects

    sim.drop_clients()
    assert _wait(lambda: sim.connects > first, timeout=10.0), "подписчик не переподключился"
    assert _wait(lambda: sum(1 for r in sim.requests
                             if (r.get("pushing") or {}).get("command") == "pushall") >= 2)


def test_stop_ends_the_thread_and_the_registry(sim, monkeypatch):
    """`stop()` обязан ОСТАНАВЛИВАТЬ поток: удалённая карточка иначе оставляет подписчика
    навсегда, а он держит соединение с машиной."""
    import threading

    ses = _session_to(sim, monkeypatch)
    assert _wait(lambda: ses.seen_report)

    bambu.sessions_stop_all()
    ses.stop()
    assert _wait(lambda: not [t for t in threading.enumerate()
                              if t.name.startswith("bambu-mqtt-sub") and t.is_alive()],
                 timeout=10.0)
