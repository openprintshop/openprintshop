"""Печатный шаблон дна ящика 1:1 для Gridfinity-проекта.

Самая дешёвая проверка во всей мастерской: клиент печатает контур дна на домашнем
принтере, склеивает листы, кладёт в ящик — и видит своими глазами, правильно ли снял
замеры и как лягут ячейки. Лист бумаги вместо заказа на пять тысяч.

Что на листах:
  * контур дна В НАТУРАЛЬНУЮ ВЕЛИЧИНУ, разрезанный на A4 с полем склейки;
  * сетка шага 42 мм ровно там, где её построит конфигуратор (spacerless: остаток стороны
    уходит в крайние сокеты по правилам fitX/fitY);
  * контрольная линейка 100 мм на КАЖДОМ листе — если принтер «подогнал под страницу»,
    линейка это покажет раньше, чем клиент склеит четыре листа и удивится;
  * метки склейки и подпись листа («ряд 2, столбец 1»).

Шрифты — DejaVu из assets/, как в quote.py и calibpdf.py (Helvetica не умеет кириллицу).
"""
from __future__ import annotations

import io
import math

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from . import config

MM = 72.0 / 25.4          # 1 мм в пунктах PDF
MARGIN_MM = 10.0          # непечатаемое поле домашнего принтера (с запасом)
OVERLAP_MM = 8.0          # поле склейки: соседние листы перекрываются на эту величину
HEAD_MM = 22.0            # шапка листа: название и номер листа
FOOT_MM = 14.0            # подвал: контрольная линейка и подпись про склейку

# Полезное окно листа — то, что реально занимает чертёж. Считается ОДИН раз и здесь:
# если окно и шаг разрезки посчитать по-разному, между листами пропадёт полоса ящика,
# и клиент склеит шаблон с дырой, ничего не заметив.
WIN_W_MM = 210.0 - 2 * MARGIN_MM
WIN_H_MM = 297.0 - 2 * MARGIN_MM - HEAD_MM - FOOT_MM
TILE_W_MM = WIN_W_MM - OVERLAP_MM
TILE_H_MM = WIN_H_MM - OVERLAP_MM

_FONT, _FONT_B, _FONT_M = "DejaVu", "DejaVu-Bold", "DejaVu-Mono"
_fonts_ready = False

_INK = colors.HexColor("#12181a")
_MUTED = colors.HexColor("#7d8b8e")
_GRID = colors.HexColor("#9fb0b3")
_ACCENT = colors.HexColor("#ff7a1a")


def _ensure_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    ttf = config.BASE_DIR / "assets" / "fonts"
    pdfmetrics.registerFont(TTFont(_FONT, str(ttf / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_B, str(ttf / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_M, str(ttf / "DejaVuSansMono.ttf")))
    _fonts_ready = True


def _ruler(c: canvas.Canvas, x_mm: float, y_mm: float) -> None:
    """Контрольная линейка 100 мм: единственный способ поймать «подгонку под страницу»."""
    c.setStrokeColor(_ACCENT)
    c.setLineWidth(0.8)
    c.line(x_mm * MM, y_mm * MM, (x_mm + 100) * MM, y_mm * MM)
    for i in range(0, 101, 10):
        h = 3.0 if i % 50 == 0 else 1.8
        c.line((x_mm + i) * MM, y_mm * MM, (x_mm + i) * MM, (y_mm + h) * MM)
    c.setFillColor(_ACCENT)
    c.setFont(_FONT_M, 7)
    c.drawString(x_mm * MM, (y_mm + 4.5) * MM,
                 "контроль: ровно 100 мм — печатайте БЕЗ «подгонки под страницу»")


def _sheet(c: canvas.Canvas, title: str, base: dict, g: dict, col: int, row: int,
           cols: int, rows: int, tile_w: float, tile_h: float, w_mm: float,
           d_mm: float) -> None:
    """Один лист A4: кусок контура с сеткой, метки склейки и подписи."""
    x0 = col * tile_w - (OVERLAP_MM if col else 0)   # левый край куска в координатах ящика
    y0 = row * tile_h - (OVERLAP_MM if row else 0)

    def px(mm_x: float) -> float:
        return (MARGIN_MM + mm_x - x0) * MM

    def py(mm_y: float) -> float:
        return (MARGIN_MM + FOOT_MM + mm_y - y0) * MM

    page_w, page_h = A4
    c.saveState()
    c.clipPath(_clip(c, page_w, page_h), stroke=0, fill=0)

    # сетка 42 мм: линии считаются от того же начала, что и в конфигураторе
    c.setStrokeColor(_GRID)
    c.setLineWidth(0.4)
    c.setDash(2, 3)
    for i in range(g["cols"] + 1):
        x = g["mL"] + i * 42.0
        c.line(px(x), py(max(0.0, y0)), px(x), py(min(d_mm, y0 + tile_h + 2 * OVERLAP_MM)))
    for j in range(g["rows"] + 1):
        y = g["mB"] + j * 42.0
        c.line(px(max(0.0, x0)), py(y), px(min(w_mm, x0 + tile_w + 2 * OVERLAP_MM)), py(y))
    c.setDash()

    # контур дна — жирным: по нему и проверяют замеры
    c.setStrokeColor(_INK)
    c.setLineWidth(1.4)
    c.rect(px(0), py(0), w_mm * MM, d_mm * MM, stroke=1, fill=0)
    c.restoreState()

    # подписи листа
    c.setFillColor(_INK)
    c.setFont(_FONT_B, 11)
    c.drawString(MARGIN_MM * MM, page_h - (MARGIN_MM - 1) * MM - 11,
                 f"{title} — шаблон дна 1:1")
    c.setFillColor(_MUTED)
    c.setFont(_FONT_M, 8)
    c.drawString(MARGIN_MM * MM, page_h - (MARGIN_MM - 1) * MM - 22,
                 f"лист {row * cols + col + 1}/{cols * rows} · ряд {row + 1}, столбец {col + 1} · "
                 f"ящик {w_mm:.0f}×{d_mm:.0f} мм · сетка {g['cols']}×{g['rows']} (шаг 42)")
    if cols * rows > 1:
        c.drawString(MARGIN_MM * MM, MARGIN_MM * MM - 4,
                     f"склейка: перекрытие {OVERLAP_MM:.0f} мм по правому и верхнему краю")
    _ruler(c, MARGIN_MM, MARGIN_MM + 6)


def _clip(c: canvas.Canvas, page_w: float, page_h: float):
    """Окно чертежа: между подвалом с линейкой и шапкой с номером листа."""
    p = c.beginPath()
    p.rect(MARGIN_MM * MM, (MARGIN_MM + FOOT_MM) * MM, WIN_W_MM * MM, WIN_H_MM * MM)
    return p


MAX_SHEETS = 60           # потолок листов: 60 A4 — это уже ящик 1.6×2.5 метра

def template_pdf(title: str, base: dict, g: dict) -> bytes:
    """PDF-шаблон дна: контур 1:1, сетка 42 мм, разрезка на A4 с полем склейки.

    Габариты ОБЯЗАНЫ быть живыми: работа растёт кубом от размера, а буфер копится в
    памяти — ящик «50000 мм» (опечатка в замерах или злой умысел) собирал бы десятки
    тысяч листов. Верхнюю границу держим здесь же, у самой тяжёлой работы."""
    _ensure_fonts()
    try:
        w_mm = max(20.0, float(base.get("W") or 0))
        d_mm = max(20.0, float(base.get("D") or 0))
    except (TypeError, ValueError):
        w_mm, d_mm = 480.0, 270.0
    if not (math.isfinite(w_mm) and math.isfinite(d_mm)):
        w_mm, d_mm = 480.0, 270.0
    if math.ceil(w_mm / TILE_W_MM) * math.ceil(d_mm / TILE_H_MM) > MAX_SHEETS:
        raise ValueError(
            f"Ящик {w_mm:.0f}×{d_mm:.0f} мм — это больше {MAX_SHEETS} листов A4. "
            "Проверьте замеры: их вводят в миллиметрах.")
    tile_w, tile_h = TILE_W_MM, TILE_H_MM
    cols = max(1, math.ceil(w_mm / tile_w))
    rows = max(1, math.ceil(d_mm / tile_h))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{title} — шаблон дна")
    for row in range(rows):
        for col in range(cols):
            _sheet(c, title, base, g, col, row, cols, rows, tile_w, tile_h, w_mm, d_mm)
            c.showPage()
    c.save()
    return buf.getvalue()
