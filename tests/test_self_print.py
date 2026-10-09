"""Самопечать мастера как «заказ админа» (status='self', слоистость 0, ВНЕ выручки) + сумма
«было бы потрачено» в профиле (мастер-план, Фаза B). Прикрепление печати к заказу-самопечати
не меняет source журнала."""


async def test_self_print_order_and_would_spend(tmp_db):
    db = tmp_db
    admin = 555
    jid = await db.add_print_journal(label="ECC2 test", plastic_id=None, plastic_name="PETG",
                                     grams=50, minutes=90, graded=0)
    rid = await db.create_self_print_order(admin, plastic_name="PETG", grams=50, minutes=90,
                                           price=123.45, label="ECC2 test", jid=jid)
    r = await db.get_request(rid)
    assert r.status == "self" and r.user_id == admin and r.price == 123.45
    assert r.quality == "слоистость 0"
    j = await db.get_print_journal(jid)                     # журнал прикреплён, но source='self'
    assert j["request_id"] == rid and j["source"] == "self"
    assert await db.admin_would_spend(admin) == 123.45
    await db.create_self_print_order(admin, plastic_name="PLA", grams=20, minutes=30,
                                     price=50.0, label="t2")
    assert await db.admin_would_spend(admin) == 173.45      # суммируется
    assert await db.admin_would_spend(999) == 0.0           # у другого мастера — своя сумма


async def test_self_print_excluded_from_revenue(tmp_db):
    db = tmp_db
    await db.create_self_print_order(1, plastic_name="PETG", grams=50, minutes=90,
                                     price=999.0, label="self")
    await db.add_request(user_id=2, user_name="c", model_label="m", printer="p", plastic_name="PLA",
                         quantity=1, quality="0.2", strength="s", grams_total=10, minutes_total=10,
                         price=100.0, preview_path=None, status="printed")
    cur = await db._conn().execute(
        "SELECT COALESCE(SUM(price),0) AS rev FROM requests WHERE status='printed'")
    assert (await cur.fetchone())["rev"] == 100.0           # самопечать (999) НЕ в выручке


async def test_print_event_unified_view(tmp_db):
    """print_events (Фаза B): единый вид события = журнал + катушки из stock_ledger + est/actual +
    происхождение граммов (source_of_grams)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    sid = await db.add_spool(pid, "Оранжевый", 1000)
    jid = await db.add_print_journal(label="ev", plastic_id=pid, plastic_name="PETG", grams=50,
                                     minutes=120, graded=0, spool_id=sid, est_minutes=100,
                                     source_of_grams="telemetry")
    ev = await db.get_print_event(jid)
    assert ev["source_of_grams"] == "telemetry"
    assert ev["est_minutes"] == 100 and ev["actual_minutes"] == 120     # пара оценка/факт
    assert len(ev["spools"]) == 1 and ev["spools"][0]["spool_id"] == sid  # катушка из ledger
    assert ev["spools"][0]["grams"] == 50 and ev["spools"][0]["color"] == "оранжевый"
    assert (await db.get_spool(sid))["remaining_g"] == 950              # реально списалось с катушки


async def test_time_prediction_gated_and_off_by_default(tmp_db):
    """Этап 5: коррекция времени по умолчанию ВЫКЛ (цену не трогаем без ведома мастера) и спит,
    пока выборка мала; просыпается при накоплении пар оценка/факт (гейт по выборке)."""
    db = tmp_db
    assert (await db.get_settings()).get("time_prediction") == 0.0     # выкл по умолчанию
    p = await db.predict_minutes(60.0, min_samples=5)                  # мало данных → спит
    assert p["applied"] is False and p["predicted"] == 60.0
    for _ in range(5):                                                # 5 пар в журнале: факт = 1.5×
        await db.add_print_journal(label="p", plastic_id=None, plastic_name="PLA", grams=10,
                                   minutes=150, graded=1, est_minutes=100, material_type="PLA")
    p2 = await db.predict_minutes(60.0, "PLA", min_samples=5)          # набралось → просыпается
    assert p2["applied"] is True and p2["factor"] == 1.5 and p2["predicted"] == 90.0


async def test_time_prediction_outliers_and_corridor(tmp_db):
    """Выбросы (факт/оценка вне [1/3, 3] — прерванные/перезапущенные печати) не попадают ни в
    медиану, ни в счёт выборки, а готовая поправка зажата коридором [0.5×, 2×]: кривой
    коэффициент на честных, но экстремальных данных не покажет клиенту абсурд."""
    db = tmp_db
    for _ in range(5):                                                # честные пары: факт = 2.5×
        await db.add_print_journal(label="c", plastic_id=None, plastic_name="PLA", grams=10,
                                   minutes=250, graded=1, est_minutes=100, material_type="PLA")
    p = await db.predict_minutes(60.0, "PLA", min_samples=5)
    assert p["applied"] is True and p["factor"] == 2.0 and p["predicted"] == 120.0  # коридор
    for _ in range(5):                                                # честные пары PETG: 1.5×
        await db.add_print_journal(label="h", plastic_id=None, plastic_name="PETG", grams=10,
                                   minutes=150, graded=1, est_minutes=100, material_type="PETG")
    for _ in range(3):                                                # мусор: печать прервали
        await db.add_print_journal(label="x", plastic_id=None, plastic_name="PETG", grams=10,
                                   minutes=2000, graded=1, est_minutes=100, material_type="PETG")
    p2 = await db.predict_minutes(60.0, "PETG", min_samples=5)
    assert p2["samples"] == 5                                         # выбросы не в счёте выборки
    assert p2["factor"] == 1.5 and p2["predicted"] == 90.0            # и не отравили медиану


async def test_print_samples_merged_into_journal(tmp_db):
    """Слияние print_samples в print_journal: predict_minutes/bigdata_stats читают пары оценка/факт
    ИЗ ЖУРНАЛА; record_time_sample обогащает событие заказа без задвоения."""
    db = tmp_db
    for _ in range(5):                                                # 5 событий журнала, факт=1.5×
        await db.add_print_journal(label="j", plastic_id=None, plastic_name="PETG", grams=10,
                                   minutes=150, graded=1, est_minutes=100, material_type="PETG")
    p = await db.predict_minutes(60.0, "PETG", min_samples=5)          # читает из журнала
    assert p["applied"] is True and p["factor"] == 1.5 and p["predicted"] == 90.0
    assert (await db.bigdata_stats())["samples"]["count"] == 5         # bigdata тоже из журнала
    # замер обогащает событие заказа (по request_id), НЕ создавая вторую строку
    rid = await db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                               plastic_name="PLA", quantity=1, quality="0.2", strength="s",
                               grams_total=20, minutes_total=40, price=5, preview_path=None)
    jid = await db.add_print_journal(label="ord", plastic_id=None, plastic_name="PLA", grams=20,
                                     minutes=48, graded=0, request_id=rid)     # без est/material
    await db.record_time_sample(rid, est_minutes=40, actual_minutes=48, material_type="PLA")
    ev = await db.get_print_journal(jid)
    assert ev["est_minutes"] == 40 and ev["material_type"] == "PLA"    # обогащено
    cur = await db._conn().execute(
        "SELECT COUNT(*) AS n FROM print_journal WHERE request_id=?", (rid,))
    assert (await cur.fetchone())["n"] == 1                            # без дубля


async def test_print_samples_merge_migration(tmp_db):
    """Разовая миграция print_samples → print_journal: обогащает событие заказа по request_id (без
    дубля) и заводит отдельное событие source='test' для замера без привязки."""
    db = tmp_db
    conn = db._conn()
    rid = await db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                               plastic_name="PLA", quantity=1, quality="0.2", strength="s",
                               grams_total=10, minutes_total=40, price=5, preview_path=None)
    jid = await db.add_print_journal(label="ord", plastic_id=None, plastic_name="PLA", grams=10,
                                     minutes=48, graded=0, request_id=rid)      # без est/material
    await conn.execute("INSERT INTO print_samples(request_id, model_hash, est_minutes, "
                       "actual_minutes, material_type, created_at) VALUES(?,?,?,?,?,?)",
                       (rid, "hh", 40, 48, "PLA", 0))                           # привязан к заказу
    await conn.execute("INSERT INTO print_samples(request_id, model_hash, est_minutes, "
                       "actual_minutes, material_type, created_at) VALUES(?,?,?,?,?,?)",
                       (None, "orphan", 30, 33, "PETG", 0))                     # без привязки
    await conn.execute("DELETE FROM settings WHERE key='print_samples_merged'")
    await conn.commit()
    await db.close()
    await db.init()                                                   # повторный прогон миграции
    ev = await db.get_print_journal(jid)                             # заказное событие обогащено
    assert ev["est_minutes"] == 40 and ev["material_type"] == "PLA"
    cur = await db._conn().execute(
        "SELECT COUNT(*) AS n FROM print_journal WHERE request_id=?", (rid,))
    assert (await cur.fetchone())["n"] == 1                          # без дубля заказной печати
    o = await (await db._conn().execute(
        "SELECT source, est_minutes, minutes FROM print_journal WHERE model_hash='orphan'")).fetchone()
    assert o and o["source"] == "test" and o["est_minutes"] == 30 and o["minutes"] == 33


async def test_prediction_sleeps_until_thirty_samples(tmp_db):
    """Порог выборки — 30 (решение владельца 30.08.2026 по замеру на боевых данных).

    На девяти замерах медиана факт/оценка была 1.019 при 95%-м интервале 0.70…1.31: данные
    не отличали «+2%» от «−30%», один добавленный замер двигал поправку на 4 п.п., а
    коридор при этом разрешал ей поднять цену заказа вдвое. Порог 8 позволял механизму
    проснуться на выборке, из которой ничего не следует."""
    db = tmp_db
    for _ in range(29):
        await db.add_print_journal(label="p", plastic_id=None, plastic_name="PLA", grams=10,
                                   minutes=150, graded=1, est_minutes=100, material_type="PLA")
    p = await db.predict_minutes(60.0, "PLA")
    assert p["applied"] is False and p["predicted"] == 60.0 and p["samples"] == 29
    await db.add_print_journal(label="p", plastic_id=None, plastic_name="PLA", grams=10,
                               minutes=150, graded=1, est_minutes=100, material_type="PLA")
    p = await db.predict_minutes(60.0, "PLA")
    assert p["applied"] is True and p["factor"] == 1.5


async def test_defective_but_finished_print_stays_in_the_sample(tmp_db):
    """`result='fail'` — это «на столе был брак», а не «печать оборвалась».

    Боевая запись 145: брак 11 %, печать шла 431.9 мин из 464.7 оценочных — время честное.
    Выбрасывать такие замеры значило бы терять годные данные; оборвавшуюся печать убирает
    не result, а коридор выбросов."""
    db = tmp_db
    for _ in range(29):
        await db.add_print_journal(label="ok", plastic_id=None, plastic_name="PLA", grams=10,
                                   minutes=100, graded=1, est_minutes=100, material_type="PLA")
    # брак 11 %, но печать доехала до конца: отношение правдоподобное
    await db.add_print_journal(label="bad", plastic_id=None, plastic_name="PLA", grams=10,
                               minutes=93, graded=1, est_minutes=100, material_type="PLA",
                               fail_pct=11.0)
    p = await db.predict_minutes(100.0, "PLA")
    assert p["samples"] == 30 and p["applied"] is True
    # а оборвавшаяся (12 % от оценки) в выборку не попадает — её снимает коридор
    await db.add_print_journal(label="dead", plastic_id=None, plastic_name="PLA", grams=10,
                               minutes=12, graded=1, est_minutes=100, material_type="PLA",
                               fail_pct=9.0)
    assert (await db.predict_minutes(100.0, "PLA"))["samples"] == 30
