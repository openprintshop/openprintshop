r"""Перерисовать ЗАПАСНУЮ карточку приветствия -> assets/menu_card.png.

Основную картинку бот рисует сам под бренд мастерской (app/menucard.py) и кэширует в
DATA_DIR/brand. Этот файл — запасной: его бот отправляет, если нарисовать не вышло. Рисунок
общий, поэтому правки делаются в app/menucard.py, а здесь — только запуск:

    .venv\Scripts\python.exe tools\build_menu_card.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import brand, menucard  # noqa: E402


def main() -> None:
    out = menucard.render(brand.DEFAULT_TITLE, menucard.sources_text(False), menucard.FALLBACK)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
