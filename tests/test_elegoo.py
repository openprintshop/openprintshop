"""Тест MQTT-примитивов и парсинга статуса принтера Elegoo (c5f8ec5f). Без сети."""
import json
import socket
import struct

from app import config, elegoo

# ВАЖНО про адреса в этом файле: тесты идут с БОЕВЫМ .env, то есть config.ELEGOO_PRINTER_IP —
# адрес живого CC2. Поэтому «настроенным» принтер в тестах делаем ТОЛЬКО через сентинелы ниже:
# так тест не зависит от машины, а путь, забывший про переданный эндпоинт, виден по адресу.
_IP1, _CODE1 = "10.98.0.1", "CODE1"


def _configure_printer(monkeypatch, *, ip: str = _IP1, code: str = _CODE1, serial: str = "SN1"):
    """Принтер по умолчанию = сентинел, а не реальная машина из .env."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", ip)
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", code)
    monkeypatch.setattr(config, "ELEGOO_SERIAL", serial)


def test_rem_len_varint():
    assert elegoo._rem_len(0) == b"\x00"
    assert elegoo._rem_len(127) == b"\x7f"
    assert elegoo._rem_len(128) == b"\x80\x01"
    assert elegoo._rem_len(16383) == b"\xff\x7f"
    assert elegoo._rem_len(16384) == b"\x80\x80\x01"


def test_connect_pkt_structure():
    pkt = elegoo._connect_pkt("cid", "elegoo", "secret")
    assert pkt[0] == 0x10                      # CONNECT
    assert b"MQTT" in pkt and b"elegoo" in pkt and b"secret" in pkt
    assert pkt[9] == 0xC2                       # connect flags: user+pass+clean


def test_subscribe_pkt_structure():
    pkt = elegoo._subscribe_pkt("elegoo/#", packet_id=7)
    assert pkt[0] == 0x82                       # SUBSCRIBE (with required flags)
    assert b"elegoo/#" in pkt
    assert pkt[-1] == 0x00                       # QoS 0


def test_parse_publish():
    topic = "elegoo/SN123/api_status"
    payload = json.dumps({"method": 6000, "result": {"extruder": {"temperature": 42}}}).encode()
    data = struct.pack(">H", len(topic)) + topic.encode() + payload
    t, obj = elegoo._parse_publish(0x30, data)         # QoS0 → без packet id
    assert t == topic
    assert obj["method"] == 6000 and obj["result"]["extruder"]["temperature"] == 42


def test_deep_merge_accumulates_deltas():
    state = {}
    elegoo._deep_merge(state, {"extruder": {"temperature": 26}})
    elegoo._deep_merge(state, {"heater_bed": {"temperature": 25}})
    elegoo._deep_merge(state, {"extruder": {"temperature": 200, "target": 210}})
    assert state == {"extruder": {"temperature": 200, "target": 210},
                     "heater_bed": {"temperature": 25}}


def test_summarize_fields():
    s = elegoo.summarize({"extruder": {"temperature": 200, "target": 210},
                          "heater_bed": {"temperature": 60}})
    assert s["extruder_temp"] == 200 and s["extruder_target"] == 210 and s["bed_temp"] == 60


# Срез реального ответа 1002 + вещания canvas_info (захваты docs/captures/, 12.07 и 20.07).
_FULL_1002 = {
    "extruder": {"filament_detect_enable": 1, "filament_detected": 0, "target": 0, "temperature": 36},
    "fans": {"aux_fan": {"speed": 0.0}, "box_fan": {"speed": 0.5}, "controller_fan": {"speed": 0.0},
             "fan": {"speed": 1.0}, "heater_fan": {"speed": 0.0}},
    "gcode_move": {"extruder": 0.0, "speed": 3000, "speed_mode": 1, "x": 52.5, "y": 264.0, "z": 80.0},
    "heater_bed": {"target": 0, "temperature": 32},
    "led": {"status": 1},
    "machine_status": {"exception_status": [], "progress": 0, "status": 1, "sub_status": 0},
    "ztemperature_sensor": {"temperature": 29},
    "tool_head": {"homed_axes": "xyz"},
    "external_device": {"camera": True, "type": "0303", "u_disk": False},
    "canvas_info": {"active_canvas_id": 0, "active_tray_id": 2, "auto_refill": False, "canvas_list": [
        {"canvas_id": 0, "connected": 1, "tray_list": [
            {"brand": "ELEGOO", "filament_code": "0x0006", "filament_color": "#000000",
             "filament_name": "PLA Matte", "filament_type": "PLA",
             "max_nozzle_temp": 230, "min_nozzle_temp": 190, "status": 1, "tray_id": 0},
            {"brand": "ELEGOO", "filament_code": "0x0100", "filament_color": "#F88D36",
             "filament_name": "PETG", "filament_type": "PETG",
             "max_nozzle_temp": 260, "min_nozzle_temp": 230, "status": 1, "tray_id": 2},
        ]}]},
}


def test_extras_full_snapshot():
    e = elegoo.extras(_FULL_1002)
    assert e["light"] == 1
    assert e["filament_detected"] == 0 and e["filament_sensor_on"] == 1
    assert e["chamber_temp"] == 29
    # скорости вентиляторов: доля 0..1 → проценты, отдаём все пять
    assert e["fans"] == {"model": 100, "aux": 0, "box": 50, "controller": 0, "heater": 0}
    # обе шкалы прошивки: 0..1 (пользовательские) и 0..255 (системные) → нормируем в проценты
    assert elegoo._fan_pct(1.0) == 100 and elegoo._fan_pct(0.5) == 50 and elegoo._fan_pct(0.0) == 0
    assert elegoo._fan_pct(255) == 100 and elegoo._fan_pct(127.5) == 50
    assert elegoo._fan_pct(None) is None
    assert e["position"] == {"x": 52.5, "y": 264.0, "z": 80.0}
    assert e["exceptions"] == []
    assert e["homed_axes"] == "xyz"
    assert e["u_disk"] is False
    # слоты: нумерация UI = tray_id+1 (как у кнопок выгрузки), сортировка по номеру
    assert [s["slot"] for s in e["slots"]] == [1, 3]
    assert e["slots"][1] == {"slot": 3, "brand": "ELEGOO", "name": "PETG", "type": "PETG",
                             "color": "#F88D36", "temp_min": 230, "temp_max": 260, "status": 1}
    assert e["active_slot"] == 3                        # active_tray_id=2 → слот 3


def test_cached_status_connection_aware():
    """Фидбэк 20.07 («дудосим принтер»): пока соединение подписчика живо, кэш актуален
    независимо от возраста кадра; max_age — только грация на реконнект. Никаких новых
    подключений cached_status не делает ни в одном исходе."""
    import time as _t
    orig = dict(elegoo.session().live)
    try:
        # соединение живо, кадр старый → кэш АКТУАЛЕН (в простое принтер молчит законно)
        elegoo.session().live.update({"state": {"heater_bed": {"temperature": 25}}, "ts": _t.time() - 300,
                             "serial": "SN", "connected": True})
        cs = elegoo.cached_status(6.0)
        assert cs and cs["ok"] and cs["online"] and cs["connected"] is True
        assert cs["state"]["heater_bed"]["temperature"] == 25
        # соединение упало, кадры недавние → грация max_age (переживаем реконнект)
        elegoo.session().live["connected"] = False
        elegoo.session().live["ts"] = _t.time() - 3
        cs = elegoo.cached_status(6.0)
        assert cs and cs["ok"] and cs["connected"] is False
        # ни соединения, ни свежих кадров → None (оффлайн)
        elegoo.session().live["ts"] = _t.time() - 300
        assert elegoo.cached_status(6.0) is None
        # соединение живо, но кадров ещё не было → честный «онлайн без данных», не None
        elegoo.session().live.update({"state": {}, "ts": 0.0, "connected": True})
        cs = elegoo.cached_status(6.0)
        assert cs and cs["ok"] and cs["online"] and cs["state"] == {}
    finally:
        elegoo.session().live.clear()
        elegoo.session().live.update(orig)


def test_extras_empty_and_partial():
    # пустой снимок (принтер в простое ещё ничего не вещал) → ни одного выдуманного ключа
    assert elegoo.extras({}) == {}
    # частичная дельта: только то, что реально пришло
    e = elegoo.extras({"ztemperature_sensor": {"temperature": 26},
                       "canvas_info": {"active_tray_id": -1}})
    assert e == {"chamber_temp": 26, "active_slot": 0}   # −1 = ничего не подаётся → слот 0
    # булев status в led не считается числом — поле опускаем, а не приводим наугад
    assert "light" not in elegoo.extras({"led": {"status": True}})


def test_enabled_gate(monkeypatch):
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "")
    assert elegoo.enabled() is False
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.5")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "code")
    assert elegoo.enabled() is True


def test_get_status_disabled(monkeypatch):
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")     # выключено → без сети, без исключения
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "")
    r = elegoo.get_status()
    assert r["ok"] is False and "не настроен" in r["error"]


def test_camera_url_explicit_override_wins(monkeypatch):
    monkeypatch.setattr(config, "PRINTER_CAMERA_URL", "http://cam.local/stream")
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.5")
    assert elegoo.camera_url() == "http://cam.local/stream"


def test_camera_url_derived_from_printer_ip(monkeypatch):
    monkeypatch.setattr(config, "PRINTER_CAMERA_URL", "")
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.5")
    monkeypatch.delenv("ELEGOO_CAMERA_PORT", raising=False)
    monkeypatch.delenv("ELEGOO_CAMERA_PATH", raising=False)
    assert elegoo.camera_url() == "http://192.168.0.5:8080/mjpeg"   # дефолт SDCP Centauri


def test_camera_url_empty_without_ip(monkeypatch):
    monkeypatch.setattr(config, "PRINTER_CAMERA_URL", "")
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    assert elegoo.camera_url() == ""


def test_publish_pkt():
    pkt = elegoo._publish_pkt("a/b", b'{"x":1}')
    assert pkt[0] == 0x30                       # PUBLISH QoS0
    assert b"a/b" in pkt and b'{"x":1}' in pkt


def test_list_files_parses(monkeypatch):
    cap = {}

    def fake_request(method, params=None, timeout=8.0, **kw):
        cap["method"], cap["params"], cap["kw"] = method, params, kw
        return {"ok": True, "result": {"file_list": [                  # форма из захвата 12.07
            {"type": "file", "filename": "a.gcode", "size": 10, "print_time": 120, "layer": 5,
             "total_filament_used": 10.39,
             "color_map": [{"color": "#F08000", "name": "PETG", "t": 1}]},
            {"type": "folder", "filename": "dir"},
            {"type": "file", "filename": "b.gcode", "size": 20, "print_time": 60}]}}

    monkeypatch.setattr(elegoo, "request", fake_request)
    r = elegoo.list_files()
    assert cap["method"] == 1044 and cap["params"]["storage_media"] == "local"
    assert cap["kw"].get("wait_data") is True   # выборка ждёт данные, а не пустой ACK (edde64e4)
    assert [f["name"] for f in r["files"]] == ["a.gcode", "b.gcode"]   # папка отфильтрована
    a = r["files"][0]
    assert a["grams"] == 10.39 and a["layer"] == 5
    assert a["colors"] == [{"hex": "#F08000", "type": "PETG", "extruder": 1}]
    assert r["files"][1]["grams"] == 0 and r["files"][1]["colors"] == []   # поля отсутствуют → дефолты


def test_passive_harvest_cache_and_lookup():
    """Путь A: ответы 1044/1036 из потока подписчика кэшируются по имени файла и достаются
    через harvested_info (граммы/время/цвета из 1044 + реальная длительность/статус из 1036)."""
    elegoo.session().harvest["files"].clear()
    elegoo.session().harvest["history"].clear()
    elegoo.session().harvest_api_response("elegoo/SN/0cli/api_response", {
        "method": elegoo.METHOD_LIST_FILES, "result": {"file_list": [
            {"type": "file", "filename": "job.gcode", "total_filament_used": 12.3, "print_time": 600,
             "color_map": [{"color": "#F08000", "name": "PETG", "t": 1}]}]}})
    elegoo.session().harvest_api_response("elegoo/SN/0cli/api_response", {
        "method": elegoo.METHOD_JOB_HISTORY, "result": {"history_task_list": [
            {"task_name": "job.gcode", "begin_time": 1000, "end_time": 1720, "task_status": 1}]}})
    info = elegoo.harvested_info("job.gcode")
    assert info["grams"] == 12.3 and info["print_time"] == 600
    assert info["colors"][0]["type"] == "PETG"
    assert info["duration_sec"] == 720 and info["ok"] is True
    assert elegoo.harvested_info("nope.gcode") is None            # неизвестный файл → None
    # не api_response / не наш метод — кэш не трогаем
    elegoo.session().harvest_api_response("elegoo/SN/x/api_status", {"method": 6000, "result": {}})
    assert set(elegoo.session().harvest["files"].keys()) == {"job.gcode"}


def test_job_history_parses(monkeypatch):
    def fake_request(method, params=None, timeout=8.0, **kw):
        return {"ok": True, "result": {"error_code": 0, "history_task_list": [
            {"task_name": "ok.gcode", "begin_time": 1000, "end_time": 1691, "task_status": 1,
             "time_lapse_video_url": "video/ok.mp4"},
            {"task_name": "fail.gcode", "begin_time": 2000, "end_time": 2100, "task_status": 2}]}}

    monkeypatch.setattr(elegoo, "request", fake_request)
    jobs = elegoo.job_history()["jobs"]
    assert jobs[0]["name"] == "ok.gcode" and jobs[0]["duration_sec"] == 691
    assert jobs[0]["status"] == 1 and jobs[0]["ok"] is True
    assert jobs[0]["time_lapse_video_url"] == "video/ok.mp4"
    assert jobs[1]["ok"] is False and jobs[1]["duration_sec"] == 100   # task_status 2 = брак/стоп


def test_start_print_envelope(monkeypatch):
    cap = {}

    def fake_request(method, params=None, timeout=8.0, **kw):
        cap["method"], cap["params"], cap["kw"] = method, params, kw
        return {"ok": True, "result": {}}

    monkeypatch.setattr(elegoo, "request", fake_request)
    elegoo.start_print("x.gcode", tray_id=2, filament_t=1)
    assert cap["method"] == 1020
    assert cap["kw"].get("command") is True     # команда: без ответа → «отправлено» (edde64e4)
    assert cap["params"]["filename"] == "x.gcode" and cap["params"]["storage_media"] == "local"
    sm = cap["params"]["config"]["slot_map"][0]
    assert sm["tray_id"] == 2 and sm["t"] == 1


def test_start_print_requires_filename(monkeypatch):
    monkeypatch.setattr(elegoo, "request", lambda *a, **k: {"ok": True})   # сеть не дёргаем
    assert elegoo.start_print("")["ok"] is False


def test_control_unconfigured_does_not_send(monkeypatch):
    # method-код команды не задан в .env → НЕ шлём наугад, возвращаем понятную ошибку
    for var in ("ELEGOO_METHOD_PAUSE", "ELEGOO_METHOD_STOP", "ELEGOO_METHOD_LIGHT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(elegoo, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должно слаться")))
    r = elegoo.control("pause")
    assert r["ok"] is False and "не разведана" in r["error"]
    assert elegoo.control_method("pause") is None


def test_control_unknown_action():
    assert elegoo.control("frobnicate")["ok"] is False


def test_control_pause_sends_configured_method(monkeypatch):
    monkeypatch.setenv("ELEGOO_METHOD_PAUSE", "1021")
    cap = {}

    def fake_request(method, params=None, timeout=8.0, **kw):
        cap["method"], cap["params"], cap["kw"] = method, params, kw
        return {"ok": True, "result": {}}

    monkeypatch.setattr(elegoo, "request", fake_request)
    assert elegoo.control_method("pause") == 1021
    r = elegoo.control("pause")
    assert r["ok"] is True
    assert cap["method"] == 1021 and cap["params"] == {}
    assert cap["kw"].get("command") is True


def test_control_light_envelope(monkeypatch):
    monkeypatch.setenv("ELEGOO_METHOD_LIGHT", "403")
    cap = {}

    def fake_request(method, params=None, timeout=8.0, **kw):
        cap["method"], cap["params"] = method, params
        return {"ok": True, "result": {}}

    monkeypatch.setattr(elegoo, "request", fake_request)
    # формат света сверен захватом ElegooSlicer: {"power": 1|0} (а не LightStatus/SecondLight)
    elegoo.control("light", on=False)
    assert cap["method"] == 403
    assert cap["params"] == {"power": 0}
    elegoo.control("light", on=True)
    assert cap["params"] == {"power": 1}


def test_control_bad_method_env_is_ignored(monkeypatch):
    monkeypatch.setenv("ELEGOO_METHOD_STOP", "not-a-number")
    monkeypatch.setattr(elegoo, "request", lambda *a, **k: {"ok": True})
    assert elegoo.control_method("stop") is None
    assert elegoo.control("stop")["ok"] is False


def test_unload_filament_tray_mapping(monkeypatch):
    # UI-слот → tray_id = слот−1 (сверено захватом: слот 3 → tray_id 2), method 2002
    monkeypatch.setenv("ELEGOO_METHOD_UNLOAD", "2002")
    cap = {}

    def fake_request(method, params=None, timeout=8.0, **kw):
        cap["method"], cap["params"] = method, params
        return {"ok": True, "result": {}}

    monkeypatch.setattr(elegoo, "request", fake_request)
    r = elegoo.unload_filament(3)
    assert r["ok"] is True
    assert cap["method"] == 2002
    assert cap["params"] == {"canvas_id": 0, "tray_id": 2}
    elegoo.unload_filament(1)
    assert cap["params"] == {"canvas_id": 0, "tray_id": 0}


def test_unload_filament_bad_slot(monkeypatch):
    monkeypatch.setenv("ELEGOO_METHOD_UNLOAD", "2002")
    monkeypatch.setattr(elegoo, "request", lambda *a, **k: {"ok": True})
    assert elegoo.unload_filament(0)["ok"] is False      # вне 1..4
    assert elegoo.unload_filament(5)["ok"] is False
    assert elegoo.unload_filament("x")["ok"] is False


def test_unload_filament_not_configured(monkeypatch):
    monkeypatch.delenv("ELEGOO_METHOD_UNLOAD", raising=False)
    assert elegoo.unload_filament(1)["ok"] is False      # код не задан → не шлём наугад


def test_enrich_status_maps_centauri_to_miniapp_shape():
    # реальный снимок телеметрии Centauri (print_status.* + fans.fan.speed)
    state = {
        "fans": {"fan": {"speed": 99.0}},
        "gcode_move": {"speed": 1470, "x": 95.0, "y": 131.9, "z": 28.0},
        "print_status": {"print_duration": 7391, "total_duration": 7494,
                         "current_layer": 140, "remaining_time_sec": 240},
        "extruder": {"temperature": 250},
    }
    ov = elegoo.enrich_status(state, now=1_700_000_000.0)
    # прогресс по времени: 7391 / (7391 + 240) ≈ 0.9685
    assert abs(ov["virtual_sdcard"]["progress"] - 7391 / 7631) < 1e-6
    assert ov["info"]["current_layer"] == 140 and "total_layer" not in ov["info"]  # всего слоёв нет
    assert ov["remaining"] == "4 мин"
    assert ov["elapsed"] == "2 ч 4 мин"          # total_duration=7494с → 2 ч 4 мин
    assert ov["fan"] == 99
    assert "eta_at" in ov and "started_at" in ov
    # активная печать → Klipper-флаг print_stats.state, который читает карточка принтера на хабе
    assert ov["print_stats"]["state"] == "printing"


def test_enrich_status_sets_printing_state_and_filename():
    # remaining_time_sec > 0 → печатает; имя задания (если прислано) пробрасываем в filename
    state = {"print_status": {"print_duration": 100, "remaining_time_sec": 300,
                              "filename": "order_42.gcode"}}
    ov = elegoo.enrich_status(state, now=1_700_000_000.0)
    assert ov["print_stats"] == {"state": "printing", "filename": "order_42.gcode"}


def test_enrich_status_idle_printer_is_empty():
    # принтер не печатает → нет полей времени/прогресса/состояния, без падения
    assert elegoo.enrich_status({}) == {}
    ov = elegoo.enrich_status({"fans": {"fan": {"speed": 0}}})
    assert ov == {"fan": 0} and "virtual_sdcard" not in ov and "print_stats" not in ov
    # завершённая печать (remaining=0, прогресс=1) НЕ считается активной
    done = elegoo.enrich_status({"print_status": {"print_duration": 500, "remaining_time_sec": 0}})
    assert "print_stats" not in done


def test_cached_status_fresh_stale_and_copy():
    """Кэш фонового подписчика: свежий снимок отдаётся копией, устаревший/пустой → None (фолбэк)."""
    import time as _t
    saved = dict(elegoo.session().live)
    try:
        with elegoo.session().live_lock:
            elegoo.session().live["state"] = {"extruder": {"temperature": 200}}
            elegoo.session().live["ts"] = _t.time()
            elegoo.session().live["serial"] = "SN1"
        c = elegoo.cached_status(6.0)
        assert c and c["ok"] and c["cached"] and c["serial"] == "SN1"
        assert c["state"]["extruder"]["temperature"] == 200
        c["state"]["extruder"]["temperature"] = 0           # копия — кэш не должен меняться
        assert elegoo.session().live["state"]["extruder"]["temperature"] == 200
        with elegoo.session().live_lock:                              # устаревший снимок → None
            elegoo.session().live["ts"] = _t.time() - 100
        assert elegoo.cached_status(6.0) is None
        with elegoo.session().live_lock:                              # пустой кэш → None
            elegoo.session().live["state"] = {}
            elegoo.session().live["ts"] = _t.time()
        assert elegoo.cached_status(6.0) is None
    finally:
        with elegoo.session().live_lock:
            elegoo.session().live.update(saved)


def test_has_snapshot_early_exit():
    # без сопла рано выходить нельзя (ещё нет ничего полезного)
    assert elegoo._has_snapshot({}) is False
    assert elegoo._has_snapshot({"heater_bed": {"temperature": 25}}) is False
    # одно сопло — ещё ждём «живой» блок (стол/печать/обдув)
    assert elegoo._has_snapshot({"extruder": {"temperature": 200}}) is False
    # сопло + стол → достаточно (idle-снимок)
    assert elegoo._has_snapshot({"extruder": {}, "heater_bed": {}}) is True
    # сопло + статус печати → достаточно (Centauri не всегда шлёт heater_bed в каждом снимке)
    assert elegoo._has_snapshot({"extruder": {}, "print_status": {"print_duration": 1}}) is True
    assert elegoo._has_snapshot({"extruder": {}, "fans": {"fan": {"speed": 50}}}) is True


def test_merge_response_status_into_live():
    """Ответы 1002/2005 слайсеру видны всем подписчикам — мержим их в живой кэш, как api_status.
    Чужие методы и ответы с ошибкой не трогают кэш."""
    orig_state, orig_ts = elegoo.session().live["state"], elegoo.session().live["ts"]
    try:
        elegoo.session().live["state"] = {}
        elegoo.session().merge_response_status({"id": 2, "method": 2005, "result": {
            "error_code": 0, "canvas_info": {"active_tray_id": 1, "canvas_list": []}}})
        assert elegoo.session().live["state"]["canvas_info"]["active_tray_id"] == 1
        elegoo.session().merge_response_status({"id": 4, "method": 1002, "result": {
            "error_code": 0, "led": {"status": 1}}})
        assert elegoo.session().live["state"]["led"]["status"] == 1
        # 1044 — не статусный ответ, в кэш не идёт (его дело — харвест файлов)
        elegoo.session().merge_response_status({"method": 1044, "result": {"error_code": 0, "file_list": []}})
        assert "file_list" not in elegoo.session().live["state"]
        # ответ с ошибкой принтера кэш не портит
        elegoo.session().merge_response_status({"method": 1002, "result": {"error_code": 5, "led": {"status": 0}}})
        assert elegoo.session().live["state"]["led"]["status"] == 1
        # эхо нашего же запроса (result нет вовсе) — игнор
        elegoo.session().merge_response_status({"method": 1002, "id": 9})
        assert elegoo.session().live["state"]["led"]["status"] == 1
    finally:
        elegoo.session().live["state"], elegoo.session().live["ts"] = orig_state, orig_ts


def test_harvested_files_fallback():
    """Фолбэк списка файлов: активный 1044 молчит → отдаём пассивно собранное."""
    with elegoo.session().harvest_lock:
        orig = dict(elegoo.session().harvest)
        elegoo.session().harvest.update({"files": {}, "history": {}, "ts": 0.0})
    try:
        assert elegoo.harvested_files() is None
        elegoo.session().harvest_api_response("elegoo/SN/0cli/api_response", {"method": 1044, "result": {
            "file_list": [{"type": "file", "filename": "a.gcode", "size": 10, "print_time": 60,
                           "layer": 2, "total_filament_used": 1.5, "color_map": []}]}})
        hv = elegoo.harvested_files()
        assert hv and hv["ok"] and hv["harvested"] and hv["files"][0]["name"] == "a.gcode"
    finally:
        with elegoo.session().harvest_lock:
            elegoo.session().harvest.clear()
            elegoo.session().harvest.update(orig)


def test_request_bound_path(monkeypatch):
    """Привязанный путь request(): публикует на наш канал и ждёт ответ из _pending по id.
    Команда с error_code=0 → подтверждённый успех (не unconfirmed); данные → отдаются."""
    sent = []

    class FakeSock:
        def sendall(self, b): sent.append(b)

    monkeypatch.setattr(elegoo.session(), "sock", FakeSock())
    monkeypatch.setattr(elegoo.session(), "bound", True)
    _configure_printer(monkeypatch, serial="SN")
    monkeypatch.setitem(elegoo.session().live, "connected", True)

    # ответ принтера подкладываем в очередь ожидания, как это делает поток подписчика
    def fake_get(timeout=None):
        rid = next(iter(elegoo.session().pending))
        return {"id": rid, "method": 1029, "result": {"error_code": 0}}
    monkeypatch.setattr(elegoo.queue.Queue, "get", lambda self, timeout=None: fake_get())

    r = elegoo.request(1029, {"power": 1}, timeout=2.0, command=True)
    assert r["ok"] and not r.get("unconfirmed")          # свет ПОДТВЕРЖДЁН, не «вслепую»
    assert sent and b'"method": 1029' in sent[0] and b'0clinbbot01/api_request' in sent[0]


def test_request_bound_error_code(monkeypatch):
    class FakeSock:
        def sendall(self, b): pass
    monkeypatch.setattr(elegoo.session(), "sock", FakeSock())
    monkeypatch.setattr(elegoo.session(), "bound", True)
    _configure_printer(monkeypatch, serial="SN")
    monkeypatch.setitem(elegoo.session().live, "connected", True)
    monkeypatch.setattr(elegoo.queue.Queue, "get",
                        lambda self, timeout=None: {"id": next(iter(elegoo.session().pending)),
                                                    "method": 1029, "result": {"error_code": 7}})
    r = elegoo.request(1029, {"power": 1}, timeout=2.0, command=True)
    assert not r["ok"] and r["err_code"] == 7            # ошибку принтера показываем честно


def test_request_falls_back_when_unbound(monkeypatch):
    """Нет привязки → уходим в transient-путь (разовое соединение)."""
    monkeypatch.setattr(elegoo.session(), "bound", False)
    _configure_printer(monkeypatch, serial="SN")
    called = {}

    def fake_transient(*a, **k):
        called["hit"] = True
        return {"ok": True, "result": {}}
    monkeypatch.setattr(elegoo, "_request_transient", fake_transient)
    r = elegoo.request(1044, {}, wait_data=True)
    assert called.get("hit") and r["ok"]


def test_manual_commands_params(monkeypatch):
    """Прогрев/оси/вентиляторы (1028/1026/1027/1030): формат params и шкалы. request замокан."""
    sent = []
    monkeypatch.setattr(elegoo, "request",
                        lambda m, p=None, t=8.0, **k: sent.append((m, p, k)) or {"ok": True, "result": {"error_code": 0}})
    monkeypatch.setattr(elegoo, "enabled", lambda: True)
    monkeypatch.setattr(elegoo.config, "ELEGOO_SERIAL", "SN")

    elegoo.set_temperature("extruder", 210)
    assert sent[-1][0] == 1028 and sent[-1][1] == {"extruder": 210} and sent[-1][2]["command"]
    elegoo.set_temperature("heater_bed", 0)
    assert sent[-1][1] == {"heater_bed": 0}
    elegoo.home("XZ")
    assert sent[-1][0] == 1026 and sent[-1][1] == {"homed_axes": "xz"}
    elegoo.move_axis("y", -10)
    assert sent[-1][0] == 1027 and sent[-1][1] == {"axes": "y", "distance": -10.0}
    # вентилятор: UI-проценты → шкала 0..255 (100%→255, 50%→127.5)
    elegoo.set_fan("model", 100)
    assert sent[-1][0] == 1030 and sent[-1][1] == {"fan": 255.0}
    elegoo.set_fan("aux", 50)
    assert sent[-1][1] == {"aux_fan": 127.5}


def test_manual_commands_validation(monkeypatch):
    monkeypatch.setattr(elegoo, "request", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(elegoo, "enabled", lambda: True)
    monkeypatch.setattr(elegoo.config, "ELEGOO_SERIAL", "SN")
    assert not elegoo.set_temperature("nozzle", 200)["ok"]        # неизвестный target
    assert not elegoo.set_temperature("extruder", 999)["ok"]      # выше потолка 320
    assert not elegoo.move_axis("w", 10)["ok"]                    # нет такой оси
    assert not elegoo.move_axis("z", 0)["ok"]                     # нулевое перемещение
    assert not elegoo.set_fan("controller", 50)["ok"]            # системный вентилятор не UI
    # проценты вентилятора клампятся в 0..100
    sent = []
    monkeypatch.setattr(elegoo, "request", lambda m, p=None, t=8.0, **k: sent.append(p) or {"ok": True})
    elegoo.set_fan("box", 250)
    assert sent[-1] == {"box_fan": 255.0}


def test_upload_gcode_validation(monkeypatch):
    """Заливка: валидация до сети (расширение/хранилище/пустой файл) — принтер зря не дёргаем."""
    monkeypatch.setattr(elegoo, "enabled", lambda: True)
    monkeypatch.setattr(elegoo.config, "ELEGOO_PRINTER_IP", "10.0.0.1")
    monkeypatch.setattr(elegoo.config, "ELEGOO_ACCESS_CODE", "CODE")
    assert not elegoo.upload_gcode("x.gcode", storage="cloud")["ok"]      # нет такого хранилища
    assert not elegoo.upload_gcode("/nope/missing.gcode")["ok"]           # файла нет


def test_upload_gcode_chunking(tmp_path, monkeypatch):
    """Чанки идут по порядку в ОДНО соединение, с верными Content-Range/MD5 (ловушка 429)."""
    import hashlib
    f = tmp_path / "part.gcode"
    payload = b"G1 X1\n" * 60000                       # ~360 КБ → 2 чанка по 256 КБ
    f.write_bytes(payload)
    monkeypatch.setattr(elegoo, "enabled", lambda: True)
    monkeypatch.setattr(elegoo.config, "ELEGOO_PRINTER_IP", "10.0.0.1")
    monkeypatch.setattr(elegoo.config, "ELEGOO_ACCESS_CODE", "CODE")

    seen = []
    conns = []

    class FakeConn:
        def __init__(self, *a, **k):
            conns.append(self)
        def request(self, method, path, body=None, headers=None):
            seen.append((method, path, headers["Content-Range"], headers["X-File-MD5"], len(body)))
        def getresponse(self):
            class R:
                status = 200
                @staticmethod
                def read():
                    return b'{"error_code": 0}'
            return R()
        def close(self):
            pass
    monkeypatch.setattr(elegoo.http.client, "HTTPConnection", FakeConn)

    r = elegoo.upload_gcode(f, chunk=256 * 1024)
    assert r["ok"] and r["size"] == len(payload) and r["name"] == "part.gcode"
    assert len(conns) == 1                              # ОДНО соединение на всю заливку (иначе 429)
    assert [s[0] for s in seen] == ["PUT"] * len(seen) and seen[0][1] == "/upload"
    # диапазоны идут подряд и покрывают файл целиком
    total = len(payload)
    assert seen[0][2] == f"bytes 0-{256 * 1024 - 1}/{total}"
    assert seen[-1][2].endswith(f"-{total - 1}/{total}")
    assert sum(s[4] for s in seen) == total
    # MD5 — от всего файла и одинаковый во всех чанках
    md5 = hashlib.md5(payload).hexdigest()              # noqa: S324
    assert {s[3] for s in seen} == {md5}


def test_upload_gcode_reports_printer_error(tmp_path, monkeypatch):
    """error_code принтера переводим в человеческую причину, а не «HTTP 200»."""
    f = tmp_path / "a.gcode"
    f.write_bytes(b"G1\n")
    monkeypatch.setattr(elegoo, "enabled", lambda: True)
    monkeypatch.setattr(elegoo.config, "ELEGOO_PRINTER_IP", "10.0.0.1")
    monkeypatch.setattr(elegoo.config, "ELEGOO_ACCESS_CODE", "CODE")

    class FakeConn:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): pass
        def getresponse(self):
            class R:
                status = 200
                @staticmethod
                def read():
                    return b'{"error_code": 9004}'      # MD5CheckFailed
            return R()
        def close(self): pass
    monkeypatch.setattr(elegoo.http.client, "HTTPConnection", FakeConn)
    r = elegoo.upload_gcode(f)
    assert not r["ok"] and "MD5" in r["error"]


def test_delete_file_sends_array(monkeypatch):
    """1047 принимает file_path МАССИВОМ — строка даёт error_code 1003 InvalidParameter
    (проверено на живом принтере 20.07). Имя берём как в списке 1044, без каталога."""
    sent = []
    monkeypatch.setattr(elegoo, "request",
                        lambda m, p=None, t=8.0, **k: sent.append((m, p)) or {"ok": True})
    elegoo.delete_file("job.gcode")
    assert sent[-1] == (1047, {"storage_media": "local", "file_path": ["job.gcode"]})
    elegoo.delete_file(["a.gcode", "b.gcode"])           # пакетное удаление — тем же полем
    assert sent[-1][1]["file_path"] == ["a.gcode", "b.gcode"]
    assert not elegoo.delete_file("")["ok"] and not elegoo.delete_file([])["ok"]


def test_load_filament_params(monkeypatch):
    """2001 — пара к 2002: {canvas_id, tray_id}, tray_id = слот−1."""
    sent = []
    monkeypatch.setattr(elegoo, "request",
                        lambda m, p=None, t=8.0, **k: sent.append((m, p)) or {"ok": True})
    elegoo.load_filament(3)
    assert sent[-1] == (2001, {"canvas_id": 0, "tray_id": 2})
    assert not elegoo.load_filament(9)["ok"]             # вне диапазона 1..4


def test_upload_gcode_custom_name(tmp_path, monkeypatch):
    """На принтер уходит ОРИГИНАЛЬНОЕ имя, а не имя временной копии (иначе в памяти принтера
    оказывался файл вида upload_<таймштамп>_….gcode)."""
    f = tmp_path / "upload_1784579418__real.gcode"      # временная копия со служебным префиксом
    f.write_bytes(b"G1\n")
    monkeypatch.setattr(elegoo, "enabled", lambda: True)
    monkeypatch.setattr(elegoo.config, "ELEGOO_PRINTER_IP", "10.0.0.1")
    monkeypatch.setattr(elegoo.config, "ELEGOO_ACCESS_CODE", "CODE")
    seen = {}

    class FakeConn:
        def __init__(self, *a, **k): pass
        def request(self, method, path, body=None, headers=None):
            seen["name"] = headers["X-File-Name"]
        def getresponse(self):
            class R:
                status = 200
                @staticmethod
                def read():
                    return b'{"error_code": 0}'
            return R()
        def close(self): pass
    monkeypatch.setattr(elegoo.http.client, "HTTPConnection", FakeConn)

    r = elegoo.upload_gcode(f, name="real.gcode")
    assert r["ok"] and r["name"] == "real.gcode"
    assert seen["name"] == "real.gcode"                  # X-File-Name — оригинальное имя


# ───────────────────── эндпоинт: кому адресован вызов (волна 2 цикла 2) ─────────────────────
# Раньше все сетевые точки модуля читали .env сами, и вызов «для второго принтера» физически
# уходил на первый. Теперь адресата можно передать параметром. Сентинелы второго принтера
# намеренно не совпадают ни с .env, ни с сентинелами первого: путь, потерявший ep, виден по
# адресу, а не маскируется случайным совпадением.

_EP2 = elegoo.Endpoint(ip="10.99.0.2", code="CODE2", serial="SN2", mqtt_user="user2",
                       camera_port="9090", camera_path="cam.mjpg")


class _Sock:
    """MQTT-сокет по сценарию: отдаёт записанные байты, дальше — socket.timeout.

    Таймаут, а не EOF: на нём циклы чтения elegoo.py выходят из ожидания сразу, и тест не
    зависит ни от одного таймера."""

    def __init__(self, script: bytes = b""):
        self.inbox = bytearray(script)
        self.sent: list[bytes] = []

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent.append(bytes(data))

    def recv(self, size):
        if not self.inbox:
            raise socket.timeout
        chunk = bytes(self.inbox[:size])
        del self.inbox[:size]
        return chunk

    def close(self):
        pass


def test_ep_defaults_to_the_env_printer(monkeypatch):
    """_ep() без аргумента = принтер по умолчанию, ровно теми же значениями, что читались раньше."""
    _configure_printer(monkeypatch)
    monkeypatch.setattr(config, "ELEGOO_MQTT_USER", "elegoo")
    monkeypatch.setenv("ELEGOO_CAMERA_PORT", "8081")
    monkeypatch.setenv("ELEGOO_CAMERA_PATH", "stream")
    e = elegoo._ep()
    assert (e.ip, e.code, e.serial, e.mqtt_user) == (_IP1, _CODE1, "SN1", "elegoo")
    assert (e.camera_port, e.camera_path) == ("8081", "stream")   # нормализует уже camera_url
    assert elegoo._ep(_EP2) is _EP2                               # явный эндпоинт не пересобираем


def test_enabled_gate_is_per_endpoint():
    """Гейт «настроен» считается по тому эндпоинту, о котором спрашивают."""
    assert elegoo.enabled(_EP2) is True
    assert elegoo.enabled(elegoo.Endpoint(ip="10.99.0.2")) is False      # нет кода доступа
    assert elegoo.enabled(elegoo.Endpoint(code="CODE2")) is False        # некуда идти


def test_is_subscriber_endpoint_compares_addresses(monkeypatch):
    """Совпадение с подписчиком — по АДРЕСУ. Пустой адрес подписчика: идти некуда, путать не с чем."""
    _configure_printer(monkeypatch)
    assert elegoo.session().is_subscriber_endpoint(elegoo.Endpoint(ip=_IP1, code=_CODE1)) is True
    assert elegoo.session().is_subscriber_endpoint(_EP2) is False
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    assert elegoo.session().is_subscriber_endpoint(_EP2) is True


def test_foreign_endpoint_never_rides_the_subscriber_socket(monkeypatch):
    """ГЛАВНОЕ ПРАВИЛО ВОЛНЫ: запрос чужому принтеру НЕ идёт по сокету подписчика.

    Быстрый путь request() публикует по постоянному соединению фонового подписчика, а подписчик
    в процессе один — он висит на ПЕРВОМ принтере. Пусти сюда чужой эндпоинт, и тело уйдёт в
    тему `elegoo/<серийник второго>/…` по сокету первого: команда не выполнится нигде, а при
    command=True вызов вернёт {ok: True, unconfirmed: True} — «успех», не покинувший первой
    машины. Именно так «стоп» на карточке второго принтера отчитался бы об остановке, оставив
    боевую печать идти дальше."""
    live = _Sock()
    _configure_printer(monkeypatch)
    monkeypatch.setattr(elegoo.session(), "sock", live)
    monkeypatch.setattr(elegoo.session(), "bound", True)
    monkeypatch.setitem(elegoo.session().live, "connected", True)
    seen = {}

    def fake_transient(method, params=None, timeout=8.0, **kw):
        seen.update(method=method, ep=kw.get("ep"))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(elegoo, "_request_transient", fake_transient)

    r = elegoo.request(1022, {}, timeout=2.0, command=True, ep=_EP2)   # «стоп» второму принтеру

    assert live.sent == []                       # по сокету ПЕРВОГО принтера не ушло ни байта
    assert seen["method"] == 1022                # вызов ушёл в разовое соединение…
    assert seen["ep"] is _EP2                    # …со своим эндпоинтом
    assert r["ok"] and not r.get("unconfirmed")


def test_endpoint_of_the_live_printer_keeps_the_fast_path(monkeypatch):
    """Барьер к предыдущему тесту: эндпоинт БОЕВОГО принтера обязан идти прежним быстрым путём.

    Иначе «предохранитель» вышел бы боком: каждая команда мастера открывала бы принтеру лишнее
    соединение (шторм connect/disconnect — больная тема проекта) и теряла бы подтверждение."""
    live = _Sock()
    _configure_printer(monkeypatch)
    monkeypatch.setattr(elegoo.session(), "sock", live)
    monkeypatch.setattr(elegoo.session(), "bound", True)
    monkeypatch.setitem(elegoo.session().live, "connected", True)
    monkeypatch.setattr(elegoo, "_request_transient",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("лишнее соединение")))
    monkeypatch.setattr(elegoo.queue.Queue, "get",
                        lambda self, timeout=None: {"id": next(iter(elegoo.session().pending)),
                                                    "method": 1029, "result": {"error_code": 0}})

    same = elegoo.Endpoint(ip=_IP1, code=_CODE1, serial="SN1")
    r = elegoo.request(1029, {"power": 1}, timeout=2.0, command=True, ep=same)

    assert r["ok"] and not r.get("unconfirmed")
    assert live.sent and b"elegoo/SN1/0clinbbot01/api_request" in live.sent[0]


def test_transient_request_dials_the_given_endpoint(monkeypatch):
    """Разовое соединение открывается на адрес ЭНДПОИНТА, и серийник в темах — его же.

    Адрес и серийник обязаны ехать вместе: уедет адрес без серийника — тело придёт на нужный
    принтер в тему чужой машины и потеряется молча."""
    sock = _Sock()
    opened = {}

    def fake_open(timeout, ep=None):
        opened["ep"] = ep
        return sock

    monkeypatch.setattr(elegoo, "_open", fake_open)
    elegoo._request_transient(elegoo.METHOD_LIST_FILES, {}, 0.05, wait_data=True, ep=_EP2)

    assert opened["ep"] is _EP2
    assert b"elegoo/SN2/#" in sock.sent[0]                                    # SUBSCRIBE
    assert b"elegoo/SN2/newbor_bot/api_request" in sock.sent[1]               # PUBLISH


def test_get_status_dials_the_given_endpoint(monkeypatch):
    """Разовый опрос статуса ходит на адрес эндпоинта и авторизуется ЕГО учёткой."""
    dialed = []
    sock = _Sock(bytes([0x20, 0x02, 0x00, 0x00]))          # CONNACK «принято», дальше тишина

    def create_connection(address, timeout=None, *a, **kw):
        dialed.append(address)
        return sock

    monkeypatch.setattr(elegoo.socket, "create_connection", create_connection)
    r = elegoo.get_status(timeout=0.05, ep=_EP2)

    assert dialed == [("10.99.0.2", elegoo.MQTT_PORT)]
    assert r["ok"] is True and r["serial"] == "SN2"
    assert b"user2" in sock.sent[0] and b"CODE2" in sock.sent[0]   # CONNECT: логин и код доступа


def test_upload_gcode_uploads_to_the_given_endpoint(tmp_path, monkeypatch):
    """Заливка (HTTP :80) уходит на адрес эндпоинта, X-Token — его код доступа."""
    f = tmp_path / "job.gcode"
    f.write_bytes(b"G1 X1\n")
    _configure_printer(monkeypatch)
    seen = {}

    class FakeConn:
        def __init__(self, host, port=None, **k):
            seen["host"], seen["port"] = host, port

        def request(self, method, path, body=None, headers=None):
            seen["token"] = (headers or {}).get("X-Token")

        def getresponse(self):
            class R:
                status = 200

                @staticmethod
                def read():
                    return b'{"error_code": 0}'
            return R()

        def close(self):
            pass

    monkeypatch.setattr(elegoo.http.client, "HTTPConnection", FakeConn)
    assert elegoo.upload_gcode(f, ep=_EP2)["ok"] is True
    assert (seen["host"], seen["port"]) == ("10.99.0.2", 80)
    assert seen["token"] == "CODE2"


def test_camera_url_of_a_foreign_endpoint_ignores_the_env_override(monkeypatch):
    """PRINTER_CAMERA_URL описывает камеру принтера из .env и чужому эндпоинту не достаётся —
    иначе мастер смотрел бы на первый принтер, думая, что видит второй."""
    monkeypatch.setattr(config, "PRINTER_CAMERA_URL", "http://cam.local/stream")
    _configure_printer(monkeypatch)
    assert elegoo.camera_url() == "http://cam.local/stream"          # свой принтер — как раньше
    assert elegoo.camera_url(_EP2) == "http://10.99.0.2:9090/cam.mjpg"   # ведущий слэш дописан


def test_file_grams_of_a_foreign_endpoint_skips_process_caches(monkeypatch):
    """Граммы чужого принтера мимо процессных кэшей: и пассивный сбор, и снимок списка файлов
    принадлежат подписчику, то есть первому принтеру. Совпадение имён файлов — обычное дело,
    и отдать чужие граммы значило бы соврать в расчёте стоимости."""
    def _boom(name):
        raise AssertionError("кэш первого принтера в ответе за второй")

    monkeypatch.setattr(elegoo.session(), "harvested_info", _boom)
    monkeypatch.setattr(elegoo.session(), "file_grams_snap", {"koala.gcode": 1.0})
    seen = {}

    def fake_list_files(timeout=8.0, ep=None):
        seen["ep"] = ep
        return {"ok": True, "files": [{"name": "koala.gcode", "grams": 42.0}]}

    monkeypatch.setattr(elegoo, "list_files", fake_list_files)
    assert elegoo.file_grams("koala.gcode", _EP2) == 42.0        # не 1.0 из чужого снимка
    assert seen["ep"] is _EP2
    assert elegoo.file_grams("ghost.gcode", _EP2) is None


def test_wrappers_forward_the_endpoint(monkeypatch):
    """Высокоуровневые обёртки не решают за вызывающего, какому принтеру он адресовался."""
    monkeypatch.setenv("ELEGOO_METHOD_PAUSE", "1021")
    monkeypatch.setenv("ELEGOO_METHOD_UNLOAD", "2002")
    seen = []
    monkeypatch.setattr(elegoo, "request",
                        lambda m, p=None, t=8.0, **k: seen.append(k.get("ep")) or
                        {"ok": True, "result": {}})

    elegoo.control("pause", ep=_EP2)
    elegoo.start_print("x.gcode", ep=_EP2)
    elegoo.set_temperature("extruder", 200, ep=_EP2)
    elegoo.home("xyz", ep=_EP2)
    elegoo.move_axis("z", 5, ep=_EP2)
    elegoo.set_fan("model", 50, ep=_EP2)
    elegoo.load_filament(1, ep=_EP2)
    elegoo.unload_filament(1, ep=_EP2)
    elegoo.delete_file("a.gcode", ep=_EP2)
    elegoo.file_info("a.gcode", ep=_EP2)
    elegoo.list_files(8.0, _EP2)
    elegoo.job_history(8.0, _EP2)
    elegoo.generate_timelapse("video/a.mp4", 5.0, _EP2)
    assert seen == [_EP2] * 13

    seen.clear()                                    # ep=None = принтер по умолчанию, как раньше
    elegoo.list_files()
    elegoo.control("pause")
    assert seen == [None, None]


def test_latest_timelapse_url_forwards_the_endpoint(monkeypatch):
    seen = {}

    def fake_history(timeout=8.0, ep=None):
        seen["ep"] = ep
        return {"ok": True, "jobs": [{"name": "A", "time_lapse_video_url": "http://p/a.mp4",
                                      "time_lapse_video_status": 2, "end_time": 1}]}

    monkeypatch.setattr(elegoo, "job_history", fake_history)
    assert elegoo.latest_timelapse_url(None, 8.0, _EP2)["url"] == "http://p/a.mp4"
    assert seen["ep"] is _EP2
