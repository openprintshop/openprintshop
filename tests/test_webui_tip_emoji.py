"""Иконка-переключатель подсказок в оверлее нарезки: шутка = 😂 (ржущий до слёз), не 🤡 клоун."""
from pathlib import Path

from app import config

_CORE = Path(config.__file__).parent / "webui" / "screens_core.js"


def test_joke_icon_is_laughing_not_clown():
    src = _CORE.read_text(encoding="utf-8")
    assert "🤡" not in src                              # клоун убран отовсюду (рендер + комменты)
    # рендер иконки-переключателя: режим 'joke' -> 😂, иначе 💡 (лампочка факта)
    assert "isJoke ? '😂' : '💡'" in src
