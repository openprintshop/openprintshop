"""Катушки CC2 Control (08.10.2026): склад филамента ведёт принтер, Print Studio — кэш.

Что проверяем:
  * разбор /api/spools и сводки склада в /api/printer;
  * сверку снимка с базой: перенос журнала принтера ровно раз, первая сверка без переноса,
    невязка, архив/удаление на принтере, катушки посреди печати;
  * склад по цветам и общий склад — из катушек, пересечение порога → алерт;
  * отказ от второго списания: заказ, журнал, «печать для себя», FIFO по катушкам;
  * себестоимость заказа из журнала принтера по имени файла и времени;
  * ручки админки: приход, поправка, взвешивание, архив, связывание, вопросы лотков;
  * ритм: /api/spools только по смене ревизии или страховочному сроку.

К железу не ходим: служба CC2 Control — подменой её HTTP-слоя (cc2community._http), как и в
tests/test_cc2_community_features.py.
"""
import copy
import time

import pytest
from conftest import admin_req, body, stub_auth

from app import cc2community, cc2spools, db, notify, printerdrv, webapp

ADMIN = 4242
HOST = "192.0.2.50"
T0 = 1_790_000_000


class FakeCC2:
    """Служба CC2 Control с учётом катушек — ровно те ручки, что зовёт cc2spools, и с их правилами
    (spools.h): остаток задаётся при создании, потом — только взвешиванием/поправкой."""

    def __init__(self):
        self.spools: list[dict] = []
        self.slots = [{"slot": i, "spool": "", "last": "", "question": "", "question_since": 0,
                       "question_mm": 0.0, "runout": False, "printer": None} for i in range(5)]
        self.log: list[dict] = []            # новые первыми, как у них
        self.revision = 1
        self.enabled = True
        self.available = True
        self.job = {"active": False, "file": "", "started": 0,
                    "slots": [{"mm": 0.0, "grams": 0.0, "spool": ""} for _ in range(5)]}
        self.calls: list[tuple] = []
        self.fail: str | None = None
        self.printing = False
        self.messages = 100                  # счётчик MQTT-сообщений их службы с её старта
        self._n = 0

    def add(self, **kw) -> str:
        self._n += 1
        sid = f"{0xa000000000000000 + self._n:016x}"
        s = {"id": sid, "name": "", "brand": "", "material": "PLA", "color": "#FFFFFF",
             "diameter": 1.75, "density": 1.24, "net": 1000.0, "remaining": 1000.0, "tare": 150.0,
             "low": 0.0, "price": 0.0, "note": "", "created": T0, "used": 0, "archived": False}
        s.update(kw)
        self.spools.append(s)
        self.event("new", sid, s["remaining"])
        return sid

    def find(self, sid: str) -> dict:
        return next(s for s in self.spools if s["id"] == sid)

    def event(self, kind, spool, grams, *, slot=-1, mm=0.0, job="", result="", at=None):
        self.log.insert(0, {"time": at or T0 + len(self.log), "spool": spool, "slot": slot,
                            "kind": kind, "grams": grams, "mm": mm, "job": job, "result": result})
        self.revision += 1

    def burn(self, sid: str, grams: float, job: str, at: int, result: str = "complete"):
        """Печать закончилась: остаток уже списан, в журнале — сегмент «печать»."""
        self.find(sid)["remaining"] -= grams
        self.event("print", sid, -grams, slot=0, mm=grams * 330, job=job, result=result, at=at)

    def payload(self) -> dict:
        return {"available": self.available, "error": "", "enabled": self.enabled,
                "revision": self.revision, "canvas": True, "active_tray": -1, "slots": self.slots,
                "job": self.job, "spools": self.spools, "log": self.log}

    def http(self, host, method, path, body=None, timeout=3.0, port=8081):
        self.calls.append((method, path, body))
        if self.fail:
            raise cc2community.CommunityError(self.fail)
        if method == "GET" and path == "/api/spools":
            return copy.deepcopy(self.payload())
        if method == "GET" and path == "/api/printer":
            return {"connected": True, "messages": self.messages,
                    "machine": {"status": 2 if self.printing else 1},
                    "spools": {"enabled": self.enabled, "revision": self.revision, "questions": []}}
        assert method == "POST", path
        if path == "/api/spools/enable":
            self.enabled = body == "on"
            self.revision += 1
            return {"ok": True, "enabled": self.enabled}
        f = dict(line.split("=", 1) for line in (body or "").split("\n") if line)
        if path == "/api/spools/save":
            if f.get("id"):
                s = self.find(f["id"])
                if "remaining" in f:
                    raise cc2community.CommunityError("Correct the remaining weight with a weigh-in")
            else:
                if not f.get("material") or not f.get("color"):
                    raise cc2community.CommunityError("A spool needs a material and a colour")
                sid = self.add(remaining=float(f.get("remaining") or f.get("net") or 1000))
                s = self.find(sid)
            for k in ("name", "brand", "material", "color", "note"):
                if k in f:
                    s[k] = f[k]
            for k in ("diameter", "density", "net", "tare", "low", "price"):
                if k in f:
                    s[k] = float(f[k])
            if "archived" in f:
                s["archived"] = f["archived"] == "1"
            self.revision += 1
            return {"ok": True, "id": s["id"]}
        if path == "/api/spools/adjust":
            s = self.find(f["id"])
            if "gross" in f:
                if s["tare"] <= 0:
                    raise cc2community.CommunityError("Enter the empty spool weight first")
                value = float(f["gross"]) - s["tare"]
            else:
                value = float(f["remaining"])
            self.event("correction", s["id"], value - s["remaining"],
                       result="weighed" if "gross" in f else "set")
            s["remaining"] = value
            return {"ok": True, "remaining": value}
        if path == "/api/spools/assign":
            slot = int(f["slot"])
            for x in self.slots:
                if x["spool"] == f["spool"] and f["spool"]:
                    x["spool"] = ""
            self.slots[slot].update(spool=f["spool"], question="")
            self.revision += 1
            return {"ok": True}
        if path == "/api/spools/dismiss":
            self.slots[int(f["slot"])]["question"] = ""
            self.revision += 1
            return {"ok": True}
        raise cc2community.CommunityError("Not found")


@pytest.fixture
def cc2(monkeypatch):
    fake = FakeCC2()
    monkeypatch.setattr(cc2community, "_http", fake.http)
    src = cc2spools.Source(printer_id=1, host=HOST, name="CC2")

    async def _source():
        return src
    monkeypatch.setattr(cc2spools, "source", _source)
    fake.src = src
    return fake


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def _alert(text, *, key=None, throttle_sec=300, audience=None):
        sent.append((key, text))
    monkeypatch.setattr(notify, "alert", _alert)
    return sent


@pytest.fixture
def admin(monkeypatch):
    stub_auth(monkeypatch, _verify_admin=ADMIN)


async def _plastic(db_, name="Elegoo PLA Matte", colors="Белый, Черный, Мятный", spool_weight=150):
    pid = await db_.add_plastic(name, 41.0, 1.24, "PLA Matte")
    await db_.update_plastic(pid, colors=colors, spool_weight_g=spool_weight)
    return pid


async def _linked(db_, fake, pid, color, grams, **kw):
    """Наша катушка + её пара на принтере, связанные явно (как перенос склада 08.10)."""
    sid = await db_.add_spool(pid, color, 1000)
    if grams != 1000:
        await db_.add_stock_ledger(sid, grams - 1000, "correction")
    cid = fake.add(remaining=float(grams), **kw)
    assert await db_.cc2_link_spool(sid, cid) == ""
    return sid, cid


async def _sync(fake):
    return await db.cc2_apply_snapshot(HOST, cc2spools.parse_spools(copy.deepcopy(fake.payload())))


# ─────────────────────────────── разбор ───────────────────────────────

def test_parse_spools_keeps_good_drops_broken():
    raw = FakeCC2()
    a = raw.add(name="Elegoo PLA Matte мятный", color="#e2f2bd", remaining=962.0, tare=195.0)
    raw.spools.append({"id": "NOT-AN-ID", "remaining": 5})          # чужой мусор
    raw.log.append({"time": "вчера", "kind": "print"})                  # битое событие
    raw.slots[1].update(spool=a, printer={"status": 1, "type": "PLA", "name": "PLA Matte",
                                          "color": "#09cc3a", "brand": "ELEGOO"})
    snap = cc2spools.parse_spools(raw.payload())
    assert [s["id"] for s in snap["spools"]] == [a]
    assert snap["spools"][0]["color"] == "#E2F2BD" and snap["spools"][0]["tare"] == 195.0
    assert snap["slots"][1]["printer"]["color"] == "#09CC3A"
    assert [e["kind"] for e in snap["log"]] == ["new"]
    assert cc2spools.location(snap, a) == 1 and cc2spools.location(snap, "x") is None
    assert cc2spools.parse_spools({"spools": []}) is None              # без ревизии — не их склад
    assert cc2spools.parse_spools("Not found") is None


def test_spools_summary_rides_on_printer_poll(monkeypatch):
    """Ревизию склада приносит опрос /api/printer, который драйвер делает и так."""
    monkeypatch.setattr(cc2community, "_http", lambda *a, **k: {
        "connected": True, "last_message_age": 0, "machine": {"status": 2, "progress": 10},
        "print": {"state": "printing", "filename": "a.gcode"},
        "spools": {"enabled": True, "revision": 96, "questions": [1]}})
    cc2community.fetch_job(HOST)
    ts, summ = cc2community.spools_summary(HOST)
    assert summ == {"enabled": True, "revision": 96, "questions": [1], "printing": True,
                    "messages": -1} and time.time() - ts < 5
    assert cc2community.parse_spools_summary({"spools": None}) is None
    assert cc2community.parse_spools_summary({"spools": {"revision": True}}) is None


def test_form_numbers_and_text_fit_their_parser():
    assert cc2spools.fmt_num(1000.0) == "1000" and cc2spools.fmt_num(0.1 + 0.2) == "0.3"
    assert cc2spools.fmt_num(-0.0001) == "0" and cc2spools.fmt_num(1e-7) == "0"
    assert cc2spools.clean_text(' Катушка "с\\кавычками"\nи строкой ') == "Катушка 'с/кавычками' и строкой"
    long = "Ж" * 80                                       # 160 байт UTF-8
    cut = cc2spools.clean_text(long)
    assert len(cut.encode()) <= 96 and cut == "Ж" * 48     # режем по символу, не по байту
    assert cc2spools.form({"id": "a", "archived": True, "tare": 150.0, "x": None}) == \
        "id=a\narchived=1\ntare=150"
    with pytest.raises(cc2spools.SpoolError):
        cc2spools.form({"note": "две\nстроки"})


def test_errors_speak_russian():
    assert "тару" in cc2spools.human_error("Enter the empty spool weight first")
    assert cc2spools.human_error("Something new") == "CC2 Control: Something new"


# ─────────────────────────────── сверка ───────────────────────────────

async def test_first_sync_mirrors_nothing_and_matches_remaining(tmp_db, cc2):
    """Первая сверка: «новые катушки» в их журнале — это наш же перенос склада, его остаток уже
    сидит в базе. Переносить нечего, невязки нет — ни одной строки в журнале движений."""
    pid = await _plastic(tmp_db)
    sid, _ = await _linked(tmp_db, cc2, pid, "Мятный", 962)
    before = await tmp_db.spool_ledger(sid)
    res = await _sync(cc2)
    assert res["mirrored"] == 0 and res["reconciled"] == 0
    assert await tmp_db.spool_ledger(sid) == before
    assert (await tmp_db.get_spool(sid))["remaining_g"] == 962


async def test_print_event_mirrored_once_with_job(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_stock(pid, "Мятный", 962, 0)
    sid, cid = await _linked(tmp_db, cc2, pid, "Мятный", 962)
    await _sync(cc2)                                          # первая сверка — метка baseline
    cc2.burn(cid, 37.5, "ECC2_0.4_Дозатор магнитов 6 мм (v5.5).gcode", at=T0 + 3600)
    res = await _sync(cc2)
    assert res["mirrored"] == 1 and res["reconciled"] == 0
    assert res["jobs"] == ["ECC2_0.4_Дозатор магнитов 6 мм (v5.5).gcode"]
    led = (await tmp_db.spool_ledger(sid))[-1]
    assert led["reason"] == "print" and led["ref_type"] == "cc2" and led["delta_g"] == -37.5
    assert led["at"] == T0 + 3600
    assert (await tmp_db.get_spool(sid))["remaining_g"] == pytest.approx(924.5)
    assert (await tmp_db.get_color_stock(pid))["Мятный"]["stock"] == pytest.approx(924.5)
    again = await _sync(cc2)                                  # тот же снимок — ничего второй раз
    assert again["mirrored"] == 0 and again["reconciled"] == 0
    assert (await tmp_db.get_spool(sid))["remaining_g"] == pytest.approx(924.5)


async def test_runout_and_correction_kinds(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    sid, cid = await _linked(tmp_db, cc2, pid, "Белый", 30)
    await _sync(cc2)
    cc2.event("runout", cid, -30.0, slot=2, job="a.gcode", result="runout")
    cc2.find(cid)["remaining"] = 0.0
    await _sync(cc2)
    led = await tmp_db.spool_ledger(sid)
    assert led[-1]["reason"] == "writeoff" and led[-1]["delta_g"] == -30.0
    sp = await tmp_db.get_spool(sid)
    assert sp["remaining_g"] == 0 and sp["status"] == "empty"
    cc2.event("correction", cid, 250.0, result="weighed")      # взвесили — что-то осталось
    cc2.find(cid)["remaining"] = 250.0
    await _sync(cc2)
    sp = await tmp_db.get_spool(sid)
    assert sp["remaining_g"] == 250 and sp["status"] == "active"


async def test_missed_events_closed_by_reconcile(tmp_db, cc2):
    """Событие выпало из окна их журнала (бот долго лежал) — невязку закрывает строка сверки."""
    pid = await _plastic(tmp_db)
    sid, cid = await _linked(tmp_db, cc2, pid, "Черный", 500)
    await _sync(cc2)
    cc2.find(cid)["remaining"] = 420.0                        # журнала об этом нет
    res = await _sync(cc2)
    assert res["reconciled"] == 1
    led = (await tmp_db.spool_ledger(sid))[-1]
    assert led["reason"] == "correction" and led["note"] == "сверка с CC2 Control"
    assert (await tmp_db.get_spool(sid))["remaining_g"] == 420


async def test_printing_spool_not_reconciled_mid_print(tmp_db, cc2):
    """Посреди печати остаток катушки в лотке живой, а журнал — в конце. Сверка до конца печати
    посчитала бы расход второй раз."""
    pid = await _plastic(tmp_db)
    sid, cid = await _linked(tmp_db, cc2, pid, "Белый", 800)
    await _sync(cc2)
    cc2.slots[0]["spool"] = cid
    cc2.job.update(active=True, file="big.gcode")
    cc2.job["slots"][0] = {"mm": 3300.0, "grams": 10.0, "spool": cid}
    cc2.find(cid)["remaining"] = 790.0
    res = await _sync(cc2)
    assert res["reconciled"] == 0 and (await tmp_db.get_spool(sid))["remaining_g"] == 800
    cc2.job.update(active=False, file="")                     # конец: сегмент в журнал
    cc2.find(cid)["remaining"] = 800.0
    cc2.burn(cid, 25.0, "big.gcode", at=T0 + 7200)
    res = await _sync(cc2)
    assert res["mirrored"] == 1 and res["reconciled"] == 0
    assert (await tmp_db.get_spool(sid))["remaining_g"] == 775


async def test_archive_delete_and_attributes_follow_printer(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    a, ca = await _linked(tmp_db, cc2, pid, "Белый", 400)
    b, cb = await _linked(tmp_db, cc2, pid, "Белый", 600)
    await _sync(cc2)
    cc2.find(ca).update(archived=True, low=100.0, net=1100.0, price=19.9, note="картон")
    await _sync(cc2)
    sa = await tmp_db.get_spool(a)
    assert sa["status"] == "archived" and sa["low_threshold"] == 100 and sa["net_weight_g"] == 1100
    assert sa["cost"] == 19.9 and sa["note"] == "картон"
    assert (await tmp_db.get_color_stock(pid))["Белый"]["stock"] == 600   # архив не в остатке
    cc2.find(ca)["archived"] = False                           # вернули из архива
    await _sync(cc2)
    assert (await tmp_db.get_spool(a))["status"] == "active"
    cc2.available = False                                      # библиотека не загружена:
    cc2.spools = [s for s in cc2.spools if s["id"] != cb]      # отсутствие — не повод архивировать
    await _sync(cc2)
    assert (await tmp_db.get_spool(b))["status"] == "active"
    cc2.available = True                                       # удалена на принтере
    await _sync(cc2)
    assert (await tmp_db.get_spool(b))["status"] == "archived"


async def test_color_stock_derived_and_crossing_reported(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_stock(pid, "Белый", 700, 300)
    await tmp_db.set_color_stock(pid, "Черный", 5000, 0)       # цвет не на принтере — не трогаем
    _, c1 = await _linked(tmp_db, cc2, pid, "белый", 400)
    _, c2 = await _linked(tmp_db, cc2, pid, "Белый", 300)
    await _sync(cc2)
    cc2.burn(c1, 150.0, "x.gcode", at=T0 + 100)
    res = await _sync(cc2)
    stock = await tmp_db.get_color_stock(pid)
    assert stock["Белый"]["stock"] == 550 and stock["Черный"]["stock"] == 5000
    assert res["crossed"] == []                                # 550 > 300
    cc2.burn(c2, 260.0, "y.gcode", at=T0 + 200)
    res = await _sync(cc2)
    assert res["crossed"] == [{"plastic_id": pid, "plastic": "Elegoo PLA Matte", "color": "Белый",
                               "stock": 290.0, "low": 300.0}]
    # отрицательный остаток катушки («счёт разошёлся») не съедает соседнюю
    cc2.find(c2)["remaining"] = -40.0
    await _sync(cc2)
    assert (await tmp_db.get_color_stock(pid))["Белый"]["stock"] == 250


async def test_uncoloured_spool_drives_general_stock(tmp_db, cc2):
    """Катушка без цвета (трёхцветный Crown) — общий склад пластика тоже из неё."""
    pid = await _plastic(tmp_db, name="Crown PLA шёлковый трехцветный", colors="Зелёный-синий-красный")
    await tmp_db.update_plastic(pid, stock_grams=438, low_threshold=100)
    _, cid = await _linked(tmp_db, cc2, pid, "", 438)
    await _sync(cc2)
    cc2.burn(cid, 350.0, "crown.gcode", at=T0 + 50)
    res = await _sync(cc2)
    assert (await tmp_db.get_plastic(pid)).stock_grams == 88
    assert res["crossed"][0]["color"] == "" and res["crossed"][0]["stock"] == 88


async def test_new_colour_row_takes_palette_name(tmp_db, cc2):
    pid = await _plastic(tmp_db, colors="Темно-синий, Мятный")
    await _linked(tmp_db, cc2, pid, "темно-синий", 363)
    await _sync(cc2)
    assert (await tmp_db.get_color_stock(pid)) == {"Темно-синий": {"stock": 363.0, "low": 0.0}}


async def test_link_rules(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    a = await tmp_db.add_spool(pid, "Белый", 1000)
    b = await tmp_db.add_spool(pid, "Белый", 1000)
    cid = cc2.add()
    assert await tmp_db.cc2_link_spool(a, "XYZ") == "неверный id катушки принтера"
    assert await tmp_db.cc2_link_spool(999, cid) == "катушка не найдена"
    assert await tmp_db.cc2_link_spool(a, cid) == ""
    assert await tmp_db.cc2_link_spool(a, cid) == ""                          # повтор — не ошибка
    assert "уже связана с катушкой" in await tmp_db.cc2_link_spool(b, cid)
    assert "уже связана с другой" in await tmp_db.cc2_link_spool(a, cc2.add())
    assert (await tmp_db.get_spool(a))["cc2_id"] == cid and await tmp_db.spool_is_cc2(a)
    assert not await tmp_db.spool_is_cc2(b)


# ─────────────────────────── второго списания нет ───────────────────────────

async def test_order_printed_reserves_but_does_not_deduct_printer_colour(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_stock(pid, "Белый", 700, 0)
    await tmp_db.set_color_stock(pid, "Черный", 500, 0)        # чёрный — без катушек принтера
    sid, _ = await _linked(tmp_db, cc2, pid, "Белый", 700)
    rid = await tmp_db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                                   plastic_name="Elegoo PLA Matte", quantity=1, quality="0.2",
                                   strength="н", grams_total=100, minutes_total=30, price=10,
                                   preview_path=None)
    await tmp_db.set_order_stock_plan(rid, [{"plastic_id": pid, "name": "P", "colors": ["Белый", "Черный"],
                                             "grams": 100, "color_grams": [60, 40]}])
    assert (await tmp_db.reserved_grams_by_plastic()).get(pid) == 100     # резерв — как был
    await tmp_db.apply_order_stock(rid)
    stock = await tmp_db.get_color_stock(pid)
    assert stock["Белый"]["stock"] == 700                       # белый ведёт принтер — не тронут
    assert stock["Черный"]["stock"] == 460                      # чёрный списан по-старому
    assert (await tmp_db.get_spool(sid))["remaining_g"] == 700
    assert (await tmp_db.reserved_grams_by_plastic()).get(pid) is None    # резерв снят


async def test_journal_and_self_print_skip_printer_spools(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_stock(pid, "Белый", 700, 0)
    sid, _ = await _linked(tmp_db, cc2, pid, "Белый", 700)
    jid = await tmp_db.add_print_journal(label="ручная", plastic_id=pid, plastic_name="P",
                                         grams=55, minutes=10, spool_id=sid)
    assert (await tmp_db.get_spool(sid))["remaining_g"] == 700
    assert (await tmp_db.get_print_journal(jid))["stock_deducted"] == 1   # и по пластику не спишет
    assert await tmp_db.apply_journal_stock(jid) == []
    new, low = await tmp_db.deduct_color_stock(pid, "белый", 80)          # «печать для себя»
    assert (new, low) == (700, False)
    assert await tmp_db.deduct_from_spools(pid, "Белый", 50) == 0          # FIFO катушки принтера не берёт
    own = await tmp_db.add_spool(pid, "Белый", 300)                       # своя катушка того же цвета
    assert await tmp_db.deduct_from_spools(pid, "Белый", 50) == 50
    assert (await tmp_db.get_spool(own))["remaining_g"] == 250


async def test_general_stock_owned_by_uncoloured_printer_spool(tmp_db, cc2):
    pid = await _plastic(tmp_db, colors="")
    await tmp_db.update_plastic(pid, stock_grams=438)
    await _linked(tmp_db, cc2, pid, "", 438)
    assert await tmp_db.deduct_stock(pid, 100) == (438, False)
    jid = await tmp_db.add_print_journal(label="без заказа", plastic_id=pid, plastic_name="P",
                                         grams=60, minutes=5)
    await tmp_db.apply_journal_stock(jid)
    assert (await tmp_db.get_plastic(pid)).stock_grams == 438


async def test_plastic_without_printer_spools_deducts_as_before(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_stock(pid, "Мятный", 400, 0)
    sid = await tmp_db.add_spool(pid, "Мятный", 400)
    await tmp_db.deduct_color_stock(pid, "Мятный", 100)
    assert (await tmp_db.get_color_stock(pid))["Мятный"]["stock"] == 300
    assert (await tmp_db.get_spool(sid))["remaining_g"] == 300


# ─────────────────────────────── себестоимость ───────────────────────────────

def test_job_names_match_through_their_truncation():
    full = "ECC2_0.4_EL+Sidemount+Drybox_rev3_hex_Elegoo PLA Matte - Откалиброван_0.2_5h50m (1).gcode"
    assert db.cc2_job_same(full, full)
    assert db.cc2_job_same(full[:60], full)                    # их журнал режет имя
    assert db.cc2_job_same("/usb/x/" + full, full)            # путь носителя — не помеха
    assert not db.cc2_job_same("ECC2_0.4_a.gcode", "ECC2_0.4_b.gcode")
    assert not db.cc2_job_same("ECC2_0.4_", "ECC2_0.4_EL+Sidemount.gcode")   # короткий префикс
    assert not db.cc2_job_same("", full)


async def test_request_cogs_from_printer_log(tmp_db, cc2):
    pid = await _plastic(tmp_db)
    sid, cid = await _linked(tmp_db, cc2, pid, "Белый", 800)
    await tmp_db._conn().execute("UPDATE spools SET cost=41 WHERE id=?", (sid,))
    await tmp_db._conn().commit()
    cc2.find(cid)["price"] = 41.0
    await _sync(cc2)
    rid = await tmp_db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                                   plastic_name="Elegoo PLA Matte", quantity=1, quality="0.2",
                                   strength="н", grams_total=100, minutes_total=60, price=30,
                                   preview_path=None)
    job = "ECC2_0.4_Брелок_Elegoo PLA Matte_0.2_1h.gcode"
    finish = int(time.time())
    jid = await tmp_db.add_print_journal(label=job, plastic_id=pid, plastic_name="P", grams=0,
                                         minutes=60, request_id=rid)
    cc2.burn(cid, 30.0, job, at=finish - 1800)                # сегмент посреди печати (смена лотка)
    cc2.burn(cid, 70.0, job, at=finish + 8)                   # конец печати
    cc2.burn(cid, 50.0, job, at=finish + 86400)               # та же модель назавтра — чужая печать
    cc2.burn(cid, 20.0, "другой.gcode", at=finish)
    await _sync(cc2)
    usage = await tmp_db.cc2_request_usage(rid)
    assert sorted(-u["delta_g"] for u in usage) == [30.0, 70.0]
    await tmp_db.update_request(rid, status="printed")
    await tmp_db.apply_order_stock(rid)
    r = await tmp_db.get_request(rid)
    assert r.cogs_actual == pytest.approx(100 * 41 / 1000)
    ev = await tmp_db.get_print_event(jid)                    # карточка печати видит катушку
    assert sum(s["grams"] for s in ev["spools"] if s.get("source") == "cc2") == 100


# ─────────────────────────────── ритм опроса ───────────────────────────────

async def test_refresh_alerts_on_crossing_and_tracking_off(tmp_db, cc2, alerts):
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_stock(pid, "Белый", 400, 300)
    _, cid = await _linked(tmp_db, cc2, pid, "Белый", 400)
    await cc2spools.refresh(cc2.src)
    cc2.burn(cid, 200.0, "a.gcode", at=T0 + 10)
    await cc2spools.refresh(cc2.src)
    assert [k for k, _ in alerts] == [f"lowstock_{pid}_Белый"]
    assert "осталось 200 г" in alerts[0][1]
    cc2.enabled = False
    await cc2spools.refresh(cc2.src)
    assert alerts[-1][0] == "cc2_spools_off"


async def test_refresh_survives_printer_down(tmp_db, cc2):
    await cc2spools.refresh(cc2.src)
    snap_before = cc2spools.snapshot(HOST)[0]
    cc2.fail = "прошивка сообщества недоступна: timed out"
    assert await cc2spools.refresh(cc2.src) is snap_before    # экран получит прежний снимок
    n = len(cc2.calls)
    assert await cc2spools.refresh(cc2.src) is snap_before    # и не долбим упавший принтер
    assert len(cc2.calls) == n
    assert "недоступна" in cc2spools.snapshot(HOST)[2]


async def test_no_background_polling():
    """Фоном склад не опрашивается вовсе (решение владельца 08.10.2026): ни цикла, ни шага."""
    assert not hasattr(cc2spools, "loop") and not hasattr(cc2spools, "tick")


def _reads(fake) -> list[str]:
    return [p for _, p, _ in fake.calls]


async def test_tab_open_reads_stock_only_on_change(tmp_db, cc2):
    """Вкладку открыли: первый раз — склад; потом короткий /api/printer, и тяжёлый /api/spools —
    только если на принтере что-то поменялось. Простой без перемен — склад не читаем, сколько бы
    времени ни прошло."""
    await cc2spools.ensure_fresh(cc2.src)
    st = cc2spools._state(HOST)
    st.fetched -= 3600                                          # вкладку открыли через час
    await cc2spools.ensure_fresh(cc2.src)
    assert _reads(cc2) == ["/api/spools", "/api/printer"]       # ревизия та же — склад не читаем
    await cc2spools.ensure_fresh(cc2.src)
    assert _reads(cc2) == ["/api/spools", "/api/printer"]       # проверка ревизии — не чаще 5 с
    cc2.revision += 1                                           # поправили катушку на самом принтере
    st.rev_checked -= cc2spools.REV_CHECK_S + 1
    ts, summ = cc2community._spools_summary[HOST]
    cc2community._spools_summary[HOST] = (ts - cc2spools.REV_CHECK_S - 1, summ)
    await cc2spools.ensure_fresh(cc2.src)
    assert _reads(cc2)[-2:] == ["/api/printer", "/api/spools"]


async def test_tab_open_during_print_rereads_live_stock(tmp_db, cc2):
    """Идёт печать: остаток катушки в лотке живой и ревизию не двигает — снимок старше LIVE_S
    перечитываем, моложе — нет."""
    await cc2spools.ensure_fresh(cc2.src)
    cc2.printing = True
    st = cc2spools._state(HOST)
    st.fetched -= cc2spools.STALE_S + 1                         # свежее LIVE_S
    await cc2spools.ensure_fresh(cc2.src)
    assert _reads(cc2) == ["/api/spools", "/api/printer"]
    st.fetched -= cc2spools.LIVE_S
    st.rev_checked -= cc2spools.REV_CHECK_S + 1
    ts, summ = cc2community._spools_summary[HOST]
    cc2community._spools_summary[HOST] = (ts - cc2spools.REV_CHECK_S - 1, summ)
    await cc2spools.ensure_fresh(cc2.src)
    assert _reads(cc2)[-2:] == ["/api/printer", "/api/spools"]


async def test_tab_open_after_cc2_restart_rereads(tmp_db, cc2):
    """Служба перезапускалась: ревизия начала счёт заново и могла совпасть с нашей случайно —
    сброс её счётчика сообщений тоже повод перечитать склад."""
    await cc2spools.ensure_fresh(cc2.src)
    st = cc2spools._state(HOST)
    st.fetched -= cc2spools.STALE_S + 1
    await cc2spools.ensure_fresh(cc2.src)                       # запомнили счётчик сообщений
    cc2.messages = 3                                            # служба перезапустилась
    st.rev_checked -= cc2spools.REV_CHECK_S + 1
    ts, summ = cc2community._spools_summary[HOST]
    cc2community._spools_summary[HOST] = (ts - cc2spools.REV_CHECK_S - 1, summ)
    await cc2spools.ensure_fresh(cc2.src)
    assert _reads(cc2) == ["/api/spools", "/api/printer", "/api/printer", "/api/spools"]


async def test_after_print_takes_one_snapshot(tmp_db, cc2, monkeypatch, alerts):
    """Печать кончилась — один снимок склада (граммы журнала, склад цветов, алерты). Без
    связанных катушек и для чужого принтера — к принтеру не идём."""
    monkeypatch.setattr(cc2spools, "AUTO_SYNC", True)
    monkeypatch.setattr(cc2spools, "AFTER_PRINT_S", 0)
    await cc2spools.after_print(1)
    assert _reads(cc2) == []                                    # связанных катушек нет
    await _linked(tmp_db, cc2, await _plastic(tmp_db), "Белый", 500)
    await cc2spools.after_print(2)
    assert _reads(cc2) == []                                    # печатал другой принтер
    await cc2spools.after_print(1)
    assert _reads(cc2) == ["/api/spools"]
    monkeypatch.setattr(cc2spools, "AUTO_SYNC", False)          # тесты и выключатель — ни шагу
    await cc2spools.after_print(1)
    assert _reads(cc2) == ["/api/spools"]


async def test_source_from_discovery_cache(tmp_db):
    """Источник — принтер, у чьей службы в discovery есть spool_tracking. Без сети."""
    p = (await tmp_db.list_printers(active_only=True))[0]
    host = cc2community._host(printerdrv.for_printer(p).community_host())
    assert await cc2spools.source() is None
    cc2community._cache[host] = (time.time(), cc2community.Info(api_version=1, caps={"canvas": True}))
    assert await cc2spools.source() is None                    # старая служба без катушек
    cc2community._cache[host] = (time.time(), cc2community.Info(
        api_version=1, caps={"spool_tracking": True}))
    src = await cc2spools.source()
    assert src and src.printer_id == p.id and src.host == host


# ─────────────────────────────── ручки админки ───────────────────────────────

async def _post(payload):
    return body(await webapp._api_admin_spool(admin_req(payload)))


async def test_admin_add_creates_on_printer_and_links(tmp_db, cc2, admin):
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_shade("мятный", "#E2F2BD")
    res = await _post({"action": "add", "plastic_id": pid, "color": "Мятный", "net_weight": "1000",
                       "remaining": "962", "low": "100", "cost": "41", "supplier": "Ozon"})
    assert res["ok"], res
    _, path, sent = next(c for c in cc2.calls if c[1] == "/api/spools/save")
    f = dict(x.split("=", 1) for x in sent.split("\n"))
    assert f == {"name": "Elegoo PLA Matte Мятный", "brand": "Elegoo", "material": "PLA Matte",
                 "color": "#E2F2BD", "diameter": "1.75", "density": "1.24", "net": "1000",
                 "remaining": "962", "tare": "150", "low": "100", "price": "41", "note": ""}
    sp = await tmp_db.get_spool(res["id"])
    assert sp["cc2_id"] == res["cc2_id"] and sp["remaining_g"] == 962 and sp["supplier"] == "Ozon"
    assert sp["color"] == "мятный"


async def test_admin_add_refuses_when_printer_down(tmp_db, cc2, admin):
    pid = await _plastic(tmp_db)
    cc2.fail = "прошивка сообщества недоступна: refused"
    res = await _post({"action": "add", "plastic_id": pid, "color": "Белый", "net_weight": 1000})
    assert res["ok"] is False and "недоступна" in res["error"]
    assert await tmp_db.list_spools(pid) == []                 # тихой записи в кэш нет


async def test_admin_correction_weigh_move_status(tmp_db, cc2, admin):
    pid = await _plastic(tmp_db)
    sid, cid = await _linked(tmp_db, cc2, pid, "Белый", 500)
    await cc2spools.refresh(cc2.src)
    res = await _post({"action": "correction", "spool_id": sid, "actual": "480"})
    assert res == {"ok": True, "remaining_g": 480.0}
    res = await _post({"action": "weigh", "spool_id": sid, "gross": "600"})   # тара 150
    assert res == {"ok": True, "remaining_g": 450.0}
    res = await _post({"action": "weigh", "spool_id": sid, "gross": "500", "tare": "195"})
    assert res == {"ok": True, "remaining_g": 305.0} and cc2.find(cid)["tare"] == 195
    res = await _post({"action": "move", "spool_id": sid, "delta": "-5"})
    assert res == {"ok": True, "remaining_g": 300.0}
    assert (await _post({"action": "status", "spool_id": sid, "status": "archived"}))["ok"]
    assert cc2.find(cid)["archived"] and (await tmp_db.get_spool(sid))["status"] == "archived"
    led = [x["reason"] for x in await tmp_db.spool_ledger(sid)]
    assert led.count("correction") >= 4                        # движения пришли журналом принтера


async def test_admin_errors_translated(tmp_db, cc2, admin):
    pid = await _plastic(tmp_db)
    sid, cid = await _linked(tmp_db, cc2, pid, "Белый", 500)
    cc2.find(cid)["tare"] = 0.0
    res = await _post({"action": "weigh", "spool_id": sid, "gross": "600"})
    assert res == {"ok": False, "error": "Сначала укажите вес пустой катушки (тару)"}


async def test_admin_link_batch_adopt_push(tmp_db, cc2, admin):
    pid = await _plastic(tmp_db)
    a = await tmp_db.add_spool(pid, "Белый", 1000)
    b = await tmp_db.add_spool(pid, "Черный", 1000)
    ca, cb = cc2.add(remaining=990.0), cc2.add(remaining=1000.0)
    res = await _post({"action": "cc2_link", "links": {str(a): ca, str(b): cb, "777": "f" * 16}})
    assert res["linked"] == 2 and res["ok"] is False and "#777" in res["error"]
    assert (await tmp_db.get_spool(a))["remaining_g"] == 990      # сверка подтянула остаток
    # катушку завели прямо на принтере («какая это катушка?» → новая) — берём в учёт
    cn = cc2.add(remaining=640.0, low=50.0, note="с принтера")
    await cc2spools.refresh(cc2.src, force=True)              # прошло время — снимок свежий
    view = body(await webapp._api_admin_spools(admin_req(query={"plastic_id": str(pid)})))
    assert [u["cc2_id"] for u in view["cc2"]["unlinked"]] == [cn]
    res = await _post({"action": "cc2_adopt", "cc2_id": cn, "plastic_id": pid, "color": "Мятный"})
    sp = await tmp_db.get_spool(res["id"])
    assert sp["remaining_g"] == 640 and sp["color"] == "мятный" and sp["low_threshold"] == 50
    # своя катушка, заведённая без принтера, — на принтер
    own = await tmp_db.add_spool(pid, "Черный", 1000)
    await tmp_db.add_stock_ledger(own, -300, "print")
    res = await _post({"action": "cc2_push", "spool_id": own})
    assert res["ok"], res
    assert cc2.find(res["cc2_id"])["remaining"] == 700 and (await tmp_db.get_spool(own))["cc2_id"]


async def test_admin_slot_questions_and_enable(tmp_db, cc2, admin):
    pid = await _plastic(tmp_db)
    _, cid = await _linked(tmp_db, cc2, pid, "Белый", 500)
    cc2.slots[2].update(question="inserted", question_mm=12.0,
                        printer={"status": 1, "type": "PLA", "name": "PLA", "color": "#FFFFFF",
                                 "brand": "ELEGOO"})
    view = body(await webapp._api_admin_spools(admin_req(query={"plastic_id": str(pid)})))
    q = view["cc2"]["questions"]
    assert len(q) == 1 and q[0]["slot"] == 2 and q[0]["name"] == "лоток 3"
    assert (await _post({"action": "cc2_assign", "slot": 2, "cc2_id": cid}))["ok"]
    assert cc2.slots[2]["spool"] == cid and cc2.slots[2]["question"] == ""
    view = body(await webapp._api_admin_spools(admin_req(query={"plastic_id": str(pid)})))
    row = view["spools"][0]
    assert row["slot"] == 2 and row["live_g"] == 500 and row["tare"] == 150
    assert (await _post({"action": "cc2_dismiss", "slot": 9}))["ok"] is False
    assert (await _post({"action": "cc2_enable", "on": False}))["enabled"] is False
    assert cc2.enabled is False


async def test_admin_without_printer_keeps_old_paths(tmp_db, admin, monkeypatch):
    """Стоковый принтер (нет spool_tracking): ручки — ровно как раньше, плюс взвешивание."""
    pid = await _plastic(tmp_db)
    res = await _post({"action": "add", "plastic_id": pid, "color": "Белый", "net_weight": 1000})
    assert res["ok"] and not (await tmp_db.get_spool(res["id"]))["cc2_id"]
    res2 = await _post({"action": "weigh", "spool_id": res["id"], "gross": "650"})   # тара 150
    assert res2 == {"ok": True, "remaining_g": 500.0}
    assert (await _post({"action": "cc2_refresh"}))["ok"] is False
    view = body(await webapp._api_admin_spools(admin_req(query={"plastic_id": str(pid)})))
    assert view["cc2"] is None and view["spools"][0]["remaining_g"] == 500


async def test_admin_colour_stock_of_printer_colour_keeps_derived(tmp_db, cc2, admin):
    pid = await _plastic(tmp_db)
    await _linked(tmp_db, cc2, pid, "Белый", 450)
    await _sync(cc2)
    res = body(await webapp._api_admin_plastic(admin_req({"action": "color_stock", "id": pid,
                                                          "color": "Белый", "stock": "9999",
                                                          "low": "200"})))
    assert res == {"ok": True, "cc2": True}
    assert (await tmp_db.get_color_stock(pid))["Белый"] == {"stock": 450.0, "low": 200.0}
    plist = body(await webapp._api_admin_plastics(admin_req(query={})))
    me = next(p for p in plist["plastics"] if p["id"] == pid)
    assert me["cc2_colors"] == ["Белый"] and me["cc2_stock"] is False


# ─────────────────────── шаг 3: граммы журнала и отчёты прошивки ───────────────────────

async def test_journal_grams_from_printer_log(tmp_db, cc2, alerts):
    """Файлы Orca журнал печатей получал с 0 г («estimate_missing»). Журнал принтера знает,
    сколько реально ушло с каких катушек — граммы и пластик берутся оттуда, и такую печать
    журнал уже не списывает по пластику (принтер списал её сам)."""
    pid = await _plastic(tmp_db)
    _, cid = await _linked(tmp_db, cc2, pid, "Белый", 800)
    await cc2spools.refresh(cc2.src)                          # первая сверка
    job = "ECC2_0.4_EL+Sidemount+Drybox_rev3_hex_Elegoo PLA Matte - Откалиброван_0.2_5h50m (1).gcode"
    jid = await tmp_db.add_print_journal(label=job, plastic_id=None, plastic_name="", grams=0,
                                         minutes=350, source_of_grams="estimate_missing", graded=0)
    measured = await tmp_db.add_print_journal(label=job, plastic_id=None, plastic_name="",
                                              grams=12, minutes=350, source_of_grams="telemetry")
    now = int(time.time())
    cc2.burn(cid, 40.0, job, at=now - 3600)                   # смена лотка посреди печати
    cc2.burn(cid, 63.5, job, at=now + 8)                      # финиш
    await cc2spools.refresh(cc2.src, force=True)
    row = await tmp_db.get_print_journal(jid)
    assert row["grams"] == 103.5 and row["source_of_grams"] == "cc2_measured"
    assert row["plastic_id"] == pid and row["plastic_name"] == "Elegoo PLA Matte"
    assert row["material_type"] == "PLA Matte"
    assert (await tmp_db.get_print_journal(measured))["grams"] == 12    # телеметрию не трогаем
    assert await tmp_db.cc2_fill_journal([job]) == []                   # повтор — без записи
    assert await tmp_db.apply_journal_stock(jid) == []
    assert (await tmp_db.get_print_journal(jid))["stock_deducted"] == 1


async def test_journal_row_written_after_sync_still_filled(tmp_db, cc2, alerts):
    """printwatch и сверка не синхронны: запись журнала может появиться ПОСЛЕ того, как расход
    уже пришёл. Ближайшая сверка всё равно её дозаполнит."""
    pid = await _plastic(tmp_db)
    _, cid = await _linked(tmp_db, cc2, pid, "Белый", 800)
    await cc2spools.refresh(cc2.src)
    now = int(time.time())
    cc2.burn(cid, 20.0, "ECC2_0.4_Брелок_Elegoo PLA Matte_0.2_1h.gcode", at=now)
    await cc2spools.refresh(cc2.src, force=True)
    jid = await tmp_db.add_print_journal(label="ECC2_0.4_Брелок_Elegoo PLA Matte_0.2_1h.gcode",
                                         plastic_id=None, plastic_name="", grams=0, minutes=60,
                                         source_of_grams="estimate_missing")
    await cc2spools.refresh(cc2.src, force=True)              # ничего нового — но запись свежая
    assert (await tmp_db.get_print_journal(jid))["grams"] == 20


def test_printer_report_parsed():
    obj = {"printer_report": {"event_id": "17.5-3", "sequence": 3, "code": 2201, "level": 2,
                              "message": " Spaghetti detected ", "age": 4}}
    assert cc2community.parse_report(obj) == {"event_id": "17.5-3", "code": 2201, "level": 2,
                                              "message": "Spaghetti detected", "age": 4}
    assert cc2community.parse_report({"printer_report": None}) is None
    assert cc2community.parse_report({"printer_report": {"event_id": ""}}) is None


class _CommunityDrv:
    kind = "elegoo"

    def __init__(self, pid=1, name="Elegoo Centauri Carbon 2", community=True):
        self.printer = type("P", (), {"id": pid, "name": name})()
        self._com = community

    def community_host(self):
        return HOST

    def community(self):
        return {"caps": {"spool_tracking": True}} if self._com else None


async def test_printer_report_alerts_once_per_event(alerts):
    from app import printwatch
    drv = _CommunityDrv()
    rep = {"event_id": "17.5-3", "sequence": 3, "code": 2201, "level": 2,
           "message": "Spaghetti detected", "age": 3}
    cc2community.remember_report(HOST, {"printer_report": rep})
    await printwatch._tick_cc2_report(drv)
    await printwatch._tick_cc2_report(drv)                    # то же событие — молчим
    assert len(alerts) == 1 and "Spaghetti detected" in alerts[0][1] and "критично" in alerts[0][1]
    assert alerts[0][1].startswith("Elegoo Centauri Carbon 2: прошивка сообщает")
    cc2community.remember_report(HOST, {"printer_report": {**rep, "event_id": "17.5-4", "level": 3}})
    await printwatch._tick_cc2_report(drv)                    # RESUME — не тревога
    cc2community.remember_report(HOST, {"printer_report": {**rep, "event_id": "9.1-1", "age": 4000}})
    await printwatch._tick_cc2_report(drv)                    # давнее — до старта бота
    cc2community.remember_report(HOST, {"printer_report": {**rep, "event_id": "17.5-5", "level": 1}})
    await printwatch._tick_cc2_report(_CommunityDrv(community=False))   # стоковый — не наш
    assert len(alerts) == 1
    await printwatch._tick_cc2_report(drv)
    assert len(alerts) == 2 and "предупреждение" in alerts[1][1]


async def test_two_screens_at_once_read_stock_once(tmp_db, cc2, monkeypatch):
    """Два экрана пришли разом: второй, дождавшись замка, берёт только что снятый снимок."""
    import asyncio
    fast = cc2.http

    def slow(*a, **k):                                        # принтер думает 0,3 с
        time.sleep(0.3)
        return fast(*a, **k)
    monkeypatch.setattr(cc2community, "_http", slow)
    first = asyncio.create_task(cc2spools.refresh(cc2.src))
    await asyncio.sleep(0.05)                                 # первый уже читает склад
    second = await cc2spools.refresh(cc2.src)
    assert second is await first
    assert sum(1 for _, p, _ in cc2.calls if p == "/api/spools") == 1


async def test_quiet_sync_writes_nothing(tmp_db, cc2):
    """Сверка раз в минуту без перемен на принтере не пишет в базу ни строчки: страницы базы
    раз в минуту уезжают копией на VPS, и холостая сверка гоняла бы весь склад."""
    pid = await _plastic(tmp_db)
    await tmp_db.set_color_stock(pid, "Белый", 900, 100)
    await _linked(tmp_db, cc2, pid, "Белый", 500, low=100.0)
    await _linked(tmp_db, cc2, pid, "Белый", 400)
    await _sync(cc2)
    await _sync(cc2)                                          # второй раз атрибуты уже совпали
    before = tmp_db._conn().total_changes
    res = await _sync(cc2)
    assert res["mirrored"] == res["reconciled"] == 0
    assert tmp_db._conn().total_changes == before


async def test_printer_spool_not_edited_locally_when_printer_gone(tmp_db, admin, monkeypatch):
    """Принтер выпал из автоопределения (перезагрузка): правка его катушки своим учётом была бы
    затёрта ближайшей сверкой — честный отказ вместо тихой потери."""
    pid = await _plastic(tmp_db)
    sid = await tmp_db.add_spool(pid, "Белый", 500)
    assert await tmp_db.cc2_link_spool(sid, "a" * 16) == ""
    res = await _post({"action": "correction", "spool_id": sid, "actual": "300"})
    assert res["ok"] is False and "не на связи" in res["error"]
    assert (await tmp_db.get_spool(sid))["remaining_g"] == 500
