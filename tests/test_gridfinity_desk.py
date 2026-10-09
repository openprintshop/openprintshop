"""Сессия «продолжить на компьютере»: ссылка на правку ОДНОГО проекта без Telegram.

Здесь проверяется не удобство, а ГРАНИЦА. Ключ из ссылки — предъявительский: у кого он есть,
тот и клиент. Поэтому он сознательно не подмешан в общий `_verify_init_data` (там он открыл бы
весь аккаунт — заказы, профиль, чат), а живёт отдельным резолвером `_gf_actor`, который вместе
с uid возвращает ГРАНИЦЫ: `scope = pid` для сессии ПК и `0` для входа из Telegram.

Цена такому решению — явная проверка в каждой ручке. Эти тесты и есть плата за то, чтобы
однажды не оказалось, что забытый роут отдаёт по ссылке на один ящик чужие заказы:

  * ручки ВНЕ проекта (список проектов, мерки, заготовки) обязаны отвечать 403;
  * ручки проекта обязаны сверять свой pid со scope — включая правку детали, где pid'а в
    адресе нет вовсе и проект берётся у самой детали;
  * распоряжаться проектом (удалить, в архив, раздать ссылки) из сессии ПК нельзя;
  * просроченный ключ не работает и убирается из базы сам.
"""
import json
import time

import pytest
from conftest import stub_auth

from app import config, ratelimit, webapp
from app.web import gridfinity as gfx

BASE = {"type": "drawer", "W": 480, "D": 270, "H": 120, "series": "M",
        "fitX": "sym", "fitY": "sym"}


def _mod(mid, cx=0, cy=0, w=2, d=2, hmm=46.85, vol=30.0):
    return {"id": mid, "kind": "bin", "gen": "tray", "cx": cx, "cy": cy, "w": w, "d": d,
            "hmm": hmm, "colorIdx": 0, "vol": vol, "ext": {"L": 0, "R": 0, "B": 0, "F": 0}}


def _cfg(*mods):
    return {"v": 4, "base": dict(BASE), "modules": list(mods), "seq": 99}


class _Req:
    def __init__(self, query=None, match_info=None):
        self.query = query or {}
        self.match_info = match_info or {}


class _Body:
    def __init__(self, raw):
        self._raw = raw

    async def read(self, n=-1):
        return self._raw if n is None or n < 0 else self._raw[:n]


class _Post:
    def __init__(self, body, match_info=None):
        self._body = body
        self.match_info = match_info or {}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.content_length = len(raw)
        self.content = _Body(raw)

    async def json(self):
        return self._body


async def _json(resp):
    return json.loads(resp.body.decode())


@pytest.fixture(autouse=True)
def _fresh_process_state():
    for d in (ratelimit._hits, gfx._price_cache, gfx._bed_cache, gfx._disc_cache):
        d.clear()
    yield
    for d in (ratelimit._hits, gfx._price_cache, gfx._bed_cache, gfx._disc_cache):
        d.clear()


@pytest.fixture
def auth(monkeypatch):
    """Как в мастерской, но ПУСТАЯ строка больше не считается подписью.

    Это принципиально: сессия ПК не шлёт initData вовсе, и стаб `lambda s: 7` признал бы её
    входом из Telegram — прибор проверял бы сам себя."""
    stub_auth(monkeypatch, _verify_init_data=lambda s: 7 if s else 0,
              _verify_user=lambda s: {"id": 7, "username": "u7"} if s else None,
              _verify_admin=lambda s: 1 if s else 0)


async def _new_project(title="Кухня", cfg=None):
    j = await _json(await webapp._api_gf_project_add(
        _Post({"initData": "x", "title": title, "config": cfg or _cfg(_mod(1))})))
    assert j["ok"] is True
    return j["id"]


async def _issue(pid):
    """Выпустить ссылку и вернуть её ключ."""
    j = await _json(await webapp._api_gf_project_patch(
        _Post({"initData": "x", "desk": 1}, {"pid": str(pid)})))
    assert j["ok"] is True and j["desk"]["url"]
    return j["desk"]["url"].rsplit("/", 1)[-1]


# ─────────────────────────── выпуск и адрес ───────────────────────────

async def test_link_is_issued_revoked_and_reissued(tmp_db, auth):
    pid = await _new_project()
    key = await _issue(pid)
    assert key and len(key) >= 20            # 18 байт энтропии, а не «показать»-девятка

    j = await _json(await webapp._api_gf_project(_Req({"initData": "x"}, {"pid": str(pid)})))
    assert j["desk"]["url"].endswith("/gfx/desk/" + key)
    assert j["desk"]["expires_at"] > time.time()

    # перевыпуск гасит прежнюю: две живые ссылки на один проект — две двери вместо одной
    key2 = await _issue(pid)
    assert key2 != key
    assert await tmp_db.gf_desk_get(key) is None

    await webapp._api_gf_project_patch(_Post({"initData": "x", "desk": 0}, {"pid": str(pid)}))
    assert await tmp_db.gf_desk_get(key2) is None
    assert await tmp_db.gf_desk_of(pid) is None


async def test_expired_key_opens_nothing_and_cleans_itself(tmp_db, auth):
    pid = await _new_project()
    await tmp_db.gf_desk_set(pid, 7, "protuhshiy", 60)
    await tmp_db._conn().execute(
        "UPDATE gridfinity_desk SET expires_at = ? WHERE token = ?",
        (int(time.time()) - 5, "protuhshiy"))
    await tmp_db._conn().commit()
    assert await tmp_db.gf_desk_get("protuhshiy") is None
    r = await webapp._api_gf_project(_Req({"gfxkey": "protuhshiy"}, {"pid": str(pid)}))
    assert r.status == 403
    # просроченную запись get убирает сам — иначе таблица растёт мёртвыми ключами
    cur = await tmp_db._conn().execute(
        "SELECT COUNT(*) AS c FROM gridfinity_desk WHERE token = 'protuhshiy'")
    assert (await cur.fetchone())["c"] == 0


# ─────────────────────────── границы ключа ───────────────────────────

async def test_key_opens_its_project_and_only_its_project(tmp_db, auth):
    mine = await _new_project("Мой")
    other = await _new_project("Соседний")
    key = await _issue(mine)

    j = await _json(await webapp._api_gf_project(_Req({"gfxkey": key}, {"pid": str(mine)})))
    assert j["ok"] is True and j["project"]["title"] == "Мой"

    # ЧУЖОЙ проект того же владельца — тоже нет: обещали «доступ к проекту», а не к аккаунту
    r = await webapp._api_gf_project(_Req({"gfxkey": key}, {"pid": str(other)}))
    assert r.status == 403


async def test_key_is_refused_outside_the_project(tmp_db, auth):
    pid = await _new_project()
    key = await _issue(pid)
    for resp in (await webapp._api_gf_projects(_Req({"gfxkey": key})),
                 await webapp._api_gf_presets(_Req({"gfxkey": key})),
                 await webapp._api_gf_project_add(_Post({"gfxkey": key, "title": "Ещё"})),
                 await webapp._api_gf_measure_add(_Post(
                     {"gfxkey": key, "title": "Кухня", "W": 600, "D": 400, "H": 80}))):
        assert resp.status == 403, resp.body


async def test_key_edits_the_layout_and_the_parts_of_its_project(tmp_db, auth):
    pid = await _new_project(cfg=_cfg(_mod(1)))
    key = await _issue(pid)

    j = await _json(await webapp._api_gf_project_save(
        _Post({"gfxkey": key, "config": _cfg(_mod(1), _mod(2, cx=2))}, {"pid": str(pid)})))
    assert j["ok"] is True and j["diff"]["added"] == 1

    part = (await _json(await webapp._api_gf_project(
        _Req({"gfxkey": key}, {"pid": str(pid)}))))["parts"][0]
    j = await _json(await webapp._api_gf_part_patch(
        _Post({"gfxkey": key, "name": "Под отвёртки"}, {"id": str(part["id"])})))
    assert j["ok"] is True
    assert [p for p in j["parts"] if p["id"] == part["id"]][0]["name"] == "Под отвёртки"


async def test_key_cannot_touch_a_part_of_another_project(tmp_db, auth):
    mine = await _new_project("Мой")
    other = await _new_project("Соседний", cfg=_cfg(_mod(1)))
    key = await _issue(mine)
    alien = (await _json(await webapp._api_gf_project(
        _Req({"initData": "x"}, {"pid": str(other)}))))["parts"][0]
    # у этой ручки pid'а в адресе НЕТ — границы берутся у самой детали
    r = await webapp._api_gf_part_patch(
        _Post({"gfxkey": key, "name": "хоп"}, {"id": str(alien["id"])}))
    assert r.status == 403


async def test_key_cannot_dispose_of_the_project(tmp_db, auth):
    pid = await _new_project()
    key = await _issue(pid)
    for body in ({"delete": 1}, {"archived": 1}, {"share": 1}, {"desk": 1}):
        r = await webapp._api_gf_project_patch(
            _Post(dict(body, gfxkey=key), {"pid": str(pid)}))
        assert r.status == 403, body
    # проект на месте, ссылка «показать» не появилась
    j = await _json(await webapp._api_gf_project(_Req({"initData": "x"}, {"pid": str(pid)})))
    assert j["ok"] is True and not j["share"]


# ─────────────────────────── заказ из сессии ───────────────────────────

async def test_order_from_desk_session_signs_the_request_by_past_name(tmp_db, auth):
    """initData в сессии нет, а подпись в заявке нужна: берём ту, которой клиент уже
    подписывался. Иначе мастер видит в списке безымянный заказ и не понимает, чей он."""
    pid = await _new_project(cfg=_cfg(_mod(1), _mod(2, cx=2)))
    key = await _issue(pid)
    parts = (await _json(await webapp._api_gf_project(
        _Req({"initData": "x"}, {"pid": str(pid)}))))["parts"]

    # первая партия — из Telegram: подпись приезжает в initData
    j = await _json(await webapp._api_gf_order(
        _Post({"initData": "x", "ids": [parts[0]["id"]]}, {"pid": str(pid)})))
    assert j["ok"] is True
    assert str(getattr(await tmp_db.get_request(j["order_id"]), "user_name", "")) == "@u7"

    # вторая — из сессии ПК: initData нет вовсе, имя обязано найтись само
    j2 = await _json(await webapp._api_gf_order(
        _Post({"gfxkey": key, "ids": [parts[1]["id"]]}, {"pid": str(pid)})))
    assert j2["ok"] is True
    assert str(getattr(await tmp_db.get_request(j2["order_id"]), "user_name", "")) == "@u7"


async def test_order_from_desk_session_falls_back_to_id(tmp_db, auth):
    """Прошлых заявок нет — подписываем id, а не пустой строкой: пустое имя в списке
    заявок мастер прочитает как сбой, а не как «клиент без имени»."""
    pid = await _new_project(cfg=_cfg(_mod(1)))
    key = await _issue(pid)
    ids = [p["id"] for p in (await _json(await webapp._api_gf_project(
        _Req({"gfxkey": key}, {"pid": str(pid)}))))["parts"]]
    j = await _json(await webapp._api_gf_order(
        _Post({"gfxkey": key, "ids": ids}, {"pid": str(pid)})))
    assert j["ok"] is True
    assert str(getattr(await tmp_db.get_request(j["order_id"]), "user_name", "")) == "id 7"


# ─────────────────────────── страница ───────────────────────────

async def test_page_serves_the_project_and_hides_nothing_else(tmp_db, auth):
    pid = await _new_project("Кухня")
    key = await _issue(pid)
    r = await webapp._gf_desk_page(_Req(match_info={"token": key}))
    assert r.status == 200
    body = r.text
    assert key in body and f"window.__GFX_PID__={pid}" in body
    # на странице ровно то, что нужно мастерской: ни админки, ни корзины
    assert "screens_workshop.js" in body and "gfxdesk.js" in body
    assert "screens_admin" not in body


async def test_unknown_and_archived_link_do_not_open(tmp_db, auth):
    pid = await _new_project()
    key = await _issue(pid)
    assert (await webapp._gf_desk_page(_Req(match_info={"token": "нетакого"}))).status == 404
    await webapp._api_gf_project_patch(_Post({"initData": "x", "archived": 1}, {"pid": str(pid)}))
    # архив клиент читает как «удалён» — ссылка обязана погаснуть вместе с проектом
    assert (await webapp._gf_desk_page(_Req(match_info={"token": key}))).status == 404


def test_link_points_at_the_public_address(monkeypatch):
    """Адрес строится от публичного URL бота, а не от того, что пришло в запросе:
    подставить свой домен в ссылку, которую клиент потом откроет, не должно быть можно."""
    # фиксированный адрес вместо .env: в worktree и на чистой машине его нет
    monkeypatch.setattr(config, "WEBAPP_PUBLIC_URL", "https://studio.example/")
    assert gfx._gf_desk_url("abc") == "https://studio.example/gfx/desk/abc"
