"""Тесты бизнес-логики БД на временной БД: промокоды, склад, таймлайн заказа."""
import time

from app import config


async def test_promo_lifecycle(tmp_db):
    db = tmp_db
    await db.add_promo("SALE10", 10, 0, 2)
    assert await db.validate_promo("sale10") == 10        # case-insensitive
    await db.redeem_promo("SALE10")
    assert await db.validate_promo("SALE10") == 10        # 1/2 — ещё активен
    await db.redeem_promo("SALE10")
    assert await db.validate_promo("SALE10") is None       # 2/2 — исчерпан
    assert await db.validate_promo("NOPE") is None


async def test_promo_expired(tmp_db):
    db = tmp_db
    await db.add_promo("OLD", 5, int(time.time()) - 10, 0)
    assert await db.validate_promo("OLD") is None


async def test_order_reserves_then_deducts_on_print(tmp_db):
    """Заказ РЕЗЕРВИРУЕТ пластик, списание — при «напечатано» (OpenTodo a89586a2)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=1000, low_threshold=100)
    rid = await db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                               plastic_name="P", quantity=1, quality="0.2", strength="н",
                               grams_total=120, minutes_total=30, price=10, preview_path=None)
    await db.set_order_stock_plan(rid, [{"plastic_id": pid, "name": "P", "colors": [],
                                         "grams": 120, "color_grams": None}])
    # резерв виден, но склад НЕ тронут
    assert (await db.reserved_grams_by_plastic()).get(pid) == 120
    assert (await db.get_plastic(pid)).stock_grams == 1000
    # «напечатано» → списание; резерв снят (stock_deducted=1)
    await db.apply_order_stock(rid)
    assert (await db.get_plastic(pid)).stock_grams == 880
    assert (await db.reserved_grams_by_plastic()).get(pid) is None
    # повторное применение идемпотентно — второй раз не спишет
    await db.apply_order_stock(rid)
    assert (await db.get_plastic(pid)).stock_grams == 880


async def test_reservation_released_on_cancel(tmp_db):
    """Отменённый/отклонённый заказ освобождает резерв (он считается только по active)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=500)
    rid = await db.add_request(user_id=1, user_name="U", model_label="X", printer="P",
                               plastic_name="P", quantity=1, quality="0.2", strength="н",
                               grams_total=80, minutes_total=20, price=5, preview_path=None)
    await db.set_order_stock_plan(rid, [{"plastic_id": pid, "grams": 80, "colors": [], "color_grams": None}])
    assert (await db.reserved_grams_by_plastic()).get(pid) == 80
    await db.update_request(rid, status="cancelled")          # больше не active
    assert (await db.reserved_grams_by_plastic()).get(pid) is None
    assert (await db.get_plastic(pid)).stock_grams == 500      # склад не тронут (не печатали)


async def test_stock_deduct_and_threshold(tmp_db):
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=1000, low_threshold=200)
    new, crossed = await db.deduct_stock(pid, 50)
    assert new == 950 and crossed is False
    new, crossed = await db.deduct_stock(pid, 800)
    assert new == 150 and crossed is True                  # пересёк порог 200
    await db.update_plastic(pid, stock_grams=0)            # учёт выключен
    new, crossed = await db.deduct_stock(pid, 100)
    assert new == 0.0 and crossed is False


async def test_journal_personal_print_deducts_stock(tmp_db):
    """Печать ВНЕ заказа, занесённая в журнал с пластиком+граммами, списывает склад один раз
    (OpenTodo 05806f15)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=1000, low_threshold=100)
    jid = await db.add_print_journal(label="Брелок для себя", plastic_id=pid, plastic_name="P",
                                     grams=60, minutes=40)        # request_id=None → личная печать
    alerts = await db.apply_journal_stock(jid)
    assert (await db.get_plastic(pid)).stock_grams == 940
    assert alerts == []                                            # порог 100 не пересечён
    # идемпотентно — повторный вызов не списывает ещё раз
    await db.apply_journal_stock(jid)
    assert (await db.get_plastic(pid)).stock_grams == 940


async def test_journal_order_print_skips_stock_until_detached(tmp_db):
    """Печать, привязанная к заказу (request_id), склад НЕ трогает — за неё списывает заказ. После
    отвязки (вне заказа) — списывает (OpenTodo 05806f15)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=500)
    jid = await db.add_print_journal(label="Заказная деталь", plastic_id=pid, plastic_name="P",
                                     grams=70, minutes=30, request_id=42)
    assert await db.apply_journal_stock(jid) == []
    assert (await db.get_plastic(pid)).stock_grams == 500          # заказная — журнал не списал
    # мастер отвязал заказ (печать оказалась личной) → теперь списываем
    await db.set_print_journal_fields(jid, request_id=None)
    await db.apply_journal_stock(jid)
    assert (await db.get_plastic(pid)).stock_grams == 430


async def test_journal_low_stock_alert_and_no_plastic(tmp_db):
    """Списание журнальной печати возвращает алерт при пересечении порога; без пластика — no-op."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=120, low_threshold=100)
    jid = await db.add_print_journal(label="Большая печать", plastic_id=pid, plastic_name="PETG",
                                     grams=40, minutes=200)
    alerts = await db.apply_journal_stock(jid)
    assert alerts and alerts[0][0] == "PETG" and alerts[0][1] == 80   # 120-40=80 < порог 100
    # запись без пластика — списывать нечего, без ошибок
    jid2 = await db.add_print_journal(label="Без пластика", plastic_id=None, plastic_name="",
                                      grams=30, minutes=10)
    assert await db.apply_journal_stock(jid2) == []


async def test_journal_stock_backfill_survives_restart(tmp_db):
    """Рестарт (повторный init) НЕ должен помечать свежую, ещё не списанную печать как учтённую —
    бэкфилл stock_deducted=1 идёт только при первом добавлении столбца (OpenTodo 05806f15)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    await db.update_plastic(pid, stock_grams=300)
    jid = await db.add_print_journal(label="Личная печать", plastic_id=pid, plastic_name="P",
                                     grams=50, minutes=20)        # stock_deducted=0 (не списана)
    await db.init()                                                # имитируем рестарт бота
    await db.apply_journal_stock(jid)                             # списание всё ещё должно сработать
    assert (await db.get_plastic(pid)).stock_grams == 250


async def test_plastic_failure_rate_override(tmp_db):
    """Индивидуальный % брака пластика хранится и сбрасывается в общий (NULL)."""
    db = tmp_db
    pid = (await db.list_plastics())[0].id
    assert (await db.get_plastic(pid)).failure_rate_pct is None     # по умолчанию — общий
    await db.update_plastic(pid, failure_rate_pct=15.0)
    assert (await db.get_plastic(pid)).failure_rate_pct == 15.0
    await db.update_plastic(pid, failure_rate_pct=0.0)              # 0% — валиден, не None
    assert (await db.get_plastic(pid)).failure_rate_pct == 0.0
    await db.update_plastic(pid, failure_rate_pct=None)            # сброс к общему
    assert (await db.get_plastic(pid)).failure_rate_pct is None


async def test_status_timeline(tmp_db):
    db = tmp_db
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None)
    assert [s for s, _ in await db.get_status_history(rid)] == ["new"]
    await db.update_request(rid, status="in_progress")
    await db.update_request(rid, status="printed")
    assert [s for s, _ in await db.get_status_history(rid)] == ["new", "in_progress", "printed"]


async def test_request_breakdown_roundtrip(tmp_db):
    """Расчётник цены сохраняется в заказе и читается обратно (для сметы в деталях заказа)."""
    db = tmp_db
    from app import pricing
    from app.db import Plastic
    s = await db.get_settings()
    plastic = Plastic(id=1, name="PLA", price_per_kg=1500, density=1.24, is_active=1)
    bd = pricing.compute_price(14.0, 55.0, 2, plastic, s, discount_pct=10)
    rid = await db.add_request(
        user_id=7, user_name="@u", model_label="m", printer="p", plastic_name="PLA",
        quantity=2, quality="q", strength="s", grams_total=bd.grams_total,
        minutes_total=bd.minutes_total, price=bd.total, preview_path=None,
        breakdown=pricing.breakdown_to_dict(bd, s))
    r = await db.get_request(rid)
    assert isinstance(r.breakdown, dict)
    restored = pricing.breakdown_from_dict(r.breakdown)
    assert abs(restored.total - bd.total) < 1e-9
    assert abs(restored.filament_cost - bd.filament_cost) < 1e-9
    assert r.breakdown["profit_margin_pct"] == s["profit_margin_pct"]
    # заказ без разбивки (старый/индивидуальный) — breakdown None, не падаем
    rid2 = await db.add_request(
        user_id=7, user_name="@u", model_label="m2", printer="", plastic_name="",
        quantity=1, quality="", strength="", grams_total=0, minutes_total=0,
        price=0, preview_path=None)
    assert (await db.get_request(rid2)).breakdown is None


async def test_custom_order_roundtrip(tmp_db):
    db = tmp_db
    rid = await db.add_request(
        user_id=42, user_name="@cust", model_label="Индивидуальный заказ", printer="",
        plastic_name="", quantity=1, quality="", strength="", grams_total=0, minutes_total=0,
        price=0, preview_path="/data/custom_orders/42_0.jpg", is_custom=1,
        description="Подставка под телефон, ~12 см", photos=["a.jpg", "b.jpg"])
    r = await db.get_request(rid)
    assert r.is_custom == 1
    assert r.description == "Подставка под телефон, ~12 см"
    # C2: список путей хранится относительно DATA_DIR, наружу — абсолютными
    assert r.photos == [str(config.DATA_DIR / "a.jpg"), str(config.DATA_DIR / "b.jpg")]
    assert r.status == "new"
    # обычный заказ остаётся не-кастомным и с пустыми фото
    rid2 = await db.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None)
    r2 = await db.get_request(rid2)
    assert r2.is_custom == 0 and r2.photos == [] and r2.description is None


async def test_referral_flow(tmp_db):
    """Рефералка (OpenTodo 7f257a33): бонус НА СЧЁТ обоим, один раз, ТОЛЬКО после первого
    напечатанного И оплаченного заказа приглашённого. Скидок больше не даёт."""
    db = tmp_db
    assert await db.add_referral(200, 100) is True        # 200 приглашён 100-м
    assert await db.add_referral(200, 999) is False       # повторно — нельзя
    assert await db.count_referrals(100) == 1
    assert await db.award_referral_if_due(200, 5) is None  # заказов ещё нет — рано
    rid = await db.add_request(
        user_id=200, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None)
    assert await db.award_referral_if_due(200, 5) is None  # просто оформлен — рано
    await db.update_request(rid, status="printed")
    assert await db.award_referral_if_due(200, 5) is None  # напечатан, но НЕ оплачен — рано
    await db.update_request(rid, paid=1)
    assert (await db.get_request(rid)).paid == 1
    ref = await db.award_referral_if_due(200, 5)           # напечатан + оплачен → бонус
    assert ref == 100
    assert await db.get_balance(200) == 5                  # бонус ОБОИМ на счёт
    assert await db.get_balance(100) == 5
    assert await db.get_user_discount(100) == 0            # скидок рефералка не даёт
    assert await db.award_referral_if_due(200, 5) is None  # повторно не награждаем
    assert await db.get_balance(100) == 5


async def test_list_customers(tmp_db):
    """OpenTodo 51c8097e: пикер клиентов — уникальные user_id из заказов, свежее имя,
    число заказов и текущая персональная скидка (если есть)."""
    db = tmp_db

    async def mk(uid, name):
        return await db.add_request(
            user_id=uid, user_name=name, model_label="m", printer="p", plastic_name="x",
            quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
            price=1, preview_path=None)

    await mk(10, "Аня")
    await mk(10, "Аня Б.")          # тот же клиент, более свежее имя
    await mk(20, "Боря")
    await db.set_user_discount(20, 15, "Боря")
    by = {c["uid"]: c for c in await db.list_customers()}
    assert set(by) == {10, 20}
    assert by[10]["orders"] == 2 and by[10]["name"] == "Аня Б."
    assert by[10]["pct"] is None and by[20]["pct"] == 15


async def test_user_profile_stats(tmp_db):
    """OpenTodo 7a5be39c: статистика клиента — отменённые не считаем, материалы по базовому имени."""
    db = tmp_db

    async def mk(status, plastic, price, review=None):
        rid = await db.add_request(
            user_id=5, user_name="t", model_label="m", printer="p", plastic_name=plastic,
            quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
            price=price, preview_path=None)
        if status != "new":
            await db.update_request(rid, status=status)
        if review:
            await db.update_request(rid, review=review)
        return rid

    await mk("printed", "PLA · красный", 30, review="ок")
    await mk("printed", "PETG", 40)
    await mk("new", "PLA · синий", 10)
    await mk("cancelled", "ABS", 5)
    s = await db.user_profile_stats(5)
    assert s["orders"] == 3 and s["printed"] == 2 and s["spent"] == 70
    assert s["materials"] == 2 and s["reviews"] == 1   # PLA (без цвета) + PETG; синий PLA не печатался


async def test_update_request_edits_params(tmp_db):
    """OpenTodo 42ce4724: update_request принимает редактируемые параметры заказа (пластик/
    качество/прочность/тираж/id), а не только статус/цену."""
    db = tmp_db
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="PLA",
        quantity=1, quality="0.2", strength="норма", grams_total=10, minutes_total=20,
        price=5, preview_path=None)
    await db.update_request(rid, plastic_name="PETG · синий", quality="0.12", strength="прочно",
                            quantity=3, quality_id=2, strength_id=4, price=15,
                            grams_total=30, minutes_total=60)
    r = await db.get_request(rid)
    assert r.plastic_name == "PETG · синий" and r.quantity == 3 and r.quality == "0.12"
    assert r.quality_id == 2 and r.strength_id == 4 and r.price == 15


async def test_activity_log(tmp_db):
    """OpenTodo f0602c66: лента активности — события пишутся, читаются новые сверху, фильтр по user."""
    db = tmp_db
    await db.log_event(1, "открыл бота", "", user_name="Аня")
    await db.log_event(1, "рассчитал цену", "Benchy ×1", user_name="Аня")
    await db.log_event(2, "загрузил модель", "Cat", user_name="Боря")
    allev = await db.list_activity()
    assert [e["action"] for e in allev] == ["загрузил модель", "рассчитал цену", "открыл бота"]
    mine = await db.list_activity(user_id=1)
    assert len(mine) == 2 and all(e["user_id"] == 1 for e in mine)
    assert mine[0]["detail"] == "Benchy ×1" and mine[0]["name"] == "Аня"


async def test_bigdata_stats(tmp_db):
    """OpenTodo 7507fba0: сводка Big Data — точность модели времени + материалы, без падений на пустой БД."""
    db = tmp_db
    empty = await db.bigdata_stats()
    assert empty["samples"]["count"] == 0 and empty["totals"]["coverage_pct"] == 0
    await db.add_print_journal(label="h1", plastic_id=None, plastic_name="PLA", grams=1,
                               minutes=110, graded=1, est_minutes=100, material_type="PLA")
    await db.add_print_journal(label="h2", plastic_id=None, plastic_name="PLA", grams=1,
                               minutes=190, graded=1, est_minutes=200, material_type="PLA")
    st = await db.bigdata_stats()
    assert st["samples"]["count"] == 2 and st["samples"]["within20_pct"] == 100.0
    assert any(m["material"] == "PLA" and m["count"] == 2 for m in st["samples_by_material"])


async def test_revenue_counts_only_printed(tmp_db):
    """Выручка (revenue_all/printed, series14, тренд, daily_revenue) — только по статусу 'printed'."""
    db = tmp_db

    async def _req(price: float, status: str | None = None) -> int:
        rid = await db.add_request(
            user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
            quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
            price=price, preview_path=None)
        if status:
            await db.update_request(rid, status=status)
        return rid

    await _req(100.0, "printed")     # напечатан → попадает в выручку
    await _req(50.0, "printed")      # напечатан → попадает в выручку
    await _req(999.0)                # new → не учитывается
    await _req(777.0, "in_progress")  # в работе → не учитывается
    await _req(555.0, "rejected")    # отказ → не учитывается

    d = await db.dashboard_stats()
    assert d["orders"] == 5                         # заказов всего — все статусы
    assert d["printed"] == 2
    assert d["revenue_printed"] == 150.0            # 100 + 50
    assert d["revenue_all"] == 150.0                # выручка == реализованная
    assert d["avg_order"] == 75.0                   # средний чек по напечатанным (150/2)
    assert d["orev_7d"] == 150.0 and d["orev_30d"] == 150.0
    assert sum(s["revenue"] for s in d["series14"]) == 150.0
    assert sum(s["orders"] for s in d["series14"]) == 5   # в ряду заказов — все статусы

    rows = await db.daily_revenue(30)
    assert sum(n for _, n, _ in rows) == 5          # заказов — все
    assert sum(rev for _, _, rev in rows) == 150.0  # выручка — только напечатанные


async def test_filament_and_materials_count_only_printed(tmp_db):
    """Филамент (grams) и топ материалов в дашборде — только по напечатанным заказам."""
    db = tmp_db

    async def _req(plastic: str, grams: float, status: str | None = None) -> int:
        rid = await db.add_request(
            user_id=1, user_name="t", model_label="m", printer="p", plastic_name=plastic,
            quantity=1, quality="q", strength="s", grams_total=grams, minutes_total=1,
            price=10, preview_path=None)
        if status:
            await db.update_request(rid, status=status)
        return rid

    await _req("PLA", 100.0, "printed")
    await _req("PLA", 50.0, "printed")
    await _req("PETG", 30.0, "printed")
    await _req("PLA", 999.0)                  # new → не считается
    await _req("ABS", 777.0, "in_progress")   # в работе → не считается

    d = await db.dashboard_stats()
    assert d["grams"] == 180.0                # 100 + 50 + 30 (только printed)
    assert d["top_materials"] == [("PLA", 2), ("PETG", 1)]   # ABS/новый PLA не в счёт


async def test_print_samples_crud(tmp_db):
    """Образцы времени печати (Этап 0): запись, подсчёт, чтение последних сверху."""
    db = tmp_db
    assert await db.print_sample_count() == 0
    sid = await db.add_print_sample(request_id=None, model_hash="abc", est_minutes=100.0,
                                    actual_minutes=120.0, grams=42.0, material_type="PLA",
                                    extra_colors=1, source="manual")
    assert sid > 0
    await db.add_print_sample(request_id=5, model_hash="def", est_minutes=60.0,
                              actual_minutes=55.0, source="telemetry")
    assert await db.print_sample_count() == 2
    rows = await db.list_print_samples()
    assert len(rows) == 2
    assert rows[0]["model_hash"] == "def"          # ORDER BY id DESC — последняя сверху
    assert rows[1]["actual_minutes"] == 120.0
    assert rows[1]["material_type"] == "PLA"


async def test_color_stock_set_get_deduct(tmp_db):
    """Остаток по цветам: set/get, списание из нужного цвета + порог, откат на общий склад."""
    db = tmp_db
    pid = await db.add_plastic("PLA", 1000, 1.24, "PLA")
    await db.set_color_stock(pid, "Красный", 500, 50)
    await db.set_color_stock(pid, "Синий", 200, 0)
    cs = await db.get_color_stock(pid)
    assert cs["Красный"] == {"stock": 500.0, "low": 50.0}
    assert cs["Синий"]["stock"] == 200.0

    new, crossed = await db.deduct_color_stock(pid, "Красный", 100)   # 500 -> 400
    assert new == 400.0 and crossed is False
    new, crossed = await db.deduct_color_stock(pid, "Красный", 360)   # 400 -> 40, порог 50 пересечён
    assert new == 40.0 and crossed is True
    # цвет без пер-цветового склада + у материала общий склад не ведётся → откат, ничего не списываем
    new, crossed = await db.deduct_color_stock(pid, "Зелёный", 100)
    assert new == 0.0 and crossed is False


async def test_color_stock_split_by_extruder(tmp_db):
    """Многоцветный заказ: граммы каждого цвета списываются с его собственного склада
    пропорционально пер-экструдерному расходу (а не весь объём с первого цвета)."""
    db = tmp_db
    pid = await db.add_plastic("PLA", 1000, 1.24, "PLA")
    await db.set_color_stock(pid, "Красный", 500, 0)
    await db.set_color_stock(pid, "Синий", 500, 0)
    await db.set_color_stock(pid, "Жёлтый", 500, 0)
    # grams_total=120 делится по пер-экструдерным граммам 30/60/30 → 30/60/30 (сумма == 120)
    new_first, crossed = await db.deduct_color_stock_split(
        pid, ["Красный", "Синий", "Жёлтый"], 120.0, [30.0, 60.0, 30.0])
    cs = await db.get_color_stock(pid)
    assert cs["Красный"]["stock"] == 470.0     # 500 - 30
    assert cs["Синий"]["stock"] == 440.0       # 500 - 60
    assert cs["Жёлтый"]["stock"] == 470.0      # 500 - 30
    assert new_first == 470.0 and crossed is False   # возвращаем остаток ПЕРВОГО цвета


async def test_color_stock_split_scales_to_grams_total(tmp_db):
    """Разбивка масштабируется под grams_total (продувка на многоцвет + ×qty), сохраняя пропорции;
    сумма списанного по цветам сходится к grams_total."""
    db = tmp_db
    pid = await db.add_plastic("PLA", 1000, 1.24, "PLA")
    await db.set_color_stock(pid, "A", 1000, 0)
    await db.set_color_stock(pid, "B", 1000, 0)
    # пер-экструдерный объём в граммах 10/30 (сумма 40), но фактический grams_total=100 (с наценкой)
    await db.deduct_color_stock_split(pid, ["A", "B"], 100.0, [10.0, 30.0])
    cs = await db.get_color_stock(pid)
    assert cs["A"]["stock"] == 975.0           # 1000 - 100*(10/40)=25
    assert cs["B"]["stock"] == 925.0           # 1000 - 100*(30/40)=75


async def test_color_stock_split_threshold_on_first(tmp_db):
    """Порог «мало» возвращается по ПЕРВОМУ цвету (для текста алерта) при списании сплитом."""
    db = tmp_db
    pid = await db.add_plastic("PLA", 1000, 1.24, "PLA")
    await db.set_color_stock(pid, "Красный", 100, 50)   # порог 50
    await db.set_color_stock(pid, "Синий", 500, 0)
    new_first, crossed = await db.deduct_color_stock_split(
        pid, ["Красный", "Синий"], 90.0, [60.0, 30.0])
    assert new_first == 40.0 and crossed is True         # 100-60=40 ≤ 50 → пересекли порог


async def test_color_stock_split_fallback_single_color(tmp_db):
    """Одноцветный заказ (<2 цветов) → прежнее поведение: весь объём с единственного цвета."""
    db = tmp_db
    pid = await db.add_plastic("PLA", 1000, 1.24, "PLA")
    await db.set_color_stock(pid, "Красный", 500, 0)
    new, crossed = await db.deduct_color_stock_split(pid, ["Красный"], 120.0, [120.0])
    assert new == 380.0 and crossed is False             # 500 - 120 (весь объём)


async def test_color_stock_split_fallback_no_breakdown(tmp_db):
    """Нет пер-экструдерной разбивки → фолбэк: весь объём с ПЕРВОГО цвета, остальные не трогаем."""
    db = tmp_db
    pid = await db.add_plastic("PLA", 1000, 1.24, "PLA")
    await db.set_color_stock(pid, "Красный", 500, 0)
    await db.set_color_stock(pid, "Синий", 500, 0)
    # breakdown None
    new, crossed = await db.deduct_color_stock_split(pid, ["Красный", "Синий"], 120.0, None)
    cs = await db.get_color_stock(pid)
    assert cs["Красный"]["stock"] == 380.0     # весь объём с первого
    assert cs["Синий"]["stock"] == 500.0       # второй цвет не тронут
    assert new == 380.0 and crossed is False
    # breakdown не совпадает по длине с числом цветов → тоже фолбэк
    new, _ = await db.deduct_color_stock_split(pid, ["Красный", "Синий"], 80.0, [80.0])
    cs = await db.get_color_stock(pid)
    assert cs["Красный"]["stock"] == 300.0     # 380 - 80 (снова весь объём с первого)
    assert cs["Синий"]["stock"] == 500.0       # второй по-прежнему не тронут


async def test_color_stock_split_fallback_to_material_stock(tmp_db):
    """Сплит, но у второго цвета нет пер-цветового склада → его доля уходит на общий склад
    материала (deduct_color_stock сам откатывается на deduct_stock). Зеркалит прежний фолбэк."""
    db = tmp_db
    pid = await db.add_plastic("PLA", 1000, 1.24, "PLA")
    await db.update_plastic(pid, stock_grams=1000, low_threshold=0)   # общий склад ведётся
    await db.set_color_stock(pid, "Красный", 500, 0)                   # только у красного пер-цвет
    await db.deduct_color_stock_split(pid, ["Красный", "Синий"], 100.0, [40.0, 60.0])
    cs = await db.get_color_stock(pid)
    assert cs["Красный"]["stock"] == 460.0     # 500 - 40 с пер-цветового
    p = await db.get_plastic(pid)
    assert p.stock_grams == 940.0              # 1000 - 60 синего ушло на общий склад материала


async def test_service_accrual_and_reset(tmp_db):
    """ТО: сидинг узлов, начисление часов, пересечение порога ровно один раз, сброс при mark_serviced."""
    db = tmp_db
    items = await db.list_service_items()
    assert len(items) >= 7                                   # дефолтные узлы засеяны
    nozzle = next(i for i in items if i.key == "nozzle")     # порог 800 ч
    assert nozzle.trigger_type == "hours" and nozzle.threshold == 800

    crossed = await db.accrue_service(minutes=600 * 60, prints=1)   # +600 ч < 800 → не пора
    assert all(i.key != "nozzle" for i in crossed)
    crossed = await db.accrue_service(minutes=300 * 60, prints=1)   # +300 → 900 ч ≥ 800 → пересекли
    assert any(i.key == "nozzle" for i in crossed)
    await db.mark_service_notified(nozzle.id, 1)
    crossed = await db.accrue_service(minutes=60 * 60, prints=1)    # уже notified → второго алерта нет
    assert all(i.key != "nozzle" for i in crossed)

    await db.mark_serviced(nozzle.id, "поставил закалённое 0.4")
    n2 = await db.get_service_item(nozzle.id)
    assert n2.hours_since == 0 and n2.prints_since == 0 and n2.notified == 0
    log = await db.list_service_log(nozzle.id)
    assert len(log) == 1 and log[0]["note"] == "поставил закалённое 0.4"


async def test_service_prints_trigger(tmp_db):
    """Триггер по числу печатей: bed_surface (порог 250 печатей)."""
    db = tmp_db
    bs = next(i for i in await db.list_service_items() if i.key == "bed_surface")
    assert bs.trigger_type == "prints" and bs.threshold == 250
    crossed = await db.accrue_service(minutes=10, prints=250)
    assert any(i.key == "bed_surface" for i in crossed)


async def test_service_item_crud(tmp_db):
    """CRUD пунктов ТО: добавить, сменить порог (сброс notified), выключить, удалить."""
    db = tmp_db
    n0 = len(await db.list_service_items())
    iid = await db.add_service_item("Калибровка экструдера", "prints", 300)
    assert len(await db.list_service_items()) == n0 + 1
    it = await db.get_service_item(iid)
    assert it.label == "Калибровка экструдера" and it.trigger_type == "prints" and it.threshold == 300
    await db.mark_service_notified(iid, 1)
    await db.update_service_item(iid, threshold=500)        # смена порога снимает notified
    it2 = await db.get_service_item(iid)
    assert it2.threshold == 500 and it2.notified == 0
    await db.update_service_item(iid, is_active=0)
    assert (await db.get_service_item(iid)).is_active == 0
    await db.delete_service_item(iid)
    assert await db.get_service_item(iid) is None


async def test_delete_request_cascades(tmp_db):
    """delete_request убирает сам заказ и его дочерние записи (история статусов, переписка)
    и возвращает удалённый Request; для отсутствующего id — None."""
    db = tmp_db
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=10, preview_path=None)
    await db.update_request(rid, status="in_progress")        # +строка в таймлайне
    await db.add_order_message(rid, from_admin=True, text="привет")  # +строка переписки

    deleted = await db.delete_request(rid)
    assert deleted is not None and deleted.id == rid
    assert await db.get_request(rid) is None
    assert await db.get_status_history(rid) == []             # история подчищена
    assert await db.list_order_messages(rid) == []            # переписка подчищена
    assert await db.delete_request(rid) is None               # повторно — нечего удалять


async def test_delete_test_requests_only_empty(tmp_db):
    """Массовая чистка удаляет только заказы без цены и без расчёта (0 г / 0 мин);
    реальные заказы (с ценой или граммами/минутами) остаются."""
    db = tmp_db
    real = await db.add_request(
        user_id=1, user_name="t", model_label="real", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=5, minutes_total=10,
        price=20, preview_path=None)
    # тестовый: индивидуальный заказ без цены и без расчёта
    custom = await db.add_request(
        user_id=2, user_name="c", model_label="Индивидуальный заказ", printer="",
        plastic_name="", quantity=1, quality="", strength="", grams_total=0, minutes_total=0,
        price=0, preview_path=None, is_custom=1, description="тест")
    # тестовый: рассчитанный, но 0 цена и 0 граммы/минуты
    empty = await db.add_request(
        user_id=3, user_name="e", model_label="m", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=0, minutes_total=0,
        price=0, preview_path=None)

    deleted = await db.delete_test_requests()
    ids = {r.id for r in deleted}
    assert ids == {custom, empty}
    assert await db.get_request(real) is not None             # реальный заказ цел
    assert await db.get_request(custom) is None
    assert await db.get_request(empty) is None


def test_parse_due_date():
    """Срок исполнения «ДД.ММ» из свободного ввода админа: нормализуем, мусор → None."""
    from app import db
    assert db.parse_due_date("10.06") == "10.06"
    assert db.parse_due_date("1.6") == "01.06"            # дополняем нулями
    assert db.parse_due_date("10.06.2026") == "10.06"     # год отбрасываем
    assert db.parse_due_date("10/06") == "10.06"          # разделитель /
    assert db.parse_due_date("29.02") == "29.02"          # 29 февраля допустимо (валидация по високосному)
    assert db.parse_due_date("") is None
    assert db.parse_due_date("-") is None
    assert db.parse_due_date("завтра") is None
    assert db.parse_due_date("32.01") is None             # нет такого дня
    assert db.parse_due_date("10.13") is None             # нет такого месяца


async def test_due_date_roundtrip_and_notify(tmp_db):
    """Срок исполнения сохраняется в заказе при переводе «в работу» и попадает в уведомление."""
    from types import SimpleNamespace

    from app import texts
    db = tmp_db
    rid = await db.add_request(
        user_id=1, user_name="t", model_label="Ваза", printer="p", plastic_name="x",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None)
    assert (await db.get_request(rid)).due_date == ""     # по умолчанию срока нет
    await db.update_request(rid, status="in_progress", due_date="10.06")
    r = await db.get_request(rid)
    assert r.due_date == "10.06"
    note = texts.user_status_note(r, "руб.")
    assert "В работе" in note and "Срок исполнения" in note and "10.06" in note
    # без срока — строки про срок в уведомлении нет
    bare = SimpleNamespace(id=9, model_label="Ваза", status="in_progress", due_date="")
    assert "Срок исполнения" not in texts.user_status_note(bare, "руб.")
    # для других статусов срок не показываем, даже если он записан в заказе
    printed = SimpleNamespace(id=9, model_label="Ваза", status="printed", due_date="10.06")
    assert "Срок исполнения" not in texts.user_status_note(printed, "руб.")


async def test_default_plastic_mutual_exclusion(tmp_db):
    db = tmp_db
    p1 = await db.add_plastic("A", 100, 1.24)
    p2 = await db.add_plastic("B", 100, 1.24)
    await db.set_default_plastic(p1)
    await db.set_default_plastic(p2)
    defaults = [p.id for p in await db.list_plastics() if p.is_default]
    assert defaults == [p2]                                # ровно один дефолт


async def test_is_self_order(tmp_db, monkeypatch):
    """Заказ мастера самому себе (фидбэк 20.07: «странно самому себе ставить оценку»).
    Критерий должен совпадать с SQL-предикатом, который делит списки client/self."""
    from app import config
    db = tmp_db
    monkeypatch.setattr(config, "ADMIN_IDS", [777])
    rid_self = await db.add_request(
        user_id=777, user_name="@master", model_label="Своё", printer="P", plastic_name="PETG",
        quantity=1, quality="0.2", strength="норм", grams_total=10, minutes_total=10,
        price=1, preview_path=None)
    rid_client = await db.add_request(
        user_id=555, user_name="@client", model_label="Клиентское", printer="P", plastic_name="PETG",
        quantity=1, quality="0.2", strength="норм", grams_total=10, minutes_total=10,
        price=1, preview_path=None)
    assert db.is_self_order(await db.get_request(rid_self)) is True      # заказчик = админ
    assert db.is_self_order(await db.get_request(rid_client)) is False   # настоящий клиент
    assert db.is_self_order(None) is False
    # и то же самое видит SQL-фильтр списков: свой заказ не в клиентском списке
    client_ids = [r.id for r in await db.list_requests(50, kind="client")]
    self_ids = [r.id for r in await db.list_requests(50, kind="self")]
    assert rid_client in client_ids and rid_self not in client_ids
    assert rid_self in self_ids
