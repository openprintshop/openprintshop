"""Догон брошенных расчётов (A5, app/abandonednudge.py).

Проверяем детектор db.abandoned_calcs (кто попадает под догон, кого отсекаем), идемпотентность
пометки mark_calc_nudged и сам тик воркера (шлёт ровно одно сообщение и не повторяется, молчит
ночью и при выключенной фиче). Ловушка отправки — _FakeBot по образцу tests/test_admin_order_create.py.
"""
import time

from app import abandonednudge, config

USER = 456
ADMIN = 777


class _FakeBot:
    """Запоминает все send_message — так проверяем, кому и что ушло."""
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))


class _FailBot:
    """send_message падает → notify.notify_user ловит и возвращает False (клиент не запускал
    бота). Считаем попытки: даже при провале расчёт должен быть помечен нуджёным, без ретрая."""
    def __init__(self):
        self.attempts = 0

    async def send_message(self, chat_id, text, reply_markup=None):
        self.attempts += 1
        raise RuntimeError("chat not found")


async def _add_calc(db, user_id, age_s, *, model="Груша", price=12.5) -> int:
    """Расчёт (строка orders-лога) заданной давности: add_order штампует now, поэтому возраст
    выставляем прямым UPDATE created_at."""
    oid = await db.add_order(user_id=user_id, model_label=model, quantity=1,
                             quality="0.2", strength="Стандарт", plastic_id=1,
                             printer="P", grams_total=10.0, minutes_total=30.0,
                             total_price=price)
    now = int(time.time())
    await db._conn().execute("UPDATE orders SET created_at=? WHERE id=?", (now - age_s, oid))
    await db._conn().commit()
    return oid


# 24 ч .. 7 дней — те же значения, что и дефолты воркера
_MIN = 24 * 3600
_MAX = 7 * 86400


async def test_abandoned_calc_detected(tmp_db):
    db = tmp_db
    await _add_calc(db, USER, age_s=25 * 3600)   # старше 24 ч, в пределах 7 дней
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert len(rows) == 1
    assert rows[0]["user_id"] == USER
    assert rows[0]["model_label"] == "Груша"


async def test_converted_user_not_nudged(tmp_db):
    db = tmp_db
    await _add_calc(db, USER, age_s=25 * 3600)
    # оформил заказ (requests) после расчёта → уже клиент, не догоняем
    await db.add_request(user_id=USER, user_name="u", model_label="Груша", printer="P",
                         plastic_name="PLA", quantity=1, quality="0.2", strength="Стандарт",
                         grams_total=10.0, minutes_total=30.0, price=12.5, preview_path=None)
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []


async def test_too_recent_not_nudged(tmp_db):
    db = tmp_db
    await _add_calc(db, USER, age_s=1 * 3600)    # моложе 24 ч
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []


async def test_too_old_not_nudged(tmp_db):
    db = tmp_db
    await _add_calc(db, USER, age_s=8 * 86400)   # старше окна 7 дней → без ретро-спама легаси
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []


async def test_no_broadcast_excluded(tmp_db):
    db = tmp_db
    await _add_calc(db, USER, age_s=25 * 3600)
    await db._conn().execute(
        "INSERT INTO customers(user_id, no_broadcast, created_at) VALUES(?,1,?)",
        (USER, int(time.time())))
    await db._conn().commit()
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []


async def test_admin_excluded(tmp_db, monkeypatch):
    db = tmp_db
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN])
    await _add_calc(db, ADMIN, age_s=25 * 3600)   # собственный расчёт мастера
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []


async def test_active_cart_excluded(tmp_db):
    """Клиента с активной корзиной ведёт cart_nudge_loop — двойного пинга быть не должно."""
    db = tmp_db
    await _add_calc(db, USER, age_s=25 * 3600)
    await db._conn().execute(
        "INSERT INTO user_cart(user_id, count, updated_at) VALUES(?,2,?)",
        (USER, int(time.time())))
    await db._conn().commit()
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []


async def test_mark_calc_nudged_idempotent(tmp_db):
    db = tmp_db
    await _add_calc(db, USER, age_s=25 * 3600)
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert len(rows) == 1
    await db.mark_calc_nudged(USER)
    rows2 = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows2 == []


async def test_latest_calc_per_user(tmp_db):
    db = tmp_db
    await _add_calc(db, USER, age_s=30 * 3600, model="Старая")
    await _add_calc(db, USER, age_s=25 * 3600, model="Новая")
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert len(rows) == 1                    # GROUP BY user_id — один кандидат
    assert rows[0]["model_label"] == "Новая"  # bare-колонка из строки с MAX(created_at)


async def test_mark_calc_nudged_marks_all_user_rows(tmp_db):
    """Пометка гасит ВСЕ расчёты клиента — иначе следующий тик поднял бы предыдущий и нуднул повторно."""
    db = tmp_db
    await _add_calc(db, USER, age_s=30 * 3600, model="Старая")
    await _add_calc(db, USER, age_s=25 * 3600, model="Новая")
    await db.mark_calc_nudged(USER)
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []


async def test_tick_disabled_noop(tmp_db, monkeypatch):
    db = tmp_db
    monkeypatch.setattr(abandonednudge, "_in_quiet_hours", lambda h: False)
    await _add_calc(db, USER, age_s=25 * 3600)   # фича выключена по умолчанию (enabled=0)
    bot = _FakeBot()
    await abandonednudge._tick(bot)
    assert bot.messages == []


async def test_tick_sends_and_marks(tmp_db, monkeypatch):
    db = tmp_db
    monkeypatch.setattr(abandonednudge, "_in_quiet_hours", lambda h: False)
    await db.set_setting("nudge_abandoned_enabled", 1)
    await _add_calc(db, USER, age_s=25 * 3600)
    bot = _FakeBot()
    await abandonednudge._tick(bot)
    assert len(bot.messages) == 1
    assert bot.messages[0][0] == USER
    # второй тик уже ничего не шлёт — orders.nudged_at проставлен
    await abandonednudge._tick(bot)
    assert len(bot.messages) == 1


async def test_notify_false_still_marks(tmp_db, monkeypatch):
    """Клиент не запускал бота → notify_user вернул False. Всё равно помечаем нуджёным, иначе
    на каждом тике повторная попытка (ретрай-хаммер). Зеркалит поведение cart_nudge_loop."""
    db = tmp_db
    monkeypatch.setattr(abandonednudge, "_in_quiet_hours", lambda h: False)
    await db.set_setting("nudge_abandoned_enabled", 1)
    await _add_calc(db, USER, age_s=25 * 3600)
    bot = _FailBot()
    await abandonednudge._tick(bot)
    assert bot.attempts == 1                     # попытка отправки была
    # расчёт помечен, несмотря на провал отправки → второй тик не пытается снова
    rows = await db.abandoned_calcs(min_age_s=_MIN, max_age_s=_MAX)
    assert rows == []
    await abandonednudge._tick(bot)
    assert bot.attempts == 1                      # без ретрая


async def test_night_window_skips(tmp_db, monkeypatch):
    db = tmp_db
    monkeypatch.setattr(abandonednudge, "_in_quiet_hours", lambda h: True)
    await db.set_setting("nudge_abandoned_enabled", 1)
    await _add_calc(db, USER, age_s=25 * 3600)
    bot = _FakeBot()
    await abandonednudge._tick(bot)
    assert bot.messages == []
