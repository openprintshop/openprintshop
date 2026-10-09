"""Настройки мастерской: числовые (таблица settings) и текстовые (text_settings).

Один предмет: значения, которыми владелец настраивает свою экономику и оформление, плюс
ВСЯ семья модульных констант — дефолты (DEFAULT_SETTINGS/DEFAULT_TEXT_SETTINGS), подписи
для админок (SETTING_LABELS/TEXT_SETTING_LABELS) и порядки групп (SETTINGS_ORDER,
TEXT_SETTINGS_ORDER, BRAND_/PAY_/OWNERS_/ACCESS_SETTINGS_ORDER). Дефолт и его подпись
разрывать между файлами нельзя — при следующей правке прайса они разъедутся; читает все
десять один и тот же потребитель, app/web/studio.py.

Выделено из app/db.py волной 2 разреза (план refactoring-db-plan-2026-08-21.md). Тела и
константы перенесены ДОСЛОВНО, правок тел НЕТ. db.init() читает DEFAULT_SETTINGS/
DEFAULT_TEXT_SETTINGS голым именем — оно резолвится фасадной строкой в db.py и остаётся
ТЕМ ЖЕ объектом (словари только читаются и мутируются на месте, никогда не
пере-присваиваются, поэтому реэкспорт корректен).

ВОЛНА 6 ДОПИСАЛА СЮДА get_text_setting (одна текстовая настройка по ключу). В db.py она
физически лежала в хвосте секции «Личный вход в настольную админку», но домен у неё —
настройки: читает text_settings и падает в DEFAULT_TEXT_SETTINGS, который живёт здесь же.
Перенос в store/team.py расщепил бы домен настроек по двум модулям ради одной константы.
Тело перенесено ДОСЛОВНО; фасадная строка вставлена в АЛФАВИТНЫЙ блок волны 2 в db.py.
"""
from __future__ import annotations

import logging

from .core import _conn

log = logging.getLogger(__name__)


# Числовые параметры расчёта по умолчанию, в бел. рублях (BYN). Это ориентир —
# админ задаёт реальные значения под свою экономику в /admin → ⚙️ Параметры.
DEFAULT_SETTINGS: dict[str, str] = {
    "machine_hour_rate": "2",     # BYN/час (электричество + амортизация + износ)
    "setup_fee": "5",             # BYN — фикс. подготовка на заказ
    "plate_fee": "0.5",           # BYN — доплата за каждый стол проекта (OpenTodo 8c89a701)
    "failure_rate_pct": "8",      # %
    "profit_margin_pct": "50",    # %
    "min_price": "5",             # BYN — минимальный заказ
    "multicolor_waste_pct": "12",  # % филамента в продувку за каждый доп. цвет
    "multicolor_time_pct": "15",   # % времени на смены цвета за каждый доп. цвет
    # «Большая модель» (pricing.volume_factor): 0 = выключено. Порог — объём ЕДИНИЦЫ, см³;
    # надбавка — % к расходникам за каждый полный порог сверх первого (линейно, без скачка).
    "volume_threshold_cm3": "0",
    "volume_surcharge_pct": "0",
    # Этап 5 мастер-плана: 1 = корректировать оценку времени печати по накопленным парам
    # оценка/факт (db.predict_minutes, с гейтом по выборке). По умолчанию 0 — включить, когда
    # накопится выборка (план: «не начинать пока выборка не выросла»). Off → поведение прежнее.
    "time_prediction": "0",
    # Честный слайс в клиентских расчётах: 0 (дефолт, решение владельца 17.07) — только
    # быстрый аналитический расчёт (approx, мастер вносит факт по печати); 1 — прежнее
    # поведение со слайсером. Явные админ-инструменты («слайсни по ссылке») слайсят всегда.
    "slicing_enabled": "0",
    # Показывать ли КЛИЕНТУ шаг «на каком принтере печатаем?». 0 (дефолт) — не показывать,
    # машину выбирает мастерская. Раньше флага не было вовсе: шаг включался сам, как только
    # активных карточек становилось больше одной, — то есть заведение второго принтера молча
    # меняло клиентский сценарий и показывало покупателю названия моделей («Elegoo Centauri
    # Carbon 2»), которые ему ничего не говорят. Решение «спрашивать ли клиента» — продуктовое
    # и должно приниматься переключателем, а не количеством строк в таблице (разбор 11.08).
    "printer_choice_enabled": "0",
    # Санация проект-3mf из BambuStudio/MakerLab под ElegooSlicer (app.bambu3mf) — правка файла
    # НА МЕСТЕ и без бэкапа. 0 (дефолт, решение владельца 18.07): .3mf остаётся в ИСХОДНОМ виде —
    # раз слайсинг в расчётах выключен (slicing_enabled=0), чинить файл под слайсер незачем, а
    # мастеру нужен оригинал автора. 1 — санировать (нужно вместе со slicing_enabled=1, иначе
    # бамбу-проекты не режутся и расчёт молча уходит в ≈оценку). На чтение модели настройка не
    # влияет: столы, превью и быстрый расчёт разбирают оригинальный файл.
    "sanitize_3mf": "0",
    # Наценка «готовое изделие» (калькулятор настольной админки): печать на склад без
    # заказчика — риск не продать. Применяется ТОЛЬКО когда мастер сам поставил галочку
    # в калькуляторе; на клиентские расчёты и на цену заказов НЕ влияет никогда.
    "ready_product_markup_pct": "15",
    "service_warn_pct": "85",      # за сколько % до порога ТО подсвечивать «скоро» (жёлтый)
    # CRM (crm-roadmap.md): пороги вычисляемых сегментов — настройки, не хардкод
    "crm_vip_spent": "100",        # {cur}: сумма напечатанных заказов, с которой клиент — VIP
    "crm_sleep_days": "45",        # дней без заказов → клиент «спящий» (кандидат на win-back)
    "crm_broadcast_gap_days": "7",  # не слать рассылки одному клиенту чаще, чем раз в N дней
    # Догон брошенных расчётов (A5, abandonednudge): клиент посчитал модель, но не оформил заказ →
    # ОДНО напоминание через nudge_abandoned_hours ч. По умолчанию ВЫКЛ (0) — владелец включит,
    # посмотрев на счётчик кандидатов. max_days — верхняя граница возраста расчёта: старые расчёты
    # не «догоняем» задним числом при первом включении (иначе ретро-спам легаси-строк orders).
    # Тихие часы (22:00–09:00 по Минску) вшиты в abandonednudge, отдельной настройкой не выносим.
    "nudge_abandoned_enabled": "0",
    "nudge_abandoned_hours": "24",
    "nudge_abandoned_max_days": "7",
    # Срочность и сроки (B3): наценка за срочную печать (%-формат, как прочие ставки) + обещанный
    # клиенту срок, который теперь ЗАВИСИТ ОТ ВРЕМЕНИ ПЕЧАТИ, а не от фикс-дней (решение владельца):
    # «срочно» → всегда завтра; «обычно» → печать короче term_split_hours ч → завтра, иначе
    # послезавтра. Порог быстрого/долгого заказа — term_split_hours (по умолчанию 12 ч). Очередь-
    # осознанный eta_date для мастера остаётся отдельно. Наценка за срочность +50%.
    "rush_surcharge_pct": "50",
    "term_split_hours": "12",
    # Печатопригодность (B2, DFM): минимальная уверенно печатаемая толщина стенки, мм. Ниже неё
    # эвристика (2·V_union/площадь — точного замера стенок в геометрии нет) мягко предупреждает
    # «возможно тонкие стенки». Только ОЦЕНКА и только для одной детали — заказ НЕ блокирует.
    # ~2× ширины линии сопла 0.4 мм = 0.8 мм: консервативный порог, чтобы не пугать клиента зря.
    "dfm_min_wall_mm": "0.8",
}
SETTING_LABELS: dict[str, str] = {
    "machine_hour_rate": "Ставка машинного времени, {cur}/час",
    "setup_fee": "Подготовка (фикс. на заказ), {cur}",
    "plate_fee": "Доплата за стол (за каждый), {cur}",
    "failure_rate_pct": "Буфер на брак, %",
    "profit_margin_pct": "Наценка, %",
    "min_price": "Минимальная цена заказа, {cur}",
    "multicolor_waste_pct": "Многоцвет: продувка, % за доп. цвет",
    "multicolor_time_pct": "Многоцвет: время, % за доп. цвет",
    "volume_threshold_cm3": "Большая модель: порог объёма, см³ (0 — выкл.)",
    "volume_surcharge_pct": "Большая модель: надбавка, % за каждый порог сверх",
    "ready_product_markup_pct": "Наценка «готовое изделие», % (только в калькуляторе)",
    "time_prediction": "Коррекция времени печати по накопленным данным (1 — вкл., Этап 5)",
    "slicing_enabled": "Честный слайс в расчётах (1 — вкл.; 0 — только быстрый расчёт)",
    "sanitize_3mf": "Чинить .3mf из BambuStudio под слайсер (0 — не трогать файл, оставить как есть)",
    "crm_vip_spent": "Клиенты: порог VIP по сумме заказов, {cur}",
    "crm_sleep_days": "Клиенты: «спящий» после стольких дней без заказов",
    "crm_broadcast_gap_days": "Клиенты: пауза между рассылками одному клиенту, дней",
    "nudge_abandoned_enabled": "Догон брошенных расчётов (1 — вкл., 0 — выкл.)",
    "nudge_abandoned_hours": "Догон: через сколько часов напомнить о расчёте",
    "nudge_abandoned_max_days": "Догон: не трогать расчёты старше стольких дней",
    "rush_surcharge_pct": "Срочность: наценка, %",
    "term_split_hours": "Срок: порог «быстро/долго» по времени печати, ч (короче — завтра, дольше — послезавтра)",
    "dfm_min_wall_mm": "Печатопригодность: мин. толщина стенки, мм (только предупреждение)",
}
SETTINGS_ORDER = ["machine_hour_rate", "setup_fee", "plate_fee", "failure_rate_pct",
                  "profit_margin_pct", "min_price", "multicolor_waste_pct", "multicolor_time_pct",
                  "volume_threshold_cm3", "volume_surcharge_pct", "ready_product_markup_pct",
                  "time_prediction",
                  "slicing_enabled", "sanitize_3mf",
                  "crm_vip_spent", "crm_sleep_days", "crm_broadcast_gap_days",
                  "nudge_abandoned_enabled", "nudge_abandoned_hours", "nudge_abandoned_max_days",
                  "rush_surcharge_pct", "term_split_hours",
                  "dfm_min_wall_mm"]


DEFAULT_TEXT_SETTINGS: dict[str, str] = {
    "business_name": "3D-печать",
    "business_contact": "",
    # Брендинг мастерской (A3, white-label). Пусто = «оставить как у нас»: подстановкой
    # дефолтов занимается app/brand.py, здесь пустая строка — это осознанное «не заменяли».
    "brand_title": "",
    "brand_tagline": "",
    "brand_accent": "",
    # Онлайн-оплата (A1, app/payments.py): токен провайдера из BotFather (пусто = выключена)
    # и ISO-валюта счетов — обязана совпадать с валютой цен и поддерживаться провайдером.
    "pay_provider_token": "",
    "pay_currency": "RUB",
    # Сообщество ВЛАДЕЛЬЦЕВ студий (c356387a): ссылка на Telegram-группу, где владельцы
    # разных мастерских общаются между собой. Своего сервера у продукта нет и каждая
    # установка изолирована, поэтому площадка внешняя. Пусто — раздел не показываем.
    "owners_chat_url": "",
    # Как входят в НАСТОЛЬНУЮ админку (app/desklogin.py):
    #   token    — как раньше: .env-токен, любой за машиной студии = первый владелец;
    #   personal — только личный вход по коду из бота, роли начинают работать по-настоящему.
    # По умолчанию прежнее поведение: обновление кода не имеет права запереть владельца
    # снаружи его собственной мастерской.
    "desk_login_mode": "token",
}
TEXT_SETTING_LABELS: dict[str, str] = {
    "business_name": "Название мастерской (в PDF)",
    "business_contact": "Контакты (в PDF)",
    "brand_title": "Название в приложении (пусто — OpenPrintShop)",
    "brand_tagline": "Подпись под названием (пусто — «расчёт 3D-печати»)",
    "brand_accent": "Акцентный цвет, #RRGGBB (пусто — #ff7a1a)",
    "pay_provider_token": "Токен провайдера из BotFather (пусто — оплата выключена)",
    "pay_currency": "Валюта счетов, ISO-код (RUB, BYN…) — как у цен мастерской",
    "owners_chat_url": "Чат владельцев студий: ссылка на Telegram-группу (пусто — скрыть)",
    "desk_login_mode": "Вход в настольную админку: token (общий) или personal (личный, по коду из бота)",
}
# Реквизиты для PDF и брендинг разведены по разным группам: в админках это разные экраны
# («Цены и скидки» против «Настройки»), и мешать их в одном списке было бы неверно.
TEXT_SETTINGS_ORDER = ["business_name", "business_contact"]
BRAND_SETTINGS_ORDER = ["brand_title", "brand_tagline", "brand_accent"]
PAY_SETTINGS_ORDER = ["pay_provider_token", "pay_currency"]
OWNERS_SETTINGS_ORDER = ["owners_chat_url"]
# Вход в настольную админку (app/desklogin.py) — отдельной группой, а не среди реквизитов:
# это не оформление, а права доступа, и переключатель меняет, кем работает человек за ПК.
ACCESS_SETTINGS_ORDER = ["desk_login_mode"]


# ---- числовые настройки ----
async def get_settings() -> dict[str, float]:
    cur = await _conn().execute("SELECT key, value FROM settings")
    out: dict[str, float] = {}
    for r in await cur.fetchall():
        try:
            out[r["key"]] = float(r["value"])
        except (TypeError, ValueError):
            out[r["key"]] = 0.0
    for k, v in DEFAULT_SETTINGS.items():
        out.setdefault(k, float(v))
    return out


async def set_setting(key: str, value: float) -> None:
    await _conn().execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    await _conn().commit()


async def delete_setting(key: str) -> None:
    """Убрать настройку совсем. Нужно там, где «нет записи» ЗНАЧИТ иное, чем 0: у
    переключателей возможностей сборки (f98aa1ef) отсутствие = «как решает редакция»."""
    await _conn().execute("DELETE FROM settings WHERE key = ?", (key,))
    await _conn().commit()


# ---- текстовые настройки ----
async def get_text_settings() -> dict[str, str]:
    cur = await _conn().execute("SELECT key, value FROM text_settings")
    out = {r["key"]: r["value"] for r in await cur.fetchall()}
    for k, v in DEFAULT_TEXT_SETTINGS.items():
        out.setdefault(k, v)
    return out


async def set_text_setting(key: str, value: str) -> None:
    await _conn().execute(
        "INSERT INTO text_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await _conn().commit()


async def get_text_setting(key: str, default: str = "") -> str:
    """Одна текстовая настройка. Есть и множественный `get_text_settings`, но читать весь
    словарь ради одного ключа на горячем пути — лишняя работа."""
    cur = await _conn().execute("SELECT value FROM text_settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    if row is not None:
        return str(row["value"])
    return DEFAULT_TEXT_SETTINGS.get(key, default)
