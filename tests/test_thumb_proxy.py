"""Регрессия OpenTodo 5ea29c10: /api/thumb отдавал ОБРЕЗАННЫЙ JPEG (aiohttp content.read(n)
возвращает первый чанк, а не n байт) — превью поиска «не грузились». Фейковый CDN стримит
картинку мелкими чанками; прокси обязан вернуть тело целиком."""
import urllib.parse

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import webapp

IMG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 700   # ~175 КБ «JPEG»


async def _fake_cdn_app() -> web.Application:
    async def img(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await resp.prepare(request)
        for i in range(0, len(IMG), 1024):      # отдаём мелкими чанками, как реальная сеть
            await resp.write(IMG[i:i + 1024])
        await resp.write_eof()
        return resp

    async def huge(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await resp.prepare(request)
        chunk = bytes(1024 * 1024)
        for _ in range(6):                      # 6 МБ > потолка _THUMB_MAX (5 МБ)
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/img.jpg", img)
    app.router.add_get("/huge.jpg", huge)
    return app


async def _proxy_client() -> TestClient:
    app = web.Application()
    app.router.add_get("/api/thumb", webapp._api_thumb)
    return TestClient(TestServer(app))


async def test_thumb_proxy_returns_full_body():
    cdn = TestClient(TestServer(await _fake_cdn_app()))
    await cdn.start_server()
    try:
        url = str(cdn.make_url("/img.jpg"))
        proxied = webapp._thumb_proxy(url)
        assert proxied.startswith("/api/thumb?u=")
        client = await _proxy_client()
        await client.start_server()
        try:
            r = await client.get(proxied)
            assert r.status == 200
            body = await r.read()
            assert body == IMG                  # целиком, не первый чанк
            assert r.headers["Content-Type"].startswith("image/")
        finally:
            await client.close()
    finally:
        await cdn.close()


async def test_thumb_proxy_rejects_oversized_and_bad_sig():
    cdn = TestClient(TestServer(await _fake_cdn_app()))
    await cdn.start_server()
    try:
        url = str(cdn.make_url("/huge.jpg"))
        proxied = webapp._thumb_proxy(url)
        client = await _proxy_client()
        await client.start_server()
        try:
            r = await client.get(proxied)
            assert r.status == 404              # больше потолка — отказ, не обрезка
            bad = f"/api/thumb?u={urllib.parse.quote(url, safe='')}&s=badbadbadbad"
            r2 = await client.get(bad)
            assert r2.status == 403             # неподписанный URL не проксируем
        finally:
            await client.close()
    finally:
        await cdn.close()
