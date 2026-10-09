"""B7 — источник лида (competitor-gap, шаг 5): «откуда берутся те ~7% конверсии».

Цепочка: регистрация токена модели ставит origin (file/link — выводится из src_url;
catalog/mine/repeat — задаёт ручка /api/use), оформление кладёт его в requests.source,
дашборд строит разбивку заказов по источникам и делит клиентов на новых/вернувшихся.

НЕ путать с print_journal.source (source ПЕЧАТИ: order/self/test) — это разные оси.
"""
import time

from app import db as appdb
from app import orders, webapp

UID = 8181


def _model(origin: str, label: str = "Кубик", price: float = 10.0) -> dict:
    """Минимальный снимок токена — ровно то, что читает orders.place_from_token."""
    return {
        "uid": UID, "label": label, "paths": [], "preview": None, "origin": origin,
        "last": {"printer": "Centauri", "plastic_name": "Test PLA · чёрный", "qty": 1,
                 "quality": "0.20", "strength": "Стандарт", "grams_total": 20.0,
                 "minutes_total": 100.0, "total": price, "dims": "40×40×20",
                 "color": "чёрный", "scale_pct": 100},
    }


async def _source_of(db, rid: int) -> str:
    cur = await appdb._conn().execute("SELECT source FROM requests WHERE id=?", (rid,))
    return (await cur.fetchone())["source"]


async def test_source_travels_from_token_to_request(tmp_db):
    """origin снимка токена доезжает до requests.source; непомеченный токен даёт ''."""
    u = {"id": UID, "username": "lead"}
    for origin in ("file", "link", "catalog", "mine", "repeat"):
        rid = await orders.place_from_token(u, _model(origin))
        assert await _source_of(tmp_db, rid) == origin
    rid = await orders.place_from_token(u, {k: v for k, v in _model("x").items()
                                            if k != "origin"})
    assert await _source_of(tmp_db, rid) == ""                # без разметки — честная пустота


async def test_register_model_derives_origin(tmp_db):
    """Регистрация без явного origin выводит его из пути: src_url → link, иначе file;
    явный origin (каталог/повтор) сильнее вывода. Реестр перед тестом пуст (autouse
    conftest.clean_models чистит ТОТ ЖЕ словарь через фасад — волна 14 endgame)."""
    tok = await webapp._register_model([], "A", UID, "https://makerworld.com/x",
                                       _info(), log=False)
    assert webapp._models[tok]["origin"] == "link"
    tok = await webapp._register_model([], "B", UID, "", _info(), log=False)
    assert webapp._models[tok]["origin"] == "file"
    tok = await webapp._register_model([], "C", UID, "", _info(), log=False, origin="catalog")
    assert webapp._models[tok]["origin"] == "catalog"


def _info():
    from types import SimpleNamespace
    return SimpleNamespace(plates=1, is_multicolor=False, num_colors=1,
                           used_colours=[], mixed_plates=False)


async def test_dashboard_breaks_orders_down_by_source(tmp_db):
    """sources_30d: группировка заказов окна по source, непомеченные — под '—'."""
    u = {"id": UID, "username": "lead"}
    for origin in ("link", "link", "catalog"):
        await orders.place_from_token(u, _model(origin))
    await orders.place_from_token(u, {k: v for k, v in _model("x").items() if k != "origin"})
    stats = await tmp_db.dashboard_stats()
    by = {row["source"]: row["orders"] for row in stats["sources_30d"]}
    assert by["link"] == 2 and by["catalog"] == 1 and by["—"] == 1


async def test_dashboard_splits_new_vs_returning(tmp_db):
    """clients_30d: клиент с заказами ДО окна — «вернувшийся», впервые заказавший — «новый»."""
    u_new = {"id": 9001, "username": "fresh"}
    u_ret = {"id": 9002, "username": "old"}
    rid_old = await orders.place_from_token(u_ret, _model("file"))
    # состариваем ПЕРВЫЙ заказ старожила за пределы 30-дневного окна
    await appdb._conn().execute("UPDATE requests SET created_at=? WHERE id=?",
                                (int(time.time()) - 40 * 86400, rid_old))
    await appdb._conn().commit()
    await orders.place_from_token(u_ret, _model("link"))      # свежий заказ старожила
    await orders.place_from_token(u_new, _model("file"))      # первый заказ новичка
    stats = await tmp_db.dashboard_stats()
    assert stats["clients_30d"] == {"new": 1, "returning": 1}
