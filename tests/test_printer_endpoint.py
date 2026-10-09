"""Адресный шпион: куда физически уходят байты к принтеру.

Сегодня адрес принтера живёт ровно в одном месте — `config.ELEGOO_PRINTER_IP` из .env, — и то,
что ВСЕ живые пути ходят именно туда, не проверяет ни один тест. Волна 2 цикла 2 собирается
протащить адрес карточки принтера через четырнадцать мест app/elegoo.py; без этого файла у неё
не будет доказательства, что единственный боевой CC2 остался на месте. Поэтому здесь прибито
СЕГОДНЯШНЕЕ поведение пяти живых путей: `get_status`, `_open` (через `_request_transient`),
`upload_gcode`, `camera_url` и `webapp._timelapse_download_url`. Файл обязан быть зелёным ДО
волны 2 и остаться зелёным ПОСЛЕ — с переписанным источником адреса, но тем же адресом.

Вместе с адресом проверяются код доступа и СЕРИЙНИК. Адрес без кода — отказ в подключении;
адрес без серийника хуже: подписчик доедет до правильного принтера и подпишется на чужую тему,
то есть на тишину, и это не диагностируется ничем, кроме «команды молча не работают».

Сети здесь нет вовсе. По умолчанию `socket.create_connection` и `http.client.HTTPConnection`
подменены заглушкой, которая ПАДАЕТ: любой не учтённый здесь выход наружу обвалит тест, а не
постучится в боевой принтер. Тесты, которым сеть нужна по существу, берут фикстуру `net` —
она те же две точки подменяет записывающими заглушками.

Существующий tests/test_elegoo.py на эту роль не годится: его фейк HTTPConnection принимает
`(*a, **k)` и на хост не смотрит вовсе.
"""
import struct
import urllib.parse

import pytest

from app import config, elegoo, webapp

# Сентинелы вместо боевых значений: они не совпадают ни с чем в .env, поэтому «адрес взят из
# настроек» доказывается по существу, а не случайным совпадением с реальным принтером.
_IP = "10.77.0.5"
_CODE = "SPY-ACCESS-CODE"
_USER = "spyuser"
_SERIAL = "SPYSN0123"

# Готовые ответы принтера. CONNACK «принято» нужен всем MQTT-путям. SUBACK — только
# `_request_transient`: он ждёт подтверждения подписки в цикле по времени (app/elegoo.py:727-734),
# и без ответа цикл впустую крутится три секунды.
_CONNACK_OK = bytes([0x20, 0x02, 0x00, 0x00])
_SUBACK_OK = bytes([0x90, 0x03, 0x00, 0x01, 0x00])


# ─────────────────────────── заглушки сети ───────────────────────────

class _ScriptedSock:
    """MQTT-сокет по сценарию: отдаёт заранее записанные байты, потом EOF.

    Именно EOF, а не `socket.timeout`: на таймауте циклы чтения в elegoo.py ждут до своего
    дедлайна (секунды), а пустой recv они трактуют как «соединение закрылось» и выходят сразу.
    Тест от этого мгновенный и не зависит ни от одного таймера."""

    def __init__(self, script: bytes = b""):
        self._inbox = bytearray(script)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent.append(bytes(data))

    def recv(self, size):
        chunk = bytes(self._inbox[:size])
        del self._inbox[:size]
        return chunk

    def close(self):
        self.closed = True


class _FakeResponse:
    """Ответ принтера на PUT-чанк: «принят»."""

    status = 200

    @staticmethod
    def read():
        return b'{"error_code": 0}'


class _FakeConn:
    """Заглушка http.client.HTTPConnection: запоминает запросы и заголовки."""

    def __init__(self):
        self.requests: list = []        # (метод, путь, заголовки, длина тела)
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, dict(headers or {}), len(body or b"")))

    def getresponse(self):
        return _FakeResponse()

    def close(self):
        self.closed = True


class _Net:
    """Журнал всех попыток выйти наружу — это и есть «шпион»."""

    def __init__(self):
        self.mqtt: list = []            # (хост, порт, таймаут) на каждый socket.create_connection
        self.http: list = []            # (хост, порт, таймаут) на каждый HTTPConnection
        self.socks: list = []
        self.conns: list = []
        self.script = _CONNACK_OK + _SUBACK_OK

    @property
    def hosts(self) -> set:
        """Все адреса, на которые ушёл хоть один байт."""
        return {h for h, _p, _t in self.mqtt} | {h for h, _p, _t in self.http}


@pytest.fixture
def net(monkeypatch):
    """Записывающие заглушки вместо сетевых примитивов app/elegoo.py."""
    spy = _Net()

    def create_connection(address, timeout=None, *a, **kw):
        host, port = address
        spy.mqtt.append((host, port, timeout))
        sock = _ScriptedSock(spy.script)
        spy.socks.append(sock)
        return sock

    def http_connection(host, port=None, timeout=None, **kw):
        spy.http.append((host, port, timeout))
        conn = _FakeConn()
        spy.conns.append(conn)
        return conn

    monkeypatch.setattr(elegoo.socket, "create_connection", create_connection)
    monkeypatch.setattr(elegoo.http.client, "HTTPConnection", http_connection)
    return spy


@pytest.fixture(autouse=True)
def printer_env(monkeypatch):
    """Настройки принтера — сентинелы, состояние подписчика — «привязки нет», сеть — запрещена.

    Про состояние подписчика отдельно: `request()` выбирает путь по модульным глобалям
    (`_live_sock`, `_bound`, `_live['connected']`), то есть по состоянию процесса. Не зафиксируй
    мы его здесь — каким путём пойдёт вызов, решал бы соседний тест, а не этот файл."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", _IP)
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", _CODE)
    monkeypatch.setattr(config, "ELEGOO_MQTT_USER", _USER)
    monkeypatch.setattr(config, "ELEGOO_SERIAL", _SERIAL)
    monkeypatch.setattr(config, "PRINTER_CAMERA_URL", "")
    monkeypatch.setattr(elegoo.session(), "sock", None)
    monkeypatch.setattr(elegoo.session(), "bound", False)
    monkeypatch.setitem(elegoo.session().live, "connected", False)

    def _no_network(*a, **kw):
        raise AssertionError(f"путь пошёл в сеть в обход шпиона: {a!r} {kw!r}")

    monkeypatch.setattr(elegoo.socket, "create_connection", _no_network)
    monkeypatch.setattr(elegoo.http.client, "HTTPConnection", _no_network)


# ─────────────────────────── разбор MQTT-пакетов ───────────────────────────
# Пакеты разбираются по-настоящему, а не грепом по байтам: тема должна ловиться целиком,
# иначе подмена `elegoo/<SN>/…` на `elegoo/<чужой SN>/…` пройдёт незамеченной.

def _decode(raw: bytes) -> tuple[int, bytes]:
    """Сырой MQTT-пакет → (тип, тело без фиксированного заголовка)."""
    mult, val, i = 1, 0, 1
    while True:
        b = raw[i]
        i += 1
        val += (b & 0x7F) * mult
        if not (b & 0x80):
            break
        mult *= 128
    return raw[0], raw[i:i + val]


def _connect_fields(raw: bytes) -> tuple[str, str, str]:
    """CONNECT → (client_id, user, password): три MQTT-строки после переменного заголовка.
    Переменный заголовок ровно 10 байт: "MQTT" (2+4) + уровень + флаги + keepalive (2)."""
    _ptype, rem = _decode(raw)
    off, out = 10, []
    for _ in range(3):
        n = struct.unpack(">H", rem[off:off + 2])[0]
        out.append(rem[off + 2:off + 2 + n].decode())
        off += 2 + n
    return out[0], out[1], out[2]


def _sub_topic(raw: bytes) -> str:
    """SUBSCRIBE → тема подписки (после packet id)."""
    _ptype, rem = _decode(raw)
    n = struct.unpack(">H", rem[2:4])[0]
    return rem[4:4 + n].decode()


def _publishes(sent: list) -> list:
    """Все PUBLISH из журнала отправки → [(тема, разобранное тело)]."""
    out = []
    for raw in sent:
        ptype, rem = _decode(raw)
        if (ptype & 0xF0) == 0x30:
            out.append(elegoo._parse_publish(ptype, rem))
    return out


# ─────────────────────────── живые пути ───────────────────────────

def test_get_status_dials_configured_printer(net):
    """Разовый опрос статуса (app/elegoo.py:221) — на адрес из настроек, порт MQTT, с кодом."""
    r = elegoo.get_status(timeout=0.5)
    assert r["ok"] is True and r["serial"] == _SERIAL
    assert net.mqtt == [(_IP, elegoo.MQTT_PORT, 5)]
    client_id, user, password = _connect_fields(net.socks[0].sent[0])
    assert client_id == "newbor-status"
    assert (user, password) == (_USER, _CODE)          # без кода принтер отвечает отказом


def test_transient_request_dials_configured_printer_with_its_serial(net):
    """Фолбэк без привязки (`_open` :648 из `_request_transient` :713): адрес — из настроек,
    и подписка, и публикация — по серийнику ЭТОГО же принтера."""
    elegoo.request(elegoo.METHOD_LIST_FILES, {}, 0.05)
    assert net.mqtt == [(_IP, elegoo.MQTT_PORT, 5)]
    sock = net.socks[0]
    client_id, user, password = _connect_fields(sock.sent[0])
    assert (client_id, user, password) == (elegoo.CLIENT_ID, _USER, _CODE)
    assert _sub_topic(sock.sent[1]) == f"elegoo/{_SERIAL}/#"
    pubs = _publishes(sock.sent)
    assert [t for t, _obj in pubs] == [f"elegoo/{_SERIAL}/{elegoo.CLIENT_ID}/api_request"]
    assert pubs[0][1]["method"] == elegoo.METHOD_LIST_FILES


def test_bound_request_publishes_to_channel_topic_of_the_same_printer(net, monkeypatch):
    """Быстрый путь (привязка жива, app/elegoo.py:674-683) шлёт в тему
    `elegoo/<SN>/<CHANNEL_ID>/api_request` ПО СОКЕТУ ПОДПИСЧИКА — своего соединения не открывает.

    Отсюда правило для второго принтера: адрес и серийник обязаны ехать вместе. Уедет адрес без
    серийника — тело уйдёт по правильному сокету в тему чужой машины и потеряется молча."""
    sock = _ScriptedSock()
    monkeypatch.setattr(elegoo.session(), "sock", sock)
    monkeypatch.setattr(elegoo.session(), "bound", True)
    monkeypatch.setitem(elegoo.session().live, "connected", True)

    elegoo.request(elegoo.METHOD_LIST_FILES, {}, 0.05)

    assert net.mqtt == [] and net.http == []           # быстрый путь не открывает соединений
    pubs = _publishes(sock.sent)
    assert [t for t, _obj in pubs] == [f"elegoo/{_SERIAL}/{elegoo.CHANNEL_ID}/api_request"]
    assert pubs[0][1]["method"] == elegoo.METHOD_LIST_FILES


def test_upload_gcode_dials_configured_printer_on_port_80(net, tmp_path):
    """Заливка .gcode (app/elegoo.py:1256-1274) — HTTP на тот же адрес, порт 80, токен = код."""
    f = tmp_path / "job.gcode"
    f.write_bytes(b"G1 X1\n" * 10)
    r = elegoo.upload_gcode(f)
    assert r["ok"] is True and r["name"] == "job.gcode"
    assert net.http == [(_IP, 80, 60.0)]               # одно соединение на всю заливку
    _method, path, headers, _size = net.conns[0].requests[0]
    assert path == "/upload"
    assert headers["X-Token"] == _CODE


def test_camera_url_points_at_configured_printer(monkeypatch):
    """Камера (app/elegoo.py:97) выводится из адреса принтера — порт и путь по умолчанию SDCP."""
    monkeypatch.delenv("ELEGOO_CAMERA_PORT", raising=False)
    monkeypatch.delenv("ELEGOO_CAMERA_PATH", raising=False)
    u = urllib.parse.urlsplit(elegoo.camera_url())
    assert u.hostname == _IP
    assert u.port == 8080 and u.path == "/mjpeg"


def test_timelapse_download_url_points_at_configured_printer():
    """Скачивание таймлапса (app/webapp.py:9071) — единственный путь к принтеру ВНЕ elegoo.py.
    Волна 2 обязана протащить эндпоинт и сюда, иначе адрес разъедется с остальными."""
    u = urllib.parse.urlsplit(webapp._timelapse_download_url("video/job.gcode.mp4"))
    assert u.hostname == _IP
    assert u.port is None                              # роут /download принтера живёт на 80
    q = urllib.parse.parse_qs(u.query)
    assert q["X-Token"] == [_CODE]
    assert q["file_name"] == ["video/job.gcode.mp4"]


def test_all_live_paths_share_one_address(net, tmp_path):
    """Свод: сегодня у продукта ОДИН принтер, и все пути ведут ровно на один адрес.

    Это и есть страховка волны 2: как только адрес поедет из карточки принтера, здесь появится
    второй хост — и тест назовёт путь, который остался на старом источнике. Порт и путь камеры
    здесь не фиксируются (это дело соседнего теста) — сверяется только хост."""
    f = tmp_path / "job.gcode"
    f.write_bytes(b"G1 X1\n")

    elegoo.get_status(timeout=0.5)
    elegoo.request(elegoo.METHOD_LIST_FILES, {}, 0.05)
    elegoo.upload_gcode(f)
    seen = set(net.hosts)
    seen.add(urllib.parse.urlsplit(elegoo.camera_url()).hostname)
    seen.add(urllib.parse.urlsplit(webapp._timelapse_download_url("video/a.mp4")).hostname)

    assert seen == {_IP}
    assert len(net.mqtt) == 2 and len(net.http) == 1   # ни одного лишнего соединения
