"""Мультицвет в админ-калькуляторе цены (OpenTodo 82ebeeef): Σ грамм×цена через эндпоинт."""
import hashlib
import hmac
import json
import time
import urllib.parse

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import config, webapp


def _admin_init() -> str:
    uid = next(iter(config.ADMIN_IDS))
    user = {"id": uid, "first_name": "A"}
    params = {"user": json.dumps(user, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/admin/price_calc", webapp._api_admin_price_calc)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def test_price_calc_colors_weighted(tmp_db):
    c = await _client()
    try:
        init = _admin_init()
        # одноцвет 40 г по 2000/кг ≡ многоцвет 30 г по 2000 + 10 г по 2000 (та же средняя цена)
        r1 = await c.post("/api/admin/price_calc", json={
            "initData": init, "grams": "40", "minutes": "60", "price_per_kg": "2000", "qty": 1})
        j1 = await r1.json()
        r2 = await c.post("/api/admin/price_calc", json={
            "initData": init, "minutes": "60", "qty": 1,
            "colors": [{"grams": "30", "price_per_kg": "2000"},
                       {"grams": "10", "price_per_kg": "2000"}]})
        j2 = await r2.json()
        assert j1["ok"] and j2["ok"]
        assert j1["bd"]["total"] == j2["bd"]["total"]
        assert "2 цв." in j2["bd"]["rows"][0]["sub"]          # строка «Филамент … · 2 цв.»
        assert "цв." not in j1["bd"]["rows"][0]["sub"]
        # разные цены за кг реально влияют: 30г×1000 + 10г×3000 дороже, чем 40г×1000
        r3 = await c.post("/api/admin/price_calc", json={
            "initData": init, "minutes": "60", "qty": 1,
            "colors": [{"grams": "30", "price_per_kg": "1000"},
                       {"grams": "10", "price_per_kg": "3000"}]})
        r4 = await c.post("/api/admin/price_calc", json={
            "initData": init, "grams": "40", "minutes": "60", "price_per_kg": "1000", "qty": 1})
        t3 = (await r3.json())["bd"]["total"]
        t4 = (await r4.json())["bd"]["total"]
        assert t3 != t4
    finally:
        await c.close()


async def test_price_calc_colors_validation(tmp_db):
    c = await _client()
    try:
        init = _admin_init()
        r = await c.post("/api/admin/price_calc", json={
            "initData": init, "minutes": "60", "qty": 1,
            "colors": [{"grams": "30", "price_per_kg": "1000"}]})    # один цвет — не многоцвет
        j = await r.json()
        assert not j["ok"] and "минимум два" in j["error"]
        r2 = await c.post("/api/admin/price_calc", json={
            "initData": init, "minutes": "60", "qty": 1,
            "colors": [{"grams": "0", "price_per_kg": "1000"},
                       {"grams": "5", "price_per_kg": "1000"}]})     # нулевые граммы
        j2 = await r2.json()
        assert not j2["ok"]
    finally:
        await c.close()
