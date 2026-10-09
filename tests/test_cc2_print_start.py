"""Старт печати через CC2 Control (шаг 3 синхронизации, 08.10.2026).

С картой слотов старт ведёт их служба (/api/gcode-files/print): сверяет карту с файлом,
флешку копирует в память и калибрует стол по-настоящему — стоковый 1020 флаг калибровки
принимает, но не выполняет. Без карты, без связи со службой (запрос не ушёл) и со службой
старше формата (отказ до старта) — прежний 1020. Остальные отказы — мастеру, без повтора.

К железу тесты не ходят: служба — подменой её HTTP-слоя (cc2community._raw/_http/_get).
"""
import json

import pytest
from conftest import admin_req, body, stub_auth

from app import cc2community, db, elegoo, printerdrv
from app.web import printerjob

HOST = "10.9.9.9"
DISCOVERY = {
    "platform": "cc2-community", "community_firmware": "4.2", "cc2_control": "1.1.31",
    "api_version": 1, "capabilities": {"file_manager": True, "gcode_thumbnails": True},
}
SM = [{"canvas_id": 0, "t": 0, "tray_id": 2}]
MISMATCH = "Canvas mapping does not match this G-code"


class _P:
    def __init__(self, address=HOST):
        self.address, self.access_code, self.serial, self.id, self.kind = (
            address, "CODE", "SN", 1, "elegoo")


class _Service:
    """Их старт: очередь ответов (статус, тело) или исключений, журнал тел запросов."""

    def __init__(self):
        self.bodies: list[str] = []
        self.answers: list = []
        self.inspect = {"tools": [0], "multicolour": False, "adaptive_mesh": False,
                        "filaments": [{"tool": 0, "color": "#FFFFFF", "material": "PLA"}]}

    def raw(self, host, path, body, timeout=3.0, port=8081):
        assert path == "/api/gcode-files/print"
        self.bodies.append(body)
        a = self.answers.pop(0) if self.answers else (
            202, {"accepted": True, "filename": body.split("\n")[1], "imported_from_usb": False})
        if isinstance(a, Exception):
            raise a
        status, obj = a
        return status, "application/json", json.dumps(obj).encode()

    def http(self, host, method, path, body=None, timeout=3.0, port=8081):
        assert path == "/api/gcode-files/inspect"
        return self.inspect


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(cc2community, "_get", lambda h, p, timeout=3.0: DISCOVERY)
    assert cc2community.probe(HOST) is not None
    s = _Service()
    monkeypatch.setattr(cc2community, "_raw", s.raw)
    monkeypatch.setattr(cc2community, "_http", s.http)
    return s


@pytest.fixture
def stock(monkeypatch):
    """Прежний 1020 — запоминает, с чем его позвали."""
    calls = []

    def start_print(filename, **kw):
        calls.append((filename, {k: v for k, v in kw.items() if k != "ep"}))
        return {"ok": True}
    monkeypatch.setattr(elegoo, "start_print", start_print)
    return calls


# ─────────────────────────── клиент службы ───────────────────────────

def test_gcode_print_body_and_answer(svc):
    r = cc2community.gcode_print(HOST, "u-disk", "dir/x.gcode", slot_map=[(0, 2), (1, 3)],
                                 plate="B", leveling="full")
    assert svc.bodies == ["usb\ndir/x.gcode\n0:2,1:3\nB\nfull\n1"]
    assert r["accepted"] is True
    for bad in ({"plate": "C"}, {"leveling": "fast"}):
        with pytest.raises(cc2community.CommunityError):
            cc2community.gcode_print(HOST, "local", "x.gcode", slot_map=[], **bad)
    with pytest.raises(cc2community.CommunityError):
        cc2community.gcode_print(HOST, "local", "x.gcode\nM112", slot_map=[])
    assert len(svc.bodies) == 1, "негодные параметры к службе не уходят"


def test_gcode_print_lost_answer_must_not_be_retried(monkeypatch):
    def raw(*a, **k):
        raise cc2community.CommunityError("прошивка сообщества недоступна: timed out")
    monkeypatch.setattr(cc2community, "_raw", raw)
    with pytest.raises(cc2community.CommunityError, match="не ответил") as e:
        cc2community.gcode_print(HOST, "local", "x.gcode", slot_map=[(0, 0)])
    assert not isinstance(e.value, cc2community.CommunityUnreachable)


def test_print_errors_speak_russian():
    assert cc2community.print_error_text("Starting a file requires an idle printer") == \
        "Принтер занят — старт только в простое"
    assert cc2community.print_error_text("Something new") == "Something new"
    assert cc2community.print_error_text("") == "CC2 Control не запустил печать"


# ─────────────────────────── драйвер ───────────────────────────

def test_driver_starts_through_service_with_a_map(svc, stock):
    drv = printerdrv.for_printer(_P())
    r = drv.start("x.gcode", slot_map=SM, plate="B", bedlevel=True)
    assert r == {"ok": True, "source": "cc2", "filename": "x.gcode", "imported_from_usb": False}
    assert svc.bodies == ["internal\nx.gcode\n0:2\nB\nfull\n1"], "флажок калибровки — полная сетка"
    drv.start("x.gcode", slot_map=SM)
    assert svc.bodies[-1] == "internal\nx.gcode\n0:2\nA\nsaved\n1", \
        "по умолчанию — сохранённая сетка стороны A, таймлапс пишется, как и 1020"
    assert stock == []


def test_driver_without_a_map_keeps_the_stock_start(svc, stock):
    drv = printerdrv.for_printer(_P())
    drv.start("x.gcode")                                   # «продолжить», мобильная админка
    drv.start("x.gcode", tray_id=2, filament_t=0)          # моноцвет без хаба
    assert svc.bodies == []
    assert stock == [("x.gcode", {}), ("x.gcode", {"tray_id": 2, "filament_t": 0})]


def test_stock_printer_starts_by_mqtt(stock, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("к службе сообщества без неё не ходим")
    monkeypatch.setattr(cc2community, "_raw", boom)
    r = printerdrv.for_printer(_P()).start("x.gcode", slot_map=SM, plate="A")
    assert r == {"ok": True} and stock == [("x.gcode", {"slot_map": SM, "plate": "A"})]


def test_unreachable_or_older_service_falls_back_to_mqtt(svc, stock):
    drv = printerdrv.for_printer(_P())
    svc.answers = [cc2community.CommunityUnreachable("refused")]
    assert drv.start("x.gcode", slot_map=SM, plate="B")["ok"]
    assert stock == [("x.gcode", {"slot_map": SM, "plate": "B"})], "запрос не ушёл — 1020 тем же"
    svc.answers = [(400, {"accepted": False, "error": "Invalid bed leveling mode"})]
    assert drv.start("x.gcode", slot_map=SM)["ok"] and len(stock) == 2, \
        "служба 1.1.31 не знает флага таймлапса и отказывает до старта"


def test_refusals_reach_the_master_and_are_not_retried(svc, stock):
    drv = printerdrv.for_printer(_P())
    svc.answers = [(409, {"accepted": False, "error": "Starting a file requires an idle printer"})]
    assert drv.start("x.gcode", slot_map=SM) == {"ok": False, "error": "Принтер занят — старт только в простое"}
    svc.answers = [cc2community.CommunityError("прошивка сообщества недоступна: timed out")]
    r = drv.start("x.gcode", slot_map=SM)
    assert r["ok"] is False and "не ответил" in r["error"]
    assert stock == [], "служба могла успеть — 1020 вслед не шлём"


def test_wider_map_is_trimmed_to_the_file_tools(svc, stock):
    """Допечатка без детали стартует с картой прежнего старта, а инструмент мог уйти с деталью:
    1020 лишнее пропускал молча, их служба требует ровно инструменты файла."""
    svc.answers = [(409, {"accepted": False, "error": MISMATCH})]
    wide = SM + [{"canvas_id": 0, "t": 1, "tray_id": 3}]
    assert printerdrv.for_printer(_P()).start("x.gcode", slot_map=wide)["ok"]
    assert svc.bodies == ["internal\nx.gcode\n0:2,1:3\nA\nsaved\n1",
                          "internal\nx.gcode\n0:2\nA\nsaved\n1"]


def test_short_map_is_the_masters_error(svc, stock):
    svc.inspect = {"tools": [0, 1], "filaments": [{"tool": 0}, {"tool": 1}]}
    svc.answers = [(409, {"accepted": False, "error": MISMATCH})]
    r = printerdrv.for_printer(_P()).start("x.gcode", slot_map=SM)
    assert r["ok"] is False and "Карта слотов" in r["error"]
    assert len(svc.bodies) == 1 and stock == []


def test_usb_start_reports_the_imported_name(svc, stock):
    svc.answers = [(202, {"accepted": True, "filename": "CC2_USB_0a1b2c3d_x.gcode",
                          "imported_from_usb": True})]
    r = printerdrv.for_printer(_P()).start("dir/x.gcode", slot_map=SM, storage="u-disk")
    assert r["filename"] == "CC2_USB_0a1b2c3d_x.gcode" and r["imported_from_usb"] is True
    assert svc.bodies[0].startswith("usb\ndir/x.gcode\n0:2\n")


# ─────────────────────────── ручка старта ───────────────────────────

async def test_start_settings_are_kept_under_the_printed_name(tmp_db, fake_printer, monkeypatch):
    """Флешку CC2 Control печатает из копии в памяти: задание придёт в журнал под её именем,
    и параметры старта должны лежать под ним же."""
    stub_auth(monkeypatch, _verify_admin=4242)
    fake_printer.st.usb_files["x.gcode"] = {"colors": [{"hex": "#FFFFFF", "type": "PLA", "extruder": 0}]}
    seen = []

    def start(filename, **kw):
        seen.append((filename, kw))
        return {"ok": True, "source": "cc2", "filename": "CC2_USB_0a1b2c3d_x.gcode",
                "imported_from_usb": True}
    fake_printer.start = start
    r = body(await printerjob._api_admin_printer_print(admin_req(
        {"filename": "x.gcode", "storage": "u-disk", "slot_map": SM, "plate": "B"})))
    assert r["ok"] and r["imported_from_usb"] is True
    assert seen[0][0] == "x.gcode"
    snap = await db.find_print_settings("CC2_USB_0a1b2c3d_x.gcode")
    assert snap and snap["start"]["plate"] == "B"
