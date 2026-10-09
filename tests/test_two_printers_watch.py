"""Цикл 3, волна 6: наблюдатель и привязка печати работают ПО МАШИНАМ.

Печать в мастерской одновременная, и до этой волны почти всё состояние автоматики было одиночкой
на процесс: одна «текущая печать», один флаг «видели печать», один гейт свипа, одна строка
привязки печать→заказ. С двумя машинами каждая из этих одиночек ломается не аварией, а тихой
подменой: печать одной попадает в журнал другой, тревога второй машины проглатывается, а её
привязка к заказу вытесняется первой.

Здесь проверяется именно разделение — на минимальных фейках драйверов, без сети.
"""
import pytest

from app import db, printerdrv, printwatch


class _Card:
    """Карточка принтера ровно в том виде, в каком её видит драйвер и наблюдатель."""

    def __init__(self, pid: int, name: str):
        self.id = pid
        self.name = name
        self.kind = "fake"


class _Drv(printerdrv.PrinterDriver):
    """Драйвер с прибитым состоянием: своя карточка, свой ответ status()."""

    kind = "fake"

    def __init__(self, card, state=None, online: bool = True):
        self.printer = card
        self._state = state or {}
        self._online = online

    def enabled(self) -> bool:
        return True

    def status(self, max_age: float = 6.0):
        if not self._online:
            return None
        return {"ok": True, "state": self._state}

    def job_state(self, state: dict) -> dict:
        ps = (state or {}).get("print_status") or {}
        return {"active": bool(ps.get("filename")), "progress": ps.get("progress")}


def _printing(name: str, minutes: float = 10.0) -> dict:
    return {"print_status": {"filename": name, "print_duration": minutes * 60}}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(printwatch, "_notify_print_started", _noop)
    monkeypatch.setattr(printwatch, "_capture_bed_shot", _noop)
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)
    printwatch._cur.clear()
    printwatch._was_printing.clear()
    printwatch._tl_sweep_at.clear()
    printwatch._fallback_warned.clear()


class _Bot:
    async def send_message(self, chat_id, text, **kw):
        return None


# ─── привязка печать→заказ ───────────────────────────────────────────────────────────────

async def test_bindings_of_two_machines_coexist(tmp_db):
    """Две привязки живут одновременно. Пока строка была одна (`CHECK (id = 1)`), вторая
    вытеснила бы первую: заказ остался бы без своей печати, а её финиш закрыл бы чужой."""
    r1 = await tmp_db.add_request(
        user_id=1, user_name="U", model_label="Дракон", printer="P", plastic_name="PLA",
        quantity=1, quality="0.2", strength="норм", grams_total=0, minutes_total=0, price=10,
        preview_path=None)
    r2 = await tmp_db.add_request(
        user_id=2, user_name="V", model_label="Кот", printer="P", plastic_name="PLA",
        quantity=1, quality="0.2", strength="норм", grams_total=0, minutes_total=0, price=10,
        preview_path=None)

    await tmp_db.set_print_binding(r1, "dragon.gcode", 1)
    await tmp_db.set_print_binding(r2, "cat.gcode", 2)

    assert (await tmp_db.get_print_binding(1))["request_id"] == r1
    assert (await tmp_db.get_print_binding(2))["request_id"] == r2
    assert len(await tmp_db.list_print_bindings()) == 2

    await tmp_db.update_print_binding(2, was_active=1)
    assert (await tmp_db.get_print_binding(1))["was_active"] == 0   # соседа не задели

    await tmp_db.clear_print_binding(1)
    assert await tmp_db.get_print_binding(1) is None
    assert (await tmp_db.get_print_binding(2))["request_id"] == r2


async def test_binding_cleared_by_request_finds_the_right_machine(tmp_db):
    """Заказ отменили/подтвердили — привязку снимаем по ЗАКАЗУ, на какой бы машине он ни печатался.
    Вызывающий машину не знает, а оставленная привязка закрыла бы финишем несуществующий заказ."""
    rid = await tmp_db.add_request(
        user_id=1, user_name="U", model_label="Дракон", printer="P", plastic_name="PLA",
        quantity=1, quality="0.2", strength="норм", grams_total=0, minutes_total=0, price=10,
        preview_path=None)
    await tmp_db.set_print_binding(rid, "dragon.gcode", 7)
    assert await tmp_db.clear_print_binding_for_request(rid) == 1
    assert await tmp_db.get_print_binding(7) is None


async def test_legacy_binding_row_survives_the_migration(tmp_path):
    """Пересоздание таблицы не теряет текущую привязку и закрепляет её за основной машиной."""
    import aiosqlite

    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("CREATE TABLE printers(id INTEGER PRIMARY KEY, is_active INTEGER, "
                           "is_default INTEGER)")
        await conn.execute("INSERT INTO printers(id, is_active, is_default) VALUES(5, 1, 1)")
        await conn.execute("""CREATE TABLE print_binding(
              id INTEGER PRIMARY KEY CHECK (id = 1),
              request_id INTEGER, job_name TEXT, started_at INTEGER,
              was_active INTEGER NOT NULL DEFAULT 0,
              finished INTEGER NOT NULL DEFAULT 0,
              confirmed INTEGER NOT NULL DEFAULT 0)""")
        await conn.execute("INSERT INTO print_binding(id, request_id, job_name, started_at, "
                           "was_active) VALUES(1, 42, 'dragon.gcode', 100, 1)")
        await conn.commit()

        await db._split_print_binding_per_printer(conn)

        rows = [dict(r) for r in await (await conn.execute("SELECT * FROM print_binding")).fetchall()]
        assert len(rows) == 1
        assert rows[0]["printer_id"] == 5           # закреплена за основной машиной
        assert rows[0]["request_id"] == 42 and rows[0]["job_name"] == "dragon.gcode"
        assert rows[0]["was_active"] == 1           # флаги перенесены, а не сброшены

        await db._split_print_binding_per_printer(conn)   # повторный запуск ничего не портит
        assert len(await (await conn.execute("SELECT * FROM print_binding")).fetchall()) == 1


# ─── наблюдатель ─────────────────────────────────────────────────────────────────────────

async def test_two_machines_are_watched_independently(tmp_db):
    """У каждой машины своя наблюдаемая печать. Общая запись означала бы, что вторая машина,
    начав печатать, стирает накопленное по первой — и печать первой в журнал не попадёт."""
    a = _Drv(_Card(1, "Первый"), _printing("dragon.gcode", 20.0))
    b = _Drv(_Card(2, "Второй"), _printing("cat.gcode", 30.0))

    await printwatch._tick_journal(_Bot(), a)
    await printwatch._tick_journal(_Bot(), b)
    assert printwatch._cur[1]["name"] == "dragon.gcode"
    assert printwatch._cur[2]["name"] == "cat.gcode"

    # печать закончилась: телеметрия ЕСТЬ, но задания в ней нет (пустой снимок значил
    # бы «кадров ещё не было», и тик просто не стал бы трогать состояние)
    a._state = {"print_status": {}}
    await printwatch._tick_journal(_Bot(), a)
    rows = await tmp_db.list_print_journal(limit=10)
    assert [r["label"] for r in rows] == ["dragon.gcode"]
    assert rows[0]["printer_id"] == 1               # записана СВОЕЙ машине
    assert 1 not in printwatch._cur and printwatch._cur[2]["name"] == "cat.gcode"

    b._state = {"print_status": {}}
    await printwatch._tick_journal(_Bot(), b)
    rows = await tmp_db.list_print_journal(limit=10)
    assert {r["label"]: r["printer_id"] for r in rows} == {"dragon.gcode": 1, "cat.gcode": 2}


async def test_offline_alert_of_the_second_machine_is_not_swallowed(tmp_db, monkeypatch):
    """Флаг «видели печать» — по машинам. Общий: печать первой держала бы его взведённым, обрыв
    второй читался бы как обрыв печатавшей, а после одного алерта тревога второй проглатывалась."""
    sent: list = []

    async def _alert(text, key="", audience=None):
        sent.append(key)

    monkeypatch.setattr(printwatch.notify, "alert", _alert)
    a = _Drv(_Card(1, "Первый"), _printing("dragon.gcode"))
    b = _Drv(_Card(2, "Второй"), _printing("cat.gcode"))

    await printwatch._tick_offline(_Bot(), a)
    await printwatch._tick_offline(_Bot(), b)
    assert sent == [] and printwatch._was_printing == {1: True, 2: True}

    a._online = False                               # первая пропала со связи
    await printwatch._tick_offline(_Bot(), a)
    assert len(sent) == 1 and "Первый" in sent[0]
    assert printwatch._was_printing[2] is True      # вторая по-прежнему «печатала»

    b._online = False                               # теперь пропала и вторая
    await printwatch._tick_offline(_Bot(), b)
    assert len(sent) == 2 and "Второй" in sent[1]


async def test_second_card_does_not_borrow_the_subscriber_cache(tmp_db, monkeypatch):
    """Фолбэк на кэш подписчика — только для ОСНОВНОЙ карточки.

    `elegoo.cached_status()` без эндпоинта — снимок принтера по умолчанию. Отдать его второй
    машине значило бы записать печать первой в журнал под номером второй: ровно та ошибка, ради
    которой в журнале и появился printer_id."""
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": _printing("dragon.gcode")})
    monkeypatch.setattr(printwatch, "_default_pid", 1)

    dead_second = _Drv(_Card(2, "Второй"), online=False)
    cs, _drv = await printwatch._status(dead_second)
    assert cs is None, "второй карточке достался снимок чужого принтера"

    dead_main = _Drv(_Card(1, "Первый"), online=False)
    cs, _drv = await printwatch._status(dead_main)
    assert cs and cs["ok"], "основная карточка обязана переживать отказ драйвера по кэшу"


async def test_sweep_looks_only_at_prints_of_its_own_machine(tmp_db, monkeypatch):
    """Свип матчит записи журнала по ИМЕНИ задания с историей одного принтера. Без фильтра по
    машине «plate_1.gcode» второй машины подобрал бы видео первой."""
    mine = await tmp_db.add_print_journal(label="plate_1.gcode", plastic_id=None,
                                          plastic_name="PLA", grams=10, minutes=30, graded=0,
                                          printer_id=1)
    theirs = await tmp_db.add_print_journal(label="plate_1.gcode", plastic_id=None,
                                            plastic_name="PLA", grams=10, minutes=30, graded=0,
                                            printer_id=2)
    orphan = await tmp_db.add_print_journal(label="old.gcode", plastic_id=None,
                                            plastic_name="PLA", grams=10, minutes=30, graded=0)

    ids = {e["id"] for e in await tmp_db.list_print_journal(60, printer_id=2)}
    assert ids == {theirs}                                   # чужого и «ничьего» не видно

    # основная машина забирает и записи БЕЗ хозяина: иначе печати, сделанные до появления
    # printer_id, навсегда остались бы без таймлапсов
    ids = {e["id"] for e in await tmp_db.list_print_journal(60, printer_id=1, with_unowned=True)}
    assert ids == {mine, orphan}


# ─── экран и «богатая шапка» ─────────────────────────────────────────────────────────────

async def test_pointer_and_binding_share_the_key(tmp_db):
    """Указатель «богатой шапки» и привязка печати обязаны ложиться под ОДИН ключ.

    Разъехавшись, они уже ломали дело: привязка ложилась под id карточки, а указатель под 0 —
    и шапка сетевой печати не зажигалась вовсе, хотя код, который её «чинил», был на месте."""
    from app import webapp

    assert await webapp._active_print_key(None) == await tmp_db.binding_key(None)
    assert await webapp._active_print_key(None) != 0, "карточка есть — ключ обязан быть её id"
    assert await webapp._active_print_key(7) == 7


def test_rich_header_pointer_is_per_machine():
    """Указатель «что печатает бот» — на машину. Один на процесс означал бы, что вторая машина,
    начав печатать заказ, забирает шапку у первой: у той она погасла бы, а у второй показала бы
    чужое превью, чужой пластик и чужой вес филамента."""
    from app import webapp

    webapp._active_print.clear()
    try:
        webapp._set_active_print(11, 1)
        webapp._set_active_print(22, 2)
        assert webapp._active_print == {1: 11, 2: 22}
    finally:
        webapp._active_print.clear()


def test_bed_tracker_is_per_machine():
    """«Модель на столе» — тоже на машину: разбор печати на одной не должен гасить экран второй."""
    from app import webapp

    webapp._lcd_prn.clear()
    try:
        webapp._lcd_prn_for(1).update(was_printing=False, done=True, file="a.gcode")
        webapp._lcd_prn_for(2).update(was_printing=False, done=True, file="b.gcode")

        webapp.lcd_print_handled(1)
        assert webapp._lcd_prn_for(1)["done"] is False
        assert webapp._lcd_prn_for(2)["done"] is True      # соседа не погасили

        webapp.lcd_print_handled()                          # машину не назвали — гасим у всех
        assert webapp._lcd_prn_for(2)["done"] is False
    finally:
        webapp._lcd_prn.clear()


async def test_two_cards_on_one_machine_are_watched_once(tmp_db, monkeypatch):
    """Две карточки, ведущие на ОДИН принтер, наблюдаются один раз.

    Карточку заводят под второе сопло, а адрес у неё пустой — поля падают на .env, и физически
    это та же машина. Считать её второй значит задвоить журнал, скрины стола и уведомления об
    одной печати."""
    from app import config, printerdrv

    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")

    class _P:
        def __init__(self, pid, address=""):
            self.id, self.name, self.kind = pid, f"карточка {pid}", "elegoo"
            self.address, self.access_code, self.serial = address, "", ""

    async def _two(active_only=False):
        return [_P(1), _P(2)]                      # обе без адреса → обе на .env-машину

    monkeypatch.setattr(printwatch.db, "list_printers", _two)
    assert len(await printwatch._drivers()) == 1

    async def _two_real(active_only=False):
        return [_P(1), _P(2, "10.77.0.2")]         # вторая — своя машина
    monkeypatch.setattr(printwatch.db, "list_printers", _two_real)
    assert len(await printwatch._drivers()) == 2
    assert printerdrv is not None


async def test_unreadable_printer_list_skips_the_tick(tmp_db, monkeypatch):
    """Ошибка чтения списка карточек — НЕ «карточек нет».

    Вернув драйвер без карточки, наблюдатель сменил бы ключ состояния с id карточки на 0:
    накопленная печать осиротела бы, тост «печать началась» ушёл бы повторно, а запись легла бы
    в журнал без машины."""
    async def _boom(active_only=False):
        raise RuntimeError("БД занята автобэкапом")

    monkeypatch.setattr(printwatch.db, "list_printers", _boom)
    assert await printwatch._drivers() == []
