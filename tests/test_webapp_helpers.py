"""Тесты веб-хелперов, включая критичную проверку подписи initData."""
import hashlib
import hmac
import json
import time
import urllib.parse

from app import config, db, downloader, webapp


def test_toint():
    assert webapp._toint("5") == 5
    assert webapp._toint("abc", 7) == 7
    assert webapp._toint(None, 0) == 0
    assert webapp._toint(3.9) == 3


def test_file_ver(tmp_path):
    f = tmp_path / "x.jpg"
    f.write_bytes(b"img")
    assert webapp._file_ver(f) > 0
    assert webapp._file_ver(tmp_path / "missing.jpg") == 0
    assert webapp._file_ver(None) == 0


def test_img_headers_versioned_vs_bare(get_req):
    versioned = webapp._img_headers(get_req({"v": "123"}))
    bare = webapp._img_headers(get_req())
    assert "immutable" in versioned["Cache-Control"]
    assert "31536000" in versioned["Cache-Control"]
    assert "max-age=300" in bare["Cache-Control"]
    assert versioned["Vary"] == "Accept"


def _forge(uid: int, token: str, tamper=False) -> str:
    user = {"id": uid, "first_name": "T"}
    params = {"user": json.dumps(user, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if tamper:
        params["user"] = json.dumps({"id": uid + 1, "first_name": "T"}, separators=(",", ":"))
    return urllib.parse.urlencode(params)


def test_verify_init_data_accepts_valid():
    uid = webapp._verify_init_data(_forge(424242, config.BOT_TOKEN))
    assert uid == 424242


def test_verify_init_data_rejects_tampered():
    assert not webapp._verify_init_data(_forge(424242, config.BOT_TOKEN, tamper=True))


def test_verify_init_data_rejects_garbage():
    assert not webapp._verify_init_data("not-valid-initdata")
    assert not webapp._verify_init_data("")


async def _async(value):
    return value


def _fake_order(rid=7):
    """Строка заказа (db.Request) для подмены db.list_requests — НЕ фейк HTTP-запроса:
    те приезжают фабриками post_req/get_req/admin_req из tests/conftest.py."""
    return db.Request(
        id=rid, user_id=111, user_name="Вася", model_label="Кубик", printer="P1",
        plastic_name="PLA", quantity=2, quality="0.2", strength="20%",
        grams_total=12.34, minutes_total=45.0, price=99.5, status="new",
        pending_price=None, preview_path=None, created_at=0, updated_at=0,
        price_comment="строка\nс переносом")


async def test_admin_orders_csv_returns_file(monkeypatch, admin_req):
    """Экспорт CSV: text/csv, UTF-8 BOM, заголовок и строка данных; ; как разделитель."""
    monkeypatch.setattr(db, "list_requests", lambda limit=0: _async([_fake_order()]))
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_orders_csv(admin_req(init_data=init))
    assert resp.status == 200
    assert resp.content_type == "text/csv"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert ".csv" in resp.headers["Content-Disposition"]
    text = resp.body.decode("utf-8")
    assert text.startswith("﻿")          # BOM для Excel
    assert "ID;Дата;Статус;Клиент;Модель;Материал" in text
    # Качество и прочность из выгрузки убраны (8c8199d1): клиент их не выбирает,
    # печать идёт профилем мастера — в отчёте это мёртвые колонки.
    assert "Кол-во;Граммы;Минуты;Цена;Ожид.цена;Комментарий" in text
    assert "Качество" not in text and "Прочность" not in text
    assert "Вася" in text and "Кубик" in text
    assert "строка с переносом" in text        # перенос строки схлопнут в пробел


async def test_admin_orders_csv_rejects_non_admin(admin_req):
    init = _forge(999999, config.BOT_TOKEN)     # валидная подпись, но не админ
    resp = await webapp._api_admin_orders_csv(admin_req(init_data=init))
    assert resp.status == 403


async def test_admin_order_delete_removes_order_and_file(tmp_db, tmp_path, admin_req):
    """POST /api/admin/order/delete: удаляет заказ и подчищает done_photo (зеркало a:rdelc)."""
    db_ = tmp_db
    rid = await db_.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=10, preview_path=None)
    photo = tmp_path / "done.jpg"
    photo.write_bytes(b"img")
    await db_.update_request(rid, done_photo=str(photo))

    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_order_delete(admin_req({"id": rid}, init_data=init))
    assert resp.status == 200
    assert await db_.get_request(rid) is None       # заказ удалён
    assert not photo.exists()                       # фото результата подчищено


async def test_admin_photo_order_saves_done_photo(tmp_db, tmp_path, monkeypatch, admin_req, body):
    """POST /api/admin/photo kind=order: пишет done_<id>.jpg и проставляет done_photo (зеркало a:rphoto)."""
    from pathlib import Path
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "coll")   # не трогаем прод-каталог
    db_ = tmp_db
    rid = await db_.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=10, preview_path=None)
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_photo(admin_req(
        {"kind": "order", "id": rid, "data": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="},
        init_data=init))
    assert resp.status == 200
    payload = body(resp)
    assert payload["ok"] and payload["url"]
    r = await db_.get_request(rid)
    assert r.done_photo and r.done_photo.endswith(f"done_{rid}.jpg")
    assert Path(r.done_photo).exists()


async def test_admin_photo_order_missing_request(tmp_db, admin_req):
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_photo(admin_req(
        {"kind": "order", "id": 999999, "data": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="},
        init_data=init))
    assert resp.status == 404


async def test_admin_order_delete_rejects_non_admin(tmp_db, admin_req):
    init = _forge(999999, config.BOT_TOKEN)          # валидная подпись, но не админ
    resp = await webapp._api_admin_order_delete(admin_req({"id": 1}, init_data=init))
    assert resp.status == 403


async def test_admin_order_delete_missing_order(tmp_db, admin_req):
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_order_delete(admin_req({"id": 999999}, init_data=init))
    assert resp.status == 404


async def test_api_idea_stores_and_accepts(tmp_db, post_req, body):
    """POST /api/idea: валидная идея сохраняется (бот не задан → без уведомления) и отвечает ok."""
    db_ = tmp_db
    init = _forge(424242, config.BOT_TOKEN)
    resp = await webapp._api_idea(post_req({"initData": init, "text": "Добавьте тёмную тему"}))
    assert resp.status == 200
    assert body(resp)["ok"] is True
    cur = await db_._conn().execute("SELECT COUNT(*) AS n, MAX(text) AS t FROM ideas")
    row = await cur.fetchone()
    assert row["n"] == 1 and "тёмную тему" in row["t"]


async def test_api_idea_rejects_short(tmp_db, post_req, body):
    init = _forge(424242, config.BOT_TOKEN)
    resp = await webapp._api_idea(post_req({"initData": init, "text": "ok"}))
    assert body(resp)["ok"] is False


async def test_api_idea_rejects_bad_auth(tmp_db, post_req):
    resp = await webapp._api_idea(post_req({"initData": "garbage", "text": "Длинная хорошая идея"}))
    assert resp.status == 403


async def test_api_admin_self_print_records_and_deducts(tmp_db, admin_req, body):
    """«Печать для себя»: пишется в ЕДИНЫЙ журнал печатей (source='self', Фаза B мастер-плана —
    self_prints объединён с print_journal) и списывает филамент материала; вне клиентских заказов."""
    db_ = tmp_db
    pid = await db_.add_plastic("PLA", 1000, 1.24, "PLA")
    await db_.update_plastic(pid, stock_grams=500, low_threshold=0)
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_self_print(admin_req(
        {"plastic_id": pid, "grams": 120, "minutes": 90, "label": "Кронштейн себе"},
        init_data=init))
    assert resp.status == 200 and body(resp)["ok"] is True
    cur = await db_._conn().execute(                               # запись в едином журнале (source='self')
        "SELECT COUNT(*) AS n FROM print_journal WHERE source='self' AND label='Кронштейн себе'")
    assert (await cur.fetchone())["n"] == 1
    assert (await db_.get_plastic(pid)).stock_grams == 380         # 500 - 120 списано
    assert len(await db_.list_requests(limit=0)) == 0              # в клиентских заказах НЕ появилось


async def test_api_admin_self_print_rejects_non_admin(tmp_db, admin_req):
    init = _forge(999999, config.BOT_TOKEN)
    resp = await webapp._api_admin_self_print(admin_req({"plastic_id": 1, "grams": 10},
                                                        init_data=init))
    assert resp.status == 403


async def test_admin_preset_edit_strength(tmp_db, admin_req, body):
    """POST /api/admin/preset kind=strength action=edit меняет label/infill/walls существующего пресета."""
    db_ = tmp_db
    sid = await db_.add_strength("Старое", 15, 2)
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_preset(admin_req(
        {"kind": "strength", "action": "edit", "id": sid,
         "label": "Новое", "infill": 55, "walls": 4}, init_data=init))
    assert resp.status == 200
    assert body(resp)["ok"] is True
    s = await db_.get_strength(sid)
    assert s.label == "Новое" and s.infill == 55 and s.walls == 4


async def test_admin_preset_edit_strength_missing(tmp_db, admin_req, body):
    """Редактирование несуществующего пресета прочности → ok:false (устаревшая клавиатура)."""
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_preset(admin_req(
        {"kind": "strength", "action": "edit", "id": 999999,
         "label": "x", "infill": 10, "walls": 1}, init_data=init))
    payload = body(resp)
    assert payload["ok"] is False and "не найден" in payload["error"].lower()


async def test_admin_preset_edit_quality(tmp_db, admin_req):
    """POST /api/admin/preset kind=quality action=edit меняет label/layer/process существующего пресета."""
    db_ = tmp_db
    qid = await db_.add_quality("Старое", 0.2, "old.json")
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_preset(admin_req(
        {"kind": "quality", "action": "edit", "id": qid,
         "label": "Тонкое", "layer": "0.12", "process": "fine.json"}, init_data=init))
    assert resp.status == 200
    q = await db_.get_quality(qid)
    assert q.label == "Тонкое" and q.layer_height == 0.12 and q.process == "fine.json"


async def test_admin_preset_edit_rejects_non_admin(tmp_db, admin_req):
    init = _forge(999999, config.BOT_TOKEN)          # валидная подпись, но не админ
    resp = await webapp._api_admin_preset(admin_req(
        {"kind": "strength", "action": "edit", "id": 1,
         "label": "x", "infill": 10, "walls": 1}, init_data=init))
    assert resp.status == 403


async def test_admin_order_open_slicer_rejects_non_admin(tmp_db, admin_req):
    init = _forge(999999, config.BOT_TOKEN)          # валидная подпись, но не админ
    resp = await webapp._api_admin_order_open_slicer(admin_req({"id": 1}, init_data=init))
    assert resp.status == 403


async def test_admin_order_open_slicer_missing_file(tmp_db, admin_req, body):
    """Нет файла на диске → ok:false с понятным текстом (зеркало a:rslice)."""
    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_order_open_slicer(admin_req({"id": 999999}, init_data=init))
    payload = body(resp)
    assert payload["ok"] is False
    assert "не найден" in payload["error"].lower()


async def test_admin_order_open_slicer_launches_local(tmp_db, tmp_path, monkeypatch, admin_req, body):
    """Файл есть, SLICER_PATH нет → фолбэк os.startfile; запуск не делаем (мок), ждём ok:true+note."""
    from pathlib import Path
    db_ = tmp_db
    model = tmp_path / "job.stl"          # не .3mf → ветку пересохранения с пресетом пропускаем
    model.write_bytes(b"solid")
    rid = await db_.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=10, preview_path=None, file_path=str(model))
    monkeypatch.setattr(config, "SLICER_PATH", tmp_path / "nope.exe")   # форсим ветку os.startfile
    opened = {}
    monkeypatch.setattr(webapp.os, "startfile", lambda p: opened.setdefault("path", p), raising=False)

    init = _forge(config.ADMIN_IDS[0], config.BOT_TOKEN)
    resp = await webapp._api_admin_order_open_slicer(admin_req({"id": rid}, init_data=init))
    payload = body(resp)
    assert payload["ok"] is True and payload["note"]
    assert opened.get("path") == str(model)        # открыли именно файл заказа, без запуска слайсера
    assert Path(model).exists()


async def test_attach_done_to_collection(tmp_db, tmp_path, monkeypatch):
    """OpenTodo 3fe0b3fa: фото готовой печати привязывается к карточке коллекции по хэшу
    модели; копируется в собственный путь col_<cid>.jpg; нет карточки → False."""
    db_ = tmp_db
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path)
    cid = await db_.add_to_collection(hash="HX", label="m", paths=["a.stl"],
                                      preview_path=None, added_by=1)
    rid = await db_.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None, hash="HX")
    photo = tmp_path / "done.jpg"
    photo.write_bytes(b"JPEGDATA")
    assert await webapp._attach_done_to_collection(rid, photo) is True
    item = await db_.get_collection_item(cid)
    assert item.photo_path == str(tmp_path / f"col_{cid}.jpg")
    assert (tmp_path / f"col_{cid}.jpg").read_bytes() == b"JPEGDATA"   # своя копия, не путь заказа

    rid2 = await db_.add_request(
        user_id=1, user_name="t", model_label="m2", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None, hash="NONE")
    assert await webapp._attach_done_to_collection(rid2, photo) is False


async def test_active_print_header(tmp_db):
    """OpenTodo 09e9ee22: шапка принтера берёт данные заказа активной печати; закрытый → None."""
    db_ = tmp_db
    rid = await db_.add_request(
        user_id=1, user_name="t", model_label="Дракон", printer="p", plastic_name="PLA · красный",
        quantity=1, quality="q", strength="s", grams_total=42.4, minutes_total=120,
        price=9, preview_path=None)
    webapp._active_print.clear()
    assert await webapp._active_print_header() is None          # нет активной печати
    webapp._set_active_print(rid, await webapp._active_print_key())
    j = await webapp._active_print_header()
    assert j["order_id"] == rid and j["label"] == "Дракон" and j["plastic"] == "PLA · красный"
    assert j["grams"] == 42.4 and j["thumb"] == ""             # превью нет → пустой thumb
    await db_.update_request(rid, status="printed")            # заказ закрыт → шапки нет
    assert await webapp._active_print_header() is None
    webapp._active_print.clear()                               # вернуть глобал в исходное


def test_blocked_payload_offers_printables_search():
    """Блок MakerWorld → ответ с blocked=True и search (имя модели из слага для поиска на Printables)."""
    p = webapp._blocked_payload(downloader.MakerWorldBlocked("заблокировано"),
                                "https://makerworld.com/models/2413543-donald-duck-urban")
    assert p["ok"] is False and p["blocked"] is True and "заблокировано" in p["error"]
    assert "duck" in p["search"].lower()


def test_thumb_proxy_signs_external_only():
    """OpenTodo 27f25b91: внешний URL превью → подписанный /api/thumb; свои/пустые не трогаем."""
    assert webapp._thumb_proxy("") == ""
    assert webapp._thumb_proxy("/img/5") == "/img/5"        # уже свой origin — без прокси
    u = "https://media.printables.com/x.png"
    p = webapp._thumb_proxy(u)
    assert p.startswith("/api/thumb?u=")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(p).query)
    assert qs["s"][0] == webapp._sign(u) and qs["u"][0] == u
