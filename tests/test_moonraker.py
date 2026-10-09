"""Тест клиента Moonraker (сетевая печать, c6e4f855) на моках — без реального принтера."""
from app import config, moonraker


class FakeResp:
    def __init__(self, status=200, text="ok", payload=None):
        self.status = status
        self._text = text
        self._payload = payload or {}

    async def text(self):
        return self._text

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload
        self.posts: list = []
        self.gets: list = []
        self.closed = False

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "headers": headers or {}})
        return FakeResp(self.status, "boom" if self.status >= 400 else "ok")

    def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers or {}})
        return FakeResp(self.status, "ok", self.payload)

    async def close(self):
        self.closed = True


async def test_disabled_when_no_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MOONRAKER_URL", "")
    f = tmp_path / "a.gcode"
    f.write_bytes(b"G28")
    r = await moonraker.upload(f)
    assert r["ok"] is False and "не настроена" in r["error"]
    assert moonraker.enabled() is False


async def test_file_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://printer:7125")
    r = await moonraker.upload(tmp_path / "nope.gcode")
    assert r["ok"] is False and "не найден" in r["error"]


async def test_upload_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://printer:7125/")
    monkeypatch.setattr(config, "MOONRAKER_API_KEY", "secret")
    f = tmp_path / "job.gcode"
    f.write_bytes(b"G28")
    sess = FakeSession(status=201)
    r = await moonraker.upload(f, start=True, session=sess)
    assert r["ok"] is True and r["filename"] == "job.gcode" and r["started"] is True
    assert sess.posts[0]["url"] == "http://printer:7125/server/files/upload"   # rstrip('/')
    assert sess.posts[0]["headers"].get("X-Api-Key") == "secret"
    assert sess.closed is False                                                # чужую сессию не закрываем


async def test_upload_error_status(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://printer:7125")
    monkeypatch.setattr(config, "MOONRAKER_API_KEY", "")
    f = tmp_path / "j.gcode"
    f.write_bytes(b"G1")
    r = await moonraker.upload(f, session=FakeSession(status=500))
    assert r["ok"] is False and "500" in r["error"]


async def test_status_ok(monkeypatch):
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://printer:7125")
    sess = FakeSession(status=200, payload={"result": {"state": "ready", "hostname": "voron"}})
    r = await moonraker.status(session=sess)
    assert r["ok"] is True and r["state"] == "ready" and r["hostname"] == "voron"
    assert sess.gets[0]["url"] == "http://printer:7125/printer/info"


async def test_query_normalizes_telemetry(monkeypatch):
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://printer:7125")
    payload = {"result": {"status": {
        "print_stats": {"state": "printing", "print_duration": 1200,
                        "info": {"current_layer": 5, "total_layer": 100}},
        "virtual_sdcard": {"progress": 0.42},
        "extruder": {"temperature": 210.5, "target": 215},
        "heater_bed": {"temperature": 60.1, "target": 60},
        "fan": {"speed": 1.0}}}}
    sess = FakeSession(status=200, payload=payload)
    r = await moonraker.query(session=sess)
    assert r["ok"] is True and r["state"] == "printing"
    assert r["overlay"]["virtual_sdcard"]["progress"] == 0.42
    assert r["overlay"]["info"] == {"current_layer": 5, "total_layer": 100}
    assert r["overlay"]["fan"] == 100
    assert r["summary"]["extruder_temp"] == 210.5 and r["summary"]["bed_target"] == 60
    assert "/printer/objects/query" in sess.gets[0]["url"]


async def test_query_disabled(monkeypatch):
    monkeypatch.setattr(config, "MOONRAKER_URL", "")
    r = await moonraker.query()
    assert r["ok"] is False


# ───────────────────── эндпоинт: кому адресован вызов (волна 2 цикла 2) ─────────────────────

async def test_upload_follows_the_given_endpoint(monkeypatch, tmp_path):
    """Второй Klipper-принтер получает СВОЙ адрес и СВОЙ ключ. Ключ первого, отправленный на
    вторую машину, — это выданный наружу секрет, а не просто лишний заголовок."""
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://first:7125")
    monkeypatch.setattr(config, "MOONRAKER_API_KEY", "secret-of-the-first")
    f = tmp_path / "job.gcode"
    f.write_bytes(b"G28")
    sess = FakeSession(status=201)
    ep = moonraker.Endpoint(url="http://second:7125/", api_key="secret-of-the-second")

    r = await moonraker.upload(f, session=sess, ep=ep)

    assert r["ok"] is True
    assert sess.posts[0]["url"] == "http://second:7125/server/files/upload"   # rstrip('/')
    assert sess.posts[0]["headers"]["X-Api-Key"] == "secret-of-the-second"


async def test_status_and_query_follow_the_given_endpoint(monkeypatch):
    """Эндпоинт задан — принтер по умолчанию не нужен вовсе (установка без MOONRAKER_URL)."""
    monkeypatch.setattr(config, "MOONRAKER_URL", "")
    monkeypatch.setattr(config, "MOONRAKER_API_KEY", "")
    ep = moonraker.Endpoint(url="http://second:7125")
    assert moonraker.enabled() is False and moonraker.enabled(ep) is True

    sess = FakeSession(status=200, payload={"result": {"state": "ready", "hostname": "voron2"}})
    r = await moonraker.status(session=sess, ep=ep)
    assert r["ok"] is True and r["hostname"] == "voron2"
    assert sess.gets[0]["url"] == "http://second:7125/printer/info"

    sess2 = FakeSession(status=200, payload={"result": {"status": {}}})
    r2 = await moonraker.query(session=sess2, ep=ep)
    assert r2["ok"] is True
    assert sess2.gets[0]["url"].startswith("http://second:7125/printer/objects/query?")
