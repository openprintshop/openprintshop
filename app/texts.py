"""Тексты сообщений (RU) и помощники форматирования."""
from __future__ import annotations

import html
import importlib.util
import re

from . import pricing
from .db import REQ_STATUS_LABELS
from .pricing import PriceBreakdown


def esc(s: str) -> str:
    return html.escape(str(s))


# ---- Печатопригодность (DFM, #9724 + B2) ----
# Структурированный вердикт БЕЗ слайсера. Реального замера толщины стенок в геометрии нет —
# только грубые прокси (2·V_union/площадь, самый тонкий габарит), поэтому тонкие стенки/мелочь
# дают МЯГКОЕ «risky», а не жёсткий запрет. B2 НИКОГДА не блокирует заказ — только предупреждает
# (плашка информативная, вердикт уходит в примечание мастеру). Уровни: 'ok' < 'risky' < 'wont_fit'.
_VERDICT_RANK = {"ok": 0, "risky": 1, "wont_fit": 2}


def _dims_nums(dims: str) -> list[float]:
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", dims or "")][:3]


def printability_verdict(dims: str, *, bed_mm: float = 256.0, n_parts=None,
                         min_extent_mm: float = 0.0, max_part_mm: float = 0.0,
                         wall_proxy_mm: float = 0.0,
                         min_wall_mm: float = 0.8) -> tuple[str, list[dict]]:
    """(verdict, dfm[]) печатопригодности для мини-аппа/примечания мастеру.
    verdict ∈ {'ok','risky','wont_fit'}; dfm — сработавшие проверки [{key,title,detail,status}],
    status ∈ {'warn','bad'}. Пустой dims (замера нет) → ('ok', []) — паники не создаём.

    ГЕЙТ ОДНОЙ ДЕТАЛИ (главное, что делает B2 безопасным, а не генератором ложных тревог):
    предупреждения про тонкие стенки/мелкоту срабатывают ТОЛЬКО для одной детали (n_parts==1).
    Валидный набор из многих мелких деталей (n_parts>1) — это нормальный многодетальный заказ:
    каждая деталь в порядке, бот сам разложит их по столам. Без гейта именно такой набор давал
    ложные предупреждения, которые пугали клиентов на живом экране."""
    verdict = "ok"
    dfm: list[dict] = []

    def _bump(level: str) -> None:
        nonlocal verdict
        if _VERDICT_RANK[level] > _VERDICT_RANK[verdict]:
            verdict = level

    nums = _dims_nums(dims)
    overall_max = max(nums) if len(nums) >= 3 else 0.0
    single = (n_parts == 1)
    bed_mm = float(bed_mm or 256.0)

    # (1) Габарит vs стол. Знаем размер самой большой ОТДЕЛЬНОЙ детали (max_part_mm) — сравниваем
    # именно её: набор мелких деталей с большим общим футпринтом бот раскладывает по столам, это
    # НЕ «не влезет». Есть только общий bbox (гигант без по-детального замера) — судим мягче.
    part_longest = max_part_mm if (max_part_mm and max_part_mm > 0) else 0.0
    if part_longest > 0:
        if part_longest > bed_mm:
            _bump("wont_fit")
            dfm.append({"key": "bed", "title": "Размещение на столе", "status": "bad",
                        "detail": f"Самая длинная сторона детали {part_longest:.0f} мм больше "
                                  f"стола ~{bed_mm:.0f} мм — деталь придётся уменьшить или разрезать."})
        elif part_longest > bed_mm * 0.92:
            _bump("risky")
            dfm.append({"key": "bed", "title": "Размещение на столе", "status": "warn",
                        "detail": f"Деталь почти на пределе стола ({part_longest:.0f} мм при "
                                  f"~{bed_mm:.0f} мм) — проверьте размещение."})
    elif overall_max > 0:
        # по-детального замера нет: судим по общему габариту, но мягко (одна это деталь или набор —
        # неизвестно) — максимум 'risky' с честной оговоркой про раскладку по столам.
        if overall_max > bed_mm:
            _bump("risky")
            dfm.append({"key": "bed", "title": "Размещение на столе", "status": "warn",
                        "detail": f"Габарит {overall_max:.0f} мм больше стола ~{bed_mm:.0f} мм. "
                                  "Если это одна деталь — её придётся уменьшить или разрезать; "
                                  "набор отдельных деталей бот сам разложит по столам."})
        elif overall_max > bed_mm * 0.92:
            _bump("risky")
            dfm.append({"key": "bed", "title": "Размещение на столе", "status": "warn",
                        "detail": f"Почти на пределе стола ({overall_max:.0f} мм при "
                                  f"~{bed_mm:.0f} мм) — проверьте размещение."})

    # (2) Тонкие стенки / мелкие детали — ТОЛЬКО одна деталь (гейт выше) и только как ОЦЕНКА.
    # Прокси толщины = min(2·V_union/площадь, самый тонкий габарит детали). Точного per-wall
    # замера в геометрии нет, поэтому статус всегда 'warn' (мягко), формулировка — «возможно».
    if single:
        cands = [v for v in (wall_proxy_mm, min_extent_mm) if v and v > 0]
        thin = min(cands) if cands else 0.0
        if 0 < thin < max(0.0, float(min_wall_mm or 0.0)):
            _bump("risky")
            dfm.append({"key": "wall", "title": "Тонкие стенки / мелкие детали", "status": "warn",
                        "detail": f"Оценка толщины ≈{thin:.1f} мм — возможен риск непропечатки "
                                  f"(мин. ~{float(min_wall_mm):.1f} мм). Мастер проверит перед печатью."})

    return verdict, dfm


# иконки для строкового (Telegram) представления проверок — единый источник с мини-аппом
_DFM_ICON = {"bed": "📐", "wall": "🧱"}


def printability_warnings(dims: str, bed_mm: float = 256.0, *, n_parts=None,
                          min_extent_mm: float = 0.0, max_part_mm: float = 0.0,
                          wall_proxy_mm: float = 0.0, min_wall_mm: float = 0.8) -> list[str]:
    """Подсказки по печатопригодности строками (Telegram-путь, #9724). Тонкая обёртка над
    printability_verdict — единый источник логики с мини-аппом. Старый вызов (только dims[, bed])
    ведёт себя как раньше: n_parts неизвестен → тонкие стенки не проверяются, остаётся габарит."""
    _verdict, dfm = printability_verdict(
        dims, bed_mm=bed_mm, n_parts=n_parts, min_extent_mm=min_extent_mm,
        max_part_mm=max_part_mm, wall_proxy_mm=wall_proxy_mm, min_wall_mm=min_wall_mm)
    return [f"{_DFM_ICON.get(d['key'], '⚠️')} {d['detail']}" for d in dfm]


def fmt_minutes(minutes: float) -> str:
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def fmt_money(value: float, cur: str) -> str:
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")  # 1 234,56
    return f"{s} {cur}"


# ---- Расчётник заказа (детальная разбивка цены) ----
def breakdown_rows(bd: PriceBreakdown, settings: dict, cur: str) -> list[dict]:
    """Строки расчётника по PriceBreakdown — единый источник для живого результата
    (мини-апка) и сметы сохранённого заказа. Каждая строка: {label, sub, value, discount?}.
    Подписи %-ставок берём из settings (для заказа это снимок на момент оформления).
    Для брака предпочитаем фактически применённый % из самой разбивки (может быть
    индивидуальным у пластика); settings — запасной вариант для старых заказов."""
    fail_pct = float(getattr(bd, "failure_rate_pct", 0.0)) or float(settings.get("failure_rate_pct", 0.0))
    marg_pct = float(settings.get("profit_margin_pct", 0.0))
    # многоцветная печать — показываем число цветов прямо в строке филамента (OpenTodo 82ebeeef)
    fil_sub = f"{bd.grams_total:.1f} г" + (f" · {bd.extra_colors + 1} цв." if bd.extra_colors else "")
    rows = [
        {"label": "Филамент", "sub": fil_sub, "value": fmt_money(bd.filament_cost, cur)},
        {"label": "Машинное время", "sub": fmt_minutes(bd.minutes_total),
         "value": fmt_money(bd.machine_cost, cur)},
        {"label": "Буфер на брак", "sub": f"{fail_pct:.0f}%", "value": fmt_money(bd.waste_cost, cur)},
        {"label": "Подготовка", "sub": "фикс. на заказ", "value": fmt_money(bd.setup_fee, cur)},
        {"label": "Наценка", "sub": f"{marg_pct:.0f}%", "value": fmt_money(bd.margin, cur)},
    ]
    if bd.discount_pct:
        rows.append({"label": "Скидка", "sub": f"{bd.discount_pct:.0f}% на всё",
                     "value": "−" + fmt_money(bd.discount_amount, cur), "discount": True})
    # Срочность (B3): отдельная строка наценки — только для срочного заказа. getattr со значением
    # по умолчанию 1.0/0.0, чтобы старые сметы без поля не падали.
    uf = float(getattr(bd, "urgency_factor", 1.0) or 1.0)
    if uf > 1.0:
        surch = float(getattr(bd, "urgency_surcharge", 0.0) or 0.0)
        rows.append({"label": "Срочность", "sub": f"×{uf:g}", "value": "+" + fmt_money(surch, cur)})
    # Опции постобработки (B4): каждая платная услуга — отдельной строкой «+цена». getattr со
    # значением по умолчанию, чтобы старые сметы без поля не падали. Цену 0 (бесплатная опция) в
    # расчётник не выводим — сумма строк должна сходиться к итогу. У «за-детальной» услуги
    # (accee7a9) в sub показываем множители: «постобработка · 0,20 × 60 дет.» — клиенту видно,
    # откуда взялась сумма склейки.
    for opt in (getattr(bd, "options", None) or []):
        price = float(opt.get("price") or 0.0)
        if price > 0:
            sub = "постобработка"
            if opt.get("per_part"):
                # показываем ПОЛНОЕ число деталей (на копию × тираж) — ровно множитель суммы
                parts = int(opt.get("parts") or 1) * max(1, int(getattr(bd, "qty", 1) or 1))
                rate = float(opt.get("rate") or 0.0)
                sub = f"постобработка · {fmt_money(rate, '').strip()} × {parts} дет."
            rows.append({"label": opt.get("name") or "Услуга", "sub": sub,
                         "value": "+" + fmt_money(price, cur)})
    return rows


def order_breakdown(r, cur: str) -> dict | None:
    """Расчётник сохранённого заказа для отдачи в JSON мини-апки. Берёт снимок разбивки из
    requests.breakdown; если его нет (старые заказы) — возвращает None (показываем только итог)."""
    bd = pricing.breakdown_from_dict(getattr(r, "breakdown", None))
    if bd is None:
        return None
    snap = getattr(r, "breakdown", None) or {}
    rows = breakdown_rows(bd, snap, cur)
    total = bd.total
    # Плата за разработку модели — отдельной строкой к счёту индивидуального заказа
    # (OpenTodo 6aaa6a19); может быть 0 — тогда строку не показываем.
    dev_fee = float(getattr(r, "dev_fee", 0.0) or 0.0)
    if dev_fee > 0:
        rows = [*rows, {"label": "Разработка модели", "sub": "индивидуальный заказ",
                        "value": fmt_money(dev_fee, cur)}]
        total = bd.total + dev_fee
    return {
        "rows": rows,
        "total": fmt_money(total, cur),
        "grams_total": f"{bd.grams_total:.1f}",
        "time_total": fmt_minutes(bd.minutes_total),
        "qty": bd.qty,
        "extra_colors": bd.extra_colors,
        "purge": f"{bd.purge_grams_unit:.1f}" if bd.purge_grams_unit else "",
    }


# Площадки, ссылки с которых бот скачивает сам. MakerWorld — только при личном модуле
# app/makerworld.py: в открытой версии его нет, и обещать MakerWorld клиенту нельзя.
_MW = importlib.util.find_spec(f"{__package__}.makerworld") is not None
_SITES = "MakerWorld, Printables" if _MW else "Printables, Thingiverse"
_SEARCH_SITES = "Printables, Thingiverse и MakerWorld" if _MW else "Printables и Thingiverse"
_ALL_SITES = ["MakerWorld", "Printables", "Thingiverse"] if _MW else ["Printables", "Thingiverse"]

# ---- Главное меню ----
_MENU_BODY = (
    f"Пришлите <b>ссылку</b> ({_SITES}) или <b>файл</b> модели — "
    "посчитаю всё за пару минут:\n\n"
    "1. скачаю модель и покажу превью\n"
    "2. нарежу под нужное качество и прочность\n"
    "3. посчитаю расход, время и цену\n"
    "4. пришлю готовую PDF-смету\n\n"
    "Нажмите кнопку, чтобы начать ↓"
)


def menu(brand_title: str = "") -> str:
    """Главное меню с именем мастерской (A3, white-label). Пусто — наш бренд."""
    return f"<b>{esc(brand_title.strip() or 'OpenPrintShop')}</b>\n\n{_MENU_BODY}"

HELP = (
    "ℹ️ <b>Как это работает</b>\n\n"
    f"1. Пришлите ссылку ({_SITES}) или файл модели (.stl/.3mf/.obj/.zip).\n"
    "2. Выберите пластик, количество, качество и прочность.\n"
    "3. Бот нарежет модель и посчитает стоимость печати.\n"
    "4. Получите детальную смету и PDF для клиента.\n\n"
    "Команды: /start — меню, /cancel — отмена."
)

# ---- Шаги мастера ----
MODEL_PROMPT = (
    "🔗 <b>Модель</b>\n\n"
    f"Пришлите <b>ссылку</b> ({', '.join(_ALL_SITES)} или прямую ссылку на файл) "
    "или загрузите файл <b>.stl / .3mf / .obj / .zip</b>."
)
MODEL_DOWNLOADING = "⏳ Скачиваю модель по ссылке…"
QUICK_CALC_STARTED = ("🚀 <b>Начал расчёт!</b>\n\nСкачаю модель и посчитаю цену — обычно это "
                      "1–3 минуты. Результат пришлю сюда: рендер, цена и кнопка со сметой.")
MODEL_DOWNLOAD_FAIL = (
    "⚠️ Не получилось скачать модель по ссылке автоматически "
    "(площадка отдаёт файлы только после входа или ссылка недоступна).\n\n"
    "Пожалуйста, скачайте файл вручную и пришлите его сюда как "
    "<b>.stl / .3mf / .obj / .zip</b>."
)
MODEL_BAD_INPUT = (
    "Не похоже на ссылку или файл. Пришлите ссылку "
    f"({' / '.join(_ALL_SITES)} / прямую на .stl/.3mf) или файл "
    "<b>.stl / .3mf / .obj / .zip</b>."
)
PREVIEW_CAPTION = "🧊 Ваша модель: <b>{label}</b>"
VARIANTS_PROMPT = (
    "🧩 <b>Выберите вариант модели</b>\n\n"
    "У этой модели на MakerWorld несколько вариантов (профилей печати). "
    "Иногда они визуально не отличаются — это нормально (разные настройки). "
    "Выберите подходящий:"
)

# ---- поиск модели по запросу (опц. прямо из бота) ----
SEARCH_PROMPT = (
    "🔎 <b>Найти модель</b>\n\n"
    "Напишите, что ищете (например «подставка для телефона» или «groot»). "
    f"Поищу на {_SEARCH_SITES} и покажу подходящие модели."
)
SEARCH_SEARCHING = "🔎 Ищу на 3D-площадках…"
SEARCH_EMPTY = "😕 Ничего не найдено. Уточните запрос или пришлите ссылку/файл."
SEARCH_HINT = "Выберите модель из списка — скачаю её и продолжу расчёт:"


def search_results_text(query: str, n: int) -> str:
    return (f"🔎 <b>Результаты по запросу:</b> {esc(query)}\n"
            f"Нашёл моделей: {n}.\n\n{SEARCH_HINT}")


def model_badges(plates: int, multicolor: bool, num_colors: int = 0) -> str:
    """Подпись к превью: метки «несколько столов» / «многоцвет»."""
    parts = []
    if plates and plates >= 2:
        parts.append(f"🧩 {plates} стола в проекте")
    if multicolor:
        n = f" (~{num_colors} цв.)" if num_colors and num_colors >= 2 else ""
        parts.append(f"🌈 многоцветная{n}")
    return ("\n" + " · ".join(parts)) if parts else ""


def coloring_prompt(num_colors: int = 2, plates: int = 1) -> str:
    n = max(2, int(num_colors or 2))
    head = "🌈 <b>Многоцветная модель</b>\n\n"
    if plates >= 2:
        head += f"Проект на <b>{plates} стол(а/ов)</b> — посчитаем все.\n\n"
    return (
        head +
        f"Похоже, в модели задана печать в несколько цветов (примерно {n}). "
        "Как печатаем?\n\n"
        "🌈 <b>Многоцветная</b> — как задумал автор, но <b>дороже</b>:\n"
        "• при каждой смене цвета филамент продувается в отходы (башня очистки) — "
        "лишний расход пластика;\n"
        "• смены цвета добавляют время печати — больше машинных часов.\n\n"
        "⚪ <b>Одноцветная</b> — печать одним цветом: дешевле и быстрее, "
        "геометрия и размеры те же.\n\n"
        "Выберите вариант:"
    )
# Слайсер здесь НЕ называем. Клиентский расчёт идёт быстрым аналитическим методом; слайс
# включается только настройкой «Честный слайс в расчётах» (calcservice: use_slicer =
# force_slice or settings.slicing_enabled), и по умолчанию она выключена. Текст остался от
# времён, когда слайс был основным путём, и врал владельцу — «нарезаю модель в слайсере»
# при выключенном слайсинге (фидбэк 11.08). Формулировка ниже верна в ОБОИХ режимах.
PROCESSING = (
    "⏳ <b>Идёт подсчёт…</b>\n\n"
    "Разбираю геометрию модели, считаю расход филамента и время печати. "
    "Это займёт некоторое время — пожалуйста, подождите."
)

PRINTER_PROMPT = "🖨 <b>Принтер</b>\n\nНа каком принтере печатаем?"


def material_prompt(label: str) -> str:
    return (
        f"✅ Модель принята: <b>{esc(label)}</b>\n\n"
        "🧵 <b>Пластик</b>\n\nВыберите материал, которым будем печатать:"
    )


COLOR_PROMPT = ("🎨 <b>Цвет</b>\n\nВыберите один или несколько цветов "
                "(несколько → переливание по высоте), затем нажмите «Готово»:")
EDIT_COLORS_PROMPT = ("Введите цвета через запятую (например «чёрный, белый, красный»). "
                      "Если цвет один — впишите один. «-» — очистить.")
CATALOG_EMPTY = "📖 Каталог пуст — администратор ещё не добавил пластики."
EDIT_DESC_PROMPT = "Введите описание свойств пластика (или «-» — взять базовое по типу материала):"
PLASTIC_PHOTO_PROMPT = "📷 Пришлите фото катушки для «{name}» — оно появится в каталоге."
EDIT_FAILURE_PROMPT = ("Введите индивидуальный <b>% брака</b> для этого пластика "
                       "(например 12). «-» — вернуть общий ({base:g}% из настроек):")


def catalog_card(p, cur: str) -> str:
    cols = ("\n🎨 Цвета: " + esc(", ".join(p.color_list))) if p.color_list else ""
    return (f"🧵 <b>{esc(p.name)}</b>\n"
            f"Тип: {esc(p.material_type)} · {p.eff_price_per_kg:g} {cur}/кг · {p.density:g} г/см³{cols}\n\n"
            f"{esc(p.description or '—')}")


QUANTITY_PROMPT = "🔢 <b>Количество</b>\n\nСколько штук нужно напечатать?"
QUANTITY_CUSTOM_PROMPT = "🔢 <b>Количество</b>\n\nВведите число штук (1–1000):"
QUANTITY_BAD = "Нужно целое число от 1 до 1000. Попробуйте ещё раз:"
# Качество/прочность клиент больше не выбирает (8c8199d1): печатаем профилем мастера,
# особые случаи — свободным текстом.
WISHES_PROMPT = (
    "💬 <b>Особые пожелания</b>\n\nНапишите, если есть: нужен к пятнице, попрочнее, "
    "печать потоньше… Мастер учтёт при подтверждении заказа.\n\n"
    "Или нажмите «Пропустить»."
)

NO_PLASTICS = "🧵 Пока нет доступных пластиков — обратитесь к администратору."

# ---- история моделей ----
MY_MODELS_EMPTY = ("📂 <b>Мои модели</b>\n\nЗдесь появятся модели, которые вы уже присылали — "
                   "чтобы печатать повторно без повторной отправки. Сделайте первый расчёт.")
MY_MODELS_TITLE = "📂 <b>Мои модели</b>\n\nВыберите модель, чтобы рассчитать снова:"
MODEL_MISSING = ("⚠️ Файлы этой модели не найдены (возможно, были удалены). "
                 "Пришлите модель заново.")


# ---- коллекция моделей ----
COLLECTION_TITLE = ("🌍 <b>Коллекция моделей</b>\n\nМодели, которые присылали пользователи. "
                    "Выберите — и посчитайте печать у себя.")
COLLECTION_WEBAPP_HINT = ("🖼 <b>Коллекция страницей</b>\n\nНажмите «📂 Открыть коллекцию» внизу — "
                          "там удобно листать карточки с превью. Или выберите из списка ниже.")
COLLECTION_EMPTY = ("🌍 <b>Коллекция</b>\n\nПока пусто — наполняется из моделей, "
                    "которые присылают пользователи.")
ADMIN_COLLECTION_TITLE = ("🌍 <b>Коллекция</b>\n\n🖼 — есть фото печати, ▫️ — только рендер. "
                          "Нажмите запись для управления.")
COL_PHOTO_PROMPT = ("📷 Пришлите <b>фото напечатанной модели</b> для «{label}» — "
                    "оно заменит авторендер в коллекции.")


def collection_caption(label: str, uses: int, has_photo: bool) -> str:
    note = "" if has_photo else "\n<i>(показан авторендер)</i>"
    return f"📦 <b>{esc(label)}</b>\nСчитали: {uses} раз.{note}"


def admin_collection_detail(label: str, uses: int, has_photo: bool, added_by) -> str:
    return (f"🌍 <b>{esc(label)}</b>\n\nСчитали: {uses} раз\n"
            f"Фото печати: {'есть 🖼' if has_photo else 'нет (рендер)'}\n"
            f"Добавил: id <code>{added_by}</code>")


def reuse_text(*, label: str, printer_name: str, plastic_name: str, qty: int) -> str:
    return (
        f"📦 <b>{esc(label)}</b>\n\n"
        "Прошлые параметры:\n"
        f"🖨 {esc(printer_name)}\n"
        f"🧵 {esc(plastic_name)}\n"
        f"🔢 {qty} шт\n\n"
        "Печатать с теми же параметрами или изменить?"
    )


def confirm_text(*, model_label: str, plastic_name: str, printer_name: str, qty: int,
                 wishes: str = "", multicolor: bool = False, extra_colors: int = 0,
                 plates: int = 1) -> str:
    extra = ""
    if plates and plates >= 2:
        extra += f"🧩 Столов в проекте: <b>{plates}</b>\n"
    if multicolor:
        n = f" (~{extra_colors + 1} цв.)" if extra_colors else ""
        extra += f"🌈 Печать: <b>многоцветная{n}</b>\n"
    if wishes:
        extra += f"💬 Пожелания: <i>{esc(wishes)}</i>\n"
    return (
        "🧾 <b>Проверьте параметры</b>\n\n"
        f"📦 Модель: <b>{esc(model_label)}</b>\n"
        f"🖨 Принтер: <b>{esc(printer_name)}</b>\n"
        f"🧵 Пластик: <b>{esc(plastic_name)}</b>\n"
        f"🔢 Количество: <b>{qty} шт</b>\n"
        f"{extra}\n"
        "Запускаем расчёт?"
    )


def result_text(*, model_label: str, plastic_name: str, printer_name: str, qty: int,
                bd: PriceBreakdown, settings: dict[str, float], cur: str,
                cached: bool = False, plates: int = 1) -> str:
    badge = ""
    if plates and plates >= 2:
        badge += f"\n🧩 Столов в проекте: {plates}"
    if bd.extra_colors:
        badge += f"\n🌈 Многоцветная печать (~{bd.extra_colors + 1} цв.)"
    unit_filament = f"• Филамент: {bd.grams_unit:.1f} г"
    if bd.purge_grams_unit:
        unit_filament += f" + {bd.purge_grams_unit:.1f} г на продувку"
    unit_time = f"• Время: {fmt_minutes(bd.minutes_unit)}"
    if bd.extra_minutes_unit:
        unit_time += f" + {fmt_minutes(bd.extra_minutes_unit)} на смены цвета"
    lines = [
        "✅ <b>Расчёт готов</b>" + ("  <i>(из кэша)</i>" if cached else ""),
        "",
        f"📦 {esc(model_label)}{badge}",
        f"🖨 {esc(printer_name)}  •  🧵 {esc(plastic_name)}",
        f"🔢 {qty} шт",
        "",
        "<b>Печать (на 1 шт):</b>",
        unit_filament,
        unit_time,
        "",
        f"<b>На весь тираж ({qty} шт):</b>",
        f"• Филамент: {bd.grams_total:.1f} г",
        f"• Время печати: {fmt_minutes(bd.minutes_total)}",
        "",
        "<b>Стоимость:</b>",
        f"• Филамент: {fmt_money(bd.filament_cost, cur)}",
        f"• Машинное время: {fmt_money(bd.machine_cost, cur)}",
        f"• Буфер на брак ({(getattr(bd, 'failure_rate_pct', 0.0) or settings['failure_rate_pct']):.0f}%): "
        f"{fmt_money(bd.waste_cost, cur)}",
        f"• Подготовка: {fmt_money(bd.setup_fee, cur)}",
        f"• Наценка ({settings['profit_margin_pct']:.0f}%): {fmt_money(bd.margin, cur)}",
    ]
    if bd.discount_pct:
        lines.append(f"• Скидка ({bd.discount_pct:.0f}% на всё): "
                     f"−{fmt_money(bd.discount_amount, cur)}")
    lines += ["━━━━━━━━━━━━━━", f"💰 <b>ИТОГО: {fmt_money(bd.total, cur)}</b>"]
    if bd.min_applied:
        lines.append(f"\n<i>(применена минимальная цена {fmt_money(settings['min_price'], cur)})</i>")
    return "\n".join(lines)


def admin_notification(*, user, model_label: str, printer_name: str, plastic_name: str,
                        qty: int, quality_label: str, strength_label: str, bd, cur: str,
                        plates: int = 1) -> str:
    uname = f"@{user.username}" if getattr(user, "username", None) else "—"
    tags = []
    if plates and plates >= 2:
        tags.append(f"🧩 {plates} стола")
    if getattr(bd, "extra_colors", 0):
        tags.append(f"🌈 многоцвет ~{bd.extra_colors + 1} цв.")
    tagline = ("\n" + " • ".join(tags)) if tags else ""
    return (
        "🔔 <b>Новый расчёт</b>\n\n"
        f"👤 {esc(user.full_name)} ({uname}, id <code>{user.id}</code>)\n"
        f"📦 {esc(model_label)}{tagline}\n"
        f"🖨 {esc(printer_name)} • 🧵 {esc(plastic_name)}\n"
        f"🔢 {qty} шт\n"
        f"⚖️ {bd.grams_total:.1f} г • ⏱ {fmt_minutes(bd.minutes_total)}\n"
        f"💰 <b>{fmt_money(bd.total, cur)}</b>"
    )


# ---- заказы ----
ORDER_PLACED = ("✅ <b>Заказ оставлен!</b>\n\nМы свяжемся по поводу печати. "
                "Статус можно смотреть в разделе «🧾 Мои заказы».")
# приписка к ORDER_PLACED при заказе из быстрого пересчёта (смена пластика без переслайса, 050aff56)
FAST_QUOTE_NOTE = ("⚡ Расчёт был быстрым (смена пластика без повторной нарезки модели). "
                   "Мастер проверит точную цену при взятии в работу — если она изменится, "
                   "вам придёт подтверждение.")
MY_ORDERS_TITLE = "🧾 <b>Мои заказы</b>\n\nВыберите заказ — увидите статус:"
MY_ORDERS_EMPTY = ("🧾 <b>Мои заказы</b>\n\nЗаказов пока нет. Посчитайте модель и нажмите "
                   "«📝 Оставить заказ».")
ORDER_PRICE_PROMPT = "💱 Введите новую цену для заказа #{id} (текущая: {price}):"
ORDER_COMMENT_PROMPT = ("💬 Комментарий клиенту о причине изменения цены "
                        "(необязательно). Отправьте текст или «-», чтобы пропустить:")
ORDER_DUE_PROMPT = ("🔧 Заказ #{id} → <b>в работу</b>.\n\n"
                    "Укажите срок исполнения в формате <b>ДД.ММ</b> (например 10.06) — "
                    "клиенту придёт уведомление со сроком. Или «-», чтобы без срока:")
ORDER_DUE_BAD = "Не похоже на дату. Введите в формате <b>ДД.ММ</b> (например 10.06) или «-»:"
ADMIN_ORDERS_TITLE = "🧾 <b>Заказы</b>\n\nВыберите заказ для управления статусом:"
ADMIN_ORDERS_EMPTY = "🧾 <b>Заказы</b>\n\nПока нет оставленных заказов."


def _req_common(r) -> list[str]:
    lines = [f"📦 {esc(r.model_label)}",
             f"🖨 {esc(r.printer)} · 🧵 {esc(r.plastic_name)}",
             f"🔢 {r.quantity} шт",
             f"⚖️ {r.grams_total:.0f} г · ⏱ {fmt_minutes(r.minutes_total)}"]
    extra = []
    if getattr(r, "dims", ""):
        extra.append(f"📐 {esc(r.dims)}")
    if getattr(r, "multicolor", 0):
        extra.append("🌈 многоцвет")
    if getattr(r, "plates", 1) and r.plates >= 2:
        extra.append(f"🧩 {r.plates} стола")
    if extra:
        lines.append(" · ".join(extra))
    return lines


def request_detail(r, cur: str) -> str:
    lines = [f"🧾 <b>Заказ #{r.id}</b>\n", *_req_common(r),
             f"\nСтатус: <b>{REQ_STATUS_LABELS.get(r.status, r.status)}</b>"]
    if r.status == "price_changed" and r.pending_price is not None:
        if (r.price or 0) <= 0:
            # Первая цена заказа (досчитал ПК или назвал мастер): «Было: 0,00 → новая цена»
            # читалось бы как подорожание с нуля — а клиенту цену просто впервые назвали.
            lines.append(f"\n💰 Цена: <b>{fmt_money(r.pending_price, cur)}</b>")
        else:
            lines += [f"\n💰 Было: {fmt_money(r.price, cur)}",
                      f"💱 Новая цена: <b>{fmt_money(r.pending_price, cur)}</b>"]
        if getattr(r, "price_comment", None):
            lines.append(f"\n💬 Комментарий: <i>{esc(r.price_comment)}</i>")
        lines.append("\nПримите или отклоните новую цену:" if (r.price or 0) > 0
                     else "\nПодтвердите цену или откажитесь:")
    elif r.status == "new" and (r.price or 0) <= 0 and int(getattr(r, "price_pending", 0) or 0):
        # После «💰 Цена:» — хвост без слова «Цена» (review:regress#1: PRICE_PENDING_NOTE давал
        # «Цена: Цена считается»). Сама подсказка мини-аппы остаётся PRICE_PENDING_NOTE.
        lines.append(f"💰 Цена: <b>{PRICE_PENDING_IN_LINE}</b> — {PRICE_PENDING_TAIL}")
    elif r.status == "new" and (r.price or 0) <= 0 and int(getattr(r, "unsliced", 0) or 0):
        lines.append("💰 Цена: <b>оценит мастер</b>")
    else:
        lines.append(f"💰 Цена: <b>{fmt_money(r.price, cur)}</b>")
    if int(getattr(r, "paid", 0) or 0):
        lines.append("💳 <b>Оплачен</b>"
                     + (" (онлайн)" if getattr(r, "pay_method", "") == "online" else ""))
    return "\n".join(lines)


def admin_request_detail(r, cur: str) -> str:
    uname = esc(r.user_name) if r.user_name else "—"
    lines = [f"🧾 <b>Заказ #{r.id}</b>",
             f"👤 {uname} (id <code>{r.user_id}</code>)\n", *_req_common(r),
             f"\nСтатус: <b>{REQ_STATUS_LABELS.get(r.status, r.status)}</b>",
             f"💰 Цена: <b>{fmt_money(r.price, cur)}</b>"]
    if int(getattr(r, "paid", 0) or 0):
        lines.append("💳 Оплачен"
                     + (" (онлайн)" if getattr(r, "pay_method", "") == "online" else ""))
    if getattr(r, "due_date", ""):
        lines.append(f"📅 Срок исполнения: <b>{esc(r.due_date)}</b>")
    if getattr(r, "promo", ""):
        mark = "✅ применён" if getattr(r, "promo_ok", 0) else "❌ не применён"
        lines.append(f"🎟 Промокод: <code>{esc(r.promo)}</code> ({mark})")
    if getattr(r, "src_url", ""):
        lines.append(f'🔗 Источник: <a href="{esc(r.src_url)}">открыть на сайте</a>')
    if getattr(r, "file_path", ""):
        lines.append(f"📁 Файл: <code>{esc(r.file_path)}</code>")
    if r.pending_price is not None:
        lines.append(f"⏳ Ожидает подтверждения новой цены: {fmt_money(r.pending_price, cur)}")
        if getattr(r, "price_comment", None):
            lines.append(f"💬 Ваш комментарий клиенту: <i>{esc(r.price_comment)}</i>")
    return "\n".join(lines)


def admin_new_order(r, cur: str) -> str:
    uname = f"@{r.user_name}" if r.user_name and not r.user_name.startswith("id ") else (r.user_name or "—")
    # пожелание клиента из поля комментария (d929c054) — мастеру сразу в уведомление
    comment = (f"\n💬 <i>{esc(r.description)}</i>"
               if getattr(r, "description", None) and not getattr(r, "is_custom", 0) else "")
    return (f"🔔 <b>Новый заказ #{r.id}</b>\n\n"
            f"👤 {esc(uname)} (id <code>{r.user_id}</code>)\n"
            f"📦 {esc(r.model_label)} · {r.quantity} шт\n"
            f"🧵 {esc(r.plastic_name)}\n"
            f"💰 <b>{fmt_money(r.price, cur)}</b>{comment}")


def admin_new_order_operator(r) -> str:
    """То же уведомление БЕЗ денег — для оператора (11.08).

    Событие операционное: оператору надо знать, что заказ пришёл и что печатать. А цена ему
    не полагается — роль оператора построена ровно на том, что выручки, балансов и цен он не
    видит (roles.ROLE_CAPS даёт только заказы и принтер). Слать ему обычный текст значило бы
    обойти роли через Telegram."""
    esc = html.escape
    uname = f"@{r.user_name}" if r.user_name and not r.user_name.startswith("id ") else (r.user_name or "—")
    comment = (f"\n💬 <i>{esc(r.description)}</i>"
               if getattr(r, "description", None) and not getattr(r, "is_custom", 0) else "")
    return (f"🔔 <b>Новый заказ #{r.id}</b>\n\n"
            f"👤 {esc(uname)}\n"
            f"📦 {esc(r.model_label)} · {r.quantity} шт\n"
            f"🧵 {esc(r.plastic_name)}{comment}")


# Способ получения (B5): значение из корзины → человекочитаемая подпись.
_DELIVERY_LABELS = {"pickup": "🏬 Самовывоз", "delivery": "🚚 Доставка"}


def _delivery_line(delivery_method: str, delivery_address: str, *, delivery_cost: float = 0.0,
                   delivery_type: str = "", cur: str = "") -> str:
    """Строка «способ получения + тип доставки + адрес + цена» для сводок заказа (A2).

    Пусто, если способ не выбран. Цена доставки печатается, только когда она реально есть:
    самовывоз и бесплатная доставка (порог «бесплатно от суммы») строку про деньги не получают."""
    label = _DELIVERY_LABELS.get(delivery_method or "", "")
    if not label:
        return ""
    line = label
    if (delivery_type or "").strip():
        line += f" · {esc(delivery_type.strip())}"
    if (delivery_method or "") == "delivery" and (delivery_address or "").strip():
        line += f": {esc(delivery_address.strip())}"
    if float(delivery_cost or 0) > 0:
        line += f" — {fmt_money(delivery_cost, cur or '')}"
    return line


def cart_order_breakdown(items: list, cur: str) -> str:
    """Построчный список позиций заказа-корзины (B5): «• Модель ×N — цена». Общий у сводки админу."""
    return "\n".join(f"• {esc(it.get('label') or '—')} ×{int(it.get('qty') or 1)} — "
                     f"{fmt_money(it.get('price') or 0, cur)}" for it in (items or []))


def admin_cart_order(*, first_name: str, gid: str, count: int, total: float, items: list,
                     cur: str, batch: bool = False, delivery_method: str = "",
                     delivery_address: str = "", comment: str = "", delivery_cost: float = 0.0,
                     delivery_type: str = "") -> str:
    """ОДНА сводка админам по заказу-корзине (B5) вместо N уведомлений per-позицию. Позиции —
    строками, общий итог — деривирован из их суммы, доставка/комментарий/партия — если заданы.
    total — сумма ПОЗИЦИЙ; платная доставка (A2) добавляет свою строку и общий итог с ней."""
    who = esc(first_name) if first_name else "клиент"
    head = (f"🧺 <b>Заказ-корзина: {count} "
            f"{plural_ru(count, 'позиция', 'позиции', 'позиций')}</b> от {who}")
    dcost = float(delivery_cost or 0)
    lines = [head, "", cart_order_breakdown(items, cur),
             (f"\n💰 Позиции: <b>{fmt_money(total, cur)}</b>" if dcost > 0
              else f"\n💰 Итого: <b>{fmt_money(total, cur)}</b>")]
    dline = _delivery_line(delivery_method, delivery_address, delivery_cost=dcost,
                           delivery_type=delivery_type, cur=cur)
    if dline:
        lines.append(dline)
    if dcost > 0:
        lines.append(f"💰 Итого с доставкой: <b>{fmt_money(total + dcost, cur)}</b>")
    if (comment or "").strip():
        lines.append(f"💬 <i>{esc(comment.strip())}</i>")
    if batch:
        lines.append("\n🧩 Клиент просит ПЕЧАТЬ ПАРТИЕЙ на один стол — сгруппируйте по "
                     "материалу+цвету и дайте цену дешевле.")
    lines.append(f"\n<code>группа {esc(gid)}</code>")
    return "\n".join(lines)


def user_cart_order_note(*, count: int, total: float, cur: str,
                         delivery_method: str = "", delivery_address: str = "",
                         delivery_cost: float = 0.0, delivery_type: str = "") -> str:
    """Приписка к ORDER_PLACED для заказа-корзины (B5): число позиций, общий итог, способ получения.
    Пусто-безопасна — при одиночной позиции/без доставки даёт лаконичную строку. total — сумма
    позиций; при платной доставке (A2) клиент видит и её, и итог с ней."""
    dcost = float(delivery_cost or 0)
    parts = [f"\n\n🧺 Позиций в заказе: <b>{count}</b> · "
             + (f"позиции <b>{fmt_money(total, cur)}</b>" if dcost > 0
                else f"итого <b>{fmt_money(total, cur)}</b>")]
    dline = _delivery_line(delivery_method, delivery_address, delivery_cost=dcost,
                           delivery_type=delivery_type, cur=cur)
    if dline:
        parts.append(f"\n{dline}")
    if dcost > 0:
        parts.append(f"\n💰 Итого с доставкой: <b>{fmt_money(total + dcost, cur)}</b>")
    return "".join(parts)


def user_order_delivery_note(*, delivery_type: str, address: str, cost: float, total: float,
                             cur: str) -> str:
    """Приписка про доставку к подтверждению ОДИНОЧНОГО заказа (без корзины).

    Клиент задал адрес один раз в профиле и заказывает в один тап — значит, про доставку и её
    цену он должен узнать здесь, иначе итог в «Моих заказах» окажется больше цены со сметы."""
    line = "\n\n🚚 Доставка"
    if (delivery_type or "").strip():
        line += f" · {esc(delivery_type.strip())}"
    if (address or "").strip():
        line += f": {esc(address.strip())}"
    if float(cost or 0) > 0:
        line += (f" — {fmt_money(cost, cur)}"
                 f"\n💰 Итого с доставкой: <b>{fmt_money(float(total or 0) + float(cost), cur)}</b>")
    else:
        line += " — бесплатно"
    return line + "\n<i>Адрес и способ доставки меняются в профиле.</i>"


def user_shipped(*, gid: str, carrier: str, number: str, url: str, count: int = 0) -> str:
    """Уведомление клиенту об отправке заказа (A2): перевозчик, номер и ссылка отслеживания."""
    head = "🚚 <b>Заказ отправлен!</b>"
    if count:
        head += (f"\n\n🧺 Позиций: {count}" if count > 1 else "")
    lines = [head]
    if (carrier or "").strip():
        lines.append(f"\n📮 Перевозчик: <b>{esc(carrier.strip())}</b>")
    if (number or "").strip():
        lines.append(f"🔢 Номер отправления: <code>{esc(number.strip())}</code>")
    if (url or "").strip():
        lines.append(f'\n<a href="{esc(url.strip())}">Отследить посылку</a>')
    lines.append("\n\nКак получите — отметьте заказ полученным или просто напишите нам.")
    return "".join(lines)


# Заказ без сметы, если автослайс не удался (OpenTodo 55a1cf8f): клиент не упирается в тупик —
# заявка уходит мастеру, цену он назначает вручную инструментом «Изменить цену».
UNSLICED_ADMIN_NOTE = ("⚠️ Автослайс не удался — модель не нарезалась автоматически. Откройте файл "
                       "(«Открыть в слайсере» / «Открыть папку» в карточке заказа) и назначьте цену "
                       "вручную: «Изменить цену» → «По граммам и времени».")
UNSLICED_CLIENT_NOTE = ("✅ <b>Заявка принята!</b>\n\nАвтоматический расчёт не получился — мастер "
                        "посмотрит модель и назовёт цену вручную. Она появится в «🧾 Мои заказы», "
                        "там же можно будет подтвердить или отказаться.")


# ---- Очередь досчёта (Э0, docs/Перспективные разработки/vps-relay-2026-10-04.md §1) ----
# Заказ уже есть, цену посчитает ПК сам — это НЕ «оценит мастер»: человек ничего не ждёт от
# мастера, он ждёт сообщения от бота. Короткая форма — для плашки цены в карточке.
PRICE_PENDING_SHORT = "Цена считается"
PRICE_PENDING_NOTE = "Цена считается — пришлём, как только посчитаем"
# Для строки «💰 Цена: …» карточки в боте: слово «Цена» там уже стоит в подписи.
PRICE_PENDING_IN_LINE = "считается"
PRICE_PENDING_TAIL = "пришлём, как только посчитаем"


def live_price_ready(rid: int, price: float, cur: str, *, taken: bool = False) -> str:
    """Клиенту: живой расчёт мини-аппы досчитал заказ, оформленный во время слайса (order_now).
    taken — мастер уже увёл заказ из «Новый» (взял в работу, отложил, ждёт оплаты), не
    дождавшись цены: обещание «мастер подтвердит и возьмёт в работу» тогда было бы неправдой."""
    head = f"✅ Заказ #{rid} посчитан: {fmt_money(price, cur)}."
    return head if taken else head + " Мастер подтвердит и возьмёт в работу."


def deferred_price_ready(r, price: float, cur: str) -> str:
    """Клиенту: ПК досчитал заказ, нужна его кнопка. Не «изменена цена» (менять было нечего —
    это первая цена заказа) и не «мастер подтвердит» (подтверждает как раз клиент)."""
    return (f"✅ Заказ <b>#{r.id}</b> «{esc(r.model_label)}» посчитан: <b>{fmt_money(price, cur)}</b>.\n\n"
            "Подтвердите цену — и заказ уйдёт в работу. Если не подходит, можно отказаться.")


def deferred_price_auto(r, price: float, cur: str) -> str:
    """Клиенту, разрешившему принимать цену без подтверждения (профиль, price_mode=auto): цена
    уже боевая. Объясняем, ПОЧЕМУ не спросили, и где это выключается — как user_price_auto_note."""
    return (f"✅ Заказ <b>#{r.id}</b> «{esc(r.model_label)}» посчитан: <b>{fmt_money(price, cur)}</b>.\n\n"
            "Подтверждение не потребовалось: вы разрешили принимать цену автоматически — "
            "заказ уже взят в работу.\n<i>Изменить это можно в профиле: «Подтверждение цены».</i>")


def deferred_admin_ready(rid: int, price: float, grams: float, cur: str, *, auto: bool) -> str:
    tail = ("клиент принимает цену автоматически — заказ в работе." if auto
            else "ждём подтверждения клиента.")
    return f"💰 Заказ #{rid} досчитан: {fmt_money(price, cur)} · {grams:.0f} г — {tail}"


def deferred_client_failed(rid: int, reason: str = "") -> str:
    """Клиенту: автоматом не вышло, цену называет мастер. reason — только наши честные тексты
    (файл больше 20 МБ, модель недоступна), сырые ошибки слайсера клиенту не показываем."""
    why = f"\n\n<i>{esc(reason)}</i>" if reason else ""
    return (f"⚠️ По заказу <b>#{rid}</b> посчитать цену автоматически не получилось — её назовёт "
            "мастер вручную. Она появится в «🧾 Мои заказы», там же можно будет подтвердить "
            f"или отказаться.{why}")


def deferred_admin_failed(rid: int, error: str) -> str:
    return (f"⚠️ Заказ #{rid}: ПК не смог досчитать цену — оцените вручную («Изменить цену» → "
            f"«По граммам и времени»).\nПричина: {esc(error or 'неизвестно')}")


def relay_order_admin_note(rid: int, label: str, user_name: str) -> str:
    return (f"🌙 Заказ #{rid} принят приёмной, пока ПК был выключен — считаю цену.\n"
            f"📦 {esc(label)} · 👤 {esc(user_name)}")


RELAY_ORDER_NOTE = ("🌙 Заказ принят, пока компьютер мастерской был выключен. Цену посчитаем "
                    "автоматически и пришлём клиенту на подтверждение.")


def price_stale_alert(price: float, cur: str) -> str:
    """Ответ на старую кнопку «Принять» с другой суммой: цену с тех пор поменяли. Принимать
    молча нельзя — клиент соглашался на другие деньги."""
    return f"Цена изменилась: теперь {fmt_money(price, cur)}. Проверьте и подтвердите заново."


def abandoned_calc_nudge(model_label: str, total_price: float, cur: str) -> str:
    """Догон брошенного расчёта (A5): клиент посчитал модель и не оформил заказ. Мягкое,
    без давления — модель сохранена, можно вернуться, когда удобно."""
    price = f" — примерно <b>{fmt_money(total_price, cur)}</b>" if total_price else ""
    return (f"👋 Вы недавно рассчитали «{esc(model_label)}»{price}, но так и не оформили заказ.\n\n"
            f"Модель сохранена — её можно открыть в разделе «📦 Мои модели» и оформить, когда будет "
            f"удобно. Если остались вопросы по цене или срокам — просто напишите нам.")


def user_status_note(r, cur: str) -> str:
    note = (f"🔔 Заказ <b>#{r.id}</b> «{esc(r.model_label)}»:\n"
            f"статус — <b>{REQ_STATUS_LABELS.get(r.status, r.status)}</b>")
    # «Взят в работу, срок 10.06»: показываем срок, если админ его задал
    if r.status == "in_progress" and getattr(r, "due_date", ""):
        note += f"\n📅 Срок исполнения: <b>{esc(r.due_date)}</b>"
    # «Отложен» без пояснения читается как отказ — объясняем, что заказ жив и никуда не делся.
    if r.status == "deferred":
        note += ("\n\nСейчас за него не берёмся, но заказ сохранён — вернёмся к нему позже. "
                 "Если передумаете, его можно отменить в «🧾 Мои заказы».")
        if getattr(r, "due_date", ""):
            note += f"\n📅 Ориентировочно вернёмся: <b>{esc(r.due_date)}</b>"
    return note


def user_price_note(r, cur: str) -> str:
    comment = ""
    if getattr(r, "price_comment", None):
        comment = f"\n💬 Комментарий: <i>{esc(r.price_comment)}</i>\n"
    return (f"💱 По заказу <b>#{r.id}</b> «{esc(r.model_label)}» изменена цена.\n\n"
            f"Было: {fmt_money(r.price, cur)}\n"
            f"Стало: <b>{fmt_money(r.pending_price, cur)}</b>\n"
            f"{comment}\n"
            "Подтвердите или откажитесь:")


def user_price_auto_note(r, old_price: float, new_price: float, cur: str, prefs: dict) -> str:
    """Цена выросла, но клиент заранее разрешил принимать такое без подтверждения.

    Уведомление обязательно и должно объяснять, ПОЧЕМУ у него не спросили: молчаливое
    подорожание — самый быстрый способ потерять доверие. Отсюда и напоминание, где настройка
    выключается."""
    why = ("вы разрешили принимать любые изменения цены"
           if (prefs or {}).get("price_mode") == "auto"
           else f"это меньше вашего порога в {float((prefs or {}).get('price_pct') or 0):g}%")
    return (f"💱 По заказу <b>#{r.id}</b> «{esc(r.model_label)}» изменилась цена.\n\n"
            f"Было: {fmt_money(old_price, cur)}\n"
            f"Стало: <b>{fmt_money(new_price, cur)}</b>\n\n"
            f"Подтверждение не потребовалось: {why} — заказ уже взят в работу.\n"
            "<i>Изменить это можно в профиле: «Подтверждение цены».</i>")


def user_balance_note(amount: float, new_balance: float, cur: str, reason: str = "",
                      comment: str = "") -> str:
    """Уведомление клиенту об изменении баланса (OpenTodo 8c428c43).
    comment — заметка мастера к транзакции (ccb1e4b6), показываем вместе с назначением."""
    head = "💰 Вам зачислено" if amount >= 0 else "💰 С вашего баланса списано"
    note = " · ".join(x for x in (reason, comment) if x)
    extra = f"\n<i>{esc(note)}</i>" if note else ""
    # без точки после суммы: валюта «руб.» сама кончается точкой — выходило «руб..» (0814912d)
    return (f"{head} <b>{fmt_money(abs(amount), cur)}</b>{extra}\n"
            f"Текущий баланс: <b>{fmt_money(new_balance, cur)}</b>")


def print_finished_admin(r) -> str:
    """Уведомление мастеру: печать по заказу, похоже, завершилась — нужно подтверждение (OpenTodo 70b5a590)."""
    return (f"🖨 Печать по заказу <b>#{r.id}</b> «{esc(r.model_label)}», похоже, завершилась.\n\n"
            "Проверьте результат и подтвердите: «Всё ок» переведёт заказ в "
            "<b>✅ Напечатано</b>, и клиенту уйдёт таймлапс печати.")


# Текст чат-опроса качества print_journal_survey выпилен вместе с самим опросом (b02bc4c6):
# оценка результата печати ставится в «Журнале печатей» приложения.


def print_classify(p: dict) -> str:
    """Печать завершена — просим мастера классифицировать: заказ или «для себя» (мастер-план, Фаза B)."""
    mins = int(round(p.get("minutes") or 0))
    hh, mm = mins // 60, mins % 60
    dur = (f"{hh} ч " if hh else "") + f"{mm} мин"
    extra = ""
    if p.get("grams"):
        extra += f" · ~{int(round(p['grams']))} г"
    if p.get("plastic_name"):
        extra += f" · {esc(p['plastic_name'])}"
    tail = (f"\nПохоже, это заказ <b>#{p['request_id']}</b> (был привязан)." if p.get("request_id")
            else "")
    return (f"🖨 Печать «<b>{esc(p.get('name') or '')}</b>» завершена — {dur}{extra}.{tail}\n\n"
            "Это <b>заказ</b> или печать <b>для себя</b>?\n"
            "• <b>Заказ</b> — прикреплю печать к нему (в «Напечатано» переводишь сам — на заказ "
            "может быть несколько печатей).\n"
            "• <b>Для себя</b> — учту как самопечать и посчитаю, во сколько обошлась бы заказом.")


def calc_ready_note(label: str, payload: dict) -> str:
    """«Расчёт готов» (OpenTodo ae4c6c13): юзер не стал ждать слайс — цена приходит сообщением."""
    note = (f"⚡ Расчёт готов: «{esc(label)}» — <b>{esc(str(payload.get('total') or ''))}</b>\n"
            "Откройте смету: можно поменять цвет и сразу оформить заказ.")
    if payload.get("approx"):
        note += "\n<i>Цена по данным MakerWorld — мастер уточнит при подтверждении заказа.</i>"
    return note


def mw_instant_note(label: str, payload: dict) -> str:
    """Мгновенная цена из профиля печати MakerWorld (вес+время уже посчитаны автором модели):
    редактируем ею ack «Начал расчёт», пока фоном скачивается файл для полноценной сметы."""
    return (f"⚡ <b>Быстрая оценка</b>: «{esc(label)}» — <b>{esc(str(payload.get('total') or ''))}</b>\n"
            f"{esc(str(payload.get('grams_total') or ''))} г · "
            f"{esc(str(payload.get('time_total') or ''))} — по данным MakerWorld.\n"
            "Секунду, готовлю смету с кнопкой заказа…")


def crm_task_reminder(t: dict) -> str:
    """Напоминание мастеру о задаче (CRM этап 4)."""
    extra = ""
    if t.get("user_id"):
        extra += f"\nКлиент: id {t['user_id']} (карточка — в приложении, раздел «Клиенты»)"
    if t.get("request_id"):
        extra += f"\nЗаказ: #{t['request_id']}"
    return f"📌 Напоминание: <b>{esc(t.get('title') or '')}</b>{extra}"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 печать / 2 печати / 5 печатей."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    return one if d == 1 else few if 2 <= d <= 4 else many


def user_timelapse_caption(r, n: int = 1) -> str:
    if n > 1:      # многостольный заказ: печатей несколько, и таймлапс есть у каждой
        return (f"🎬 Таймлапсы вашего заказа <b>#{r.id}</b> «{esc(r.model_label)}» — "
                f"{n} {plural_ru(n, 'печать', 'печати', 'печатей')}")
    return f"🎬 Таймлапс печати вашего заказа <b>#{r.id}</b> «{esc(r.model_label)}»"


def user_invoice_note(r, cur: str) -> str:
    """Счёт по индивидуальному заказу: мастер слайснул модель и подготовил расчёт (OpenTodo 6aaa6a19)."""
    comment = ""
    if getattr(r, "price_comment", None):
        comment = f"\n💬 Комментарий: <i>{esc(r.price_comment)}</i>\n"
    dev = ""
    if float(getattr(r, "dev_fee", 0.0) or 0.0) > 0:
        dev = f"\nв т.ч. разработка модели: {fmt_money(r.dev_fee, cur)}"
    return (f"🧾 По индивидуальному заказу <b>#{r.id}</b> «{esc(r.model_label)}» "
            "мастер подготовил расчёт.\n\n"
            f"Сумма к оплате: <b>{fmt_money(r.pending_price, cur)}</b>{dev}\n{comment}\n"
            "Откройте приложение — там полный расчёт и скрин из слайсера. Подтвердите или откажитесь:")


def desk_login_code(code: str, minutes: int) -> str:
    """Код входа в настольную админку. Код — В МОНОШИРИННОМ блоке: его перепечатывают руками
    с телефона на компьютер, и «0/O» с «1/l» в обычном шрифте путаются."""
    return (f"🔑 Код для входа в настольную админку:\n\n<code>{esc(code)}</code>\n\n"
            f"Действует {minutes} мин, одноразовый. Введите его на компьютере мастерской — "
            f"там, где открыта админка. Если код запросили не вы, просто не вводите его.")


def admin_order_created_note(r, cur: str) -> str:
    """Заказ оформил за клиента мастер — бот шлёт его клиенту на подтверждение (OpenTodo cd3258d6).
    Та же механика подтверждения, что у счёта (status='invoiced' + pending_price)."""
    comment = ""
    if getattr(r, "price_comment", None):
        comment = f"\n💬 Комментарий: <i>{esc(r.price_comment)}</i>\n"
    return (f"📦 <b>Мастер оформил заказ за вас</b>\n\n"
            f"📦 {esc(r.model_label)}\n"
            f"🖨 {esc(r.printer)} · 🧵 {esc(r.plastic_name)}\n"
            f"🔢 {r.quantity} шт\n\n"
            f"💰 Сумма к оплате: <b>{fmt_money(r.pending_price, cur)}</b>\n{comment}\n"
            "Проверьте параметры и подтвердите заказ — или откажитесь:")


def user_recalc_note(r, old_price: float, new_price: float, cur: str) -> str:
    """Уведомление клиента о СНИЖЕНИИ цены после пересчёта (применяется сразу, без подтверждения)."""
    return (f"💱 Цена по заказу <b>#{r.id}</b> «{esc(r.model_label)}» пересчитана.\n\n"
            f"Было: {fmt_money(old_price, cur)}\n"
            f"Стало: <b>{fmt_money(new_price, cur)}</b> 🎉\n\n"
            "Новая цена уже применена к заказу.")


def slice_error(msg: str) -> str:
    return (f"❌ <b>Не удалось рассчитать</b>\n\n{esc(msg)}\n\n"
            "Попробуйте другую модель/параметры или начните заново.")


# ---- Админка ----
ADMIN_MENU = "⚙️ <b>Админ-панель</b>\n\nВыберите раздел:"
NUMBER_BAD = "Нужно положительное число. Попробуйте ещё раз:"
INT_BAD = "Нужно целое число. Попробуйте ещё раз:"

# пластики
ADD_NAME_PROMPT = "➕ <b>Новый пластик</b>\n\nВведите название (например «Elegoo PLA чёрный»):"
ADD_MATERIAL_PROMPT = "Выберите тип материала (влияет на профиль слайсинга и подсказку плотности):"
ADD_PRICE_PROMPT = "Введите цену за <b>1 кг</b> (в {cur}), например 1500:"
ADD_DENSITY_PROMPT = ("Введите плотность филамента, г/см³. "
                      "Можно отправить «-», чтобы взять {hint} (по типу материала):")

# настройки
SETTING_PROMPT = "Введите новое значение для «{label}» (текущее: {value}):"
TEXT_PROMPT = "Введите новое значение для «{label}» (текущее: {value}). «-» — очистить:"

# принтеры
PR_NAME = "➕ <b>Новый принтер</b>\n\nНазвание принтера:"
PR_MACHINE = "Путь к JSON профиля принтера (machine), например:\n<code>C:\\Program Files\\ElegooSlicer\\resources\\profiles\\...\\machine\\...\\Printer 0.4 nozzle.json</code>"
PR_PROCESS = "Путь к папке профилей процессов (process dir) для этого принтера:"
PR_FILAMENT = "Путь к папке профилей филамента (filament dir) для этого принтера:"
PATH_BAD = "Путь не найден на диске. Проверьте и пришлите снова (или «Отмена»):"

# пресеты качества
Q_LABEL = "➕ <b>Пресет качества</b>\n\nНазвание (например «Высокое — 0.12 мм»):"
Q_LAYER = "Высота слоя, мм (например 0.12):"
Q_PROCESS = "Выберите базовый профиль процесса (из профилей принтера по умолчанию):"

# пресеты прочности
S_LABEL = "➕ <b>Пресет прочности</b>\n\nНазвание (например «Высокая»):"
S_INFILL = "Заполнение, % (0–100):"
S_WALLS = "Число периметров (стенок), напр. 3:"

# скидки
D_QTY = "➕ <b>Скидка за тираж</b>\n\nОт какого количества штук действует? (например 5):"
D_PCT = "Размер скидки, % (например 10):"

# пользовательские оттенки цветов
SHADE_NAME = ("🎨 <b>Оттенок цвета</b>\n\nВведите название цвета (например «изумрудный» "
              "или «золото»). Оно будет распознаваться в названиях цветов пластика:")
SHADE_HEX = "Введите HEX-код для «{name}» (например <code>#2ecc71</code> или <code>2ecc71</code>):"
SHADE_BAD = "Нужен HEX вида #RRGGBB (например #2ecc71). Попробуйте снова:"
SHADES_TITLE = ("🎨 <b>Оттенки цветов</b>\n\nТочные цвета для превью по названию. Переливание "
                "задаётся через тире в названии цвета пластика: «зелёный-красный-синий».\n"
                "Нажмите запись, чтобы удалить.")
SHADES_EMPTY = ("🎨 <b>Оттенки цветов</b>\n\nПока нет своих оттенков — используются встроенные. "
                "Добавьте точный HEX для нужного названия.")

# персональные скидки клиентам
UD_PICK = ("👤 <b>Персональная скидка</b>\n\n"
           "Нажмите кнопку «Выбрать пользователя» ниже и выберите клиента из списка.\n\n"
           "Можно также поделиться его контактом или прислать числовой Telegram ID. "
           "Отмена — /admin.")
UD_NO_USER = ("⚠️ У контакта не оказалось Telegram-ID (или он скрыт). Нужен контакт "
              "<b>пользователя Telegram</b>, либо пришлите его числовой ID.")
UD_PCT = "Размер персональной скидки для <b>{name}</b>, % (например 10):"

# объединение счетов «семьи» (OpenTodo): перенос всего со второго счёта на основной
MERGE_HELP = (
    "🔗 <b>Объединить счета</b>\n\n"
    "Объедините счета членов семьи в один профиль для удобства.\n"
    "История заказов, баланс и скидки будут перенесены на основной счёт."
)

# ---- идеи пользователей ----
IDEA_PROMPT = (
    "💡 <b>Предложить идею</b>\n\n"
    "Напишите, что улучшить, добавить или исправить. "
    "Идея отправится нам напрямую — читаем всё!\n\n"
    "<i>Отправьте текст или нажмите «Отмена».</i>"
)
IDEA_TOO_SHORT = "Текст слишком короткий — напишите хотя бы пару слов:"
IDEA_THANKS = "🙌 Спасибо за идею! Обязательно рассмотрим."


def admin_idea(user, text: str) -> str:
    uname = f"@{user.username}" if getattr(user, "username", None) else "—"
    return (
        "💡 <b>Новая идея от пользователя</b>\n\n"
        f"👤 {esc(user.full_name)} ({uname}, id <code>{user.id}</code>)\n\n"
        f"{esc(text)}"
    )


# ---- обслуживание принтера (ТО) ----
def service_due_alert(it) -> str:
    units = {"hours": "ч", "prints": "печатей", "days": "дн"}.get(it.trigger_type, "")
    return (f"🛠 Пора обслужить: <b>{esc(it.label)}</b> — "
            f"достигнут порог {it.threshold:g} {units} с прошлого ТО.")
