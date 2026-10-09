"""Файлы на принтере через CC2 Control (шаг 3 синхронизации, 08.10.2026).

С прошивкой сообщества список файлов, разбор файла и превью отдаёт их служба (file_manager,
gcode_thumbnails): она видит память и флешку, вложенные папки и файлы мимо вендорской базы.
Без неё — прежний путь по MQTT; отказ службы тоже не тупик. Файлы из папок удаляет их служба
(только в простое), обычные — прежним 1047. Превью бот кэширует на диске и спрашивает по одному.

К железу тесты не ходят: служба — подменой её HTTP-слоя (cc2community._get/_http/_raw).
"""
import asyncio
import threading
import time

import pytest
from aiohttp import web
from conftest import admin_req, stub_auth

from app import cc2community, config, elegoo, printerdrv
from app.printerdrv import Caps
from app.web import printerext

ADMIN = 4242
HOST = "10.9.9.9"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32

DISCOVERY = {
    "platform": "cc2-community", "community_firmware": "4.2", "cc2_control": "1.1.40",
    "api_version": 1, "services": {"cc2_control": 8081},
    "capabilities": {"file_manager": True, "gcode_thumbnails": True},
}

LISTING = {
    "internal": {"available": True, "truncated": False, "count": 2, "total": 2, "files": [
        {"path": "Orca/b.gcode", "size": 2048, "modified": 1791000200},
        {"path": "a.gcode", "size": 1024, "modified": 1791000100}]},
    "usb": {"available": False, "truncated": False, "count": 0, "total": 0, "files": []},
}

INSPECT = {"tools": [0, 1], "multicolour": True, "adaptive_mesh": False, "filaments": [
    {"tool": 0, "color": "#FF0000FF", "material": "PLA", "mm": 1234.6},
    {"tool": 1, "color": "#00FF00", "material": "PETG"}]}


class _P:
    def __init__(self, address=HOST):
        self.address, self.access_code, self.serial, self.id, self.kind = (
            address, "CODE", "SN", 1, "elegoo")


def _found(monkeypatch, caps: dict) -> None:
    """Прошивка найдена на HOST с такими способностями (кэш автоопределения)."""
    monkeypatch.setattr(cc2community, "_get",
                        lambda h, p, timeout=3.0: {**DISCOVERY, "capabilities": caps})
    assert cc2community.probe(HOST) is not None


class _Service:
    """Подмена HTTP-слоя службы: ответы по пути (исключение — отказ) и журнал запросов."""

    def __init__(self):
        self.log: list = []
        self.replies: dict = {"/api/gcode-files": LISTING, "/api/gcode-files/inspect": INSPECT}

    def http(self, host, method, path, body=None, timeout=3.0, port=8081):
        self.log.append((method, path, body))
        r = self.replies.get(path, {"deleted": True})
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def service(monkeypatch):
    """Служба с file_manager и превью."""
    _found(monkeypatch, DISCOVERY["capabilities"])
    svc = _Service()
    monkeypatch.setattr(cc2community, "_http", svc.http)
    monkeypatch.setattr(cc2community, "_get",
                        lambda h, p, timeout=3.0: svc.http(h, "GET", p, timeout=timeout))
    monkeypatch.setattr(cc2community, "_post",
                        lambda h, p, b="", timeout=3.0: svc.http(h, "POST", p, b, timeout=timeout))
    return svc


@pytest.fixture
def mqtt(monkeypatch):
    """Прежний путь по MQTT — запоминает, что к нему обращались."""
    calls = []

    def list_files(timeout=8.0, ep=None, *, storage="local"):
        calls.append(("list", storage))
        return {"ok": True, "files": [{"name": "stock.gcode"}]}

    def file_info(filename, timeout=6.0, ep=None, *, storage="local"):
        calls.append(("info", filename))
        return {"ok": True, "file": {"name": filename, "colors": []}}

    def delete_file(filename, storage="local", timeout=6.0, ep=None):
        calls.append(("delete", storage, filename))
        return {"ok": True}
    monkeypatch.setattr(elegoo, "list_files", list_files)
    monkeypatch.setattr(elegoo, "file_info", file_info)
    monkeypatch.setattr(elegoo, "delete_file", delete_file)
    monkeypatch.setattr(elegoo, "harvested_files", lambda ep=None: None)
    return calls


# ─────────────────────────── клиент службы ───────────────────────────

def test_gcode_files_maps_storage_and_fields(service):
    r = cc2community.gcode_files(HOST, "local")
    assert r["ok"] and r["source"] == "cc2" and r["total"] == 2
    assert r["files"][0] == {"name": "Orca/b.gcode", "path": "Orca/b.gcode", "size": 2048,
                             "created": 1791000200}
    usb = cc2community.gcode_files(HOST, "u-disk")
    assert usb == {"ok": False, "error": "флешка не вставлена"}
    with pytest.raises(cc2community.CommunityError):
        cc2community.gcode_files(HOST, "sd-card")


def test_gcode_inspect_takes_the_file_info_shape(service):
    r = cc2community.gcode_inspect(HOST, "local", "Orca/b.gcode")
    assert ("POST", "/api/gcode-files/inspect", "internal\nOrca/b.gcode") in service.log
    assert r["source"] == "cc2" and r["file"]["multicolour"] is True
    assert r["file"]["colors"] == [
        {"hex": "#FF0000", "type": "PLA", "extruder": 0, "mm": 1234.6},     # #RRGGBBAA → #RRGGBB
        {"hex": "#00FF00", "type": "PETG", "extruder": 1}]
    assert r["file"]["tools"] == [0, 1]
    with pytest.raises(cc2community.CommunityError):
        cc2community.gcode_inspect(HOST, "local", "a.gcode\nM112")


def test_gcode_thumbnail_statuses(monkeypatch):
    seen = []
    answers = iter([(200, "image/png", PNG), (404, "application/json", b"{}"),
                    (415, "application/json", b"{}"), (500, "application/json", b"{}"),
                    (200, "application/json", b"{}")])

    def raw(host, path, body, timeout=3.0, port=8081):
        seen.append((path, body))
        return next(answers)
    monkeypatch.setattr(cc2community, "_raw", raw)
    assert cc2community.gcode_thumbnail(HOST, "u-disk", "dir/x.gcode") == (PNG, "image/png")
    assert seen[0] == ("/api/gcode-files/thumbnail", "usb\ndir/x.gcode")
    assert cc2community.gcode_thumbnail(HOST, "local", "x.gcode") is None      # превью нет
    assert cc2community.gcode_thumbnail(HOST, "local", "x.gcode") is None      # не картинка
    for _ in range(2):                                    # сбой службы и «картинка» не картинка
        with pytest.raises(cc2community.CommunityError):
            cc2community.gcode_thumbnail(HOST, "local", "x.gcode")


def test_raw_carries_csrf_marker_and_returns_bytes(monkeypatch):
    seen = {}

    class Conn:
        def __init__(self, host, port, timeout=None):
            seen["addr"] = (host, port)

        def connect(self):
            pass

        def request(self, method, path, body=None, headers=None):
            seen["req"] = (method, path, body, dict(headers or {}))

        def getresponse(self):
            return type("R", (), {"status": 200, "read": lambda s: PNG,
                                  "getheader": lambda s, k: "image/png"})()

        def close(self):
            pass
    monkeypatch.setattr(cc2community.http.client, "HTTPConnection", Conn)
    assert cc2community._raw(HOST, "/api/gcode-files/thumbnail", "internal\na.gcode") == (
        200, "image/png", PNG)
    method, path, data, headers = seen["req"]
    assert seen["addr"] == (HOST, 8081) and method == "POST" and data == b"internal\na.gcode"
    assert headers["X-CC2-Request"] == "1" and headers["Content-Type"].startswith("text/plain")


def test_raw_tells_unreachable_from_lost_answer(monkeypatch):
    """Соединение не установилось — запрос не ушёл (можно стоковым путём); ответ потерялся
    после отправки — нельзя: служба могла успеть."""
    stage = {"fail": "connect"}

    class Conn:
        def __init__(self, *a, **k):
            pass

        def connect(self):
            if stage["fail"] == "connect":
                raise ConnectionRefusedError("refused")

        def request(self, *a, **k):
            pass

        def getresponse(self):
            raise TimeoutError("timed out")

        def close(self):
            pass
    monkeypatch.setattr(cc2community.http.client, "HTTPConnection", Conn)
    with pytest.raises(cc2community.CommunityUnreachable):
        cc2community._raw(HOST, "/x", "")
    stage["fail"] = "answer"
    with pytest.raises(cc2community.CommunityError) as e:
        cc2community._raw(HOST, "/x", "")
    assert not isinstance(e.value, cc2community.CommunityUnreachable)


def test_gcode_delete_speaks_russian_and_forgives_missing(monkeypatch):
    replies = iter([{"deleted": True}, cc2community.CommunityError("G-code file not found or unsafe"),
                    cc2community.CommunityError("Printer must be idle and connected")])
    sent = []

    def post(h, p, b="", timeout=3.0):
        sent.append((p, b))
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(cc2community, "_post", post)
    cc2community.gcode_delete(HOST, "local", "Orca/b.gcode")
    assert sent[0] == ("/api/gcode-files/delete", "internal\nOrca/b.gcode")
    cc2community.gcode_delete(HOST, "local", "Orca/b.gcode")              # уже нет — тоже успех
    with pytest.raises(cc2community.CommunityError, match="простаивает"):
        cc2community.gcode_delete(HOST, "local", "Orca/b.gcode")


def test_thumbs_need_both_service_caps(monkeypatch):
    _found(monkeypatch, {"file_manager": True})
    assert printerdrv.for_printer(_P()).caps.thumbs is False
    cc2community.forget()
    _found(monkeypatch, {"file_manager": True, "gcode_thumbnails": True})
    assert printerdrv.for_printer(_P()).caps.thumbs is True
    assert printerdrv.ElegooDriver.caps.thumbs is False, "стоковый CC2 превью не умеет"


# ─────────────────────────── драйвер ───────────────────────────

def test_driver_lists_files_from_service_with_harvested_meta(service, mqtt, monkeypatch):
    monkeypatch.setattr(elegoo, "harvested_files", lambda ep=None: {"files": [
        {"name": "b.gcode", "grams": 12.5, "print_time": 600,
         "colors": [{"hex": "#FF0000", "type": "PLA", "extruder": 0}]}]})
    r = printerdrv.for_printer(_P()).files()
    assert [f["name"] for f in r["files"]] == ["Orca/b.gcode", "a.gcode"]
    b = r["files"][0]
    assert b["grams"] == 12.5 and b["print_time"] == 600, "граммы — из пассивного сбора по имени"
    assert "grams" not in r["files"][1]
    assert mqtt == [], "с работающей службой 1044 не спрашиваем"


def test_driver_falls_back_to_mqtt_when_service_fails(service, mqtt):
    service.replies["/api/gcode-files"] = cc2community.CommunityError("timed out")
    drv = printerdrv.for_printer(_P())
    assert drv.files()["files"] == [{"name": "stock.gcode"}]
    assert drv.files_on("u-disk")["files"] == [{"name": "stock.gcode"}]
    assert mqtt == [("list", "local"), ("list", "u-disk")]


def test_stock_printer_keeps_the_mqtt_path(mqtt, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("к службе сообщества без неё не ходим")
    monkeypatch.setattr(cc2community, "_http", boom)
    monkeypatch.setattr(cc2community, "_raw", boom)
    drv = printerdrv.for_printer(_P())
    assert drv.files()["files"] == [{"name": "stock.gcode"}]
    assert drv.file_info("a.gcode")["ok"]
    assert drv.thumbnail("a.gcode") is None
    assert drv.delete_files(["a.gcode"])["ok"]
    assert mqtt == [("list", "local"), ("info", "a.gcode"), ("delete", "local", ["a.gcode"])]


def test_driver_file_info_prefers_inspect(service, mqtt):
    drv = printerdrv.for_printer(_P())
    assert drv.file_info("Orca/b.gcode")["file"]["colors"][1]["type"] == "PETG"
    assert mqtt == []
    service.replies["/api/gcode-files/inspect"] = cc2community.CommunityError("Analysis queue is full")
    assert drv.file_info("a.gcode")["ok"] and mqtt == [("info", "a.gcode")]


def test_driver_deletes_folder_files_through_service(service, mqtt):
    drv = printerdrv.for_printer(_P())
    assert drv.delete_files(["a.gcode", "Orca/b.gcode"])["ok"]
    assert mqtt == [("delete", "local", ["a.gcode"])], "обычные — прежним 1047, одной командой"
    assert ("POST", "/api/gcode-files/delete", "internal\nOrca/b.gcode") in service.log
    mqtt.clear()
    assert drv.delete_file("Orca/c.gcode")["ok"] and mqtt == []
    service.replies["/api/gcode-files/delete"] = cc2community.CommunityError(
        "Printer must be idle and connected")
    r = drv.delete_files(["a.gcode", "Orca/b.gcode"], storage="u-disk")
    assert r["ok"] is False and "простаивает" in r["error"]
    assert mqtt == [("delete", "u-disk", ["a.gcode"])], "обычные удалены и во время печати"


# ─────────────────────────── ручка превью ───────────────────────────

@pytest.fixture
def thumbs(fake_printer, monkeypatch, tmp_path):
    """Симулятор с превью: счётчик обращений к «принтеру» и кэш во временном каталоге."""
    stub_auth(monkeypatch, _verify_admin=ADMIN)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(printerext, "_thumb_gate", asyncio.Semaphore(1))
    fake_printer.community_info = cc2community.parse_info(DISCOVERY).as_dict()
    fake_printer.caps = Caps(**{**fake_printer.caps.as_dict(), "thumbs": True})
    calls = []

    def thumbnail(path, *, storage="local"):
        calls.append((storage, path))
        if path.startswith("broken"):
            raise cc2community.CommunityError("timed out")
        return (PNG, "image/png") if path.endswith("with.gcode") else None
    fake_printer.thumbnail = thumbnail
    return calls


async def _thumb(query):
    return await printerext._api_printer_thumb(admin_req(query=query))


async def test_thumb_route_caches_hits_and_misses(tmp_db, thumbs):
    r = await _thumb({"path": "Orca/with.gcode", "m": "1791000200"})
    assert r.status == 200 and r.body == PNG and r.content_type == "image/png"
    again = await _thumb({"path": "Orca/with.gcode", "m": "1791000200"})
    assert isinstance(again, web.FileResponse) and again.status == 200
    assert (await _thumb({"path": "none.gcode", "m": "5"})).status == 404
    assert (await _thumb({"path": "none.gcode", "m": "5"})).status == 404
    assert thumbs == [("local", "Orca/with.gcode"), ("local", "none.gcode")], \
        "тот же файл второй раз к принтеру не ходит — ни с превью, ни без"
    await _thumb({"path": "Orca/with.gcode", "m": "1791009999"})          # файл перезалили
    assert len(thumbs) == 3


async def test_thumb_route_does_not_cache_failures(tmp_db, thumbs):
    assert (await _thumb({"path": "broken.gcode"})).status == 502
    assert (await _thumb({"path": "broken.gcode"})).status == 502
    assert len(thumbs) == 2


async def test_thumb_route_refuses_bad_input_and_stock_printers(tmp_db, fake_printer, thumbs):
    for q in ({"path": ""}, {"path": "../etc/passwd"}, {"path": "a\nb.gcode"},
              {"path": "a.gcode", "storage": "sd-card"}, {"path": "x" * 600}):
        assert (await _thumb(q)).status == 400, q
    fake_printer.caps = Caps(**{**fake_printer.caps.as_dict(), "thumbs": False})
    assert (await _thumb({"path": "with.gcode"})).status == 404
    assert thumbs == []


async def test_thumb_route_asks_the_printer_one_at_a_time(tmp_db, fake_printer, thumbs):
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def slow(path, *, storage="local"):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return PNG, "image/png"
    fake_printer.thumbnail = slow
    rs = await asyncio.gather(*[_thumb({"path": f"f{i}.gcode"}) for i in range(4)])
    assert all(r.status == 200 for r in rs)
    assert state["peak"] == 1, "их служба однопоточная — к принтеру строго по одному"


async def test_thumb_cache_is_bounded(tmp_db, thumbs, monkeypatch):
    monkeypatch.setattr(printerext, "_THUMB_KEEP", 3)
    for i in range(6):
        await _thumb({"path": f"f{i}-with.gcode"})
    folder = config.DATA_DIR / "cache" / "cc2_thumbs"
    assert len(list(folder.iterdir())) == 3
