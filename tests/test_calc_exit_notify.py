"""Авто-«Уведомить, когда посчитается» при выходе из заказа во время слайса (2026-07-11).

- клиент закрыл мини-апп, пока шёл честный слайс → aiohttp отменяет /api/calc (разрыв
  соединения), shield-обёртка сама подписывает его — доживающий слайс шлёт смету сообщением;
- тихий расчёт (карточка товара / пакет ссылок / кастомайзер, body.quiet) при разрыве
  НЕ подписывает: юзер цену не ждал;
- фоновое уточнение (_refine_calc) досылает смету, если подписка висит: мгновенные пути
  _api_calc_inner (MW/calc_cache) возвращаются до своей отправки уведомления.
"""
import asyncio
import json

import pytest
from conftest import stub_auth, stub_bot

from app import calcservice, pricing, webapp


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def _wait_until(pred, tries: int = 100) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.01)


async def _std_presets(tmp_db):
    printer_id = await tmp_db.add_printer("P", "mp.json", "pd", "fd")
    await tmp_db.set_default_printer(printer_id)
    quality_id = await tmp_db.add_quality("Standard", 0.2, "proc")
    await tmp_db.set_default_quality(quality_id)
    strength_id = await tmp_db.add_strength("Normal", 15, 3)
    await tmp_db.set_default_strength(strength_id)
    return (await tmp_db.get_default_printer(), await tmp_db.get_quality(quality_id),
            await tmp_db.get_strength(strength_id))


def _compute_result(settings, kw, volume=9.0, minutes=50.0):
    bd = pricing.compute_price(volume, minutes, kw["qty"], kw["plastic"], settings,
                               extra_colors=kw.get("extra_colors", 0))
    return {"bd": bd, "settings": settings, "preview_path": "", "cached": False,
            "pdisp": kw["plastic"].name, "plates": 1, "dims": "",
            "color_grams": [], "plastic_grams": [], "scale_pct": kw.get("scale_pct", 100),
            "scale_clamped": False, "scale_max_pct": None}


def _capture_notify(monkeypatch):
    """Подменяет notify_user и _bot; возвращает список (uid, text) отправленных сообщений."""
    sent = []

    async def fake_notify(bot, nuid, text, **kw):
        sent.append((nuid, text))
        return True

    monkeypatch.setattr(webapp.notify, "notify_user", fake_notify)
    stub_bot(monkeypatch, object())
    return sent


async def _model_with_file(tmp_db, tmp_path, uid, name):
    pid = await tmp_db.add_plastic("Test PLA", 1000.0, 1.24, "PLA")
    plastic = await tmp_db.get_plastic(pid)
    stl = tmp_path / f"{name}.stl"
    stl.write_bytes(b"fake-stl-" + name.encode())
    token = f"tok-{name}"
    webapp._models[token] = {"paths": [str(stl)], "label": "Кубик", "uid": uid, "plates": 1,
                             "multicolor": False, "num_colors": 1}
    return token, plastic


# ---- разрыв соединения /api/calc во время честного слайса --------------------------------

async def test_disconnect_during_slice_auto_subscribes_and_notifies(tmp_db, tmp_path, monkeypatch):
    uid = 71
    printer, quality, strength = await _std_presets(tmp_db)
    token, plastic = await _model_with_file(tmp_db, tmp_path, uid, "exit1")

    started, release = asyncio.Event(), asyncio.Event()

    async def compute(**kw):
        started.set()
        await release.wait()
        return _compute_result(await tmp_db.get_settings(), kw)

    monkeypatch.setattr(calcservice, "compute", compute)
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    sent = _capture_notify(monkeypatch)

    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 1}
    t = asyncio.create_task(webapp._api_calc(_FakeRequest(body)))
    await asyncio.wait_for(started.wait(), 5)
    t.cancel()   # клиент закрыл мини-апп: aiohttp отменяет хендлер
    with pytest.raises(asyncio.CancelledError):
        await t
    assert webapp._models[token].get("notify_calc") == uid   # авто-подписка на уведомление

    release.set()   # слайс доживает в отдельной задаче (shield) и сам шлёт смету
    await _wait_until(lambda: sent)
    assert sent and sent[0][0] == uid and "Расчёт готов" in sent[0][1]
    assert "notify_calc" not in webapp._models[token]


async def test_disconnect_quiet_calc_does_not_subscribe(tmp_db, tmp_path, monkeypatch):
    uid = 72
    printer, quality, strength = await _std_presets(tmp_db)
    token, plastic = await _model_with_file(tmp_db, tmp_path, uid, "exit2")

    started, release = asyncio.Event(), asyncio.Event()

    async def compute(**kw):
        started.set()
        await release.wait()
        return _compute_result(await tmp_db.get_settings(), kw)

    monkeypatch.setattr(calcservice, "compute", compute)
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    sent = _capture_notify(monkeypatch)

    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 1, "quiet": 1}
    t = asyncio.create_task(webapp._api_calc(_FakeRequest(body)))
    await asyncio.wait_for(started.wait(), 5)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert "notify_calc" not in webapp._models[token]   # тихий расчёт не подписывает

    release.set()
    # дожидаемся, пока доживающий слайс дойдёт до конца (last_payload появляется после
    # точки отправки уведомления) — сообщение так и не должно уйти
    await _wait_until(lambda: webapp._models[token].get("last_payload"))
    assert sent == []


async def test_disconnect_after_done_does_not_leave_flag(tmp_db, tmp_path, monkeypatch):
    """Отмена ПОСЛЕ завершения inner-задачи (ответ уже готов) — подписку не вешаем,
    чтобы флаг не жил до следующего расчёта."""
    uid = 74
    printer, quality, strength = await _std_presets(tmp_db)
    token, plastic = await _model_with_file(tmp_db, tmp_path, uid, "exit4")

    async def compute(**kw):
        return _compute_result(await tmp_db.get_settings(), kw)

    monkeypatch.setattr(calcservice, "compute", compute)
    stub_auth(monkeypatch, _verify_init_data=lambda s: uid)
    sent = _capture_notify(monkeypatch)

    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 1}
    resp = await webapp._api_calc(_FakeRequest(body))   # без отмены — обычный успешный проход
    assert json.loads(resp.body.decode())["ok"] is True
    assert "notify_calc" not in webapp._models[token]
    assert sent == []


# ---- фоновое уточнение досылает смету, если подписка висит --------------------------------

async def test_refine_sends_pending_notify_on_success(tmp_db, tmp_path, monkeypatch):
    uid = 75
    printer, quality, strength = await _std_presets(tmp_db)
    token, plastic = await _model_with_file(tmp_db, tmp_path, uid, "exit5")
    webapp._models[token].update({
        "calc_seq": 7,
        "last_payload": {"refining": True, "approx": True, "total": "100 руб."},
        "notify_calc": uid,   # вышел, не дождавшись даже мгновенной сметы
    })

    async def compute(**kw):
        return _compute_result(await tmp_db.get_settings(), kw)

    monkeypatch.setattr(calcservice, "compute", compute)
    sent = _capture_notify(monkeypatch)

    await webapp._refine_calc(token, 7, uid=uid, printer=printer, plastic=plastic,
                              quality=quality, strength=strength, color="", colors_sel=[],
                              qty=1, extra_colors=0, multicolor=False, promo_pct=0.0)
    assert sent and sent[0][0] == uid and "Расчёт готов" in sent[0][1]
    assert "notify_calc" not in webapp._models[token]
    assert "refining" not in webapp._models[token]["last_payload"]
    # дослали УТОЧНЁННУЮ цену (честный слайс), а не приближённую
    assert webapp._models[token]["last"]["approx"] is False


async def test_refine_failure_still_sends_approx_price(tmp_db, tmp_path, monkeypatch):
    """Уточняющий слайс упал — смета остаётся быстрой, но подписанному юзеру всё равно
    досылаем то, что есть (approx), а не молчим."""
    uid = 76
    printer, quality, strength = await _std_presets(tmp_db)
    token, plastic = await _model_with_file(tmp_db, tmp_path, uid, "exit6")
    webapp._models[token].update({
        "calc_seq": 3,
        "last_payload": {"refining": True, "approx": True, "total": "100 руб."},
        "notify_calc": uid,
    })

    async def compute(**kw):
        raise calcservice.CalcError("слайсер упал")

    monkeypatch.setattr(calcservice, "compute", compute)
    sent = _capture_notify(monkeypatch)

    await webapp._refine_calc(token, 3, uid=uid, printer=printer, plastic=plastic,
                              quality=quality, strength=strength, color="", colors_sel=[],
                              qty=1, extra_colors=0, multicolor=False, promo_pct=0.0)
    assert sent and sent[0][0] == uid and "100 руб." in sent[0][1]
    assert "notify_calc" not in webapp._models[token]
    assert "refining" not in webapp._models[token]["last_payload"]
