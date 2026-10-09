"""Генерация PDF-сметы для клиента (reportlab) — фирменный бланк «Print Studio».

Кириллица через шрифт DejaVu из assets/fonts/ — раньше он брался из поставки matplotlib,
но тот тянул 45 МБ зависимостей ради трёх .ttf и уезжал каждому покупателю;
моноширинный DejaVuSansMono для чисел/цены — технический акцент, как в дизайне мини-аппа.
Палитра: тёплый акцент #ff7a1a, зелёная цена, нейтральные чернила на белой бумаге.
"""
from __future__ import annotations

import datetime
import html
import logging
import uuid
from pathlib import Path

from reportlab.graphics.shapes import Circle, Drawing, Line
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import config
from .pricing import PriceBreakdown

log = logging.getLogger(__name__)

_FONT = "DejaVu"
_FONT_B = "DejaVu-Bold"
_FONT_M = "DejaVu-Mono"
_fonts_ready = False

# Фирменная палитра (на белой бумаге)
_INK = colors.HexColor("#16202b")
_MUTED = colors.HexColor("#5d6b78")
_FAINT = colors.HexColor("#93a1ad")
_LINE = colors.HexColor("#e7ebef")
_ACCENT = colors.HexColor("#ff7a1a")
_PRICE = colors.HexColor("#16a45c")
_NOTE_BG = colors.HexColor("#fbf4ec")
_NOTE_BD = colors.HexColor("#f3e1cf")
_NOTE_TX = colors.HexColor("#8a6a44")
_PANEL = colors.HexColor("#f3f6f9")


def _ensure_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    # Шрифты — из assets/, а не из matplotlib (см. пояснение в app/calibpdf.py).
    ttf = config.BASE_DIR / "assets" / "fonts"
    pdfmetrics.registerFont(TTFont(_FONT, str(ttf / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_B, str(ttf / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_M, str(ttf / "DejaVuSansMono.ttf")))
    _fonts_ready = True


def _money(v: float, cur: str) -> str:
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} {cur}"


def _hm(minutes: float) -> str:
    t = int(round(minutes))
    h, m = divmod(t, 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def quote_total(bd: PriceBreakdown, dev_fee: float = 0.0, delivery_cost: float = 0.0) -> float:
    """ИТОГО сметы = печать + разработка модели + доставка.

    Одна формула на оба места документа (строка «ИТОГО к оплате» и штамп суммы в футере —
    раньше `bd.total + _dc` был выписан там дважды) и на тест. Обязана совпадать с суммой,
    которую клиент видит в приложении (texts.order_breakdown) и с requests.price
    индивидуального заказа (webapp: `total = round(bd.total + dev_fee, 2)`)."""
    return round(float(bd.total) + float(dev_fee or 0.0) + float(delivery_cost or 0.0), 2)


def _brand_mark(size: float = 30.0) -> Drawing:
    """Простой знак-катушка: два концентрических круга + 4 спицы (в фирменном оранжевом)."""
    d = Drawing(size, size)
    c = size / 2.0
    hub = size / 6.0
    d.add(Circle(c, c, c - 1.2, strokeColor=_ACCENT, strokeWidth=1.6, fillColor=None))
    d.add(Circle(c, c, hub, strokeColor=_ACCENT, strokeWidth=1.6, fillColor=None))
    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        d.add(Line(c + dx * (hub + 1.5), c + dy * (hub + 1.5),
                   c + dx * (c - 1.8), c + dy * (c - 1.8),
                   strokeColor=_ACCENT, strokeWidth=1.4))
    return d


def build_quote(
    *, business_name: str, business_contact: str, model_label: str, printer_name: str,
    plastic_name: str, qty: int, quality_label: str, strength_label: str,
    bd: PriceBreakdown, cur: str, settings: dict[str, float], preview_path: Path | None,
    plates: int = 1, delivery_cost: float = 0.0, delivery_label: str = "",
    dev_fee: float = 0.0,
) -> Path:
    """delivery_cost (A2) — стоимость доставки заказа отдельной строкой перед ИТОГО.

    Доставка одна на весь заказ-корзину, а смета — по позиции, поэтому строку получает
    ТОЛЬКО якорная позиция группы (та же, что несёт доставку в онлайн-счёте). Иначе клиент
    увидел бы доставку в каждой смете и решил, что заплатит за неё N раз.

    dev_fee (OpenTodo 6aaa6a19) — плата за разработку модели по индивидуальному заказу. Она
    живёт не в разбивке, а колонкой requests.dev_fee, поэтому в bd.total её нет и в PDF она
    раньше не попадала вовсе: в приложении клиент видел одну сумму, а в присланной смете —
    меньшую на dev_fee. Строка и ИТОГО считаются как в texts.order_breakdown. Живая смета
    (расчёт до заказа) разработки не знает — там аргумент не передают, документ прежний."""
    _ensure_fonts()
    qid = uuid.uuid4().hex[:8].upper()
    out = config.QUOTE_DIR / f"quote_{qid}.pdf"
    today = datetime.date.today().strftime("%d.%m.%Y")

    base = getSampleStyleSheet()["Normal"]
    wm = ParagraphStyle("wm", parent=base, fontName=_FONT_B, fontSize=15, textColor=_INK, leading=17)
    sub = ParagraphStyle("sub", parent=base, fontName=_FONT, fontSize=8, textColor=_FAINT, leading=11, spaceBefore=1)
    meta = ParagraphStyle("meta", parent=base, fontName=_FONT_M, fontSize=8.5, textColor=_MUTED, leading=13, alignment=TA_RIGHT)
    h1 = ParagraphStyle("h1", parent=base, fontName=_FONT_B, fontSize=21, textColor=_INK, leading=24, spaceAfter=2)
    lede = ParagraphStyle("lede", parent=base, fontName=_FONT, fontSize=10, textColor=_MUTED, leading=13)
    seclab = ParagraphStyle("seclab", parent=base, fontName=_FONT_M, fontSize=8.5, textColor=_ACCENT, leading=11, spaceAfter=5)
    pk = ParagraphStyle("pk", parent=base, fontName=_FONT, fontSize=9.5, textColor=_MUTED, leading=12)
    pv = ParagraphStyle("pv", parent=base, fontName=_FONT_B, fontSize=9.5, textColor=_INK, leading=12, alignment=TA_RIGHT)
    pvm = ParagraphStyle("pvm", parent=base, fontName=_FONT_M, fontSize=9, textColor=_INK, leading=12, alignment=TA_RIGHT)
    nm = ParagraphStyle("nm", parent=base, fontName=_FONT_B, fontSize=12.5, textColor=_INK, leading=15)
    note = ParagraphStyle("note", parent=base, fontName=_FONT, fontSize=8.5, textColor=_NOTE_TX, leading=12)
    foot = ParagraphStyle("foot", parent=base, fontName=_FONT, fontSize=8.5, textColor=_MUTED, leading=13)

    doc = SimpleDocTemplate(str(out), pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.3 * cm,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm, title="Смета на 3D-печать")

    # ── шапка: знак + вордмарк + мета ──
    biz = (business_name or "").strip()
    wordmark = _e(biz) if biz else 'PRINT<font color="#ff7a1a">·</font>STUDIO'
    sub_txt = "Мастерская 3D-печати" + (f" · {_e(printer_name)}" if printer_name else "")
    brand_block = [Paragraph(wordmark, wm), Paragraph(sub_txt, sub)]
    meta_block = Paragraph(f"СМЕТА № <b>{qid}</b><br/>Дата: <b>{today}</b>", meta)
    header = Table([[_brand_mark(30), brand_block, meta_block]],
                   colWidths=[1.05 * cm, 10.3 * cm, 6.65 * cm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (1, 0), "MIDDLE"), ("VALIGN", (2, 0), (2, 0), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 9),
    ]))

    story: list = [
        header, Spacer(1, 7),
        HRFlowable(width="100%", thickness=2, color=_INK, spaceAfter=14),
        Paragraph("Смета на 3D-печать", h1),
        Paragraph("Предварительный расчёт по автоматическому слайсингу модели.", lede),
        Spacer(1, 16),
    ]

    # ── параметры (правая колонка / на всю ширину, если нет превью) ──
    filament_val = f"{bd.grams_total:.1f} г ({bd.grams_unit:.1f} г/шт)"
    if bd.purge_grams_unit:
        filament_val += f", из них {bd.purge_grams_unit:.1f} г/шт на продувку"
    prows = [
        (Paragraph("Принтер", pk), Paragraph(_e(printer_name), pv)),
        (Paragraph("Материал", pk), Paragraph(_e(plastic_name), pv)),
        (Paragraph("Количество", pk), Paragraph(f"{qty} шт", pvm)),
        # Качество и прочность в смете не показываем: клиент их не выбирает (8c8199d1),
        # печать идёт профилем мастера — для клиента это лишний шум.
    ]
    if plates and plates >= 2:
        prows.append((Paragraph("Столов в проекте", pk), Paragraph(str(plates), pvm)))
    if bd.extra_colors:
        prows.append((Paragraph("Тип печати", pk), Paragraph(f"многоцветная (~{bd.extra_colors + 1} цв.)", pv)))
    if float(getattr(bd, "urgency_factor", 1.0) or 1.0) > 1.0:   # срочный заказ (B3)
        prows.append((Paragraph("Срочность", pk), Paragraph("срочная печать", pv)))
    prows += [
        (Paragraph("Расход филамента", pk), Paragraph(filament_val, pvm)),
        (Paragraph("Время печати", pk), Paragraph(f"{_hm(bd.minutes_total)} ({_hm(bd.minutes_unit)}/шт)", pvm)),
    ]

    have_img = bool(preview_path and Path(preview_path).exists())
    pcol_w = (9.4 if have_img else 18.0) * cm
    ptbl = Table([[k, v] for k, v in prows],
                 colWidths=[pcol_w * 0.46, pcol_w * 0.54])
    ptbl.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, _LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    params_block = [Paragraph("ПАРАМЕТРЫ ПЕЧАТИ", seclab), ptbl]

    if have_img:
        try:
            img = Image(str(preview_path), width=6.2 * cm, height=6.2 * cm)
            card = Table([[img], [Paragraph(_e(model_label), nm)]], colWidths=[7.0 * cm])
            card.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.7, _LINE),
                ("LINEABOVE", (0, 1), (-1, 1), 0.7, _LINE),
                ("BACKGROUND", (0, 0), (0, 0), _PANEL),
                ("ALIGN", (0, 0), (0, 0), "CENTER"),
                ("TOPPADDING", (0, 0), (0, 0), 10), ("BOTTOMPADDING", (0, 0), (0, 0), 10),
                ("LEFTPADDING", (0, 1), (-1, 1), 12), ("RIGHTPADDING", (0, 1), (-1, 1), 12),
                ("TOPPADDING", (0, 1), (-1, 1), 9), ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
                ("VALIGN", (0, 1), (-1, 1), "MIDDLE"),
            ]))
            body = Table([[card, params_block]], colWidths=[7.4 * cm, 10.6 * cm])
            body.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 12),
                ("LEFTPADDING", (1, 0), (1, 0), 0), ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(body)
        except Exception:  # noqa: BLE001
            story.extend([Paragraph(_e(model_label), nm), Spacer(1, 6), *params_block])
    else:
        story.extend([Paragraph(_e(model_label), nm), Spacer(1, 8), *params_block])

    # ── стоимость ──
    story += [Spacer(1, 20), Paragraph("СТОИМОСТЬ", seclab)]
    lab = ParagraphStyle("clab", parent=base, fontName=_FONT, fontSize=10, textColor=_INK, leading=13)
    val = ParagraphStyle("cval", parent=base, fontName=_FONT_M, fontSize=10, textColor=_INK, leading=13, alignment=TA_RIGHT)

    def crow(title, em, money_txt, disc=False):
        t = Paragraph(f"{title}  <font size=8 color='#93a1ad'>· {em}</font>" if em else title, lab)
        v = Paragraph(money_txt, ParagraphStyle("cv", parent=val, textColor=(_PRICE if disc else _INK)))
        return [t, v]

    crows = [
        crow("Филамент", f"{bd.grams_total:.1f} г", _money(bd.filament_cost, cur)),
        crow("Машинное время", _hm(bd.minutes_total), _money(bd.machine_cost, cur)),
        crow("Буфер на брак", f"{(getattr(bd, 'failure_rate_pct', 0.0) or settings['failure_rate_pct']):.0f}%",
             _money(bd.waste_cost, cur)),
        crow("Подготовка", "фикс. на заказ", _money(bd.setup_fee, cur)),
        crow("Наценка", f"{settings['profit_margin_pct']:.0f}%", _money(bd.margin, cur)),
    ]
    if bd.discount_pct:
        # Скидка в pricing.compute_price считается от ВСЕГО итога, включая филамент — подпись
        # «кроме филамента» врала (сумма при этом была верной). Формулировка как в texts.py.
        crows.append(crow("Персональная скидка", f"{bd.discount_pct:.0f}% на всё",
                          f"−{_money(bd.discount_amount, cur)}", disc=True))
    # Срочность (B3): строка наценки за срочную печать; getattr — старые сметы без поля не падают.
    _uf = float(getattr(bd, "urgency_factor", 1.0) or 1.0)
    if _uf > 1.0:
        crows.append(crow("Срочность", f"срочная печать ×{_uf:g}",
                          f"+{_money(float(getattr(bd, 'urgency_surcharge', 0.0) or 0.0), cur)}"))
    # Опции постобработки (B4): PDF-смета строит строки ВРУЧНУЮ (не через texts.breakdown_rows),
    # поэтому опции добавляем здесь отдельно — иначе сумма строк не сойдётся с ИТОГО. Цену 0 не рисуем.
    for _opt in (getattr(bd, "options", None) or []):
        _p = float(_opt.get("price") or 0.0)
        if _p > 0:
            crows.append(crow(_opt.get("name") or "Услуга", "постобработка", f"+{_money(_p, cur)}"))
    # Разработка модели (OpenTodo 6aaa6a19): отдельной строкой перед доставкой — ровно как в
    # расчётнике приложения (texts.order_breakdown). Ноль не рисуем: у обычного заказа её нет.
    _dev = float(dev_fee or 0.0)
    if _dev > 0:
        crows.append(crow("Разработка модели", "индивидуальный заказ", f"+{_money(_dev, cur)}"))
    # Доставка (A2): строка перед ИТОГО, чтобы сумма в смете сходилась с суммой в заказе
    # и в счёте на оплату. Ноль не рисуем — самовывоз и бесплатная доставка строки не получают.
    _dc = float(delivery_cost or 0.0)
    if _dc > 0:
        crows.append(crow("Доставка", delivery_label or "доставка заказа", f"+{_money(_dc, cur)}"))
    _grand = quote_total(bd, _dev, _dc)
    total_lab = Paragraph("ИТОГО к оплате", ParagraphStyle("tl", parent=base, fontName=_FONT_B, fontSize=12, textColor=_INK))
    total_val = Paragraph(_money(_grand, cur), ParagraphStyle("tv", parent=base, fontName=_FONT_M, fontSize=14, textColor=_PRICE, alignment=TA_RIGHT))
    crows.append([total_lab, total_val])

    ctbl = Table(crows, colWidths=[12.5 * cm, 5.5 * cm])
    ctbl.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, _LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 1.4, _INK),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, -1), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(ctbl)

    # ── примечание ──
    note_tbl = Table([[Paragraph(
        "Расчёт предварительный, основан на автоматическом слайсинге. Итоговая стоимость может "
        "измениться после проверки модели мастером (поддержки, ориентация, ручная доработка).", note)]],
        colWidths=[18.0 * cm])
    note_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.7, _NOTE_BD), ("BACKGROUND", (0, 0), (-1, -1), _NOTE_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [Spacer(1, 16), note_tbl]

    # ── футер ──
    contact_lines = []
    if biz:
        contact_lines.append(f"<b>{_e(biz)}</b> · мастерская 3D-печати")
    if business_contact:
        contact_lines.append(f"Контакты: <b>{_e(business_contact)}</b>")
    contact_lines.append("Расчёт действителен 7 дней с даты составления.")
    foot_left = Paragraph("<br/>".join(contact_lines), foot)
    stamp = Paragraph(
        f'<font name="{_FONT_M}" size=12 color="#16202b">{_money(_grand, cur)}</font><br/>'
        f'<font size=8 color="#93a1ad">включая все коэффициенты и скидку'
        + (', с разработкой модели' if _dev > 0 else '')
        + (', с доставкой' if _dc > 0 else '') + '</font>',
        ParagraphStyle("stamp", parent=base, fontName=_FONT, alignment=TA_RIGHT, leading=15))
    foota = Table([[foot_left, stamp]], colWidths=[11.0 * cm, 7.0 * cm])
    foota.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [Spacer(1, 22), HRFlowable(width="100%", thickness=0.7, color=_LINE, spaceAfter=12), foota]

    doc.build(story)
    return out
