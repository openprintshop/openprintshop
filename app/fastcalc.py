"""Аналитическая оценка цены БЕЗ слайсера — площадь-осознанная модель v2 (17.07.2026).

ЗАЧЕМ. Слайс — самое медленное место расчёта (секунды-минуты), а на koala-классе может лечь.
Здесь мы оцениваем объём филамента (см³) и время печати (мин) прямо из геометрии и настроек
печати и отдаём их в ту же pricing.compute_price — как мгновенную ≈цену и как фолбэк, когда
слайсер лёг.

ЧТО НОВОГО ПРОТИВ v1 (куб-эквивалент). Старая модель знала только объём тела и приближала
деталь кубом — оболочка на реальных моделях занижалась в разы (медиана ошибки против честного
слайса ~61%, bbox-фолбэк давал до ×10). Теперь app.fastmeasure меряет реальную ПЛОЩАДЬ
поверхности с разложением по наклону нормали, союз-объём (самопересечения print-in-place не
двоятся), «воздух под телом» (поддержки) — и модель собирает филамент из физичных слагаемых:

  стенки   = k_wall · A_бок · t_wall,  t_wall = w_outer + (walls−1)·w_inner
  корки    = k_skin · (A_проекц.верх · top·h + A_проекц.низ · bottom·h)
  оболочка = мягкий min(V, стенки+корки)  (насыщение p_sat: тонкие детали → почти сплошняк)
  инфилл   = rho_eff · (V − оболочка);  rho_eff растёт до 1 в «узких каналах» (gap fill
             слайсера печатает узкие полости сплошными — калиброванный масштаб g_scale)
  поддержки = k_air · «воздух под телом» (только если support включён; tree-опоры лёгкие)
  продувка  = k_flush · слои · 280 мм³ (только ПОКРАШЕННЫЙ многоцвет НЕ-бамбу проектов:
              санитайзер бамбу сводит цвета к одному филаменту — продувки нет, проверено gcode)

НАСТРОЙКИ ПЕЧАТИ. У одиночного проекта-3mf берём СВОИ настройки автора (project_settings.config:
layer/walls/infill/top/bottom/support) — при любом масштабе и любом выборе столов: чем печатается
модель, от масштаба не зависит, масштабируется только геометрия. Иначе — процесс-профиль КАК ЕСТЬ
(предвыбор настроек убран 16.07, см. slicer._build_process_json): расплющиваем профиль качества и
берём wall_loops/плотность/корки из него, НЕ из пресетов бота. Раньше настройки автора были
привязаны к нативному пути слайсера (масштаб ровно 100%), и цена РВАЛАСЬ на границе: полка на
своих 5% gyroid — 784 г, на пресетных 15% — 1365 г, поэтому 92% выходили дороже 100%
(тикет клиента 26.07.2026). slicer.slice_unit при масштабе/выборе столов подставляет те же
настройки автора в процесс-профиль — оба пути считают одно и то же.

КАЛИБРОВКА. Коэффициенты подобраны 17.07.2026 по 32 свежим слайсам (11 последних заказов с
файлами + коала + калибровочный набор): |ошибка| объёма — медиана 3.5%, на целевых заказах
1.3%, 27/32 в пределах 10% (худшие: непредсказуемое падение нативного слайса → пресет, и
мелкие фактурные фигурки +10..30%). Время: медиана ~16% (вес времени в цене мал). Скрипты
калибровки — scratchpad сессии; переснять эталоны: слайс тех же моделей через slice_unit.

ЛАТЕНТНОСТЬ. Точный замер ~40-80 МБ/с: почти все модели укладываются в секунды. Гиганты
(коала: 573 МБ XML, ~16 c) считаются в ФОНЕ с персистентным кэшем по хэшу модели
(config.CACHE_DIR/fastmeasure): первый ответ — прежний bbox-фолбэк, все следующие — точные.
wait_precise=True заставляет посчитать точно синхронно (фоновое уточнение сметы в webapp).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import zipfile
from pathlib import Path

import numpy as np

from . import cache, config, fastmeasure, inspect3mf, mesh_volume
from .bedspec import DEFAULT_BED, BedSpec, bed_tag

log = logging.getLogger(__name__)

# --- Калиброванные коэффициенты модели ФИЛАМЕНТА (фит 17.07.2026, 32 свежих слайса) ----------
K_WALL = 0.92        # стенки: доля номинала A·t (перекрытие линий, недоход в тонкое)
K_SKIN = 0.903       # корки: доля номинала проекционной площади × толщина корки
P_SAT = 2.0          # мягкость насыщения оболочки против объёма тела
G_SCALE = 0.755      # мм; масштаб «узкого канала» — gap fill печатает узкие полости сплошняком
K_AIR = 0.012        # поддержки: доля «воздуха под телом» (tree-опоры лёгкие), кэп 12% тела
K_FLUSH = 1.134      # продувка покрашенного многоцвета: доля от (слои × 280 мм³)
FLUSH_MM3 = 280.0    # средний флаш на смену цвета (матрица Elegoo-профилей)

# --- Калиброванные коэффициенты модели ВРЕМЕНИ (медиана ~16%; вес времени в цене мал) --------
MVS_WALL_MM3_S = 5.52    # эффективная объёмная скорость оболочки, мм³/с
MVS_BULK_MM3_S = 9.02    # эффективная объёмная скорость инфилла/прочего, мм³/с
SEC_PER_LAYER = 2.64     # накладные на слой (суммарно по деталям), с
SEC_PER_TOOLCHANGE = 48.4  # смена цвета (покрашенный многоцвет), с
# Сопло 0.6 (пресеты 0.30/0.36): свои константы — перефит 21.08.2026 по 11 боевым моделям
# против слайсера (0.30mm Standard @0.6, робастный фит по медианной ошибке: 24.9% против
# 62.3% у 0.4-констант; старые ЗАВЫШАЛИ время крупных моделей на 100–180%). Выбор — по
# полю nozzle настроек (пресет носит сопло явно, f71f4973).
MVS_WALL_06_MM3_S = 10.0
MVS_BULK_06_MM3_S = 26.5
SEC_PER_LAYER_06 = 5.25


def _time_consts(s: dict) -> tuple[float, float, float]:
    """Константы модели времени под сопло настроек (0.6 у пресетов 0.30/0.36)."""
    if str(s.get("nozzle") or "") == "0.6":
        return MVS_WALL_06_MM3_S, MVS_BULK_06_MM3_S, SEC_PER_LAYER_06
    return MVS_WALL_MM3_S, MVS_BULK_MM3_S, SEC_PER_LAYER

# --- Настройки печати по умолчанию (0.20mm Standard @ECC2, если профиль не прочитался) -------
_FALLBACK_SETTINGS = {"layer_height": 0.2, "walls": 2, "infill_pct": 15.0, "top": 5,
                      "bottom": 3, "w_outer": 0.42, "w_inner": 0.45, "support": False,
                      "n_filaments": 1, "pattern": "rectilinear"}

# --- Страховки латентности -------------------------------------------------------------------
# Суммарный распакованный вес мешей, который меряем точно СИНХРОННО (~40-80 МБ/с ⇒ до ~2-3 c).
FAST_PRECISE_BYTES = 96_000_000
# Крупнее — bbox-фолбэк сразу + ТОЧНЫЙ замер в фоне с кэшем (следующая оценка уже точная).
OVERSIZE_PREFIX_BYTES = 8_000_000   # сколько распакованных байт крупнейшего меша читаем для bbox
BBOX_FILL_FRACTION = 0.40           # V_solid ≈ V_bbox × это (только bbox-фолбэк)

MEASURE_VERSION = "fm6"             # версия формата кэша замеров (менять при смене fastmeasure)
                                    # fm5: + part_fps (XY-футпринты деталей для раскладки по столам)
                                    # fm6: замер зависит от СТОЛА принтера (BedSpec) — все прежние
                                    #      записи считались столом CC2 на любом железе. Бамп ОБЯЗАН
                                    #      идти в одном коммите с добавлением стола в ключ ниже:
                                    #      иначе между ними кэш отдаёт цифры чужого стола.

_VERT_RE = re.compile(rb'<vertex[^>]*\bx="([-0-9.eE]+)"[^>]*\by="([-0-9.eE]+)"[^>]*\bz="([-0-9.eE]+)"')
_MESH_SUFFIXES = (".stl", ".obj", ".ply", ".off")

_bg_measures: set = set()           # ключи замеров, уже идущих в фоне (дедуп задач)


# ─────────────────────────── настройки печати ───────────────────────────

_NUM_RE = re.compile(r"([0-9.]+)")


def _num(v, default: float = 0.0) -> float:
    if isinstance(v, list):
        v = v[0] if v else default
    m = _NUM_RE.search(str(v))
    return float(m.group(1)) if m else default


def _project_print_settings(path) -> dict | None:
    """Настройки печати НАТИВНОГО слайса из project_settings.config .3mf (или None)."""
    try:
        with zipfile.ZipFile(str(path)) as z:
            cfgs = [n for n in z.namelist() if n.endswith("project_settings.config")]
            if not cfgs:
                return None
            d = json.loads(z.read(cfgs[0]))
    except Exception:  # noqa: BLE001
        return None
    if not d.get("layer_height"):
        return None
    return {"layer_height": _num(d.get("layer_height"), 0.2),
            "walls": int(_num(d.get("wall_loops"), 2)),
            "infill_pct": _num(d.get("sparse_infill_density"), 15.0),
            "top": int(_num(d.get("top_shell_layers"), 5)),
            "bottom": int(_num(d.get("bottom_shell_layers"), 3)),
            "w_outer": _num(d.get("outer_wall_line_width"), 0.42),
            "w_inner": _num(d.get("inner_wall_line_width"), 0.45),
            "support": str(d.get("enable_support", "0")) in ("1", "true", "True"),
            "n_filaments": len(d.get("filament_colour") or []) or 1,
            "pattern": str(d.get("sparse_infill_pattern", "rectilinear"))}


_process_memo: dict = {}


def _process_print_settings(quality, printer) -> dict:
    """Настройки ПРЕСЕТНОГО пути: процесс-профиль качества КАК ЕСТЬ (см. модульный докстринг).
    Профиль не найден/не читается → консервативный 0.20 Standard."""
    proc_dir = getattr(printer, "process_dir", "") if printer is not None else ""
    proc_name = getattr(quality, "process", "") or ""
    p = Path(proc_dir) / proc_name if proc_dir and proc_name else None
    out = dict(_FALLBACK_SETTINGS)
    out["layer_height"] = float(getattr(quality, "layer_height", 0.2) or 0.2)
    # сопло — свойство ПРЕСЕТА (f71f4973), не файла: ставим вне мемо по пути профиля
    out["nozzle"] = str(getattr(quality, "nozzle", "") or "")
    if not p or not p.exists():
        return out
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns)
        hit = _process_memo.get(key)
        if hit:
            merged = dict(hit)
            merged["nozzle"] = out["nozzle"]
            return merged
        from . import slicer  # локально: избегаем цикла при импорте
        prof = slicer._flatten_profile(p)
        out.update({
            "layer_height": _num(prof.get("layer_height"), out["layer_height"]),
            "walls": int(_num(prof.get("wall_loops"), 2)),
            "infill_pct": _num(prof.get("sparse_infill_density"), 15.0),
            "top": int(_num(prof.get("top_shell_layers"), 5)),
            "bottom": int(_num(prof.get("bottom_shell_layers"), 3)),
            "w_outer": _num(prof.get("outer_wall_line_width"), 0.42),
            "w_inner": _num(prof.get("inner_wall_line_width"), 0.45),
            "support": str(prof.get("enable_support", "0")) in ("1", "true", "True"),
        })
        if len(_process_memo) > 64:
            _process_memo.clear()
        _process_memo[key] = dict(out)
    except Exception:  # noqa: BLE001
        log.debug("process settings flatten failed for %s", p, exc_info=True)
    return out


def _has_project_settings(model_paths: list) -> bool:
    """Есть ли у входа СВОИ настройки печати автора: одиночный проект-3mf с
    project_settings.config. От масштаба и выбора столов НЕ зависит — это свойство файла."""
    return (len(model_paths) == 1 and Path(model_paths[0]).suffix.lower() == ".3mf"
            and inspect3mf.has_project_settings(model_paths[0]))


def _quality_is_explicit(plastic, quality) -> bool:
    """Клиент ЯВНО выбрал высоту слоя (ползунок, 03.08.2026): пришедшее качество отличается
    от профиля по умолчанию для пластика — best_quality_id мастера, иначе глобальный дефолт.

    Выводится из объектов, а не из флага в API: правило одинаково срабатывает при расчёте,
    пересчёте мастером и повторе заказа (все они восстанавливают те же plastic/quality).
    Обратная сторона: возврат ползунка на дефолтную высоту равен «не трогал» — для
    проект-3mf это снова слой автора, принятый компромисс."""
    if quality is None:
        return False
    best = getattr(plastic, "best_quality_id", None) if plastic is not None else None
    if best:
        try:
            return int(quality.id) != int(best)
        except (TypeError, ValueError):
            return False
    # Нет атрибута (стаб в тестах/старый вызов) → считаем дефолтом: прежнее поведение.
    return not bool(getattr(quality, "is_default", 1))


def _is_native_path(model_paths: list, scale: float,
                    plates_only: tuple | None = None) -> bool:
    """Пойдёт ли слайс НАТИВНЫМ путём (зеркало условия в slicer.slice_unit): одиночный
    проект-3mf, масштаб 100%, без выбора столов. Управляет только ГЕОМЕТРИЕЙ (проект как есть
    против дедупа наложенных копий) — источник НАСТРОЕК печати выбирает _has_project_settings:
    масштаб не меняет того, чем автор печатает эту модель (см. _estimate_sync)."""
    return (_has_project_settings(model_paths)
            and round(float(scale) * 100) == 100 and not plates_only)


def _flush_survives(path, s: dict, meas: dict) -> bool:
    """Будет ли продувка в реальном слайсе: только ПОКРАШЕННЫЙ (mmu_segmentation/paint_color)
    многоцвет НЕ-бамбу проектов. Бамбу-проекты санитайзер сводит к одному филаменту на объект
    (продувки нет — сверено с gcode коалы), объектный многоцвет CC2-слайс тоже печатает одним.
    Флаг покраски даёт сам замер (fastmeasure по уже прочитанным байтам — бесплатно)."""
    if s.get("n_filaments", 1) <= 1 or not meas.get("painted"):
        return False
    try:
        from . import bambu3mf
        return not bambu3mf.is_bambu_project(Path(path))
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────── модель филамента и времени ───────────────────────────

def filament_volume_mm3(meas: dict, s: dict, scale: float = 1.0) -> dict:
    """Слагаемые объёма филамента (мм³) из статов fastmeasure + настроек печати.

    Возвращает {"total", "shell", "infill", "support", "flush", "v_body"} — total идёт в цену,
    разбивка — в модель времени и логи. Формула и коэффициенты — см. модульный докстринг."""
    sc = float(scale)
    b = np.asarray(meas["buckets"], dtype=float) * sc ** 2 * float(meas.get("exposure", 1.0))
    V = float(meas.get("v_union_mm3", meas["v_mm3"])) * sc ** 3
    if V <= 0:
        return {"total": 0.0, "shell": 0.0, "infill": 0.0, "support": 0.0, "flush": 0.0,
                "v_body": 0.0}
    t_wall = s["w_outer"] + max(0, int(s["walls"]) - 1) * s["w_inner"]
    a_lat = float(b[:, :, 2].sum())               # боковая составляющая всей поверхности
    a_up = float(b[0, :, 1].sum())                # проекция вверх-смотрящих граней
    a_dn = float(b[1, :, 1].sum())                # проекция вниз-смотрящих граней
    layer = max(0.05, float(s["layer_height"]))
    shell_raw = (K_WALL * a_lat * t_wall
                 + K_SKIN * (a_up * s["top"] * layer + a_dn * s["bottom"] * layer))
    x = shell_raw / V
    shell = V * (x / (1.0 + x ** P_SAT) ** (1.0 / P_SAT))   # мягкое насыщение против V
    a_total = float(b[:, :, 0].sum())
    thick = 2.0 * V / max(1e-9, a_total)          # характерная толщина детали
    gap = max(0.0, thick - 2.0 * t_wall)          # «канал» между стенками под инфилл
    rho = min(100.0, float(s["infill_pct"])) / 100.0
    rho_eff = rho + (1.0 - rho) * math.exp(-gap / G_SCALE)  # узкий канал → gap fill → сплошняк
    infill = rho_eff * (V - shell)
    support = 0.0
    if s.get("support"):
        support = min(K_AIR * float(meas.get("air_mm3", 0.0)) * sc ** 3,
                      0.12 * (shell + infill))
    flush = 0.0
    if s.get("_flush"):
        h = float(meas.get("h_mm", 0.0)) * sc
        flush = K_FLUSH * (h / layer) * FLUSH_MM3
    total = shell + infill + support + flush
    return {"total": total, "shell": shell, "infill": infill, "support": support,
            "flush": flush, "v_body": V}


def print_minutes(fil: dict, meas: dict, s: dict, scale: float = 1.0) -> float:
    """Оценка времени печати, мин: оболочка и инфилл со своими эффективными скоростями +
    накладные на слои (по суммарной высоте деталей) + смены цвета при продувке."""
    mvs_wall, mvs_bulk, sec_layer = _time_consts(s)
    sec = (fil["shell"] / mvs_wall
           + (fil["infill"] + fil["support"] + fil["flush"]) / mvs_bulk)
    layer = max(0.05, float(s["layer_height"]))
    sum_h = sum(meas.get("part_heights") or [meas.get("h_mm", 0.0)]) * float(scale)
    sec += sec_layer * sum_h / layer
    if fil["flush"] > 0:
        sec += (fil["flush"] / FLUSH_MM3) * SEC_PER_TOOLCHANGE
    return max(0.5, sec / 60.0)


# ─────────────────────────── замер: кэш, фон, bbox-фолбэк ───────────────────────────

def _total_mesh_bytes(model_paths: list) -> int:
    total = 0
    for p in model_paths:
        p = Path(p)
        try:
            if p.suffix.lower() == ".3mf":
                with zipfile.ZipFile(str(p)) as z:
                    total += sum(i.file_size for i in z.infolist()
                                 if i.filename.lower().endswith(".model"))
            else:
                total += p.stat().st_size
        except (OSError, zipfile.BadZipFile):
            pass
    return total


def _bed_tag(bed: BedSpec) -> str:
    """Отпечаток стола для имени файла кэша замеров — делегация в app.bedspec.bed_tag.

    Формат жил здесь, но теперь стол метит ещё и ключи превью/геометрии/GLB (B1, хвосты):
    отпечаток обязан быть ОДИН на проект, иначе форматы разъедутся при первой правке.
    Имя оставлено на месте: на него ссылаются тесты и вызовы внутри модуля."""
    return bed_tag(bed)


def _measure_cache_path(model_paths: list, replicate_geometry: bool,
                        plates_only: tuple | None = None,
                        bed: BedSpec = DEFAULT_BED) -> Path:
    h = cache.model_hash(model_paths)
    d = config.CACHE_DIR / "fastmeasure"
    d.mkdir(parents=True, exist_ok=True)
    psuf = ("-p" + ".".join(map(str, plates_only))) if plates_only else ""
    return d / f"{h}-rg{int(replicate_geometry)}{psuf}-{_bed_tag(bed)}-{MEASURE_VERSION}.json"


def _measure_cache_get(model_paths: list, replicate_geometry: bool,
                       plates_only: tuple | None = None,
                       bed: BedSpec = DEFAULT_BED) -> dict | None:
    try:
        p = _measure_cache_path(model_paths, replicate_geometry, plates_only, bed)
        if not p.exists():
            return None
        meas = json.loads(p.read_text("utf-8"))
        meas["buckets"] = np.asarray(meas["buckets"], dtype=float)
        return meas
    except Exception:  # noqa: BLE001
        return None


def _measure_and_cache(model_paths: list, replicate_geometry: bool,
                       plates_only: tuple | None = None,
                       bed: BedSpec = DEFAULT_BED) -> dict | None:
    meas = fastmeasure.measure_model(model_paths, replicate_geometry,
                                     plates_only=set(plates_only) if plates_only else None,
                                     bed=bed)
    if meas is None:
        return None
    try:
        out = dict(meas)
        out["buckets"] = np.asarray(meas["buckets"]).tolist()
        out["part_heights"] = [round(h, 2) for h in out["part_heights"][:500]]
        out["part_fps"] = [[round(float(w), 1), round(float(d), 1)]
                           for w, d in (out.get("part_fps") or [])[:500]]
        _measure_cache_path(model_paths, replicate_geometry, plates_only, bed).write_text(
            json.dumps(out), "utf-8")
    except Exception:  # noqa: BLE001
        log.debug("fastmeasure cache write failed", exc_info=True)
    return meas


def _bg_measure(model_paths: list, replicate_geometry: bool, key: str,
                plates_only: tuple | None = None, bed: BedSpec = DEFAULT_BED) -> None:
    """Фоновый точный замер гиганта (worker-поток): следующая оценка возьмёт кэш."""
    t0 = time.perf_counter()
    try:
        meas = _measure_and_cache(model_paths, replicate_geometry, plates_only, bed)
        if meas is not None:
            log.info("fastcalc: фоновый точный замер готов (%.0f c) для %s",
                     time.perf_counter() - t0,
                     ", ".join(Path(p).name for p in model_paths))
    except Exception:  # noqa: BLE001
        log.debug("background fastmeasure failed", exc_info=True)
    finally:
        _bg_measures.discard(key)


# --- bbox-фолбэк (как в v1) — только пока фоновый точный замер не готов ----------------------

def _fast_bbox_3mf(path: Path) -> tuple[float, float, float] | None:
    """Дешёвый габарит .3mf: распакованный ПРЕФИКС крупнейшего .model + байтовый regex."""
    try:
        with zipfile.ZipFile(str(path)) as z:
            models = [i for i in z.infolist() if i.filename.lower().endswith(".model")]
            if not models:
                return None
            big = max(models, key=lambda i: i.file_size)
            with z.open(big.filename) as fh:
                data = fh.read(OVERSIZE_PREFIX_BYTES)
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    pts = _VERT_RE.findall(data)
    if len(pts) < 3:
        return None
    try:
        arr = np.array(pts, dtype="S24").astype("float64")
        ext = arr.max(0) - arr.min(0)
        return (max(0.0, float(ext[0])), max(0.0, float(ext[1])), max(0.0, float(ext[2])))
    except Exception:  # noqa: BLE001
        return None


def _bbox_estimate(model_paths: list, s: dict, scale: float,
                   allow_slow: bool = False) -> dict | None:
    """Грубая оценка по v1-модели куба (bbox × доля заполнения) — фолбэк для гигантов,
    пока точный фоновый замер не готов, и для нечитаемой геометрии. allow_slow — можно
    падать на потоковый mesh_volume (только для небольших файлов: на гиганте это минута)."""
    total_v = 0.0
    max_h = 0.0
    for p in model_paths:
        p = Path(p)
        v = h = None
        if p.suffix.lower() == ".3mf":
            ext = _fast_bbox_3mf(p)
            if ext:
                v = ext[0] * ext[1] * ext[2] * BBOX_FILL_FRACTION
                h = ext[2]
            elif allow_slow:
                vs = mesh_volume.solid_volume_cm3(p)
                if vs and vs > 0:
                    v = vs * 1000.0
                    h = (vs * 1000.0) ** (1.0 / 3.0)
        elif p.suffix.lower() == ".stl":
            r = fastmeasure._read_binary_stl(p)
            if r is not None:
                verts, tris = r
                a = verts[tris[:, 0]]
                b = verts[tris[:, 1]]
                c = verts[tris[:, 2]]
                vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum())) / 6.0
                ext = verts.max(0) - verts.min(0)
                v = vol if vol > 0 else float(ext[0] * ext[1] * ext[2]) * BBOX_FILL_FRACTION
                h = float(ext[2])
        if v and v > 0:
            total_v += v * scale ** 3
            max_h = max(max_h, (h or 0.0) * scale)
    if total_v <= 0:
        return None
    # куб-эквивалент: side² на каждую из сторон, 4 стенки соответствуют старой v1-калибровке
    side = total_v ** (1.0 / 3.0)
    t_wall = s["w_outer"] + max(0, int(s["walls"]) - 1) * s["w_inner"]
    layer = max(0.05, float(s["layer_height"]))
    inner = max(0.0, side - 2 * t_wall) ** 2 * max(0.0, side - (s["top"] + s["bottom"]) * layer)
    shell = total_v - min(inner, total_v)
    rho = min(100.0, float(s["infill_pct"])) / 100.0
    total = min(total_v, max(shell + rho * (total_v - shell), rho * total_v))
    return {"total": total, "shell": shell, "infill": total - shell, "support": 0.0,
            "flush": 0.0, "v_body": total_v, "_h": max_h or side}


# ─────────────────────────── публичная оценка ───────────────────────────

def _estimate_sync(model_paths: list, *, density: float, quality, printer, scale: float,
                   wait_precise: bool,
                   plates_only: tuple | None = None,
                   quality_explicit: bool = False,
                   bed: BedSpec | None = None) -> tuple[dict | None, tuple | None]:
    """(результат, запрос-на-фоновый-замер|None). Блокирующая часть — зовётся в потоке.

    bed — стол принтера (app.bedspec). Не задан — читаем его из профиля машины ЗДЕСЬ, в потоке:
    разбор профиля лезет на диск (~17 мс), а звать его из событийного цикла незачем. Стол входит
    и в замер (дедуп копий/мультиплейт), и в ключ его кэша, и в раскладку по столам."""
    if bed is None:
        bed = BedSpec.from_machine_profile(getattr(printer, "machine_profile", None))
    native = _is_native_path(model_paths, scale, plates_only)
    # НАСТРОЙКИ печати проект-3mf берём у автора при ЛЮБОМ масштабе и выборе столов. Раньше они
    # были привязаны к нативному пути (масштаб ровно 100%), и цена рвалась на 100%: полка
    # «Large Display Shelf» на своих 5% gyroid весит 784 г, а на пресетных 15% — 1365 г, поэтому
    # 92% выходили ДОРОЖЕ 100% (тикет клиента 26.07). Масштаб не меняет того, чем автор печатает
    # модель; меняется только геометрия, а её масштабирует filament_volume_mm3.
    s = _project_print_settings(model_paths[0]) if _has_project_settings(model_paths) else None
    project_settings = s is not None
    if s is None:
        native = False
        s = _process_print_settings(quality, printer)
    elif quality_explicit:
        # Ползунок высоты слоя (03.08.2026): клиент ЯВНО выбрал слой — он и ширины линий его
        # процесса (0.30/0.36 идут профилями сопла 0.6) перекрывают авторские. Стенки, корки,
        # инфилл и опоры автора сохраняем: ползунок меняет слой, а не то, ЧЕМ печатается модель.
        qs = _process_print_settings(quality, printer)
        s["layer_height"] = qs["layer_height"]
        s["w_outer"], s["w_inner"] = qs["w_outer"], qs["w_inner"]
        s["nozzle"] = qs.get("nozzle", "")   # 0.30/0.36 → константы времени сопла 0.6
    replicate = not native
    total_bytes = _total_mesh_bytes(model_paths)

    def _measure(rep: bool):
        meas = _measure_cache_get(model_paths, rep, plates_only, bed)
        if meas is not None:
            return meas, "cache"
        if total_bytes <= FAST_PRECISE_BYTES or wait_precise:
            return _measure_and_cache(model_paths, rep, plates_only, bed), "precise"
        return None, "bbox"

    small = total_bytes <= FAST_PRECISE_BYTES
    meas, src = _measure(replicate)

    if project_settings and meas is not None and meas.get("painted") and s.get("n_filaments", 1) > 1:
        # Покрашенный многоцвет: нативный слайс таких проектов на CC2 в 3 из 4 случаев ПАДАЕТ
        # (organic/var-layer и тихие крэши) и расчёт уходит пресетным путём БЕЗ продувки —
        # оцениваем как пресет (иначе флаш-терм даёт абсурд на моделях, где натив не выжил).
        # Если натив всё же выживет, продувка занизится (обезьянки: −80%) — принятый компромисс.
        # Гейт на настройках автора, а не на нативном пути: при масштабе ≠100% слайс тем более
        # идёт пресетным путём, и цена не должна прыгать на границе 100%.
        native = False
        project_settings = False
        s = _process_print_settings(quality, printer)
        if not replicate:
            replicate = True
            meas, src = _measure(True)

    if native and meas is not None:
        s["_flush"] = _flush_survives(model_paths[0], s, meas)

    if meas is not None:
        fil = filament_volume_mm3(meas, s, scale)
        if fil["total"] <= 0:
            return None, None
        minutes = print_minutes(fil, meas, s, scale)
        height = float(meas.get("h_mm", 0.0)) * scale
        v_body = fil["v_body"]
        bg = None
    else:
        # bbox-фолбэк меряет ВСЮ модель (выбор столов тут не различить — грубая прикидка
        # до фонового точного замера, который выбор столов уже уважает)
        fil = _bbox_estimate(model_paths, s, scale, allow_slow=small)
        if not fil:
            return None, None
        v_body = fil["v_body"]
        # v1-модель времени для bbox-пути (объём в мм³ → секунды по эффективной скорости)
        minutes = ((fil["total"] / 6.0 + 2.0 * (fil["_h"] / max(0.05, s["layer_height"])))
                   * 1.3 / 60.0)
        height = fil["_h"]
        bg = None
        if not small:
            key = _measure_cache_path(model_paths, replicate, plates_only, bed).name
            bg = (list(model_paths), replicate, key, plates_only, bed)

    v_fil_cm3 = fil["total"] / 1000.0
    # DFM (B2): габарит детали/тонкие стенки для вердикта печатопригодности. Есть ТОЛЬКО на точном
    # замере (meas); на bbox-фолбэке (гигант без кэша) детальность неизвестна → n_parts=None,
    # остальное 0 — вердикт остаётся консервативным (гейт одной детали не сработает, останется
    # только проверка общего габарита из строки dims). scale линеен по габаритам.
    if meas is not None:
        n_parts = int(meas.get("n_parts", 1))
        min_extent_mm = float(meas.get("min_extent_mm", 0.0)) * scale
        max_part_mm = float(meas.get("max_part_mm", 0.0)) * scale
        wall_proxy_mm = float(meas.get("wall_proxy_mm", 0.0)) * scale
    else:
        n_parts = None
        min_extent_mm = max_part_mm = wall_proxy_mm = 0.0
    # Раскладка набора по столам (defe5dc0): полочная укладка XY-футпринтов деталей на стол
    # принтера. None — деталей/замера нет (bbox-фолбэк), вызывающий трактует как «неизвестно».
    est_plates = None
    if meas is not None and meas.get("part_fps"):
        # Стол — тот же BedSpec, что решал дедуп копий в самом замере (раньше здесь был
        # ОТДЕЛЬНЫЙ вызов slicer.bed_size, и два числа стола могли разойтись).
        fps = [(w * scale, d * scale) for w, d in meas["part_fps"]]
        est_plates = fastmeasure.estimate_plates(fps, bed=bed)
    return ({"grams_unit": v_fil_cm3 * max(0.01, density),
             "minutes_unit": minutes,
             "volume_cm3": v_fil_cm3,
             "v_solid_cm3": v_body / 1000.0,
             "fill_factor": fil["total"] / v_body if v_body > 0 else 0.0,
             "height_mm": height,
             "measure": src,
             "n_parts": n_parts, "min_extent_mm": min_extent_mm,
             "max_part_mm": max_part_mm, "wall_proxy_mm": wall_proxy_mm,
             "est_plates": est_plates,
             # native — проект целиком как есть; project — настройки автора на масштабированной
             # геометрии / выбранных столах; preset — процесс-профиль бота
             "settings_src": "native" if native else ("project" if project_settings else "preset")},
            bg)


async def estimate(model_paths, *, plastic, quality, strength, qty: int = 1,
                   extra_colors: int = 0, plates: int = 1, settings=None,
                   scale: float = 1.0, printer=None, allow_background: bool = True,
                   wait_precise: bool = False, plates_only=None,
                   bed: BedSpec | None = None) -> dict | None:
    """Аналитическая оценка ОДНОЙ единицы: {grams_unit, minutes_unit, volume_cm3}
    (+ диагностика v_solid_cm3/fill_factor/height_mm/measure) или None, если оценить нельзя.

    volume_cm3 — объём филамента единицы (см³); grams_unit = volume_cm3·density. Это БАЗОВЫЕ
    цифры на 1 шт БЕЗ тиража/продувки многоцвета из pricing (qty/extra_colors/plates приняты
    для совместимости — их домножает pricing.compute_price). strength принят для совместимости:
    слайс идёт настройками профиля/проекта, а не пресетов (предвыбор убран 16.07).

    printer — для чтения процесс-профиля пресетного пути (без него — дефолт 0.20 Standard).
    plates_only — 0-базные индексы выбранных столов (как у slice_unit): меряется только их
    геометрия; для наборов файлов/моделей без данных о столах — вся модель (как слайс).
    wait_precise=True — мерить точно сколько бы ни заняло (фоновое уточнение сметы).
    allow_background=False — не запускать фоновый точный замер гигантов (тесты).

    bed — стол принтера (app.bedspec): от него зависят дедуп наложенных копий и гейт
    мультиплейта в самом замере (а значит объём, число деталей и ЦЕНА), раскладка набора по
    столам и ключ кэша замеров. Не задан — читается из printer.machine_profile (в рабочем
    потоке). calcservice.compute считает его ОДИН раз и передаёт сюда, чтобы кламп масштаба и
    замер судили об одном и том же столе, а профиль не перечитывался на каждую группу столов."""
    try:
        density = float(getattr(plastic, "density", 0.0) or 0.0)
        scale = float(scale or 1.0)
        plates_sel = tuple(sorted({int(i) for i in plates_only})) if plates_only else None
    except (TypeError, ValueError):
        return None
    if density <= 0:
        return None
    # Дубль модели в карточке (полный .3mf + STL-россыпь «Plate N») удваивал вес мгновенной
    # ≈оценки — фильтр тот же, что у calcservice.compute (фидбэк 24.07, Фалкон/Валли)
    model_paths = inspect3mf.drop_redundant_plate_stls(list(model_paths))
    t0 = time.perf_counter()
    try:
        res, bg = await asyncio.to_thread(
            _estimate_sync, list(model_paths), density=density, quality=quality,
            printer=printer, scale=scale, wait_precise=bool(wait_precise),
            plates_only=plates_sel, bed=bed,
            quality_explicit=_quality_is_explicit(plastic, quality))
    except Exception:  # noqa: BLE001
        log.debug("fastcalc.estimate failed for %s", model_paths, exc_info=True)
        return None
    if bg and allow_background and bg[2] not in _bg_measures:
        # гигант без кэша: точный замер в фоне, кэш по хэшу модели — следующая оценка точная
        _bg_measures.add(bg[2])
        asyncio.get_running_loop().create_task(
            asyncio.to_thread(_bg_measure, bg[0], bg[1], bg[2], bg[3], bg[4]))
    if res is not None:
        log.info("fastcalc: ≈%.1f г / ≈%.0f мин (V_body %.1f см³, f %.2f, %s/%s, %.0f мс) для %s",
                 res["grams_unit"], res["minutes_unit"], res["v_solid_cm3"], res["fill_factor"],
                 res["measure"], res["settings_src"], (time.perf_counter() - t0) * 1000.0,
                 ", ".join(Path(p).name for p in model_paths))
    return res
