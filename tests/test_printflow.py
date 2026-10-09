"""Привязка печати к заказу, детект финиша, подтверждение и таймлапс (OpenTodo 70b5a590)."""
from conftest import stub_bot

from app import elegoo


def _only(state: dict) -> dict:
    """Единственная наблюдаемая печать. Ключ — id карточки принтера, и зашивать его в тест
    значило бы привязаться к сиду БД; тестов с двумя машинами здесь нет."""
    assert len(state) == 1, f"ожидалась одна наблюдаемая печать, а их {len(state)}"
    return next(iter(state.values()))


async def _mk_order(db):
    return await db.add_request(
        user_id=1, user_name="U", model_label="Дракон", printer="P", plastic_name="PLA",
        quantity=1, quality="0.2", strength="норм", grams_total=0, minutes_total=0, price=10,
        preview_path=None)


# ───────── привязка печать→заказ ─────────
async def test_print_binding_lifecycle(tmp_db):
    db = tmp_db
    assert await db.get_print_binding() is None
    rid = await _mk_order(db)
    await db.set_print_binding(rid, "dragon.gcode")
    b = await db.get_print_binding()
    assert b["request_id"] == rid and b["job_name"] == "dragon.gcode"
    assert b["was_active"] == 0 and b["finished"] == 0
    await db.update_print_binding(was_active=1, finished=1)
    b = await db.get_print_binding()
    assert b["was_active"] == 1 and b["finished"] == 1
    await db.set_print_binding(rid, "x")          # переустановка сбрасывает флаги
    assert (await db.get_print_binding())["finished"] == 0
    await db.clear_print_binding()
    assert await db.get_print_binding() is None


# ───────── разбор таймлапса (method 1036) ─────────
def test_find_timelapse_url_variants():
    assert elegoo._find_timelapse_url({"time_lapse_video_url": "http://x/a.mp4"}) == "http://x/a.mp4"
    assert elegoo._find_timelapse_url({"timeLapseVideoUrl": "http://x/b.mp4"}) == "http://x/b.mp4"
    assert elegoo._find_timelapse_url({"some_video_link": "http://x/c.mp4"}) == "http://x/c.mp4"
    assert elegoo._find_timelapse_url({"name": "нет ссылки"}) == ""


def test_job_history_parses(monkeypatch):
    monkeypatch.setattr(elegoo, "request", lambda *a, **k: {"ok": True, "result": {"history_list": [
        {"task_name": "A", "time_lapse_video_url": "http://p/a.mp4", "end_time": 100},
        {"task_name": "B", "end_time": 200},
    ]}})
    r = elegoo.job_history()
    assert r["ok"] and len(r["jobs"]) == 2
    assert r["jobs"][0]["time_lapse_video_url"] == "http://p/a.mp4"
    assert r["jobs"][1]["time_lapse_video_url"] == ""


def test_job_history_unknown_wrapper_key(monkeypatch):
    # имя ключа-обёртки не разведано — берём первый список из result
    monkeypatch.setattr(elegoo, "request", lambda *a, **k: {"ok": True, "result": {
        "weird": [{"name": "Z", "time_lapse_video_url": "http://p/z.mp4"}]}})
    r = elegoo.job_history()
    assert r["ok"] and r["jobs"][0]["time_lapse_video_url"] == "http://p/z.mp4"


def test_latest_timelapse_url(monkeypatch):
    # ep=None у заглушки — тот же необязательный «кому адресован вызов», что у настоящей
    # job_history: latest_timelapse_url пробрасывает его дальше (app/elegoo.py)
    monkeypatch.setattr(elegoo, "job_history", lambda timeout=8.0, ep=None: {"ok": True, "jobs": [
        {"name": "A", "time_lapse_video_url": "http://p/a.mp4", "end_time": 100},
        {"name": "B", "time_lapse_video_url": "http://p/b.mp4", "end_time": 300},
    ]})
    assert elegoo.latest_timelapse_url()["url"] == "http://p/b.mp4"        # самая свежая
    assert elegoo.latest_timelapse_url("A")["url"] == "http://p/a.mp4"     # по имени
    monkeypatch.setattr(elegoo, "job_history",
                        lambda timeout=8.0, ep=None: {"ok": True, "jobs": []})
    assert elegoo.latest_timelapse_url()["ok"] is False


# ───────── наблюдатель за финишем ─────────
async def test_printwatch_notifies_once_on_finish(tmp_db, monkeypatch):
    db = tmp_db
    from app import printwatch
    rid = await _mk_order(db)
    await db.set_print_binding(rid, "job")
    # state непустой: пустой {} теперь означает «подписчик подключился, но кадров ещё не было»,
    # и printwatch по нему сознательно НЕ судит о финише (защита от ложного завершения после
    # рестарта бота). Содержимое не важно — _active_and_progress ниже замокан.
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": {"print_status": {}}})
    sent = []

    async def fake_notify(bot, text, **kw):
        sent.append(text)
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)

    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.4))   # активна
    await printwatch._tick(bot=object())
    assert (await db.get_print_binding())["was_active"] == 1 and sent == []

    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))  # завершилась
    await printwatch._tick(bot=object())
    assert (await db.get_print_binding())["finished"] == 1
    # _tick больше НЕ шлёт print_confirm на финише — завершение + классификацию (заказ/для себя) +
    # оценку присылает _tick_journal единым опросом (мастер-план, Фаза B).
    assert sent == []
    await printwatch._tick(bot=object())                                            # без дублей
    assert sent == []


async def test_printwatch_no_telemetry_noop(tmp_db, monkeypatch):
    db = tmp_db
    from app import printwatch
    rid = await _mk_order(db)
    await db.set_print_binding(rid, "job")
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda max_age=180.0: None)
    await printwatch._tick(bot=object())
    assert (await db.get_print_binding())["was_active"] == 0    # нет данных — ничего не меняем


# ─── журнал: печать заносится СРАЗУ (graded=0), опрос по jid (OpenTodo c587c48b, 5ac0ae9f) ───
async def test_printwatch_journal_autoentry_once(tmp_db, monkeypatch):
    db = tmp_db
    from app import printwatch
    printwatch._cur.clear()
    sent = []

    async def fake_notify(bot, text, **kw):
        sent.append(text)
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    # Принтер молчит: без этой заглушки _tick_journal (Путь A, вес файла с принтера)
    # уходил РЕАЛЬНЫМ MQTT на адрес из .env машины, где идёт прогон, — поймано
    # запуском под сетевым сторожем. Тесты к железу не ходят.
    monkeypatch.setattr(printwatch.elegoo, "file_grams", lambda name, ep=None: None)
    st = {"print_status": {"filename": "dragon.gcode", "print_duration": 600}}   # 10 минут
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})

    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.2))
    await printwatch._tick_journal(bot=object())
    assert _only(printwatch._cur)["name"] == "dragon.gcode" and _only(printwatch._cur)["minutes"] == 10.0
    assert sent == [] and await db.list_print_journal() == []

    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())
    items = await db.list_print_journal()
    assert len(items) == 1 and items[0]["label"] == "dragon.gcode"
    assert items[0]["graded"] == 0 and items[0]["minutes"] == 10.0   # занесена, но не оценена
    assert len(sent) == 1 and "для себя" in sent[0].lower()   # опрос-классификация (мастер-план Фаза B)
    await printwatch._tick_journal(bot=object())                     # повторный тик — без дублей
    assert len(await db.list_print_journal()) == 1


async def test_printwatch_journal_short_print_ignored(tmp_db, monkeypatch):
    db = tmp_db
    from app import printwatch
    printwatch._cur.clear()
    sent = []

    async def fake_notify(bot, text, **kw):
        sent.append(text)
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    st = {"print_status": {"filename": "fail.gcode", "print_duration": 60}}      # 1 минута
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.1))
    await printwatch._tick_journal(bot=object())
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())
    assert await db.list_print_journal() == [] and sent == []


async def test_printwatch_journal_prefills_from_binding(tmp_db, monkeypatch):
    db = tmp_db
    from app import printwatch
    printwatch._cur.clear()
    rid = await db.add_request(
        user_id=1, user_name="U", model_label="Дракон", printer="P", plastic_name="PETG",
        quantity=1, quality="0.2", strength="норм", grams_total=123, minutes_total=60,
        price=10, preview_path=None)
    await db.set_print_binding(rid, "dragon.gcode")

    async def fake_notify(bot, text, **kw):
        pass
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    st = {"print_status": {"filename": "dragon.gcode", "print_duration": 1200}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.5))
    await printwatch._tick_journal(bot=object())
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())
    items = await db.list_print_journal()
    assert items and items[0]["request_id"] == rid and items[0]["grams"] == 123
    assert items[0]["plastic_name"] == "PETG"


async def test_printwatch_journal_multiplate_uses_file_grams(tmp_db, monkeypatch):
    """Многостольный заказ: запись журнала берёт вес ТЕКУЩЕГО файла с принтера (file_grams), а не
    643 г всего заказа (ed689ac0, #79 — пластина ~11 г, а не 643). Одностольный (соседний тест) —
    по-прежнему вес заказа."""
    db = tmp_db
    from app import printwatch
    printwatch._cur.clear()
    rid = await db.add_request(
        user_id=1, user_name="U", model_label="Осьминог", printer="P", plastic_name="PLA",
        quantity=1, quality="0.2", strength="норм", grams_total=643, minutes_total=1900,
        price=50, preview_path=None, plates=7)
    await db.set_print_binding(rid, "Montaje.gcode")
    monkeypatch.setattr(printwatch.config, "ELEGOO_PASSIVE_HARVEST", True, raising=False)
    # ep= появился, когда вес файла пошёл через драйвер: чужому принтеру нельзя отдавать
    # граммы одноимённого файла с первой машины — это враньё в стоимости заказа.
    monkeypatch.setattr(printwatch.elegoo, "file_grams",
                        lambda name, ep=None: 11.08 if name == "Montaje.gcode" else None)
    st = {"print_status": {"filename": "Montaje.gcode", "print_duration": 9720}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.5))
    await printwatch._tick_journal(bot=object())
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())
    items = await db.list_print_journal()
    assert items and items[0]["request_id"] == rid
    assert abs(float(items[0]["grams"]) - 11.08) < 0.01   # вес пластины, а не 643 г заказа


async def test_printwatch_bound_print_does_not_ask_which_order(tmp_db, monkeypatch):
    """Печать привязана к заказу: вопроса «заказ или для себя» нет, а чат-опрос качества
    ВЫПИЛЕН (OpenTodo b02bc4c6) — в Telegram не уходит ничего. Мастеру остаются Windows-тост
    (клик ведёт в «Журнал мастера») и карточка в «Уведомлениях» приложения."""
    db = tmp_db
    from app import deskevents, printwatch
    printwatch._cur.clear()
    deskevents.reset()
    rid = await db.add_request(
        user_id=1, user_name="U", model_label="Держатель", printer="P", plastic_name="PETG",
        quantity=1, quality="0.2", strength="норм", grams_total=33, minutes_total=98,
        price=10, preview_path=None)
    await db.set_print_binding(rid, "body1.gcode")
    sent = []

    async def fake_notify(bot, text, **kw):
        sent.append(text)
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    st = {"print_status": {"filename": "body1.gcode", "print_duration": 5880}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.5))
    await printwatch._tick_journal(bot=object())
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())

    assert sent == []                                  # Telegram молчит (b02bc4c6)
    evs = deskevents.since(0)
    assert evs and evs[-1]["go"] == "masterlog"        # тост ведёт в «Журнал мастера»
    assert f"#{rid}" in evs[-1]["text"]
    assert await db.get_print_binding() is None        # привязка закрыта — печать разобрана
    items = await db.list_print_journal()
    assert items and items[0]["request_id"] == rid
    notifs = [n for n in await db.list_admin_notifications() if n["kind"] == "print_journal"]
    assert notifs, "карточка «оцените результат» в приложении осталась"


async def test_printwatch_journal_binding_other_job_not_prefilled(tmp_db, monkeypatch):
    """Привязка от другого файла (мастер не подтвердил прошлую печать) — граммы не тянем."""
    db = tmp_db
    from app import printwatch
    printwatch._cur.clear()
    rid = await db.add_request(
        user_id=1, user_name="U", model_label="Дракон", printer="P", plastic_name="PETG",
        quantity=1, quality="0.2", strength="норм", grams_total=123, minutes_total=60,
        price=10, preview_path=None)
    await db.set_print_binding(rid, "old_job.gcode")

    async def fake_notify(bot, text, **kw):
        pass
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    # Принтер молчит: без этой заглушки _tick_journal (Путь A, вес файла с принтера)
    # уходил РЕАЛЬНЫМ MQTT на адрес из .env машины, где идёт прогон, — поймано
    # запуском под сетевым сторожем. Тесты к железу не ходят.
    monkeypatch.setattr(printwatch.elegoo, "file_grams", lambda name, ep=None: None)
    st = {"print_status": {"filename": "another.gcode", "print_duration": 1200}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.5))
    await printwatch._tick_journal(bot=object())
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())
    items = await db.list_print_journal()
    assert items and items[0]["request_id"] is None and items[0]["grams"] == 0


async def test_grade_print_journal(tmp_db):
    """Авто-занесённую печать можно оценить позже (5ac0ae9f)."""
    db = tmp_db
    jid = await db.add_print_journal(label="X", plastic_id=None, plastic_name="PLA",
                                     grams=20, minutes=30, graded=0)
    stats = await db.print_journal_stats()
    assert stats["ungraded"] == 1 and stats["fail_pct"] == 0   # неоценённая не в брак-стате
    assert await db.grade_print_journal(jid, 40, "отлип")
    e = await db.get_print_journal(jid)
    assert e["graded"] == 1 and e["fail_pct"] == 40 and e["result"] == "fail"
    assert (await db.print_journal_stats())["ungraded"] == 0


# ───────── фактический расход филамента с принтера (OpenTodo 921a08b8) ─────────
def test_job_grams_weight_key():
    from app import printwatch
    # прямой вес в граммах
    assert printwatch._job_grams({"print_status": {"filament_weight": 42.7}}) == 42.7
    assert printwatch._job_grams({"print_status": {"usedMaterialWeight": 15}}) == 15.0


def test_job_grams_length_key_converts_mm_to_grams():
    from app import printwatch
    # 1000 мм филамента 1.75 мм, плотность 1.24 → ~2.98 г (площадь сечения × длина × плотность)
    g = printwatch._job_grams({"print_status": {"filament_used": 1000}})
    assert g is not None and 2.9 < g < 3.1


def test_job_grams_absent():
    from app import printwatch
    assert printwatch._job_grams({"print_status": {"print_duration": 600}}) is None
    assert printwatch._job_grams({}) is None


async def test_printwatch_journal_records_printer_grams(tmp_db, monkeypatch):
    """Распознанный расход с принтера пишется в журнал (приоритетнее оценки заказа)."""
    db = tmp_db
    from app import printwatch
    printwatch._cur.clear()

    async def fake_notify(bot, text, **kw):
        pass
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    st = {"print_status": {"filename": "vase.gcode", "print_duration": 600, "filament_weight": 33.3}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.5))
    await printwatch._tick_journal(bot=object())
    assert _only(printwatch._cur)["grams"] == 33.3                  # накоплено во время печати
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())
    items = await db.list_print_journal()
    assert len(items) == 1 and items[0]["grams"] == 33.3     # факт с принтера в журнале


# ─── Windows-тост при старте печати: вопрос привязки к заказу (OpenTodo d2f67e51) ───
async def test_printwatch_print_start_pushes_desk_toast(tmp_db, monkeypatch):
    """Печать пошла без привязки → событие шины настольной админки с переходом на экран
    «Печать» (там карточка «Заказ печати»). Второй тик той же печати дубль не плодит."""
    from app import deskevents, printwatch, webapp
    printwatch._cur.clear()
    deskevents.reset()
    webapp._active_print.clear()   # чужой rid из прошлых тестов не мешает

    async def fake_notify(bot, text, **kw):
        pass
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    st = {"print_status": {"filename": "dragon.gcode", "print_duration": 60}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.1))
    await printwatch._tick_journal(bot=object())
    evs = deskevents.since(0)
    assert len(evs) == 1 and evs[0]["go"] == "print", evs
    assert "dragon.gcode" in evs[0]["text"] and "привязать" in evs[0]["text"].lower()
    await printwatch._tick_journal(bot=object())          # та же печать — тост один
    assert len(deskevents.since(0)) == 1


async def test_printwatch_print_start_bound_no_toast(tmp_db, monkeypatch):
    """Печать уже привязана к заказу — вопрос «привязать?» не задаём."""
    db = tmp_db
    from app import deskevents, printwatch
    printwatch._cur.clear()
    deskevents.reset()
    rid = await _mk_order(db)
    await db.set_print_binding(rid, "dragon.gcode")

    async def fake_notify(bot, text, **kw):
        pass
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    st = {"print_status": {"filename": "dragon.gcode", "print_duration": 60}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.1))
    await printwatch._tick_journal(bot=object())
    assert deskevents.since(0) == []


# ─── вес в карточке уведомления: «вес ?» вместо «0 г» (OpenTodo db0f0337) ───
async def test_printwatch_unbound_notification_shows_unknown_weight(tmp_db, monkeypatch):
    """Печать без заказа и без веса из телеметрии: карточка «Уведомлений» пишет «вес ?»,
    а не вводящие в заблуждение «0 г»."""
    db = tmp_db
    from app import printwatch, webapp
    printwatch._cur.clear()
    webapp._active_print.clear()

    async def fake_notify(bot, text, **kw):
        pass
    monkeypatch.setattr(printwatch.notify, "notify_admins", fake_notify)
    # Принтер молчит: без этой заглушки _tick_journal (Путь A, вес файла с принтера)
    # уходил РЕАЛЬНЫМ MQTT на адрес из .env машины, где идёт прогон, — поймано
    # запуском под сетевым сторожем. Тесты к железу не ходят.
    monkeypatch.setattr(printwatch.elegoo, "file_grams", lambda name, ep=None: None)
    st = {"print_status": {"filename": "vase.gcode", "print_duration": 600}}
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda max_age=180.0: {"ok": True, "state": st})
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (True, 0.5))
    await printwatch._tick_journal(bot=object())
    monkeypatch.setattr(printwatch, "_active_and_progress", lambda s, d=None: (False, 1.0))
    await printwatch._tick_journal(bot=object())
    notifs = [n for n in await db.list_admin_notifications() if n["kind"] == "print_classify"]
    assert notifs and notifs[0]["body"].startswith("вес ?"), notifs
    assert "0 г" not in notifs[0]["body"]


# ───────── подтверждение мастером ─────────
async def test_confirm_print_marks_printed_and_clears(tmp_db, monkeypatch):
    db = tmp_db
    from app import printerdrv, webapp
    # «Нет принтера → без таймлапса». Раньше здесь подменялась elegoo.enabled, но ворота
    # доставки переехали на drv.enabled(), а он спрашивает поля КАРТОЧКИ, не модульную
    # функцию — подмена стала мёртвой, и тест уходил по-настоящему в MQTT боевого принтера.
    # Поймано сетевым сторожем в приёмке; гасим через слой драйверов, autouse-фикстура снимет.
    printerdrv.use_driver(printerdrv.NoneDriver())
    stub_bot(monkeypatch, None)
    rid = await _mk_order(db)
    await db.set_print_binding(rid, "job")
    res = await webapp.confirm_print(rid, True)
    assert res["ok"] and res["confirmed"] is True
    assert (await db.get_request(rid)).status == "printed"
    assert await db.get_print_binding() is None


async def test_confirm_print_not_ok_keeps_status(tmp_db, monkeypatch):
    db = tmp_db
    from app import webapp
    stub_bot(monkeypatch, None)
    rid = await _mk_order(db)
    await db.set_print_binding(rid, "job")
    res = await webapp.confirm_print(rid, False)
    assert res["confirmed"] is False
    assert (await db.get_request(rid)).status != "printed"
    assert await db.get_print_binding() is None


# ─── «богатая шапка»: граммы = вес текущего печатающегося ФАЙЛА, а не заказа ───
def _raise_list_files(*a, **k):
    raise AssertionError("активный list_files не должен вызываться, когда есть пассивный сбор")


async def test_active_print_header_grams_from_printer_file(tmp_db, monkeypatch):
    """Live-граммы в плитке «Филамент на модель» берутся из данных ПРИНТЕРА по имени печатающегося
    файла из телеметрии (OpenTodo ed689ac0) — как «Filament Consumption» в Web UI. Это чинит и
    многостольные заказы (флекси-осьминог на 7 столов, 643 г — больше не «11 из 643»), и печать
    без заказа. Фолбэк на граммы заказа — только для одностольного; нет ничего → грамм не отдаём."""
    from app import elegoo, webapp
    webapp._active_print.clear()
    monkeypatch.setattr(elegoo, "cached_status", lambda *a, **k: {
        "ok": True, "state": {"print_status": {"filename": "plate1.gcode"}}})
    multi = await tmp_db.add_request(
        user_id=1, user_name="U", model_label="Флекси-осьминог", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=643.1, minutes_total=1951,
        price=50, preview_path=None, plates=7)
    webapp._set_active_print(multi)

    def _reset_cache():
        monkeypatch.setattr(elegoo.session(), "file_grams_snap", {})
        monkeypatch.setattr(elegoo.session(), "file_grams_probe_ts", 0.0)

    # (а) главный источник — пассивный сбор по имени файла; активный 1044 при этом НЕ дёргаем
    _reset_cache()
    monkeypatch.setattr(elegoo.session(), "harvested_info",
                        lambda name: {"grams": 11.08} if name == "plate1.gcode" else None)
    monkeypatch.setattr(elegoo, "list_files", _raise_list_files)
    job = await webapp._active_print_header()
    assert job and job["grams"] == 11.1                # «Filament Consumption» текущей пластины

    # (а') нет пассивного сбора — берём тот же грамм активным list_files (под троттлингом)
    _reset_cache()
    monkeypatch.setattr(elegoo.session(), "harvested_info", lambda name: None)
    monkeypatch.setattr(elegoo, "list_files",
                        lambda timeout=8.0, ep=None: {"ok": True,
                                      "files": [{"name": "plate1.gcode", "grams": 11.08}]})
    job = await webapp._active_print_header()
    assert job and job["grams"] == 11.1

    # (г) по файлу ничего нет, заказ многостольный → грамм НЕ отдаём (плитка скрыта, а не врёт)
    _reset_cache()
    monkeypatch.setattr(elegoo, "list_files",
                        lambda timeout=8.0, ep=None: {"ok": True,
                                      "files": [{"name": "other.gcode", "grams": 5}]})
    job = await webapp._active_print_header()
    assert job and job["grams"] is None                # НЕ 643.1 — граммы заказа за печать не выдаём

    # (в) одностольный заказ, по файлу пусто → приблизительный фолбэк на граммы заказа
    _reset_cache()
    single = await tmp_db.add_request(
        user_id=1, user_name="U", model_label="Ваза", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=22, minutes_total=60,
        price=5, preview_path=None, plates=1)
    webapp._set_active_print(single)
    monkeypatch.setattr(elegoo, "list_files", lambda timeout=8.0, ep=None: {"ok": False, "error": "нет привязки"})
    job = await webapp._active_print_header()
    assert job and job["grams"] == 22                  # для одной пластины граммы заказа = граммы печати


def test_file_grams_throttle(monkeypatch):
    """Граммы файла неизменны и кэшируются: дорогой сетевой list_files (1044) не дёргается на
    каждый ~3-секундный поллинг статуса. Первый вызов ходит в принтер, следующие — из кэша; файла
    нет в свежем снимке → принтер повторно НЕ опрашиваем (защита от шторма 1044)."""
    from app import elegoo
    monkeypatch.setattr(elegoo.session(), "file_grams_snap", {})
    monkeypatch.setattr(elegoo.session(), "file_grams_probe_ts", 0.0)
    monkeypatch.setattr(elegoo.session(), "harvested_info", lambda name: None)   # без пассивного сбора
    calls = {"n": 0}

    def _lf(timeout=8.0, ep=None):
        calls["n"] += 1
        return {"ok": True, "files": [{"name": "koala.gcode", "grams": 42.0}]}

    monkeypatch.setattr(elegoo, "list_files", _lf)
    assert elegoo.file_grams("koala.gcode") == 42.0
    assert elegoo.file_grams("koala.gcode") == 42.0
    assert elegoo.file_grams("koala.gcode") == 42.0
    assert calls["n"] == 1, "list_files должен вызваться ровно один раз (кэш граммов файла)"
    assert elegoo.file_grams("ghost.gcode") is None    # нет в снимке
    assert calls["n"] == 1, "в пределах TTL повторно 1044 не зовём даже для незнакомого файла"


# ─── журнал: таймлапс подхватывается из авто-кэша (OpenTodo df00e8ff) ───
async def test_journal_backfills_timelapse_from_auto_cache(tmp_db, monkeypatch, tmp_path):
    """Запись журнала без timelapse, а видео в авто-кэше ЕСТЬ (фоновая привязка ждёт ~10 минут
    и гибнет при рестарте бота): журнал находит файл по имени задания и чинит запись сам."""
    import json

    from app import printmedia, webapp
    db = tmp_db
    jid = await db.add_print_journal(label="ECC2_koala.gcode", plastic_id=None,
                                     plastic_name="PLA", grams=10, minutes=60, graded=0)
    auto = tmp_path / "auto"
    auto.mkdir()
    # авто-кэш назван КАНОНИЧНЫМ именем задания (elegoo.normalize_job_name: без .gcode, lower)
    (auto / "ecc2_koala.mp4").write_bytes(b"fakemp4")
    # авто-кэш живёт в app/printmedia.py (волна 3); webapp зовёт cached_timelapse_path через
    # фасад, а та читает каталог у СЕБЯ — подмена в webapp ничего бы не изменила
    monkeypatch.setattr(printmedia, "_TIMELAPSE_AUTO_DIR", auto)
    monkeypatch.setattr(webapp, "_verify_admin", lambda s: 999)

    class _Q:
        query = {"initData": "x"}
    resp = await webapp._api_admin_journal(_Q())
    j = json.loads(resp.body.decode())
    it = next(x for x in j["items"] if x["id"] == jid)
    assert it["timelapse_url"], "таймлапс из авто-кэша должен подхватиться"
    e = await db.get_print_journal(jid)
    assert e["timelapse"] and e["timelapse"].endswith(".mp4")   # запись починена насовсем
