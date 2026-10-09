"""Анализ .3mf: сколько столов (плит) и многоцветная ли печать.

Bambu/Orca .3mf — это zip. Сигналы:
- Metadata/model_settings.config (XML): <plate ...> на каждый стол;
  <metadata key="extruder" value="N"/> у деталей — назначенный экструдер.
  Несколько разных значений (или значение ≥2) = многоцветная печать по деталям.
- 3D/3dmodel.model (XML): атрибуты paint_color / mmu_segmentation у треугольников —
  покраска граней (многоцвет по граням).
- Metadata/project_settings.config (JSON): filament_colour[] — настроенные филаменты
  (используем только как оценку числа цветов для крашеных моделей).

Любая ошибка → безопасный дефолт (1 стол, 1 цвет): фича опциональна, не должна ронять расчёт.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_EXTRUDER_RE = re.compile(r'key="extruder"\s+value="(\d+)"')
_PLATE_RE = re.compile(r"<plate\b")
# Только покраска ЦВЕТОМ. НЕ paint_seam / paint_fuzzy_skin / paint_supports — это шов,
# текстура и поддержки, к многоцвету отношения не имеют. mmu_segmentation — формат
# Prusa/старого Bambu (в т.ч. с префиксом slic3rpe: — суффикс совпадает).
_PAINT_MARKERS = (b'paint_color="', b'mmu_segmentation="')
_SCAN_CAP = 60 * 1024 * 1024  # не сканируем .model больше 60 МБ в поисках покраски


def _norm_hex(c: str) -> str:
    """'#RRGGBBAA' / 'RRGGBB' / '#RGB' → '#rrggbb'; не hex → '' (Bambu пишет с альфой)."""
    s = (c or "").strip().lstrip("#")
    if len(s) == 8:
        s = s[:6]
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return ""
    try:
        int(s, 16)
    except ValueError:
        return ""
    return "#" + s.lower()


@dataclass
class ModelInfo:
    plates: int = 1
    extruders: set = field(default_factory=set)
    painted: bool = False
    filament_colours: list = field(default_factory=list)
    # Цвета реально СМЕШИВАЮТСЯ хотя бы на одном столе (покраска граней или ≥2 экструдеров
    # на столе). Многоцвет «по столам» (каждый стол одним цветом, как Timmy No-AMS) — False:
    # продувки/смен цвета в печати нет → надбавка за мультицвет не берётся (OpenTodo 0bcdd728).
    mixed_plates: bool = False

    @property
    def distinct_extruders(self) -> set:
        return {c for c in self.extruders if c}

    @property
    def num_colors(self) -> int:
        """Оценка числа цветов (для пояснения и масштабирования наценки)."""
        n = len(self.distinct_extruders)
        if n >= 2:
            return n
        if self.painted:
            return 2  # покраску по цветам точно не считаем — берём минимум
        return 1

    @property
    def is_multicolor(self) -> bool:
        # многоцвет = либо ≥2 РАЗНЫХ экструдера у деталей, либо покраска граней цветом.
        # Один экструдер (любой, хоть №3) = один филамент = одноцвет.
        return self.painted or len(self.distinct_extruders) >= 2

    @property
    def used_colours(self) -> list:
        """Hex-цвета ('#rrggbb') ДИЗАЙНА в порядке слотов печати — чтобы мини-апп предвыбрал
        ближайшие цвета своей палитры (заказчик видит модель «в своих» цветах сразу).
        Экструдеры размечены → цвета именно ЗАНЯТЫХ экструдеров (номер = индекс палитры
        filament_colour, 1-базный). Крашеная модель без разметки мешает неизвестное
        подмножество палитры → отдаём палитру целиком (обычно это точный набор проекта).
        Одноцветная без разметки печатает первым филаментом. Нет цветов в .3mf → []."""
        # нормализуем БЕЗ фильтрации: индекс в pal должен совпадать с номером экструдера−1,
        # битую запись заменяет '' (иначе позиции палитры съехали бы)
        pal = [_norm_hex(x) for x in self.filament_colours]
        if not any(pal):
            return []
        used = [e for e in sorted(self.distinct_extruders) if 0 < e <= len(pal)]
        if used:
            return [pal[e - 1] for e in used if pal[e - 1]]
        non_empty = [c for c in pal if c]
        return non_empty[:8] if self.painted else non_empty[:1]

    @property
    def is_multiplate(self) -> bool:
        return self.plates >= 2


def _scan_painted(z: zipfile.ZipFile, name: str) -> bool:
    """Потоково ищем маркеры покраски в .model (большой файл — читаем чанками, рано выходим)."""
    try:
        with z.open(name) as f:
            remaining = _SCAN_CAP
            tail = b""
            while remaining > 0:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                remaining -= len(chunk)
                buf = tail + chunk
                if any(m in buf for m in _PAINT_MARKERS):
                    return True
                tail = chunk[-32:]  # перекрытие на стыке чанков
    except Exception:  # noqa: BLE001
        log.debug("paint scan failed for %s", name, exc_info=True)
    return False


def _inspect_one(path: Path) -> ModelInfo:
    info = ModelInfo()
    try:
        with zipfile.ZipFile(str(path)) as z:
            names = z.namelist()
            model_entries: list[str] = []
            for n in names:
                ln = n.lower()
                if ln.endswith("model_settings.config") or ln.endswith("_model.config"):
                    xml = z.read(n).decode("utf-8", "ignore")
                    for m in _EXTRUDER_RE.finditer(xml):
                        try:
                            info.extruders.add(int(m.group(1)))
                        except ValueError:
                            pass
                    pc = len(_PLATE_RE.findall(xml))
                    if pc > info.plates:
                        info.plates = pc
                elif ln.endswith("project_settings.config"):
                    try:
                        d = json.loads(z.read(n).decode("utf-8", "ignore"))
                        fc = d.get("filament_colour") or d.get("filament_color") or []
                        if isinstance(fc, list):
                            info.filament_colours = [str(x) for x in fc]
                    except Exception:  # noqa: BLE001
                        pass
                elif ln.endswith(".model"):
                    model_entries.append(n)
            # покраска граней — сканируем только если не нашли многоцвет по экструдерам
            if not info.is_multicolor:
                for n in model_entries:
                    if _scan_painted(z, n):
                        info.painted = True
                        break
    except Exception:  # noqa: BLE001
        log.debug("inspect_3mf failed for %s", path, exc_info=True)
    # смешение на столе: покраска — всегда смешение; ≥2 экструдеров — смотрим по столам
    # (plate_multicolor); данных по столам нет (один стол/нестандартная разметка) —
    # консервативно считаем смешением (надбавка лучше ложного нуля)
    if info.painted:
        info.mixed_plates = True
    elif len(info.distinct_extruders) >= 2:
        pm = plate_multicolor(path)
        info.mixed_plates = any(pm) if pm else True
    return info


# Детали, которые слайсер НЕ печатает материалом, но которые НЕ вычитаются из тела:
#   modifier_part    — область с другими настройками внутри обычной детали (её стенки/инфилл
#                      меняются, но сама она в g-code не идёт);
#   support_enforcer — зона принудительных поддержек;
#   support_blocker  — зона запрета поддержек.
# Геометрия у них настоящая, поэтому trimesh/fastmeasure грузят их как обычные тела и
# завышают объём (iPhone-чехол с «iPhone17e_修改器.STL»: 23 см³ тела → 57 см³, +142%).
# Отличие от negative_part: те ещё и РЕЖУТ тело — их геометрия нужна для вычитания.
NONPRINTING_SUBTYPES = ("modifier_part", "support_enforcer", "support_blocker")


def _part_ids_by_subtype(path, subtypes: tuple) -> set:
    """ID деталей заданных subtype из model_settings.config одного .3mf.

    Bambu/Orca помечают роль детали ТОЛЬКО в model_settings.config; в обычном 3dmodel.model
    этого флага нет, поэтому trimesh грузит такую деталь как обычное тело. Возвращаем set
    строковых id, чтобы геометрия их отбросила. ID детали в Bambu совпадает с id объекта
    в .model (и ключом геометрии trimesh)."""
    ids: set = set()
    wanted = tuple(f'subtype="{s}"' for s in subtypes)
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return ids
        with zipfile.ZipFile(str(p)) as z:
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
        log.debug("part subtype scan failed for %s", path, exc_info=True)
    return ids


def negative_part_ids(path) -> set:
    """ID «вычитающих» деталей (subtype="negative_part").

    Вырез, загруженный как обычное тело, ДОБАВЛЯЕТСЯ вместо вычитания — расход/превью
    завышаются. Эти id геометрия не просто отбрасывает, а режет ими тело (_subtract_negatives),
    поэтому список держим отдельно от непечатаемых (см. nonprinting_part_ids)."""
    return _part_ids_by_subtype(path, ("negative_part",))


def nonprinting_part_ids(path) -> set:
    """ID деталей, которые не печатаются материалом и НЕ вычитаются (NONPRINTING_SUBTYPES).

    Их надо просто выкинуть из геометрии: вычитать нельзя — модификатор обычно лежит ВНУТРИ
    обычной детали, и вычитание выело бы из неё настоящий материал."""
    return _part_ids_by_subtype(path, NONPRINTING_SUBTYPES)


def _read_model_settings(z: zipfile.ZipFile) -> str:
    """Текст model_settings.config из открытого .3mf (или "" если его нет)."""
    for n in z.namelist():
        ln = n.lower()
        if ln.endswith("model_settings.config") or ln.endswith("_model.config"):
            return z.read(n).decode("utf-8", "ignore")
    return ""


def declared_face_total(path) -> int:
    """Сколько граней заявлено в УНИКАЛЬНЫХ объектах модели — Σ face_count по <object> из
    model_settings.config (БЕЗ умножения на размещения: сверяется с уникальными геометриями
    trimesh-сцены, у которых копий-инстансов тоже нет).

    Нужно как дешёвая валидация загрузки (OpenTodo 041be6d4): у некоторых production-3mf
    (компонентные сборки с p:path) trimesh кладёт в каждую геометрию слитый меш всего файла —
    сцена раздувается кратно (springy kitty: ×4 по уникальным). 0 — посчитать нельзя."""
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return 0
        with zipfile.ZipFile(str(p)) as z:
            xml = _read_model_settings(z)
        if not xml:
            return 0
        total = 0
        for om in re.finditer(r'<object\b[^>]*\bid="([^"]+)"', xml):
            end = xml.find("</object>", om.start())
            block = xml[om.start():end if end > 0 else len(xml)]
            fm = re.search(r'<metadata\s+face_count="(\d+)"', block)
            if fm:
                total += int(fm.group(1))
        return total
    except Exception:  # noqa: BLE001
        log.debug("declared_face_total failed for %s", path, exc_info=True)
        return 0


def has_project_settings(path) -> bool:
    """Файл — ПРОЕКТ слайсера (Bambu/Orca/Elegoo/MakerWorld production) с полными родными
    настройками печати (Metadata/project_settings.config): слои, заполнение, поддержки,
    скорости. Такой файл можно слайсить «как есть» (OpenTodo 463c5e9e)."""
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return False
        with zipfile.ZipFile(str(p)) as z:
            return "Metadata/project_settings.config" in z.namelist()
    except Exception:  # noqa: BLE001
        log.debug("has_project_settings failed for %s", path, exc_info=True)
        return False


def has_production_components(path) -> bool:
    """Есть ли в ядре 3D/3dmodel.model компоненты из внешних sub-model (p:path="/3D/Objects/…") —
    признак production-3mf (Bambu/Orca/Elegoo), сборку которого trimesh может продублировать.
    Дёшево: ядро маленькое, тяжёлые меши лежат в sub-model файлах."""
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return False
        with zipfile.ZipFile(str(p)) as z:
            core = z.read("3D/3dmodel.model").decode("utf-8", "ignore")
        return bool(re.search(r'<component\b[^>]*\bp:path="', core))
    except Exception:  # noqa: BLE001
        log.debug("has_production_components failed for %s", path, exc_info=True)
        return False


def _object_to_part_ids(xml: str) -> dict:
    """{object_id -> [part_id, ...]} из model_settings.config.

    В Bambu/Orca <plate> ссылается на ОБЪЕКТ (object_id), а геометрия trimesh/вычитающие
    детали адресуются по ВНУТРЕННЕЙ детали (<part id=...>). Эта карта связывает одно с другим."""
    out: dict = {}
    for om in re.finditer(r'<object\b[^>]*\bid="([^"]+)"', xml):
        oid = om.group(1)
        end = xml.find("</object>", om.start())
        block = xml[om.start():end if end > 0 else len(xml)]
        out[oid] = re.findall(r'<part\b[^>]*\bid="([^"]+)"', block)
    return out


def plate_part_ids(path) -> list:
    """Список (в порядке столов) множеств part-id, лежащих на каждом столе .3mf.

    Возвращает [] если столов нет / один / файл не .3mf — вызывающий тогда работает по-старому.
    Каждый <plate> содержит <model_instance> с object_id; разворачиваем object_id в его part-id
    через _object_to_part_ids — так получаем именно те ключи, по которым адресуется геометрия.
    Порядок сохраняем — пользователь хочет превью «Стол 1, Стол 2, …» в порядке столов."""
    out: list = []
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return out
        with zipfile.ZipFile(str(p)) as z:
            xml = _read_model_settings(z)
        if not xml:
            return out
        obj2part = _object_to_part_ids(xml)
        for pm in re.finditer(r"<plate\b", xml):
            end = xml.find("</plate>", pm.start())
            block = xml[pm.start():end if end > 0 else len(xml)]
            parts: set = set()
            for im in re.finditer(r'<model_instance\b.*?</model_instance>', block, re.DOTALL):
                oidm = re.search(r'key="object_id"\s+value="([^"]+)"', im.group(0))
                if oidm:
                    parts.update(obj2part.get(oidm.group(1), []))
            out.append(parts)
        # Один (или ноль) стол — отдаём пусто: вызывающий идёт прежним, проверенным путём.
        if len(out) <= 1:
            return []
    except Exception:  # noqa: BLE001
        log.debug("plate_part_ids scan failed for %s", path, exc_info=True)
        return []
    return out


def plate_object_ids(path) -> list:
    """Список (в порядке столов) множеств OBJECT-id на каждом столе .3mf — параллель к
    plate_part_ids, но БЕЗ разворота в part-id. Нужен как ЗАПАСНОЙ ключ сопоставления: обычно
    trimesh адресует геометрию по part-id (тогда сработает plate_part_ids), но у нестандартных
    .3mf ключ геометрии может совпадать с object_id — тогда сопоставляем по нему (см. geometry).
    [] если столов нет / один / файл не .3mf."""
    out: list = []
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return out
        with zipfile.ZipFile(str(p)) as z:
            xml = _read_model_settings(z)
        if not xml:
            return out
        for pm in re.finditer(r"<plate\b", xml):
            end = xml.find("</plate>", pm.start())
            block = xml[pm.start():end if end > 0 else len(xml)]
            objs: set = set()
            for im in re.finditer(r'<model_instance\b.*?</model_instance>', block, re.DOTALL):
                oidm = re.search(r'key="object_id"\s+value="([^"]+)"', im.group(0))
                if oidm:
                    objs.add(oidm.group(1))
            out.append(objs)
        if len(out) <= 1:
            return []
    except Exception:  # noqa: BLE001
        log.debug("plate_object_ids scan failed for %s", path, exc_info=True)
        return []
    return out


def placed_object_ids(path) -> set:
    """МНОЖЕСТВО object-id, размещённых хоть на каком-то столе .3mf — в отличие от
    plate_object_ids работает и для ОДНОГО стола (той функции [] при ≤1 столе нужен
    вызывающим для «мультиплейт или нет»). Объекты проекта, которых нет ни на одном столе,
    не печатаются (Orca держит их «в закромах») — по этому множеству production-сборка
    отсекает лишнее (OpenTodo 90b32407: «пивные открывашка» рисовала и считала Tab Opener,
    лежащий вне стола). set() — нет model_settings/столов (фильтровать нечем)."""
    out: set = set()
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return out
        with zipfile.ZipFile(str(p)) as z:
            xml = _read_model_settings(z)
        if not xml:
            return out
        for im in re.finditer(r"<model_instance\b.*?</model_instance>", xml, re.DOTALL):
            oidm = re.search(r'key="object_id"\s+value="([^"]+)"', im.group(0))
            if oidm:
                out.add(oidm.group(1))
    except Exception:  # noqa: BLE001
        log.debug("placed_object_ids scan failed for %s", path, exc_info=True)
        return set()
    return out


def plate_multicolor(path) -> list:
    """[bool по столам]: печатается ли стол НЕСКОЛЬКИМИ экструдерами (многоцвет) — для бейджа
    на плитке выбора столов (OpenTodo d7518198). Считаем по model_settings.config: экструдеры
    объекта и его part-ов; стол многоцветный, если у его объектов суммарно >1 разных экструдера.
    [] — не мультиплейт/нет данных."""
    out: list = []
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return out
        with zipfile.ZipFile(str(p)) as z:
            xml = _read_model_settings(z)
        if not xml:
            return out
        obj_ext: dict = {}                      # object_id -> set(extruders)
        for om in re.finditer(r'<object\b[^>]*\bid="([^"]+)"', xml):
            oid = om.group(1)
            end = xml.find("</object>", om.start())
            block = xml[om.start():end if end > 0 else len(xml)]
            exts: set = set()
            oe = re.search(r'key="extruder"\s+value="(\d+)"', block)
            base = int(oe.group(1)) if oe else None
            for prm in re.finditer(r'<part\b[^>]*\bid="[^"]+"', block):
                pend = block.find("</part>", prm.start())
                pblock = block[prm.start():pend if pend > 0 else len(block)]
                pe = re.search(r'key="extruder"\s+value="(\d+)"', pblock)
                exts.add(int(pe.group(1)) if pe else (base if base is not None else 1))
            if base is not None and not exts:
                exts.add(base)
            obj_ext[oid] = exts or {1}
        for pm in re.finditer(r"<plate\b", xml):
            end = xml.find("</plate>", pm.start())
            block = xml[pm.start():end if end > 0 else len(xml)]
            union: set = set()
            for im in re.finditer(r'key="object_id"\s+value="([^"]+)"', block):
                union |= obj_ext.get(im.group(1), set())
            out.append(len(union) > 1)
        if len(out) <= 1:
            return []
    except Exception:  # noqa: BLE001
        log.debug("plate_multicolor scan failed for %s", path, exc_info=True)
        return []
    return out


# Скан главного .model ограничиваем по распакованному размеру: bridge-карта компонентов нужна
# 3D-вьюеру, а его вход и так капнут (webapp._MODEL3D_MAX_SRC_BYTES); монстров не читаем.
_COMPONENT_SCAN_MAX_BYTES = 512 * 1024 * 1024

_PLATE_STL_RE = re.compile(r"^plate[\s_-]*\d+", re.IGNORECASE)


def drop_redundant_plate_stls(paths) -> list:
    """У карточек VK-каталога модель часто лежит ДВАЖДЫ: полный многостольный .3mf ПЛЮС те же
    столы россыпью STL «Plate N - Color.stl». Считать всё вместе = удвоить вес/детали/цену
    (Фалкон: 8 STL + 3mf тех же 8 плит; Валли: 18 + 3mf — аудит избранного, фидбэк 24.07).

    Консервативное правило: в наборе есть хотя бы один .3mf → STL-файлы с именем «Plate N …»
    выкидываем из РАСЧЁТА (полный 3mf богаче: родные столы, цвета, метаданные). Карточку/БД
    фильтр не трогает — применяется на входе расчёта/замера/3D. Наборы БЕЗ 3mf (чистая
    STL-россыпь по столам, Дарт Вейдер 5236) не трогаем: там россыпь и есть модель. Если
    после фильтра не осталось ни одного файла — возвращаем как было (не решаем за автора)."""
    plist = list(paths)
    if not any(Path(p).suffix.lower() == ".3mf" for p in plist):
        return plist
    out = [p for p in plist
           if not (Path(p).suffix.lower() == ".stl"
                   and _PLATE_STL_RE.match(Path(p).stem.strip()))]
    if out and len(out) < len(plist):
        log.info("набор модели: %d STL «Plate N» отброшены — считаем по полному .3mf (%s)",
                 len(plist) - len(out),
                 ", ".join(Path(p).name for p in out if Path(p).suffix.lower() == ".3mf")[:120])
        return out
    return plist


def part_extruders(path) -> dict:
    """{id → номер экструдера} из одного .3mf — привязка деталей к цвету (3D-вьюер, пер-цвет
    грамовка). Экструдер задаётся на уровне <object> (наследуется его part-ами) и/или прямо
    на <part>. Геометрия сцены адресуется РАЗНЫМИ пространствами id, поэтому карта покрывает
    все, без коллизий (первый записанный ключ побеждает):

      1) part-id — ТОЛЬКО уникальные в файле: у Bambu-стиля КАЖДЫЙ объект несёт <part id="1">,
         и старая карта «по part-id» схлопывалась в один мусорный ключ, из-за чего шерсть
         «Овечки Тимми» красилась чёрным (фидбэк 24.07). Крашеные модели с уникальными
         part-id работают как раньше.
      2) object-id из model_settings (2, 4, … у Bambu) — уникальны по построению.
      3) component-objectid из главного 3dmodel.model (<object id=X><component objectid=Y>):
         именно этими Y адресует детали trimesh-сцена у Bambu-обёрток — мост X→Y решает Тимми.

    Пустой dict при любой ошибке — фича опциональна."""
    out: dict = {}
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return out
        with zipfile.ZipFile(str(p)) as z:
            xml = _read_model_settings(z)
            if not xml:
                return out
            part_vals: dict = {}
            part_seen: dict = {}
            obj_vals: dict = {}
            for om in re.finditer(r'<object\b[^>]*\bid="([^"]+)"', xml):
                oid = om.group(1)
                end = xml.find("</object>", om.start())
                block = xml[om.start():end if end > 0 else len(xml)]
                obj_ext = re.search(r'key="extruder"\s+value="(\d+)"', block)
                obj_val = int(obj_ext.group(1)) if obj_ext else None
                first_part_val = None
                for prm in re.finditer(r'<part\b[^>]*\bid="([^"]+)"', block):
                    pid = prm.group(1)
                    pend = block.find("</part>", prm.start())
                    pblock = block[prm.start():pend if pend > 0 else len(block)]
                    pext = re.search(r'key="extruder"\s+value="(\d+)"', pblock)
                    val = int(pext.group(1)) if pext else obj_val
                    if val is not None:
                        part_seen[pid] = part_seen.get(pid, 0) + 1
                        part_vals[pid] = val
                        if first_part_val is None:
                            first_part_val = val
                if obj_val is None:
                    obj_val = first_part_val
                if obj_val is not None:
                    obj_vals[oid] = obj_val
            for pid, val in part_vals.items():
                if part_seen.get(pid) == 1:            # только УНИКАЛЬНЫЕ part-id (см. докстринг)
                    out[pid] = val
            for oid, val in obj_vals.items():
                out.setdefault(oid, val)
            # мост в пространство id мешей сцены: Bambu-обёртка <object id=X> → <component objectid=Y>
            if obj_vals:
                main = next((n for n in z.namelist() if n.lower().endswith("3dmodel.model")), None)
                zi = z.getinfo(main) if main else None
                if zi is not None and zi.file_size <= _COMPONENT_SCAN_MAX_BYTES:
                    cur = None
                    data = z.read(main)
                    for mm in re.finditer(
                            rb'<object\b[^>]*?\bid="([^"]+)"|<component\b[^>]*?\bobjectid="([^"]+)"',
                            data):
                        if mm.group(1) is not None:
                            cur = mm.group(1).decode("utf-8", "replace")
                        elif cur is not None:
                            comp = mm.group(2).decode("utf-8", "replace")
                            val = obj_vals.get(cur)
                            if val is not None:
                                out.setdefault(comp, val)
    except Exception:  # noqa: BLE001
        log.debug("part_extruders scan failed for %s", path, exc_info=True)
    return out


# ───────────────── Цветовые группы в ИМЕНАХ файлов набора (Ам Ням) ─────────────────
# Авторы мультицветных наборов раскладывают детали по цветам прямо в именах файлов
# («obj_1_(Green) Body.stl», «(White) Eyes.stl»). Единый разбор живёт ЗДЕСЬ: расчёт
# (calcservice — столы по цвету), 3D (webapp — слоты GLB) и превью (preview — покраска
# деталей) обязаны видеть ОДНИ И ТЕ ЖЕ группы в ОДНОМ порядке, иначе слот k у клиента
# укажет не на тот цвет. Только базовые английские слова: чем шире список, тем больше
# ложных срабатываний на обычных именах.
FILENAME_COLOR_WORDS = {
    "green", "white", "black", "red", "blue", "yellow", "orange", "purple", "pink",
    "gray", "grey", "brown", "gold", "silver", "clear", "transparent", "natural",
    "cyan", "magenta", "lime", "teal", "beige", "tan", "ivory", "cream", "maroon",
    "navy", "olive", "violet", "turquoise",
}
# Представительный hex под каждое слово — для mc_colors карточки (клиент предвыберет
# ближайший цвет своей палитры, точность «на глаз» достаточна) и покраски превью.
FILENAME_COLOR_HEX = {
    "green": "#2e9e4f", "white": "#f4f4f4", "black": "#262626", "red": "#d63b3b",
    "blue": "#2b6fd8", "yellow": "#f2c11e", "orange": "#f28a2e", "purple": "#8e44ad",
    "pink": "#e87ab2", "gray": "#9aa3ad", "grey": "#9aa3ad", "brown": "#8b5a2b",
    "gold": "#d4af37", "silver": "#c0c0c0", "clear": "#e9edf0", "transparent": "#e9edf0",
    "natural": "#e8e2d5", "cyan": "#29b8ce", "magenta": "#d9309f", "lime": "#9ed941",
    "teal": "#2a9d8f", "beige": "#e6d3b3", "tan": "#d2b48c", "ivory": "#f4f1e0",
    "cream": "#f2e8c9", "maroon": "#7b2d26", "navy": "#24365c", "olive": "#7a7a2f",
    "violet": "#7d5ba6", "turquoise": "#35c1c9",
}
_FN_COLOR_TAG_RE = re.compile(r"\(([A-Za-z][A-Za-z ]{1,24})\)")
# Имя файла ЦЕЛИКОМ = слово-цвет с номером/тиражом: «gold5», «black1x12», «silver_x6»,
# «white4_x4» (Железный человек/Дональд-Росомаха — россыпь по цветам БЕЗ скобок).
_FN_COLOR_BARE_RE = re.compile(r"^([A-Za-z][A-Za-z ]{1,24}?)[\s_-]*\d*(?:[\s_-]*x\d+)?$",
                               re.IGNORECASE)


def filename_color_tag(name: str) -> str:
    """Цветовой тег из имени файла: слово-цвет в скобках («(Green) Body») либо имя целиком =
    цвет с номером («gold5», «black1x12»). Допускаем префикс light/dark — «light blue» и
    «dark blue» — разные филаменты, сливать нельзя. '' — тега нет. Ложные срабатывания на
    обычных именах гасит порог в filename_color_groups (≥2 групп и ≥60% размеченных)."""
    stem = Path(name).stem
    for m in _FN_COLOR_TAG_RE.finditer(stem):
        cand = m.group(1).strip().lower()
        if cand.split()[-1] in FILENAME_COLOR_WORDS:
            return cand
    m = _FN_COLOR_BARE_RE.match(stem.strip())
    if m:
        cand = m.group(1).strip().lower()
        if cand.split()[-1] in FILENAME_COLOR_WORDS:
            return cand
    return ""


def filename_color_groups(paths) -> dict:
    """{str(path) → тег} ТОЛЬКО когда набор реально размечен цветами: ≥2 разных тегов и
    помечено ≥ max(2, 60%) файлов (порог из calcservice, кейс «Ам Ням» defe5dc0). Иначе {} —
    случайная скобка в одном имени не делает набор цветным."""
    plist = [str(p) for p in paths]
    tagged = {p: t for p in plist if (t := filename_color_tag(p))}
    if len(set(tagged.values())) >= 2 and len(tagged) >= max(2, int(0.6 * len(plist))):
        return tagged
    return {}


def _tag_hex(tag: str) -> str:
    """Hex представителя тега; префиксы light/dark осветляют/затемняют базовое слово."""
    words = tag.split()
    base = FILENAME_COLOR_HEX.get(words[-1], "")
    if not base:
        return ""
    if len(words) >= 2 and words[0] in ("light", "dark"):
        r, g, b = (int(base[i:i + 2], 16) for i in (1, 3, 5))
        if words[0] == "light":
            r, g, b = (min(255, int(c + (255 - c) * 0.4)) for c in (r, g, b))
        else:
            r, g, b = (int(c * 0.6) for c in (r, g, b))
        return f"#{r:02x}{g:02x}{b:02x}"
    return base


def filename_group_slots(paths) -> tuple:
    """({str(path) → слот}, [hex по слотам]) — слоты в порядке ПЕРВОГО появления тега в
    наборе (порядок paths стабилен: он хранится в БД/токене). ({}, []) — групп нет."""
    groups = filename_color_groups(paths)
    if not groups:
        return {}, []
    order: list = []
    for p in (str(x) for x in paths):
        t = groups.get(p)
        if t and t not in order:
            order.append(t)
    slot_of_tag = {t: i for i, t in enumerate(order)}
    return ({p: slot_of_tag[t] for p, t in groups.items()},
            [_tag_hex(t) or "#9aa3ad" for t in order])


# ───────────────── Варианты набора файлов карточки (OpenTodo 87eb9cf3) ─────────────────
def variant_groups(paths) -> list | None:
    """Карточки VK-каталога часто несут одну модель НЕСКОЛЬКИМИ КОМПЛЕКТАМИ файлов: полный
    .3mf + та же модель россыпью STL по цветам («Железный человек»: iron man.3mf + 28 STL —
    смета суммировала 68 деталей вместо 34). Автоматика тут опасна (россыпь бывает и
    настоящими деталями), поэтому выбор комплекта отдаём клиенту.

    Группировка: каждый .3mf — свой вариант; вся не-3mf россыпь (STL/OBJ) разом — ещё один.
    Возвращает [{key, label, paths}] (≥2 групп, первый — дефолт: полный .3mf богаче) или
    None — вариантов нет: <2 групп, россыпь мала (≤2 файла — аксессуар к модели, считаем
    вместе) или набор без .3mf вовсе (чистая россыпь и есть модель). «Plate N»-дубли при
    живом .3mf уже вырезает drop_redundant_plate_stls — они НЕ вариант."""
    plist = [str(p) for p in paths]
    if len(plist) < 2:
        return None
    plist = drop_redundant_plate_stls(plist)
    mfs = [p for p in plist if Path(p).suffix.lower() == ".3mf"]
    loose = [p for p in plist if Path(p).suffix.lower() != ".3mf"]
    if not mfs:
        return None
    groups = [{"key": f"3mf:{Path(p).name}", "label": Path(p).stem, "paths": [p]} for p in mfs]
    if len(loose) >= 3:
        groups.append({"key": "loose", "label": f"Отдельные детали ({len(loose)} STL)",
                       "paths": loose})
    return groups if len(groups) >= 2 else None


# ───────────────── Покраска треугольников (Bambu/Orca paint_color) ─────────────────
# Каждый <triangle> может нести RLE-код TriangleSelector'а (PrusaSlicer-формат, у Bambu —
# атрибут paint_color, у Prusa/старого Bambu — mmu_segmentation): деталь буквально раскрашена
# по граням вторым-третьим филаментом («двухветвная деталь» Dyson dryer holder, OpenTodo
# 3b91dbaa). Ниже — декодер, достаточный для 3D/превью: код читается С КОНЦА строки, нибл
# «xxyy»: yy=0 — лист (xx = состояние 0..2; xx=3 → состояние в следующем нибле), yy>0 —
# деление на yy+1 дочерних (xx — «особая сторона», для окраски не важна). Для поделённых
# треугольников берём ДОМИНИРУЮЩЕЕ состояние по весу 1/(потомков) — субтреугольную точность
# вьюер всё равно не показывает. Состояние k = филамент k; 0 = экструдер самой детали.
_PAINT_TRI_RE = re.compile(rb'<object\b[^>]*?\bid="([^"]+)"|</object>|<triangle\b[^>]*>')
_PAINT_ATTR_RE = re.compile(rb'\b(?:paint_color|mmu_segmentation)="([0-9A-Fa-f]+)"')
_PAINT_MODEL_MAX_BYTES = 256 * 1024 * 1024   # .model крупнее не разбираем (защита памяти)
_PAINT_MAX_DEPTH = 12                        # RLE-рекурсия глубже — битый код, отдаём 0


def _paint_majority_state(code: str) -> int:
    """Итоговое состояние одного треугольника из RLE-кода. 0 — код пуст/не разобрался."""
    try:
        nibbles = [int(ch, 16) for ch in reversed(code)]
    except ValueError:
        return 0
    pos = 0
    weights: dict = {}

    def _read() -> int:
        nonlocal pos
        v = nibbles[pos] if pos < len(nibbles) else 0
        pos += 1
        return v

    def _rec(weight: float, depth: int) -> None:
        if depth > _PAINT_MAX_DEPTH:
            raise ValueError("paint code too deep")
        nib = _read()
        split = nib & 0b11
        top = (nib >> 2) & 0b11
        if split == 0:
            state = top if top < 3 else 3 + _read()
            weights[state] = weights.get(state, 0.0) + weight
            return
        kids = split + 1
        for _ in range(kids):
            _rec(weight / kids, depth + 1)

    try:
        _rec(1.0, 0)
    except (ValueError, IndexError):
        return 0
    if not weights:
        return 0
    state = max(weights, key=lambda s: weights[s])
    return state if 0 <= state <= 8 else 0


def paint_states(path) -> dict:
    """{object_id → [состояние на КАЖДЫЙ треугольник]} по всем .model внутри .3mf — только
    объекты, где покраска реально есть. Индекс списка = порядковый номер <triangle> в файле
    (тот же порядок, в котором trimesh/ручная сборка строят faces). Пусто — покраски нет,
    файл не .3mf или не разобрался: вызывающий красит деталь одним цветом, как раньше."""
    out: dict = {}
    try:
        p = Path(path)
        if p.suffix.lower() != ".3mf" or not p.exists():
            return {}
        with zipfile.ZipFile(str(p)) as z:
            for n in z.namelist():
                if not n.lower().endswith(".model"):
                    continue
                zi = z.getinfo(n)
                if zi.file_size > _PAINT_MODEL_MAX_BYTES or not _scan_painted(z, n):
                    continue
                data = z.read(n)
                cur: str | None = None
                states: list = []
                painted = False
                for m in _PAINT_TRI_RE.finditer(data):
                    if m.group(1) is not None:            # <object id=…>
                        cur = m.group(1).decode("utf-8", "replace")
                        states, painted = [], False
                        out[cur] = states                 # список наполняется по месту
                    elif m.group(0).startswith(b"</object"):
                        if cur is not None and not painted:
                            out.pop(cur, None)            # некрашеный объект не тащим
                        cur = None
                    elif cur is not None:                 # <triangle …>
                        am = _PAINT_ATTR_RE.search(m.group(0))
                        if am:
                            st = _paint_majority_state(am.group(1).decode("ascii", "replace"))
                            painted = painted or st > 0
                            states.append(st)
                        else:
                            states.append(0)
                if cur is not None and not painted:       # файл без закрывающего </object>
                    out.pop(cur, None)
    except Exception:  # noqa: BLE001
        log.debug("paint_states scan failed for %s", path, exc_info=True)
        return {}
    return out


def inspect_model(paths) -> ModelInfo:
    """Анализ набора файлов модели: берём все .3mf и агрегируем сигналы."""
    agg = ModelInfo()
    for p in paths:
        p = Path(p)
        if p.suffix.lower() != ".3mf" or not p.exists():
            continue
        one = _inspect_one(p)
        agg.plates = max(agg.plates, one.plates)
        agg.extruders |= one.extruders
        agg.painted = agg.painted or one.painted
        # смешение агрегируем ПО-ФАЙЛОВО: два одноцветных файла с разными экструдерами
        # печатаются раздельно — union экструдеров сам по себе смешения не создаёт
        agg.mixed_plates = agg.mixed_plates or one.mixed_plates
        if len(one.filament_colours) > len(agg.filament_colours):
            agg.filament_colours = one.filament_colours
    return agg
