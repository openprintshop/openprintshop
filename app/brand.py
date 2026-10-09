"""Брендинг мастерской — white-label (A3 из competitor-feature-gap-2026-07.md).

Смысл: покупатель софта должен уметь заменить наше имя, знак и цвет своими, не трогая
код. Всё, что видит КЛИЕНТ (мини-апп, шапка, карточка профиля, заголовок вкладки, тексты
бота), берётся отсюда; реквизиты в PDF-смете жили в настройках и раньше (business_name).

Пустая настройка означает «оставить как у нас», а не «пусто»: мастерская заполняет только
то, что реально хочет заменить, и частичный брендинг (своё имя, наш знак) — валидный режим.

Логотип лежит в data/brand/logo.<ext> — один файл, старый удаляется при заливке нового.
В БД его нет специально: бинарь в SQLite мешает бэкапам, а data/ и так копируется целиком.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

# Наш собственный бренд = дефолты. Меняются они только вместе с айдентикой продукта.
DEFAULT_TITLE = "OpenPrintShop"
# «Наши» имена: имя открытой версии и прежнее имя продукта (с ним живёт мастерская, где
# проект вырос). Для них мини-апп и админка рисуют фирменный вордмарк с точками-акцентами
# (OPEN·PRINT·SHOP, PRINT·STUDIO); чужое имя мастерской показывается как есть.
STOCK_TITLES = (DEFAULT_TITLE, "Print Studio")
DEFAULT_TAGLINE = "расчёт 3D-печати"
DEFAULT_ACCENT = "#ff7a1a"

LOGO_DIR = config.DATA_DIR / "brand"
LOGO_EXTS = (".svg", ".png", ".webp", ".jpg")
MAX_LOGO_BYTES = 512 * 1024        # логотип в шапке — 25 px, полмегабайта хватает с запасом

MAX_TITLE = 40                     # шапка мини-аппа узкая: длиннее просто не влезет
MAX_TAGLINE = 60

# Цвет уезжает ПРЯМО в CSS-переменную, поэтому формат проверяем жёстко: любая другая
# строка — это инъекция в стиль ('red;} body{display:none}' и подобное).
_ACCENT_RE = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$")


def clean_accent(raw: str) -> str:
    """Нормализовать акцентный цвет. Возвращает '' на всём, что не #rgb / #rrggbb."""
    v = (raw or "").strip().lower()
    if not v:
        return ""
    if not v.startswith("#"):
        v = "#" + v
    return v if _ACCENT_RE.match(v) else ""


def logo_file() -> Path | None:
    """Путь к залитому логотипу или None. Расширение неизвестно заранее — перебираем."""
    for ext in LOGO_EXTS:
        p = LOGO_DIR / f"logo{ext}"
        if p.is_file():
            return p
    return None


def clear_logo() -> None:
    for ext in LOGO_EXTS:
        p = LOGO_DIR / f"logo{ext}"
        if p.is_file():
            p.unlink()


def save_logo(data: bytes, filename: str) -> Path:
    """Сохранить логотип, заменив прежний. ValueError — на формате и размере."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in LOGO_EXTS:
        raise ValueError("Поддерживаются SVG, PNG, WEBP и JPG")
    if not data:
        raise ValueError("Пустой файл")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError(f"Файл больше {MAX_LOGO_BYTES // 1024} КБ")
    clear_logo()                                   # ровно один логотип, без «хвостов» форматов
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    p = LOGO_DIR / f"logo{ext}"
    p.write_bytes(data)
    return p


def wordmark(title: str) -> list[str]:
    """Части вордмарка: слова названия, а одно слово в CamelCase — по заглавным буквам
    (OpenPrintShop → OPEN·PRINT·SHOP). Точки между частями рисует фронт."""
    words = title.split()
    if len(words) == 1:
        words = re.findall(r"[A-ZА-ЯЁ][a-zа-яё0-9]*|[a-zа-яё0-9]+", title) or words
    return [w.upper() for w in words if w]


def resolve(ts: dict) -> dict:
    """Текстовые настройки → готовый бренд для фронта.

    `stock` = имя наше (STOCK_TITLES): по нему мини-апп решает, рисовать ли фирменный
    вордмарк с точками-акцентами (части — в `wordmark`) или просто имя мастерской.
    В `logo` подмешан mtime — иначе браузер держал бы старый файл после замены логотипа.
    """
    title = ((ts or {}).get("brand_title") or "").strip()[:MAX_TITLE] or DEFAULT_TITLE
    tagline = ((ts or {}).get("brand_tagline") or "").strip()[:MAX_TAGLINE] or DEFAULT_TAGLINE
    accent = clean_accent((ts or {}).get("brand_accent", "")) or DEFAULT_ACCENT
    lf = logo_file()
    logo = ""
    if lf is not None:
        try:
            logo = f"/brand/logo?v={int(lf.stat().st_mtime)}"
        except OSError:                            # файл увели между проверкой и stat
            logo = "/brand/logo"
    return {"title": title, "tagline": tagline, "accent": accent, "logo": logo,
            "stock": title in STOCK_TITLES, "wordmark": wordmark(title)}


def page_title(b: dict) -> str:
    return f"{b['title']} — {b['tagline']}"


def to_script_json(b: dict) -> str:
    """JSON для вставки внутрь <script>. `</` экранируем: строка вида '</script>' в имени
    мастерской иначе закрыла бы тег и уронила загрузку мини-аппа."""
    return json.dumps(b, ensure_ascii=False).replace("</", "<\\/")
