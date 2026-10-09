"""Регрессия 10.09: MJPEG-поток камеры висел ВЕЧНО на зрителе, переставшем читать сокет.

По логу за 9–10.09 девять запросов к /api/admin/camera провисели по 7216 c (два часа) и
кончились 500-й с алертом админам. Причина не в клиенте, который «отвалился»: замёрзшее окно
WebView деска (или свёрнутая вкладка) соединение НЕ закрывает, оно просто перестаёт забирать
байты. Окно приёма схлопывается, `await resp.write(...)` засыпает в drain — а потолок в 30
минут проверяется МЕЖДУ итерациями цикла и до висящей записи не доходит. Всё это время
обработчик держал очередь в relay.clients, и зритель, переоткрывая поток, копил такие.

Замёрзший зритель здесь — ответ, чья запись не возвращается никогда. Так и есть на самом деле
(aiohttp спит в drain-ожидании), но БЕЗ опоры на размер буферов сокета: сколько петля успевает
проглотить до первой блокировки, на этой машине плавало от 6 МБ до сотни, и тест на живом
сокете ловил бы регрессию через раз. Живой сокет остался во втором тесте — там, где важно,
что нормальный зритель кадры получает.
"""
import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.web import printerlive

CAM_URL = "http://camera.test:8080/mjpeg"
SMALL = b"\xff\xd8" + b"JPEG-BODY" + b"\xff\xd9"
WRITE_TIMEOUT = 0.5                                          # вместо боевых 12 c
BUDGET = 5.0                                                 # сколько ждём ухода зрителя


class _NeverDrains:
    """Ответ зрителя, который не читает сокет: prepare проходит, write не возвращается."""

    def __init__(self, *, status=200, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self.writes = 0

    async def prepare(self, _request):
        return None

    async def write(self, data):
        self.writes += 1
        assert data.startswith(b"--pcbframe")
        await asyncio.Event().wait()     # ждём места в окне приёма, которого не будет


class _FakeTransport:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


@pytest.fixture
def lone_camera(monkeypatch):
    """Камера есть, апстрима нет: подписка зрителей настоящая, к железу не ходим."""
    monkeypatch.setattr(printerlive, "_verify_admin", lambda _init_data: True)

    async def _url(_printer_id=None):
        return CAM_URL

    async def _no_upstream(_self):
        return None                    # кадры кладёт сам тест через relay.feed

    monkeypatch.setattr(printerlive, "_camera_url", _url)
    monkeypatch.setattr(printerlive._CamRelay, "_relay", _no_upstream)
    monkeypatch.setattr(printerlive, "_CAM_WRITE_TIMEOUT_S", WRITE_TIMEOUT)
    yield
    printerlive._cam_relays.clear()


async def _subscribed_relay() -> printerlive._CamRelay:
    """Дождаться, пока обработчик подпишется очередью на кадры камеры."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        relay = printerlive._cam_relays.get(CAM_URL)
        if relay is not None and relay.clients:
            return relay
        await asyncio.sleep(0.01)
    raise AssertionError("обработчик камеры не подписался на кадры за 5 c")


async def test_stalled_viewer_is_dropped(lone_camera, admin_req, monkeypatch):
    """Зритель не забирает кадр → обработчик уходит сам, а не висит до обрыва сокета по ОС."""
    monkeypatch.setattr(printerlive.web, "StreamResponse", _NeverDrains)
    request = admin_req()
    request.transport = _FakeTransport()

    task = asyncio.create_task(printerlive._api_admin_camera(request))
    relay = await _subscribed_relay()
    started = time.monotonic()
    relay.feed(SMALL)                    # кадр ушёл в запись — и та не вернётся

    # именно asyncio.wait: wait_for на таймауте СНЯЛ БЫ задачу, обработчик проглотил бы отмену
    # своим except CancelledError и вернулся как ни в чём не бывало — тест зелёный на висяке
    done, _ = await asyncio.wait({task}, timeout=BUDGET)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError(f"обработчик висит на resp.write дольше {BUDGET} c")

    resp = task.result()
    assert resp.writes == 1
    assert time.monotonic() - started < BUDGET / 2, "ушёл, но слишком поздно"
    # соединение снято сами: close() ждал бы слива буфера, которого зритель не читает
    assert request.transport.aborted, "зависшего зрителя не отцепили от сокета"
    # finally отработал целиком
    assert not relay.clients, "очередь осталась в relay.clients"
    assert time.monotonic() - relay.seen < 5.0, "relay.seen не обновлён — линджер поедет"


async def test_reading_viewer_gets_frames(lone_camera):
    """Обратная сторона на ЖИВОМ сокете: нормальный зритель кадры получает, таймаут не мешает;
    закрытая вкладка снимает очередь на первом же кадре, а не через 25 c ожидания."""
    app = web.Application()
    app.router.add_get("/api/admin/camera", printerlive._api_admin_camera)
    server = TestServer(app)
    await server.start_server()
    try:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(f"GET /api/admin/camera?initData=x HTTP/1.1\r\n"
                         f"Host: {server.host}:{server.port}\r\n\r\n".encode())
            await writer.drain()
            relay = await _subscribed_relay()
            relay.feed(SMALL)

            got = b""
            deadline = time.monotonic() + 5.0
            while b"JPEG-BODY" not in got and time.monotonic() < deadline:
                got += await asyncio.wait_for(reader.read(65536), timeout=5.0)
            assert b"multipart/x-mixed-replace" in got
            assert b"--pcbframe" in got and b"JPEG-BODY" in got
            assert relay.clients, "зритель, который читает, из подписки не выпадает"
        finally:
            writer.close()               # закрытие с непрочитанным хвостом = RST зрителя
        deadline = time.monotonic() + BUDGET
        while relay.clients and time.monotonic() < deadline:
            relay.feed(SMALL)
            await asyncio.sleep(0.1)
        assert not relay.clients, "закрытая вкладка не сняла очередь из relay.clients"
    finally:
        await server.close()
