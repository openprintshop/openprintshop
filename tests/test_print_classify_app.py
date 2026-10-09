"""«Чья печать?» через приложение (OpenTodo 4effbc1b): экранные API выбора заказа для
завершённой печати — данные экрана, привязка к заказу, самопечать, гашение дубль-уведомления."""
import json

from conftest import stub_bot

from app import webapp


class _FakeRequest:
    def __init__(self, body=None, query=None, match_info=None):
        self._body = body or {}
        self.query = query or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._body


async def _mk_journal(tmp_db, label="ECC2_koala.gcode"):
    return await tmp_db.add_print_journal(
        label=label, plastic_id=None, plastic_name="Elegoo PLA",
        grams=12.4, minutes=96.0, graded=0)


async def test_classify_screen_lists_active_orders(tmp_db, monkeypatch):
    jid = await _mk_journal(tmp_db)
    active = await tmp_db.add_request(
        user_id=11, user_name="@c1", model_label="Willys Jeep", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=30, price=15,
        preview_path=None)
    done = await tmp_db.add_request(
        user_id=12, user_name="@c2", model_label="Старый", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=30, price=5,
        preview_path=None, status="printed")
    monkeypatch.setattr(webapp, "_verify_admin", lambda s: 999)

    resp = await webapp._api_admin_print_classify(_FakeRequest(query={"jid": str(jid)}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True
    assert j["print"]["jid"] == jid and j["print"]["label"] == "ECC2_koala.gcode"
    ids = [o["id"] for o in j["orders"]]
    assert active in ids and done not in ids               # только активные заказы


async def test_classify_screen_includes_admin_own_orders(tmp_db, monkeypatch):
    """Заказ, оставленный самим админом (user_id ∈ ADMIN_IDS), тоже должен быть в списке
    выбора (OpenTodo 90c4ac36): раньше kind='client' его отфильтровывал, и печать по такому
    заказу было не к чему привязать из «Уведомлений»."""
    jid = await _mk_journal(tmp_db)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    own = await tmp_db.add_request(
        user_id=999, user_name="@master", model_label="Свой заказ", printer="P",
        plastic_name="PLA", quantity=1, quality="Q", strength="S", grams_total=10,
        minutes_total=30, price=15, preview_path=None)
    client_o = await tmp_db.add_request(
        user_id=11, user_name="@c1", model_label="Клиентский", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=30, price=15,
        preview_path=None)
    monkeypatch.setattr(webapp, "_verify_admin", lambda s: 999)

    resp = await webapp._api_admin_print_classify(_FakeRequest(query={"jid": str(jid)}))
    j = json.loads(resp.body.decode())
    ids = [o["id"] for o in j["orders"]]
    assert own in ids and client_o in ids
    assert ids == sorted(ids, reverse=True)                # свежие сверху, дедуп не ломает порядок


async def test_classify_pick_order_attaches_and_marks_notification(tmp_db, monkeypatch):
    jid = await _mk_journal(tmp_db)
    rid = await tmp_db.add_request(
        user_id=11, user_name="@c1", model_label="Джип", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=30, price=15,
        preview_path=None)
    nid = await tmp_db.add_admin_notification(
        "print_classify", title="Печать завершена", body="?", payload={"jid": jid})
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (999, None))
    stub_bot(monkeypatch, None)

    resp = await webapp._api_admin_print_classify_pick(
        _FakeRequest({"jid": jid, "action": "order", "id": rid}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and str(rid) in j["message"]

    entry = await tmp_db.get_print_journal(jid)
    assert entry["request_id"] == rid                      # печать прикреплена
    n = await tmp_db.get_admin_notification(nid)
    assert n["status"] == "done"                           # дубль-уведомление погашено


async def test_classify_pick_sends_no_tg_quality_survey(tmp_db, monkeypatch):
    """Чат-опрос качества после классификации выпилен (OpenTodo b02bc4c6): даже с живым
    ботом в Telegram ничего не уходит — оценка ставится в «Журнале печатей» приложения."""
    jid = await _mk_journal(tmp_db)
    rid = await tmp_db.add_request(
        user_id=11, user_name="@c1", model_label="Джип", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=30, price=15,
        preview_path=None)
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (999, None))
    stub_bot(monkeypatch, object())
    sent = []

    async def _notify(bot, text, **kw):
        sent.append(text)
    monkeypatch.setattr(webapp.notify, "notify_admins", _notify)

    resp = await webapp._api_admin_print_classify_pick(
        _FakeRequest({"jid": jid, "action": "order", "id": rid}))
    assert json.loads(resp.body.decode())["ok"] is True
    assert sent == [], "опрос качества в Telegram больше не отправляется"


async def test_classify_pick_self_creates_self_order(tmp_db, monkeypatch):
    jid = await _mk_journal(tmp_db, label="Своя печать")
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (999, None))
    stub_bot(monkeypatch, None)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)

    resp = await webapp._api_admin_print_classify_pick(
        _FakeRequest({"jid": jid, "action": "self"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True

    rows = await tmp_db.list_requests(10, kind="self")
    assert any(r.status == "self" for r in rows)           # самопечать заведена


async def test_classify_pick_self_is_idempotent(tmp_db, monkeypatch):
    """Повторный «Для себя» не должен заводить ВТОРОЙ заказ-самопечать: старый оставался бы
    сиротой с self_print=1, и admin_would_spend суммировал бы оба (12.50 превращались в 25).
    Нарваться легко: карточка в «Уведомлениях» переживала разбор из чата и держала живую
    кнопку выбора заказа."""
    jid = await _mk_journal(tmp_db, label="Своя печать")
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (999, None))
    stub_bot(monkeypatch, None)
    monkeypatch.setattr(webapp.config, "ADMIN_IDS", [999], raising=False)
    body = {"jid": jid, "action": "self"}

    assert json.loads((await webapp._api_admin_print_classify_pick(
        _FakeRequest(body))).body.decode())["ok"] is True
    first = [r.id for r in await tmp_db.list_requests(50, kind="self")]

    j2 = json.loads((await webapp._api_admin_print_classify_pick(
        _FakeRequest(body))).body.decode())
    assert j2["ok"] is False and "уже" in j2["error"]
    assert [r.id for r in await tmp_db.list_requests(50, kind="self")] == first, \
        "второй заказ-самопечать создаваться не должен"


async def test_classify_pick_order_still_allowed_when_prebound(tmp_db, monkeypatch):
    """Гард идемпотентности не должен ломать ОСНОВНОЙ сценарий: printwatch проставляет
    request_id ещё при создании записи, когда печать шла по заказу, и мастер потом просто
    подтверждает этот заказ."""
    rid = await tmp_db.add_request(
        user_id=11, user_name="@c1", model_label="Джип", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=30, price=15,
        preview_path=None)
    jid = await tmp_db.add_print_journal(
        label="ECC2.gcode", plastic_id=None, plastic_name="PLA", grams=12.4, minutes=96.0,
        graded=0, request_id=rid)
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (999, None))
    stub_bot(monkeypatch, None)

    resp = await webapp._api_admin_print_classify_pick(
        _FakeRequest({"jid": jid, "action": "order", "id": rid}))
    assert json.loads(resp.body.decode())["ok"] is True


async def test_classify_pick_bad_inputs(tmp_db, monkeypatch):
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (999, None))
    stub_bot(monkeypatch, None)
    resp = await webapp._api_admin_print_classify_pick(
        _FakeRequest({"jid": 99999, "action": "order", "id": 1}))
    assert json.loads(resp.body.decode())["ok"] is False   # нет записи журнала

    jid = await _mk_journal(tmp_db)
    resp = await webapp._api_admin_print_classify_pick(
        _FakeRequest({"jid": jid, "action": "order", "id": 424242}))
    assert json.loads(resp.body.decode())["ok"] is False   # нет такого заказа

    resp = await webapp._api_admin_print_classify_pick(
        _FakeRequest({"jid": jid, "action": "wat"}))
    assert json.loads(resp.body.decode())["ok"] is False   # неизвестное действие
