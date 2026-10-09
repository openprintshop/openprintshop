"""Цикл 3, волна 1: единый контракт «печатает ли и как далеко» + правдивый симулятор.

Почему эта волна первая. До неё ни одну мультипринтерную правку нечем было принять:

  • метод `enrich` потреблялся ДВУМЯ способами — экран мержил overlay поверх сырого состояния,
    а автоматика читала один только возврат. У Klipper прогресс лежит в сыром состоянии, и
    «читатель возврата» видел вечный простой;
  • симулятор отдавал СВОЮ форму (плоскую сводку и ключ `items` вместо `jobs`), которой нет ни
    у одного настоящего драйвера, и потому проверял себя, а не продукт;
  • его печать не распознавалась активной вообще: он не заполнял `print_duration` и
    `remaining_time_sec` — те самые поля, по которым `elegoo.enrich_status` решает, идёт ли
    печать.

Здесь проверяется, что вопрос задаётся одним методом `job_state` и что он одинаково честно
отвечает на трёх РАЗНЫХ формах телеметрии, которые реально приходят от трёх драйверов.
"""
import json
import re
from pathlib import Path

import pytest

from app import elegoo
from app.printerdrv import Caps, base, fake

CAPTURES = Path(__file__).resolve().parent.parent / "docs" / "captures"


# ---------- 1. один вопрос — три формы телеметрии ----------

class _Elegoo(base.PrinterDriver):
    """Форма CC2: overlay достраивает enrich_status из остатка времени."""

    def enrich(self, state: dict) -> dict:
        return elegoo.enrich_status(state)


class _Moonraker(base.PrinterDriver):
    """Форма Klipper в нашем приведении: состояние в print_status.status, прогресс — в сыром."""

    def enrich(self, state: dict) -> dict:
        return elegoo.enrich_status(state)


ELEGOO_PRINTING = {"print_status": {"print_duration": 600, "remaining_time_sec": 1200,
                                    "current_layer": 42, "filename": "octopus.gcode"}}
ELEGOO_IDLE = {"print_status": {"print_duration": 2991, "remaining_time_sec": 0, "filename": ""}}
KLIPPER_PRINTING = {"print_status": {"status": "printing"},
                    "virtual_sdcard": {"progress": 0.33}}
KLIPPER_IDLE = {"print_status": {"status": "standby"}, "virtual_sdcard": {"progress": 0.0}}


def test_job_state_reads_elegoo_shape():
    js = _Elegoo().job_state(ELEGOO_PRINTING)
    assert js["active"] is True and js["name"] == "octopus.gcode"
    assert js["progress"] == pytest.approx(600 / 1800, abs=1e-4)
    assert js["minutes"] == 10.0
    assert _Elegoo().job_state(ELEGOO_IDLE)["active"] is False


def test_job_state_reads_klipper_shape():
    """Главная дыра, которую закрывает волна: у Klipper признак печати живёт в СЫРОМ состоянии,
    а overlay пуст. Читатель одного только overlay видел вечный простой."""
    assert elegoo.enrich_status(KLIPPER_PRINTING) == {}, "overlay для этой формы пуст — так и есть"
    js = _Moonraker().job_state(KLIPPER_PRINTING)
    assert js["active"] is True and js["progress"] == 0.33
    assert _Moonraker().job_state(KLIPPER_IDLE)["active"] is False


def test_job_state_survives_garbage_from_firmware():
    """Телеметрия приходит от чужой прошивки: под ключом может оказаться что угодно."""
    d = base.PrinterDriver()
    for junk in ({}, {"print_status": None}, {"print_status": "printing"},
                 {"print_stats": []}, {"virtual_sdcard": "нет"}, {"print_status": {"status": None}}):
        js = d.job_state(junk)
        assert js["active"] is False and js["progress"] is None and js["name"] == ""


def test_job_state_ignores_broken_enrich():
    """Сломанный overlay не должен ронять вопрос «печатает ли» — это гард ручного управления."""
    class _Broken(base.PrinterDriver):
        def enrich(self, state: dict) -> dict:
            raise RuntimeError("прошивка отдала мусор")

    assert _Broken().job_state(KLIPPER_PRINTING)["active"] is True


@pytest.mark.parametrize("word", ["printing", "PRINTING", "Printing (paused)"])
def test_job_state_accepts_vendor_spellings(word):
    assert base.PrinterDriver().job_state({"print_status": {"status": word}})["active"] is True


# ---------- 2. симулятор говорит на языке прошивки ----------

def test_simulator_speaks_firmware_shape():
    """Состояние симулятора обязано разбираться ТЕМ ЖЕ кодом, что и настоящее."""
    p = fake.FakePrinter()
    p.begin("plate.gcode", minutes=60, grams=20, layers=300)
    st = p.status()["state"]
    assert "print_duration" in st["print_status"] and "remaining_time_sec" in st["print_status"]
    ov = elegoo.enrich_status(st)
    assert ov["print_stats"]["state"] == "printing"
    assert ov["print_stats"]["filename"] == "plate.gcode"


@pytest.mark.parametrize("phase, setter, busy", [("paused", "pause", True),
                                                 ("heating", "heating", True),
                                                 ("error", "error", False)])
def test_simulator_phases(phase, setter, busy):
    """Пауза, прогрев и ошибка — половина сценариев автоматики, и раньше они были невыразимы:
    симулятор знал только «печатает / не печатает».

    Пауза и прогрев СЧИТАЮТСЯ занятостью: принтер так же слаб по сети, задание не закончено, и
    финиш объявлять нельзя. Ошибка — не занятость: задание встало, для журнала это финиш.
    Время в любой из фаз не идёт."""
    p = fake.FakePrinter()
    p.begin("plate.gcode", minutes=60)
    p.advance(10)
    getattr(p, setter)()
    assert p.status()["state"]["print_status"]["status"] == phase
    p.advance(30)
    js = p.job_state(p.status()["state"])
    assert js["minutes"] == 10.0, "время шло на паузе/прогреве/ошибке"
    assert js["phase"] == phase
    assert js["active"] is busy


def test_simulator_resume_continues_the_print():
    p = fake.FakePrinter()
    p.begin("plate.gcode", minutes=60)
    p.advance(10)
    p.pause()
    p.advance(30)
    p.resume()
    p.advance(20)
    assert p.job_state(p.status()["state"])["minutes"] == 30.0
    assert p.job_state(p.status()["state"])["active"] is True


def test_simulator_history_speaks_jobs():
    """Ключ 'jobs' и поля финиша — их читает свип таймлапсов. На ключе 'items' весь этот путь
    молча получал пустой список, то есть проверялся как «ничего не нашлось»."""
    p = fake.FakePrinter()
    p.begin("plate.gcode", minutes=5)
    p.advance(5)
    p.finish()
    h = p.history()
    assert "items" not in h and h["jobs"], "история отдаётся не под тем ключом"
    rec = h["jobs"][0]
    assert rec["name"] == "plate.gcode" and rec["end_time"] > 0
    assert rec["time_lapse_video_url"].endswith(".mp4") and rec["time_lapse_video_status"] == 1


def test_failed_print_has_no_timelapse():
    p = fake.FakePrinter()
    p.begin("plate.gcode", minutes=5)
    p.advance(2)
    p.finish(ok=False)
    rec = p.history()["jobs"][0]
    assert rec["ok"] is False and rec["time_lapse_video_status"] == 0
    assert rec["time_lapse_video_url"] == ""


def test_poor_printer_still_answers_the_question():
    """У принтера без телеметрии вопрос «печатает ли» обязан отвечаться «нет», а не падать."""
    p = fake.FakePrinter(caps=Caps(upload=True, start=True))
    assert p.status() is None
    assert p.job_state({})["active"] is False


# ---------- 3. реплей настоящих записей протокола ----------

_FRAME = re.compile(r"^\[\d\d:\d\d:\d\d\]\s+(\S+)")


def _replay(path: Path):
    """Разобрать захват `docs/captures/*.log` в пары (топик, payload).

    Формат: строка «[чч:мм:сс] топик method=… id=…», следом отступом — JSON одной или
    несколькими строками до следующего кадра."""
    topic, buf = None, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _FRAME.match(line)
        if m:
            if topic and buf:
                try:
                    yield topic, json.loads("\n".join(buf))
                except json.JSONDecodeError:
                    pass
            topic, buf = m.group(1), []
        elif topic and line.startswith(("    ", "\t")):
            buf.append(line.strip())
    if topic and buf:
        try:
            yield topic, json.loads("\n".join(buf))
        except json.JSONDecodeError:
            pass


def _status_states(path: Path):
    """Состояния принтера из захвата.

    Обмен идёт JSON-RPC поверх MQTT, и состояние — это `result` ответа на метод 1002 (форма с
    ключами extruder / heater_bed / fans / led / machine_status / canvas_info, ровно та, что
    разбирают summarize и extras). Вещания приходят тем же телом без обёртки."""
    for _topic, payload in _replay(path):
        if not isinstance(payload, dict):
            continue
        res = payload.get("result")
        if isinstance(res, dict) and ({"extruder", "machine_status", "print_status", "canvas_info"}
                                      & set(res)):
            yield res


@pytest.mark.parametrize("name, least", [("mqtt-capture-2026-07-12-full.log", 2),
                                         ("mqtt-register-binding-2026-07-20.log", 16)])
def test_replay_of_real_captures_does_not_crash(name, least):
    """Разбор настоящих кадров прошивки — на реальных данных, а не на рукописном срезе.

    ЧЕСТНАЯ ОГОВОРКА: во всех имеющихся захватах принтер в ПРОСТОЕ (remaining_time_sec = 0,
    пустое имя файла) — они сняты вне печати. Поэтому реплей покрывает устойчивость разбора и
    ветку простоя, но НЕ активную ветку, от которой зависит вся автоматика мастерской. Её
    закрывает симулятор выше; запись печатающего принтера — отдельная работа у железа."""
    path = CAPTURES / name
    assert path.exists(), f"захват {name} пропал — тест устарел"
    seen = 0
    for st in _status_states(path):
        seen += 1
        elegoo.summarize(st)          # не должно бросать ни на одном настоящем кадре
        elegoo.extras(st)
        ov = elegoo.enrich_status(st)
        assert isinstance(ov, dict)
        assert ov.get("print_stats") is None, "в этих захватах печати нет — форма изменилась?"
    assert seen >= least, f"в {name} ожидалось ≥{least} кадров, нашлось {seen} — парсер разошёлся"


def test_replay_covers_slots_and_temperatures():
    """Реплей не просто «не падает»: на настоящих кадрах разбор обязан что-то ДОСТАВАТЬ.

    Иначе тест зелен и при парсере, который молча отдаёт пустые словари."""
    got_temp = got_slots = False
    for st in _status_states(CAPTURES / "mqtt-capture-2026-07-12-full.log"):
        s = elegoo.summarize(st)
        if isinstance(s.get("extruder_temp"), (int, float)):
            got_temp = True
        if (elegoo.extras(st).get("slots") or []):
            got_slots = True
    assert got_temp, "температуры не извлеклись ни из одного настоящего кадра"
    assert got_slots, "слоты CANVAS не извлеклись — разбор разошёлся с прошивкой"
