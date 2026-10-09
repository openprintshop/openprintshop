"""Слой драйверов принтеров и симулятор (OpenTodo 0613014e, multi-printer-support.md этап 0).

Что здесь защищается:

* **совместимость** — у боевого принтера поле kind пустое, и он обязан по-прежнему
  разговаривать по Elegoo. Регресс здесь означает «мастерская осталась без пульта»;
* **честные способности** — принтер, который чего-то не умеет, должен говорить это вслух,
  а не делать вид, что команда прошла. Именно на этом раньше держались скрытые связки:
  фича «работала» на CC2 и молча не работала на всём остальном;
* **симулятор** — тестовое окружение вместо железа: печать начинается, идёт и заканчивается
  по команде теста, поэтому автоматику финиша можно проверять без часа ожидания и пластика.
"""
import pytest

from app import config, elegoo, printerdrv
from app.printerdrv import Caps, FakePrinter


@pytest.fixture(autouse=True)
def _no_override():
    """Подмена драйвера глобальна на процесс — снимаем её, чтобы симулятор не протёк дальше."""
    yield
    printerdrv.use_driver(None)


class _P:
    """Минимальная карточка принтера (нам нужны только поля протокола и id).

    id по умолчанию 1 — как у боевого принтера: старые тесты писались, когда карточка была
    одна, и не должны были обрасти лишним аргументом из-за появления второй."""
    def __init__(self, kind="", address="", access_code="", pid=1, serial=""):
        self.id, self.name = pid, "тестовый"
        self.kind, self.address, self.access_code = kind, address, access_code
        self.serial = serial


# ── выбор драйвера ─────────────────────────────────────────────────────────────────────────
def test_empty_kind_keeps_todays_behaviour(monkeypatch):
    """Боевой принтер заведён до этой задачи: kind пуст, адрес в .env — должен остаться Elegoo."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    assert printerdrv.for_printer(_P()).kind == "elegoo"


def test_autodetect_falls_back_to_moonraker_then_none(monkeypatch):
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://192.168.0.50:7125")
    assert printerdrv.for_printer(_P()).kind == "moonraker"
    monkeypatch.setattr(config, "MOONRAKER_URL", "")
    assert printerdrv.for_printer(_P()).kind == "none"


def test_explicit_kind_wins(monkeypatch):
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    assert printerdrv.for_printer(_P(kind="none")).kind == "none"
    assert printerdrv.for_printer(_P(kind="moonraker")).kind == "moonraker"


def test_unknown_kind_degrades_instead_of_crashing():
    """Тип из будущей версии в старой сборке не должен ронять пульт."""
    assert printerdrv.for_printer(_P(kind="prusa-connect")).kind == "none"


def test_bambu_card_promises_nothing_until_the_printer_answers():
    """Карточку Bambu можно завести, но способности она получает ПО ФАКТУ ответа машины.

    До волны 7а `kind="bambu"` не сохранялся вовсе («Неизвестный тип принтера»), и владелец не
    мог подготовить мастерскую к приезду принтера — назначить профили, слайсер, место в ёмкости
    сроков. Теперь карточка живёт, а сетевая часть написана, но на живом железе не проверена ни
    байтом — поэтому набор способностей пуст, пока сессия не приняла хотя бы один отчёт."""
    d = printerdrv.for_printer(_P(kind="bambu", address="10.0.0.7", access_code="C", serial="S"))
    assert d.kind == "bambu"
    assert d.enabled() is True         # карточка заполнена: идти есть куда
    assert d.describe()["caps"] == Caps().as_dict()   # но обещать пока нечего
    assert d.describe()["note"]
    assert d.status() is None          # не пустой словарь: иначе это «на связи и простаивает»
    assert d.camera_url() == ""
    assert "bambu" in dict(printerdrv.KINDS)


def test_card_address_overrides_env(monkeypatch):
    """White-label: адрес принтера у покупателя живёт в карточке, а не в .env."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "")
    d = printerdrv.for_printer(_P(kind="elegoo", address="10.0.0.5", access_code="1234"))
    assert d.enabled()
    assert printerdrv.for_printer(_P(kind="elegoo")).enabled() is False


# ── честные способности ────────────────────────────────────────────────────────────────────
def test_none_driver_admits_it_cannot():
    d = printerdrv.for_printer(_P(kind="none"))
    assert d.caps.status is False and d.caps.upload is False
    assert d.start("job.gcode")["ok"] is False
    assert d.control("pause")["ok"] is False
    assert d.camera_url() == ""


def test_elegoo_declares_the_full_set(monkeypatch):
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    caps = printerdrv.for_printer(_P(kind="elegoo")).caps
    assert caps.status and caps.upload and caps.start and caps.control and caps.timelapse
    # Стоковая прошивка не Klipper: команды «пропусти объект» в протоколе нет.
    assert caps.exclude_object is False


def test_moonraker_declares_less_but_honestly(monkeypatch):
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://192.168.0.50:7125")
    d = printerdrv.for_printer(_P(kind="moonraker"))
    assert d.caps.status and d.caps.upload
    assert d.caps.timelapse is False and d.caps.camera is False
    # exclude_object и start раньше объявлялись «на вырост»: метода exclude_object нет ни в
    # одном драйвере и даже в интерфейсе, а start всегда отказывал. Способность обязана
    # означать «умею сейчас», иначе сервер пропускает кнопку, а принтер отвечает отказом.
    assert d.caps.exclude_object is False
    assert d.caps.start is False
    assert d.start("x.gcode")["ok"] is False       # и метод по-прежнему честно объясняет почему
    assert d.start("job.gcode")["ok"] is False    # печатает только при заливке — честно говорим


def test_describe_is_ui_ready():
    d = printerdrv.for_printer(_P(kind="none"))
    info = d.describe()
    assert info["kind"] == "none" and info["title"]
    assert set(info["caps"]) == set(Caps().as_dict())


# ── пульт у КАЖДОЙ настроенной карточки (предохранитель снят, цикл 3 волна 6) ──────────────
# Предохранитель гасил пульт карточке, чей адрес не совпал с адресом единственного живого
# канала. Держался он не на адресе — тот едет параметром протокола с волны 1, — а на том, что
# живой канал в процессе был ОДИН: подписчик MQTT, ретранслятор камеры и printwatch знали ровно
# один принтер, и вторая карточка получила бы правдивые команды и лживую телеметрию. Теперь у
# каждого адреса своя сессия, у каждой камеры свой ретранслятор, а журнал, привязка и наработка
# ТО ведутся по машинам — гасить стало нечего.

def test_todays_live_card_keeps_the_full_remote(monkeypatch):
    """БАРЬЕР. Боевая карточка (kind="", address="") обязана сохранить ПОЛНЫЙ пульт.

    Красный здесь означает не «предохранитель сработал», а «мастерская осталась без пульта»:
    у боевого принтера поля карточки пусты, адрес берётся из .env — то есть ровно тот же, куда
    ходит подписчик. Это самое дорогое, что может сломать работа по второму принтеру, поэтому
    проверка стоит первой и сверяет набор способностей целиком, а не выборочно."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")

    d = printerdrv.for_printer(_P())
    assert d.kind == "elegoo" and d.enabled() is True

    caps = d.describe()["caps"]
    assert caps == printerdrv.ElegooDriver.caps.as_dict()
    # Поимённо — то, чем мастер пользуется каждый день: пауза/стоп/свет, заливка, камера, таймлапс.
    assert caps["control"] and caps["upload"] and caps["camera"] and caps["timelapse"]
    assert caps["status"] and caps["files"] and caps["start"] and caps["move"]


def test_live_card_survives_spaces_around_the_address(monkeypatch):
    """Тот же барьер, но адрес вбит руками с пробелами: сравнение идёт ПОСЛЕ strip.

    Иначе предохранитель снимал бы пульт из-за пробела в поле админки, и выглядело бы это как
    «принтер отвалился», а не как ошибка ввода."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    d = printerdrv.for_printer(_P(kind="elegoo", address="  192.168.0.77  "))
    assert d.enabled() is True
    assert d.describe()["caps"] == printerdrv.ElegooDriver.caps.as_dict()


def test_second_card_gets_its_own_full_remote(monkeypatch):
    """Карточка второго принтера получает ПОЛНЫЙ пульт — ради этого волна и делалась.

    Раньше здесь стоял обратный тест: enabled=False и пустые способности. Он описывал не
    желаемое, а вынужденное — вторая карточка обещала бы кнопки, которые нажимали бы первый
    принтер. Теперь у неё своя сессия, своя камера и свой журнал."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")

    d = printerdrv.for_printer(
        _P(kind="elegoo", address="192.168.0.99", access_code="OTHER", pid=2))
    info = d.describe()

    assert info["enabled"] is True
    assert info["caps"] == printerdrv.ElegooDriver.caps.as_dict()
    assert d.endpoint().ip == "192.168.0.99" and d.endpoint().code == "OTHER"
    # …но «та самая машина из .env» — по-прежнему первая: от этого зависит, чья камера-мост
    assert d.is_the_default_printer() is False
    assert printerdrv.for_printer(_P()).is_the_default_printer() is True


def test_card_without_a_code_still_has_no_remote(monkeypatch):
    """Незаполненная карточка пульта не получает: идти некуда и нечем.

    Это и есть остаток гейта после снятия предохранителя — проверка НАСТРОЕК, а не «та ли это
    машина»: кнопка, которой нечем авторизоваться, обещала бы несуществующее."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "")
    assert printerdrv.for_printer(_P(kind="elegoo", address="10.0.0.5")).enabled() is False
    assert printerdrv.for_printer(_P(kind="elegoo", access_code="X")).enabled() is False


def test_card_is_the_only_address_when_env_is_empty(monkeypatch):
    """White-label: .env пуст → подписчику идти некуда, путать не с чем — пульт у карточки.

    Без этой ветки предохранитель отобрал бы пульт у установки покупателя, где адрес живёт
    только в карточке, — а именно ради неё карточка и заводилась."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "")
    d = printerdrv.for_printer(_P(kind="elegoo", address="10.0.0.5", access_code="1234"))
    assert d.enabled() is True
    assert d.describe()["caps"] == printerdrv.ElegooDriver.caps.as_dict()


def test_env_serial_belongs_to_the_env_machine_only(monkeypatch):
    """.env-серийник достаётся ТОЛЬКО карточке, ведущей на .env-машину.

    Поле «серийник» в карточке необязательное (у боевой установки он автоопределяется из топика).
    Подставь .env-серийник карточке с ЧУЖИМ адресом — и тело уйдёт на правильный принтер в тему
    ЧУЖОЙ машины: команда не выполнится нигде, а вызов отчитается об успехе. Хуже того, подписчик
    такого адреса не привяжется НИКОГДА: автоопределение серийника из топика срабатывает только
    при пустом серийнике."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_SERIAL", "SN-FROM-ENV")

    # карточка боевого принтера (поля пусты) — фолбэк как был
    assert printerdrv.for_printer(_P(kind="elegoo")).endpoint().serial == "SN-FROM-ENV"
    # та же машина, адрес вписан руками в другой форме — тоже своя
    assert printerdrv.for_printer(
        _P(kind="elegoo", address="http://192.168.0.77/")).endpoint().serial == "SN-FROM-ENV"
    # карточка со СВОИМ серийником — он и едет
    assert printerdrv.for_printer(_P(kind="elegoo", serial=" SN-CARD ")).endpoint().serial == "SN-CARD"

    # ЧУЖОЙ адрес без серийника — пусто, а не серийник первого принтера
    foreign = printerdrv.for_printer(_P(kind="elegoo", address="10.77.0.2", pid=2))
    assert foreign.endpoint().serial == ""
    assert foreign.endpoint().ip == "10.77.0.2"

    # white-label: .env пуст, адрес есть только у карточки — путать не с чем
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "")
    assert printerdrv.for_printer(
        _P(kind="elegoo", address="10.77.0.2", pid=2)).endpoint().serial == "SN-FROM-ENV"


def test_subscriber_endpoint_names_the_live_printer(monkeypatch):
    """Источник правды предохранителя: (ip, serial, connected) живого подписчика."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", " 192.168.0.77 ")
    monkeypatch.setattr(config, "ELEGOO_SERIAL", "SN-FROM-ENV")
    monkeypatch.setitem(elegoo.session().live, "serial", "SN-FROM-TOPIC")
    monkeypatch.setitem(elegoo.session().live, "connected", True)
    assert elegoo.subscriber_endpoint() == ("192.168.0.77", "SN-FROM-TOPIC", True)

    # Серийник из топика ещё не прилетел — фолбэк на настройки; связи нет — connected=False.
    monkeypatch.setitem(elegoo.session().live, "serial", "")
    monkeypatch.setitem(elegoo.session().live, "connected", False)
    assert elegoo.subscriber_endpoint() == ("192.168.0.77", "SN-FROM-ENV", False)


def test_moonraker_card_with_a_foreign_url_gets_no_remote(monkeypatch):
    """То же и у Klipper: app/moonraker.py собирает URL из .env, а не из карточки."""
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://192.168.0.50:7125")

    live = printerdrv.for_printer(_P(kind="moonraker"))
    assert live.enabled() is True
    assert live.describe()["caps"] == printerdrv.MoonrakerDriver.caps.as_dict()
    # Хвостовой слэш — тот же принтер: app/moonraker.py его срезает.
    assert printerdrv.for_printer(_P(kind="moonraker",
                                     address="http://192.168.0.50:7125/")).enabled() is True

    # Вторая карточка Klipper — свой полный пульт: status() у него и так ходит по сети с
    # эндпоинтом карточки, единственным доводом за предохранитель была симметрия с Elegoo.
    other = printerdrv.for_printer(_P(kind="moonraker", address="http://10.0.0.9:7125", pid=2))
    assert other.enabled() is True
    assert other.describe()["caps"] == printerdrv.MoonrakerDriver.caps.as_dict()
    assert other.is_the_default_printer() is False


def test_two_cards_work_at_once_and_do_not_share_a_channel(monkeypatch):
    """Мастерская с двумя принтерами: работают ОБЕ, и каждая по своему каналу.

    Печать одновременная, поэтому «пульт остаётся у боевого, второй молчит» — уже не цель, а
    регрессия. Проверяем и то, ради чего снимался предохранитель: команды и телеметрия у
    карточек разные, а не общие."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")

    live = printerdrv.for_printer(_P(pid=1))
    second = printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99",
                                       access_code="CODE2", pid=2))
    assert (live.enabled(), second.enabled()) == (True, True)
    assert live.endpoint().ip != second.endpoint().ip
    assert elegoo.session_for(live.endpoint()) is not elegoo.session_for(second.endpoint())


def test_seam_can_simulate_the_second_card_without_muting_the_first(monkeypatch):
    """Расширенный шов (use_driver с printer_id) и предохранитель не мешают друг другу.

    Симулятор на карточке 2 — то, чем будет проверяться второй принтер, когда адрес поедет
    параметром; карточка 1 при этом обязана остаться настоящим Elegoo с полным пультом."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    sim = FakePrinter()
    printerdrv.use_driver(sim, printer_id=2)

    assert printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99", pid=2)) is sim
    live = printerdrv.for_printer(_P(pid=1))
    assert live.enabled() is True and live.describe()["caps"]["control"] is True


def test_factory_seam_sees_the_muted_card_by_its_address(monkeypatch):
    """Фабрика различает карточки по адресу — id второму принтеру может быть ещё не присвоен."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    sim = FakePrinter()
    printerdrv.use_driver_factory(
        lambda p: sim if getattr(p, "address", "").strip() == "192.168.0.99" else None)

    assert printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99")) is sim


# ── эндпоинт из карточки: куда физически уходят байты ──────────────────────────────────────
# Здесь закрывается находка, ради которой затевалась вся волна. Раньше поля карточки не
# покидали драйвера: `camera_url()` карточки со своим адресом отдавал адрес ПЕРВОГО принтера,
# `upload()` заливал файл на первый, `control("pause")` вставал на паузу у первого. Проверено
# запуском (разведка волны 4 цикла 1), а не выведено из чтения. Теперь адрес, код и серийник
# едут параметром `ep`, и ловится это без единого байта в сеть — по тому, ЧТО драйвер передал.

def test_endpoint_is_built_from_the_card(monkeypatch):
    """Адрес, код и серийник в эндпоинте — из карточки, а не из .env."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE-ENV")
    monkeypatch.setattr(config, "ELEGOO_SERIAL", "SN-ENV")

    e = printerdrv.for_printer(_P(kind="elegoo", address=" 10.0.0.5 ", access_code=" C2 ",
                                  serial=" SN2 ", pid=2)).endpoint()
    assert (e.ip, e.code, e.serial) == ("10.0.0.5", "C2", "SN2")


def test_endpoint_falls_back_to_env_and_keeps_camera_and_mqtt_settings(monkeypatch):
    """Пустая карточка = принтер из .env (боевая установка), а поля, которых в карточке нет,
    берутся у принтера по умолчанию.

    Второе важнее первого: пользователь MQTT и порт/путь камеры карточка не хранит, и собери
    драйвер эндпоинт «с нуля» — у тех, кто задал ELEGOO_MQTT_USER или ELEGOO_CAMERA_PORT/PATH,
    они бы молча заменились на значения по умолчанию, то есть камера бы просто исчезла."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE-ENV")
    monkeypatch.setattr(config, "ELEGOO_SERIAL", "SN-ENV")
    monkeypatch.setattr(config, "ELEGOO_MQTT_USER", "custom-user")
    monkeypatch.setenv("ELEGOO_CAMERA_PORT", "8081")
    monkeypatch.setenv("ELEGOO_CAMERA_PATH", "/stream")

    e = printerdrv.for_printer(_P(kind="elegoo")).endpoint()
    assert (e.ip, e.code, e.serial) == ("192.168.0.77", "CODE-ENV", "SN-ENV")
    assert (e.mqtt_user, e.camera_port, e.camera_path) == ("custom-user", "8081", "/stream")


def test_camera_url_follows_the_card_not_env(monkeypatch):
    """БАРЬЕР на доказанный запуском баг: камера второй карточки показывала ПЕРВЫЙ принтер.

    Сети здесь нет — URL строится из эндпоинта, и этого достаточно: именно его подставляли
    и ретранслятор камеры, и скрин стола по итогам печати."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    monkeypatch.setattr(config, "PRINTER_CAMERA_URL", "")
    monkeypatch.delenv("ELEGOO_CAMERA_PORT", raising=False)
    monkeypatch.delenv("ELEGOO_CAMERA_PATH", raising=False)

    second = printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99",
                                       access_code="CODE2", pid=2))
    assert second.camera_url() == "http://192.168.0.99:8080/mjpeg"
    assert printerdrv.for_printer(_P(pid=1)).camera_url() == "http://192.168.0.77:8080/mjpeg"


def test_every_protocol_call_carries_the_cards_endpoint(monkeypatch):
    """Ни один делегат драйвера не имеет права уйти в протокол без эндпоинта.

    Проверяем скопом, а не по одному вызову: самое дорогое место этой работы — забыть ОДИН
    делегат. Забытый уедет на принтер из .env, а вызов отчитается об успехе."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    seen: dict = {}

    def spy(name):
        def _fn(*a, ep=None, **kw):
            seen[name] = ep
            return {"ok": True}
        return _fn

    names = ("list_files", "start_print", "delete_file", "file_grams", "control",
             "set_temperature", "home", "move_axis", "set_fan", "load_filament",
             "unload_filament", "job_history", "latest_timelapse_url", "generate_timelapse")
    for n in names:
        monkeypatch.setattr(elegoo, n, spy(n))

    d = printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99", access_code="CODE2",
                                  serial="SN2", pid=2))
    d.files()
    d.start("job.gcode")
    d.delete_file("job.gcode")
    d.file_grams("job.gcode")
    d.control("pause")
    d.set_temperature("extruder", 200)
    d.home()
    d.move_axis("x", 10)
    d.set_fan("model", 100)
    d.load_filament(1)
    d.unload_filament(1)
    d.history()
    d.latest_timelapse_url("job.gcode")
    d.generate_timelapse("video/job.mp4")

    assert set(seen) == set(names), "делегат ушёл в протокол мимо эндпоинта"
    assert {(e.ip, e.code, e.serial) for e in seen.values()} == {("192.168.0.99", "CODE2", "SN2")}


@pytest.mark.asyncio
async def test_upload_goes_to_the_card_address(tmp_path, monkeypatch):
    """Заливка отдельно от остальных: она единственная асинхронная и читает адрес и код
    в СВОЁМ месте протокола (app/elegoo.py, HTTP-роут /upload), а не через request()."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    got: dict = {}

    def fake_upload(path, *, name=None, storage="local", timeout=60.0, ep=None):
        got["ep"], got["name"] = ep, name
        return {"ok": True}
    monkeypatch.setattr(elegoo, "upload_gcode", fake_upload)

    f = tmp_path / "job.gcode"
    f.write_bytes(b"G1 X1\n")
    d = printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99", access_code="CODE2",
                                  serial="SN2", pid=2))
    assert (await d.upload(f))["ok"] is True
    assert (got["ep"].ip, got["ep"].code, got["ep"].serial) == ("192.168.0.99", "CODE2", "SN2")
    assert got["name"] == "job.gcode"


def test_telemetry_asks_the_session_of_this_very_card(monkeypatch):
    """Телеметрия ТЕПЕРЬ едет с эндпоинтом — это и есть снятие ограничения волны 6.

    Раньше `cached_status` был кэшем единственного подписчика на процесс, и карточка с чужим
    адресом получила бы температуры ПЕРВОГО принтера как свои (ровно поэтому у неё и гасили
    пульт). Теперь вопрос адресован сессии ЭТОЙ карточки: у неё либо свои показания, либо
    честный None, пока её подписчика не подняли. Молчание честнее чужих цифр: по ним мастер
    решает, можно ли трогать машину."""
    import time as _t

    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    calls: list = []
    monkeypatch.setattr(elegoo, "cached_status",
                        lambda max_age=6.0, ep=None: calls.append((max_age, ep and ep.ip)))

    printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99", pid=2)).status(30.0)
    assert calls == [(30.0, "192.168.0.99")]

    # …и по существу: снимок принтера по умолчанию чужой карточке не достаётся
    monkeypatch.undo()
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    with elegoo.session().live_lock:
        elegoo.session().live.update({"state": {"extruder": {"temperature": 200}},
                                      "ts": _t.time(), "serial": "SN1", "connected": True})
    assert elegoo.cached_status(6.0) is not None            # у своего принтера показания есть
    foreign = printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99", pid=2))
    assert foreign.status(6.0) is None                      # у чужой карточки — молчание


def test_moonraker_endpoint_carries_url_and_its_own_key(monkeypatch):
    """У Klipper та же работа: база API из карточки, а ключ — ИМЕННО к ней.

    Ключ первой машины, отправленный на вторую, — это выданный наружу секрет, а не лишний
    заголовок, поэтому api_key обязан ехать вместе с адресом."""
    monkeypatch.setattr(config, "MOONRAKER_URL", "http://192.168.0.50:7125")
    monkeypatch.setattr(config, "MOONRAKER_API_KEY", "KEY-ENV")

    live = printerdrv.for_printer(_P(kind="moonraker")).endpoint()
    assert (live.url, live.api_key) == ("http://192.168.0.50:7125", "KEY-ENV")

    second = printerdrv.for_printer(_P(kind="moonraker", address="http://10.0.0.9:7125/",
                                       access_code="KEY-2", pid=2)).endpoint()
    assert (second.url, second.api_key) == ("http://10.0.0.9:7125", "KEY-2")


def test_timelapse_download_url_follows_the_endpoint(monkeypatch):
    """Единственная ручка принтера, которую строит сам вебапп, тоже слушается эндпоинта.

    Не перееди она вместе с остальными — таймлапс второго принтера качался бы с первого
    (и с кодом доступа первого)."""
    from app import webapp
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", "192.168.0.77")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "CODE")
    d = printerdrv.for_printer(_P(kind="elegoo", address="192.168.0.99", access_code="CODE2",
                                  pid=2))

    u = webapp._timelapse_download_url("video/job.mp4", webapp._printer_ep(d))
    assert u.startswith("http://192.168.0.99/download?") and "X-Token=CODE2" in u
    # Без эндпоинта — принтер по умолчанию, ровно как до появления параметра.
    assert webapp._timelapse_download_url("video/job.mp4").startswith("http://192.168.0.77/")
    assert printerdrv.for_printer(_P()).enabled() is True


# ── симулятор ──────────────────────────────────────────────────────────────────────────────
def test_simulator_runs_a_print_without_hardware():
    """Прогон печати спрашивается через job_state — тем же вопросом, что задаёт автоматика.

    Раньше тест читал плоскую сводку из fake.enrich — форму, которой нет ни у одного реального
    драйвера. Симулятор с собственным контрактом проверяет себя, а не продукт (разбор 11.08)."""
    p = FakePrinter()
    p.begin("ECC2_0.4_Кот_PLA_0.2.gcode", minutes=40, grams=13.5, layers=200)

    live = p.job_state(p.status()["state"])
    assert live["active"] and live["name"].startswith("ECC2")
    assert live["progress"] == 0.0 and live["minutes"] is None

    p.advance(minutes=20)
    mid = p.job_state(p.status()["state"])
    assert mid["progress"] == 0.5 and mid["minutes"] == 20.0
    assert p.enrich(p.status()["state"])["info"]["current_layer"] == 100

    p.finish()
    done = p.job_state(p.status()["state"])
    assert done["active"] is False
    assert p.history()["jobs"][0]["name"].startswith("ECC2")


def test_simulator_records_what_we_commanded():
    """Проверяем НАШИ команды, а не чужой протокол: что именно ушло на принтер."""
    p = FakePrinter()
    p.st.files["job.gcode"] = {"grams": 12, "print_time": 600}
    p.start("job.gcode", plate="textured", bedlevel=True)
    p.set_temperature("extruder", 215)
    p.control("light", on=True)

    kinds = [c[0] for c in p.st.commands]
    assert kinds == ["start", "temp", "control"]
    assert p.st.commands[0][2] == {"plate": "textured", "bedlevel": True}
    assert p.st.light is True


def test_simulator_start_refuses_while_busy():
    p = FakePrinter()
    p.begin("first.gcode", minutes=10)
    assert p.start("second.gcode")["ok"] is False


def test_simulator_stop_ends_the_print_as_failed():
    p = FakePrinter()
    p.begin("job.gcode", minutes=10)
    p.advance(3)
    p.control("stop")
    assert p.st.printing is False
    assert p.history()["jobs"][0]["ok"] is False


def test_simulator_can_be_a_poor_printer():
    """Изящная деградация: у бедного принтера кнопок нет, а не «нажалось и ничего»."""
    p = FakePrinter(caps=Caps(status=True, upload=True, start=True))
    assert p.files()["ok"] is False
    assert p.control("pause")["ok"] is False
    assert p.home()["ok"] is False
    assert p.camera_url() == ""
    assert p.latest_timelapse_url()["ok"] is False


def test_simulator_can_go_offline():
    p = FakePrinter()
    p.begin("job.gcode", minutes=5)
    p.disconnect()
    assert p.status() is None          # связи нет — и это не исключение, а «нет данных»


@pytest.mark.asyncio
async def test_simulator_upload_then_start(tmp_path):
    p = FakePrinter()
    f = tmp_path / "Кот.gcode"
    f.write_text("; gcode", encoding="utf-8")
    up = await p.upload(f)
    assert up["ok"] and up["name"] == "Кот.gcode"
    assert p.files()["files"][0]["name"] == "Кот.gcode"
    assert p.start("Кот.gcode")["ok"]


# ── шов подмены драйверов ──────────────────────────────────────────────────────────────────
def test_use_driver_replaces_the_whole_app():
    """Старая форма подмены — «на все карточки» РАЗОМ, и это сознательно.

    На ней держатся все существующие тесты (и фикстура fake_printer): им принтер безразличен,
    важно лишь, что железа не будет. Поэтому одна карточка или десять — драйвер один."""
    sim = FakePrinter()
    printerdrv.use_driver(sim)
    assert printerdrv.for_printer(_P(kind="elegoo")) is sim
    assert printerdrv.for_printer(_P(kind="moonraker", pid=7)) is sim
    printerdrv.use_driver(None)
    assert printerdrv.for_printer(_P(kind="none")) is not sim


def test_two_cards_get_their_own_driver():
    """Две карточки — два разных принтера: каждая получает СВОЙ драйвер, а не общий.

    Это тот сценарий, ради которого шов и расширен: пока подмена была одна на процесс, код,
    берущий «принтер» вместо «принтер этой карточки», нельзя было отличить от правильного."""
    first, second = FakePrinter(), FakePrinter()
    printerdrv.use_driver(first, printer_id=1)
    printerdrv.use_driver(second, printer_id=2)

    assert printerdrv.for_printer(_P(kind="elegoo", pid=1)) is first
    assert printerdrv.for_printer(_P(kind="elegoo", pid=2)) is second


def test_point_override_leaves_other_cards_to_their_kind():
    """Подмена одной карточки не ослепляет выбор драйвера у остальных."""
    printerdrv.use_driver(FakePrinter(), printer_id=1)
    assert printerdrv.for_printer(_P(kind="moonraker", pid=2)).kind == "moonraker"


def test_point_override_beats_the_blanket_one():
    """Точечная подмена ставится ПОВЕРХ общей: «всем симулятор, а второму — вот этот»."""
    everyone, special = FakePrinter(), FakePrinter()
    printerdrv.use_driver(everyone)
    printerdrv.use_driver(special, printer_id=2)

    assert printerdrv.for_printer(_P(pid=1)) is everyone
    assert printerdrv.for_printer(_P(pid=2)) is special


def test_driver_factory_decides_by_card_fields():
    """Фабрика различает принтеры по полям карточки — id в тестах бывает ещё не присвоен.

    None из фабрики означает «эту карточку не подменяю», и выбор драйвера идёт как обычно."""
    sim = FakePrinter()
    printerdrv.use_driver_factory(lambda p: sim if getattr(p, "address", "") == "10.0.0.5" else None)

    assert printerdrv.for_printer(_P(kind="elegoo", address="10.0.0.5")) is sim
    assert printerdrv.for_printer(_P(kind="moonraker", address="10.0.0.6")).kind == "moonraker"


def test_clearing_the_override_wipes_the_whole_seam():
    """use_driver(None) обязан гасить шов ЦЕЛИКОМ — на нём висит autouse-страховка от протечки.

    Если точечная подмена или фабрика переживут снятие, симулятор второго принтера утечёт
    в следующий тест, и упадёт он не там, где сломали."""
    printerdrv.use_driver(FakePrinter(), printer_id=1)
    printerdrv.use_driver_factory(lambda p: FakePrinter())
    printerdrv.use_driver(FakePrinter())

    printerdrv.use_driver(None)
    assert printerdrv.for_printer(_P(kind="moonraker", pid=1)).kind == "moonraker"
    assert printerdrv.for_printer(_P(kind="none", pid=2)).kind == "none"


def test_point_override_can_be_removed_alone():
    """Снять подмену у одной карточки, не трогая остальные."""
    printerdrv.use_driver(FakePrinter(), printer_id=1)
    other = FakePrinter()
    printerdrv.use_driver(other, printer_id=2)

    printerdrv.use_driver(None, printer_id=1)
    assert printerdrv.for_printer(_P(kind="moonraker", pid=1)).kind == "moonraker"
    assert printerdrv.for_printer(_P(pid=2)) is other


def test_fake_printers_fixture_gives_two_independent_cards(fake_printers):
    """Общая фикстура двух карточек: у каждой свой симулятор и своя память команд."""
    assert printerdrv.for_printer(_P(pid=1)) is fake_printers[1]
    assert printerdrv.for_printer(_P(pid=2)) is fake_printers[2]

    fake_printers[1].begin("first.gcode", minutes=10)
    live = [printerdrv.for_printer(_P(pid=i)) for i in (1, 2)]
    assert live[0].job_state(live[0].status()["state"])["active"] is True
    assert live[1].job_state(live[1].status()["state"])["active"] is False


@pytest.mark.asyncio
async def test_printer_card_keeps_protocol_fields(tmp_db):
    """Поля протокола переживают запись/чтение: без них второй принтер описать нечем."""
    pid = await tmp_db.add_printer("K1 Max", "m.json", "proc", "fil")
    await tmp_db.update_printer(pid, kind="moonraker", address="http://10.0.0.9:7125",
                                access_code="key", serial="SN1")
    p = await tmp_db.get_printer(pid)
    assert (p.kind, p.address, p.access_code, p.serial) == (
        "moonraker", "http://10.0.0.9:7125", "key", "SN1")
    assert printerdrv.for_printer(p).kind == "moonraker"


@pytest.mark.asyncio
async def test_existing_printer_has_empty_kind(tmp_db):
    """Миграция не переписывает боевой принтер: kind пуст → поведение как раньше."""
    p = (await tmp_db.list_printers())[0]
    assert p.kind == ""
