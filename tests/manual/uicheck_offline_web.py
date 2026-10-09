"""Ручная сквозная проверка Э2 в браузере (в общий прогон не входит: имя не test_*).

Настоящий фронт (app/webui) в headless Chromium + настоящая приёмная (relay/) со снимком,
собранным настоящим кодом ПК, + прослойка, повторяющая офлайн-маршруты nginx-psb-web.conf:
баннер «мастерская офлайн», «Мои заказы» из снимка, заказ без цены по ссылке (с пожеланием)
и файлом (multipart) — до события deferred_order и сообщения клиенту. Скриншоты — в tmp_path.

Нужен Playwright с Chromium (requirements-makerworld.txt). Запуск:
    .venv/Scripts/python.exe -m pytest tests/manual/uicheck_offline_web.py -s
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_kit import CLIENT, make_relay_fixture, snapshot, tg_fixture  # noqa: E402,F401
from test_deferred_runner import _presets  # noqa: E402
from test_relay_web import init_data, web_relay  # noqa: E402

from app import relaylink  # noqa: E402

WEBUI = Path(__file__).resolve().parents[2] / "app" / "webui"
MODEL_OFFLINE = {"ok": False, "pc_offline": True, "can_defer": True,
                 "error": "Компьютер мастерской сейчас выключен, поэтому посчитать цену прямо сейчас не "
                          "получится. Оставьте заказ: модель сохраним, а цену посчитаем и пришлём в чат с "
                          "ботом, как только он включится."}
API_OFFLINE = {"ok": False, "pc_offline": True, "error": "Мастерская сейчас офлайн."}
SEEN = web.AppKey("seen", list)

STUB = """
(function () {
  function noop() {}
  var alerts = window.__alerts = [];
  var btn = {show: noop, hide: noop, onClick: noop, offClick: noop, setText: noop, setParams: noop,
             enable: noop, disable: noop, showProgress: noop, hideProgress: noop, isVisible: false};
  var store = function (k, cb) { if (cb) cb(null, ''); };
  window.Telegram = {WebApp: {
    initData: %INIT%, initDataUnsafe: {user: {id: %UID%, first_name: 'Иван'}},
    version: '8.0', platform: 'android', colorScheme: 'dark', themeParams: {},
    isExpanded: true, viewportHeight: 860, viewportStableHeight: 860,
    ready: noop, expand: noop, close: noop, onEvent: noop, offEvent: noop,
    setHeaderColor: noop, setBackgroundColor: noop, setBottomBarColor: noop,
    enableClosingConfirmation: noop, disableClosingConfirmation: noop,
    disableVerticalSwipes: noop, enableVerticalSwipes: noop, requestFullscreen: noop,
    MainButton: btn, SecondaryButton: btn,
    BackButton: {show: noop, hide: noop, onClick: noop, offClick: noop, isVisible: false},
    SettingsButton: {show: noop, hide: noop, onClick: noop, offClick: noop},
    HapticFeedback: {impactOccurred: noop, notificationOccurred: noop, selectionChanged: noop},
    CloudStorage: {getItem: store, setItem: function (k, v, cb) { if (cb) cb(null, true); },
                   getItems: function (k, cb) { if (cb) cb(null, {}); }, removeItem: store, getKeys: store},
    showAlert: function (m, cb) { alerts.push(m); if (cb) cb(); },
    showConfirm: function (m, cb) { alerts.push('confirm:' + m); if (cb) cb(true); },
    showPopup: function (p, cb) { alerts.push('popup:' + (p && p.message)); if (cb) cb(); },
    openLink: noop, openTelegramLink: noop, sendData: noop, switchInlineQuery: noop,
    isVersionAtLeast: function () { return true; }
  }};
})();
"""


def mimic(relay) -> tuple[web.Application, aiohttp.ClientSession]:
    """nginx-psb-web.conf при выключенном ПК: опции/заказы/заявка — приёмной, модель — сразу 503."""
    base = f"http://127.0.0.1:{relay.port}"
    session = aiohttp.ClientSession(trust_env=False)
    seen: list[str] = []

    async def ui(request):
        rel = request.match_info.get("path") or "index.html"
        full = (WEBUI / rel).resolve()
        if not str(full).startswith(str(WEBUI)) or not full.is_file():
            raise web.HTTPNotFound()
        if full.name == "index.html":
            page = (full.read_text(encoding="utf-8").replace("%%VER%%", "uicheck")
                    .replace("%%BRAND_JSON%%", "{}").replace("%%FEATURES_JSON%%", "{}")
                    .replace("%%BRAND_TITLE%%", "Print Studio").replace("%%BRAND_NAME%%", "Print Studio"))
            return web.Response(text=page, content_type="text/html")
        return web.FileResponse(full)

    async def version(request):           # nginx: последняя версия из кэша
        return web.json_response({"v": "uicheck", "channel": "stable", "rev": "x"})

    async def to_relay(request):
        seen.append(f"{request.method} {request.path}")
        body = await request.read()
        headers = {"X-PSB-Web": "1"}
        if "Content-Type" in request.headers:
            headers["Content-Type"] = request.headers["Content-Type"]
        async with session.request(request.method, base + request.path_qs, data=body or None,
                                   headers=headers) as resp:
            return web.Response(status=resp.status, body=await resp.read(),
                                content_type=resp.content_type)

    async def model_offline(request):
        seen.append(f"{request.method} {request.path}")
        await request.read()
        return web.json_response(MODEL_OFFLINE, status=503)

    async def api_offline(request):
        seen.append(f"{request.method} {request.path}")
        return web.json_response(API_OFFLINE, status=503)

    app = web.Application(client_max_size=60 * 1024 * 1024)
    app[SEEN] = seen
    app.router.add_get("/api/version", version)
    for path in ("/api/options", "/api/orders"):
        app.router.add_get(path, to_relay)
    app.router.add_post("/api/order_deferred", to_relay)
    for path in ("/api/model", "/api/use", "/api/variant", "/api/file_variant"):
        app.router.add_post(path, model_offline)
    app.router.add_route("*", "/api/{tail:.*}", api_offline)
    app.router.add_get("/app", ui)
    app.router.add_get("/app/", ui)
    app.router.add_get("/app/{path:.*}", ui)
    return app, session


async def test_offline_order_in_real_browser(tmp_db, make_relay, tg, tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    async_playwright = playwright.async_playwright
    SHOTS = tmp_path / "shots"
    SHOTS.mkdir()
    print("скриншоты:", SHOTS)
    await _presets(tmp_db)
    relaylink._web_cache.clear()
    web_section = await relaylink.build_web_section(time.time())
    relay, client = await web_relay(make_relay, tmp_path)
    snap = snapshot()
    snap["web"] = web_section
    snap["web"]["orders"] = {str(CLIENT): [{"id": 41, "label": "Кронштейн", "status": "awaiting_payment",
                                            "status_text": "Ждёт оплаты", "price": 45.0}]}
    await client.snapshot(snap)
    assert not relay.pc_online()

    app, session = mimic(relay)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    stub = STUB.replace("%INIT%", json.dumps(init_data(CLIENT))).replace("%UID%", str(CLIENT))
    errors: list[str] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 400, "height": 860}, locale="ru-RU")
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

            async def outside(route):
                if route.request.url.startswith("https://telegram.org/js/telegram-web-app.js"):
                    await route.fulfill(status=200, content_type="application/javascript", body=stub)
                else:
                    await route.abort()
            await page.route("https://**/*", outside)

            await page.goto(f"http://127.0.0.1:{port}/app/")
            await page.wait_for_selector("text=Мастерская сейчас офлайн", timeout=20000)
            await page.screenshot(path=str(SHOTS / "1-home-offline-banner.png"))
            print("баннер есть")

            # «Заказы» — из снимка приёмной
            await page.get_by_text("Заказы", exact=True).last.click()
            await page.wait_for_selector("text=Кронштейн", timeout=20000)
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(SHOTS / "1b-orders-offline.png"))
            await page.get_by_text("Расчёт", exact=True).last.click()
            await page.wait_for_selector(".hbar input", timeout=20000)

            # ссылка → 503 can_defer → оверлей с предложением заказа
            await page.fill(".hbar input", "https://www.printables.com/model/123456-test-cube")
            await page.press(".hbar input", "Enter")
            await page.wait_for_selector("text=Оставить заказ — цену пришлём", timeout=20000)
            await page.wait_for_timeout(800)
            await page.screenshot(path=str(SHOTS / "2-defer-overlay.png"))
            await page.fill("textarea", "Красный PETG, 2 штуки")
            await page.click("text=Оставить заказ — цену пришлём")
            await page.wait_for_function("window.__alerts && window.__alerts.length > 0", timeout=20000)
            alerts = await page.evaluate("window.__alerts")
            print("alert:", alerts)
            await page.screenshot(path=str(SHOTS / "3-after-order.png"))

            # файл → тот же путь, но заявка — multipart с файлом на приёмную
            async with page.expect_file_chooser() as fc_info:
                await page.click("label.fchip")
            chooser = await fc_info.value
            model = tmp_path / "Кубик.stl"
            model.write_bytes(b"solid cube\nendsolid cube\n" * 50)
            await chooser.set_files(str(model))
            await page.wait_for_selector("text=Оставить заказ — цену пришлём", timeout=20000)
            await page.click("text=Оставить заказ — цену пришлём")
            await page.wait_for_function("window.__alerts.length > 1", timeout=20000)
            print("alert 2:", (await page.evaluate("window.__alerts"))[-1])
            await browser.close()
    finally:
        await runner.cleanup()
        await session.close()

    print("маршруты:", app[SEEN])
    print("ошибки страницы:", [e for e in errors if "favicon" not in e][:10])
    events = relay.store.db.execute("SELECT kind, payload FROM events ORDER BY created_at").fetchall()
    payloads = [json.loads(r["payload"]) for r in events if r["kind"] == "deferred_order"]
    assert len(payloads) == 2, payloads
    first, second = payloads
    assert first["src"]["type"] == "url" and "printables.com" in first["src"]["url"]
    assert first["params"].get("comment") == "Красный PETG, 2 штуки" and first.get("origin") == "web"
    assert second["src"]["type"] == "relay_file" and second["src"]["file_name"] == "Кубик.stl"
    assert (relay.cfg.files_dir / second["src"]["ref"]).stat().st_size == model.stat().st_size
    dms = [p["text"] for p in tg.sent("sendMessage") if p.get("chat_id") == CLIENT]
    print("сообщения клиенту:", dms)
    assert len(dms) == 2 and alerts and "принят" in alerts[0].lower()
