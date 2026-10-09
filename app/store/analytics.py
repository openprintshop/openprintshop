"""Производные цифры слоя данных, у которых НЕТ своей таблицы.

Объединяющий признак домена — отсутствие собственности: ни одна из четырёх функций не
владеет таблицей, а три из них вообще единственные в слое данных, кто не трогает _conn.

* parse_due_date — нормализация срока из ввода админа в вид «DD.MM»;
* eta_date — ОЧЕРЕДЬ-ОСОЗНАННЫЙ срок для мастера: ёмкость = часов в сутки на машину, умножить
  на число печатающих машин (без множителя вторая машина не сокращала бы срок ни на день);
* promised_date — простая клиентская витрина срока (B3), зависит от времени печати заказа;
* dashboard_stats — сводка админ-дашборда: агрегаты по ЧУЖИМ таблицам orders/requests
  (конверсия, выручка по напечатанным, ряд за 14 дней, статусы, источник лида B7).

Почему promised_date не уехала волной 4 вместе с заказами: top-level имя рядом с
add_request(..., promised_date=...) в store/orders.py дало бы F811 — та же ловушка, что у
`plates` в волне 3 и у `settings` в волне 2.

Выделено из app/db.py волной 7 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ. Ленивые `import datetime`/`import math` внутри
тел — АБСОЛЮТНЫЕ stdlib-импорты: подъёма на `..` правило endgame от них не требует, и
трогать их значило бы нарушить дословность. Из внешнего мира нужны config (часы печати в
сутки и число активных принтеров), core._conn и stdlib time.
"""
from __future__ import annotations

import time

from .. import config
from .core import _conn

# ---- Сроки исполнения и сводка админ-дашборда ----


def parse_due_date(raw: str) -> str | None:
    """Срок исполнения из ввода админа в нормализованный вид «DD.MM».
    Принимает «10.06», «10.6», «10.06.2026», «10/06» и т.п. Возвращает None, если дата
    не распознана / некорректна (несуществующий день-месяц). «-»/пусто = None (без срока)."""
    import datetime
    s = (raw or "").strip().replace("/", ".").replace("-", ".").replace(" ", "")
    if not s or s == ".":
        return None
    parts = [p for p in s.split(".") if p]
    if len(parts) < 2:
        return None
    try:
        day, month = int(parts[0]), int(parts[1])
        datetime.date(2000, month, day)   # 2000 — високосный, валидирует 29.02
    except ValueError:
        return None
    return f"{day:02d}.{month:02d}"


def eta_date(total_minutes: float) -> str:
    """Ориентировочная дата готовности «DD.MM»: сегодня + дни на печать очереди + этой работы.

    Ёмкость = часов в сутки НА МАШИНУ × число печатающих машин. Раньше множителя не было, и
    вторая машина не сокращала срок ни на день: клиент слышал бы прежнее обещание, хотя
    мастерская печатает вдвое быстрее — то есть выигрыш, ради которого второй принтер и
    покупают, до него бы не дошёл. С одной машиной формула та же, что была."""
    import datetime
    import math
    per_day = max(1.0, config.PRINT_HOURS_PER_DAY) * max(1, config.ACTIVE_PRINTERS) * 60.0
    days = max(1, math.ceil(max(0.0, total_minutes) / per_day))
    return (datetime.date.today() + datetime.timedelta(days=days)).strftime("%d.%m")


def promised_date(total_minutes: float, urgency: str | None, settings: dict) -> str:
    """Обещанный клиенту срок «DD.MM» (B3) — теперь зависит от ВРЕМЕНИ ПЕЧАТИ заказа, а не от
    фикс-дней (решение владельца). Простая клиентская витрина срока — в отличие от очередь-
    осознанного eta_date, который считает загрузку принтера для мастера. Правило:
      • 'rush' (срочно) → всегда завтра (сегодня+1), сколько бы ни печаталось (наценку ×1.5 не
        трогаем — она считается отдельно в pricing);
      • иначе (обычно) → печать короче term_split_hours ч → завтра (+1), иначе послезавтра (+2)."""
    import datetime
    rush = str(urgency or "").strip().lower() == "rush"
    if rush:
        days = 1
    else:
        try:
            split_h = float(settings.get("term_split_hours", 12) or 12)
        except (TypeError, ValueError):
            split_h = 12.0
        days = 1 if max(0.0, float(total_minutes or 0.0)) < split_h * 60.0 else 2
    return (datetime.date.today() + datetime.timedelta(days=days)).strftime("%d.%m")


async def dashboard_stats() -> dict:
    """Сводка для админ-дашборда: расчёты, заказы, конверсия, выручка (реализованная —
    только по напечатанным заказам, за 7-30 дней), топ материалов/моделей, филамент."""
    c = _conn()
    out: dict = {}

    async def _one(sql, args=()):
        cur = await c.execute(sql, args)
        return await cur.fetchone()

    out["calcs"] = (await _one("SELECT COUNT(*) AS n FROM orders"))["n"]
    out["orders"] = (await _one("SELECT COUNT(*) AS n FROM requests"))["n"]
    out["printed"] = (await _one("SELECT COUNT(*) AS n FROM requests WHERE status='printed'"))["n"]
    out["conv"] = (out["orders"] / out["calcs"] * 100.0) if out["calcs"] else 0.0
    out["conv_print"] = (out["printed"] / out["orders"] * 100.0) if out["orders"] else 0.0
    # Выручка — только по напечатанным заказам (status='printed'); revenue_all == revenue_printed.
    out["revenue_printed"] = (await _one(
        "SELECT COALESCE(SUM(price),0) AS s FROM requests WHERE status='printed'"))["s"]
    out["revenue_all"] = out["revenue_printed"]
    out["avg_order"] = (out["revenue_printed"] / out["printed"]) if out["printed"] else 0.0
    # Филамент — только по напечатанным заказам (как выручка), а не по всем расчётам.
    out["grams"] = (await _one(
        "SELECT COALESCE(SUM(grams_total),0) AS g FROM requests WHERE status='printed'"))["g"]

    now = int(time.time())
    for days in (7, 30):
        r = await _one("SELECT COUNT(*) AS n, COALESCE(SUM(total_price),0) AS s "
                       "FROM orders WHERE created_at >= ?", (now - days * 86400,))
        out[f"calcs_{days}d"] = r["n"]
        out[f"revenue_{days}d"] = r["s"]
        ro = await _one(    # фактические заказы; выручка — только по напечатанным
            "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status='printed' THEN price END),0) AS s "
            "FROM requests WHERE created_at >= ?", (now - days * 86400,))
        out[f"orders_{days}d"] = ro["n"]
        out[f"orev_{days}d"] = ro["s"]

    cur = await c.execute(    # топ материалов — по напечатанным заказам (requests.plastic_name)
        "SELECT plastic_name AS name, COUNT(*) AS n FROM requests "
        "WHERE status='printed' AND plastic_name IS NOT NULL AND plastic_name <> '' "
        "GROUP BY plastic_name ORDER BY n DESC LIMIT 5")
    out["top_materials"] = [(r["name"], r["n"]) for r in await cur.fetchall()]
    cur = await c.execute(
        "SELECT model_label AS name, COUNT(*) AS n FROM orders WHERE model_label IS NOT NULL "
        "GROUP BY model_label ORDER BY n DESC LIMIT 5")
    out["top_models"] = [(r["name"], r["n"]) for r in await cur.fetchall()]

    # Суточный ряд за 14 дней (заказы + выручка) — для графиков дашборда (area/bars).
    # Сутки нарезаем по UTC-полуночи; для тренда этого достаточно.
    day = 86400
    today0 = now - (now % day)
    series: list[dict] = []
    for i in range(13, -1, -1):
        d0 = today0 - i * day
        r = await _one(    # заказы — все за сутки; выручка — только по напечатанным
            "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status='printed' THEN price END),0) AS s "
            "FROM requests WHERE created_at >= ? AND created_at < ?", (d0, d0 + day))
        series.append({"orders": r["n"], "revenue": r["s"]})
    out["series14"] = series

    # Распределение заказов по статусам — для доната «Статусы заказов».
    cur = await c.execute("SELECT status, COUNT(*) AS n FROM requests GROUP BY status")
    out["status_mix"] = {r["status"]: r["n"] for r in await cur.fetchall()}

    # B7 (источник лида): разбивка заказов за 30 дней по requests.source — «откуда берутся
    # те ~7% конверсии». '—' = непомеченные (старые заказы, индивидуальные, товары витрины);
    # выручка — только напечатанные, как везде на дашборде.
    cur = await c.execute(
        "SELECT COALESCE(NULLIF(source,''),'—') AS src, COUNT(*) AS n, "
        "COALESCE(SUM(CASE WHEN status='printed' THEN price END),0) AS s "
        "FROM requests WHERE created_at >= ? GROUP BY src ORDER BY n DESC",
        (now - 30 * 86400,))
    out["sources_30d"] = [{"source": r["src"], "orders": r["n"], "revenue": r["s"]}
                         for r in await cur.fetchall()]
    # Новые vs вернувшиеся клиенты за 30 дней: «новый» = ПЕРВЫЙ заказ клиента внутри окна
    # (по MIN(created_at) всей истории — наличие прошлых заказов и есть критерий из B7).
    r = await _one(
        "SELECT COALESCE(SUM(CASE WHEN f.first_at >= ? THEN 1 ELSE 0 END),0) AS new_n, "
        "COALESCE(SUM(CASE WHEN f.first_at < ? THEN 1 ELSE 0 END),0) AS ret_n FROM ("
        "SELECT user_id, MIN(created_at) AS first_at FROM requests GROUP BY user_id) f "
        "WHERE f.user_id IN (SELECT DISTINCT user_id FROM requests WHERE created_at >= ?)",
        (now - 30 * 86400, now - 30 * 86400, now - 30 * 86400))
    out["clients_30d"] = {"new": int(r["new_n"] or 0), "returning": int(r["ret_n"] or 0)}
    return out
