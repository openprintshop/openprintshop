"""Необязательные модули сборки: в открытой версии проекта (OpenPrintShop) их файлов нет.

Личные интеграции владельца — автоскачивание с MakerWorld (пул аккаунтов, Bambu API, браузер) —
живут в отдельных модулях пакета `app`. Код, который ими пользуется, берёт их через load(): есть
файл — работает как раньше, нет файла — возможность честно выключена, а не падает на импорте.
"""
from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType


def load(name: str) -> ModuleType | None:
    """Модуль `app.<name>` или None, если его нет в этой сборке.

    Ошибку ВНУТРИ существующего модуля не глотаем: молча выключенная возможность на машине
    владельца хуже упавшего старта — поломку надо видеть сразу."""
    full = f"{__package__}.{name}"
    if importlib.util.find_spec(full) is None:
        return None
    return importlib.import_module(full)
