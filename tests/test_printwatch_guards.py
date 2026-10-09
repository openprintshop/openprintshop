"""Гарды printwatch, читавшие СИНТЕТИЧЕСКИЙ ключ из сырого состояния (разбор 11.08).

`print_stats` присылает не принтер, а наш же `elegoo.enrich_status`: прошивка Centauri отдаёт
только `print_status.*`, и в записях протокола (`docs/captures/`) слово `print_stats` не
встречается ни разу. Три места в printwatch читали его напрямую из сырого снимка подписчика
и потому ВСЕГДА получали «не печатает»:

  • сторож связи — алерт «принтер пропал со связи во время печати» не мог сработать никогда;
  • свип таймлапсов — гард «пока печатает, по сети не трогаем» не включался, и свип каждые
    10 минут тянул с принтера файл ~5 МБ по тому же серверу, что отдаёт MJPEG-камеру. Ровно
    та регрессия, ради которой гард написан (кейс 22.07: камера падала на печати осьминога);
  • ожидание сборки таймлапса — выход «печать пошла, перестаём дёргать» не срабатывал.

Здесь проверяется, что признак печати берётся ЧЕРЕЗ enrich_status, и что оба следствия
(алерт и гард) действительно живые.
"""
from pathlib import Path

from app import elegoo, printwatch

# Снимок печатающего CC2 в том виде, в каком его отдаёт прошивка: print_status.*, БЕЗ
# print_stats. Значения — из docs/captures (там все кадры простоя, поэтому остаток дописан).
PRINTING = {"print_status": {"print_duration": 600, "remaining_time_sec": 1200,
                             "current_layer": 42, "filename": "octopus.gcode"}}
IDLE = {"print_status": {"print_duration": 2991, "remaining_time_sec": 0, "filename": ""}}


def test_printer_never_sends_print_stats():
    """Опора всего файла: ключ синтетический. Если прошивка начнёт его слать — тест упадёт,
    и это будет поводом перечитать, а не молча поверить."""
    assert "print_stats" not in PRINTING
    assert "print_stats" in elegoo.enrich_status(PRINTING)
    assert elegoo.enrich_status(IDLE).get("print_stats") is None


def test_is_printing_reads_through_enrich():
    """Главный регресс: сырое состояние печати обязано опознаваться как печать."""
    assert printwatch._is_printing({"ok": True, "state": PRINTING}) is True
    assert printwatch._is_printing({"ok": True, "state": IDLE}) is False
    assert printwatch._is_printing({"ok": False, "state": PRINTING}) is False
    assert printwatch._is_printing(None) is False


def test_printwatch_does_not_parse_status_itself():
    """printwatch не смеет разбирать телеметрию руками — вопрос идёт драйверу.

    Правило ужесточилось волной 2: раньше допускалось одно чтение из overlay, теперь их ноль —
    признак печати живёт в PrinterDriver.job_state, где собраны ВСЕ формы (достроенный ключ
    Elegoo, родное поле Klipper, форма симулятора). Тест текстовый сознательно: исходный дефект
    был именно в том, что три копии одного выражения разъехались с источником данных, и поймать
    это можно только запретом на само выражение."""
    src = Path(printwatch.__file__).read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines()
            if "print_stats" in ln and not ln.lstrip().startswith("#")]
    reads = [ln for ln in code if ".get(\"print_stats\")" in ln or "['print_stats']" in ln]
    assert reads == [], f"printwatch снова разбирает статус сам: {reads}"
    assert "job_state" in src, "вопрос «печатает ли» больше не задаётся драйверу"


async def test_offline_alert_fires_after_printing(monkeypatch):
    """Сторож связи: увидели печать → связь пропала → алерт ушёл.

    До правки _was_printing не становился True никогда, и единственный операционный алерт
    проекта молчал при любом обрыве."""
    sent: list[tuple[str, str]] = []

    async def _alert(text, *, key=None, throttle_sec=300, audience=None):
        sent.append((text, key))

    monkeypatch.setattr(printwatch.notify, "alert", _alert)
    printwatch._was_printing.clear()

    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": PRINTING})
    await printwatch._tick_offline(None)
    assert sent == [] and printwatch._was_printing.get(0) is True   # печать идёт — тревоги нет

    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)
    await printwatch._tick_offline(None)
    # Ключ троттлинга содержит машину: общий ключ проглотил бы тревогу второго принтера,
    # если первый отвалился в те же пять минут.
    assert len(sent) == 1 and sent[0][1].startswith("printer_offline")
    assert printwatch._was_printing.get(0) is False                 # повторно не звеним


async def test_offline_alert_silent_when_printer_was_idle(monkeypatch):
    """Принтер просто выключили в простое — это не повод будить мастерскую."""
    sent: list = []

    async def _alert(text, *, key=None, throttle_sec=300, audience=None):
        sent.append(text)

    monkeypatch.setattr(printwatch.notify, "alert", _alert)
    printwatch._was_printing.clear()
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": IDLE})
    await printwatch._tick_offline(None)
    monkeypatch.setattr(printwatch.elegoo, "cached_status", lambda *a, **k: None)
    await printwatch._tick_offline(None)
    assert sent == []


async def test_timelapse_sweep_keeps_off_the_printer_while_printing(monkeypatch, tmp_db):
    """Гард свипа: пока принтер печатает, по сети к нему не ходим.

    Проверяем именно то, ради чего гард написан: history() принтера НЕ вызывается. До правки
    признак печати был всегда False, и свип лез к принтеру каждые 10 минут прямо на печати,
    конкурируя с потоком камеры."""
    called: list[str] = []

    class _Drv:
        kind = "elegoo"
        caps = type("C", (), {"history": True, "timelapse": True})()

        def enabled(self):
            return True

        def history(self, timeout=8.0):
            called.append("history")
            return {"ok": True, "jobs": []}

    async def _drv():
        return _Drv()

    monkeypatch.setattr(printwatch, "_driver", _drv)
    printwatch._tl_sweep_at.clear()
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": PRINTING})

    await printwatch._tick_timelapse_sweep()
    assert called == [], "свип пошёл к принтеру во время печати — гард снова мёртв"
