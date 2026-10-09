"""Этап 0 автотюнинга параметров печати (docs/Перспективные разработки/ai-auto-tuning.md).

ЧТО ЭТО. Копилка исходов: «какими ручками печатали → чем кончилось». Ничего не
рекомендует, ничего не предвыбирает, UX выбора не трогает — этапы 1–3 плана за рамками.
Задача модуля ровно одна: чтобы через полгода было на чём проверять правила, а владелец
видел, сколько данных уже накоплено.

ПОЧЕМУ БЕЗ НОВОЙ ТАБЛИЦЫ. Схема под §3 плана уже существует и пустует: миграция
`print_samples_merged` (app/db.py) добавила в print_journal весь богатый набор колонок —
layer_height, infill_pct, walls, volume_cm3, bbox_x/y/z, bbox_volume, parts, process_name,
material_type, model_hash, printer_id, extra_colors. Ни одна из них ни разу не
заполнялась, потому что единственный писатель (_capture_print_sample → record_time_sample)
заточен под пару «оценка времени → факт» и выходит РАНЬШЕ, если факта времени нет. Этап 0
= снять эту зависимость (печать без замера времени — всё равно годный пример), дописать
замороженные параметры в ту же строку журнала и научиться читать их обратно. Второй копии
данных не заводим: исход (rating/review/result/fail_pct) живёт в requests/print_journal,
меняется во времени и берётся JOIN'ом на чтении.

ПОЧЕМУ ЗАМОРАЖИВАЕМ ЧИСЛАМИ. quality_presets/strength_presets — редактируемые мастером
строки без истории (пресет «0.28 мм» уже отключён, «Золотая середина» заведена задним
числом). Датасет, восстанавливающий layer_height/infill/walls JOIN'ом по quality_id на
момент ЧТЕНИЯ, описывал бы сегодняшние настройки, а не применённые. Поэтому числа кладём
в строку журнала в момент, когда печать состоялась.

ШКАЛА ОЦЕНКИ — 1–10 (requests.rating; миграция bdcc18e4 разово удвоила старые 1–5). В §3
плана было написано «1–5» — это ошибка плана, исправлена вместе с этим кодом. Никакой
молчаливой нормализации здесь нет: отдаём сырое значение, приводить будет этап 1.

ГДЕ СНИМАЕТСЯ. На переходе заказа в «напечатано» (app/web/ordermoney.py и confirm_print в
app/webapp.py) — это НЕ горячий путь клиента: расчёт цены модуль не трогает вовсе. Плюс
идемпотентный добор истории раз в сутки из app/maintenance.py.

ИДЕМПОТЕНТНОСТЬ. Пишем ТОЛЬКО в пустые поля (идиома record_time_sample) и всегда в ОДНУ И
ТУ ЖЕ строку журнала заказа — первую по id (MIN(id)), тем же правилом, что и
record_time_sample «ORDER BY id LIMIT 1». У многостольного заказа строк несколько, и
выбери сборщик другую — повторный прогон создал бы дубль примера молча.

ЕДИНИЦА ВЫБОРКИ — ЗАКАЗ, а не строка журнала: исход (оценка клиента) у заказа один, а
столов может быть семь. ЗНАМЕНАТЕЛИ РАЗВЕДЕНЫ ПО ИМЕНАМ, а не смешаны в одной строке:
`volume_cm3_per_unit` — на ОДИН экземпляр заказа (комплект внутри экземпляра может быть из
нескольких файлов), `bbox_scene_*` — bbox самой крупной СЦЕНЫ (файла или стола) из
requests.dims, а вовсе НЕ детали, `dims_groups` — сколько таких сцен в строке габаритов,
`*_order` — на ВЕСЬ заказ, просуммированный по всем столам журнала (граммы, минуты).
Те же bbox лежат и в колонках print_journal.bbox_x/y/z/bbox_volume: там это тоже СЦЕНА
(preview.model_dims: «строка идёт ПО СТОЛАМ»), и подписывать их «деталью» нельзя — этап 1
будет учиться по имени признака. Раньше объём брался с заказа, а граммы — с первой
строки журнала, и у многостольного заказа расходились в разы. Заказ, чья печать в журнал не попала вовсе, из выборки не
выбрасываем: параметры восстанавливаем по сегодняшним пресетам и ставим
params_source='presets' — этап 1 сам решит, доверять такой строке или нет. Замороженное
на момент печати помечено params_source='frozen'.

МАТЕРИАЛ ЧИТАЕМ ЧЕРЕЗ СПРАВОЧНИК. В `print_journal.material_type` до нас пишет
`record_time_sample`, и туда попадало сырое `plastic_name` ВМЕСТЕ С ЦВЕТОМ («Kingroon PETG ·
Хаки»). Починено с двух концов: источник (`_capture_print_sample`) теперь подбирает
material_type тем же префиксным матчем, что и мы, а на чтении и витрина, и датасет берут
СПРАВОЧНИК ПЕРВЫМ (чужие исторические строки уже засорены, и перетирать их мы не вправе).
ПОБОЧНЫЙ ЭФФЕКТ, о котором надо знать: material_type — колонка ОБЩАЯ с предсказанием времени
(timepred._journal_ratios фильтрует по ней выборку predict_minutes). Пока заполненных строк
меньше min_samples, предсказание падает на общий пул и медиана не меняется; по мере роста
выборки группировка по материалу станет точнее — это улучшение, а не регресс, но искать причину
сдвига фактора потом будут здесь.

ЧЕГО В ДАННЫХ НЕТ. `parts` (число деталей) знает только fastcalc в момент расчёта и никуда
не сохраняет — колонку оставляем пустой, врать не будем. `intent` (сценарий
использования) не существует ни как колонка, ни как шаг FSM: в строке датасета он всегда
None, чтобы этап 1 дописал его без смены формата.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import db

log = logging.getLogger(__name__)

#: Шкала клиентской оценки (requests.rating). Не 1–5, как было написано в плане.
RATING_MAX = 10

#: Колонки print_journal, которые замораживает этап 0. model_hash/extra_colors не наши —
#: их пишет record_time_sample, и перетирать их отсюда незачем.
FEATURE_FIELDS = ("layer_height", "infill_pct", "walls", "volume_cm3",
                  "bbox_x", "bbox_y", "bbox_z", "bbox_volume", "process_name",
                  "material_type")

#: Единица выборки — ЗАКАЗ, а не строка журнала: у многостольного заказа строк несколько,
#: а исход (оценка клиента) один. Журнал подцепляем LEFT JOIN'ом и всегда ПЕРВОЙ строкой
#: (MIN(id)) — тем же правилом, что и record_time_sample. Через JOIN по строке журнала
#: выборка на боевой БД теряла бы 10 заказов из 26: у части печать в журнал не заносилась.
_DATASET_FROM = (
    "FROM requests r "
    "LEFT JOIN print_journal j ON j.id = "
    "  (SELECT MIN(id) FROM print_journal WHERE request_id = r.id) "
    "LEFT JOIN quality_presets qp ON qp.id = r.quality_id "
    "LEFT JOIN strength_presets sp ON sp.id = r.strength_id "
    "WHERE r.status IN ('printed','handed') "
    "AND r.quality_id IS NOT NULL AND r.strength_id IS NOT NULL")

#: Материал для разбивки витрины, пока он не заморожен в журнале: справочник пластиков по
#: САМОМУ ДЛИННОМУ имени-префиксу (в заказе к имени приклеен цвет). Именно подзапросом, а
#: не LEFT JOIN'ом: префиксов у одного имени бывает несколько («Elegoo PLA» и «Elegoo PLA
#: Matte»), и join размножил бы строки, завысив счётчики витрины.
#: Сравнение НЕ через LIKE: в LIKE символы _ и % внутри plastics.name («PLA_Basic») стали бы
#: шаблоном, а сам LIKE регистронезависим — и SQL расходился бы с питон-двойником
#: _plastic_of (str.startswith, буквальный и регистрозависимый). substr совпадает с ним точно.
_PL_MATERIAL = ("(SELECT p2.material_type FROM plastics p2 "
                "WHERE p2.name <> '' "
                "AND substr(r.plastic_name, 1, LENGTH(p2.name)) = p2.name "
                "ORDER BY LENGTH(p2.name) DESC LIMIT 1)")


#: Плотность из того же справочника и тем же префиксным правилом — нужна, чтобы у заказа
#: без строки журнала посчитать объём на чтении (иначе признак терялся у трети выборки).
_PL_DENSITY = ("(SELECT p2.density FROM plastics p2 "
               "WHERE p2.name <> '' "
               "AND substr(r.plastic_name, 1, LENGTH(p2.name)) = p2.name "
               "ORDER BY LENGTH(p2.name) DESC LIMIT 1)")


def _dataset_from() -> tuple[str, list]:
    """FROM+WHERE выборки вместе с параметрами. Своих печатей мастера в выборке быть не
    должно, и критерий берётся БУКВАЛЬНО тот же, что у db.is_self_order — его собственный
    SQL-двойник db._self_print_predicate(). Своя копия условия (по колонке self_print)
    расходилась с питоном на заказе self_print=1/status='printed': признаки в него писались,
    а в выборку он не попадал никогда."""
    pred, args = db._self_print_predicate()
    # Предикат написан НЕквалифицированными именами (status, user_id) под однотабличный
    # запрос, а у нас FROM из четырёх таблиц. Не вклеиваем его в общее WHERE (в день,
    # когда в print_journal заведут колонку status, SQLite ответил бы «ambiguous column
    # name», а stats() глушит исключения и витрина молча показала бы нули) — запираем
    # его в подзапрос по одной таблице requests, где имена однозначны.
    return f"{_DATASET_FROM} AND r.id NOT IN (SELECT id FROM requests WHERE {pred})", list(args)

_DIMS_RE = re.compile(r"([\d.,]+)\s*[x×хX]\s*([\d.,]+)\s*[x×хX]\s*([\d.,]+)")


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parse_all_dims(dims: str | None) -> list[tuple[float, float, float]]:
    """ВСЕ тройки габаритов из текстовой строки заказа. У многодетального заказа их несколько
    через « · » («58×118×28 · 163×375×36 мм»), и брать молча первую — врать про размер."""
    out: list[tuple[float, float, float]] = []
    for m in _DIMS_RE.finditer(str(dims or "")):
        vals = [_num(x) for x in m.groups()]
        if not any(v is None for v in vals):
            out.append((vals[0], vals[1], vals[2]))  # type: ignore[arg-type]
    return out


def parse_dims(dims: str | None) -> tuple[float, float, float] | None:
    """Габариты САМОЙ КРУПНОЙ СЦЕНЫ заказа — тройки в requests.dims пишет preview.model_dims,
    и это bbox файла/СТОЛА целиком («строка идёт по столам»), а НЕ отдельной детали.
    Крупнейшая, а не первая: печатность заказа определяет она. Мусор и пустое → None."""
    all_ = parse_all_dims(dims)
    if not all_:
        return None
    return max(all_, key=lambda d: d[0] * d[1] * d[2])


async def _plastic_of(plastic_name: str):
    """Пластик заказа по имени с ЦВЕТОМ («Kingroon PETG · Белый»). Точный поиск первым,
    иначе самое длинное имя-префикс из справочника: цвет приклеен к имени, и на боевых
    данных префиксный матч разбирает все заказы до одного."""
    if not plastic_name:
        return None
    pl = await db.get_plastic_by_name(plastic_name)
    if pl is not None:
        return pl
    best = None
    for p in await db.list_plastics():
        nm = (p.name or "").strip()
        if nm and plastic_name.startswith(nm) and (best is None or len(nm) > len(best.name)):
            best = p
    return best


async def material_of(plastic_name: str | None) -> str | None:
    """material_type справочника по имени пластика ЗАКАЗА (имя несёт ещё и цвет).

    Публичный, потому что этим же должен пользоваться старый писатель журнала
    (_capture_print_sample → record_time_sample): точный db.get_plastic_by_name на боевых
    именах не срабатывает, и в print_journal.material_type уезжало «Kingroon PETG · Хаки».
    """
    pl = await _plastic_of(str(plastic_name or ""))
    return ((getattr(pl, "material_type", "") or "").strip() or None) if pl else None


async def order_features(r) -> dict:
    """Признаки §3 плана, снятые с заказа В МОМЕНТ печати. Пустые значения не возвращаем —
    так вызывающий не затрёт ими уже известное."""
    out: dict[str, Any] = {}
    qid, sid = getattr(r, "quality_id", None), getattr(r, "strength_id", None)
    if qid:
        q = await db.get_quality(int(qid))
        if q is not None:
            if _num(q.layer_height):
                out["layer_height"] = float(q.layer_height)
            if (q.process or "").strip():
                out["process_name"] = str(q.process).strip()
    if sid:
        s = await db.get_strength(int(sid))
        if s is not None:
            if int(s.infill or 0) > 0:
                out["infill_pct"] = int(s.infill)
            if int(s.walls or 0) > 0:
                out["walls"] = int(s.walls)
    qty = max(int(getattr(r, "quantity", 1) or 1), 1)
    grams_u = _num(float(getattr(r, "grams_total", 0) or 0) / qty)
    # Материал — разрешённый material_type (plastic_name несёт ещё и цвет: «Kingroon PETG ·
    # Белый»), объём — производная grams/density (идиома calcservice), отдельно его никто
    # не хранит.
    pl = await _plastic_of(str(getattr(r, "plastic_name", "") or ""))
    mat = (getattr(pl, "material_type", "") or "").strip() if pl else ""
    if mat:
        out["material_type"] = mat
    # Материал не распознан — поле оставляем ПУСТЫМ. Сырое plastic_name сюда класть нельзя:
    # оно несёт ещё и цвет («Elegoo PLA Matte · Бежевый»), и разбивка витрины рассыпалась бы
    # в синглтоны — ровно это делает старый фолбэк в _capture_print_sample.
    dens = _num(getattr(pl, "density", None)) if pl else None
    if grams_u and dens:
        out["volume_cm3"] = round(grams_u / dens, 3)
    # В bbox_* журнала кладём габариты САМОЙ КРУПНОЙ СЦЕНЫ (файла/стола) — ровно то, что
    # пишет в requests.dims preview.model_dims. Это НЕ габарит одной детали, и читатель
    # (build_dataset) называет их bbox_scene_*, чтобы этап 1 не принял одно за другое.
    dims = parse_dims(getattr(r, "dims", ""))
    if dims:
        out["bbox_x"], out["bbox_y"], out["bbox_z"] = dims
        out["bbox_volume"] = round(dims[0] * dims[1] * dims[2], 3)
    return {k: v for k, v in out.items() if v not in (None, "")}


async def capture_features(r) -> int | None:
    """Заморозить признаки заказа в его строку журнала. Best-effort и НИКОГДА не бросает:
    зовётся из горячей ветки «напечатано», где падение стоило бы мастеру статуса заказа.

    Возвращает id обогащённой строки журнала либо None (нечего писать / нечего обогащать).
    Идемпотентно: заполняем только пустые поля первой строки журнала заказа."""
    try:
        rid = int(getattr(r, "id", 0) or 0)
        if not rid or db.is_self_order(r):
            return None            # свои печати мастера идут отдельным классом, не в выборку
        feats = await order_features(r)
        if not feats:
            return None
        rows = await db.list_print_journal_for_request(rid)
        if not rows:
            return None            # печати в журнале нет — обогащать нечего (строку не плодим)
        row = rows[0]              # ровно то же правило выбора, что у record_time_sample
        sets = {k: v for k, v in feats.items() if k in FEATURE_FIELDS and not row.get(k)}
        if not sets:
            return None
        await db.set_print_journal_fields(int(row["id"]), **sets)
        return int(row["id"])
    except Exception:  # noqa: BLE001
        log.debug("tuning: не снять признаки заказа #%s", getattr(r, "id", "?"), exc_info=True)
        return None


async def backfill_features(limit: int = 500) -> int:
    """Добрать признаки по историческим печатям (образец backfill_missing_previews).

    Идемпотентно и самоограничено: берём только заказы, у чьей строки журнала признаки ещё
    пусты. Пресеты с тех пор могли поправить — ретроспективные числа приблизительны, и
    именно поэтому добор разовый по каждой строке, а не пересчёт при каждом проходе."""
    try:
        frm, args = _dataset_from()
        cur = await db._conn().execute(
            f"SELECT r.id AS rid {frm} "
            "AND j.id IS NOT NULL AND j.layer_height IS NULL ORDER BY r.id DESC LIMIT ?",
            (*args, int(limit)))
        rids = [int(x["rid"]) for x in await cur.fetchall()]
    except Exception:  # noqa: BLE001
        log.debug("tuning: выборка для добора не удалась", exc_info=True)
        return 0
    fixed = 0
    for rid in rids:
        r = await db.get_request(rid)
        if r is not None and await capture_features(r):
            fixed += 1
    if fixed:
        log.info("Автотюнинг, этап 0: дозаполнено примеров — %d", fixed)
    return fixed


def _outcome(row: Any) -> str:
    """Исход примера. Строку без оценки НЕ выбрасываем — она полезна как «исход
    неизвестен», но помечена, чтобы этап 1 не принял её за молчаливое одобрение."""
    if row["rating"]:
        return "rated"
    if (row["result"] or "") == "fail" or float(row["fail_pct"] or 0) > 0:
        return "failed"
    # graded=1 — ещё и МОЛЧАЛИВЫЙ ДЕФОЛТ колонки (db.py: _ensure_column ... DEFAULT 1,
    # add_print_journal(graded=1)), поэтому одной единицы мало: исход считаем подтверждённым
    # мастером, только когда он вписан явно (result).
    if row["graded"] and (row["result"] or ""):
        return "graded"
    return "unknown"


async def build_dataset(limit: int = 1000) -> list[dict]:
    """Выборка этапа 0 — ПРОИЗВОДНАЯ, а не вторая копия данных: один read-only JOIN
    журнала с заказом. Строка = один выполненный заказ: применённые ручки (замороженные),
    признаки модели, исход. Тяжёлое место, в /api/admin/status ему делать нечего.

    ЗНАМЕНАТЕЛИ НАЗВАНЫ ЯВНО: `volume_cm3_per_unit` — на ОДИН экземпляр заказа (граммы/
    количество/плотность; комплект в экземпляре может быть из нескольких файлов),
    `bbox_scene_*` — bbox самой крупной СЦЕНЫ из requests.dims (файл или стол целиком, НЕ
    деталь), `dims_groups` — сколько таких сцен перечислено в строке габаритов,
    `grams_order`/`minutes_order`/
    `est_minutes_order` — на ВЕСЬ заказ суммой по всем столам журнала (`journal_rows`).
    Смешивать их в одной строке нельзя: у заказа #136 первый стол — 102 г из 173 г заказа.

    Материал берём справочником по имени пластика и лишь потом полем журнала: туда
    record_time_sample писал имя ВМЕСТЕ С ЦВЕТОМ, и датасет расходился с витриной.
    """
    frm, args = _dataset_from()
    cur = await db._conn().execute(
        "SELECT r.id AS request_id, j.id AS journal_id, r.created_at, "
        f"COALESCE(NULLIF({_PL_MATERIAL},''), NULLIF(j.material_type,'')) AS material_type, "
        "j.layer_height, j.infill_pct, j.walls, j.volume_cm3, "
        "j.bbox_x, j.bbox_y, j.bbox_z, j.bbox_volume, j.parts, j.process_name, j.printer_id, "
        "j.result, j.fail_pct, j.graded, j.model_hash, j.extra_colors, "
        "(SELECT SUM(grams) FROM print_journal WHERE request_id = r.id) AS grams_order, "
        "(SELECT SUM(minutes) FROM print_journal WHERE request_id = r.id) AS minutes_order, "
        "(SELECT SUM(est_minutes) FROM print_journal WHERE request_id = r.id) AS est_order, "
        "(SELECT COUNT(*) FROM print_journal WHERE request_id = r.id) AS journal_rows, "
        f"{_PL_DENSITY} AS pl_density, "
        "qp.layer_height AS qp_layer, qp.process AS qp_process, sp.infill AS sp_infill, "
        "sp.walls AS sp_walls, r.grams_total, r.dims, "
        "r.quality_id, r.strength_id, r.quantity, r.plates, r.plastic_name, r.rating, r.review, "
        "(SELECT COUNT(*) FROM requests r2 WHERE r2.hash <> '' AND r2.hash = r.hash) AS same_model "
        f"{frm} ORDER BY r.id LIMIT ?", (*args, int(limit)))
    out: list[dict] = []
    for row in await cur.fetchall():
        # Замороженное на момент печати — истина. Если заморозить не успели (печать в журнал
        # не заносилась вовсе), подставляем СЕГОДНЯШНИЕ пресеты и честно помечаем
        # params_source='presets': строки пресетов мутабельны и историю не хранят. Тем же
        # правилом добираем объём и габариты — считаются они из полей самого заказа, которые
        # не менялись, поэтому терять их у заказов без журнала незачем.
        frozen = row["layer_height"] is not None
        dims_all = parse_all_dims(row["dims"])
        box = max(dims_all, key=lambda d: d[0] * d[1] * d[2]) if dims_all else None
        vol = row["volume_cm3"]
        if vol is None:
            qty = max(int(row["quantity"] or 1), 1)
            grams_u = _num(float(row["grams_total"] or 0) / qty)
            dens = _num(row["pl_density"])
            vol = round(grams_u / dens, 3) if (grams_u and dens) else None
        # Габариты считаем ИЗ ТЕКСТА ЗАКАЗА даже поверх замороженных: источник (requests.dims)
        # не мутабелен, в отличие от пресетов, зато у первых замороженных строк туда попала
        # ПЕРВАЯ тройка многодетальной строки вместо самой крупной.
        bbox = box
        if bbox is None and row["bbox_x"] is not None:
            bbox = (row["bbox_x"], row["bbox_y"], row["bbox_z"])
        out.append({
            "journal_id": int(row["journal_id"]) if row["journal_id"] else None,
            "journal_rows": int(row["journal_rows"] or 0),
            "request_id": int(row["request_id"]),
            "created_at": int(row["created_at"] or 0),
            "material_type": row["material_type"] or None,
            "quality_id": row["quality_id"],
            "layer_height": row["layer_height"] if frozen else row["qp_layer"],
            "process_name": (row["process_name"] if frozen else row["qp_process"]) or None,
            "strength_id": row["strength_id"],
            "infill_pct": row["infill_pct"] if frozen else row["sp_infill"],
            "walls": row["walls"] if frozen else row["sp_walls"],
            "params_source": "frozen" if frozen else "presets",
            "volume_cm3_per_unit": vol,
            "grams_order": row["grams_order"], "minutes_order": row["minutes_order"],
            "est_minutes_order": row["est_order"],
            "bbox_scene_x": bbox[0] if bbox else None,
            "bbox_scene_y": bbox[1] if bbox else None,
            "bbox_scene_z": bbox[2] if bbox else None,
            "bbox_scene_volume": (round(bbox[0] * bbox[1] * bbox[2], 3) if bbox else None),
            "parts": row["parts"],           # не собирается ничем (см. шапку модуля)
            "dims_groups": len(dims_all) or None,   # сцен в строке габаритов, НЕ деталей
            "plates": row["plates"], "quantity": row["quantity"],
            "extra_colors": row["extra_colors"],
            "printer_id_first": row["printer_id"],   # машина ПЕРВОГО стола, не всего заказа
            "rating": row["rating"], "rating_max": RATING_MAX, "review": row["review"] or None,
            "result": row["result"] or None, "fail_pct": row["fail_pct"],
            "graded": int(row["graded"] or 0),
            # признак «повтор той же модели» — вычисляемый и приблизительный: своей колонки
            # под передел в базе нет вовсе.
            "repeat_model": int(row["same_model"] or 0) > 1,
            "intent": None,                  # уровень A, этап 1 — сегодня не спрашиваем
            "outcome": _outcome(row),
        })
    return out


async def stats() -> dict:
    """Счётчик для витрины владельца: сколько исходов накоплено и сколько из них с оценкой.

    НИКОГДА не бросает: уезжает в общий /api/admin/status, и исключение погасило бы
    владельцу весь экран «Статус» (та же доктрина, что у license.status)."""
    out: dict[str, Any] = {"count": 0, "rated": 0, "with_params": 0, "by_material": []}
    try:
        c = db._conn()
        frm, args = _dataset_from()
        cur = await c.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN r.rating > 0 THEN 1 ELSE 0 END) AS rated, "
            f"SUM(CASE WHEN j.layer_height IS NOT NULL THEN 1 ELSE 0 END) AS params {frm}", args)
        row = await cur.fetchone()
        if row is None:
            return out
        out["count"] = int(row["n"] or 0)
        out["rated"] = int(row["rated"] or 0)
        out["with_params"] = int(row["params"] or 0)
        # Группируем ТОЛЬКО по material_type: plastic_name несёт ещё и цвет, и разбивка
        # рассыпалась бы в синглтоны.
        cur = await c.execute(
            # справочник ПЕРВЫМ: в журнале у старых строк лежит имя с цветом (фолбэк
            # _capture_print_sample), и разбивка от него рассыпалась бы в синглтоны
            f"SELECT COALESCE(NULLIF({_PL_MATERIAL},''), NULLIF(j.material_type,''),'—') AS m, "
            "COUNT(*) AS n, SUM(CASE WHEN r.rating > 0 THEN 1 ELSE 0 END) AS rated "
            f"{frm} "
            "GROUP BY m ORDER BY n DESC, m LIMIT 8", args)
        out["by_material"] = [{"material": x["m"], "count": int(x["n"] or 0),
                               "rated": int(x["rated"] or 0)} for x in await cur.fetchall()]
    except Exception:  # noqa: BLE001
        log.debug("tuning stats failed", exc_info=True)
    return out
