"""Память-безопасный объём тела .3mf потоковым парсингом (без загрузки всех мешей разом).

ЗАЧЕМ. Для аналитической оценки цены (docs/…/slicing-speedup-analysis.md, Рычаг Б) нужен
СПЛОШНОЙ геометрический объём модели V_solid (см³). Обычно его даёт trimesh (`mesh.volume`),
но у тяжёлых AMS-проектов вроде «коала.3mf» (11 филаментов, 34 объекта, ~573 МБ меш-XML во
внешних sub-model'ах production-extension) trimesh грузит все меши разом и уходит в OOM
(пик ~49 ГБ — валит машину). Здесь мы НИКОГДА не держим все меши: ядро 3dmodel.model крошечное,
а каждый sub-model (и каждый <object> внутри него) обрабатываем ПО ОДНОМУ через streaming XML
(iterparse), считая объём знаковой суммой тетраэдров и тут же очищая элементы. Пик памяти ~
крупнейший ОДИН меш (у коалы ~0.5 М вершин ⇒ ~12 МБ в array('d')).

Точность. Для водонепроницаемого меша знаковая сумма тетраэдров даёт ТОЧНЫЙ объём тела —
сверено с trimesh на объектах коалы «до знака» (0.6893 см³ == 0.6893 см³). Масштаб в трансформах
компонентов/build-item МЕНЯЕТ объём, поэтому домножаем на |det| линейной части каждой трансформы.

Считаем только РАЗМЕЩЁННУЮ геометрию (та, что реально в <build> на плитах). НЕ вычитаем
пересечения и не делаем булевых операций — это грубая солид-оценка (верхняя граница «как если бы
100 % залито»), из которой аналитика выводит филамент через коэффициент заполнения f (см. док).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from array import array
from pathlib import Path

log = logging.getLogger(__name__)

_CORE = "3D/3dmodel.model"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _linear_det(transform: str | None) -> float:
    """|det| линейной части трансформы 3MF (12 чисел: 3×3 + сдвиг). 1.0 — нет/битая трансформа.

    Объём масштабируется на |det(3×3)| и не зависит от транспонирования (det(A)==det(Aᵀ)),
    поэтому берём первые 9 чисел как матрицу в любом согласованном порядке."""
    if not transform:
        return 1.0
    try:
        n = [float(x) for x in transform.split()]
    except ValueError:
        return 1.0
    if len(n) < 9:
        return 1.0
    a, b, c, d, e, f, g, h, i = n[:9]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return abs(det)


def _stream_object_volumes(z: zipfile.ZipFile, entry: str, wanted: set[str]) -> dict[str, float]:
    """{object_id -> объём тела, мм³} для нужных <object id> из одного .model, потоково.

    Держим массив вершин ТОЛЬКО для интересующего объекта; треугольники не храним — сразу
    аккумулируем знаковый объём и очищаем элемент. Так пик памяти ~ один крупнейший меш."""
    out: dict[str, float] = {}
    cur: str | None = None
    keep = False
    verts = array("d")
    vol = 0.0
    try:
        with z.open(entry) as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):
                ln = _localname(el.tag)
                if event == "start":
                    if ln == "object":
                        cur = el.get("id")
                        keep = cur in wanted
                        verts = array("d")
                        vol = 0.0
                    continue
                # end events
                if ln == "vertex":
                    if keep:
                        verts.append(float(el.get("x", "0")))
                        verts.append(float(el.get("y", "0")))
                        verts.append(float(el.get("z", "0")))
                    el.clear()
                elif ln == "triangle":
                    if keep:
                        a = int(el.get("v1")); b = int(el.get("v2")); c = int(el.get("v3"))
                        if 3 * a + 2 < len(verts) and 3 * b + 2 < len(verts) and 3 * c + 2 < len(verts):
                            ax, ay, az = verts[3 * a], verts[3 * a + 1], verts[3 * a + 2]
                            bx, by, bz = verts[3 * b], verts[3 * b + 1], verts[3 * b + 2]
                            cx, cy, cz = verts[3 * c], verts[3 * c + 1], verts[3 * c + 2]
                            vol += (ax * (by * cz - bz * cy)
                                    - ay * (bx * cz - bz * cx)
                                    + az * (bx * cy - by * cx))
                    el.clear()
                elif ln == "object":
                    if keep and cur is not None:
                        out[cur] = abs(vol) / 6.0
                    verts = array("d")
                    keep = False
                    el.clear()
    except (KeyError, ET.ParseError, OSError):
        log.debug("stream volumes failed for %s in %s", entry, getattr(z, "filename", "?"), exc_info=True)
    return out


def _iter_instances(core: str):
    """Из ядра 3dmodel.model выдаёт (submodel_entry|None, sub_object_id, comp_det, item_det) для
    каждой реально РАЗМЕЩЁННОЙ на плите единицы геометрии.

    Верхний <object id=T> либо ссылается на компоненты во внешних sub-model (production-extension:
    <component p:path=… objectid=… transform=…>), либо несёт <mesh> прямо в ядре (обычный .3mf).
    Домножаем на трансформу build-item верхнего объекта."""
    # build items: objectid -> item transform (тег как самозакрывающийся <item .../>, так и <item ...>)
    item_det: dict[str, float] = {}
    for m in re.finditer(r"<item\b[^>]*>", core):
        tag = m.group(0)
        oid = re.search(r'objectid="([^"]+)"', tag)
        if oid:
            tr = re.search(r'transform="([^"]+)"', tag)
            item_det[oid.group(1)] = _linear_det(tr.group(1) if tr else None)
    placed = set(item_det)
    # top objects
    for m in re.finditer(r'<object\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</object>', core, re.S):
        tid, body = m.group(1), m.group(2)
        if tid not in placed:
            continue  # объект-ресурс не на плите (альтернатива/заготовка) — не печатается
        idet = item_det.get(tid, 1.0)
        comps = re.findall(r"<component\b[^>]*>", body)
        if comps:
            for tag in comps:
                p = re.search(r'p:path="([^"]+)"', tag)
                oid = re.search(r'objectid="([^"]+)"', tag)
                tr = re.search(r'transform="([^"]+)"', tag)
                yield (p.group(1).lstrip("/") if p else None,
                       oid.group(1) if oid else None,
                       _linear_det(tr.group(1) if tr else None), idet)
        elif "<mesh" in body:
            # inline-меш в ядре (обычный .3mf без production-extension)
            yield (_CORE, tid, 1.0, idet)


def solid_volume_cm3(model_path: str | Path) -> float | None:
    """Сплошной геометрический объём (см³) всей размещённой геометрии .3mf, память-безопасно.

    Возвращает None, если это не разбираемый .3mf (пусть вызывающий откатится на trimesh/слайс).
    Безопасно для тяжёлых AMS-проектов, которые валят trimesh по памяти (см. модуль)."""
    p = Path(model_path)
    if p.suffix.lower() != ".3mf" or not p.exists():
        return None
    try:
        with zipfile.ZipFile(str(p)) as z:
            names = set(z.namelist())
            if _CORE not in names:
                return None
            core = z.read(_CORE).decode("utf-8", "replace")
            instances = list(_iter_instances(core))
            if not instances:
                return None
            # какие object_id нужны из каждого файла
            wanted: dict[str, set[str]] = {}
            for entry, oid, _c, _i in instances:
                if entry and oid:
                    wanted.setdefault(entry, set()).add(oid)
            # объём каждого меша считаем ОДИН раз (кэш по файлу)
            raw: dict[tuple[str, str], float] = {}
            for entry, ids in wanted.items():
                if entry not in names:
                    continue
                for oid, v in _stream_object_volumes(z, entry, ids).items():
                    raw[(entry, oid)] = v
            total_mm3 = 0.0
            for entry, oid, cdet, idet in instances:
                if not entry or not oid:
                    continue
                total_mm3 += raw.get((entry, oid), 0.0) * cdet * idet
    except (zipfile.BadZipFile, OSError, KeyError):
        log.debug("solid_volume_cm3 failed for %s", p, exc_info=True)
        return None
    if total_mm3 <= 0:
        return None
    return total_mm3 / 1000.0
