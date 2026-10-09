"""Загрузка печатной геометрии модели с дедупликацией наложенных копий.

Некоторые .3mf содержат НЕСКОЛЬКО одинаковых копий всей модели, наложенных друг на
друга (напр. 2 динозавра почти в одной точке) — их нельзя считать и рисовать дважды.
Но другие .3mf собирают модель из МНОЖЕСТВА одинаковых сегментов, РАЗЛОЖЕННЫХ в
пространстве (флекси-осьминог из 60 одинаковых звеньев) — там все экземпляры нужны.

Правило: группируем экземпляры по ТОЧНОМУ содержимому (хэш вершин+граней). В группе:
- лежат КУЧЕЙ (разброс центров мал относительно размера экземпляра) → копии-дубли,
  оставляем ОДНУ;
- РАЗЛОЖЕНЫ (разброс велик) → это структура модели, оставляем ВСЕ.
Разные по форме детали в одну группу не попадают и не дедуплицируются никогда.

Используется и превью (app.preview), и слайсером (app.slicer) — чтобы грамовка и
картинка считались по одной и той же геометрии.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np
import trimesh

from . import config, inspect3mf
from .bedspec import DEFAULT_BED, BedSpec

log = logging.getLogger(__name__)

# ---- Предохранитель по ОЗУ (14.08.2026) -------------------------------------------------
# trimesh разбирает .3mf через XML-дерево, и пик памяти кратно больше самого XML: замер на
# живой машине — 21 МБ распакованного 3dmodel.model дают пик 0.55 ГБ, то есть ×25. В каталоге
# лежат монстры на 750 МБ XML («Pocket Dinos», «Gothic Ornamental Chess Set»), а это уже ~19 ГБ
# на одну модель; пакетный обход таких карточек уводил процесс за 40 ГБ и оставлял мастерской
# 1.8 ГБ свободных — бота в этот момент мог убить OOM.
#
# Вес РАСПАКОВАННОЙ геометрии виден из оглавления zip БЕСПЛАТНО (file_size, распаковывать не
# надо), поэтому решение принимаем ДО загрузки:
#   • MESH_SRC_LIMIT_MB — абсолютный потолок «такое не грузим никогда»;
#   • и доля СВОБОДНОЙ памяти: ожидаемый пик (вес × MESH_RAM_FACTOR) не должен съесть больше
#     MESH_RAM_SHARE от свободной ОЗУ. Машина занята — тяжёлую модель не берём, освободится —
#     возьмём. Обе крутилки переопределяются из .env, ничего не пересобирая.
MESH_SRC_LIMIT_MB = float(os.getenv("MESH_SRC_LIMIT_MB", "1000"))
MESH_RAM_FACTOR = float(os.getenv("MESH_RAM_FACTOR", "25"))
MESH_RAM_SHARE = float(os.getenv("MESH_RAM_SHARE", "0.5"))

# ---- Повторные разборы sub-model у trimesh (09.10.2026) -----------------------------------
# trimesh 4.x (exchange/threemf.py) на КАЖДЫЙ <component p:path=...> заново разбирает ВЕСЬ
# sub-model файл и дописывает его меши к геометрии компонента. Отсюда и «баг сборки
# production-3mf» (см. _trimesh_misassembled), и настоящая цена загрузки: не вес файлов, а вес ×
# число ссылок на них. «Flexi Cartoon Scorpion»: 48 МБ XML, но ~50 компонентов ссылаются на один
# 36-МБ файл → trimesh разбирал 1.8 ГБ XML 3 минуты, воркер дорос до 9.2 ГБ, система — до 96%.
# Предохранитель считал 48 МБ и пропускал. В каталоге таких бомб сотни (худшая — ×2562).
#
# Сцену, раздутую сильнее допуска _trimesh_misassembled (×1.5), тот всё равно выбросил бы в
# ручную сборку, поэтому такие файлы trimesh не отдаём вовсе (_manual_first): сразу собираем
# вручную, она читает каждый sub-model один раз.
_REPARSE_TOL = 1.5
_CORE_SCAN_MAX = 32_000_000   # ядро крупнее — это меши прямо в ядре, а не production-сборка
_COMPONENT_PATH_RE = re.compile(r'<component\b[^>]*\bp:path="([^"]+)"')


def _3mf_xml_mb(p: Path) -> tuple[float, float]:
    """(МБ распакованных «*.model»; МБ XML, которые разберёт trimesh.load). Оглавление архива
    читается без распаковки, ядро 3D/3dmodel.model у production-3mf мелкое (одни компоненты).
    Второе число ≥ первого: каждая лишняя ссылка на sub-model — ещё один его полный разбор."""
    with zipfile.ZipFile(str(p)) as z:
        sizes = {i.filename: i.file_size for i in z.infolist()
                 if i.filename.lower().endswith(".model")}
        core = ""
        if 0 < sizes.get("3D/3dmodel.model", 0) <= _CORE_SCAN_MAX:
            core = z.read("3D/3dmodel.model").decode("utf-8", "ignore")
    real = sum(sizes.values())
    refs = Counter(path.strip("/") for path in _COMPONENT_PATH_RE.findall(core))
    extra = sum((n - 1) * sizes.get(path, 0) for path, n in refs.items())
    return real / 1e6, (real + extra) / 1e6


def _manual_first(p: Path) -> bool:
    """production-3mf, который trimesh раздул бы больше допуска (см. _3mf_xml_mb) и который
    можно собрать вручную: вычитающих деталей нет (им нужен прежний путь с резкой негативов)."""
    if p.suffix.lower() != ".3mf":
        return False
    try:
        real, via_trimesh = _3mf_xml_mb(p)
        return via_trimesh > real * _REPARSE_TOL and not inspect3mf.negative_part_ids(p)
    except Exception:  # noqa: BLE001
        log.debug("_manual_first failed for %s", p, exc_info=True)
        return False


def source_mesh_mb(model_paths, *, via_trimesh: bool = False) -> float:
    """Вес XML, который РЕАЛЬНО разберёт загрузка, МБ: у .3mf — распакованные «*.model» плюс
    повторные разборы sub-model внутри trimesh (_3mf_xml_mb); файл, который уйдёт в ручную
    сборку мимо trimesh (_manual_first), — по весу самих файлов. via_trimesh=True — цена именно
    trimesh.load (для тех, кто грузит только им). У остальных форматов — размер файла.
    0.0 — посчитать не вышло (тогда предохранитель не мешает)."""
    total = 0.0
    for p in model_paths:
        p = Path(p)
        try:
            if p.suffix.lower() == ".3mf":
                real, tm = _3mf_xml_mb(p)
                total += real if (not via_trimesh and _manual_first(p)) else tm
            else:
                total += p.stat().st_size / 1e6
        except Exception:  # noqa: BLE001
            log.debug("source_mesh_mb failed for %s", p, exc_info=True)
    return total


def too_heavy_to_load(model_paths, *, via_trimesh: bool = False) -> str:
    """Причина отказа грузить геометрию («» — грузить можно). Отказ НЕ ошибка: вызывающие
    возвращают пустой набор мешей, а мини-апп остаётся на вшитой в .3mf картинке. Цену это не
    трогает — её считает fastmeasure, он парсит .3mf сам и в память дерево не поднимает.
    via_trimesh — см. source_mesh_mb."""
    mb = source_mesh_mb(model_paths, via_trimesh=via_trimesh)
    if mb <= 0:
        return ""
    if mb > MESH_SRC_LIMIT_MB:
        return (f"геометрия {mb:.1f} МБ в распакованном виде — больше потолка "
                f"{MESH_SRC_LIMIT_MB:.0f} МБ (MESH_SRC_LIMIT_MB)")
    need = mb * MESH_RAM_FACTOR / 1024.0                       # ожидаемый пик, ГиБ
    try:
        import psutil
        free = psutil.virtual_memory().available / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return ""                                              # нечем измерить — не мешаем
    if need > free * MESH_RAM_SHARE:
        return (f"разбор ~{need:.1f} ГиБ при {free:.1f} ГиБ свободной памяти — "
                f"дороже {MESH_RAM_SHARE:.0%} свободной ОЗУ (MESH_RAM_SHARE)")
    return ""

# Числа стола (порог «умещается» и pile_ratio дедупа копий) живут в app.bedspec и ездят сюда
# ПАРАМЕТРОМ bed. Разброс центров одинаковых экземпляров < bed.pile_ratio × размер экземпляра
# => куча копий. Если ОБЩИЙ след раскладки больше стола — это мультиплейт-сборка (детали
# разложены по нескольким столам), копии в ней намеренные → дедуп «наложенных копий»
# отключаем (иначе схлопывает реальные детали — баг расчёта «устройства перетасовки карт»).


def _footprint_exceeds_bed(insts: list, bed: BedSpec = DEFAULT_BED) -> bool:
    """Общий габарит всех экземпляров (X или Y) больше стола → раскладка на нескольких столах."""
    mins, maxs = [], []
    for it in insts:
        try:
            b = it[0].bounds
            mins.append(b[0])
            maxs.append(b[1])
        except Exception:  # noqa: BLE001
            continue
    if not mins:
        return False
    ext = np.array(maxs).max(0) - np.array(mins).min(0)
    return float(ext[0]) > bed.fit_xy_mm or float(ext[1]) > bed.fit_xy_mm


def _exact_hash(mesh) -> str:
    v = np.ascontiguousarray(np.round(mesh.vertices, 4))
    f = np.ascontiguousarray(mesh.faces)
    return hashlib.sha1(v.tobytes() + f.tobytes()).hexdigest()


def _cap_scene_faces(scene: "trimesh.Scene", budget: int) -> None:
    """Если суммарно (с учётом ИНСТАНСОВ) граней больше budget — прорежаем УНИКАЛЬНЫЕ геометрии
    сцены ОДИН раз (не каждую копию). Спасает память на моделях вроде «100 mini ducks»: 1 меш на
    ~1.07M граней ×101 инстанс = 108M граней → воркер уходит в OOM. Дециация почти не меняет объём,
    поэтому смета по филаменту остаётся точной; рендер/слайс перестают валить процесс по памяти.

    Прорежаем по ОДНОЙ глобальной доле (budget/total) каждый уникальный меш — так и одиночные
    тяжёлые модели, и тиражи инстансов сводятся под общий потолок. Нетронуто, если уже в бюджете."""
    if not budget or budget <= 0:
        return
    try:
        counts: dict = {}
        for node in scene.graph.nodes_geometry:
            try:
                _, gname = scene.graph[node]
            except Exception:  # noqa: BLE001
                continue
            counts[gname] = counts.get(gname, 0) + 1
        total = 0
        for gname, c in counts.items():
            g = scene.geometry.get(gname)
            if g is not None and hasattr(g, "faces"):
                total += len(g.faces) * c
        if total <= budget:
            return
        ratio = budget / float(total)
        log.warning("geometry: %d граней c инстансами > бюджета %d — прорежаю уникальные меши до "
                    "~%.0f%% (память; объём/смета почти не меняются)", total, budget, ratio * 100)
        for gname, g in list(scene.geometry.items()):
            try:
                if g is None or not hasattr(g, "faces"):
                    continue
                nf = len(g.faces)
                target = int(nf * ratio)
                if nf < 2000 or target >= nf:        # совсем мелкие/без выгоды не трогаем
                    continue
                d = g.simplify_quadric_decimation(face_count=max(1000, target))
                if d is not None and len(d.faces):
                    scene.geometry[gname] = d
            except Exception:  # noqa: BLE001
                log.debug("cap decimation failed for %s", gname, exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("scene face cap failed", exc_info=True)


def _scene_instances(scene: "trimesh.Scene") -> list:
    """[(mesh_world, content_hash, center(np3), diag, key)] по каждому размещённому экземпляру.
    key = имя геометрии (совпадает с id детали в model_settings.config — так находим вычитающие)."""
    _cap_scene_faces(scene, config.MAX_TOTAL_FACES)   # тираж инстансов 1М-меша не должен съесть RAM
    out: list = []
    hcache: dict = {}
    for node in scene.graph.nodes_geometry:
        try:
            T, gname = scene.graph[node]
        except Exception:  # noqa: BLE001
            continue
        base = scene.geometry.get(gname)
        if base is None or not hasattr(base, "faces") or not len(base.faces):
            continue
        h = hcache.get(gname)
        if h is None:
            h = _exact_hash(base)
            hcache[gname] = h
        m = base.copy()
        try:
            m.apply_transform(T)
        except Exception:  # noqa: BLE001
            pass
        b = m.bounds
        out.append((m, h, (b[0] + b[1]) / 2.0, float(np.linalg.norm(b[1] - b[0])), str(gname)))
    return out


_VERT_RE = re.compile(r'<vertex[^>]*\bx="([^"]+)"[^>]*\by="([^"]+)"[^>]*\bz="([^"]+)"')
_TRI_RE = re.compile(r'<triangle[^>]*\bv1="(\d+)"[^>]*\bv2="(\d+)"[^>]*\bv3="(\d+)"')


def _parse_3mf_object_mesh(zip_path, object_id) -> "trimesh.Trimesh | None":
    """Прочитать РЕАЛЬНЫЙ меш объекта <object id=...> прямо из .model (в обход бага trimesh,
    который у некоторых .3mf — как «бэйбизавр» — грузит чужой меш вместо вычитающей детали)."""
    oid = str(object_id)
    try:
        with zipfile.ZipFile(str(zip_path)) as z:
            for n in z.namelist():
                if not n.lower().endswith(".model"):
                    continue
                data = z.read(n).decode("utf-8", "ignore")
                m = re.search(r'<object\s+id="' + re.escape(oid) + r'"[\s>]', data)
                if not m:
                    continue
                end = data.find("</object>", m.start())
                block = data[m.start():end if end > 0 else len(data)]
                verts = _VERT_RE.findall(block)
                tris = _TRI_RE.findall(block)
                if len(verts) < 3 or len(tris) < 1:
                    continue
                V = np.array(verts, dtype=np.float64)
                F = np.array(tris, dtype=np.int64)
                if F.max() >= len(V):
                    continue
                return trimesh.Trimesh(vertices=V, faces=F, process=False)
    except Exception:  # noqa: BLE001
        log.debug("parse 3mf object %s failed", object_id, exc_info=True)
    return None


# ───── Ручная сборка production-3mf (OpenTodo 041be6d4) ─────
# У Bambu production-extension верхние объекты собраны из КОМПОНЕНТОВ во внешних sub-model
# файлах (<component p:path="/3D/Objects/..." objectid=... transform=...>). trimesh такие сцены
# иногда собирает неверно: каждому компоненту достаётся слитая геометрия, и модель раздувается
# кратно (springy kitty: 2 кота по 4 покрасочных части → «8 котов», 4.6М граней вместо 0.58М).
# Детектор — сверка с заявленным числом граней (inspect3mf.declared_face_total); при расхождении
# собираем сцену сами: меши компонентов читаем напрямую из sub-model, применяем трансформ
# компонента и build-item (формат 3MF: 12 чисел, row-vector → транспонируем для trimesh).

# Теги парсим целиком, атрибуты вытаскиваем по отдельности — порядок атрибутов в 3MF не
# фиксирован, а «ленивые» опциональные группы в одном большом regex молча теряют transform.
_COMPONENT_TAG_RE = re.compile(r"<component\b[^>]*>")
_ITEM_TAG_RE = re.compile(r"<item\b[^>]*>")


def _attr(tag: str, name: str) -> str | None:
    m = re.search(re.escape(name) + r'="([^"]*)"', tag)
    return m.group(1) if m else None


def _mat_from_3mf(s: str | None) -> np.ndarray:
    """Матрица 4×4 (колоночная, для trimesh) из атрибута transform 3MF (12 чисел, row-vector)."""
    M = np.eye(4)
    try:
        vals = [float(x) for x in (s or "").split()]
        if len(vals) >= 12:
            M[:3, :3] = np.array(vals[:9]).reshape(3, 3).T
            M[:3, 3] = vals[9:12]
    except Exception:  # noqa: BLE001
        pass
    return M


def _cfg_part_matrices(z: "zipfile.ZipFile") -> dict:
    """{part_id -> 4×4} из Metadata/model_settings.config: Orca/Elegoo хранят позицию ДЕТАЛИ
    внутри объекта именно здесь (16 чисел row-major), а transform компонента в core-файле
    оставляют пустым. Без этих матриц детали рисуются в сырых STL-координатах — кучей
    (OpenTodo 90b32407, «пивные открывашка»)."""
    try:
        s = z.read("Metadata/model_settings.config").decode("utf-8", "ignore")
    except KeyError:
        return {}
    out: dict = {}
    for m in re.finditer(r'<part id="(\d+)"[^>]*>(.*?)</part>', s, re.S):
        mm = re.search(r'<metadata key="matrix" value="([^"]+)"', m.group(2))
        if not mm:
            continue
        try:
            vals = [float(x) for x in mm.group(1).split()]
        except ValueError:
            continue
        if len(vals) == 16:
            out[m.group(1)] = np.array(vals, dtype=np.float64).reshape(4, 4)
    return out


def _cfg_assemble_transforms(z: "zipfile.ZipFile") -> dict:
    """{object_id -> 4×4} из <assemble_item> model_settings.config — сборочная позиция объекта
    (12 чисел, формат transform 3MF). Item в build у Orca-проектов обычно без transform —
    без assemble-позиций объекты наваливаются в куче на превью (OpenTodo 90b32407)."""
    try:
        s = z.read("Metadata/model_settings.config").decode("utf-8", "ignore")
    except KeyError:
        return {}
    out: dict = {}
    for m in re.finditer(r'<assemble_item[^>]*object_id="(\d+)"[^>]*transform="([^"]+)"', s):
        out[m.group(1)] = _mat_from_3mf(m.group(2))
    return out


def _parse_objects_from_xml(data: str, ids: set) -> dict:
    """{object_id -> Trimesh} для запрошенных <object id=...> из текста .model-файла."""
    out: dict = {}
    for oid in ids:
        m = re.search(r'<object\s[^>]*\bid="' + re.escape(str(oid)) + r'"[\s>]', data)
        if not m:
            continue
        end = data.find("</object>", m.start())
        block = data[m.start():end if end > 0 else len(data)]
        verts = _VERT_RE.findall(block)
        tris = _TRI_RE.findall(block)
        if len(verts) < 3 or len(tris) < 1:
            continue
        V = np.array(verts, dtype=np.float64)
        F = np.array(tris, dtype=np.int64)
        if F.max() >= len(V):
            continue
        out[str(oid)] = trimesh.Trimesh(vertices=V, faces=F, process=False)
    return out


def production_items(model_path) -> list:
    """[(top_object_id, [world-меши компонентов])] по каждому печатаемому build-item
    production-3mf, собрано ВРУЧНУЮ в обход trimesh. Компоненты НЕ склеиваем — отдаём
    по-отдельности (как и trimesh-путь отдаёт детали): дальнейшая логика сама решает
    «склеить-если-влезает» или раскладывать. [] — файл не того формата / собрать не вышло."""
    return [(oid, [m for m, _pid in pairs]) for oid, pairs in production_items_with_ids(model_path)]


def production_items_with_ids(model_path) -> list:
    """Как production_items, но каждый меш компонента идёт парой (mesh, part_id): part_id —
    id компонента в sub-model, он же id детали в model_settings.config (по нему же фильтруются
    непечатаемые). Нужен 3D-вьюеру для привязки деталь→экструдер→цвет; production_items —
    тонкая обёртка над этой функцией, набор/порядок мешей у них гарантированно совпадают."""
    p = Path(model_path)
    try:
        with zipfile.ZipFile(str(p)) as z:
            core = z.read("3D/3dmodel.model").decode("utf-8", "ignore")
            # компоненты верхних объектов: object_id -> [(sub_path, sub_oid, T)]
            comps: dict = {}
            for om in re.finditer(r'<object\b[^>]*\bid="([^"]+)"[^>]*>', core):
                oid = om.group(1)
                end = core.find("</object>", om.start())
                block = core[om.start():end if end > 0 else len(core)]
                cl = []
                for cm in _COMPONENT_TAG_RE.finditer(block):
                    tag = cm.group(0)
                    sub_path, sub_oid = _attr(tag, "p:path"), _attr(tag, "objectid")
                    if sub_path and sub_oid:
                        cl.append((sub_path, sub_oid, _attr(tag, "transform")))
                if cl:
                    comps[oid] = cl
            if not comps:
                return []
            items = []
            for im in _ITEM_TAG_RE.finditer(core):
                tag = im.group(0)
                oid = _attr(tag, "objectid")
                if oid in comps and (_attr(tag, "printable") or "1") != "0":
                    items.append((oid, _attr(tag, "transform")))
            if not items:
                return []
            # Объекты ВНЕ столов не печатаются (Orca держит их «в закромах» проекта) — в наборе
            # «пивные открывашка» так лежит лишний Tab Opener: рисовался и считался как дубль
            # (OpenTodo 90b32407). Столы известны → берём только размещённые объекты.
            try:
                plate_ids = {str(o) for o in inspect3mf.placed_object_ids(p)}
            except Exception:  # noqa: BLE001
                plate_ids = set()
            if plate_ids:
                dropped = [oid for oid, _t in items if str(oid) not in plate_ids]
                if dropped:
                    log.info("production 3mf %s: объекты вне столов не печатаются — пропускаю %s",
                             p.name, dropped)
                    items = [(oid, t) for oid, t in items if str(oid) in plate_ids]
                    if not items:
                        return []
            # позиции деталей/объектов из model_settings.config (см. _cfg_part_matrices)
            part_mats = _cfg_part_matrices(z)
            asm_mats = _cfg_assemble_transforms(z)
            # непечатаемые детали (модификаторы, зоны поддержек) материалом не идут — их
            # компоненты не грузим вовсе (иначе объём/превью завышаются, см. inspect3mf)
            try:
                skip = inspect3mf.nonprinting_part_ids(p)
            except Exception:  # noqa: BLE001
                skip = set()
            # какие sub-меши реально нужны (только по размещённым объектам)
            need: dict = {}
            for oid, _t in items:
                for sub_path, sub_oid, _ct in comps[oid]:
                    if str(sub_oid) not in skip:
                        need.setdefault(sub_path, set()).add(str(sub_oid))
            sub_meshes: dict = {}
            for sub_path, ids in need.items():
                try:
                    data = z.read(sub_path.lstrip("/")).decode("utf-8", "ignore")
                except KeyError:
                    log.warning("production 3mf: sub-model %s не найден в архиве", sub_path)
                    return []
                sub_meshes[sub_path] = _parse_objects_from_xml(data, ids)
        out: list = []
        for oid, t in items:
            # transform item'а в core обычно пуст у Orca-проектов — тогда позиция объекта
            # берётся из assemble_item (иначе все объекты рисуются в куче, OpenTodo 90b32407)
            T_item = _mat_from_3mf(t) if (t or "").strip() else asm_mats.get(str(oid), np.eye(4))
            parts = []
            for sub_path, sub_oid, ct in comps[oid]:
                if str(sub_oid) in skip:
                    continue
                base = sub_meshes.get(sub_path, {}).get(str(sub_oid))
                if base is None or not len(base.faces):
                    log.warning("production 3mf: компонент %s/%s не распарсился", sub_path, sub_oid)
                    return []
                # аналогично для детали: пустой transform компонента → матрица части из
                # model_settings.config (позиции «форма + вставка» и т.п.)
                T_comp = (_mat_from_3mf(ct) if (ct or "").strip()
                          else part_mats.get(str(sub_oid), np.eye(4)))
                m = base.copy()
                m.apply_transform(T_item @ T_comp)
                parts.append((m, str(sub_oid)))
            out.append((oid, parts))
        return out
    except Exception:  # noqa: BLE001
        log.warning("production 3mf compose failed for %s", p.name, exc_info=True)
        return []


def _scene_unique_face_total(scene: "trimesh.Scene") -> int:
    """Суммарные грани УНИКАЛЬНЫХ геометрий сцены (каждая один раз, без инстансов-копий).

    Именно так ловится баг сборки: при нём trimesh кладёт в КАЖДУЮ геометрию слитый меш всего
    файла (kitty: 4 «уникальных» геометрии по 579К граней при настоящих 242К+16К+16К+15К).
    Считать по нодам нельзя — у здоровых моделей легитимные копии-инстансы (ветряк: ×3.5 по
    нодам при корректной сборке) дали бы ложное срабатывание."""
    total = 0
    for g in scene.geometry.values():
        if g is not None and hasattr(g, "faces"):
            total += len(g.faces)
    return total


def _dup_unique_geoms(scene: "trimesh.Scene") -> bool:
    """Есть ли среди УНИКАЛЬНЫХ геометрий сцены дубли по содержимому (разные ключи geometry
    с одинаковым мешем). Это сигнатура бага сборки production-3mf: trimesh кладёт слитый меш
    объекта в КАЖДЫЙ ключ его компонентов. Сначала группируем дёшево по (граней, вершин),
    полный хэш считаем только внутри совпавших групп."""
    sizes: dict = {}
    for name, g in scene.geometry.items():
        if g is None or not hasattr(g, "faces") or not len(g.faces):
            continue
        sizes.setdefault((len(g.faces), len(g.vertices)), []).append(name)
    for names in sizes.values():
        if len(names) < 2:
            continue
        seen: set = set()
        for name in names:
            hx = _exact_hash(scene.geometry[name])
            if hx in seen:
                return True
            seen.add(hx)
    return False


def _trimesh_misassembled(p: Path, scene: "trimesh.Scene") -> bool:
    """trimesh собрал сцену с кратно бОльшим суммарным размером уникальных геометрий, чем
    заявлено в метаданных модели — признак бага сборки production-3mf (springy kitty: ×4 по
    уникальным). Допуск ×1.5 на погрешности/неучтённые мелочи.

    ElegooSlicer face_count в model_settings.config НЕ пишет (только <mesh_stat> без числа
    граней) → declared=0 и сверять не с чем. Тогда ловим сигнатуру самого бага: файл —
    production-3mf (компоненты с p:path) И среди уникальных геометрий есть дубли содержимого
    («пивные открывашка» 044a4bcd: пары 1==2, 8==9, 11==12 → 227 см³ вместо 140).

    Сигнатуру проверяем и когда face_count ЗАЯВЛЕН: сверка по числу граней разбавляется
    повторами. Bambu на каждое размещение заводит ОТДЕЛЬНЫЙ <object> в model_settings (6 одинаковых
    стержней = 6 объектов по 914 граней), а уникальных геометрий у trimesh на них одна — declared
    завышен относительно того, с чем сравнивается, и раздув уходит под допуск ×1.5 (CATAN card
    holder box, заказ #136: 35594 против «заявленных» 24678 = ×1.44 при настоящем ×1.82 — верхняя
    панель рисовалась ДВАЖДЫ, вторым цветом со сдвигом текстовой вставки)."""
    try:
        declared = inspect3mf.declared_face_total(p)
        if declared:
            actual = _scene_unique_face_total(scene)
            if actual > declared * 1.5:
                log.warning("geometry %s: trimesh собрал %d граней уникальных геометрий при "
                            "заявленных %d (×%.1f) — собираю сцену вручную (production-3mf)",
                            p.name, actual, declared, actual / declared)
                return True
        if inspect3mf.has_production_components(p) and _dup_unique_geoms(scene):
            log.warning("geometry %s: production-3mf, и trimesh продублировал геометрии "
                        "компонентов (сверка по face_count молчала) — собираю сцену вручную", p.name)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _negative_world_meshes(model_path, scene: "trimesh.Scene", neg_ids: frozenset) -> list:
    """РЕАЛЬНЫЕ вычитающие меши (из .model напрямую), размещённые трансформом из сцены.
    Так обходим миспарс trimesh: трансформ объекта он берёт верно, а меш — нет."""
    out: list = []
    for node in scene.graph.nodes_geometry:
        try:
            T, gname = scene.graph[node]
        except Exception:  # noqa: BLE001
            continue
        if str(gname) not in neg_ids:
            continue
        real = _parse_3mf_object_mesh(model_path, gname)
        if real is None or not len(real.faces):
            continue
        m = real.copy()
        try:
            m.apply_transform(np.asarray(T))
        except Exception:  # noqa: BLE001
            pass
        out.append(m)
    return out


def _subtract_negatives(pos_meshes: list, neg_meshes: list) -> list:
    """#3ca1fe86/#329fc86b: вырезаем вычитающие детали булевой разностью — ОЧЕНЬ осторожно.
    Режем, только если вырез герметичен И база герметична И результат валиден. Иначе (флекси,
    print-in-place — негерметичная база) ПРОСТО НЕ рисуем вычитающие детали: они уже исключены
    из позитивов, так что модель показывается в обычной графике без блочных артефактов
    (a379c7eb: воксельный путь убран). neg_meshes уже размещены трансформом."""
    cutters = [c for c in neg_meshes
               if c is not None and getattr(c, "is_watertight", False) and len(getattr(c, "faces", []))]
    if not cutters or not pos_meshes:
        return pos_meshes
    base = pos_meshes[0] if len(pos_meshes) == 1 else trimesh.util.concatenate(pos_meshes)
    if not getattr(base, "is_watertight", False):
        return pos_meshes              # негерметичную базу не режем — вычитающие детали просто скрыты
    try:
        base_vol = float(base.volume)
        result = trimesh.boolean.difference([base, *cutters])
        if result is None or not hasattr(result, "faces") or not len(result.faces):
            return pos_meshes
        rv = float(result.volume)
        if rv <= 0 or rv > base_vol * 1.001:   # результат должен быть меньше базы
            return pos_meshes
        log.info("carved (boolean) %d negative part(s): %.1f -> %.1f cm3",
                 len(cutters), base_vol / 1000.0, rv / 1000.0)
        return [result]
    except Exception:  # noqa: BLE001
        log.debug("boolean subtract failed — скрываем вычитающие детали", exc_info=True)
        return pos_meshes


def _dedup_piled(instances: list, bed: BedSpec = DEFAULT_BED) -> list:
    return [it[0] for it in _dedup_piled_insts(instances, bed)]


def _dedup_piled_insts(instances: list, bed: BedSpec = DEFAULT_BED) -> list:
    """Как _dedup_piled, но возвращает сами ЭКЗЕМПЛЯРЫ (см. _scene_instances; part-id в [4]) —
    load_print_meshes_with_ids нужен id оставленных деталей. _dedup_piled — обёртка, поведение
    (набор и порядок мешей) идентично прежнему."""
    groups: dict = {}
    for inst in instances:
        groups.setdefault(inst[1], []).append(inst)
    kept: list = []
    for items in groups.values():
        if len(items) == 1:
            kept.append(items[0])
            continue
        centers = np.array([it[2] for it in items])
        spread = float(np.linalg.norm(centers.max(0) - centers.min(0)))
        diag = float(np.median([it[3] for it in items])) or 1.0
        if spread < bed.pile_ratio * diag:
            kept.append(items[0])               # копии лежат кучей — оставляем одну
            log.info("collapsed %d piled identical copies into 1 (spread %.1f < %.1f)",
                     len(items), spread, bed.pile_ratio * diag)
        else:
            kept.extend(items)                  # разложены в структуру — оставляем все
    return kept


def _rests_on_plate(instances: list, bed: BedSpec = DEFAULT_BED) -> bool:
    """Все экземпляры стоят примерно на одной опорной плоскости (реальная плита)? Сравниваем
    разброс НИЗОВ (min Z) экземпляров: ≤ стола → физичная раскладка. Большой разброс по Z = детали
    «парят» на разной высоте — частый артефакт мультицветных/битых .3mf (как Donald Duck Urban:
    два одинаковых тела разнесены на ~0.5 м по X и Z). Такую раскладку слайсер не берёт."""
    zs = []
    for it in instances:
        try:
            zs.append(float(it[0].bounds[0][2]))
        except Exception:  # noqa: BLE001
            continue
    return len(zs) < 2 or (max(zs) - min(zs)) <= bed.fit_xy_mm


def _unique_native_geoms(scene: "trimesh.Scene", instances: list) -> list:
    """По одному СЫРОМУ (без мирового трансформа) мешу на каждую уникальную геометрию. Нужен при
    нефизичной раскладке: мировые трансформы у таких .3mf битые (один и тот же меш размещён сразу
    с масштабом 5.95× и 0.25× — как в Donald Duck Urban), поэтому мировой меш бессмысленного
    размера. У сырой геометрии размер правильный (≈100×97×25 мм). Ключ — content-hash inst[1],
    имя геометрии — inst[4]."""
    return [m for m, _pid in _unique_native_geoms_pairs(scene, instances)]


def _unique_native_geoms_pairs(scene: "trimesh.Scene", instances: list) -> list:
    """Как _unique_native_geoms, но парами (mesh, part_id): id (= inst[4], имя геометрии) нужен
    load_print_meshes_with_ids. Старая функция — обёртка, набор/порядок мешей идентичны."""
    seen: set = set()
    out: list = []
    for it in instances:
        if it[1] in seen:
            continue
        seen.add(it[1])
        g = scene.geometry.get(it[4])
        out.append(((g.copy() if (g is not None and hasattr(g, "faces") and len(g.faces)) else it[0]),
                    it[4]))
    return out


def _drop_to_plate(meshes: list) -> list:
    """Опустить каждый меш на стол: центр по XY → в начало координат, низ (min Z) → 0. Слайсер с
    --arrange сам разложит несколько деталей; одиночная встанет по центру стола. Чинит «парящие»
    детали, которые иначе оказываются вне зоны печати и слайсер не даёт gcode."""
    for m in meshes:
        try:
            b = m.bounds
            m.apply_translation([-(b[0][0] + b[1][0]) / 2.0, -(b[0][1] + b[1][1]) / 2.0, -b[0][2]])
        except Exception:  # noqa: BLE001
            log.debug("drop-to-plate failed", exc_info=True)
    return meshes


def load_print_meshes(model_paths, bed: BedSpec = DEFAULT_BED) -> list:
    """world-размещённые меши модели; наложенные копии-дубли убраны, структура сохранена."""
    return [m for m, _pid, _src in load_print_meshes_with_ids(model_paths, bed)]


def load_print_meshes_with_ids(model_paths, bed: BedSpec = DEFAULT_BED) -> list:
    """Как load_print_meshes, но тройками (mesh, part_id | None, src_path): part_id — id детали
    из model_settings.config (ключ inspect3mf.part_extruders; может нести trimesh-суффикс «_N»
    дубля — снимается _base_part_id), src_path — файл-источник. По ним 3D-вьюер привязывает
    деталь к её экструдеру/цвету. None — id не определить (STL/OBJ, склеенный булевой разностью
    результат). ЕДИНСТВЕННАЯ реализация загрузки: load_print_meshes — обёртка над ней, поэтому
    набор и порядок мешей для pricing/slicing не меняются."""
    heavy = too_heavy_to_load(model_paths)
    if heavy:                       # предохранитель по ОЗУ — см. too_heavy_to_load
        log.warning("geometry: не гружу %s — %s",
                    ", ".join(Path(p).name for p in model_paths), heavy)
        return []
    out: list = []
    for p in model_paths:
        p = Path(p)
        # раздутый production-3mf trimesh не отдаём вовсе — см. _manual_first
        manual = _manual_first(p)
        obj = None
        if not manual:
            try:
                obj = trimesh.load(str(p))
            except Exception:  # noqa: BLE001
                log.debug("geometry load failed: %s", p, exc_info=True)
                continue
        neg: frozenset = frozenset()
        skip: frozenset = frozenset()
        if p.suffix.lower() == ".3mf":
            try:
                neg = frozenset(inspect3mf.negative_part_ids(p))
                skip = frozenset(inspect3mf.nonprinting_part_ids(p))
            except Exception:  # noqa: BLE001
                neg = skip = frozenset()
        # trimesh собрал production-3mf неверно (граней кратно больше заявленных, springy
        # kitty: ×8 → «4 кота») → собираем сцену вручную из компонентов (OpenTodo 041be6d4).
        # С вычитающими деталями не рискуем — там нужен прежний путь с резкой негативов
        # (непечатаемые детали ручной сборке не мешают — production_items их сам выкидывает).
        if manual or (isinstance(obj, trimesh.Scene) and p.suffix.lower() == ".3mf" and not neg
                      and _trimesh_misassembled(p, obj)):
            items = production_items_with_ids(p)
            if items:
                log.info("geometry %s: production-сборка вручную — %d объект(ов), %d дет.",
                         p.name, len(items), sum(len(g) for _, g in items))
                for _oid, group in items:
                    out.extend((m, pid, p) for m, pid in group)
                continue
        if obj is None:                 # ручная сборка не вышла, а trimesh ещё не грузили
            heavy = too_heavy_to_load([p], via_trimesh=True)
            if heavy:
                log.warning("geometry: не гружу %s — ручная сборка не вышла, а trimesh: %s",
                            p.name, heavy)
                continue
            try:
                obj = trimesh.load(str(p))
            except Exception:  # noqa: BLE001
                log.debug("geometry load failed: %s", p, exc_info=True)
                continue
        if isinstance(obj, trimesh.Scene):
            insts = _scene_instances(obj)
            if not insts:
                continue
            pos_insts = [i for i in insts if i[4] not in neg and i[4] not in skip]
            if not pos_insts:            # всё помечено непечатаемым — не доверяем, берём всё
                log.warning("all parts marked negative in %s — keeping all", p.name)
                out.extend((it[0], it[4], p) for it in _dedup_piled_insts(insts, bed))
                continue
            if not _rests_on_plate(pos_insts, bed):
                # Нефизичная раскладка: детали «парят» на разной высоте (артефакт мультицветных
                # .3mf — напр. Donald Duck Urban: 2 одинаковых тела разнесены на ~0.5 м по X и Z).
                # Слайсер такую модель не берёт (нет gcode даже по одной детали). Сводим к одному
                # экземпляру каждой уникальной геометрии и опускаем на стол → модель считается.
                pairs = _unique_native_geoms_pairs(obj, pos_insts)
                _drop_to_plate([m for m, _pid in pairs])
                log.info("geometry %s: нефизичная раскладка (детали парят / битый масштаб) — взял "
                         "сырую геометрию: %d уник. и опустил на стол", p.name, len(pairs))
            elif _footprint_exceeds_bed(pos_insts, bed):
                # Мультиплейт-сборка: копии деталей намеренные — НЕ дедупим (см. BedSpec.fit_xy_mm).
                pairs = [(i[0], i[4]) for i in pos_insts]
                log.info("geometry %s: мультиплейт-раскладка (>%.0f мм) — дедуп пропущен, деталей %d",
                         p.name, bed.fit_xy_mm, len(pairs))
            else:
                kept = _dedup_piled_insts(pos_insts, bed)
                pairs = [(it[0], it[4]) for it in kept]
                if len(pairs) != len(pos_insts):
                    log.info("geometry %s: экземпляров %d → после дедупа %d (непечатаемых %d)",
                             p.name, len(pos_insts), len(pairs), len(insts) - len(pos_insts))
            if neg and p.suffix.lower() == ".3mf":   # вычитающие детали → режем РЕАЛЬНЫМИ мешами из .model
                real_negs = _negative_world_meshes(p, obj, neg)
                if real_negs:
                    pos_list = [m for m, _pid in pairs]
                    res = _subtract_negatives(pos_list, real_negs)
                    if res is not pos_list:     # булево склеило детали в один меш → part-id больше нет
                        pairs = [(m, None) for m in res]
            out.extend((m, pid, p) for m, pid in pairs)
        elif hasattr(obj, "faces") and len(obj.faces):
            out.append((obj, None, p))
    return out


_scale_limit_memo: dict = {}


def scale_limit(model_paths, bed_xy: float, bed_z: float, bed: BedSpec = DEFAULT_BED) -> float:
    """Максимальный коэффициент РАВНОМЕРНОГО масштаба, при котором каждая печатаемая деталь
    модели ещё помещается на стол: XY — по осям (как кладёт --arrange, поворот не пробуем),
    Z — по высоте печати. Лимитирует самая крупная деталь; по ОБЩЕМУ bbox считать нельзя —
    мультиплейт-раскладки шире стола изначально, а детали слайсер раскладывает сам.
    inf — геометрию загрузить не удалось (битый файл; либо .step/.stp у СТАРОЙ сохранённой
    модели — новые загрузчик больше не принимает, downloader.CAD_EXTS) → лимит неизвестен.

    bed_xy/bed_z — стол, ПРОТИВ которого меряем масштаб (приходит от вызывающего). bed —
    стол, по которому загружается сама геометрия (дедуп копий), поэтому он входит в мемо-ключ."""
    key = None
    try:
        from . import cache
        key = (cache.model_hash(model_paths), round(bed_xy, 1), round(bed_z, 1), bed)
        hit = _scale_limit_memo.get(key)
        if hit is not None:
            return hit
    except Exception:  # noqa: BLE001
        key = None
    lim = float("inf")
    try:
        for g in load_print_meshes(model_paths, bed):
            try:
                e = g.bounds[1] - g.bounds[0]
            except Exception:  # noqa: BLE001
                continue
            for ext, lim_mm in ((float(e[0]), bed_xy), (float(e[1]), bed_xy), (float(e[2]), bed_z)):
                if ext > 1e-6 and lim_mm > 0:
                    lim = min(lim, lim_mm / ext)
    except Exception:  # noqa: BLE001
        log.debug("scale_limit failed", exc_info=True)
        return float("inf")
    if key is not None:
        if len(_scale_limit_memo) >= 256:
            _scale_limit_memo.clear()
        _scale_limit_memo[key] = lim
    return lim


def unit_groups(model_paths, bed: BedSpec = DEFAULT_BED) -> list | None:
    """Модель-ТИРАЖ (один уникальный меш растиражирован много раз, как «100 mini ducks»: 1 меш
    на ~1.07M граней ×101 инстанс): вернуть [(world_mesh, count), …] — по одному мешу на КАЖДУЮ
    уникальную геометрию + сколько у неё инстансов. Слайсер режет ОДИН экземпляр и умножает
    объём/время на count — не материализуя N×1М-меша (иначе OOM воркера) и без дорогой дециации.

    None, если это НЕ тираж: не один .3mf, мало повторов (обычная сборка/флекси — там нужен
    --arrange по всем деталям), есть вычитающие детали, или геометрия и так влезает в память
    (тогда обычный путь точнее по времени). Гейт повторности защищает флекси/мультидетальные
    модели — они идут прежним путём без изменений.

    bed — стол принтера расчёта. СЕГОДНЯ НИ ОДНО РЕШЕНИЕ ЗДЕСЬ ОТ СТОЛА НЕ ЗАВИСИТ: гейты
    тиража — число инстансов, вычитающие детали и бюджет граней, порог fit_xy_mm не участвует
    (стол работает ниже, в _dedup_piled_insts/_rests_on_plate). Параметр принят ради
    единообразия цепочки «стол доходит до всех загрузчиков геометрии», чтобы вызывающему не
    приходилось помнить, какая из двух веток _load_dedup стол читает, а какая нет."""
    _ = bed
    paths = [Path(p) for p in model_paths]
    if len(paths) != 1 or paths[0].suffix.lower() != ".3mf":
        return None
    p = paths[0]
    # Эта ветка грузит ТОЛЬКО trimesh — и шла раньше предохранителя load_print_meshes, без
    # него вовсе (скорпион 09.10.2026 разбирался здесь первым). Меряем цену именно trimesh.
    heavy = too_heavy_to_load([p], via_trimesh=True)
    if heavy:
        log.info("geometry %s: тираж не проверяю — %s", p.name, heavy)
        return None
    try:
        obj = trimesh.load(str(p))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, trimesh.Scene):
        return None
    try:
        neg = frozenset(inspect3mf.negative_part_ids(p))
        skip = frozenset(inspect3mf.nonprinting_part_ids(p))
    except Exception:  # noqa: BLE001
        neg = skip = frozenset()
    counts: dict = {}                         # gname -> [count, sample_transform]
    order: list = []
    for node in obj.graph.nodes_geometry:
        try:
            T, gname = obj.graph[node]
        except Exception:  # noqa: BLE001
            continue
        base = obj.geometry.get(gname)
        if base is None or not hasattr(base, "faces") or not len(base.faces):
            continue
        if gname in neg:                      # тираж с вычитающими деталями — обычным путём
            return None
        if gname in skip:                     # модификатор/зона поддержек — не печатается
            continue
        if gname not in counts:
            counts[gname] = [0, T]
            order.append(gname)
        counts[gname][0] += 1
    total = sum(c for c, _ in counts.values())
    unique = len(counts)
    if unique == 0 or total < 8 or total < unique * 4:
        return None                           # не тираж (мало повторов) → обычный путь
    proj = sum(len(obj.geometry[g].faces) * c for g, (c, _) in counts.items())
    if proj <= config.MAX_TOTAL_FACES:
        return None                           # и так влезает → обычный путь (точнее по времени)
    groups: list = []
    for g in order:
        c, T = counts[g]
        m = obj.geometry[g].copy()
        try:
            m.apply_transform(T)
        except Exception:  # noqa: BLE001
            pass
        groups.append((m, c))
    log.info("geometry %s: ТИРАЖ — %d уник. меш(ей), всего %d инстансов (%.0fM граней) → режу по "
             "одному ×count", p.name, unique, total, proj / 1e6)
    return groups


def part_color_context(model_paths) -> dict:
    """Общий контекст РАСКРАСКИ деталей для 3D-GLB (webapp._model3d_glb_bytes) и цветного
    превью (preview.render_multicolor_preview): обе площадки обязаны раздавать слоты цветов
    ОДИНАКОВО, иначе слот k у клиента укажет не на тот цвет, что в mc_colors[k].

      slot_of      {экструдер → слот}: слоты = занятые экструдеры по возрастанию с той же
                   фильтрацией по палитре, что ModelInfo.used_colours (mc_colors);
      ext_maps     {str(путь .3mf) → {part_id → экструдер}} (inspect3mf.part_extruders);
      paint        {str(путь .3mf) → {object_id → [состояние на треугольник]}} — покраска
                   граней (Dyson 3b91dbaa): состояние k = филамент k, 0 = экструдер детали;
      file_slots   {str(путь) → слот} — наборы БЕЗ .3mf с цветами в именах файлов
                   (Ам Ням 2f2a102c): слот по цветовой группе имени;
      mc_colors    [hex по слотам] — дизайн-цвета набора (палитра .3mf или цвета групп имён).

    Пустые словари — раскраски нет (одноцвет/не разобралось): вызывающий красит по-старому."""
    ctx: dict = {"slot_of": {}, "ext_maps": {}, "paint": {}, "file_slots": {}, "mc_colors": []}
    paths = [Path(p) for p in model_paths]
    try:
        if any(p.suffix.lower() == ".3mf" for p in paths):
            info = inspect3mf.inspect_model(paths)
            pal = [inspect3mf._norm_hex(x) for x in info.filament_colours]
            if any(pal):
                used = [e for e in sorted(info.distinct_extruders)
                        if 0 < e <= len(pal) and pal[e - 1]]
            else:   # палитры в .3mf нет (mc_colors пуст) → слоты просто по номерам экструдеров
                used = [e for e in sorted(info.distinct_extruders) if e > 0]
            ctx["slot_of"] = {e: s for s, e in enumerate(used)}
            ctx["mc_colors"] = info.used_colours
            for p in paths:
                if p.suffix.lower() != ".3mf":
                    continue
                sp = str(p)
                emap = inspect3mf.part_extruders(sp)
                if emap:
                    ctx["ext_maps"][sp] = emap
                pmap = inspect3mf.paint_states(sp)
                if pmap:
                    ctx["paint"][sp] = pmap
        else:
            slots, colors = inspect3mf.filename_group_slots([str(p) for p in paths])
            ctx["file_slots"] = slots
            ctx["mc_colors"] = colors
    except Exception:  # noqa: BLE001
        log.debug("part_color_context failed", exc_info=True)
    return ctx


def split_mesh_by_paint(mesh, states, default_ext) -> list | None:
    """Разрезать меш детали по покраске треугольников: [(подмеш, экструдер|None), …].
    states — состояния по треугольникам (inspect3mf.paint_states; 0 = экструдер детали).
    None — резать нечего/нельзя: длина не сошлась с числом граней (негативы/дедуп могли
    перестроить меш) или реальная группа одна — вызывающий красит деталь одним цветом."""
    try:
        if states is None or len(states) != len(mesh.faces):
            return None
        arr = np.asarray(states, dtype=np.int32)
        exts = np.where(arr > 0, arr, np.int32(int(default_ext or 0)))
        uniq = [int(e) for e in np.unique(exts)]
        if len(uniq) < 2:
            return None
        out: list = []
        for e in uniq:
            idx = np.nonzero(exts == e)[0]
            sub = mesh.submesh([idx], append=True)
            if sub is not None and len(getattr(sub, "faces", [])):
                out.append((sub, e if e > 0 else None))
        return out if len(out) >= 2 else None
    except Exception:  # noqa: BLE001
        log.debug("split_mesh_by_paint failed", exc_info=True)
        return None


def _base_part_id(gname: str, known: set) -> str:
    """part-id для ключа геометрии. trimesh при НЕуникальном имени детали добавляет суффикс
    «_N» (две детали с одинаковым id в .model → «3_1», «3_2»). Сводим к исходному part-id:
    если сам ключ уже известный part-id — берём как есть, иначе срезаем хвост «_N»."""
    if gname in known:
        return gname
    m = re.match(r"^(.*)_\d+$", gname)
    if m and m.group(1) in known:
        return m.group(1)
    return gname


def _split_instances_by_plate(insts: list, plate_parts: list, plate_objects: list = None) -> list:
    """Разложить экземпляры сцены по столам (в порядке столов) по их part-id.

    insts — из _scene_instances (элемент [4] = ключ геометрии = part-id, возможно с «_N»).
    plate_parts — из inspect3mf.plate_part_ids (множество part-id на каждый стол, по порядку).
    plate_objects — из inspect3mf.plate_object_ids (object-id на стол); ЗАПАСНОЙ ключ: у обычных
    Bambu/Orca .3mf ключ геометрии = part-id (сопоставляем по plate_parts), но если экземпляр
    не нашёл ни одного стола по part-id, пробуем сопоставить его по object_id. Это спасает
    нестандартные .3mf, где trimesh адресует геометрию по object_id, и НЕ влияет на обычный
    путь: задействуется только для экземпляров, которые иначе ушли бы в «orphans».
    Деталь, заявленную НЕСКОЛЬКИМИ столами (зеркальная копия на двух столах Mark 85), раздаём
    её экземпляры по очереди claim'ающим столам — каждый экземпляр учитывается РОВНО на одном
    столе (честная сумма, без двойного счёта). Экземпляры, не попавшие ни на один стол, идут
    последним элементом «orphans», чтобы геометрия не терялась молча."""
    known_parts: set = set()
    for ps in plate_parts:
        known_parts |= set(ps)
    # part-id -> очередь индексов столов, которые его заявили (в порядке столов)
    part_claims: dict = {}
    for pi, ps in enumerate(plate_parts):
        for pid in ps:
            part_claims.setdefault(pid, []).append(pi)
    # запасная карта по object_id (тот же порядок столов)
    known_objs: set = set()
    obj_claims: dict = {}
    for pi, os_ in enumerate(plate_objects or []):
        for oid in os_:
            known_objs.add(oid)
            obj_claims.setdefault(oid, []).append(pi)
    buckets: list = [[] for _ in plate_parts]
    orphans: list = []
    cursor: dict = {}                          # round-robin курсор по claim'ам каждой детали
    for inst in insts:
        key = inst[4]
        ckey = _base_part_id(key, known_parts)
        owners = part_claims.get(ckey)
        if not owners and obj_claims:           # part-id не дал стол → пробуем object_id
            oid = _base_part_id(key, known_objs)
            owners = obj_claims.get(oid)
            ckey = "obj:" + oid                 # отдельное пространство курсоров от part-id
        if not owners:
            orphans.append(inst)
            continue
        k = cursor.get(ckey, 0)
        buckets[owners[k % len(owners)]].append(inst)
        cursor[ckey] = k + 1
    return buckets + ([orphans] if orphans else [])


def load_print_meshes_by_plate(model_paths) -> list:
    """world-меши модели, СГРУППИРОВАННЫЕ по столам (в порядке столов) — для честного per-plate
    расчёта мультиплейт-проектов. Возвращает [] если это НЕ мультиплейт (один .3mf, >1 стола);
    тогда вызывающий считает по-старому через load_print_meshes (критический путь не меняется).

    Внутри каждого стола исключаем вычитающие детали (negative_part) и режем их реальными
    мешами из .model, а непечатаемые (модификаторы, зоны поддержек) просто выкидываем —
    как в load_print_meshes. НО дедуп «наложенных копий» здесь НЕ применяем:
    столы известны точно, и каждый экземпляр стола — отдельная деталь (см. комментарий ниже)."""
    paths = [Path(p) for p in model_paths]
    # Мультиплейт определяем ТОЛЬКО для одиночного .3mf-входа: если рядом есть другие файлы
    # (доп. .stl и т.п.), они не привязаны к столам — честнее посчитать всё прежним путём.
    if len(paths) != 1 or paths[0].suffix.lower() != ".3mf":
        return []
    p = paths[0]
    try:
        plate_parts = inspect3mf.plate_part_ids(p)
    except Exception:  # noqa: BLE001
        plate_parts = []
    if len(plate_parts) <= 1:                  # один/ноль столов → обычный путь
        return []
    heavy = too_heavy_to_load([p])             # предохранитель по ОЗУ — см. too_heavy_to_load
    if heavy:
        log.warning("geometry(by_plate): не гружу %s — %s", p.name, heavy)
        return []
    # раздутый production-3mf trimesh не отдаём вовсе — см. _manual_first
    manual = _manual_first(p)
    obj = None
    if not manual:
        try:
            obj = trimesh.load(str(p))
        except Exception:  # noqa: BLE001
            log.debug("geometry(by_plate) load failed: %s", p, exc_info=True)
            return []
        if not isinstance(obj, trimesh.Scene):
            return []
    try:
        neg = frozenset(inspect3mf.negative_part_ids(p))
        skip = frozenset(inspect3mf.nonprinting_part_ids(p))
    except Exception:  # noqa: BLE001
        neg = skip = frozenset()
    # production-3mf, который trimesh собрал неверно → ручная сборка + раскладка по столам
    # по object_id (OpenTodo 041be6d4). С вычитающими деталями — прежний путь.
    if manual or (not neg and _trimesh_misassembled(p, obj)):
        items = production_items(p)
        plate_objs_p = []
        try:
            plate_objs_p = inspect3mf.plate_object_ids(p)
        except Exception:  # noqa: BLE001
            plate_objs_p = []
        if items and plate_objs_p:
            groups: list = []
            used = 0
            for objs in plate_objs_p:
                g: list = []
                for oid, part_meshes in items:
                    if oid in objs:
                        g.extend(part_meshes)
                        used += 1
                groups.append(g)
            if used == len(items) and any(groups):
                log.info("geometry(by_plate) %s: production-сборка вручную — %s дет. по столам",
                         p.name, [len(g) for g in groups])
                return groups
            log.warning("geometry(by_plate) %s: ручная сборка не сошлась со столами "
                        "(%d из %d размещено) — пробую прежний путь", p.name, used, len(items))
    if obj is None:                            # ручная сборка не вышла, а trimesh ещё не грузили
        heavy = too_heavy_to_load([p], via_trimesh=True)
        if heavy:
            log.warning("geometry(by_plate): не гружу %s — ручная сборка не вышла, а trimesh: %s",
                        p.name, heavy)
            return []
        try:
            obj = trimesh.load(str(p))
        except Exception:  # noqa: BLE001
            log.debug("geometry(by_plate) load failed: %s", p, exc_info=True)
            return []
        if not isinstance(obj, trimesh.Scene):
            return []
    insts = _scene_instances(obj)
    if not insts:
        return []
    # непечатаемая деталь может быть с «_N» (дубль) — отсекаем по базовому part-id тоже
    known: set = set()
    for ps in plate_parts:
        known |= set(ps)
    drop = neg | skip
    pos_insts = [i for i in insts if i[4] not in drop and _base_part_id(i[4], known) not in drop]
    if not pos_insts:                          # всё непечатаемое — не доверяем, обычный путь
        log.warning("all parts marked negative in %s — fallback to single-unit", p.name)
        return []
    real_negs = _negative_world_meshes(p, obj, neg) if neg else []
    try:                                       # запасной ключ сопоставления (object_id)
        plate_objs = inspect3mf.plate_object_ids(p)
    except Exception:  # noqa: BLE001
        plate_objs = []
    plates_out: list = []
    for plate_insts in _split_instances_by_plate(pos_insts, plate_parts, plate_objs):
        if not plate_insts:
            plates_out.append([])
            continue
        # БЕЗ _dedup_piled: экземпляры стола — это ИМЕННО то, что юзер разложил печатать (каждая
        # копия — отдельная деталь). Эвристика «схлопывания наложенных копий» нужна была только
        # когда столы не различить (single-unit путь); здесь столы известны точно, и схлопывание
        # занизило бы вес (баг card shuffler: 3 одинаковые детали на столе — это 3 детали, не 1).
        pos = [i[0] for i in plate_insts]
        if real_negs:                           # вычитающие детали режем реальными мешами
            pos = _subtract_negatives(pos, real_negs)
        plates_out.append(pos)
    n_meshes = sum(len(x) for x in plates_out)
    log.info("geometry %s: мультиплейт — %d стол(ов), мешей по столам %s (всего %d)",
             p.name, len(plate_parts), [len(x) for x in plates_out], n_meshes)
    return plates_out
