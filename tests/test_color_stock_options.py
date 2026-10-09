"""Палитра клиента в /api/options не содержит цветов «на нуле склада».

Решение владельца 21.08.2026 (приёмка перед промоушеном): шарик отсутствующего цвета
должен ПРОПАДАТЬ, а не «гаситься» — погашенный на экране сметы читался как доступный.
Сервер скрывает такие цвета из colors/color_css/spools; пластик, у которого цвета
заведены, но доступных не осталось, исчезает из выдачи целиком. Незаведённые цвета
остаются доступными: учёт по цветам может не вестись (страховка прежнего поведения).
"""
import json

import pytest
from conftest import stub_auth

from app import webapp

UID = 4242


@pytest.fixture()
def _auth(monkeypatch):
    stub_auth(monkeypatch, _verify_init_data=UID)


def _plastic(j, pid):
    return next((p for p in j["plastics"] if p["id"] == pid), None)


async def _options(get_req):
    resp = await webapp._api_options(get_req({"initData": "x"}))
    return json.loads(resp.body.decode())


async def test_zero_stock_color_disappears(tmp_db, get_req, _auth):
    db = tmp_db
    pid = await db.add_plastic("Тест-палитра", 90, 1.24)
    await db.update_plastic(pid, colors="Белый, Синий, Красный")
    await db.set_color_stock(pid, "Белый", 500, 0)
    await db.set_color_stock(pid, "Синий", 0, 0)        # заведён и кончился → пропадает
    j = await _options(get_req)
    p = _plastic(j, pid)
    assert p is not None
    assert p["colors"] == ["Белый", "Красный"]          # Красный не заведён → доступен
    assert len(p["color_css"]) == len(p["colors"])      # параллельные массивы не разъехались
    assert p["out_of_stock"] == []                      # гасить фронту больше нечего
    assert "Синий" not in p["spools"]


async def test_all_colors_out_hides_the_plastic(tmp_db, get_req, _auth):
    db = tmp_db
    pid = await db.add_plastic("Всё кончилось", 90, 1.24)
    await db.update_plastic(pid, colors="Белый, Синий")
    await db.set_color_stock(pid, "Белый", 0, 0)
    await db.set_color_stock(pid, "Синий", 0, 0)
    j = await _options(get_req)
    assert _plastic(j, pid) is None, "пластик без единого доступного цвета не должен предлагаться"


async def test_untracked_palette_stays_visible(tmp_db, get_req, _auth):
    """Учёт по цветам не ведётся (ни одной строки склада) — палитра как была."""
    db = tmp_db
    pid = await db.add_plastic("Без учёта", 90, 1.24)
    await db.update_plastic(pid, colors="Белый, Синий")
    j = await _options(get_req)
    assert _plastic(j, pid)["colors"] == ["Белый", "Синий"]


async def test_low_stock_still_visible_and_marked(tmp_db, get_req, _auth):
    """«На исходе» — это не «нет»: цвет остаётся в палитре и помечен low_stock."""
    db = tmp_db
    pid = await db.add_plastic("На исходе", 90, 1.24)
    await db.update_plastic(pid, colors="Белый")
    await db.set_color_stock(pid, "Белый", 5, 10)
    p = _plastic(await _options(get_req), pid)
    assert p["colors"] == ["Белый"] and p["low_stock"] == ["Белый"]
