"""Допечатать без сбойной детали — «Путь A» из docs/Перспективные разработки/exclude-object.md.

Задача мастера: на столе набор деталей, одна отвалилась или ушла в спагетти. Сегодня выбор
бинарный — терпеть до конца или отменить всю печать. Путь A — третий: остановить печать,
пересобрать задание БЕЗ сбойной детали и допечатать остальные. Работает на СТОКОВОМ принтере:
ни прошивки, ни меток `M486`, ни правки боевых процесс-профилей (это Путь B, и он ждёт решения
владельца — у стоковой CC2 нет команды «пропусти объект» в протоколе вовсе).

Здесь — ядро: гейты доступности (`job_context`), список деталей текущего задания
(`list_parts`), пересборка задания без выбранных (`rebuild_without`) и боевой поток. Ручки
(app/web/printerjob.py) поверх этого тонкие.

ПОТОК РАЗОРВАН НА ДВА ДЕЙСТВИЯ, и это главное в модуле. Новое задание — это НЕ продолжение
прерванной печати: остаток режется целиком и печатается С ПЕРВОГО СЛОЯ в НОВОЙ раскладке
(`arrange`), а на столе в этот момент лежат недопечатанные детали и сбойная. Автоматический
старт означал бы заход соплом в существующий пластик через несколько минут после нажатия, когда
мастер уже отошёл. Поэтому:

  1. `reprint_without` — стоп + переслайс + заливка. Отвечает «залито, СНИМИТЕ ВСЁ СО СТОЛА»;
  2. `start_reprint` — старт. Отдельное подтверждение мастера У СТАНКА, с пустым столом.

ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ (и почему):
  * догадок. Нет привязки печати к заказу, печать уже кончилась, нет файла модели, нет снимка
    настроек, драйвер не умеет upload/start/control, набор мультиплейтный, деталь одна, модель
    покрашена по граням — действие НЕДОСТУПНО с названной причиной (`ReprintUnavailable`), а не
    «попробуем угадать». Каждая такая причина — это место, где мы могли бы выбросить не ту деталь;
  * остановки печати до проверки выбора. Выбор деталей проверяется ПЕРВЫМ (`_check_selection`
    до `drv.control`): отказ «список устарел» после убитой печати — это отказ ценой печати;
  * автоматических остановок. `reprint_without` останавливает печать только потому, что мастер
    нажал и подтвердил; сам модуль ничего не решает и никем не вызывается по таймеру;
  * второго слайс-пути. Режем теми же кирпичами, что и смета (`slicer.slice_to_gcode` поверх
    `_resolve_slice_inputs`/`_write_job_profiles`), и тем же столом, что и 3D-вьюер заказа.

ИДЕНТИФИКАТОР ДЕТАЛИ — пара (позиционный индекс в `geometry.load_print_meshes_with_ids`,
`part_id`). Индекс сам по себе устойчив только при неизменных (файлы, стол, версия geometry):
стол решает дедуп наложенных копий, то есть и число, и порядок деталей. Поэтому фронт присылает
обратно ОБА поля, и перед резкой мы сверяем, что по индексу лежит та же деталь (`expect_ids`).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db, elegoo, geometry, slicer
from .bedspec import DEFAULT_BED, BedSpec

log = logging.getLogger(__name__)

# Расширение, которое производит наш слайс-путь (ElegooSlicer CLI кладёт plate_*.gcode). У
# драйвера свой список принимаемого (`upload_exts`): Bambu ждёт .3mf/.gcode.3mf и допечатку
# нашим gcode не примет — это честный отказ, а не повод подсунуть ему чужой формат.
PRODUCED_EXT = ".gcode"


class ReprintUnavailable(RuntimeError):
    """Допечатка невозможна. Текст исключения — ПРИЧИНА ДЛЯ МАСТЕРА, она идёт прямо в интерфейс."""


@dataclass(frozen=True)
class PartInfo:
    """Одна деталь задания: позиция в наборе + чем её опознать и что она весит."""
    index: int
    part_id: str
    volume_cm3: float
    dims_mm: tuple[float, float, float]

    @property
    def name(self) -> str:
        return f"part{self.index}"

    def as_dict(self) -> dict:
        return {"index": self.index, "name": self.name, "part_id": self.part_id,
                "volume_cm3": round(self.volume_cm3, 2),
                "dims_mm": [round(x, 1) for x in self.dims_mm]}


@dataclass(frozen=True)
class JobContext:
    """Всё, чем можно пересобрать ЭТО задание: заказ, стол, профили, движок.

    Собирается из ЗАКАЗА (а не из снимка настроек): снимок хранит человеческие строки из
    конфиг-блока gcode («Сопло 0.4», «Поддержки: да») и параметры старта, но ни путей к
    профилям, ни пресетов в нём нет. Снимок здесь — гейт и сверка, см. `job_context`."""
    request_id: int
    label: str
    job_name: str
    printer_id: int | None
    printer_name: str
    paths: list[Path]
    bed: BedSpec
    scale: float
    machine_profile: str
    process_dir: str
    base_process_name: str
    filament_profile: str
    slicer_kind: str = ""
    slicer_path: str = ""
    density: float = 1.24
    start_kw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReprintPlan:
    """Пересобранное задание: файл + честная сводка для подтверждения мастером."""
    gcode: Path
    kept: list[PartInfo]
    dropped: list[PartInfo]
    volume_cm3: float
    minutes: float
    grams: float = 0.0

    def as_dict(self) -> dict:
        return {"kept": [p.as_dict() for p in self.kept],
                "dropped": [p.as_dict() for p in self.dropped],
                "minutes": round(self.minutes), "grams": round(self.grams, 1)}


def check_driver(drv) -> None:
    """Умеет ли ЭТА машина допечатку. Проверяем ДО показа кнопки и ещё раз перед стопом.

    Четыре способности, и каждая нужна: `control` — остановить текущую печать, `upload` —
    залить пересобранное задание, `start` — запустить его, а расширение произведённого файла
    должно быть в списке принимаемого драйвером. Флаг `caps.exclude_object` здесь НИ ПРИ ЧЁМ:
    это Путь B (снять деталь на лету), у всех наших драйверов он False, и гейтить им Путь A
    значило бы погасить фичу ровно на той машине, где она работает."""
    if drv is None or not drv.enabled():
        raise ReprintUnavailable("Принтер не подключён")
    caps = drv.caps
    if not caps.control:
        raise ReprintUnavailable(f"Принтер ({drv.title}) не умеет останавливать печать по команде")
    if not caps.upload:
        raise ReprintUnavailable(f"Принтер ({drv.title}) не умеет принимать задания")
    if not caps.start:
        raise ReprintUnavailable(f"Принтер ({drv.title}) не умеет запускать печать по команде")
    exts = {str(e).lower() for e in (getattr(drv, "upload_exts", ()) or ())}
    if exts and PRODUCED_EXT not in exts:
        raise ReprintUnavailable(
            f"Принтер ({drv.title}) принимает {', '.join(sorted(exts))}, "
            f"а переслайс даёт {PRODUCED_EXT}")


async def ensure_printing(drv) -> None:
    """Идёт ли печать ПРЯМО СЕЙЧАС. Проверяем и при показе кнопки, и вплотную перед стопом.

    Печать могла кончиться, пока мастер выбирал детали в диалоге: наблюдатель ставит
    `finished` и снимает привязку только на своём тике (printwatch.POLL_SEC = 60), так что
    флаг в БД отстаёт на минуту-две. Без этой проверки допечатка спокойно запустила бы ПОЛНУЮ
    повторную печать остатка поверх готового, уже снятого или ещё стоящего на столе набора.

    Судим только по ЖИВОМУ ответу драйвера и только когда он есть: нет связи или нет кадра —
    молчим (та же осторожность, что у printwatch._tick: без данных о финише судить нельзя),
    иначе сорванная телеметрия отняла бы у мастера рабочее действие."""
    if drv is None or not getattr(drv, "caps", None) or not drv.caps.status:
        return
    try:
        st = await asyncio.to_thread(drv.status)
    except Exception:  # noqa: BLE001
        log.debug("допечатка: статус принтера не прочитать", exc_info=True)
        return
    if not st or not st.get("ok") or not st.get("state"):
        return
    try:
        active = bool(drv.job_state(st.get("state") or {}).get("active"))
    except Exception:  # noqa: BLE001
        log.debug("допечатка: job_state не разобрать", exc_info=True)
        return
    if not active:
        raise ReprintUnavailable("Принтер сейчас не печатает — допечатывать нечего "
                                 "(печать уже закончилась или остановлена)")


async def ensure_idle(drv) -> None:
    """Зеркало ensure_printing для ШАГА 2: принтер обязан быть СВОБОДЕН.

    Между заливкой и стартом проходит столько, сколько мастер чистит стол, — а карточка
    «задание залито» живёт часами. За это время на принтере могла начаться другая печать
    (руками с панели, другим заказом): старт допечатки поверх неё убил бы чужую работу.
    Судим, как и там, только по ЖИВОМУ ответу драйвера: нет связи — не мешаем мастеру."""
    if drv is None or not getattr(drv, "caps", None) or not drv.caps.status:
        return
    try:
        st = await asyncio.to_thread(drv.status)
    except Exception:  # noqa: BLE001
        log.debug("допечатка: статус принтера не прочитать", exc_info=True)
        return
    if not st or not st.get("ok") or not st.get("state"):
        return
    try:
        active = bool(drv.job_state(st.get("state") or {}).get("active"))
    except Exception:  # noqa: BLE001
        log.debug("допечатка: job_state не разобрать", exc_info=True)
        return
    if active:
        raise ReprintUnavailable("Принтер сейчас печатает — сначала дождитесь конца или "
                                 "остановите текущую печать, иначе допечатка встанет поверх неё")


async def job_context(printer_id: int | None = None, *, drv=None) -> JobContext:
    """Контекст допечатки для машины `printer_id` — или `ReprintUnavailable` с причиной.

    Порядок проверок — от дешёвых к дорогим, и каждая закрывает свой способ напечатать не то:
    привязка к заказу (иначе мы не знаем, ЧТО на столе — боевой сценарий «мастер порезал в
    слайсере руками» файла у нас не оставляет), файл модели на диске, снимок настроек задания
    (им же берём параметры повторного старта: пластина и слоты хаба в gcode не записаны),
    одноплейтность (выбор столов мультиплейта нигде не сохранён — какой стол сейчас на столе,
    мы не знаем) и разрешимость пресетов заказа."""
    if drv is not None:
        check_driver(drv)
    binding = await db.get_print_binding(printer_id)
    if not binding or not binding.get("request_id"):
        raise ReprintUnavailable("Печать не привязана к заказу — непонятно, что сейчас на столе")
    if binding.get("finished"):
        raise ReprintUnavailable("Эта печать уже завершена — допечатывать нечего")
    job_name = str(binding.get("job_name") or "").strip()
    if not job_name:
        raise ReprintUnavailable("Не известно имя текущего задания на принтере")
    if drv is not None:
        await ensure_printing(drv)
    rid = int(binding["request_id"])
    r = await db.get_request(rid)
    if not r:
        raise ReprintUnavailable(f"Заказ #{rid} не найден")
    path = Path(str(getattr(r, "file_path", "") or ""))
    if not str(path) or not path.is_file():
        raise ReprintUnavailable("Файл модели заказа не найден на диске")
    if int(getattr(r, "plates", 1) or 1) > 1:
        raise ReprintUnavailable(
            "Заказ мультиплейтный — какой стол сейчас печатается, мы не знаем")
    snap = await db.find_print_settings(job_name, elegoo.normalize_job_name(job_name),
                                        printer_id=printer_id)
    if not snap:
        raise ReprintUnavailable(f"Нет снимка настроек задания «{job_name}» — "
                                 "чем резали то, что на столе, неизвестно")
    # Ленивые импорты: calcservice тянет за собой весь ценовой путь, и держать его на верхнем
    # уровне значило бы завязать ядро допечатки на порядок импортов бота.
    from . import calcservice, materials
    printer = await calcservice._resolve_printer(getattr(r, "printer", "") or "")
    quality = await calcservice._resolve_quality(r)
    strength = await calcservice._resolve_strength(r)
    plastic = await calcservice._resolve_plastic(getattr(r, "plastic_name", "") or "")
    if not (printer and quality and strength and plastic):
        raise ReprintUnavailable("Не восстановить параметры заказа "
                                 "(принтер / пластик / качество / прочность)")
    bed = await asyncio.to_thread(BedSpec.from_machine_profile,
                                  getattr(printer, "machine_profile", None)) or DEFAULT_BED
    _ = strength   # пресет прочности проверен выше как гейт: слайс идёт по профилю КАК ЕСТЬ
    machine = await db.machine_profile_for(printer, db.nozzle_of(quality))
    filament = materials.resolve_filament(
        printer.filament_dir, plastic.material_type,
        fallback=plastic.filament_profile or str(config.DEFAULT_FILAMENT_PROFILE),
        prefer=plastic.filament_profile)
    scale_pct = 100
    try:
        scale_pct = int((getattr(r, "breakdown", None) or {}).get("scale_pct", 100) or 100)
    except (TypeError, ValueError):
        scale_pct = 100
    return JobContext(
        request_id=rid, label=f"#{rid}", job_name=job_name,
        printer_id=printer_id, printer_name=getattr(printer, "name", "") or "",
        paths=[path], bed=bed, scale=scale_pct / 100.0,
        machine_profile=machine, process_dir=printer.process_dir,
        base_process_name=quality.process, filament_profile=filament,
        slicer_kind=getattr(printer, "slicer", "") or "",
        slicer_path=getattr(printer, "slicer_path", "") or "",
        density=float(getattr(plastic, "density", 1.24) or 1.24),
        start_kw=dict(snap.get("start") or {}))


def _load_triples(ctx: JobContext) -> list:
    """Детали задания как тройки (меш, part_id, файл) — в МАСШТАБЕ ЗАКАЗА и на СТОЛЕ ЗАКАЗА.

    Тот же вызов и тот же стол, что у 3D-вьюера деска (`web/mesh3d._model3d_glb_bytes` через
    `_order_bed`): разъедься столы — мастер отметит одну деталь, а выкинется другая."""
    triples = geometry.load_print_meshes_with_ids(ctx.paths, ctx.bed)
    triples = [t for t in (triples or []) if hasattr(t[0], "faces") and len(t[0].faces)]
    if ctx.scale and abs(ctx.scale - 1.0) > 1e-9:
        slicer._apply_scale([t[0] for t in triples], ctx.scale)
    return triples


def _part_infos(triples: list) -> list[PartInfo]:
    out: list[PartInfo] = []
    for i, (mesh, pid, _src) in enumerate(triples):
        try:
            lo, hi = mesh.bounds
            dims = (float(hi[0] - lo[0]), float(hi[1] - lo[1]), float(hi[2] - lo[2]))
        except Exception:  # noqa: BLE001
            dims = (0.0, 0.0, 0.0)
        try:
            vol = max(0.0, float(mesh.volume) / 1000.0)   # мм³ → см³
        except Exception:  # noqa: BLE001
            vol = 0.0
        out.append(PartInfo(index=i, part_id=str(pid or ""), volume_cm3=vol, dims_mm=dims))
    return out


def _painted(ctx: JobContext) -> bool:
    """Покрашена ли модель по граням. Такая деталь в 3D-вьюере разрезается на НЕСКОЛЬКО узлов
    (`geometry.split_mesh_by_paint`), и наши индексы деталей с именами part{i} во вьюере уже не
    совпадают. Молча предлагать выбор в такой ситуации нельзя — мастер отметит деталь по
    картинке, а выкинется соседняя."""
    try:
        return any(bool(v) for v in (geometry.part_color_context(ctx.paths).get("paint") or {}).values())
    except Exception:  # noqa: BLE001
        return False


async def list_parts(ctx: JobContext) -> list[PartInfo]:
    """Список деталей текущего задания (или `ReprintUnavailable` с причиной)."""
    if await asyncio.to_thread(_painted, ctx):
        raise ReprintUnavailable("Модель покрашена по граням — нумерация деталей во вьюере "
                                 "не совпадает с деталями слайса, выбор был бы вслепую")
    triples = await asyncio.to_thread(_load_triples, ctx)
    if not triples:
        raise ReprintUnavailable("Детали модели не опознать (геометрия не читается)")
    if len(triples) < 2:
        raise ReprintUnavailable("В задании одна деталь — допечатывать без неё нечего")
    if len(triples) > config.MAX_MESH_FILES:
        raise ReprintUnavailable(
            f"Деталей в наборе {len(triples)} — больше лимита {config.MAX_MESH_FILES}: "
            "переслайс потерял бы и хорошие детали")
    return _part_infos(triples)


def _check_selection(parts: list[PartInfo], drop: list[int],
                     expect_ids: dict[int, str] | None) -> None:
    n = len(parts)
    bad = [i for i in drop if not (0 <= i < n)]
    if bad:
        raise ReprintUnavailable(f"Деталей с номерами {bad} в задании нет")
    if not drop:
        raise ReprintUnavailable("Не отмечено ни одной сбойной детали")
    if len(set(drop)) >= n:
        raise ReprintUnavailable("Отмечены все детали — это просто остановка печати, "
                                 "допечатывать нечего")
    for i, pid in (expect_ids or {}).items():
        if 0 <= i < n and str(parts[i].part_id) != str(pid):
            # Набор и порядок деталей зависят от стола и версии geometry: если они разъехались
            # между показом списка и подтверждением, выкинули бы не ту деталь.
            raise ReprintUnavailable("Список деталей изменился — откройте его заново")


def _export_kept(ctx: JobContext, drop: set[int], out_dir: Path,
                 expect: list[PartInfo] | None = None) -> tuple[list[Path], list[PartInfo], list[PartInfo]]:
    """Отфильтровать детали и выгрузить оставшиеся в STL. Возврат: (stl, оставшиеся, выкинутые).

    Фильтруем на уровне ТРОЕК, а не STL: `_placed_to_stls` склеивает все меши в один файл, если
    их общий bbox влезает на стол (нужно для flexi), поэтому «деталей стало меньше» считается
    по длине отфильтрованного списка, а не по числу файлов.

    Геометрия разбирается ЗАНОВО (держать меши набора через await значило бы держать и всю их
    память), поэтому индексы отбраковки здесь сверяются с тем списком, который видел мастер:
    номера применяются к ЭТОМУ разбору, а сверка part_id была сделана по тому."""
    triples = _load_triples(ctx)
    infos = _part_infos(triples)
    if expect is not None and [p.part_id for p in infos] != [p.part_id for p in expect]:
        raise ReprintUnavailable("Список деталей изменился, пока шло подтверждение — "
                                 "откройте его заново")
    kept_idx = [i for i in range(len(triples)) if i not in drop]
    kept_meshes = [triples[i][0] for i in kept_idx]
    stls = slicer._placed_to_stls(kept_meshes, out_dir / "parts", f"reprint {ctx.label}",
                                  prefix="part", bed=ctx.bed)
    return stls, [infos[i] for i in kept_idx], [infos[i] for i in sorted(drop)]


async def rebuild_without(ctx: JobContext, drop: list[int], *,
                          expect_ids: dict[int, str] | None = None,
                          out_dir: Path | None = None,
                          parts: list[PartInfo] | None = None) -> ReprintPlan:
    """Пересобрать задание БЕЗ отмеченных деталей → файл + сводка.

    Режем пресетами заказа с настройками автора проекта поверх (`slicer._project_overrides`) —
    ровно так же, как считает смету путь с выбором столов. Нативный путь (проект-.3mf «как
    есть») подмножеству деталей недоступен по определению, поэтому время и расход остатка могут
    отличаться от исходной печати; интерфейс говорит об этом мастеру вслух."""
    # `parts` передаёт боевой поток: там список деталей и выбор проверены ДО остановки печати,
    # и второй раз разбирать геометрию (минуты на большом наборе) незачем.
    parts = parts if parts is not None else await list_parts(ctx)
    _check_selection(parts, list(drop), expect_ids)
    drop_set = {int(i) for i in drop}
    out_dir = Path(out_dir or (config.SLICE_DIR / f"reprint_{ctx.request_id}_{uuid.uuid4().hex[:8]}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stls, kept, dropped = await asyncio.to_thread(_export_kept, ctx, drop_set, out_dir, parts)
    if not stls:
        raise ReprintUnavailable("Оставшиеся детали не удалось выгрузить для слайса")
    gcode, volume, minutes = await slicer.slice_to_gcode(
        stls, out_dir / "gcode", machine_profile=ctx.machine_profile,
        process_dir=ctx.process_dir, base_process_name=ctx.base_process_name,
        filament_profile=ctx.filament_profile, slicer_kind=ctx.slicer_kind,
        slicer_path=ctx.slicer_path, overrides=slicer._project_overrides(ctx.paths))
    return ReprintPlan(gcode=Path(gcode), kept=kept, dropped=dropped,
                       volume_cm3=volume, minutes=minutes,
                       grams=round(volume * ctx.density, 1))


def job_filename(ctx: JobContext) -> str:
    """Имя нового задания на принтере. Только ASCII и без пробелов: имя ездит по протоколу
    принтера и возвращается обратно в телеметрии, где кириллица уже ломала сопоставление.
    Секунды и случайный хвост — чтобы две допечатки одного заказа подряд (сбойными оказались
    две детали) не совпали именем: заливка перезаписала бы файл, а снимок настроек и привязка
    склеились бы с предыдущим заданием."""
    return (f"o{ctx.request_id}_reprint_{time.strftime('%m%d_%H%M%S', time.localtime())}"
            f"_{uuid.uuid4().hex[:4]}.gcode")


@dataclass(frozen=True)
class PendingStart:
    """Залитое, но ЕЩЁ НЕ ЗАПУЩЕННОЕ задание допечатки — ждёт, пока мастер очистит стол.

    Живёт в памяти процесса: перезапуск бота её теряет, и это честно — после перезапуска мастер
    получит «состояние потеряно, запустите файл с панели принтера», а не тихий старт."""
    printer_id: int | None
    request_id: int
    label: str
    filename: str
    start_kw: dict
    kept: int
    dropped: int
    minutes: float
    grams: float
    at: float


_PENDING: dict[int, PendingStart] = {}
# Сколько ждёт залитое задание. Мастер уходит, отвлекается, начинает другую работу — но через
# полдня «запустить допечатку» уже не про то, что стоит на столе, и красная карточка в деске
# врала бы. Протухшее забываем: файл на принтере остаётся, запустить его можно с панели.
PENDING_TTL_SEC = 6 * 3600


def pending_start(printer_id: int | None) -> PendingStart | None:
    """Задание допечатки этой машины, ожидающее старта (или None)."""
    key = int(printer_id or 0)
    pend = _PENDING.get(key)
    if pend and time.time() - pend.at > PENDING_TTL_SEC:
        _PENDING.pop(key, None)
        return None
    return pend


async def reprint_without(drv, printer_id: int | None, drop: list[int], *,
                          uid: int | None = None,
                          expect_ids: dict[int, str] | None = None) -> dict:
    """ШАГ 1 допечатки: проверка выбора → стоп → переслайс → заливка → журнал. БЕЗ СТАРТА.

    Зовётся ТОЛЬКО из ручки, ТОЛЬКО после подтверждения мастера. Порядок сознательный:

      * СНАЧАЛА проверка (контекст, список деталей, выбор). Любой отказ на этом шаге стоит
        мастеру ничего; отказ после стопа стоил бы ему всей печати;
      * потом остановка — сбойная деталь как раз сейчас елозит по столу и портит соседей;
      * и НИКОГДА старт. Новое задание печатается с первого слоя и в новой раскладке, а на
        столе лежит недопечатанное: запуск — отдельное действие мастера у станка
        (`start_reprint`), после того как он снимет всё со стола.

    Всё, что случилось после успешного стопа, помечается в ответе `stopped: true` — мастер
    обязан узнать, что печать убита, даже если дальше не заладилось."""
    ctx = await job_context(printer_id, drv=drv)
    parts = await list_parts(ctx)
    _check_selection(parts, list(drop), expect_ids)
    await ensure_printing(drv)          # выбирали детали не минуту — печать могла и кончиться
    stop = await asyncio.to_thread(drv.control, "stop")
    if not (stop or {}).get("ok"):
        return {"ok": False, "error": (stop or {}).get("error") or "Не удалось остановить печать"}
    plan_dir = config.SLICE_DIR / f"reprint_{ctx.request_id}_{uuid.uuid4().hex[:8]}"
    try:
        try:
            plan = await rebuild_without(ctx, drop, expect_ids=expect_ids, out_dir=plan_dir,
                                         parts=parts)
            name = job_filename(ctx)
            up = await drv.upload(plan.gcode, name=name)
        except ReprintUnavailable as exc:
            return {"ok": False, "stopped": True,
                    "error": f"Печать остановлена, но допечатка не собралась: {exc}"}
        except Exception as exc:  # noqa: BLE001
            log.warning("допечатка %s: переслайс/заливка не удались", ctx.label, exc_info=True)
            return {"ok": False, "stopped": True,
                    "error": f"Печать остановлена, но переслайс не удался: {exc}"}
        if not (up or {}).get("ok"):
            return {"ok": False, "stopped": True,
                    "error": "Печать остановлена, но задание не залилось: "
                             + str((up or {}).get("error") or "принтер не принял файл")}
        # Снимок настроек НОВОГО файла: без него вторую сбойную деталь тем же путём выбросить
        # уже не выйдет (гейт «нет снимка»), а финиш печати не свяжет журнал с настройками.
        # Помощник тот же, что у ручной заливки из деска — ленивый импорт, чтобы ядро не тянуло
        # веб-слой при старте бота.
        from .web.printerlive import _remember_gcode_settings
        try:
            await _remember_gcode_settings(name, await asyncio.to_thread(plan.gcode.read_bytes))
        except Exception:  # noqa: BLE001
            log.debug("не запомнить настройки допечатки %s", name, exc_info=True)
        _PENDING[int(printer_id or 0)] = PendingStart(
            printer_id=printer_id, request_id=ctx.request_id, label=ctx.label, filename=name,
            start_kw=dict(ctx.start_kw), kept=len(plan.kept), dropped=len(plan.dropped),
            minutes=plan.minutes, grams=plan.grams, at=time.time())
        dropped = ", ".join(p.name + (f" ({p.part_id})" if p.part_id else "") for p in plan.dropped)
        await db.log_event(uid, "допечатка без детали",
                           f"{ctx.label}: печать остановлена, выкинуто {len(plan.dropped)} из "
                           f"{len(plan.kept) + len(plan.dropped)} ({dropped}), "
                           f"осталось {len(plan.kept)} дет. → залито «{name}» "
                           f"~{round(plan.minutes)} мин, ~{plan.grams} г (ждёт старта)")
        log.info("Допечатка %s: %s → %s залито, ждёт пустого стола (осталось %d дет.)",
                 ctx.label, ctx.job_name, name, len(plan.kept))
        return {"ok": True, "filename": name, "stopped": True, "uploaded": name,
                "needs_start": True, **plan.as_dict()}
    finally:
        shutil.rmtree(plan_dir, ignore_errors=True)


async def start_reprint(drv, printer_id: int | None, filename: str, *,
                        uid: int | None = None) -> dict:
    """ШАГ 2 допечатки: запустить залитое задание. Только по ОТДЕЛЬНОМУ подтверждению мастера.

    К этому моменту стол обязан быть пустым: печать идёт с первого слоя и в новой раскладке.
    Проверить это за мастера нельзя ничем (камера показывает стол, но не отвечает за него),
    поэтому проверяем то, что можем: файл — тот самый, который мы залили в этом же процессе."""
    pend = pending_start(printer_id)
    if not pend:
        raise ReprintUnavailable("Задание допечатки не найдено — "
                                 "запустите залитый файл с панели принтера или из списка файлов")
    if not (filename or "").strip():
        # Пустое имя = «запусти что-нибудь залитое»: так стартовать печать вслепую нельзя,
        # фронт обязан прислать ровно то имя, которое ему показали после заливки.
        raise ReprintUnavailable("Не указано, какое задание запускать — откройте диалог заново")
    if filename != pend.filename:
        raise ReprintUnavailable(f"Ожидалось задание «{pend.filename}» — откройте диалог заново")
    check_driver(drv)
    await ensure_idle(drv)              # стол чистит мастер, но чужую печать не топчем
    name = pend.filename
    res = await asyncio.to_thread(lambda: drv.start(name, **dict(pend.start_kw)))
    if not (res or {}).get("ok"):
        return {"ok": False, "uploaded": name,
                "error": (res or {}).get("error") or "Не удалось запустить допечатку"}
    _PENDING.pop(int(printer_id or 0), None)
    try:
        await db.save_print_settings(name, start=dict(pend.start_kw), started=True,
                                     norm_name=elegoo.normalize_job_name(name),
                                     printer_id=printer_id)
        # Привязку СТАВИМ ЗАНОВО, а не патчим: пока шла нарезка (это дольше тика наблюдателя),
        # printwatch мог увидеть остановленный принтер, закрыть прерванную печать и УДАЛИТЬ
        # строку привязки — UPDATE тогда молча обновил бы ноль строк, и новая печать осталась
        # бы без заказа (ни пуша клиенту, ни журнала, ни второй допечатки). set_print_binding
        # пересоздаёт строку и заодно сбрасывает was_active/finished, иначе финиш прошлой
        # печати выключил бы наблюдение за новой.
        await db.set_print_binding(pend.request_id, name, printer_id)
    except Exception:  # noqa: BLE001
        log.warning("допечатка: не обновить привязку/снимок для %s", name, exc_info=True)
    await db.log_event(uid, "допечатка без детали",
                       f"{pend.label}: старт допечатки «{name}» — {pend.kept} дет., "
                       f"~{round(pend.minutes)} мин, ~{pend.grams} г")
    log.info("Допечатка %s: старт %s (%d дет.)", pend.label, name, pend.kept)
    return {"ok": True, "filename": name, "started": True, "kept": pend.kept,
            "minutes": round(pend.minutes), "grams": round(pend.grams, 1)}


async def availability(printer_id: int | None = None, *, drv=None) -> dict:
    """Ответ для интерфейса: можно ли допечатать и, если да, какие детали в задании.

    Недоступность — это не ошибка, а СОСТОЯНИЕ с названной причиной: кнопка не показывается, а
    на её месте одна строка «почему». Поломка разбора (битый .3mf, нет памяти) — тоже состояние:
    500 с голым текстом исключения мастеру ничего не говорит, а в лог мы её кладём.

    `pending` идёт ПЕРЕД гейтами и мимо них: после стопа принтер уже не печатает, то есть все
    гейты закрыты, — а залитое задание как раз ждёт, пока мастер очистит стол и нажмёт «старт».
    Спрятать его значило бы оставить мастера с файлом, о котором интерфейс молчит."""
    out: dict = {"ok": True}
    pend = pending_start(printer_id)
    if pend:
        out["pending"] = {"filename": pend.filename, "label": pend.label, "kept": pend.kept,
                          "dropped": pend.dropped, "minutes": round(pend.minutes),
                          "grams": round(pend.grams, 1)}
    try:
        ctx = await job_context(printer_id, drv=drv)
        parts = await list_parts(ctx)
    except ReprintUnavailable as exc:
        return {**out, "available": False, "reason": str(exc)}
    except Exception:  # noqa: BLE001
        log.warning("допечатка: задание не разобрать", exc_info=True)
        return {**out, "available": False,
                "reason": "Не удалось разобрать задание (см. журнал бота)"}
    return {**out, "available": True, "request_id": ctx.request_id, "label": ctx.label,
            "job_name": ctx.job_name, "printer": ctx.printer_name,
            "parts": [p.as_dict() for p in parts]}
