"""Катушки (spools) + журнал движений (stock_ledger) — Фаза A мастер-плана учёта.
Проверяем: нормализацию цвета, приход/журнал, FIFO-списание, агрегат по цвету, dual-write рядом
с legacy-складом и единый color_availability (в т.ч. пластик, у которого нет legacy-строк)."""


async def test_canon_color():
    from app import db
    assert db._canon_color(" Чёрный ") == db._canon_color("черный") == "черный"
    assert db._canon_color("Тёмно   Синий") == "темно синий"
    assert db._canon_color("") == ""


async def test_add_spool_and_ledger(tmp_db):
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    sid = await db.add_spool(pid, "Оранжевый", 1000, low_threshold=100, cost=15.0, supplier="Shop")
    sp = await db.get_spool(sid)
    assert sp["remaining_g"] == 1000 and sp["net_weight_g"] == 1000
    assert sp["color"] == "оранжевый" and sp["status"] == "active" and sp["cost"] == 15.0
    led = await db.spool_ledger(sid)
    assert len(led) == 1 and led[0]["reason"] == "purchase" and led[0]["delta_g"] == 1000
    # ручное движение пересчитывает кэш остатка
    assert await db.add_stock_ledger(sid, -250, "print", ref_type="request", ref_id=7) == 750
    assert (await db.get_spool(sid))["remaining_g"] == 750
    assert await db.spool_stock_total(pid) == 750


async def test_deduct_from_spools_fifo(tmp_db):
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    s1 = await db.add_spool(pid, "синий", 100)
    s2 = await db.add_spool(pid, "синий", 100)
    took = await db.deduct_from_spools(pid, "Синий", 150)     # canon-match, FIFO старые сначала
    assert took == 150
    assert (await db.get_spool(s1))["remaining_g"] == 0 and (await db.get_spool(s1))["status"] == "empty"
    assert (await db.get_spool(s2))["remaining_g"] == 50 and (await db.get_spool(s2))["status"] == "active"
    # цвет без катушек — списывать нечего (best-effort, учёт катушками не ведётся)
    assert await db.deduct_from_spools(pid, "зелёный", 10) == 0.0


async def test_spool_stock_by_color_merges_case(tmp_db):
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.add_spool(pid, "Чёрный", 500)
    await db.add_spool(pid, "черный", 300)          # тот же canon-цвет
    agg = await db.spool_stock_by_color(pid)
    assert agg["черный"]["stock"] == 800 and agg["черный"]["spools"] == 2


async def test_dual_write_on_deduct_color(tmp_db):
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=1000)
    await db.set_color_stock(pid, "Оранжевый", 400, 50)
    sid = await db.add_spool(pid, "оранжевый", 400)
    await db.deduct_color_stock(pid, "Оранжевый", 120)         # legacy + зеркало в катушку
    assert (await db.get_spool(sid))["remaining_g"] == 280      # катушка хранит canon-цвет
    assert (await db.get_color_stock(pid))["Оранжевый"]["stock"] == 280   # legacy сохраняет вид


async def test_color_availability_merges_spool_and_legacy(tmp_db):
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.set_color_stock(pid, "Красный", 200, 20)          # только legacy
    await db.add_spool(pid, "Зелёный", 500, low_threshold=50)  # только катушка
    av = await db.color_availability(pid)
    assert av["красный"]["stock"] == 200
    assert av["зеленый"]["stock"] == 500 and av["зеленый"]["low"] == 50


async def test_availability_legacy_wins_over_stale_spool(tmp_db):
    """Переходный период (§4.5): если по цвету есть И legacy, И катушка — клиентский сигнал берёт
    LEGACY (источник истины старой админки), а не устаревшую катушку. Катушки — только для цветов
    без legacy-строки. Закрывает риск: пополнил legacy старым UI → dual-write не тронул катушку."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.add_spool(pid, "Синий", 500)                 # катушка 500 (устареет)
    await db.set_color_stock(pid, "Синий", 1000, 0)       # мастер пополнил legacy до 1000
    assert (await db.color_availability(pid))["синий"]["stock"] == 1000   # клиент видит legacy, не катушку
    await db.add_spool(pid, "Зелёный", 300)               # цвет только с катушкой (без legacy)
    assert (await db.color_availability(pid))["зеленый"]["stock"] == 300  # катушка заполняет пробел


async def test_emptied_spool_reads_out_of_stock(tmp_db):
    """Цвет с опустевшей катушкой обязан показываться «нет в наличии» (остаток 0), а не исчезать
    из сигнала (иначе клиент увидел бы его доступным) — регресс vs legacy-строки с 0 г."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    sid = await db.add_spool(pid, "Синий", 100)
    await db.deduct_from_spools(pid, "Синий", 100)
    assert (await db.get_spool(sid))["status"] == "empty"
    assert (await db.color_availability(pid))["синий"]["stock"] == 0


async def test_ref_linkage_and_cogs(tmp_db):
    """Списание по заказу линкуется к нему в stock_ledger (ref_type='request'), и по цене катушки
    считается реальная себестоимость requests.cogs_actual (Фаза B-линковка + Фаза C-cogs)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=1000)
    await db.set_color_stock(pid, "Оранжевый", 1000, 0)
    await db.add_spool(pid, "Оранжевый", 1000, cost=20.0)      # 1000 г за 20 у.е. → 0.02 у.е./г
    rid = await db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                               plastic_name="P", quantity=1, quality="0.2", strength="н",
                               grams_total=150, minutes_total=30, price=10, preview_path=None)
    await db.set_order_stock_plan(rid, [{"plastic_id": pid, "name": "P", "colors": ["Оранжевый"],
                                         "grams": 150, "color_grams": [150]}])
    await db.apply_order_stock(rid)
    sp = (await db.list_spools(pid))[0]
    consume = [x for x in await db.spool_ledger(sp["id"]) if x["delta_g"] < 0]
    assert consume and consume[0]["ref_type"] == "request" and consume[0]["ref_id"] == rid
    cur = await db._conn().execute("SELECT cogs_actual FROM requests WHERE id=?", (rid,))
    assert round((await cur.fetchone())["cogs_actual"], 2) == 3.00   # 150 г × 0.02


async def test_cogs_on_request_and_summaries(tmp_db):
    """Фаза C: cogs_actual доступен на Request; inventory_summary/cogs_summary дают склад и маржу."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=1000)
    await db.set_color_stock(pid, "Оранжевый", 1000, 0)
    await db.add_spool(pid, "Оранжевый", 1000, cost=20.0)      # 0.02 у.е./г
    rid = await db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                               plastic_name="P", quantity=1, quality="0.2", strength="н",
                               grams_total=150, minutes_total=30, price=10, preview_path=None)
    await db.set_order_stock_plan(rid, [{"plastic_id": pid, "name": "P", "colors": ["Оранжевый"],
                                         "grams": 150, "color_grams": [150]}])
    await db.update_request(rid, status="printed")
    await db.apply_order_stock(rid)
    assert round((await db.get_request(rid)).cogs_actual, 2) == 3.00      # cogs на Request
    inv = await db.inventory_summary()
    assert inv["active_spools"] == 1 and inv["priced_spools"] == 1
    cog = await db.cogs_summary()
    assert cog["orders_with_cogs"] == 1 and cog["revenue"] == 10.0
    assert cog["cogs"] == 3.0 and cog["margin"] == 7.0


async def test_print_journal_source(tmp_db):
    """Фаза B (print_events lite): source в журнале печатей — order/self/test; None выводится из
    request_id (привязка к заказу → 'order', иначе 'self')."""
    db = tmp_db
    j_order = await db.add_print_journal(label="o", plastic_id=None, plastic_name="P",
                                         grams=10, minutes=20, request_id=5)
    j_self = await db.add_print_journal(label="s", plastic_id=None, plastic_name="P",
                                        grams=5, minutes=10)
    j_test = await db.add_print_journal(label="t", plastic_id=None, plastic_name="P",
                                        grams=1, minutes=2, source="test")
    cur = await db._conn().execute("SELECT id, source FROM print_journal")
    src = {r["id"]: r["source"] for r in await cur.fetchall()}
    assert src[j_order] == "order" and src[j_self] == "self" and src[j_test] == "test"


async def test_print_events_merge_spool_and_predict(tmp_db):
    """Фаза B (print_events): self_prints пишутся в единый журнал (source='self'); печать с
    катушкой списывает её через stock_ledger (ref_type='journal'). Этап 5: predict_minutes спит на
    малой выборке (applied=False) и применяет медианную коррекцию при накоплении."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.add_self_print("Elegoo PLA", 25, 40, "калибровка")          # → print_journal, source='self'
    cur = await db._conn().execute(
        "SELECT source, grams FROM print_journal WHERE label='калибровка'")
    row = await cur.fetchone()
    assert row and row["source"] == "self" and row["grams"] == 25
    sid = await db.add_spool(pid, "синий", 1000)
    jid = await db.add_print_journal(label="p", plastic_id=pid, plastic_name="P", grams=30,
                                     minutes=20, spool_id=sid, source="test", est_minutes=18)
    assert (await db.get_spool(sid))["remaining_g"] == 970                # печать списала катушку
    led = [x for x in await db.spool_ledger(sid) if x["ref_type"] == "journal"]
    assert led and led[0]["ref_id"] == jid and led[0]["delta_g"] == -30
    # порог выборки задаём явно: боевой по умолчанию поднят до 30 (решение владельца
    # 30.08.2026), а этот тест про склейку событий печати с катушкой, а не про порог
    assert (await db.predict_minutes(100, None, 8))["applied"] is False   # <8 замеров — спит
    for _ in range(8):                                                    # 8 пар в журнале ×1.2
        await db.add_print_journal(label="s", plastic_id=None, plastic_name="PLA", grams=1,
                                   minutes=120, graded=1, est_minutes=100, material_type="PLA")
    p = await db.predict_minutes(100, "PLA", 8)
    assert p["applied"] is True and abs(p["factor"] - 1.2) < 0.01 and p["predicted"] == 120


async def test_backfill_migration_v3(tmp_db):
    """Бэкфилл прод-БД в катушки: имитируем БД до Фазы A (user_version=2, legacy-склад с дублем
    цвета по регистру), прогоняем init() ещё раз и проверяем миграцию v3."""
    db = tmp_db
    conn = db._conn()
    pid1 = (await db.list_plastics())[0].id
    pid2 = await db.add_plastic("PETG Test", 1000, 1.27, "PETG")
    await db.update_plastic(pid1, stock_grams=560, low_threshold=100)   # общий склад, без цветов
    await conn.execute("INSERT INTO plastic_color_stock(plastic_id,color,stock_grams,low_threshold) "
                       "VALUES(?,?,?,?)", (pid2, "Чёрный", 500, 50))
    await conn.execute("INSERT INTO plastic_color_stock(plastic_id,color,stock_grams,low_threshold) "
                       "VALUES(?,?,?,?)", (pid2, "черный", 300, 0))     # дубль по регистру
    await conn.execute("DELETE FROM spools")
    await conn.execute("DELETE FROM stock_ledger")
    await conn.execute("PRAGMA user_version = 2")                        # откат версии → прод до Фазы A
    await conn.commit()

    await db.close()                                                   # закрыть текущее соединение
    await db.init()                                                     # повторный прогон миграций (v3)

    cs = await db.get_color_stock(pid2)                                  # (a) дубли слиты, вид сохранён
    assert set(cs.keys()) == {"Чёрный"} and cs["Чёрный"]["stock"] == 800 and cs["Чёрный"]["low"] == 50
    by2 = await db.spool_stock_by_color(pid2)                            # (b) катушка на цвет (canon)
    assert by2["черный"]["stock"] == 800 and by2["черный"]["spools"] == 1
    by1 = await db.spool_stock_by_color(pid1)                            # (c) общий склад → катушка ''
    assert by1[""]["stock"] == 560 and by1[""]["low"] == 100
