"""Снимки смет, которые видел клиент (26.08).

Лента событий хранила только строку текста, а сама смета жила в памяти процесса и умирала с
рестартом бота — «рассчитал цену» в ленте было нечем открыть. Здесь проверяется вся цепочка:
снимок пишется → событие несёт на него ссылку → админская ручка отдаёт ровно то, что видел
клиент → фоновый слайс дописывает уточнённую цену, не затирая показанную.
"""
import pytest

from app import webapp
from app.store import crm as store_crm
from app.web import clientflow
from tests.conftest import stub_auth

ADMIN_ID = 1000
UID = 4242


@pytest.fixture
def admin(monkeypatch):
    stub_auth(monkeypatch, _verify_admin=ADMIN_ID)
    return ADMIN_ID


async def _snap(db, **kw):
    """Снимок с осмысленными дефолтами — тесту важны только те поля, что он проверяет."""
    fields = {"action": "рассчитал цену", "source": "estimate", "model_label": "Ваза",
              "printer": "CC2", "plastic_name": "PLA Matte", "color": "чёрный",
              "quality": "0.20 мм", "strength": "Золотая середина", "qty": 1,
              "grams_total": 213.1, "minutes_total": 576.0, "total": 21.7,
              "total_before": 21.7, "discount_pct": 0.0, "approx": True,
              "payload": {"lines": [["Филамент (213.1 г)", "9,60 BYN"]], "dims": "120×80×45 мм",
                          "plates": 1, "verdict": "ok", "queue": {"count": 3}}}
    fields.update(kw)
    return await db.add_calc_snapshot(UID, **fields)


# ───────── хранение ─────────

async def test_snapshot_roundtrip_and_event_link(tmp_db):
    db = tmp_db
    cid = await _snap(db)
    assert cid
    await db.log_event(UID, "рассчитал цену", "Ваза · PLA Matte ×1 · ≈оценка", calc_id=cid)

    ev = (await db.list_activity())[0]
    assert ev["calc_id"] == cid          # плитка ленты знает, какую смету открывать

    c = await db.get_calc_snapshot(cid)
    assert c["total"] == 21.7 and c["approx"] == 1 and c["source"] == "estimate"
    assert c["payload"]["lines"][0][0].startswith("Филамент")   # JSON развернулся обратно
    assert c["payload"]["dims"] == "120×80×45 мм"
    assert await db.get_calc_snapshot(999999) is None


async def test_event_without_calc_keeps_none(tmp_db):
    """Событие не про цену ссылки не несёт — фронт по-прежнему открывает карточку клиента."""
    db = tmp_db
    await db.log_event(UID, "открыл бота", "", user_name="Антон")
    assert (await db.list_activity())[0]["calc_id"] is None


async def test_refined_price_kept_beside_shown(tmp_db):
    """Фоновый слайс уточнил цену — показанную клиенту НЕ затираем: разница и есть ответ на
    вопрос «нормальную ли цену назвали»."""
    db = tmp_db
    cid = await _snap(db)
    await db.mark_calc_refined(cid, total=32.5, grams=240.4, minutes=662.0)
    c = await db.get_calc_snapshot(cid)
    assert c["total"] == 21.7 and c["refined_total"] == 32.5
    assert c["refined_grams"] == 240.4 and c["refined_at"] > 0
    await db.mark_calc_refined(None, total=1, grams=1, minutes=1)     # без id — молча ничего


async def test_snapshot_trim_keeps_last(tmp_db, monkeypatch):
    """Снимки чистятся своей обрезкой, как и лента: старые вытесняются, свежие живут."""
    db = tmp_db
    monkeypatch.setattr(store_crm, "_CALC_KEEP", 2)
    ids = [await _snap(db, model_label=f"М{i}") for i in range(4)]
    alive = [i for i in ids if await db.get_calc_snapshot(i)]
    assert alive == ids[-2:]


async def test_huge_payload_never_stored_broken(tmp_db, monkeypatch):
    """Жирный payload не режем посимвольно: обрезанный JSON не разобрался бы вовсе."""
    db = tmp_db
    monkeypatch.setattr(store_crm, "_CALC_PAYLOAD_MAX", 300)
    cid = await _snap(db, payload={"lines": [["Филамент", "9,60 BYN"]], "dfm": ["x" * 500]})
    c = await db.get_calc_snapshot(cid)
    assert c["payload"] == {"lines": [["Филамент", "9,60 BYN"]]}   # разбивка уцелела
    cid2 = await _snap(db, payload={"lines": [["y" * 500, "1"]]})
    assert (await db.get_calc_snapshot(cid2))["payload"] == {}      # не влезло — честно пусто


async def test_next_request_after_calc(tmp_db):
    """«Согласился ли клиент с ценой» — по первому заказу в сутки после расчёта, без догадок."""
    db = tmp_db
    cid = await _snap(db)
    c = await db.get_calc_snapshot(cid)
    assert await db.calc_next_request(UID, c["created_at"], "Ваза") is None
    rid = await db.add_request(
        user_id=UID, user_name="Антон", model_label="Ваза", printer="CC2", plastic_name="PLA",
        quantity=1, quality="0.2", strength="норм", grams_total=213, minutes_total=576,
        price=21.7, preview_path=None)
    nxt = await db.calc_next_request(UID, c["created_at"], "Ваза")
    assert nxt["id"] == rid and nxt["same_model"] is True and nxt["minutes_after"] == 0
    # заказ другого клиента к этому расчёту отношения не имеет
    assert await db.calc_next_request(UID + 1, c["created_at"], "Ваза") is None


# ───────── путь записи из расчёта ─────────

class _BD:
    """Минимальный PriceBreakdown: помощнику нужны только цифры."""
    grams_total, minutes_total, total, discount_amount, discount_pct = 213.1, 576.0, 21.7, 0.0, 0.0


class _Named:
    def __init__(self, name):
        self.name = self.label = name


async def test_snap_calc_writes_snapshot_and_event(tmp_db):
    """_snap_calc — единственная точка, где расчёт оставляет след: снимок + событие со ссылкой."""
    db = tmp_db
    m = {"label": "Ваза", "pdf": {"bd": _BD()}}
    payload = {"total": "21,70 BYN", "approx": True, "lines": [["Филамент", "9,60 BYN"]],
               "queue": {"count": 3}, "cpreview": "/api/cpreview/tok"}
    cid = await clientflow._snap_calc(
        UID, "рассчитал цену", "Ваза · PLA ×1 · ≈оценка", source="estimate", m=m, payload=payload,
        printer=_Named("CC2"), plastic=_Named("PLA"), quality=_Named("0.20 мм"),
        strength=_Named("норм"), color="чёрный", qty=1)
    assert cid and m["calc_snap_id"] == cid      # фоновый слайс допишет уточнение сюда

    ev = (await db.list_activity())[0]
    assert ev["action"] == "рассчитал цену" and ev["calc_id"] == cid

    c = await db.get_calc_snapshot(cid)
    assert c["total"] == 21.7 and c["grams_total"] == 213.1 and c["plastic_name"] == "PLA"
    assert c["color"] == "чёрный" and c["approx"] == 1
    # живая очередь и ссылка на превью по токену протухнут — в снимок не идут
    assert "queue" not in c["payload"] and "cpreview" not in c["payload"]
    assert c["payload"]["lines"] == [["Филамент", "9,60 BYN"]]


# ───────── админская ручка ─────────

async def test_api_admin_calc_returns_what_client_saw(tmp_db, admin, admin_req, body):
    db = tmp_db
    cid = await _snap(db, discount_pct=10.0, total=21.7, total_before=24.1, qty=2)
    await db.mark_calc_refined(cid, total=32.5, grams=240.4, minutes=662.0)

    out = body(await webapp._api_admin_calc(admin_req(query={"id": str(cid)})))
    assert out["ok"] is True
    c = out["calc"]
    assert c["model_label"] == "Ваза" and c["source_label"] == "≈оценка по геометрии"
    assert c["lines"] and c["dims"] == "120×80×45 мм"
    assert c["discount_pct"] == 10.0 and c["total_before_label"]
    assert c["each_label"]                       # тираж 2 → цена за штуку показывается
    assert c["refined"]["delta_pct"] == 50       # 21.7 → 32.5
    assert "queue" not in c


async def test_api_admin_calc_missing_snapshot(tmp_db, admin, admin_req, body):
    """Снимок вытеснен обрезкой — говорим честно, фронт откатится на карточку клиента."""
    out = body(await webapp._api_admin_calc(admin_req(query={"id": "999999"})))
    assert out["ok"] is False and "не сохранил" in out["error"]


async def test_api_admin_calc_needs_admin(tmp_db, monkeypatch, admin_req, body):
    stub_auth(monkeypatch, _verify_admin=lambda init: None)
    resp = await webapp._api_admin_calc(admin_req(query={"id": "1"}))
    assert body(resp)["ok"] is False
