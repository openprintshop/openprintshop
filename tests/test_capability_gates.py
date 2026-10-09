"""Цикл 3, волна 3 (серверная половина): способность обязана означать «умею СЕЙЧАС».

Способности заводились против UX «выясняем нажатием»: интерфейс прячет кнопку, которой у
принтера нет, вместо того чтобы показать её и получить отказ. Но сами гейты местами врали:

  • удаление файла гейтилось флагом `files` — способностью ПОКАЗАТЬ список. Принтер, умеющий
    показать и не умеющий удалить, кнопку получал;
  • статус проверял только `enabled()`, поэтому «не умею телеметрию» отрисовывалось как
    «нет связи» — то есть как поломка, и мастер искал неисправность там, где её нет;
  • расширение файла было ЖЁСТКО зашито в ручке заливки (`.gcode`), хотя это свойство
    протокола: Bambu ест `.3mf`.
"""
from app import printerdrv, webapp
from app.printerdrv import Caps
from app.web import printerhub, printerjob


class _Req:
    """Минимальный GET-запрос админки."""

    def __init__(self, **query):
        self.query = {"initData": "x", **query}


async def test_status_says_it_cannot_instead_of_looking_broken(monkeypatch):
    """Принтер без телеметрии обязан сказать это словами, а не выглядеть сломанным."""
    poor = printerdrv.FakePrinter(caps=Caps(upload=True, start=True))
    # Подмена во ВСЕ модули: ручка живёт в app/web/printerjob.py (волна 11), драйвер — в
    # printerhub, webapp лишь реэкспортирует фасадом; printerjob связал _printer_driver и
    # _verify_admin по значению на импорте.
    monkeypatch.setattr(webapp, "_verify_admin", lambda s: 1)
    monkeypatch.setattr(printerjob, "_verify_admin", lambda s: 1)
    monkeypatch.setattr(webapp, "_printer_driver", _drv(poor))
    monkeypatch.setattr(printerhub, "_printer_driver", _drv(poor))
    monkeypatch.setattr(printerjob, "_printer_driver", _drv(poor))

    import json
    j = json.loads((await webapp._api_admin_printer_status(_Req())).body.decode())
    assert j["ok"] is False
    assert "телеметри" in j["error"].lower(), f"ответ звучит как поломка: {j['error']}"
    assert j["driver"]["caps"]["status"] is False, "интерфейсу нужен повод спрятать блок"


def _drv(d):
    async def _f(*a, **k):
        return d
    return _f


async def test_delete_has_its_own_capability():
    """Показать список и удалить из него — разные умения."""
    reader = printerdrv.FakePrinter(caps=Caps(status=True, files=True))   # видит, но не удаляет
    assert reader.files()["ok"] is True
    assert reader.delete_file("x.gcode")["ok"] is False

    full = printerdrv.FakePrinter()
    assert full.caps.delete is True
    assert full.delete_file("x.gcode")["ok"] is True


def test_elegoo_declares_delete():
    """Правило волны: каждый новый флаг в том же коммите объявляется у Elegoo как True —
    иначе у мастера пропадёт рабочая кнопка."""
    from app.printerdrv import elegoo_drv
    assert elegoo_drv.ElegooDriver.caps.delete is True


def test_upload_extensions_come_from_the_driver():
    """Расширения — свойство протокола, а не общее правило."""
    assert printerdrv.PrinterDriver().upload_exts == (".gcode",)
    assert ".gcode" in printerdrv.FakePrinter().describe()["upload_exts"]


def test_describe_carries_extensions():
    """Интерфейсу нужно знать, что можно заливать, до попытки заливки."""
    d = printerdrv.FakePrinter().describe()
    assert isinstance(d["upload_exts"], list) and d["upload_exts"]
    assert "delete" in d["caps"], "новая способность не доехала до интерфейса"
