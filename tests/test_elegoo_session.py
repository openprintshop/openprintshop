"""Волна 6, шаг 1: состояние принтера Elegoo — в объекте-сессии, а не в модуле.

Раньше живой снимок телеметрии, сокет подписчика, привязка, пассивный сбор и снимок граммов были
модульными глобалями. «Подписчик в процессе один» было не решением, а следствием: второй карточке
принтера просто некуда положить своё состояние — она получала показания первого. Здесь проверяется
то единственное, ради чего состояние переехало в объект: две сессии НИЧЕГО не делят, а модульные
функции остались фасадом над сессией принтера по умолчанию (иначе тринадцать вызовов из других
модулей поехали бы одним коммитом).
"""
import queue

import pytest

from app import config, elegoo

_EP2 = elegoo.Endpoint(ip="10.77.0.2", code="CODE2", serial="SN2")


def _session2() -> elegoo.ElegooSession:
    """Сессия ВТОРОГО принтера — со своим эндпоинтом, не из .env."""
    return elegoo.ElegooSession(lambda: _EP2)


def test_module_functions_are_a_facade_over_the_default_session():
    """Модульные `cached_status`/`is_bound` обязаны смотреть в сессию по умолчанию.

    Это контракт для остальных модулей: они зовут `elegoo.cached_status()`, и подмена реализации
    на сессии не должна требовать их правки."""
    import time as _t
    ses = elegoo.session()
    with ses.live_lock:
        ses.live["state"] = {"extruder": {"temperature": 207}}
        ses.live["ts"] = _t.time()
        ses.live["serial"] = "SN-DEFAULT"
        ses.live["connected"] = True
    cs = elegoo.cached_status(6.0)
    assert cs and cs["serial"] == "SN-DEFAULT"
    assert cs["state"]["extruder"]["temperature"] == 207


def test_two_sessions_do_not_share_the_live_snapshot():
    """Снимок телеметрии — у каждой сессии свой. Общий снимок и был причиной, по которой вторая
    карточка показывала температуры первого принтера."""
    import time as _t
    ses2 = _session2()
    with ses2.live_lock:
        ses2.live["state"] = {"extruder": {"temperature": 42}}
        ses2.live["ts"] = _t.time()
        ses2.live["serial"] = "SN2"
        ses2.live["connected"] = True

    own = ses2.cached_status(6.0)
    assert own and own["serial"] == "SN2" and own["state"]["extruder"]["temperature"] == 42

    default_cs = elegoo.session().cached_status(6.0)
    assert default_cs is None or default_cs.get("serial") != "SN2"


def test_two_sessions_do_not_share_passive_harvest():
    """Пассивный сбор (граммы/история по имени gcode-файла) — тоже на сессию.

    Имена файлов на двух машинах совпадают сплошь и рядом («plate1.gcode»), так что общий кэш
    отдал бы граммы чужой печати в расчёт стоимости."""
    ses2 = _session2()
    ses2.harvest_api_response("elegoo/SN2/0cli/api_response", {
        "method": elegoo.METHOD_LIST_FILES, "result": {"file_list": [
            {"type": "file", "filename": "plate1.gcode", "total_filament_used": 77.7,
             "print_time": 300, "color_map": []}]}})

    assert (ses2.harvested_info("plate1.gcode") or {}).get("grams") == 77.7
    assert elegoo.harvested_info("plate1.gcode") is None      # фасад смотрит в СВОЮ сессию


def test_two_sessions_throttle_file_grams_independently(monkeypatch):
    """Троттлинг дорогого 1044 — на сессию. Общий счётчик означал бы, что запрос одной машины
    затыкает вторую на весь TTL, и её граммы молча не собираются."""
    ses2 = _session2()
    default = elegoo.session()
    monkeypatch.setattr(default, "harvested_info", lambda name: None)
    monkeypatch.setattr(default, "file_grams_snap", {})
    monkeypatch.setattr(default, "file_grams_probe_ts", 0.0)

    seen: list = []

    def _lf(timeout=8.0, ep=None):
        seen.append(ep)
        return {"ok": True, "files": [{"name": "plate1.gcode", "grams": 5.0}]}

    monkeypatch.setattr(elegoo, "list_files", _lf)

    assert elegoo.file_grams("plate1.gcode") == 5.0       # сессия по умолчанию сходила в принтер
    assert ses2.file_grams("plate1.gcode") == 5.0         # вторая НЕ заткнута чужим троттлингом
    assert len(seen) == 2
    assert seen[1] is not None and seen[1].ip == _EP2.ip  # …и спросила СВОЙ принтер


def test_bound_request_rides_the_socket_of_its_own_session(monkeypatch):
    """Главное, ради чего всё затевалось: быстрый путь публикует по сокету СВОЕЙ сессии.

    Пока сокет был модульным, запрос второго принтера уходил бы по соединению первого — в тему
    `elegoo/<серийник второго>/…`: команда не выполнилась бы нигде, а вызов с command=True
    отчитался бы «ок»."""
    class _Sock:
        def __init__(self):
            self.sent: list[bytes] = []

        def sendall(self, b):
            self.sent.append(b)

    default_sock, own_sock = _Sock(), _Sock()
    default = elegoo.session()
    monkeypatch.setattr(default, "sock", default_sock)
    monkeypatch.setattr(default, "bound", True)
    monkeypatch.setitem(default.live, "connected", True)

    ses2 = _session2()
    ses2.sock, ses2.bound = own_sock, True
    ses2.live["connected"] = True

    r = ses2.request(1029, {"power": 1}, timeout=0.05, command=True, ep=_EP2)

    assert r["ok"] and r.get("unconfirmed")            # ответа не ждали — сокет ненастоящий
    assert default_sock.sent == []                     # по чужому соединению не ушло ни байта
    assert own_sock.sent and b"elegoo/SN2/" in own_sock.sent[0]
    assert not ses2.pending                            # ожидание снято в finally


def test_session_endpoint_is_reread_not_snapshotted():
    """Эндпоинт хранится провайдером: адрес принтера правят в настройках на ходу, и подписчик
    обязан подхватить новый при следующем заходе цикла, а не держать снимок с момента старта."""
    box = {"ip": "10.0.0.1"}
    ses = elegoo.ElegooSession(lambda: elegoo.Endpoint(ip=box["ip"], code="C"))
    assert ses.endpoint().ip == "10.0.0.1"
    box["ip"] = "10.0.0.9"
    assert ses.endpoint().ip == "10.0.0.9"
    assert ses.is_subscriber_endpoint(elegoo.Endpoint(ip="10.0.0.9")) is True
    assert ses.is_subscriber_endpoint(elegoo.Endpoint(ip="10.0.0.1")) is False


def test_start_is_idempotent(monkeypatch):
    """Второй `start()` не поднимает второго подписчика к тому же принтеру: два соединения с
    одинаковым client_id брокер обязан рвать по очереди — это война реконнектов, снаружи видная
    как «принтер мигает онлайн/оффлайн»."""
    ses = _session2()
    monkeypatch.setattr(elegoo.ElegooSession, "_loop", lambda self: None)
    threads: list = []
    real = elegoo.threading.Thread

    def _spy(*a, **kw):
        t = real(*a, **kw)
        threads.append(t)
        return t

    monkeypatch.setattr(elegoo.threading, "Thread", _spy)
    ses.start()
    ses.start()
    assert len(threads) == 1
    threads[0].join(timeout=2.0)


def test_pending_queue_is_per_session():
    """Очереди ожидания ответов тоже раздельные — иначе ответ одного принтера разбудил бы
    запрос, отправленный другому."""
    a, b = _session2(), _session2()
    a.pending[1] = queue.Queue()
    assert 1 not in b.pending
    assert a.pending is not b.pending


# ─── Реестр сессий (шаг 2 волны 6) ───────────────────────────────────────────────────────

def _configure_default(monkeypatch, ip="192.168.0.77", code="CODE1", serial="SN1"):
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", ip)
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", code)
    monkeypatch.setattr(config, "ELEGOO_SERIAL", serial)


@pytest.mark.parametrize("form", [
    "192.168.0.77", " 192.168.0.77 ", "192.168.0.77/", "http://192.168.0.77",
    "192.168.0.77:1883", "HTTP://192.168.0.77/",
])
def test_registry_never_opens_a_second_session_to_the_same_broker(monkeypatch, form):
    """Ключ реестра — НОРМАЛИЗОВАННЫЙ адрес.

    Побайтовое сравнение считало бы эти шесть форм шестью принтерами. На деле это один брокер,
    и второй подписчик пришёл бы к нему с тем же client_id (CHANNEL_ID зашит константой) —
    брокер обязан рвать предыдущую сессию, и получается бесконечная война реконнектов, снаружи
    видная как «принтер мигает онлайн/оффлайн»."""
    _configure_default(monkeypatch)
    assert elegoo.session_for(elegoo.Endpoint(ip=form, code="X")) is elegoo.session()


def test_registry_gives_a_foreign_address_its_own_session(monkeypatch):
    """Чужой адрес — своя сессия, и та же самая при повторном спросе (иначе каждый вызов
    поднимал бы нового подписчика)."""
    _configure_default(monkeypatch)
    a = elegoo.session_for(_EP2)
    b = elegoo.session_for(_EP2)
    assert a is b
    assert a is not elegoo.session()
    assert a.endpoint().ip == _EP2.ip


def test_registry_updates_credentials_without_reopening(monkeypatch):
    """Сменили код доступа в карточке — сессия та же, реквизиты новые. Иначе принтер отваливался
    бы после правки кода без единой строчки в логе."""
    _configure_default(monkeypatch)
    first = elegoo.session_for(_EP2)
    again = elegoo.session_for(elegoo.Endpoint(ip=_EP2.ip, code="NEW", serial="SN2"))
    assert again is first
    assert first.endpoint().code == "NEW"


def test_sessions_sync_starts_new_and_stops_gone(monkeypatch):
    """`sessions_sync` — единственная точка, где реестр меняется под список карточек."""
    _configure_default(monkeypatch)
    started, stopped = [], []
    monkeypatch.setattr(elegoo.ElegooSession, "start",
                        lambda self: started.append(self.endpoint().ip))
    monkeypatch.setattr(elegoo.ElegooSession, "stop",
                        lambda self: stopped.append(self.endpoint().ip))

    ep3 = elegoo.Endpoint(ip="10.77.0.3", code="CODE3", serial="SN3")
    assert elegoo.sessions_sync([_EP2, ep3]) == ["10.77.0.2", "10.77.0.3"]
    assert sorted(started) == ["10.77.0.2", "10.77.0.3"] and stopped == []

    started.clear()
    assert elegoo.sessions_sync([ep3]) == ["10.77.0.3"]     # вторую карточку удалили
    assert stopped == [_EP2.ip]                             # её поток погашен, а не брошен
    assert started == ["10.77.0.3"]                         # уцелевшая перезапуску не мешает


def test_sessions_sync_never_touches_the_default_printer(monkeypatch):
    """Принтер по умолчанию в реестр не попадает: его сессией управляет start_subscriber(), и
    погасить её означало бы обезглавить всю автоматику мастерской (журнал, автофиниш, свип)."""
    _configure_default(monkeypatch)
    stopped = []
    monkeypatch.setattr(elegoo.ElegooSession, "start", lambda self: None)
    monkeypatch.setattr(elegoo.ElegooSession, "stop", lambda self: stopped.append(self))

    own = elegoo.Endpoint(ip="192.168.0.77", code="CODE1", serial="SN1")
    assert elegoo.sessions_sync([own]) == []
    assert elegoo.sessions_stop_all() is None
    assert stopped == []                                   # сессия по умолчанию не гасилась
    assert elegoo.session().cached_status is not None      # …и осталась на месте


def test_sessions_sync_ignores_printers_with_nowhere_to_go(monkeypatch):
    """Карточка без адреса или без кода доступа подписчика не получает: идти некуда, а поток
    вечно ломился бы в пустоту."""
    _configure_default(monkeypatch)
    monkeypatch.setattr(elegoo.ElegooSession, "start", lambda self: None)
    assert elegoo.sessions_sync([elegoo.Endpoint(ip="10.77.0.9", code="")]) == []
    assert elegoo.sessions_sync([elegoo.Endpoint(ip="", code="CODE")]) == []


def test_stop_lets_the_subscriber_thread_end(monkeypatch):
    """`stop()` обязан ОСТАНАВЛИВАТЬ поток, а не только помечать флаг: иначе удалённая карточка
    оставляет подписчика навсегда."""
    _configure_default(monkeypatch)
    ses = elegoo.ElegooSession(lambda: elegoo.Endpoint(ip="", code=""))   # некуда идти → холостой ход
    ses.start()
    assert ses._started is True
    ses.stop()
    assert ses._started is False
    live = [t for t in elegoo.threading.enumerate() if t.name.startswith("elegoo-mqtt-sub")]
    for t in live:
        t.join(timeout=12.0)
    assert not [t for t in live if t.is_alive()]


@pytest.mark.asyncio
async def test_db_refresh_lifts_subscribers_only_for_elegoo_cards(tmp_db, monkeypatch):
    """`db.refresh_printers` — тот самый хук «после любой правки карточек».

    Карточка Moonraker не должна получить MQTT-подписчика Elegoo, а карточка без собственного
    адреса — вообще ничего: она про тот же принтер, что и .env."""
    _configure_default(monkeypatch)
    seen: list = []
    monkeypatch.setattr(elegoo, "sessions_sync", lambda eps: seen.append(list(eps)) or [])

    pid = await tmp_db.add_printer("Второй", "profile", "proc", "fil", is_active=True)
    await tmp_db.update_printer(pid, kind="elegoo", address="10.77.0.2", access_code="CODE2",
                                serial="SN2")
    mid = await tmp_db.add_printer("Мунрейкер", "profile", "proc", "fil", is_active=True)
    await tmp_db.update_printer(mid, kind="moonraker", address="http://10.0.0.9:7125")

    await tmp_db.refresh_printers()
    assert seen, "хук не позвал реестр"
    ips = [e.ip for e in seen[-1]]
    assert "10.77.0.2" in ips                    # карточка Elegoo со своим адресом — да
    assert "http://10.0.0.9:7125" not in ips     # Moonraker — не наш протокол
