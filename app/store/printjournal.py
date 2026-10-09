"""Журнал печатей мастера: print_journal целиком, телеметрия принтера и мост в калибровку.

Один модуль обязателен из-за транзакционной связки и двустороннего цикла:

* add_print_journal под своим commit-ом делает пять записей — INSERT print_journal,
  доклейка requests.printer_id, списание катушки через add_stock_ledger, отметка
  stock_deducted и НА ХВОСТЕ attach_journal_to_calib;
* attach_journal_to_calib зовёт обратно get_print_journal/set_journal_calib —
  двусторонний цикл «журнал <-> калибровочная привязка», ровно тот случай, из-за которого
  волна 2 не резала склад по границе пластики/катушки.

Поэтому волна 3 выковыряла из секции «Калибровка» (домен волны 6) четыре имени —
active_calib_run, _calib_step_row, get_calib_step, update_calib_step (52 строки). Волна 6
их либо импортирует отсюда, либо переносит store->store; `_calib_step_row` вдобавок зовёт
оставшаяся в db.py list_calib_steps, поэтому его фасадная строка обязана дожить до волны 6.

set_journal_calib/list_calib_journal/backfill_calib_types физически лежали в секции
«пластины стола», но SELECT/UPDATE-ят print_journal — это журнал, а не плиты.

Списание склада не режется по границе commit: apply_journal_stock захватывает
print_journal.stock_deducted и вызывает store.plastics.deduct_stock — та же одна транзакция,
что и раньше внутри db.py.

Выделено из app/db.py волной 3 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ (ленивых импортов и global в них не было). Изменился
только модульный `log`: теперь app.store.printjournal, а не app.db.

ЛОВУШКА ДЛЯ ТЕСТОВ: attach_journal_to_calib зовёт get_print_journal ГОЛЫМ именем, поэтому
`monkeypatch.setattr(db, "get_print_journal", ...)` (tests/test_media_delete.py) до этого
внутреннего вызова больше не достаёт — при нужде патчить app.store.printjournal. Сегодняшние
тесты ходят через webapp -> db.get_print_journal и остаются зелёными.

Предсказание времени (predict_minutes/record_time_sample) — ПОТРЕБИТЕЛЬ журнала, а не его
часть: record_time_sample зовёт add_print_journal, обратных вызовов нет. Домен «аналитика»,
волна 7 (это ответ на открытый вопрос 2 плана).
"""
from __future__ import annotations

import json
import logging
import time

from .. import calibration
from .core import _abs, _conn, _jload, _rel
from .plastics import (
    CC2_JOB_SLACK_S,
    add_stock_ledger,
    cc2_job_same,
    cc2_job_usage,
    deduct_stock,
    get_plastic,
    spool_is_cc2,
)

log = logging.getLogger(__name__)

# source_of_grams записи журнала, чьи граммы — реальный расход по журналу принтера (CC2 Control)
CC2_MEASURED = "cc2_measured"
# Чьи граммы журнал принтера вправе переписать: оценки и «веса нет». Телеметрия и весы — факт.
_CC2_REPLACEABLE = ("", "estimate_missing", "slicer_estimate", CC2_MEASURED)


# ---- «печать для себя»: внутренние печати админа ----
# Фаза B мастер-плана: пишем в ЕДИНЫЙ журнал печатей (print_journal, source='self') вместо отдельной
# таблицы self_prints — устраняем дублирующую сущность. self_prints оставлена только для истории.
async def add_self_print(plastic_name: str, grams: float, minutes: float = 0.0, label: str = "") -> int:
    return await add_print_journal(
        label=label or "печать для себя", plastic_id=None, plastic_name=plastic_name,
        grams=max(0.0, grams), minutes=max(0.0, minutes), graded=1, source="self")


# ---- журнал печатей мастера (OpenTodo ed1518ed) ----
# C2: в print_journal путь под DATA_DIR хранят done_photo и timelapse. Пишем их через
# rel_data (choke-point'ы add_print_journal/set_print_journal_fields), а наружу отдаём
# абсолютными — на них смотрят Path.exists()/FSInputFile/FileResponse десятков читателей.
_PJ_PATH_FIELDS = ("done_photo", "timelapse")


def _pj_row(r) -> dict:
    d = dict(r)
    for k in _PJ_PATH_FIELDS:
        if k in d:
            d[k] = _abs(d[k])
    return d


def _fail_pct_norm(fail_pct) -> float:
    """Нормализация ползунка брака 0–100 (OpenTodo 60e0b2a8)."""
    try:
        return max(0.0, min(100.0, float(fail_pct)))
    except (TypeError, ValueError):
        return 0.0


async def add_print_journal(*, label: str, plastic_id: int | None, plastic_name: str,
                            grams: float, minutes: float, result: str = "",
                            fail_pct: float | None = None,
                            fail_reason: str = "", note: str = "", graded: int = 1,
                            request_id: int | None = None, timelapse: str | None = None,
                            source: str | None = None, spool_id: int | None = None,
                            est_minutes: float | None = None,
                            source_of_grams: str | None = None,
                            material_type: str | None = None,
                            printer_id: int | None = None,
                            watch_key: str | None = None) -> int:
    """result оставлен для совместимости; источник истины — fail_pct (0 = удача, 100 = полный
    брак, между — частичный). Без fail_pct берём из result ('fail' → 100).
    graded=0 — печать занесена автоматически, оценку (результат) мастер поставит позже
    (OpenTodo 5ac0ae9f). request_id/timelapse — привязка к заказу и видео (0f848906).
    source (Фаза B) — order/self/test; None → выводим из request_id. spool_id (Фаза B) — катушка
    печати: списываем с неё grams в журнал движений. est_minutes — оценка слайсера (пара к факту).

    watch_key — ключ наблюдения printwatch («эта печать этой машины»): с ним вызов
    ИДЕМПОТЕНТЕН. Строка с тем же ключом уже есть → вторая не вставляется, возвращается её id.
    Нужно повтору записи финиша: INSERT мог пройти, а commit — упасть («database is locked»),
    и тогда строка висит в общей неявной транзакции соединения (store.md §3) и уедет в БД с
    первым же чужим commit-ом. Слепой повтор INSERT дал бы дубль печати в журнале."""
    pct = _fail_pct_norm(fail_pct if fail_pct is not None
                         else (100 if result == "fail" else 0))
    src = source or ("order" if request_id else "self")
    jid, deducted = None, False
    if watch_key:
        # Своё соединение видит и незакоммиченную строку прошлой попытки — то, что нужно.
        cur = await _conn().execute(
            "SELECT id, stock_deducted FROM print_journal WHERE watch_key = ? "
            "ORDER BY id LIMIT 1", (watch_key,))
        prev = await cur.fetchone()
        if prev:
            jid, deducted = int(prev["id"]), bool(prev["stock_deducted"])
    if jid is None:
        cur = await _conn().execute(
            "INSERT INTO print_journal(label, plastic_id, plastic_name, grams, minutes, result, "
            "fail_pct, fail_reason, note, graded, request_id, timelapse, source, spool_id, "
            "est_minutes, source_of_grams, material_type, printer_id, watch_key, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((label or "")[:120], plastic_id, (plastic_name or "")[:80], float(grams or 0),
             float(minutes or 0), "fail" if pct > 0 else "ok", pct,
             (fail_reason or "")[:200] or None, (note or "")[:300] or None, int(graded),
             request_id, _rel(timelapse), src, spool_id,
             (float(est_minutes) if est_minutes else None), source_of_grams, material_type,
             (int(printer_id) if printer_id else None), watch_key or None,
             int(time.time())))
        jid = cur.lastrowid
    # commit и для найденной строки: если она осталась от сорвавшегося commit-а, сейчас она
    # становится настоящей. Хвост ниже идемпотентен (UPDATE «только в пустое», привязка к
    # калибровке не перетирает занятое), а списание катушки стережёт stock_deducted — поэтому
    # повтор доделывает то, до чего прошлая попытка не дошла, и ничего не делает дважды.
    await _conn().commit()
    # Заказ узнаёт СВОЮ машину в момент, когда его напечатали, — раньше её просто нет: при
    # оформлении клиент машину не выбирает, а мастерская решает у стола. Пишем только в пустое:
    # заказ печатают один раз, но допечатки и переделки случаются, и переписывать первую машину
    # на вторую значило бы задним числом соврать про то, где он был сделан (цикл 3, волна 4).
    if request_id and printer_id:
        try:
            await _conn().execute(
                "UPDATE requests SET printer_id = ? WHERE id = ? AND printer_id IS NULL",
                (int(printer_id), int(request_id)))
            await _conn().commit()
        except Exception:  # noqa: BLE001 — привязка машины не должна ронять запись печати
            log.debug("не проставить printer_id заказу #%s", request_id, exc_info=True)
    if spool_id and float(grams or 0) > 0 and not deducted:   # печать списывает свою катушку (линк в stock_ledger)
        try:
            # Катушку CC2 Control списывает сам принтер, по факту выдавленного: здесь только
            # отметка «учтено», чтобы apply_journal_stock не списал печать ещё и по пластику.
            if not await spool_is_cc2(spool_id):
                await add_stock_ledger(spool_id, -float(grams), "print", ref_type="journal", ref_id=jid)
            # катушка списана напрямую → помечаем запись учтённой, чтобы apply_journal_stock не
            # списал повторно (идемпотентность, как для печатей вне заказа).
            await _conn().execute("UPDATE print_journal SET stock_deducted = 1 WHERE id = ?", (jid,))
            await _conn().commit()
        except Exception:  # noqa: BLE001
            log.debug("journal spool deduct failed", exc_info=True)
    # Калибровка: тест узнаётся по имени задания принтера и сам ложится в открытую кампанию
    # (attach_journal_to_calib). Ошибка здесь не должна ронять запись печати в журнал.
    try:
        await attach_journal_to_calib(jid)
    except Exception:  # noqa: BLE001
        log.debug("calib attach failed", exc_info=True)
    return jid


# ── Фазы печати (05.09.2026, допечатка со слоя W4) ─────────────────────────────────────────
async def add_print_phase(*, printer_id: int | None, phase: str, layer: int | None = None,
                          z: float | None = None, job_name: str = "", ts: int | None = None) -> int:
    """Переход фазы печати (printing → paused → stopped …) с последним слоем. Пишет printwatch."""
    cur = await _conn().execute(
        "INSERT INTO print_phases(printer_id, ts, phase, layer, z, job_name) VALUES(?,?,?,?,?,?)",
        (printer_id, int(ts or time.time()), str(phase or ""), layer, z, job_name or ""))
    await _conn().commit()
    return int(cur.lastrowid or 0)


async def last_print_phase(printer_id: int | None = None, *,
                           phases: tuple[str, ...] = ("paused",), within_sec: int = 3 * 86400) -> dict | None:
    """Последний переход в одну из фаз (по умолчанию — пауза) за последние дни, или None.
    printer_id=None — любая машина: с одним принтером поле в записях бывает пустым."""
    q = "SELECT * FROM print_phases WHERE ts >= ? AND phase IN (%s)" % ",".join("?" * len(phases))
    args: list = [int(time.time()) - within_sec, *phases]
    if printer_id is not None:
        q += " AND (printer_id = ? OR printer_id IS NULL)"
        args.append(int(printer_id))
    q += " ORDER BY ts DESC LIMIT 1"
    cur = await _conn().execute(q, tuple(args))
    r = await cur.fetchone()
    return {k: r[k] for k in r.keys()} if r else None


# ── История телеметрии принтера (фидбэк 20.07) ──────────────────────────────────────────────
async def add_printer_telemetry(*, printing: int, nozzle: float | None = None,
                                nozzle_target: float | None = None, bed: float | None = None,
                                bed_target: float | None = None, chamber: float | None = None,
                                progress: float | None = None, layer: int | None = None,
                                filename: str = "", light: int | None = None,
                                active_slot: int | None = None,
                                fan_model: int | None = None, ts: int | None = None,
                                printer_id: int | None = None) -> int:
    """Снимок телеметрии в историю. None = принтер этого не вещал (домыслов не пишем).

    printer_id — ЧЬЯ это телеметрия. С одной машиной поле избыточно, с двумя без него история
    превращается в мешанину: температуры и прогресс двух принтеров лягут вперемешку, и ни один
    график задним числом не разделить."""
    cur = await _conn().execute(
        "INSERT INTO printer_telemetry(ts, printing, nozzle, nozzle_target, bed, bed_target, "
        "chamber, progress, layer, filename, light, active_slot, fan_model, printer_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(ts or time.time()), int(printing), nozzle, nozzle_target, bed, bed_target,
         chamber, progress, layer, (filename or "")[:200], light, active_slot, fan_model,
         (int(printer_id) if printer_id else None)))
    await _conn().commit()
    return cur.lastrowid


async def prune_printer_telemetry(keep_days: int = 90) -> int:
    """Удалить снимки старше keep_days; вернуть число удалённых (ретенция истории)."""
    cur = await _conn().execute("DELETE FROM printer_telemetry WHERE ts < ?",
                                (int(time.time()) - int(keep_days) * 86400,))
    await _conn().commit()
    return cur.rowcount or 0


async def list_printer_telemetry(since: int, until: int | None = None,
                                 limit: int = 5000) -> list[dict]:
    """Снимки телеметрии за интервал [since, until] по возрастанию времени — для будущей
    аналитики/графиков (потребителей пока нет, пишем историю заранее)."""
    q = "SELECT * FROM printer_telemetry WHERE ts >= ?"
    args: list = [int(since)]
    if until is not None:
        q += " AND ts <= ?"
        args.append(int(until))
    q += " ORDER BY ts LIMIT ?"
    args.append(int(limit))
    cur = await _conn().execute(q, args)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_print_event(jid: int) -> dict | None:
    """Единый вид события печати (print_events, мастер-план Фаза B): запись журнала + катушки,
    списанные на эту печать (из stock_ledger по линку journal/заказа — многоцвет = несколько
    строк) + пара оценка/факт по времени + происхождение граммов. Объединённая модель поверх
    print_journal + stock_ledger (без отдельной таблицы — линки уже есть)."""
    ev = await get_print_journal(jid)
    if not ev:
        return None
    conds, args = ["(l.ref_type='journal' AND l.ref_id=?)"], [int(jid)]
    if ev.get("request_id"):
        conds.append("(l.ref_type='request' AND l.ref_id=?)")
        args.append(int(ev["request_id"]))
    cur = await _conn().execute(
        "SELECT l.spool_id, s.color, -l.delta_g AS grams FROM stock_ledger l "
        "LEFT JOIN spools s ON s.id = l.spool_id "
        f"WHERE l.delta_g < 0 AND ({' OR '.join(conds)}) ORDER BY l.id", args)
    ev["spools"] = [{"spool_id": r["spool_id"], "color": r["color"],
                     "grams": round(float(r["grams"]), 1)} for r in await cur.fetchall()]
    # Катушки CC2 Control: их расход пришёл журналом принтера (ref_type='cc2') — узнаём печать
    # по имени файла и времени (запись журнала — финиш, minutes — длительность).
    end = int(ev.get("created_at") or 0)
    start = end - int(float(ev.get("minutes") or 0) * 60)
    for r in await cc2_job_usage(ev.get("label") or "", start - CC2_JOB_SLACK_S,
                                 end + CC2_JOB_SLACK_S):
        ev["spools"].append({"spool_id": r["spool_id"], "color": r["color"],
                             "grams": round(-float(r["delta_g"]), 1), "source": "cc2"})
    ev["actual_minutes"] = ev.get("minutes")     # факт = minutes журнала (пара к est_minutes)
    return ev


async def grade_print_journal(jid: int, fail_pct: float, fail_reason: str = "") -> bool:
    """Поставить оценку (результат) ранее авто-занесённой печати (OpenTodo 5ac0ae9f).
    graded → 1; fail_pct 0 = удача, 100 = полный брак."""
    pct = _fail_pct_norm(fail_pct)
    cur = await _conn().execute(
        "UPDATE print_journal SET fail_pct=?, result=?, fail_reason=?, graded=1 WHERE id=?",
        (pct, "fail" if pct > 0 else "ok", (fail_reason or "")[:200] or None, int(jid)))
    await _conn().commit()
    return cur.rowcount > 0


async def set_print_journal_fields(jid: int, **fields) -> None:
    """Дозаполнить запись журнала (фото/таймлапс/заказ/пластик) — полный опрос (0f848906)."""
    allowed = {"request_id", "done_photo", "timelapse", "plastic_id", "plastic_name", "grams",
               "note", "tl_deleted", "settings_id", "printer_id",
               # Замороженные признаки печати (этап 0 автотюнинга, app/tuning.py): колонки
               # заведены давно миграцией print_samples_merged и до сих пор пустовали.
               # Пишет их только tuning.capture_features и только в пустое — правки мастера
               # в журнале этих полей не касаются.
               "layer_height", "infill_pct", "walls", "volume_cm3", "bbox_x", "bbox_y",
               "bbox_z", "bbox_volume", "parts", "process_name", "material_type"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    # Явная привязка таймлапса снимает след удаления (1faab6cb) — иначе мастер, удаливший видео
    # и затем привязавший новое, не увидел бы его из-за tl_deleted в самолечении/свипе.
    if sets.get("timelapse") and "tl_deleted" not in sets:
        sets["tl_deleted"] = 0
    if not sets:
        return
    for k in _PJ_PATH_FIELDS:      # C2: в БД пути только относительно DATA_DIR
        if k in sets:
            sets[k] = _rel(sets[k])
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE print_journal SET {assignments} WHERE id = ?",
                          (*sets.values(), int(jid)))
    await _conn().commit()


async def get_print_journal(jid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM print_journal WHERE id = ?", (int(jid),))
    r = await cur.fetchone()
    return _pj_row(r) if r else None


async def apply_journal_stock(jid: int) -> list[tuple[str, float]]:
    """Списать филамент завершённой ПЕЧАТИ ВНЕ ЗАКАЗА со склада её пластика РОВНО ОДИН РАЗ
    (OpenTodo 05806f15). Идемпотентно через print_journal.stock_deducted. Печать, привязанную к
    заказу (request_id задан), пропускаем — за неё списывает сам заказ при «напечатано»
    (apply_order_stock), иначе двойное списание. Возвращает [(имя_пластика, остаток)], если
    списание пересекло порог «мало» (для алерта)."""
    cur = await _conn().execute(
        "SELECT plastic_id, plastic_name, grams, request_id, COALESCE(stock_deducted,0) AS done, "
        "source_of_grams FROM print_journal WHERE id = ?", (int(jid),))
    row = await cur.fetchone()
    if not row or row["done"]:
        return []
    # Граммы из журнала принтера (CC2 Control): он же их и списал со своих катушек.
    if row["source_of_grams"] == CC2_MEASURED:
        await _conn().execute("UPDATE print_journal SET stock_deducted = 1 WHERE id = ?", (int(jid),))
        await _conn().commit()
        return []
    # привязана к заказу или нечего/некуда списывать → пока не трогаем (флаг НЕ ставим: если мастер
    # позже отвяжет заказ или укажет пластик/граммы — спишем тогда).
    if row["request_id"] or not row["plastic_id"] or float(row["grams"] or 0) <= 0:
        return []
    new, low = await deduct_stock(int(row["plastic_id"]), float(row["grams"]),
                                  ref_type="journal", ref_id=int(jid))
    await _conn().execute("UPDATE print_journal SET stock_deducted = 1 WHERE id = ?", (int(jid),))
    await _conn().commit()
    return [((row["plastic_name"] or "пластик"), new)] if low else []


async def cc2_fill_journal(jobs: list[str], *, within_s: int = 3 * 86400) -> list[int]:
    """Граммы печатей — из журнала принтера (CC2 Control): он меряет реально выдавленное по
    катушкам, а журнал печатей у файлов Orca получал 0 г («estimate_missing») или оценку слайсера.

    Берёт записи журнала за последние дни, чей файл совпал с одним из jobs, и ставит им сумму
    расхода катушек за время печати (`source_of_grams = cc2_measured`). Пластик — если запись
    его не знала, а печать шла катушками одного пластика. Телеметрию и весы не трогает.
    Повторный вызов с тем же расходом ничего не пишет. Возвращает id изменённых записей."""
    if not jobs:
        return []
    ph = ",".join("?" * len(_CC2_REPLACEABLE))
    cur = await _conn().execute(
        "SELECT id, label, created_at, minutes, grams, plastic_id, source_of_grams FROM print_journal "
        f"WHERE created_at >= ? AND COALESCE(source_of_grams, '') IN ({ph})",
        (int(time.time()) - int(within_s), *_CC2_REPLACEABLE))
    changed: list[int] = []
    for row in await cur.fetchall():
        label = row["label"] or ""
        if not any(cc2_job_same(j, label) for j in jobs):
            continue
        end = int(row["created_at"] or 0)
        start = end - int(float(row["minutes"] or 0) * 60)
        usage = await cc2_job_usage(label, start - CC2_JOB_SLACK_S, end + CC2_JOB_SLACK_S)
        grams = round(sum(-float(u["delta_g"]) for u in usage), 2)
        if grams <= 0:
            continue
        sets: dict = {}
        if row["source_of_grams"] != CC2_MEASURED or abs(float(row["grams"] or 0) - grams) >= 0.05:
            sets.update(grams=grams, source_of_grams=CC2_MEASURED)
        pids = {u["plastic_id"] for u in usage}
        if row["plastic_id"] is None and len(pids) == 1:
            p = await get_plastic(int(next(iter(pids))))
            if p:
                sets.update(plastic_id=p.id, plastic_name=p.name, material_type=p.material_type)
        if not sets:
            continue
        assignments = ", ".join(f"{k} = ?" for k in sets)
        await _conn().execute(f"UPDATE print_journal SET {assignments} WHERE id = ?",
                              (*sets.values(), int(row["id"])))
        changed.append(int(row["id"]))
    if changed:
        await _conn().commit()
    return changed


async def update_print_journal(jid: int, *, label: str, plastic_id: int | None,
                               plastic_name: str, grams: float, minutes: float,
                               fail_pct: float, fail_reason: str = "", note: str = "") -> bool:
    """Правка прошлой записи журнала (OpenTodo 60e0b2a8). True — запись существовала."""
    pct = _fail_pct_norm(fail_pct)
    cur = await _conn().execute(
        "UPDATE print_journal SET label=?, plastic_id=?, plastic_name=?, grams=?, minutes=?, "
        "result=?, fail_pct=?, fail_reason=?, note=?, graded=1 WHERE id=?",   # правка = оценка
        ((label or "")[:120], plastic_id, (plastic_name or "")[:80], float(grams or 0),
         float(minutes or 0), "fail" if pct > 0 else "ok", pct,
         (fail_reason or "")[:200] or None, (note or "")[:300] or None, int(jid)))
    await _conn().commit()
    return cur.rowcount > 0


async def list_print_journal(limit: int = 60, printer_id: int | None = None,
                             with_unowned: bool = False) -> list[dict]:
    """Последние записи журнала. `printer_id` — только печати ЭТОЙ машины.

    Фильтр нужен свипу таймлапсов: он ищет видео в истории ОДНОГО принтера и матчит их по имени
    задания. Без фильтра запись, напечатанная на второй машине, матчилась бы по имени с историей
    первой — имена заданий («plate_1.gcode») совпадают сплошь и рядом, и к печати уехало бы
    чужое видео.

    `with_unowned` добавляет записи БЕЗ хозяина (printer_id IS NULL). Спрашивает это только
    основная машина, и не по доброте: пока принтер был один, машину в журнал не писали вовсе, а
    печать, сделанная в режиме «всё из .env», хозяина не получает и сегодня. Без этой оговорки
    такие записи навсегда остались бы без таймлапсов — молча."""
    if printer_id:
        where = "printer_id = ?" + (" OR printer_id IS NULL" if with_unowned else "")
        cur = await _conn().execute(
            f"SELECT * FROM print_journal WHERE {where} ORDER BY id DESC LIMIT ?",
            (int(printer_id), limit))
    else:
        cur = await _conn().execute(
            "SELECT * FROM print_journal ORDER BY id DESC LIMIT ?", (limit,))
    return [_pj_row(r) for r in await cur.fetchall()]


async def list_print_journal_for_request(rid: int) -> list[dict]:
    """Все печати, отнесённые к заказу (на многостольный заказ их несколько) — в порядке печати.
    Источник агрегата «таймлапсы и скрины стола заказа»: медиа копится per-печать именно здесь."""
    cur = await _conn().execute(
        "SELECT * FROM print_journal WHERE request_id = ? ORDER BY id", (int(rid),))
    return [_pj_row(r) for r in await cur.fetchall()]


async def delete_print_journal(jid: int) -> None:
    await _conn().execute("DELETE FROM print_journal WHERE id = ?", (jid,))
    await _conn().commit()


async def print_journal_stats() -> dict:
    """Сводка журнала по ползунку брака (60e0b2a8): «печати с браком» = fail_pct > 0,
    граммы в брак = Σ grams × fail_pct/100, % брака — взвешенный по граммам.
    Неоценённые записи (graded=0, OpenTodo 5ac0ae9f) в брак-статистику НЕ входят (нет оценки),
    но считаются в общем числе печатей и расходе пластика; их количество — отдельно (ungraded)."""
    c = _conn()
    cur = await c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(minutes),0) AS minutes, "
                          "COALESCE(SUM(grams),0) AS grams_all, "
                          "SUM(CASE WHEN COALESCE(graded,1)=0 THEN 1 ELSE 0 END) AS ungraded "
                          "FROM print_journal")
    tot = await cur.fetchone()
    # брак-метрики — ТОЛЬКО по оценённым записям
    cur = await c.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN COALESCE(fail_pct,0) > 0 THEN 1 ELSE 0 END) AS fails, "
        "COALESCE(SUM(grams),0) AS grams, "
        "COALESCE(SUM(grams * COALESCE(fail_pct,0) / 100.0),0) AS waste "
        "FROM print_journal WHERE COALESCE(graded,1)=1")
    r = await cur.fetchone()
    cur = await c.execute(
        "SELECT plastic_name, COUNT(*) AS n, "
        "SUM(CASE WHEN COALESCE(fail_pct,0) > 0 THEN 1 ELSE 0 END) AS fails, "
        "COALESCE(SUM(grams),0) AS grams, "
        "COALESCE(SUM(grams * COALESCE(fail_pct,0) / 100.0),0) AS waste "
        "FROM print_journal WHERE plastic_name != '' AND COALESCE(graded,1)=1 "
        "GROUP BY plastic_name ORDER BY n DESC LIMIT 12")
    by_plastic = [{"plastic": x["plastic_name"], "n": x["n"], "fails": x["fails"] or 0,
                   "fail_pct": round((x["waste"] or 0) / x["grams"] * 100) if x["grams"] else 0,
                   "grams": round(x["grams"]), "waste": round(x["waste"] or 0)}
                  for x in await cur.fetchall()]
    return {"total": tot["n"] or 0, "ungraded": tot["ungraded"] or 0, "fails": r["fails"] or 0,
            "fail_pct": round((r["waste"] or 0) / r["grams"] * 100) if r["grams"] else 0,
            "grams": round(tot["grams_all"]), "minutes": round(tot["minutes"]),
            "waste": round(r["waste"] or 0), "by_plastic": by_plastic}


async def active_calib_run(plastic_id: int | None = None) -> dict | None:
    """Открытая кампания — к ней автоматически привязываются новые калибровочные печати.

    С plastic_id сначала ищем кампанию именно по этому пластику: одновременно открытых
    кампаний может быть несколько (разные катушки), и печать должна лечь в свою.
    """
    if plastic_id:
        cur = await _conn().execute(
            "SELECT * FROM calib_runs WHERE status='active' AND plastic_id=? ORDER BY id DESC "
            "LIMIT 1", (int(plastic_id),))
        r = await cur.fetchone()
        if r:
            return dict(r)
    cur = await _conn().execute(
        "SELECT * FROM calib_runs WHERE status='active' ORDER BY id DESC LIMIT 1")
    r = await cur.fetchone()
    return dict(r) if r else None


def _calib_step_row(r) -> dict:
    d = dict(r)
    d["values"] = _jload(d.pop("values_json", ""), {})
    return d


async def get_calib_step(sid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM calib_steps WHERE id = ?", (int(sid),))
    r = await cur.fetchone()
    return _calib_step_row(r) if r else None


async def update_calib_step(sid: int, **fields) -> bool:
    """values={ключ_параметра: число} — для тестов с парой параметров (глажка).
    value остаётся зеркалом ПЕРВОГО параметра теста: по нему считаются дрейф и PDF."""
    allowed = {"status", "value", "unit", "range_from", "range_to", "verdict", "note",
               "journal_id", "applied", "applied_at"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "values" in fields:
        vals = {}
        for k, v in (fields.get("values") or {}).items():
            try:
                vals[str(k)[:48]] = None if v is None or v == "" else float(v)
            except (TypeError, ValueError):
                continue
        sets["values_json"] = json.dumps(vals, ensure_ascii=False)
        if "value" not in sets:
            row = await get_calib_step(sid)
            t = calibration.test_info((row or {}).get("test_key") or "")
            if t and t.param in vals:
                sets["value"] = vals[t.param]
    if not sets:
        return False
    sets["updated_at"] = int(time.time())
    cur = await _conn().execute(
        f"UPDATE calib_steps SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), int(sid)))
    await _conn().commit()
    return cur.rowcount > 0


async def set_journal_calib(jid: int, *, calib_type: str | None = None,
                            run_id: int | None = None, step_id: int | None = None) -> None:
    sets: dict = {}
    if calib_type is not None:
        sets["calib_type"] = calib_type
    if run_id is not None:
        sets["calib_run_id"] = run_id or None
    if step_id is not None:
        sets["calib_step_id"] = step_id or None
    if not sets:
        return
    await _conn().execute(
        f"UPDATE print_journal SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), int(jid)))
    await _conn().commit()


async def list_calib_journal(run_id: int | None = None, calib_only: bool = True,
                             limit: int = 200) -> list[dict]:
    """Калибровочные печати из журнала. Это и есть накопленный опыт: у каждой уже есть
    фото и таймлапс — их снимает printwatch, независимо от калибровки."""
    q = "SELECT * FROM print_journal WHERE 1=1"
    args: list = []
    if calib_only:
        q += " AND COALESCE(calib_type,'') != ''"
    if run_id:
        q += " AND calib_run_id = ?"
        args.append(int(run_id))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    cur = await _conn().execute(q, args)
    return [_pj_row(r) for r in await cur.fetchall()]


async def backfill_calib_types() -> dict:
    """Пересканировать журнал печатей и проставить тип калибровочного теста.

    Идемпотентно: пересчитывает calib_type для ВСЕХ записей (реестр тестов мог пополниться
    новыми алиасами — тогда старые печати наконец распознаются). Вернуть сводку по типам.
    """
    rows = await (await _conn().execute("SELECT id, label, calib_type FROM print_journal")).fetchall()
    changed = 0
    by_type: dict[str, int] = {}
    for r in rows:
        k = calibration.detect_test(r["label"] or "")
        if k:
            by_type[k] = by_type.get(k, 0) + 1
        if (r["calib_type"] or "") != k:
            await _conn().execute("UPDATE print_journal SET calib_type=? WHERE id=?", (k, r["id"]))
            changed += 1
    await _conn().commit()
    return {"scanned": len(rows), "changed": changed, "by_type": by_type,
            "calib": sum(by_type.values())}


async def attach_journal_to_calib(jid: int) -> dict:
    """Привязать калибровочную печать к открытой кампании и её шагу.

    Печать теста узнаётся по имени задания принтера, поэтому шаг кампании заполняется САМ:
    мастер режет тест в слайсере как обычно, а кампания получает печать вместе с её фото и
    таймлапсом. Шаг, у которого печать уже есть, не перетираем — вторая печать того же теста
    остаётся отдельной записью журнала (перепечатка после правки — нормальный ход калибровки).
    """
    e = await get_print_journal(jid)
    if not e:
        return {}
    key = calibration.detect_test(e.get("label") or "")
    if not key:
        return {}
    if (e.get("calib_type") or "") != key:
        await set_journal_calib(jid, calib_type=key)
    if e.get("calib_run_id"):
        return {"calib_type": key, "run_id": e["calib_run_id"], "step_id": e.get("calib_step_id")}
    run = await active_calib_run(e.get("plastic_id"))
    if not run:
        return {"calib_type": key}
    # Кампания заведена под конкретный пластик, а печать — заведомо другого: не притягиваем.
    # Иначе тест новой катушки PETG лёг бы в чужую кампанию по PLA только потому, что та
    # открыта. Печать с неизвестным пластиком (авто-запись журнала) сомнений не вызывает.
    if run.get("plastic_id") and e.get("plastic_id") and run["plastic_id"] != e["plastic_id"]:
        return {"calib_type": key}
    cur = await _conn().execute(
        "SELECT * FROM calib_steps WHERE run_id=? AND test_key=? ORDER BY sort, id LIMIT 1",
        (run["id"], key))
    step = await cur.fetchone()
    step_id = None
    if step:
        step_id = step["id"]
        if not step["journal_id"]:
            await update_calib_step(step_id, journal_id=int(jid),
                                    status="measured" if step["status"] == "todo" else step["status"])
    await set_journal_calib(jid, run_id=run["id"], step_id=step_id)
    return {"calib_type": key, "run_id": run["id"], "step_id": step_id}
