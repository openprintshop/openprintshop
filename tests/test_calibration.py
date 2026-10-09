"""Калибровка филамента: распознавание тестов, правила, пресеты, кампании.

Главная ценность фичи — накопленный опыт не теряется. Поэтому тесты защищают именно это:
имя задания принтера должно узнаваться как тест (иначе история печатей не разберётся),
правило должно давать тот же вывод, что дал живой замер, пресет должен читаться и писаться
в родной для слайсера форме, а печать теста — сама ложиться в открытую кампанию.
"""
import json

import pytest

from app import calibration


# ── распознавание теста по имени задания ───────────────────────────────────────────────────
@pytest.mark.parametrize("label,expect", [
    # реальные имена из журнала печатей (27.07.2026)
    ("ECC2_0.4_temperature_tower_Elegoo PLA Matte _0.2_28m13s.gcode", "temp"),
    ("ECC2_0.4_SpeedTestStructure_Elegoo PLA Matte _0.2_37m23s.gcode", "mvs"),
    ("ECC2_0.4_flowrate_0_Elegoo PLA Matte - Откалиброван_0.2_33m4s.gcode", "flow"),
    ("ECC2_0.4_pa_pattern_110_5000_Elegoo PLA Matte_0.2.gcode", "pa"),
    ("ECC2_0.4_retraction_tower_Elegoo PLA Matte_0.2.gcode", "retract"),
    ("ECC2_0.4_VFA_Elegoo PLA Matte - Откалиброван_0.2_47m17s.gcode", "vfa"),
    ("ECC2_0.4_Куб_Elegoo PLA Matte - Откалиброван_0.2_6m21s.gcode", "cube"),
    ("ECC2_0.4_熨烫测试 v4_Elegoo PLA Matte _0.2_1h12m.gcode", "ironing"),
    # обычные печати не должны попадать в калибровку
    ("ECC2_0.4_Koala AMS_Elegoo PLA Matte _0.2_5m50s.gcode", ""),
    ("ECC2_0.4_Camel (1)_Elegoo PLA Matte _0.12_5h18m.gcode", ""),
    ("печать без имени", ""),
    ("", ""),
])
def test_detect_test(label, expect):
    assert calibration.detect_test(label) == expect


def test_detect_prefers_longer_alias():
    """`retraction_tower` не должен проиграть короткому совпадению другого теста."""
    assert calibration.detect_test("my_retraction_tower_v2.gcode") == "retract"


def test_test_order_is_dependency_order():
    """Порядок шагов — не косметика: поток меряется после температуры, PA после потока."""
    order = calibration.TEST_ORDER
    for key, t in calibration.TESTS.items():
        for dep in t.depends:
            assert order.index(dep) < order.index(key), f"{dep} должен идти до {key}"


# ── правила диагностики ────────────────────────────────────────────────────────────────────
def test_wall_thickness_underextrusion():
    """Стенка утоньшилась → материал реально убыл → это потолок расхода, ставим −10 %."""
    r = calibration.verdict("wall_thickness", {"clean": 0.86, "ripple": 0.79, "top_mvs": 13})
    assert r["verdict"] == "недоэкструзия"
    assert r["value"] == pytest.approx(11.7)


def test_wall_thickness_resonance():
    """Толщина та же → предел НЕ найден, значения для пресета правило не даёт."""
    r = calibration.verdict("wall_thickness", {"clean": 0.86, "ripple": 0.858, "top_mvs": 13})
    assert r["verdict"] == "резонанс"
    assert r["value"] is None


def test_cube_verdicts_match_real_measurements():
    """Живые замеры 27.07: при PA 0.015 углы торчат, при 0.03 куб в норме."""
    bad = calibration.verdict("cube_xy", {"x": 20.25, "y": 20.17, "nominal": 20})
    good = calibration.verdict("cube_xy", {"x": 20.02, "y": 20.02, "nominal": 20})
    assert bad["rule"] == "corners_pa"
    assert good["verdict"] == "в норме"


def test_mvs_window_formula():
    """MVS = основание + высота × шаг: окно 10.0–15.9 мм высоты → 10–12.95 мм³/с."""
    r = calibration.verdict("mvs_window", {"low_mm": 10.0, "high_mm": 15.9})
    assert r["value"] == pytest.approx(12.95)
    assert calibration.mvs_at_height(10.0) == pytest.approx(10.0)


def test_retract_takes_middle_of_window():
    """Берём СЕРЕДИНУ окна: отсчёт ступеней неоднозначен, край может оказаться вне окна."""
    assert calibration.verdict("retract_window", {"low": 0.3, "high": 0.7})["value"] == 0.5


def test_flow_yolo_is_additive():
    """В YOLO число на плашке — добавка, а не множитель (0.98 + 0.03 = 1.01)."""
    assert calibration.verdict("flow_yolo", {"base": 0.98, "delta": 0.03})["value"] == 1.01


def test_verdict_unknown_kind_is_quiet():
    assert calibration.verdict("нет такого", {})["verdict"] == ""
    assert calibration.verdict("cube_xy", {})["detail"]      # не хватает данных → понятный текст


def test_every_verdict_kind_is_implemented():
    """Вид замера, который UI покажет мастеру, обязан давать вывод."""
    for spec in calibration.VERDICT_KINDS:
        data = {f["k"]: 1.0 for f in spec["fields"]}
        assert calibration.verdict(spec["key"], data)["verdict"], spec["key"]


def test_diagnostics_reference_real_tests():
    for d in calibration.DIAGNOSTICS:
        for k in d["tests"]:
            assert k in calibration.TESTS, f"{d['key']} ссылается на несуществующий тест {k}"


# ── пресеты слайсера ───────────────────────────────────────────────────────────────────────
def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


def test_preset_write_keeps_shape(tmp_path, monkeypatch):
    """Пер-экструдерные параметры слайсер хранит массивом строк, скалярные — строкой.
    Запись не той формы слайсер молча проигнорирует."""
    f = tmp_path / "filament" / "Тест.json"
    _write(f, {"name": "Тест", "filament_flow_ratio": ["0.98"], "is_custom_defined": "0"})
    assert calibration.set_preset_value(f, "filament_flow_ratio", 1.01)
    assert calibration.set_preset_value(f, "is_custom_defined", 1)
    d = calibration.read_preset(f)
    assert d["filament_flow_ratio"] == ["1.01"]
    assert d["is_custom_defined"] == "1"


def test_preset_new_key_takes_shape_from_parent(tmp_path, monkeypatch):
    """Пользовательский лист содержит только правленые ключи. Новый параметр должен получить
    форму РОДИТЕЛЯ, иначе `pressure_advance: "0.03"` вместо `["0.03"]`."""
    monkeypatch.setattr(calibration.config, "SLICER_USER_DIR", tmp_path)
    monkeypatch.setattr(calibration.config, "PROFILES_DIR", tmp_path / "sys")
    _write(tmp_path / "sys" / "filament" / "База.json",
           {"name": "База", "pressure_advance": ["0.02"]})
    leaf = tmp_path / "filament" / "Мой.json"
    _write(leaf, {"name": "Мой", "inherits": "База"})
    assert calibration.set_preset_value(leaf, "pressure_advance", 0.03)
    assert calibration.read_preset(leaf)["pressure_advance"] == ["0.03"]


def test_flatten_resolves_user_over_system(tmp_path, monkeypatch):
    """Паспорт показывает и своё, и унаследованное — значит цепочка inherits должна тянуться
    из пользовательской папки в системную."""
    monkeypatch.setattr(calibration.config, "SLICER_USER_DIR", tmp_path)
    monkeypatch.setattr(calibration.config, "PROFILES_DIR", tmp_path / "sys")
    _write(tmp_path / "sys" / "filament" / "База.json",
           {"name": "База", "nozzle_temperature": ["220"], "hot_plate_temp": ["60"]})
    leaf = tmp_path / "filament" / "Мой.json"
    _write(leaf, {"name": "Мой", "inherits": "База", "nozzle_temperature": ["215"]})
    flat = calibration.flatten_preset(leaf)
    assert calibration.preset_num(flat, "nozzle_temperature") == 215
    assert calibration.preset_num(flat, "hot_plate_temp") == 60


def test_passport_marks_own_vs_inherited(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration.config, "SLICER_USER_DIR", tmp_path)
    monkeypatch.setattr(calibration.config, "PROFILES_DIR", tmp_path / "sys")
    _write(tmp_path / "sys" / "filament" / "База.json",
           {"name": "База", "nozzle_temperature": ["220"], "hot_plate_temp": ["60"]})
    leaf = tmp_path / "filament" / "Мой.json"
    _write(leaf, {"name": "Мой", "inherits": "База", "nozzle_temperature": ["215"]})
    p = calibration.passport_from_presets({"filament": str(leaf)})
    assert p["values"]["temp"] == 215
    assert p["source"]["temp"] == "own"
    assert p["source"]["bed"] == "inherited"


def test_diff_presets_by_keys():
    old = json.dumps({"a": ["1"], "b": "x"})
    new = json.dumps({"a": ["2"], "c": "y"})
    d = {x["key"]: (x["old"], x["new"]) for x in calibration.diff_presets(old, new)}
    assert d["a"] == ("1", "2")
    assert d["b"] == ("x", None)
    assert d["c"] == (None, "y")


def test_snapshot_reads_kind_and_hash(tmp_path):
    f = tmp_path / "machine" / "Мой принтер.json"
    _write(f, {"name": "Мой принтер", "retraction_length": ["0.5"]})
    s = calibration.snapshot(f)
    assert s["kind"] == "machine" and s["name"] == "Мой принтер" and len(s["sha"]) == 16
    assert "retraction_length" in s["body"]


def test_drift_summary():
    st = calibration.drift_summary([{"value": 20.02}, {"value": 20.05}, {"value": 20.11}])
    assert st["n"] == 3 and st["delta"] == pytest.approx(0.09)
    assert st["min"] == 20.02 and st["max"] == 20.11


# ── кампании в БД ──────────────────────────────────────────────────────────────────────────
async def test_run_creates_steps_in_order(tmp_db):
    rid = await tmp_db.add_calib_run(title="Смена сопла", plastic_name="Elegoo PLA Matte")
    keys = [s["test_key"] for s in await tmp_db.list_calib_steps(rid)]
    assert keys[:5] == ["temp", "flow", "pa", "retract", "mvs"]
    # повторный сид не задваивает шаги
    assert await tmp_db.seed_calib_steps(rid) == 0


async def test_calibration_print_attaches_itself(tmp_db):
    """Мастер режет тест в слайсере как обычно — шаг кампании заполняется сам по имени файла."""
    rid = await tmp_db.add_calib_run(title="Смена сопла")
    jid = await tmp_db.add_print_journal(
        label="ECC2_0.4_pa_pattern_110_5000_Elegoo PLA Matte_0.2.gcode",
        plastic_id=None, plastic_name="", grams=4.5, minutes=18, graded=0)
    e = await tmp_db.get_print_journal(jid)
    assert e["calib_type"] == "pa" and e["calib_run_id"] == rid
    step = next(s for s in await tmp_db.list_calib_steps(rid) if s["test_key"] == "pa")
    assert step["journal_id"] == jid and step["status"] == "measured"


async def test_ordinary_print_is_not_calibration(tmp_db):
    await tmp_db.add_calib_run(title="Смена сопла")
    jid = await tmp_db.add_print_journal(label="ECC2_0.4_Koala AMS_Elegoo PLA_0.2.gcode",
                                         plastic_id=None, plastic_name="", grams=3, minutes=40)
    e = await tmp_db.get_print_journal(jid)
    assert e["calib_type"] == "" and e["calib_run_id"] is None


async def test_second_print_of_same_test_does_not_steal_step(tmp_db):
    """Перепечатка теста после правки — нормальный ход калибровки: первая печать за шагом
    остаётся, вторая просто попадает в кампанию."""
    rid = await tmp_db.add_calib_run(title="Смена сопла")
    first = await tmp_db.add_print_journal(label="cube_1.gcode", plastic_id=None,
                                           plastic_name="", grams=4, minutes=8)
    second = await tmp_db.add_print_journal(label="cube_2.gcode", plastic_id=None,
                                            plastic_name="", grams=4, minutes=8)
    step = next(s for s in await tmp_db.list_calib_steps(rid) if s["test_key"] == "cube")
    assert step["journal_id"] == first
    assert (await tmp_db.get_print_journal(second))["calib_run_id"] == rid


async def test_backfill_recognises_history(tmp_db):
    """Журнал ведётся автоматически, поэтому вся прошлая история разбирается задним числом."""
    for lab in ("ECC2_0.4_temperature_tower_x.gcode", "ECC2_0.4_flowrate_0_x.gcode",
                "ECC2_0.4_Koala AMS_x.gcode"):
        await tmp_db.add_print_journal(label=lab, plastic_id=None, plastic_name="",
                                       grams=1, minutes=5)
    res = await tmp_db.backfill_calib_types()
    assert res["calib"] == 2
    assert set(res["by_type"]) == {"temp", "flow"}


async def test_measure_saves_verdict_with_numbers(tmp_db):
    rid = await tmp_db.add_calib_run(title="Смена сопла")
    step = next(s for s in await tmp_db.list_calib_steps(rid) if s["test_key"] == "mvs")
    r = await tmp_db.add_calib_measure(kind="wall_thickness", run_id=rid, step_id=step["id"],
                                       data={"clean": 0.86, "ripple": 0.79, "top_mvs": 13})
    assert r["value"] == pytest.approx(11.7)
    saved = (await tmp_db.list_calib_measures(step_id=step["id"]))[0]
    assert saved["verdict"] == "недоэкструзия"
    assert json.loads(saved["data"])["clean"] == 0.86      # числа хранятся рядом с выводом


async def test_passport_roundtrip(tmp_db):
    pid = await tmp_db.save_calib_passport(plastic_id=1, printer_id=1, nozzle="0.4",
                                           values={"temp": 215, "flow": 1.01},
                                           source={"temp": "own"},
                                           presets_map={"filament": "x.json"})
    again = await tmp_db.save_calib_passport(plastic_id=1, printer_id=1, nozzle="0.4",
                                             values={"temp": 210})
    assert pid == again                                    # ключ (пластик, принтер, сопло) один
    p = await tmp_db.get_calib_passport(1, 1, "0.4")
    assert p["values"] == {"temp": 210}


async def test_preset_snapshots_only_on_change(tmp_db, tmp_path):
    item = {"kind": "filament", "name": "Мой", "path": str(tmp_path / "f.json"),
            "sha": "aaa", "body": '{"a": 1}', "mtime": 1}
    assert (await tmp_db.snapshot_calib_presets([item]))["new"] == 1
    assert (await tmp_db.snapshot_calib_presets([item]))["changed"] == 0
    item2 = dict(item, sha="bbb", body='{"a": 2}')
    assert (await tmp_db.snapshot_calib_presets([item2]))["changed"] == 1
    # файл исчез (обновление слайсера сбросило пресет) — тело последнего снимка остаётся
    assert (await tmp_db.snapshot_calib_presets([]))["gone"] == 1
    hist = await tmp_db.list_calib_preset_history(item["path"])
    assert [h["gone"] for h in hist] == [1, 0, 0]
    assert hist[1]["body"] == '{"a": 2}'


async def test_delete_run_keeps_journal(tmp_db):
    """Кампанию можно удалить, но печати в журнале — самостоятельная ценность."""
    rid = await tmp_db.add_calib_run(title="Смена сопла")
    jid = await tmp_db.add_print_journal(label="ECC2_0.4_temperature_tower_x.gcode",
                                         plastic_id=None, plastic_name="", grams=8, minutes=29)
    await tmp_db.delete_calib_run(rid)
    e = await tmp_db.get_print_journal(jid)
    assert e is not None and e["calib_type"] == "temp" and e["calib_run_id"] is None


async def test_run_of_other_plastic_does_not_steal_print(tmp_db):
    """Открытая кампания по одному пластику не должна притягивать тест другого."""
    pla = await tmp_db.add_plastic("PLA тест", 40, 1.24, material_type="PLA")
    petg = await tmp_db.add_plastic("PETG тест", 45, 1.27, material_type="PETG")
    rid = await tmp_db.add_calib_run(title="Кампания PLA", plastic_id=pla)
    jid = await tmp_db.add_print_journal(label="ECC2_0.4_flowrate_0_petg.gcode", plastic_id=petg,
                                         plastic_name="PETG тест", grams=14, minutes=33)
    e = await tmp_db.get_print_journal(jid)
    assert e["calib_type"] == "flow"          # тест распознан…
    assert e["calib_run_id"] is None          # …но в чужую кампанию не попал
    own = await tmp_db.add_print_journal(label="ECC2_0.4_flowrate_0_pla.gcode", plastic_id=pla,
                                         plastic_name="PLA тест", grams=14, minutes=33)
    assert (await tmp_db.get_print_journal(own))["calib_run_id"] == rid


# ── глажка: тест с ПАРОЙ параметров и процентным значением в пресете ────────────────────────
def test_ironing_has_two_params():
    """Глажку одним числом не настроить: поток и скорость меняют результат вместе."""
    t = calibration.TESTS["ironing"]
    assert [p.key for p in t.params] == ["ironing_flow", "ironing_speed"]
    assert t.param == "ironing_flow"          # основной — тот, что ложится в calib_steps.value
    assert t.params[0].pct and not t.params[1].pct
    assert all(p.scope == "process" for p in t.params)


@pytest.mark.parametrize("label", [
    "ECC2_0.4_熨烫测试 v4_Elegoo PLA Matte _0.2_1h12m.gcode",
    "ECC2_0.4_ironing_test_PLA.gcode",
    "ECC2_0.4_разглаживание_проба.gcode",
])
def test_ironing_prints_are_recognised(label):
    assert calibration.detect_test(label) == "ironing"


def test_preset_percent_value_is_read_as_number():
    """ironing_flow слайсер хранит как «19%» — float('19%') падает, процент надо снимать."""
    d = {"ironing_flow": "19%", "ironing_speed": "43"}
    assert calibration.preset_num(d, "ironing_flow") == 19
    assert calibration.preset_num(d, "ironing_speed") == 43
    assert calibration.preset_is_pct(d, "ironing_flow")
    assert not calibration.preset_is_pct(d, "ironing_speed")


def test_preset_write_keeps_percent(tmp_path):
    """Голое «23» вместо «23%» слайсер не подхватит — знак процента должен сохраниться."""
    f = tmp_path / "process" / "Процесс.json"
    _write(f, {"name": "Процесс", "ironing_flow": "19%", "ironing_speed": "43"})
    assert calibration.set_preset_value(f, "ironing_flow", 23)
    assert calibration.set_preset_value(f, "ironing_speed", 33)
    d = calibration.read_preset(f)
    assert d["ironing_flow"] == "23%"
    assert d["ironing_speed"] == "33"


@pytest.mark.parametrize("symptom,verdict,value", [
    ("недолив", "мало материала", 23.0),        # +4 % к текущим 19
    ("перелив", "много материала", 15.0),       # −4 %
    ("шершаво", "слишком быстро", 33.0),        # скорость −10 мм/с
    ("ок", "поверхность в порядке", 19.0),
])
def test_ironing_verdicts(symptom, verdict, value):
    r = calibration.verdict("ironing", {"flow": 19, "speed": 43, "symptom": symptom})
    assert r["verdict"] == verdict
    assert r["value"] == pytest.approx(value)


def test_ironing_in_passport_fields():
    for k in ("ironing_flow", "ironing_speed", "ironing_spacing"):
        assert k in calibration.PASSPORT_FIELDS
        assert calibration.PASSPORT_FIELDS[k][0] == "process"
        assert k in calibration.PASSPORT_LABELS


async def test_step_keeps_both_ironing_values(tmp_db):
    """values_json хранит оба параметра, value остаётся зеркалом основного (для дрейфа и PDF)."""
    rid = await tmp_db.add_calib_run(title="Смена сопла", steps=["ironing"])
    step = (await tmp_db.list_calib_steps(rid))[0]
    await tmp_db.update_calib_step(step["id"],
                                   values={"ironing_flow": 19, "ironing_speed": 43})
    got = await tmp_db.get_calib_step(step["id"])
    assert got["values"] == {"ironing_flow": 19.0, "ironing_speed": 43.0}
    assert got["value"] == 19.0


async def test_ironing_is_in_default_campaign(tmp_db):
    rid = await tmp_db.add_calib_run(title="Смена сопла")
    keys = [s["test_key"] for s in await tmp_db.list_calib_steps(rid)]
    assert "ironing" in keys
    assert keys.index("flow") < keys.index("ironing")      # глажка после потока, не до
