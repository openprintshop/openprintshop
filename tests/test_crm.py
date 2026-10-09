"""CRM-слой (crm-roadmap.md, OpenTodo a8b591cf): агрегаты клиента, сегменты, ручные поля,
задачи-напоминания, охват рассылки, причины отказов, сквозной поиск."""
import time


async def _mk(db, uid, name, status="new", price=10.0, created_at=None):
    rid = await db.add_request(
        user_id=uid, user_name=name, model_label=f"Модель {uid}", printer="P",
        plastic_name="PLA", quantity=1, quality="0.2", strength="норм",
        grams_total=10, minutes_total=30, price=price, preview_path=None)
    if status != "new":
        await db.update_request(rid, status=status)
    if created_at is not None:
        await db._conn().execute("UPDATE requests SET created_at = ? WHERE id = ?",
                                 (created_at, rid))
        await db._conn().commit()
    return rid


# ───────── этап 0: агрегаты карточки ─────────
async def test_customer_card_aggregates(tmp_db):
    db = tmp_db
    await _mk(db, 1, "Аня", status="printed", price=30)
    await _mk(db, 1, "Аня", status="new", price=99)        # не printed → в LTV не входит
    card = await db.customer_card(1)
    assert card["orders"] == 2 and card["printed"] == 1
    assert card["spent"] == 30 and card["avg_check"] == 30
    assert card["name"] == "Аня" and card["fav_material"] == "PLA"
    assert len(card["recent"]) == 2
    assert await db.customer_card(404) is None


async def test_customer_manual_fields_override(tmp_db):
    db = tmp_db
    await _mk(db, 2, "@vasya")
    await db.upsert_customer(2, name="Вася (сосед)", note="забирает по вторникам", phone="+375…")
    card = await db.customer_card(2)
    assert card["name"] == "Вася (сосед)" and card["tg_name"] == "@vasya"
    assert card["note"] == "забирает по вторникам"
    # карточка без заказов, но с ручной записью — существует
    await db.upsert_customer(77, note="написал в личку")
    assert (await db.customer_card(77))["orders"] == 0


# ───────── этап 3: вычисляемые сегменты ─────────
async def test_crm_segments_computed(tmp_db):
    db = tmp_db
    now = int(time.time())
    await _mk(db, 1, "Новичок")                                            # 1 заказ
    await _mk(db, 2, "Кит", status="printed", price=500)                   # VIP
    await _mk(db, 2, "Кит", status="printed", price=500)
    await _mk(db, 3, "Соня", created_at=now - 90 * 86400)                  # спит 90 дней
    await db.set_setting("crm_vip_spent", 100)
    await db.set_setting("crm_sleep_days", 45)
    by_uid = {c["uid"]: c for c in await db.list_customers_crm()}
    assert by_uid[1]["segments"] == ["new"]
    assert "vip" in by_uid[2]["segments"] and "repeat" in by_uid[2]["segments"]
    assert "sleeping" in by_uid[3]["segments"]
    sleepy = await db.list_customers_crm(segment="sleeping")
    assert [c["uid"] for c in sleepy] == [3]


async def test_list_customers_search_and_sort(tmp_db):
    db = tmp_db
    await _mk(db, 1, "Аня", status="printed", price=10)
    await _mk(db, 2, "Боря", status="printed", price=200)
    assert [c["uid"] for c in await db.list_customers_crm(search="бор")] == [2]
    assert [c["uid"] for c in await db.list_customers_crm(search="1")] == [1]
    assert [c["uid"] for c in await db.list_customers_crm(sort="ltv")][0] == 2


# ───────── этап 4: задачи-напоминания ─────────
async def test_crm_tasks_due_flow(tmp_db):
    db = tmp_db
    past = int(time.time()) - 60
    tid = await db.add_crm_task("Перезвонить Ане", due_at=past, user_id=1)
    await db.add_crm_task("Без срока")
    due = await db.crm_tasks_due()
    assert [t["id"] for t in due] == [tid]
    await db.mark_crm_task_notified(tid)
    assert await db.crm_tasks_due() == []          # повторно не напоминаем
    assert await db.set_crm_task_done(tid) is True
    assert all(t["id"] != tid for t in await db.list_crm_tasks())
    assert any(t["id"] == tid for t in await db.list_crm_tasks(include_done=True))


# ───────── этап 5: охват рассылки ─────────
async def test_broadcast_targets_safe_defaults(tmp_db):
    db = tmp_db
    await _mk(db, 1, "Аня")
    await _mk(db, 2, "Боря")
    await _mk(db, 3, "Вера")
    await db.upsert_customer(2, no_broadcast=1)                            # отписан
    await db.upsert_customer(3, last_broadcast_at=int(time.time()) - 3600)  # слали час назад
    t = await db.crm_broadcast_targets()
    assert t["total"] == 3 and t["skipped_optout"] == 1 and t["skipped_recent"] == 1
    assert [x["uid"] for x in t["eligible"]] == [1]


# ───────── этап 6: причины отказов + воронка периодов ─────────
async def test_crm_dashboard_reject_reasons(tmp_db):
    db = tmp_db
    rid = await _mk(db, 1, "Аня")
    await db.update_request(rid, status="rejected", reject_reason="дорого")
    rid2 = await _mk(db, 2, "Боря")
    await db.update_request(rid2, status="rejected", reject_reason="дорого")
    await _mk(db, 3, "Вера", status="printed")
    d = await db.crm_dashboard()
    assert d["reject_reasons"][0] == ["дорого", 2]
    assert d["rejected_total"] == 2
    assert d["7d"]["orders"] == 3 and d["7d"]["printed"] == 1


# ───────── этап 2: сквозной поиск по заказам ─────────
async def test_list_requests_search(tmp_db):
    db = tmp_db
    rid = await _mk(db, 1, "Аня")
    await _mk(db, 2, "Боря")
    assert [r.id for r in await db.list_requests(search=f"#{rid}")] == [rid]
    assert [r.id for r in await db.list_requests(search="Аня")] == [rid]
    assert len(await db.list_requests(search="Модель")) == 2
    assert await db.list_requests(search="нет такого") == []


# ───────── клиенты со скидкой/карточкой, но без заказов, не теряются (2884565c) ─────────
async def test_clients_without_orders_listed(tmp_db):
    db = tmp_db
    await _mk(db, 1, "Аня")
    await db.set_user_discount(99, 15, "Лёша (скидка из ТГ)")
    await db.upsert_customer(77, note="написал в личку, заказов нет")
    by_uid = {c["uid"]: c for c in await db.list_customers_crm()}
    assert 99 in by_uid and by_uid[99]["discount_pct"] == 15
    assert by_uid[99]["name"] == "Лёша (скидка из ТГ)" and by_uid[99]["orders"] == 0
    assert 77 in by_uid and by_uid[77]["orders"] == 0
    # визитов в бота у них нет — «посетителями» их назвать нельзя, сегмент прежний
    assert by_uid[99]["segments"] == ["new"] and by_uid[77]["segments"] == ["new"]
    card = await db.customer_card(99)
    assert card and card["discount_pct"] == 15 and card["recent"] == []


# ───────── посетители: человек в боте есть, заказа ещё нет (26.08) ─────────
async def test_visitor_without_orders_is_a_client(tmp_db):
    """Раньше открывший бота жил ТОЛЬКО в ленте событий: список клиентов строился из
    requests, и до первого заказа человека в CRM не существовало."""
    db = tmp_db
    now = int(time.time())
    await _mk(db, 1, "Аня", status="printed", price=10, created_at=now - 5 * 86400)
    await db.log_event(2, "открыл бота", user_name="Антон")
    await db.log_event(2, "рассчитал цену", "Ваза · PLA ×1")     # веб-путь имени не передаёт
    by_uid = {c["uid"]: c for c in await db.list_customers_crm()}
    assert 2 in by_uid
    v = by_uid[2]
    assert v["orders"] == 0 and v["spent"] == 0 and v["segments"] == ["visitor"]
    assert v["name"] == "Антон"                    # имя нашлось в его же событиях
    assert v["last_at"] == 0 and v["last_seen_at"] > 0   # заказа нет, визит есть
    assert [c["uid"] for c in await db.list_customers_crm(segment="visitor")] == [2]
    # «Свежие» — по последнему касанию: посетитель заходил только что, заказ Ани пятидневный
    assert [c["uid"] for c in await db.list_customers_crm()][0] == 2
    assert 2 not in {c["uid"] for c in await db.list_customers_crm(visitors=False)}
    card = await db.customer_card(2)
    assert card and card["segments"] == ["visitor"] and card["recent"] == []
    assert await db.customer_card(404) is None      # о ком не знаем ничего — по-прежнему None


async def test_visitor_not_in_broadcast_all(tmp_db):
    """Появление посетителей в CRM не должно молча расширить аудиторию рассылки:
    «Все» — прежние клиенты, посетителям пишем, только выбрав их сегмент."""
    db = tmp_db
    await _mk(db, 1, "Аня")
    await db.log_event(2, "открыл бота", user_name="Антон")
    assert [x["uid"] for x in (await db.crm_broadcast_targets())["eligible"]] == [1]
    assert [x["uid"] for x in (await db.crm_broadcast_targets("visitor"))["eligible"]] == [2]


async def test_staff_is_not_a_visitor(tmp_db):
    """Свои ходят в бота по работе — в клиентах им делать нечего."""
    db = tmp_db
    await db.set_staff(9, "operator", name="Оператор")
    await db.log_event(9, "открыл бота", user_name="Оператор")
    assert 9 not in {c["uid"] for c in await db.list_customers_crm()}


async def test_activity_name_kept_for_visitor(tmp_db):
    """Имя в ленте: у посетителя заказов нет, и раньше все его события после /start
    показывались «Гостем» — имя искали только в requests."""
    db = tmp_db
    await db.log_event(2, "открыл бота", user_name="Антон")
    await db.log_event(2, "рассчитал цену", "Ваза")
    assert [a["name"] for a in await db.list_activity()] == ["Антон", "Антон"]
    cur = await db._conn().execute("SELECT user_name FROM activity_log ORDER BY id DESC LIMIT 1")
    assert (await cur.fetchone())["user_name"] == "Антон"   # дозаполнено при записи


# ───────── «непросмотренные» заказы для LCD-дашборда (design_handoff_lcd) ─────────
async def test_unseen_requests_counter(tmp_db):
    db = tmp_db
    rid1 = await _mk(db, 1, "Аня")
    rid2 = await _mk(db, 2, "Боря")
    assert await db.count_unseen_requests() == 2
    await db.mark_request_seen(rid1)
    assert await db.count_unseen_requests() == 1
    await db.mark_request_seen(rid1)                       # повторно — без эффекта
    assert await db.count_unseen_requests() == 1
    await db.mark_request_seen(rid2)
    assert await db.count_unseen_requests() == 0

# Тест слияния счетов удалён вместе с merge_user_accounts (2026-07-16): семейный счёт
# теперь через family_accounts (tests/test_family.py), слияние уничтожало историю клиента.
