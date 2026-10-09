"""Модели слоя данных: dataclass-ы строк БД и статусы заказов.

Один предмет: типы, которыми слой данных отдаёт строки наверх (Plastic, Printer,
PrinterNozzle, QualityPreset, StrengthPreset, DiscountTier, PostprocOption, SavedModel,
CollectionItem, Request, ServiceItem, Product) и словарь статусов заказа с перечнем
отменяемых — они живут рядом с Request, потому что описывают его поле `status`.

Выделено из app/db.py волной 1 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО; у моделей нет ни одной ссылки на функции db — модуль не зависит ни
от core, ни от db, и любой доменный модуль store/ может типизироваться от него, не
замыкая цикла. Фасад db.py реэкспортирует каждое имя строкой — потребители по-прежнему
пишут `db.Plastic`, `from .db import REQ_STATUS_LABELS`, и это ТОТ ЖЕ объект.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Plastic:
    id: int
    name: str
    price_per_kg: float
    density: float
    is_active: int
    material_type: str = "PLA"
    filament_profile: str = ""
    photo_path: str = ""
    description: str = ""
    uses: int = 0
    colors: str = ""   # цвета через запятую
    is_default: int = 0
    stock_grams: float = 0.0     # остаток филамента, г (0 = склад не ведётся)
    low_threshold: float = 0.0   # порог «мало», г (0 = без предупреждений)
    failure_rate_pct: float | None = None   # индивид. % брака (None = общий settings.failure_rate_pct)
    best_quality_id: int | None = None      # лучший профиль печати для этого пластика (94cc52c7)
    price_factor: float = 1.0    # коэффициент сложности работы с материалом (админ; клиент не видит)
    category: str = ""           # категория витрины (3af25fc2): materials.PLASTIC_CATEGORIES
    # Вес ПУСТОЙ катушки, г — тара. Мастер взвешивает катушку целиком, и остаток филамента =
    # показание весов − эта тара. 0 = не замерена. spool_note — оговорки к таре свободным
    # текстом: у части материалов катушка зависит от партии (картон 150 г / пластик 195 г
    # у Elegoo PLA Matte), одним числом это не выразить.
    spool_weight_g: float = 0.0
    spool_note: str = ""

    @property
    def price_per_gram(self) -> float:
        # Цена за грамм С УЧЁТОМ коэффициента сложности — единственная цена, которой считаем.
        return self.price_per_kg * (self.price_factor or 1.0) / 1000.0

    @property
    def eff_price_per_kg(self) -> float:
        """Цена за кг × коэффициент сложности — «ценник», который показываем клиенту.
        Сам коэффициент клиенту нигде не показываем (видит только итоговую цену)."""
        return round(self.price_per_kg * (self.price_factor or 1.0), 2)

    @property
    def color_list(self) -> list[str]:
        return [c.strip() for c in (self.colors or "").split(",") if c.strip()]


@dataclass
class Printer:
    id: int
    name: str
    machine_profile: str
    process_dir: str
    filament_dir: str
    is_active: int
    is_default: int
    # Как с ним разговаривать (OpenTodo 0613014e): пусто = определить по .env, как раньше.
    kind: str = ""
    address: str = ""
    access_code: str = ""
    serial: str = ""
    slicer: str = ""          # чем резать задания этой машины ("" = движок по умолчанию)
    slicer_path: str = ""     # путь к exe, если он не там, где обычно
    # Множитель времени печати этой машины (B1): оценка слайсера/fastcalc × time_factor.
    # 1.0 = как откалибровано (CC2); медленнее/быстрее машина — мастер ставит сам, подсказка
    # по журналу печатей — db.printer_time_stats. Границы TIME_FACTOR_MIN..MAX у store.
    time_factor: float = 1.0


@dataclass
class PrinterNozzle:
    """Сопло принтера + машинный профиль слайсера под него (OpenTodo f71f4973)."""
    id: int
    printer_id: int
    nozzle: str
    machine_profile: str
    note: str = ""
    is_default: int = 0


@dataclass
class QualityPreset:
    id: int
    label: str
    layer_height: float
    process: str
    sort: int
    is_active: int
    is_default: int = 0
    # Сопло, которым печатается этот профиль. Пусто — не проставлено явно: разбираем имя
    # процесс-файла (см. nozzle_of). Держим строкой: ключ калибровки сравнивается строкой.
    nozzle: str = ""


@dataclass
class StrengthPreset:
    id: int
    label: str
    infill: int
    walls: int
    sort: int
    is_active: int
    is_default: int = 0


@dataclass
class DiscountTier:
    id: int
    min_qty: int
    discount_pct: float


@dataclass
class PostprocOption:
    id: int
    name: str
    price: float                    # per_part=0 — цена за заказ; per_part=1 — ставка ЗА ДЕТАЛЬ
    is_active: int = 1
    sort: int = 0
    per_part: int = 0               # 1 = множить на число деталей модели × тираж (accee7a9)


@dataclass
class SavedModel:
    id: int
    user_id: int
    hash: str
    label: str
    paths: list           # list[str] — пути к файлам модели
    preview_path: str | None
    printer_id: int | None
    plastic_id: int | None
    quality_id: int | None
    strength_id: int | None
    quantity: int | None
    updated_at: int
    multicolor: int = 0
    plates: int = 1


@dataclass
class CollectionItem:
    id: int
    hash: str
    label: str
    paths: list
    preview_path: str | None
    photo_path: str | None      # реальное фото печати (добавляет админ)
    added_by: int | None
    uses: int
    created_at: int
    palette: str = ""           # палитра hex из фото печати (через запятую)
    dims: str = ""              # габариты модели, напр. «60×88×23 мм»
    size: float = 0.0           # объём габ. коробки (мм³) — для сортировки по размеру
    last_material: str = ""     # материал последнего расчёта
    last_calc_at: int = 0       # время последнего расчёта (unix)
    multicolor: int = 0         # модель многоцветная (для бейджа)
    plates: int = 1             # столов в проекте
    last_price: float = 0.0     # цена за 1 шт из последнего расчёта — «от N» на плитке (ux-simpl. №3)
    category: str = ""          # категория каталога (c9c42789); '' = вне каталога («Мои»/поиск)
    tags: str = ""              # теги через запятую от ИИ-теггера (e0dedc27) — участвуют в поиске
    orig_label: str = ""        # исходное название до ИИ-переименования (e0dedc27)


# Статусы заказов
REQ_STATUS_LABELS = {
    "new": "🆕 Новый",
    # Мастер одобрил заказ и выставил счёт (A1-этап, фидбэк 31.07): клиент получил инвойс,
    # оплата переведёт заказ в «В работе» автоматически (payments.record_success).
    "awaiting_payment": "💳 Ждёт оплаты",
    "in_progress": "🔧 В работе",
    "printed": "✅ Напечатано",
    "rejected": "🚫 Отказ",
    "price_changed": "💱 Изменена цена — нужно подтверждение",
    "invoiced": "🧾 Счёт выставлен — нужно подтверждение",
    "cancelled": "🗑 Отменён клиентом",
    # заказ, который сейчас не берём (нет материала, занят принтер, ждём деталей), но клиент
    # хочет сохранить его на будущее. Это НЕ отказ: заказ жив, просто вне очереди — под него
    # не резервируется филамент (_RESERVING_STATUSES) и он не считается в загрузку (queue_load).
    "deferred": "⏸ Отложен",
    "self": "🏠 Печать для себя",   # заказ-самопечать мастера (вне клиентской выручки)
    # заказ ГОТОВОГО ИЗДЕЛИЯ (product_id): товар уже напечатан, у него ровно два статуса —
    # «Новый» и «Вручено» (OpenTodo 26a44d79)
    "handed": "🤝 Вручено",
}

# Статусы, при которых клиент может отменить заказ. Отложенный — тем более: он висит
# неизвестно сколько, и не дать клиенту закрыть его было бы издевательством.
REQ_CANCELLABLE = ("new", "awaiting_payment", "in_progress", "price_changed", "invoiced", "deferred")


@dataclass
class Request:
    id: int
    user_id: int
    user_name: str
    model_label: str
    printer: str
    plastic_name: str
    quantity: int
    quality: str
    strength: str
    grams_total: float
    minutes_total: float
    price: float
    status: str
    pending_price: float | None
    preview_path: str | None
    created_at: int
    updated_at: int
    price_comment: str | None = None   # необязательный комментарий админа к смене цены
    dims: str = ""                     # габариты модели, напр. «60×88×23 мм»
    multicolor: int = 0                # модель многоцветная (для бейджа)
    plates: int = 1                    # столов в проекте
    hash: str = ""                     # хэш модели — связь с saved_models для «Повторить»
    rating: int | None = None          # оценка клиента после печати (1–10; bdcc18e4, старые 1–5 ×2)
    review: str | None = None          # текстовый отзыв
    done_photo: str | None = None      # фото готовой печати (от админа)
    src_url: str = ""                  # ссылка-источник модели (MakerWorld/Printables/…), если был URL
    file_path: str = ""                # локальный путь к сохранённому файлу модели (для админа)
    promo: str = ""                    # промокод, который ввёл клиент (пишем даже если не применился)
    promo_ok: int = 0                  # 1 — промокод применился; 0 — введён, но не сработал
    quality_id: int | None = None      # пресет качества (для пересохранения .3mf с пресетом, #6)
    strength_id: int | None = None     # пресет прочности (для пересохранения .3mf с пресетом, #6)
    is_custom: int = 0                 # 1 — индивидуальный заказ (текст + фото, без авторасчёта)
    description: str | None = None     # текст индивидуального заказа от клиента
    photos: list = field(default_factory=list)  # пути к приложенным фото индивидуального заказа
    breakdown: dict | None = None      # снимок расчётника цены (граммы/время/материал/наценка/…)
    due_date: str = ""                 # срок исполнения «DD.MM» (админ задаёт при «В работе»)
    dev_fee: float = 0.0               # плата за разработку модели (индивид. заказ, может быть 0)
    slicer_shot: str | None = None     # путь к скрину из слайсера, приложенному к счёту
    timelapse: str | None = None       # путь к таймлапсу печати, привязанному к заказу
    created_by_admin_id: int | None = None  # id админа, оформившего заказ за клиента (None = сам клиент)
    paid: int = 0                      # 1 — заказ оплачен (галочка мастера; рефералка 7f257a33)
    paid_at: int = 0                   # когда оплачен, unix (0 = не оплачен/старый заказ; A1)
    pay_method: str = ""               # '' | 'online' (Telegram Payments) | 'manual' (галочка мастера)
    reject_reason: str | None = None   # причина отказа — аналитика воронки (crm-roadmap этап 6)
    unsliced: int = 0                  # 1 — оформлен без сметы: автослайс не удался, цену
                                        # назначает мастер вручную (OpenTodo 55a1cf8f)
    cogs_actual: float | None = None   # реальная себестоимость по факту (Фаза C мастер-плана):
                                        # граммы из stock_ledger × цена катушки; None — не считалась
    product_id: int | None = None      # заказ готового изделия из витрины (OpenTodo bd896aa6)
    urgency: str = ""                  # срочность заказа (B3): '' = обычная, 'rush' = срочная
    promised_date: str = ""            # обещанный клиенту срок «DD.MM», зафиксирован при оформлении (B3)
    order_group: str = ""              # группа заказа-корзины (B5): общий hex у позиций одного
                                        # заказа; '' = одиночный заказ (НЕ группируется)
    price_pending: int = 0             # 1 — цену посчитает ПК сам (очередь досчёта deferred_calc,
                                        # Э0); это НЕ «оценит мастер»: unsliced=1 при нём — пока
                                        # просто «сметы ещё нет». Смысл флаг имеет только при status='new'


@dataclass
class ServiceItem:
    id: int
    key: str | None
    label: str
    trigger_type: str          # 'hours' | 'prints' | 'days'
    threshold: float
    hours_since: float
    prints_since: int
    last_service_at: int
    is_active: int
    notified: int
    sort: int = 0

    def progress(self, now: int) -> tuple[float, float]:
        """(текущее значение, порог) в единицах trigger_type — для % и текста UI."""
        if self.trigger_type == "prints":
            return (float(self.prints_since), self.threshold)
        if self.trigger_type == "days":
            return ((now - self.last_service_at) / 86400.0, self.threshold)
        return (self.hours_since, self.threshold)   # 'hours' (по умолчанию)

    def is_due(self, now: int) -> bool:
        cur, thr = self.progress(now)
        return thr > 0 and cur >= thr


@dataclass
class Product:
    id: int
    title: str
    description: str = ""
    price: float = 0.0
    stock: int = 0
    photo_path: str = ""
    is_active: int = 1
    sold: int = 0
    sort: int = 0
    created_at: int = 0
    updated_at: int = 0
