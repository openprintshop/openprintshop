"""Батч 24.07 (вторая волна): цветовые слоты и варианты наборов.

Покрывает:
  • inspect3mf.filename_color_groups / filename_group_slots — цветовые группы имён STL-наборов
    («Ам Ням», OpenTodo 2f2a102c): пороги срабатывания, стабильный порядок слотов, hex-цвета;
  • inspect3mf._paint_majority_state / paint_states — декодер покраски граней Bambu/Orca
    (TriangleSelector RLE, «двухветвная деталь» Dyson, OpenTodo 3b91dbaa);
  • inspect3mf.variant_groups — варианты набора карточки (полный .3mf + россыпь STL,
    «Железный человек», OpenTodo 87eb9cf3);
  • geometry.split_mesh_by_paint — разрезание меша детали по покраске;
  • webapp._api_colors_sel — синхронизация выбранных цветов слотов с серверным снимком
    заказа (жалоба «цвет применяется только после захода в параметры», OpenTodo 2f7b597f);
  • webapp._register_full — регистрация карточки с вариантами (дефолт = полный .3mf).
"""
import json
import zipfile

import pytest
from conftest import stub_auth

from app import cpupool, geometry, inspect3mf, webapp

UID = 424242


# ─────────────── цветовые группы имён файлов (Ам Ням) ───────────────

AMNYAM = ["obj_1_(Green) Body.stl", "obj_2_(Green) Top.stl", "obj_3_(White) Eyes.stl",
          "obj_8_(Black) Eye.stl"]


def test_filename_groups_detected():
    groups = inspect3mf.filename_color_groups(AMNYAM)
    assert groups == {AMNYAM[0]: "green", AMNYAM[1]: "green",
                      AMNYAM[2]: "white", AMNYAM[3]: "black"}


def test_filename_groups_below_threshold():
    # один цвет / случайная скобка в одном имени — набор НЕ цветной
    assert inspect3mf.filename_color_groups(["(Green) a.stl", "(Green) b.stl"]) == {}
    assert inspect3mf.filename_color_groups(["(Green) a.stl", "b.stl", "c.stl", "d.stl"]) == {}
    # скобка не из словаря цветов — не тег
    assert inspect3mf.filename_color_groups(["(Print 2) a.stl", "(Print 3) b.stl"]) == {}


def test_filename_group_slots_stable_order():
    slots, hexes = inspect3mf.filename_group_slots(AMNYAM)
    # слоты в порядке ПЕРВОГО появления тега: green=0, white=1, black=2
    assert slots[AMNYAM[0]] == 0 and slots[AMNYAM[1]] == 0
    assert slots[AMNYAM[2]] == 1 and slots[AMNYAM[3]] == 2
    assert hexes == [inspect3mf.FILENAME_COLOR_HEX["green"],
                     inspect3mf.FILENAME_COLOR_HEX["white"],
                     inspect3mf.FILENAME_COLOR_HEX["black"]]


def test_filename_groups_bare_color_names():
    # россыпь «по цветам без скобок» (Железный человек/Дональд): gold5, black1x12, silver_x6
    files = ["gold.stl", "gold5.stl", "blue1x12.stl", "red3.stl", "silver_x6.stl", "white4_x4.stl"]
    groups = inspect3mf.filename_color_groups(files)
    assert groups == {"gold.stl": "gold", "gold5.stl": "gold", "blue1x12.stl": "blue",
                      "red3.stl": "red", "silver_x6.stl": "silver", "white4_x4.stl": "white"}
    # обычные имена деталей цветом не считаются
    assert inspect3mf.filename_color_tag("Gear.stl") == ""
    assert inspect3mf.filename_color_tag("Big Connector.stl") == ""
    assert inspect3mf.filename_color_tag("axe 2.stl") == ""


def test_light_dark_prefix_shades():
    slots, hexes = inspect3mf.filename_group_slots(
        ["(Light Blue) a.stl", "(Dark Blue) b.stl", "(Blue) c.stl"])
    assert len(set(slots.values())) == 3
    assert len(set(hexes)) == 3      # light/dark дают РАЗНЫЕ оттенки базового


# ─────────────── декодер покраски граней (Dyson 3b91dbaa) ───────────────

def test_paint_simple_codes():
    assert inspect3mf._paint_majority_state("") == 0
    assert inspect3mf._paint_majority_state("4") == 1     # 0b0100: лист, состояние 1
    assert inspect3mf._paint_majority_state("8") == 2     # 0b1000: лист, состояние 2
    assert inspect3mf._paint_majority_state("0") == 0     # лист, состояние 0
    assert inspect3mf._paint_majority_state("zz") == 0    # мусор → безопасный 0


def test_paint_split_majority():
    # «3» = 0b0011: деление на 4 потомка; дальше нибл-поток пуст → все листы читаются нулями
    assert inspect3mf._paint_majority_state("3") == 0
    # реальный код из Dyson dryer holder (усечённая форма): большинство листов — состояние 1
    assert inspect3mf._paint_majority_state("404006044A2442446404006044A2446404006044A"
                                            "6400200AAA20003000300030003") in (0, 1, 2)


def _mini_3mf(tmp_path, model_xml: str):
    p = tmp_path / "painted.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", model_xml)
        z.writestr("Metadata/model_settings.config", "<config></config>")
    return p


def test_paint_states_from_3mf(tmp_path):
    xml = """<?xml version="1.0"?>
    <model><resources>
      <object id="1" type="model"><mesh><triangles>
        <triangle v1="0" v2="1" v3="2" paint_color="4"/>
        <triangle v1="0" v2="1" v3="3"/>
        <triangle v1="1" v2="2" v3="3" paint_color="8"/>
      </triangles></mesh></object>
      <object id="2" type="model"><mesh><triangles>
        <triangle v1="0" v2="1" v3="2"/>
      </triangles></mesh></object>
    </resources></model>"""
    ps = inspect3mf.paint_states(_mini_3mf(tmp_path, xml))
    assert ps == {"1": [1, 0, 2]}          # некрашеный объект 2 не тащим


def test_paint_states_non_3mf(tmp_path):
    f = tmp_path / "a.stl"
    f.write_bytes(b"solid a\n")
    assert inspect3mf.paint_states(f) == {}


# ─────────────── варианты набора карточки (87eb9cf3) ───────────────

def test_variants_full_3mf_plus_loose():
    got = inspect3mf.variant_groups(["iron man.3mf", "gold1.stl", "red1.stl", "blue1.stl"])
    assert got is not None and len(got) == 2
    assert got[0]["key"] == "3mf:iron man.3mf" and got[0]["paths"] == ["iron man.3mf"]
    assert got[1]["key"] == "loose" and len(got[1]["paths"]) == 3


def test_variants_two_3mf():
    got = inspect3mf.variant_groups(
        ["Skull Presupported.3mf", "Skull not Supported.3mf",
         "a.stl", "b.stl", "c.stl", "d.stl"])
    assert got is not None and [v["key"] for v in got] == \
        ["3mf:Skull Presupported.3mf", "3mf:Skull not Supported.3mf", "loose"]


def test_variants_none_cases():
    assert inspect3mf.variant_groups(["a.stl", "b.stl", "c.stl"]) is None      # чистая россыпь
    assert inspect3mf.variant_groups(["m.3mf"]) is None                        # один файл
    assert inspect3mf.variant_groups(["m.3mf", "stand.stl"]) is None           # аксессуар (<3 STL)
    # «Plate N»-дубли при живом 3mf вырезаны фильтром → это НЕ вариант
    assert inspect3mf.variant_groups(
        ["falcon.3mf", "Plate 1 - Grey.stl", "Plate 2 - Grey.stl", "Plate 3 - Grey.stl"]) is None


# ─────────────── разрезание меша по покраске ───────────────

def test_split_mesh_by_paint():
    import trimesh
    box = trimesh.creation.box()                     # 12 граней
    states = [1] * 6 + [0] * 6
    subs = geometry.split_mesh_by_paint(box, states, default_ext=2)
    assert subs is not None and len(subs) == 2
    exts = sorted(e for _m, e in subs)
    assert exts == [1, 2]                            # 0 → экструдер детали (2), крашеные → 1
    assert sum(len(m.faces) for m, _e in subs) == 12
    # длина не совпала с гранями (негативы перестроили меш) → None, красим одним цветом
    assert geometry.split_mesh_by_paint(box, [1, 0], default_ext=2) is None
    # все грани одним состоянием → резать нечего
    assert geometry.split_mesh_by_paint(box, [0] * 12, default_ext=2) is None


# ─────────────── /api/colors_sel (2f7b597f) ───────────────

class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _Plastic:
    def __init__(self):
        self.id = 7
        self.name = "Test PLA"
        self.color_list = ["Белый", "Чёрный", "Красный"]


@pytest.fixture
def _colors_env(monkeypatch):
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    async def _get_plastic(pid):
        return _Plastic() if pid == 7 else None
    monkeypatch.setattr(webapp.db, "get_plastic", _get_plastic)

    async def _log(*a, **kw):
        return None
    monkeypatch.setattr(webapp.db, "log_event", _log)
    webapp._models["ctok"] = {
        "uid": UID, "label": "Тест", "paths": [],   # без paths — превью не перерисовываем
        "pdf": {"plastic_name": "старое"},
        "last": {"plastic_id": 7, "plastic_name": "Test PLA · Белый - Белый",
                 "color": "Белый - Белый", "colors_sel": ["Белый", "Белый"]},
    }
    yield
    webapp._models.pop("ctok", None)


async def test_colors_sel_updates_snapshot(_colors_env):
    resp = await webapp._api_colors_sel(_FakeRequest(
        {"initData": "x", "token": "ctok", "colors_sel": ["Красный", "Чёрный"]}))
    out = json.loads(resp.body.decode())
    assert out["ok"] is True
    last = webapp._models["ctok"]["last"]
    assert last["colors_sel"] == ["Красный", "Чёрный"]
    assert last["color"] == "Красный - Чёрный"
    assert last["plastic_name"] == "Test PLA · Красный - Чёрный"
    # снимок PDF-сметы тоже перекрашен — заказ и смета согласованы
    assert webapp._models["ctok"]["pdf"]["plastic_name"] == "Test PLA · Красный - Чёрный"


async def test_colors_sel_rejects_foreign_colors(_colors_env):
    resp = await webapp._api_colors_sel(_FakeRequest(
        {"initData": "x", "token": "ctok", "colors_sel": ["Зелёный неон"]}))
    out = json.loads(resp.body.decode())
    assert out["ok"] is False
    assert webapp._models["ctok"]["last"]["color"] == "Белый - Белый"   # снимок не тронут


async def test_colors_sel_requires_calc(monkeypatch):
    stub_auth(monkeypatch, _verify_init_data=lambda init_data: UID)
    resp = await webapp._api_colors_sel(_FakeRequest(
        {"initData": "x", "token": "нет такого", "colors_sel": ["Белый"]}))
    assert resp.status == 404


# ─────────────── регистрация карточки с вариантами ───────────────

async def test_register_full_defaults_to_3mf(tmp_path, monkeypatch):
    # cpupool → синхронно (в тестах пул не поднимаем)
    async def _run(func, *args, **kw):
        return func(*args, **kw)
    monkeypatch.setattr(cpupool, "run", _run)

    async def _log(*a, **kw):
        return None
    monkeypatch.setattr(webapp.db, "log_event", _log)

    m3 = tmp_path / "iron man.3mf"
    with zipfile.ZipFile(m3, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    stls = []
    for name in ("gold1.stl", "red1.stl", "blue1.stl"):
        f = tmp_path / name
        f.write_bytes(b"solid x\n")
        stls.append(str(f))

    res = await webapp._register_full([str(m3)] + stls, "Железный человек", UID, "")
    assert res["ok"] is True
    assert res["variant_sel"] == "3mf:iron man.3mf"
    assert [v["key"] for v in res["file_variants"]] == ["3mf:iron man.3mf", "loose"]
    m = webapp._models[res["token"]]
    # токен живёт ТОЛЬКО файлами выбранного комплекта — расчёт/3D/заказ не суммируют дубль
    assert m["paths"] == [str(m3)]
    assert m["all_paths"] == [str(m3)] + stls

    # смена комплекта тем же полным набором: теперь считаем россыпь
    res2 = await webapp._register_full(m["all_paths"], "Железный человек", UID, "",
                                       sel_key="loose", log=False)
    m2 = webapp._models[res2["token"]]
    assert m2["paths"] == stls
    assert res2["variant_sel"] == "loose"
    webapp._models.pop(res["token"], None)
    webapp._models.pop(res2["token"], None)


async def test_register_full_amnyam_colors(tmp_path, monkeypatch):
    async def _run(func, *args, **kw):
        return func(*args, **kw)
    monkeypatch.setattr(cpupool, "run", _run)

    async def _log(*a, **kw):
        return None
    monkeypatch.setattr(webapp.db, "log_event", _log)

    paths = []
    for name in AMNYAM:
        f = tmp_path / name
        f.write_bytes(b"solid x\n")
        paths.append(str(f))
    res = await webapp._register_full(paths, "Ам Ням", UID, "")
    # STL-набор с цветами в именах: карточка мультицветная, слоты «Цвета печати» из групп
    assert res["multicolor"] is True
    assert res["num_colors"] == 3
    assert res["mc_colors"] == [inspect3mf.FILENAME_COLOR_HEX["green"],
                                inspect3mf.FILENAME_COLOR_HEX["white"],
                                inspect3mf.FILENAME_COLOR_HEX["black"]]
    m = webapp._models[res["token"]]
    assert m["mixed_plates"] is False       # столы одноцветные — надбавки за продувку нет
    webapp._models.pop(res["token"], None)
