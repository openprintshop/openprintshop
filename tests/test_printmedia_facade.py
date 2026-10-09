"""Фасад webapp → printmedia: каждое перенесённое имя обязано быть ТЕМ ЖЕ объектом.

Волна 3 цикла 2 вынесла пласт медиа таймлапсов в app/printmedia.py, а в app/webapp.py оставила
явный реэкспорт. Этот файл — единственная страховка на сам механизм фасада, и ловит он два
разных отказа, ни один из которых не виден на старте бота:

- ЗАБЫТОЕ ИМЯ. `from .printmedia import *` приватные имена не берёт, `__all__` в webapp нет,
  поэтому фасад набран построчно — и строку легко не дописать. Без этого файла пропажа
  всплывала бы в момент вызова у живого клиента (668 якорей тестов держатся за webapp._*),
  а `ruff check --fix` вдобавок помечает каждую строку фасада как fixable и снёс бы её молча.
- ВТОРАЯ КОПИЯ. Худший случай переноса: функция уехала в printmedia, но осталась и в webapp.
  Оба модуля тогда работают, тесты зелёные, а поведение расходится — например, тест патчит
  качалку в одном модуле, а живой путь идёт через другую. Именно поэтому проверка `is`,
  а не `hasattr`/`callable`.

Модульные `_bot` и `set_bot` у printmedia СВОИ и в фасад НЕ входят: реэкспорт затёр бы
одноимённые webapp'а. Это проверяется отдельно.
"""
import pytest

from app import printmedia, webapp

# Ровно то, что переехало волной 3. Список ведётся руками: он и есть контракт фасада.
MOVED = (
    "_TIMELAPSE_DIR",
    "_TG_UPLOAD_LIMIT",
    "_http_get_bytes",
    "_download_to",
    "_timelapse_has_video",
    "_timelapse_file_name",
    "_timelapse_download_url",
    "_timelapse_abs_url",
    "_timelapse_candidate_urls",
    "_download_timelapse",
    "_TIMELAPSE_AUTO_DIR",
    "_safe_job_name",
    "cached_timelapse_path",
    "cache_timelapse_from_url",
    "_heal_journal_timelapse",
    "deliver_late_timelapses",
    "_deliver_timelapses",
    "_deliver_timelapse",
    "_file_size",
)


@pytest.mark.parametrize("name", MOVED)
def test_webapp_reexports_the_very_same_object(name):
    assert hasattr(printmedia, name), f"{name} не доехал до app/printmedia.py"
    assert hasattr(webapp, name), f"{name} потерян фасадом webapp — допиши строку реэкспорта"
    assert getattr(webapp, name) is getattr(printmedia, name), \
        f"{name}: webapp и printmedia держат РАЗНЫЕ объекты — в webapp осталась вторая копия"


def test_facade_does_not_shadow_the_web_layer_bot():
    """`_bot`/`set_bot` у каждого модуля свои: реэкспорт затёр бы бота веб-слоя."""
    assert webapp.set_bot is not printmedia.set_bot
    prev = webapp.set_bot(webapp._bot)              # публичная set_bot возвращает прежнего
    assert webapp._bot is prev
    assert "_bot" not in MOVED and "set_bot" not in MOVED


def test_printmedia_does_not_import_webapp():
    """Цикл разорван, а не спрятан в отложенном импорте.

    Ради этого доставка (deliver_late_timelapses/_deliver_timelapses) уехала в printmedia
    целиком: в «минимальном» варианте переноса модуль был бы вынужден делать
    `from . import webapp` внутри функции — тот самый цикл, ради разрыва которого затевался
    разрез, только незаметный, и следующая волна решила бы, что он вылечен."""
    src = __import__("pathlib").Path(printmedia.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        assert "import webapp" not in code, f"printmedia тянет webapp: {line.strip()}"
