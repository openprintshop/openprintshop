"""Расчёт стоимости печати по результату слайсинга и параметрам админа."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from types import SimpleNamespace

from .db import Plastic

_ROUND_KOPECKS = 10   # итог округляем вверх до этого шага копеек (10 коп.)


def ceil10(value: float) -> float:
    """Итог ВВЕРХ до 10 копеек — единственная копия правила «круглого итога».

    Формула жила тремя дословными копиями (compute_price и два места в apply_options), и
    разъехаться им было нечем помешать. Сначала переводим в целые копейки через round() —
    иначе двоичный «хвост» вида 12.299999999 округлился бы вверх лишним гривенником.

    JS-копия того же правила живёт в мини-аппе (app/webui/app.js, ceil10) и обязана
    оставаться бит-в-бит совместимой: смета считает опции локально при тапе."""
    kopecks = int(round(value * 100))
    return (-(-kopecks // _ROUND_KOPECKS) * _ROUND_KOPECKS) / 100.0


def effective_discount_pct(tier_pct: float, user_pct: float, promo_pct: float | None = 0.0) -> float:
    """Эффективная скидка клиента = максимум из тиражной, персональной и промокода.

    Правило было выписано дословно четырьмя копиями (calcservice.compute и три ручки webapp:
    _mw_snapshot, _cache_snapshot, _api_requote) — теперь одно.

    Клампа 0..100 здесь СОЗНАТЕЛЬНО нет. Тиражную ступень и промокод админка пишет в базу без
    верхней границы (webapp: action='add_tier' и action='add'), поэтому кламп означал бы тихую
    смену уже действующих условий, а не рефакторинг. promo_pct=None («промокода не вводили»)
    равносилен нулю — три из четырёх снятых копий писали ровно `promo_pct or 0.0`."""
    return max(float(tier_pct), float(user_pct), float(promo_pct or 0.0))


def volume_factor(volume_cm3_unit: float, settings: dict[str, float]) -> float:
    """Коэффициент «большой модели»: крупную вещь тяжелее печатать (адгезия, риск дорогого
    брака, снятие/обработка), поэтому расходники дорожают на volume_surcharge_pct за каждый
    полный порог объёма сверх первого. Линейный и непрерывный (ровно на пороге = 1.0):
    порог 200 см³ и 15% → 400 см³ = ×1.15, 600 см³ = ×1.30. 0/пусто в настройках = выключено."""
    thr = float(settings.get("volume_threshold_cm3", 0.0) or 0.0)
    pct = float(settings.get("volume_surcharge_pct", 0.0) or 0.0)
    if thr <= 0 or pct <= 0 or volume_cm3_unit <= thr:
        return 1.0
    return 1.0 + (pct / 100.0) * (volume_cm3_unit / thr - 1.0)


@dataclass
class PriceBreakdown:
    qty: int
    grams_unit: float
    minutes_unit: float
    grams_total: float
    minutes_total: float
    filament_cost: float      # филамент на весь тираж
    machine_cost: float       # машинное время на весь тираж
    waste_cost: float         # буфер на брак
    setup_fee: float          # фикс. подготовка
    margin: float             # наценка (деньги)
    discount_pct: float       # скидка за тираж, %
    discount_amount: float    # скидка (деньги)
    subtotal: float           # до наценки
    total: float              # итог
    min_applied: bool         # сработала ли минимальная цена
    purge_grams_unit: float = 0.0   # филамент в отходы на смены цвета (на 1 шт)
    extra_minutes_unit: float = 0.0  # доп. время на смены цвета (на 1 шт)
    extra_colors: int = 0            # доп. цветов сверх одного (0 = одноцветная)
    failure_rate_pct: float = 0.0    # фактически применённый % брака (индивид. пластика или общий)
    volume_factor: float = 1.0       # применённый коэффициент «большой модели» (1.0 = не было)
    plate_fee: float = 0.0           # доплата за столы проекта (ставка × число столов), спрятана в machine_cost
    urgency_factor: float = 1.0      # множитель срочности (B3): 1.0 = обычная печать, >1 = срочно
    urgency_surcharge: float = 0.0   # наценка за срочность (деньги); входит в total отдельной строкой
    options_cost: float = 0.0        # опции постобработки (B4): плоская сумма услуг сверху, в total
    # снимок выбранных опций [{id, name, price}] — для строк расчётника и данных-сейфа заказа
    options: list = field(default_factory=list)
    total_unrounded: float = 0.0     # итог ДО скрытого округления — чтобы опции (B4) округлялись ОДИН раз


def compute_price(
    volume_cm3_unit: float,
    minutes_unit: float,
    qty: int,
    plastic: Plastic,
    settings: dict[str, float],
    discount_pct: float = 0.0,
    extra_colors: int = 0,
    volume_cm3_for_factor: float | None = None,
    plates: int = 1,
    urgency_factor: float = 1.0,
) -> PriceBreakdown:
    """Слайсится одна единица; тираж = ×qty по расходникам и времени.

    extra_colors > 0 — многоцветная печать: на каждый доп. цвет добавляем продувку
    (% филамента) и время на смены цвета (% времени) — оба коэффициента задаёт админ.

    plates — число столов проекта: за каждый берём фикс. доплату settings["plate_fee"]
    (OpenTodo 8c89a701 — «0.5 р за каждую платформу»). Доплата участвует в наценке (как
    «Подготовка»), но не в буфере на брак, и спрятана в строке машинного времени.

    volume_cm3_for_factor — объём единицы для коэффициента «большой модели», когда первый
    аргумент объёмом НЕ является (синтетический «пластик» с density=1 получает граммы):
    None = взять volume_cm3_unit, 0 = коэффициент не применять.
    """
    grams_unit = volume_cm3_unit * plastic.density
    extra_colors = max(0, int(extra_colors))

    waste_pct = float(settings.get("multicolor_waste_pct", 0.0)) if extra_colors else 0.0
    mtime_pct = float(settings.get("multicolor_time_pct", 0.0)) if extra_colors else 0.0
    purge_grams_unit = grams_unit * (waste_pct / 100.0) * extra_colors
    extra_minutes_unit = minutes_unit * (mtime_pct / 100.0) * extra_colors

    eff_grams_unit = grams_unit + purge_grams_unit
    eff_minutes_unit = minutes_unit + extra_minutes_unit

    filament_unit = eff_grams_unit * plastic.price_per_gram
    machine_unit = (eff_minutes_unit / 60.0) * settings["machine_hour_rate"]

    filament_cost = filament_unit * qty
    machine_cost = machine_unit * qty
    consumables = filament_cost + machine_cost

    # «Большая модель»: надбавку считаем от ВСЕХ расходников (филамент + время), но прячем в
    # строку машинного времени — строка филамента остаётся «граммы × цена», как в каталоге,
    # а клиент видит просто цену подороже (тот же приём, что с округлением итога ниже).
    vf = volume_factor(volume_cm3_unit if volume_cm3_for_factor is None else volume_cm3_for_factor,
                       settings)
    if vf > 1.0:
        surcharge = consumables * (vf - 1.0)
        machine_cost += surcharge
        consumables += surcharge

    # Индивидуальный % брака пластика (если задан) важнее общего из settings.
    failure_pct = plastic.failure_rate_pct
    if failure_pct is None:
        failure_pct = settings["failure_rate_pct"]
    waste_cost = consumables * failure_pct / 100.0
    setup_fee = settings["setup_fee"]

    # Доплата за столы проекта: фикс. ставка × число столов (за КАЖДЫЙ, включая первый —
    # «0.5 р за каждую платформу»). Как и надбавку за большую модель, прячем в строку машинного
    # времени: филамент остаётся «граммы × цена», разбивка чистая, а сумма строк сходится к итогу.
    # Добавляем ПОСЛЕ буфера на брак (доплата — работа оператора, не расход материала), но до
    # наценки, поэтому наценку получает — ровно как «Подготовка».
    plate_fee = float(settings.get("plate_fee", 0.0) or 0.0) * max(1, int(plates))
    if plate_fee:
        machine_cost += plate_fee
        consumables += plate_fee

    subtotal = consumables + waste_cost + setup_fee
    margin = subtotal * settings["profit_margin_pct"] / 100.0
    total = subtotal + margin

    # Скидка распространяется на ВСЁ, включая филамент (OpenTodo 07451408 — мастер так решил:
    # скидка должна быть щедрой и заметной, без скрытых исключений).
    discount_amount = total * discount_pct / 100.0
    total -= discount_amount

    # Срочность (B3): наценка за срочную печать — множитель поверх цены ПОСЛЕ скидки (премия за
    # срочность НЕ скидируется) и ДО минимума и скрытого округления (инвариант «круглого итога»
    # сохраняется). Зеркалит блок скидки выше, но со знаком плюс — строка «Срочность» в разбивке
    # сходится к итогу так же, как «Скидка».
    uf = max(1.0, float(urgency_factor or 1.0))
    urgency_surcharge = total * (uf - 1.0)
    total += urgency_surcharge

    min_price = settings["min_price"]
    min_applied = total < min_price
    if min_applied:
        total = min_price

    # Скрытое округление: итог ВВЕРХ до 10 копеек, а разницу прячем в машинное время —
    # так показанная сумма всегда «круглая», а разбивка по строкам по-прежнему сходится к ней.
    total_unrounded = total                                     # запоминаем ДО округления (для B4 apply_options)
    rounded = ceil10(total)
    machine_cost += rounded - total                             # дельта округления прячется в машинное время
    total = rounded

    return PriceBreakdown(
        qty=qty, grams_unit=grams_unit, minutes_unit=minutes_unit,
        grams_total=eff_grams_unit * qty, minutes_total=eff_minutes_unit * qty,
        filament_cost=filament_cost, machine_cost=machine_cost, waste_cost=waste_cost,
        setup_fee=setup_fee, margin=margin, discount_pct=discount_pct,
        discount_amount=discount_amount, subtotal=subtotal, total=total, min_applied=min_applied,
        purge_grams_unit=purge_grams_unit, extra_minutes_unit=extra_minutes_unit,
        extra_colors=extra_colors, failure_rate_pct=failure_pct, volume_factor=vf,
        plate_fee=plate_fee, urgency_factor=uf, urgency_surcharge=urgency_surcharge,
        total_unrounded=total_unrounded,
    )


def apply_options(bd: PriceBreakdown, options: list | None,
                  n_parts: int | None = None) -> PriceBreakdown:
    """Опции постобработки (B4): плоские платные услуги мастера ПОВЕРХ печати (шлифовка, покраска,
    вставки). Прибавляются к итогу ПОСЛЕ скидки и ПОСЛЕ срочности — это розничная цена готовой
    услуги, наценка/скидка на неё НЕ распространяются (в отличие от материалов). Складываем плоскую
    сумму в total и заново прячем округление до 10 копеек в машинное время (инвариант «круглого
    итога» сохраняется, как в compute_price). Пустой список → bd без изменений.

    options — снимок каталога [{id, name, price}]; храним его в bd.options целиком (в т.ч. цену 0 —
    для тега услуги у мастера), а в сумму options_cost попадают только положительные цены.

    Услуга «за деталь» (OpenTodo accee7a9, кейс «склейка 20 коп/деталь»): у опции per_part=1
    снимок несёт rate (ставку за отдельный элемент модели), а price пересчитывается здесь:
    price = rate × детали × тираж (bd.qty). Число деталей: свежий n_parts от вызывающего
    (замер fastmeasure) приоритетнее, затем parts, запомненный в снимке прошлым применением
    (так пере-применение при requote/оформлении не теряет множитель, даже когда замера под
    рукой нет), фолбэк — 1 деталь (кастомные/ручные пути, где деталей никто не считал).

    ИДЕМПОТЕНТНА (B4-инстант): опции на смете переключаются ЛОКАЛЬНО на клиенте (плоская цена,
    без переслайса), а при оформлении заказа /api/order переприкладывает актуальный набор к уже
    посчитанной bd. Поэтому перед применением СБРАСЫВАЕМ прошлый набор к «чистой печати» (по
    инварианту pure_base = total_unrounded − options_cost), иначе повтор с теми же id удвоил бы
    доплату. На СВЕЖЕЙ bd (options_cost==0) сброс — no-op, поведение как раньше."""
    opts = list(options or [])
    # Эффективная цена «за-детальных» услуг — ДО суммирования. Снимок мутируем на месте: он живёт
    # в m["options"]/bd.options и дальше уезжает в заказ уже с готовой ценой (данные-сейф).
    qty = max(1, int(getattr(bd, "qty", 1) or 1))
    for o in opts:
        if not (isinstance(o, dict) and o.get("per_part")):
            continue
        try:
            rate = float(o.get("rate", o.get("price")) or 0.0)
        except (TypeError, ValueError):
            rate = 0.0
        try:
            parts = int(n_parts or 0) or int(o.get("parts") or 0) or 1
        except (TypeError, ValueError):
            parts = 1
        o["rate"] = rate
        o["parts"] = parts
        o["price"] = round(rate * parts * qty, 2)
    # Откат ранее применённых опций → восстановить «чистый» итог печати (идемпотентность).
    # getattr со значениями по умолчанию: на bd без полей опций (частичный снимок/заглушка)
    # откатывать нечего — ведём себя как при первом применении.
    prev_cost = getattr(bd, "options_cost", 0.0) or 0.0
    if prev_cost or getattr(bd, "options", None):
        pure_base = bd.total_unrounded - prev_cost
        pure_total = ceil10(pure_base)
        bd.machine_cost -= bd.total - (pure_total + prev_cost)                 # снять прошлую дельту округления
        bd.total = pure_total
        bd.total_unrounded = pure_base
        bd.options = []
        bd.options_cost = 0.0
    bd.options = opts
    cost = sum(float(o.get("price") or 0.0) for o in opts)
    bd.options_cost = cost
    if cost <= 0:
        return bd
    # ЕДИНОЕ финальное округление: округляем (непокруглённый итог печати + опции) ОДИН раз.
    # Если округлять уже покруглённый compute_price итог, второй ceil перебрал бы до 10 копеек
    # (переплата ≤0.10 при цене опции с «хвостом» мельче гривенника). base — итог ДО округления.
    base = bd.total_unrounded if bd.total_unrounded else bd.total
    unrounded = base + cost
    rounded = ceil10(unrounded)
    bd.machine_cost += rounded - (bd.total + cost)               # строки по-прежнему сходятся к итогу
    bd.total = rounded
    bd.total_unrounded = unrounded
    return bd


def compute_price_manual(
    grams_unit: float,
    minutes_unit: float,
    price_per_kg: float,
    settings: dict[str, float],
    *,
    qty: int = 1,
    discount_pct: float = 0.0,
    failure_rate_pct: float | None = None,
    volume_cm3_unit: float | None = None,
    plates: int = 1,
    urgency_factor: float = 1.0,
) -> PriceBreakdown:
    """Ручной расчёт цены по ГОТОВЫМ граммам/времени/цене — для калькулятора в админке.

    Та же формула, что и при слайсинге (compute_price): подсовываем «пластик» с density=1 и
    объёмом=grams (тогда grams_unit = объём×плотность = grams), а цену за кг переводим в цену за
    грамм. Многоцвет не учитываем (extra_colors=0). failure_rate_pct=None → берётся общий процент
    брака из settings (как у пластика без индивидуального %). volume_cm3_unit — объём единицы для
    коэффициента «большой модели» (пути пересчёта знают плотность → передают граммы/плотность);
    None = объём неизвестен, коэффициент не применяем (чистый ручной калькулятор)."""
    plastic = SimpleNamespace(density=1.0, price_per_gram=price_per_kg / 1000.0,
                              failure_rate_pct=failure_rate_pct)
    return compute_price(grams_unit, minutes_unit, max(1, int(qty)), plastic,  # type: ignore[arg-type]
                         settings, discount_pct=discount_pct, extra_colors=0,
                         volume_cm3_for_factor=(volume_cm3_unit if volume_cm3_unit is not None
                                                else 0.0),
                         plates=plates, urgency_factor=urgency_factor)


def urgency_factor_from(urgency: str | None, settings: dict[str, float]) -> float:
    """Множитель срочности из настроек (B3): 'rush' → 1 + rush_surcharge_pct/100 (наценка за
    срочную печать, единый %-формат с прочими ставками расчёта). Всё остальное ('normal'/пусто/
    неизвестное) → 1.0 (обычная печать) — старые кэшированные клиенты без поля работают как раньше."""
    if str(urgency or "").strip().lower() == "rush":
        pct = float(settings.get("rush_surcharge_pct", 0.0) or 0.0)
        return 1.0 + pct / 100.0
    return 1.0


# Поля settings, которые задают подписи строк расчёта (%-ставки). Сохраняем их СНИМОК
# вместе с разбивкой заказа — настройки админ потом меняет, а расчётник заказа должен
# показывать ставки, действовавшие на момент оформления.
_LABEL_RATES = ("failure_rate_pct", "profit_margin_pct")


def breakdown_to_dict(bd: PriceBreakdown, settings: dict[str, float]) -> dict:
    """Разбивку → плоский dict для JSON-хранения в заказе (requests.breakdown).
    Кроме полей самой PriceBreakdown кладём снимок %-ставок для подписей строк.
    Для брака берём фактически применённый процент (индивид. пластика, если был задан)."""
    out = asdict(bd)
    for k in _LABEL_RATES:
        out[k] = float(settings.get(k, 0.0))
    out["failure_rate_pct"] = float(bd.failure_rate_pct)   # эффективный %, а не общий из settings
    return out


def breakdown_from_dict(d: dict | None) -> PriceBreakdown | None:
    """dict (из requests.breakdown) → PriceBreakdown; None/мусор → None. Толерантно к
    отсутствующим полям (старые/частичные записи) — недостающее берём из значений по умолчанию."""
    if not d:
        return None
    names = {f.name for f in fields(PriceBreakdown)}
    kw = {k: v for k, v in d.items() if k in names}
    try:
        return PriceBreakdown(**kw)
    except TypeError:
        return None


def settings_from_breakdown(d: dict | None) -> dict[str, float]:
    """Снимок %-ставок (failure/margin) из сохранённой разбивки заказа — обратно к build_quote."""
    return {k: float((d or {}).get(k, 0.0)) for k in _LABEL_RATES}
