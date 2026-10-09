"""Тексты бота называют только те площадки, ссылки с которых в этой сборке скачиваются."""
import importlib
import importlib.util

from app import texts


def test_without_makerworld_module_texts_do_not_promise_it(monkeypatch):
    real = importlib.util.find_spec

    def no_mw(name, *a, **k):
        return None if name == "app.makerworld" else real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", no_mw)
    try:
        t = importlib.reload(texts)
        for text in (t.menu(""), t.HELP, t.MODEL_PROMPT, t.MODEL_BAD_INPUT, t.SEARCH_PROMPT):
            assert "MakerWorld" not in text
        assert "Printables" in t.MODEL_PROMPT and "Thingiverse" in t.MODEL_PROMPT
    finally:
        monkeypatch.undo()
        importlib.reload(texts)                    # вернуть модуль к виду этой сборки
