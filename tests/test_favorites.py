"""Избранное (сердечко на модельках каталога, 2026-07-11).

- db: set_favorite идемпотентен (on/off), list_favorite_ids — свежие первыми,
  list_favorites отдаёт карточки коллекции и сам отбрасывает удалённые из каталога;
- /api/favorite: auth, несуществующая модель → 404, установка/снятие;
- /api/collection?category=__fav__ — виртуальный раздел «Избранное» юзера;
- /api/options отдаёт fav_ids (источник сердечек на фронте).
"""
import json

from conftest import stub_auth

from app import webapp


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeGetRequest:
    def __init__(self, query):
        self.query = query


async def _add_item(tmp_db, n: int, uid: int = 1) -> int:
    return await tmp_db.add_to_collection(
        hash=f"favhash{n}", label=f"Модель {n}", paths=[f"/x/m{n}.stl"], preview_path=None,
        added_by=uid, dims="10×10×10", size=1.0, material="PLA")


# ---- db ----------------------------------------------------------------------------------

async def test_set_favorite_roundtrip_and_order(tmp_db):
    uid = 51
    a = await _add_item(tmp_db, 1)
    b = await _add_item(tmp_db, 2)

    assert await tmp_db.list_favorite_ids(uid) == []
    await tmp_db.set_favorite(uid, a, True)
    await tmp_db.set_favorite(uid, b, True)
    await tmp_db.set_favorite(uid, b, True)   # повторное добавление — идемпотентно
    ids = await tmp_db.list_favorite_ids(uid)
    assert set(ids) == {a, b}
    assert ids[0] == b   # свежедобавленное — первым (created_at DESC, при равенстве id DESC)

    await tmp_db.set_favorite(uid, a, False)
    await tmp_db.set_favorite(uid, a, False)  # повторное снятие — тоже идемпотентно
    assert await tmp_db.list_favorite_ids(uid) == [b]

    # избранное персонально: чужой uid ничего не видит
    assert await tmp_db.list_favorite_ids(uid + 1) == []


async def test_list_favorites_drops_deleted_collection_items(tmp_db):
    uid = 52
    a = await _add_item(tmp_db, 3)
    b = await _add_item(tmp_db, 4)
    await tmp_db.set_favorite(uid, a, True)
    await tmp_db.set_favorite(uid, b, True)
    assert {it.id for it in await tmp_db.list_favorites(uid)} == {a, b}

    await tmp_db.delete_collection_item(a)   # модель убрали из каталога
    items = await tmp_db.list_favorites(uid)
    assert [it.id for it in items] == [b]
    assert await tmp_db.list_favorite_ids(uid) == [b]   # и в ids её больше нет (JOIN)


# ---- /api/favorite -----------------------------------------------------------------------

async def test_api_favorite_set_and_unset(tmp_db, monkeypatch):
    uid = 53
    cid = await _add_item(tmp_db, 5)
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)

    resp = await webapp._api_favorite(_FakeRequest({"id": cid, "on": 1}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and j["fav"] is True
    assert await tmp_db.list_favorite_ids(uid) == [cid]

    resp = await webapp._api_favorite(_FakeRequest({"id": cid, "on": 0}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and j["fav"] is False
    assert await tmp_db.list_favorite_ids(uid) == []


async def test_api_favorite_unknown_model_404(tmp_db, monkeypatch):
    stub_auth(monkeypatch, _verify_init_data=lambda s: 54)
    resp = await webapp._api_favorite(_FakeRequest({"id": 999999, "on": 1}))
    assert resp.status == 404


async def test_api_favorite_requires_auth(tmp_db, monkeypatch):
    stub_auth(monkeypatch, _verify_init_data=lambda s: None)
    resp = await webapp._api_favorite(_FakeRequest({"id": 1, "on": 1}))
    assert resp.status == 403


# ---- /api/collection?category=__fav__ + /api/options.fav_ids ------------------------------

async def test_api_collection_fav_category(tmp_db, monkeypatch):
    uid = 55
    a = await _add_item(tmp_db, 6)
    b = await _add_item(tmp_db, 7)
    await _add_item(tmp_db, 8)   # не в избранном — в разделе быть не должно
    await tmp_db.set_favorite(uid, a, True)
    await tmp_db.set_favorite(uid, b, True)
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)

    resp = await webapp._api_collection(_FakeGetRequest({"initData": "x", "category": "__fav__"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert [it["id"] for it in j["items"]] == [b, a]   # свежедобавленное первым
    assert j["next_offset"] is None                    # всё уместилось в страницу


async def test_api_options_exposes_fav_ids(tmp_db, monkeypatch):
    uid = 56
    cid = await _add_item(tmp_db, 9)
    await tmp_db.set_favorite(uid, cid, True)
    # минимум пресетов для _api_options
    printer_id = await tmp_db.add_printer("P", "mp.json", "pd", "fd")
    await tmp_db.set_default_printer(printer_id)
    await tmp_db.add_quality("Standard", 0.2, "proc")
    await tmp_db.add_strength("Normal", 15, 3)
    await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)

    resp = await webapp._api_options(_FakeGetRequest({"initData": "x"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert j["fav_ids"] == [cid]
