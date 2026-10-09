"""Пластины стола: библиотеку ведёт CC2 Control (шаг 3 синхронизации, 08.10.2026).

Решения владельца: связанную пластину ведёт принтер (имя, Z, «на столе»), её правка из нашей
админки уходит в CC2; пластины принтера без пары заводятся у нас сами; монтаж и удаление —
только в CC2 Control; связь — колонкой cc2_plate_id по карте мастера, не по имени. Фоном
библиотеку не опрашиваем: снимок — по открытию экрана калибровки и после правки.

К железу тесты не ходят: служба — подменой cc2community._get/_post.
"""
import pytest
from conftest import admin_req, body, stub_auth

from app import calibration, cc2community, cc2plates, db
from app.web import calib

HOST = "10.9.9.9"
A, B, CARBON = "701e40ba6a176df7", "bbf0a24fc47cab61", "8a6b7baa151f3fb6"


def _plate(pid, name, side, z, in_printer=True):
    return {"id": pid, "name": name, "side": side, "z_offset": z, "measured": 1791223992,
            "in_printer": in_printer, "mesh": {"points": [[0.0]]}}


LIBRARY = {"available": True, "error": "", "current": A, "pending": "", "result": "",
           "z_applied": True, "slots": {"A": "mesh", "B": "mesh"},
           "plates": [_plate(CARBON, "Карбон", "A", 0.055, False),
                      _plate(B, "Штатный лист B", "B", 0),
                      _plate(A, "Штатный лист A", "A", 0.04)]}


class _Service:
    """Их библиотека пластин: ответ на GET, журнал запросов, отказ правки по желанию."""

    def __init__(self):
        self.library = dict(LIBRARY)
        self.log: list = []
        self.refuse = ""

    def get(self, host, path, timeout=3.0):
        assert path == "/api/plates"
        self.log.append(("GET", path, None))
        if isinstance(self.library, Exception):
            raise self.library
        return self.library

    def post(self, host, path, body="", timeout=3.0):
        self.log.append(("POST", path, body))
        if self.refuse:
            raise cc2community.CommunityError(self.refuse)
        cid, name, z = body.split("\n")
        self.library = {**self.library, "plates": [
            {**p, "name": name, "z_offset": float(z)} if p["id"] == cid else p
            for p in self.library["plates"]]}
        return {"saved": True, "applied": cid == self.library["current"]}


@pytest.fixture
def svc(monkeypatch):
    s = _Service()
    monkeypatch.setattr(cc2community, "_get", s.get)
    monkeypatch.setattr(cc2community, "_post", s.post)

    async def _source():
        return cc2plates.Source(printer_id=1, host=HOST)
    monkeypatch.setattr(cc2plates, "source", _source)
    cc2plates.forget()
    yield s
    cc2plates.forget()


async def _studio_plates(tmp_db):
    """Пластины Print Studio владельца на 08.10.2026."""
    one = await tmp_db.save_calib_plate(printer_id=1, name="Текстурированная PEI (родная)",
                                        kind="textured", z_offset=-0.065)
    two = await tmp_db.save_calib_plate(printer_id=1, name="Пластина PEI из комплекта (там где звездочки)",
                                        kind="textured", z_offset=-0.005)
    await tmp_db.set_default_calib_plate(one)
    return one, two


def _by_name(rows):
    return {r["name"]: r for r in rows}


# ─────────────────────────── разбор и сверка ───────────────────────────

def test_parse_plates_drops_meshes_and_junk():
    snap = cc2plates.parse_plates({**LIBRARY, "plates": LIBRARY["plates"] + [
        {"id": "nothex", "name": "x", "side": "A"}, {"id": "0" * 16, "name": "y", "side": "C"}]})
    assert [p["id"] for p in snap["plates"]] == [CARBON, B, A]
    assert snap["plates"][2] == {"id": A, "name": "Штатный лист A", "side": "A", "z": 0.04,
                                 "measured": 1791223992, "in_printer": True}
    assert snap["current"] == A and snap["available"] is True
    assert cc2plates.parse_plates({"plates": "нет"}) is None


async def test_apply_mirrors_linked_adopts_the_rest_and_is_quiet(tmp_db):
    one, two = await _studio_plates(tmp_db)
    assert await tmp_db.cc2_plate_link(one, A) == ""
    snap = cc2plates.parse_plates(LIBRARY)
    assert await tmp_db.cc2_plates_apply(1, snap) > 0
    rows = _by_name(await tmp_db.list_calib_plates())
    a = rows["Штатный лист A"]
    assert a["id"] == one and a["z_offset"] == 0.04 and a["is_default"] == 1
    assert a["kind"] == "textured", "тип покрытия — наш"
    assert "Z до связи с CC2 Control: -0.065" in a["note"], "прежний Z не пропал"
    star = rows["Пластина PEI из комплекта (там где звездочки)"]
    assert star["cc2_plate_id"] is None and star["is_default"] == 0 and star["z_offset"] == -0.005
    assert rows["Штатный лист B"]["cc2_plate_id"] == B and rows["Карбон"]["z_offset"] == 0.055
    assert "сторона A" in rows["Карбон"]["note"]
    assert await tmp_db.cc2_plates_apply(1, snap) == 0, "без перемен — ни строчки в базу"


async def test_apply_never_matches_by_name(tmp_db):
    """Одноимённая наша пластина — не повод связать: пластина принтера заводится рядом."""
    await tmp_db.save_calib_plate(printer_id=1, name="Карбон", z_offset=-0.1)
    await tmp_db.cc2_plates_apply(1, cc2plates.parse_plates(LIBRARY))
    rows = _by_name(await tmp_db.list_calib_plates())
    assert rows["Карбон"]["cc2_plate_id"] is None and rows["Карбон"]["z_offset"] == -0.1
    assert rows["Карбон (CC2)"]["cc2_plate_id"] == CARBON


async def test_apply_unlinks_deleted_and_respects_nothing_mounted(tmp_db):
    one, two = await _studio_plates(tmp_db)
    await tmp_db.cc2_plate_link(one, A)
    await tmp_db.cc2_plates_apply(1, cc2plates.parse_plates(LIBRARY))
    gone = {**LIBRARY, "current": "", "plates": [p for p in LIBRARY["plates"] if p["id"] != CARBON]}
    await tmp_db.set_default_calib_plate(two)          # мастер отметил несвязанную — его право
    await tmp_db.cc2_plates_apply(1, cc2plates.parse_plates(gone))
    rows = _by_name(await tmp_db.list_calib_plates())
    assert rows["Карбон"]["cc2_plate_id"] is None, "удалена на принтере — у нас запись без связи"
    assert rows["Пластина PEI из комплекта (там где звездочки)"]["is_default"] == 1
    assert rows["Штатный лист A"]["is_default"] == 0
    await tmp_db.cc2_plates_apply(1, cc2plates.parse_plates(LIBRARY))
    rows = _by_name(await tmp_db.list_calib_plates())
    assert rows["Штатный лист A"]["is_default"] == 1, "смонтированная на принтере — единственная на столе"
    assert rows["Пластина PEI из комплекта (там где звездочки)"]["is_default"] == 0


async def test_apply_leaves_another_printers_plates_alone(tmp_db):
    """У второй машины своя библиотека: снимок первой её связанные пластины не отвязывает."""
    other = await tmp_db.save_calib_plate(printer_id=2, name="Пластина второго", z_offset=0.01)
    assert await tmp_db.cc2_plate_link(other, "0123456789abcdef") == ""
    await tmp_db.cc2_plates_apply(1, cc2plates.parse_plates(LIBRARY))
    row = await tmp_db.get_calib_plate(other)
    assert row["cc2_plate_id"] == "0123456789abcdef" and row["printer_id"] == 2


async def test_apply_ignores_an_unavailable_library(tmp_db):
    await _studio_plates(tmp_db)
    assert await tmp_db.cc2_plates_apply(1, {"available": False, "plates": []}) == 0
    assert len(await tmp_db.list_calib_plates()) == 2


async def test_link_replaces_an_adopted_copy_without_passports(tmp_db):
    """Экран калибровки открыли раньше, чем пришла карта: пластину принтера сверка уже
    завела сама. Копию без паспортов связь убирает; с паспортами — отказ."""
    one, two = await _studio_plates(tmp_db)
    await tmp_db.cc2_plates_apply(1, cc2plates.parse_plates(LIBRARY))
    assert "Штатный лист A" in _by_name(await tmp_db.list_calib_plates())
    assert await tmp_db.cc2_plate_link(one, A) == ""
    rows = await tmp_db.list_calib_plates()
    assert [r["id"] for r in rows if r["cc2_plate_id"] == A] == [one]
    adopted_b = next(r["id"] for r in rows if r["cc2_plate_id"] == B)
    await db._conn().execute("INSERT INTO calib_passport(plastic_id, printer_id, nozzle, plate_id, "
                             "updated_at) VALUES(1, 1, '0.4', ?, 0)", (adopted_b,))
    why = await tmp_db.cc2_plate_link(two, B)
    assert "паспорта" in why


# ─────────────────────────── экран и ручки ───────────────────────────

@pytest.fixture
def admin(monkeypatch):
    stub_auth(monkeypatch, _verify_admin=4242)
    monkeypatch.setattr(calibration, "list_presets", lambda *a, **k: [])


async def _screen():
    return body(await calib._api_admin_calib(admin_req()))


async def _save(payload):
    return body(await calib._api_admin_calib_plate_save(admin_req(payload)))


async def test_screen_reads_the_library_at_most_every_30_s(tmp_db, svc, admin):
    one, _two = await _studio_plates(tmp_db)
    assert (await _save({"action": "cc2_link", "links": {str(one): A}}))["ok"]
    svc.log.clear()
    s = await _screen()
    await _screen()
    assert [x[1] for x in svc.log] == [], "снимок после связи свежий — к принтеру не ходим"
    a = next(p for p in s["plates"] if p["id"] == one)
    assert a["cc2"] == {"side": "A", "in_printer": True, "mounted": True, "z_applied": True}
    assert s["plates_cc2"]["ok"] is True and s["plates_cc2"]["error"] == ""
    cc2plates._hosts[HOST].fetched -= cc2plates.STALE_S + 1
    await _screen()
    assert [x[1] for x in svc.log] == ["/api/plates"]


async def test_screen_survives_a_silent_printer(tmp_db, svc, admin):
    await _studio_plates(tmp_db)
    svc.library = cc2community.CommunityError("timed out")
    s = await _screen()
    assert len(s["plates"]) == 2 and s["plates_cc2"]["ok"] is False
    assert "timed out" in s["plates_cc2"]["error"]
    await _screen()
    assert len(svc.log) == 1, "не ответил — 30 с к нему не стучимся"


async def test_printer_led_plate_refuses_mount_and_delete(tmp_db, svc, admin):
    one, two = await _studio_plates(tmp_db)
    await _save({"action": "cc2_link", "links": {str(one): A}})
    r = await _save({"action": "delete", "id": one})
    assert r["ok"] is False and "CC2 Control" in r["error"]
    r = await _save({"action": "default", "id": one})
    assert r["ok"] is False and "перезагрузить" in r["error"]
    r = await _save({"action": "default", "id": two})
    assert r["ok"] is False and "Штатный лист A" in r["error"], "на принтере своя пластина"
    assert await tmp_db.get_calib_plate(one)


async def test_edit_of_a_led_plate_goes_to_the_printer(tmp_db, svc, admin):
    one, _two = await _studio_plates(tmp_db)
    await _save({"action": "cc2_link", "links": {str(one): A}})
    r = await _save({"action": "save", "id": one, "name": 'Штатный "A"', "z_offset": "-0,02",
                     "kind": "smooth", "note": "после чистки"})
    assert r["ok"] and r["applied"] is True
    assert ("POST", "/api/plates/edit", f"{A}\nШтатный 'A'\n-0.02") in svc.log
    row = await tmp_db.get_calib_plate(one)
    assert (row["name"], row["z_offset"], row["kind"], row["note"]) == (
        "Штатный 'A'", -0.02, "smooth", "после чистки")
    assert (await _save({"action": "save", "id": one, "name": "x", "z_offset": "2"}))["ok"] is False
    assert (await _save({"action": "save", "id": one, "name": "x", "z_offset": ""}))["ok"] is False
    svc.refuse = "A plate with this name already exists"
    r = await _save({"action": "save", "id": one, "name": "Карбон", "z_offset": "0"})
    assert r == {"ok": False, "error": "Пластина с таким именем на принтере уже есть"}


async def test_plain_plates_keep_working_without_cc2(tmp_db, admin, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("без библиотеки пластин к принтеру не ходим")
    monkeypatch.setattr(cc2community, "_get", boom)
    one, two = await _studio_plates(tmp_db)
    s = await _screen()
    assert s["plates_cc2"] is None and "cc2" not in s["plates"][0]
    assert (await _save({"action": "default", "id": two}))["ok"]
    assert (await _save({"action": "delete", "id": one}))["ok"]
