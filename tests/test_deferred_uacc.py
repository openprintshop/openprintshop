"""Очередь досчёта (Э0): кнопка «Принять» помнит сумму, карточки честно говорят «цена считается».

* uacc:<rid>:<копейки> принимает ТОЛЬКО ту цену, что видел клиент; цену успели поменять —
  показываем актуальную с новыми кнопками и не принимаем; старые uacc:<rid> — как раньше;
* /api/order/respond: необязательное поле expected с тем же смыслом;
* _order_json / request_detail / карточка мастера: price_pending при status='new' — «Цена
  считается», а не «Оценит мастер» / «Автослайс не удался».
"""
import json
import pathlib

import pytest
from conftest import FakeBot, stub_auth

from app import config, keyboards, texts, webapp
from app.handlers import user_flow

UID = 777
REPO = pathlib.Path(__file__).resolve().parents[1]


class _State:
    async def get_data(self):
        return {}

    async def update_data(self, **kw):
        pass


class _Msg:
    def __init__(self):
        self.chat = type("C", (), {"id": UID})()
        self.message_id = 1
        self.edits = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class _Cb:
    def __init__(self, data, uid=UID):
        self.data = data
        self.from_user = type("U", (), {"id": uid, "username": "c", "full_name": "C"})()
        self.message = _Msg()
        self.bot = FakeBot()
        self.answers = []

    async def answer(self, text="", **kw):
        self.answers.append(text)


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _no_webapp_url(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_PUBLIC_URL", None)


async def _priced(db, pending=123.45, price=0.0):
    return await db.add_request(
        user_id=UID, user_name="c", model_label="Ваза", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=10, minutes_total=10, price=price,
        preview_path=None, status="price_changed", pending_price=pending)


def _data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def test_price_confirm_carries_amount():
    assert _data(keyboards.price_confirm(5)) == ["uacc:5", "udec:5"]
    assert _data(keyboards.price_confirm(5, 123.45)) == ["uacc:5:12345", "udec:5"]
    kb = keyboards.price_confirm(5, 0.1 + 0.2, webapp_url="https://x.example")
    assert _data(kb)[0] == "uacc:5:30"                   # round, не int(): 0.30000000000000004
    assert kb.inline_keyboard[-1][0].web_app.url.endswith("&go=orders")


async def test_uacc_with_matching_amount_accepts(tmp_db):
    rid = await _priced(tmp_db)
    cb = _Cb(f"uacc:{rid}:12345")
    await user_flow.user_accept_price(cb, _State())
    r = await tmp_db.get_request(rid)
    assert r.status == "in_progress" and r.price == 123.45 and r.pending_price is None
    assert cb.answers == ["Принято ✅"]


async def test_uacc_with_stale_amount_shows_actual_price(tmp_db):
    """Мастер (или ПК) поменял цену после того, как клиент её увидел: старая кнопка цену
    не принимает, а показывает актуальную и кнопки уже под неё."""
    rid = await _priced(tmp_db, pending=150.0)
    cb = _Cb(f"uacc:{rid}:12345")
    await user_flow.user_accept_price(cb, _State())
    r = await tmp_db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price == 150.0 and r.price == 0
    assert "изменилась" in cb.answers[0] and "150" in cb.answers[0]
    text, kb = cb.message.edits[-1]
    assert "150" in text and f"uacc:{rid}:15000" in _data(kb)
    assert not any("принял" in t for _, t, _ in cb.bot.messages), "мастеру «принял» не уходит"


async def test_old_button_without_amount_works_as_before(tmp_db):
    rid = await _priced(tmp_db, pending=99.0)
    cb = _Cb(f"uacc:{rid}")
    await user_flow.user_accept_price(cb, _State())
    assert (await tmp_db.get_request(rid)).price == 99.0


async def test_uacc_garbage_amount_is_treated_as_old_button(tmp_db):
    rid = await _priced(tmp_db, pending=10.0)
    cb = _Cb(f"uacc:{rid}:abc")
    await user_flow.user_accept_price(cb, _State())
    assert (await tmp_db.get_request(rid)).status == "in_progress"


async def test_my_orders_item_button_carries_amount(tmp_db):
    rid = await _priced(tmp_db, pending=42.0)
    kb = keyboards.user_request_item(await tmp_db.get_request(rid))
    assert f"uacc:{rid}:4200" in _data(kb)


# ─────────────────────────────── мини-аппа ───────────────────────────────

@pytest.mark.parametrize("expected, accepted", [(12345, True), (100, False), (None, True)])
async def test_respond_expected(tmp_db, monkeypatch, expected, accepted):
    rid = await _priced(tmp_db)
    stub_auth(monkeypatch, _verify_init_data=lambda s: UID)
    body = {"order_id": rid, "accept": True}
    if expected is not None:
        body["expected"] = expected
    j = json.loads((await webapp._api_order_respond(_Req(body))).body.decode())
    r = await tmp_db.get_request(rid)
    if accepted:
        assert j["ok"] and r.status == "in_progress" and r.price == 123.45
    else:
        assert j["ok"] is False and j["stale"] is True and j["pending_minor"] == 12345
        assert r.status == "price_changed" and r.price == 0


async def test_order_json_says_price_is_being_calculated(tmp_db):
    rid = await tmp_db.add_request(
        user_id=UID, user_name="c", model_label="Ваза", printer="", plastic_name="", quantity=1,
        quality="", strength="", grams_total=0, minutes_total=0, price=0, preview_path=None,
        unsliced=1, price_pending=1)
    j = webapp._order_json(await tmp_db.get_request(rid))
    assert j["price"] == texts.PRICE_PENDING_SHORT and j["price_pending"] is True
    assert j["price_note"] == texts.PRICE_PENDING_NOTE
    detail = texts.request_detail(await tmp_db.get_request(rid), config.CURRENCY)
    # review:regress#1 — «💰 Цена: Цена считается» повторяло слово; строка цены читается
    # одной фразой, без повтора
    assert "Цена: <b>Цена" not in detail
    assert "💰 Цена: <b>считается</b> — пришлём, как только посчитаем" in detail
    # ушёл из «Новый» — флаг смысла не имеет
    await tmp_db._conn().execute("UPDATE requests SET status='cancelled' WHERE id=?", (rid,))
    await tmp_db._conn().commit()
    assert webapp._order_json(await tmp_db.get_request(rid))["price_pending"] is False


async def test_order_json_first_price_and_minor(tmp_db):
    rid = await _priced(tmp_db, pending=19.9)
    j = webapp._order_json(await tmp_db.get_request(rid))
    assert j["needs_response"] and j["first_price"] is True and j["pending_minor"] == 1990
    detail = texts.request_detail(await tmp_db.get_request(rid), config.CURRENCY)
    assert "Было" not in detail and "Подтвердите цену" in detail


class _AdminReq:
    def __init__(self, oid):
        self.query = {"initData": "x"}
        self.match_info = {"id": str(oid)}


async def test_admin_order_json_separates_pending_from_failed_slice(tmp_db, monkeypatch):
    """Мастеру: «ждёт расчёта ПК» — отдельный признак, не «автослайс не удался» (обе админки)."""
    stub_auth(monkeypatch, _verify_admin=lambda init_data: config.ADMIN_IDS[0])

    async def _card(rid):
        return json.loads((await webapp._api_admin_order(_AdminReq(rid))).body.decode())["order"]

    common = dict(user_id=UID, user_name="c", model_label="Ваза", printer="", plastic_name="",
                  quantity=1, quality="", strength="", grams_total=0, minutes_total=0, price=0,
                  preview_path=None, unsliced=1)
    waiting = await _card(await tmp_db.add_request(**common, price_pending=1))
    failed = await _card(await tmp_db.add_request(**common))
    assert waiting["price_pending"] == 1 and waiting["unsliced"] == 1
    assert failed["price_pending"] == 0 and failed["unsliced"] == 1
    js = (REPO / "app/webui/screens_admin.js").read_text(encoding="utf-8")
    assert "Ждёт расчёта ПК" in js and "Автослайс не удался — оцените вручную" in js
    jsx = (REPO / "app/webui/desk/src/screens/orders.jsx").read_text(encoding="utf-8")
    assert jsx.count("o.price_pending ?") >= 2
    # бандл деска пересобран (esbuild экранирует кириллицу — проверяем по имени поля)
    assert "price_pending" in (REPO / "app/webui/desk/desk.js").read_text(encoding="utf-8")
