"""Этап 0 автотюнинга (app/tuning.py): сбор выборки «применённые ручки → исход».

Проверяем ровно то, что обещано планом и решением задачи: признаки замораживаются ЧИСЛАМИ
в строку журнала (пресеты мутабельны — JOIN на чтении врал бы про прошлое), повторный
вызов ничего не задваивает, неполные данные не выбрасывают строку из выборки, а счётчик
витрины не падает никогда. Ничего рекомендательного здесь нет и быть не должно.
"""
from __future__ import annotations

import pytest

from app import db, tuning

pytestmark = pytest.mark.asyncio


async def _plastic(name="Kingroon PETG", density=1.27, mt="PETG") -> int:
    """Справочник пластиков БЕЗ цвета — ровно как на боевой БД: plastics.name='Kingroon PETG',
    а в заказе лежит 'Kingroon PETG · Белый'. Регистрировать пластик уже с цветом значило бы
    проверять несуществующую схему именования (и не ловить дефект префиксного матча)."""
    return await db.add_plastic(name, 60.0, density, mt)


async def _order(*, qid, sid, plastic_name, grams=100.0, qty=1, dims="60×88×23 мм",
                 status="printed", hash_="") -> int:
    return await db.add_request(
        user_id=555001, user_name="Клиент", model_label="деталь", printer="CC2",
        plastic_name=plastic_name, quantity=qty, quality="Стандарт", strength="Средняя",
        grams_total=grams, minutes_total=120.0, price=30.0, preview_path=None, dims=dims,
        hash=hash_, quality_id=qid, strength_id=sid, status=status)


async def _presets() -> tuple[int, int]:
    qid = await db.add_quality("Стандарт — 0.20 мм", 0.20, "0.20mm Standard @CC2 0.4", 10)
    sid = await db.add_strength("Золотая середина", 15, 3, 10)
    return qid, sid


async def _journal(rid: int, pid: int, *, minutes=118.0, result="ok", fail_pct=0.0,
                   graded=1) -> int:
    return await db.add_print_journal(
        label="job.gcode", plastic_id=pid, plastic_name="Kingroon PETG · Белый",
        grams=100.0, minutes=minutes, result=result, fail_pct=fail_pct, graded=graded,
        request_id=rid)


async def test_full_order_gives_a_frozen_example(tmp_db):
    """Полный заказ: слой/заполнение/стенки/материал/объём/габариты замораживаются в журнал."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    jid = await _journal(rid, pid)
    await db.update_request(rid, rating=9, review="всё отлично")

    assert await tuning.capture_features(await db.get_request(rid)) == jid
    row = await db.get_print_journal(jid)
    assert row["layer_height"] == pytest.approx(0.20)
    assert row["infill_pct"] == 15
    assert row["walls"] == 3
    assert row["material_type"] == "PETG"
    assert row["volume_cm3"] == pytest.approx(100.0 / 1.27, rel=1e-3)
    assert (row["bbox_x"], row["bbox_y"], row["bbox_z"]) == (60.0, 88.0, 23.0)
    assert row["material_type"] == "PETG"           # справочник без цвета, заказ — с цветом

    ds = await tuning.build_dataset()
    assert [d["request_id"] for d in ds] == [rid]
    d = ds[0]
    assert d["outcome"] == "rated" and d["rating"] == 9 and d["rating_max"] == 10
    assert d["intent"] is None and d["parts"] is None      # не собирается ничем — и не врём
    assert d["params_source"] == "frozen"
    assert d["repeat_model"] is False
    assert d["material_type"] == "PETG"
    # знаменатели названы явно: объём — на экземпляр заказа, bbox — сцены (файла/стола),
    # граммы/минуты — на весь заказ
    assert d["volume_cm3_per_unit"] == pytest.approx(100.0 / 1.27, rel=1e-3)
    assert d["grams_order"] == pytest.approx(100.0) and d["journal_rows"] == 1


async def test_order_without_rating_stays_in_dataset_but_flagged(tmp_db):
    """Нет оценки — строка всё равно полезна, но помечена «исход неизвестен»."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    jid = await _journal(rid, pid, graded=0)          # мастер печать ещё не оценивал
    await tuning.capture_features(await db.get_request(rid))

    ds = await tuning.build_dataset()
    assert len(ds) == 1
    assert ds[0]["rating"] is None and ds[0]["outcome"] == "unknown"
    # мастер отметил печать удачной — исход уже не «неизвестен», но и не оценка клиента
    await db._conn().execute("UPDATE print_journal SET graded = 1 WHERE id = ?", (jid,))
    await db._conn().commit()
    assert (await tuning.build_dataset())[0]["outcome"] == "graded"


async def test_graded_default_without_result_is_not_a_verdict(tmp_db):
    """graded=1 — ещё и молчаливый ДЕФОЛТ колонки (db.py: DEFAULT 1, add_print_journal(graded=1)),
    поэтому исходом считаем только пару «graded + вписанный вердикт». Строка без вердикта —
    'unknown', иначе выборка получила бы подтверждение, которого мастер не давал."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    jid = await _journal(rid, pid, graded=1)
    # Ровно так выглядят строки, перенесённые миграцией из print_samples: вердикта в них нет,
    # а graded=1 им проставил DEFAULT. add_print_journal сам пустой result не пишет никогда.
    await db._conn().execute("UPDATE print_journal SET result = '' WHERE id = ?", (jid,))
    await db._conn().commit()
    await tuning.capture_features(await db.get_request(rid))

    assert (await tuning.build_dataset())[0]["outcome"] == "unknown"
    await db.grade_print_journal(jid, fail_pct=0.0)       # мастер оценил — вердикт появился
    assert (await tuning.build_dataset())[0]["outcome"] == "graded"


async def test_failed_print_is_a_negative_signal(tmp_db):
    """Брак берём из строки журнала, а не из глобальной ставки брака в цене."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    await _journal(rid, pid, result="fail", fail_pct=100.0)
    await tuning.capture_features(await db.get_request(rid))
    assert (await tuning.build_dataset())[0]["outcome"] == "failed"


async def test_order_without_print_falls_back_to_presets_and_says_so(tmp_db):
    """Заказ без печати в журнале: строку журнала НЕ плодим, заморозить негде — но заказ из
    выборки не выкидываем, а честно помечаем, что параметры восстановлены по СЕГОДНЯШНИМ
    пресетам (на боевой БД так живут 10 заказов из 26)."""
    await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    assert await tuning.capture_features(await db.get_request(rid)) is None
    assert await db.list_print_journal_for_request(rid) == []

    ds = await tuning.build_dataset()
    assert len(ds) == 1 and ds[0]["journal_id"] is None
    assert ds[0]["params_source"] == "presets" and ds[0]["layer_height"] == pytest.approx(0.20)
    assert ds[0]["outcome"] == "unknown"
    st = await tuning.stats()
    assert st["count"] == 1 and st["with_params"] == 0


async def test_capture_is_idempotent_and_never_switches_journal_rows(tmp_db):
    """Повторный вызов не плодит примеров и не уезжает на вторую строку многостольного
    заказа: строка выбирается тем же правилом MIN(id), что и у record_time_sample."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    first = await _journal(rid, pid)
    second = await _journal(rid, pid)          # второй стол того же заказа

    assert await tuning.capture_features(await db.get_request(rid)) == first
    for _ in range(3):
        assert await tuning.capture_features(await db.get_request(rid)) is None
    assert (await db.get_print_journal(second))["layer_height"] is None
    assert len(await tuning.build_dataset()) == 1
    assert await tuning.backfill_features() == 0


async def test_capture_writes_only_into_empty_fields(tmp_db):
    """Уже замороженное не перетираем: пресеты мутабельны, истина — момент печати."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    jid = await _journal(rid, pid)
    await db.set_print_journal_fields(jid, layer_height=0.12, infill_pct=40, walls=2)

    await tuning.capture_features(await db.get_request(rid))
    row = await db.get_print_journal(jid)
    assert (row["layer_height"], row["infill_pct"], row["walls"]) == (0.12, 40, 2)


async def test_self_prints_and_orders_without_params_are_out(tmp_db):
    """Свои печати мастера и заказы без применённых ручек в выборку не идут."""
    pid = await _plastic()
    qid, sid = await _presets()
    self_rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый",
                            status="self")
    await _journal(self_rid, pid)
    bare = await _order(qid=None, sid=None, plastic_name="Kingroon PETG · Белый")
    await _journal(bare, pid)

    assert await tuning.build_dataset() == []
    assert (await tuning.stats())["count"] == 0


async def test_broken_and_empty_fields_do_not_break_capture(tmp_db):
    """Битые габариты, нулевые граммы и неизвестный пластик: пример собирается, чем можем."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Неведомый пластик", grams=0.0,
                       dims="габариты неизвестны")
    jid = await _journal(rid, pid)
    await tuning.capture_features(await db.get_request(rid))

    row = await db.get_print_journal(jid)
    assert row["layer_height"] == pytest.approx(0.20) and row["walls"] == 3
    assert row["volume_cm3"] is None and row["bbox_x"] is None
    assert row["material_type"] is None                  # не распознан — поле пустое, не врём
    assert tuning.parse_dims(None) is None and tuning.parse_dims("60×0×23") is None

    # а имя с цветом («Elegoo PLA Matte · Бежевый») разбирается по префиксу справочника
    await db.add_plastic("Elegoo PLA Matte", 55.0, 1.24, "PLA Matte")
    color = await _order(qid=qid, sid=sid, plastic_name="Elegoo PLA Matte · Бежевый")
    cjid = await _journal(color, pid)
    await tuning.capture_features(await db.get_request(color))
    assert (await db.get_print_journal(cjid))["material_type"] == "PLA Matte"


async def test_backfill_fills_history_once(tmp_db):
    """Исторические печати добираются по расписанию и ровно один раз."""
    pid = await _plastic()
    qid, sid = await _presets()
    for _ in range(3):
        rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
        await _journal(rid, pid)

    assert await tuning.backfill_features() == 3
    assert await tuning.backfill_features() == 0
    assert (await tuning.stats())["with_params"] == 3


async def test_stats_counts_and_never_raises(tmp_db, monkeypatch):
    """Счётчик витрины: сколько накоплено, сколько с оценкой, разбивка по МАТЕРИАЛУ типу."""
    pid = await _plastic()
    pid2 = await _plastic("Elegoo PLA Matte", 1.24, "PLA Matte")
    qid, sid = await _presets()
    rated = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    await _journal(rated, pid)
    await db.update_request(rated, rating=10)
    plain = await _order(qid=qid, sid=sid, plastic_name="Elegoo PLA Matte · Чёрный")
    await _journal(plain, pid2)
    await tuning.backfill_features()

    st = await tuning.stats()
    assert st["count"] == 2 and st["rated"] == 1 and st["with_params"] == 2
    assert {m["material"] for m in st["by_material"]} == {"PETG", "PLA Matte"}

    def boom():
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(tuning.db, "_conn", boom)
    assert await tuning.stats() == {"count": 0, "rated": 0, "with_params": 0, "by_material": []}
    # и сам захват признаков при мёртвой БД тоже молчит, а не роняет «напечатано»
    assert await tuning.capture_features(await db.get_request(rated)) is None


async def test_material_is_read_through_the_catalogue_not_the_polluted_journal(tmp_db):
    """Материал датасета = справочник, а не то, что лежит в journal.material_type.

    Туда до нас пишет record_time_sample (старый путь «оценка → факт»), и клал он имя
    пластика ВМЕСТЕ С ЦВЕТОМ. Витрина такие строки нормализует справочником — датасет обязан
    делать ровно то же, иначе два читателя одних данных расходятся, а разбивка по ключевому
    признаку рассыпается в синглтоны.
    """
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Хаки")
    jid = await _journal(rid, pid)
    # ровно то, что осталось в боевой БД от старого писателя
    await db.set_print_journal_fields(jid, material_type="Kingroon PETG · Хаки")
    await tuning.capture_features(await db.get_request(rid))

    ds = await tuning.build_dataset()
    assert ds[0]["material_type"] == "PETG"
    assert [m["material"] for m in (await tuning.stats())["by_material"]] == ["PETG"]


async def test_time_sample_writer_no_longer_pollutes_material(tmp_db, monkeypatch):
    """Источник загрязнения починен: полный путь «напечатано» кладёт в журнал 'PETG', а не
    'Kingroon PETG · Хаки' — точного совпадения имени на боевых данных не бывает."""
    from app.web import ordermoney

    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Хаки")
    jid = await _journal(rid, pid, minutes=0.0)
    r = await db.get_request(rid)
    await ordermoney._capture_print_sample(r, {"actual_minutes": "118"})

    assert (await db.get_print_journal(jid))["material_type"] == "PETG"
    await tuning.capture_features(r)
    assert (await tuning.build_dataset())[0]["material_type"] == "PETG"


async def test_multi_plate_order_keeps_one_denominator_per_field(tmp_db):
    """Многостольный заказ на несколько штук: граммы и минуты — СУММА по всем столам, объём и
    габариты — на одну деталь, габариты — самой КРУПНОЙ (а не первой в строке dims)."""
    pid = await _plastic()
    qid, sid = await _presets()
    rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый", grams=254.0,
                       qty=2, dims="58×118×28 · 163×375×36 мм")
    await _journal(rid, pid, minutes=299.1)
    second = await _journal(rid, pid, minutes=23.4)
    await db.set_print_journal_fields(second, grams=5.28)
    await tuning.capture_features(await db.get_request(rid))

    d = (await tuning.build_dataset())[0]
    assert d["journal_rows"] == 2
    assert d["grams_order"] == pytest.approx(105.28)      # 100 (первый стол) + 5.28 (второй)
    assert d["minutes_order"] == pytest.approx(322.5)
    assert d["volume_cm3_per_unit"] == pytest.approx(254.0 / 2 / 1.27, rel=1e-3)
    assert (d["bbox_scene_x"], d["bbox_scene_y"], d["bbox_scene_z"]) == (163.0, 375.0, 36.0)
    assert d["dims_groups"] == 2
    assert tuning.parse_dims("58×118×28 · 163×375×36 мм") == (163.0, 375.0, 36.0)


async def test_order_without_print_still_gets_volume_and_bbox(tmp_db):
    """Заказ без строки журнала: ручки восстановлены пресетами, а объём и габариты считаются
    на чтении из полей самого заказа — терять их незачем, они не менялись."""
    await _plastic()
    qid, sid = await _presets()
    await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")

    d = (await tuning.build_dataset())[0]
    assert d["params_source"] == "presets" and d["journal_rows"] == 0
    assert d["volume_cm3_per_unit"] == pytest.approx(100.0 / 1.27, rel=1e-3)
    assert (d["bbox_scene_x"], d["bbox_scene_y"], d["bbox_scene_z"]) == (60.0, 88.0, 23.0)
    assert d["grams_order"] is None and d["minutes_order"] is None


async def test_self_print_flag_and_admin_orders_are_out(tmp_db, monkeypatch):
    """Критерий «своя печать» тот же, что у db.is_self_order: заказ админа в выборку не идёт,
    а колонка self_print сама по себе заказ из неё не выбрасывает (её ставят и клиентам)."""
    pid = await _plastic()
    qid, sid = await _presets()
    monkeypatch.setattr(db.config, "ADMIN_IDS", [555001])
    admin_rid = await _order(qid=qid, sid=sid, plastic_name="Kingroon PETG · Белый")
    await _journal(admin_rid, pid)
    assert await tuning.build_dataset() == []
    assert await tuning.capture_features(await db.get_request(admin_rid)) is None

    monkeypatch.setattr(db.config, "ADMIN_IDS", [1])
    await db._conn().execute("UPDATE requests SET self_print = 1 WHERE id = ?", (admin_rid,))
    await db._conn().commit()
    # тот же заказ клиента: пишем признаки и читаем его ОДНИМ И ТЕМ ЖЕ критерием
    assert await tuning.capture_features(await db.get_request(admin_rid)) is not None
    assert [d["request_id"] for d in await tuning.build_dataset()] == [admin_rid]
