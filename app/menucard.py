"""Карточка приветствия бота (/start): картинка с именем мастерской.

Раньше это был один статичный assets/menu_card.png с нашим именем — у всех студий одинаковый.
Теперь картинку рисует сам бот: имя берётся из бренда мастерской (white-label, app/brand.py),
а в подсказке перечислены площадки, ссылки с которых реально скачиваются в этой сборке
(MakerWorld — только при личном модуле app/makerworld.py, в открытой версии его нет).

Готовый PNG кэшируется в DATA_DIR/brand под ключом (имя, площадки, версия рисунка): рисуется
один раз на смену бренда. Не вышло нарисовать (нет шрифта, битый Pillow) — бот отправляет
запасной статичный assets/menu_card.png, меню без картинки не остаётся.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import brand, config, db, downloader

log = logging.getLogger(__name__)

ASSETS = config.BASE_DIR / "assets"
FALLBACK = ASSETS / "menu_card.png"
ONEST = ASSETS / "fonts" / "Onest-var.ttf"
RENDER_VERSION = 2            # поднять при любой правке рисунка: старые картинки не подойдут

W = 1080                      # ширина карточки (2x для чёткости в Telegram)
PAD = 72
CW = W - 2 * PAD              # рабочая ширина контента

# ---- палитра ----
ORANGE = (255, 122, 26)       # #ff7a1a — фирменный акцент
HEAD = (244, 246, 248)        # заголовок (почти белый)
BODY = (174, 185, 196)        # основной текст (мягкий светло-серый)
MUTED = (107, 118, 130)       # подсказка снизу
WHITE = (255, 255, 255)


def _font(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(ONEST), size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:  # noqa: BLE001 — нет оси веса: остаётся обычное начертание
        pass
    return f


def _wrap_runs(draw, runs, max_w):
    """runs = [(text, color, font)]. Возвращает строки как список слов-токенов
    [(word, color, font, width)], перенос по словам в пределах max_w."""
    tokens = []
    for text, color, fnt in runs:
        parts = text.split(" ")
        for i, p in enumerate(parts):
            if i > 0:
                tokens.append((" ", color, fnt))
            if p:
                tokens.append((p, color, fnt))
    lines, cur, cur_w = [], [], 0.0
    for word, color, fnt in tokens:
        w = draw.textlength(word, font=fnt)
        if word == " " and not cur:
            continue
        if cur_w + w > max_w and cur:
            while cur and cur[-1][0] == " ":
                cur.pop()
            lines.append(cur)
            cur, cur_w = [], 0.0
            if word == " ":
                continue
        cur.append((word, color, fnt, w))
        cur_w += w
    if cur:
        while cur and cur[-1][0] == " ":
            cur.pop()
        lines.append(cur)
    return lines


def _draw_line(draw, x, y, line):
    for word, color, fnt, w in line:
        draw.text((x, y), word, font=fnt, fill=color)
        x += w


def sources_text(makerworld: bool) -> str:
    """Площадки в подсказке: только те, ссылки с которых в этой сборке скачиваются."""
    return "MakerWorld, Printables" if makerworld else "Printables, Thingiverse"


def render(title: str, sources: str, out: Path) -> Path:
    """Нарисовать карточку с именем мастерской и площадками в подсказке; вернуть путь."""
    f_head, f_body, f_body_hl = _font(56, 700), _font(34, 400), _font(34, 600)
    f_step, f_badge, f_hint = _font(33, 400), _font(30, 700), _font(29, 400)

    # фон = угловой цвет логотипа -> вставка логотипа сольётся бесшовно
    logo = Image.open(ASSETS / "avatar.png").convert("RGB")
    bg = logo.getpixel((4, 4))
    canvas = Image.new("RGB", (W, 1500), bg)
    d = ImageDraw.Draw(canvas)

    # ---- шапка: логотип + имя мастерской (длинное — ужимаем, чтобы влезло в строку) ----
    logo_px = 104
    canvas.paste(logo.resize((logo_px, logo_px), Image.LANCZOS), (PAD, PAD))
    head_x = PAD + logo_px + 26
    size = 56
    while size > 30 and d.textlength(title, font=f_head) > W - PAD - head_x:
        size -= 4
        f_head = _font(size, 700)
    y = PAD
    d.text((head_x, y + logo_px // 2), title, font=f_head, fill=HEAD, anchor="lm")
    y += logo_px + 46

    # ---- абзац с подсветкой ----
    runs = [
        ("Пришлите ", BODY, f_body),
        ("ссылку", ORANGE, f_body_hl),
        (f" ({sources}) или ", BODY, f_body),
        ("файл", ORANGE, f_body_hl),
        (" модели — посчитаю всё за пару минут:", BODY, f_body),
    ]
    for line in _wrap_runs(d, runs, CW):
        _draw_line(d, PAD, y, line)
        y += 48
    y += 40

    # ---- 4 шага с бейджами ----
    steps = [
        "скачаю модель и покажу превью",
        "нарежу под нужное качество и прочность",
        "посчитаю расход, время и цену",
        "пришлю готовую PDF-смету",
    ]
    badge = 48
    for i, s in enumerate(steps, 1):
        d.rounded_rectangle([PAD, y, PAD + badge, y + badge], radius=14, fill=ORANGE)
        d.text((PAD + badge / 2, y + badge / 2 + 1), str(i), font=f_badge, fill=WHITE, anchor="mm")
        d.text((PAD + badge + 24, y + badge / 2), s, font=f_step, fill=BODY, anchor="lm")
        y += badge + 20
    y += 30

    # ---- подсказка ----
    d.text((PAD, y), "Нажмите кнопку, чтобы начать ↓", font=f_hint, fill=MUTED, anchor="lm")
    y += 30 + PAD

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.png")     # .png в имени: Pillow выбирает формат по нему
    canvas.crop((0, 0, W, y)).save(tmp)
    tmp.replace(out)
    return out


async def card_path() -> Path:
    """Картинка для /start под текущий бренд; при сбое рисования — запасной статичный файл."""
    title = brand.resolve(await db.get_text_settings())["title"]
    sources = sources_text(downloader.makerworld_available())
    key = hashlib.sha1(f"{RENDER_VERSION}|{title}|{sources}".encode()).hexdigest()[:12]
    out = brand.LOGO_DIR / f"menu_card_{key}.png"
    if out.exists():
        return out
    try:
        await asyncio.to_thread(render, title, sources, out)
    except Exception:  # noqa: BLE001 — без картинки меню хуже, чем с запасной
        log.warning("menu card: не удалось нарисовать, отправляю запасную", exc_info=True)
        return FALLBACK
    for old in brand.LOGO_DIR.glob("menu_card_*.png"):      # прежние бренды больше не нужны
        if old != out:
            old.unlink(missing_ok=True)
    return out
