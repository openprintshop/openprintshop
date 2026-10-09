"""PDF по калибровке: паспорт филамента и этикетки на полку образцов.

Два документа с разной задачей:

  • passport_pdf() — «Паспорт калибровки»: восемь чисел, чем каждое получено, какие тесты
    печатались и какие замеры их подтвердили. Это одновременно резервная копия результата
    (пресеты слайсера слетают при его обновлении), памятка при следующей калибровке и
    материал, который не стыдно показать клиенту: «печатаем на откалиброванном оборудовании,
    вот числа и вот чем они подтверждены».

  • labels_pdf() — этикетки с QR на коробку с образцами. Через полгода башня на полке — это
    просто кусок пластика; с этикеткой это «клин расхода, чистое окно 10–13 мм³/с, 27.07».

Шрифты и палитра — как в quote.py (DejaVu из assets/fonts/), чтобы документы выглядели
одной семьёй.
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path

from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import calibration, config

log = logging.getLogger(__name__)

_FONT = "DejaVu"
_FONT_B = "DejaVu-Bold"
_FONT_M = "DejaVu-Mono"
_fonts_ready = False

_INK = colors.HexColor("#16202b")
_MUTED = colors.HexColor("#5d6b78")
_FAINT = colors.HexColor("#93a1ad")
_LINE = colors.HexColor("#e7ebef")
_ACCENT = colors.HexColor("#ff7a1a")
_OK = colors.HexColor("#16a45c")
_PANEL = colors.HexColor("#f3f6f9")


def _ensure_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    # Шрифты лежат в assets/, а НЕ берутся из matplotlib: тот тянул 45 МБ зависимостей
    # ради трёх файлов .ttf и уезжал каждому покупателю (разбор состава 28.08.2026).
    ttf = config.BASE_DIR / "assets" / "fonts"
    pdfmetrics.registerFont(TTFont(_FONT, str(ttf / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_B, str(ttf / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_M, str(ttf / "DejaVuSansMono.ttf")))
    _fonts_ready = True


def _styles() -> dict:
    base = getSampleStyleSheet()["BodyText"]
    return {
        "h1": ParagraphStyle("h1", parent=base, fontName=_FONT_B, fontSize=20, textColor=_INK,
                             leading=23, spaceAfter=2),
        "lede": ParagraphStyle("lede", parent=base, fontName=_FONT, fontSize=9.5,
                               textColor=_MUTED, leading=13),
        "sec": ParagraphStyle("sec", parent=base, fontName=_FONT_M, fontSize=8.5,
                              textColor=_ACCENT, leading=11, spaceBefore=10, spaceAfter=5),
        "k": ParagraphStyle("k", parent=base, fontName=_FONT, fontSize=9, textColor=_MUTED,
                            leading=12),
        "v": ParagraphStyle("v", parent=base, fontName=_FONT_M, fontSize=9.5, textColor=_INK,
                            leading=12),
        "vb": ParagraphStyle("vb", parent=base, fontName=_FONT_B, fontSize=11, textColor=_INK,
                             leading=14),
        "small": ParagraphStyle("small", parent=base, fontName=_FONT, fontSize=8,
                                textColor=_FAINT, leading=11),
        "body": ParagraphStyle("body", parent=base, fontName=_FONT, fontSize=8.5,
                               textColor=_INK, leading=12),
    }


def _dt(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "—"


def _num(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:g}"


def _grid(rows, widths, st) -> Table:
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), _INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, _LINE),
        ("BACKGROUND", (0, 0), (-1, 0), _PANEL),
        ("FONTNAME", (0, 0), (-1, 0), _FONT_B),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def passport_pdf(out: str | Path, *, passport: dict, run: dict | None = None,
                 steps: list[dict] | None = None, measures: list[dict] | None = None,
                 samples: list[dict] | None = None, shop: str = "") -> Path:
    """Паспорт калибровки: значения, шаги, замеры, образцы.

    passport — как отдаёт db.get_calib_passport (values/source/presets уже словарями).
    """
    _ensure_fonts()
    s = _styles()
    dest = Path(out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(dest), pagesize=A4, title="Паспорт калибровки",
                            leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm)
    flow: list = []
    name = passport.get("plastic") or passport.get("plastic_name") or "Филамент"
    printer = passport.get("printer") or "—"
    flow.append(Paragraph("Паспорт калибровки", s["h1"]))
    flow.append(Paragraph(
        f"{name} · {printer} · сопло {passport.get('nozzle') or '0.4'} мм · "
        f"откалиброван {_dt(passport.get('calibrated_at'))}", s["lede"]))
    if shop:
        flow.append(Paragraph(shop, s["small"]))
    flow.append(Spacer(1, 10))

    # ── значения
    vals = passport.get("values") or {}
    src = passport.get("source") or {}
    rows = [["Параметр", "Значение", "", "Откуда"]]
    # Z-offset идёт первой строкой и ВНЕ PASSPORT_FIELDS: он не читается из пресетов слайсера
    # (там он всегда 0) — он живёт в самом принтере и привязан к пластине (1028ab67).
    z = passport.get("z_offset")
    z_own = z is not None
    if z is None:
        z = passport.get("plate_z")
    if z is not None:
        plate = passport.get("plate") or ""
        rows.append([Paragraph("Z-offset" + (f" · {plate}" if plate else ""), s["k"]),
                     Paragraph(f"<b>{_num(z)}</b>", s["v"]),
                     Paragraph("мм", s["small"]),
                     Paragraph("замер этого филамента" if z_own else "вылет пластины",
                               s["small"])])
    for key in calibration.PASSPORT_FIELDS:
        if key not in vals:
            continue
        label, unit = calibration.PASSPORT_LABELS.get(key, (key, ""))
        own = src.get(key) == "own"
        rows.append([Paragraph(label, s["k"]),
                     Paragraph(f"<b>{_num(vals[key])}</b>", s["v"]),
                     Paragraph(unit, s["small"]),
                     Paragraph("своя калибровка" if own else "из системного профиля",
                               s["small"])])
    if len(rows) > 1:
        flow.append(Paragraph("ЗНАЧЕНИЯ", s["sec"]))
        flow.append(_grid(rows, [6.2 * cm, 2.6 * cm, 1.6 * cm, 6.0 * cm], s))

    # ── шаги кампании
    if steps:
        flow.append(Paragraph("ЧЕМ ПОЛУЧЕНО", s["sec"]))
        rows = [["Тест", "Результат", "Вывод"]]
        for st in steps:
            t = calibration.test_info(st.get("test_key") or "")
            val = _num(st.get("value"))
            unit = st.get("unit") or (t.unit if t else "")
            # Тест с парой параметров (глажка) в одну ячейку «19 %» не укладывается — выводим
            # обе строки, иначе из паспорта не восстановить режим.
            vals = st.get("values") or {}
            if t and len(t.params) > 1 and vals:
                cell = "<br/>".join(f"<b>{_num(vals.get(p.key))}</b> {p.unit}"
                                    for p in t.params if vals.get(p.key) is not None)
            else:
                cell = f"<b>{val}</b> {unit}" if val != "—" else "—"
            rows.append([Paragraph(t.label if t else (st.get("test_key") or "—"), s["k"]),
                         Paragraph(cell, s["v"]),
                         Paragraph(str(st.get("verdict") or st.get("note") or "—")[:400],
                                   s["body"])])
        flow.append(_grid(rows, [4.2 * cm, 3.0 * cm, 9.2 * cm], s))

    # ── замеры
    if measures:
        flow.append(Paragraph("ЗАМЕРЫ И ВЕРДИКТЫ", s["sec"]))
        rows = [["Замер", "Вывод", "Обоснование"]]
        for m in measures:
            kind = next((k["label"] for k in calibration.VERDICT_KINDS
                         if k["key"] == m.get("kind")), m.get("kind") or "—")
            rows.append([Paragraph(str(kind), s["k"]),
                         Paragraph(f"<b>{m.get('verdict') or '—'}</b>", s["v"]),
                         Paragraph(str(m.get("detail") or "")[:500], s["body"])])
        flow.append(_grid(rows, [4.6 * cm, 3.4 * cm, 8.4 * cm], s))

    # ── образцы
    if samples:
        flow.append(Paragraph("ОБРАЗЦЫ НА ПОЛКЕ", s["sec"]))
        rows = [["Шифр", "Образец", "Что доказывает", "Где лежит"]]
        for x in samples:
            rows.append([Paragraph(str(x.get("code") or "—"), s["v"]),
                         Paragraph(str(x.get("title") or "—"), s["k"]),
                         Paragraph(str(x.get("proves") or "")[:300], s["body"]),
                         Paragraph(str(x.get("place") or "—"), s["small"])])
        flow.append(_grid(rows, [2.2 * cm, 4.4 * cm, 8.0 * cm, 2.2 * cm], s))

    # ── памятка: правила, по которым принимались решения
    flow.append(PageBreak())
    flow.append(Paragraph("Правила диагностики", s["h1"]))
    flow.append(Paragraph("Выработаны по ходу этой калибровки — годятся для любого филамента.",
                          s["lede"]))
    flow.append(Spacer(1, 8))
    for d in calibration.DIAGNOSTICS:
        block = [Paragraph(f"<b>{d['title']}</b>", s["vb"]),
                 Paragraph(f"<b>Симптом.</b> {d['symptom']}", s["body"])]
        if d.get("distinguish"):
            block.append(Paragraph(f"<b>Отличать от.</b> {d['distinguish']}", s["body"]))
        if d.get("cause"):
            block.append(Paragraph(f"<b>Причина.</b> {d['cause']}", s["body"]))
        block.append(Paragraph(f"<b>Что делать.</b> {d['fix']}", s["body"]))
        block.append(Spacer(1, 7))
        flow.append(KeepTogether(block))

    if run:
        flow.append(Spacer(1, 6))
        flow.append(Paragraph(
            f"Кампания «{run.get('title') or '—'}» · повод: {run.get('reason') or '—'} · "
            f"{_dt(run.get('started_at'))}", s["small"]))
    doc.build(flow)
    return dest


def _qr_drawing(value: str, size_mm: float) -> Drawing:
    w = qr.QrCodeWidget(value, barLevel="M")
    b = w.getBounds()
    bw, bh = (b[2] - b[0]) or 1, (b[3] - b[1]) or 1
    side = size_mm * mm
    d = Drawing(side, side, transform=[side / bw, 0, 0, side / bh, -b[0] * side / bw,
                                       -b[1] * side / bh])
    d.add(w)
    return d


def labels_pdf(out: str | Path, samples: list[dict], *, base_url: str = "") -> Path:
    """Этикетки на образцы: сетка 2 × 6 на A4, у каждой QR + шифр + что доказывает.

    base_url задан → QR ведёт на подписанную страницу образца (открывается с телефона без
    логина); иначе в QR кладём сам шифр — этикетка остаётся полезной и без сети.
    """
    _ensure_fonts()
    s = _styles()
    dest = Path(out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(dest), pagesize=A4, title="Этикетки образцов",
                            leftMargin=1.0 * cm, rightMargin=1.0 * cm,
                            topMargin=1.0 * cm, bottomMargin=1.0 * cm)
    cells: list = []
    for x in samples:
        link = x.get("url") or (f"{base_url.rstrip('/')}/csample/{x['id']}?s={x.get('sig', '')}"
                                if base_url and x.get("id") else "")
        payload = link or f"PS-CALIB {x.get('code') or ''}"
        inner = Table([[
            _qr_drawing(payload, 20),
            [Paragraph(f"<b>{x.get('code') or ''}</b>", s["v"]),
             Paragraph(str(x.get("title") or "")[:60], s["k"]),
             Paragraph(str(x.get("value") or "")[:40], s["v"]),
             Paragraph(str(x.get("proves") or "")[:110], s["small"]),
             Paragraph(_dt(x.get("created_at")), s["small"])],
        ]], colWidths=[2.4 * cm, 6.0 * cm])
        inner.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]))
        cells.append(inner)
    if not cells:
        cells = [Paragraph("Нет образцов", s["k"])]
    rows = [cells[i:i + 2] for i in range(0, len(cells), 2)]
    if rows and len(rows[-1]) == 1:
        rows[-1].append("")
    grid = Table(rows, colWidths=[9.0 * cm, 9.0 * cm], rowHeights=[3.4 * cm] * len(rows))
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.3, _LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, _LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    doc.build([grid])
    return dest


def out_dir() -> Path:
    d = config.DATA_DIR / "calibration"
    d.mkdir(parents=True, exist_ok=True)
    return d
