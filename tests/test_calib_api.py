"""HTTP-слой калибровки: ручки /api/admin/calib/* и страница образца по QR.

Доменные правила калибровки закрыты tests/test_calibration.py, а веб-слой над ними (два
десятка ручек одним куском webapp.py) не был покрыт ничем. Это самый чистый шов для выноса
калибровки в отдельный модуль, поэтому здесь — страховка ровно под такой перенос.

Проверяется НЕ «ручка ответила ok», а что число, введённое мастером на первом шаге, доезжает
до последнего: замер → шаг кампании → файл пресета слайсера → история снимков → паспорт →
сводка экрана. Потерянное при переносе поле ломает именно эту цепочку, а не факт ответа 200.

Отдельно — страница образца /csample/{sid}: она единственная в блоке живёт БЕЗ initData, по
подписи ссылки с этикетки, и её регистрацию в роутере легко потерять вместе с переездом.
"""
import json
from pathlib import Path

import pytest
from conftest import stub_auth

from app import calibpdf, calibration, config, webapp

ADMIN_ID = 777


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


def _tower_png(path) -> None:
    """Синтетическое «фото башни»: гладкая стенка и рябь в верхней пятой части.

    Настоящий снимок в тесты не кладём: метрика считает вторую разность яркости вдоль строки,
    и чередование через столбец даёт её в чистом виде — без зависимости от камеры и света.
    Верх файла = верх башни (метрика инвертирует полосы сама).
    """
    import numpy as np
    from PIL import Image
    a = np.full((200, 100), 128, dtype="uint8")
    a[:40, ::2] = 0
    a[:40, 1::2] = 255
    Image.fromarray(a, mode="L").save(path)


@pytest.fixture
def admin(monkeypatch):
    """Подпись админа считаем валидной: сама проверка initData — забота других тестов."""
    stub_auth(monkeypatch, _verify_admin=ADMIN_ID)
    return ADMIN_ID


@pytest.fixture
def slicer(tmp_path, monkeypatch):
    """Пресеты слайсера во ВРЕМЕННОЙ папке.

    Без подмены путей «применить» записало бы значение в боевой профиль ElegooSlicer на машине
    разработчика: это единственное место, где бот вообще пишет в файлы слайсера. Пользовательский
    лист наследует системный — так же, как на живой машине, иначе паспорт не проверит own vs
    inherited.
    """
    user = tmp_path / "user" / "default"
    sysroot = tmp_path / "sys"
    monkeypatch.setattr(config, "SLICER_USER_DIR", user)
    monkeypatch.setattr(config, "PROFILES_DIR", sysroot)
    monkeypatch.setattr(config, "FILAMENT_DIR", sysroot / "filament" / "ECC2")
    monkeypatch.setattr(config, "PROCESS_DIR", sysroot / "process" / "ECC2")
    monkeypatch.setattr(config, "MACHINE_PROFILE", sysroot / "machine" / "ECC2" / "ECC2.json")
    base = sysroot / "filament" / "ECC2" / "База PLA.json"
    _write(base, {"name": "База PLA", "nozzle_temperature": ["215"], "hot_plate_temp": ["60"],
                  "filament_flow_ratio": ["1.0"]})
    leaf = user / "filament" / "Мой PLA.json"
    _write(leaf, {"name": "Мой PLA", "inherits": "База PLA", "filament_flow_ratio": ["0.98"]})
    return {"filament": leaf, "base": base}


# ── сквозной путь мастера ──────────────────────────────────────────────────────────────────

async def test_calibration_happy_path(tmp_db, slicer, admin, admin_req, body):
    """Кампания → шаг → замер → запись в пресет → снимок → паспорт → сводка.

    Каждый шаг проверяется дважды: ответ самой ручки И то, что данные ВИДНЫ следующей ручке.
    """
    fil = str(slicer["filament"])
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")

    # 1. кампания: имя пластика подставляет сервер по plastic_id, набор шагов — из тела
    created = body(await webapp._api_admin_calib_run_save(admin_req({
        "title": "Смена сопла 0.6", "reason": "новое сопло", "plastic_id": plastic,
        "nozzle": "0.6", "note": "первая после смены", "steps": ["temp", "flow"]})))
    assert created["ok"] is True and created["adopted"] == 0
    rid = created["id"]

    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    assert view["ok"] is True
    assert view["run"]["title"] == "Смена сопла 0.6"
    assert view["run"]["nozzle"] == "0.6"
    assert view["run"]["plastic_id"] == plastic
    assert view["run"]["plastic_name"] == "Elegoo PLA Matte (тест)"   # имя взято из карточки
    steps = {s["test_key"]: s for s in view["steps"]}
    assert list(steps) == ["temp", "flow"]                           # порядок из реестра тестов

    # 2. результат шага «температура» руками
    saved = body(await webapp._api_admin_calib_step_save(admin_req({
        "id": steps["temp"]["id"], "value": "215", "status": "done",
        "verdict": "спайка держит", "note": "секция 215 без соплей"})))
    assert saved["ok"] is True
    assert saved["step"]["value"] == 215.0
    assert saved["step"]["label"] == "Температура"                   # вид обогащён реестром
    # значение основного параметра показывается, даже если values_json ещё пуст
    assert saved["step"]["values"] == {"nozzle_temperature": 215.0}

    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    temp = next(s for s in view["steps"] if s["test_key"] == "temp")
    assert (temp["value"], temp["status"], temp["note"]) == (215.0, "done", "секция 215 без соплей")

    # 3. замер потока: правило считается на сервере, число с ЗАПЯТОЙ разбирается
    flow_id = steps["flow"]["id"]
    measured = body(await webapp._api_admin_calib_measure_add(admin_req({
        "kind": "flow_yolo", "run_id": rid, "step_id": flow_id,
        "label": "плашка +0.03", "data": {"base": "0,98", "delta": 0.03}})))
    assert measured["ok"] is True
    assert measured["result"]["value"] == pytest.approx(1.01)        # YOLO — добавка, не множитель

    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    flow = next(s for s in view["steps"] if s["test_key"] == "flow")
    assert flow["value"] == pytest.approx(1.01)                      # вердикт лёг в шаг сам
    assert flow["status"] == "measured" and flow["verdict"]
    assert [m["kind"] for m in flow["measures"]] == ["flow_yolo"]
    assert json.loads(flow["measures"][0]["data"])["base"] == 0.98   # числа хранятся с вердиктом

    # 4. снимок пресетов ДО правки — иначе истории не с чем сравнивать
    baseline = body(await webapp._api_admin_calib_presets_snapshot(admin_req({})))
    assert baseline == {"ok": True, "new": 1, "changed": 0, "gone": 0, "files": 1}

    # 5. «применить»: значение шага уезжает в файл пресета слайсера
    applied = body(await webapp._api_admin_calib_step_apply(admin_req({"id": flow_id, "path": fil})))
    assert applied["ok"] is True
    assert applied["param"] == "filament_flow_ratio"
    assert applied["value"] == pytest.approx(1.01)
    assert applied["written"] == [{"param": "filament_flow_ratio", "value": 1.01, "path": fil}]
    # файл на диске, а не только ответ ручки: форма (массив строк) сохранена
    assert calibration.read_preset(fil)["filament_flow_ratio"] == ["1.01"]
    assert calibration.read_preset(fil)["inherits"] == "База PLA"    # наследование не затёрто

    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    flow = next(s for s in view["steps"] if s["test_key"] == "flow")
    assert flow["applied"] == 1 and flow["applied_at"] and flow["status"] == "done"
    assert flow["values"] == {"filament_flow_ratio": 1.01}

    # 6. снимок ПОСЛЕ: «применить» уже сняло его само — повтор ничего не добавляет
    again = body(await webapp._api_admin_calib_presets_snapshot(admin_req({})))
    assert again == {"ok": True, "new": 0, "changed": 0, "gone": 0, "files": 1}

    hist = body(await webapp._api_admin_calib_preset_history(admin_req(query={"path": fil})))
    assert hist["path"] == fil and len(hist["items"]) == 2           # было и стало
    diff = {d["key"]: (d["old"], d["new"]) for d in hist["items"][0]["diff"]}
    assert diff["filament_flow_ratio"] == ("0.98", "1.01")           # правка видна в истории

    # 7. паспорт: значения собираются из пресета — своё и унаследованное
    passport = body(await webapp._api_admin_calib_passport_save(admin_req({
        "plastic_id": plastic, "nozzle": "0.6", "run_id": rid,
        "presets": {"filament": fil, "мусор": "нет такого вида"},
        "note": "после смены сопла"})))
    assert passport["ok"] is True
    assert passport["values"] == {"temp": 215.0, "bed": 60.0, "flow": 1.01}
    assert passport["source"] == {"temp": "inherited", "bed": "inherited", "flow": "own"}
    # карточка пластика запоминает свой откалиброванный профиль
    assert (await tmp_db.get_plastic(plastic)).filament_profile == fil

    # 8. сводка экрана: всё созданное видно одним запросом
    screen = body(await webapp._api_admin_calib(admin_req()))
    assert screen["ok"] is True
    assert screen["stats"] == {"runs": 1, "active": 1, "passports": 1, "samples": 0,
                               "presets": 1, "prints": 0, "loose": 0, "by_type": []}
    assert [r["id"] for r in screen["runs"]] == [rid]
    assert screen["runs"][0]["steps"] == 2 and screen["runs"][0]["steps_done"] == 2
    card = screen["passports"][0]
    assert card["id"] == passport["id"] and card["nozzle"] == "0.6"
    assert card["plastic"] == "Elegoo PLA Matte (тест)"
    assert card["values"]["flow"] == pytest.approx(1.01)             # число доехало от замера
    assert card["presets"] == {"filament": fil}
    assert [p["path"] for p in screen["presets"]] == [fil]           # снимок
    assert [p["path"] for p in screen["presets_disk"]] == [fil]      # файл на диске
    assert screen["journal"] == [] and screen["samples"] == []


async def test_presets_screen_marks_disk_and_snapshots(tmp_db, slicer, admin, admin_req, body):
    """Экран пресетов сводит диск и снимки: пока снимка нет — пресет «не снят»."""
    fil = str(slicer["filament"])
    before = body(await webapp._api_admin_calib_presets(admin_req()))
    assert before["ok"] is True
    assert [i["path"] for i in before["items"]] == [fil]
    assert before["items"][0]["snapped"] is False and before["items"][0]["versions"] == 0
    assert before["user_dir"] == str(config.SLICER_USER_DIR)

    await webapp._api_admin_calib_presets_snapshot(admin_req({}))
    after = body(await webapp._api_admin_calib_presets(admin_req()))
    assert after["items"][0]["snapped"] is True and after["items"][0]["versions"] == 1
    assert after["items"][0]["snap_id"] > 0


# ── страница образца по QR ─────────────────────────────────────────────────────────────────

async def test_csample_page_opens_only_by_its_own_signature(tmp_db, admin, admin_req, get_req, body):
    """Ссылка с этикетки читается с телефона без логина, но только своя и только на чтение."""
    saved = body(await webapp._api_admin_calib_sample_save(admin_req({
        "title": "Куб после PA", "proves": "PA 0.03 даёт номинал", "value": "20.02",
        "place": "коробка №1", "note": "не выбрасывать"})))
    assert saved["ok"] is True
    sid = saved["id"]
    assert saved["url"] == f"/csample/{sid}?s={webapp._sign('csample' + str(sid))}"
    sig = saved["url"].split("s=")[1]

    ok = await webapp._csample_page(get_req({"s": sig}, match_info={"sid": str(sid)}))
    assert ok.status == 200 and ok.content_type == "text/html"
    page = ok.text
    for part in ("Куб после PA", "PA 0.03 даёт номинал", "20.02", "коробка №1",
                 "не выбрасывать", f"C-{sid}"):
        assert part in page, part                    # шифр C-<id> присвоила БД, он на этикетке

    # подпись привязана к id: годная подпись СОСЕДНЕГО образца эту страницу не открывает
    alien = await webapp._csample_page(
        get_req({"s": webapp._sign("csample" + str(sid + 1))}, match_info={"sid": str(sid)}))
    assert alien.status == 403
    assert (await webapp._csample_page(get_req(match_info={"sid": str(sid)}))).status == 403

    gone = sid + 1000
    missing = await webapp._csample_page(
        get_req({"s": webapp._sign("csample" + str(gone))}, match_info={"sid": str(gone)}))
    assert missing.status == 404


# ── пресеты: снимок как страховка от «слайсер обновился и всё слетело» ──────────────────────

async def test_preset_restore_puts_file_back_on_disk(tmp_db, slicer, admin, admin_req, body):
    """Снимок → правка → восстановление: на диске снова ровно то, что было.

    Это лекарство от «обновил слайсер — пресеты сбросились к стоку», поэтому проверяем ФАЙЛ, а
    не ответ ручки: восстановление, отчитавшееся об успехе и не тронувшее диск, бесполезно.
    """
    fil = str(slicer["filament"])
    orig = Path(fil).read_text(encoding="utf-8")
    await webapp._api_admin_calib_presets_snapshot(admin_req({}))
    hist = body(await webapp._api_admin_calib_preset_history(admin_req(query={"path": fil})))
    snap_id = hist["items"][0]["id"]

    calibration.set_preset_value(fil, "filament_flow_ratio", 1.5)   # «поток уехал»
    assert calibration.read_preset(fil)["filament_flow_ratio"] == ["1.5"]

    res = body(await webapp._api_admin_calib_preset_restore(admin_req({"id": snap_id})))
    assert res == {"ok": True, "path": fil}
    assert Path(fil).read_text(encoding="utf-8") == orig            # тело снимка, а не «ключ на месте»

    # худший случай: обновление слайсера СНЕСЛО пользовательский лист целиком
    Path(fil).unlink()
    again = body(await webapp._api_admin_calib_preset_restore(admin_req({"id": snap_id})))
    assert again["ok"] is True
    assert Path(fil).read_text(encoding="utf-8") == orig
    assert calibration.read_preset(fil)["inherits"] == "База PLA"   # наследование вернулось тоже

    missing = await webapp._api_admin_calib_preset_restore(admin_req({"id": snap_id + 500}))
    assert missing.status == 404


# ── паспорт: резервная копия настроек печати ────────────────────────────────────────────────

async def test_passport_apply_writes_numbers_back_into_presets(tmp_db, slicer, admin, admin_req,
                                                               body):
    """«Применить паспорт» возвращает накопленные числа в пресет слайсера.

    Ключевое здесь — куда НЕ пишем: у значения, чей вид пресета в паспорте не указан, файла нет,
    и оно обязано попасть в skipped, а не уехать наугад в первый попавшийся пресет.
    """
    fil = str(slicer["filament"])
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")
    saved = body(await webapp._api_admin_calib_passport_save(admin_req({
        "plastic_id": plastic, "nozzle": "0.4", "presets": {"filament": fil}})))
    assert saved["values"] == {"temp": 215.0, "bed": 60.0, "flow": 0.98}
    pid = saved["id"]

    calibration.set_preset_value(fil, "filament_flow_ratio", 1.0)   # слайсер сбросил лист
    res = body(await webapp._api_admin_calib_passport_apply(admin_req({"id": pid})))
    assert res["ok"] is True
    assert sorted(res["applied"]) == ["bed", "flow", "temp"] and res["skipped"] == []
    data = calibration.read_preset(fil)
    assert data["filament_flow_ratio"] == ["0.98"]                  # своё число вернулось
    assert data["nozzle_temperature"] == ["215.0"]                  # унаследованное закрепилось своим
    assert data["hot_plate_temp"] == ["60.0"]

    # выборочное применение: чиним только поток, температуры не трогаем
    calibration.set_preset_value(fil, "filament_flow_ratio", 1.0)
    calibration.set_preset_value(fil, "nozzle_temperature", 200)
    only = body(await webapp._api_admin_calib_passport_apply(admin_req({"id": pid,
                                                                       "fields": ["flow"]})))
    assert only["applied"] == ["flow"] and only["skipped"] == []
    data = calibration.read_preset(fil)
    assert data["filament_flow_ratio"] == ["0.98"] and data["nozzle_temperature"] == ["200"]

    # у отката вид пресета machine, а в паспорте указан только filament — записывать НЕКУДА
    manual = body(await webapp._api_admin_calib_passport_save(admin_req({
        "plastic_id": plastic, "nozzle": "0.6", "presets": {"filament": fil},
        "values": {"flow": 1.02, "retract": 0.8}})))
    mixed = body(await webapp._api_admin_calib_passport_apply(admin_req({"id": manual["id"]})))
    assert mixed["applied"] == ["flow"] and mixed["skipped"] == ["retract"]
    assert "retraction_length" not in calibration.read_preset(fil)

    assert (await webapp._api_admin_calib_passport_apply(admin_req({"id": 999999}))).status == 404


async def test_passport_delete_removes_card_from_screen(tmp_db, slicer, admin, admin_req, body):
    """Удаление паспорта убирает карточку и из списка, и из счётчиков экрана."""
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")
    pid = body(await webapp._api_admin_calib_passport_save(admin_req({
        "plastic_id": plastic, "nozzle": "0.4", "values": {"flow": 0.98}})))["id"]
    screen = body(await webapp._api_admin_calib(admin_req()))
    assert [c["id"] for c in screen["passports"]] == [pid]
    assert screen["stats"]["passports"] == 1

    assert body(await webapp._api_admin_calib_passport_delete(admin_req({"id": pid}))) == {"ok": True}
    screen = body(await webapp._api_admin_calib(admin_req()))
    assert screen["passports"] == [] and screen["stats"]["passports"] == 0


# ── удаления: где проходит граница «кампания» / «история печатей» ───────────────────────────

async def test_run_delete_wipes_campaign_but_keeps_prints(tmp_db, slicer, admin, admin_req, body):
    """Удаление кампании уносит шаги, замеры и образцы — но НЕ записи журнала печатей.

    Печать сама по себе ценность: у неё фото и таймлапс, и она остаётся доказательством даже
    после того, как кампанию переиграли. Поэтому проверяем обе стороны границы, а не «ok».
    """
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")
    jid = await tmp_db.add_print_journal(label="ECC2_0.4_flowrate_PLA_0.2_11m.gcode",
                                         plastic_id=plastic, plastic_name="Elegoo PLA Matte (тест)",
                                         grams=5, minutes=11)
    created = body(await webapp._api_admin_calib_run_save(admin_req({
        "title": "К удалению", "plastic_id": plastic, "steps": ["flow"], "adopt": 1})))
    rid = created["id"]
    assert created["adopted"] == 1                       # печать подобрана в кампанию

    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    step_id = view["steps"][0]["id"]
    await webapp._api_admin_calib_measure_add(admin_req({
        "kind": "cube_xy", "run_id": rid, "step_id": step_id, "data": {"x": 20.02, "y": 19.99}}))
    await webapp._api_admin_calib_sample_save(admin_req({"run_id": rid, "title": "Куб"}))

    assert body(await webapp._api_admin_calib_run_delete(admin_req({"id": rid}))) == {"ok": True}
    assert (await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)}))).status == 404
    assert await tmp_db.get_calib_step(step_id) is None
    assert await tmp_db.list_calib_measures(run_id=rid) == []
    assert await tmp_db.list_calib_samples(run_id=rid) == []

    entry = await tmp_db.get_print_journal(jid)
    assert entry is not None                             # печать пережила удаление кампании
    assert entry["calib_run_id"] is None and entry["calib_step_id"] is None
    assert entry["calib_type"] == "flow"                 # ... и осталась опознанным тестом


async def test_measure_delete_takes_it_out_of_the_run(tmp_db, slicer, admin, admin_req, body):
    """Замер виден в кампании и у своего шага; удаление убирает его из обоих мест."""
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")
    rid = body(await webapp._api_admin_calib_run_save(admin_req({
        "title": "Замеры", "plastic_id": plastic, "steps": ["cube"]})))["id"]
    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    step_id = view["steps"][0]["id"]
    added = body(await webapp._api_admin_calib_measure_add(admin_req({
        "kind": "cube_xy", "run_id": rid, "step_id": step_id, "label": "первый замер",
        "data": {"x": 20.02, "y": 19.99, "nominal": 20}})))
    assert added["result"]["verdict"] == "в норме"

    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    assert [m["id"] for m in view["measures"]] == [added["result"]["id"]]
    assert [m["kind"] for m in view["steps"][0]["measures"]] == ["cube_xy"]

    assert body(await webapp._api_admin_calib_measure_delete(
        admin_req({"id": added["result"]["id"]}))) == {"ok": True}
    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    assert view["measures"] == [] and view["steps"][0]["measures"] == []


async def test_sample_delete_closes_its_qr_page(tmp_db, admin, admin_req, get_req, body):
    """Удалили образец с полки — этикетка на нём перестаёт открываться.

    Этикетка уехала вместе с деталью, поэтому «карточки нет» обязано означать 404, а не пустую
    страницу с чужой подписью.
    """
    saved = body(await webapp._api_admin_calib_sample_save(admin_req({
        "title": "Клин расхода", "proves": "чистое окно 10–13 мм³/с", "place": "коробка №1"})))
    sid = saved["id"]
    sig = saved["url"].split("s=")[1]
    ok = await webapp._csample_page(get_req({"s": sig}, match_info={"sid": str(sid)}))
    assert ok.status == 200

    assert body(await webapp._api_admin_calib_sample_delete(admin_req({"id": sid}))) == {"ok": True}
    gone = await webapp._csample_page(get_req({"s": sig}, match_info={"sid": str(sid)}))
    assert gone.status == 404


# ── пластина стола: Z-offset живёт в принтере, и хранить его больше негде ───────────────────

async def test_plate_save_keeps_z_offset_and_current_plate(tmp_db, slicer, admin, admin_req, body):
    """Пластина, её вылет и отметка «лежит на столе сейчас» — одной ручкой на четыре действия."""
    created = body(await webapp._api_admin_calib_plate_save(admin_req({
        "name": "PEI текстурированная", "kind": "pei", "z_offset": "-0,07", "note": "штатная"})))
    assert created["ok"] is True
    first = created["id"]
    cards = {p["id"]: p for p in body(await webapp._api_admin_calib(admin_req()))["plates"]}
    assert cards[first]["name"] == "PEI текстурированная"
    assert cards[first]["z_offset"] == pytest.approx(-0.07)     # запятая разобрана как разделитель
    assert cards[first]["is_default"] == 1                      # первая пластина сама текущая

    second = body(await webapp._api_admin_calib_plate_save(admin_req({"name": "Гладкая"})))["id"]
    cards = {p["id"]: p for p in body(await webapp._api_admin_calib(admin_req()))["plates"]}
    # «ещё не мерил» и «ровно 0» — разные состояния: пустой вылет не подменяется нулём
    assert cards[second]["z_offset"] is None and cards[second]["is_default"] == 0

    assert body(await webapp._api_admin_calib_plate_save(
        admin_req({"action": "default", "id": second})))["ok"] is True
    cards = {p["id"]: p for p in body(await webapp._api_admin_calib(admin_req()))["plates"]}
    assert (cards[first]["is_default"], cards[second]["is_default"]) == (0, 1)

    assert body(await webapp._api_admin_calib_plate_save(
        admin_req({"action": "delete", "id": second})))["ok"] is True
    cards = {p["id"]: p for p in body(await webapp._api_admin_calib(admin_req()))["plates"]}
    assert second not in cards and first in cards

    # без имени пластину не завести: безымянная строка в списке ничем не помогает мастеру
    assert body(await webapp._api_admin_calib_plate_save(admin_req({"name": "   "}))) == {
        "ok": False, "error": "Введите имя пластины"}


# ── журнал печатей ↔ кампания ──────────────────────────────────────────────────────────────

async def test_link_binds_print_to_step_by_hand(tmp_db, admin, admin_req, body):
    """Ручная привязка печати к шагу — когда имя задания не распозналось автоматически."""
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")
    jid = await tmp_db.add_print_journal(label="деталь_клиента_v3", plastic_id=plastic,
                                         plastic_name="Elegoo PLA Matte (тест)", grams=8,
                                         minutes=25)
    assert not (await tmp_db.get_print_journal(jid))["calib_type"]   # автоматика не узнала тест

    rid = body(await webapp._api_admin_calib_run_save(admin_req({
        "title": "Ручная привязка", "plastic_id": plastic, "steps": ["temp", "flow"]})))["id"]
    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    step_id = next(s["id"] for s in view["steps"] if s["test_key"] == "temp")

    assert body(await webapp._api_admin_calib_link(admin_req({
        "journal_id": jid, "step_id": step_id}))) == {"ok": True}
    entry = await tmp_db.get_print_journal(jid)
    assert (entry["calib_run_id"], entry["calib_step_id"]) == (rid, step_id)
    assert entry["calib_type"] == "temp"                 # тип берётся у шага, а не у имени файла
    step = await tmp_db.get_calib_step(step_id)
    assert step["journal_id"] == jid and step["status"] == "measured"   # шаг ушёл из «todo» сам
    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    assert [e["id"] for e in view["journal"]] == [jid]

    # тип, проставленный руками, автоматика затем НЕ затирает (иначе привязка бессмысленна)
    other = await tmp_db.add_print_journal(label="без_имени_теста", plastic_id=plastic,
                                           plastic_name="Elegoo PLA Matte (тест)", grams=3,
                                           minutes=9)
    assert body(await webapp._api_admin_calib_link(admin_req({
        "journal_id": other, "calib_type": "flow"})))["ok"] is True
    assert (await tmp_db.get_print_journal(other))["calib_type"] == "flow"

    assert (await webapp._api_admin_calib_link(admin_req({"journal_id": jid + 5000}))).status == 404
    assert (await webapp._api_admin_calib_link(admin_req({
        "journal_id": jid, "step_id": step_id + 5000}))).status == 404


async def test_backfill_recognizes_tests_and_adopts_prints(tmp_db, admin, admin_req, body):
    """Пересканирование журнала: тип теста узнаётся по имени задания задним числом.

    Так накопленная до реестра тестов история наконец опознаётся, а ключ attach сажает
    опознанные печати в открытую кампанию.
    """
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")
    jid = await tmp_db.add_print_journal(label="ECC2_0.4_temperature_tower_PLA_0.2_28m13s.gcode",
                                         plastic_id=plastic, plastic_name="Elegoo PLA Matte (тест)",
                                         grams=6, minutes=28)
    await tmp_db.set_journal_calib(jid, calib_type="")    # печать записана до реестра тестов
    plain = await tmp_db.add_print_journal(label="Ваза клиента", plastic_id=plastic,
                                           plastic_name="Elegoo PLA Matte (тест)", grams=40,
                                           minutes=300)
    rid = body(await webapp._api_admin_calib_run_save(admin_req({
        "title": "После пересканирования", "plastic_id": plastic, "steps": ["temp"]})))["id"]

    res = body(await webapp._api_admin_calib_backfill(admin_req({"attach": 1})))
    assert res["ok"] is True
    assert (res["scanned"], res["changed"], res["calib"]) == (2, 1, 1)
    assert res["by_type"] == {"temp": 1}                  # обычная печать типом не обзавелась

    e = await tmp_db.get_print_journal(jid)
    assert e["calib_type"] == "temp" and e["calib_run_id"] == rid
    assert not (await tmp_db.get_print_journal(plain))["calib_type"]
    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    assert view["steps"][0]["journal_id"] == jid          # шаг получил свою печать


async def test_drift_shows_cube_creep_and_time_deviation(tmp_db, admin, admin_req, body):
    """Регресс-эталон: уползание куба по замерам и расхождение оценки с фактом по журналу."""
    for x, y in ((20.02, 19.99), (20.06, 20.01), (20.10, 20.03)):
        await webapp._api_admin_calib_measure_add(admin_req({
            "kind": "cube_xy", "data": {"x": x, "y": y, "nominal": 20}}))
    await tmp_db.add_print_journal(label="Ваза", plastic_id=None, plastic_name="PLA",
                                   grams=30, minutes=120, est_minutes=100)

    res = body(await webapp._api_admin_calib_drift(admin_req()))
    assert res["ok"] is True
    # ряд идёт ПО ВОЗРАСТАНИЮ времени, хотя из БД замеры приходят свежими вперёд: иначе
    # «уползание» на графике читалось бы задом наперёд
    assert [p["value"] for p in res["cube_x"]] == [20.02, 20.06, 20.10]
    assert [p["value"] for p in res["cube_y"]] == [19.99, 20.01, 20.03]
    assert res["cube_x_stat"]["delta"] == pytest.approx(0.08)
    assert res["cube_y_stat"]["n"] == 3
    assert [(e["est"], e["fact"], e["dev"]) for e in res["time_dev"]] == [(100.0, 120.0, 20.0)]
    assert res["time_dev_stat"]["n"] == 1


# ── PDF: паспорт и этикетки ────────────────────────────────────────────────────────────────

async def test_export_and_labels_produce_real_pdf_files(tmp_path, tmp_db, slicer, admin,
                                                        monkeypatch, admin_req, body):
    """Паспорт и этикетки доходят до генератора PDF и оставляют файл на диске.

    app/calibpdf.py не был назван ни одним тестом, а QR-этикетки уже наклеены на физические
    образцы: сорванный импорт reportlab или переименованное поле заметил бы только мастер у
    принтера. Сам PDF не разбираем — проверяем, что он собран и непуст.
    """
    out = tmp_path / "pdfs"
    monkeypatch.setattr(calibpdf, "out_dir", lambda: out)
    monkeypatch.setattr(config, "WEBAPP_PUBLIC_URL", "https://studio.example")
    fil = str(slicer["filament"])
    plastic = await tmp_db.add_plastic("Elegoo PLA Matte (тест)", 40, 1.24, material_type="PLA")
    rid = body(await webapp._api_admin_calib_run_save(admin_req({
        "title": "Паспорт в PDF", "plastic_id": plastic, "steps": ["temp"]})))["id"]
    view = body(await webapp._api_admin_calib_run(admin_req(query={"id": str(rid)})))
    await webapp._api_admin_calib_step_save(admin_req({
        "id": view["steps"][0]["id"], "value": "215", "status": "done", "verdict": "спайка держит"}))
    await webapp._api_admin_calib_measure_add(admin_req({
        "kind": "cube_xy", "run_id": rid, "data": {"x": 20.02, "y": 19.99}}))
    await webapp._api_admin_calib_sample_save(admin_req({
        "run_id": rid, "title": "Башня температур", "proves": "215 без соплей",
        "place": "коробка №1"}))
    pid = body(await webapp._api_admin_calib_passport_save(admin_req({
        "plastic_id": plastic, "nozzle": "0.6", "run_id": rid, "presets": {"filament": fil}})))["id"]

    passport = await webapp._api_admin_calib_export(admin_req(query={"id": str(pid)}))
    assert passport.status == 200
    assert passport.headers["Content-Disposition"] == f'inline; filename="calibration_{pid}.pdf"'
    made = out / f"passport_{pid}.pdf"
    assert made.read_bytes()[:4] == b"%PDF" and made.stat().st_size > 2000

    labels = await webapp._api_admin_calib_labels(admin_req(query={"run": str(rid)}))
    assert labels.status == 200
    tags = out / f"labels_{rid}.pdf"
    assert tags.read_bytes()[:4] == b"%PDF" and tags.stat().st_size > 1000

    assert (await webapp._api_admin_calib_export(admin_req(query={"id": "999999"}))).status == 404


# ── подсказка по фото башни ────────────────────────────────────────────────────────────────

async def test_photo_hint_points_where_to_measure(tmp_path, tmp_db, admin, admin_req, body):
    """Разбор фото башни: снимок берётся у печати журнала или у образца, полосы — снизу вверх.

    Проверяем сквозной путь чисел: bands/base/step/height_mm из тела доезжают до метрики, а
    «чистое окно» пересчитывается в миллиметры высоты и в расход — ровно это мастер и читает.
    """
    photo = tmp_path / "tower.png"
    _tower_png(photo)
    jid = await tmp_db.add_print_journal(label="ECC2_0.4_volumetric_PLA_0.2_31m.gcode",
                                         plastic_id=None, plastic_name="PLA", grams=12, minutes=31)
    await tmp_db.set_print_journal_fields(jid, done_photo=str(photo))

    res = body(await webapp._api_admin_calib_photo(admin_req({
        "journal_id": jid, "bands": 10, "base": 5, "step": 0.5, "height_mm": 50})))
    assert res["ok"] is True
    assert [b["band"] for b in res["bands"]] == list(range(1, 11))   # снизу вверх, как на башне
    assert [b["ripple"] for b in res["bands"]] == [False] * 8 + [True, True]
    assert res["window"] == {"from_pct": 0, "to_pct": 80, "from_mm": 0.0, "to_mm": 35.0,
                             "mvs_from": 5.0, "mvs_to": 22.5}

    # тот же снимок, но со стороны образца с полки
    sid = await tmp_db.save_calib_sample(title="Клин расхода")
    await tmp_db.set_calib_sample_photo(sid, str(photo))
    by_sample = body(await webapp._api_admin_calib_photo(admin_req({"sample_id": sid,
                                                                   "bands": 10})))
    assert by_sample["ok"] is True and len(by_sample["bands"]) == 10

    bare = await tmp_db.add_print_journal(label="Ваза", plastic_id=None, plastic_name="PLA",
                                          grams=30, minutes=120)
    assert body(await webapp._api_admin_calib_photo(admin_req({"journal_id": bare}))) == {
        "ok": False, "error": "У печати нет фото"}

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a picture")
    await tmp_db.set_calib_sample_photo(sid, str(broken))
    bad = body(await webapp._api_admin_calib_photo(admin_req({"sample_id": sid})))
    assert bad["ok"] is False and bad["error"]


# ── сторож переезда ────────────────────────────────────────────────────────────────────────

# Путь → ИМЯ ОБРАБОТЧИКА за ним. Пара, а не голый путь: перенос кода меняет не адрес, а то,
# кто на нём сидит, и снимок из одних путей такую подмену пропускает — свап двух калибровочных
# обработчиков местами проходил весь набор зелёным. Правится только вместе с make_app.
CALIB_HANDLERS: dict[str, str] = {
    "/api/admin/calib": "_api_admin_calib",
    "/api/admin/calib/run": "_api_admin_calib_run",
    "/api/admin/calib/run_save": "_api_admin_calib_run_save",
    "/api/admin/calib/run_delete": "_api_admin_calib_run_delete",
    "/api/admin/calib/step_save": "_api_admin_calib_step_save",
    "/api/admin/calib/step_apply": "_api_admin_calib_step_apply",
    "/api/admin/calib/measure_add": "_api_admin_calib_measure_add",
    "/api/admin/calib/measure_delete": "_api_admin_calib_measure_delete",
    "/api/admin/calib/sample_save": "_api_admin_calib_sample_save",
    "/api/admin/calib/sample_delete": "_api_admin_calib_sample_delete",
    "/api/admin/calib/presets": "_api_admin_calib_presets",
    "/api/admin/calib/presets_snapshot": "_api_admin_calib_presets_snapshot",
    "/api/admin/calib/preset_history": "_api_admin_calib_preset_history",
    "/api/admin/calib/preset_restore": "_api_admin_calib_preset_restore",
    "/api/admin/calib/passport_save": "_api_admin_calib_passport_save",
    "/api/admin/calib/passport_apply": "_api_admin_calib_passport_apply",
    "/api/admin/calib/passport_delete": "_api_admin_calib_passport_delete",
    "/api/admin/calib/plate_save": "_api_admin_calib_plate_save",
    "/api/admin/calib/photo": "_api_admin_calib_photo",
    "/api/admin/calib/drift": "_api_admin_calib_drift",
    "/api/admin/calib/backfill": "_api_admin_calib_backfill",
    "/api/admin/calib/link": "_api_admin_calib_link",
    "/api/admin/calib/export": "_api_admin_calib_export",
    "/api/admin/calib/labels": "_api_admin_calib_labels",
    # единственный маршрут блока вне /api/admin: страница образца по QR с этикетки
    "/csample/{sid}": "_csample_page",
}


def _calib_routes() -> list[tuple[str, str, object]]:
    """(метод, канонический путь, обработчик) для всего блока — ИЗ РОУТЕРА, а не из списка.

    Обход make_app, а не перечисление руками: новая калибровочная ручка попадает под проверку
    прав сама, а забытая в CALIB_HANDLERS валит сторож регистрации. HEAD пропускаем — aiohttp
    заводит его к каждому GET сам, и обработчик в нём тот же.
    """
    out = []
    for r in webapp.make_app().router.routes():
        res = r.resource
        if res is None or r.method == "HEAD":
            continue
        path = res.canonical
        if path.startswith("/api/admin/calib") or path == "/csample/{sid}":
            out.append((r.method, path, r.handler))
    return out


def test_calib_routes_stay_registered():
    """Сторож переезда: ручка без строки в make_app исчезает, а подменённая — молча меняет смысл.

    Сверяем СЛОВАРЬ целиком, а не вхождение подмножества: лишний маршрут под /api/admin/calib
    так же обязан быть назван здесь (и покрыт), иначе он проедет мимо проверки прав ниже.
    """
    assert {p: h.__qualname__ for _m, p, h in _calib_routes()} == CALIB_HANDLERS


async def test_calib_endpoints_reject_stranger(tmp_db, monkeypatch, admin_req):
    """Проверка прав живёт в КАЖДОЙ ручке блока — и в GET по query, и в POST по телу.

    Ждём именно 403, а не «не 200»: на таком теле часть ручек законно отвечает 400/404, и
    проверка «лишь бы не 200» зеленела бы даже у ручки, из которой преамбулу прав вырезали.
    """
    stub_auth(monkeypatch, _verify_admin=None)
    routes = [r for r in _calib_routes() if r[1].startswith("/api/admin/calib")]
    assert len(routes) == 24            # /csample закрыт не правами, а подписью — он ниже
    for method, path, handler in routes:
        req = admin_req({"id": 1}) if method == "POST" else admin_req(query={"id": "1"})
        assert (await handler(req)).status == 403, f"{method} {path}"
