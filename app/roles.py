"""Роли и права команды мастерской (A4, competitor-gap 2026-07).

До этого модуля доступ в админку был плоским: ADMIN_IDS в .env, единственная проверка
is_admin() — и любой, кто в списке, мог всё: менять цены, видеть выручку, править настройки
бота, начислять баланс. Мастерская с наёмным оператором такого сотрудника внутрь не пустит.

Здесь появляются две роли и матрица прав:

  ВЛАДЕЛЕЦ (owner)   — всё, включая деньги, цены, клиентскую базу и настройки.
  ОПЕРАТОР (operator) — производственный контур: заказы (статусы, переписка с клиентом,
                        очередь, отправка) и принтер. БЕЗ цен, выручки, балансов,
                        клиентской базы, рассылок и настроек.

Как это устроено:
- Список сотрудников — таблица staff в БД; ADMIN_IDS из .env остаются КОРНЕВЫМИ владельцами
  (всегда owner, из приложения не понижаются и не удаляются — иначе владелец может запереть
  сам себя, и вернуть доступ выйдет только правкой .env с перезапуском бота).
- Роли кэшируются в config.STAFF_ROLES: is_admin() зовут из синхронного кода, ходить оттуда
  в базу нельзя. Любая правка команды идёт через set_role()/remove() — они обновляют кэш.
- Права проверяются ДВАЖДЫ: на входе в HTTP (карта прав на роуты в app/webapp.py — оператор
  физически не может дёрнуть ручку цены) и в выдаче (денежные поля из ответов вырезаются).
  Скрытая кнопка во фронте — удобство, а не защита.
- Кто что сделал (attribution): действия сотрудников пишутся в activity_log с именем и ролью,
  поэтому в ленте активности видно «Оператор Иван перевёл заказ #12 в „Напечатано“».
"""
from __future__ import annotations

import logging

from . import config, db, desklogin

log = logging.getLogger(__name__)

# ---- Права (capabilities) ----
CAP_ORDERS = "orders"        # заказы: статусы, переписка, очередь, отправка, файлы
CAP_PRINTER = "printer"      # принтер: управление, загрузка файлов, печать, камера
CAP_FINANCE = "finance"      # деньги: цены заказов, счета, баланс, выручка, себестоимость
CAP_PRICING = "pricing"      # прайс: пластики, пресеты, наценки, опции, промокоды, витрина
CAP_CLIENTS = "clients"      # клиентская база, семьи, скидки клиентам, рассылки
CAP_SETTINGS = "settings"    # настройки бота, тексты, бренд, оплата, сервис, команда

ALL_CAPS: tuple[str, ...] = (CAP_ORDERS, CAP_PRINTER, CAP_FINANCE, CAP_PRICING,
                             CAP_CLIENTS, CAP_SETTINGS)

ROLE_CAPS: dict[str, frozenset[str]] = {
    config.ROLE_OWNER: frozenset(ALL_CAPS),
    config.ROLE_OPERATOR: frozenset({CAP_ORDERS, CAP_PRINTER}),
}

ROLE_LABELS = {config.ROLE_OWNER: "Владелец", config.ROLE_OPERATOR: "Оператор"}
ROLE_HINTS = {
    config.ROLE_OWNER: "Полный доступ: деньги, цены, клиенты, настройки",
    config.ROLE_OPERATOR: "Заказы и принтер. Без цен, выручки, клиентской базы и настроек",
}
CAP_LABELS = {
    CAP_ORDERS: "Заказы", CAP_PRINTER: "Принтер", CAP_FINANCE: "Деньги и цены заказов",
    CAP_PRICING: "Прайс и каталог", CAP_CLIENTS: "Клиенты и рассылки", CAP_SETTINGS: "Настройки",
}


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", "Клиент")


def caps_of(user_id: int | None) -> frozenset[str]:
    """Права пользователя. Пустое множество — не сотрудник."""
    return ROLE_CAPS.get(config.role_of(user_id) or "", frozenset())


def can(user_id: int | None, cap: str) -> bool:
    return cap in caps_of(user_id)


def is_operator(user_id: int | None) -> bool:
    return config.role_of(user_id) == config.ROLE_OPERATOR


def is_root_owner(user_id: int | None) -> bool:
    """Владелец из .env: его строки в staff нет, роль менять/удалять нельзя."""
    return user_id is not None and int(user_id) in [int(x) for x in config.ADMIN_IDS]


def whoami(user_id: int | None) -> dict:
    """Блок о правах для фронта: роль, ярлык, список прав и ИМЯ. Фронт по нему прячет разделы.

    Имя появилось вместе с личным входом в настольную админку (app/desklogin.py): пока вход был
    один на машину, показывать было нечего — за компьютером всегда «владелец». Теперь в шапке
    видно, КТО работает, иначе человек не заметит, что сидит под чужой сессией."""
    role = config.role_of(user_id)
    return {"role": role or "", "role_label": role_label(role),
            "caps": sorted(caps_of(user_id)), "root": is_root_owner(user_id),
            "uid": int(user_id) if user_id else 0,
            "name": desklogin.name_of(user_id) if user_id else ""}


# ---- Кэш ролей ----
async def refresh() -> None:
    """Перечитать таблицу staff в кэш config.STAFF_ROLES. Зовётся на старте (run.py) и после
    каждой правки команды. Строки с неизвестной ролью игнорируем — лучше не пустить, чем
    выдать права по опечатке в БД."""
    try:
        rows = await db.list_staff()
    except Exception:  # noqa: BLE001
        log.warning("не удалось прочитать команду из БД", exc_info=True)
        return
    config.set_staff_roles({int(r["user_id"]): str(r["role"]) for r in rows
                            if str(r["role"]) in ROLE_CAPS})


async def set_role(user_id: int, role: str, *, name: str = "", note: str = "",
                   added_by: int | None = None) -> tuple[bool, str]:
    """Пригласить сотрудника / сменить роль. (ok, ошибка)."""
    uid = int(user_id or 0)
    if uid <= 0:
        return False, "Нужен Telegram ID сотрудника (число)"
    if role not in ROLE_CAPS:
        return False, "Неизвестная роль"
    if is_root_owner(uid):
        return False, "Это владелец из настроек сервера — его роль здесь не меняется"
    await db.set_staff(uid, role, name=name, note=note, added_by=added_by)
    await refresh()
    return True, ""


async def remove(user_id: int) -> tuple[bool, str]:
    uid = int(user_id or 0)
    if is_root_owner(uid):
        return False, "Владельца из настроек сервера удалить нельзя"
    await db.remove_staff(uid)
    # Снять роль МАЛО: у уволенного могла остаться живая сессия настольной админки на машине
    # студии, и она пережила бы удаление. Отбираем доступ вместе с должностью.
    await desklogin.revoke_user(uid)
    await refresh()
    return True, ""


async def staff_list() -> list[dict]:
    """Вся команда для админки: сначала корневые владельцы (.env), затем приглашённые.

    `sessions` — сколько живых входов в настольную админку у человека. Без этого числа
    владелец не знает, остался ли уволенный внутри: роль он снял, а сессия на машине студии
    пережила бы снятие, если бы её никто не гасил."""
    out: list[dict] = []
    for uid in config.ADMIN_IDS:
        out.append({"user_id": int(uid), "role": config.ROLE_OWNER,
                    "role_label": ROLE_LABELS[config.ROLE_OWNER], "name": "", "note": "",
                    "root": True, "created_at": 0,
                    "sessions": desklogin.sessions_of(int(uid))})
    for r in await db.list_staff():
        if is_root_owner(r["user_id"]):
            continue      # подстраховка: строка на корневого владельца ничего не решает
        role = str(r["role"])
        out.append({"user_id": int(r["user_id"]), "role": role,
                    "role_label": role_label(role), "name": r["name"] or "",
                    "note": r["note"] or "", "root": False,
                    "created_at": int(r["created_at"] or 0),
                    "sessions": desklogin.sessions_of(int(r["user_id"]))})
    return out


async def log_action(user_id: int | None, action: str, detail: str = "") -> None:
    """Attribution: кто из команды что сделал — в общую ленту активности, с ролью в имени."""
    if user_id is None:
        return
    role = config.role_of(user_id)
    if role is None:
        return
    entry = await db.get_staff(int(user_id))
    who = (entry or {}).get("name") or f"id {int(user_id)}"
    await db.log_event(int(user_id), action, detail, user_name=f"{role_label(role)} {who}")
