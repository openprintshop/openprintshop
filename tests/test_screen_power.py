"""Тумблер питания TURZX-экрана: флаг settings.turzx_enabled, локальный poll-эндпоинт для
рендерера и админ-эндпоинт вкл/выкл. Зеркало остальных админ-тестов (прямой вызов хендлеров)."""
import hashlib
import hmac
import json
import time
import urllib.parse
from types import SimpleNamespace

from app import config, db, webapp


def _forge(uid: int, token: str) -> str:
    user = {"id": uid, "first_name": "T"}
    params = {"user": json.dumps(user, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


class _JsonRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _local_get(host="127.0.0.1", remote="127.0.0.1", headers=None):
    """headers добавлены слиянием предикатов локальности (волна 4 цикла 2): единый
    webauth.is_local_request читает peer, заголовки прокси и Host разом."""
    return SimpleNamespace(host=host, remote=remote, headers=headers or {}, query={})


async def test_screen_power_default_on(tmp_db):
    """Без флага в БД экран считается включённым — поведение как до фичи."""
    assert await webapp._turzx_enabled() is True
    resp = await webapp._api_screen_power(_local_get())
    assert resp.status == 200
    assert json.loads(resp.body.decode("utf-8")) == {"ok": True, "enabled": True}


async def test_screen_power_toggle_off_then_on(tmp_db):
    """Админ гасит экран → флаг и локальный эндпоинт отдают enabled:false; затем зажигает обратно."""
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)

    resp = await webapp._api_admin_screen_power(_JsonRequest({"initData": init, "on": False}))
    assert resp.status == 200
    assert json.loads(resp.body.decode("utf-8")) == {"ok": True, "turzx": {"enabled": False}}
    assert await webapp._turzx_enabled() is False
    pr = await webapp._api_screen_power(_local_get())
    assert json.loads(pr.body.decode("utf-8"))["enabled"] is False

    resp = await webapp._api_admin_screen_power(_JsonRequest({"initData": init, "on": True}))
    assert json.loads(resp.body.decode("utf-8"))["turzx"]["enabled"] is True
    assert await webapp._turzx_enabled() is True


async def test_admin_status_reports_turzx(tmp_db):
    """/api/admin/status несёт текущее питание экрана — мини-апка засевает им тумблер."""
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    await db.set_setting("turzx_enabled", 0)
    resp = await webapp._api_admin_status(SimpleNamespace(query={"initData": init}))
    assert resp.status == 200
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["status"]["turzx"] == {"enabled": False}


async def test_screen_power_rejects_remote(tmp_db):
    """Поллер локален: чужой peer ИЛИ публичный Host (туннель) → 403, состояние наружу не отдаём."""
    pub_host = await webapp._api_screen_power(_local_get(host="bot.example.ts.net"))
    assert pub_host.status == 403
    far_peer = await webapp._api_screen_power(_local_get(remote="203.0.113.7"))
    assert far_peer.status == 403


async def test_admin_screen_power_rejects_non_admin(tmp_db):
    """Валидная подпись, но не админ → 403; флаг не меняется."""
    init = _forge(999999, config.BOT_TOKEN)
    resp = await webapp._api_admin_screen_power(_JsonRequest({"initData": init, "on": False}))
    assert resp.status == 403
    assert await webapp._turzx_enabled() is True
