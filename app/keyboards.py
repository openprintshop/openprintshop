"""Инлайн-клавиатуры."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import config
from .db import (
    Plastic,
    Printer,
)

QUANTITY_QUICK = [1, 2, 3, 5, 10]

# request_id нативного пикера «выбрать близкого» для семейного счёта (OpenTodo 0528ba47):
# по нему users_shared-хендлер отличает наш запрос от любых будущих пикеров.
FAMILY_PICK_REQUEST_ID = 7788


def family_pick_partner() -> ReplyKeyboardMarkup:
    """Нативный выбор партнёра для семейного счёта (OpenTodo 0528ba47): Telegram открывает
    список контактов/чатов, бот получает точный user_id выбранного (users_shared) — без
    ручного ввода «@username или имя»."""
    btn = KeyboardButton(
        text="👤 Выбрать близкого из контактов",
        request_users=KeyboardButtonRequestUsers(
            request_id=FAMILY_PICK_REQUEST_ID, user_is_bot=False, max_quantity=1,
            request_name=True, request_username=True))
    return ReplyKeyboardMarkup(keyboard=[[btn]], resize_keyboard=True,
                               one_time_keyboard=True,
                               input_field_placeholder="Выберите близкого кнопкой ниже")


# ===== Главное меню =====
def main_menu(is_admin: bool, has_saved: bool = False, has_collection: bool = False,
              has_requests: bool = False, webapp_url: str | None = None,
              pending: int = 0) -> InlineKeyboardMarkup:
    # Чат-меню намеренно минимально: только «Открыть приложение» (вся работа — в приложении,
    # где есть заказы/модели/коллекция/каталог/инвайт/помощь). Админу — ещё «Админ-панель».
    # has_saved/has_collection/has_requests/pending больше не влияют на меню (оставлены в
    # сигнатуре для совместимости вызовов).
    kb = InlineKeyboardBuilder()
    if webapp_url:
        kb.button(text="Открыть приложение",
                  web_app=WebAppInfo(url=webapp_url.rstrip("/") + "/app/?v=" + config.webui_version()))
    else:
        kb.button(text="🧮 Рассчитать стоимость", callback_data="m:calc")  # фолбэк, если приложение недоступно
    if is_admin and webapp_url:        # чат-админка убрана — кнопка ведёт в админку приложения
        kb.button(text="⚙️ Админ-панель",
                  web_app=WebAppInfo(url=webapp_url.rstrip("/") + "/app/?v=" + config.webui_version() + "&go=admin"))
    kb.adjust(1)
    return kb.as_markup()


def user_requests(items) -> InlineKeyboardMarkup:
    from .db import REQ_STATUS_LABELS
    kb = InlineKeyboardBuilder()
    for r in items:
        st = REQ_STATUS_LABELS.get(r.status, r.status).split(" ", 1)
        emoji = st[0]
        kb.button(text=f"{emoji} #{r.id} · {r.model_label}", callback_data=f"ureq:{r.id}")
    kb.button(text="⬅️ В меню", callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def user_request_item(r, pay_on: bool = False) -> InlineKeyboardMarkup:
    """pay_on — онлайн-оплата настроена (payments.enabled()); флаг передаёт вызывающий,
    потому что клавиатуры синхронные, а настройка живёт в БД."""
    from . import payments
    from .db import REQ_CANCELLABLE
    kb = InlineKeyboardBuilder()
    if r.status == "price_changed":
        kb.button(text="✅ Принять новую цену", callback_data=_uacc_data(r.id, r.pending_price))
        kb.button(text="❌ Отказаться", callback_data=f"udec:{r.id}")
    if payments.is_payable(r, pay_on):
        kb.button(text="💳 Оплатить онлайн", callback_data=f"upay:{r.id}")
    if r.status in REQ_CANCELLABLE:
        kb.button(text="🗑 Отменить заказ", callback_data=f"ucancel:{r.id}")
    kb.button(text="⬅️ К заказам", callback_data="m:myorders")
    kb.button(text="🏠 В меню", callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def _uacc_data(rid: int, amount: float | None) -> str:
    """callback «Принять цену». С суммой (в копейках) кнопка принимает РОВНО ту цену, что видел
    клиент: мастер мог поменять её дважды, а цену из очереди досчёта — пересчитать, и старая
    кнопка без суммы приняла бы текущую, которую клиент не видел. Без суммы — как раньше
    (такие кнопки уже лежат в старых сообщениях, их ломать нельзя)."""
    if amount is None:
        return f"uacc:{rid}"
    return f"uacc:{rid}:{int(round(float(amount) * 100))}"


def price_confirm(rid: int, amount: float | None = None, *, webapp_url: str | None = None,
                  accept_text: str = "✅ Принять новую цену") -> InlineKeyboardMarkup:
    """Принять / отказаться от цены заказа. amount — сумма, на которую соглашается клиент
    (см. _uacc_data); webapp_url — добавить «Открыть в приложении» на экран заказов."""
    kb = InlineKeyboardBuilder()
    kb.button(text=accept_text, callback_data=_uacc_data(rid, amount))
    kb.button(text="❌ Отказаться", callback_data=f"udec:{rid}")
    if webapp_url:
        kb.button(text="Открыть в приложении",
                  web_app=WebAppInfo(url=webapp_url.rstrip("/") + "/app/?v=" + config.webui_version()
                                     + "&go=orders"))
    kb.adjust(1)
    return kb.as_markup()


def rate_request(rid: int) -> InlineKeyboardMarkup:
    """Оценка заказа по 10-балльной шкале (bdcc18e4; раньше 1–5) — два ряда по 5 кнопок."""
    kb = InlineKeyboardBuilder()
    for n in range(1, 11):
        kb.button(text=f"{n}⭐", callback_data=f"rate:{rid}:{n}")
    kb.adjust(5, 5)
    return kb.as_markup()


def print_confirm(rid: int) -> InlineKeyboardMarkup:
    """Подтверждение мастером завершения печати по заказу (OpenTodo 70b5a590).
    «Всё ок» → статус «напечатано» + клиенту уйдёт таймлапс."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Всё ок — напечатано", callback_data=f"pok:{rid}")
    kb.button(text="❌ Не ок", callback_data=f"pnok:{rid}")
    kb.adjust(1)
    return kb.as_markup()


# Клавиатура опроса качества print_journal_survey выпилена вместе с чат-опросом (b02bc4c6):
# оценка ставится в «Журнале печатей» приложения. Обработчики pj:-кнопок в user_flow ОСТАВЛЕНЫ —
# старые сообщения в чате продолжают работать (в т.ч. pj_later ниже).


def print_classify(jid: int, rid: int | None = None,
                   webapp_url: str | None = None) -> InlineKeyboardMarkup:
    """Классификация завершённой печати (мастер-план, Фаза B): заказ или «для себя». Если печать
    была привязана к заказу (rid) — предлагаем подтвердить именно его. jid — id записи журнала.
    Оценка результата — в «Журнале печатей» приложения (чат-опрос выпилен, b02bc4c6).

    webapp_url задан → вместо неудобного ввода «№ заказа текстом» даём кнопку-вебапку
    «Выбрать заказ в приложении» (OpenTodo 4effbc1b): экран printclassify со списком
    активных заказов с картинками. Без URL — прежний текстовый фолбэк pe:oth."""
    kb = InlineKeyboardBuilder()
    if rid:
        kb.button(text=f"✅ К заказу #{rid}", callback_data=f"pe:ord:{jid}:{rid}")
    if webapp_url:
        url = (webapp_url.rstrip("/") + "/app/?v=" + config.webui_version()
               + "&go=printclassify&id=" + str(jid))
        kb.button(text="📦 Выбрать заказ в приложении", web_app=WebAppInfo(url=url))
    else:
        kb.button(text="📦 К заказу №…" if not rid else "📦 Другой заказ №…",
                  callback_data=f"pe:oth:{jid}")
    kb.button(text="🏠 Для себя", callback_data=f"pe:self:{jid}")
    kb.adjust(1)
    return kb.as_markup()


def pj_later(jid: int) -> InlineKeyboardMarkup:
    """Единственная кнопка «оценю позже» — выход из обязательного шага фото (OpenTodo 7bb76b35)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🕒 Оценю позже", callback_data=f"pj:later:{jid}")
    return kb.as_markup()


def broadcast_footer(webapp_url: str | None, go: str = "",
                     button_text: str = "Открыть приложение") -> InlineKeyboardMarkup:
    """Подвал рассылки (CRM этап 5): открыть приложение + отписаться от рассылок.
    go — deep-link экран (например collection для «модели дня», 6fbed3ad)."""
    kb = InlineKeyboardBuilder()
    if webapp_url:
        url = webapp_url.rstrip("/") + "/app/?v=" + config.webui_version()
        if go:
            url += f"&go={go}"
        kb.button(text=button_text, web_app=WebAppInfo(url=url))
    kb.button(text="🔕 Отписаться от рассылок", callback_data="unsub")
    kb.adjust(1)
    return kb.as_markup()


def crm_task_done(tid: int) -> InlineKeyboardMarkup:
    """Кнопка под напоминанием мастеру о задаче (CRM этап 4)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделано", callback_data=f"ct:done:{tid}")
    return kb.as_markup()


def open_app(webapp_url: str | None, *, go: str | None = None, oid: int | None = None,
             token: str | None = None, text: str = "Открыть в приложении") -> InlineKeyboardMarkup | None:
    """Одна кнопка-вебапка «Открыть в приложении» с адресацией на нужный экран.
    `go` — экран мини-аппы (orders/adminorder/printer/…), `oid` — id заказа для адресации,
    `token` — токен модели для go=quote (смета из уведомления «расчёт готов», ae4c6c13).
    Управление вынесено в приложение, поэтому в уведомлениях остаётся только этот переход.
    Возвращает None, если публичный URL мини-аппы не задан — вызывающий код оставит свой фолбэк."""
    if not webapp_url:
        return None
    url = webapp_url.rstrip("/") + "/app/?v=" + config.webui_version()
    if go:
        url += "&go=" + go
    if oid:
        url += "&id=" + str(oid)
    if token:
        url += "&t=" + str(token)
    kb = InlineKeyboardBuilder()
    kb.button(text=text, web_app=WebAppInfo(url=url))
    return kb.as_markup()


def quote_actions(webapp_url: str | None, token: str, *, order_text: str | None,
                  quote_text: str = "Открыть смету") -> InlineKeyboardMarkup:
    """Кнопки под быстрой сметой в чате (ux-paths-next.md, идеи 1+3): «Заказать» прямо из чата
    без открытия приложения (callback qorder:<token>) + «Открыть смету» (deep-link go=quote&t=…,
    где можно сменить пластик/цвет/масштаб). order_text=None — только смета (заказ уже
    оформлен/цена ещё считается)."""
    kb = InlineKeyboardBuilder()
    if order_text:
        kb.button(text=order_text, callback_data=f"qorder:{token}")
    if webapp_url:
        url = (webapp_url.rstrip("/") + "/app/?v=" + config.webui_version()
               + "&go=quote&t=" + str(token))
        kb.button(text=quote_text, web_app=WebAppInfo(url=url))
    kb.adjust(1)
    return kb.as_markup()


def skip_review() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="review:skip")
    return kb.as_markup()


def collection_list(items, webapp_url: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if webapp_url:
        kb.button(text="🖼 Открыть страницей (листать)", web_app=WebAppInfo(url=webapp_url))
    for it in items:
        suffix = f"  ·  ×{it.uses}" if it.uses > 1 else ""
        kb.button(text=f"📦 {it.label}{suffix}", callback_data=f"col:{it.id}")
    kb.button(text="⬅️ В меню", callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def collection_item(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧮 Рассчитать эту модель", callback_data=f"colgo:{cid}")
    kb.button(text="⬅️ К коллекции", callback_data="m:collection")
    kb.button(text="🏠 В меню", callback_data="m:home")
    kb.adjust(1, 2)
    return kb.as_markup()


def mw_variants(variants) -> InlineKeyboardMarkup:
    """Выбор варианта модели MakerWorld (несколько профилей одной модели, #14)."""
    kb = InlineKeyboardBuilder()
    for i, v in enumerate(variants):
        title = (v.get("title") or f"Вариант {i + 1}")[:55]
        kb.button(text=f"🔹 {title}", callback_data=f"mwv:{v['id']}")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(1)
    return kb.as_markup()


def model_step(has_saved: bool = False, can_search: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_search:    # поиск моделей по запросу прямо в боте (опц.; ведёт в Calc.search_results)
        kb.button(text="🔎 Найти модель", callback_data="m:search")
    if has_saved:
        kb.button(text="📂 Ранее отправленные", callback_data="m:mymodels")
    # кнопки-ссылки для поиска модели на популярных площадках (открываются в браузере)
    kb.button(text="🌐 MakerWorld", url="https://makerworld.com")
    kb.button(text="🌐 Printables", url="https://www.printables.com")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    # «Найти модель» / «Ранее отправленные» — отдельными строками; площадки — рядом; «Отмена» — отдельно
    sizes = ([1] if can_search else []) + ([1] if has_saved else []) + [2, 1]
    kb.adjust(*sizes)
    return kb.as_markup()


def search_results(hits: list[dict]) -> InlineKeyboardMarkup:
    """Список найденных моделей inline-кнопками (поиск из бота). callback find:<source>:<ext_id>
    — по нему восстанавливаем page_url из FSM и качаем обычным fetch_from_url."""
    src_emoji = {"printables": "🟠", "thingiverse": "🔵", "makerworld": "🟢"}
    kb = InlineKeyboardBuilder()
    for hit in hits:
        emoji = src_emoji.get(hit.get("source", ""), "▫️")
        title = (hit.get("title") or "Модель")[:55]
        kb.button(text=f"{emoji} {title}", callback_data=f"find:{hit['source']}:{hit['ext_id']}")
    kb.button(text="⬅️ Назад", callback_data="nav:model")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(*([1] * len(hits)), 2)
    return kb.as_markup()


def saved_models(items) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for m in items:
        kb.button(text=f"📦 {m.label}", callback_data=f"sm:{m.id}")
    kb.button(text="⬅️ Назад", callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def reuse_settings(sid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Рассчитать с теми же", callback_data="rmgo")
    kb.button(text="✏️ Изменить параметры", callback_data="rmedit")
    kb.button(text="🗑 Убрать из истории", callback_data=f"smdel:{sid}")
    kb.button(text="⬅️ К списку", callback_data="m:mymodels")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(1, 1, 1, 2)
    return kb.as_markup()


def back_home() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 В меню", callback_data="m:home")
    return kb.as_markup()


def cancel_only() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    return kb.as_markup()


# ===== Мастер расчёта =====
def printers_user(printers: list[Printer]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in printers:
        kb.button(text=f"🖨 {p.name}", callback_data=f"prn:{p.id}")
    kb.button(text="⬅️ Назад", callback_data="nav:model")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(1)
    return kb.as_markup()


def materials(plastics: list[Plastic], webapp_url: str | None = None,
              default_combo: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if default_combo:
        kb.button(text=f"⚡ По умолчанию: {default_combo}", callback_data="mat:quick")
    for p in plastics:
        star = " ⭐" if p.is_default else ""
        kb.button(text=f"🧵 {p.name}{star}", callback_data=f"mat:{p.id}")
    if webapp_url:
        kb.button(text="📖 Каталог страницей (фото, свойства)",
                  web_app=WebAppInfo(url=webapp_url.rstrip("/") + "/catalog"))
    kb.button(text="📖 Каталог пластиков", callback_data="mat:catalog")
    kb.button(text="⬅️ Назад", callback_data="nav:printer")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(1)
    return kb.as_markup()


def colors(plastic, selected: list | None = None) -> InlineKeyboardMarkup:
    """Мультивыбор цветов: отмеченные — с ✅; «Готово» подтверждает. Несколько → переливание."""
    selected = selected or []
    kb = InlineKeyboardBuilder()
    n = len(plastic.color_list)
    for i, c in enumerate(plastic.color_list):
        mark = "✅ " if c in selected else "🎨 "
        kb.button(text=f"{mark}{c}", callback_data=f"clr:{i}")
    if selected:
        kb.button(text=f"✅ Готово ({len(selected)})", callback_data="clr:done")
    kb.button(text="⬅️ Назад", callback_data="nav:material")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    sizes = [2] * (n // 2)
    if n % 2:
        sizes.append(1)
    if selected:
        sizes.append(1)   # «Готово» — отдельной строкой
    sizes.append(2)       # «Назад» + «Отмена»
    kb.adjust(*sizes)
    return kb.as_markup()


def catalog_card(pid: int, idx: int, total: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️", callback_data="cat:prev")
    kb.button(text=f"{idx + 1}/{total}", callback_data="cat:noop")
    kb.button(text="▶️", callback_data="cat:next")
    kb.button(text="✅ Выбрать этот пластик", callback_data=f"cat:pick:{pid}")
    kb.button(text="⬅️ К списку", callback_data="nav:material")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(3, 1, 2)
    return kb.as_markup()


def quantity() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in QUANTITY_QUICK:
        kb.button(text=str(n), callback_data=f"qty:{n}")
    kb.button(text="✏️ Другое", callback_data="qty:custom")
    kb.button(text="⬅️ Назад", callback_data="nav:material")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(5, 1, 2)
    return kb.as_markup()


def wishes() -> InlineKeyboardMarkup:
    """Шаг «особые пожелания» вместо выбора качества/прочности (8c8199d1)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data="wish:skip")
    kb.button(text="⬅️ Назад", callback_data="nav:qty")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(1, 2)
    return kb.as_markup()


def coloring() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🌈 Многоцветная", callback_data="mc:multi")
    kb.button(text="⚪ Одноцветная (дешевле)", callback_data="mc:mono")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(1)
    return kb.as_markup()


def confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Рассчитать", callback_data="go")
    kb.button(text="⬅️ Назад", callback_data="nav:wish")
    kb.button(text="❌ Отмена", callback_data="c:cancel")
    kb.adjust(1, 2)
    return kb.as_markup()


def result() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Оставить заказ", callback_data="order:place")
    kb.button(text="📄 PDF-смета", callback_data="result:pdf")
    kb.button(text="💡 Предложить идею", callback_data="m:idea")
    kb.button(text="🔄 Новый расчёт", callback_data="m:calc")
    kb.button(text="🏠 В меню", callback_data="m:home")
    kb.adjust(1, 1, 1, 2)
    return kb.as_markup()


def after_order() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧾 Мои заказы", callback_data="m:myorders")
    kb.button(text="💡 Предложить идею", callback_data="m:idea")
    kb.button(text="🔄 Новый расчёт", callback_data="m:calc")
    kb.button(text="🏠 В меню", callback_data="m:home")
    kb.adjust(1, 1, 2)
    return kb.as_markup()
