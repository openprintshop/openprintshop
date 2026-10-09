"""Цикл 3, волна 7а: разбор отчёта Bambu Lab — написан ДО приезда машины.

Почему заранее. У Elegoo первый час у принтера ушёл на отладку разбора вместо ЗАПИСИ обмена, и
активную ветку `enrich_status` там до сих пор нечем покрыть: все захваты в `docs/captures/` сняты
в простое. Здесь наоборот — разбор написан по опубликованному формату и покрыт рукописными
payload'ами, а у железа останется одно: снять записи и сверить поля.

Payload'ы ниже — РУКОПИСНЫЕ, не с живой машины. Это записано намеренно: тест доказывает, что наш
перевод внутренне согласован и что автоматика проекта увидит машину занятой, а не что прошивка
шлёт ровно эти ключи. Расхождения ловятся первой же записью с принтера.
"""
from app import bambu, printerdrv


class _Card:
    def __init__(self, **kw):
        self.id, self.name = 2, "Bambu"
        self.kind = "bambu"
        self.address = kw.get("address", "10.0.0.7")
        self.access_code = kw.get("access_code", "12345678")
        self.serial = kw.get("serial", "01P00A000000000")


_PRINTING = {
    "gcode_state": "RUNNING",
    "subtask_name": "plate_1.gcode",
    "mc_percent": 42,
    "mc_remaining_time": 73,            # МИНУТЫ, а не секунды
    "layer_num": 120,
    "total_layer_num": 300,
    "nozzle_temper": 220.5, "nozzle_target_temper": 220,
    "bed_temper": 59.8, "bed_target_temper": 60,
    "chamber_temper": 38,
    "cooling_fan_speed": "15",          # ступени 0..15, приходят строкой
    "stg_cur": 0,
}


def test_report_payload_takes_only_the_print_envelope():
    """Принтер шлёт в ту же тему `info`/`system`/`camera`. Разбери их как телеметрию — и
    состояние затрётся пустотой, а экран решит, что машина замолчала."""
    assert bambu.report_payload({"print": _PRINTING})["gcode_state"] == "RUNNING"
    assert bambu.report_payload({"info": {"command": "get_version"}}) == {}
    assert bambu.report_payload("не словарь") == {}
    assert bambu.report_payload({}) == {}


def test_printing_report_maps_into_the_project_shape():
    o = bambu.enrich_status(_PRINTING)
    assert o["print_stats"]["state"] == "printing"
    assert o["print_stats"]["filename"] == "plate_1.gcode"
    assert o["virtual_sdcard"]["progress"] == 0.42
    assert o["info"] == {"current_layer": 120, "total_layer": 300}
    assert o["extruder"] == {"temperature": 220.5, "target": 220.0}
    assert o["heater_bed"] == {"temperature": 59.8, "target": 60.0}
    assert o["ztemperature_sensor"]["temperature"] == 38.0
    assert o["fans"]["fan"]["speed"] == 1.0        # 15 ступеней = 100 %


def test_remaining_time_is_converted_from_minutes_to_seconds():
    """У Bambu остаток в МИНУТАХ, а весь проект считает его в СЕКУНДАХ.

    Перепутай — и клиент увидит срок в шестьдесят раз меньше: «осталось 73 секунды» вместо часа с
    четвертью. Поэтому пересчёт живёт в переводчике, а не у потребителя."""
    assert bambu.enrich_status(_PRINTING)["print_status"]["remaining_time_sec"] == 73 * 60


def test_busy_stages_count_as_busy_even_without_gcode_state():
    """Калибровка стола, прогрев и смена филамента — ЗАНЯТОСТЬ.

    Тот же принцип, что у Elegoo: гард «пока занята, по сети не трогаем» и детектор финиша
    спрашивают «машина занята заданием», а не «сопло движется прямо сейчас». Иначе печать,
    ушедшая на калибровку, выглядела бы законченной и заводила запись в журнал."""
    drv = printerdrv.for_printer(_Card())
    for stage in (1, 7, 4):
        js = drv.job_state({"stg_cur": stage})
        assert js["active"] is True, f"стадия {stage} прочиталась как простой"


def test_pause_is_busy_and_failure_is_not():
    """Пауза — занятость (иначе обед мастера заведёт запись в журнал на половину печати),
    а сбой — финиш: задание встало, и для журнала это конец."""
    drv = printerdrv.for_printer(_Card())
    assert drv.job_state({"gcode_state": "PAUSE"})["active"] is True
    assert drv.job_state({"gcode_state": "PREPARE"})["active"] is True
    assert drv.job_state({"gcode_state": "FAILED"})["active"] is False
    assert drv.job_state({"gcode_state": "FINISH"})["active"] is False
    assert drv.job_state({"gcode_state": "IDLE"})["active"] is False


def test_job_state_reads_the_same_answers_as_for_elegoo():
    """Сквозная проверка контракта: автоматика спрашивает драйвер одним вопросом и получает
    те же поля, что у Elegoo, — иначе журнал и наблюдатель завели бы третью ветку разбора."""
    js = printerdrv.for_printer(_Card()).job_state(_PRINTING)
    assert js["active"] is True
    assert js["progress"] == 0.42
    assert js["name"] == "plate_1.gcode"
    assert js["phase"] == "printing"


def test_numbers_arriving_as_strings_do_not_break_the_parse():
    """Прошивка отдаёт числа и строками, и числами. Строгий разбор дал бы «принтер молчит»."""
    o = bambu.enrich_status({"gcode_state": "RUNNING", "mc_percent": "7",
                             "nozzle_temper": "215", "mc_remaining_time": "10"})
    assert o["virtual_sdcard"]["progress"] == 0.07
    assert o["extruder"]["temperature"] == 215.0
    assert o["print_status"]["remaining_time_sec"] == 600


def test_partial_report_does_not_invent_fields():
    """Отчёты после первого — ЧАСТИЧНЫЕ. Переводчик обязан молчать о том, чего в кадре нет:
    выдуманный ноль лёг бы в историю телеметрии как настоящее показание."""
    o = bambu.enrich_status({"bed_temper": 61})
    assert o["heater_bed"] == {"temperature": 61.0, "target": None}
    assert "extruder" not in o and "virtual_sdcard" not in o and "info" not in o
    assert bambu.enrich_status({}) == {}


def test_summary_and_extras_match_the_telemetry_columns():
    """История телеметрии пишется одними и теми же полями для всех вендоров."""
    assert bambu.summarize(_PRINTING) == {"extruder_temp": 220.5, "extruder_target": 220.0,
                                          "bed_temp": 59.8, "bed_target": 60.0}
    ex = bambu.extras(_PRINTING)
    assert ex["chamber_temp"] == 38.0 and ex["fans"]["model"] == 100
    # Слоты AMS сознательно не разбираются: привязку цвета к лотку нельзя выдумывать — она
    # уезжает в заказ клиента. Это работа у железа (волна 7б).
    assert "slots" not in ex and "active_slot" not in ex


def test_topics_carry_the_serial():
    """Серийник у Bambu — в ТЕМЕ, а не в теле. Уедет адрес без серийника — команда уйдёт на
    правильный принтер в тему чужой машины и потеряется молча."""
    assert bambu.topic_report(" 01P00A ") == "device/01P00A/report"
    assert bambu.topic_request("01P00A") == "device/01P00A/request"


# ─── сетевая часть (волна 7б): написана до железа, проверяется на скриптованном сокете ────

class _Sock:
    """Сокет-скрипт: отдаёт заготовленные пакеты, запоминает отправленное."""

    def __init__(self, script):
        self.script, self.sent, self.closed = list(script), [], False

    def sendall(self, b):
        self.sent.append(b)

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


def _connack_ok() -> tuple:
    return (0x20, b"\x00\x00")


def _session(monkeypatch, script) -> tuple:
    """Сессия Bambu с подменённым транспортом: ни одного настоящего сокета."""
    from app import bambu as b

    sock = _Sock(script)
    monkeypatch.setattr(b.socket, "create_connection", lambda *a, **k: sock)

    class _Ctx:
        def wrap_socket(self, s, server_hostname=None):
            return sock
    monkeypatch.setattr(b, "_tls_context", lambda: _Ctx())
    it = iter(sock.script)

    def _recv(_s):
        try:
            return next(it)
        except StopIteration:
            raise TimeoutError from None
    monkeypatch.setattr(b.elegoo, "_recv_pkt", _recv)

    ses = b.BambuSession(lambda: b.Endpoint(ip="10.0.0.7", code="CODE", serial="01P00A"))
    return ses, sock


def test_handshake_asks_for_a_full_snapshot(monkeypatch):
    """На подключении обязателен `pushall`: без него в кэш попадут одни дельты, и экран покажет
    принтер полупустым — половина полей просто не придёт."""
    ses, sock = _session(monkeypatch, [_connack_ok()])
    sock2 = ses._connect(ses.endpoint())
    ses._pushall(sock2, "01P00A")
    joined = b"".join(sock.sent)
    assert b"device/01P00A/report" in joined      # подписались на отчёты
    assert b"pushall" in joined
    assert b"device/01P00A/request" in joined     # и попросили в правильной теме


def test_rejected_access_code_is_an_honest_error(monkeypatch):
    """CONNACK с ненулевым кодом — не «принтер молчит», а неверный код доступа."""
    import pytest as _p

    ses, _sock = _session(monkeypatch, [(0x20, b"\x00\x05")])
    with _p.raises(OSError, match="код доступа"):
        ses._connect(ses.endpoint())


def test_partial_reports_are_merged_not_replaced(monkeypatch):
    """Дельты обязаны СЛИВАТЬСЯ. Замена состояния целиком стирала бы половину показаний на
    каждом кадре: полный снимок приходит только на pushall."""
    from app import bambu as b

    ses = b.BambuSession(lambda: b.Endpoint(ip="10.0.0.7", code="C", serial="S"))
    with ses.live_lock:
        b.elegoo._deep_merge(ses.live["state"], dict(_PRINTING))
        b.elegoo._deep_merge(ses.live["state"], b.enrich_status(_PRINTING))
        ses.live["ts"] = __import__("time").time()
        ses.live["connected"] = True
    # прилетела дельта только про стол
    with ses.live_lock:
        delta = {"bed_temper": 61}
        b.elegoo._deep_merge(ses.live["state"], delta)
        b.elegoo._deep_merge(ses.live["state"], b.enrich_status(delta))
    cs = ses.cached_status(30.0)
    assert cs["state"]["heater_bed"]["temperature"] == 61        # новое приехало
    assert cs["state"]["extruder"]["temperature"] == 220.5       # старое не стёрлось
    assert cs["state"]["print_stats"]["state"] == "printing"


def test_command_without_connection_is_refused_not_faked(monkeypatch):
    """Без соединения команда обязана честно отказать. Тихое «ок» означало бы, что мастер видит
    «Стоп выполнен», а печать идёт дальше."""
    from app import bambu as b

    ses = b.BambuSession(lambda: b.Endpoint(ip="10.0.0.7", code="C", serial="S"))
    r = ses.send({"print": {"command": "stop"}})
    assert r["ok"] is False and "соединения" in r["error"]


def test_capabilities_appear_only_after_the_printer_answered(monkeypatch):
    """Способности — ПО ФАКТУ. Сетевая часть на живом железе не проверена ни байтом, поэтому
    до первого отчёта карточка не обещает ничего; ответила машина — набор появился сам."""
    from app import bambu as b

    card = _Card()
    drv = printerdrv.for_printer(card)
    assert drv.enabled() is True                  # карточка заполнена — идти есть куда
    assert drv.describe()["caps"] == printerdrv.Caps().as_dict()
    assert drv.describe()["note"]

    b.session_for(drv.endpoint()).seen_report = True
    try:
        assert drv.describe()["caps"] == printerdrv.BambuDriver.caps.as_dict()
        assert drv.describe()["caps"]["status"] and drv.describe()["caps"]["control"]
        assert drv.describe()["caps"]["camera"] is False   # у Bambu камера своим потоком
    finally:
        b.sessions_stop_all()


def test_card_without_serial_has_no_remote():
    """Серийник обязателен: у Bambu он в ТЕМЕ MQTT. Без него команда ушла бы в никуда, а
    подписчик не получил бы ни одного отчёта."""
    assert printerdrv.for_printer(_Card(serial="")).enabled() is False
    assert printerdrv.for_printer(_Card(address="")).enabled() is False
    assert printerdrv.for_printer(_Card(access_code="")).enabled() is False


# ─── файловая часть: FTPS ─────────────────────────────────────────────────────────────────
# Транспорт подменён: настоящий FTPS-сервер поднимать незачем, а рукопожатие implicit TLS всё
# равно проверяется только на живой машине. Здесь проверяется НАША логика: какие каталоги
# читаем, как отбираем печатаемое, куда кладём ролик и что делаем с оборванной закачкой.

class _FakeFTP:
    """Мини-FTP: отдаёт заготовленные каталоги, запоминает команды."""

    def __init__(self, tree=None, fail_delete=()):
        self.tree = tree or {}
        self.fail_delete = set(fail_delete)
        self.deleted, self.stored, self.retrieved = [], [], []
        self.quit_called = False

    def mlsd(self, path, facts=None):
        for name, (size, ts) in self.tree.get(path, {}).items():
            yield name, {"type": "file", "size": str(size), "modify": ts}

    def delete(self, path):
        import ftplib as _f
        if path in self.fail_delete:
            raise _f.error_perm("550 No such file")
        self.deleted.append(path)

    def storbinary(self, cmd, fh):
        self.stored.append((cmd, len(fh.read())))

    def retrbinary(self, cmd, cb):
        self.retrieved.append(cmd)
        cb(b"MP4DATA" * 100)

    def quit(self):
        self.quit_called = True


def _with_ftp(monkeypatch, ftp):
    monkeypatch.setattr(bambu, "_ftp_connect", lambda ep, timeout=20.0: ftp)
    return bambu.Endpoint(ip="10.0.0.7", code="C", serial="S")


def test_files_merge_cache_and_model_and_skip_junk(monkeypatch):
    """Печатаемое ищем в ДВУХ каталогах и берём только задания: в /cache и /model лежит и мусор
    (превью, логи), а показать его как файл для печати — обещать несуществующее."""
    ftp = _FakeFTP({
        "/cache": {"plate_1.gcode": (1000, "20260812101500"),
                   "thumb.png": (10, "20260812101500")},
        "/model": {"cat.3mf": (2000, "20260812090000"),
                   "plate_1.gcode": (900, "20260811090000")},
    })
    r = bambu.list_files(_with_ftp(monkeypatch, ftp))
    assert r["ok"]
    names = [f["name"] for f in r["files"]]
    assert names == ["cat.3mf", "plate_1.gcode"]     # png отсеян, дубль схлопнут
    plate = next(f for f in r["files"] if f["name"] == "plate_1.gcode")
    assert plate["size"] == 1000 and plate["dir"] == "/cache"   # свежая копия победила
    # граммов и времени FTP не знает — 0, а не выдуманное число
    assert plate["grams"] == 0 and plate["print_time"] == 0
    assert ftp.quit_called


def test_delete_tries_both_directories(monkeypatch):
    """Имя приходит из общего списка, а лежать файл может в любом из двух каталогов."""
    ftp = _FakeFTP(fail_delete={"/cache/cat.3mf"})
    r = bambu.delete_file(_with_ftp(monkeypatch, ftp), "cat.3mf")
    assert r["ok"] is True and ftp.deleted == ["/model/cat.3mf"]


def test_upload_goes_to_cache(monkeypatch, tmp_path):
    src = tmp_path / "job.gcode"
    src.write_bytes(b"G1 X0\n" * 10)
    ftp = _FakeFTP()
    r = bambu.upload(_with_ftp(monkeypatch, ftp), src)
    assert r["ok"] and r["name"] == "job.gcode"
    assert ftp.stored and ftp.stored[0][0] == "STOR /cache/job.gcode"


def test_timelapses_come_freshest_first(monkeypatch):
    """Свежие первыми: «последний ролик» без сортировки был бы случайным."""
    ftp = _FakeFTP({"/timelapse": {"old.mp4": (10, "20260810100000"),
                                   "new.mp4": (10, "20260812100000"),
                                   "notes.txt": (1, "20260812110000")}})
    r = bambu.timelapses(_with_ftp(monkeypatch, ftp))
    assert [f["name"] for f in r["files"]] == ["new.mp4", "old.mp4"]


def test_fetch_writes_through_a_temp_file(monkeypatch, tmp_path):
    """Пишем во временный файл и переименовываем.

    Оборванная закачка иначе оставит куцый .mp4, который и свип, и клиент примут за готовое
    видео: у Elegoo это уже ловилось — недокачанный файл «есть», и повторно его никто не берёт."""
    ftp = _FakeFTP()
    dest = tmp_path / "p2" / "cat.mp4"
    r = bambu.fetch_timelapse(_with_ftp(monkeypatch, ftp), "cat.mp4", dest)
    assert r["ok"] and dest.exists() and dest.stat().st_size > 0
    assert not list(tmp_path.rglob("*.part")), "временный файл не убран"
    assert ftp.retrieved == ["RETR /timelapse/cat.mp4"]


def test_fetch_leaves_nothing_behind_when_it_fails(monkeypatch, tmp_path):
    class _Boom(_FakeFTP):
        def retrbinary(self, cmd, cb):
            cb(b"partial")
            raise OSError("связь оборвалась")

    dest = tmp_path / "cat.mp4"
    r = bambu.fetch_timelapse(_with_ftp(monkeypatch, _Boom()), "cat.mp4", dest)
    assert r["ok"] is False and "оборв" in r["error"]
    assert not dest.exists() and not list(tmp_path.rglob("*.part"))


def test_driver_says_it_has_no_job_history():
    """У Bambu в локальном режиме истории заданий НЕТ — она в облаке. Собрать её из списка файлов
    нельзя: свип сверяет ИМЕНА заданий, и чужой источник привязал бы к печати чужое видео."""
    drv = printerdrv.for_printer(_Card())
    assert drv.caps.history is False
    assert drv.history()["ok"] is False
    # и сборку mp4 запрашивать нечего: Bambu пишет ролик сам по ходу печати
    assert drv.generate_timelapse("x")["ok"] is False


def test_driver_finds_the_timelapse_by_job_name(monkeypatch):
    """Ролик ищется по ИМЕНИ задания, а url остаётся пустым: файл лежит на FTPS, а не за
    http-ссылкой, и качается fetch_timelapse."""
    ftp = _FakeFTP({"/timelapse": {"cat.gcode.mp4": (10, "20260812100000"),
                                   "dog.gcode.mp4": (10, "20260812110000")}})
    monkeypatch.setattr(bambu, "_ftp_connect", lambda ep, timeout=20.0: ftp)
    drv = printerdrv.for_printer(_Card())

    r = drv.latest_timelapse_url("cat.gcode")
    assert r["ok"] and r["name"] == "cat.gcode.mp4" and r["url"] == ""
    assert drv.latest_timelapse_url()["name"] == "dog.gcode.mp4"      # без имени — самый свежий
    assert drv.latest_timelapse_url("нет-такого.gcode")["ok"] is False
