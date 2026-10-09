"""Обёртка над ElegooSlicer / OrcaSlicer CLI.

Слайсит модель(и) headless и достаёт из gcode объём филамента (cm3) и время печати.
Вес НЕ берём из слайсера (filament_density через --load-filaments не резолвится →
показывает 0): считаем grams = объём(cm3) × плотность пластика из БД.

Ключевые факты (ElegooSlicer 1.5.1.6 / Centauri Carbon 2):
- НЕ передавать --export-3mf: падает на headless OpenGL рендере превью. Без него
  exit 0, gcode пишется в --outputdir как plate_*.gcode.
- --load-settings резолвит цепочку inherits из системных профилей.
- Профиль процесса берём КАК ЕСТЬ (расплющенным по inherits): предвыбор настроек убран (16.07),
  layer_height / infill / walls админ выставляет сам в ElegooSlicer, бот их не навязывает.

Параметры принтера (machine_profile, process_dir, filament) приходят из БД-принтера,
профиль процесса — из пресета качества, филамент — из materials.resolve_filament.
Результат кэшируется по хэшу (модель + параметры). Параллельные слайсы ограничены
семафором, чтобы не положить машину.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import cache, config, cpupool, inspect3mf, profilepaths, slicers
from .bedspec import DEFAULT_BED, BedSpec

log = logging.getLogger(__name__)

# Версия ЛОГИКИ слайс-расчёта в ключе кэша (OpenTodo f6a28445). Кэш хранит граммы/время по
# хэшу «модель + параметры», но не знает о смене самого КОДА: поменял флаги CLI, флэттенинг
# профилей, парсинг G-кода — старые записи станут враньём. Бампни число при любом изменении,
# влияющем на объём/время, и весь кэш честно пересчитается.
LOGIC_VERSION = 2   # 2: убран предвыбор настроек — слайс по профилю как есть (кэш пересчитается)

_sem = asyncio.Semaphore(config.MAX_CONCURRENT_SLICES)


class SlicerError(Exception):
    pass


def _looks_like_crash(returncode: int | None) -> bool:
    """Аварийный КРЭШ процесса слайсера — который стоит ПОВТОРИТЬ (транзиентный), а не обычный
    код-ошибка CLI. Windows: NTSTATUS-исключение 0xC0000000..0xCFFFFFFF (напр. 0xC00000FD
    STATUS_STACK_OVERFLOW / 0xC000041D — так ElegooSlicer падает при СТАРТЕ, когда занята VRAM:
    инициализация GL-контекста в драйвере NVIDIA переполняет стек, OpenTodo afe841b8). НЕ считаем
    крэшем 0xFFFFFF00..0xFFFFFFFF — это собственные коды Orca/Elegoo CLI (-1..-255: коллизии
    деталей, пустой файл), они детерминированы, повтор бесполезен → сразу фолбэк на ≈оценку.
    POSIX: отрицательный код = убит сигналом (тоже крэш)."""
    if returncode is None or returncode == 0:
        return False
    if returncode < 0:
        return True
    return 0xC0000000 <= (returncode & 0xFFFFFFFF) <= 0xCFFFFFFF


async def _exec_slicer(args: list[str]) -> tuple[int | None, bytes, bytes]:
    """Запустить процесс слайсера под общим семафором с таймаутом; вернуть (код, stdout, stderr).
    При АВАРИЙНОМ крэше старта (GL/VRAM, _looks_like_crash) — короткая пауза и ОДИН повтор:
    переполнение стека в GL-контексте при пиковой занятости VRAM (аватарки/GPU-превью) обычно
    транзиентно — через пару секунд память освобождается и повтор проходит. Таймаут НЕ повторяем."""
    rc: int | None = None
    out = err = b""
    for attempt in (0, 1):
        async with _sem:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out = err = b""
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=config.SLICE_TIMEOUT)
            except asyncio.TimeoutError:
                raise SlicerError(f"Слайсинг превысил лимит {config.SLICE_TIMEOUT} c.")
            finally:
                if proc.returncode is None:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
        rc = proc.returncode
        if attempt == 0 and _looks_like_crash(rc):
            log.warning("слайсер аварийно завершился (%s) — пауза и повтор (транзиентный крэш GL/VRAM?)", rc)
            await asyncio.sleep(4.0)
            continue
        break
    return rc, out, err


@dataclass
class SliceResult:
    volume_cm3: float
    minutes: float
    cached: bool = False
    volumes_cm3: list[float] = field(default_factory=list)  # пер-филаментный объём (по экструдерам)
    # DFM (B2): по-детальные габариты для вердикта печатопригодности. Заполняются только на
    # аналитическом пути (fastcalc через _estimate_fallback); честный слайс их не даёт (None/0) —
    # вердикт тогда падает на консервативную проверку общего габарита из строки dims.
    n_parts: int | None = None
    min_extent_mm: float = 0.0
    max_part_mm: float = 0.0
    wall_proxy_mm: float = 0.0
    # Оценка раскладки набора деталей по столам (полочная укладка fastmeasure.estimate_plates,
    # OpenTodo defe5dc0): сколько столов займёт печать. None — не считали (честный слайс/bbox).
    est_plates: int | None = None


_CM3_RE = re.compile(r"filament used \[cm3\]\s*=\s*([0-9.,\s]+)")
_TIME_RE = re.compile(r"estimated printing time \(normal mode\)\s*=\s*([0-9hms\s]+)")
# Фолбэк: если слайсер не пишет «(normal mode)» (другой режим/формат) — берём любое время печати.
_TIME_RE_ANY = re.compile(r"estimated printing time[^=\n]*=\s*([0-9hms\s]+)")


def _parse_time_to_minutes(text: str) -> float:
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m", text)
    s = re.search(r"(\d+)\s*s", text)
    seconds = 0
    if h:
        seconds += int(h.group(1)) * 3600
    if m:
        seconds += int(m.group(1)) * 60
    if s:
        seconds += int(s.group(1))
    return seconds / 60.0


def _flatten_profile(leaf: Path) -> dict:
    """Расплющить цепочку `inherits` профиля (процесса ИЛИ машины) в один самодостаточный набор.

    Системные профили ElegooSlicer — это короткие листы-дельты, а реальные значения лежат на
    несколько уровней глубже по `inherits`. Так теряются ДВА разных набора параметров:
      • процесс: скорости/ускорения (outer_wall_speed=160, default_acceleration=10000 — в
        0.20mm Standard → fdm_process_elegoo_04020 → …_common → fdm_process_common);
      • машина: кинематические лимиты (machine_max_acceleration_x/y=20000, machine_max_speed_x/y=500,
        в fdm_elegoo_3dp_001_common) — именно по ним планировщик слайсера и СЧИТАЕТ время.

    Бот раньше отдавал слайсеру лист как есть, полагаясь, что CLI `--load-settings` дотянет
    наследование. Он молча НЕ дотягивает ни для процесса, ни для машины → скорости/ускорения и
    кинематика падали в медленные дефолты → расчётное время печати раздувалось (заказ #20:
    15ч38м вместо ~7.5ч — ×2 по машинному времени, цена клиенту задиралась вдвое). Поэтому
    собираем полный профиль сами и ни от какого резолва inherits не зависим.

    Семантика слияния как в Bambu/Orca — плоское переопределение: child перекрывает parent.
    Родителей ищем по `name` в пределах папки вендора (…/profiles/Elegoo), чтобы не подцепить
    одноимённый fdm_*_common другого вендора. Любой обрыв цепочки (родитель не найден) →
    отдаём, что успели собрать (не хуже прежнего поведения, но обычно уже со всеми лимитами)."""
    vendor_root = leaf.parent
    for anc in leaf.parents:
        # Корень вендора: «…/profiles/<Вендор>» у установки, «…/system/<Вендор>» у её
        # OTA-зеркала в %APPDATA% (хвост C2: после перекоренения профиль читается ИМЕННО
        # оттуда). Не узнать зеркало значило бы оборвать цепочку inherits на листе — молча,
        # без исключения: терялись бы machine_max_acceleration/speed, то есть время печати
        # и цена клиенту снова уехали бы вдвое (см. заказ #20 выше).
        if anc.parent is not None and anc.parent.name.lower() in ("profiles", "system"):
            vendor_root = anc
            break
        try:      # запасной признак вендорского корня — по составу папок, а не по имени
            if any((anc / sub).is_dir() for sub in ("machine", "process", "filament")):
                vendor_root = anc
                break
        except OSError:
            pass
    index: dict[str, Path] = {}
    for f in vendor_root.rglob("*.json"):
        index.setdefault(f.stem, f)        # первый по имени; стемы профилей вендора уникальны

    chain: list[dict] = []
    node: Path | None = leaf
    seen: set[str] = set()
    while node is not None and node.exists():
        try:
            d = json.loads(node.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            break
        chain.append(d)
        parent = d.get("inherits")
        if not parent or parent in seen:
            break
        seen.add(parent)
        node = index.get(parent)
    merged: dict = {}
    for d in reversed(chain):              # от корня к листу: лист перекрывает родителей
        merged.update(d)
    merged.pop("inherits", None)           # профиль самодостаточен — наследование больше не нужно
    merged.pop("setting_id", None)         # не выдаём пользовательский профиль за системный пресет
    return merged


def bed_size(machine_profile: str | Path) -> tuple[float, float]:
    """(XY, Z) печатаемой области стола, мм — из профиля машины (printable_area/printable_height,
    лежат в родителях цепочки inherits → флэттеним). Для прямоугольного стола XY = меньшая
    сторона (равномерный масштаб должен влезть по обеим). Не разобрали — консервативный CC2."""
    try:
        prof = _flatten_profile(Path(machine_profile))
        xs: list[float] = []
        ys: list[float] = []
        for pt in prof.get("printable_area") or []:
            x, _, y = str(pt).lower().partition("x")
            xs.append(float(x))
            ys.append(float(y))
        xy = min(max(xs), max(ys)) if xs and ys else DEFAULT_BED.xy_mm
        z = float(prof.get("printable_height") or 0)
        return (xy if xy > 0 else DEFAULT_BED.xy_mm, z if z > 0 else DEFAULT_BED.z_mm)
    except Exception:  # noqa: BLE001
        log.debug("bed_size parse failed for %s", machine_profile, exc_info=True)
        return (DEFAULT_BED.xy_mm, DEFAULT_BED.z_mm)


# Настройки печати проекта-3mf, которые переносим в процесс-профиль, когда нативный слайс
# невозможен (масштаб ≠100% / выбран не весь набор столов). Ключи — как в project_settings.config.
_PROJECT_KEYS = ("layer_height", "wall_loops", "sparse_infill_density", "sparse_infill_pattern",
                 "top_shell_layers", "bottom_shell_layers", "enable_support")


def _project_overrides(model_paths: list[Path]) -> dict:
    """Настройки автора из одиночного проекта-3mf (или {}). Нужны, когда нативный слайс не
    применим: без них масштабированная модель резалась бы плотностью процесс-профиля бота и
    выходила ДОРОЖЕ себя же в 100% (тикет клиента 26.07.2026: полка 5% gyroid → 784 г,
    те же граммы на пресетных 15% → 1365 г). Тот же выбор делает fastcalc._has_project_settings."""
    paths = [Path(p) for p in model_paths]
    if len(paths) != 1 or paths[0].suffix.lower() != ".3mf":
        return {}
    try:
        if not inspect3mf.has_project_settings(paths[0]):
            return {}
        with zipfile.ZipFile(str(paths[0])) as z:
            cfgs = [n for n in z.namelist() if n.endswith("project_settings.config")]
            if not cfgs:
                return {}
            d = json.loads(z.read(cfgs[0]))
    except Exception:  # noqa: BLE001
        log.debug("project settings read failed for %s", model_paths, exc_info=True)
        return {}
    if not d.get("layer_height"):
        return {}
    return {k: d[k] for k in _PROJECT_KEYS if d.get(k) not in (None, "")}


def _build_process_json(base_process: Path, out_path: Path, machine_name: str = "",
                        overrides: dict | None = None) -> None:
    # ПРЕДВЫБОР НАСТРОЕК УБРАН (по просьбе владельца 16.07): бот больше НЕ навязывает
    # layer_height / sparse_infill_density / wall_loops из пресетов качества/прочности — слайс идёт
    # с настройками процесс-профиля КАК ЕСТЬ (админ выставляет их сам в ElegooSlicer). Расплющивание
    # оставляем: без него скорости/ускорения из цепочки inherits падают в дефолт и время печати ×2
    # (см. test_slicer_profile). Чтобы вернуть предвыбор — снова присвоить эти 3 поля из аргументов.
    # overrides — настройки САМОГО проекта-3mf (_project_overrides), а не пресеты бота: они идут
    # поверх профиля, когда нативный слайс невозможен, чтобы масштаб не менял цену модели.
    data = _flatten_profile(base_process)
    data.update(overrides or {})
    data["name"] = "bot_override"
    # ElegooSlicer (свежие версии) требует у пользовательского профиля процесса эти поля,
    # иначе --load-settings отвергает файл («from unsupported» / «unknown config type» /
    # «process not compatible»). Явно объявляем совместимость с нашим принтером.
    data["from"] = "User"
    data["type"] = "process"
    if machine_name:
        data["compatible_printers"] = [machine_name]
    # Метки объектов в g-code (05.09.2026, допечатка со слоя W1): `; printing object …` — это
    # КОММЕНТАРИИ, прошивке они безразличны, а «Допечатка со слоя» по ним вырезает деталь точно и
    # называет её по имени из проекта. `exclude_object` (команды EXCLUDE_OBJECT_*) не трогаем:
    # его понимает только Klipper, стоковый CC2 не проверялся.
    data["gcode_label_objects"] = "1"
    out_path.write_text(json.dumps(data), encoding="utf-8")


def _parse_gcode_dir(gcode_dir: Path, be: slicers.SlicerBackend | None = None) -> tuple[float, float]:
    """Сводка филамент/время из gcode. `be` — движок, которым резали: подписи сводки У КАЖДОГО
    СВОИ, и это выяснено запуском, а не предположением (см. SlicerBackend.cm3_pattern)."""
    be = be or slicers.for_kind(None)
    cm3_re, time_re, time_any = _res_for(be)
    volume = 0.0
    minutes = 0.0
    found = False
    plates = sorted(gcode_dir.glob("plate_*.gcode")) or sorted(gcode_dir.glob("*.gcode"))
    for gp in plates:
        text = gp.read_text(encoding="utf-8", errors="ignore")
        plate_vol = 0.0
        cm3_matches = cm3_re.findall(text)
        if cm3_matches:
            for num in cm3_matches[-1].split(","):     # последняя строка = итог по плите, по цветам
                num = num.strip()
                if num:
                    try:
                        plate_vol += float(num) * be.cm3_scale
                        found = True
                    except ValueError:
                        pass
        volume += plate_vol
        time_matches = time_re.findall(text) or time_any.findall(text)
        if time_matches:
            minutes += _parse_time_to_minutes(time_matches[-1])
            found = True
        log.debug("  плита %s: %.2f cm3", gp.name, plate_vol)
    if not found:
        # Диагностика: gcode есть, но без сводки филамент/время (обычно — оборванный файл при
        # сбое/контеншене слайсера). Пишем список файлов и хвост последнего в лог для разбора.
        try:
            files = [(p.name, p.stat().st_size) for p in plates]
            tail = plates[-1].read_text("utf-8", "ignore")[-800:] if plates else "(нет .gcode)"
            log.warning("Слайс без сводки филамент/время. Файлы=%s\nхвост gcode:\n%s", files, tail)
        except Exception:  # noqa: BLE001
            log.debug("parse diag failed", exc_info=True)
        raise SlicerError("Не удалось разобрать результат слайсинга (нет данных о филаменте/времени).")
    log.info("Слайс-итог: плит %d → объём %.2f cm3, время %.0f мин", len(plates), volume, minutes)
    return volume, minutes


def _res_for(be: slicers.SlicerBackend):
    """Скомпилированные выражения сводки для движка. Компиляция кэшируется модулем `re`."""
    return (re.compile(be.cm3_pattern, re.I), re.compile(be.time_pattern, re.I),
            re.compile(be.time_pattern_any, re.I))


def _parse_filament_cm3_list(gcode_dir: Path, be: slicers.SlicerBackend | None = None) -> list[float]:
    """Пер-филаментный объём (cm3 на каждый экструдер) из gcode — для разбивки склада по цветам.
    Строка `filament used [cm3] = a, b, c` идёт по филаментам в порядке экструдеров. Суммируем
    одноимённые слоты по всем плитам. Пусто при любой неоднозначности — вызывающий тогда не
    делит склад (фолбэк). Намеренно отдельно от _parse_gcode_dir, чтобы не трогать проверенный
    путь объёма/времени и его рекурсивные фолбэки."""
    be = be or slicers.for_kind(None)
    cm3_re = _res_for(be)[0]
    plates = sorted(gcode_dir.glob("plate_*.gcode")) or sorted(gcode_dir.glob("*.gcode"))
    totals: list[float] = []
    for gp in plates:
        try:
            text = gp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        m = cm3_re.findall(text)
        if not m:
            continue
        vals: list[float] = []
        for num in m[-1].split(","):
            num = num.strip()
            if num:
                try:
                    vals.append(float(num) * be.cm3_scale)
                except ValueError:
                    return []
        for i, v in enumerate(vals):
            if i < len(totals):
                totals[i] += v
            else:
                totals.append(v)
    return totals


# «умещается на столе» (стол минус запас на края) и полный стол живут в app.bedspec:
# DEFAULT_BED.fit_xy_mm == 250.0, DEFAULT_BED.xy_mm == 256.0 (реальный стол CC2). Ниже стол
# едет параметром bed; вызовы без него считают по DEFAULT_BED, то есть как раньше.

# Меши сложнее этого порога (граней) валят слайсер на некоторых деталях: вырожденные слайверы
# дают вырожденные траектории, и чекер gcode-path-conflict падает (exit -100 «found slicing or
# export error» — напр. броне-плита Mark 85, 542k граней, 77 оболочек). Восстановить такую деталь
# из УЖЕ экспортированного STL не выходит (роундтрип меняет топологию необратимо), поэтому для
# крупных мешей при экспорте сразу кладём рядом «ремонтную» децимированную копию из чистого
# in-memory меша — _slice_group подхватит её, только если основной STL не заслайсился (для деталей,
# которые и так режутся, копия не используется и расход/время не меняются).
_REPAIR_FACE_THRESHOLD = 250_000


def _decimate_mesh(mesh, target_faces: int):
    """Квадрик-децимация in-memory → упрощённый меш (или None). Квадрик перетриангулирует меш и
    схлопывает вырожденные слайверы, из-за которых слайсер падает; форму сохраняет (ошибка
    субмиллиметровая), так что оценка расхода остаётся честной. Низкий target форсит максимальное
    схлопывание к «полу» децимации — именно у пола геометрия становится слайсуемой."""
    try:
        d = mesh.simplify_quadric_decimation(face_count=target_faces)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return None
    if d is None or not len(d.faces):
        return None
    d.merge_vertices()
    return d


def _export_mesh_with_repair(mesh, sp: Path) -> None:
    """Экспорт меша в STL `sp`; для сложных мешей (> _REPAIR_FACE_THRESHOLD граней) ДОПОЛНИТЕЛЬНО
    кладёт рядом ремонтную децимированную копию <stem>.repair.stl (см. _REPAIR_FACE_THRESHOLD).
    Децимируем ЗДЕСЬ, пока меш ещё чистый in-memory: из экспортированного STL восстановить нельзя."""
    mesh.export(str(sp))
    try:
        n = len(mesh.faces)
        if n <= _REPAIR_FACE_THRESHOLD:
            return
        d = _decimate_mesh(mesh, max(20_000, n // 6))   # < «пола» → макс. схлопывание слайверов
        if d is not None and len(d.faces) < n:
            d.export(str(sp.parent / (sp.stem + ".repair.stl")))
    except Exception:  # noqa: BLE001 — ремонтная копия необязательна, её отсутствие не критично
        log.debug("repair sibling export failed for %s", sp, exc_info=True)


def _placed_to_stls(placed: list, dest: Path, label: str, prefix: str = "part",
                    bed: BedSpec = DEFAULT_BED) -> list[Path]:
    """Список world-мешей → STL в dest. Если общий bbox умещается на столе — склеиваем в один
    меш (важно для flexi: пересекающиеся шарниры не облицовываются по отдельности). Иначе —
    отдельные STL (слайсер разложит --arrange / вызывающий побьёт по столам). label — для логов."""
    import trimesh

    if not placed:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    merged = placed[0] if len(placed) == 1 else trimesh.util.concatenate(placed)
    ext = None
    try:
        ext = merged.bounds[1] - merged.bounds[0]
        fits = ext[0] <= bed.fit_xy_mm and ext[1] <= bed.fit_xy_mm
    except Exception:  # noqa: BLE001
        fits = False

    if fits:
        log.info("3mf→geometry %s: %d дет. → склейка в 1 меш (bbox %.0f×%.0f мм, влезает на стол)",
                 label, len(placed), ext[0], ext[1])
        sp = dest / f"{prefix}_merged.stl"
        _export_mesh_with_repair(merged, sp)
        return [sp]
    bbox = f"{ext[0]:.0f}×{ext[1]:.0f} мм" if ext is not None else "?"
    if len(placed) > config.MAX_MESH_FILES:
        # НЕ молча: иначе у моделей со множеством деталей (мультиплейт-проекты, напр. устройство
        # перетасовки карт) лишние детали выпадают и расход/цена занижаются без следа в логах.
        log.warning("3mf→geometry %s: деталей %d > лимита MAX_MESH_FILES=%d — режем только первые "
                    "%d, ОСТАЛЬНЫЕ %d НЕ учитываются (расход/цена будут занижены, bbox %s)",
                    label, len(placed), config.MAX_MESH_FILES, config.MAX_MESH_FILES,
                    len(placed) - config.MAX_MESH_FILES, bbox)
    else:
        log.info("3mf→geometry %s: %d дет. → отдельные STL + --arrange (bbox %s)",
                 label, len(placed), bbox)
    out: list[Path] = []
    for i, g in enumerate(placed[: config.MAX_MESH_FILES]):
        sp = dest / f"{prefix}_{i}.stl"
        _export_mesh_with_repair(g, sp)
        out.append(sp)
    return out


def _apply_scale(placed: list, scale: float) -> list:
    """Равномерно масштабирует world-меши (in-place). scale≈1 — не трогаем (обычный путь)."""
    if abs(scale - 1.0) > 1e-9:
        for g in placed:
            g.apply_scale(scale)
    return placed


def _3mf_to_stls(p: Path, scale: float = 1.0, bed: BedSpec = DEFAULT_BED) -> list[Path]:
    """Геометрия из .3mf → STL. Дедуп наложенных копий всей модели (см. app.geometry):
    2 одинаковых динозавра в куче → один, а флекси из множества разложенных звеньев → все.
    Если детали умещаются вместе — склеиваем в один меш и режем как один объект (важно для
    flexi: пересекающиеся шарниры не облицовываются по отдельности → корректный расход).
    Иначе — отдельные детали, слайсер разложит --arrange."""
    from . import geometry

    placed = _apply_scale(geometry.load_print_meshes([p], bed), scale)
    if not placed:
        return []
    return _placed_to_stls(placed, p.parent / (p.stem + "_parts"), p.name, bed=bed)


def _3mf_to_plate_stls(p: Path, scale: float = 1.0, bed: BedSpec = DEFAULT_BED) -> list[list[Path]]:
    """Мультиплейт-проект → STL, СГРУППИРОВАННЫЕ по столам (в порядке столов). [] если это не
    мультиплейт (один/ноль столов) — тогда слайсим прежним единым путём. Каждый стол готовится
    как самостоятельная единица (склейка-если-влезает / отдельные STL), чтобы посчитать его
    объём+время отдельно и просуммировать (а не лумпить все столы в один --arrange)."""
    from . import geometry

    plates = geometry.load_print_meshes_by_plate([p])
    if not plates:
        return []
    dest = p.parent / (p.stem + "_plates")
    out: list[list[Path]] = []
    for idx, placed in enumerate(plates, 1):
        stls = _placed_to_stls(_apply_scale(placed, scale), dest / f"plate_{idx}",
                               f"{p.name}#стол{idx}", prefix=f"p{idx}", bed=bed) if placed else []
        out.append(stls)
    return out


def _prepare_plates(model_paths: list[Path], scale: float = 1.0,
                    bed: BedSpec = DEFAULT_BED) -> list[list[Path]]:
    """Если вход — ОДИН мультиплейт .3mf, вернуть STL по столам (для пер-стол слайса+суммы).
    Иначе [] — слайсим прежним единым путём (одноплейтные модели не трогаем). Гейт строго на
    одиночный .3mf-вход: доп. файлы рядом не привязаны к столам → честнее прежний единый путь."""
    paths = [Path(p) for p in model_paths]
    if len(paths) != 1 or paths[0].suffix.lower() != ".3mf":
        return []
    try:
        return _3mf_to_plate_stls(paths[0], scale, bed)
    except Exception:  # noqa: BLE001
        log.warning("3mf->plate stls failed for %s — fallback to single-unit", paths[0], exc_info=True)
        return []


def _prepare_unit_groups(model_paths: list[Path], scale: float = 1.0,
                         bed: BedSpec = DEFAULT_BED) -> list[tuple[list[Path], int]]:
    """Модель-ТИРАЖ (geometry.unit_groups): [(STL одной единицы, count)] — слайсер режет ОДНУ
    единицу и умножает объём/время на count. [] если это не тираж (идём обычным путём)."""
    from . import geometry
    try:
        groups = geometry.unit_groups([Path(p) for p in model_paths], bed)
    except Exception:  # noqa: BLE001
        log.warning("unit_groups failed for %s — fallback", model_paths, exc_info=True)
        return []
    if not groups:
        return []
    out: list[tuple[list[Path], int]] = []
    base = Path(model_paths[0])
    dest = base.parent / (base.stem + "_unit")
    for i, (mesh, count) in enumerate(groups):
        _apply_scale([mesh], scale)
        stls = _placed_to_stls([mesh], dest / f"u{i}", f"{base.name}#ед{i}×{count}",
                               prefix=f"u{i}", bed=bed)
        if stls:
            out.append((stls, count))
    return out


def _scaled_copy(p: Path, scale: float) -> Path | None:
    """Масштабированная копия НЕ-3mf модели (.stl/.obj/…): <stem>_sNNN.stl рядом с оригиналом.
    Оригинал не трогаем. None — trimesh файл не осилил (напр. STEP): НЕ кидаем исключение —
    функция бежит в cpupool-воркере, а cpupool.run любой сбой задачи трактует как поломку пула
    и навсегда уводит все CPU-задачи в потоки. Отказ решает вызывающий (slice_unit)."""
    import trimesh

    out = p.with_name(f"{p.stem}_s{round(scale * 100)}.stl")
    try:
        m = trimesh.load(str(p), force="mesh")
        faces = getattr(m, "faces", None)
        if m is None or faces is None or not len(faces):
            return None
        m.apply_scale(scale)
        _export_mesh_with_repair(m, out)
        return out
    except Exception:  # noqa: BLE001
        log.warning("scale copy failed for %s", p, exc_info=True)
        return None


def _prepare_models(model_paths: list[Path], scale: float = 1.0,
                    bed: BedSpec = DEFAULT_BED) -> list[Path]:
    """Проектные .3mf (с чужими настройками Bambu) валят слайсер — конвертируем их в STL.
    Остальные форматы (.stl/.obj/.step) отдаём как есть; при масштабе ≠ 1 — их
    масштабированную STL-копию (сам слайсер масштабировать не умеем). Файл, который при
    масштабе ≠ 1 не удалось масштабировать, возвращаем ОРИГИНАЛОМ — slice_unit по этому
    признаку честно откажет (молча посчитать его в 100% = наврать в размере и цене)."""
    out: list[Path] = []
    for p in model_paths:
        p = Path(p)
        if p.suffix.lower() == ".3mf":
            try:
                stls = _3mf_to_stls(p, scale, bed)
                if stls:
                    out.extend(stls)
                    continue
                log.warning("3mf has no geometry: %s", p)
            except Exception:  # noqa: BLE001
                log.warning("3mf->stl failed for %s, passing original", p, exc_info=True)
        if abs(scale - 1.0) > 1e-9:
            out.append(_scaled_copy(p, scale) or p)
        else:
            out.append(p)
    return out


def _sizes(paths: list[Path]) -> list[tuple[Path, float, float]]:
    """XY-габариты (мм) каждой подготовленной детали — для укладки по столам. Если размер не
    определить (не загрузилось) → (0,0): такая деталь уедет в общий стол (поведение как раньше)."""
    import trimesh

    out: list[tuple[Path, float, float]] = []
    for p in paths:
        try:
            m = trimesh.load(str(p), force="mesh")
            ext = m.bounds[1] - m.bounds[0]
            out.append((Path(p), float(ext[0]), float(ext[1])))
        except Exception:  # noqa: BLE001
            out.append((Path(p), 0.0, 0.0))
    return out


def _pack_into_beds(sized: list[tuple[Path, float, float]], bed_mm: float | None = None,
                    bed: BedSpec = DEFAULT_BED) -> list[list[Path]]:
    """Грубая укладка деталей по нескольким столам (FFD по площади XY), чтобы каждый набор
    гарантированно умещался и слайсерский --arrange не падал на огромных сборках. Это НЕ
    оптимальная упаковка — задача лишь «влезло и не упало»; объём/время суммируются по плитам.
    Деталь крупнее стола (или больше бюджета площади) уезжает на свой стол отдельно.
    bed_mm не задан — берём порог «умещается на столе» из bed (BedSpec.fit_xy_mm)."""
    bed_mm = bed.fit_xy_mm if bed_mm is None else bed_mm
    bed_area = bed_mm * bed_mm
    pack = bed_area * 0.6                     # запас под зазоры, которые добавит --arrange
    order = sorted(sized, key=lambda t: t[1] * t[2], reverse=True)   # FFD: крупные детали первыми
    bins: list[list[Path]] = []
    areas: list[float] = []
    for path, sx, sy in order:
        area = sx * sy
        if sx > bed_mm or sy > bed_mm or area >= pack:
            bins.append([path])               # деталь крупнее «бюджета» стола — отдельный стол
            areas.append(area)
            continue
        for i in range(len(bins)):
            if areas[i] + area <= pack:
                bins[i].append(path)
                areas[i] += area
                break
        else:
            bins.append([path])
            areas.append(area)
    return bins


def _repair_sibling(part: Path) -> Path:
    """Путь «ремонтной» (децимированной) копии детали: <stem>.repair.stl рядом с оригиналом.
    Её ЗАРАНЕЕ кладёт _export_mesh_with_repair из ЧИСТОГО in-memory меша (восстановить геометрию
    из уже экспортированного STL нельзя: роундтрип меняет топологию так, что децимация не лечит).
    _slice_group берёт её фолбэком, если основной STL детали не заслайсился."""
    part = Path(part)
    return part.with_name(part.stem + ".repair.stl")


# Реальный стол (для центрирования при --arrange 0) — bed.xy_mm (DEFAULT_BED: 256.0, CC2);
# порог склейки/«умещается» — bed.fit_xy_mm (стол минус запас на края, DEFAULT_BED: 250.0).


def _center_on_bed(stl_path: str, out_path: str, bed: BedSpec = DEFAULT_BED) -> str | None:
    """Сдвинуть деталь в ЦЕНТР стола (XY) и опустить на z=0, сохранить STL. Для повторного слайса с
    --arrange 0: деталь размером ~со стол (напр. шлем Mark 85, 250 мм) слайсер не может РАЗЛОЖИТЬ
    (--arrange 1 добавляет отступы → exit -50 «no suitable objects»), но нарезает НА МЕСТЕ, если она
    в пределах стола. В .3mf деталь уже разложена под один стол, но в мировых координатах может
    стоять далеко от начала (видели X≈742) → центрируем. Это лишь сдвиг — топологию не трогает
    (в отличие от децимации, чувствительной к STL-роундтрипу). None при ошибке."""
    import trimesh
    try:
        m = trimesh.load(stl_path, force="mesh")
        lo, hi = m.bounds
        cx, cy = (lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0
        m.apply_translation([bed.xy_mm / 2 - cx, bed.xy_mm / 2 - cy, -lo[2]])
        m.export(out_path)
        return out_path
    except Exception:  # noqa: BLE001 — фолбэк необязателен, сбой → None (исходная ошибка важнее)
        log.debug("center-on-bed failed for %s", stl_path, exc_info=True)
        return None


async def _slice_group(parts: list[Path], outdir: Path, run_slice, depth: int = 0,
                       bed: BedSpec = DEFAULT_BED) -> tuple[float, float]:
    """Слайсит группу деталей на один стол через `run_slice(parts, outdir)`. Два режима фолбэка:

    • Группа из НЕСКОЛЬКИХ деталей не разложилась на одну плиту (exit -101 на «неукладываемом»
      наборе — напр. большая пластина + крупные детали рядом, см. card shuffler): делим через одну
      (крупные детали расходятся по половинам) и слайсим половины отдельно, суммируя объём+время.
      Рекурсия идёт до единичных деталей; depth ограничивает её (2^8 = 256 ≥ любой сборки).
    • ОДНА деталь, которую слайсер отверг: пробуем по очереди фолбэки (каждый — отдельный класс
      отказа на плитах Mark 85), и лишь если ВСЕ не помогли — пробрасываем ошибку:
        1) «ремонтная» децимированная копия-сиблинг <stem>.repair.stl (вырожденные слайверы валят
           чекер gcode-path-conflict, exit -100 — броне-плита; копия заготовлена из чистого
           in-memory меша при экспорте, т.к. из STL геометрию уже не восстановить);
        2) деталь ~размером со стол — слайсер не может РАЗЛОЖИТЬ её с отступами (exit -50 «no
           suitable objects», шлем-плита 250 мм): центрируем на столе и режем НА МЕСТЕ (--arrange 0);
        3) комбо: центрированная ремонтная копия без --arrange (деталь и битая, и крупная).
      Без этих фолбэков одна «плохая» плита роняла весь расчёт мультиплейт-модели."""
    try:
        return await run_slice(parts, outdir)
    except SlicerError:
        if len(parts) == 1:
            part = Path(parts[0])
            repair = _repair_sibling(part)
            if repair.exists():                        # (1) децимированная копия, обычный --arrange
                try:
                    log.warning("Деталь %s не заслайсилась — повтор с ремонтной (децимированной) копией",
                                part.name)
                    return await run_slice([repair], outdir / "rep")
                except SlicerError:
                    pass
            centered = await cpupool.run(_center_on_bed, str(part), str(outdir / "_centered.stl"), bed)
            if centered:                               # (2) центр стола + резать на месте (--arrange 0)
                try:
                    log.warning("Деталь %s не разместилась — центрирую на столе и режу без --arrange",
                                part.name)
                    return await run_slice([Path(centered)], outdir / "cen", arrange="0")
                except SlicerError:
                    pass
            if repair.exists():                        # (3) комбо: ремонтная копия по центру без --arrange
                cen_rep = await cpupool.run(_center_on_bed, str(repair),
                                            str(outdir / "_centered_rep.stl"), bed)
                if cen_rep:
                    log.warning("Деталь %s — комбо-фолбэк: ремонтная копия по центру без --arrange",
                                part.name)
                    return await run_slice([Path(cen_rep)], outdir / "cenrep", arrange="0")
            raise
        if depth >= 8:
            raise
        a, b = parts[::2], parts[1::2]
        log.warning("Плита из %d дет. не разложилась — делю на %d+%d и слайсю отдельно",
                    len(parts), len(a), len(b))
        va, ma = await _slice_group(a, outdir / "a", run_slice, depth + 1, bed)
        vb, mb = await _slice_group(b, outdir / "b", run_slice, depth + 1, bed)
        return va + vb, ma + mb


async def _slice_plates_sum(plate_groups: list[list[Path]], work: Path, run_slice,
                            bed: BedSpec = DEFAULT_BED) -> tuple[float, float]:
    """Слайсит КАЖДЫЙ стол отдельно (через _slice_group — с фолбэком на bin-packing/деление и
    починку-децимацию одиночной детали) и СУММИРУЕТ объём+время по столам. Так мультиплейт-проект
    считается честно по столам, а не лумпом всех столов в один --arrange (баг Mark 85).

    Столы режутся ПАРАЛЛЕЛЬНО (asyncio.gather): на запуски самого слайсера всё равно надет общий
    семафор MAX_CONCURRENT_SLICES (в run_slice), так что одновременно бежит не больше разрешённого
    числа процессов — а простой большой модели (напр. Mark 85, 5+ столов) теперь реально
    раскладывается по ядрам, а не считается по одному столу. Пустые столы (без печатной геометрии)
    пропускаем. Если хоть один стол не доберётся даже починкой — ошибка пробрасывается (честный
    отказ, без молчаливого занижения). Если в сумме ноль — ошибка (как раньше)."""
    indexed = [(i, g) for i, g in enumerate(plate_groups, 1) if g]
    if not indexed:
        raise SlicerError("нет геометрии для слайсинга (мультиплейт).")

    async def _one(i: int, group: list[Path]) -> tuple[float, float]:
        v, m = await _slice_group(group, work / f"plate_{i}", run_slice, bed=bed)
        log.info("  стол %d: %d дет. → %.2f cm3, %.0f мин", i, len(group), v, m)
        return v, m

    # return_exceptions=True: дожидаемся ВСЕХ столов, прежде чем пробросить отказ. Иначе обычный
    # gather пробросил бы первую ошибку сразу, НЕ отменив соседние корутины — они дослайсивали бы
    # в фон, а finally (shutil.rmtree(work)) снёс бы рабочую папку под живыми процессами слайсера.
    results = await asyncio.gather(*(_one(i, g) for i, g in indexed), return_exceptions=True)
    oks: list[tuple[float, float]] = []
    for r in results:
        if isinstance(r, BaseException):
            raise r                            # первый стол, не добитый даже починкой — честный отказ
        oks.append(r)
    volume = sum(v for v, _ in oks)
    minutes = sum(m for _, m in oks)
    if volume <= 0 or minutes <= 0:
        raise SlicerError("Слайсер вернул нулевой объём/время — проверьте модель.")
    return volume, minutes


async def _native_slice(model_path: Path, machine: Path,
                        backend: slicers.SlicerBackend | None = None,
                        exe: str = "") -> tuple[float, float, list[float]]:
    """Слайс ПРОЕКТА-3mf с его родными настройками (OpenTodo 463c5e9e): оригинальный файл
    позиционным аргументом, --load-settings ТОЛЬКО с расплющенной машиной (наш принтер),
    без --arrange (раскладка авторская). Проверено живьём на ElegooSlicer 1.5.2.2: процесс и
    филамент проекта сохраняются (cm3/время идентичны слайсу совсем без подмены), --slice 0
    режет все плиты проекта → plate_*.gcode суммируются. Возвращает (cm3, минуты, по-цветам)."""
    work = config.SLICE_DIR / f"native_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        machine_arg: Path = machine
        try:
            machine_json = work / "machine.json"
            machine_json.write_text(json.dumps(_flatten_profile(machine)), encoding="utf-8")
            machine_arg = machine_json
        except Exception:  # noqa: BLE001
            log.warning("machine flatten failed (native), using raw profile", exc_info=True)
        outdir = work / "out"
        outdir.mkdir(parents=True, exist_ok=True)
        be = backend or slicers.for_kind(None)
        args = be.slice_args(exe or be.path(), settings=[str(machine_arg)],
                             arrange=None,     # раскладка авторская — не переразмещаем
                             outdir=str(outdir), inputs=[str(model_path)])
        rc, out, err = await _exec_slicer(args)   # с семафором, таймаутом и повтором при крэше старта
        try:
            volume, minutes = _parse_gcode_dir(outdir, be)
        except SlicerError:
            # Часть падений (exit -24/-4) идёт с ПУСТЫМ stderr — реальная причина у Orca/Elegoo
            # CLI часто уходит в stdout. Логируем оба хвоста + модель, чтобы падение было
            # диагностируемым (аудит 07.2026: тихие креши без stderr).
            if rc or err or out:
                log.warning("Нативный слайс без gcode (exit %s) model=%s\n  stderr: %s\n  stdout: %s",
                            rc, model_path.name,
                            (err.decode("utf-8", "ignore")[-400:] if err else "<пусто>"),
                            (out.decode("utf-8", "ignore")[-400:] if out else "<пусто>"))
            # Аварийный крэш слайсера ПОСЛЕ повтора (обычно занята VRAM — переполнение стека в
            # GL-контексте, OpenTodo afe841b8): вызывающий (calcservice.compute) уже фолбэчит на
            # аналитическую ≈оценку. Сюда — только если и оценка невозможна; тогда честный совет.
            if _looks_like_crash(rc):
                raise SlicerError(
                    "Слайсер сейчас недоступен (возможно, занята видеопамять). Оформите заказ "
                    "без сметы — мастер посчитает вручную."
                ) from None
            raise
        try:
            volumes_cm3 = _parse_filament_cm3_list(outdir, be)
        except Exception:  # noqa: BLE001
            volumes_cm3 = []
        return volume, minutes, volumes_cm3
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _resolve_slice_inputs(machine_profile: str, process_dir: str, base_process_name: str,
                          filament_profile: str, slicer_kind: str, slicer_path: str
                          ) -> tuple[slicers.SlicerBackend, str, Path, Path, Path]:
    """Движок + три профиля (машина / процесс / филамент) одного слайса.

    Вынесено из `slice_unit` БЕЗ изменения поведения: тот же порядок проверок, те же тексты
    ошибок и та же подмена ненайденного филамента стоковым с предупреждением. Второй
    потребитель — `slice_to_gcode` (допечатка без сбойной детали, app/reprint.py): разрешать
    профили там второй копией кода значило бы, что допечатка однажды поедет не тем, чем ехала
    смета, и разойдётся с ней расходом."""
    be = slicers.for_kind(slicer_kind)
    exe = be.path(slicer_path)
    if not exe or not Path(exe).exists():
        raise SlicerError(f"Слайсер не найден ({be.title}): {exe or 'путь не задан'}")
    machine = Path(machine_profile)
    if not machine.exists():
        raise SlicerError(f"Профиль принтера не найден: {machine}")
    base_process = Path(process_dir) / base_process_name
    if not base_process.exists():
        raise SlicerError(f"Профиль процесса не найден: {base_process}")
    filament = Path(filament_profile) if filament_profile else config.DEFAULT_FILAMENT_PROFILE
    if not filament.exists():
        # Контракт прежний (падать нельзя — это сломало бы расчёт), но молчать нельзя тем
        # более: филамент мастера подменяется стоком, а с ним уезжают поток и MVS, то есть
        # калибровка теряется без единой строчки в логе. На чужой машине это ОСНОВНОЙ сценарий:
        # пользовательские пресеты живут в %APPDATA% и физически туда не приезжают.
        if filament_profile:
            log.warning("Профиль филамента не найден (%s) — режем стоковым %s. %s",
                        filament_profile, config.DEFAULT_FILAMENT_PROFILE,
                        profilepaths.explain(filament_profile, slicer_kind, slicer_path))
        filament = config.DEFAULT_FILAMENT_PROFILE
    return be, exe, machine, base_process, filament


def _write_job_profiles(work: Path, machine: Path, base_process: Path,
                        overrides: dict | None = None) -> tuple[Path, Path]:
    """Разложить в `work` профили ОДНОГО запуска слайсера → (машинный профиль, процесс).

    Машинный профиль расплющиваем: кинематические лимиты (machine_max_acceleration_x/y=20000,
    machine_max_speed_x/y=500) лежат в родителе fdm_elegoo_3dp_001_common, а планировщик
    слайсера СЧИТАЕТ время именно по ним. CLI их по `inherits` не дотягивал → расчётное время
    раздувалось ~×1.7 даже при верных скоростях/ускорениях в тулпасе (заказ #20: 13.5 ч вместо
    ~8 ч). Любой сбой расплющивания → сырой файл (прежнее поведение)."""
    work.mkdir(parents=True, exist_ok=True)
    try:
        machine_name = json.loads(machine.read_text(encoding="utf-8")).get("name", "")
    except Exception:  # noqa: BLE001
        machine_name = ""
    proc_json = work / "process.json"
    _build_process_json(base_process, proc_json, machine_name, overrides)
    machine_arg: Path = machine
    try:
        machine_json = work / "machine.json"
        machine_json.write_text(json.dumps(_flatten_profile(machine)), encoding="utf-8")
        machine_arg = machine_json
    except Exception:  # noqa: BLE001
        log.warning("machine flatten failed, using raw profile", exc_info=True)
    return machine_arg, proc_json


async def slice_to_gcode(parts: list[Path], out_dir: Path, *, machine_profile: str,
                         process_dir: str, base_process_name: str, filament_profile: str,
                         slicer_kind: str = "", slicer_path: str = "",
                         overrides: dict | None = None) -> tuple[Path, float, float]:
    """Слайс набора деталей В ФАЙЛ: (путь к gcode, объём cm³, минуты). БЕЗ КЭША.

    `slice_unit` — ИЗМЕРИТЕЛЬ: он работает во временном каталоге и сносит его в `finally`, а
    при попадании в кэш вообще не запускает слайсер (`cached=True` без единого файла на диске).
    Допечатке без сбойной детали (app/reprint.py) нужен ФАЙЛ, поэтому здесь отдельная
    ПРОИЗВОДЯЩАЯ точка входа: те же кирпичи (`_resolve_slice_inputs`, `_write_job_profiles`,
    `be.slice_args`, `_exec_slicer`, `_parse_gcode_dir`), но результат остаётся лежать в
    `out_dir`, и кэш не трогается НИ на чтение, НИ на запись — иначе «успешная» допечатка
    однажды вернула бы путь к несуществующему gcode.

    Ни высоты слоя, ни заполнения, ни стенок здесь нет сознательно: с LOGIC_VERSION 2 слайс
    идёт по профилю процесса КАК ЕСТЬ (предвыбор настроек убран), и в `slice_unit` эти числа
    участвуют только в ключе кэша. Настройки автора проекта передаются `overrides`
    (`_project_overrides`) — ровно как на пресетном пути `slice_unit`."""
    if not parts:
        raise SlicerError("Нечего резать: список деталей пуст.")
    be, exe, machine, base_process, filament = _resolve_slice_inputs(
        machine_profile, process_dir, base_process_name, filament_profile,
        slicer_kind, slicer_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    machine_arg, proc_json = _write_job_profiles(out_dir / "_profiles", machine, base_process,
                                                 overrides)
    args = be.slice_args(exe, settings=[str(machine_arg), str(proc_json)],
                         filament=str(filament), arrange="1", outdir=str(out_dir),
                         inputs=[str(p) for p in parts])
    rc, out, err = await _exec_slicer(args)
    try:
        volume, minutes = _parse_gcode_dir(out_dir, be)
    except SlicerError:
        log.warning("Слайсер не дал gcode для допечатки (exit %s) parts=%s\n"
                    "  stderr: %s\n  stdout: %s", rc, [Path(p).name for p in parts],
                    (err.decode("utf-8", "ignore")[-400:] if err else "<пусто>"),
                    (out.decode("utf-8", "ignore")[-400:] if out else "<пусто>"))
        raise
    plates = sorted(out_dir.glob("plate_*.gcode")) or sorted(out_dir.glob("*.gcode"))
    if not plates:
        raise SlicerError("Слайсер не оставил файла задания.")
    if len(plates) > 1:
        # Оставшиеся детали не уместились на один стол. Допечатать их одним заданием нельзя, а
        # молча взять первую плиту значило бы недопечатать часть деталей и не сказать об этом.
        raise SlicerError(f"Оставшиеся детали разложились на {len(plates)} стол(ов) — "
                          "допечатать одним заданием нельзя.")
    return plates[0], volume, minutes


async def slice_unit(
    model_paths: list[Path],
    *,
    machine_profile: str,
    process_dir: str,
    base_process_name: str,
    filament_profile: str,
    layer_height: float,
    infill_pct: int,
    walls: int,
    plates_only: list[int] | None = None,
    scale: float = 1.0,
    bed: BedSpec = DEFAULT_BED,
    slicer_kind: str = "",
    slicer_path: str = "",
) -> SliceResult:
    """Слайсит набор моделей как ОДНУ единицу; возвращает объём + время (с кэшем).

    plates_only — выбранные столы мультиплейт-модели (0-базные индексы, OpenTodo 77dc9b01):
    режем и суммируем только их. None/полный набор — прежний расчёт целиком.

    scale — равномерный масштаб модели (1.0 = как загружена): меши масштабируются при
    подготовке, слайс идёт по масштабированной геометрии (стенки/поддержки не линейны по
    масштабу, поэтому именно переслайс, а не умножение объёма). Валидность против стола
    проверяет ВЫЗЫВАЮЩИЙ (calcservice кламп по geometry.scale_limit) — здесь не проверяем.

    bed — стол принтера (app.bedspec). Он решает дедуп наложенных копий, гейт мультиплейта и
    раскладку по столам, то есть ОБЪЁМ и число столов. Дефолт оставлен ради вызовов, у которых
    принтера под рукой нет, но боевой путь обязан передавать стол ЯВНО: иначе честный слайс
    судит по CC2, а быстрый расчёт — по реальному столу, и два ценовых пути разъезжаются
    на принтере с другим столом. Именно этой половины не хватало в первой редакции волны 6.

    slicer_kind/slicer_path — каким движком резать (ElegooSlicer / Bambu Studio / Orca) и где он
    лежит. Пусто — движок по умолчанию, то есть прежнее поведение. Вид движка ОБЯЗАН попадать в
    ключ кэша: иначе смена слайсера вернула бы старые cm³ и минуты как свои — деньги клиента без
    единой строчки в логе."""
    be, exe, machine, base_process, filament = _resolve_slice_inputs(
        machine_profile, process_dir, base_process_name, filament_profile,
        slicer_kind, slicer_path)

    plates_only = sorted({int(i) for i in plates_only}) if plates_only else None
    scale = float(scale or 1.0)
    scale_pct = round(scale * 100)
    if scale_pct == 100:
        scale = 1.0   # нормализуем «почти 1» → ровно 1: прежний путь и прежний ключ кэша

    # --- НАТИВНЫЙ путь (OpenTodo 463c5e9e): проект-3mf слайсится со СВОИМИ настройками ---
    # Владелец: «не выставлять самим настройки в 3mf — у модели они уже идеально выставлены,
    # максимум поменять принтер на наш». Пресеты качества/прочности здесь игнорируются
    # (клиент их всё равно не выбирает, 8c8199d1), поэтому и в ключе кэша их нет. Масштаб
    # ≠100% и выбор столов требуют пересборки геометрии → прежний путь. Любой сбой → честный
    # фолбэк на прежний STL-путь с пресетами ниже.
    if (len(model_paths) == 1 and Path(model_paths[0]).suffix.lower() == ".3mf"
            and scale == 1.0 and not plates_only
            and inspect3mf.has_project_settings(model_paths[0])):
        nkey = cache.make_key(
            model_paths, machine_profile=str(machine), process="__native__",
            layer_height=0.0, infill=0, walls=0, filament="__project__",
            extra=f"logic:{LOGIC_VERSION};native:1;slicer:{be.kind}")
        nhit = cache.get(nkey)
        if nhit:
            return SliceResult(nhit["volume_cm3"], nhit["minutes"], cached=True,
                               volumes_cm3=nhit.get("volumes_cm3") or [])
        try:
            log.info("Slicing NATIVE (родные настройки проекта): %s", Path(model_paths[0]).name)
            volume, minutes, volumes_cm3 = await _native_slice(
                Path(model_paths[0]), machine, be, exe)
            if volume > 0 and minutes > 0:
                cache.put(nkey, {"volume_cm3": volume, "minutes": minutes,
                                 "volumes_cm3": volumes_cm3})
                return SliceResult(volume_cm3=volume, minutes=minutes, cached=False,
                                   volumes_cm3=volumes_cm3)
            log.warning("Нативный слайс дал нулевой объём/время — фолбэк на путь с пресетами")
        except SlicerError as exc:
            log.warning("Нативный слайс не удался (%s) — фолбэк на путь с пресетами", exc)
    # --- /нативный путь. -----------

    # Нативный слайс не применим (масштаб / выбор столов / это не проект-3mf), но если настройки
    # автора у файла есть — режем ИМИ, а не плотностью профиля бота: иначе масштабированная
    # модель стоила бы дороже себя же в 100% (тикет клиента 26.07.2026, см. _project_overrides).
    overrides = _project_overrides(model_paths)
    key = cache.make_key(
        model_paths, machine_profile=str(machine), process=base_process_name,
        layer_height=layer_height, infill=infill_pct, walls=walls, filament=str(filament),
        extra=f"logic:{LOGIC_VERSION};slicer:{be.kind}"
              + (";plates:" + ",".join(map(str, plates_only)) if plates_only else "")
              + (f";scale:{scale_pct}" if scale != 1.0 else "")
              + (";proj:1" if overrides else ""),
    )
    hit = cache.get(key)
    if hit:
        return SliceResult(hit["volume_cm3"], hit["minutes"], cached=True,
                           volumes_cm3=hit.get("volumes_cm3") or [])

    work = config.SLICE_DIR / f"job_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        machine_arg, proc_json = _write_job_profiles(work, machine, base_process, overrides)
        if overrides:
            log.info("Слайс с настройками ПРОЕКТА (натив недоступен): %s",
                     ", ".join(f"{k}={v}" for k, v in overrides.items()))

        async def _run_slice(parts: list[Path], outdir: Path, arrange: str = "1") -> tuple[float, float]:
            outdir.mkdir(parents=True, exist_ok=True)
            # arrange="1" — слайсер сам раскладывает детали на стол (обычный путь). arrange="0" —
            # режем как есть, без переразмещения (фолбэк _slice_group для деталей ~со стол, что
            # --arrange отвергает: их предварительно центрируют через _center_on_bed).
            # Сборка аргументов — в app/slicers.py: три движка семейства Orca различаются не
            # флагами, а тем, что вокруг (санитайзер, пути профилей), и держать эту строку в
            # трёх копиях значило бы разъехаться на первом же различии.
            args = be.slice_args(exe, settings=[str(machine_arg), str(proc_json)],
                                 filament=str(filament), arrange=arrange,
                                 outdir=str(outdir), inputs=[str(p) for p in parts])
            # Глобальный потолок ОДНОВРЕМЕННЫХ процессов слайсера держим вокруг САМОГО subprocess'а
            # (а не вокруг всего slice_unit) — так плиты одной модели режутся параллельно
            # (_slice_plates_sum через gather), упираясь в общий лимит MAX_CONCURRENT_SLICES, а не
            # блокируя друг друга под одним permit'ом.
            # _exec_slicer: семафор MAX_CONCURRENT_SLICES + таймаут + повтор при крэше старта GL/VRAM.
            rc, out, err = await _exec_slicer(args)
            try:
                return _parse_gcode_dir(outdir, be)
            except SlicerError:
                # stderr+stdout в лог: тихие креши (-24/-4) идут с пустым stderr, причина в stdout.
                if rc or err or out:
                    log.warning("Слайсер не дал gcode (exit %s) parts=%s\n  stderr: %s\n  stdout: %s",
                                rc, [Path(p).name for p in parts],
                                (err.decode("utf-8", "ignore")[-400:] if err else "<пусто>"),
                                (out.decode("utf-8", "ignore")[-400:] if out else "<пусто>"))
                # Отказ НЕ движка по умолчанию — отдельная громкая строка. Флаги CLI для Bambu
                # Studio и Orca взяты от общего предка и живьём не проверялись; вызывающий
                # молча подменит результат приблизительной оценкой, и расхождение будет видно
                # только по деньгам. Ошибка обязана называть движок вслух.
                if be.kind != slicers.DEFAULT_KIND:
                    log.error("Слайсер %s не дал gcode (exit %s). Если это первый заказ на этом "
                              "движке — проверьте его кнопкой «проверить слайсер»: сборка "
                              "команды под него могла не подойти.", be.title, rc)
                raise

        # --- Мультиплейт-проект: честно режем КАЖДЫЙ стол отдельно и суммируем -----------
        # (баг Mark 85: раньше детали ВСЕХ столов лумпились в один --arrange → вес/время
        # множились по столам или молча обрезались). Гейт строго на >1 стол; одноплейтные
        # модели сюда не заходят и идут прежним, проверенным путём ниже без изменений.
        plate_groups = await cpupool.run(_prepare_plates, model_paths, scale, bed)
        if plate_groups:
            if plates_only:
                # Пользователь выбрал не все столы (OpenTodo 77dc9b01) — режем только выбранные.
                picked = [plate_groups[i] for i in plates_only if 0 <= i < len(plate_groups)]
                if not picked or not any(picked):
                    raise SlicerError("На выбранных столах нет печатаемых деталей — "
                                      "проверьте выбор столов.")
                plate_groups = picked
            log.info("Slicing %d стол(ов) by-plate [%d input] with %s — суммирую%s",
                     len([g for g in plate_groups if g]), len(model_paths), base_process_name,
                     f" (выбраны столы {plates_only})" if plates_only else "")
            volume, minutes = await _slice_plates_sum(plate_groups, work, _run_slice, bed)
            cache.put(key, {"volume_cm3": volume, "minutes": minutes})
            return SliceResult(volume_cm3=volume, minutes=minutes, cached=False)
        elif plates_only:
            # Подмножество столов запрошено, но разделить модель по столам не вышло — честно
            # отказываем, а не считаем молча ВСЮ модель по цене выбранных столов.
            raise SlicerError("Не удалось разделить модель по столам — посчитайте её целиком.")
        # --- /мультиплейт. -----------

        # --- Модель-ТИРАЖ (один меш ×N, напр. «100 mini ducks»): режем ОДНУ единицу и
        # умножаем на количество. Иначе материализация N×1М-меша валит воркер по памяти
        # (OOM → BrokenProcessPool → бот перезапускается по кругу) и дико тормозит слайс.
        # Гейт — высокая повторность (geometry.unit_groups); обычные сборки сюда не заходят.
        unit_groups = await cpupool.run(_prepare_unit_groups, model_paths, scale, bed)
        if unit_groups:
            n_total = sum(c for _, c in unit_groups)
            log.info("Slicing ТИРАЖ: %d уник. ед. → %d инстансов ×count with %s",
                     len(unit_groups), n_total, base_process_name)
            volume, minutes = 0.0, 0.0
            for i, (stls, count) in enumerate(unit_groups):
                v, m = await _slice_group(stls, work / f"unit_{i}", _run_slice, bed=bed)
                volume += v * count
                minutes += m * count
            if volume <= 0 or minutes <= 0:
                raise SlicerError("Слайсер вернул нулевой объём/время — проверьте модель.")
            cache.put(key, {"volume_cm3": volume, "minutes": minutes})
            return SliceResult(volume_cm3=volume, minutes=minutes, cached=False)
        # --- /тираж. Дальше — ИСХОДНЫЙ единый путь (одноплейтные модели) -----------

        prepared = await cpupool.run(_prepare_models, model_paths, scale, bed)
        if not prepared:
            raise SlicerError("нет геометрии для слайсинга.")
        if scale != 1.0:
            # Файл, который не удалось масштабировать, вернулся оригиналом (см. _prepare_models) —
            # отказ здесь, в основном процессе (исключение в cpupool-воркере «ломает» пул).
            originals = {str(Path(p)) for p in model_paths}
            bad = [Path(p).name for p in prepared if str(Path(p)) in originals]
            if bad:
                raise SlicerError("Не удалось масштабировать модель "
                                  f"({', '.join(bad)}) — попробуйте масштаб 100%.")
        log.info("Slicing %d part(s) [%d input] with %s",
                 len(prepared), len(model_paths), base_process_name)
        volumes_cm3: list[float] = []
        try:
            # Как и раньше: один проход — слайсер сам разложит детали на стол (--arrange).
            volume, minutes = await _run_slice(prepared, work)
            # Пер-филаментный объём для разбивки склада по цветам — только на этом простом
            # одно-проходном пути (gcode целиком в work). Любая ошибка → пусто (фолбэк).
            try:
                volumes_cm3 = _parse_filament_cm3_list(work, be)
            except Exception:  # noqa: BLE001
                volumes_cm3 = []
        except SlicerError:
            # Крупные мультиплейт-сборки не умещаются на один стол → ElegooSlicer падает без
            # gcode (exit -101). Бьём детали по столам (bin-packing) и слайсим каждую плиту
            # отдельным запуском, суммируя объём+время — иначе крупная модель вообще не даёт
            # смету (а не просто заниженную). Модели, что и так резались, идут прежним путём.
            if len(prepared) <= 1:
                raise
            sized = await cpupool.run(_sizes, prepared)
            bins = _pack_into_beds(sized, bed=bed)
            if len(bins) <= 1:
                raise                      # не удалось осмысленно разбить — отдаём исходную ошибку
            log.warning("Сборка не разложилась на один стол — режу по %d плит(ам) и суммирую",
                        len(bins))
            volume, minutes = 0.0, 0.0
            for i, group in enumerate(bins):
                v, m = await _slice_group(group, work / f"bin_{i}", _run_slice, bed=bed)
                volume += v
                minutes += m
        if volume <= 0 or minutes <= 0:
            raise SlicerError("Слайсер вернул нулевой объём/время — проверьте модель.")
        cache.put(key, {"volume_cm3": volume, "minutes": minutes, "volumes_cm3": volumes_cm3})
        return SliceResult(volume_cm3=volume, minutes=minutes, cached=False,
                           volumes_cm3=volumes_cm3)
    finally:
        shutil.rmtree(work, ignore_errors=True)
