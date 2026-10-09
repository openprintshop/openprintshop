"""Санитайзер проект-3mf из BambuStudio под ElegooSlicer / Elegoo Centauri Carbon 2.

ЗАЧЕМ. Модели, которые автор готовил в BambuStudio (Application=BambuStudio-*,
printer_model=Bambu/BBL), в ElegooSlicer НЕ слайсятся — и это касается ВСЕХ таких моделей,
не одной. Разведано вживую на ElegooSlicer 1.5.2.2 (движок Slic3r-CLI 2.3.1), два барьера:

  1. Версионный гейт: «File Version 2.4.0.70 not supported by current cli version 2.3.1»
     (версия читается из 3D/3dmodel.model, metadata name="Application").
  2. Валидация конфига падает на бамбу-специфичных значениях: tree_support_wall_count=-1
     и raft_first_layer_expansion=-1 (движок ждёт диапазоны [0,2]/[0,∞)), wall_filament/
     solid_infill_filament/sparse_infill_filament=0 (ждёт ≥1), плюс ссылки на несуществующие
     BBL-пресеты (Bambu Lab X1E, 0.20mm Standard @BBL X1C).

Патчить бамбу-конфиг по одному полю бесполезно (500 ключей vs 548 у Elegoo, 48 «бамбу-только»
и 96 недостающих Elegoo-ключей). Поэтому sanitize() ЗАМЕНЯЕТ project_settings.config целиком
на валидный конфиг B-профиля Centauri Carbon 2 (шаблон assets/ecc2_b_project_template.json,
снят с реального проекта, сохранённого ElegooSlicer), переносит из оригинала N филаментов
(материал + цвет), подставляет по каждому материалу профиль пластика (из базы бота, иначе
стандартный Elegoo), ставит плиту по доминирующему материалу (PLA → сторона Б, иначе → сторона
А) и нейтрализует версию, чтобы файл открывался/слайсился без гейта. Мешь и раскладку
(3dmodel.model geometry, Objects/, model_settings.config) НЕ трогаем.

Оригинал сохраняется рядом как <имя>.bambu.bak (обратимо). Функция идемпотентна: после
санации is_bambu_project() уже False, повторный вызов — no-op.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from . import config, materials
from .slicer import _flatten_profile

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).resolve().parent / "assets" / "ecc2_b_project_template.json"

# curr_bed_type — enum-ключи ElegooSlicer (выяснено из ElegooSlicer.dll; в GUI это русские
# подписи «Текстурированная пластина (сторона Б/А)»). Двусторонняя пластина Centauri Carbon 2.
BED_PLA = "Smooth Build Plate (Side B)"       # сторона Б — для PLA (владелец: B = для PLA)
BED_OTHER = "Textured Build Plate (Side A)"   # сторона А — для всего остального

# Строка Application, которой заменяем бамбу-версию (ниже гейта 2.3.1 → без «not supported»).
_APP_VERSION = "ElegooSlicer-1.5.2.2"

# Авторитетный набор ПОФИЛАМЕНТНЫХ ключей конфига ElegooSlicer (выведен диффом реальных
# Elegoo-проектов N=1 против N=4/5). ВСЕ они в готовом конфиге обязаны иметь длину N —
# иначе движок падает на «vector of different length». Заполняем из выбранного профиля
# пластика, недостающее — дефолтом из шаблона.
_PER_FILAMENT_KEYS: frozenset[str] = frozenset({
    "activate_air_filtration", "activate_chamber_temp_control", "adaptive_pressure_advance",
    "adaptive_pressure_advance_bridges", "adaptive_pressure_advance_model",
    "adaptive_pressure_advance_overhangs", "additional_cooling_fan_speed", "chamber_temperature",
    "close_fan_the_first_x_layers", "complete_print_exhaust_fan_speed", "cool_plate_temp",
    "cool_plate_temp_initial_layer", "default_filament_colour", "dont_slow_down_outer_wall",
    "during_print_exhaust_fan_speed", "enable_overhang_bridge_fan", "enable_pressure_advance",
    "eng_plate_temp", "eng_plate_temp_initial_layer", "fan_cooling_layer_time", "fan_max_speed",
    "fan_min_speed", "filament_colour", "filament_cooling_final_speed",
    "filament_cooling_initial_speed", "filament_cooling_moves", "filament_cost", "filament_density",
    "filament_deretraction_speed", "filament_diameter", "filament_end_gcode", "filament_flow_ratio",
    "filament_ids", "filament_is_support", "filament_loading_speed", "filament_loading_speed_start",
    "filament_long_retractions_when_cut", "filament_max_volumetric_speed",
    "filament_minimal_purge_on_wipe_tower", "filament_multitool_ramming",
    "filament_multitool_ramming_flow", "filament_multitool_ramming_volume", "filament_notes",
    "filament_ramming_parameters", "filament_retract_before_wipe", "filament_retract_lift_above",
    "filament_retract_lift_below", "filament_retract_lift_enforce", "filament_retract_restart_extra",
    "filament_retract_when_changing_layer", "filament_retraction_distances_when_cut",
    "filament_retraction_length", "filament_retraction_minimum_travel", "filament_retraction_speed",
    "filament_settings_id", "filament_shrink", "filament_shrinkage_compensation_z",
    "filament_soluble", "filament_stamping_distance", "filament_stamping_loading_speed",
    "filament_start_gcode", "filament_toolchange_delay", "filament_type", "filament_unloading_speed",
    "filament_unloading_speed_start", "filament_vendor", "filament_wipe", "filament_wipe_distance",
    "filament_z_hop", "filament_z_hop_types", "full_fan_speed_layer", "hot_plate_temp",
    "hot_plate_temp_initial_layer", "idle_temperature", "internal_bridge_fan_speed",
    "ironing_fan_speed", "nozzle_temperature", "nozzle_temperature_initial_layer",
    "nozzle_temperature_range_high", "nozzle_temperature_range_low", "overhang_fan_speed",
    "overhang_fan_threshold", "pellet_flow_coefficient", "pressure_advance",
    "reduce_fan_stop_start_freq", "required_nozzle_HRC", "slow_down_for_layer_cooling",
    "slow_down_layer_time", "slow_down_min_speed", "supertack_plate_temp",
    "supertack_plate_temp_initial_layer", "support_material_interface_fan_speed",
    "temperature_vitrification", "textured_cool_plate_temp", "textured_cool_plate_temp_initial_layer",
    "textured_plate_temp", "textured_plate_temp_initial_layer",
})

# Идентификационные пофиламентные поля выставляем явно (не из профиля), поэтому из общего
# заполнения их исключаем.
_IDENTITY_KEYS = frozenset({
    "filament_settings_id", "filament_type", "filament_colour", "filament_ids", "filament_vendor",
})


def is_bambu_project(path: str | Path) -> bool:
    """3mf ли это проект, подготовленный в BambuStudio (и потому непригодный к слайсу as-is).

    Признаки (любой достаточен): printer_model/printer_settings_id указывает на Bambu/BBL,
    либо Application в 3dmodel.model = BambuStudio-*, либо конфиг содержит бамбу-сентинелы
    (tree_support_wall_count=-1 / *_filament=0), которые движок Elegoo отвергает."""
    p = Path(path)
    if p.suffix.lower() != ".3mf":
        return False
    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
            if "Metadata/project_settings.config" not in names:
                return False   # не проект (голая геометрия) — трогать нечего
            cfg = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "replace"))
            pid = str(cfg.get("printer_settings_id", "")) + " " + str(cfg.get("printer_model", ""))
            if re.search(r"bambu|\bBBL\b|\bX1|\bP1|\bA1\b", pid, re.I):
                return True
            if str(cfg.get("tree_support_wall_count")) == "-1":
                return True
            if "3D/3dmodel.model" in names:
                head = z.read("3D/3dmodel.model")[:4000].decode("utf-8", "replace")
                if re.search(r'name="Application"[^>]*>\s*BambuStudio', head):
                    return True
    except Exception:  # noqa: BLE001
        log.debug("is_bambu_project: не удалось прочитать %s", p, exc_info=True)
    return False


def _default_resolver(material: str) -> str:
    """Профиль филамента для типа материала — стандартный Elegoo из папки принтера.

    Фолбэк, если вызывающий не передал резолвер из базы бота (см. resolver в sanitize)."""
    return materials.resolve_filament(
        config.FILAMENT_DIR, material, fallback=str(config.DEFAULT_FILAMENT_PROFILE))


def _flatten_filament(profile_path: str) -> dict:
    """Расплющенный (по inherits) профиль филамента → {ключ: значение}. Пусто при сбое."""
    try:
        p = Path(profile_path)
        if p.exists():
            return _flatten_profile(p)
    except Exception:  # noqa: BLE001
        log.debug("не удалось расплющить профиль филамента %s", profile_path, exc_info=True)
    return {}


def _hex6(colour: str) -> str:
    """#RRGGBBAA / #RRGGBB / RRGGBB → #RRGGBB (шаблон Elegoo хранит цвета как #RRGGBB)."""
    c = (colour or "").strip().lstrip("#")
    if len(c) >= 6 and re.fullmatch(r"[0-9a-fA-F]+", c[:6]):
        return "#" + c[:6].upper()
    return "#FFFFFF"


def _detect_filaments(cfg: dict) -> list[tuple[str, str]]:
    """Список (материал, цвет#RRGGBB) из бамбу-конфига. Пусто → один PLA белый."""
    types = cfg.get("filament_type") or []
    cols = cfg.get("filament_colour") or []
    if not isinstance(types, list):
        types = [types]
    if not isinstance(cols, list):
        cols = [cols]
    n = max(len(types), len(cols), 1)
    out: list[tuple[str, str]] = []
    for i in range(n):
        mat = str(types[i]) if i < len(types) and types[i] else "PLA"
        col = _hex6(cols[i]) if i < len(cols) else "#FFFFFF"
        out.append((mat, col))
    return out


def _dominant_material(filaments: list[tuple[str, str]]) -> str:
    counts: dict[str, int] = {}
    for mat, _ in filaments:
        counts[mat] = counts.get(mat, 0) + 1
    # больше всего слотов; при равенстве — первый по порядку модели
    return max(filaments, key=lambda f: counts[f[0]])[0] if filaments else "PLA"


def _bed_for(material: str) -> str:
    return BED_PLA if material.upper().startswith("PLA") else BED_OTHER


def _perfil_value(flat: dict, key: str, default):
    """Значение пофиламентного ключа из расплющенного профиля (list→[0]) или дефолт шаблона."""
    v = flat.get(key)
    if isinstance(v, list) and v:
        return v[0]
    if v is not None and not isinstance(v, list):
        return v
    return default


def build_config(orig_cfg: dict, resolver: Callable[[str], str]) -> dict:
    """Валидный Elegoo-конфиг из шаблона B-профиля + N филаментов оригинала (материал+цвет)."""
    base: dict = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
    filaments = _detect_filaments(orig_cfg)
    n = len(filaments)

    # Профиль пластика на каждый филамент (из базы бота через resolver; иначе стандартный Elegoo).
    flats: list[dict] = []
    prof_names: list[str] = []
    for mat, _ in filaments:
        prof_path = ""
        try:
            prof_path = resolver(mat) or ""
        except Exception:  # noqa: BLE001
            log.debug("resolver упал на материале %s", mat, exc_info=True)
        if not prof_path:
            prof_path = _default_resolver(mat)
        flat = _flatten_filament(prof_path)
        flats.append(flat)
        prof_names.append(str(flat.get("name") or Path(prof_path).stem or f"Elegoo {mat} @ECC2"))

    # Пофиламентные массивы (не идентификационные) — длина N, значения из профиля/дефолта шаблона.
    for key in _PER_FILAMENT_KEYS:
        if key in _IDENTITY_KEYS:
            continue
        tmpl_v = base.get(key)
        default = tmpl_v[0] if isinstance(tmpl_v, list) and tmpl_v else tmpl_v
        base[key] = [_perfil_value(flats[i], key, default) for i in range(n)]

    # Идентификация филаментов — явно.
    base["filament_settings_id"] = list(prof_names)
    base["filament_type"] = [mat for mat, _ in filaments]
    base["filament_colour"] = [col for _, col in filaments]
    base["filament_vendor"] = [str(flats[i].get("filament_vendor") or "Elegoo")
                               if not isinstance(flats[i].get("filament_vendor"), list)
                               else (flats[i]["filament_vendor"][0] if flats[i]["filament_vendor"] else "Elegoo")
                               for i in range(n)]
    base["filament_ids"] = [_perfil_value(flats[i], "filament_id", "") or "EPLAB00" for i in range(n)]
    base["default_filament_profile"] = [prof_names[0]]

    # Промывочные массивы согласованной длины (иначе движок падает): вектор 2N, матрица N×N
    # (диагональ 0), wiping_volumes_extruders всегда 10 (как во всех реальных Elegoo-проектах).
    base["flush_volumes_vector"] = ["140"] * (2 * n)
    base["flush_volumes_matrix"] = ["0" if i == j else "280" for i in range(n) for j in range(n)]
    base["wiping_volumes_extruders"] = ["70"] * 10

    # Плита по доминирующему материалу.
    base["curr_bed_type"] = _bed_for(_dominant_material(filaments))

    base["different_settings_to_system"] = []
    return base


def _build_item_transforms(model_xml: str) -> dict[str, str]:
    """objectid → transform из <build><item …> в 3dmodel.model (позиция объекта на плите)."""
    out: dict[str, str] = {}
    for tag in re.findall(r"<item\b[^>]*>", model_xml):
        oid = re.search(r'objectid="([^"]+)"', tag)
        tr = re.search(r'transform="([^"]+)"', tag)
        if oid and tr:
            out[oid.group(1)] = tr.group(1)
    return out


# Настройки уровня объекта, которые безопасно переносим в чистый model_settings (авторские
# намерения по поддержкам/шву). Всё остальное (битые метаданные MakerLab) отбрасываем.
_SAFE_OBJECT_KEYS = ("enable_support", "support_type", "support_threshold_angle", "seam_position",
                     "brim_type", "support_filament")

# Метаданные плиты, которые переносим (превью). gcode_file и пр. — намеренно НЕ переносим.
_PLATE_THUMBS = ("thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file")


def _obj_meta(block: str, key: str) -> str | None:
    m = re.search(rf'key="{re.escape(key)}"\s+value="([^"]*)"', block)
    return m.group(1) if m else None


def _rebuild_model_settings(model_settings: bytes, model_xml: bytes) -> bytes:
    """Пересобрать model_settings.config в ЧИСТОЙ структуре, которую ElegooSlicer принимает.

    ПОЧЕМУ. Проект-3mf из MakerLab (Image-to-3D) несёт model_settings.config, из-за структуры
    которого ElegooSlicer НЕ ставит объект на плиту: лог «total 1 models, 0 objects», слайс
    молча ничего не даёт (exit 0, без gcode), и ни --arrange, ни заполнение <assemble> не
    помогают (разведано: тот же меш в структуре нормального Elegoo-проекта режется без проблем —
    значит дело именно в обёртке model_settings). Точечно вычистить не удаётся, поэтому
    пересобираем секцию заново по рабочему образцу: для каждого объекта — чистые <part>/<mesh_stat>
    и белый список авторских настроек (поддержки/шов); для каждой плиты — <model_instance>; плюс
    непустой <assemble> с трансформом из <build><item>. Геометрию (Objects/, 3dmodel.model) НЕ
    трогаем. При нестандартной разметке (не распарсили объекты/инстансы) возвращаем оригинал —
    тогда сработает прежний STL-фолбэк расчёта."""
    txt = model_settings.decode("utf-8", "replace")
    transforms = _build_item_transforms(model_xml.decode("utf-8", "replace"))

    objects: list[tuple[str, str, list[str]]] = []   # (id, name, [safe-metadata-строки])
    for om in re.finditer(r'<object id="([^"]+)">(.*?)</object>', txt, re.S):
        oid, body = om.group(1), om.group(2)
        head = body.split("<part", 1)[0]              # метаданные до первого <part>
        name = _obj_meta(head, "name") or f"object_{oid}"
        extruder = _obj_meta(head, "extruder") or "1"
        safe = [f'    <metadata key="extruder" value="{extruder}"/>']
        for k in _SAFE_OBJECT_KEYS:
            v = _obj_meta(head, k)
            if v is not None:
                safe.append(f'    <metadata key="{k}" value="{v}"/>')
        objects.append((oid, name, safe))
    if not objects:
        return model_settings                         # непонятная разметка → не трогаем

    plates: list[tuple[str, str, list[tuple[str, str]]]] = []   # (plater_id, thumbs, [(obj,inst)])
    for pm in re.finditer(r"<plate>(.*?)</plate>", txt, re.S):
        pbody = pm.group(1)
        pid = _obj_meta(pbody, "plater_id") or "1"
        insts: list[tuple[str, str]] = []
        for inst in re.findall(r"<model_instance>(.*?)</model_instance>", pbody, re.S):
            oid = _obj_meta(inst, "object_id")
            if not oid:
                continue
            insts.append((oid, _obj_meta(inst, "instance_id") or "0"))
        thumbs = "".join(f'\n    <metadata key="{t}" value="{v}"/>'
                         for t in _PLATE_THUMBS if (v := _obj_meta(pbody, t)))
        plates.append((pid, thumbs, insts))
    if not any(p[2] for p in plates):
        return model_settings

    def _tr(oid: str) -> str:
        return transforms.get(oid) or "1 0 0 0 1 0 0 0 1 128 128 0"

    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for oid, name, safe in objects:
        out.append(f'  <object id="{oid}">')
        out.append(f'    <metadata key="name" value="{name}"/>')
        out.extend(safe)
        out.append('    <part id="1" subtype="normal_part">')
        out.append(f'      <metadata key="name" value="{name}"/>')
        out.append('      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>')
        out.append('      <metadata key="source_object_id" value="0"/>')
        out.append('      <metadata key="source_volume_id" value="0"/>')
        out.append('      <mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" '
                   'facets_reversed="0" backwards_edges="0"/>')
        out.append("    </part>")
        out.append("  </object>")
    identify = 1
    assemble: list[str] = []
    for pid, thumbs, insts in plates:
        out.append("  <plate>")
        out.append(f'    <metadata key="plater_id" value="{pid}"/>')
        out.append('    <metadata key="plater_name" value=""/>')
        out.append('    <metadata key="locked" value="false"/>')
        out.append('    <metadata key="filament_map_mode" value="Auto For Flush"/>')
        out.append(thumbs.lstrip("\n") and thumbs or "")
        for oid, iid in insts:
            out.append("    <model_instance>")
            out.append(f'      <metadata key="object_id" value="{oid}"/>')
            out.append(f'      <metadata key="instance_id" value="{iid}"/>')
            out.append(f'      <metadata key="identify_id" value="{identify}"/>')
            out.append("    </model_instance>")
            assemble.append(f'   <assemble_item object_id="{oid}" instance_id="{iid}" '
                            f'transform="{_tr(oid)}" offset="0 0 0" />')
            identify += 1
        out.append("  </plate>")
    out.append("  <assemble>")
    out.extend(assemble)
    out.append("  </assemble>")
    out.append("</config>")
    return "\n".join(line for line in out if line != "").encode("utf-8")


def _neutralize_version(model_xml: bytes) -> bytes:
    """3dmodel.model: Application=BambuStudio-* → ElegooSlicer-* (снимает версионный гейт)."""
    txt = model_xml.decode("utf-8", "replace")
    txt = re.sub(r'(<metadata name="Application"[^>]*>)\s*BambuStudio[^<]*(</metadata>)',
                 rf'\g<1>{_APP_VERSION}\g<2>', txt)
    return txt.encode("utf-8")


def sanitize(path: str | Path, *, resolver: Callable[[str], str] | None = None,
             backup: bool = True) -> bool:
    """Переписать бамбу-3mf на валидный Elegoo-проект НА МЕСТЕ. True — файл изменён.

    resolver(material)->profile_path подставляет вызывающий (из базы бота); None → стандартный
    Elegoo. Оригинал сохраняется как <path>.bambu.bak (если backup и копии ещё нет)."""
    p = Path(path)
    if not is_bambu_project(p):
        return False
    resolver = resolver or _default_resolver

    with zipfile.ZipFile(p) as z:
        orig_cfg = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "replace"))
        model_xml = z.read("3D/3dmodel.model") if "3D/3dmodel.model" in z.namelist() else b""
    new_cfg = build_config(orig_cfg, resolver)
    new_cfg_bytes = json.dumps(new_cfg, ensure_ascii=False, indent=1).encode("utf-8")

    if backup:
        bak = p.with_name(p.name + ".bambu.bak")
        if not bak.exists():
            shutil.copy2(p, bak)

    fd, tmp_name = tempfile.mkstemp(suffix=".3mf", dir=str(p.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(p) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "Metadata/project_settings.config":
                    data = new_cfg_bytes
                elif item.filename == "3D/3dmodel.model":
                    data = _neutralize_version(data)
                elif item.filename == "Metadata/model_settings.config":
                    data = _rebuild_model_settings(data, model_xml)
                elif item.filename == "Metadata/slice_info.config":
                    data = re.sub(rb'(X-BBL-Client-Version" value=")[^"]*"',
                                  rb'\g<1>1.5.2.2"', data)
                zout.writestr(item, data)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    log.info("bambu3mf: санирован %s (филаментов: %d, плита: %s)",
             p.name, len(new_cfg["filament_type"]), new_cfg["curr_bed_type"])
    return True
