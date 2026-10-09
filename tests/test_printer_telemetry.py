"""История телеметрии принтера + ретранслятор камеры (фидбэк 20.07: «тащить данные всегда,
не дудосить принтер»). БД — во временном файле, сеть не используется."""
import asyncio
import time

from app import elegoo, printwatch, webapp


# ── БД: снимки телеметрии ────────────────────────────────────────────────────────────────────
async def test_printer_telemetry_roundtrip_and_prune(tmp_db):
    old_ts = int(time.time()) - 10 * 86400
    await tmp_db.add_printer_telemetry(printing=0, nozzle=29, bed=26, chamber=26, ts=old_ts)
    await tmp_db.add_printer_telemetry(printing=1, nozzle=248.5, nozzle_target=250,
                                       bed=70, bed_target=70, chamber=41, progress=0.4,
                                       layer=247, filename="job.gcode", light=1,
                                       active_slot=3, fan_model=100)
    rows = await tmp_db.list_printer_telemetry(0)
    assert len(rows) == 2 and rows[0]["ts"] <= rows[1]["ts"]      # по возрастанию времени
    hot = rows[1]
    assert hot["printing"] == 1 and hot["nozzle"] == 248.5 and hot["chamber"] == 41
    assert hot["filename"] == "job.gcode" and hot["active_slot"] == 3 and hot["light"] == 1
    # выборка интервалом: старый снимок отфильтровывается
    assert len(await tmp_db.list_printer_telemetry(old_ts + 1)) == 1
    # ретенция: старше 7 дней — удаляется, свежее — остаётся
    assert await tmp_db.prune_printer_telemetry(keep_days=7) == 1
    assert len(await tmp_db.list_printer_telemetry(0)) == 1


# ── printwatch: тик записи истории ───────────────────────────────────────────────────────────
_PRINTING_STATE = {
    "extruder": {"temperature": 248, "target": 250},
    "heater_bed": {"temperature": 70, "target": 70},
    "ztemperature_sensor": {"temperature": 41},
    "led": {"status": 1},
    "fans": {"fan": {"speed": 1.0}},
    "canvas_info": {"active_tray_id": 2},
    "print_status": {"filename": "job.gcode", "current_layer": 247,
                     "print_duration": 600, "remaining_time_sec": 600},
}


def _arm_idle_tick() -> None:
    """Взвести счётчик разрежения так, чтобы СЛЕДУЮЩИЙ тик был «пишущим».

    Счётчик теперь на машину, а ключ — id карточки из сида БД. Зашивать его в тест значило бы
    привязаться к сиду, поэтому взводим все известные ключи: тестов с двумя машинами здесь нет."""
    for k in list(printwatch._tele_tick):
        printwatch._tele_tick[k] = printwatch.TELE_IDLE_EVERY - 1


async def test_tick_telemetry_writes_and_thins_idle(tmp_db, monkeypatch):
    printwatch._tele_tick.clear()
    monkeypatch.setattr(elegoo, "cached_status",
                        lambda max_age=0: {"ok": True, "state": dict(_PRINTING_STATE)})
    await printwatch._tick_telemetry()                       # печать → пишем каждый тик
    rows = await tmp_db.list_printer_telemetry(0)
    assert len(rows) == 1
    r = rows[0]
    assert r["printing"] == 1 and r["nozzle"] == 248 and r["chamber"] == 41
    assert r["progress"] == 0.5 and r["layer"] == 247 and r["filename"] == "job.gcode"
    assert r["light"] == 1 and r["active_slot"] == 3 and r["fan_model"] == 100

    idle = {"heater_bed": {"temperature": 26}}
    monkeypatch.setattr(elegoo, "cached_status", lambda max_age=0: {"ok": True, "state": idle})
    await printwatch._tick_telemetry()                       # простой, тик №2 → пропуск
    assert len(await tmp_db.list_printer_telemetry(0)) == 1
    _arm_idle_tick()
    await printwatch._tick_telemetry()                       # каждый TELE_IDLE_EVERY-й → пишем
    rows = await tmp_db.list_printer_telemetry(0)
    assert len(rows) == 2 and rows[-1]["printing"] == 0

    # оффлайн и «подписчик без кадров» историю не пишут
    _arm_idle_tick()
    monkeypatch.setattr(elegoo, "cached_status", lambda max_age=0: None)
    await printwatch._tick_telemetry()
    _arm_idle_tick()
    monkeypatch.setattr(elegoo, "cached_status", lambda max_age=0: {"ok": True, "state": {}})
    await printwatch._tick_telemetry()
    assert len(await tmp_db.list_printer_telemetry(0)) == 2


# ── камера: нарезка кадров ретранслятора ─────────────────────────────────────────────────────
def test_cam_feed_slices_frames_and_drops_lagging():
    j1 = b"\xff\xd8AAAA\xff\xd9"
    j2 = b"\xff\xd8BB\xff\xd9"
    j3 = b"\xff\xd8CCC\xff\xd9"
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    relay = webapp._CamRelay("http://printer.local:8080/mjpeg")
    relay.clients.add(q)
    # мусор до кадра + два целых кадра + начало третьего → кадры розданы, хвост сохранён
    tail = relay.feed(b"junk" + j1 + b"mid" + j2 + b"\xff\xd8part")
    assert tail == b"\xff\xd8part"
    assert relay.last[1] == j2
    assert q.get_nowait() == j1 and q.get_nowait() == j2
    # отстающий зритель: очередь полна → теряем старый кадр, берём новый (лаг не копится)
    relay.feed(j1)
    relay.feed(j2)
    relay.feed(j3)
    assert q.qsize() == 2
    assert q.get_nowait() == j2 and q.get_nowait() == j3


def test_camera_relays_are_keyed_by_camera_not_by_card():
    """Ключ реестра ретрансляторов — URL КАМЕРЫ, а не id карточки.

    На один принтер заводят две карточки (например под разные сопла). Ключ по карточке поднял бы
    к одной камере два апстрима — то есть вернул бы ровно тот шторм подключений к :8080, от
    которого камера принтера и отваливалась (фидбэк 20.07)."""
    one = webapp._cam_relay_for("http://10.0.0.5:8080/mjpeg")
    same = webapp._cam_relay_for("http://10.0.0.5:8080/mjpeg")
    other = webapp._cam_relay_for("http://10.0.0.9:8080/mjpeg")
    try:
        assert one is same
        assert one is not other
        # кадры одной камеры не видны зрителям другой
        one.feed(b"\xff\xd8AAAA\xff\xd9")
        assert one.last is not None and other.last is None
    finally:
        webapp._cam_relays.clear()


# ── таймлапс: сборка URL скачивания (протокол разведан 20.07) ───────────────────────────────
def test_timelapse_url_build(monkeypatch):
    """1036 отдаёт относительный путь с ОБМАНЧИВЫМ префиксом: picture/ИМЯ или video/ИМЯ.mp4.
    Реальный файл ВСЕГДА video/ИМЯ.mp4 и качается одним роутом /download с X-Token."""
    monkeypatch.setattr(webapp.config, "ELEGOO_PRINTER_IP", "192.168.1.50")
    monkeypatch.setattr(webapp.config, "ELEGOO_ACCESS_CODE", "CODE1")
    # picture/ НЕ значит «видео нет» — тот же ролик лежит в video/ и качается (проверено живьём)
    assert webapp._timelapse_file_name("picture/job.gcode2026") == "video/job.gcode2026.mp4"
    assert webapp._timelapse_file_name("video/job.gcode2026.mp4") == "video/job.gcode2026.mp4"
    url = webapp._timelapse_abs_url("picture/job.gcode2026")
    assert url.startswith("http://192.168.1.50/download?X-Token=CODE1&file_name=")
    assert "video%2Fjob.gcode2026.mp4" in url
    # пустое поле истории = видео действительно не записано
    assert webapp._timelapse_has_video("") is False
    assert webapp._timelapse_has_video("picture/x") is True
    assert webapp._timelapse_abs_url("") == ""
    # абсолютный URL не трогаем
    assert webapp._timelapse_abs_url("http://x/y.mp4") == "http://x/y.mp4"


def test_timelapse_url_needs_ip_and_code(monkeypatch):
    """Без IP или кода доступа URL не построить — молча отдаём пусто, а не кривую ссылку."""
    monkeypatch.setattr(webapp.config, "ELEGOO_PRINTER_IP", "")
    monkeypatch.setattr(webapp.config, "ELEGOO_ACCESS_CODE", "CODE1")
    assert webapp._timelapse_abs_url("picture/job") == ""
    monkeypatch.setattr(webapp.config, "ELEGOO_PRINTER_IP", "10.0.0.1")
    monkeypatch.setattr(webapp.config, "ELEGOO_ACCESS_CODE", "")
    assert webapp._timelapse_abs_url("picture/job") == ""
