"""Ядро расчёта печати без привязки к Telegram — общее для бота и мини-апки.

Делает: слайс → цена → цветное превью → запись в историю/коллекцию/заказы. Telegram-
специфичное (сообщения, FSM) остаётся в хендлерах; веб-API вызывает то же ядро и отдаёт JSON.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from . import (
    bambu3mf,
    cache,
    config,
    cpupool,
    db,
    fastcalc,
    geometry,
    inspect3mf,
    materials,
    notify,
    preview,
    pricing,
    slicer,
    slicers,
)
from .bedspec import BedSpec

log = logging.getLogger(__name__)


class CalcError(Exception):
    pass


async def effective_discount(user_id: int | None, qty: int,
                             promo_pct: float | None = 0.0) -> float:
    """Скидка клиента для НОВОГО расчёта: максимум из тиражной, персональной и промокода.

    Единственное место, где это правило ходит в базу. Раньше две строки запросов и max() были
    выписаны дословно четырьмя копиями: здесь (compute) и в трёх ручках app/webapp.py —
    _mw_snapshot, _cache_snapshot, _api_requote. Сам максимум считает чистая функция
    `pricing.effective_discount_pct` (там же объяснено, почему нет клампа 0..100).

    user_id=None (расчёт без клиента) → персональной скидки нет, в базу за ней не ходим.
    promo_pct=None («промокод не вводили») равносилен нулю: у быстрого расчёта по ссылке в чате
    и у заказа за клиента промокода нет и взять его неоткуда — там аргумент просто не передают.

    Пере-расчёт СУЩЕСТВУЮЩЕГО заказа сюда не относится: у него авторитетен снимок разбивки
    (условия сделки заморожены на момент оформления) — это _request_discount ниже."""
    user_pct = await db.get_user_discount(user_id) if user_id else 0.0
    return pricing.effective_discount_pct(await db.best_discount(qty), user_pct, promo_pct)


async def _bambu_filament_resolver():
    """Резолвер материал→профиль филамента ИЗ БАЗЫ БОТА для санации бамбу-3mf.

    Для каждого типа материала берём filament_profile активного пластика этого типа
    (дефолтный — в приоритете). Тип не найден → "" (санитайзер сам возьмёт стандартный Elegoo)."""
    by_type: dict[str, str] = {}
    try:
        for pl in await db.list_plastics(active_only=True):
            prof = (pl.filament_profile or "").strip()
            key = (pl.material_type or "").upper()
            if prof and (key not in by_type or pl.is_default):
                by_type[key] = prof
    except Exception:  # noqa: BLE001
        log.debug("резолвер пластиков из БД не собрался", exc_info=True)

    def resolve(material: str) -> str:
        return by_type.get((material or "").upper(), "")
    return resolve


async def _sanitize_bambu_models(paths: list[Path], *, enabled: bool,
                                 printer=None) -> None:
    """Проекты BambuStudio чиним НА МЕСТЕ перед слайсом (см. app.bambu3mf): подменяем
    принтер/плиту/пластики на Elegoo (пластик — по типу из базы бота), снимаем версионный
    гейт, пересобираем структуру. Сбой на файле не роняет расчёт — оставляем как есть
    (сработает прежний STL-путь).

    enabled — настройка sanitize_3mf (OpenTodo 67dfaeaf, решение владельца 18.07). Выключена
    (дефолт) → .3mf НЕ трогаем вовсе, файл остаётся в исходном виде: правка идёт на месте и
    без бэкапа, а раз слайсинг в клиентских расчётах отключён (slicing_enabled=0), чинить
    файл под слайсер незачем — мастеру нужен оригинал автора. Разбор модели от этого не
    страдает: столы/превью/быстрый расчёт читают оригинальный файл (bambu3mf только
    переписывает конфиг и переупаковывает model_settings, но саму разметку столов берёт из
    оригинала — значит она там уже есть)."""
    if not enabled:
        return
    # НЕ САНИРУЕМ, если резать будет сам Bambu Studio или Orca. Санитайзер подменяет
    # project_settings.config ЦЕЛИКОМ на шаблон Centauri Carbon 2 — для ElegooSlicer это
    # лекарство (иначе бамбу-проект не режется вовсе), а для родного движка это порча
    # авторских настроек, причём необратимая: файл правится на месте и без бэкапа.
    be = slicers.for_printer(printer)
    if not be.wants_bambu_sanitizer:
        log.info("bambu3mf.sanitize пропущен: режем %s, проект остаётся авторским", be.title)
        return
    targets = [p for p in paths if bambu3mf.is_bambu_project(p)]
    if not targets:
        return
    resolver = await _bambu_filament_resolver()
    for p in targets:
        try:
            # backup=False: владелец отказался от .bambu.bak (каталог VK при нужде перескачивается)
            await asyncio.to_thread(bambu3mf.sanitize, p, resolver=resolver, backup=False)
        except Exception:  # noqa: BLE001
            log.warning("bambu3mf.sanitize не удался на %s — оставляю как есть", p, exc_info=True)


async def effective_scale(model_paths: list, printer, scale_pct: int,
                          bed: BedSpec | None = None) -> tuple[int, bool, int | None]:
    """(эффективный масштаб %, был ли кламп, максимум % или None-неизвестен). Пользовательский
    масштаб проверяем против стола принтера: каждая деталь модели должна влезать (XY/высота,
    geometry.scale_limit); просят больше — молча не режем, а «сбрасываем до максимально
    возможного» и говорим об этом (флаг → тост в мини-апке). 100% НЕ трогаем никогда:
    модели крупнее стола и сейчас считаются раскладкой по столам — это не наш случай.

    bed — уже посчитанный стол принтера (compute считает его один раз на расчёт); не задан —
    читаем из профиля машины здесь. Тот же BedSpec уходит третьим аргументом в scale_limit:
    от него зависит и сама ЗАГРУЗКА геометрии (дедуп наложенных копий), а не только порог."""
    scale_pct = max(10, min(1000, int(scale_pct or 100)))
    if scale_pct == 100:
        return 100, False, None
    if bed is None:
        bed = BedSpec.from_machine_profile(getattr(printer, "machine_profile", None))
    lim = await cpupool.run(geometry.scale_limit, [str(p) for p in model_paths],
                            bed.xy_mm, bed.z_mm, bed)
    if lim == float("inf") or lim <= 0:
        return scale_pct, False, None            # геометрия не читается → лимит неизвестен
    max_pct = max(10, int(lim * 100))            # int = floor: на максимуме гарантированно влезает
    if scale_pct > max_pct:
        return max_pct, True, max_pct
    return scale_pct, False, max_pct


# Цветовые слова в именах файлов наборов: авторы мультицветных моделей раскладывают детали по
# цветам («obj_1_(Green) Body.stl», «(White) Eyes.stl») — печатается такой набор ОДНОЦВЕТНЫМИ
# столами (по столу на цвет, без продувки — как mixed_plates, 0bcdd728). Разбор живёт в
# inspect3mf (filename_color_groups): те же группы в том же порядке видят 3D-GLB и превью.
def _filename_color_groups(paths) -> int:
    """Число цветовых групп в именах файлов набора (кейс «Ам Ням», OpenTodo defe5dc0: 9 STL
    «(Green)/(White)/(Black)» — это 3 одноцветных стола, а смета показывала один). 0 — набор
    не размечен цветами (мало помеченных файлов или один цвет): решает вызывающий, обычно
    «столов как было»."""
    return len(set(inspect3mf.filename_color_groups(paths).values()))


async def _estimate_fallback(model_paths, *, plastic, quality, strength, qty: int,
                             extra_colors: int, plates: int, scale: float,
                             printer=None, plates_only=None,
                             bed: BedSpec | None = None) -> "slicer.SliceResult | None":
    """Аналитическая ≈оценка (app.fastcalc, Рычаг Б) в форме SliceResult — фолбэк, когда слайс
    упал/лёг (крэш при занятом VRAM, таймаут). volume_cm3 = объём филамента ОДНОЙ единицы;
    pricing.compute_price дальше умножит его на плотность (граммы), тираж, продувку многоцвета и
    доплату за столы — ровно как результат честного слайса. None → оценить нельзя (формат без
    меша/битый файл), вызывающий тогда даёт честный отказ (заказ без сметы). printer — для
    чтения процесс-профиля (площадь-осознанная модель v2 берёт настройки печати из него).
    bed — стол принтера (app.bedspec): он решает дедуп наложенных копий и гейт мультиплейта в
    замере, то есть граммы и ЦЕНУ. None — fastcalc прочитает стол из printer сам."""
    try:
        est = await fastcalc.estimate(
            model_paths, plastic=plastic, quality=quality, strength=strength, qty=qty,
            extra_colors=extra_colors, plates=plates, settings=None, scale=scale,
            printer=printer, plates_only=plates_only, bed=bed)
    except Exception:  # noqa: BLE001
        log.debug("analytical fallback failed", exc_info=True)
        return None
    if not est:
        return None
    # DFM (B2): по-детальные габариты/прокси толщины тащим на SliceResult, чтобы вердикт
    # печатопригодности мог применить гейт одной детали (n_parts) и мягко предупредить о тонких
    # стенках. На bbox-фолбэке fastcalc отдаёт n_parts=None — вердикт останется консервативным.
    return slicer.SliceResult(volume_cm3=float(est["volume_cm3"]),
                              minutes=float(est["minutes_unit"]), cached=False,
                              n_parts=est.get("n_parts"),
                              min_extent_mm=float(est.get("min_extent_mm") or 0.0),
                              max_part_mm=float(est.get("max_part_mm") or 0.0),
                              wall_proxy_mm=float(est.get("wall_proxy_mm") or 0.0),
                              est_plates=est.get("est_plates"))


def _plastic_groups(plastic, plate_plastics: dict | None, plates: int,
                    plates_only: list[int] | None) -> list[tuple[object, list[int]]]:
    """Группирует ВЫБРАННЫЕ столы по эффективному пластику (свой пластик стола или общий).
    Возвращает [(plastic, [0-базные индексы столов]), ...] в порядке столов."""
    sel = plates_only if plates_only else list(range(max(1, plates)))
    pp = plate_plastics or {}
    groups: dict[int, tuple[object, list[int]]] = {}
    for i in sel:
        p = pp.get(i) or plastic
        groups.setdefault(p.id, (p, []))[1].append(i)
    return list(groups.values())


async def compute(*, user_id: int, model_paths: list, model_label: str, printer, plastic,
                  color: str, qty: int, quality, strength, extra_colors: int = 0,
                  plates: int = 1, is_multicolor: bool = False, base_preview: str | None = None,
                  save: bool = True, promo_pct: float = 0.0,
                  plates_only: list[int] | None = None,
                  plate_plastics: dict | None = None,
                  scale_pct: int = 100, force_slice: bool = False,
                  urgency_factor: float = 1.0,
                  colors_sel: list | None = None) -> dict:
    """Слайсит одну единицу, считает цену, рисует цветное превью. Возвращает данные расчёта.
    save=True — пишет в историю/коллекцию/заказы и инкрементит счётчик пластика.
    plates_only — выбранные столы мультиплейт-модели (0-базные, OpenTodo 77dc9b01).
    plate_plastics — свой пластик на стол {0-базный индекс: Plastic} (OpenTodo 70525c99):
    столы группируются по пластику, каждая группа слайсится СВОИМ филамент-профилем
    (кэшируется отдельно), цена — по граммам каждой группы своей ценой (средневзвешенная
    цена/кг, ровно как в многоцветном пересчёте efca76c3).
    scale_pct — масштаб модели в % (запрос клиента): проверяется против стола принтера и при
    превышении клампится к максимуму (см. effective_scale); фактически применённый масштаб —
    в ответе ("scale_pct" + "scale_clamped")."""
    # macOS-мусор из чужих архивов (AppleDouble ._*, __MACOSX): это не модели, слайсер на них
    # падает («No layers detected», exit -6/-100). Новые распаковки фильтрует downloader,
    # здесь — оборона от уже сохранённых путей (ВК-импорт до фикса 2026-07-11).
    mp = [Path(p) for p in model_paths
          if not Path(p).name.startswith("._") and "__MACOSX" not in str(p)]
    if not mp:
        mp = [Path(p) for p in model_paths]   # всё «мусор»? не решаем за юзера, пусть падает явно
    # Дубль модели в карточке: полный .3mf + те же столы россыпью «Plate N.stl» → вес/цена ×2
    # (Фалкон/Валли, фидбэк 24.07). Считаем по полному 3mf; карточка/БД не меняются.
    mp = [Path(p) for p in inspect3mf.drop_redundant_plate_stls(mp)]
    settings = await db.get_settings()
    # Бамбу-3mf → валидный Elegoo-проект (перед слайсом), если мастер это разрешил (67dfaeaf).
    # force_slice санирует ВСЕГДА, независимо от настройки: это явные админ-инструменты
    # («слайсни по ссылке»), которым нужен НАСТОЯЩИЙ слайс — ровно так же, как use_slicer ниже
    # уважает force_slice. Иначе единственный оставшийся потребитель слайсера получал бы
    # несанированный бамбу-проект, слайс падал бы и молча подменялся ≈оценкой.
    await _sanitize_bambu_models(
        mp, enabled=force_slice or bool(settings.get("sanitize_3mf")), printer=printer)
    # Стол ЭТОГО принтера — один раз на расчёт (разбор профиля машины лезет на диск, ~17 мс, и
    # ниже он нужен и клампу масштаба, и каждой группе столов). Стол решает дедуп наложенных
    # копий и гейт мультиплейта в замере (app.bedspec) — то есть объём, число деталей и ЦЕНУ:
    # раньше все принтеры считались зашитым столом CC2. Профиль не читается → DEFAULT_BED,
    # прежнее поведение.
    bed = await asyncio.to_thread(BedSpec.from_machine_profile,
                                  getattr(printer, "machine_profile", None))
    scale_pct, scale_clamped, scale_max = await effective_scale(mp, printer, scale_pct, bed)
    scale = scale_pct / 100.0
    groups = _plastic_groups(plastic, plate_plastics, plates, plates_only)
    mixed = len(groups) > 1
    if not mixed and groups:
        plastic = groups[0][0]   # все выбранные столы на одном пластике → считаем им целиком

    def _filament(p) -> str:
        return materials.resolve_filament(
            printer.filament_dir, p.material_type,
            fallback=p.filament_profile or str(config.DEFAULT_FILAMENT_PROFILE),
            prefer=p.filament_profile)     # откалиброванный профиль важнее автоподбора

    # Машинный профиль ПОД СОПЛО пресета качества (OpenTodo f71f4973): 0.30/0.36 идут
    # процессами сопла 0.6, и резать их профилем 0.4 нельзя — ширины линий и расход
    # считались бы по чужому диаметру. Профиля под сопло нет → прежний профиль принтера.
    machine = await db.machine_profile_for(printer, db.nozzle_of(quality))

    async def _slice(p, idxs: list[int] | None):
        return await slicer.slice_unit(
            mp, machine_profile=machine, process_dir=printer.process_dir,
            base_process_name=quality.process, filament_profile=_filament(p),
            layer_height=quality.layer_height, infill_pct=strength.infill, walls=strength.walls,
            plates_only=idxs, scale=scale, bed=bed,
            # чем резать задания ЭТОЙ машины: у Bambu свои профили, и резать их ElegooSlicer'ом
            # значит считать клиенту цену по чужой машине
            slicer_kind=getattr(printer, "slicer", "") or "",
            slicer_path=getattr(printer, "slicer_path", "") or "")

    plastic_grams: list[dict] = []   # разбивка по пластикам для склада/заказа (только mixed)
    estimated = False                # True → цена от аналитической ≈оценки, см. ниже
    # Честный слайс в клиентских расчётах выключаем настройкой slicing_enabled (решение
    # владельца 17.07: «пока что вовсе убрать слайсинг — админ по факту печати внесёт все
    # данные»). 0 (дефолт) → только быстрый расчёт (approx, мастер уточнит); 1 — прежнее
    # поведение. force_slice=True — явные админ-инструменты («слайсни по ссылке») слайсят
    # всегда, независимо от настройки.
    use_slicer = force_slice or bool(settings.get("slicing_enabled"))
    try:
        if not use_slicer:
            # Только быстрый расчёт: та же аналитика, что и фолбэк при упавшем слайсере,
            # но как ОСНОВНОЙ путь. Выбор столов/свой пластик стола уважаем per-plate
            # замером (fastcalc plates_only) — группы считаются так же, как слайсились бы.
            if mixed:
                parts = []
                for gp, idxs in groups:
                    est = await _estimate_fallback(
                        mp, plastic=gp, quality=quality, strength=strength, qty=qty,
                        extra_colors=extra_colors, plates=plates, scale=scale,
                        printer=printer, plates_only=idxs, bed=bed)
                    if est is None:
                        raise CalcError("Не удалось оценить модель (нечитаемая геометрия).")
                    parts.append(est)
                result = slicer.SliceResult(
                    volume_cm3=sum(r.volume_cm3 for r in parts),
                    minutes=sum(r.minutes for r in parts), cached=False)
                for (gp, idxs), r in zip(groups, parts):
                    plastic_grams.append({"plastic_id": gp.id, "name": gp.name, "plates": idxs,
                                          "grams_unit": r.volume_cm3 * gp.density,
                                          "price_per_kg": gp.price_per_gram * 1000.0})
            else:
                est = await _estimate_fallback(
                    mp, plastic=plastic, quality=quality, strength=strength, qty=qty,
                    extra_colors=extra_colors, plates=plates, scale=scale,
                    printer=printer, plates_only=plates_only, bed=bed)
                if est is None:
                    raise CalcError("Не удалось оценить модель (нечитаемая геометрия).")
                result = est
            estimated = True
        elif mixed:
            # Каждая группа столов — отдельный слайс своим профилем; группы параллельно
            # (внутри run_slice общий семафор MAX_CONCURRENT_SLICES, лишнего не запустим).
            parts = await asyncio.gather(*(_slice(p, idxs) for p, idxs in groups))
            result = slicer.SliceResult(
                volume_cm3=sum(r.volume_cm3 for r in parts),
                minutes=sum(r.minutes for r in parts),
                cached=all(r.cached for r in parts))
            for (p, idxs), r in zip(groups, parts):
                plastic_grams.append({"plastic_id": p.id, "name": p.name, "plates": idxs,
                                      "grams_unit": r.volume_cm3 * p.density,
                                      # эффективная цена (с коэффициентом сложности пластика) —
                                      # ниже из неё считается средневзвешенная цена расчёта
                                      "price_per_kg": p.price_per_gram * 1000.0})
        else:
            result = await _slice(plastic, plates_only)
    except CalcError:
        raise                                  # честный отказ fast-only ветки — без обёртки
    except slicer.SlicerError as exc:
        # Слайс не удался: крэш слайсера при занятом VRAM (slicer._looks_like_crash), таймаут или
        # нулевой объём. Вместо тупика — БЫСТРАЯ аналитическая ≈оценка (Рычаг Б), помеченная
        # approx («предварительно, мастер уточнит»): koala-класс и «слайсер лёг» всё равно дают
        # клиенту авто-цену, которую мастер поправит. Оценить нельзя (формат без меша/битый
        # файл) → прежний честный отказ (заказ без сметы). Оценка одно-пластиковая, поэтому
        # цену считаем обычной (не mixed) ветвью ниже.
        est = await _estimate_fallback(
            mp, plastic=plastic, quality=quality, strength=strength, qty=qty,
            extra_colors=extra_colors, plates=plates, scale=scale, printer=printer, bed=bed)
        if est is None:
            raise CalcError(str(exc)) from exc
        log.warning("Слайс не удался (%s) — отдаю аналитическую ≈оценку (Рычаг Б)", exc)
        result = est
        estimated = True
        mixed = False
        plastic_grams = []
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected slicing failure")
        await notify.alert(f"Сбой расчёта (слайсинг): {exc}", key="calc_slice_fail")
        raise CalcError(f"Внутренняя ошибка: {exc}") from exc

    # Раскладка набора по столам (OpenTodo defe5dc0, кейс «Ам Ням»): у модели нет родных столов
    # (STL-набор/одиночный файл, plates<=1), но печататься одним столом она не будет — либо
    # детали геометрически не влезают на один стол (est_plates полочной укладки fastmeasure),
    # либо автор разбил набор по цветам в именах файлов («(Green) Body» + «(White) Eyes» →
    # одноцветный стол на цвет, печать без продувки — как mixed_plates, 0bcdd728). Дальше это
    # честное число столов уходит в цену (plate_fee×N), ответ расчёта, заказ и коллекцию
    # (бейдж ×N на плитке). Надбавку multicolor_* НЕ добавляем: столы одноцветные. Родные столы
    # .3mf (plates>1) авторитетны — их не трогаем, как и выбор столов (plates_only — 3mf-функция).
    if int(plates or 1) <= 1 and not plates_only:
        lay_pl = int(getattr(result, "est_plates", 0) or 0)
        col_pl = _filename_color_groups(mp) if len(mp) > 1 else 0
        eff_pl = max(lay_pl, col_pl)
        if eff_pl > 1:
            plates = eff_pl
            log.info("расчёт «%s»: набор разложен на %d стола(ов) (укладка=%s, цвета имён=%s)",
                     model_label, plates, lay_pl or "-", col_pl or "-")

    discount = await effective_discount(user_id, qty, promo_pct)
    # Этап 5 (мастер-план): коррекция оценки времени по накопленным парам оценка/факт — ТОЛЬКО
    # если мастер включил настройку time_prediction И механизм «проснулся» (набралась выборка,
    # db.predict_minutes с гейтом). По умолчанию выкл → минуты слайсера как есть, цена не меняется.
    # Коэффициент времени машины (B1, трек B roadmap): слайсер/fastcalc откалиброваны по
    # CC2, у другой машины мастер задаёт множитель на карточке. Применяем ДО predict_minutes:
    # в журнал печатей оценка уходит уже умноженной, и коррекция по журналу учит остаток,
    # а не складывается с множителем второй раз.
    tf = float(getattr(printer, "time_factor", 1.0) or 1.0)
    eff_minutes = result.minutes * tf
    if settings.get("time_prediction"):
        try:
            pred = await db.predict_minutes(
                eff_minutes, None if mixed else getattr(plastic, "material_type", None),
                printer_id=getattr(printer, "id", None))
            eff_minutes = pred.get("predicted") or eff_minutes
        except Exception:  # noqa: BLE001
            log.debug("time prediction failed", exc_info=True)
    if mixed:
        # Средневзвешенная цена/кг по граммам групп (= Σ грамм×цена) + общий грамм-итог; брак —
        # индивидуальный, только если у всех пластиков совпадает (та же логика, что у многоцвета).
        grams_unit = sum(g["grams_unit"] for g in plastic_grams)
        if grams_unit <= 0:
            raise CalcError("Слайсер вернул нулевой объём — проверьте модель.")
        weighted_kg = sum(g["grams_unit"] * g["price_per_kg"] for g in plastic_grams) / grams_unit
        fr_set = {gp.failure_rate_pct for gp, _ in groups}
        synth = SimpleNamespace(density=1.0, price_per_gram=weighted_kg / 1000.0,
                                failure_rate_pct=fr_set.pop() if len(fr_set) == 1 else None)
        bd = pricing.compute_price(grams_unit, eff_minutes, qty, synth,  # type: ignore[arg-type]
                                   settings, discount_pct=discount, extra_colors=extra_colors,
                                   # первый аргумент — граммы (density=1), настоящий объём для
                                   # коэффициента «большой модели» передаём отдельно
                                   volume_cm3_for_factor=result.volume_cm3, plates=plates,
                                   urgency_factor=urgency_factor)
    else:
        bd = pricing.compute_price(result.volume_cm3, eff_minutes, qty, plastic, settings,
                                   discount_pct=discount, extra_colors=extra_colors, plates=plates,
                                   urgency_factor=urgency_factor)

    # цветное превью (цвет/градиент пластика); цвет не распознан → базовый
    pv_path = base_preview
    overrides = await db.get_color_overrides()
    color_rgbs = materials.color_to_rgbs(color, overrides)
    cp = None
    # Мультицвет красим ПО ДЕТАЛЯМ (слоты = экструдеры .3mf / покраска граней / цветовые
    # группы имён — тот же geometry.part_color_context, что у 3D-GLB) вместо прежнего
    # «градиента по высоте» на весь меш из склейки имён цветов через « - » (заказ выглядел
    # одинаковым градиентом на всех моделях, OpenTodo 18ddd6c8). colors_sel — выбранные
    # клиентом цвета слотов; пусто → дизайн-цвета модели. Слоты не нашлись → прежний путь.
    if is_multicolor:
        slot_rgbs = [materials.color_to_rgb(c, overrides) or (0.6, 0.64, 0.68)
                     for c in (colors_sel or [])]
        try:
            # стол — ТОТ ЖЕ, что у сметы выше: иначе картинка судит о копиях по CC2,
            # а смета по реальному принтеру, и слоты цветов лягут на разные детали
            cp = await cpupool.run_render(preview.render_multicolor_preview, mp,
                                          slot_rgbs or None, bed=bed)
        except Exception:  # noqa: BLE001
            log.debug("multicolor preview failed", exc_info=True)
    try:
        if not (cp and Path(cp).exists()):
            cp = await cpupool.run_render(preview.render_preview, mp, color_rgbs or None, bed=bed)
        if cp and Path(cp).exists():
            pv_path = str(cp)
    except Exception:  # noqa: BLE001
        log.debug("colored preview failed", exc_info=True)
    # Фолбэк для тяжёлых моделей: рендер вернул None ИЛИ воркер умер по памяти (напр. 107 МБ
    # 3mf, пик ~49 ГБ). Берём готовый thumbnail из самого 3mf, чтобы карточка не осталась без
    # обложки (иначе /img/{id} → 404, как было у collection.id=70). Проверка на верхнем уровне,
    # а не внутри render_preview, — чтобы сработать даже если воркер рендера убит по OOM.
    if not (pv_path and Path(pv_path).exists()):
        try:
            tp = await cpupool.run(preview.extract_3mf_thumbnail, mp)
            if tp and Path(tp).exists():
                pv_path = str(tp)
        except Exception:  # noqa: BLE001
            log.debug("3mf thumbnail fallback failed", exc_info=True)

    mat_name = " + ".join(g["name"] for g in plastic_grams) if mixed else plastic.name
    pdisp = f"{mat_name} · {color}" if color else mat_name
    dims, gsize = await cpupool.run(preview.model_dims, mp, scale, bed=bed)

    if save:
        try:
            if mixed:
                for gp, _idxs in groups:
                    await db.increment_plastic_uses(gp.id)
            else:
                await db.increment_plastic_uses(plastic.id)
        except Exception:  # noqa: BLE001
            pass
        try:
            mhash = cache.model_hash(model_paths)
            await db.upsert_saved_model(
                user_id=user_id, hash=mhash, label=model_label, paths=model_paths,
                preview_path=pv_path, printer_id=printer.id, plastic_id=plastic.id,
                quality_id=quality.id, strength_id=strength.id, quantity=qty,
                multicolor=int(is_multicolor), plates=plates)
            # Коллекция канонична в 100%: расчёт с масштабом не должен подменять габариты и
            # кэш цены «от N» на плитке (иначе после расчёта ×3 плитка врёт всем клиентам).
            cdims, csize = (dims, gsize) if scale_pct == 100 else \
                await cpupool.run(preview.model_dims, mp, bed=bed)
            await db.add_to_collection(
                hash=mhash, label=model_label, paths=model_paths, preview_path=pv_path,
                added_by=user_id, dims=cdims, size=csize, material=mat_name,
                multicolor=int(is_multicolor), plates=plates,
                # кэш цены за 1 шт — «от N» на плитке коллекции (ux-simplification.md, идея 3);
                # аналитическую ≈оценку в плитку НЕ кэшируем (не выдаём приблизительное за факт)
                price=round(bd.total / max(1, qty), 2)
                if (scale_pct == 100 and not estimated) else 0.0)
        except Exception:  # noqa: BLE001
            log.debug("save history/collection failed", exc_info=True)
        try:
            # для смешанных пластиков история хранит доминирующий по граммам
            order_pid = (max(plastic_grams, key=lambda g: g["grams_unit"])["plastic_id"]
                         if mixed else plastic.id)
            await db.add_order(
                user_id=user_id, model_label=model_label, quantity=qty, quality=quality.label,
                strength=strength.label, plastic_id=order_pid, printer=printer.name,
                grams_total=bd.grams_total, minutes_total=bd.minutes_total, total_price=bd.total)
        except Exception:  # noqa: BLE001
            log.debug("add_order failed", exc_info=True)

    # Пер-цветовые граммы (по экструдерам) для разбивки списания склада многоцветного заказа.
    # Объём филамента каждого экструдера × плотность; пусто если слайсер не дал разбивку (фолбэк
    # на списание всего объёма с первого цвета). Сами цены/итоги это НЕ меняет — только склад.
    # При смешанных пластиках пер-экструдерной разбивки нет (слайсы раздельные) — склад
    # спишется по-пластиково через plastic_grams.
    color_grams = [] if mixed else [v * plastic.density for v in (result.volumes_cm3 or [])]

    # DFM (B2): по-детальные габариты/прокси толщины для вердикта печатопригодности мини-аппа.
    # Есть только на аналитическом пути (result — SliceResult из fastcalc); честный слайс/mixed
    # их не заполняют (n_parts=None) → вердикт останется на консервативной проверке габарита.
    dfm_meas = {"n_parts": getattr(result, "n_parts", None),
                "min_extent_mm": float(getattr(result, "min_extent_mm", 0.0) or 0.0),
                "max_part_mm": float(getattr(result, "max_part_mm", 0.0) or 0.0),
                "wall_proxy_mm": float(getattr(result, "wall_proxy_mm", 0.0) or 0.0)}
    return {"bd": bd, "settings": settings, "preview_path": pv_path,
            "cached": result.cached, "pdisp": pdisp, "plates": plates, "dims": dims,
            "color_grams": color_grams, "plastic_grams": plastic_grams,
            "scale_pct": scale_pct, "scale_clamped": scale_clamped, "scale_max_pct": scale_max,
            "dfm": dfm_meas,
            # аналитическая ≈оценка вместо честного слайса (слайсер лёг): вызывающий помечает
            # смету approx («~», «мастер уточнит») и не кэширует её как результат слайса
            "estimated": estimated}


# --- Пересчёт существующего заказа (админ-кнопка «Пересчитать») -----------------------------
# Заказ хранит принтер/пластик как имена, качество/прочность — как label (+ id, если есть).
# Восстанавливаем объекты пресетов, повторно слайсим тем же путём, что и первичный расчёт.

async def _resolve_printer(name: str):
    for p in await db.list_printers(active_only=False):
        if p.name == name:
            return p
    return await db.get_default_printer()


async def _resolve_plastic(plastic_name: str):
    # в заказе plastic_name = «<имя пластика> · <цвет>»; матчим по имени-префиксу до « · »
    raw = (plastic_name or "").strip()
    base = raw.split(" · ")[0].strip()
    plist = await db.list_plastics(active_only=False)
    for p in plist:
        if p.name == base or (raw and raw.startswith(p.name)):
            return p
    return None


async def _resolve_quality(r):
    if getattr(r, "quality_id", None):
        q = await db.get_quality(r.quality_id)
        if q:
            return q
    for q in await db.list_quality(active_only=False):   # фолбэк по label (старые заказы без id)
        if q.label == r.quality:
            return q
    return None


async def _resolve_strength(r):
    if getattr(r, "strength_id", None):
        s = await db.get_strength(r.strength_id)
        if s:
            return s
    for s in await db.list_strength(active_only=False):
        if s.label == r.strength:
            return s
    return None


async def recalc_request(r) -> dict:
    """Повторный слайс + цена существующего заказа БЕЗ сохранения. Те же параметры, что при
    оформлении (принтер/пластик/качество/прочность/тираж); скидку и доп.цвета берём из снимка
    разбивки заказа, чтобы условия сделки не «поплыли». Возвращает PriceBreakdown (new)."""
    if not r.file_path or not Path(r.file_path).exists():
        raise CalcError("Файл модели не найден — пересчёт невозможен.")
    printer = await _resolve_printer(r.printer)
    plastic = await _resolve_plastic(r.plastic_name)
    quality = await _resolve_quality(r)
    strength = await _resolve_strength(r)
    if not (printer and plastic and quality and strength):
        raise CalcError("Не удалось восстановить параметры заказа (принтер/пластик/качество/прочность).")
    filament = materials.resolve_filament(
        printer.filament_dir, plastic.material_type,
        fallback=plastic.filament_profile or str(config.DEFAULT_FILAMENT_PROFILE),
        prefer=plastic.filament_profile)
    settings = await db.get_settings()
    # бамбу-3mf → Elegoo (перед пересчётом), если мастер это разрешил (67dfaeaf)
    await _sanitize_bambu_models([Path(r.file_path)], enabled=bool(settings.get("sanitize_3mf")),
                                 printer=printer)
    if not settings.get("slicing_enabled"):
        # слайсинг в расчётах выключен (17.07) → пересчёт той же быстрой аналитикой
        result = await _estimate_fallback(
            [Path(r.file_path)], plastic=plastic, quality=quality, strength=strength,
            qty=max(1, int(r.quantity or 1)), extra_colors=0,
            plates=int(getattr(r, "plates", 1) or 1),
            scale=_snapshot_scale_pct(r) / 100.0, printer=printer)
        if result is None:
            raise CalcError("Не удалось оценить модель (нечитаемая геометрия).")
    else:
        try:
            result = await slicer.slice_unit(
                [Path(r.file_path)],
                machine_profile=await db.machine_profile_for(printer, db.nozzle_of(quality)),
                process_dir=printer.process_dir,
                base_process_name=quality.process, filament_profile=filament,
                layer_height=quality.layer_height, infill_pct=strength.infill,
                walls=strength.walls,
                # стол принтера — тот же, что у быстрого расчёта: иначе честный слайс судит
                # по CC2, а смета по реальному столу, и два ценовых пути разъезжаются
                bed=BedSpec.from_machine_profile(getattr(printer, "machine_profile", None)),
                slicer_kind=getattr(printer, "slicer", "") or "",
                slicer_path=getattr(printer, "slicer_path", "") or "",
                scale=_snapshot_scale_pct(r) / 100.0)   # масштаб заказа — из снимка разбивки
        except slicer.SlicerError as exc:
            raise CalcError(str(exc)) from exc
    bd = (r.breakdown or {})
    extra_colors = int(bd.get("extra_colors", 0) or 0)
    discount = float(bd.get("discount_pct", 0.0) or 0.0)
    # Срочность заказа (B3): множитель из снимка разбивки, а у order_now со сбоем слайса (снимка
    # нет) — из колонки заказа, чтобы пересчёт мастера НЕ ронял срочную наценку живого заказа
    # (старые заказы без поля/колонки → 1.0, обычная цена).
    urgency_factor = _request_urgency_factor(r, settings)
    qty = max(1, int(r.quantity or 1))
    breakdown = pricing.compute_price(result.volume_cm3, result.minutes, qty, plastic, settings,
                                      discount_pct=discount, extra_colors=extra_colors,
                                      plates=int(getattr(r, "plates", 1) or 1),
                                      urgency_factor=urgency_factor)
    # Опции постобработки (B4): восстанавливаем ПЛОСКУЮ доплату услуг мастера из снимка разбивки
    # заказа — как discount_pct/urgency_factor выше, чтобы пересчёт мастера не терял услугу и её
    # цену (иначе итог перестанет сходиться со строками сметы/PDF/карточки + утечка выручки). Читаем
    # name+price из снимка заказа (не из живого каталога) — правки каталога не переписывают историю.
    pricing.apply_options(breakdown, bd.get("options"))
    return {"bd": breakdown, "settings": settings}


def _snapshot_scale_pct(r) -> int:
    """Масштаб заказа из снимка разбивки (breakdown.scale_pct); старые заказы без ключа → 100."""
    try:
        return max(10, min(1000, int((r.breakdown or {}).get("scale_pct", 100) or 100)))
    except (TypeError, ValueError):
        return 100


async def recompute_request(r, *, plastic_id: int, quality_id: int, strength_id: int,
                            qty: int, color: str = "") -> dict:
    """Пересчёт заказа с НОВЫМИ параметрами — клиент меняет пластик/цвет/качество/прочность/тираж
    до взятия в работу (OpenTodo 42ce4724). Слайсит файл заказа новыми качеством/прочностью и
    считает цену под новый пластик/тираж. save=False — историю/коллекцию не трогаем (заказ уже
    есть). Доп.цвета берём из снимка разбивки. Возвращает данные для обновления заказа."""
    if not r.file_path or not Path(r.file_path).exists():
        raise CalcError("Файл модели не найден — изменить параметры нельзя.")
    printer = await _resolve_printer(r.printer)
    plastic = await db.get_plastic(plastic_id)
    quality = await db.get_quality(quality_id)
    strength = await db.get_strength(strength_id)
    if not (printer and plastic and quality and strength):
        raise CalcError("Проверьте выбор параметров.")
    qty = max(1, min(1000, int(qty or 1)))
    extra_colors = int((r.breakdown or {}).get("extra_colors", 0) or 0)
    # срочность заказа (B3): из снимка разбивки, а у order_now со сбоем слайса (снимка нет) —
    # из колонки заказа, чтобы смена параметров не сбросила срочную наценку
    urgency_factor = _request_urgency_factor(r, await db.get_settings())
    res = await compute(
        user_id=r.user_id, model_paths=[r.file_path], model_label=r.model_label, printer=printer,
        plastic=plastic, color=(color or "").strip(), qty=qty, quality=quality, strength=strength,
        extra_colors=extra_colors, plates=int(getattr(r, "plates", 1) or 1),
        is_multicolor=bool(getattr(r, "multicolor", 0)), save=False,
        scale_pct=_snapshot_scale_pct(r),   # смена пластика/качества не сбрасывает масштаб заказа
        urgency_factor=urgency_factor)
    # Опции постобработки (B4): re-apply из снимка разбивки заказа поверх нового расчёта. Услуги
    # ПЛОСКИЕ на заказ — смена тиража/пластика их НЕ масштабирует (apply_options просто складывает
    # цены снимка), поэтому применяем ПОСЛЕ compute к готовому bd, а не через qty. Снимок = name+price
    # заказа, живой каталог историю не переписывает (симметрично discount_pct/urgency_factor выше).
    pricing.apply_options(res["bd"], (r.breakdown or {}).get("options"))
    return {"bd": res["bd"], "settings": res["settings"], "pdisp": res["pdisp"],
            "dims": res.get("dims", ""), "preview_path": res["preview_path"],
            "quality": quality, "strength": strength, "qty": qty}


async def _request_discount(r, qty: int, user_id: int | None) -> float:
    """Скидка для пере-расчёта заказа: из снимка разбивки (чтобы условия сделки не поплыли),
    а без снимка — как у обычного заказа: max(тираж, персональная клиента)."""
    if r.breakdown and "discount_pct" in r.breakdown:
        return float(r.breakdown.get("discount_pct", 0.0) or 0.0)
    uid = user_id if user_id is not None else getattr(r, "user_id", None)
    user_discount = await db.get_user_discount(uid) if uid else 0.0
    return max(await db.best_discount(qty), user_discount)


def _request_urgency_factor(r, settings) -> float:
    """Множитель срочности для пере-расчёта заказа (B3): авторитетно — из снимка разбивки
    (условия сделки заморожены на момент оформления). Но у заказа order_now со сбоем слайса
    снимка нет (breakdown пуст), а колонка urgency='rush' — тогда выводим множитель из колонки
    по текущим настройкам, иначе ручной пересчёт мастера потерял бы срочную наценку заказа."""
    uf = (r.breakdown or {}).get("urgency_factor")
    if uf:
        return float(uf or 1.0)
    return pricing.urgency_factor_from(getattr(r, "urgency", "") or "", settings)


async def reprice_request_colors(r, colors: list[tuple], minutes_total: float,
                                 user_id: int | None = None) -> dict:
    """Цена индивидуального заказа в МНОГОЦВЕТНОЙ печати (OpenTodo efca76c3): мастер вводит
    граммы ПО КАЖДОМУ цвету (из своего слайсера — продувка/башня уже в них) + общее время.

    colors = [(plastic, grams_total), ...] (≥2). Материал считаем по каждому пластику своей
    ценой (через средневзвешенную цену за кг — математически ровно Σ грамм×цена), время/брак/
    наценка/скидка — той же формулой, что у обычного заказа. Надбавки multicolor_waste/time_pct
    НЕ добавляем: введённые числа — реальные итоги многоцветного слайса, добавка задвоила бы их.
    Брак: у всех цветов один пластик с инд. %% — берём его, иначе общий процент."""
    total_grams = sum(g for _, g in colors)
    if total_grams <= 0:
        raise CalcError("Граммы по цветам должны быть больше нуля.")
    weighted_kg = sum(p.price_per_gram * 1000.0 * g for p, g in colors) / total_grams
    fr_set = {p.failure_rate_pct for p, _ in colors}
    failure_rate = fr_set.pop() if len(fr_set) == 1 else None
    settings = await db.get_settings()
    qty = max(1, int(r.quantity or 1))
    discount = await _request_discount(r, qty, user_id)
    # объём единицы для коэффициента «большой модели» — граммы каждого цвета через его плотность
    vol_unit = sum((g / p.density) for p, g in colors if p.density) / qty
    # срочность заказа (B3): из снимка разбивки, а без снимка — из колонки заказа, чтобы
    # пересчёт по цветам не сбросил срочную наценку
    urgency_factor = _request_urgency_factor(r, settings)
    bd = pricing.compute_price_manual(
        max(0.0, float(total_grams)) / qty, max(0.0, float(minutes_total)) / qty,
        weighted_kg, settings, qty=qty, discount_pct=discount, failure_rate_pct=failure_rate,
        volume_cm3_unit=vol_unit, plates=int(getattr(r, "plates", 1) or 1),
        urgency_factor=urgency_factor)
    bd.extra_colors = max(0, len(colors) - 1)   # для подписи «многоцветная (~N цв.)» в смете/PDF
    # Опции постобработки (B4): re-apply плоскую доплату услуг из снимка разбивки заказа поверх
    # многоцветного пересчёта — иначе покраска/вставки молча пропадут при пересчёте по цветам, а
    # тег услуги у мастера и request_options останутся. Снимок = name+price заказа (не живой каталог).
    pricing.apply_options(bd, (r.breakdown or {}).get("options"))
    return {"bd": bd, "settings": settings}


async def reprice_request(r, grams_total: float, minutes_total: float,
                          user_id: int | None = None) -> dict:
    """Цена по ГОТОВЫМ итоговым граммам/минутам (правка админа в форме пересчёта), без слайса.
    Граммы трактуем как финальные (продувка многоцвета уже в них), поэтому считаем «вручную»
    (compute_price_manual). Та же формула/округление.

    Скидку: при наличии снимка разбивки (повторный пересчёт) берём из него, чтобы условия сделки
    не «поплыли». Для индивидуального заказа без разбивки (первичный счёт) считаем скидку как у
    обычного заказа — max(тираж, персональная клиента), — иначе счёт игнорировал бы скидки."""
    plastic = await _resolve_plastic(r.plastic_name)
    if not plastic:
        raise CalcError("Пластик заказа не найден.")
    settings = await db.get_settings()
    qty = max(1, int(r.quantity or 1))
    discount = await _request_discount(r, qty, user_id)
    grams_unit = max(0.0, float(grams_total)) / qty
    # срочность заказа (B3): из снимка разбивки, а без снимка — из колонки заказа, чтобы
    # ручная правка цены не сбросила срочную наценку
    urgency_factor = _request_urgency_factor(r, settings)
    breakdown = pricing.compute_price_manual(
        grams_unit, max(0.0, float(minutes_total)) / qty,
        plastic.price_per_gram * 1000.0, settings, qty=qty, discount_pct=discount,
        failure_rate_pct=plastic.failure_rate_pct,
        volume_cm3_unit=(grams_unit / plastic.density) if plastic.density else None,
        plates=int(getattr(r, "plates", 1) or 1), urgency_factor=urgency_factor)
    # Опции постобработки (B4): re-apply плоскую доплату услуг из снимка разбивки заказа поверх
    # ручной правки цены — иначе услуга исчезнет из stored price и разбивки, а тег/request_options
    # останутся (утечка выручки). Снимок = name+price заказа, живой каталог историю не переписывает.
    pricing.apply_options(breakdown, (r.breakdown or {}).get("options"))
    return {"bd": breakdown, "settings": settings}
