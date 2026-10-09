"""Gridfinity-конфигуратор, СТАРЫЙ контур: формулы сводки и ручки черновика/заявки целиком.

Эти ручки живут ради мини-аппа, который ещё не обновился: новый клиент работает проектами
(tests/test_gridfinity_workshop.py). Ломать их нельзя — на старом фронте они единственные.

Формулы _gf_grid/_gf_summary обязаны совпадать со state.js прототипа
(design_handoff_gridfinity): шаг 42, бокс 42N−0.62, порог добора 2.3 и минус 0.3,
серии царг Blum. До этих тестов «1-в-1 со state.js» держалось только на ревью.
"""
import json

from conftest import stub_auth

from app import webapp
from app.web import gridfinity as gfx


class _Req:
    """GET-запрос: только query (+ match_info для админ-ручки)."""

    def __init__(self, query, match_info=None):
        self.query = query
        self.match_info = match_info or {}


class _Body:
    """Поток тела: ручки читают его сами с потолком, а не через request.json()."""

    def __init__(self, raw):
        self._raw = raw

    async def read(self, n=-1):
        return self._raw if n is None or n < 0 else self._raw[:n]


class _Post:
    """POST-запрос: тело потоком и json(), content_length считается сам."""

    def __init__(self, body):
        self._body = body
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.content_length = len(raw)
        self.content = _Body(raw)

    async def json(self):
        return self._body


# ─────────────────────────── формулы (эталон — state.js) ───────────────────────────

def test_gf_grid_matches_state_js():
    # 480×270: 11×6 ячеек, остаток X = 480−462 = 18, Y = 270−252 = 18
    g = gfx._gf_grid({"W": 480, "D": 270, "fitX": "sym", "fitY": "sym"})
    assert (g["cols"], g["rows"]) == (11, 6)
    assert (g["mL"], g["mR"]) == (9.0, 9.0)
    assert (g["mB"], g["mF"]) == (9.0, 9.0)
    # весь остаток влево / назад
    g = gfx._gf_grid({"W": 480, "D": 270, "fitX": "left", "fitY": "back"})
    assert (g["mL"], g["mR"], g["mB"], g["mF"]) == (18.0, 0.0, 18.0, 0.0)
    # дробные размеры строкой не превращаются в 0×0 (int('480.5') падал бы)
    g = gfx._gf_grid({"W": "480.5", "D": "270.0"})
    assert g["cols"] == 11


def test_gf_summary_numbers_and_extension():
    cfg = {"base": {"type": "drawer", "W": 480, "D": 270, "H": 120, "fitX": "sym", "fitY": "sym"},
           "modules": [
               # бокс 4×2 у левой стенки с добором: 4·42−0.62+8.7 = 176.08 → 176.1
               {"kind": "bin", "gen": "tray", "cx": 0, "cy": 0, "w": 4, "d": 2, "hmm": 53.85,
                "colorIdx": 0, "ext": {"L": 1}},
               # без добора: 167.4×83.4 (42N−0.62)
               {"kind": "bin", "gen": "grid", "gridX": 1, "gridY": 2, "cx": 4, "cy": 0,
                "w": 4, "d": 2, "hmm": 46.85, "colorIdx": 3}]}
    s = gfx._gf_summary(cfg)
    assert "Сетка 11×6" in s
    assert "176.1×83.4×53.9" in s and "добор ←" in s
    assert "167.4×83.4×46.9" in s and "сетка 1×2" in s
    assert "Модулей: 2" in s


def test_gf_summary_tandem_useful_height():
    # полезная высота = max(H, верх серии) — ровно innerH() движка
    cfg = {"base": {"type": "tandem", "W": 650, "D": 450, "H": 200, "series": "C"}, "modules": []}
    s = gfx._gf_summary(cfg)
    assert "серия C (верх 192 мм) · полезная высота 200 мм" in s
    cfg["base"]["H"] = 120
    assert "полезная высота 192 мм" in gfx._gf_summary(cfg)


def test_gf_summary_cuts_by_lines_with_tail():
    mods = [{"kind": "bin", "gen": "tray", "cx": i % 11, "cy": i // 11, "w": 1, "d": 1,
             "hmm": 46.85, "colorIdx": i} for i in range(60)]
    cfg = {"base": {"type": "drawer", "W": 480, "D": 270, "H": 120}, "modules": mods}
    s = gfx._gf_summary(cfg)
    assert len(s) <= 3600
    assert "…и ещё" in s and "полная раскладка в снимке" in s
    # режем по строкам: последняя строка модуля цела (содержит позицию)
    tail_lines = [x for x in s.splitlines() if x and x[0].isdigit()]
    assert all("позиция (" in x for x in tail_lines)


def test_gf_broken_module_spoils_one_line_not_whole_summary():
    cfg = {"base": {"type": "drawer", "W": 480, "D": 270, "H": 120},
           "modules": [{"kind": "bin", "gen": "tray", "w": "мусор", "d": None, "hmm": "x",
                        "colorIdx": "y", "cx": 0, "cy": 0},
                       {"kind": "bin", "gen": "tray", "cx": 4, "cy": 0, "w": 2, "d": 2,
                        "hmm": 46.85, "colorIdx": 1}]}
    s = gfx._gf_summary(cfg)   # не бросает
    assert "83.4×83.4×46.9" in s      # здоровый модуль посчитан честно


# ─────────────────────────── ручки ───────────────────────────

async def test_gridfinity_config_roundtrip_and_order(tmp_db, monkeypatch):
    db = tmp_db
    stub_auth(monkeypatch,
              _verify_init_data=lambda s: 7,
              _verify_user=lambda s: {"id": 7, "username": "u7"})
    cfg = {"v": 4, "base": {"type": "drawer", "W": 480, "D": 270, "H": 120},
           "modules": [{"kind": "bin", "gen": "tray", "cx": 0, "cy": 0, "w": 2, "d": 2,
                        "hmm": 46.85, "colorIdx": 0}]}
    # черновик: POST → GET
    j = json.loads((await webapp._api_gridfinity_config_set(
        _Post({"initData": "x", "config": cfg}))).body.decode())
    assert j["ok"] is True
    j = json.loads((await webapp._api_gridfinity_config(_Req({"initData": "x"}))).body.decode())
    assert j["ok"] is True and j["config"]["base"]["W"] == 480

    # мусорные «модули» не рождают пустую заявку
    j = json.loads((await webapp._api_gridfinity_order(
        _Post({"initData": "x", "config": {"base": {}, "modules": [0]}}))).body.decode())
    assert j["ok"] is False

    # негабарит отбивается по заголовку, ДО разбора тела
    fat = _Post({"initData": "x"})
    fat.content_length = gfx._GF_MAX_BYTES + 100 * 1024
    j = json.loads((await webapp._api_gridfinity_order(fat)).body.decode())
    assert j["ok"] is False and "больш" in j["error"]

    # заявка: is_custom-заказ с source=gridfinity, снимок конфигурации сохранён
    j = json.loads((await webapp._api_gridfinity_order(
        _Post({"initData": "x", "config": cfg, "comment": "к пятнице"}))).body.decode())
    assert j["ok"] is True
    rid = j["order_id"]
    r = await db.get_request(rid)
    assert r.is_custom == 1
    # source в датаклассе Request не разворачивается — читаем колонку напрямую
    cur = await db._conn().execute("SELECT source FROM requests WHERE id = ?", (rid,))
    assert (await cur.fetchone())["source"] == "gridfinity"
    assert "83.4×83.4×46.9" in (r.description or "") and "к пятнице" in r.description
    snap = await db.gridfinity_order_config_get(rid)
    assert snap and json.loads(snap)["base"]["W"] == 480

    # админ-ручка отдаёт снимок (читатель gridfinity_order_configs)
    stub_auth(monkeypatch, _verify_admin=lambda s: 1)
    j = json.loads((await webapp._api_admin_gridfinity_config(
        _Req({"initData": "a"}, {"rid": str(rid)}))).body.decode())
    assert j["ok"] is True and j["config"]["base"]["W"] == 480 and "Сетка" in j["summary"]
