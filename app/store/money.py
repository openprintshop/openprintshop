"""Деньги клиента ОДНОЙ транзакционной связкой: личный баланс, онлайн-платежи, семейный счёт.

Три секции db.py, разрез между которыми запрещён — граница «личное/общее» проходит
ВНУТРИ одного commit-а:

* ЛИЧНЫЙ БАЛАНС (user_balance + журнал balance_tx). adjust_balance под одним commit-ом
  делает UPSERT баланса и запись в журнал, НО при членстве в семье целиком делегирует в
  adjust_family_balance — то есть «баланс» не самостоятелен без «семьи».
* ОНЛАЙН-ПЛАТЕЖИ A1 (payments). add_payment идемпотентен по tg_charge_id через
  aiosqlite.IntegrityError -> None: повторный successful_payment от Telegram не задваивает
  ни отметку «оплачен», ни уведомления.
* СЕМЕЙНЫЙ СЧЁТ ЦЕЛИКОМ (family_accounts + family_members + family_tx) — ИНЦИДЕНТНАЯ ЗОНА
  16.07.2026 (merge «семейный счёт» уничтожил данные). create_family/add_family_member
  одним commit-ом переносят ЛИЧНЫЕ балансы в общий, remove_family_member одним commit-ом
  трогает family_members + user_balance + balance_tx + family_accounts, возвращая остаток
  на личный баланс. adjust_family_balance пишет относительным UPSERT-ом — защита от чужого
  коммита между чтением и записью. Любой разрез внутри этих 334 строк = повтор data-loss.

Модуль-лист рядом с core: из store нужен ТОЛЬКО _conn, ни models, ни другие домены.
Заказы (app/store/orders.py) отсюда не зовутся и сюда не зовут — рёбер между двумя
модулями волны 4 нет ни одного.

Имя модуля — money, а НЕ payments: рядом живёт app/payments.py, и `from .store import
payments` в db.py было бы заявкой на путаницу (а стоит кому-то дописать в db.py
`from . import payments` — сразу ruff F811).

Выделено из app/db.py волной 4 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ (ленивых импортов и global в них не было). Логгер не
заводится: ни одно из 16 тел не пишет в лог.
"""
from __future__ import annotations

import re
import sqlite3
import time

import aiosqlite

from .core import _conn


# ---- Баланс клиента (OpenTodo 8c428c43) ----
# С редизайном семейного счёта (2026-07-16) обе функции прозрачно работают с ОБЩИМ счётом,
# если клиент состоит в семье: существующие вызовы (оплата заказа, пополнение мастером,
# карточка клиента, профиль) менять не пришлось.
async def get_balance(user_id: int) -> float:
    fam = await family_of(user_id)
    if fam is not None:
        cur = await _conn().execute("SELECT balance FROM family_accounts WHERE id = ?", (fam,))
        r = await cur.fetchone()
        return float(r["balance"]) if r else 0.0
    return await get_personal_balance(user_id)


async def get_personal_balance(user_id: int) -> float:
    """Личный остаток БЕЗ учёта семьи — нужен переносу в общий счёт при вступлении."""
    cur = await _conn().execute("SELECT balance FROM user_balance WHERE user_id = ?", (user_id,))
    r = await cur.fetchone()
    return float(r["balance"]) if r else 0.0


async def adjust_balance(user_id: int, amount: float, reason: str = "",
                         comment: str | None = None, request_id: int | None = None) -> float:
    """Изменить баланс на amount (может быть отрицательным = списание). Пишет запись в журнал.
    Возвращает новый баланс. Округляем до копеек, чтобы не копить float-хвосты.
    comment — необязательная заметка мастера к транзакции (ccb1e4b6), видна клиенту в истории.
    Член семьи → операция уходит на общий счёт с подписью «чьё действие» и номером заказа
    (request_id; если не передан — пробуем распознать из reason «Оплата заказа #N»).

    Личный остаток меняется ОТНОСИТЕЛЬНЫМ UPSERT-ом (balance + amount прямо в SQL), а не
    записью абсолюта, посчитанного в питоне: соединение одно на бота и веб, и чужой коммит
    между чтением баланса и его записью абсолютом затирался молча — в balance_tx оставались
    обе записи, а на счету только последняя. Общий счёт семьи так чинили ещё в 2026-07
    (adjust_family_balance); здесь тот же приём. Кламп не ставим сознательно: личный баланс
    имеет право уходить в минус (клиент должен мастеру) — см. test_balance_can_go_negative."""
    fam = await family_of(user_id)
    if fam is not None:
        if request_id is None:
            m = re.search(r"заказа #(\d+)", reason or "")
            request_id = int(m.group(1)) if m else None
        return await adjust_family_balance(fam, amount, reason, user_id=user_id,
                                           comment=comment, request_id=request_id)
    now = int(time.time())
    delta = round(float(amount), 2)
    await _conn().execute(
        "INSERT INTO user_balance(user_id, balance, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "balance=ROUND(user_balance.balance + excluded.balance, 2), updated_at=excluded.updated_at",
        (user_id, delta, now))
    await _conn().execute(
        "INSERT INTO balance_tx(user_id, amount, reason, at, comment) VALUES(?,?,?,?,?)",
        (user_id, delta, reason or None, now, (comment or "").strip() or None))
    await _conn().commit()
    return await get_personal_balance(user_id)


# ---- Онлайн-платежи (A1, app/payments.py) ----
async def add_payment(*, request_id: int, user_id: int, amount: float, currency: str,
                      tg_charge_id: str, provider_charge_id: str = "",
                      provider: str = "telegram") -> int | None:
    """Строка леджера об успешной оплате. None — платёж с таким telegram charge id уже
    записан (Telegram может доставить successful_payment повторно): вызывающий по None
    молча выходит, не задваивая ни отметку «оплачен», ни уведомления."""
    try:
        cur = await _conn().execute(
            "INSERT INTO payments(request_id, user_id, amount, currency, provider, "
            "tg_charge_id, provider_charge_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (int(request_id), int(user_id), round(float(amount), 2), currency, provider,
             tg_charge_id or None, provider_charge_id or None, int(time.time())))
    except aiosqlite.IntegrityError:
        return None
    await _conn().commit()
    return cur.lastrowid


async def payments_for_request(request_id: int) -> list[dict]:
    cur = await _conn().execute(
        "SELECT id, amount, currency, provider, tg_charge_id, provider_charge_id, created_at "
        "FROM payments WHERE request_id = ? ORDER BY id", (int(request_id),))
    return [dict(r) for r in await cur.fetchall()]


async def list_balance_tx(user_id: int, limit: int = 20) -> list[dict]:
    cur = await _conn().execute(
        "SELECT id, amount, reason, at, comment FROM balance_tx WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?", (user_id, limit))
    return [{"id": r["id"], "amount": r["amount"], "reason": r["reason"] or "", "at": r["at"],
             "comment": (r["comment"] if "comment" in r.keys() else None) or ""}
            for r in await cur.fetchall()]


async def set_balance_tx_comment(tx_id: int, comment: str) -> bool:
    """Комментарий к УЖЕ совершённой транзакции (ccb1e4b6): мастер аннотирует задним числом.
    Пустая строка стирает заметку. False — транзакции с таким id нет."""
    cur = await _conn().execute("UPDATE balance_tx SET comment = ? WHERE id = ?",
                                ((comment or "").strip() or None, tx_id))
    await _conn().commit()
    return cur.rowcount > 0


# ---- Семейный счёт (редизайн 2026-07-16, после инцидента со слиянием) ----
# Общий ТОЛЬКО баланс; участники равноправны, число не ограничено. Личные балансы при
# вступлении переносятся в общий (с записью в журнале), личная история balance_tx остаётся
# у каждого как архив.
async def family_of(user_id: int) -> int | None:
    cur = await _conn().execute(
        "SELECT family_id FROM family_members WHERE user_id = ?", (int(user_id),))
    r = await cur.fetchone()
    return int(r["family_id"]) if r else None


async def get_family(user_id: int) -> dict | None:
    """Семья глазами участника: общий баланс, состав, название. None — не состоит."""
    fam = await family_of(user_id)
    if fam is None:
        return None
    cur = await _conn().execute("SELECT * FROM family_accounts WHERE id = ?", (fam,))
    acc = await cur.fetchone()
    if not acc:
        return None
    cur = await _conn().execute(
        "SELECT user_id, user_name, joined_at FROM family_members WHERE family_id = ? "
        "ORDER BY joined_at", (fam,))
    members = [dict(r) for r in await cur.fetchall()]
    return {"id": acc["id"], "title": acc["title"], "balance": float(acc["balance"]),
            "created_at": acc["created_at"], "members": members}


async def _family_member_name(family_id: int, user_id: int | None) -> str:
    if not user_id:
        return ""
    cur = await _conn().execute(
        "SELECT user_name FROM family_members WHERE family_id = ? AND user_id = ?",
        (family_id, user_id))
    r = await cur.fetchone()
    return (r["user_name"] if r else "") or ""


async def adjust_family_balance(family_id: int, amount: float, reason: str = "", *,
                                user_id: int | None = None, comment: str | None = None,
                                request_id: int | None = None) -> float:
    """Изменить ОБЩИЙ баланс семьи. user_id — чьё это действие (подпись в истории у всех
    участников; None = мастер/система), request_id — номер заказа при оплате.
    Баланс меняется атомарным UPDATE (не read-modify-write) — конкурентные операции
    на общем соединении не теряют друг друга."""
    now = int(time.time())
    cur = await _conn().execute(
        "UPDATE family_accounts SET balance = ROUND(balance + ?, 2), updated_at = ? "
        "WHERE id = ?", (float(amount), now, family_id))
    if cur.rowcount == 0:
        raise ValueError(f"Семейный счёт #{family_id} не существует")
    cur = await _conn().execute(
        "SELECT balance FROM family_accounts WHERE id = ?", (family_id,))
    new = float((await cur.fetchone())["balance"])
    await _conn().execute(
        "INSERT INTO family_tx(family_id, user_id, user_name, request_id, amount, reason, "
        "comment, at) VALUES(?,?,?,?,?,?,?,?)",
        (family_id, user_id, await _family_member_name(family_id, user_id), request_id,
         round(float(amount), 2), reason or "", (comment or "").strip() or None, now))
    await _conn().commit()
    return new


async def list_family_tx(family_id: int, limit: int = 20) -> list[dict]:
    cur = await _conn().execute(
        "SELECT id, user_id, user_name, request_id, amount, reason, at, comment "
        "FROM family_tx WHERE family_id = ? ORDER BY id DESC LIMIT ?", (family_id, limit))
    return [{"id": r["id"], "user_id": r["user_id"], "user_name": r["user_name"] or "",
             "request_id": r["request_id"], "amount": r["amount"], "reason": r["reason"] or "",
             "at": r["at"], "comment": r["comment"] or ""} for r in await cur.fetchall()]


async def create_family(members: list[tuple[int, str]], title: str = "",
                        opening_bonus: float = 0.0) -> dict:
    """Открыть семейный счёт: участники (user_id, отображаемое имя) равноправны. Личный
    остаток каждого переносится в общий счёт записью «Перенос личного баланса», затем
    начисляется разовый бонус за открытие. ValueError — мало участников/дубли/кто-то уже
    в семье. Возвращает get_family первого участника."""
    seen: set[int] = set()
    clean: list[tuple[int, str]] = []
    for uid, name in members:
        uid = int(uid)
        if uid in seen:
            continue
        seen.add(uid)
        clean.append((uid, (name or "").strip()[:80]))
    if len(clean) < 2:
        raise ValueError("Нужно минимум два участника")
    for uid, _name in clean:
        if await family_of(uid) is not None:
            raise ValueError(f"Клиент {uid} уже состоит в семейном счёте")
    now = int(time.time())
    c = _conn()
    ids = [uid for uid, _ in clean]
    q = ",".join("?" * len(ids))
    # Бонус за открытие НЕ начисляется, если внутри этой компании реферальный бонус уже
    # был реально ВЫПЛАЧЕН (bonus_paid=1: один пригласил другого и оба +N получили) —
    # иначе одна и та же пара собрала бы оба бонуса. rewarded=1 без выплаты (глушение
    # при создании семьи / ручная чистка) бонусу не мешает.
    if opening_bonus:
        cur = await c.execute(
            f"SELECT 1 FROM referrals WHERE bonus_paid = 1 AND referee_id IN ({q}) "
            f"AND referrer_id IN ({q}) LIMIT 1", (*ids, *ids))
        if await cur.fetchone():
            opening_bonus = 0.0
    # всё одной транзакцией: создание, участники, перенос личных остатков, бонус —
    # краш посередине не должен ни потерять деньги, ни оставить полусозданную семью
    cur = await c.execute(
        "INSERT INTO family_accounts(title, balance, created_at, updated_at) VALUES(?,0,?,?)",
        ((title or "").strip()[:80], now, now))
    fam = cur.lastrowid
    total = 0.0
    for uid, name in clean:
        try:
            await c.execute(
                "INSERT INTO family_members(user_id, family_id, user_name, joined_at) "
                "VALUES(?,?,?,?)", (uid, fam, name, now))
        except sqlite3.IntegrityError:
            # гонка двойного тапа «Создать счёт»: PK(user_id) поймал дубль — откатываем всё
            await c.rollback()
            raise ValueError(f"Клиент {uid} уже состоит в семейном счёте") from None
        bal = await get_personal_balance(uid)
        if bal:
            total = round(total + bal, 2)
            await c.execute("DELETE FROM user_balance WHERE user_id = ?", (uid,))
            await c.execute(
                "INSERT INTO family_tx(family_id, user_id, user_name, request_id, amount, "
                "reason, comment, at) VALUES(?,?,?,NULL,?,?,NULL,?)",
                (fam, uid, name, bal, "Перенос личного баланса", now))
    if opening_bonus:
        total = round(total + float(opening_bonus), 2)
        await c.execute(
            "INSERT INTO family_tx(family_id, user_id, user_name, request_id, amount, "
            "reason, comment, at) VALUES(?,NULL,'',NULL,?,?,NULL,?)",
            (fam, float(opening_bonus), "Бонус за открытие семейного счёта", now))
    await c.execute("UPDATE family_accounts SET balance = ?, updated_at = ? WHERE id = ?",
                    (total, now, fam))
    # рефералка между членами семьи гасится навсегда: вместо неё — бонус за открытие
    await c.execute(f"UPDATE referrals SET rewarded = 1 WHERE rewarded = 0 "
                    f"AND referee_id IN ({q}) AND referrer_id IN ({q})", (*ids, *ids))
    await c.commit()
    out = await get_family(clean[0][0])
    # был ли начислен бонус — вызывающему (уведомление участникам не должно обещать
    # подарок, если бонус обнулён из-за уже выплаченной рефералки внутри пары)
    out["bonus_granted"] = bool(opening_bonus)
    return out


async def add_family_member(family_id: int, user_id: int, user_name: str = "") -> dict:
    """Добавить участника в существующую семью (личный остаток переносится в общий).
    ValueError — семьи нет или человек уже состоит в какой-либо семье."""
    user_id = int(user_id)
    cur = await _conn().execute("SELECT 1 FROM family_accounts WHERE id = ?", (family_id,))
    if await cur.fetchone() is None:
        raise ValueError(f"Семейный счёт #{family_id} не существует")
    if await family_of(user_id) is not None:
        raise ValueError(f"Клиент {user_id} уже состоит в семейном счёте")
    now = int(time.time())
    c = _conn()
    name = (user_name or "").strip()[:80]
    try:
        await c.execute(
            "INSERT INTO family_members(user_id, family_id, user_name, joined_at) VALUES(?,?,?,?)",
            (user_id, family_id, name, now))
    except sqlite3.IntegrityError:
        raise ValueError(f"Клиент {user_id} уже состоит в семейном счёте") from None
    bal = await get_personal_balance(user_id)
    if bal:
        await c.execute("DELETE FROM user_balance WHERE user_id = ?", (user_id,))
        await c.execute(
            "INSERT INTO family_tx(family_id, user_id, user_name, request_id, amount, "
            "reason, comment, at) VALUES(?,?,?,NULL,?,?,NULL,?)",
            (family_id, user_id, name, bal, "Перенос личного баланса", now))
        await c.execute(
            "UPDATE family_accounts SET balance = ROUND(balance + ?, 2), updated_at = ? "
            "WHERE id = ?", (bal, now, family_id))
    # рефералка между новым участником и остальными членами семьи больше не начислится
    cur = await c.execute(
        "SELECT user_id FROM family_members WHERE family_id = ?", (family_id,))
    ids = [r["user_id"] for r in await cur.fetchall()]
    q = ",".join("?" * len(ids))
    await c.execute(f"UPDATE referrals SET rewarded = 1 WHERE rewarded = 0 "
                    f"AND referee_id IN ({q}) AND referrer_id IN ({q})", (*ids, *ids))
    await c.commit()
    return await get_family(user_id)


async def remove_family_member(family_id: int, user_id: int) -> dict | None:
    """Исключить участника (мастер исправляет ошибочное добавление). Общий баланс остаётся
    семье. Если после исключения остаётся один человек — семья распускается: остаток общего
    счёта возвращается ему на ЛИЧНЫЙ баланс («Возврат с семейного счёта»), запись семьи
    удаляется (family_tx остаётся как архив). Возвращает обновлённую семью или None, если
    семья распущена. ValueError — участник не в этой семье."""
    c = _conn()
    cur = await c.execute(
        "SELECT 1 FROM family_members WHERE family_id = ? AND user_id = ?",
        (family_id, int(user_id)))
    if await cur.fetchone() is None:
        raise ValueError("Этот клиент не состоит в данном семейном счёте")
    now = int(time.time())
    await c.execute("DELETE FROM family_members WHERE family_id = ? AND user_id = ?",
                    (family_id, int(user_id)))
    cur = await c.execute(
        "SELECT user_id, user_name FROM family_members WHERE family_id = ?", (family_id,))
    left = [dict(r) for r in await cur.fetchall()]
    if len(left) > 1:
        await c.commit()
        return await get_family(left[0]["user_id"])
    # распускаем: остаток — последнему участнику на личный баланс
    cur = await c.execute("SELECT balance FROM family_accounts WHERE id = ?", (family_id,))
    bal = float((await cur.fetchone())["balance"])
    last = left[0] if left else None
    if last and bal:
        await c.execute(
            "INSERT INTO user_balance(user_id, balance, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance=ROUND(balance+excluded.balance,2), "
            "updated_at=excluded.updated_at", (last["user_id"], bal, now))
        await c.execute(
            "INSERT INTO balance_tx(user_id, amount, reason, at, comment) VALUES(?,?,?,?,NULL)",
            (last["user_id"], bal, "Возврат с семейного счёта", now))
    if last:
        await c.execute("DELETE FROM family_members WHERE family_id = ?", (family_id,))
    await c.execute("DELETE FROM family_accounts WHERE id = ?", (family_id,))
    await c.commit()
    return None


async def list_families() -> list[dict]:
    """Все семейные счета для админки: состав и общий баланс."""
    cur = await _conn().execute("SELECT * FROM family_accounts ORDER BY id")
    out = []
    for acc in await cur.fetchall():
        cur2 = await _conn().execute(
            "SELECT user_id, user_name, joined_at FROM family_members WHERE family_id = ? "
            "ORDER BY joined_at", (acc["id"],))
        out.append({"id": acc["id"], "title": acc["title"], "balance": float(acc["balance"]),
                    "created_at": acc["created_at"],
                    "members": [dict(r) for r in await cur2.fetchall()]})
    return out
