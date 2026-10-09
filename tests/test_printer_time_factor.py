"""B1 (трек B roadmap-2026-08-20): коэффициент времени на карточке принтера.

fastcalc и константы времени откалиброваны по одной паре «движок+машина» (ElegooSlicer + CC2);
у второго принтера минуты — а с ними и цена — считались бы молча неверно. Множитель
printers.time_factor применяется к оценке ДО коррекции по журналу, predict_minutes учится по
журналу ЭТОЙ машины (с откатом на общую выборку), а подсказка printer_time_stats говорит
мастеру, куда крутить коэффициент.
"""
import json

import pytest
from conftest import stub_auth
from test_fast_only import _cube_stl, _no_slice, _presets, _sync_pools

from app import calcservice, fastcalc, webapp


async def _seed_journal(db, printer_id, ratio, n=8, material="PLA"):
    for _ in range(n):
        await db.add_print_journal(label="s", plastic_id=None, plastic_name=material, grams=1,
                                   minutes=100 * ratio, graded=1, est_minutes=100,
                                   material_type=material, printer_id=printer_id)


async def test_column_model_and_whitelist(tmp_db):
    db = tmp_db
    p = await db.get_default_printer()
    assert p.time_factor == 1.0                                  # колонка с дефолтом, модель читает
    await db.update_printer(p.id, time_factor=1.3)
    assert (await db.get_default_printer()).time_factor == 1.3
    # мусор в БД (руками/старой версией) читается как 1.0, а не роняет расчёт
    await db._conn().execute("UPDATE printers SET time_factor = 42 WHERE id = ?", (p.id,))
    await db._conn().commit()
    await db.refresh_printers()
    assert (await db.get_default_printer()).time_factor == 1.0


def test_valid_time_factor():
    assert webapp.db.valid_time_factor("1,25") == 1.25
    assert webapp.db.valid_time_factor(" 0.5 ") == 0.5
    assert webapp.db.valid_time_factor("5") == 5.0
    assert webapp.db.valid_time_factor("5.01") is None
    assert webapp.db.valid_time_factor("0.1") is None
    assert webapp.db.valid_time_factor("abc") is None
    assert webapp.db.valid_time_factor(None) is None


async def test_predict_minutes_prefers_this_printer_then_falls_back(tmp_db):
    db = tmp_db
    p1 = (await db.get_default_printer()).id
    p2 = await db.add_printer("Второй", "m.json", "proc", "fil")
    await _seed_journal(db, p1, 1.5)                             # CC2 медленнее оценки в 1.5
    await _seed_journal(db, p2, 0.8)                             # вторая — быстрее
    # порог выборки задаём явно: боевой по умолчанию поднят до 30 (решение владельца
    # 30.08.2026), а этот тест про КАСКАД «своя машина → общая выборка», а не про порог
    ms = 8
    assert (await db.predict_minutes(100, "PLA", ms, printer_id=p2))["factor"] == pytest.approx(0.8)
    assert (await db.predict_minutes(100, "PLA", ms, printer_id=p1))["factor"] == pytest.approx(1.5)
    # машина без журнала → общая выборка (медиана по всем 16 парам), как было до B1
    p3 = await db.add_printer("Третий", "m.json", "proc", "fil")
    r = await db.predict_minutes(100, "PLA", ms, printer_id=p3)
    assert r["applied"] is True and r["samples"] == 16
    # без printer_id — прежнее поведение
    assert (await db.predict_minutes(100, "PLA", ms))["samples"] == 16


async def test_printer_time_stats_hint(tmp_db):
    db = tmp_db
    p = await db.get_default_printer()
    assert await db.printer_time_stats(p.id, 1.0) == {"samples": 0, "median": None, "suggested": None}
    await _seed_journal(db, p.id, 1.2, n=5)
    st = await db.printer_time_stats(p.id, 1.1)
    assert st["samples"] == 5 and st["median"] == pytest.approx(1.2)
    assert st["suggested"] == pytest.approx(1.32)                 # текущий × медиана остатка


@pytest.mark.asyncio
async def test_compute_applies_time_factor_to_minutes_and_price(tmp_db, monkeypatch, tmp_path):
    """Множитель машины умножает минуты оценки и через machine_hour_rate — цену."""
    monkeypatch.setattr(calcservice.config, "CACHE_DIR", tmp_path / "cache", raising=False)
    monkeypatch.setattr(fastcalc.config, "CACHE_DIR", tmp_path / "cache")
    _no_slice(monkeypatch)
    _sync_pools(monkeypatch)
    await tmp_db.set_setting("time_prediction", 0)
    printer, plastic, quality, strength = await _presets(tmp_db)
    stl = _cube_stl(tmp_path / "cube20.stl", 20.0)

    async def run():
        return await calcservice.compute(
            user_id=1, model_paths=[str(stl)], model_label="cube", printer=printer, plastic=plastic,
            color="", qty=1, quality=quality, strength=strength, save=False)

    base = await run()
    await tmp_db.update_printer(printer.id, time_factor=2.0)
    printer = await tmp_db.get_default_printer()
    doubled = await run()
    assert doubled["bd"].minutes_unit == pytest.approx(base["bd"].minutes_unit * 2, rel=1e-6)
    assert doubled["bd"].grams_unit == pytest.approx(base["bd"].grams_unit)   # материал не трогаем
    assert doubled["bd"].total > base["bd"].total


async def test_admin_api_time_factor_action(tmp_db, post_req, get_req, monkeypatch):
    stub_auth(monkeypatch, _verify_admin=True, _admin_body=(1, None))
    p = await tmp_db.get_default_printer()
    resp = await webapp._api_admin_printer(post_req({"action": "time_factor", "id": p.id, "time_factor": "1.4"}))
    assert json.loads(resp.body)["ok"] is True
    assert (await tmp_db.get_default_printer()).time_factor == 1.4
    resp = await webapp._api_admin_printer(post_req({"action": "time_factor", "id": p.id, "time_factor": "7"}))
    assert json.loads(resp.body)["ok"] is False                  # вне коридора — отказ, не зажатие
    assert (await tmp_db.get_default_printer()).time_factor == 1.4
    j = json.loads((await webapp._api_admin_printers(get_req({"initData": "x"}))).body)
    card = j["printers"][0]
    assert card["time_factor"] == 1.4 and card["time_stats"]["samples"] == 0
