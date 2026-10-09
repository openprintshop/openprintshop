"""Допечатка с N-го слоя без сорвавшихся деталей — «Путь В» (docs/Уже в продукте/resume-from-layer.md,
план улучшений — docs/Перспективные разработки/resume-tool-3d-plan-2026-09-05.md).

Ситуация: на столе набор деталей, одна отвалилась на N-м слое, остальные стоят. Стоковый CC2 не
умеет «пропустить объект», а Путь A (app/reprint.py) печатает остаток С НУЛЯ. Здесь из g-code ТОЙ ЖЕ
печати собирается файл, который начинается СРАЗУ с N-го слоя, а все ходы сорвавшихся деталей
вырезаны; оставшиеся детали допечатываются поверх уже напечатанного. Проверено на железе 03.09.2026.

Как детали опознаются — ДВА пути, и первый всегда в приоритете:
  * по МЕТКАМ слайсера (`; printing object … id:N copy M`, `EXCLUDE_OBJECT_START NAME=…`, `M486 S<n>`):
    принадлежность каждого хода точная, детали получают имена из проекта;
  * без меток — эвристика: детали = связные компоненты растра первого присутствующего слоя, ход →
    ближайшая деталь (bbox, при равенстве центр). Верно, только если ни один ход экструзии не
    пересекает границу между деталями (`cross == 0`), иначе сборка ОТКАЗЫВАЕТ.

Стартовый блок пишем свой: без калибровки стола (`BED_MESH_CALIBRATE` щупает стол соплом прямо по
деталям) и без загрузки пластика (`M6211 A1 L200`). Продувка у Elegoo копируется из исходника
(участок `G180 S7 … G180 S8`); у Klipper-профилей — своя короткая линия у переднего края (на железе
не проверена, о чём сборка предупреждает); Bambu — отказ (нужна упаковка в .3mf).

Ядро — чистые функции над файлом: `analyze`, `build`, `verify` (побуквенная сверка экструзий с
исходником), `previews` (PNG), `toolpaths` (бинарные траектории для 3D-вьюера деска). Принтер и БД
здесь не трогаются — это дело ручек app/web/tools.py.
"""
from __future__ import annotations

import base64
import collections
import io
import json
import logging
import math
import re
import struct
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_W = re.compile(r"([XYZEFIJ])(-?\d*\.?\d+)")
_LAYER_RE = re.compile(r"^;LAYER:(\d+)$")
_M73_RE = re.compile(r"^M73 P(\d+) R(\d+)")
_KV_RE = re.compile(r"^; ([a-z0-9_ ]+?)\s*[=:]\s*(.*)$")
_END_RE = re.compile(r"^; filament end gcode|^;=+ .*END[ _-]?G-?CODE", re.I)
_M109_RE = re.compile(r"^M109 S\d+(\.\d+)?")
# метки объектов: OrcaSlicer/ElegooSlicer (`gcode_label_objects`), Klipper (`exclude_object`), Marlin (M486)
_LBL_START = re.compile(r"^; printing object (.+?)(?: id:(\d+))?(?: copy (\d+))?\s*$")
_LBL_STOP = re.compile(r"^; stop printing object")
_EXO_START = re.compile(r"^EXCLUDE_OBJECT_START NAME=(\S+)")
_EXO_END = re.compile(r"^EXCLUDE_OBJECT_END")
_M486_RE = re.compile(r"^M486 S(-?\d+)")
_M486_NAME = re.compile(r'^M486 A"?([^"]+)"?\s+S(\d+)')

CELL = 2.0            # растр первого слоя для поиска деталей, мм
MERGE_MM = 3.0        # компоненты ближе этого расстояния — одна деталь (юбка, разрыв линии)
SAFE_OVER_MM = 4.0    # переезд к столу — выше деталей на столько
HOME_LIFT_Z = 20.0    # подъём сразу после G28, ДО любого движения по XY
LIFT_F = 1200
TRAVEL_F = 20000
CROSS_LIMIT = 0       # ноль пересечений — единственный надёжный критерий разделимости эвристики
DOUBLE_RATIO = 1.7    # E детали ≥ 1.7 медианы при том же bbox — похоже на две копии одна на другой
ARC_STEP_MM = 0.8     # шаг растеризации дуг G2/G3

TYPE_CODES = {"outer wall": 1, "inner wall": 2, "sparse infill": 3, "internal solid infill": 4,
              "top surface": 5, "bottom surface": 5, "solid infill": 4, "support": 6,
              "support interface": 6, "skirt": 7, "brim": 7, "bridge": 8, "overhang wall": 1,
              "gap infill": 4, "custom": 9, "internal bridge": 8, "ironing": 5}
TYPE_NAMES = {0: "переезд", 1: "внешняя стенка", 2: "внутренняя стенка", 3: "заполнение",
              4: "сплошное заполнение", 5: "верх/низ", 6: "поддержка", 7: "юбка/брим",
              8: "мост", 9: "служебное"}


class ResumeUnavailable(RuntimeError):
    """Допечатка невозможна. Текст — ПРИЧИНА ДЛЯ МАСТЕРА, идёт прямо в интерфейс."""


Progress = Callable[[str, float], None]


def _noop(_phase: str, _pct: float) -> None:
    return None


def _parse(ln: str) -> dict | None:
    if not ln.startswith(("G1", "G0", "G2", "G3")):
        return None
    d = {}
    for k, v in _W.findall(ln.split(";")[0]):
        d[k] = float(v)
    if d:
        d["_arc"] = 2 if ln.startswith("G2") else (3 if ln.startswith("G3") else 0)
    return d or None


def _fmt(v: float) -> str:
    s = f"{v:.5f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-", "-0") else "0"


def _arc_points(x0: float, y0: float, x1: float, y1: float, i: float, j: float,
                clockwise: bool, step: float = ARC_STEP_MM) -> list[tuple[float, float]]:
    """Точки вдоль дуги G2/G3 (центр — смещением I/J от старта), без начальной точки."""
    cx, cy = x0 + i, y0 + j
    r = math.hypot(x0 - cx, y0 - cy)
    if r < 1e-6:
        return [(x1, y1)]
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    da = a1 - a0
    if clockwise:
        if da >= 0:
            da -= 2 * math.pi
    elif da <= 0:
        da += 2 * math.pi
    n = max(1, int(abs(da) * r / step))
    out = []
    for k in range(1, n + 1):
        a = a0 + da * k / n
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    out[-1] = (x1, y1)
    return out


# ---------------------------------------------------------------- разбор файла

@dataclass(frozen=True)
class LayerMark:
    n: int
    idx: int          # строка `;LAYER:n`
    change_idx: int   # строка `;LAYER_CHANGE` перед ней (у слоя 0 = idx)
    z: float | None


@dataclass
class ObjectInfo:
    id: int
    cx: float
    cy: float
    x0: float
    y0: float
    x1: float
    y1: float
    cells: int
    name: str = ""
    key: str = ""      # ключ метки слайсера (пусто у эвристики)
    e_mm: float = 0.0
    ax0: float = 0.0   # bbox по ВСЕМ слоям — его рисует карта стола
    ay0: float = 0.0
    ax1: float = 0.0
    ay1: float = 0.0
    zmax: float = 0.0

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name or f"#{self.id}", "key": self.key,
                "cx": round(self.cx, 1), "cy": round(self.cy, 1),
                "bbox": [round(self.x0, 1), round(self.y0, 1), round(self.x1, 1), round(self.y1, 1)],
                "bbox_all": [round(self.ax0, 1), round(self.ay0, 1),
                             round(self.ax1, 1), round(self.ay1, 1)],
                "zmax": round(self.zmax, 2), "e_mm": round(self.e_mm), "cells": self.cells}


@dataclass
class GcodeDoc:
    """Разобранный файл: строки + разметка. Живёт только на время одной операции."""
    path: Path
    lines: list[str]
    exec_start: int
    exec_end: int
    layers: dict[int, LayerMark]
    config: dict[str, str]
    labels: array | None = None          # строка → индекс ключа метки (−1 = без метки)
    label_keys: list[str] = field(default_factory=list)

    @property
    def last_layer(self) -> int:
        return max(self.layers)

    @property
    def first_layer(self) -> int:
        """Первый напечатанный слой файла: 1 у обычного задания, N у уже собранной допечатки."""
        return min(n for n in self.layers if n >= 1)

    @property
    def labeled(self) -> bool:
        return bool(self.label_keys)

    def layer(self, n: int) -> LayerMark:
        if n not in self.layers or n < 1:
            raise ResumeUnavailable(f"Слоя {n} в файле нет (слои {self.first_layer}…{self.last_layer})")
        return self.layers[n]

    def end_marker(self, start: int) -> int:
        for i in range(start, self.exec_end):
            if _END_RE.match(self.lines[i]):
                return i
        return self.exec_end

    def flavor(self) -> str:
        return (self.config.get("gcode_flavor") or "").strip().lower()


def _scan_labels(lines: list[str], lo: int, hi: int) -> tuple[array | None, list[str]]:
    """Карта «строка → ключ метки объекта». Понимает три диалекта разом; пустая — если меток нет."""
    keys: dict[str, int] = {}
    m486_names: dict[int, str] = {}
    labels = array("h", [-1]) * len(lines)
    cur = -1
    found = False
    for i in range(lo, hi):
        ln = lines[i]
        if not ln or ln[0] not in ";EM":
            labels[i] = cur
            continue
        if ln.startswith("; printing object"):
            m = _LBL_START.match(ln)
            if m:
                key = m.group(1).strip()
                if m.group(2):
                    key += f" id:{m.group(2)}"
                if m.group(3):
                    key += f" copy {m.group(3)}"
                cur = keys.setdefault(key, len(keys))
                found = True
        elif ln.startswith("; stop printing object"):
            cur = -1
        elif ln.startswith("EXCLUDE_OBJECT_START"):
            m = _EXO_START.match(ln)
            if m:
                cur = keys.setdefault(m.group(1), len(keys))
                found = True
        elif ln.startswith("EXCLUDE_OBJECT_END"):
            cur = -1
        elif ln.startswith("M486"):
            mn = _M486_NAME.match(ln)
            if mn:
                m486_names[int(mn.group(2))] = mn.group(1).strip()
            else:
                m = _M486_RE.match(ln)
                if m:
                    n = int(m.group(1))
                    if n < 0:
                        cur = -1
                    else:
                        cur = keys.setdefault(m486_names.get(n, f"object {n}"), len(keys))
                        found = True
        labels[i] = cur
    if not found:
        return None, []
    return labels, [k for k, _ in sorted(keys.items(), key=lambda kv: kv[1])]


def load(path: str | Path) -> GcodeDoc:
    """Прочитать файл и разметить: исполняемый блок, слои с их Z, конфиг-хвост, метки объектов."""
    p = Path(path)
    if not p.is_file():
        raise ResumeUnavailable("Файл не найден")
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        lines = f.read().split("\n")
    lines = [ln.rstrip("\r") for ln in lines]
    try:
        exec_start = next(i for i, l in enumerate(lines) if l.startswith("; EXECUTABLE_BLOCK_START"))
    except StopIteration:
        raise ResumeUnavailable("Это не g-code ElegooSlicer/OrcaSlicer: нет блока EXECUTABLE_BLOCK") from None
    exec_end = next((i for i in range(exec_start, len(lines))
                     if lines[i].startswith("; EXECUTABLE_BLOCK_END")), len(lines))
    layers: dict[int, LayerMark] = {}
    for i in range(exec_start, exec_end):
        m = _LAYER_RE.match(lines[i])
        if not m:
            continue
        n = int(m.group(1))
        j = i
        while j > exec_start and not lines[j].startswith(";LAYER_CHANGE"):
            j -= 1
            if i - j > 40:
                j = i
                break
        z = None
        for k in range(j, i):
            if lines[k].startswith(";Z:"):
                try:
                    z = float(lines[k][3:])
                except ValueError:
                    z = None
                break
        layers[n] = LayerMark(n=n, idx=i, change_idx=j if j != i or n == 0 else i, z=z)
    if len([n for n in layers if n >= 1]) < 2:
        raise ResumeUnavailable("В файле нет разметки слоёв (;LAYER_CHANGE / ;LAYER:n)")
    config: dict[str, str] = {}
    for ln in lines[exec_end:]:
        m = _KV_RE.match(ln)
        if m:
            config.setdefault(m.group(1).strip(), m.group(2).strip())
    for ln in lines[:exec_start]:
        m = _KV_RE.match(ln)
        if m:
            config.setdefault(m.group(1).strip(), m.group(2).strip())
        g = re.match(r"^; generated by (.+?) on (.+)$", ln)
        if g:
            config.setdefault("generated by", g.group(1).strip())
            config.setdefault("sliced at", g.group(2).strip())
    labels, keys = _scan_labels(lines, exec_start, exec_end)
    return GcodeDoc(path=p, lines=lines, exec_start=exec_start, exec_end=exec_end,
                    layers=layers, config=config, labels=labels, label_keys=keys)


def bed_size(doc: GcodeDoc) -> tuple[float, float]:
    """Размер стола из `printable_area` (0x0,256x0,256x256,0x256); нет — 256×256."""
    raw = doc.config.get("printable_area", "")
    xs, ys = [], []
    for pair in raw.split(","):
        if "x" in pair:
            a, _, b = pair.partition("x")
            try:
                xs.append(float(a))
                ys.append(float(b))
            except ValueError:
                pass
    if xs and ys and max(xs) > 0 and max(ys) > 0:
        return max(xs), max(ys)
    return 256.0, 256.0


def _minutes_at(doc: GcodeDoc, upto: int) -> tuple[int | None, int | None]:
    """(всего минут по первому M73, осталось минут по последнему M73 до строки upto)."""
    total = remaining = None
    for i in range(doc.exec_start, min(upto, doc.exec_end)):
        m = _M73_RE.match(doc.lines[i])
        if m:
            if total is None:
                total = int(m.group(2))
            remaining = int(m.group(2))
    return total, remaining


# ---------------------------------------------------------------- ходы

def _segments(lines: list[str], lo: int, hi: int, *, with_index: bool = False):
    """Экструзионные отрезки (x0, y0, x1, y1, e, i) между строками; дуги растеризованы."""
    X = Y = None
    for i in range(lo, hi):
        d = _parse(lines[i])
        if d is None:
            continue
        nx = d.get("X", X)
        ny = d.get("Y", Y)
        if (d.get("E", 0.0) > 0 and ("X" in d or "Y" in d)
                and X is not None and Y is not None and nx is not None and ny is not None):
            if d["_arc"] and ("I" in d or "J" in d):
                px, py = X, Y
                pts = _arc_points(X, Y, nx, ny, d.get("I", 0.0), d.get("J", 0.0), d["_arc"] == 2)
                share = d["E"] / len(pts)
                for qx, qy in pts:
                    yield (px, py, qx, qy, share, i)
                    px, py = qx, qy
            else:
                yield (X, Y, nx, ny, d["E"], i)
        X, Y = nx, ny


def _points(lines: list[str], lo: int, hi: int, step: float):
    """Точки вдоль экструзионных ходов (для растра); переезды не считаются."""
    for x0, y0, x1, y1, _e, _i in _segments(lines, lo, hi):
        dist = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(dist / step))
        for t in range(n + 1):
            yield x0 + (x1 - x0) * t / n, y0 + (y1 - y0) * t / n


# ---------------------------------------------------------------- детали

def _objects_from_raster(doc: GcodeDoc) -> list[ObjectInfo]:
    """Эвристика: детали = связные компоненты растра ПЕРВОГО присутствующего слоя. Первый слой
    выбран сознательно: на нём детали разделены зазором, а выше лучи/свесы соседей могут
    подходить вплотную."""
    n1 = doc.first_layer
    l1 = doc.layer(n1)
    hi = doc.layers[n1 + 1].change_idx if n1 + 1 in doc.layers else doc.end_marker(l1.idx)
    occ: dict[tuple[int, int], int] = {}
    pts: list[tuple[float, float]] = []
    for x, y in _points(doc.lines, l1.idx, hi, CELL / 2):
        pts.append((x, y))
        occ.setdefault((int(math.floor(x / CELL)), int(math.floor(y / CELL))), -1)
    if not occ:
        raise ResumeUnavailable("На первом слое нет экструзии — детали не найти")
    comp = 0
    for c in list(occ):
        if occ[c] != -1:
            continue
        stack = [c]
        occ[c] = comp
        while stack:
            a = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    b = (a[0] + dx, a[1] + dy)
                    if occ.get(b) == -1:
                        occ[b] = comp
                        stack.append(b)
        comp += 1
    boxes: dict[int, list[float]] = {}
    for x, y in pts:
        k = occ[(int(math.floor(x / CELL)), int(math.floor(y / CELL)))]
        b = boxes.setdefault(k, [x, y, x, y, 0])
        b[0] = min(b[0], x)
        b[1] = min(b[1], y)
        b[2] = max(b[2], x)
        b[3] = max(b[3], y)
        b[4] += 1
    comps = list(boxes.values())
    merged = True
    while merged:
        merged = False
        for a in range(len(comps)):
            for b in range(a + 1, len(comps)):
                A, B = comps[a], comps[b]
                if (A[0] - MERGE_MM <= B[2] and B[0] - MERGE_MM <= A[2]
                        and A[1] - MERGE_MM <= B[3] and B[1] - MERGE_MM <= A[3]):
                    comps[a] = [min(A[0], B[0]), min(A[1], B[1]), max(A[2], B[2]),
                                max(A[3], B[3]), A[4] + B[4]]
                    del comps[b]
                    merged = True
                    break
            if merged:
                break
    comps.sort(key=lambda c: (round((c[1] + c[3]) / 2 / 30), (c[0] + c[2]) / 2))
    out = []
    for i, c in enumerate(comps, 1):
        out.append(ObjectInfo(id=i, cx=(c[0] + c[2]) / 2, cy=(c[1] + c[3]) / 2,
                              x0=c[0], y0=c[1], x1=c[2], y1=c[3], cells=int(c[4]),
                              ax0=c[0], ay0=c[1], ax1=c[2], ay1=c[3]))
    return out


def _pretty_label(key: str, copies: dict[str, int]) -> str:
    """«Сборка id:3 copy 1» → «Сборка (копия 2)»; «Spinner_id_2_copy_0» → «Spinner (копия 1)»."""
    m = re.match(r"^(.*?)(?: id:(\d+))?(?: copy (\d+))?$", key)
    name, copy = key, None
    if m:
        name = m.group(1).strip()
        copy = m.group(3)
    m2 = re.match(r"^(.*?)_id_\d+_copy_(\d+)$", name)
    if m2:
        name, copy = m2.group(1), m2.group(2)
    name = name.replace("_", " ").strip() or key
    if copies.get(name, 0) > 1 and copy is not None:
        return f"{name} (копия {int(copy) + 1})"
    return name


def _objects_from_labels(doc: GcodeDoc) -> list[ObjectInfo]:
    """Детали по меткам слайсера: bbox и центр — по первому присутствующему слою, если деталь на
    нём есть, иначе по первому своему слою. Порядок — как у эвристики (рядами от передней кромки)."""
    keys = doc.label_keys
    first: dict[int, list[float]] = {}
    n1 = doc.first_layer
    l1 = doc.layer(n1)
    hi1 = doc.layers[n1 + 1].change_idx if n1 + 1 in doc.layers else doc.end_marker(l1.idx)
    for x0, y0, x1, y1, _e, i in _segments(doc.lines, l1.idx, hi1):
        k = doc.labels[i]
        if k < 0:
            continue
        b = first.setdefault(k, [x1, y1, x1, y1, 0])
        for x, y in ((x0, y0), (x1, y1)):
            b[0] = min(b[0], x)
            b[1] = min(b[1], y)
            b[2] = max(b[2], x)
            b[3] = max(b[3], y)
        b[4] += 1
    # детали, которых нет на первом слое (поддержки, подставки): bbox по первым своим ходам
    missing = [k for k in range(len(keys)) if k not in first]
    if missing:
        need = set(missing)
        for x0, y0, x1, y1, _e, i in _segments(doc.lines, l1.idx, doc.end_marker(l1.idx)):
            k = doc.labels[i]
            if k in need:
                b = first.setdefault(k, [x1, y1, x1, y1, 0])
                for x, y in ((x0, y0), (x1, y1)):
                    b[0] = min(b[0], x)
                    b[1] = min(b[1], y)
                    b[2] = max(b[2], x)
                    b[3] = max(b[3], y)
                b[4] += 1
                if b[4] > 400:
                    need.discard(k)
                    if not need:
                        break
    base_names = collections.Counter()
    for key in keys:
        m = re.match(r"^(.*?)(?: id:\d+)?(?: copy \d+)?$", key)
        nm = (m.group(1) if m else key).strip()
        nm = re.sub(r"_id_\d+_copy_\d+$", "", nm).replace("_", " ").strip()
        base_names[nm] += 1
    items = []
    for k, key in enumerate(keys):
        b = first.get(k)
        if not b:
            continue
        items.append((k, key, b))
    items.sort(key=lambda t: (round((t[2][1] + t[2][3]) / 2 / 30), (t[2][0] + t[2][2]) / 2))
    out = []
    for i, (k, key, b) in enumerate(items, 1):
        out.append(ObjectInfo(id=i, cx=(b[0] + b[2]) / 2, cy=(b[1] + b[3]) / 2,
                              x0=b[0], y0=b[1], x1=b[2], y1=b[3], cells=int(b[4]),
                              name=_pretty_label(key, base_names), key=key,
                              ax0=b[0], ay0=b[1], ax1=b[2], ay1=b[3]))
    if not out:
        raise ResumeUnavailable("Метки объектов есть, но ни одна деталь не содержит экструзии")
    return out


def detect_objects(doc: GcodeDoc) -> list[ObjectInfo]:
    return _objects_from_labels(doc) if doc.labeled else _objects_from_raster(doc)


class Membership:
    """Какой детали принадлежит ход. С метками — по метке строки; без метки (переезды, wipe вне
    объекта, файлы без разметки) — ближайший bbox первого слоя (внутри = 0), при равенстве —
    ближайший центр; `centroid=True` — чистый Voronoi по центрам (запасной режим эвристики).

    Память на сетке 1 мм хранит ответ только там, где до второй по близости детали ЗАВЕДОМО
    далеко: у границ считаем каждую точку честно, иначе клетка памяти размазала бы границу."""

    def __init__(self, objects: list[ObjectInfo], doc: GcodeDoc | None = None, *,
                 centroid: bool = False):
        self.objects = objects
        self.centroid = centroid
        self.memo: dict[tuple[int, int], int] = {}
        self.labels = doc.labels if (doc is not None and doc.labeled) else None
        self.by_key: dict[int, int] = {}
        if self.labels is not None:
            key_index = {key: n for n, key in enumerate(doc.label_keys)}
            for o in objects:
                if o.key in key_index:
                    self.by_key[key_index[o.key]] = o.id

    @property
    def mode(self) -> str:
        return "labels" if self.labels is not None else ("centroid" if self.centroid else "bbox")

    def _exact(self, x: float, y: float) -> tuple[int, float]:
        best, bd, bc, second = None, 1e18, 1e18, 1e18
        for o in self.objects:
            if self.centroid:
                d = (x - o.cx) ** 2 + (y - o.cy) ** 2
                c = d
            else:
                dx = max(o.x0 - x, 0.0, x - o.x1)
                dy = max(o.y0 - y, 0.0, y - o.y1)
                d = dx * dx + dy * dy
                c = (x - o.cx) ** 2 + (y - o.cy) ** 2
            if d < bd - 1e-9 or (abs(d - bd) <= 1e-9 and c < bc):
                second = bd
                best, bd, bc = o.id, d, c
            elif d < second:
                second = d
        margin = math.sqrt(second) - math.sqrt(bd) if second < 1e17 else 1e9
        return best, margin

    def nearest(self, x: float, y: float) -> int:
        key = (int(math.floor(x)), int(math.floor(y)))
        r = self.memo.get(key)
        if r is not None:
            return r
        best, margin = self._exact(x, y)
        if margin > 3.0:
            self.memo[key] = best
        return best

    def __call__(self, x: float, y: float, i: int | None = None) -> int:
        if self.labels is not None and i is not None:
            k = self.labels[i]
            if k >= 0:
                oid = self.by_key.get(k)
                if oid is not None:
                    return oid
        return self.nearest(x, y)


def _scan_assignment(doc: GcodeDoc, objects: list[ObjectInfo], assign: Membership,
                     start: int, progress: Progress = _noop) -> int:
    """Один проход по телу: пересечения границ (у эвристики), E, bbox и высота по всем слоям."""
    by_id = {o.id: o for o in objects}
    cross = 0
    first = {o.id: True for o in objects}
    end = doc.end_marker(start)
    z = 0.0
    X = Y = None
    span = max(1, end - start)
    for i in range(start, end):
        if (i - start) % 200000 == 0:
            progress("analyze", (i - start) / span)
        ln = doc.lines[i]
        d = _parse(ln)
        if d is None:
            continue
        nx = d.get("X", X)
        ny = d.get("Y", Y)
        if "Z" in d:
            z = d["Z"]
        if (d.get("E", 0.0) > 0 and ("X" in d or "Y" in d) and X is not None
                and nx is not None and ny is not None):
            r1 = assign(nx, ny, i)
            if assign.labels is None and assign(X, Y, i) != r1:
                cross += 1
            o = by_id[r1]
            o.e_mm += d["E"]
            if first[r1]:
                o.ax0, o.ay0, o.ax1, o.ay1 = nx, ny, nx, ny
                first[r1] = False
            o.ax0 = min(o.ax0, nx)
            o.ay0 = min(o.ay0, ny)
            o.ax1 = max(o.ax1, nx)
            o.ay1 = max(o.ay1, ny)
            if z > o.zmax:
                o.zmax = z
        X, Y = nx, ny
    return cross


def membership(doc: GcodeDoc, objects: list[ObjectInfo], progress: Progress = _noop) -> tuple[Membership, int]:
    """Подобрать способ принадлежности: метки → bbox → центры. Возвращает (assign, cross)."""
    start = doc.layer(doc.first_layer).idx
    assign = Membership(objects, doc)
    cross = _scan_assignment(doc, objects, assign, start, progress)
    if assign.labels is None and cross > CROSS_LIMIT and len(objects) > 1:
        # Второй заход: детали растут выше первого слоя (свесы, поддержки, «грибы» на ножке) —
        # ближайший bbox ПЕРВОГО слоя приписывает их верх соседу. Берём габариты по всем слоям
        # из первого прохода и считаем заново; если и так пересечения — Voronoi по центрам.
        grown = [ObjectInfo(id=o.id, cx=o.cx, cy=o.cy, x0=min(o.x0, o.ax0), y0=min(o.y0, o.ay0),
                            x1=max(o.x1, o.ax1), y1=max(o.y1, o.ay1), cells=o.cells, name=o.name,
                            key=o.key) for o in objects]
        overlap = any(a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1
                      for i, a in enumerate(grown) for b in grown[i + 1:])
        if not overlap:
            assign2 = Membership(grown, doc)
            cross2 = _scan_assignment(doc, grown, assign2, start, progress)
            if cross2 <= CROSS_LIMIT:
                for o, g in zip(objects, grown):
                    o.e_mm, o.zmax = g.e_mm, g.zmax
                    o.ax0, o.ay0, o.ax1, o.ay1 = g.ax0, g.ay0, g.ax1, g.ay1
                return assign2, cross2
        for o in objects:
            o.e_mm = 0.0
            o.zmax = 0.0
        assign = Membership(objects, doc, centroid=True)
        cross = _scan_assignment(doc, objects, assign, start, progress)
    return assign, cross


@dataclass
class Analysis:
    path: str
    objects: list[ObjectInfo]
    layers: int
    first_layer: int
    layer_z: list[float | None]      # индекс = номер слоя (0 — пусто)
    bed: tuple[float, float]
    minutes_total: int | None
    printer_model: str
    slicer: str
    separable: bool
    cross: int
    mode: str
    style: str
    warnings: list[str] = field(default_factory=list)
    supported: bool = True
    reason: str = ""
    labeled: bool = False

    def as_dict(self) -> dict:
        return {"ok": True, "path": self.path, "objects": [o.as_dict() for o in self.objects],
                "layers": self.layers, "first_layer": self.first_layer, "layer_z": self.layer_z,
                "bed": list(self.bed), "minutes_total": self.minutes_total,
                "printer_model": self.printer_model, "slicer": self.slicer,
                "separable": self.separable, "cross": self.cross, "mode": self.mode,
                "style": self.style, "warnings": self.warnings, "supported": self.supported,
                "reason": self.reason, "labeled": self.labeled}


def start_style(doc: GcodeDoc) -> tuple[str, str]:
    """Каким стартовым блоком собирать: ('elegoo'|'klipper', причина отказа или '')."""
    head = doc.lines[doc.exec_start:doc.layers[doc.first_layer].change_idx]
    has_g28 = any(l.startswith("G28") for l in head)
    if any(l.startswith("G180 S7") for l in head) and any(l.startswith("G180 S8") for l in head):
        return "elegoo", "" if has_g28 else "В стартовом блоке нет G28 — профиль незнаком"
    model = (doc.config.get("printer_model") or "").lower()
    flavor = doc.flavor()
    if "bambu" in model or flavor == "bambu" or any(l.startswith("M1002") for l in head):
        return "bambu", ("Bambu ждёт задание в .3mf, а не .gcode, и калибрует стол своим стартом — "
                         "допечатка для него пока не собирается")
    if flavor in ("klipper", "marlin2", "marlin", "reprapfirmware") and has_g28:
        return "klipper", ""
    return "unknown", ("Стартовый блок этого профиля незнаком (нет продувки G180 S7…S8 и не Klipper): "
                       "поддерживаются профили Elegoo Centauri / CC2 из ElegooSlicer и Klipper-профили Orca")


def analyze(path: str | Path, progress: Progress = _noop) -> Analysis:
    """Разобрать файл: детали, слои, время, разделимость. Ничего не пишет."""
    progress("load", 0.0)
    doc = load(path)
    progress("objects", 0.0)
    objects = detect_objects(doc)
    assign, cross = membership(doc, objects, progress)
    warnings: list[str] = []
    es = sorted(o.e_mm for o in objects)
    med = es[len(es) // 2] if es else 0.0
    if med > 0 and len(objects) > 2:
        for o in objects:
            if o.e_mm >= DOUBLE_RATIO * med:
                warnings.append(f"Деталь «{o.name or '#%d' % o.id}»: пластика в {o.e_mm / med:.1f} раза больше "
                                "медианы при тех же габаритах — похоже на две копии одна на другой "
                                "(слайсер пишет «gcode path conflicts»). Именно так детали и срываются.")
    style, reason = start_style(doc)
    model = doc.config.get("printer_model", "")
    if not reason and style == "klipper":
        warnings.append(f"Профиль «{model or '?'}» (Klipper): стартовый блок собран по общему образцу и на "
                        "железе не проверялся — первые слои смотреть особенно внимательно")
    elif not reason and "centauri" not in model.lower():
        warnings.append(f"Профиль принтера «{model or '?'}» на железе не проверялся: "
                        "стартовый блок собран по образцу Elegoo Centauri")
    if not doc.labeled:
        warnings.append("В файле нет меток объектов (gcode_label_objects = 0): детали найдены по первому "
                        "слою эвристикой. С метками слайсера принадлежность точная, а детали получают имена.")
    total, _ = _minutes_at(doc, doc.exec_end)
    layer_z: list[float | None] = [None] * (doc.last_layer + 1)
    for n, lm in doc.layers.items():
        layer_z[n] = lm.z
    progress("done", 1.0)
    return Analysis(path=str(doc.path), objects=objects, layers=doc.last_layer,
                    first_layer=doc.first_layer, layer_z=layer_z, bed=bed_size(doc),
                    minutes_total=total, printer_model=model,
                    slicer=doc.config.get("generated by", ""),
                    separable=assign.labels is not None or cross <= CROSS_LIMIT, cross=cross,
                    mode=assign.mode, style=style, warnings=warnings, supported=not reason,
                    reason=reason, labeled=doc.labeled)


# ---------------------------------------------------------------- сборка

@dataclass
class BuildReport:
    out: Path
    layer: int
    z_layer: float
    parts_height: float
    excluded: list[int]
    excluded_names: list[str]
    kept_objects: int
    dropped_lines: int
    resyncs: int
    mismatch_xy: int
    dropped_e_mm: float
    kept_e_mm: float
    retract_fix: float
    minutes_remaining: int | None
    first_object: int | None
    first_object_name: str
    nozzle: float
    bed: float
    size_bytes: int
    warnings: list[str]
    last_layer: int = 0
    style: str = "elegoo"
    mode: str = "bbox"
    start_moves: list[list[float]] = field(default_factory=list)   # [x, y, z] точки стартового блока
    start_check: dict = field(default_factory=dict)
    centre_object: str = ""

    def as_dict(self) -> dict:
        return {"filename": self.out.name, "layer": self.layer, "z_layer": self.z_layer,
                "last_layer": self.last_layer, "parts_height": round(self.parts_height, 3),
                "excluded": self.excluded, "excluded_names": self.excluded_names,
                "kept_objects": self.kept_objects, "dropped_lines": self.dropped_lines,
                "resyncs": self.resyncs, "mismatch_xy": self.mismatch_xy,
                "dropped_e_mm": round(self.dropped_e_mm), "kept_e_mm": round(self.kept_e_mm),
                "retract_fix": round(self.retract_fix, 3),
                "minutes_remaining": self.minutes_remaining, "first_object": self.first_object,
                "first_object_name": self.first_object_name, "nozzle": self.nozzle, "bed": self.bed,
                "size_bytes": self.size_bytes, "warnings": self.warnings, "style": self.style,
                "mode": self.mode, "start_moves": self.start_moves, "start_check": self.start_check,
                "centre_object": self.centre_object}


def _last_state(lines: list[str], lo: int, hi: int) -> dict:
    """Последние значения температур/вентиляторов/PA/ускорений и позиция ДО среза."""
    st: dict = {"X": None, "Y": None, "Z": None, "F": None, "tool": None, "vel": None}
    for i in range(lo, hi):
        ln = lines[i]
        d = _parse(ln)
        if d:
            for k in ("X", "Y", "Z", "F"):
                if k in d:
                    st[k] = d[k]
            continue
        # температуры: последняя из M104/M109 (сопло) и M140/M190 (стол) — в профилях Elegoo
        # рабочая температура ставится на втором слое через M104, но у другого профиля её может
        # задать только M109, и тогда «последний M104» был бы стартовыми 140°
        for tags, key in ((("M104", "M109"), "nozzle"), (("M140", "M190"), "bed"), (("M204",), "M204")):
            if ln.startswith(tuple(t + " " for t in tags)) or ln in tags:
                m = re.search(r"S(-?\d+(?:\.\d+)?)", ln)
                if m:
                    st[key] = float(m.group(1))
        if ln.startswith("M106 P2"):
            st["fan_p2"] = ln.split(";")[0].strip()
        elif ln.startswith("M106 P3"):
            st["fan_p3"] = ln.split(";")[0].strip()
        elif ln.startswith("M106"):
            st["fan"] = ln.split(";")[0].strip()
        elif ln.startswith("M107"):
            st["fan"] = "M106 S0"
        elif ln.startswith("SET_PRESSURE_ADVANCE"):
            st["pa"] = ln.split(";")[0].strip()
        elif ln.startswith("SET_VELOCITY_LIMIT"):
            st["vel"] = ln.split(";")[0].strip()
        elif re.match(r"^T\d+\s*$", ln):
            st["tool"] = ln.strip()
    return st


def _purge_block(doc: GcodeDoc, style: str, nozzle: float, bed: tuple[float, float]) -> list[str]:
    """Продувка перед допечаткой: у Elegoo — стоковый участок `G180 S7 … G180 S8` исходника (с
    температурой печати), у Klipper — короткая линия у переднего края стола."""
    head = doc.lines[doc.exec_start:doc.layers[doc.first_layer].change_idx]
    if style == "elegoo":
        out, grab = [], False
        for l in head:
            if l.startswith("G180 S7"):
                grab = True
            if grab:
                if _M73_RE.match(l):
                    continue
                out.append(_M109_RE.sub(f"M109 S{_fmt(nozzle)}", l))
            if l.startswith("G180 S8"):
                break
        return out
    x0 = max(2.0, bed[0] * 0.25)
    return [
        f"G1 X{_fmt(x0)} Y1 F{TRAVEL_F}",
        "G1 Z0.4 F900",
        f"M109 S{_fmt(nozzle)}",
        "M83",
        "G92 E0",
        "G1 E4 F300",
        f"G1 X{_fmt(x0 + 40)} E8 F1200",
        f"G1 X{_fmt(x0 + 45)} E0.4 F6000",
        "G92 E0",
    ]


def _position_after(lines_out: list[str], start_xy: tuple[float, float] | None = None) -> tuple[float | None, float | None, float | None]:
    """Позиция сопла после списка строк (абсолютные XY/Z)."""
    X = Y = Z = None
    if start_xy:
        X, Y = start_xy
    for s in lines_out:
        d = _parse(s)
        if d:
            X = d.get("X", X)
            Y = d.get("Y", Y)
            Z = d.get("Z", Z)
    return X, Y, Z


def _check_start_moves(moves: list[list[float]], objects: list[ObjectInfo], excl: set[int],
                       parts_height: float) -> dict:
    """Переезды стартового блока над оставленными деталями обязаны идти выше их макушки."""
    kept = [o for o in objects if o.id not in excl]
    bad = []
    for a, b in zip(moves, moves[1:]):
        if a[2] is None or b[2] is None:
            continue
        zmin = min(a[2], b[2])
        if zmin >= parts_height + 0.05:
            continue
        for o in kept:
            for t in (0.0, 0.25, 0.5, 0.75, 1.0):
                x = a[0] + (b[0] - a[0]) * t
                y = a[1] + (b[1] - a[1]) * t
                if o.x0 - 1 <= x <= o.x1 + 1 and o.y0 - 1 <= y <= o.y1 + 1:
                    bad.append({"object": o.id, "name": o.name or f"#{o.id}", "z": round(zmin, 3),
                                "x": round(x, 1), "y": round(y, 1)})
                    break
            else:
                continue
            break
    return {"ok": not bad, "collisions": bad}


def build(path: str | Path, layer: int, exclude: list[int], out: str | Path, *,
          expect: dict[int, tuple[float, float]] | None = None, name_hint: str = "",
          progress: Progress = _noop) -> BuildReport:
    """Собрать файл допечатки: слои < layer выброшены, детали `exclude` вырезаны везде.

    `expect` — центры деталей, которые видел мастер: если файл на диске подменили и раскладка
    другая, вырезать «деталь 3» значило бы вырезать не ту. Сверяем и отказываем."""
    progress("load", 0.0)
    doc = load(path)
    style, reason = start_style(doc)
    if reason:
        raise ResumeUnavailable(reason)
    objects = detect_objects(doc)
    by_id = {o.id: o for o in objects}
    exclude = sorted({int(i) for i in exclude})
    if not exclude:
        raise ResumeUnavailable("Не выбрано ни одной детали для выреза")
    for i in exclude:
        if i not in by_id:
            raise ResumeUnavailable(f"Детали #{i} в файле нет — откройте файл заново")
        if expect and i in expect:
            ex, ey = expect[i]
            if abs(ex - by_id[i].cx) > 0.6 or abs(ey - by_id[i].cy) > 0.6:
                raise ResumeUnavailable("Раскладка деталей изменилась — откройте файл заново")
    if len(exclude) >= len(objects):
        raise ResumeUnavailable("Нельзя вырезать все детали — печатать было бы нечего")
    lo = doc.first_layer + 1
    if layer < lo or layer > doc.last_layer:
        raise ResumeUnavailable(f"Слой должен быть от {lo} до {doc.last_layer}")
    assign, cross = membership(doc, objects, progress)
    if assign.labels is None and cross > CROSS_LIMIT:
        raise ResumeUnavailable(
            f"Детали не разделяются: {cross} ходов экструзии пересекают границы между ними. "
            "Включите метки объектов в профиле слайсера (gcode_label_objects) — тогда вырез точный")
    lines = doc.lines
    lm = doc.layer(layer)
    cut = lm.change_idx
    z_layer = lm.z
    prev = doc.layer(layer - 1).z
    if z_layer is None or prev is None:
        raise ResumeUnavailable("У слоя нет высоты Z в разметке — файл незнакомого слайсера")
    end_marker = doc.end_marker(cut)
    st = _last_state(lines, doc.exec_start, cut)
    nozzle = st.get("nozzle") or 0.0
    bed_t = st.get("bed") or 0.0
    if nozzle <= 0 or bed_t < 0:
        raise ResumeUnavailable("Не нашёл температуру сопла до среза (M104) — файл незнакомого профиля")
    warnings: list[str] = []
    excl = set(exclude)
    bed = bed_size(doc)
    progress("build", 0.0)

    head = lines[doc.exec_start:doc.layers[doc.first_layer].change_idx]
    purge = _purge_block(doc, style, nozzle, bed)
    m190 = next((l for l in head if l.startswith("M190")), "M190 S{bed}")
    m190 = re.sub(r"S-?\d+(?:\.\d+)?", f"S{_fmt(bed_t)}", m190, count=1).replace("{bed}", _fmt(bed_t))
    total, remaining = _minutes_at(doc, cut)
    safe_z = prev + SAFE_OVER_MM
    excl_names = [by_id[i].name or f"#{i}" for i in exclude]
    start: list[str] = [
        "; EXECUTABLE_BLOCK_START",
        f"M73 P{max(0, min(99, round(100 * (1 - (remaining or 0) / total)) if total else 0))} "
        f"R{remaining or 0}",
        f";===== RESUME START (Print Studio, {time.strftime('%Y-%m-%d %H:%M')}, style {style}) =====",
        f";===== continue from layer {layer} (Z {_fmt(z_layer)}); parts on bed are {_fmt(prev)} mm tall",
        ";===== excluded objects: " + ", ".join(
            f"#{i} {by_id[i].name} ({by_id[i].cx:.1f},{by_id[i].cy:.1f})" for i in exclude),
        ";===== NO bed mesh, NO nozzle probing, NO filament load: G28 only -> keep the bed centre clear",
        "G90",
        "G21",
        "M83",
        f"M140 S{_fmt(bed_t)}",
        "M104 S150 ; warm, no ooze while homing",
        m190,
        "M106 S0",
        "M106 P2 S0",
        f"M204 S{_fmt(st.get('M204') or 5000)}",
        "G28",
        f"G1 Z{_fmt(max(HOME_LIFT_Z, safe_z))} F{LIFT_F} ; lift BEFORE any XY move",
    ]
    if st.get("tool"):
        start.append(st["tool"])
    start += purge
    start += [f"G1 F{TRAVEL_F}", "G92 E0"]

    out_lines: list[str] = []
    stats = collections.Counter()
    dropped_e = kept_e = 0.0
    X, Y, Z, f_orig = st["X"], st["Y"], st["Z"], st["F"]
    if X is None or Y is None:
        raise ResumeUnavailable("Не нашёл позицию сопла до среза")
    ox = oy = None
    oz = safe_z
    f_out = LIFT_F
    desync = True
    kept_before_first = 0.0
    first_ext = False
    first_object: int | None = None
    mismatch = 0
    span = max(1, end_marker - cut)

    def emit(s: str) -> None:
        nonlocal f_out, oz
        out_lines.append(s)
        d = _parse(s)
        if d:
            if "F" in d:
                f_out = d["F"]
            if "Z" in d:
                oz = d["Z"]

    def ensure_f(d: dict) -> None:
        if "F" not in d and f_orig is not None and f_out != f_orig:
            emit(f"G1 F{_fmt(f_orig)} ; resync F")

    last_lbl_obj: int | None = None
    for i in range(cut, end_marker):
        if (i - cut) % 200000 == 0:
            progress("build", (i - cut) / span)
        ln = lines[i]
        d = _parse(ln)
        if d is None:
            # метки вырезанной детали (`; printing object …`, EXCLUDE_OBJECT_*, M486) уходят вместе с
            # ней: файл не должен объявлять объект, которого в нём больше нет
            if assign.labels is not None and ln and ln[0] in ";EM":
                k = assign.labels[i]
                if k >= 0:
                    last_lbl_obj = assign.by_key.get(k)
                is_start = ln.startswith(("; printing object", "EXCLUDE_OBJECT_START")) or _M486_RE.match(ln)
                is_stop = ln.startswith(("; stop printing object", "EXCLUDE_OBJECT_END")) or ln.startswith("M486 S-1")
                if (is_start and last_lbl_obj in excl) or (is_stop and last_lbl_obj in excl):
                    stats["dropped"] += 1
                    continue
            out_lines.append(ln)
            continue
        has_xy = "X" in d or "Y" in d
        nx = d.get("X", X)
        ny = d.get("Y", Y)
        nz = d.get("Z", Z)
        e = d.get("E", 0.0)
        if has_xy and nx is not None and ny is not None:
            reg = assign(nx, ny, i)
        elif X is not None and Y is not None:
            reg = assign(X, Y, i)
        else:
            reg = None
        sx, sy, sz = X, Y, Z
        X, Y, Z = nx, ny, nz
        if "F" in d:
            f_orig = d["F"]
        if reg in excl:
            stats["dropped"] += 1
            if e > 0 and has_xy:
                dropped_e += e
            desync = True
            continue
        if e > 0 and has_xy:
            kept_e += e
            if not first_ext:
                first_ext = True
        elif not first_ext:
            kept_before_first += e
        if first_object is None and has_xy and reg is not None:
            first_object = reg
        if desync and has_xy:
            target_z = d["Z"] if "Z" in d else sz
            if e > 0:
                if sx is not None and (ox is None or abs(ox - sx) > 1e-6 or abs(oy - sy) > 1e-6):
                    mismatch += 1
                    if target_z is not None and oz < target_z:
                        emit(f"G1 Z{_fmt(target_z)} F{LIFT_F} ; resync")
                    emit(f"G1 X{_fmt(sx)} Y{_fmt(sy)} F{TRAVEL_F} ; resync")
                    ox, oy = sx, sy
                if target_z is not None and abs(oz - target_z) > 1e-6:
                    emit(f"G1 Z{_fmt(target_z)} F{LIFT_F} ; resync")
                ensure_f(d)
                emit(ln)
            else:
                if target_z is not None and oz < target_z - 1e-6:
                    emit(f"G1 Z{_fmt(target_z)} F{LIFT_F} ; resync")
                    ensure_f(d)
                    emit(ln)
                elif target_z is not None and oz > target_z + 1e-6:
                    ensure_f(d)
                    emit(re.sub(r"\s*Z-?\d*\.?\d+", "", ln))
                    emit(f"G1 Z{_fmt(target_z)} F{LIFT_F} ; resync")
                    if "F" not in d and f_orig is not None:
                        emit(f"G1 F{_fmt(f_orig)} ; resync F")
                else:
                    ensure_f(d)
                    emit(ln)
            ox, oy = nx, ny
            desync = False
            stats["resync"] += 1
            continue
        emit(ln)
        if has_xy:
            ox, oy = nx, ny

    retract_fix = -kept_before_first
    start += [
        f"G1 E{_fmt(retract_fix)} F1800 ; match retract state expected at layer start",
        f"G1 Z{_fmt(safe_z)} F{LIFT_F} ; clear the parts before travelling onto the plate",
        f"M104 S{_fmt(nozzle)}",
    ]
    for key in ("fan", "fan_p2", "fan_p3", "pa", "vel"):
        if st.get(key):
            start.append(st[key])
    start += [f";LAYER_COUNT:{doc.last_layer}", ";LAYER:0", ";===== /RESUME START ====="]

    # пост-проверка тела: ни одной экструзии в вырезанных деталях
    progress("check", 0.0)
    X = Y = None
    bad = 0
    for k, ln in enumerate(out_lines):
        d = _parse(ln)
        if d is None:
            continue
        nx = d.get("X", X)
        ny = d.get("Y", Y)
        if d.get("E", 0.0) > 0 and ("X" in d or "Y" in d) and nx is not None and ny is not None \
                and assign(nx, ny, None if assign.labels is None else None) in excl \
                and assign.labels is None:
            bad += 1
        X, Y = nx, ny
    if bad:
        raise ResumeUnavailable(f"Самопроверка не прошла: {bad} ходов остались в вырезанных деталях")

    # стартовые переезды: точки от хоминга до первой экструзии слоя N — для 3D и проверки коллизий
    moves: list[list[float]] = []
    px, py, pz = bed[0] / 2, bed[1] / 2, max(HOME_LIFT_Z, safe_z)
    moves.append([px, py, pz])
    for s in start[start.index("G28") + 1:]:
        d = _parse(s)
        if not d or not ("X" in d or "Y" in d or "Z" in d):
            continue
        px, py, pz = d.get("X", px), d.get("Y", py), d.get("Z", pz)
        moves.append([px, py, pz])
    seen_ext = False
    for s in out_lines[:400]:
        d = _parse(s)
        if not d:
            continue
        if d.get("E", 0.0) > 0 and ("X" in d or "Y" in d):
            seen_ext = True
            break
        if "X" in d or "Y" in d or "Z" in d:
            px, py, pz = d.get("X", px), d.get("Y", py), d.get("Z", pz)
            moves.append([px, py, pz])
    if not seen_ext:
        moves = moves[:1]
    start_check = _check_start_moves(moves, objects, excl, prev)

    result = lines[:doc.exec_start] + start + out_lines + lines[end_marker:]
    outp = Path(out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text("\n".join(result), encoding="utf-8", newline="\n")
    centre = (bed[0] / 2, bed[1] / 2)
    centre_object = ""
    for o in objects:
        if o.id not in excl and o.x0 - 2 <= centre[0] <= o.x1 + 2 and o.y0 - 2 <= centre[1] <= o.y1 + 2:
            centre_object = o.name or f"#{o.id}"
            warnings.append(f"Деталь «{centre_object}» стоит на точке хоминга Z (центр стола). При высоте "
                            f"{_fmt(prev)} мм сопло при G28 может её коснуться; 03.09.2026 деталь "
                            "4.16 мм в центре не помешала.")
    if not start_check["ok"]:
        warnings.append("Стартовый переезд проходит над деталью ниже её макушки — старт запрещён "
                        "(см. start_check)")
    if style == "klipper":
        warnings.append("Стартовый блок Klipper собран по общему образцу и на железе не проверен")
    first_name = by_id[first_object].name or f"#{first_object}" if first_object in by_id else ""
    progress("done", 1.0)
    return BuildReport(out=outp, layer=layer, z_layer=z_layer, parts_height=prev, excluded=exclude,
                       excluded_names=excl_names, kept_objects=len(objects) - len(exclude),
                       dropped_lines=stats["dropped"], resyncs=stats["resync"], mismatch_xy=mismatch,
                       dropped_e_mm=dropped_e, kept_e_mm=kept_e, retract_fix=retract_fix,
                       minutes_remaining=remaining, first_object=first_object,
                       first_object_name=first_name, nozzle=nozzle, bed=bed_t,
                       size_bytes=outp.stat().st_size, warnings=warnings,
                       last_layer=doc.last_layer, style=style, mode=assign.mode,
                       start_moves=[[round(v, 3) for v in m] for m in moves],
                       start_check=start_check, centre_object=centre_object)


# ---------------------------------------------------------------- сверка

def _walk(doc: GcodeDoc, from_layer: int, assign: Membership | None, skip: set[int]):
    lines = doc.lines
    X = Y = Z = F = None
    layer = None
    layer_z = None
    ext = []
    z_viol = 0
    dive = 0
    in_body = False
    min_travel = None
    for i in range(doc.exec_start, doc.exec_end):
        ln = lines[i]
        if ln.startswith(";LAYER:"):
            try:
                layer = int(ln[7:])
            except ValueError:
                continue
            in_body = layer >= from_layer
            continue
        if ln.startswith(";Z:"):
            try:
                layer_z = float(ln[3:])
            except ValueError:
                pass
            continue
        if _END_RE.match(ln):
            break
        d = _parse(ln)
        if d is None:
            continue
        nx = d.get("X", X)
        ny = d.get("Y", Y)
        nz = d.get("Z", Z)
        F = d.get("F", F)
        e = d.get("E", 0.0)
        has_xy = "X" in d or "Y" in d
        if in_body and has_xy and e > 0 and nx is not None and ny is not None:
            if assign is not None and assign(nx, ny, i) in skip:
                X, Y, Z = nx, ny, nz
                continue
            if layer_z is None or nz is None or abs(nz - layer_z) > 1e-6:
                z_viol += 1
            if min_travel is not None and layer_z is not None and min_travel < layer_z - 1e-6:
                dive += 1
            min_travel = None
            ext.append((layer, round(X or 0, 3), round(Y or 0, 3), round(nx, 3), round(ny, 3),
                        round(e, 5), F, nz))
        elif in_body and has_xy and nz is not None:
            min_travel = nz if min_travel is None else min(min_travel, nz)
        X, Y, Z = nx, ny, nz
    return ext, z_viol, dive


def verify(src: str | Path, out: str | Path, layer: int, exclude: list[int],
           progress: Progress = _noop) -> dict:
    """Независимая сверка: экструзии выходного файла = экструзии исходника без вырезанных
    деталей (старт, конец, E, F, Z побуквенно), Z каждой экструзии = Z слоя. «Ныряния»
    переездов сравниваются с исходником: там одно на слой (wipe на старой высоте после метки
    нового слоя) — это не дефект."""
    progress("verify", 0.0)
    doc = load(src)
    objects = detect_objects(doc)
    assign, _cross = membership(doc, objects)
    ref, zv0, dv0 = _walk(doc, layer, assign, set(exclude))
    del doc
    progress("verify", 0.5)
    got, zv1, dv1 = _walk(load(out), layer, None, set())
    n = min(len(ref), len(got))
    bad = 0
    first_bad = None
    for i in range(n):
        if ref[i] != got[i]:
            bad += 1
            if first_bad is None:
                first_bad = {"index": i, "ref": list(ref[i]), "out": list(got[i])}
    ok = len(ref) == len(got) and bad == 0 and zv1 == 0 and dv1 <= dv0
    progress("verify", 1.0)
    return {"ok": ok, "ref_count": len(ref), "out_count": len(got), "differ": bad,
            "first_bad": first_bad, "z_violations": zv1, "dives_ref": dv0, "dives_out": dv1}


# ---------------------------------------------------------------- превью и траектории

def _layer_segments(lines: list[str], lo: int, hi: int) -> list[tuple[float, float, float, float]]:
    return [(x0, y0, x1, y1) for x0, y0, x1, y1, _e, _i in _segments(lines, lo, hi)]


def previews(src: str | Path, out: str | Path, layers: list[int], *, scale: int = 2) -> list[dict]:
    """PNG слоёв: чёрное — печатается (из выходного файла), красное — вырезано (есть в исходнике,
    нет в выходе). Без Pillow — пустой список, а не ошибка: превью справка, не условие."""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return []
    a = load(src)
    b = load(out)
    bx, by = bed_size(a)
    W, H = int(bx * scale), int(by * scale)
    res = []
    for L in layers:
        if L not in a.layers or L not in b.layers:
            continue
        la, lb = a.layers[L], b.layers[L]
        ha = a.layers[L + 1].change_idx if L + 1 in a.layers else a.end_marker(la.idx)
        hb = b.layers[L + 1].change_idx if L + 1 in b.layers else b.end_marker(lb.idx)
        sa = _layer_segments(a.lines, la.idx, ha)
        sb = _layer_segments(b.lines, lb.idx, hb)
        img = Image.new("RGB", (W + 2, H + 2), "white")
        dr = ImageDraw.Draw(img)
        dr.rectangle([0, 0, W + 1, H + 1], outline="#bbb")

        def P(x, y):
            return (1 + x * scale, 1 + (by - y) * scale)

        for x0, y0, x1, y1 in sa:
            dr.line([P(x0, y0), P(x1, y1)], fill="#e33", width=1)
        for x0, y0, x1, y1 in sb:
            dr.line([P(x0, y0), P(x1, y1)], fill="#111", width=1)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        res.append({"layer": L, "z": la.z, "kept": len(sb), "dropped": len(sa) - len(sb),
                    "png": base64.b64encode(buf.getvalue()).decode("ascii")})
    return res


def preview_layers(layer: int, last: int) -> list[int]:
    """Какие слои показать: первый допечатываемый, середина остатка и последний."""
    mid = layer + max(1, (last - layer) // 2)
    return sorted({layer, min(mid, last), last})


TOOLPATH_MAGIC = b"PSTP"
TOOLPATH_VERSION = 1


def toolpaths(src: str | Path, *, max_segments: int = 1_200_000, progress: Progress = _noop) -> bytes:
    """Бинарные траектории для 3D-вьюера деска.

    Формат (little-endian): 'PSTP', u16 версия, u32 длина JSON-шапки, JSON (стол, слои с Z, детали,
    типы), затем N отрезков: f32 x0,y0,x1,y1,z (5×4), u16 слой, u8 деталь, u8 тип. Отрезки идут в
    порядке файла — слои по возрастанию, так что «до слоя N» = префикс массива.

    Децимация: подряд идущие ходы одной детали, одного типа и одного направления (угол < 2°)
    склеиваются; если отрезков всё ещё больше `max_segments`, каждый k-й пропускается для типов
    заполнения — стенки остаются целиком, они и задают силуэт."""
    progress("paths", 0.0)
    doc = load(src)
    objects = detect_objects(doc)
    assign, _cross = membership(doc, objects)
    lines = doc.lines
    start = doc.layer(doc.first_layer).change_idx
    end = doc.end_marker(start)
    layer_no = 0
    z = 0.0
    tcode = 9
    X = Y = None
    segs: list[tuple[float, float, float, float, float, int, int, int]] = []
    last_dir = None
    span = max(1, end - start)
    for i in range(start, end):
        if (i - start) % 300000 == 0:
            progress("paths", (i - start) / span * 0.8)
        ln = lines[i]
        if ln.startswith(";"):
            if ln.startswith(";LAYER:"):
                try:
                    layer_no = int(ln[7:])
                except ValueError:
                    pass
            elif ln.startswith(";TYPE:"):
                tcode = TYPE_CODES.get(ln[6:].strip().lower(), 9)
            continue
        d = _parse(ln)
        if d is None:
            continue
        nx = d.get("X", X)
        ny = d.get("Y", Y)
        if "Z" in d:
            z = d["Z"]
        if (d.get("E", 0.0) > 0 and ("X" in d or "Y" in d) and X is not None and Y is not None
                and nx is not None and ny is not None):
            oid = assign(nx, ny, i)
            pts: list[tuple[float, float]]
            if d["_arc"] and ("I" in d or "J" in d):
                pts = _arc_points(X, Y, nx, ny, d.get("I", 0.0), d.get("J", 0.0), d["_arc"] == 2)
            else:
                pts = [(nx, ny)]
            px, py = X, Y
            for qx, qy in pts:
                dx, dy = qx - px, qy - py
                ln2 = math.hypot(dx, dy)
                if ln2 < 1e-6:
                    continue
                cur_dir = (dx / ln2, dy / ln2)
                if (segs and last_dir is not None and segs[-1][5] == layer_no and segs[-1][6] == oid
                        and segs[-1][7] == tcode and abs(segs[-1][2] - px) < 1e-6
                        and abs(segs[-1][3] - py) < 1e-6
                        and cur_dir[0] * last_dir[0] + cur_dir[1] * last_dir[1] > 0.9994):
                    s = segs[-1]
                    segs[-1] = (s[0], s[1], qx, qy, s[4], s[5], s[6], s[7])
                else:
                    segs.append((px, py, qx, qy, z, layer_no, oid, tcode))
                last_dir = cur_dir
                px, py = qx, qy
        else:
            last_dir = None
        X, Y = nx, ny
    if len(segs) > max_segments:
        k = math.ceil(len(segs) / max_segments)
        kept = []
        n = 0
        for s in segs:
            if s[7] in (1, 2, 5, 7) or n % k == 0:
                kept.append(s)
            n += 1
        segs = kept
    progress("paths", 0.9)
    layer_z = {n: (lm.z or 0.0) for n, lm in doc.layers.items() if n >= 1}
    header = {"version": TOOLPATH_VERSION, "bed": list(bed_size(doc)), "layers": doc.last_layer,
              "first_layer": doc.first_layer,
              "layer_z": [layer_z.get(n) for n in range(doc.last_layer + 1)],
              "objects": [o.as_dict() for o in objects], "types": TYPE_NAMES, "count": len(segs),
              "mode": assign.mode}
    hj = json.dumps(header, ensure_ascii=False).encode("utf-8")
    buf = bytearray()
    buf += TOOLPATH_MAGIC + struct.pack("<HI", TOOLPATH_VERSION, len(hj)) + hj
    pack = struct.Struct("<fffffHBB").pack
    for s in segs:
        buf += pack(s[0], s[1], s[2], s[3], s[4], min(65535, s[5]), min(255, s[6]), s[7])
    progress("paths", 1.0)
    return bytes(buf)
