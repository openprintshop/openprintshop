"""База знаний ВЛАДЕЛЬЦА студии (OpenTodo c356387a).

Раздел адресован владельцу и его операторам (как настроить, откалибровать, что делать при
типовой проблеме), а не заказчикам мастерской: клиентских ручек у базы знаний нет вовсе —
это закреплено тестом, иначе инструкции «для себя» однажды снова уедут наружу. Черновик
не показывается даже на самом экране базы знаний.
"""
import json

import pytest
from conftest import stub_auth

from app import config, roles, webapp

OWNER_ID = 950
OPERATOR_ID = 951


class _Post:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _Get:
    def __init__(self, **query):
        self.query = dict(query, initData="x")


def _body(resp):
    return json.loads(resp.body.decode())


# ---- db -----------------------------------------------------------------------------------

async def test_kb_crud_and_sort(tmp_db):
    b = await tmp_db.kb_save(None, "Вторая", "текст 2", sort=10, published=True)
    a = await tmp_db.kb_save(None, "Первая", "текст 1", sort=1, published=True)
    draft = await tmp_db.kb_save(None, "Черновик", "не готово", sort=0, published=False)

    assert [x["id"] for x in await tmp_db.kb_list()] == [a, b]           # порядок по sort
    assert [x["id"] for x in await tmp_db.kb_list(published_only=False)] == [draft, a, b]

    await tmp_db.kb_save(a, "Первая (правка)", "текст 1.1", sort=1, published=True)
    art = await tmp_db.kb_get(a)
    assert art["title"] == "Первая (правка)" and art["body"] == "текст 1.1"
    assert art["created_at"] and art["updated_at"]

    await tmp_db.kb_delete(b)
    assert await tmp_db.kb_get(b) is None
    assert [x["id"] for x in await tmp_db.kb_list()] == [a]


# ---- клиентских ручек не существует ---------------------------------------------------------

def test_no_client_facing_kb_endpoints():
    """Сообщество и база знаний делаются для ВЛАДЕЛЬЦЕВ студий, а не для клиентов мастерской.

    Клиентские ручки /api/kb и /api/community были свёрнуты вместе с фичей: сторож ловит
    попытку вернуть их случайной правкой — с ними в мини-апп вернутся и статьи «для себя»."""
    app = webapp.make_app()
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert not [p for p in paths if p.startswith("/api/kb") or p.startswith("/api/community")]
    for name in ("_api_kb", "_api_kb_article", "_api_community", "_api_community_post"):
        assert not hasattr(webapp, name), name


# ---- админские ручки ----------------------------------------------------------------------

async def test_admin_crud_roundtrip(tmp_db, monkeypatch):
    stub_auth(monkeypatch, _verify_admin=OWNER_ID)

    j = _body(await webapp._api_admin_kb_save(
        _Post({"title": "Уход за принтером", "body": "чистить сопло", "sort": 5,
               "published": True})))
    aid = j["id"]
    assert j["ok"] is True and aid

    arts = _body(await webapp._api_admin_kb(_Get()))["articles"]
    assert len(arts) == 1 and arts[0]["published"] == 1 and arts[0]["sort"] == 5

    _body(await webapp._api_admin_kb_save(
        _Post({"id": aid, "title": "Уход за принтером", "body": "чистить сопло и стол",
               "sort": 5, "published": False})))
    arts = _body(await webapp._api_admin_kb(_Get()))["articles"]
    assert len(arts) == 1 and arts[0]["published"] == 0     # правка, а не второй экземпляр
    assert arts[0]["body"].endswith("и стол")

    assert _body(await webapp._api_admin_kb_save(_Post({"title": "  "})))["ok"] is False
    assert _body(await webapp._api_admin_kb_delete(_Post({"id": 0})))["ok"] is False
    assert _body(await webapp._api_admin_kb_delete(_Post({"id": aid})))["ok"] is True
    assert _body(await webapp._api_admin_kb(_Get()))["articles"] == []


async def test_admin_routes_are_owner_only(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [OWNER_ID], raising=False)
    config.set_staff_roles({OPERATOR_ID: config.ROLE_OPERATOR})
    # подмена через stub_auth: _caps_mw уехал в app/webguard.py и зовёт _verify_init_data из
    # СВОИХ глобалов — патч только на webapp до него больше не достаёт (волна 4 цикла 2)
    stub_auth(monkeypatch, _verify_init_data=lambda s: OPERATOR_ID)

    async def handler(_req):
        raise AssertionError("хендлер не должен вызваться")

    for path in ("/api/admin/kb", "/api/admin/kb/save", "/api/admin/kb/delete"):
        assert webapp._route_cap(path) == roles.CAP_SETTINGS
        res = await webapp._caps_mw(_MwReq(path), handler)
        assert res.status == 403, path


@pytest.mark.asyncio
async def test_admin_endpoints_reject_stranger(tmp_db, monkeypatch):
    stub_auth(monkeypatch, _verify_admin=None)
    assert (await webapp._api_admin_kb(_Get())).status == 403
    assert (await webapp._api_admin_kb_save(_Post({"title": "x"}))).status == 403


class _MwReq:
    """Минимальный запрос для прогона _caps_mw (как в tests/test_roles.py)."""

    def __init__(self, path):
        self.path = path
        self.method = "POST"
        self.content_type = "application/json"
        self.query = {}
        self._state = {}
        self.match_info = _MatchInfo(path)

    async def json(self):
        return {"initData": "x"}

    def __getitem__(self, k):
        return self._state[k]

    def __setitem__(self, k, v):
        self._state[k] = v

    def get(self, k, default=None):
        return self._state.get(k, default)


class _MatchInfo(dict):
    def __init__(self, canonical):
        super().__init__()
        self.route = type("R", (), {"resource": type("Res", (), {"canonical": canonical})()})()
