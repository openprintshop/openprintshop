"""Лента рекомендаций (df0c9674 + feba4074 + 7405a432): 70/30, ротация, без «Напечатанного».
Плюс лента F4 (/api/feed): курсорная пагинация и время печати из последнего расчёта."""
import json

from conftest import stub_auth

from app import webapp
from app.web import storefront


async def _seed(db, n=12):
    for i in range(n):
        cid = await db.add_to_collection(
            hash=f"h{i}", label=f"Модель {i}", paths=[f"m{i}.stl"], preview_path=None,
            added_by=None)
        await db.set_collection_category(cid, "Напечатанное" if i < 3 else
                                         ("Игрушки" if i < 8 else "Лампы"))


async def test_recommend_excludes_printed_and_rotates(tmp_db):
    db = tmp_db
    await _seed(db)
    page, nxt = await db.recommend_page(101, limit=50, offset=0)
    labels = [it.label for it in page]
    # «Напечатанное» (Модель 0–2) в ленту не попадает (7405a432)
    assert labels and all(int(x.split()[1]) >= 3 for x in labels)
    assert nxt is None                       # всё уместилось — конца ленты нет

    # раскладка стабильна в пределах юзера+дня (пагинация не дублирует)…
    page2, _ = await db.recommend_page(101, limit=50, offset=0)
    assert [it.id for it in page2] == [it.id for it in page]
    # …но у другого юзера порядок другой (ротация, feba4074)
    for other in range(102, 130):
        alt, _ = await db.recommend_page(other, limit=50, offset=0)
        if [it.id for it in alt] != [it.id for it in page]:
            break
    else:
        raise AssertionError("порядок ленты одинаков у 28 разных юзеров — ротация не работает")


async def test_recommend_pagination_no_duplicates(tmp_db):
    db = tmp_db
    await _seed(db, n=20)
    first, nxt = await db.recommend_page(7, limit=5, offset=0)
    assert nxt == 5
    second, _ = await db.recommend_page(7, limit=5, offset=nxt)
    ids1 = {it.id for it in first}
    ids2 = {it.id for it in second}
    assert ids1 and ids2 and not (ids1 & ids2)   # страницы не пересекаются


# ---- лента F4: время печати и /api/feed --------------------------------------------------

async def _add_calc(db, hash, minutes, qty=1, rid_label="m"):
    await db.add_request(
        user_id=1, user_name="u", model_label=rid_label, printer="P", plastic_name="PLA",
        quantity=qty, quality="q", strength="s", grams_total=10.0, minutes_total=minutes,
        price=1.0, preview_path=None, hash=hash)


async def test_collection_last_minutes_latest_per_piece(tmp_db):
    db = tmp_db
    # два расчёта одной модели: берётся ПОСЛЕДНИЙ, минуты — на одну штуку (÷ qty)
    await _add_calc(db, "hA", minutes=60.0, qty=1)
    await _add_calc(db, "hA", minutes=240.0, qty=2)
    await _add_calc(db, "hB", minutes=0.0)          # нулевое время не считается расчётом
    got = await db.collection_last_minutes(["hA", "hB", "hA", "", "hZ"])
    assert got == {"hA": 120.0}


async def test_api_feed_cursor_and_time(tmp_db, monkeypatch):
    db = tmp_db
    await _seed(db, n=20)
    await _add_calc(db, "h5", minutes=90.0)         # «Модель 5» из _seed — с временем печати

    class _Req:
        def __init__(self, query):
            self.query = query

    stub_auth(monkeypatch, _verify_init_data=lambda s: 7)
    # чередование карточки Gridfinity-конфигуратора начинается с чистого листа —
    # процессное состояние могло остаться от других тестов
    storefront._GF_FEED_SHOWN.clear()
    j = json.loads((await webapp._api_feed(_Req({"cursor": "0", "limit": "5"}))).body.decode())
    # ПЕРВОЕ открытие первой страницы подмешивает карточку конфигуратора: позиция 2..5
    # (индекс 1..4 — самую верхнюю рекомендацию не заслоняет), сами рекомендации целы
    gf = [x for x in j["items"] if x.get("kind") == "gridfinity"]
    real = [x for x in j["items"] if x.get("kind") != "gridfinity"]
    assert j["ok"] is True and len(real) == 5 and j["next"] == 5
    assert len(gf) == 1 and j["items"].index(gf[0]) in (1, 2, 3, 4)
    it = real[0]
    assert it["title"] == it["label"] and "preview" in it and "price_from" in it

    # время печати из последнего расчёта — на полной странице, где «Модель 5» точно есть.
    # Это ВТОРОЕ открытие первой страницы — карточки конфигуратора нет (через раз).
    jall = json.loads((await webapp._api_feed(_Req({"cursor": "0", "limit": "30"}))).body.decode())
    assert not any(x.get("kind") == "gridfinity" for x in jall["items"])
    by_label = {x["label"]: x for x in jall["items"]}
    assert by_label["Модель 5"]["time_min"] == 90.0
    assert by_label["Модель 6"]["time_min"] == 0.0   # не считалась — времени нет

    # вторая страница не пересекается с первой; хвост ленты отдаёт next = null
    # (карточка конфигуратора на не-первые страницы не подмешивается)
    j2 = json.loads((await webapp._api_feed(_Req({"cursor": "5", "limit": "5"}))).body.decode())
    assert not any(x.get("kind") == "gridfinity" for x in j2["items"])
    assert not ({x["id"] for x in real} & {x["id"] for x in j2["items"]})
    j3 = json.loads((await webapp._api_feed(_Req({"cursor": "5", "limit": "50"}))).body.decode())
    assert j3["next"] is None

    # без авторизации — 403
    stub_auth(monkeypatch, _verify_init_data=lambda s: 0)
    resp = await webapp._api_feed(_Req({}))
    assert resp.status == 403
