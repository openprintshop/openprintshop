"""Брендинг мастерской — white-label (A3).

Что здесь важно поймать:
1. Акцентный цвет уезжает ПРЯМО в CSS-переменную страницы. Пропустим сюда произвольную
   строку — сломаем тему у всех клиентов, а «red;}» ещё и закроет правило (инъекция).
2. Бренд подставляется в index.html на сервере. Если подстановка отвалится, клиент увидит
   не чужое имя, а буквальный `%%BRAND_JSON%%` — мини-апп при этом молча теряет бренд.
3. Пустая настройка = «наш дефолт», а не пустая строка в шапке.
"""
import hashlib
import hmac
import json
import time
import urllib.parse

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import brand, config, texts, webapp
from app import db as dbm


def _admin_init() -> str:
    uid = next(iter(config.ADMIN_IDS))
    params = {"user": json.dumps({"id": uid, "first_name": "A"}, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


async def _settings_client() -> TestClient:
    app = web.Application()
    app.router.add_get("/api/admin/settings", webapp._api_admin_settings)
    app.router.add_post("/api/admin/setting", webapp._api_admin_setting)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


# ---- цвет: единственное поле, которое утекает в CSS ----

def test_accent_accepts_hex_forms():
    assert brand.clean_accent("#2F6DF6") == "#2f6df6"
    assert brand.clean_accent("2f6df6") == "#2f6df6"      # решётку дописываем сами
    assert brand.clean_accent("  #abc  ") == "#abc"       # короткая форма и пробелы
    assert brand.clean_accent("") == ""                   # пусто = «вернуть наш цвет»


def test_accent_rejects_css_injection():
    """Любое НЕ #rgb/#rrggbb — отказ. Список не про «некрасивые значения», а про то,
    что каждая из этих строк, попав в `--accent: …`, ломает или переопределяет стиль."""
    for bad in ("red", "red;} body{display:none}", "#12345", "#gggggg",
                "var(--bg)", "url(http://evil/x.png)", "#fff;--bg:#f00", "rgb(1,2,3)",
                "expression(alert(1))", "#ff7a1a<script>"):
        assert brand.clean_accent(bad) == "", bad


async def test_accent_rejected_on_save_with_message(tmp_db):
    c = await _settings_client()
    try:
        init = _admin_init()
        r = await c.post("/api/admin/setting", json={
            "initData": init, "kind": "text", "key": "brand_accent", "value": "red;}"})
        j = await r.json()
        assert j["ok"] is False and "#RRGGBB" in j["error"]
        # и в базе ничего не осело
        assert (await dbm.get_text_settings()).get("brand_accent", "") == ""
    finally:
        await c.close()


async def test_accent_saved_normalized(tmp_db):
    c = await _settings_client()
    try:
        r = await c.post("/api/admin/setting", json={
            "initData": _admin_init(), "kind": "text", "key": "brand_accent", "value": "2F6DF6"})
        assert (await r.json())["ok"] is True
        assert (await dbm.get_text_settings())["brand_accent"] == "#2f6df6"
    finally:
        await c.close()


# ---- подстановка дефолтов ----

def test_empty_settings_resolve_to_our_brand():
    b = brand.resolve({})
    assert b["title"] == "OpenPrintShop"
    assert b["tagline"] == "расчёт 3D-печати"
    assert b["accent"] == "#ff7a1a"
    assert b["stock"] is True and b["logo"] == ""
    assert b["wordmark"] == ["OPEN", "PRINT", "SHOP"]


def test_former_product_name_keeps_its_wordmark():
    """Прежнее имя продукта — тоже «наше»: мастерская, где проект вырос, вписала его явно и
    сохраняет фирменный вордмарк PRINT·STUDIO, а не голое имя."""
    b = brand.resolve({"brand_title": "Print Studio"})
    assert b["stock"] is True and b["wordmark"] == ["PRINT", "STUDIO"]


def test_partial_branding_is_valid(monkeypatch, tmp_path):
    """Мастерская заменила только имя — знак и цвет остаются наши, и это нормальный режим."""
    monkeypatch.setattr(brand, "LOGO_DIR", tmp_path / "brand")
    b = brand.resolve({"brand_title": "ФабЛаб Минск"})
    assert b["title"] == "ФабЛаб Минск"
    assert b["stock"] is False            # фирменный вордмарк больше не рисуем
    assert b["accent"] == "#ff7a1a"
    assert b["tagline"] == "расчёт 3D-печати"


def test_long_title_is_cut_not_wrapped():
    """Шапка мини-аппа узкая: длинное имя режем на сервере, а не надеемся на CSS."""
    b = brand.resolve({"brand_title": "Ы" * 200})
    assert len(b["title"]) == brand.MAX_TITLE


# ---- инжект в разметку ----

def test_script_json_cannot_close_the_tag():
    """Имя вида '</script>' не должно закрывать тег и ронять загрузку мини-аппа."""
    payload = brand.to_script_json(brand.resolve({"brand_title": "</script><b>x"}))
    assert "</script>" not in payload
    assert "<\\/" in payload
    # и это по-прежнему валидный JSON
    assert json.loads(payload.replace("<\\/", "</"))["title"] == "</script><b>x"


async def test_index_html_carries_brand(tmp_db, monkeypatch, tmp_path):
    """Через настоящий HTTP: плейсхолдеров в отданной странице не остаётся, а имя и цвет
    мастерской уже внутри. Без этого мини-апп грузился бы с нашим брендом и моргал."""
    monkeypatch.setattr(brand, "LOGO_DIR", tmp_path / "brand")
    await dbm.set_text_setting("brand_title", "ФабЛаб Минск")
    await dbm.set_text_setting("brand_accent", "#2f6df6")
    client = TestClient(TestServer(webapp.make_app()))
    await client.start_server()
    try:
        body = await (await client.get("/app/")).text()
        assert "%%BRAND_JSON%%" not in body and "%%BRAND_TITLE%%" not in body
        assert "ФабЛаб Минск" in body
        assert "#2f6df6" in body
        assert "OpenPrintShop" not in body     # нашего имени в чужой странице быть не должно
        assert "Print Studio" not in body

        desk = await (await client.get("/app/desk/")).text()
        assert "%%BRAND_NAME%%" not in desk and "ФабЛаб Минск" in desk
    finally:
        await client.close()


async def test_index_html_stays_ours_by_default(tmp_db):
    client = TestClient(TestServer(webapp.make_app()))
    await client.start_server()
    try:
        body = await (await client.get("/app/")).text()
        assert "OpenPrintShop — расчёт 3D-печати" in body
        assert '"stock": true' in body.replace("'", '"')
    finally:
        await client.close()


# ---- логотип ----

def test_logo_roundtrip_and_format_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(brand, "LOGO_DIR", tmp_path / "brand")
    assert brand.logo_file() is None
    brand.save_logo(b"<svg/>", "mylogo.SVG")                 # регистр расширения не важен
    assert brand.logo_file().name == "logo.svg"
    assert brand.resolve({})["logo"].startswith("/brand/logo?v=")

    # замена другим форматом не оставляет «хвост» прежнего файла
    brand.save_logo(b"\x89PNG", "l.png")
    assert brand.logo_file().name == "logo.png"
    assert not (tmp_path / "brand" / "logo.svg").exists()

    brand.clear_logo()
    assert brand.logo_file() is None


def test_logo_rejects_bad_input(monkeypatch, tmp_path):
    monkeypatch.setattr(brand, "LOGO_DIR", tmp_path / "brand")
    for data, name in ((b"x", "logo.exe"), (b"x", "logo.gif"), (b"", "logo.png"),
                       (b"y" * (brand.MAX_LOGO_BYTES + 1), "logo.png")):
        try:
            brand.save_logo(data, name)
            raise AssertionError(f"приняли то, что не должны: {name} ({len(data)} б)")
        except ValueError:
            pass
    assert brand.logo_file() is None


# ---- тексты бота ----

def test_bot_menu_uses_brand_and_escapes_it():
    assert "<b>OpenPrintShop</b>" in texts.menu("")
    assert "<b>ФабЛаб Минск</b>" in texts.menu("ФабЛаб Минск")
    # Имя приходит из настроек и уходит в HTML-разметку Telegram: неэкранированное «<b>»
    # в названии — это ошибка парсинга и несовпадение сообщения у всех клиентов.
    assert texts.menu("<b>x").split("\n")[0] == "<b>&lt;b&gt;x</b>"
