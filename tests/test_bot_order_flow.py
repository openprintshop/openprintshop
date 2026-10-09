"""Кнопочная цепочка бота: «Оставить заказ» → принять/отклонить цену → отменить заказ.

Из ~95 хендлеров app/handlers/user_flow.py подавляющее большинство не было названо ни в одном
тесте, и денежный путь бота (оформление заказа + реакция клиента на назначенную цену) шёл вслепую.
Здесь он прогоняется РЕАЛЬНЫМИ вызовами хендлеров с фейковыми CallbackQuery/Message/FSM — фейки
взяты по образцу tests/test_print_classify.py (тот же вид, третьего варианта не заводим), бот —
общий FakeBot из tests/conftest.py плюс правка экрана (ui.render редактирует screen_id).

Тесты фиксируют ТЕКУЩЕЕ поведение. Склад бота сведён с веб-путём волной 5 (шаг 5): заказ
резервирует филамент, а списывается он при «напечатано» — см.
test_place_order_reserves_stock_and_deducts_on_printed.
"""
import pytest
from conftest import FakeBot

from app import config, payments, pricing
from app import db as dbmod
from app.handlers import user_flow

USER_ID = 777
ADMIN_ID = 999


# ─────────────────────────────── фейки ───────────────────────────────

class _FakeState:
    """FSM-контекст: тот же фейк, что в tests/test_print_classify.py."""

    def __init__(self, **data):
        self._state = None
        self.data = dict(data)

    async def set_state(self, s=None):
        self._state = s

    async def get_state(self):
        return self._state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


class _FakeMsg:
    """Сообщение-экран. К фейку из test_print_classify.py добавлен edit_text: карточку заказа
    хендлеры обновляют именно им (_refresh_request_msg)."""

    def __init__(self, chat_id=USER_ID):
        self.text = None
        self.chat = type("C", (), {"id": chat_id})()
        self.message_id = 555
        self.answers = []
        self.edits = []

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def edit_reply_markup(self, **kw):
        pass

    async def edit_text(self, text, **kw):
        self.edits.append(text)


class _FakeBot(FakeBot):
    """FakeBot из conftest + правка экрана: ui.render() сперва редактирует уже отправленное
    сообщение (screen_id из FSM-данных), а такого метода общему фейку не хватает."""

    def __init__(self):
        super().__init__()
        self.edited: list = []          # (chat_id, message_id, text)

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, **kw):
        self.edited.append((chat_id, message_id, text))

    async def delete_message(self, chat_id, message_id):
        pass


class _FakeCb:
    """CallbackQuery: к фейку из test_print_classify.py добавлены bot (хендлеры заказа шлют
    уведомления мастеру через cb.bot) и username/full_name (из них лепится user_name заказа).

    first_name/last_name появились вместе с общим конструктором заказа: подпись клиента лепит
    теперь он, и склеивает он именно эту пару — ровно так же, как aiogram в User.full_name."""

    def __init__(self, data, uid=USER_ID, *, bot=None, username="tester",
                 full_name="Тест Тестов", first_name="Тест", last_name="Тестов"):
        self.data = data
        self.from_user = type("U", (), {"id": uid, "username": username,
                                        "full_name": full_name,
                                        "first_name": first_name, "last_name": last_name})()
        self.message = _FakeMsg(chat_id=uid)
        self.bot = bot if bot is not None else _FakeBot()
        self.answers = []

    async def answer(self, text="", **kw):
        self.answers.append(text)


async def _true() -> bool:
    """Готовый «да» для подмены асинхронных payments.enabled/send_invoice."""
    return True


@pytest.fixture(autouse=True)
def _admins(monkeypatch):
    """Мастер, которому уходят уведомления, — не клиент из тестов: иначе заказ считался бы
    самозаказом и часть веток менялась бы."""
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(config, "WEBAPP_PUBLIC_URL", None)   # без кнопки-вебапки в уведомлении


# ─────────────────────────────── данные ───────────────────────────────

def _breakdown() -> pricing.PriceBreakdown:
    """Разбивка цены расчёта: 2 шт, 120 г, 180 мин, итог 250."""
    return pricing.PriceBreakdown(
        qty=2, grams_unit=60.0, minutes_unit=90.0, grams_total=120.0, minutes_total=180.0,
        filament_cost=100.0, machine_cost=50.0, waste_cost=10.0, setup_fee=20.0, margin=70.0,
        discount_pct=0.0, discount_amount=0.0, subtotal=180.0, total=250.0, min_applied=False)


def _last_result(bd, plastic_name="Тест PLA") -> dict:
    return {"bd": bd, "model_label": "Дракон", "printer_name": "CC2",
            "plastic_name": plastic_name, "qty": bd.qty, "quality_label": "0.20 мм",
            "strength_label": "стандарт", "settings": {}, "preview_path": None,
            "dims": "60×40×20 мм", "multicolor": 0, "plates": 1}


def _calc_state(pid: int, qid: int, sid: int, bd) -> _FakeState:
    """FSM после завершённого расчёта — ровно то, на что смотрит place_order."""
    return _FakeState(last_result=_last_result(bd), model_paths=[], plastic_id=pid,
                      colors_sel=["Чёрный"], color_grams=None, quality_id=qid, strength_id=sid,
                      wishes="  хочу поярче  ", model_src_url="https://makerworld.com/models/1",
                      screen_id=555)


async def _seed(db, stock=1000.0):
    """Пластик со складом + пресеты качества/прочности."""
    pid = await db.add_plastic("Тест PLA", 1200.0, 1.24)
    await db.update_plastic(pid, stock_grams=stock)
    qid = (await db.list_quality(active_only=True))[0].id
    sid = (await db.list_strength(active_only=True))[0].id
    return pid, qid, sid


async def _mk_request(db, *, status="new", price=250.0, pending=None, paid=0) -> int:
    rid = await db.add_request(
        user_id=USER_ID, user_name="@tester", model_label="Дракон", printer="CC2",
        plastic_name="Тест PLA", quantity=2, quality="0.20 мм", strength="стандарт",
        grams_total=120.0, minutes_total=180.0, price=price, preview_path=None,
        status=status, pending_price=pending)
    if paid:
        await db.update_request(rid, paid=1)
    return rid


# ─────────────────────────── «Оставить заказ» ───────────────────────────

async def test_place_order_creates_request(tmp_db):
    """order:place создаёт строку requests с полями из расчёта и отвечает клиенту/мастеру."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bd = _breakdown()
    st = _calc_state(pid, qid, sid, bd)
    cb = _FakeCb("order:place")

    await user_flow.place_order(cb, st)

    rows = await db.list_user_requests(USER_ID)
    assert len(rows) == 1
    r = rows[0]
    assert r.user_name == "@tester"                 # username есть → «@…», иначе full_name
    assert r.model_label == "Дракон" and r.printer == "CC2" and r.plastic_name == "Тест PLA"
    assert r.quantity == 2 and r.quality == "0.20 мм" and r.strength == "стандарт"
    assert r.grams_total == 120.0 and r.minutes_total == 180.0
    assert r.price == 250.0                          # цена = итог разбивки
    assert r.status == "new" and r.pending_price is None
    assert r.quality_id == qid and r.strength_id == sid
    assert r.description == "хочу поярче"            # особые пожелания, обрезанные по краям
    assert r.src_url == "https://makerworld.com/models/1"
    assert r.multicolor == 0 and r.plates == 1
    assert (r.breakdown or {}).get("total") == 250.0  # снимок расчётника лежит в заказе

    assert cb.answers == ["Заказ оставлен ✅"]
    assert cb.bot.edited and "Заказ оставлен" in cb.bot.edited[0][2]     # экран клиента заменён
    assert any(chat == ADMIN_ID and f"#{r.id}" in text                   # мастеру ушло уведомление
               for chat, text, _ in cb.bot.messages)
    assert any(a["action"] == "оставил заказ" for a in await db.list_activity(user_id=USER_ID))


async def test_place_order_reserves_stock_and_deducts_on_printed(tmp_db):
    """Склад бот-заказа: РЕЗЕРВ при оформлении, списание — при «напечатано», со ссылкой на заказ.

    До волны 5 (шаг 5) бот списывал склад прямо в place_order, плана резерва не строил и движение
    ссылкой на заказ не помечал. Двойного списания это НЕ давало (apply_order_stock ходит только
    по requests.stock_plan, а он у бот-заказа был пуст), но последствие было тише и денежнее:
    без ref_type='request' compute_request_cogs не находил расхода, и cogs_actual у ВСЕХ бот-
    заказов оставался нулевым — отчёты по марже завышены.

    Обе половины починки — снятие немедленного списания и план резерва — проверяются здесь ОДНИМ
    тестом, потому что порознь они дают беду: план поверх немедленного списания = двойной расход,
    снятое списание без плана = заказ, за который склад не спишется никогда.

    Числа: заказ на 120 г, склад пластика 1000 г, катушка 800 г за 1600 ₽ (⇒ 2 ₽/г)."""
    db = tmp_db
    pid, qid, sid = await _seed(db, stock=1000.0)
    await db.update_plastic(pid, low_threshold=900.0)   # чтобы проверить и алерт «мало»
    spool = await db.add_spool(pid, "Чёрный", 800.0, cost=1600.0)
    bd = _breakdown()
    st = _calc_state(pid, qid, sid, bd)

    await user_flow.place_order(_FakeCb("order:place"), st)
    rid = (await db.list_user_requests(USER_ID))[0].id

    # 1. ОФОРМЛЕНИЕ. Склад не тронут ни в пластике, ни в катушке — филамент только зарезервирован.
    assert (await db.get_plastic(pid)).stock_grams == pytest.approx(1000.0)
    assert (await db.get_spool(spool))["remaining_g"] == pytest.approx(800.0)
    assert [m for m in await db.spool_ledger(spool) if m["delta_g"] < 0] == []
    plan = await db.get_order_stock_plan(rid)
    assert len(plan) == 1
    assert plan[0]["plastic_id"] == pid and plan[0]["name"] == "Тест PLA"
    assert plan[0]["colors"] == ["Чёрный"] and plan[0]["color_grams"] is None
    assert plan[0]["grams"] == pytest.approx(120.0)
    assert await db.reserved_grams_by_plastic() == {pid: pytest.approx(120.0)}
    cur = await db._conn().execute("SELECT stock_deducted FROM requests WHERE id = ?", (rid,))
    assert (await cur.fetchone())["stock_deducted"] == 0

    # 2. «НАПЕЧАТАНО». Списывается ровно 120 г, и алерт «низкий остаток» отдаётся вызывающему
    # (webapp._consume_order_stock) — своего алерта в боте больше нет, но предупреждение не потеряно.
    assert await db.apply_order_stock(rid) == [("Тест PLA", pytest.approx(880.0))]
    assert (await db.get_plastic(pid)).stock_grams == pytest.approx(880.0)
    assert (await db.get_spool(spool))["remaining_g"] == pytest.approx(680.0)
    assert await db.reserved_grams_by_plastic() == {}      # резерв снят, он же и стал расходом

    # 3. ССЫЛКА НА ЗАКАЗ — то, ради чего всё и затевалось.
    spend = [m for m in await db.spool_ledger(spool) if m["delta_g"] < 0]
    assert len(spend) == 1
    assert spend[0]["delta_g"] == pytest.approx(-120.0)
    assert spend[0]["ref_type"] == "request" and spend[0]["ref_id"] == rid

    # 4. СЕБЕСТОИМОСТЬ ПЕРЕСТАЛА БЫТЬ НУЛЁМ: 120 г × (1600 ₽ / 800 г) = 240 ₽.
    assert (await db.get_request(rid)).cogs_actual == pytest.approx(240.0)

    # 5. ДВОЙНОГО СПИСАНИЯ НЕТ: повторное «напечатано» не двигает ни склад, ни журнал, ни cogs.
    assert await db.apply_order_stock(rid) == []
    assert (await db.get_plastic(pid)).stock_grams == pytest.approx(880.0)
    assert (await db.get_spool(spool))["remaining_g"] == pytest.approx(680.0)
    assert len([m for m in await db.spool_ledger(spool) if m["delta_g"] < 0]) == 1
    assert (await db.get_request(rid)).cogs_actual == pytest.approx(240.0)


async def test_place_order_without_calc_does_nothing(tmp_db):
    """Без последнего расчёта кнопка только ругается — заказ не появляется."""
    db = tmp_db
    cb = _FakeCb("order:place")

    await user_flow.place_order(cb, _FakeState())

    assert await db.list_user_requests(USER_ID) == []
    assert cb.answers == ["Сначала сделайте расчёт"]


# ─────────────────────── принять / отклонить цену ───────────────────────

async def test_accept_price_applies_pending(tmp_db):
    """uacc: назначенная мастером цена становится ценой заказа, статус — «В работе»
    (онлайн-оплата в тестовой БД не настроена, иначе был бы awaiting_payment)."""
    db = tmp_db
    assert await payments.enabled() is False
    rid = await _mk_request(db, status="price_changed", price=250.0, pending=310.0)
    cb = _FakeCb(f"uacc:{rid}")

    await user_flow.user_accept_price(cb, _FakeState())

    r = await db.get_request(rid)
    assert r.price == 310.0 and r.pending_price is None and r.status == "in_progress"
    assert cb.answers == ["Принято ✅"]
    assert any(chat == ADMIN_ID and "принял" in text for chat, text, _ in cb.bot.messages)
    assert any(a["action"] == "принял цену" for a in await db.list_activity(user_id=USER_ID))
    assert cb.message.edits                      # карточка заказа перерисована


async def test_accept_price_with_payments_waits_for_payment(tmp_db, monkeypatch):
    """При настроенной онлайн-оплате принятая сумма уходит этапом «Ждёт оплаты» (A1)."""
    db = tmp_db
    monkeypatch.setattr(payments, "enabled", lambda: _true())
    monkeypatch.setattr(payments, "send_invoice", lambda *a, **k: _true())
    rid = await _mk_request(db, status="invoiced", price=250.0, pending=310.0)
    cb = _FakeCb(f"uacc:{rid}")

    await user_flow.user_accept_price(cb, _FakeState())

    r = await db.get_request(rid)
    assert r.price == 310.0 and r.status == "awaiting_payment"
    assert any("ждёт оплаты" in text.lower() for _, text, _ in cb.bot.messages)


async def test_accept_price_foreign_order_refused(tmp_db):
    """Чужой заказ по подделанному callback-у не трогается."""
    db = tmp_db
    rid = await _mk_request(db, status="price_changed", pending=310.0)
    cb = _FakeCb(f"uacc:{rid}", uid=USER_ID + 1)

    await user_flow.user_accept_price(cb, _FakeState())

    r = await db.get_request(rid)
    assert r.status == "price_changed" and r.pending_price == 310.0
    assert cb.answers == ["Не найдено"]


async def test_decline_price_marks_rejected(tmp_db):
    """udec: заказ уходит в «отказ», предложенная цена стирается."""
    db = tmp_db
    rid = await _mk_request(db, status="price_changed", price=250.0, pending=310.0)
    cb = _FakeCb(f"udec:{rid}")

    await user_flow.user_decline_price(cb, _FakeState())

    r = await db.get_request(rid)
    assert r.status == "rejected" and r.pending_price is None
    assert r.price == 250.0                       # прежняя цена НЕ переписывается
    assert cb.answers == ["Отказ зафиксирован"]
    assert any(chat == ADMIN_ID and "отказался" in text for chat, text, _ in cb.bot.messages)
    assert any(a["action"] == "отклонил цену" for a in await db.list_activity(user_id=USER_ID))


async def test_decline_price_wrong_status_ignored(tmp_db):
    """Кнопка на заказе без назначенной цены ничего не меняет."""
    db = tmp_db
    rid = await _mk_request(db, status="new")
    cb = _FakeCb(f"udec:{rid}")

    await user_flow.user_decline_price(cb, _FakeState())

    assert (await db.get_request(rid)).status == "new"
    assert cb.answers == ["Действие недоступно"]


# ─────────────────────────── отмена заказа ───────────────────────────

async def test_cancel_awaiting_payment_order(tmp_db):
    """ucancel на заказе «Ждёт оплаты»: статус «отменён», мастеру уведомление без приписки
    про возврат (заказ не оплачен)."""
    db = tmp_db
    assert "awaiting_payment" in dbmod.REQ_CANCELLABLE
    rid = await _mk_request(db, status="awaiting_payment", pending=310.0)
    cb = _FakeCb(f"ucancel:{rid}")

    await user_flow.user_cancel_order(cb, _FakeState())

    r = await db.get_request(rid)
    assert r.status == "cancelled" and r.pending_price is None
    assert cb.answers == ["Заказ отменён"]
    admin = [text for chat, text, _ in cb.bot.messages if chat == ADMIN_ID]
    assert admin and f"#{rid}" in admin[0] and "отменил заказ" in admin[0]
    assert "возврат" not in admin[0]


async def test_cancel_paid_order_warns_about_refund(tmp_db):
    """Оплаченный заказ отменяется так же, но мастеру приписывают напоминание о возврате."""
    db = tmp_db
    rid = await _mk_request(db, status="awaiting_payment", paid=1)
    cb = _FakeCb(f"ucancel:{rid}")

    await user_flow.user_cancel_order(cb, _FakeState())

    assert (await db.get_request(rid)).status == "cancelled"
    admin = [text for chat, text, _ in cb.bot.messages if chat == ADMIN_ID]
    assert admin and "ОПЛАЧЕН" in admin[0] and "возврат" in admin[0]


async def test_cancel_printed_order_refused(tmp_db):
    """Напечатанный заказ отменить нельзя — статус остаётся прежним."""
    db = tmp_db
    assert "printed" not in dbmod.REQ_CANCELLABLE
    rid = await _mk_request(db, status="printed")
    cb = _FakeCb(f"ucancel:{rid}")

    await user_flow.user_cancel_order(cb, _FakeState())

    assert (await db.get_request(rid)).status == "printed"
    assert cb.answers == ["Этот заказ уже нельзя отменить"]
    assert not [t for chat, t, _ in cb.bot.messages if chat == ADMIN_ID]
