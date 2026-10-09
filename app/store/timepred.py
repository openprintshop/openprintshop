"""Предсказание времени печати: замеры «оценка слайсера -> факт» и коррекция оценки.

Домен = ПОТРЕБИТЕЛЬ журнала печатей, а не его часть (ответ на открытый вопрос 2 плана
разреза): record_time_sample зовёт store.printjournal.add_print_journal, обратных вызовов
нет ни одного. Своя таблица у модуля ровно одна — print_samples (бэкап-копилка замеров,
Этап 0 docs/print-time-prediction.md); коррекция и сводки читают чужой print_journal.

predict_minutes спит (factor=1.0, applied=False), пока выборка меньше min_samples, выбросы
вне [1/3, 3] в медиану не берёт, а готовую поправку зажимает коридором [0.5x, 2x] — кривой
коэффициент не должен показать клиенту абсурд (план print-time-prediction §6/§8).

Транзакционных связок модуль не создаёт: add_print_sample — один INSERT под своим commit,
всё остальное — чтение. Связка «журнал + списание катушки» целиком живёт в
store/printjournal (волна 3), record_time_sample её только ВЫЗЫВАЕТ — границу commit волна 7
не режет.

Выделено из app/db.py волной 7 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ (относительных ленивых импортов и global в них не
было). Из внешнего мира нужны core._conn, printjournal.add_print_journal,
plastics.inventory_summary/cogs_summary и stdlib time; aiosqlite — только ради аннотации
`list_print_samples() -> list[aiosqlite.Row]` (под `from __future__ import annotations`
symtable её «не видит», брали текстовой сверкой).

ЛОВУШКА ДЛЯ ТЕСТОВ: predict_minutes зовёт САМА СЕБЯ, bigdata_stats зовёт predict_minutes, а
record_time_sample — add_print_journal, и все три раза ГОЛЫМ именем. После переезда
`monkeypatch.setattr(db, "predict_minutes"/"add_print_journal", ...)` до этих внутренних
вызовов не достаёт — патчить app.store.timepred. Сегодня таких подмен в репозитории нет.
"""
from __future__ import annotations

import time

import aiosqlite

from .core import _conn
from .plastics import cogs_summary, inventory_summary
from .printjournal import add_print_journal

# ---- Предсказание времени печати (Этап 0/5 docs/print-time-prediction.md) ----


async def add_print_sample(*, request_id: int | None, model_hash: str, est_minutes: float,
                           actual_minutes: float, grams: float | None = None,
                           material_type: str | None = None, extra_colors: int = 0,
                           source: str = "manual") -> int:
    """Образец «оценка слайсера → факт» на единицу для будущей модели предсказания времени
    (Этап 0 docs/print-time-prediction.md). Цену НЕ трогает — только копит данные."""
    cur = await _conn().execute(
        "INSERT INTO print_samples(request_id, model_hash, est_minutes, actual_minutes, grams, "
        "material_type, extra_colors, source, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (request_id, model_hash, est_minutes, actual_minutes, grams, material_type,
         int(extra_colors), source, int(time.time())))
    await _conn().commit()
    return cur.lastrowid


async def record_time_sample(request_id: int | None, est_minutes: float, actual_minutes: float, *,
                             material_type: str | None = None, grams: float | None = None,
                             model_hash: str = "", extra_colors: int = 0) -> None:
    """Записать замер оценка/факт по времени в print_journal (единый print_events — источник
    predict_minutes/bigdata_stats после слияния). Обогащает событие заказа (по request_id)
    недостающими полями; если события нет — создаёт отдельное source='test'. print_samples пишется
    отдельно как бэкап (add_print_sample)."""
    row = None
    if request_id:
        row = await (await _conn().execute(
            "SELECT id, est_minutes, minutes, material_type FROM print_journal WHERE request_id=? "
            "ORDER BY id LIMIT 1", (int(request_id),))).fetchone()
    if row:
        sets: dict = {}
        if not (row["est_minutes"] or 0) and est_minutes:
            sets["est_minutes"] = float(est_minutes)
        if not (row["minutes"] or 0) and actual_minutes:
            sets["minutes"] = float(actual_minutes)
        if not (row["material_type"] or "") and material_type:
            sets["material_type"] = material_type
        if model_hash:
            sets["model_hash"] = model_hash
        if extra_colors:
            sets["extra_colors"] = int(extra_colors)
        if sets:
            await _conn().execute(
                f"UPDATE print_journal SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                (*sets.values(), row["id"]))
            await _conn().commit()
    else:
        await add_print_journal(
            label=(model_hash or "замер"), plastic_id=None, plastic_name="",
            grams=float(grams or 0), minutes=float(actual_minutes or 0), graded=1, source="test",
            est_minutes=float(est_minutes or 0) or None, material_type=material_type,
            request_id=request_id)


async def print_sample_count() -> int:
    cur = await _conn().execute("SELECT COUNT(*) AS n FROM print_samples")
    return (await cur.fetchone())["n"]


async def list_print_samples(limit: int = 100) -> list[aiosqlite.Row]:
    cur = await _conn().execute("SELECT * FROM print_samples ORDER BY id DESC LIMIT ?", (limit,))
    return list(await cur.fetchall())


async def predict_minutes(est_minutes: float, material_type: str | None = None,
                          min_samples: int = 30, printer_id: int | None = None) -> dict:
    """Этап 5 мастер-плана: корректирует оценку слайсера по накопленным парам оценка/факт из
    print_samples (медиана факт/оценка). Пока выборка мала (< min_samples) — factor=1.0,
    applied=False: механизм СПИТ до накопления данных (плана: «не начинать пока выборка не
    выросла» — не выдумываем коррекцию на паре замеров). Пары с отношением вне [1/3, 3] —
    выбросы (прерванная/перезапущенная печать, кривая привязка) и в медиану не попадают, а
    готовая поправка зажата коридором [0.5×, 2×] (план print-time-prediction §6/§8: кривой
    коэффициент не должен показать клиенту абсурд). {predicted, factor, samples, applied}.

    ПОРОГ 30, А НЕ 8 (решение владельца 30.08.2026 по замеру). На девяти замерах медиана
    факт/оценка была 1.019 при 95%-м интервале 0.70…1.31 — данные не отличали «+2%» от
    «−30%», а один добавленный замер двигал поправку на 4 п.п. Пользы такая поправка давала
    на гривенник со среднего заказа, а коридор при этом разрешал ей поднять цену вдвое.

    printer_id (B1, второй принтер): сперва журнал ЭТОЙ машины (+материал), мало — без
    материала по ней же, всё ещё мало — прежняя общая выборка. Оценка в журнале уже несёт
    printers.time_factor, поэтому медиана здесь — остаток поверх ручного множителя."""
    est = float(est_minutes or 0)
    if est <= 0:
        return {"predicted": est, "factor": 1.0, "samples": 0, "applied": False}
    ratios = await _journal_ratios(material_type, printer_id)
    n = len(ratios)
    if n < min_samples and material_type:           # мало по материалу — пробуем по всем материалам
        return await predict_minutes(est, None, min_samples, printer_id)
    if n < min_samples and printer_id is not None:  # мало по машине — общая выборка, как раньше
        return await predict_minutes(est, material_type, min_samples, None)
    if n < min_samples:
        return {"predicted": round(est, 1), "factor": 1.0, "samples": n, "applied": False}
    factor = min(2.0, max(0.5, ratios[n // 2]))      # медиана факт/оценка, зажатая коридором
    return {"predicted": round(est * factor, 1), "factor": round(factor, 3),
            "samples": n, "applied": True}


async def _journal_ratios(material_type: str | None, printer_id: int | None) -> list[float]:
    """Отношения факт/оценка из print_journal без выбросов, по возрастанию.

    ПО `result` НЕ ФИЛЬТРУЕМ — и это осознанно (проверка 30.08.2026). `result='fail'` тут
    значит «на столе был брак», а не «печать оборвалась»: источник истины — `fail_pct`, доля
    испорченного. У боевой записи 145 брак 11 %, печать шла 431.9 мин из 464.7 оценочных —
    время у неё честное, и выкинуть её значило бы потерять годный замер. Оборвавшуюся печать
    (запись 139: 57.9 мин из 464.7 при том же fail_pct 9 %) отличает не result, а отношение —
    её и убирает коридор выбросов ниже."""
    where: str = "est_minutes > 0 AND minutes > 0"   # print_journal: факт = minutes, оценка = est
    args: list = []
    if material_type:
        where += " AND material_type = ?"
        args.append(material_type)
    if printer_id is not None:
        where += " AND printer_id = ?"
        args.append(int(printer_id))
    cur = await _conn().execute(
        f"SELECT minutes / est_minutes AS ratio FROM print_journal WHERE {where}", args)
    return sorted(float(r["ratio"]) for r in await cur.fetchall()
                  if r["ratio"] and 1 / 3 <= float(r["ratio"]) <= 3.0)


async def printer_time_stats(printer_id: int, current_factor: float = 1.0) -> dict:
    """Подсказка к коэффициенту времени машины (B1): по журналу ЭТОЙ машины медиана факт/оценка
    и предлагаемый множитель = текущий × медиана (оценка в журнале уже с текущим множителем).
    {samples, median, suggested} — samples=0 → подсказывать нечего (машина только приехала)."""
    ratios = await _journal_ratios(None, printer_id)
    n = len(ratios)
    if not n:
        return {"samples": 0, "median": None, "suggested": None}
    med = ratios[n // 2]
    return {"samples": n, "median": round(med, 3),
            "suggested": round(float(current_factor or 1.0) * med, 2)}


async def bigdata_stats() -> dict:
    """Сводка собранных данных для алгоритмов (OpenTodo 7507fba0): обучающая выборка модели
    предсказания времени (print_samples: оценка слайсера vs факт), расход материалов по
    напечатанным заказам, переиспользование моделей в коллекции и покрытие замерами."""
    c = _conn()
    out: dict = {}
    cur = await c.execute(
        "SELECT COUNT(*) AS n, AVG(est_minutes) AS est, AVG(minutes) AS act, "
        "AVG(ABS(minutes - est_minutes)) AS mae, "
        "AVG(ABS(minutes - est_minutes) / minutes) AS mape, "
        "SUM(CASE WHEN ABS(minutes - est_minutes) / minutes <= 0.2 THEN 1 ELSE 0 END) AS w20 "
        "FROM print_journal WHERE est_minutes > 0 AND minutes > 0")
    r = await cur.fetchone()
    n = r["n"] or 0
    out["samples"] = {"count": n, "avg_est": round(r["est"] or 0, 1),
                      "avg_actual": round(r["act"] or 0, 1), "mae_min": round(r["mae"] or 0, 1),
                      "mape_pct": round((r["mape"] or 0) * 100, 1),
                      "within20_pct": round((r["w20"] / n * 100) if n else 0, 1)}
    cur = await c.execute(
        "SELECT COALESCE(NULLIF(material_type,''),'—') AS m, COUNT(*) AS n, "
        "AVG(ABS(minutes - est_minutes)) AS mae FROM print_journal "
        "WHERE est_minutes > 0 AND minutes > 0 GROUP BY m ORDER BY n DESC LIMIT 8")
    out["samples_by_material"] = [{"material": x["m"], "count": x["n"],
                                   "mae_min": round(x["mae"] or 0, 1)} for x in await cur.fetchall()]
    cur = await c.execute(
        "SELECT plastic_name AS m, COUNT(*) AS n, COALESCE(SUM(grams_total),0) AS g, "
        "COALESCE(SUM(minutes_total),0) AS mins FROM requests WHERE status='printed' "
        "GROUP BY plastic_name ORDER BY g DESC LIMIT 8")
    out["materials"] = [{"material": x["m"], "orders": x["n"], "grams": round(x["g"], 1),
                         "minutes": round(x["mins"])} for x in await cur.fetchall()]
    cur = await c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(uses),0) AS u FROM collection")
    r = await cur.fetchone()
    out["collection"] = {"models": r["n"] or 0, "total_uses": r["u"] or 0}
    cur = await c.execute(
        "SELECT label, uses, last_material FROM collection ORDER BY uses DESC, id DESC LIMIT 8")
    out["top_models"] = [{"label": x["label"], "uses": x["uses"],
                          "material": x["last_material"]} for x in await cur.fetchall()]
    cur = await c.execute("SELECT COUNT(*) AS n FROM requests WHERE status='printed'")
    printed = (await cur.fetchone())["n"] or 0
    cur = await c.execute("SELECT COUNT(*) AS n FROM saved_models")
    saved = (await cur.fetchone())["n"] or 0
    out["totals"] = {"printed_orders": printed, "saved_models": saved,
                     "coverage_pct": round((n / printed * 100) if printed else 0, 1)}
    # Фаза C мастер-плана: остаток по катушкам и реальная себестоимость/маржа на новых данных
    out["inventory"] = await inventory_summary()
    out["cogs"] = await cogs_summary()
    out["prediction"] = await predict_minutes(60.0)   # Этап 5: текущая коррекция оценки (для эталона 60 мин)
    return out
