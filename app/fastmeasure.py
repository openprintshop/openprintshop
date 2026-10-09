"""Быстрое измерение печатной геометрии: объём, площадь по наклону нормали, союз-объём.

ЗАЧЕМ. Аналитическая ≈оценка цены (app.fastcalc) раньше знала только объём тела и приближала
деталь кубом — оболочка (стенки+корки) на реальных моделях занижалась в разы (медианная ошибка
против честного слайса была ~61%). Здесь мы за секунды меряем всё, что нужно площадь-осознанной
модели филамента (калибровка 17.07.2026 на 32 свежих слайсах, см. docs заметку в fastcalc):

  • v_mm3        — знаковая сумма тетраэдров (двоит самопересечения print-in-place!);
  • v_union_mm3  — объём СОЮЗА тела по z-колонкам (винтинг-числа) — то, что реально печатается;
  • exposure     — доля НАРУЖНОЙ поверхности (грани внутри союза слайсер сливает — стенок нет);
  • buckets      — площадь по 7 бакетам |nz| × знак (вверх/вниз): [ΣA, ΣA·|nz|, ΣA·√(1−nz²)];
                   из них модель берёт боковую (стенки) и проекционную (корки) составляющие;
  • air_mm3      — «воздух под телом» по тем же z-колонкам (база объёма поддержек);
  • h_mm / part_heights / footprint — для модели времени и bbox-гейтов.

ПАРСЕР. .model-XML внутри .3mf машинный и регулярный, поэтому вместо XML-парсера — C-скоростные
bytes.replace + np.fromstring(sep=' ') (~40-80 МБ/с с распаковкой; ET.iterparse давал ~8 МБ/с).
Лишние атрибуты треугольников (покраска pid/p1/paint_*…) вычищаются универсальным ретраем.
Память: entry читается целиком, но ПО ОДНОМУ (пик ~ крупнейший .model, не сумма).

ГЕОМЕТРИЯ КАК У СЛАЙСА. replicate_geometry=True воспроизводит выбор geometry.load_print_meshes
(пресетный путь слайса): дедуп «наложенных копий», схлопывание «парящих» раскладок. False —
проект как есть (нативный слайс 3mf со своими настройками). Непечатаемые детали (вырезы,
модификаторы, зоны поддержек) не меряются НИКОГДА — материалом они не идут ни там, ни там.
"""
from __future__ import annotations

import logging
import math
import re
import zipfile
from pathlib import Path

import numpy as np

from .bedspec import DEFAULT_BED, BedSpec

log = logging.getLogger(__name__)

# бакеты |nz| (nz — z-компонента единичной нормали грани)
NB_EDGES = np.array([0.0, 0.2, 0.4, 0.6, 0.75, 0.87, 0.965, 1.0001])
NB = len(NB_EDGES) - 1

# Гейты стола (bed.fit_xy_mm, bed.pile_ratio) — как в geometry.py (реплика семантики
# _dedup_piled / _rests_on_plate); сами числа живут в app.bedspec и едут сюда параметром bed.

# страховки ресурсов
ENTRY_BYTES_CAP = 700_000_000       # один .model крупнее — не парсим (fallback у вызывающего)
UNION_GRID = 192                    # сетка z-колонок союз-объёма
UNION_MAX_PAIRS = 40_000_000        # больше пар (треугольник, колонка) — союз пропускаем

_CORE = "3D/3dmodel.model"


class ObjStats:
    __slots__ = ("v_mm3", "buckets", "bbox", "nvtx", "v_union_mm3", "exposure", "air_mm3")

    def __init__(self, v_mm3, buckets, bbox, nvtx):
        self.v_mm3 = v_mm3
        self.buckets = buckets
        self.bbox = bbox
        self.nvtx = nvtx
        self.v_union_mm3 = v_mm3
        self.exposure = 1.0
        self.air_mm3 = 0.0


# ─────────────────────────── парсинг чисел из XML-байтов ───────────────────────────

_V_REPS = [(b'<vertex x="', b" "), (b'" y="', b" "), (b'" z="', b" "),
           (b'"/>', b" "), (b'" />', b" ")]
_T_REPS = [(b'<triangle v1="', b" "), (b'" v2="', b" "), (b'" v3="', b" "),
           (b'"/>', b" "), (b'" />', b" ")]
_T_EXTRA = re.compile(rb'\s[a-zA-Z_:][-\w:.]*="[^"]*"')   # любые доп.атрибуты (paint_*, pid…)
_OBJ_RE = re.compile(rb'<object\b[^>]*\bid="([^"]+)"')


def _parse_floats(seg: bytes, reps: list) -> np.ndarray | None:
    for old, new in reps:
        seg = seg.replace(old, new)
    try:
        return np.fromstring(seg, sep=" ")       # текстовый режим (sep=' ') не deprecated
    except Exception:  # noqa: BLE001
        return None


def _object_spans(data: bytes) -> list:
    """[(oid, start, end)] по <object id=…>…</object> в сыром XML."""
    spans = []
    pos = 0
    while True:
        m = _OBJ_RE.search(data, pos)
        if not m:
            break
        end = data.find(b"</object>", m.end())
        if end < 0:
            end = len(data)
        spans.append((m.group(1).decode("ascii", "replace"), m.start(), end))
        pos = end + 9
    return spans


def _column_union(verts, tris, a, b, c, cr, grid: int = UNION_GRID,
                  max_pairs: int = UNION_MAX_PAIRS):
    """(V_union мм³, exposure, air мм³) через z-колонки (винтинг по знаку нормали).

    Колонка = центр ячейки сетки grid×grid по XY-bbox. Союз: Σ интервалов winding>0 —
    самопересечения print-in-place не двоятся. exposure — доля пересечений на границе
    0↔тело (наружные поверхности). air — от пола до первого тела + внутренние просветы
    (стоящие на столе колонки дают 0) — база для объёма поддержек."""
    try:
        mn = verts.min(0)
        ext = verts.max(0) - mn
        if ext[0] <= 0 or ext[1] <= 0 or len(tris) < 4:
            return None
        cw = ext[0] / grid
        ch = ext[1] / grid
        txy = np.stack([a[:, :2], b[:, :2], c[:, :2]], 1)
        tmin = txy.min(1)
        tmax = txy.max(1)
        ix0 = np.clip(((tmin[:, 0] - mn[0]) / cw - 0.5).astype(np.int64), 0, grid - 1)
        ix1 = np.clip(((tmax[:, 0] - mn[0]) / cw + 0.5).astype(np.int64), 0, grid - 1)
        iy0 = np.clip(((tmin[:, 1] - mn[1]) / ch - 0.5).astype(np.int64), 0, grid - 1)
        iy1 = np.clip(((tmax[:, 1] - mn[1]) / ch + 0.5).astype(np.int64), 0, grid - 1)
        nx = ix1 - ix0 + 1
        ncell = nx * (iy1 - iy0 + 1)
        total = int(ncell.sum())
        if total > max_pairs:
            return None
        ti = np.repeat(np.arange(len(tris)), ncell)
        starts = np.concatenate([[0], np.cumsum(ncell)[:-1]])
        loc = np.arange(total) - np.repeat(starts, ncell)
        nxr = np.repeat(nx, ncell)
        cx = np.repeat(ix0, ncell) + loc % nxr
        cy = np.repeat(iy0, ncell) + loc // nxr
        px = mn[0] + (cx + 0.5) * cw
        py = mn[1] + (cy + 0.5) * ch
        ax, ay = a[ti, 0], a[ti, 1]
        bx, by = b[ti, 0], b[ti, 1]
        cxx, cyy = c[ti, 0], c[ti, 1]
        d = (by - ay) * (cxx - ax) - (bx - ax) * (cyy - ay)
        ok = np.abs(d) > 1e-12
        w1 = ((py - ay) * (cxx - ax) - (px - ax) * (cyy - ay)) / np.where(ok, d, 1.0)
        w2 = ((by - ay) * (px - ax) - (bx - ax) * (py - ay)) / np.where(ok, d, 1.0)
        inside = ok & (w1 >= 0) & (w2 >= 0) & (w1 + w2 <= 1)
        if not inside.any():
            return None
        ti = ti[inside]
        cell = cy[inside] * grid + cx[inside]
        w1 = w1[inside]
        w2 = w2[inside]
        az, bz, cz = a[ti, 2], b[ti, 2], c[ti, 2]
        z = az + w1 * (bz - az) + w2 * (cz - az)
        s = np.where(cr[ti, 2] < 0, 1, -1)        # нормаль вниз → вход в тело (идём снизу вверх)
        order = np.lexsort((z, cell))
        cell = cell[order]
        z = z[order]
        s = s[order]
        newgrp = np.empty(len(cell), bool)
        newgrp[0] = True
        newgrp[1:] = cell[1:] != cell[:-1]
        wind = np.cumsum(s)
        grp_start = np.where(newgrp)[0]
        base_per_group = np.concatenate([[0], wind[grp_start[1:] - 1]])
        wind = wind - base_per_group[np.cumsum(newgrp) - 1]
        dz = np.empty(len(z))
        dz[:-1] = z[1:] - z[:-1]
        dz[-1] = 0.0
        dz[np.roll(newgrp, -1)] = 0.0             # последний элемент группы интервала не даёт
        solid = wind > 0
        vol_union = float((dz * solid).sum()) * cw * ch
        prev = wind - s
        boundary = ((prev <= 0) & solid) | ((prev > 0) & ~solid)
        exposure = float(boundary.sum()) / max(1, len(boundary))
        zmin_all = float(mn[2])
        air = float((dz * (~solid)).sum())        # внутренние просветы между телами
        enter0 = newgrp & (prev <= 0) & solid     # от пола до первого входа в тело
        air += float((z[enter0] - zmin_all).sum())
        return vol_union, min(1.0, max(0.05, exposure)), max(0.0, air * cw * ch)
    except (MemoryError, ValueError):
        log.debug("column union failed", exc_info=True)
        return None


def _mesh_stats(vseg: bytes, tseg: bytes) -> ObjStats | None:
    """Статы одного меша из байтовых секций <vertices>/<triangles>."""
    verts = _parse_floats(vseg, _V_REPS)
    if verts is None or verts.size < 9 or verts.size % 3:
        return None
    verts = verts.reshape(-1, 3)
    tris = _parse_floats(tseg, _T_REPS)
    if tris is None or tris.size < 3 or tris.size % 3:
        # покраска/доп.атрибуты ломают счёт: защищаем v1/v2/v3 маркерами, сносим ЛЮБЫЕ
        # оставшиеся атрибуты, возвращаем маркеры и парсим заново
        tseg2 = (tseg.replace(b' v1="', b'\x00v1="').replace(b' v2="', b'\x00v2="')
                     .replace(b' v3="', b'\x00v3="'))
        tseg2 = _T_EXTRA.sub(b"", tseg2)
        tseg2 = (tseg2.replace(b'\x00v1="', b' v1="').replace(b'\x00v2="', b' v2="')
                      .replace(b'\x00v3="', b' v3="'))
        tris = _parse_floats(tseg2, _T_REPS)
        if tris is None or tris.size < 3 or tris.size % 3:
            return None
    tris = tris.reshape(-1, 3).astype(np.int64)
    if tris.min() < 0 or tris.max() >= len(verts):
        return None
    return _stats_from_arrays(verts, tris)


def _stats_from_arrays(verts: np.ndarray, tris: np.ndarray) -> ObjStats | None:
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    cr = np.cross(b - a, c - a)                   # |cr| = 2·площадь, cr_z = 2·A·nz
    vol = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0
    a2 = np.einsum("ij,ij->i", cr, cr)
    norm = np.sqrt(a2)
    area = norm / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        nz = np.where(norm > 0, cr[:, 2] / norm, 0.0)
    lat = np.sqrt(np.maximum(0.0, 1.0 - nz * nz))
    sign = (nz < 0).astype(np.int64)
    bidx = np.clip(np.digitize(np.abs(nz), NB_EDGES) - 1, 0, NB - 1)
    flat = sign * NB + bidx
    buckets = np.zeros((2, NB, 3))
    m = 2 * NB
    buckets[:, :, 0].flat = np.bincount(flat, weights=area, minlength=m)[:m]
    buckets[:, :, 1].flat = np.bincount(flat, weights=area * np.abs(nz), minlength=m)[:m]
    buckets[:, :, 2].flat = np.bincount(flat, weights=area * lat, minlength=m)[:m]
    st = ObjStats(abs(vol), buckets, np.stack([verts.min(0), verts.max(0)]), len(verts))
    u = _column_union(verts, tris, a, b, c, cr)
    if u is not None and u[0] > 0:
        # z-колонки — выборка: союз не может превышать знаковый объём
        st.v_union_mm3 = min(u[0], st.v_mm3) if st.v_mm3 > 0 else u[0]
        st.exposure = u[1]
        st.air_mm3 = u[2]
    return st


def _entry_objects(z: zipfile.ZipFile, entry: str, wanted: set) -> tuple[dict, bool]:
    """({oid: ObjStats}, painted) нужных объектов одного .model (entry читается целиком,
    но по одному). painted — есть ли в entry покраска граней (mmu/paint_color) — почти
    бесплатная проверка по уже прочитанным байтам, нужна гейту продувки в fastcalc."""
    try:
        info = z.getinfo(entry)
        if info.file_size > ENTRY_BYTES_CAP:
            log.warning("fastmeasure: %s в %s слишком велик (%d МБ) — пропускаю",
                        entry, z.filename, info.file_size // 2**20)
            return {}, False
        data = z.read(entry)
    except (KeyError, OSError, MemoryError):
        return {}, False
    # только ЦВЕТОВАЯ покраска (продувка): paint_supports — покраска поддержек, не цвета
    painted = b'paint_color="' in data or b'mmu_segmentation="' in data
    out = {}
    for oid, s, e in _object_spans(data):
        if oid not in wanted:
            continue
        seg = data[s:e]
        vs = seg.find(b"<vertices>")
        ve = seg.find(b"</vertices>")
        ts = seg.find(b"<triangles>")
        te = seg.find(b"</triangles>")
        if min(vs, ve, ts, te) < 0:
            continue
        st = _mesh_stats(seg[vs + 10:ve], seg[ts + 11:te])
        if st is not None:
            out[oid] = st
    return out, painted


# ─────────────────────────── инстансы из core с трансформами ───────────────────────────

def _mat(transform: str | None):
    """Трансформа 3MF (12 чисел, row-major) → (R 3×3 колоночная, t 3)."""
    M = np.eye(3)
    t = np.zeros(3)
    if transform:
        try:
            n = [float(x) for x in transform.split()]
            if len(n) >= 12:
                M = np.array(n[:9]).reshape(3, 3).T
                t = np.array(n[9:12])
        except ValueError:
            pass
    return M, t


def iter_instances(core: str):
    """(entry, oid, R, t, top_id) на каждый РАЗМЕЩЁННЫЙ экземпляр (как mesh_volume.
    _iter_instances, но с полными трансформами — нужны для дедупа копий и мировых bbox —
    и id ВЕРХНЕГО объекта: по нему фильтруем выбранные столы, plate_object_ids)."""
    item_tr: dict = {}                             # oid -> СПИСОК трансформ (item может дублироваться)
    for m in re.finditer(r"<item\b[^>]*>", core):
        tag = m.group(0)
        oid = re.search(r'objectid="([^"]+)"', tag)
        if oid:
            tr = re.search(r'transform="([^"]+)"', tag)
            item_tr.setdefault(oid.group(1), []).append(_mat(tr.group(1) if tr else None))
    for m in re.finditer(r'<object\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</object>', core, re.S):
        tid, body = m.group(1), m.group(2)
        placements = item_tr.get(tid)
        if not placements:
            continue                               # объект-ресурс не на плите — не печатается
        comps = re.findall(r"<component\b[^>]*>", body)
        for iR, it in placements:
            if comps:
                for tag in comps:
                    p = re.search(r'p:path="([^"]+)"', tag)
                    oid = re.search(r'objectid="([^"]+)"', tag)
                    tr = re.search(r'transform="([^"]+)"', tag)
                    cR, ct = _mat(tr.group(1) if tr else None)
                    yield (p.group(1).lstrip("/") if p else _CORE,
                           oid.group(1) if oid else None, iR @ cR, iR @ ct + it, tid)
            elif "<mesh" in body:
                yield (_CORE, tid, iR, it, tid)


def _skipped_ids(z: zipfile.ZipFile) -> set:
    """id деталей, которые НЕ печатаются материалом — их геометрию не меряем.

    Для ЗАМЕРА разницы между вырезом (negative_part) и непечатаемой деталью
    (modifier_part, support_*) нет: ни та, ни другая в g-code материалом не идут, а вычитать
    мы здесь всё равно ничего не вычитаем — просто не считаем. Список subtype'ов — общий
    с inspect3mf (single source of truth), читаем из УЖЕ открытого архива."""
    ids: set = set()
    try:
        from . import inspect3mf
        wanted = tuple(f'subtype="{s}"'
                       for s in ("negative_part",) + inspect3mf.NONPRINTING_SUBTYPES)
        for n in z.namelist():
            ln = n.lower()
            if ln.endswith("model_settings.config") or ln.endswith("_model.config"):
                xml = z.read(n).decode("utf-8", "ignore")
                for m in re.finditer(r"<part\b[^>]*>", xml):
                    tag = m.group(0)
                    if any(w in tag for w in wanted):
                        idm = re.search(r'id="([^"]+)"', tag)
                        if idm:
                            ids.add(idm.group(1))
    except Exception:  # noqa: BLE001
        log.debug("skipped ids scan failed", exc_info=True)
    return ids


def _apply_transform(st: ObjStats, R: np.ndarray, t: np.ndarray):
    """(v, buckets, world_bbox) экземпляра. Площади ~ |det|^(2/3) (для ротаций det=1 — точно);
    разложение по nz валидно, пока R сохраняет вертикаль, иначе — изотропная переукладка."""
    det = abs(float(np.linalg.det(R))) or 1.0
    s2 = det ** (2.0 / 3.0)
    corners = np.array([[st.bbox[i][0], st.bbox[j][1], st.bbox[k][2]]
                        for i in (0, 1) for j in (0, 1) for k in (0, 1)])
    wc = corners @ R.T + t
    wbbox = np.stack([wc.min(0), wc.max(0)])
    buckets = st.buckets * s2
    rz = R.T @ np.array([0.0, 0.0, 1.0])
    nrz = float(np.linalg.norm(rz))
    if nrz > 0:
        cz = rz[2] / nrz
        if cz < -0.99:
            buckets = buckets[::-1].copy()        # перевёрнут: верх ↔ низ
        elif cz < 0.99:
            # произвольный поворот: площадь сохраняем, split по nz усредняем по сфере
            A = buckets[:, :, 0].sum()
            iso = np.zeros_like(buckets)
            for bi in range(NB):
                frac = min(NB_EDGES[bi + 1], 1.0) - NB_EDGES[bi]
                mid = (NB_EDGES[bi] + min(NB_EDGES[bi + 1], 1.0)) / 2.0
                aa = A * frac / 2.0
                for sgn in (0, 1):
                    iso[sgn, bi] = [aa, aa * mid, aa * math.sqrt(max(0.0, 1 - mid * mid))]
            buckets = iso
    return st.v_mm3 * det, buckets, wbbox


def _aggregate(placed: list, replicate_geometry: bool, bed: BedSpec = DEFAULT_BED) -> dict | None:
    if not placed:
        return None
    allmin = np.min([p["bbox"][0] for p in placed], 0)
    allmax = np.max([p["bbox"][1] for p in placed], 0)
    ext = allmax - allmin
    multiplate = ext[0] > bed.fit_xy_mm or ext[1] > bed.fit_xy_mm
    z_bots = [p["bbox"][0][2] for p in placed]
    kept = placed
    if not replicate_geometry:
        pass                                       # нативный слайс: проект как есть
    elif len(placed) > 1 and (max(z_bots) - min(z_bots)) > bed.fit_xy_mm:
        # нефизичная раскладка («парящие» детали) → по одному экземпляру каждой геометрии
        seen: set = set()
        kept = []
        for p in placed:
            if p["fp"] in seen:
                continue
            seen.add(p["fp"])
            kept.append(p)
    elif not multiplate:
        # дедуп «наложенных копий» (реплика geometry._dedup_piled)
        groups: dict = {}
        for p in placed:
            groups.setdefault(p["fp"], []).append(p)
        kept = []
        for items in groups.values():
            if len(items) == 1:
                kept.append(items[0])
                continue
            centers = np.array([(it["bbox"][0] + it["bbox"][1]) / 2.0 for it in items])
            spread = float(np.linalg.norm(centers.max(0) - centers.min(0)))
            diag = float(np.median([np.linalg.norm(it["bbox"][1] - it["bbox"][0])
                                    for it in items])) or 1.0
            if spread < bed.pile_ratio * diag:
                kept.append(items[0])
            else:
                kept.extend(items)
    V = sum(p["v"] for p in kept)
    if V <= 0:
        return None
    buckets = np.sum([p["buckets"] for p in kept], 0)
    a_tot = buckets[:, :, 0].sum()
    expo = (sum(p["buckets"][:, :, 0].sum() * p["exposure"] for p in kept) / a_tot
            if a_tot > 0 else 1.0)
    heights = [float(p["bbox"][1][2] - p["bbox"][0][2]) for p in kept]
    # DFM (B2): габарит самой большой ОТДЕЛЬНОЙ детали vs стол (max_part_mm) и самый тонкий
    # габарит среди деталей (min_extent_mm) — по kept, т.е. после дедупа копий/наборов. wall_proxy
    # = 2·V_union/площадь_поверхности ≈ характерная толщина стенки (грубая ОЦЕНКА, не замер: для
    # оболочки толщиной t площадь≈2·A_срединной, объём≈A·t ⇒ 2V/A≈t). Всё низкоуверенное — вердикт
    # трактует его как мягкое предупреждение и только для одной детали (см. texts.printability_verdict).
    p_ext = [p["bbox"][1] - p["bbox"][0] for p in kept]
    max_part_mm = float(max(float(np.max(e)) for e in p_ext)) if p_ext else 0.0
    min_extent_mm = float(min(float(np.min(e)) for e in p_ext)) if p_ext else 0.0
    v_union = float(sum(p["v_union"] for p in kept))
    wall_proxy_mm = float(2.0 * v_union / a_tot) if a_tot > 0 else 0.0
    return {"v_mm3": float(V),
            "v_union_mm3": v_union,
            "exposure": float(expo),
            "air_mm3": float(sum(p["air"] for p in kept)),
            "buckets": buckets, "h_mm": max(heights), "part_heights": heights,
            "n_parts": len(kept), "n_placed": len(placed),
            "footprint": (float(ext[0]), float(ext[1])),
            # XY-футпринты деталей (мм) — для оценки раскладки набора по столам
            # (estimate_plates, кейс «Ам Ням»: 9 деталей → 3 стола, OpenTodo defe5dc0)
            "part_fps": [(float(e[0]), float(e[1])) for e in p_ext],
            "max_part_mm": max_part_mm, "min_extent_mm": min_extent_mm,
            "wall_proxy_mm": wall_proxy_mm}


def measure_3mf(path, replicate_geometry: bool = True,
                plates_only: set | None = None,
                bed: BedSpec = DEFAULT_BED) -> dict | None:
    """Статы .3mf. replicate_geometry=True — как пресетный слайс-путь (дедуп копий);
    False — как нативный слайс (проект как есть). plates_only — 0-базные индексы выбранных
    столов: меряем только их геометрию (как слайс по столам); нет данных о столах → меряем
    всё (честная деградация). None → пусть вызывающий фолбэчит.

    bed — стол принтера: от него зависят гейт мультиплейта и дедуп наложенных копий, то есть
    итоговые объём и число деталей (см. app.bedspec)."""
    p = Path(path)
    try:
        with zipfile.ZipFile(str(p)) as z:
            names = set(z.namelist())
            if _CORE not in names:
                return None
            core = z.read(_CORE).decode("utf-8", "replace")
            skip = _skipped_ids(z)
            insts = [i for i in iter_instances(core) if i[1] is not None]
            if not insts:
                return None
            if plates_only is not None:
                sel = _plate_top_ids(p, plates_only)
                if sel:
                    insts = [i for i in insts if i[4] in sel]
                    if not insts:
                        return None
            wanted: dict = {}
            for entry, oid, _R, _t, _top in insts:
                if entry in names:
                    wanted.setdefault(entry, set()).add(oid)
            raw = {}
            painted = False
            for entry, ids in wanted.items():
                objs, pnt = _entry_objects(z, entry, ids)
                painted = painted or pnt
                raw.update({(entry, oid): st for oid, st in objs.items()})
    except (zipfile.BadZipFile, OSError, MemoryError):
        log.debug("fastmeasure 3mf failed for %s", p, exc_info=True)
        return None
    real = [(e, o, R, t) for e, o, R, t, _top in insts if raw.get((e, o)) is not None]
    kept = [i for i in real if i[1] not in skip]
    if real and not kept:                          # всё помечено непечатаемым — не доверяем
        log.warning("fastmeasure %s: все детали непечатаемые — меряю всё", p.name)
        kept = real
    placed = []
    for entry, oid, R, t in kept:
        st = raw[(entry, oid)]
        v, buckets, wbbox = _apply_transform(st, R, t)
        det = v / st.v_mm3 if st.v_mm3 > 0 else 1.0
        placed.append({"v": v, "v_union": st.v_union_mm3 * det, "exposure": st.exposure,
                       "air": st.air_mm3 * det, "buckets": buckets, "bbox": wbbox,
                       "fp": (entry, oid)})
    agg = _aggregate(placed, replicate_geometry, bed)
    if agg is not None:
        agg["painted"] = painted
    return agg


def _plate_top_ids(p: Path, plates_only: set) -> set:
    """Верхние object-id выбранных столов (inspect3mf.plate_object_ids; id-пространство
    model_settings совпадает с objectid билд-items). Пусто → фильтр не применим."""
    try:
        from . import inspect3mf
        plate_objs = inspect3mf.plate_object_ids(p)
    except Exception:  # noqa: BLE001
        log.debug("plate ids for measure failed for %s", p, exc_info=True)
        return set()
    sel: set = set()
    for i in sorted({int(x) for x in plates_only}):
        if 0 <= i < len(plate_objs):
            sel |= {str(x) for x in plate_objs[i]}
    return sel


# ─────────────────────────── STL/OBJ ───────────────────────────

def _read_binary_stl(path: Path):
    """(verts, tris) бинарного STL через numpy (без trimesh — быстро и без слияния вершин;
    для объёма/площадей/союза дубли вершин не мешают). None — не бинарный STL."""
    try:
        with open(path, "rb") as f:
            head = f.read(84)
            if len(head) < 84:
                return None
            n = int.from_bytes(head[80:84], "little")
            if n <= 0 or 84 + n * 50 != path.stat().st_size:
                return None
            buf = np.frombuffer(f.read(n * 50), dtype=np.uint8)
        if buf.size < n * 50:
            return None
        rec = buf[: n * 50].reshape(n, 50)
        coords = rec[:, 12:48].copy().view("<f4").reshape(n, 3, 3).astype("float64")
        verts = coords.reshape(-1, 3)
        tris = np.arange(n * 3, dtype=np.int64).reshape(n, 3)
        return verts, tris
    except (OSError, ValueError, MemoryError):
        return None


def measure_mesh_file(path) -> dict | None:
    """Статы .stl/.obj/…: бинарный STL — напрямую numpy, прочее — через trimesh."""
    p = Path(path)
    verts = tris = None
    if p.suffix.lower() == ".stl":
        r = _read_binary_stl(p)
        if r is not None:
            verts, tris = r
    if verts is None:
        try:
            import trimesh
            m = trimesh.load(str(p), force="mesh")
            if m is None or not len(getattr(m, "faces", [])):
                return None
            verts = np.asarray(m.vertices, dtype=np.float64)
            tris = np.asarray(m.faces, dtype=np.int64)
        except Exception:  # noqa: BLE001
            log.debug("fastmeasure mesh load failed for %s", p, exc_info=True)
            return None
    st = _stats_from_arrays(verts, tris)
    if st is None:
        return None
    ext = st.bbox[1] - st.bbox[0]
    v = st.v_mm3
    if v <= 0:
        v = st.v_union_mm3 if st.v_union_mm3 > 0 else float(ext[0] * ext[1] * ext[2]) * 0.4
    return {"v_mm3": float(v), "v_union_mm3": float(st.v_union_mm3 or v),
            "exposure": float(st.exposure), "air_mm3": float(st.air_mm3),
            "buckets": st.buckets, "h_mm": float(ext[2]), "part_heights": [float(ext[2])],
            "n_parts": 1, "n_placed": 1, "footprint": (float(ext[0]), float(ext[1])),
            "part_fps": [(float(ext[0]), float(ext[1]))],
            "painted": False}


_MESH_SUFFIXES = (".stl", ".obj", ".ply", ".off")


def measure_model(paths, replicate_geometry: bool = True,
                  plates_only: set | None = None,
                  bed: BedSpec = DEFAULT_BED) -> dict | None:
    """Суммарные статы набора файлов модели. Нечитаемые файлы пропускаются;
    ни одного читаемого → None. plates_only применяется только к одиночному .3mf —
    как у слайса (столы прочих наборов не определены). bed — стол принтера (см. measure_3mf)."""
    paths = list(paths)
    plate_filter = plates_only if (plates_only is not None and len(paths) == 1) else None
    tot: dict | None = None
    for p in paths:
        p = Path(p)
        suf = p.suffix.lower()
        if suf == ".3mf":
            r = measure_3mf(p, replicate_geometry, plates_only=plate_filter, bed=bed)
        elif suf in _MESH_SUFFIXES:
            r = measure_mesh_file(p)
        else:
            r = None
        if not r:
            continue
        if tot is None:
            tot = r
        else:
            a1 = tot["buckets"][:, :, 0].sum()
            a2 = r["buckets"][:, :, 0].sum()
            tot["exposure"] = (tot["exposure"] * a1 + r["exposure"] * a2) / max(1e-9, a1 + a2)
            tot["painted"] = bool(tot.get("painted")) or bool(r.get("painted"))
            tot["v_mm3"] += r["v_mm3"]
            tot["v_union_mm3"] += r["v_union_mm3"]
            tot["air_mm3"] += r["air_mm3"]
            tot["buckets"] = tot["buckets"] + r["buckets"]
            tot["h_mm"] = max(tot["h_mm"], r["h_mm"])
            tot["part_heights"] += r["part_heights"]
            tot["n_parts"] += r["n_parts"]
            tot["n_placed"] += r["n_placed"]
            # футпринты деталей копим ОБЩИМ списком: раскладка по столам считается один раз
            # по всему набору (детали разных файлов делят столы), а не суммой по файлам
            tot["part_fps"] = list(tot.get("part_fps") or []) + list(r.get("part_fps") or [])
    return tot


def estimate_plates(part_fps, bed_mm: float | None = None, *, gap_mm: float | None = None,
                    margin_mm: float | None = None, bed: BedSpec = DEFAULT_BED) -> int:
    """Сколько столов займёт набор деталей при авто-раскладке (кейс «Ам Ням»: 9 деталей с общим
    габаритом 408 мм → слайсер кладёт их на 3 стола, а смета показывала 1, OpenTodo defe5dc0).

    Полочная укладка (shelf FFD) — консервативная реплика auto-arrange слайсера: деталь кладём
    длинной стороной вдоль полки, полки набиваем по убыванию высоты детали; не влезло ни на одну
    полку ни одного стола — новый стол. gap_mm — зазор между деталями (arrange ElegooSlicer
    держит ~5-8 мм с учётом юбки), margin_mm — отступ от краёв стола. Деталь крупнее рабочей
    зоны в любой ориентации считается занимающей стол целиком (реально она не влезет вовсе —
    это дело DFM-вердикта, не раскладки). Пустой список → 1.

    bed_mm / gap_mm / margin_mm не заданы — берутся из bed (BedSpec). Явные значения
    перекрывают стол: так прежние вызовы `estimate_plates(fps, bed_xy)` считают как раньше."""
    bed_mm = bed.xy_mm if bed_mm is None else bed_mm
    gap_mm = bed.arrange_gap_mm if gap_mm is None else gap_mm
    margin_mm = bed.arrange_margin_mm if margin_mm is None else margin_mm
    usable = max(1.0, float(bed_mm) - 2.0 * float(margin_mm))
    items = []
    beds = 0
    for w, d in (part_fps or []):
        a, b = max(float(w), float(d)), min(float(w), float(d))   # a — вдоль полки
        if b > usable or a > usable:
            beds += 1                                  # негабарит: отдельный «стол»
            continue
        items.append((a + gap_mm, b + gap_mm))         # зазор закладываем в саму деталь
    items.sort(key=lambda ab: ab[1], reverse=True)     # по убыванию высоты полки
    shelves: list[list[float]] = []                    # [x_занято, y_верх_полки, h_полки] на все столы
    used_h: list[float] = []                           # занятая высота по столам
    for a, b in items:
        placed = False
        for sh in shelves:
            if sh[2] >= b and sh[0] + a <= usable + gap_mm:   # последний в ряду зазора не требует
                sh[0] += a
                placed = True
                break
        if placed:
            continue
        for i in range(len(used_h)):                   # новая полка на существующем столе
            if used_h[i] + b <= usable + gap_mm:
                shelves.append([a, i, b])
                used_h[i] += b
                placed = True
                break
        if not placed:                                 # новый стол
            used_h.append(b)
            shelves.append([a, len(used_h) - 1, b])
    return max(1, beds + len(used_h))
