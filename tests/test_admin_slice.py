"""Мастер «закидывает» модель по ссылке для расчёта индивидуального заказа (OpenTodo e78bf8f2).

Эндпоинт /api/admin/order/slice скачивает модель по ссылке, слайсит выбранным пластиком и
дефолтными (или указанными) пресетами и возвращает готовые ИТОГОВЫЕ граммы/время — мастер не
считает руками, а подставляет их в счёт. Скачивание и слайсинг подменяем (без сети и без слайсера),
тестируем именно обвязку эндпоинта: разбор параметров, авто-выбор дефолтных пресетов, формат
ответа, отказ не-админу и аккуратную обработку плохой ссылки / ошибки слайсинга.
"""
import json
from types import SimpleNamespace

from conftest import stub_auth

from app import calcservice, downloader, webapp

ADMIN_ID = 777


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def _custom_order(db):
    return await db.add_request(
        user_id=1, user_name="U", model_label="Инд.", printer="", plastic_name="",
        quantity=1, quality="", strength="", grams_total=0, minutes_total=0, price=0,
        preview_path=None, is_custom=1, description="хочу")


def _fake_fetch(label="Модель.stl"):
    async def fetch(url):                       # имитируем downloader.fetch_from_url
        return ([f"/tmp/{label}"], label)
    return fetch


def _fake_compute(*, grams=84.0, minutes=370.0, dims="60×88×23 мм", capture=None):
    async def compute(**kw):                    # имитируем calcservice.compute(save=False)
        if capture is not None:
            capture.update(kw)
        return {"bd": SimpleNamespace(grams_total=grams, minutes_total=minutes), "dims": dims}
    return compute


def _patch(monkeypatch, *, fetch=None, compute=None, admin=ADMIN_ID):
    stub_auth(monkeypatch, _verify_admin=admin)
    if fetch is not None:
        monkeypatch.setattr(downloader, "fetch_from_url", fetch)
    if compute is not None:
        monkeypatch.setattr(calcservice, "compute", compute)


async def _call(body):
    resp = await webapp._api_admin_order_slice(_FakeRequest(body))
    return resp.status, json.loads(resp.body.decode())


async def test_slice_returns_totals(tmp_db, monkeypatch):
    """Успешный путь: ссылка → готовые итоговые граммы/время (округлённые) и габариты."""
    db = tmp_db
    pid = await db.add_plastic("Test PLA", 1000.0, 1.0)
    rid = await _custom_order(db)
    cap = {}
    _patch(monkeypatch, fetch=_fake_fetch(), compute=_fake_compute(capture=cap))

    status, out = await _call({"initData": "x", "id": rid, "url": "https://example.com/m.stl",
                               "plastic_id": pid, "qty": 3})
    assert status == 200
    assert out["ok"] is True
    assert out["grams"] == 84.0
    assert out["minutes"] == 370          # округлено до целых минут
    assert out["dims"] == "60×88×23 мм"
    # эндпоинт честно проброшен в compute: выбранный пластик, тираж, save=False
    assert cap["plastic"].id == pid
    assert cap["qty"] == 3
    assert cap["save"] is False
    assert cap["quality"] is not None and cap["strength"] is not None   # дефолтные пресеты подобраны


async def test_slice_defaults_presets_when_unspecified(tmp_db, monkeypatch):
    """Без quality_id/strength_id берём дефолтный (или первый) пресет, а не падаем."""
    db = tmp_db
    pid = await db.add_plastic("Test PLA", 1000.0, 1.0)
    rid = await _custom_order(db)
    cap = {}
    _patch(monkeypatch, fetch=_fake_fetch(), compute=_fake_compute(capture=cap))

    status, out = await _call({"initData": "x", "id": rid, "url": "https://example.com/m.stl",
                               "plastic_id": pid})
    assert out["ok"] is True
    quals = await db.list_quality(active_only=True)
    strs = await db.list_strength(active_only=True)
    expect_q = next((q for q in quals if q.is_default), quals[0]).id
    expect_s = next((s for s in strs if s.is_default), strs[0]).id
    assert cap["quality"].id == expect_q
    assert cap["strength"].id == expect_s
    assert cap["qty"] == 1                # дефолтный тираж


async def test_slice_rejects_bad_url(tmp_db, monkeypatch):
    db = tmp_db
    rid = await _custom_order(db)
    _patch(monkeypatch, fetch=_fake_fetch(), compute=_fake_compute())
    _, out = await _call({"initData": "x", "id": rid, "url": "не ссылка"})
    assert out["ok"] is False
    assert "ссылк" in out["error"].lower()


async def test_slice_forbidden_for_non_admin(tmp_db, monkeypatch):
    db = tmp_db
    rid = await _custom_order(db)
    _patch(monkeypatch, fetch=_fake_fetch(), compute=_fake_compute(), admin=None)
    resp = await webapp._api_admin_order_slice(
        _FakeRequest({"initData": "x", "id": rid, "url": "https://example.com/m.stl"}))
    assert resp.status == 403


async def test_slice_unknown_order(tmp_db, monkeypatch):
    _patch(monkeypatch, fetch=_fake_fetch(), compute=_fake_compute())
    _, out = await _call({"initData": "x", "id": 999999, "url": "https://example.com/m.stl"})
    assert out["ok"] is False


async def test_slice_reports_slicer_error(tmp_db, monkeypatch):
    """Ошибка слайсинга (CalcError) превращается в аккуратный ok:false, а не в 500."""
    db = tmp_db
    pid = await db.add_plastic("Test PLA", 1000.0, 1.0)
    rid = await _custom_order(db)

    async def boom(**kw):
        raise calcservice.CalcError("слайсер не смог")
    _patch(monkeypatch, fetch=_fake_fetch(), compute=boom)

    status, out = await _call({"initData": "x", "id": rid, "url": "https://example.com/m.stl",
                               "plastic_id": pid})
    assert status == 200
    assert out["ok"] is False
    assert "слайсер не смог" in out["error"]
