"""Очередь досчёта (Э0): сборщик app/deferred.py и его стыки с живым путём мини-аппы.

Расчёт (calcservice.compute) и пул процессов замоканы — проверяем проводку и правила
контракта (docs/Перспективные разработки/vps-relay-2026-10-04.md §1):
  * задание → цена клиенту на подтверждение (uacc:<rid>:<копейки>), мастеру «досчитан»;
  * автоприём по профилю: «принимать любые» — сразу в работу; порог в % при цене 0 — нет;
  * CalcError → «Оценит мастер» + алерт; временный сбой → повтор; рестарт посреди расчёта
    раз за разом → к мастеру;
  * order_now ставит задание, живой хвост закрывает его без второго сообщения;
  * рестарт до хвоста → задание досчитывает сборщик, поздний живой хвост молчит;
  * import_relay_order идемпотентен и не пишет клиенту.
"""
import asyncio
import json
import re
import sqlite3
import time

import pytest
from conftest import FakeBot, stub_auth, stub_bot

from app import calcservice, config, cpupool, deferred, downloader, inspect3mf, pricing, webapp
from app.web import clientflow

UID = 4242
ADMIN = 999_999_999          # conftest держит ADMIN_IDS = [999_999_999]


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def bot(monkeypatch):
    b = FakeBot()
    monkeypatch.setattr(deferred, "_runner_bot", b)
    monkeypatch.setattr(config, "WEBAPP_PUBLIC_URL", None)
    return b


@pytest.fixture
def calc(monkeypatch, tmp_db):
    """compute замокан: цена — ручной формулой по граммам/минутам; inspect — без пула."""
    state = {"calls": 0, "grams": 40.0, "minutes": 90.0, "raise": None}

    async def fake_run(fn, *a, **k):
        return fn(*a, **k)

    async def fake_compute(**kw):
        state["calls"] += 1
        state["kw"] = kw
        if state["raise"] is not None:
            raise state["raise"]
        settings = await tmp_db.get_settings()
        bd = pricing.compute_price_manual(state["grams"], state["minutes"],
                                          kw["plastic"].price_per_gram * 1000.0, settings,
                                          qty=kw["qty"])
        return {"bd": bd, "settings": settings, "preview_path": None, "cached": False,
                "pdisp": f'{kw["plastic"].name} · {kw["color"]}'.rstrip(" ·"), "plates": 1,
                "dims": "10×10×10 мм", "color_grams": [], "plastic_grams": [], "scale_pct": 100,
                "scale_clamped": False, "scale_max_pct": None,
                "dfm": {"n_parts": 1}, "estimated": True}

    monkeypatch.setattr(cpupool, "run", fake_run)
    monkeypatch.setattr(calcservice, "compute", fake_compute)
    return state


async def _presets(db):
    printer_id = await db.add_printer("P", "mp.json", "pd", "fd")
    await db.set_default_printer(printer_id)
    q = await db.add_quality("Standard", 0.2, "proc")
    await db.set_default_quality(q)
    s = await db.add_strength("Normal", 15, 3)
    pid = await db.add_plastic("Elegoo PLA Matte", 1000.0, 1.24, "PLA")
    await db.update_plastic(pid, colors="Белый")
    return (await db.get_default_printer(), await db.get_plastic(pid),
            await db.get_quality(q), await db.get_strength(s))


async def _model(tmp_path, name="m.stl"):
    p = tmp_path / name
    p.write_bytes(b"solid fake\nendsolid fake\n")
    return p


async def _pc_order(db, tmp_path, *, hold=0.0, uid=UID):
    printer, plastic, quality, strength = await _presets(db)
    stl = await _model(tmp_path)
    rid = await db.add_request(
        user_id=uid, user_name="@client", model_label="Кубик", printer=printer.name,
        plastic_name=plastic.name, quantity=2, quality=quality.label, strength=strength.label,
        grams_total=0, minutes_total=0, price=0, preview_path=None, file_path=str(stl),
        quality_id=quality.id, strength_id=strength.id, unsliced=1, price_pending=1, commit=False)
    jid = await db.enqueue_deferred(
        rid, source=deferred.pc_model_source([stl], "Кубик"),
        params=deferred.params_from_choice(printer=printer, plastic=plastic, quality=quality,
                                           strength=strength, color="Белый", qty=2),
        client_req_id=f"now:{rid}", delay_sec=hold)
    return rid, jid


def _client_msgs(bot, uid=UID):
    return [(t, kb) for chat, t, kb in bot.messages if chat == uid]


def _admin_msgs(bot):
    return [t for chat, t, _ in bot.messages if chat == ADMIN]


def _cb_data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


# ─────────────────────────────── успех ───────────────────────────────

async def test_job_prices_order_and_asks_client(tmp_db, tmp_path, bot, calc):
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    assert await deferred.drain() == 1
    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price > 0 and r.price == 0
    assert r.price_pending == 0 and r.unsliced == 0 and r.grams_total > 0
    assert r.quantity == 2 and calc["kw"]["qty"] == 2 and calc["kw"]["color"] == "Белый"
    assert (await db.get_deferred(jid))["status"] == "done"
    msgs = _client_msgs(bot)
    assert len(msgs) == 1
    text, kb = msgs[0]
    assert f"#{rid}" in text and "посчитан" in text and "Подтвердите" in text
    minor = int(round(r.pending_price * 100))
    assert f"uacc:{rid}:{minor}" in _cb_data(kb) and f"udec:{rid}" in _cb_data(kb)
    assert any("досчитан" in t for t in _admin_msgs(bot))
    # повторный проход ничего не делает и ничего не шлёт
    assert await deferred.drain() == 0
    assert len(_client_msgs(bot)) == 1


async def test_auto_accept_profile_makes_price_final(tmp_db, tmp_path, bot, calc):
    db = tmp_db
    rid, _ = await _pc_order(db, tmp_path)
    await db.set_user_price_prefs(UID, "auto", 0)
    await deferred.drain()
    r = await db.get_request(rid)
    assert r.status == "in_progress" and r.price > 0 and r.pending_price is None
    (text, _kb), = _client_msgs(bot)
    assert "автоматически" in text and "в работу" in text


async def test_pct_profile_still_confirms_first_price(tmp_db, tmp_path, bot, calc):
    """Порог «принимать подорожание до N%» от нулевой цены не считается — первую цену клиент,
    не включавший «принимать любые», подтверждает всегда."""
    db = tmp_db
    rid, _ = await _pc_order(db, tmp_path)
    await db.set_user_price_prefs(UID, "pct", 50)
    await deferred.drain()
    assert (await db.get_request(rid)).status == "price_changed"


# ─────────────────────────────── ошибки ───────────────────────────────

async def test_calc_error_hands_order_to_master(tmp_db, tmp_path, bot, calc):
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    calc["raise"] = calcservice.CalcError("нечитаемая геометрия")
    await deferred.drain()
    r = await db.get_request(rid)
    assert r.price_pending == 0 and r.unsliced == 1 and r.status == "new"
    assert webapp._order_json(r)["price"] == "Оценит мастер"
    assert (await db.get_deferred(jid))["status"] == "failed"
    (text, _), = _client_msgs(bot)
    assert "мастер" in text and "нечитаемая" not in text   # сырую ошибку клиенту не показываем
    assert any("оцените вручную" in t and "нечитаемая геометрия" in t for t in _admin_msgs(bot))


async def test_missing_model_files_fail_with_honest_text(tmp_db, tmp_path, bot, calc):
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    (tmp_path / "m.stl").unlink()
    await deferred.drain()
    assert (await db.get_deferred(jid))["status"] == "failed"
    (text, _), = _client_msgs(bot)
    assert "не найдены" in text
    assert calc["calls"] == 0


async def test_transient_error_requeues_with_backoff(tmp_db, tmp_path, bot, calc):
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    calc["raise"] = RuntimeError("сеть моргнула")
    await deferred.drain()
    row = await db.get_deferred(jid)
    assert row["status"] == "queued" and row["attempts"] == 1 and "моргнула" in row["error"]
    assert (await db.get_request(rid)).price_pending == 1
    assert bot.messages == [], "временный сбой — не повод беспокоить ни клиента, ни мастера"


async def test_transient_error_after_limit_goes_to_master(tmp_db, tmp_path, bot, calc):
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    await db._conn().execute("UPDATE deferred_calc SET attempts = ? WHERE id = ?",
                             (db.deferred.MAX_ATTEMPTS - 1, jid))
    await db._conn().commit()
    calc["raise"] = RuntimeError("418")
    await deferred.drain()
    assert (await db.get_deferred(jid))["status"] == "failed"
    assert (await db.get_request(rid)).unsliced == 1
    assert len(_client_msgs(bot)) == 1


async def test_crash_loop_gives_up_without_computing(tmp_db, tmp_path, bot, calc):
    """Процесс умирал посреди расчёта (рестарт возвращает 'running' в очередь без ошибки):
    захватов больше лимита — дальше к мастеру, а не ронять ПК ещё раз."""
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    await db._conn().execute("UPDATE deferred_calc SET attempts = ? WHERE id = ?",
                             (db.deferred.MAX_ATTEMPTS, jid))
    await db._conn().commit()
    await deferred.drain()
    assert calc["calls"] == 0
    assert (await db.get_deferred(jid))["status"] == "failed"
    assert any("прерывался" in t for t in _admin_msgs(bot))


async def test_lost_race_is_silent(tmp_db, tmp_path, bot, calc, monkeypatch):
    """Пока шёл расчёт, мастер назвал цену сам: результат не применяется, никто ничего не
    получает от сборщика (дубль хуже пропуска)."""
    db = tmp_db
    rid, _ = await _pc_order(db, tmp_path)
    real = calcservice.compute

    async def compute_then_master(**kw):
        res = await real(**kw)
        await db.update_request(rid, status="price_changed", pending_price=555.0)
        return res
    monkeypatch.setattr(calcservice, "compute", compute_then_master)
    await deferred.drain()
    assert (await db.get_request(rid)).pending_price == 555.0
    assert bot.messages == []


# ─────────────────────────────── order_now и живой хвост ───────────────────────────────

async def _order_now(db, tmp_path, monkeypatch, cfbot):
    printer, plastic, quality, strength = await _presets(db)
    stl = await _model(tmp_path, "now.stl")
    token = "tok-deferred-now"
    webapp._models[token] = {"paths": [str(stl)], "label": "Слайсится", "uid": UID, "plates": 1,
                             "multicolor": False, "num_colors": 1, "calc_scale_pct": 100,
                             "urgency": "normal", "options": []}
    stub_auth(monkeypatch, _verify_user=lambda s: {"id": UID, "username": "client"})
    stub_bot(monkeypatch, cfbot)
    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 3, "color": "Белый"}
    j = json.loads((await webapp._api_order_now(_Req(body))).body.decode())
    assert j["ok"] and j["priced"] is False
    return j["order_id"], token, (printer, plastic, quality, strength)


def _last(plastic, printer, quality, strength, total=19.9):
    return {"plastic_name": f"{plastic.name} · Белый", "printer": printer.name, "qty": 3,
            "quality": quality.label, "strength": strength.label, "dims": "1×1×1 мм",
            "grams_total": 12.0, "minutes_total": 30.0, "total": total, "plastic_id": plastic.id,
            "quality_id": quality.id, "strength_id": strength.id, "color": "Белый",
            "colors_sel": [], "color_grams": [], "plastic_grams": [], "scale_pct": 100, "approx": False}


async def test_order_now_enqueues_held_job(tmp_db, tmp_path, monkeypatch, bot, calc):
    db = tmp_db
    rid, token, _ = await _order_now(db, tmp_path, monkeypatch, bot)
    r = await db.get_request(rid)
    assert r.price_pending == 1 and r.unsliced == 1
    job = await db.deferred_by_client_req(f"now:{rid}")
    assert job and job["status"] == "queued" and job["source"]["type"] == "pc_model"
    assert job["params"]["qty"] == 3 and job["params"]["plastic_id"]
    assert job["next_try_at"] > time.time() + 60, "живому расчёту дана фора"
    assert webapp._order_json(r)["price"] == "Цена считается"
    assert await deferred.drain() == 0, "придержанное задание сборщик не трогает"


async def test_live_tail_closes_job_without_double_message(tmp_db, tmp_path, monkeypatch, bot, calc):
    db = tmp_db
    rid, token, (printer, plastic, quality, strength) = await _order_now(db, tmp_path, monkeypatch, bot)
    m = webapp._models[token]
    m["last"] = _last(plastic, printer, quality, strength)
    m["pdf"] = {}
    await webapp._finalize_pending_order(rid, m, UID)
    r = await db.get_request(rid)
    assert r.price == pytest.approx(19.9) and r.price_pending == 0 and r.unsliced == 0
    assert (await db.deferred_by_client_req(f"now:{rid}"))["status"] == "cancelled"
    # рестарт после хвоста: придержка снята — но заказ уже не ждёт, сборщик молчит
    await db.requeue_running()
    assert await deferred.drain() == 0
    assert calc["calls"] == 0
    assert sum("посчитан" in t for t, _ in _client_msgs(bot)) == 1


async def test_restart_before_tail_collector_prices_order(tmp_db, tmp_path, monkeypatch, bot, calc):
    """Процесс перезапустился, пока живой слайс считал: при старте придержка снимается и
    заказ досчитывает сборщик. Поздний живой хвост (если бы дошёл) ничего не пишет."""
    db = tmp_db
    rid, token, (printer, plastic, quality, strength) = await _order_now(db, tmp_path, monkeypatch, bot)
    m = webapp._models.pop(token)              # «рестарт»: память токенов пропала
    await db.requeue_running()                 # то, что делает deferred.loop() при старте
    assert await deferred.drain() == 1
    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price > 0
    assert calc["kw"]["qty"] == 3
    asks = [t for t, _ in _client_msgs(bot) if "посчитан" in t]
    assert len(asks) == 1 and "Подтвердите" in asks[0]
    m["last"] = _last(plastic, printer, quality, strength, total=1.0)
    m["pdf"] = {}
    await webapp._finalize_pending_order(rid, m, UID)
    r2 = await db.get_request(rid)
    assert r2.pending_price == r.pending_price and r2.price == 0, "поздний хвост ничего не перетёр"
    assert sum("посчитан" in t for t, _ in _client_msgs(bot)) == 1


async def test_failed_live_calc_releases_hold(tmp_db, tmp_path, monkeypatch, bot, calc):
    """Живой расчёт от заказа отказался (слайс упал / клиент пересчитал с другими параметрами)
    → задание созревает сразу, а не через LIVE_HOLD_SEC; дальше сборщик честно отдаёт заказ
    мастеру, если модель и правда не считается (до Э0 заявка молча висела «оценит мастер»)."""
    db = tmp_db
    rid, token, (printer, plastic, quality, strength) = await _order_now(db, tmp_path, monkeypatch, bot)
    webapp._models[token]["pending_order_id"] = rid
    calc["raise"] = calcservice.CalcError("деталь не помещается на стол")
    stub_auth(monkeypatch, _verify_init_data=lambda s: UID)
    body = {"token": token, "plastic_id": plastic.id, "quality_id": quality.id,
            "strength_id": strength.id, "qty": 3, "color": "Белый"}
    j = json.loads((await webapp._api_calc_inner(_Req(body))).body.decode())
    assert j["ok"] is False and "pending_order_id" not in webapp._models[token]
    job = await db.claim_next_deferred()
    assert job and job["rid"] == rid, "придержка снята — задание созрело сразу"
    await deferred.run_one(job)
    r = await db.get_request(rid)
    assert r.unsliced == 1 and r.price_pending == 0
    assert any("оцените вручную" in t for t in _admin_msgs(bot))


# ─────────────────────────────── приём из приёмной ───────────────────────────────

_PAYLOAD = {"offer_id": "o1", "uid": UID, "chat_id": UID,
            "user": {"id": UID, "username": "night_owl", "first_name": "Ночь"},
            "src": {"type": "url", "url": "https://makerworld.com/en/models/123-cool-vase"},
            "created_at": 1790000000}


async def test_import_relay_order_is_idempotent(tmp_db, bot, calc):
    db = tmp_db
    await _presets(db)
    rid = await deferred.import_relay_order("evt0001", dict(_PAYLOAD))
    rid2 = await deferred.import_relay_order("evt0001", dict(_PAYLOAD))
    assert rid == rid2 > 0
    assert len(await db.list_user_requests(UID)) == 1
    assert len(await db.deferred_for_request(rid)) == 1
    r = await db.get_request(rid)
    assert r.price_pending == 1 and r.unsliced == 1 and r.price == 0 and r.status == "new"
    assert r.src_url == _PAYLOAD["src"]["url"] and r.user_name == "@night_owl"
    assert r.model_label == "Cool Vase" and r.plastic_name == "Elegoo PLA Matte"
    job = await db.deferred_by_client_req("rly:evt0001")
    assert job["params"] == {"quick": True}
    assert await db.relay_event_was_seen("evt0001")
    assert _client_msgs(bot) == [], "клиенту на приёме не пишем — ему придёт цена"
    admin = _admin_msgs(bot)
    assert len(admin) == 1 and "🌙" in admin[0] and f"#{rid}" in admin[0]


async def test_import_concurrent_duplicates_make_one_order(tmp_db, bot, calc):
    db = tmp_db
    await _presets(db)
    rids = await asyncio.gather(*(deferred.import_relay_order("evt-dup", dict(_PAYLOAD))
                                  for _ in range(3)))
    assert len(set(rids)) == 1
    assert len(await db.list_user_requests(UID)) == 1


async def test_import_then_collector_uses_quick_defaults(tmp_db, tmp_path, bot, calc, monkeypatch):
    """Ссылка из приёмной качается тем же путём, что быстрый расчёт, и считается его дефолтами."""
    db = tmp_db
    printer, plastic, quality, strength = await _presets(db)
    stl = await _model(tmp_path, "vase.stl")

    async def fake_download(url):
        assert url == _PAYLOAD["src"]["url"]
        return [str(stl)], "Ваза с площадки"
    monkeypatch.setattr(clientflow, "_quick_link_download", fake_download)
    rid = await deferred.import_relay_order("evt-q", dict(_PAYLOAD))
    assert await deferred.drain() == 1
    kw = calc["kw"]
    assert kw["plastic"].id == plastic.id and kw["qty"] == 1 and kw["printer"].id == printer.id
    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.model_label == "Ваза с площадки"
    assert r.file_path == str(stl)


@pytest.mark.parametrize("payload, err", [
    ({**_PAYLOAD, "uid": 0}, "uid"),
    ({**_PAYLOAD, "src": {"type": "pc_model", "paths": ["C:/Windows/x.stl"]}}, "источник"),
    ({**_PAYLOAD, "src": {"type": "url", "url": "file:///etc/passwd"}}, "http"),
    ({**_PAYLOAD, "src": {"type": "tg_file"}}, "file_id"),
])
async def test_import_rejects_bad_payload(tmp_db, bot, payload, err):
    with pytest.raises(ValueError, match=err):
        await deferred.import_relay_order("evt-bad", payload)
    assert await tmp_db.list_user_requests(UID) == []


async def test_import_rejects_bad_event_id(tmp_db, bot):
    with pytest.raises(ValueError):
        await deferred.import_relay_order("../../etc", dict(_PAYLOAD))


async def test_tg_file_too_big_fails_before_download(tmp_db, bot, calc):
    db = tmp_db
    await _presets(db)
    payload = {**_PAYLOAD, "src": {"type": "tg_file", "file_id": "BQAC", "file_unique_id": "u1",
                                    "file_name": "../../big.stl", "file_size": 25 * 1024 * 1024}}
    rid = await deferred.import_relay_order("evt-big", payload)
    assert (await db.get_request(rid)).model_label == "big"
    await deferred.drain()
    r = await db.get_request(rid)
    assert r.unsliced == 1 and r.price_pending == 0
    (text, _), = _client_msgs(bot)
    assert "20 МБ" in text


async def test_tg_file_downloaded_by_file_id(tmp_db, tmp_path, bot, calc, monkeypatch):
    db = tmp_db
    await _presets(db)
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    got = {}

    async def fake_download(file, destination=None, **kw):
        got["file"] = file
        got["dest"] = destination
        with open(destination, "wb") as f:
            f.write(b"solid x\nendsolid x\n")
    bot.download = fake_download
    payload = {**_PAYLOAD, "src": {"type": "tg_file", "file_id": "BQAC-1", "file_unique_id": "u2",
                                    "file_name": "..\\..\\part.stl", "file_size": 1000}}
    rid = await deferred.import_relay_order("evt-tg", payload)
    await deferred.drain()
    assert got["file"] == "BQAC-1"
    assert got["dest"].endswith("part.stl") and str(tmp_path / "dl") in got["dest"]
    assert (await db.get_request(rid)).status == "price_changed"


# ─────────────────────────────── сам цикл ───────────────────────────────

async def test_loop_requeues_orphans_and_wakes(tmp_db, tmp_path, bot, calc, monkeypatch):
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    await db.claim_next_deferred()                     # «сирота» прошлого процесса
    monkeypatch.setattr(deferred, "POLL_SEC", 60.0)
    task = asyncio.create_task(deferred.loop(bot))
    try:
        for _ in range(200):
            if (await db.get_deferred(jid))["status"] == "done":
                break
            await asyncio.sleep(0.01)
        assert (await db.get_deferred(jid))["status"] == "done"
        rid2, jid2 = await _pc_order(db, tmp_path, uid=UID + 1)
        deferred.wake()                                # не ждём POLL_SEC
        for _ in range(200):
            if (await db.get_deferred(jid2))["status"] == "done":
                break
            await asyncio.sleep(0.01)
        assert (await db.get_deferred(jid2))["status"] == "done"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ─────────────────────── ревью Э0 (review1): найденные дефекты ───────────────────────

class _MultiInfo:
    """Инспекция многоцветного .3mf на три стола (цвета смешиваются на столе)."""
    plates = 3
    is_multicolor = True
    num_colors = 3
    mixed_plates = True


async def _multi_setup(db, tmp_path, monkeypatch):
    """Каталог и модель под «полный» выбор клиента: многоцвет, столы 1 и 3, свой пластик
    третьего стола, масштаб, срочность, опция постобработки и промокод. Возвращает токен,
    тело /api/calc и тела «Оставить заказ» старого (до ревью) и нового фронта."""
    printer, plastic, quality, strength = await _presets(db)
    await db.update_plastic(plastic.id, colors="Белый, Красный, Синий")
    plastic = await db.get_plastic(plastic.id)
    pid2 = await db.add_plastic("Elegoo PETG", 1300.0, 1.27, "PETG")
    await db.update_plastic(pid2, colors="Чёрный, Серый")
    opt = await db.add_postproc_option("Шлифовка", 5.0)
    await db.add_promo("SALE10", 10)
    model = tmp_path / "dragon.3mf"
    model.write_bytes(b"PK-fake-3mf")
    token = "tok-e0-full"
    webapp._models[token] = {"paths": [str(model)], "label": "Дракон", "uid": UID, "plates": 3,
                             "multicolor": True, "num_colors": 3, "mixed_plates": True}
    monkeypatch.setattr(inspect3mf, "inspect_model", lambda paths: _MultiInfo())

    async def no_render(*a, **k):
        return []

    async def queue_load(*a, **k):
        return {"minutes": 0}
    monkeypatch.setattr(cpupool, "run_render", no_render)
    monkeypatch.setattr(clientflow, "_live_progress", lambda: None)
    monkeypatch.setattr(webapp.db, "queue_load", queue_load)
    stub_auth(monkeypatch, _verify_init_data=lambda s: UID,
              _verify_user=lambda s: {"id": UID, "username": "client"})
    stub_bot(monkeypatch, None)
    sel = {"printer_id": printer.id, "plastic_id": plastic.id, "quality_id": quality.id,
           "strength_id": strength.id, "qty": 2, "multicolor": True,
           "colors_sel": ["Белый", "Красный", "Синий"], "urgency": "rush"}
    calc_body = {**sel, "token": token, "color": "Белый", "scale": 120, "plates": [0, 2],
                 "plate_plastics": {"2": pid2}, "plate_colors": {"0": "Красный"},
                 "options": [opt], "promo": "sale10"}
    # «Оставить заказ» с экрана слайса: старый фронт слал только это...
    old_now = {**sel, "token": token, "color": "Белый", "comment": ""}
    # ...новый — весь выбор, с которым ушёл /api/calc
    full_now = {**old_now, "scale": 120, "plates": [0, 2], "plate_plastics": {"2": pid2},
                "plate_colors": {"0": "Красный"}, "options": [opt], "promo": "sale10"}
    return token, calc_body, {"old": old_now, "full": full_now}, pid2


_PRICE_KW = ("color", "qty", "extra_colors", "plates", "is_multicolor", "promo_pct",
             "plates_only", "scale_pct", "urgency_factor", "colors_sel")


def _kw_view(kw):
    """Аргументы calcservice.compute, от которых зависит цена, — в сравнимом виде."""
    out = {k: kw.get(k) for k in _PRICE_KW}
    out["plates_only"] = list(out["plates_only"] or []) or None
    out["colors_sel"] = list(out["colors_sel"] or [])
    out.update({k: kw[k].id for k in ("printer", "plastic", "quality", "strength")})
    out["plate_plastics"] = {int(i): p.id for i, p in (kw.get("plate_plastics") or {}).items()}
    return out


@pytest.mark.parametrize("frontend", ["old", "full"])
async def test_collector_computes_order_now_with_live_calc_choice(tmp_db, tmp_path, monkeypatch,
                                                                  bot, calc, frontend):
    """review:e0#0 — сборщик досчитывает order_now ТЕМ ЖЕ выбором, что живой /api/calc:
    многоцвет и слоты цветов, выбранные столы, пластик на стол (по id), масштаб, срочность,
    промо. Клиент жмёт «Оставить заказ» посреди блокирующего слайса; сравниваем аргументы
    compute у живого расчёта и у сборщика по заданию этого заказа."""
    db = tmp_db
    token, calc_body, now_bodies, _pid2 = await _multi_setup(db, tmp_path, monkeypatch)
    fake = calcservice.compute
    live: dict = {}

    async def live_compute(**kw):
        live.update(kw)
        j = json.loads((await webapp._api_order_now(_Req(now_bodies[frontend]))).body.decode())
        assert j["ok"] and j["priced"] is False
        live["rid"] = j["order_id"]
        return await fake(**kw)
    monkeypatch.setattr(calcservice, "compute", live_compute)
    j = json.loads((await webapp._api_calc_inner(_Req(calc_body))).body.decode())
    assert j["ok"], j
    rid = live["rid"]
    job = await db.deferred_by_client_req(f"now:{rid}")
    assert job is not None
    seen: dict = {}

    async def job_compute(**kw):
        seen.update(kw)
        return await fake(**kw)
    monkeypatch.setattr(calcservice, "compute", job_compute)
    await deferred._compute(job)
    assert _kw_view(seen) == _kw_view(live)
    assert _kw_view(seen)["extra_colors"] == 2 and _kw_view(seen)["plates_only"] == [0, 2]
    assert ([o["id"] for o in job["params"]["options"]]
            == [o["id"] for o in webapp._models[token]["options"]])


async def test_restart_mid_slice_collector_keeps_multicolor_and_plates(tmp_db, tmp_path, monkeypatch,
                                                                      bot, calc):
    """review:e0#0 — рестарт посреди слайса: сборщик считает выбранные столы и многоцвет,
    requests.multicolor не сбрасывается в 0, в заказе — число выбранных столов и метка."""
    db = tmp_db
    token, calc_body, now_bodies, pid2 = await _multi_setup(db, tmp_path, monkeypatch)
    out: dict = {}

    async def dying_compute(**kw):
        j = json.loads((await webapp._api_order_now(_Req(now_bodies["full"]))).body.decode())
        out["rid"] = j["order_id"]
        raise RuntimeError("процесс умер посреди слайса")
    real = calcservice.compute
    monkeypatch.setattr(calcservice, "compute", dying_compute)
    with pytest.raises(RuntimeError):
        await webapp._api_calc_inner(_Req(calc_body))
    monkeypatch.setattr(calcservice, "compute", real)
    rid = out["rid"]
    r0 = await db.get_request(rid)
    assert r0.multicolor == 1 and r0.plates == 2 and "стол(ы) 1,3" in r0.model_label
    webapp._models.pop(token)                  # «рестарт»: память токенов пропала
    await db.requeue_running()
    assert await deferred.drain() == 1
    kw = calc["kw"]
    assert kw["extra_colors"] == 2 and kw["is_multicolor"] is True
    assert list(kw["plates_only"]) == [0, 2]
    assert {i: p.id for i, p in kw["plate_plastics"].items()} == {2: pid2}
    assert kw["colors_sel"] == ["Белый", "Красный", "Синий"] and kw["scale_pct"] == 120
    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price > 0
    assert r.multicolor == 1 and r.plates == 2


@pytest.mark.parametrize("status", ["in_progress", "deferred", "awaiting_payment"])
async def test_live_tail_prices_order_master_took_without_price(tmp_db, tmp_path, monkeypatch,
                                                               bot, calc, status):
    """review:regress#0 — мастер увёл заказ order_now из «Новый», не дожидаясь цены (до Э0
    живой хвост всё равно писал цену): цена, граммы, время и план склада — на месте."""
    db = tmp_db
    rid, token, (printer, plastic, quality, strength) = await _order_now(db, tmp_path, monkeypatch, bot)
    await db.update_request(rid, status=status)          # как /api/admin/order/status
    m = webapp._models[token]
    m["last"] = _last(plastic, printer, quality, strength, total=19.9)
    m["pdf"] = {}
    await webapp._finalize_pending_order(rid, m, UID)
    r = await db.get_request(rid)
    assert r.status == status and r.price == pytest.approx(19.9)
    assert r.grams_total == pytest.approx(12.0) and r.minutes_total == pytest.approx(30.0)
    assert r.unsliced == 0 and r.price_pending == 0
    assert await db.get_order_stock_plan(rid), "план резерва склада записан"
    asks = [t for t, _ in _client_msgs(bot) if "посчитан" in t]
    assert len(asks) == 1 and "Мастер подтвердит" not in asks[0]


async def test_live_tail_never_overwrites_price_decided_by_master(tmp_db, tmp_path, monkeypatch,
                                                                 bot, calc):
    """review:regress#0, обратная сторона: цену уже решили (мастер прислал на подтверждение
    или поставил сам) — живой хвост не пишет ничего и молчит."""
    db = tmp_db
    rid, token, (printer, plastic, quality, strength) = await _order_now(db, tmp_path, monkeypatch, bot)
    m = webapp._models[token]
    m["last"] = _last(plastic, printer, quality, strength, total=19.9)
    m["pdf"] = {}
    await db.update_request(rid, status="price_changed", pending_price=555.0)
    await webapp._finalize_pending_order(rid, m, UID)
    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price == 555.0 and r.price == 0
    await db.update_request(rid, status="in_progress", price=300.0, pending_price=None)
    await webapp._finalize_pending_order(rid, m, UID)
    assert (await db.get_request(rid)).price == 300.0
    assert not [t for t, _ in _client_msgs(bot) if "посчитан" in t]


async def test_live_tail_crash_before_price_write_leaves_order_to_collector(tmp_db, tmp_path,
                                                                           monkeypatch, bot, calc):
    """review:e0#2 — запись цены живым хвостом упала (БД занята, процесс убит): заказ не
    должен остаться без цены и без задания. Флаг ожидания и задание — на месте, цену
    досчитывает сборщик."""
    db = tmp_db
    rid, token, (printer, plastic, quality, strength) = await _order_now(db, tmp_path, monkeypatch, bot)
    conn = db._conn()
    real_execute = conn.execute

    def failing_execute(sql, *a, **k):
        if re.match(r"\s*UPDATE requests SET price = \?", sql):
            raise sqlite3.OperationalError("database is locked")
        return real_execute(sql, *a, **k)
    monkeypatch.setattr(conn, "execute", failing_execute)
    m = webapp._models[token]
    m["last"] = _last(plastic, printer, quality, strength, total=19.9)
    m["pdf"] = {}
    await webapp._finalize_pending_order(rid, m, UID)
    monkeypatch.setattr(conn, "execute", real_execute)
    r = await db.get_request(rid)
    assert r.price == 0 and r.price_pending == 1, "заказ всё ещё ждёт цену от ПК"
    assert (await db.deferred_by_client_req(f"now:{rid}"))["status"] == "queued"
    await db.requeue_running()
    assert await deferred.drain() == 1
    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price > 0
    assert len([t for t, _ in _client_msgs(bot) if "посчитан" in t]) == 1


async def test_db_error_in_apply_requeues_job(tmp_db, tmp_path, bot, calc, monkeypatch):
    """review:e0#1 — исключение БД в apply_deferred_result не оставляет задание 'running'
    до рестарта: оно возвращается в очередь с паузой, следующий проход применяет цену,
    клиенту — ровно одно сообщение."""
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    real = db.apply_deferred_result
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real(*a, **k)
    monkeypatch.setattr(db, "apply_deferred_result", flaky)
    await deferred.drain()
    row = await db.get_deferred(jid)
    assert row["status"] == "queued" and row["next_try_at"] > time.time()
    assert "locked" in row["error"] and row["claim_token"] is None
    assert bot.messages == []
    await db._conn().execute("UPDATE deferred_calc SET next_try_at = 0 WHERE id = ?", (jid,))
    await db._conn().commit()
    assert await deferred.drain() == 1
    assert (await db.get_request(rid)).status == "price_changed"
    assert len(_client_msgs(bot)) == 1


async def test_db_error_in_retry_requeues_job(tmp_db, tmp_path, bot, calc, monkeypatch):
    """review:e0#1 — то же для retry_deferred (временный сбой расчёта + занятая БД)."""
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    calc["raise"] = RuntimeError("сеть моргнула")

    async def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "retry_deferred", broken)
    await deferred.drain()
    row = await db.get_deferred(jid)
    assert row["status"] == "queued" and row["claim_token"] is None
    assert (await db.get_request(rid)).price_pending == 1
    assert bot.messages == []


async def test_loop_recovers_job_when_release_also_fails(tmp_db, tmp_path, bot, calc, monkeypatch):
    """review:e0#1 — БД лежит и на возврате задания в очередь: цикл сам возвращает сироту
    'running' на следующем проходе, рестарт процесса не нужен."""
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    real_apply, real_release = db.apply_deferred_result, db.release_claim
    state = {"apply": 0, "release": 0}

    async def flaky_apply(*a, **k):
        state["apply"] += 1
        if state["apply"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real_apply(*a, **k)

    async def flaky_release(*a, **k):
        state["release"] += 1
        if state["release"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real_release(*a, **k)
    monkeypatch.setattr(db, "apply_deferred_result", flaky_apply)
    monkeypatch.setattr(db, "release_claim", flaky_release)
    monkeypatch.setattr(deferred, "POLL_SEC", 0.02)
    task = asyncio.create_task(deferred.loop(bot))
    try:
        for _ in range(300):
            if (await db.get_deferred(jid))["status"] == "done":
                break
            await asyncio.sleep(0.01)
        assert (await db.get_deferred(jid))["status"] == "done"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await db.get_request(rid)).status == "price_changed"
    assert len(_client_msgs(bot)) == 1


async def _edit_out(db, r, *, qty=5, label="Elegoo PETG · Чёрный"):
    """Ответ calcservice.recompute_request для «Изменить» (сам пересчёт замокан)."""
    settings = await db.get_settings()
    bd = pricing.compute_price_manual(10.0, 20.0, 1000.0, settings, qty=qty)
    return {"bd": bd, "settings": settings, "pdisp": label, "dims": "5×5×5 мм",
            "preview_path": None, "quality": await db.get_quality(r.quality_id),
            "strength": await db.get_strength(r.strength_id), "qty": qty}


def _edit_body(r, qty=5):
    return {"initData": "x", "order_id": r.id, "plastic_id": 1, "quality_id": r.quality_id,
            "strength_id": r.strength_id, "qty": qty, "color": "Чёрный"}


async def test_order_edit_does_not_overwrite_price_sent_by_collector(tmp_db, tmp_path, monkeypatch,
                                                                     bot, calc):
    """review:e0#3 — «Изменить» прочитал заказ ещё 'new', а сборщик успел прислать цену X до
    записи правки: правка не пишется поверх (иначе клиент принял бы X за НОВЫЕ параметры)."""
    db = tmp_db
    rid, _jid = await _pc_order(db, tmp_path)
    stub_auth(monkeypatch, _verify_init_data=lambda s: UID)
    stub_bot(monkeypatch, None)
    real_get = db.get_request
    stale: dict = {}

    async def get_request(rid_):
        if rid_ == rid and "row" in stale:
            return stale["row"]           # чтение, которое случилось ДО применения сборщиком
        return await real_get(rid_)

    async def recompute(r, **kw):
        row = await real_get(rid)
        assert row.status == "new"
        assert await deferred.drain() == 1, "сборщик досчитал заказ, пока шёл пересчёт"
        stale["row"] = row
        return await _edit_out(db, r)
    monkeypatch.setattr(db, "get_request", get_request)
    monkeypatch.setattr(calcservice, "recompute_request", recompute)
    r0 = await real_get(rid)
    j = json.loads((await webapp._api_order_edit(_Req(_edit_body(r0)))).body.decode())
    stale.clear()
    r = await real_get(rid)
    assert j["ok"] is False and "изменился" in j["error"]
    assert r.status == "price_changed" and r.pending_price > 0 and r.price == 0
    assert r.quantity == 2 and "PETG" not in r.plastic_name, "параметры — те, что посчитал ПК"


async def test_order_edit_wins_over_queue_and_keeps_scale(tmp_db, tmp_path, monkeypatch, bot, calc):
    """review:e0#3 — правка клиента применена одним условным UPDATE: задание отменено,
    флаг ожидания снят, масштаб из прежнего снимка сохранён; позже сборщик ничего не шлёт."""
    db = tmp_db
    rid, jid = await _pc_order(db, tmp_path)
    await db._conn().execute("UPDATE requests SET breakdown = ? WHERE id = ?",
                             (json.dumps({"scale_pct": 150}), rid))
    await db._conn().commit()
    stub_auth(monkeypatch, _verify_init_data=lambda s: UID)
    stub_bot(monkeypatch, None)

    async def recompute(r, **kw):
        return await _edit_out(db, r)
    monkeypatch.setattr(calcservice, "recompute_request", recompute)
    r0 = await db.get_request(rid)
    j = json.loads((await webapp._api_order_edit(_Req(_edit_body(r0)))).body.decode())
    assert j["ok"], j
    r = await db.get_request(rid)
    assert r.status == "new" and r.price > 0 and r.price_pending == 0 and r.quantity == 5
    assert r.plastic_name == "Elegoo PETG · Чёрный" and r.breakdown["scale_pct"] == 150
    assert (await db.get_deferred(jid))["status"] == "cancelled"
    assert await deferred.drain() == 0 and bot.messages == []
