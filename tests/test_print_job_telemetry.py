"""Телеметрия задания CC2, когда файл нарезан не в ElegooSlicer (06.10.2026).

После перехода на Orca и прошивку сообщества экран TURZX показывал печать «готово на 100 %,
осталось 0 мин», а вместо имени файла — демо-строку макета. Причина в стоковом MQTT: у файла
Orca прошивка шлёт remaining_time_sec = 0 всю печать, а бот считал прогресс по времени
(прошло / (прошло + 0) = 1) и, не видя «живых» признаков, прятал задание вместе с именем.
Тот же прогресс 1.0 printwatch принимал за финиш печати заказа сразу после старта.

Что держат тесты:
  • стоковая прошивка: прогресс — machine_status.progress (так его показывает официальный SDK
    Elegoo), задание с нулевым остатком остаётся заданием, остаток оценивается по прогрессу;
  • прошивка сообщества: остаток по оценке слайсера, всего слоёв и прогресс Klipper берутся
    из GET /api/printer их службы и дописываются в снимок драйвера;
  • LCD-ручка отдаёт экрану настоящие поля, а не «0 мин».

Снимки — с живой печати 06.10.2026 (ключи print_status — из лога printwatch, ответ службы —
curl к принтеру), к железу тесты не ходят.
"""
import json
from types import SimpleNamespace

import pytest

from app import cc2community, elegoo, printerdrv, webapp
from app.web import screens

FILE = "ECC2_0.4_EL+Sidemount+Drybox_rev3_hex_Elegoo PLA Matte - Откалиброван_0.2_5h52m.gcode"


def _orca_state(**ps_over) -> dict:
    """Стоковый MQTT посреди печати файла из Orca: остаток 0, всего слоёв нет."""
    ps = {"bed_mesh_detect": False, "current_layer": 19, "enable": True,
          "filament_detect": True, "filename": FILE, "print_duration": 7400,
          "remaining_time_sec": 0, "state": "printing", "total_duration": 7495, "uuid": "u-1"}
    ps.update(ps_over)
    return {"print_status": ps,
            "machine_status": {"status": 2, "sub_status": 2075, "progress": 23},
            "extruder": {"temperature": 214.9, "target": 215},
            "heater_bed": {"temperature": 59.9, "target": 60}}


# Ответ /api/printer службы CC2 Control 1.1.31 в ту же минуту (лишнее вырезано).
API_PRINTER = {
    "connected": True, "messages": 9517, "last_message_age": 1,
    "machine": {"status": 2, "status_name": "Printing", "sub_status": 2075, "reason": 0,
                "progress": 23},
    "print": {"enabled": True, "filename": FILE, "state": "printing", "uuid": "u-1",
              "current_layer": 19, "total_layers": 321, "duration": 7400, "remaining": 13702,
              "remaining_source": "gcode", "total_duration": 7495},
}


# ─────────────────────────── стоковая прошивка ───────────────────────────

def test_zero_remaining_is_not_a_finished_print():
    ov = elegoo.enrich_status(_orca_state(), now=1_700_000_000.0)
    assert ov["virtual_sdcard"]["progress"] == pytest.approx(0.23), "прогресс — machine_status"
    assert ov["print_stats"] == {"state": "printing", "filename": FILE}
    # остатка прошивка не знает — оценка по прогрессу: 7400 × 0.77 / 0.23
    assert ov["remaining_sec"] == round(7400 * 0.77 / 0.23)
    assert ov["remaining"] and ov["eta_at"]


def test_no_progress_anywhere_means_unknown_not_hundred():
    st = _orca_state()
    st["machine_status"].pop("progress")
    ov = elegoo.enrich_status(st)
    assert "virtual_sdcard" not in ov and "remaining_sec" not in ov
    assert ov["print_stats"]["state"] == "printing"


def test_early_progress_is_not_extrapolated():
    st = _orca_state(print_duration=300)
    st["machine_status"]["progress"] = 2            # прогрев и юбка — оценка соврала бы в разы
    ov = elegoo.enrich_status(st)
    assert ov["virtual_sdcard"]["progress"] == pytest.approx(0.02)
    assert "remaining_sec" not in ov


def test_firmware_remaining_still_wins_and_progress_follows_sdk():
    st = _orca_state(remaining_time_sec=600)
    ov = elegoo.enrich_status(st)
    assert ov["remaining_sec"] == 600
    assert ov["virtual_sdcard"]["progress"] == pytest.approx(0.23)


def test_finished_print_keeps_old_meaning():
    done = {"print_status": {"print_duration": 500, "remaining_time_sec": 0, "state": "complete"},
            "machine_status": {"status": 1, "progress": 100}}
    ov = elegoo.enrich_status(done)
    assert ov["virtual_sdcard"]["progress"] == 1.0 and "print_stats" not in ov
    assert "remaining_sec" not in ov


def test_job_running_reads_word_then_machine_status():
    assert elegoo.job_running({"print_status": {"state": "paused"}})
    assert not elegoo.job_running({"print_status": {"state": "complete"},
                                   "machine_status": {"status": 2}})
    assert elegoo.job_running({"machine_status": {"status": 2}})
    assert not elegoo.job_running({"machine_status": {"status": 1}})
    assert not elegoo.job_running({})


def test_printwatch_does_not_see_finish_right_after_orca_start():
    """DONE_PROGRESS 0.99: с прогрессом 1.0 печать заказа «заканчивалась» сразу после старта."""
    from app import printwatch
    active, prog = printwatch._active_and_progress(_orca_state(), printerdrv.ElegooDriver(None))
    assert active and prog == pytest.approx(0.23)


# ─────────────────────────── прошивка сообщества ───────────────────────────

def test_parse_job_takes_what_mqtt_lacks():
    job = cc2community.parse_job(API_PRINTER)
    assert job["total_layer"] == 321 and job["remaining_time_sec"] == 13702
    assert job["remaining_source"] == "gcode" and job["progress"] == pytest.approx(0.23)
    assert job["filename"] == FILE


@pytest.mark.parametrize("patch", [
    {"connected": False},
    {"last_message_age": 60},
    {"machine": {"status": 1, "progress": 0}},
    {"print": {**API_PRINTER["print"], "state": "complete"}},
])
def test_parse_job_refuses_idle_or_stale(patch):
    assert cc2community.parse_job({**API_PRINTER, **patch}) is None


def test_merge_job_fills_gaps_and_never_overwrites():
    st = cc2community.merge_job(_orca_state(), cc2community.parse_job(API_PRINTER))
    ps = st["print_status"]
    assert ps["remaining_time_sec"] == 13702 and ps["total_layer"] == 321
    assert ps["current_layer"] == 19 and ps["filename"] == FILE
    assert st["virtual_sdcard"] == {"progress": pytest.approx(0.23)}
    # свой остаток у прошивки есть — его не трогаем
    st = cc2community.merge_job(_orca_state(remaining_time_sec=99),
                                cc2community.parse_job(API_PRINTER))
    assert st["print_status"]["remaining_time_sec"] == 99


def test_merge_job_ignores_snapshot_of_another_job():
    st = cc2community.merge_job(_orca_state(filename="other.gcode"),
                                cc2community.parse_job(API_PRINTER))
    assert st["print_status"]["remaining_time_sec"] == 0 and "virtual_sdcard" not in st


class _Card:
    def __init__(self, address="10.9.9.9"):
        self.address, self.access_code, self.serial, self.id, self.kind = (
            address, "CODE", "SN", 1, "elegoo")


@pytest.fixture
def community_printer(monkeypatch):
    """Карточка CC2 с найденной прошивкой сообщества и стоковым MQTT посреди печати Orca."""
    replies = {"/api/v1/system/info": {"platform": "cc2-community", "community_firmware": "4.2",
                                       "cc2_control": "1.1.31", "api_version": 1,
                                       "capabilities": {}},
               "/api/printer": API_PRINTER}
    calls = []

    def get(host, path, timeout=3.0):
        calls.append(path)
        return replies[path]
    monkeypatch.setattr(cc2community, "_get", get)
    cc2community.probe("10.9.9.9")
    monkeypatch.setattr(elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "online": True, "state": _orca_state()})
    return calls


def test_driver_status_carries_community_job(community_printer):
    drv = printerdrv.ElegooDriver(_Card())
    # автоопрос в тестах погашен: первый снимок кладём синхронно, как это сделал бы фон
    cc2community.fetch_job("10.9.9.9")
    st = drv.status()["state"]
    assert st["print_status"]["remaining_time_sec"] == 13702
    assert st["print_status"]["total_layer"] == 321
    js = drv.job_state({**st, **drv.enrich(st)})
    assert js["active"] and js["progress"] == pytest.approx(0.23)


def test_driver_does_not_ask_service_when_idle(community_printer, monkeypatch):
    monkeypatch.setattr(cc2community, "AUTO_PROBE", True)
    started = []
    monkeypatch.setattr(cc2community.threading, "Thread",
                        lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))
    idle = {"print_status": {"state": "", "remaining_time_sec": 0},
            "machine_status": {"status": 1}}
    monkeypatch.setattr(elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "online": True, "state": idle})
    printerdrv.ElegooDriver(_Card()).status()
    assert not [k for k in started if "job" in k.get("name", "")]


def test_job_refresh_is_background_and_single(community_printer, monkeypatch):
    monkeypatch.setattr(cc2community, "AUTO_PROBE", True)
    started = []
    monkeypatch.setattr(cc2community.threading, "Thread",
                        lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))
    assert cc2community.job("10.9.9.9") is None        # снимка ещё нет — не ждём сеть
    cc2community.job("10.9.9.9")                       # фон уже «идёт» — второй не запускаем
    assert len(started) == 1
    assert cc2community.job("10.9.9.8") is None        # без прошивки сообщества — ни запроса
    assert len(started) == 1


# ─────────────────────────── экран TURZX ───────────────────────────

@pytest.fixture
def lcd(monkeypatch):
    screens._lcd_cache.update(ts=0.0, data=None)
    screens._lcd_prn.clear()
    monkeypatch.setattr(screens, "_LCD_KEY", "")
    monkeypatch.setattr(screens, "psutil", None)
    monkeypatch.setattr(screens, "_lcd_gpu", _async({}))
    monkeypatch.setattr(screens, "_lcd_cpu_temp", _async(None))
    yield
    screens._lcd_cache.update(ts=0.0, data=None)
    screens._lcd_prn.clear()


def _async(value):
    async def f(*a, **k):
        return value
    return f


async def _lcd_printer() -> dict:
    req = SimpleNamespace(host="127.0.0.1", remote="127.0.0.1", headers={}, query={})
    resp = await webapp._api_lcd_data(req)
    assert resp.status == 200
    return json.loads(resp.body.decode("utf-8"))["printer"]


async def test_lcd_shows_real_job_with_community(tmp_db, lcd, community_printer):
    cc2community.fetch_job("10.9.9.9")
    printerdrv.use_driver(printerdrv.ElegooDriver(_Card()))
    try:
        p = await _lcd_printer()
    finally:
        printerdrv.use_driver(None)
    assert p["state"] == "printing" and p["file"] == FILE
    assert p["progress"] == 23.0 and p["layer"] == 19 and p["layers_total"] == 321
    assert p["remain_min"] == round(13702 / 60)


class _RawRemainingDriver(printerdrv.PrinterDriver):
    """Драйвер, у которого остаток лежит только в сыром print_status (так отдаёт Bambu)."""
    kind = "elegoo"

    def status(self, max_age=6.0):
        return {"ok": True, "state": {"print_status": {"state": "printing", "filename": "b.3mf",
                                                       "remaining_time_sec": 600}}}


async def test_lcd_other_drivers_keep_raw_remaining(tmp_db, lcd):
    printerdrv.use_driver(_RawRemainingDriver())
    try:
        p = await _lcd_printer()
    finally:
        printerdrv.use_driver(None)
    assert p["state"] == "printing" and p["remain_min"] == 10


async def test_lcd_stock_firmware_never_says_zero_minutes(tmp_db, lcd, monkeypatch):
    st = _orca_state()
    st["machine_status"].pop("progress")
    monkeypatch.setattr(elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "online": True, "state": st})
    printerdrv.use_driver(printerdrv.ElegooDriver(_Card("10.9.9.7")))
    try:
        p = await _lcd_printer()
    finally:
        printerdrv.use_driver(None)
    assert p["state"] == "printing" and p["file"] == FILE
    assert "progress" not in p and "remain_min" not in p and "layers_total" not in p
