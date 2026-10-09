"""Версия фронта мини-апки + инжект в index.html (авто-сброс кеша Telegram WebView)."""
import re
from pathlib import Path

from app import config

_IDX = Path(config.__file__).parent / "webui" / "index.html"


def test_webui_version_stable_hex():
    v1 = config.webui_version()
    v2 = config.webui_version()
    assert v1 == v2                                  # стабильна между вызовами (мемо 1 с)
    assert re.fullmatch(r"[0-9a-f]{8}", v1) or v1 == "dev"


def test_index_html_has_placeholder_and_check_script():
    idx = _IDX.read_text(encoding="utf-8")
    assert "%%VER%%" in idx                          # сервер подставит сюда версию
    assert "window.__APPVER__" in idx and "/api/version" in idx


def test_injection_replaces_placeholder():
    """Как _ui_file: подставляем версию — плейсхолдер исчезает, версия вшита, скрипт-проверка цел."""
    injected = _IDX.read_text(encoding="utf-8").replace("%%VER%%", config.webui_version())
    assert "%%VER%%" not in injected                 # все плейсхолдеры заменены
    assert f'window.__APPVER__ = "{config.webui_version()}"' in injected
    # защитный guard «версия не вшита» НЕ должен пострадать от замены (склейка '%%'+'VER%%')
    assert "'%%' + 'VER%%'" in injected
