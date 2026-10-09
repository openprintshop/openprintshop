"""Удаление медиа печатей и заказов (OpenTodo 1faab6cb): скрины стола и таймлапсы.

Ключевые контракты:
- удалённый таймлапс журнала помечается tl_deleted=1 — «самолечение» и свип его не воскресят;
- вместе с журнальным видео удаляется авто-кэш по имени задания и копия у привязанного
  заказа (дубль определяется по размеру файла — как в _order_prints_media);
- файлы удаляются ТОЛЬКО внутри data/ (_unlink_data_file), чужие пути не трогаются.
"""
import hashlib
import hmac
import json
import time
import urllib.parse
from types import SimpleNamespace

from app import config, db, webapp


def _forge(uid: int) -> str:
    user = {"id": uid, "first_name": "T"}
    params = {"user": json.dumps(user, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


def _req(body: dict):
    async def _json():
        return body
    return SimpleNamespace(json=_json)


def _admin_body(extra: dict) -> dict:
    return {"initData": _forge(config.ADMIN_IDS[0]), **extra}


async def _resp_json(resp) -> dict:
    return json.loads(resp.body.decode())


async def test_journal_photo_delete(monkeypatch, tmp_path):
    photo = tmp_path / "collection" / "jphoto_5.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpg")
    calls = {}

    async def fake_get(jid):
        return {"id": 5, "done_photo": str(photo), "timelapse": None, "label": "Job"}

    async def fake_set(jid, **fields):
        calls[jid] = fields

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "get_print_journal", fake_get)
    monkeypatch.setattr(db, "set_print_journal_fields", fake_set)
    resp = await webapp._api_admin_journal_media_delete(_req(_admin_body({"id": 5, "kind": "photo"})))
    assert (await _resp_json(resp))["ok"] is True
    assert calls[5] == {"done_photo": None}
    assert not photo.exists()


async def test_journal_timelapse_delete_tombstones_and_kills_copies(monkeypatch, tmp_path):
    tl = tmp_path / "timelapses" / "auto" / "job.mp4"
    tl.parent.mkdir(parents=True)
    tl.write_bytes(b"vid")
    cache = tmp_path / "timelapses" / "auto" / "job_cache.mp4"
    cache.write_bytes(b"vid2")
    order_copy = tmp_path / "timelapses" / "9.mp4"
    order_copy.write_bytes(b"vid")            # тот же размер, что tl, → это дубль заказа
    jcalls, rcalls = {}, {}

    async def fake_get_journal(jid):
        return {"id": 5, "done_photo": None, "timelapse": str(tl), "label": "Job",
                "request_id": 9}

    async def fake_set(jid, **fields):
        jcalls[jid] = fields

    async def fake_get_request(rid):
        return SimpleNamespace(id=rid, timelapse=str(order_copy))

    async def fake_update_request(rid, **fields):
        rcalls[rid] = fields

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "get_print_journal", fake_get_journal)
    monkeypatch.setattr(db, "set_print_journal_fields", fake_set)
    monkeypatch.setattr(db, "get_request", fake_get_request)
    monkeypatch.setattr(db, "update_request", fake_update_request)
    monkeypatch.setattr(webapp, "cached_timelapse_path", lambda label, printer_id=None: cache)
    resp = await webapp._api_admin_journal_media_delete(
        _req(_admin_body({"id": 5, "kind": "timelapse"})))
    assert (await _resp_json(resp))["ok"] is True
    assert jcalls[5] == {"timelapse": None, "tl_deleted": 1}
    assert rcalls[9] == {"timelapse": None}
    assert not tl.exists() and not cache.exists() and not order_copy.exists()


async def test_order_media_delete(monkeypatch, tmp_path):
    photo = tmp_path / "collection" / "done_9.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpg")
    tldir = tmp_path / "timelapses"
    tldir.mkdir()
    tl = tldir / "9.mp4"
    tl.write_bytes(b"vid")
    rcalls = []

    async def fake_get_request(rid):
        return SimpleNamespace(id=rid, done_photo=str(photo), timelapse=str(tl))

    async def fake_update_request(rid, **fields):
        rcalls.append(fields)

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(webapp, "_TIMELAPSE_DIR", tldir)
    monkeypatch.setattr(db, "get_request", fake_get_request)
    monkeypatch.setattr(db, "update_request", fake_update_request)
    resp = await webapp._api_admin_order_media_delete(_req(_admin_body({"id": 9, "kind": "photo"})))
    assert (await _resp_json(resp))["ok"] is True
    resp = await webapp._api_admin_order_media_delete(
        _req(_admin_body({"id": 9, "kind": "timelapse"})))
    assert (await _resp_json(resp))["ok"] is True
    assert rcalls == [{"done_photo": None}, {"timelapse": None}]
    assert not photo.exists() and not tl.exists()


async def test_heal_respects_tombstone(monkeypatch):
    """Запись с tl_deleted «не лечится» — иначе удалённое видео воскресало бы из авто-кэша."""
    async def boom(*a, **k):  # noqa: ARG001 — самолечение не должно трогать БД
        raise AssertionError("не должен вызываться")

    monkeypatch.setattr(db, "set_print_journal_fields", boom)
    got = await webapp._heal_journal_timelapse(
        {"id": 1, "timelapse": "", "label": "Job", "tl_deleted": 1})
    assert got == ""


def test_unlink_data_file_refuses_outside_data(tmp_path, monkeypatch):
    inside = tmp_path / "data" / "x.jpg"
    inside.parent.mkdir()
    inside.write_bytes(b"1")
    outside = tmp_path / "y.jpg"
    outside.write_bytes(b"1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    webapp._unlink_data_file(inside)
    webapp._unlink_data_file(outside)
    assert not inside.exists()
    assert outside.exists()      # чужой путь не трогаем
