"""Очередь досчёта (Э0): контракт store-слоя app/store/deferred.py.

Docs: docs/Перспективные разработки/vps-relay-2026-10-04.md §1. Здесь — правила-блокеры
контракта, проверенные прямо на БД, без сборщика:
  * enqueue идемпотентен по client_req_id и ставит заказу price_pending=1;
  * захват отменяет задание, если заказ больше не ждёт расчёта (мастер назвал цену, клиент
    отменил, заказ удалён);
  * старт процесса возвращает 'running' в очередь и снимает придержки живого расчёта;
  * применение — одно и условное: второй вызов (и вызов с чужим токеном) возвращает False;
  * отказ уводит заказ к мастеру, временный сбой — назад в очередь с экспонентой и лимитом;
  * дедуп приёмной: tg_seen_mark / relay_event_seen_mark.
"""
import time

import pytest

from app import db as dbmod
from app.store import deferred as store_deferred


async def _order(db, uid=500, **kw):
    fields = dict(user_id=uid, user_name="u", model_label="Модель", printer="P",
                  plastic_name="PLA", quantity=1, quality="Q", strength="S", grams_total=0,
                  minutes_total=0, price=0, preview_path=None, unsliced=1)
    fields.update(kw)
    return await db.add_request(**fields)


async def _queued(db, rid, crid=None, **kw):
    return await db.enqueue_deferred(rid, source={"type": "url", "url": "https://e.x/m"},
                                     params={"quick": True}, client_req_id=crid, **kw)


async def _job_row(db, jid):
    return await db.get_deferred(jid)


# ─────────────────────────────── постановка ───────────────────────────────

async def test_enqueue_is_idempotent_and_marks_order(tmp_db):
    db = tmp_db
    rid = await _order(db)
    j1 = await _queued(db, rid, "rly:evt-1")
    j2 = await _queued(db, rid, "rly:evt-1")
    assert j1 == j2, "повтор с тем же ключом обязан вернуть то же задание"
    assert len(await db.deferred_for_request(rid)) == 1
    r = await db.get_request(rid)
    assert r.price_pending == 1 and r.status == "new"
    job = await db.get_deferred(j1)
    assert job["status"] == "queued" and job["attempts"] == 0
    assert job["source"] == {"type": "url", "url": "https://e.x/m"}   # JSON разобран
    assert (await db.deferred_by_client_req("rly:evt-1"))["id"] == j1


async def test_enqueue_does_not_flag_order_that_left_new(tmp_db):
    """Отменённый/взятый мастером заказ «ждать ПК» не может — флаг не ставится."""
    db = tmp_db
    rid = await _order(db, status="cancelled")
    await _queued(db, rid, "now:1")
    assert (await db.get_request(rid)).price_pending == 0


async def test_add_request_with_commit_false_lands_with_the_job(tmp_db):
    """Заказ (commit=False) и задание — один commit: enqueue коммитит и строку заказа."""
    db = tmp_db
    rid = await db.add_request(user_id=1, user_name="u", model_label="m", printer="", plastic_name="",
                               quantity=1, quality="", strength="", grams_total=0, minutes_total=0,
                               price=0, preview_path=None, unsliced=1, price_pending=1, commit=False)
    await _queued(db, rid, "now:x")
    cur = await db._conn().execute("SELECT price_pending FROM requests WHERE id = ?", (rid,))
    assert (await cur.fetchone())[0] == 1
    assert not db._conn().in_transaction, "после enqueue не должно остаться незакоммиченного"


# ─────────────────────────────── захват ───────────────────────────────

async def test_claim_takes_due_job_once(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    job = await db.claim_next_deferred()
    assert job and job["id"] == jid and job["status"] == "running" and job["attempts"] == 1
    assert job["claim_token"]
    assert await db.claim_deferred(jid) is None, "второй захват того же задания невозможен"
    assert await db.claim_next_deferred() is None


async def test_claim_skips_held_job(tmp_db):
    """Придержанное под живой расчёт задание (order_now) не созревает раньше срока."""
    db = tmp_db
    rid = await _order(db)
    await _queued(db, rid, delay_sec=600)
    assert await db.claim_next_deferred() is None
    assert await db.claim_next_deferred(now=time.time() + 601) is not None


@pytest.mark.parametrize("how", ["master_price", "client_cancel", "raw_status", "deleted"])
async def test_claim_cancels_when_order_no_longer_waits(tmp_db, how):
    """Пока задание ждало ПК, заказ ушёл: мастер назвал цену, клиент отменил, заказ удалили.
    Задание отменяется, а не считается впустую (и уж тем более не перетирает цену)."""
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    if how == "master_price":
        await db.update_request(rid, status="price_changed", pending_price=99.0)
    elif how == "client_cancel":
        await db.update_request(rid, status="cancelled", pending_price=None)
    elif how == "raw_status":
        # мимо update_request: проверяем сам захват, а не страховку в update_request
        await db._conn().execute("UPDATE requests SET status='in_progress' WHERE id=?", (rid,))
        await db._conn().commit()
    else:
        await db.delete_request(rid)
    assert await db.claim_next_deferred() is None
    job = await db.get_deferred(jid)
    assert job["status"] == "cancelled" and job["error"]
    if how == "deleted":
        assert "удалён" in job["error"]


async def test_leaving_new_cancels_job_and_clears_flag(tmp_db):
    """Уход заказа из «Новый» снимает ожидание тем же commit-ом: верни мастер заказ в «Новый»
    позже — старое задание не оживёт и цену мастера не перепишет."""
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    await db.update_request(rid, status="in_progress")
    assert (await db.get_request(rid)).price_pending == 0
    assert (await db.get_deferred(jid))["status"] == "cancelled"
    await db.update_request(rid, status="new")
    assert await db.claim_next_deferred() is None


# ─────────────────────────────── старт процесса ───────────────────────────────

async def test_requeue_running_on_start(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    job = await db.claim_next_deferred()
    assert job["status"] == "running"
    assert await db.requeue_running() == 1
    row = await db.get_deferred(jid)
    assert row["status"] == "queued" and row["claim_token"] is None and row["attempts"] == 1
    # старый токен больше ничего не применит
    assert await db.apply_deferred_result(jid, job["claim_token"], {"price": 10.0}) is False
    assert (await db.claim_next_deferred())["id"] == jid


async def test_requeue_releases_live_holds_but_not_backoff(tmp_db):
    """Придержка order_now (ни разу не захватывалось) снимается при старте — живого расчёта
    больше нет. Пауза после сбоя (attempts>0) остаётся: сеть от рестарта не починилась."""
    db = tmp_db
    held = await _queued(db, await _order(db), "now:held", delay_sec=900)
    backoff = await _queued(db, await _order(db), "now:backoff")
    job = await db.claim_deferred(backoff)
    assert await db.retry_deferred(backoff, job["claim_token"], "сеть")
    await db.requeue_running()
    now = time.time()
    assert (await db.get_deferred(held))["next_try_at"] <= now
    assert (await db.get_deferred(backoff))["next_try_at"] > now


async def test_release_hold_for_request(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid, "now:r", delay_sec=900)
    assert await db.release_deferred_hold(rid) == 1
    assert (await db.claim_next_deferred())["id"] == jid


# ─────────────────────────────── применение ───────────────────────────────

_RESULT = {"price": 123.456, "grams_total": 40.0, "minutes_total": 90.0,
           "breakdown": {"total": 123.46, "scale_pct": 100}, "plastic_name": "PLA · Белый",
           "quality": "Стандарт", "strength": "Норма", "quantity": 2, "dims": "10×10×10 мм",
           "hash": "abc", "plates": 2, "multicolor": 0, "model_label": "Настоящее имя",
           "stock_plan": [{"plastic_id": 1, "grams": 40.0}]}


async def test_apply_once_then_false(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    job = await db.claim_next_deferred()
    assert await db.apply_deferred_result(jid, job["claim_token"], dict(_RESULT)) is True
    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price == pytest.approx(123.46)
    assert r.price == 0, "цена до подтверждения не боевая — клиент её ещё не принял"
    assert r.price_pending == 0 and r.unsliced == 0
    assert r.grams_total == 40.0 and r.quantity == 2 and r.plates == 2
    assert r.model_label == "Настоящее имя" and r.breakdown["scale_pct"] == 100
    assert await db.get_order_stock_plan(rid) == [{"plastic_id": 1, "grams": 40.0}]
    hist = [s for s, _ in await db.get_status_history(rid)]
    assert hist[-1] == "price_changed"
    assert (await db.get_deferred(jid))["status"] == "done"
    # повтор (двойной вызов, рестарт посреди уведомлений) — False, ничего не трогает
    assert await db.apply_deferred_result(jid, job["claim_token"], dict(_RESULT, price=1.0)) is False
    assert (await db.get_request(rid)).pending_price == pytest.approx(123.46)


async def test_apply_auto_accept_makes_price_final(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    job = await db.claim_next_deferred()
    assert await db.apply_deferred_result(jid, job["claim_token"], {"price": 50.0}, auto_accept=True)
    r = await db.get_request(rid)
    assert r.status == "in_progress" and r.price == 50.0 and r.pending_price is None


async def test_apply_refuses_when_master_took_the_order_meanwhile(tmp_db):
    """Расчёт шёл, а мастер тем временем назвал цену сам: результат не применяется, цена
    мастера цела, задание отменено, вызывающий получает False (никому ничего не шлёт)."""
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    job = await db.claim_next_deferred()
    await db.update_request(rid, status="price_changed", pending_price=777.0)
    assert await db.apply_deferred_result(jid, job["claim_token"], dict(_RESULT)) is False
    r = await db.get_request(rid)
    assert r.pending_price == 777.0 and r.model_label == "Модель"
    assert (await db.get_deferred(jid))["status"] == "cancelled"


async def test_apply_with_foreign_token_is_refused(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    await db.claim_next_deferred()
    assert await db.apply_deferred_result(jid, "чужой-токен", dict(_RESULT)) is False
    assert (await db.get_request(rid)).status == "new"
    assert (await db.get_deferred(jid))["status"] == "running"


# ─────────────────────────────── отказ и повтор ───────────────────────────────

async def test_fail_sends_order_to_master(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    job = await db.claim_next_deferred()
    assert await db.fail_deferred(jid, job["claim_token"], "модель битая") is True
    r = await db.get_request(rid)
    assert r.price_pending == 0 and r.unsliced == 1 and r.status == "new"
    row = await db.get_deferred(jid)
    assert row["status"] == "failed" and row["error"] == "модель битая"
    assert await db.fail_deferred(jid, job["claim_token"], "ещё раз") is False


async def test_fail_is_silent_if_order_already_left(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    job = await db.claim_next_deferred()
    await db._conn().execute("UPDATE requests SET status='price_changed', price_pending=0 WHERE id=?", (rid,))
    await db._conn().commit()
    assert await db.fail_deferred(jid, job["claim_token"], "x") is False


async def test_retry_backoff_is_exponential_and_limited(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    t = time.time() + 10      # «сейчас» теста — после постановки задания
    waits = []
    for attempt in range(1, store_deferred.MAX_ATTEMPTS + 1):
        job = await db.claim_deferred(jid, now=t)
        assert job is not None and job["attempts"] == attempt
        ok = await db.retry_deferred(jid, job["claim_token"], "сеть", now=t)
        if attempt < store_deferred.MAX_ATTEMPTS:
            assert ok is True
            row = await db.get_deferred(jid)
            assert row["status"] == "queued" and row["error"] == "сеть"
            waits.append(row["next_try_at"] - t)
            t = row["next_try_at"]
        else:
            assert ok is False, "после MAX_ATTEMPTS повтора нет — заказ уходит мастеру"
    assert waits == [min(30 * 2 ** n, 3600) for n in range(1, store_deferred.MAX_ATTEMPTS)]
    assert store_deferred.backoff_sec(20) == 3600


# ─────────────────────────────── живой хвост и отмена ───────────────────────────────

_LIVE = {"price": 19.9, "grams_total": 12.0, "minutes_total": 30.0, "plastic_name": "PLA · Белый",
         "breakdown": {"scale_pct": 100}, "stock_plan": [{"plastic_id": 1, "grams": 12.0}]}


async def test_apply_live_price_once_and_cancels_job(tmp_db):
    """Живой хвост: цена, снимок, план склада и отмена задания — один commit; второй раз — нет."""
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid, "now:t")
    assert await db.apply_live_price(rid, dict(_LIVE)) is True
    assert not db._conn().in_transaction
    r = await db.get_request(rid)
    assert r.price == 19.9 and r.grams_total == 12.0 and r.price_pending == 0 and r.unsliced == 0
    assert r.breakdown == {"scale_pct": 100} and await db.get_order_stock_plan(rid)
    assert (await db.get_deferred(jid))["status"] == "cancelled"
    assert await db.apply_live_price(rid, {**_LIVE, "price": 1.0}) is False
    assert (await db.get_request(rid)).price == 19.9


@pytest.mark.parametrize("status", ["in_progress", "deferred", "awaiting_payment"])
async def test_apply_live_price_when_master_took_order_without_price(tmp_db, status):
    """review:regress#0 — мастер увёл заказ из «Новый» без цены: живой хвост цену пишет."""
    db = tmp_db
    rid = await _order(db)
    await _queued(db, rid, "now:s")
    await db.update_request(rid, status=status)
    assert await db.apply_live_price(rid, dict(_LIVE)) is True
    r = await db.get_request(rid)
    assert r.status == status and r.price == 19.9 and r.unsliced == 0


@pytest.mark.parametrize("fields", [
    {"status": "price_changed", "pending_price": 50.0},     # мастер прислал цену на подтверждение
    {"status": "invoiced", "pending_price": 50.0},
    {"status": "in_progress", "price": 300.0},              # мастер поставил цену сам
    {"status": "cancelled"}, {"status": "rejected"}, {"status": "printed"},
    {"unsliced": 0},                                        # мастер пересчитал по слайсеру
    {"price": 42.0},                                        # клиент пересчитал в «Изменить»
])
async def test_apply_live_price_never_overrides_decided_price(tmp_db, fields):
    db = tmp_db
    rid = await _order(db)
    await _queued(db, rid, "now:d")
    await db.update_request(rid, **fields)
    before = await db.get_request(rid)
    assert await db.apply_live_price(rid, dict(_LIVE)) is False
    after = await db.get_request(rid)
    assert (after.price, after.pending_price, after.status) == (before.price, before.pending_price,
                                                                before.status)
    assert not db._conn().in_transaction


async def test_release_claim_requeues_with_backoff(tmp_db):
    """review:e0#1 — сбой исполнения вне классификации: задание назад в очередь с паузой,
    чужой токен — не трогаем."""
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    t = time.time()
    job = await db.claim_deferred(jid, now=t)
    assert await db.release_claim(jid, "чужой", "x", now=t) is False
    assert await db.release_claim(jid, job["claim_token"], "database is locked", now=t) is True
    row = await db.get_deferred(jid)
    assert row["status"] == "queued" and row["claim_token"] is None
    assert row["next_try_at"] == pytest.approx(t + store_deferred.backoff_sec(1))
    assert row["error"] == "database is locked" and row["attempts"] == 1
    assert await db.release_claim(jid, job["claim_token"], "again", now=t) is False


async def test_apply_client_edit_is_conditional(tmp_db):
    """review:e0#3 — правка клиента пишется только в «Новый» заказ, одним commit-ом с отменой
    задания; масштаб из прежнего снимка переносится."""
    db = tmp_db
    rid = await _order(db, breakdown={"scale_pct": 150})
    jid = await _queued(db, rid, "now:e")
    assert await db.apply_client_edit(rid, price=33.0, quantity=3, plastic_name="PETG",
                                      breakdown={"total": 33.0}, status="in_progress") is True
    r = await db.get_request(rid)
    assert r.price == 33.0 and r.quantity == 3 and r.status == "new", "статус правкой не меняется"
    assert r.price_pending == 0 and r.breakdown == {"total": 33.0, "scale_pct": 150}
    assert (await db.get_deferred(jid))["status"] == "cancelled"
    await db.update_request(rid, status="price_changed", pending_price=40.0)
    assert await db.apply_client_edit(rid, price=1.0, quantity=9) is False
    r = await db.get_request(rid)
    assert r.price == 33.0 and r.quantity == 3 and r.pending_price == 40.0
    assert not db._conn().in_transaction


async def test_cancel_for_request(tmp_db):
    db = tmp_db
    rid = await _order(db)
    jid = await _queued(db, rid)
    assert await db.cancel_deferred_for_request(rid, "клиент пересчитал сам") == 1
    row = await db.get_deferred(jid)
    assert row["status"] == "cancelled" and "пересчитал" in row["error"]
    assert (await db.get_request(rid)).price_pending == 0


async def test_counts(tmp_db):
    db = tmp_db
    await _queued(db, await _order(db))
    await _queued(db, await _order(db), delay_sec=900)
    c = await db.deferred_counts()
    assert c["queued"] == 2 and c["pending_orders"] == 2 and c["oldest_due_age"] >= 0


# ─────────────────────────────── дедуп приёмной ───────────────────────────────

async def test_tg_seen_mark_and_prune(tmp_db):
    db = tmp_db
    assert await db.tg_seen_mark(1001) is True
    assert await db.tg_seen_mark(1001) is False
    assert await db.tg_seen_mark(1002) is True
    await db._conn().execute("UPDATE tg_seen SET at = 0 WHERE update_id = 1001")
    await db._conn().commit()
    assert await db.prune_tg_seen() == 1
    assert await db.tg_seen_mark(1002) is False


async def test_relay_event_seen_mark(tmp_db):
    db = tmp_db
    assert await db.relay_event_was_seen("e1") is False
    assert await db.relay_event_seen_mark("e1") is True
    assert await db.relay_event_seen_mark("e1") is False
    assert await db.relay_event_was_seen("e1") is True
    assert await db.relay_event_seen_mark("") is False


def test_facade_reexports_store_module():
    """Фасад db отдаёт ТЕ ЖЕ объекты, что модуль store (правило фасада)."""
    for name in ("enqueue_deferred", "claim_deferred", "claim_next_deferred", "requeue_running",
                 "apply_deferred_result", "fail_deferred", "retry_deferred",
                 "cancel_deferred_for_request", "apply_live_price", "release_claim",
                 "release_deferred_hold",
                 "deferred_counts", "tg_seen_mark", "prune_tg_seen", "relay_event_seen_mark",
                 "relay_event_was_seen"):
        assert getattr(dbmod, name) is getattr(store_deferred, name), name
    assert dbmod.deferred is store_deferred
    from app.store import orders as store_orders
    assert dbmod.apply_client_edit is store_orders.apply_client_edit
    assert not hasattr(dbmod, "take_pending_price"), "захват без записи цены — путь review:e0#2"


def test_store_module_does_not_import_db():
    import pathlib
    import subprocess
    import sys
    repo = pathlib.Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c",
                          "import app.store.deferred, sys; print('app.db' in sys.modules)"],
                         cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"
