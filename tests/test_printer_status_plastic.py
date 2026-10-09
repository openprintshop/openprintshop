"""«Пластик не указан» при живой печати (фидбэк 21.07): печать, стартованная не из бота,
не имела state.plastic, хотя пластик знают привязанный заказ и активный слот CANVAS.
/api/admin/printer_status обязан заполнять его цепочкой: заказ из привязки → слот CANVAS.
"""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import elegoo, webapp
from app.web import printerjob


def _fake_elegoo(monkeypatch, *, printing: bool, extras: dict) -> None:
    monkeypatch.setattr(elegoo, "enabled", lambda: True)
    monkeypatch.setattr(elegoo, "cached_status",
                        lambda max_age=30.0, ep=None: {"ok": True, "state": {}})
    monkeypatch.setattr(elegoo, "enrich_status",
                        lambda state: {"print_stats": {"state": "printing" if printing else "standby"}})
    monkeypatch.setattr(elegoo, "summarize", lambda state: {})
    monkeypatch.setattr(elegoo, "extras", lambda state: extras)
    # ep — эндпоинт принтера, которому адресован вызов (волна 2 цикла 2): драйвер передаёт
    # сюда адрес СВОЕЙ карточки, поэтому заглушка обязана его принимать.
    monkeypatch.setattr(elegoo, "camera_url", lambda ep=None: "")
    # ручка живёт в app/web/printerjob.py (волна 11) и связала _verify_admin на импорте —
    # подмена в оба модуля
    monkeypatch.setattr(webapp, "_verify_admin", lambda init_data: 1)
    monkeypatch.setattr(printerjob, "_verify_admin", lambda init_data: 1)


_SLOTS = {"active_slot": 4, "slots": [
    {"slot": 1, "brand": "ELEGOO", "name": "PLA Silk", "type": "PLA"},
    {"slot": 4, "brand": "ELEGOO", "name": "PLA Matte", "type": "PLA"}]}


async def _status() -> dict:
    client = TestClient(TestServer(webapp.make_app()))
    await client.start_server()
    try:
        resp = await client.get("/api/admin/printer_status", params={"initData": "x"})
        return await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_plastic_from_active_canvas_slot(tmp_db, monkeypatch):
    """Печать без бота и без привязки: пластик — из активного слота CANVAS."""
    _fake_elegoo(monkeypatch, printing=True, extras=_SLOTS)
    j = await _status()
    assert j["state"]["plastic"] == "ELEGOO PLA Matte"


@pytest.mark.asyncio
async def test_bound_order_plastic_wins_over_slot(tmp_db, monkeypatch):
    """Есть привязка к заказу — берём пластик заказа (каталожное имя), слот — фолбэк."""
    _fake_elegoo(monkeypatch, printing=True, extras=_SLOTS)
    rid = await tmp_db.add_request(
        user_id=7, user_name="c", model_label="Овечка Тимми", printer="P",
        plastic_name="Elegoo PLA Matte чёрный", quantity=1, quality="Q", strength="S",
        grams_total=10, minutes_total=60, price=5, preview_path=None)
    await tmp_db.set_print_binding(rid)
    j = await _status()
    assert j["state"]["plastic"] == "Elegoo PLA Matte чёрный"


@pytest.mark.asyncio
async def test_idle_printer_gets_no_plastic(tmp_db, monkeypatch):
    """Принтер свободен — пластик не выдумываем."""
    _fake_elegoo(monkeypatch, printing=False, extras=_SLOTS)
    j = await _status()
    assert "plastic" not in j["state"]
