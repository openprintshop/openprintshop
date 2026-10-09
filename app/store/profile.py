"""Клиент как личность: аватарка, личные настройки, профиль v2/XP, корзина, история, идеи.

Всё, что описывает конкретного пользователя, а не его заказ:

* АВАТАРКИ ПРОФИЛЯ (user_avatars, OpenTodo fca4246e) — промпт и seed процедурной картинки.
* ЛИЧНЫЕ НАСТРОЙКИ (user_prefs, OpenTodo 0d11db78): режим подтверждения цены
  (PRICE_MODES/PRICE_PCT_MAX/price_auto_accept — денежный контракт «оптимистичного
  requote») и доставка по умолчанию.
* ПРОФИЛЬ V2: просмотренные достижения (ach_seen) и лента начислений XP.
* СЫРЬЁ ГЕЙМИФИКАЦИИ — user_profile_stats (OpenTodo 7a5be39c).
* БРОШЕННАЯ КОРЗИНА (idea #10) — ТРАНЗАКЦИОННАЯ СВЯЗКА: save_user_cart одним commit-ом
  делает DELETE либо UPSERT строки user_cart, остальные функции блока читают-пишут ту же
  таблицу; парные по смыслу нуджа abandoned_calcs/mark_calc_nudged (orders.nudged_at)
  едут вместе с ними.
* СОХРАНЁННЫЕ МОДЕЛИ (saved_models) — upsert_saved_model одним commit-ом делает
  SELECT + UPDATE|INSERT + DELETE-обрезку истории до keep штук; режется только целиком.
* ИДЕИ ПОЛЬЗОВАТЕЛЕЙ (ideas).

Выделено из app/db.py волной 5 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ: ни ленивых импортов `from .`, ни global, ни
квалификаций. Единственный ленивый импорт в модуле — `import time as _t` внутри add_idea:
это stdlib, поднимать его правилом волн не требуется, тело перенесено как есть. Логгер не
заводится: ни одно из 29 тел не пишет в лог. aiosqlite нужен только ради аннотации
`_to_saved(r: aiosqlite.Row)`.

ВТЯНУТОЕ ИМЯ: count_referrals (db.py, секция рефералки) — единственное, что добавило
замыкание. Её зовёт ГОЛЫМ именем тело user_profile_stats, а импорт app.db из store-модуля
запрещён, значит функция обязана была переехать в эту же волну. Это чистый
SELECT COUNT(*) FROM referrals без commit-а, транзакционную связку рефералки он не рвёт
(award_referral_if_due его не зовёт). Остальной блок рефералки (add_referral/get_referral/
award_referral_if_due/redeem_promo с атомарным захватом права на выплату) остаётся в db.py
до волны 6. ВАЖНО ДЛЯ ВОЛНЫ 6: остающаяся в db.py customer_card зовёт count_referrals
голым именем — её фасадная строка в db.py обязана дожить до волны 6, которая либо
импортирует count_referrals отсюда, либо переносит его store->store вместе с рефералкой
(прецедент — _calib_step_row/list_calib_steps из волны 3).

РЁБРА НАРУЖУ: set_user_delivery_prefs валидирует тип доставки через
store.orders.get_delivery_type, user_profile_stats читает членство в семье через
store.money.family_of, abandoned_calcs смотрит config.ADMIN_IDS. К соседям по волне
(store/collection.py, store/chat.py) рёбер нет ни одного.
"""
from __future__ import annotations

import json
import time

import aiosqlite

from .. import config
from .core import _abs, _abs_list, _conn, _jload, _rel, _rel_list
from .models import SavedModel
from .money import family_of
from .orders import get_delivery_type


# ---- Аватарки профиля (OpenTodo fca4246e) ----
async def set_user_avatar(user_id: int, prompt: str, seed: str) -> None:
    now = int(time.time())
    await _conn().execute(
        "INSERT INTO user_avatars(user_id, prompt, seed, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET prompt=excluded.prompt, seed=excluded.seed, "
        "updated_at=excluded.updated_at",
        (user_id, prompt, seed, now))
    await _conn().commit()


async def get_user_avatar(user_id: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM user_avatars WHERE user_id = ?", (user_id,))
    r = await cur.fetchone()
    return {k: r[k] for k in r.keys()} if r else None


# ---- Личные настройки клиента (OpenTodo 0d11db78) ----

PRICE_MODES = ("ask", "auto", "pct")
# Верхняя граница порога: «принимать подорожание до 100%» — это уже «всегда принимать»,
# для которого есть отдельный режим. Ограничение заодно ловит опечатку вроде «500».
PRICE_PCT_MAX = 100.0


_PREFS_DEFAULT = {"price_mode": "ask", "price_pct": 0.0,
                  "delivery_type_id": 0, "delivery_address": ""}


async def get_user_prefs(user_id: int) -> dict:
    """Настройки клиента. Строки может не быть — тогда отдаём значения по умолчанию
    (всегда подтверждать, доставка не задана), чтобы вызывающему не разбирать None."""
    cur = await _conn().execute(
        "SELECT price_mode, price_pct, delivery_type_id, delivery_address "
        "FROM user_prefs WHERE user_id = ?", (user_id,))
    r = await cur.fetchone()
    if not r:
        return dict(_PREFS_DEFAULT)
    mode = r["price_mode"] if r["price_mode"] in PRICE_MODES else "ask"
    return {"price_mode": mode,
            "price_pct": float(r["price_pct"] or 0) if mode in PRICE_MODES else 0.0,
            "delivery_type_id": int(r["delivery_type_id"] or 0),
            "delivery_address": r["delivery_address"] or ""}


async def set_user_delivery_prefs(user_id: int, type_id: int, address: str) -> dict:
    """Доставка по умолчанию (тип + адрес). Нормализация ЗДЕСЬ, как и у цены: тип обязан
    существовать и быть включённым, иначе он бы «протух» после удаления типа мастером и
    заказ уехал бы с несуществующим тарифом. Адрес без типа (и наоборот) хранить можно —
    клиент мог заполнить половину; применяется такая пара только целиком."""
    try:
        tid = int(type_id or 0)
    except (TypeError, ValueError):
        tid = 0
    if tid:
        t = await get_delivery_type(tid)
        if not t or not int(t["is_active"] or 0):
            tid = 0
    addr = (address or "").strip()[:300]
    now = int(time.time())
    await _conn().execute(
        "INSERT INTO user_prefs(user_id, delivery_type_id, delivery_address, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
        "delivery_type_id = excluded.delivery_type_id, "
        "delivery_address = excluded.delivery_address, updated_at = excluded.updated_at",
        (int(user_id), tid, addr, now))
    await _conn().commit()
    return await get_user_prefs(int(user_id))


async def set_user_price_prefs(user_id: int, mode: str, pct: float) -> dict:
    """Сохранить режим подтверждения цены. Значения нормализуем здесь, а не на входе в
    эндпоинт: настройка влияет на деньги, и «сколько попало» из клиента сюда попасть не должно."""
    mode = mode if mode in PRICE_MODES else "ask"
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = 0.0
    pct = min(PRICE_PCT_MAX, max(0.0, pct))
    if mode == "pct" and pct <= 0:
        mode = "ask"          # «принимать подорожание до 0%» — это и есть «всегда спрашивать»
    await _conn().execute(
        "INSERT INTO user_prefs(user_id, price_mode, price_pct, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET price_mode = excluded.price_mode, "
        "price_pct = excluded.price_pct, updated_at = excluded.updated_at",
        (user_id, mode, pct, int(time.time())))
    await _conn().commit()
    # Отдаём ВСЕ настройки, а не только цену: фронт кладёт ответ в состояние секции целиком,
    # и частичный объект стёр бы у него доставку по умолчанию.
    return await get_user_prefs(int(user_id))


def price_auto_accept(prefs: dict, old_price: float, new_price: float) -> bool:
    """Можно ли принять новую цену без клиента? Чистая функция — её же гоняют тесты.

    Считается ТОЛЬКО для подорожания: цена вниз и так применяется сразу, спрашивать там нечего.
    Порог в процентах невычислим, если прежней цены нет (заказ без сметы, первая цена от
    мастера) — такой случай всегда идёт на подтверждение: «+15%» от нуля не бывает."""
    mode = (prefs or {}).get("price_mode") or "ask"
    if mode == "auto":
        return True
    if mode != "pct":
        return False
    old = float(old_price or 0)
    if old <= 0:
        return False
    growth = (float(new_price) - old) / old * 100.0
    return growth <= float((prefs or {}).get("price_pct") or 0) + 1e-9


async def count_referrals(referrer_id: int) -> int:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (referrer_id,))
    return (await cur.fetchone())["c"]


# ---- профиль v2: просмотренные достижения + лента начислений XP ----
async def seen_achievements(user_id: int) -> set[str]:
    """Ключи достижений, которые пользователь уже видел (точка/NEW по ним не показываются)."""
    cur = await _conn().execute("SELECT key FROM ach_seen WHERE user_id = ?", (user_id,))
    return {r["key"] for r in await cur.fetchall()}


async def mark_achievements_seen(user_id: int, keys: list[str]) -> None:
    """Пометить достижения просмотренными (идемпотентно)."""
    if not keys:
        return
    now = int(time.time())
    await _conn().executemany(
        "INSERT OR IGNORE INTO ach_seen(user_id, key, seen_at) VALUES(?,?,?)",
        [(user_id, k, now) for k in keys])
    await _conn().commit()


async def xp_events(user_id: int, limit: int = 4) -> list[dict]:
    """Последние XP-события клиента для секции «Как начисляется XP»: [{kind, ref, at}, ...],
    новые сверху. kind ∈ printed|order|review|referral — суммы и подписи добавляет gamify."""
    cur = await _conn().execute(
        "SELECT 'printed' AS kind, id AS ref, updated_at AS at FROM requests "
        "  WHERE user_id=:u AND status='printed' "
        "UNION ALL "
        "SELECT 'order', id, created_at FROM requests WHERE user_id=:u AND status!='cancelled' "
        "UNION ALL "
        "SELECT 'review', id, updated_at FROM requests "
        "  WHERE user_id=:u AND review IS NOT NULL AND review!='' AND status!='cancelled' "
        "UNION ALL "
        "SELECT 'referral', referee_id, created_at FROM referrals WHERE referrer_id=:u "
        "ORDER BY at DESC LIMIT :n", {"u": user_id, "n": limit})
    return [{"kind": r["kind"], "ref": r["ref"], "at": r["at"]} for r in await cur.fetchall()]


async def user_profile_stats(user_id: int) -> dict:
    """Сырьё для геймификации (OpenTodo 7a5be39c): заказы/печати/потрачено/отзывы/материалы/
    рефералы по конкретному клиенту. Отменённые заказы не считаем. Разные пластики — по базовому
    имени (без «· цвет»)."""
    c = _conn()
    cur = await c.execute(
        "SELECT COUNT(*) AS orders, "
        "SUM(CASE WHEN status='printed' THEN 1 ELSE 0 END) AS printed, "
        "COALESCE(SUM(CASE WHEN status='printed' THEN price END),0) AS spent, "
        "SUM(CASE WHEN review IS NOT NULL AND review!='' THEN 1 ELSE 0 END) AS reviews "
        "FROM requests WHERE user_id=? AND status!='cancelled'", (user_id,))
    r = await cur.fetchone()
    cur = await c.execute(
        "SELECT DISTINCT plastic_name FROM requests "
        "WHERE user_id=? AND status='printed' AND plastic_name!=''", (user_id,))
    mats = {(row["plastic_name"] or "").split(" · ")[0].strip() for row in await cur.fetchall()}
    return {"orders": r["orders"] or 0, "printed": r["printed"] or 0,
            "spent": float(r["spent"] or 0), "reviews": r["reviews"] or 0,
            "materials": len(mats), "referrals": await count_referrals(user_id),
            # достижение «Семейный счёт» (2026-07-16): 1 — состоит в семье
            "family": 1 if await family_of(user_id) is not None else 0}


# ---- Брошенная корзина (idea #10): серверная копия корзины + нуджи ----
async def save_user_cart(uid: int, items: list, total: str) -> None:
    """Сохранить корзину пользователя (серверная копия для нуджа). Пустая корзина → удаляем строку."""
    if not items:
        await _conn().execute("DELETE FROM user_cart WHERE user_id=?", (uid,))
        await _conn().commit()
        return
    await _conn().execute(
        "INSERT INTO user_cart(user_id, items, total, count, updated_at, nudged_at) VALUES(?,?,?,?,?,0) "
        "ON CONFLICT(user_id) DO UPDATE SET items=excluded.items, total=excluded.total, "
        "count=excluded.count, updated_at=excluded.updated_at, nudged_at=0",
        (uid, json.dumps(items, ensure_ascii=False), str(total or ""), len(items), int(time.time())))
    await _conn().commit()


async def clear_user_cart(uid: int) -> None:
    await _conn().execute("DELETE FROM user_cart WHERE user_id=?", (uid,))
    await _conn().commit()


async def get_user_cart(uid: int) -> dict | None:
    """Текущая (неоформленная) корзина клиента — для карточки клиента в админке (a327c3ec).
    None — корзина пуста/оформлена (при checkout строка удаляется, clear_user_cart)."""
    cur = await _conn().execute(
        "SELECT items, total, count, updated_at FROM user_cart WHERE user_id=?", (uid,))
    r = await cur.fetchone()
    if not r or not r["count"]:
        return None
    items = _jload(r["items"], [])
    return {"items": items, "total": r["total"] or "", "count": r["count"],
            "updated_at": r["updated_at"]}


async def stale_carts(min_age_s: int, max_age_s: int) -> list[dict]:
    """Корзины, заброшенные между min_age и max_age назад и ещё не нуджёные (count>0)."""
    now = int(time.time())
    cur = await _conn().execute(
        "SELECT user_id, count, total FROM user_cart WHERE nudged_at=0 AND count > 0 "
        "AND updated_at <= ? AND updated_at >= ?", (now - min_age_s, now - max_age_s))
    return [{"user_id": r["user_id"], "count": r["count"], "total": r["total"]} for r in await cur.fetchall()]


async def mark_cart_nudged(uid: int) -> None:
    await _conn().execute("UPDATE user_cart SET nudged_at=? WHERE user_id=?", (int(time.time()), uid))
    await _conn().commit()


async def abandoned_calcs(min_age_s: int, max_age_s: int) -> list[dict]:
    """Брошенные расчёты (A5): последний ещё-не-нуджёный расчёт клиента давностью [min_age..max_age],
    по которому НЕ оформлен заказ (нет requests-строки не старше самого расчёта). Отсекаем:
    отписавшихся от рассылок (customers.no_broadcast), мастера (config.ADMIN_IDS — его собственные
    расчёты) и клиентов с активной корзиной (их ведёт maintenance.cart_nudge_loop — иначе два пинга).
    GROUP BY user_id + MAX(created_at): один кандидат на клиента (SQLite берёт bare-колонки из строки
    с максимумом created_at). Кандидатов НЕ помечаем — это делает mark_calc_nudged после отправки."""
    now = int(time.time())
    admin_ids = list(config.ADMIN_IDS) or [0]   # пустой список сломал бы NOT IN (); [0] безвреден
    ph = ",".join("?" for _ in admin_ids)
    cur = await _conn().execute(
        "SELECT o.id, o.user_id, o.model_label, o.total_price, MAX(o.created_at) AS created_at "
        "FROM orders o "
        "WHERE o.nudged_at = 0 "
        "AND o.created_at <= ? AND o.created_at >= ? "
        "AND NOT EXISTS (SELECT 1 FROM requests r "
        "                WHERE r.user_id = o.user_id AND r.created_at >= o.created_at) "
        "AND NOT EXISTS (SELECT 1 FROM customers c "
        "                WHERE c.user_id = o.user_id AND c.no_broadcast = 1) "
        "AND NOT EXISTS (SELECT 1 FROM user_cart uc "
        "                WHERE uc.user_id = o.user_id AND uc.count > 0) "
        f"AND o.user_id NOT IN ({ph}) "
        "GROUP BY o.user_id",
        (now - min_age_s, now - max_age_s, *admin_ids))
    return [{"id": r["id"], "user_id": r["user_id"], "model_label": r["model_label"],
             "total_price": r["total_price"], "created_at": r["created_at"]}
            for r in await cur.fetchall()]


async def mark_calc_nudged(user_id: int) -> None:
    """Пометить нуджёнными ВСЕ ещё-не-нуджёные расчёты клиента (не только последний): иначе
    следующий тик поднимет предыдущий по времени расчёт того же клиента и нуднёт повторно."""
    await _conn().execute(
        "UPDATE orders SET nudged_at=? WHERE user_id=? AND nudged_at=0", (int(time.time()), user_id))
    await _conn().commit()


# ---- сохранённые модели пользователя (история) ----
def _to_saved(r: aiosqlite.Row) -> SavedModel:
    return SavedModel(
        id=r["id"], user_id=r["user_id"], hash=r["hash"], label=r["label"],
        paths=_abs_list(r["paths"]), preview_path=_abs(r["preview_path"]),
        printer_id=r["printer_id"], plastic_id=r["plastic_id"], quality_id=r["quality_id"],
        strength_id=r["strength_id"], quantity=r["quantity"], updated_at=r["updated_at"],
        multicolor=(r["multicolor"] if "multicolor" in r.keys() else 0) or 0,
        plates=(r["plates"] if "plates" in r.keys() else 1) or 1,
    )


async def count_saved_models(user_id: int) -> int:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM saved_models WHERE user_id = ?", (user_id,))
    return (await cur.fetchone())["c"]


async def list_saved_models(user_id: int, limit: int = 10) -> list[SavedModel]:
    cur = await _conn().execute(
        "SELECT * FROM saved_models WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
    return [_to_saved(r) for r in await cur.fetchall()]


async def get_saved_model(sid: int) -> SavedModel | None:
    cur = await _conn().execute("SELECT * FROM saved_models WHERE id = ?", (sid,))
    r = await cur.fetchone()
    return _to_saved(r) if r else None


async def get_saved_model_by_hash(user_id: int, hash: str) -> SavedModel | None:
    """Сохранённая модель пользователя по хэшу — для «Повторить заказ» (связь request→модель)."""
    if not hash:
        return None
    cur = await _conn().execute(
        "SELECT * FROM saved_models WHERE user_id = ? AND hash = ? ORDER BY updated_at DESC LIMIT 1",
        (user_id, hash))
    r = await cur.fetchone()
    return _to_saved(r) if r else None


async def delete_saved_model(sid: int) -> None:
    await _conn().execute("DELETE FROM saved_models WHERE id = ?", (sid,))
    await _conn().commit()


async def upsert_saved_model(*, user_id: int, hash: str, label: str, paths: list,
                             preview_path: str | None, printer_id, plastic_id, quality_id,
                             strength_id, quantity, multicolor: int = 0, plates: int = 1,
                             keep: int = 30) -> int:
    now = int(time.time())
    pj = _rel_list(paths)
    cur = await _conn().execute(
        "SELECT id FROM saved_models WHERE user_id = ? AND hash = ?", (user_id, hash))
    row = await cur.fetchone()
    if row:
        await _conn().execute(
            "UPDATE saved_models SET label=?, paths=?, preview_path=?, printer_id=?, plastic_id=?, "
            "quality_id=?, strength_id=?, quantity=?, updated_at=?, multicolor=?, plates=? WHERE id=?",
            (label, pj, _rel(preview_path), printer_id, plastic_id, quality_id, strength_id,
             quantity, now, int(multicolor), int(plates), row["id"]))
        sid = row["id"]
    else:
        c2 = await _conn().execute(
            "INSERT INTO saved_models(user_id,hash,label,paths,preview_path,printer_id,plastic_id,"
            "quality_id,strength_id,quantity,created_at,updated_at,multicolor,plates) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, hash, label, pj, _rel(preview_path), printer_id, plastic_id, quality_id,
             strength_id, quantity, now, now, int(multicolor), int(plates)))
        sid = c2.lastrowid
    # оставляем только последние keep моделей на пользователя
    await _conn().execute(
        "DELETE FROM saved_models WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM saved_models WHERE user_id=? ORDER BY updated_at DESC LIMIT ?)",
        (user_id, user_id, keep))
    await _conn().commit()
    return sid


# ---- идеи пользователей ----
async def add_idea(user_id: int, user_name: str, text: str) -> int:
    import time as _t
    cur = await _conn().execute(
        "INSERT INTO ideas(user_id, user_name, text, created_at) VALUES(?,?,?,?)",
        (user_id, user_name, text, int(_t.time())),
    )
    await _conn().commit()
    return cur.lastrowid
