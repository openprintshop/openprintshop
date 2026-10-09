"""Один конструктор заказа: снимок расчёта → строка requests плюс хвост побочных эффектов.

Выделено из app/webapp.py волной 5 цикла 2 (шаг 4) БЕЗ единой правки тел — сюда уехали три
имени: сам конструктор (`webapp._place_one` → `place_from_token`), план резерва пластика
`_order_stock_plan` и последний рубеж превью `_embedded_thumb`. webapp реэкспортирует все три
фасадом на месте выреза, поэтому код и тесты, знающие их как webapp.*, продолжают находить их
там же — включая `inspect.getsource(webapp._place_one)` в tests/test_b2_intersection_final.py.

Помощники уехали ЦЕЛИКОМ, а не остались в webapp: модуль обязан не импортировать ни webapp,
ни handlers — иначе цикл, ради разрыва которого затевался вынос, оказался бы спрятан в
отложенном импорте, а не разорван. Правило то же, что у app/printmedia.py (волна 3).

Конструктор зовут ОБА пути оформления: мини-аппа (app/webapp.py) и бот
(app/handlers/user_flow.py). Разница между ними — только содержимое снимка, а не код.
"""
from __future__ import annotations

import asyncio
import logging

from . import cache, db, preview, pricing

log = logging.getLogger(__name__)


# Зовут: webapp._api_order / _api_order_cart / покупка готового изделия / заказ из чата
# (через фасад webapp._place_one) и user_flow.place_order — кнопка «Оставить заказ» в боте.
#
# СКЛАД У ОБОИХ ПУТЕЙ ОДИНАКОВ (волна 5, шаг 5): заказ РЕЗЕРВИРУЕТ пластик планом, списание —
# при «напечатано» (db.apply_order_stock, движение помечается ref_type='request'/ref_id).
# До шага 5 бот-заказ приходил сюда БЕЗ `plastic_id` в снимке и списывал склад сам, сразу и
# без ссылки на заказ — из-за чего cogs_actual у бот-заказов оставался нулевым. Если снова
# заведёте где-то немедленное списание, план из снимка надо убирать ТЕМ ЖЕ изменением: план
# плюс немедленное списание = двойной расход при «напечатано».
async def place_from_token(u: dict, m: dict, order_group: str | None = None,
                           comment: str = "") -> int | None:
    """Создаёт заявку из токена модели: запись + redeem промо + списание склада. Возвращает rid.
    Уведомления НЕ шлёт — это решает вызывающий (один заказ vs корзина с одной сводкой)."""
    last = m.get("last")
    if not last:
        return None
    uname = ("@" + u["username"]) if u.get("username") else \
        (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {u['id']}")
    pdf = m.get("pdf") or {}        # снимок расчётника цены (для сметы заказа), если расчёт ещё жив
    breakdown = (pricing.breakdown_to_dict(pdf["bd"], pdf["settings"])
                 if pdf.get("bd") is not None and pdf.get("settings") is not None else None)
    if breakdown is not None:
        # масштаб — в снимок: пересчёты заказа (recalc/recompute) слайсят с ним же
        breakdown["scale_pct"] = int(last.get("scale_pct", 100) or 100)
    # Срочность (B3): в колонку пишем '' (обычный) / 'rush' (срочный); обещанный срок фиксируем на
    # момент оформления по настройкам-снимку (pdf.settings), чтобы он не «поплыл» при смене настроек.
    urgency = "rush" if (m.get("urgency") == "rush") else ""
    _settings_snap = pdf.get("settings") or await db.get_settings()
    # Срок по времени печати заказа (last.minutes_total) — «срочно» всегда завтра, «обычно» —
    # завтра/послезавтра относительно порога term_split_hours.
    promised = db.promised_date(float(last.get("minutes_total") or 0.0),
                                "rush" if urgency == "rush" else "normal", _settings_snap)
    rid = await db.add_request(
        user_id=int(u["id"]), user_name=uname,
        model_label=m.get("order_label") or m["label"], printer=last["printer"],
        plastic_name=last["plastic_name"], quantity=last["qty"], quality=last["quality"],
        strength=last["strength"], grams_total=last["grams_total"],
        # cpreview — цветной рендер слайсера; он есть не всегда (флекси-осьминог: цветной
        # рендер/коллаж не формируется). Откат: серое базовое превью → встроенная в .3mf
        # миниатюра. Без цепочки заказ уходил бы без картинки (OpenTodo c6d9cebc).
        minutes_total=last["minutes_total"], price=last["total"],
        preview_path=(m.get("cpreview") or m.get("preview") or await _embedded_thumb(m.get("paths"))),
        dims=last.get("dims", ""), multicolor=int(m.get("multicolor", 0)),
        # столов в заказе: выбранные, иначе — из последнего расчёта (calc_plates: раскладка
        # набора деталей по столам, defe5dc0), иначе — родные столы модели
        plates=int(len(m["plates_sel"]) if m.get("plates_sel")
                   else (m.get("calc_plates") or m.get("plates", 1))),
        hash=cache.model_hash(m["paths"]), order_group=order_group,
        src_url=m.get("src_url", ""), file_path=(m["paths"][0] if m.get("paths") else ""),
        promo=m.get("promo_typed", ""), promo_ok=int(m.get("promo_ok", 0)),
        quality_id=last.get("quality_id"), strength_id=last.get("strength_id"),
        breakdown=breakdown, description=comment or None,
        urgency=urgency, promised_date=promised,
        # B7 (источник лида): происхождение модели из снимка токена — file/link/catalog/
        # mine/repeat; '' у путей, где токен не размечен (индивидуальный, товар витрины)
        source=str(m.get("origin") or ""))
    # Опции постобработки (B4): снимок имя+цена в таблицу связи (данные-сейф — поздняя правка
    # каталога не переписывает историю). В requests.breakdown они уже уехали снимком выше через
    # bd.options; request_options — дублирующий слой, удобный для выборок и тега у мастера.
    try:
        await db.set_request_options(rid, m.get("options"))
    except Exception:  # noqa: BLE001
        log.debug("set request options failed", exc_info=True)
    if m.get("promo_code"):
        try:
            await db.redeem_promo(m["promo_code"])
        except Exception:  # noqa: BLE001
            log.debug("redeem promo failed", exc_info=True)
    # Заказ РЕЗЕРВИРУЕТ пластик, а не списывает (OpenTodo a89586a2): строим план списания и
    # сохраняем на заказе; реальное списание — при «напечатано» (db.apply_order_stock).
    plan = _order_stock_plan(last)
    if plan:
        try:
            await db.set_order_stock_plan(rid, plan)
        except Exception:  # noqa: BLE001
            log.debug("set stock plan failed", exc_info=True)
    await db.log_event(int(u["id"]), "оставил заказ", f'#{rid} «{m["label"]}»', user_name=uname)
    # Любимый цвет клиента (ux-paths-next.md, идея 4): запоминаем выбор для следующего расчёта.
    try:
        await db.set_user_last_color(int(u["id"]), last.get("color") or "")
    except Exception:  # noqa: BLE001
        log.debug("remember last color failed", exc_info=True)
    return rid


def _order_stock_plan(last: dict) -> list[dict]:
    """План резерва пластика по расчёту заказа (OpenTodo a89586a2/70525c99/a197bb93): столы на
    разных пластиках → доля grams_total на каждый; иначе — один пластик со всеми выбранными
    цветами. Общий для _place_one и досчёта заказа, оформленного во время слайса (order_now)."""
    pid = last.get("plastic_id")
    pgs = last.get("plastic_grams") or []
    plan: list[dict] = []
    if len(pgs) > 1:
        total_u = sum(g["grams_unit"] for g in pgs) or 1.0
        for g in pgs:
            share = last.get("grams_total", 0) * g["grams_unit"] / total_u
            plan.append({"plastic_id": g["plastic_id"], "name": g.get("name") or "",
                         "colors": [g["color"]] if g.get("color") else [], "grams": share,
                         "color_grams": None})
    elif pid:
        cols = last.get("colors_sel") or ([last["color"]] if last.get("color") else [])
        plan.append({"plastic_id": pid, "name": last.get("plastic_name") or "",
                     "colors": cols,
                     "grams": last.get("grams_total", 0), "color_grams": last.get("color_grams")})
    return plan


async def _embedded_thumb(paths) -> str | None:
    """Готовая миниатюра, встроенная слайсером в .3mf (Metadata/plate_1.png и т.п.). Достаётся
    мгновенно — распаковка одного PNG, без загрузки меша. Последний рубеж превью там, где наш
    рендер не потянуть: 7-стольный флекси-осьминог грузится/дециируется минутами, коллаж
    плейт-рендеров не собирается — и заказ оставался без картинки (OpenTodo c6d9cebc)."""
    try:
        if not paths:
            return None
        p = await asyncio.to_thread(preview.extract_3mf_thumbnail, list(paths))
        return str(p) if p else None
    except Exception:  # noqa: BLE001
        return None
