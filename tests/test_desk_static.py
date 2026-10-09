"""Отдача статики настольной админки (app/webui/desk).

Эти проверки идут через настоящий HTTP-слой, а не вызовом хендлера напрямую: ловим именно
ошибки маршрутизации (каталог не разворачивается в index.html, catch-all перехватывает чужой
путь) — их вызов функции с фейковым request не видит в принципе.
"""
from aiohttp.test_utils import TestClient, TestServer

from app import config, webapp


async def _client() -> TestClient:
    client = TestClient(TestServer(webapp.make_app()))
    await client.start_server()
    return client


async def test_desk_index_served_for_directory_url():
    """/app/desk/ → desk/index.html. Без разворачивания каталога отдавался 404."""
    client = await _client()
    try:
        resp = await client.get("/app/desk/")
        assert resp.status == 200
        body = await resp.text()
        assert "OpenPrintShop" in body
        assert "desk.js" in body            # бандл подключён
        assert "@babel/standalone" not in body   # Babel в рантайме не участвует
    finally:
        await client.close()


async def test_desk_bundle_and_css_served():
    """Собранный бандл и CSS доступны. desk.js коммитится — бот работает без Node."""
    client = await _client()
    try:
        for path in ("/app/desk/desk.js", "/app/desk/desktop.css"):
            resp = await client.get(path)
            assert resp.status == 200, path
    finally:
        await client.close()


async def test_desk_denied_for_tunneled_request():
    """Порт 8081 опубликован наружу туннелем — статика админки наружу не отдаётся.

    Признак чужого запроса — заголовок прокси (см. webapp._request_is_local)."""
    client = await _client()
    try:
        resp = await client.get("/app/desk/", headers={"X-Forwarded-For": "203.0.113.7"})
        assert resp.status == 404
        resp = await client.get("/app/desk/desk.js", headers={"Cf-Connecting-Ip": "203.0.113.7"})
        assert resp.status == 404
    finally:
        await client.close()


async def test_miniapp_still_served_through_tunnel():
    """Мини-апп клиентов через туннель работать обязан — ограничение только на desk."""
    client = await _client()
    try:
        resp = await client.get("/app/", headers={"X-Forwarded-For": "203.0.113.7"})
        assert resp.status == 200
    finally:
        await client.close()


async def test_path_traversal_still_blocked():
    """Файлы вне app/webui не отдаются. Разворачивание каталога в index.html не должно было
    ослабить эту границу.

    Варианты со слешем схлопывает ещё URL-нормализация, а вот обратный слеш до неё не доходит:
    на Windows его понимает уже Path.resolve(), то есть проверку границы папки он и проверяет.
    """
    client = await _client()
    try:
        for path in ("/app/../config.py", "/app/..%2Fconfig.py", "/app/desk/../../config.py",
                     r"/app/..\config.py", "/app/..%5Cconfig.py", "/app/desk/..%5C..%5Cconfig.py"):
            resp = await client.get(path)
            assert resp.status == 404, path
            assert b"BOT_TOKEN" not in await resp.read(), path
    finally:
        await client.close()


def test_desk_excluded_from_miniapp_version(tmp_path, monkeypatch):
    """Правка десктопа НЕ меняет версию мини-аппа: иначе каждое изменение админки
    принудительно перезагружало бы мини-апп у всех клиентов Telegram."""
    webui = tmp_path / "webui"
    (webui / "desk").mkdir(parents=True)
    (webui / "app.js").write_text("// mini-app", encoding="utf-8")
    (webui / "desk" / "desk.js").write_text("// desk", encoding="utf-8")

    monkeypatch.setattr(config, "_WEBUI_DIR", webui)
    monkeypatch.setattr(config, "_DESK_DIR", webui / "desk")
    monkeypatch.setattr(config, "_ver_memo", {"v": "", "exp": 0.0})

    before = config.webui_version()

    (webui / "desk" / "desk.js").write_text("// desk, изменён", encoding="utf-8")
    config._ver_memo["exp"] = 0.0                      # сбрасываем мемо на 1 с
    assert config.webui_version() == before, "правка desk не должна двигать версию мини-аппа"

    (webui / "app.js").write_text("// mini-app, изменён", encoding="utf-8")
    config._ver_memo["exp"] = 0.0
    assert config.webui_version() != before, "правка мини-аппа обязана двигать версию"
