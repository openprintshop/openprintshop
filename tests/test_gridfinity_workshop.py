"""«Мастерская органайзеров»: проекты, спецификация деталей и частичный заказ.

Здесь проверяется не оформление, а те правила, из-за которых конфигуратор вообще
переставали быть одноразовым черновиком:

  * СВЕРКА раскладки со спецификацией (`_gf_reconcile`) — правда по геометрии лежит в
    JSON движка, детали её проекция. Ошибка здесь стоит клиенту денег: лишняя деталь в
    партии — это лишний напечатанный лоток.
  * ЗАМОРОЗКА заказанного: пока заявку не взяли в работу, правка проходит насквозь;
    после — модуль форкается в v2, а напечатанная версия остаётся в истории.
  * ЧАСТИЧНЫЙ ЗАКАЗ: в заявку уезжают ТОЛЬКО выбранные детали, и заявка описывает именно
    их — иначе мастер напечатает лишнее.
  * НЕУДАЛЯЕМОСТЬ заказанного: заявка не должна ссылаться в пустоту.
"""
import json
import math

import pytest
from conftest import stub_auth

from app import ratelimit, webapp
from app.web import gridfinity as gfx

BASE = {"type": "drawer", "W": 480, "D": 270, "H": 120, "series": "M",
        "fitX": "sym", "fitY": "sym"}


def _mod(mid, cx=0, cy=0, w=2, d=2, hmm=46.85, color=0, gen="tray", vol=30.0):
    return {"id": mid, "kind": "bin", "gen": gen, "cx": cx, "cy": cy, "w": w, "d": d,
            "hmm": hmm, "colorIdx": color, "vol": vol,
            "ext": {"L": 0, "R": 0, "B": 0, "F": 0}}


def _cfg(*mods):
    return {"v": 4, "base": dict(BASE), "modules": list(mods), "seq": 99}


class _Req:
    def __init__(self, query=None, match_info=None):
        self.query = query or {"initData": "x"}
        self.match_info = match_info or {}


class _Body:
    """Поток тела запроса: ручки читают его САМИ с потолком (заголовку Content-Length
    доверять нельзя — его можно не прислать вовсе)."""

    def __init__(self, raw):
        self._raw = raw

    async def read(self, n=-1):
        return self._raw if n is None or n < 0 else self._raw[:n]


class _Post:
    def __init__(self, body, match_info=None):
        self._body = body
        self.match_info = match_info or {}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.content_length = len(raw)
        self.content = _Body(raw)

    async def json(self):
        return self._body


async def _json(resp):
    return json.loads(resp.body.decode())


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """Счётчики ratelimit и минутные кэши живут в памяти ПРОЦЕССА, а БД у каждого теста своя.
    Без сброса десятый тест подряд упирается в лимит создания проектов, а оценка приезжает
    с прайс-листом от чужой базы."""
    for d in (ratelimit._hits, gfx._price_cache, gfx._bed_cache, gfx._disc_cache):
        d.clear()
    yield
    for d in (ratelimit._hits, gfx._price_cache, gfx._bed_cache, gfx._disc_cache):
        d.clear()


@pytest.fixture
def auth(monkeypatch):
    stub_auth(monkeypatch, _verify_init_data=lambda s: 7,
              _verify_user=lambda s: {"id": 7, "username": "u7"},
              _verify_admin=lambda s: 1)


async def _new_project(cfg=None, title="Кухня, нижний ящик"):
    j = await _json(await webapp._api_gf_project_add(
        _Post({"initData": "x", "title": title, "config": cfg or _cfg()})))
    assert j["ok"] is True
    return j["id"]


async def _save(pid, cfg):
    return await _json(await webapp._api_gf_project_save(
        _Post({"initData": "x", "config": cfg}, {"pid": str(pid)})))


async def _get(pid):
    return await _json(await webapp._api_gf_project(_Req(match_info={"pid": str(pid)})))


# ─────────────────────────── проекты ───────────────────────────

async def test_project_create_list_and_duplicate(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1), _mod(2, cx=2)))
    j = await _json(await webapp._api_gf_projects(_Req()))
    assert j["ok"] is True and len(j["projects"]) == 1
    card = j["projects"][0]
    # карточка несёт ПЛАН, а не саму раскладку: 20 проектов по 256 КБ в ленту не влезут
    assert card["preview"]["cols"] == 11 and len(card["preview"]["mods"]) == 2
    assert "config" not in card
    assert card["counts"]["total"] == 2 and card["counts"]["draft"] == 2

    # дублирование — главное действие мастерской; детали в копию НЕ едут заказанными
    j = await _json(await webapp._api_gf_project_add(_Post({"initData": "x", "from_id": pid})))
    copy = await _get(j["id"])
    assert copy["project"]["title"].endswith("— копия")
    assert len(copy["parts"]) == 2 and all(p["state"] == "draft" for p in copy["parts"])


async def test_project_limit_and_measures(tmp_db, auth, monkeypatch):
    monkeypatch.setattr(gfx, "_GF_MAX_PROJECTS", 2)
    await _new_project(title="1")
    await _new_project(title="2")
    j = await _json(await webapp._api_gf_project_add(_Post({"initData": "x", "title": "3"})))
    assert j["ok"] is False and "архивируйте" in j["error"]

    # мерки: новый проект стартует без рулетки
    j = await _json(await webapp._api_gf_measure_add(_Post(
        {"initData": "x", "title": "Кухня 1", "type": "drawer", "W": 600, "D": 400, "H": 80})))
    assert j["ok"] is True and j["measures"][0]["title"] == "Кухня 1"
    j = await _json(await webapp._api_gf_measure_add(_Post(
        {"initData": "x", "title": "мусор", "W": 5, "D": 5, "H": 5})))
    assert j["ok"] is False and "замеры" in j["error"]


# ─────────────────────────── сверка спецификации ───────────────────────────

async def test_reconcile_adds_drops_and_keeps_ordered(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1), _mod(2, cx=2)))
    got = await _get(pid)
    assert len(got["parts"]) == 2
    assert got["parts"][0]["name"] == "Бокс 2×2 · лоток · 47 мм"   # авто-имя
    assert got["parts"][0]["units"] == 6                            # H(u) = 7u + 4.85

    # модуль добавили — деталь появилась; убрали черновой — деталь исчезла
    j = await _save(pid, _cfg(_mod(1), _mod(2, cx=2), _mod(3, cx=4)))
    assert j["diff"]["added"] == 1
    j = await _save(pid, _cfg(_mod(1), _mod(3, cx=4)))
    assert j["diff"]["dropped"] == 0
    got = await _get(pid)
    assert {p["mod_id"] for p in got["parts"]} == {1, 3}


async def test_color_change_does_not_fork_but_geometry_does(tmp_db, auth):
    """Перекраска не должна форкать напечатанную деталь: цвет заказанной версии заморожен."""
    pid = await _new_project(_cfg(_mod(1)))
    before = (await _get(pid))["parts"][0]["id"]
    await _save(pid, _cfg(_mod(1, color=3)))
    got = await _get(pid)
    assert len(got["parts"]) == 1 and got["parts"][0]["id"] == before
    assert got["parts"][0]["color"] == "лайм"


# ─────────────────────────── детали ───────────────────────────

async def test_part_patch_name_note_qty_and_hold(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1), _mod(2, cx=2)))
    part = (await _get(pid))["parts"][0]
    j = await _json(await webapp._api_gf_part_patch(_Post(
        {"initData": "x", "name": "Под лопатки", "note": "лопатки, венчик", "qty": 3},
        {"id": str(part["id"])})))
    p = [x for x in j["parts"] if x["id"] == part["id"]][0]
    assert p["name"] == "Под лопатки" and p["qty"] == 3 and p["custom_name"] is True
    assert p["note"] == "лопатки, венчик"

    # «отложено» снимает деталь с партии и не попадает в «выбрать всё»
    await webapp._api_gf_part_patch(_Post({"initData": "x", "sel": 1}, {"id": str(part["id"])}))
    j = await _json(await webapp._api_gf_part_patch(
        _Post({"initData": "x", "state": "hold"}, {"id": str(part["id"])})))
    p = [x for x in j["parts"] if x["id"] == part["id"]][0]
    assert p["state"] == "hold" and p["sel"] == 0


async def test_group_select_only_touches_own_project(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1), _mod(2, cx=2)))
    other = await _new_project(_cfg(_mod(1)), title="Чужой")
    alien = (await _get(other))["parts"][0]["id"]
    ids = [p["id"] for p in (await _get(pid))["parts"]] + [alien]
    j = await _json(await webapp._api_gf_parts_select(
        _Post({"initData": "x", "ids": ids, "sel": 1}, {"pid": str(pid)})))
    assert j["counts"]["sel"] == 2
    assert (await _get(other))["parts"][0]["sel"] == 0   # чужая деталь не тронута


# ─────────────────────────── партия ───────────────────────────

async def _order(pid, ids, comment=""):
    return await _json(await webapp._api_gf_order(
        _Post({"initData": "x", "ids": ids, "comment": comment}, {"pid": str(pid)})))


async def test_partial_order_takes_only_selected_parts(tmp_db, auth):
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1), _mod(2, cx=2, hmm=67.5), _mod(3, cx=4)))
    parts = (await _get(pid))["parts"]
    take = [parts[0]["id"], parts[1]["id"]]
    j = await _order(pid, take, comment="к пятнице")
    assert j["ok"] is True and j["n"] == 2
    rid = j["order_id"]

    got = await _get(pid)
    by_id = {p["id"]: p for p in got["parts"]}
    assert by_id[take[0]]["state"] == "ordered" and by_id[take[0]]["request_id"] == rid
    assert by_id[parts[2]["id"]]["state"] == "draft"      # третья осталась черновиком
    assert by_id[take[0]]["frozen"] == 0                  # заявка ещё не в работе

    # заявка описывает ТОЛЬКО заказанные детали
    r = await db.get_request(rid)
    assert r.is_custom == 1 and "партия 1" in r.description
    assert "В партии деталей: 2" in r.description and "к пятнице" in r.description
    # снимок партии несёт и модули, и человеческую спецификацию
    snap = json.loads(await db.gridfinity_order_config_get(rid))
    assert len(snap["modules"]) == 2 and len(snap["parts"]) == 2
    assert snap["parts"][0]["h_mm"] > 0 and snap["title"]

    # вторая партия — «партия 2», и она видна в проекте
    j2 = await _order(pid, [parts[2]["id"]])
    assert j2["ok"] is True
    got = await _get(pid)
    assert len(got["batches"]) == 2
    assert got["batches"][1]["n"] == 1
    r2 = await db.get_request(j2["order_id"])
    assert "партия 2" in r2.description
    # партии НЕ склеиваются в один заказ-корзину: это разные заказы с разной доставкой
    assert not (r.order_group or "") and not (r2.order_group or "")


async def test_order_needs_a_selection(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1)))
    j = await _order(pid, [])
    assert j["ok"] is False and "хотя бы одну" in j["error"]


async def test_preflight_blocks_part_taller_than_the_drawer(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1, hmm=200.0)))
    part = (await _get(pid))["parts"][0]
    j = await _json(await webapp._api_gf_preflight(
        _Req({"initData": "x", "ids": str(part["id"])}, {"pid": str(pid)})))
    assert j["blocked"] == 1
    assert any("выше полезной высоты" in c["text"] for c in j["checks"])
    # оформление с блокирующей проверкой не проходит
    j = await _order(pid, [part["id"]])
    assert j["ok"] is False and "выше полезной высоты" in j["error"]


async def test_estimate_is_positive_and_scales_with_qty(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1, vol=40.0)))
    part = (await _get(pid))["parts"][0]
    assert part["vol_cm3"] == 40.0 and part["grams"] > 0 and part["minutes"] > 0
    j = await _json(await webapp._api_gf_part_patch(
        _Post({"initData": "x", "qty": 2}, {"id": str(part["id"])})))
    two = [x for x in j["parts"] if x["id"] == part["id"]][0]
    assert two["grams"] > part["grams"] and two["price"] >= part["price"]


# ───────────────── правка заказанного: сквозная и форк ─────────────────

async def test_change_passes_through_while_order_is_fresh(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1)))
    part = (await _get(pid))["parts"][0]
    await _order(pid, [part["id"]])
    j = await _save(pid, _cfg(_mod(1, hmm=67.5)))
    assert j["diff"]["forked"] == 0 and j["diff"]["changed"] == 1
    got = await _get(pid)
    assert len(got["parts"]) == 1
    assert got["parts"][0]["changed"] == 1 and got["parts"][0]["h_mm"] == 67.5


async def test_change_after_work_started_forks_into_v2(tmp_db, auth):
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1)))
    part = (await _get(pid))["parts"][0]
    j = await _order(pid, [part["id"]])
    await db.update_request(j["order_id"], status="in_progress")

    r = await _save(pid, _cfg(_mod(1, hmm=67.5)))
    assert r["diff"]["forked"] == 1
    got = await _get(pid)
    old = [p for p in got["parts"] if p["id"] == part["id"]][0]
    new = [p for p in got["parts"] if p["id"] != part["id"]][0]
    # v1 заморожена, осталась в истории со своей заявкой и снята с раскладки
    assert old["state"] == "ordered" and old["frozen"] == 1 and old["on_plan"] == 0
    assert old["live_state"] == "in_progress"
    # v2 — обычный черновик в той же ячейке, с наследованным именем и версией 2
    assert new["version"] == 2 and new["parent_id"] == part["id"]
    assert new["state"] == "draft" and new["mod_id"] == 1 and new["h_mm"] == 67.5


async def test_ordered_part_cannot_be_edited_or_deleted(tmp_db, auth):
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1)))
    part = (await _get(pid))["parts"][0]
    j = await _order(pid, [part["id"]])
    # количество заказанной детали не меняется — оно уже уехало мастеру
    resp = await _json(await webapp._api_gf_part_patch(
        _Post({"initData": "x", "qty": 5}, {"id": str(part["id"])})))
    assert resp["ok"] is False and "новую версию" in resp["error"]

    # модуль удалили из раскладки — деталь не удаляется, она снимается с плана
    await db.update_request(j["order_id"], status="in_progress")
    await _save(pid, _cfg())
    got = await _get(pid)
    assert len(got["parts"]) == 1 and got["parts"][0]["on_plan"] == 0
    assert got["parts"][0]["state"] == "ordered"

    # и сам проект теперь не удаляется, а уходит в архив
    j = await _json(await webapp._api_gf_project_patch(
        _Post({"initData": "x", "delete": 1}, {"pid": str(pid)})))
    assert j["ok"] is True and j.get("archived") == 1
    assert await db.gf_project(pid) is not None


async def test_dead_request_frees_the_part_by_itself(tmp_db, auth):
    """Отклонённая заявка отпускает деталь при первом же чтении — без уборки за мастером."""
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1)))
    part = (await _get(pid))["parts"][0]
    j = await _order(pid, [part["id"]])
    await db.update_request(j["order_id"], status="rejected")
    got = (await _get(pid))["parts"][0]
    assert got["state"] == "draft" and got["request_id"] == 0


async def test_dead_request_releases_parts(tmp_db, auth):
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1)))
    part = (await _get(pid))["parts"][0]
    j = await _order(pid, [part["id"]])
    await db.update_request(j["order_id"], status="rejected")
    # показываемое состояние — снова черновик: заявка деталь больше не держит
    assert (await _get(pid))["parts"][0]["live_state"] == "draft"
    # и деталь снова МОЖНО выбрать в партию, не дожидаясь уборки за мастером: иначе
    # единственным способом перезаказать отклонённое было бы нарисовать модуль заново
    sel = await _json(await webapp._api_gf_parts_select(
        _Post({"initData": "x", "ids": [part["id"]], "sel": 1}, {"pid": str(pid)})))
    assert sel["counts"]["sel"] == 1
    o2 = await _order(pid, [part["id"]])
    assert o2["ok"] is True and o2["order_id"] != j["order_id"]
    j = {"order_id": o2["order_id"]}
    await db.update_request(o2["order_id"], status="rejected")
    r = await _json(await webapp._api_admin_gf_release(
        _Post({"initData": "a"}, {"rid": str(j["order_id"])})))
    assert r["released"] == 1
    got = await _get(pid)
    assert got["parts"][0]["state"] == "draft" and got["parts"][0]["request_id"] == 0


# ─────────────────────────── мастер ───────────────────────────

async def test_admin_snapshot_carries_print_sheet_and_marks_printed(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1), _mod(2, cx=2)))
    parts = (await _get(pid))["parts"]
    j = await _order(pid, [p["id"] for p in parts])
    rid = j["order_id"]

    snap = await _json(await webapp._api_admin_gridfinity_config(
        _Req({"initData": "a"}, {"rid": str(rid)})))
    assert snap["ok"] is True and len(snap["parts"]) == 2
    assert snap["preview"]["cols"] == 11 and "Сетка" in snap["summary"]
    assert snap["parts"][0]["w_mm"] > 0 and snap["parts"][0]["color_hex"].startswith("#")

    r = await _json(await webapp._api_admin_gf_part_printed(
        _Post({"initData": "a", "printed": True}, {"id": str(parts[0]["id"])})))
    assert r["ok"] is True
    # повторный клик по галочке не шлёт клиенту второе «деталь готова»
    again = await _json(await webapp._api_admin_gf_part_printed(
        _Post({"initData": "a", "printed": True}, {"id": str(parts[0]["id"])})))
    assert again.get("same") == 1
    got = await _get(pid)
    done = [p for p in got["parts"] if p["id"] == parts[0]["id"]][0]
    assert done["state"] == "printed" and done["printed_at"] > 0
    assert got["batches"][0]["printed"] == 1


# ─────────────────────────── ссылка «показать» и шаблон ───────────────────────────

async def test_share_link_renders_read_only_page(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1), _mod(2, cx=2)))
    j = await _json(await webapp._api_gf_project_patch(
        _Post({"initData": "x", "share": 1}, {"pid": str(pid)})))
    token = j["share"]
    assert token
    resp = await webapp._gf_share_page(_Req(match_info={"token": token}))
    assert resp.status == 200 and "Кухня" in resp.text and "grid-column" in resp.text
    # ссылку можно отозвать
    await webapp._api_gf_project_patch(_Post({"initData": "x", "share": 0}, {"pid": str(pid)}))
    resp = await webapp._gf_share_page(_Req(match_info={"token": token}))
    assert resp.status == 404


async def test_bottom_template_pdf(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1)))
    resp = await webapp._api_gf_template(_Req(match_info={"pid": str(pid)}))
    assert resp.content_type == "application/pdf" and resp.body[:4] == b"%PDF"


def test_template_tiles_cover_the_whole_bottom():
    """Разрезка на A4 не должна терять полосу ящика.

    Шаг разрезки и полезное окно листа считаются в разных местах, и стоит им разъехаться —
    клиент склеит шаблон с дырой и ничего не заметит, пока не приложит его к ящику."""
    from app import gfxpdf
    assert gfxpdf.TILE_W_MM <= gfxpdf.WIN_W_MM - gfxpdf.OVERLAP_MM + 1e-9
    assert gfxpdf.TILE_H_MM <= gfxpdf.WIN_H_MM - gfxpdf.OVERLAP_MM + 1e-9
    for w, d in ((480, 380), (352, 224), (1200, 600)):
        cols = math.ceil(w / gfxpdf.TILE_W_MM)
        rows = math.ceil(d / gfxpdf.TILE_H_MM)
        assert cols * gfxpdf.TILE_W_MM >= w and rows * gfxpdf.TILE_H_MM >= d
        pdf = gfxpdf.template_pdf("Тест", {"W": w, "D": d}, gfx._gf_grid({"W": w, "D": d}))
        assert pdf[:4] == b"%PDF" and pdf.count(b"/Contents") == cols * rows


# ─────────────────────────── чужое не отдаём ───────────────────────────

async def test_foreign_project_is_not_readable(tmp_db, monkeypatch):
    stub_auth(monkeypatch, _verify_init_data=lambda s: 7,
              _verify_user=lambda s: {"id": 7, "username": "u7"})
    pid = await _new_project(_cfg(_mod(1)))
    stub_auth(monkeypatch, _verify_init_data=lambda s: 8)
    resp = await webapp._api_gf_project(_Req(match_info={"pid": str(pid)}))
    assert resp.status == 404
    resp = await webapp._api_gf_project_save(
        _Post({"initData": "x", "config": _cfg()}, {"pid": str(pid)}))
    assert resp.status == 404


async def test_versions_snapshot_and_restore(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1)))
    await webapp._api_gf_project_patch(_Post({"initData": "x", "snapshot": 1, "note": "до правок"},
                                             {"pid": str(pid)}))
    await _save(pid, _cfg(_mod(1), _mod(2, cx=2)))
    got = await _get(pid)
    assert len(got["parts"]) == 2 and len(got["versions"]) == 1
    await webapp._api_gf_project_patch(
        _Post({"initData": "x", "restore": got["versions"][0]["id"]}, {"pid": str(pid)}))
    back = await _get(pid)
    assert len(back["parts"]) == 1 and len(back["versions"]) == 2   # откат сам стал снимком


async def test_old_draft_becomes_a_project_on_first_visit(tmp_db, auth):
    """Переход на проекты не должен стоить людям их незаказанных раскладок."""
    db = tmp_db
    cfg = _cfg(_mod(1), _mod(2, cx=2))
    await db.gridfinity_config_set(7, json.dumps(cfg, ensure_ascii=False))
    j = await _json(await webapp._api_gf_projects(_Req()))
    assert j["ok"] is True and len(j["projects"]) == 1
    assert j["projects"][0]["title"] == "Мой органайзер"
    assert j["projects"][0]["counts"]["total"] == 2      # спецификация собрана сразу
    # повторный заход второго проекта не плодит
    j2 = await _json(await webapp._api_gf_projects(_Req()))
    assert len(j2["projects"]) == 1


async def test_empty_draft_is_not_migrated(tmp_db, auth):
    """Пустой черновик заворачивать не во что — мастерская должна открыться чистой."""
    db = tmp_db
    await db.gridfinity_config_set(7, json.dumps(_cfg(), ensure_ascii=False))
    j = await _json(await webapp._api_gf_projects(_Req()))
    assert j["ok"] is True and j["projects"] == []


# ─────────────── деньги, границы и гонки (аудит 30.08) ───────────────

async def test_batch_price_is_the_sum_of_its_lines(tmp_db, auth):
    """Позаказных статей у gridfinity нет: итог партии = сумма строк, без «сюрприза» сверху.

    До 01.09.2026 подготовка, стол и минимум брались один раз на партию, и этот же прибор
    сторожил «один раз, а не шесть». 01.09.2026 их убрали совсем: органайзер продаётся как
    народный, а на лотке за три рубля полтора рубля позаказных были половиной чека.
    Проверяем ровно новое правило: клиент складывает строки и получает итог, расхождение —
    только округление до гривенника (каждая строка округляется вверх сама, итог — один раз,
    поэтому сумма строк может оказаться на копейки БОЛЬШЕ)."""
    mods = [_mod(i + 1, cx=(i % 5) * 2, cy=(i // 5) * 2, vol=8.0) for i in range(6)]
    pid = await _new_project(_cfg(*mods))
    parts = (await _get(pid))["parts"]
    ids = [p["id"] for p in parts]
    j = await _json(await webapp._api_gf_preflight(
        _Req({"initData": "x", "ids": ",".join(str(i) for i in ids)}, {"pid": str(pid)})))
    naive = sum(p["price"] for p in parts)
    assert j["total"]["n"] == 6 and j["total"]["price"] > 0
    assert j["total"]["price"] == pytest.approx(naive, abs=0.1 * len(parts))
    assert j["total"]["price"] <= naive
    assert j["total"]["grams"] == pytest.approx(sum(p["grams"] for p in parts), rel=1e-6)


async def test_bulk_volume_is_not_counted_as_solid_plastic(tmp_db, auth):
    """Ножки и плита дна печатаются инфиллом: расход считается по volb, а не по объёму меша."""
    pid = await _new_project(_cfg(dict(_mod(1, vol=40.0), volb=30.0)))
    p = (await _get(pid))["parts"][0]
    # 10 см³ стенок + 30 см³ сплошных тел × 0.45 = 23.5 см³ филамента
    assert p["vol_cm3"] == pytest.approx(23.5, abs=0.05)
    assert p["grams"] > 0


async def test_monstrous_box_is_rejected_and_infinity_does_not_kill_the_project(tmp_db, auth):
    """Габариты валидируются на сохранении: 50 000 мм доезжали до генератора PDF."""
    pid = await _new_project(_cfg(_mod(1)))
    bad = _cfg(_mod(1)); bad["base"]["W"] = 50000
    j = await _save(pid, bad)
    assert j["ok"] is False
    j = await _save(pid, json.loads('{"v":4,"base":{"type":"drawer","W":1e400,"D":270,"H":120},"modules":[]}'))
    assert j["ok"] is False
    # проект по-прежнему открывается
    assert (await _get(pid))["ok"] is True


async def test_two_orders_do_not_double_book_the_same_parts(tmp_db, auth):
    """Ретрай по таймауту или второй открытый мини-апп не должны печатать партию дважды.

    Последовательный повтор отбивает предпроверка, а настоящую гонку (обе ручки прошли
    проверку одновременно) держит УСЛОВНЫЙ захват в gf_parts_order — его и проверяем."""
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1)))
    part = (await _get(pid))["parts"][0]
    first = await _order(pid, [part["id"]])
    assert first["ok"] is True
    second = await _order(pid, [part["id"]])
    assert second["ok"] is False and "хотя бы одну" in second["error"]
    # деталь осталась в ПЕРВОЙ заявке
    assert (await _get(pid))["parts"][0]["request_id"] == first["order_id"]
    # захват второй заявкой не проходит на уровне SQL: деталь уже не в черновике
    assert await db.gf_parts_order(pid, [part["id"]], 999, {}) == 0
    assert (await _get(pid))["parts"][0]["request_id"] == first["order_id"]


async def test_invoiced_order_freezes_the_part(tmp_db, auth):
    """Цена названа — геометрию менять нельзя, иначе мастер печатает больше за те же деньги."""
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1)))
    part = (await _get(pid))["parts"][0]
    j = await _order(pid, [part["id"]])
    await db.update_request(j["order_id"], status="awaiting_payment")
    r = await _save(pid, _cfg(_mod(1, hmm=120.0)))
    assert r["diff"]["forked"] == 1


async def test_ordered_part_shows_the_frozen_snapshot(tmp_db, auth):
    """Печать-лист мастера — про то, за что заплатили, а не про живую раскладку."""
    db = tmp_db
    pid = await _new_project(_cfg(_mod(1, hmm=46.85)))
    part = (await _get(pid))["parts"][0]
    j = await _order(pid, [part["id"]])
    await db.update_request(j["order_id"], status="in_progress")
    await _save(pid, _cfg(_mod(1, hmm=120.0)))
    snap = await _json(await webapp._api_admin_gridfinity_config(
        _Req({"initData": "a"}, {"rid": str(j["order_id"])})))
    assert snap["parts"][0]["h_mm"] == pytest.approx(46.85, abs=0.1)
    assert snap["preview"]["cols"] == 11        # план — по раскладке ПРОЕКТА


async def test_archived_project_kills_the_public_link(tmp_db, auth):
    pid = await _new_project(_cfg(_mod(1)))
    tok = (await _json(await webapp._api_gf_project_patch(
        _Post({"initData": "x", "share": 1}, {"pid": str(pid)}))))["share"]
    assert (await webapp._gf_share_page(_Req(match_info={"token": tok}))).status == 200
    await webapp._api_gf_project_patch(_Post({"initData": "x", "archived": 1}, {"pid": str(pid)}))
    assert (await webapp._gf_share_page(_Req(match_info={"token": tok}))).status == 404


async def test_restore_returns_the_layout_to_the_client(tmp_db, auth):
    """Без конфигурации в ответе откат не доезжал до сцены и затирался автосохранением."""
    pid = await _new_project(_cfg(_mod(1)))
    await webapp._api_gf_project_patch(_Post({"initData": "x", "snapshot": 1}, {"pid": str(pid)}))
    await _save(pid, _cfg(_mod(1), _mod(2, cx=2)))
    vid = (await _get(pid))["versions"][0]["id"]
    j = await _json(await webapp._api_gf_project_patch(
        _Post({"initData": "x", "restore": vid}, {"pid": str(pid)})))
    assert j["config"] and len(j["config"]["modules"]) == 1 and j["config"]["ts"] > 0


async def test_deleted_project_does_not_come_back_from_the_old_draft(tmp_db, auth):
    """Миграция не должна воскрешать только что удалённый ящик."""
    db = tmp_db
    await db.gridfinity_config_set(7, json.dumps(_cfg(_mod(1)), ensure_ascii=False))
    j = await _json(await webapp._api_gf_projects(_Req()))
    pid = j["projects"][0]["id"]
    await webapp._api_gf_project_patch(_Post({"initData": "x", "delete": 1}, {"pid": str(pid)}))
    j2 = await _json(await webapp._api_gf_projects(_Req()))
    assert j2["projects"] == []


# ─────────────────────── стандарт мастерской и цена (01.09.2026) ───────────────────────
#
# Курс на «народный» gridfinity: позаказных статей нет, печатаем одним обкатанным пластиком
# толстым слоем, и за это к цене идёт скидка, которая СКЛАДЫВАЕТСЯ с клиентской.
# Всё это — обещания клиенту в интерфейсе и мастеру в описании заявки, поэтому проверяются
# не формулы pricing (у них свои тесты), а именно они: что обещано, то и посчитано.

async def _price_of(pid):
    """Цена партии из ВСЕХ деталей проекта: новая деталь не выбрана, и смета пустой партии —
    ноль (это правильно, но мерить на ней нечего)."""
    ids = [p["id"] for p in (await _get(pid))["parts"]]
    await webapp._api_gf_parts_select(
        _Post({"initData": "x", "ids": ids, "sel": 1}, {"pid": str(pid)}))
    return (await _get(pid))["money"]


async def _std_plastic(dbmod):
    """Завести в прайсе тот самый стандарт: в тестовой базе сидируется только Elegoo PLA."""
    pid = await dbmod.add_plastic(gfx._GF_STD_PLASTIC, 35.0, 1.27, material_type="PETG")
    gfx._price_cache.clear()
    return pid


async def test_per_order_fees_never_touch_gridfinity(tmp_db, auth):
    """Подготовка, доплата за стол и минимум заказа на органайзер не действуют совсем.

    Прибор грубый нарочно: ставим все три статьи заведомо огромными. Если хоть одна снова
    попадёт в расчёт, цена вырастет в разы — «народная» цена так и умирала бы незаметно."""
    pid = await _new_project(_cfg(_mod(1, vol=30.0)))
    before = (await _price_of(pid))["price"]
    for k, v in (("setup_fee", 500.0), ("plate_fee", 300.0), ("min_price", 900.0)):
        await tmp_db.set_setting(k, v)
    gfx._price_cache.clear()
    after = (await _price_of(pid))["price"]
    assert before > 0
    assert after == pytest.approx(before, abs=0.01)


async def test_standard_discount_is_applied_and_named(tmp_db, auth):
    """Скидка за стандарт входит в цену и приезжает клиенту отдельным числом.

    Без `std_pct` в ответе интерфейс не смог бы разложить итог на слагаемые, а «−25%»
    без объяснения читается как маркетинговый шум."""
    pid = await _new_project(_cfg(_mod(1, vol=30.0)))
    m = await _price_of(pid)
    assert m["std_pct"] == 25.0 and m["discount_pct"] == 25.0
    full = m["price"]
    await tmp_db.set_setting("gfx_standard_discount_pct", 0.0)
    gfx._price_cache.clear()
    gfx._disc_cache.clear()
    off = (await _price_of(pid))["price"]
    assert off > full
    assert full == pytest.approx(off * 0.75, rel=0.02)


async def test_client_discount_adds_to_the_standard_one(tmp_db, auth):
    """«Скидки складываются»: личные 10% и стандартные 25% дают 35%, а не максимум из них.

    Это ровно та развилка, где легко ошибиться: во всём остальном приложении скидки
    выбираются МАКСИМУМОМ (pricing.effective_discount_pct), и стандарт — единственное
    исключение, потому что он про себестоимость, а не про лояльность."""
    pid = await _new_project(_cfg(_mod(1, vol=30.0)))
    plain = await _price_of(pid)
    await tmp_db.set_user_discount(7, 10.0)
    gfx._disc_cache.clear()
    with_own = await _price_of(pid)
    assert plain["discount_pct"] == 25.0
    assert with_own["discount_pct"] == 35.0
    assert with_own["price"] < plain["price"]


async def test_public_link_shows_only_the_standard_discount(tmp_db, auth):
    """Страница по ссылке открыта БЕЗ авторизации — личная скидка в ней светиться не должна."""
    settings = await tmp_db.get_settings()
    await tmp_db.set_user_discount(7, 10.0)
    assert await gfx._gf_discount(7, 1, settings) == 35.0
    assert await gfx._gf_discount(None, 1, settings) == 25.0


async def test_estimate_uses_the_standard_layer_not_the_machine_profile(tmp_db):
    """Время считается стандартом (слой 0.3, сопло 0.6), а не зашитым прежде 0.2/0.4.

    Раньше оценка была почти вдвое длиннее правды и молча дорожала машинным временем —
    при том, что печатать всё равно собирались толстым слоем."""
    from app import fastcalc
    fil = {"shell": 30.0 * 1000.0, "infill": 0.0, "support": 0.0, "flush": 0.0}
    old = fastcalc.print_minutes(fil, {"part_heights": [47.0]},
                                 {"layer_height": 0.2, "nozzle": "0.4"})
    now = gfx._gf_minutes(30.0, 47.0)
    assert now == pytest.approx(fastcalc.print_minutes(
        fil, {"part_heights": [47.0]}, {"layer_height": 0.3, "nozzle": "0.6"}))
    assert now < old * 0.8


async def test_standard_plastic_beats_the_cheapest_one(tmp_db):
    """Считаем по тому пластику, которым напечатаем, а не по самому дешёвому в прайсе.

    Иначе обещанная скидка считалась бы от чужой цены: у Elegoo PETG килограмм дороже
    почти на треть, и «−25%» от дешёвого пластика были бы враньём в свою пользу."""
    await tmp_db.add_plastic("Дешёвый филамент", 10.0, 1.24)
    await tmp_db.add_plastic(gfx._GF_STD_PLASTIC, 35.0, 1.27, material_type="PETG")
    gfx._price_cache.clear()
    plastic, _ = await gfx._gf_price_ctx()
    assert str(plastic.name) == gfx._GF_STD_PLASTIC


async def test_pinned_plastic_wins_over_the_name(tmp_db):
    """Закреплённый настройкой пластик важнее совпадения по имени: стандарт можно сменить,
    не переименовывая прайс."""
    pinned = await tmp_db.add_plastic("Свой PETG", 33.0, 1.27, material_type="PETG")
    await tmp_db.add_plastic(gfx._GF_STD_PLASTIC, 35.0, 1.27, material_type="PETG")
    await tmp_db.set_setting("gfx_standard_plastic_id", float(pinned))
    gfx._price_cache.clear()
    plastic, _ = await gfx._gf_price_ctx()
    assert int(plastic.id) == pinned


async def test_order_description_tells_the_master_the_deal(tmp_db, auth):
    """Условия сделки — В ОПИСАНИИ заявки, а не только на экране клиента.

    Цену этой заявки мастер ставит руками (is_custom): без этой строки он не узнал бы ни
    про стандарт, ни про обещанную скидку, и клиент увидел бы в мастерской одну цифру, а
    в счёте другую. Из всех разъездов денежный замечают последним."""
    await _std_plastic(tmp_db)
    await tmp_db.set_user_discount(7, 10.0)
    pid = await _new_project(_cfg(_mod(1, vol=30.0)))
    ids = [p["id"] for p in (await _get(pid))["parts"]]
    j = await _json(await webapp._api_gf_order(
        _Post({"initData": "x", "ids": ids}, {"pid": str(pid)})))
    assert j["ok"] is True
    req = await tmp_db.get_request(j["order_id"])
    desc = str(getattr(req, "description", ""))
    assert "Стандарт мастерской" in desc and gfx._GF_STD_PLASTIC in desc
    assert "слой 0.3 мм" in desc
    assert "35%" in desc and "25% за стандарт" in desc and "10% личная" in desc
