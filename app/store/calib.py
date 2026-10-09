"""Калибровка: кампании, шаги, замеры, образцы с фото, паспорта и снимки пресетов слайсера.

Хранилище всего калибровочного контура. Правила, разбор и работа с ФАЙЛАМИ пресетов —
в app/calibration.py (без БД), здесь только SQL.

* КАМПАНИИ (calib_runs) и их ШАГИ (calib_steps): add_calib_run сразу заводит шаги через
  seed_calib_steps — одна связка, режется только вместе.
* ЗАМЕРЫ (calib_measure) и ОБРАЗЦЫ с фото (calib_samples).
* ПАСПОРТА (calib_passport) — итог кампании, из которого печатается PDF и QR-этикетка.
* СНИМКИ ПРЕСЕТОВ (calib_presets): snapshot_calib_presets под ОДНИМ commit-ом вставляет
  изменившиеся по sha снимки и помечает пропавшие — режется только целиком.
* calib_overview — сводка для экрана калибровки.

Выделено из app/db.py волной 6 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ.

ИМЯ МОДУЛЯ — `calib`, а НЕ `calibration`: в db.py есть top-level `from . import calibration`
(app/calibration.py), и фасадная строка `from .store import calibration` дала бы F811 — та же
грабля, что `presets`/`settings`/`plates` в волнах 2–3.

ДОЛГ ВОЛНЫ 3 ЗАКРЫТ: `_calib_step_row` уехал в store/printjournal ещё волной 3 (за ним
потянулся мост журнал<->калибровка), поэтому list_calib_steps импортирует его оттуда явно,
а не через фасад db. Store->store перенос не делался: печатный журнал зовёт его сам.

Внутри вырезанного региона db.py лежали ШЕСТЬ однострочных фасадных маркеров волны 3
(куски калибровки, уехавшие в printjournal/printjob/printers). После выреза они легли бы
подряд без разделяющего кода — регион сведён в один фасадный блок волны 6, маркеры сняты.
"""
from __future__ import annotations

import json
import time

from .. import calibration
from ..profilepaths import abs_profile_str, rel_profile
from .core import _abs, _conn, _jload, _rel
from .printjournal import _calib_step_row


# ── Калибровка: кампании, шаги, замеры, образцы, паспорта, снимки пресетов ──────────────────
# Правила и работа с файлами пресетов — в app/calibration.py (без БД). Здесь только хранение.
async def add_calib_run(*, title: str, reason: str = "", printer_id: int | None = None,
                        nozzle: str = "0.4", plastic_id: int | None = None,
                        plastic_name: str = "", spool_id: int | None = None,
                        note: str = "", steps: list[str] | None = None,
                        started_at: int | None = None) -> int:
    """Новая калибровочная кампания + её шаги в правильном порядке.

    steps=None → набор по умолчанию (calibration.DEFAULT_STEPS). Порядок шагов зафиксирован
    реестром тестов: каждый следующий опирается на результат предыдущего.
    """
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO calib_runs(title, reason, printer_id, nozzle, plastic_id, plastic_name, "
        "spool_id, status, note, started_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
        ((title or "")[:120], (reason or "")[:200], printer_id, (nozzle or "0.4")[:12],
         plastic_id, (plastic_name or "")[:80], spool_id, (note or "")[:2000] or None,
         int(started_at or now)))
    rid = cur.lastrowid
    await _conn().commit()
    await seed_calib_steps(rid, steps)
    return rid


async def seed_calib_steps(run_id: int, keys: list[str] | None = None) -> int:
    """Досоздать недостающие шаги кампании (идемпотентно). Вернуть число созданных."""
    keys = list(keys or calibration.DEFAULT_STEPS)
    have = {r["test_key"] for r in await (await _conn().execute(
        "SELECT test_key FROM calib_steps WHERE run_id = ?", (int(run_id),))).fetchall()}
    now = int(time.time())
    made = 0
    for k in keys:
        t = calibration.test_info(k)
        if not t or k in have:
            continue
        await _conn().execute(
            "INSERT INTO calib_steps(run_id, test_key, sort, unit, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)", (int(run_id), t.key, t.order, t.unit, now, now))
        made += 1
    await _conn().commit()
    return made


async def list_calib_runs(limit: int = 50) -> list[dict]:
    cur = await _conn().execute(
        "SELECT r.*, (SELECT COUNT(*) FROM calib_steps s WHERE s.run_id = r.id) AS steps, "
        "(SELECT COUNT(*) FROM calib_steps s WHERE s.run_id = r.id AND s.status = 'done') "
        "AS steps_done FROM calib_runs r ORDER BY r.id DESC LIMIT ?", (int(limit),))
    return [dict(x) for x in await cur.fetchall()]


async def get_calib_run(rid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM calib_runs WHERE id = ?", (int(rid),))
    r = await cur.fetchone()
    return dict(r) if r else None


async def update_calib_run(rid: int, **fields) -> bool:
    allowed = {"title", "reason", "printer_id", "nozzle", "plastic_id", "plastic_name",
               "spool_id", "status", "note", "finished_at"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    if sets.get("status") == "done" and "finished_at" not in sets:
        sets["finished_at"] = int(time.time())
    cur = await _conn().execute(
        f"UPDATE calib_runs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), int(rid)))
    await _conn().commit()
    return cur.rowcount > 0


async def delete_calib_run(rid: int) -> None:
    """Удалить кампанию со всем содержимым. Записи журнала печатей НЕ трогаем — они сами по
    себе ценность, только отвязываем."""
    c = _conn()
    await c.execute("UPDATE print_journal SET calib_run_id=NULL, calib_step_id=NULL "
                    "WHERE calib_run_id=?", (int(rid),))
    for t in ("calib_steps", "calib_measure", "calib_samples"):
        await c.execute(f"DELETE FROM {t} WHERE run_id = ?", (int(rid),))
    await c.execute("DELETE FROM calib_runs WHERE id = ?", (int(rid),))
    await c.commit()


async def list_calib_steps(run_id: int) -> list[dict]:
    cur = await _conn().execute(
        "SELECT * FROM calib_steps WHERE run_id = ? ORDER BY sort, id", (int(run_id),))
    return [_calib_step_row(x) for x in await cur.fetchall()]


async def add_calib_measure(*, kind: str, data: dict, run_id: int | None = None,
                            step_id: int | None = None, label: str = "") -> dict:
    """Замер + вердикт правила. Правило считается ЗДЕСЬ и сохраняется вместе с числами:
    иначе через полгода останется значение без обоснования."""
    v = calibration.verdict(kind, data or {})
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO calib_measure(run_id, step_id, kind, label, data, value, unit, verdict, "
        "detail, rule, at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, step_id, (kind or "")[:40], (label or "")[:120],
         json.dumps(data or {}, ensure_ascii=False), v.get("value"), (v.get("unit") or "")[:16],
         (v.get("verdict") or "")[:120], (v.get("detail") or "")[:1000], (v.get("rule") or "")[:40],
         now))
    mid = cur.lastrowid
    await _conn().commit()
    out = dict(v)
    out["id"] = mid
    return out


async def list_calib_measures(run_id: int | None = None, step_id: int | None = None,
                              kind: str = "", limit: int = 200) -> list[dict]:
    q = "SELECT * FROM calib_measure WHERE 1=1"
    args: list = []
    if run_id:
        q += " AND run_id = ?"
        args.append(int(run_id))
    if step_id:
        q += " AND step_id = ?"
        args.append(int(step_id))
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    cur = await _conn().execute(q, args)
    return [dict(x) for x in await cur.fetchall()]


async def delete_calib_measure(mid: int) -> None:
    await _conn().execute("DELETE FROM calib_measure WHERE id = ?", (int(mid),))
    await _conn().commit()


async def save_calib_sample(*, sid: int | None = None, run_id: int | None = None,
                            step_id: int | None = None, journal_id: int | None = None,
                            code: str = "", title: str = "", proves: str = "", value: str = "",
                            place: str = "", note: str = "") -> int:
    """Карточка физического образца с полки. code — короткий шифр для этикетки с QR."""
    now = int(time.time())
    if sid:
        await _conn().execute(
            "UPDATE calib_samples SET run_id=?, step_id=?, journal_id=?, code=?, title=?, "
            "proves=?, value=?, place=?, note=? WHERE id=?",
            (run_id, step_id, journal_id, (code or "")[:24], (title or "")[:120],
             (proves or "")[:300], (value or "")[:60], (place or "")[:80],
             (note or "")[:500] or None, int(sid)))
        await _conn().commit()
        return int(sid)
    cur = await _conn().execute(
        "INSERT INTO calib_samples(run_id, step_id, journal_id, code, title, proves, value, "
        "place, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, step_id, journal_id, (code or "")[:24], (title or "")[:120],
         (proves or "")[:300], (value or "")[:60], (place or "")[:80],
         (note or "")[:500] or None, now))
    new_id = cur.lastrowid
    if not code:                       # шифр по умолчанию: C-<id> — короткий и не повторяется
        await _conn().execute("UPDATE calib_samples SET code=? WHERE id=?",
                              (f"C-{new_id}", new_id))
    await _conn().commit()
    return new_id


async def list_calib_samples(run_id: int | None = None, limit: int = 300) -> list[dict]:
    q = "SELECT * FROM calib_samples"
    args: list = []
    if run_id:
        q += " WHERE run_id = ?"
        args.append(int(run_id))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    cur = await _conn().execute(q, args)
    return [_sample_row(x) for x in await cur.fetchall()]


def _sample_row(r) -> dict:
    """Строка образца с абсолютным photo (C2): его отдают файлом по подписанному URL."""
    d = dict(r)
    if "photo" in d:
        d["photo"] = _abs(d["photo"])
    return d


async def get_calib_sample(sid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM calib_samples WHERE id = ?", (int(sid),))
    r = await cur.fetchone()
    return _sample_row(r) if r else None


async def set_calib_sample_photo(sid: int, path: str) -> None:
    await _conn().execute("UPDATE calib_samples SET photo = ? WHERE id = ?",
                          (_rel(path), int(sid)))
    await _conn().commit()


async def delete_calib_sample(sid: int) -> None:
    await _conn().execute("DELETE FROM calib_samples WHERE id = ?", (int(sid),))
    await _conn().commit()


async def save_calib_passport(*, plastic_id: int, printer_id: int | None, nozzle: str = "0.4",
                              values: dict | None = None, source: dict | None = None,
                              presets_map: dict | None = None, status: str = "calibrated",
                              run_id: int | None = None, spool_id: int | None = None,
                              note: str = "", calibrated_at: int | None = None,
                              plate_id: int | None = None, z_offset: float | None = None) -> int:
    """Создать/обновить паспорт филамента для пары (пластик, принтер, сопло).

    plate_id/z_offset (1028ab67) — пластина, на которой калибровались, и её вылет для ЭТОГО
    филамента: Z-offset живёт в принтере, а не в пресетах, и иначе нигде не сохранялся."""
    now = int(time.time())
    key = (int(plastic_id), printer_id, (nozzle or "0.4")[:12])
    cur = await _conn().execute(
        "SELECT id FROM calib_passport WHERE plastic_id=? AND printer_id IS ? AND nozzle=?", key)
    row = await cur.fetchone()
    # Пути пресетов в паспорте — те же профили слайсера, что и в карточках: храним маркером
    # корня, иначе «применить паспорт» на чужой машине вернёт written=[] молча (set_preset_value
    # отдаёт False, а обработчик ошибкой это не считает).
    presets_map = {k: (rel_profile(v) if isinstance(v, str) else v)
                   for k, v in (presets_map or {}).items()}
    payload = (json.dumps(values or {}, ensure_ascii=False),
               json.dumps(source or {}, ensure_ascii=False),
               json.dumps(presets_map, ensure_ascii=False),
               (status or "calibrated")[:20], run_id, spool_id, (note or "")[:1000] or None,
               plate_id or None, z_offset, int(calibrated_at or now), now)
    if row:
        await _conn().execute(
            "UPDATE calib_passport SET values_json=?, source_json=?, presets_json=?, status=?, "
            "run_id=?, spool_id=?, note=?, plate_id=?, z_offset=?, calibrated_at=?, updated_at=? "
            "WHERE id=?",
            (*payload, row["id"]))
        await _conn().commit()
        return int(row["id"])
    cur = await _conn().execute(
        "INSERT INTO calib_passport(plastic_id, printer_id, nozzle, values_json, source_json, "
        "presets_json, status, run_id, spool_id, note, plate_id, z_offset, calibrated_at, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*key, *payload))
    pid = cur.lastrowid
    await _conn().commit()
    return pid


def _passport_row(r) -> dict:
    d = dict(r)
    for src, dst in (("values_json", "values"), ("source_json", "source"),
                     ("presets_json", "presets")):
        d[dst] = _jload(d.pop(src), {})
    if isinstance(d.get("presets"), dict):
        d["presets"] = {k: (abs_profile_str(v) if isinstance(v, str) else v)
                        for k, v in d["presets"].items()}
    return d


async def list_calib_passports() -> list[dict]:
    # plate/plate_z — имя пластины и ЕЁ базовый вылет: в карточке показываем собственный
    # z_offset паспорта, а когда его нет — значение пластины (и честно помечаем, чьё оно).
    cur = await _conn().execute(
        "SELECT p.*, pl.name AS plastic, pl.material_type, pr.name AS printer, "
        "       cp.name AS plate, cp.z_offset AS plate_z "
        "FROM calib_passport p LEFT JOIN plastics pl ON pl.id = p.plastic_id "
        "LEFT JOIN printers pr ON pr.id = p.printer_id "
        "LEFT JOIN calib_plates cp ON cp.id = p.plate_id ORDER BY pl.name, p.nozzle")
    return [_passport_row(r) for r in await cur.fetchall()]


async def get_calib_passport(plastic_id: int, printer_id: int | None = None,
                             nozzle: str = "0.4") -> dict | None:
    cur = await _conn().execute(
        "SELECT * FROM calib_passport WHERE plastic_id=? AND printer_id IS ? AND nozzle=?",
        (int(plastic_id), printer_id, (nozzle or "0.4")[:12]))
    r = await cur.fetchone()
    return _passport_row(r) if r else None


async def get_calib_passport_by_id(pid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM calib_passport WHERE id = ?", (int(pid),))
    r = await cur.fetchone()
    return _passport_row(r) if r else None


async def delete_calib_passport(pid: int) -> None:
    await _conn().execute("DELETE FROM calib_passport WHERE id = ?", (int(pid),))
    await _conn().commit()


async def snapshot_calib_presets(items: list[dict]) -> dict:
    """Сохранить снимки пресетов, пишем ТОЛЬКО изменившиеся (по sha).

    Возвращает {new, changed, gone}: changed — пресет правился с прошлого снимка (это и есть
    история калибровки), gone — файл пропал (обновление слайсера сбросило пользовательский
    пресет); тело последнего снимка остаётся в БД и восстанавливается кнопкой.
    """
    now = int(time.time())
    seen: set[str] = set()
    new = changed = 0
    for it in items or []:
        # Ключ идентичности снимка. Нормализуем ОБА конца одинаково (здесь и в чтении):
        # смешение форматов в одной таблице раздвоило бы историю пресета — по path
        # группируются версии и вычисляется «пресет пропал».
        path = rel_profile(str(it.get("path") or "")) or ""
        if not path:
            continue
        seen.add(path)
        cur = await _conn().execute(
            "SELECT sha, gone FROM calib_presets WHERE path=? ORDER BY id DESC LIMIT 1", (path,))
        prev = await cur.fetchone()
        if prev and prev["sha"] == it.get("sha") and not prev["gone"]:
            continue
        await _conn().execute(
            "INSERT INTO calib_presets(kind, name, path, sha, body, mtime, gone, at) "
            "VALUES(?,?,?,?,?,?,0,?)",
            ((it.get("kind") or "")[:20], (it.get("name") or "")[:120], path,
             (it.get("sha") or "")[:32], it.get("body") or "", int(it.get("mtime") or 0), now))
        if prev:
            changed += 1
        else:
            new += 1
    # пропавшие: были в истории, сейчас файла нет — отмечаем «исчез» ОДИН раз
    cur = await _conn().execute(
        "SELECT path, kind, name, sha FROM calib_presets WHERE id IN "
        "(SELECT MAX(id) FROM calib_presets GROUP BY path) AND gone = 0")
    gone = 0
    for r in await cur.fetchall():
        if r["path"] in seen:
            continue
        await _conn().execute(
            "INSERT INTO calib_presets(kind, name, path, sha, body, mtime, gone, at) "
            "VALUES(?,?,?,?,'',0,1,?)", (r["kind"], r["name"], r["path"], r["sha"], now))
        gone += 1
    await _conn().commit()
    return {"new": new, "changed": changed, "gone": gone}


async def list_calib_preset_latest() -> list[dict]:
    """Последний снимок каждого пресета + сколько всего версий накоплено."""
    cur = await _conn().execute(
        "SELECT p.*, (SELECT COUNT(*) FROM calib_presets q WHERE q.path = p.path) AS versions "
        "FROM calib_presets p WHERE p.id IN (SELECT MAX(id) FROM calib_presets GROUP BY path) "
        "ORDER BY p.kind, p.name")
    return [_preset_row(r) for r in await cur.fetchall()]


def _preset_row(r) -> dict:
    """Снимок наружу: path — абсолютным, как его видит мастер и как его отдаёт
    calibration.list_presets (web/calib.py сшивает снимки БД с диском ПО СТРОКЕ пути, и разные
    формы означали бы «все пресеты разом пропали»)."""
    d = dict(r)
    if isinstance(d.get("path"), str):
        d["path"] = abs_profile_str(d["path"])
    return d


async def list_calib_preset_history(path: str, limit: int = 40) -> list[dict]:
    cur = await _conn().execute(
        "SELECT * FROM calib_presets WHERE path = ? ORDER BY id DESC LIMIT ?",
        (rel_profile(str(path)) or "", int(limit)))
    return [_preset_row(r) for r in await cur.fetchall()]


async def get_calib_preset_snapshot(sid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM calib_presets WHERE id = ?", (int(sid),))
    r = await cur.fetchone()
    return _preset_row(r) if r else None


async def calib_overview() -> dict:
    """Сводка для экрана калибровки: кампании, паспорта, образцы, снимки, неразобранные печати."""
    c = _conn()
    runs = (await (await c.execute("SELECT COUNT(*) AS n FROM calib_runs")).fetchone())["n"]
    active = (await (await c.execute(
        "SELECT COUNT(*) AS n FROM calib_runs WHERE status='active'")).fetchone())["n"]
    passports = (await (await c.execute("SELECT COUNT(*) AS n FROM calib_passport")).fetchone())["n"]
    samples = (await (await c.execute("SELECT COUNT(*) AS n FROM calib_samples")).fetchone())["n"]
    snaps = (await (await c.execute(
        "SELECT COUNT(DISTINCT path) AS n FROM calib_presets")).fetchone())["n"]
    prints = (await (await c.execute(
        "SELECT COUNT(*) AS n FROM print_journal WHERE COALESCE(calib_type,'') != ''")).fetchone())["n"]
    loose = (await (await c.execute(
        "SELECT COUNT(*) AS n FROM print_journal WHERE COALESCE(calib_type,'') != '' "
        "AND calib_run_id IS NULL")).fetchone())["n"]
    cur = await c.execute(
        "SELECT calib_type, COUNT(*) AS n FROM print_journal WHERE COALESCE(calib_type,'') != '' "
        "GROUP BY calib_type ORDER BY n DESC")
    by_type = [{"key": r["calib_type"], "n": r["n"]} for r in await cur.fetchall()]
    return {"runs": runs, "active": active, "passports": passports, "samples": samples,
            "presets": snaps, "prints": prints, "loose": loose, "by_type": by_type}
