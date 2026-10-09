"""Один конструктор заказа: app/orders.py, оба пути оформления и фасад в webapp.

Волна 5 цикла 2, шаг 4. `_place_one` уехал из webapp.py в app/orders.py как
`orders.place_from_token`, туда же — `_order_stock_plan` и `_embedded_thumb`; webapp держит
реэкспорт, а бот (user_flow.place_order) больше не строит свою строку requests, а собирает
снимок расчёта и зовёт тот же конструктор.

Что здесь проверяется и почему именно это:
  1. ФАСАД. Реэкспорт держится на построчных `# noqa` и отказывает не на старте, а в момент
     вызова у живого клиента — значит его нужно проверять тестом, а не глазами.
  2. ГЕЙТ ВОЛНЫ. Строка requests у бот-заказа и у веб-заказа с ОДНИМИ И ТЕМИ ЖЕ числами
     совпадает по ВСЕМ колонкам (кроме служебных id/времён). До унификации расходились
     `promised_date` и `breakdown.scale_pct`, а после шага 4 — ещё и `stock_plan`.
  3. ПЛАН РЕЗЕРВА теперь одинаков у обоих путей (шаг 5: бот перестал списывать склад немедленно
     и стал резервировать). Тест стоит здесь сторожем обратной стороны: снимок бота без
     plastic_id снова оставил бы заказ без плана — склад за него не списался бы никогда.

Фейки бота (CallbackQuery/FSM) и числа расчёта берутся из tests/test_bot_order_flow.py —
третьего комплекта фейков не заводим, и оба теста обязаны гонять ОДИН И ТОТ ЖЕ расчёт.
"""
import pytest
from test_bot_order_flow import ADMIN_ID, USER_ID, _breakdown, _calc_state, _FakeCb, _seed

from app import config, orders, webapp
from app.handlers import user_flow

# служебные колонки: ключ и метки времени у двух заказов разные по определению
_SKIP_COLS = {"id", "created_at", "updated_at"}


@pytest.fixture(autouse=True)
def _admins(monkeypatch):
    """Мастер — не клиент из тестов: иначе заказ считался бы самозаказом (add_request сразу
    проставляет admin_seen_at), и колонки двух заказов разъехались бы не по делу."""
    monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN_ID])
    monkeypatch.setattr(config, "WEBAPP_PUBLIC_URL", None)


def _web_token(bd, *, pid, qid, sid):
    """Снимок расчёта мини-аппы под те же числа, что кладёт в FSM бот (_calc_state).

    Пластик и цвета здесь ЕСТЬ — из них конструктор строит план резерва; с шага 5 их кладёт в
    снимок и бот, так что план у обоих путей выходит одинаковым. Лишний ключ `color` плану не
    нужен (тот берёт `colors_sel`) и на строку requests не влияет — он остался как есть, чтобы
    снимок веба был полным."""
    return {
        "label": "Дракон", "paths": [], "preview": None,
        "src_url": "https://makerworld.com/models/1", "multicolor": 0, "plates": 1,
        "pdf": {"bd": bd, "settings": {}},
        "last": {"printer": "CC2", "plastic_name": "Тест PLA", "qty": bd.qty,
                 "quality": "0.20 мм", "strength": "стандарт",
                 "grams_total": bd.grams_total, "minutes_total": bd.minutes_total,
                 "total": bd.total, "dims": "60×40×20 мм",
                 "quality_id": qid, "strength_id": sid,
                 "plastic_id": pid, "color": "Чёрный", "colors_sel": ["Чёрный"],
                 "color_grams": None},
    }


async def _row(db, rid: int) -> dict:
    cur = await db._conn().execute("SELECT * FROM requests WHERE id = ?", (rid,))
    return dict(await cur.fetchone())


# ─────────────────────────────── фасад ───────────────────────────────

def test_webapp_reexports_order_constructor():
    """Реэкспорт на построчных noqa жив: три имени в webapp — те же объекты, что в orders.

    Забытая строка фасада ломается не на старте, а в момент вызова: у клиента, оформляющего
    заказ. `ruff check --fix` этот блок сносит молча — тест единственная защита."""
    assert webapp._place_one is orders.place_from_token
    assert webapp._order_stock_plan is orders._order_stock_plan
    assert webapp._embedded_thumb is orders._embedded_thumb


# ──────────────────── бот и веб дают одну и ту же строку ────────────────────

async def test_bot_and_web_orders_agree_on_every_column(tmp_db):
    """ГЕЙТ ВОЛНЫ: набор полей requests у бот-заказа и веб-заказа совпадает ПОЛНОСТЬЮ.

    До общего конструктора расходились `promised_date` (у бота пусто) и `breakdown` (без
    scale_pct); после шага 4 оставался `stock_plan` (у бота пустой) — шаг 5 закрыл и его."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bd = _breakdown()

    await user_flow.place_order(_FakeCb("order:place"), _calc_state(pid, qid, sid, bd))
    bot_rid = (await db.list_user_requests(USER_ID))[0].id

    web_rid = await orders.place_from_token(
        {"id": USER_ID, "username": "tester", "first_name": "Тест", "last_name": "Тестов"},
        _web_token(bd, pid=pid, qid=qid, sid=sid), comment="хочу поярче")

    bot_row, web_row = await _row(db, bot_rid), await _row(db, web_rid)
    diff = {k for k in bot_row if k not in _SKIP_COLS and bot_row[k] != web_row[k]}
    assert diff == set(), f"неожиданное расхождение колонок: {sorted(diff)}"

    # выборочно и явно — то, что расходилось до унификации
    assert bot_row["promised_date"] and bot_row["promised_date"] == web_row["promised_date"]
    assert bot_row["user_name"] == web_row["user_name"] == "@tester"
    assert bot_row["description"] == web_row["description"] == "хочу поярче"
    assert bot_row["urgency"] == web_row["urgency"] == ""


async def test_bot_and_web_orders_build_the_same_stock_plan(tmp_db):
    """План резерва у бот-заказа и веб-заказа побайтово одинаков (шаг 5 волны 5).

    Пустой план у бот-заказа здесь означал бы, что из его снимка снова выпал `plastic_id`, —
    и склад за такой заказ не списался бы НИКОГДА (apply_order_stock ходит только по плану).
    Обратная беда — план ПЛЮС немедленное списание в user_flow.place_order — даёт двойной
    расход; за ней следит tests/test_bot_order_flow.py, где считаются граммы до и после."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bd = _breakdown()

    await user_flow.place_order(_FakeCb("order:place"), _calc_state(pid, qid, sid, bd))
    bot_rid = (await db.list_user_requests(USER_ID))[0].id
    web_rid = await orders.place_from_token(
        {"id": USER_ID, "username": "tester"},
        _web_token(bd, pid=pid, qid=qid, sid=sid))

    bot_plan = await db.get_order_stock_plan(bot_rid)
    assert len(bot_plan) == 1 and bot_plan[0]["plastic_id"] == pid
    assert bot_plan[0]["colors"] == ["Чёрный"]
    assert bot_plan[0]["grams"] == pytest.approx(bd.grams_total)
    assert bot_plan == await db.get_order_stock_plan(web_rid)


# ──────────────────── хвост побочных эффектов у бота ────────────────────

async def test_bot_order_logs_activity_once_with_label(tmp_db):
    """Запись в активности ровно одна и с названием модели.

    Своя `log_event` из бота убрана — её делает конструктор. Две записи на один заказ означали
    бы, что старую строку вернули обратно."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    await user_flow.place_order(_FakeCb("order:place"), _calc_state(pid, qid, sid, _breakdown()))

    rid = (await db.list_user_requests(USER_ID))[0].id
    placed = [a for a in await db.list_activity(user_id=USER_ID) if a["action"] == "оставил заказ"]
    assert len(placed) == 1
    assert placed[0]["detail"] == f"#{rid} «Дракон»"
    assert placed[0]["name"] == "@tester"       # подпись та же, что в самом заказе


async def test_bot_order_keeps_breakdown_snapshot(tmp_db):
    """Смета заказа на месте, и в ней появился scale_pct — тот же снимок, что у веб-заказа
    (пересчёты заказа слайсят по нему же)."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    bd = _breakdown()
    await user_flow.place_order(_FakeCb("order:place"), _calc_state(pid, qid, sid, bd))

    r = (await db.list_user_requests(USER_ID))[0]
    assert (r.breakdown or {}).get("total") == bd.total
    assert (r.breakdown or {}).get("scale_pct") == 100


async def test_bot_order_without_username_signs_with_full_name(tmp_db):
    """Клиент без @username подписан склеенным именем — конструктор джойнит first+last ровно
    так же, как aiogram в User.full_name. Это проверка перехода на пару first/last."""
    db = tmp_db
    pid, qid, sid = await _seed(db)
    cb = _FakeCb("order:place", username=None, first_name="Иван", last_name="Петров")

    await user_flow.place_order(cb, _calc_state(pid, qid, sid, _breakdown()))

    assert (await db.list_user_requests(USER_ID))[0].user_name == "Иван Петров"
