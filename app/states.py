"""FSM-состояния."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Calc(StatesGroup):
    model = State()             # ждём ссылку или файл
    search_results = State()    # ждём поисковый запрос → показываем результаты площадок (поиск модели)
    variant = State()           # выбор варианта модели (несколько профилей на MakerWorld, #14)
    coloring = State()          # выбор моно/многоцвет (если модель многоцветная)
    printer = State()           # выбор принтера (если их несколько)
    material = State()          # выбор пластика
    color = State()             # выбор цвета (если у пластика их несколько)
    quantity = State()          # выбор количества (кнопки)
    quantity_custom = State()   # ручной ввод количества
    wishes = State()            # особые пожелания к печати (текст или «Пропустить»; 8c8199d1)
    confirm = State()           # подтверждение/запуск
    reuse = State()             # экран «повторить с теми же / изменить» для сохранённой модели


class AdminFSM(StatesGroup):
    # пластики
    add_name = State()
    add_material = State()      # выбор типа материала (callback)
    add_price = State()
    add_density = State()
    edit_plastic_price = State()
    edit_plastic_density = State()
    edit_plastic_material = State()  # callback
    edit_plastic_desc = State()
    edit_plastic_photo = State()     # ждём фото катушки
    edit_plastic_colors = State()    # цвета через запятую
    edit_plastic_stock = State()     # ждём «ОСТАТОК [ПОРОГ]» в граммах (data: edit_pid)
    edit_plastic_failure = State()   # индивид. % брака («-» = общий из настроек; data: edit_pid)
    # числовые и текстовые настройки
    edit_setting = State()      # data: setting_key
    edit_text = State()         # data: text_key
    # принтеры
    pr_name = State()
    pr_machine = State()
    pr_process = State()
    pr_filament = State()
    # пресеты качества
    q_label = State()
    q_layer = State()
    q_process = State()         # callback (выбор файла процесса)
    # пресеты прочности
    s_label = State()
    s_infill = State()
    s_walls = State()
    # скидки за тираж
    d_qty = State()
    d_pct = State()
    # персональные скидки клиентам
    ud_contact = State()   # ждём контакт / id пользователя
    ud_pct = State()       # ждём % (data: ud_uid, ud_name)
    # пользовательские оттенки цветов
    shade_name = State()   # ждём название цвета
    shade_hex = State()    # ждём HEX (data: shade_name)
    # коллекция: фото печати
    col_photo = State()    # ждём фото (data: col_id)
    col_rename = State()   # ждём новое название записи коллекции (data: col_id)
    # заказы: ввод новой цены
    req_price = State()    # ждём новую цену (data: req_id)
    req_comment = State()  # ждём необязательный комментарий к смене цены (data: req_id, req_new_price)
    req_due = State()      # ждём срок исполнения «ДД.ММ» при переводе в работу (data: req_id)
    # промокоды
    promo_add = State()    # ждём «КОД ПРОЦЕНТ [ДНЕЙ] [ИСПОЛЬЗОВАНИЙ]»
    req_photo = State()    # ждём фото готовой печати (data: req_id)


class PrintStat(StatesGroup):
    """Опрос мастера после завершившейся печати: занести итог в журнал (OpenTodo c587c48b)."""
    photo = State()        # ждём ФОТО готовой печати — обязательно перед оценкой (data: pj_jid, pj_action)
    pct = State()          # ждём % брака 1–99 (data: pj_token, pj_prompt_chat/msg)
    reason = State()       # ждём причину брака или «-» (data: pj_token, pj_pct)
    order_no = State()     # ждём № заказа для привязки печати (классификация, мастер-план Фаза B; data: pe_jid)


class Feedback(StatesGroup):
    review = State()       # ждём текст отзыва после оценки (data: review_rid)


class Idea(StatesGroup):
    text = State()         # ждём текст идеи по улучшению продукта
