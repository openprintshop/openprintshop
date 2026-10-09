"""Фичи, подсмотренные у прошивки сообщества CC2 и сделанные своими (03.10.2026).

План и инвентарь: docs/Перспективные разработки/cc2-community-features-2026-10-03.md. Две группы:

  • стоковый протокол из официального SDK Elegoo — коды ошибок, состояния машины, режим
    скорости (1031), автодозаправка (2004), флешка как носитель, карта слотов при старте;
  • прошивка сообщества — автоопределение, способности драйвера, клиент её HTTP-службы,
    замер винтов, сессионный Z-offset и ручки пульта поверх всего этого.

К железу тесты не ходят: протокол — подменой elegoo.request, служба сообщества — подменой
её HTTP-слоя (cc2community._http).
"""
import json

import pytest
from conftest import admin_req, body, stub_auth

from app import cc2community, elegoo, printerdrv
from app.printerdrv import Caps
from app.web import printerext, printerjob, printerlive

ADMIN = 4242


@pytest.fixture
def admin(monkeypatch):
    stub_auth(monkeypatch, _verify_admin=ADMIN)


# ─────────────────────────── 1. справочники SDK ───────────────────────────

def test_error_text_speaks_human():
    assert elegoo.error_text(1009) == "Принтер занят (код 1009)"
    assert "выравнивания стола" in elegoo.error_text(1026)
    assert elegoo.error_text(4321) == "Принтер вернул ошибку (код 4321)"
    assert elegoo.error_text("мусор") == "Принтер вернул ошибку (мусор)"


def test_machine_phase_names_state_and_pause():
    ph = elegoo.machine_phase({"machine_status": {"status": 2, "sub_status": 2502}})
    assert ph["label"] == "Печать" and ph["detail"] == "Пауза" and ph["paused"] is True
    ph = elegoo.machine_phase({"machine_status": {"status": 2, "sub_status": 1405}})
    assert ph["detail"] == "Прогрев стола" and ph["paused"] is False
    ph = elegoo.machine_phase({"machine_status": {"status": 5, "sub_status": 2901}})
    assert ph["label"] == "Автовыравнивание стола" and ph["detail"] == ""   # без повтора
    assert elegoo.machine_phase({}) is None
    assert elegoo.machine_phase({"machine_status": {"status": "x"}}) is None
    assert elegoo.machine_phase({"machine_status": {"status": 99}})["label"] == "Состояние 99"


def test_extras_carry_speed_mode_refill_and_machine():
    st = {"gcode_move": {"speed_mode": 2, "x": 1.0},
          "machine_status": {"status": 1, "sub_status": 0, "exception_status": []},
          "canvas_info": {"auto_refill": True, "canvas_list": []}}
    ex = elegoo.extras(st)
    assert ex["speed_mode"] == 2 and ex["auto_refill"] is True
    assert ex["machine"]["label"] == "Свободен"
    # мусор вместо режима — поля просто нет
    assert "speed_mode" not in elegoo.extras({"gcode_move": {"speed_mode": 9}})


def test_file_list_keeps_creation_time():
    files = elegoo._parse_file_list({"file_list": [
        {"type": "file", "filename": "a.gcode", "create_time": 1784503283,
         "color_map": [{"color": "#FFFFFF", "name": "PLA", "t": 0}]}]})
    assert files[0]["created"] == 1784503283
    assert files[0]["colors"] == [{"hex": "#FFFFFF", "type": "PLA", "extruder": 0}]


# ─────────────────────────── 2. стоковые команды ───────────────────────────

@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_request(method, params=None, timeout=8.0, *, wait_data=False, command=False, ep=None):
        calls.append((method, params))
        return {"ok": True, "result": {"file_list": []}}
    monkeypatch.setattr(elegoo, "request", fake_request)
    return calls


def test_speed_mode_and_refill_use_sdk_codes(sent):
    assert elegoo.set_speed_mode(3)["ok"]
    assert elegoo.set_auto_refill(True)["ok"]
    assert sent == [(1031, {"mode": 3}), (2004, {"auto_refill": True})]
    assert elegoo.set_speed_mode(7)["ok"] is False and len(sent) == 2   # мимо — не шлём


def test_usb_storage_reaches_list_start_and_delete(sent):
    elegoo.list_files(storage="u-disk")
    elegoo.start_print("x.gcode", storage="u-disk", plate="B")
    elegoo.delete_file(["a.gcode", "b.gcode"], storage="u-disk")
    elegoo.list_files(storage="../etc")                 # неизвестный носитель → своя память
    assert sent[0][1]["storage_media"] == "u-disk"
    assert sent[1][1]["storage_media"] == "u-disk" and sent[1][1]["config"]["print_layout"] == "B"
    assert sent[2] == (1047, {"storage_media": "u-disk", "file_path": ["a.gcode", "b.gcode"]})
    assert sent[3][1]["storage_media"] == "local"


def test_elegoo_declares_stock_caps():
    c = printerdrv.ElegooDriver.caps
    assert c.speed_mode and c.auto_refill and c.usb
    assert not (c.exclude_object or c.bed_mesh or c.live_tune or c.gcode or c.slot_edit)


# ─────────────────────────── 3. автоопределение прошивки ───────────────────────────

DISCOVERY = {
    "platform": "cc2-community", "community_firmware": "4.2", "cc2_control": "1.1.31",
    "api_version": 1, "services": {"cc2_control": 8081, "moonraker_compat": 7125},
    "capabilities": {"object_exclusion": True, "gcode_console": True, "canvas": True},
}


class _P:
    def __init__(self, address="10.9.9.9"):
        self.address, self.access_code, self.serial, self.id, self.kind = (
            address, "CODE", "SN", 1, "elegoo")


def test_parse_info_rejects_foreign_service():
    assert cc2community.parse_info({"platform": "moonraker"}) is None
    assert cc2community.parse_info("<html>") is None
    info = cc2community.parse_info(DISCOVERY)
    assert (info.firmware, info.control, info.api_version) == ("4.2", "1.1.31", 1)


def test_driver_turns_on_community_caps_from_cache(monkeypatch):
    monkeypatch.setattr(cc2community, "_get", lambda h, p, timeout=3.0: DISCOVERY)
    assert cc2community.probe("10.9.9.9") is not None
    drv = printerdrv.for_printer(_P())
    assert drv.caps.exclude_object and drv.caps.bed_mesh and drv.caps.gcode and drv.caps.slot_edit
    assert drv.caps.speed_mode, "стоковые способности не должны пропасть"
    assert drv.describe()["community"]["control"] == "1.1.31"
    # чужой адрес — стоковые способности, без «community»
    other = printerdrv.for_printer(_P("10.9.9.8"))
    assert not other.caps.exclude_object and "community" not in other.describe()


def test_failed_probe_is_cached_as_absent(monkeypatch):
    def boom(*a, **k):
        raise cc2community.CommunityError("connection refused")
    monkeypatch.setattr(cc2community, "_get", boom)
    assert cc2community.probe("10.9.9.9") is None
    assert cc2community.cached("10.9.9.9") is None
    assert printerdrv.for_printer(_P()).caps.exclude_object is False


def test_ensure_probe_is_off_in_tests_and_does_not_block(monkeypatch):
    started = []
    monkeypatch.setattr(cc2community.threading, "Thread",
                        lambda *a, **k: started.append(k) or type("T", (), {"start": lambda s: None})())
    cc2community.ensure_probe("10.9.9.9")
    assert started == [], "автофикстура гасит автопробу"
    monkeypatch.setattr(cc2community, "AUTO_PROBE", True)
    cc2community.ensure_probe("10.9.9.9")
    cc2community.ensure_probe("10.9.9.9")          # вторая — пока первая «идёт», не дублирует
    assert len(started) == 1


def test_http_goes_direct_without_env_proxy(monkeypatch):
    """На машине владельца HTTP(S)_PROXY указывает в туннель; к принтеру ходим мимо него."""
    seen = {}

    class Conn:
        def __init__(self, host, port, timeout=None):
            seen["addr"] = (host, port)

        def request(self, method, path, body=None, headers=None):
            seen["req"] = (method, path, body)

        def getresponse(self):
            return type("R", (), {"status": 200, "read": lambda s: b'{"platform":"cc2-community"}'})()

        def close(self):
            pass
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10809")
    monkeypatch.setattr(cc2community.http.client, "HTTPConnection", Conn)
    assert cc2community._get("http://10.9.9.9/", "/api/v1/system/info")["platform"] == "cc2-community"
    assert seen["addr"] == ("10.9.9.9", 8081)


def test_post_carries_csrf_marker(monkeypatch):
    """Служба новее 1.1.31 отклоняет изменяющий запрос без X-CC2-Request: 1."""
    seen = {}

    class Conn:
        def __init__(self, host, port, timeout=None):
            pass

        def request(self, method, path, body=None, headers=None):
            seen["headers"] = dict(headers or {})

        def getresponse(self):
            return type("R", (), {"status": 202, "read": lambda s: b'{"accepted":true}'})()

        def close(self):
            pass
    monkeypatch.setattr(cc2community.http.client, "HTTPConnection", Conn)
    cc2community.action("10.9.9.9", "system:motors_off")
    assert seen["headers"]["X-CC2-Request"] == "1"
    assert seen["headers"]["Content-Type"].startswith("text/plain")


def test_http_error_text_comes_from_service(monkeypatch):
    class Conn:
        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return type("R", (), {"status": 409,
                                  "read": lambda s: b'{"accepted":false,"error":"Axis must be homed first"}'})()

        def close(self):
            pass
    monkeypatch.setattr(cc2community.http.client, "HTTPConnection", Conn)
    with pytest.raises(cc2community.CommunityError, match="homed"):
        cc2community.action("10.9.9.9", "move:X:10")


# ─────────────────────────── 4. разбор ответов службы ───────────────────────────

OBJECTS = {"id": 203, "result": {"status": {"exclude_object": {
    "objects": [{"name": "Cube_id_0_copy_0", "center": [100, 100],
                 "polygon": [[90, 90], [110, 90], [110, 110], [90, 110]]},
                {"name": "Cube_id_0_copy_1", "center": [150, 100], "polygon": []}],
    "excluded_objects": ["Cube_id_0_copy_1"], "current_object": "Cube_id_0_copy_0"}}}}

MESH = {"result": {"status": {"bed_mesh": {
    "profile_name": "default", "mesh_min": [10, 10], "mesh_max": [246, 246],
    "probed_matrix": [[0.0, 0.1], [-0.05, 0.02]],
    "profiles": {"default": {"points": [[0.0, 0.1], [-0.05, 0.02]],
                             "mesh_params": {"min_x": 10, "min_y": 10, "max_x": 246, "max_y": 246}},
                 "default1": {"points": [[0.2]], "mesh_params": {}}}}}}}


def test_objects_and_mesh_parse(monkeypatch):
    replies = {"/api/exclude-objects": OBJECTS, "/api/mesh": MESH}
    monkeypatch.setattr(cc2community, "_get", lambda h, p, timeout=3.0: replies[p])
    o = cc2community.objects("h")
    assert [x["name"] for x in o["objects"]] == ["Cube_id_0_copy_0", "Cube_id_0_copy_1"]
    assert o["excluded"] == ["Cube_id_0_copy_1"] and o["current"] == "Cube_id_0_copy_0"
    m = cc2community.mesh("h")
    assert m["active"]["range"] == pytest.approx(0.15)
    assert m["profiles"]["default"]["side"] == "A" and m["profiles"]["default1"]["side"] == "B"


def test_exclude_refuses_names_the_firmware_would_reject(monkeypatch):
    sent = []
    monkeypatch.setattr(cc2community, "_post", lambda h, p, b="", timeout=3.0: sent.append((p, b)))
    cc2community.exclude("h", "Cube_id_0_copy_0")
    assert sent == [("/api/control", "object:exclude:Cube_id_0_copy_0")]
    with pytest.raises(cc2community.CommunityError):
        cc2community.exclude("h", 'x" ; M112')


CONSOLE_SCREWS = "\n".join(
    [f"// probe at {x},{y} is z={z}" for (x, y), zs in {
        (30, 30): (1.000, 1.002, 1.001), (230, 30): (1.050, 1.051, 1.049),
        (230, 225): (0.980, 0.981, 0.979), (30, 225): (1.010, 1.010, 1.011)}.items()
     for z in zs])


def test_screws_from_console_and_plan():
    v = cc2community.screws_from_console(CONSOLE_SCREWS)
    assert v == {"ПЛ": 1.001, "ПП": 1.050, "ЗП": 0.980, "ЗЛ": 1.010}
    plan = cc2community.screws_plan(v)
    fl = {r["code"]: r for r in plan["from_front_left"]}
    assert fl["ПЛ"]["um"] == 0 and fl["ПП"]["um"] == 49 and fl["ПП"]["action"] == "опустить"
    assert fl["ЗП"]["um"] == -21 and fl["ЗП"]["action"] == "поднять"
    assert fl["ЗЛ"]["action"] == "ок"
    # «минимум кручения» — самый большой поворот не больше половины размаха
    assert plan["peak_minimal_um"] <= plan["spread_um"] / 2 + 1
    assert plan["peak_minimal_um"] < plan["peak_um"]


def test_screws_incomplete_is_none():
    assert cc2community.screws_from_console("// probe at 30,30 is z=1.0") is None


def test_zoffset_session_limits_and_undo(monkeypatch):
    acts = []
    monkeypatch.setattr(cc2community, "action", lambda h, a: acts.append(a) or {"ok": True})
    for _ in range(10):
        cc2community.zoffset_adjust("h", 0.05)
    assert cc2community.zoffset_session("h") == 0.5
    with pytest.raises(cc2community.CommunityError, match="0.5"):
        cc2community.zoffset_adjust("h", 0.01)
    with pytest.raises(cc2community.CommunityError, match="шаг"):
        cc2community.zoffset_adjust("h", 0.2)
    cc2community.zoffset_undo("h")
    assert acts[-1] == "zoffset:undo:-0.500" and cc2community.zoffset_session("h") == 0.0


# ─────────────────────────── 5. ручки: стоковые ───────────────────────────

async def test_speed_endpoint_drives_the_printer(tmp_db, fake_printer, admin):
    r = body(await printerext._api_printer_speed(admin_req({"mode": 0})))
    assert r["ok"] and fake_printer.st.speed_mode == 0
    r = body(await printerext._api_printer_speed(admin_req({"mode": "быстро"})))
    assert r["ok"] is False and ("speed_mode", 0) in fake_printer.st.commands


async def test_speed_endpoint_respects_caps(tmp_db, admin):
    sim = printerdrv.FakePrinter(caps=Caps(status=True))
    printerdrv.use_driver(sim)
    r = body(await printerext._api_printer_speed(admin_req({"mode": 1})))
    assert r["ok"] is False and "не умеет" in r["error"]


async def test_refill_endpoint(tmp_db, fake_printer, admin):
    r = body(await printerext._api_printer_refill(admin_req({"on": True})))
    assert r["ok"] and fake_printer.st.auto_refill is True


async def test_off_refuses_fans_during_print_but_soft_stop_works(tmp_db, fake_printer, admin):
    fake_printer.begin("job.gcode", minutes=30)
    fake_printer.advance(5)
    r = body(await printerext._api_printer_off(admin_req({"what": "fans"})))
    assert r["ok"] is False and "Аварийный стоп" in r["error"]
    r = body(await printerext._api_printer_off(admin_req({"what": "soft_stop"})))
    assert r["ok"], r
    cmds = fake_printer.st.commands
    assert ("control", "stop", True) in cmds
    assert ("temp", "extruder", 0) in cmds and ("temp", "heater_bed", 0) in cmds
    assert ("fan", "box", 0) in cmds
    assert fake_printer.st.printing is False


async def test_off_heaters_idle(tmp_db, fake_printer, admin):
    r = body(await printerext._api_printer_off(admin_req({"what": "heaters"})))
    assert r["ok"] and r["done"] == ["нагрев выключен"]
    assert not any(c[0] == "control" for c in fake_printer.st.commands)


async def test_heat_presets_roundtrip(tmp_db, admin):
    r = body(await printerext._api_heat_presets_get(admin_req(query={})))
    assert [p["name"] for p in r["presets"]][:2] == ["PLA", "PETG"]
    mine = [{"name": "PETG HF", "nozzle": 250, "bed": 75}]
    r = body(await printerext._api_heat_presets_set(admin_req({"presets": mine})))
    assert r["ok"] and r["presets"] == mine
    r = body(await printerext._api_heat_presets_get(admin_req(query={})))
    assert r["presets"] == mine
    bad = body(await printerext._api_heat_presets_set(admin_req({"presets": [
        {"name": "PLA", "nozzle": 999, "bed": 60}]})))
    assert bad["ok"] is False
    r = body(await printerext._api_heat_presets_set(admin_req({"reset": True})))
    assert r["presets"][0]["name"] == "PLA"


async def test_print_validates_slot_map_against_file(tmp_db, fake_printer, admin):
    fake_printer.st.files["grogu.gcode"] = {"grams": 23, "print_time": 5000, "colors": [
        {"hex": "#E4BD68", "type": "PLA", "extruder": 0},
        {"hex": "#3F8E43", "type": "PLA", "extruder": 1}]}
    dup = [{"t": 0, "tray_id": 0}, {"t": 0, "tray_id": 1}]
    r = body(await printerjob._api_admin_printer_print(admin_req({"filename": "grogu.gcode", "slot_map": dup})))
    assert r["ok"] is False and "несколько слотов" in r["error"]
    short = [{"t": 0, "tray_id": 0}]
    r = body(await printerjob._api_admin_printer_print(admin_req({"filename": "grogu.gcode", "slot_map": short})))
    assert r["ok"] is False and "T1" in r["error"]
    full = [{"t": 0, "tray_id": 2}, {"t": 1, "tray_id": 3}]
    r = body(await printerjob._api_admin_printer_print(admin_req(
        {"filename": "grogu.gcode", "slot_map": full, "plate": "b", "bedlevel": True})))
    assert r["ok"], r
    start = next(c for c in fake_printer.st.commands if c[0] == "start")
    assert start[2]["slot_map"] == [{"canvas_id": 0, "t": 0, "tray_id": 2},
                                    {"canvas_id": 0, "t": 1, "tray_id": 3}]
    assert start[2]["plate"] == "B" and start[2]["bedlevel"] is True


async def test_print_rejects_bad_plate(tmp_db, fake_printer, admin):
    r = body(await printerjob._api_admin_printer_print(admin_req({"filename": "x.gcode", "plate": "C"})))
    assert r["ok"] is False and "A или B" in r["error"]


async def test_usb_files_list_and_multi_delete(tmp_db, fake_printer, admin):
    fake_printer.st.usb_files = {"usb1.gcode": {"grams": 1}, "usb2.gcode": {"grams": 2}}
    r = body(await printerlive._api_admin_printer_files(admin_req(query={"storage": "u-disk"})))
    assert [f["name"] for f in r["files"]] == ["usb1.gcode", "usb2.gcode"]
    r = body(await printerlive._api_admin_printer_delete(admin_req(
        {"names": ["usb1.gcode", "usb2.gcode"], "storage": "u-disk"})))
    assert r["ok"] and fake_printer.st.usb_files == {}


async def test_file_info_endpoint(tmp_db, fake_printer, admin):
    fake_printer.st.files["a.gcode"] = {"colors": [{"hex": "#000000", "type": "PLA", "extruder": 0}]}
    r = body(await printerext._api_printer_file_info(admin_req(query={"name": "a.gcode"})))
    assert r["ok"] and r["file"]["colors"][0]["extruder"] == 0


# ─────────────────────────── 6. ручки: прошивка сообщества ───────────────────────────

@pytest.fixture
def community(fake_printer, monkeypatch):
    """Симулятор «с прошивкой сообщества» + записывающая подмена её HTTP-слоя."""
    fake_printer.community_info = cc2community.parse_info(DISCOVERY).as_dict()
    fake_printer.caps = Caps(**{**fake_printer.caps.as_dict(),
                                **cc2community.driver_caps(cc2community.parse_info(DISCOVERY))})
    log = []
    replies = {"/api/exclude-objects": OBJECTS, "/api/mesh": MESH,
               "/api/console": {"busy": False, "completed": True, "success": True,
                                "command": "screws", "output": CONSOLE_SCREWS},
               "/api/printer": {"tuning": {"speed_percent": 100.0, "flow_percent": 98.0}}}

    def http(host, method, path, body=None, timeout=3.0, port=8081):
        log.append((method, path, body))
        if method == "GET":
            return replies[path]
        return {"accepted": True}
    monkeypatch.setattr(cc2community, "_http", http)
    return log


async def test_community_without_firmware_says_so(tmp_db, fake_printer, admin):
    r = body(await printerext._api_community_get(admin_req(query={"what": "mesh"})))
    assert r["ok"] is False and "не умеет" in r["error"]


async def test_community_exclude_needs_a_running_print(tmp_db, fake_printer, community, admin):
    r = body(await printerext._api_community_post(admin_req({"op": "exclude", "name": "Cube_id_0_copy_0"})))
    assert r["ok"] is False and "во время печати" in r["error"]
    fake_printer.begin("job.gcode", minutes=30)
    fake_printer.advance(1)
    r = body(await printerext._api_community_post(admin_req({"op": "exclude", "name": "Cube_id_0_copy_0"})))
    assert r["ok"], r
    assert ("POST", "/api/control", "object:exclude:Cube_id_0_copy_0") in community


async def test_community_reads(tmp_db, fake_printer, community, admin):
    r = body(await printerext._api_community_get(admin_req(query={"what": "objects"})))
    assert r["ok"] and r["current"] == "Cube_id_0_copy_0"
    r = body(await printerext._api_community_get(admin_req(query={"what": "mesh"})))
    assert r["ok"] and "default1" in r["profiles"]
    r = body(await printerext._api_community_get(admin_req(query={"what": "screws"})))
    assert r["ok"] and r["plan"]["peak_um"] == 49 and "output" not in r
    r = body(await printerext._api_community_get(admin_req(query={"what": "tuning"})))
    assert r["flow_percent"] == 98.0 and r["zoffset"] == 0.0
    r = body(await printerext._api_community_get(admin_req(query={"what": "nope"})))
    assert r["ok"] is False


async def test_community_actions_map_to_service_strings(tmp_db, fake_printer, community, admin):
    async def post(b):
        return body(await printerext._api_community_post(admin_req(b)))
    assert (await post({"op": "mesh_calibrate", "side": "B"}))["ok"]
    assert ("POST", "/api/console/command", "BED_MESH_CALIBRATE PROFILE=default1 BED_TEMP=60") in community
    assert (await post({"op": "screws_measure"}))["ok"]
    assert (await post({"op": "zoffset", "delta": -0.01}))["session"] == -0.01
    assert (await post({"op": "slot_edit", "slot": 2, "type": "PETG", "color": "#ff8800",
                        "tmin": 230, "tmax": 260}))["ok"]
    assert ("POST", "/api/control", "canvas:material:1:PETG:FF8800:230:260") in community
    bad = await post({"op": "slot_edit", "slot": 2, "type": "PETG", "color": "orange", "tmin": 230, "tmax": 260})
    assert bad["ok"] is False
    r = await post({"op": "tune", "speed": 150})
    assert r["ok"] is False and "во время печати" in r["error"]
    fake_printer.begin("job.gcode", minutes=30)
    fake_printer.advance(1)
    assert (await post({"op": "tune", "speed": 150}))["ok"]
    assert ("POST", "/api/control", "tune:speed:150") in community
    assert (await post({"op": "tune", "flow": 300}))["ok"] is False
    assert (await post({"op": "rm -rf"}))["ok"] is False


async def test_community_heat_targets_only_while_printing(tmp_db, fake_printer, community, admin):
    """Цели нагрева на ходу (их heaters:set): только в печати — в простое работает свой 1028;
    обе цели разом и в их пределах (сопло 0…300, стол 0…120)."""
    async def post(b):
        return body(await printerext._api_community_post(admin_req(b)))
    r = await post({"op": "heat", "nozzle": 215, "bed": 65})
    assert r["ok"] is False and "простое" in r["error"]
    fake_printer.begin("job.gcode", minutes=30)
    fake_printer.advance(1)
    assert (await post({"op": "heat", "nozzle": 215, "bed": 65}))["ok"]
    assert ("POST", "/api/control", "heaters:set:215:65") in community
    assert (await post({"op": "heat", "nozzle": 350, "bed": 65}))["ok"] is False
    assert (await post({"op": "heat", "nozzle": 215}))["ok"] is False
    assert (await post({"op": "heat", "nozzle": True, "bed": 60}))["ok"] is False


async def test_community_console_and_estop(tmp_db, fake_printer, community, admin):
    async def post(b):
        return body(await printerext._api_community_post(admin_req(b)))
    assert (await post({"op": "console", "command": "G28"}))["ok"]
    assert (await post({"op": "console", "command": "G28\nM112"}))["ok"] is False
    assert (await post({"op": "estop"}))["ok"]
    assert ("POST", "/api/control", "system:emergency_stop") in community


def test_discovery_payload_is_json_roundtrip():
    """describe() уходит во фронт JSON-ом — Info обязан сериализоваться."""
    info = cc2community.parse_info(DISCOVERY)
    assert json.loads(json.dumps(info.as_dict()))["caps"]["object_exclusion"] is True


# ─────────────────────────── 7. предел хода осей ───────────────────────────

class _Pos:
    """Драйвер с известной позицией головы — только то, что читает проверка хода."""
    kind = "elegoo"

    def __init__(self, x=250.0, homed="xyz"):
        self._x, self._homed = x, homed

    def status(self, max_age=6.0):
        return {"ok": True, "state": {"gcode_move": {"x": self._x, "y": 10.0, "z": 5.0},
                                      "tool_head": {"homed_axes": self._homed}}}


def test_move_bounds_refuse_only_when_position_is_known():
    assert "за пределы" in printerext._move_out_of_bounds(_Pos(250.0), "x", 10)
    assert printerext._move_out_of_bounds(_Pos(250.0), "x", -10) == ""
    assert printerext._move_out_of_bounds(_Pos(250.0, homed=""), "x", 10) == "", "не запаркована — не гадаем"
    assert "Z" in printerext._move_out_of_bounds(_Pos(), "z", -8)        # 5 − 8 < −2
    other = _Pos(250.0)
    other.kind = "moonraker"
    assert printerext._move_out_of_bounds(other, "x", 10) == "", "пределы — только CC2"


def test_probe_logs_only_changes(monkeypatch, caplog):
    """Перепроверка раз в 5 минут не должна писать «найдена» в лог на каждом круге."""
    import logging
    replies = [DISCOVERY, DISCOVERY, {**DISCOVERY, "cc2_control": "1.1.32"}]

    def fake_get(h, p, timeout=3.0):
        if not replies:
            raise cc2community.CommunityError("refused")
        return replies.pop(0)
    monkeypatch.setattr(cc2community, "_get", fake_get)
    with caplog.at_level(logging.INFO, logger="app.cc2community"):
        for _ in range(4):
            cc2community.probe("10.9.9.9")
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("найдена" in m for m in msgs) == 2, msgs     # первая находка + смена версии
    assert sum("больше не отвечает" in m for m in msgs) == 1, msgs


def test_labels_script_enables_both_keys(tmp_path, monkeypatch):
    """Скрипт меток включает и комментарии для допечатки, и EXCLUDE_OBJECT для снятия детали."""
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "enable_slicer_labels", pathlib.Path(__file__).resolve().parent.parent / "enable_slicer_labels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    proc = tmp_path / "ElegooSlicer" / "user" / "default" / "process"
    proc.mkdir(parents=True)
    (proc / "a.json").write_text(json.dumps({"inherits": "x", "gcode_label_objects": "1"}), encoding="utf-8")
    (proc / "b.json").write_text(json.dumps({"inherits": "x", "gcode_label_objects": "1",
                                             "exclude_object": "1"}), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(mod, "slicer_running", lambda: False)
    assert mod.main([]) == 0
    a = json.loads((proc / "a.json").read_text(encoding="utf-8"))
    assert a["exclude_object"] == "1" and a["gcode_label_objects"] == "1"
    assert len(list(proc.glob("a.json.bak-*"))) == 1
    assert not list(proc.glob("b.json.bak-*")), "уже включённый профиль не трогаем"


def test_screws_parse_by_corner_for_both_service_versions():
    """1.1.31 касается в X35/X225, ветка разработки — в X30/X230: винт у угла один."""
    v131 = "\n".join(f"// probe at {x},{y} is z={z}" for (x, y), z in {
        (35, 30): 1.0, (225, 30): 1.05, (225, 225): 0.98, (35, 225): 1.01}.items() for _ in range(3))
    got = cc2community.screws_from_console(v131)
    assert got == {"ПЛ": 1.0, "ПП": 1.05, "ЗП": 0.98, "ЗЛ": 1.01}
