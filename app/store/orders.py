"""Заказ как сущность целиком: requests и его таймлайн, корзина-группа, доставка, скидки.

Один модуль на всё, что живёт вокруг таблицы requests — семь секций db.py, которые
разрезать нельзя:

* ЗАКАЗ И ТАЙМЛАЙН (requests + req_status_history). add_request под ОДНИМ commit-ом
  вставляет строку заказа и начальную точку таймлайна; update_request тем же commit-ом
  дописывает точку при смене статуса; delete_request одним commit-ом сносит таймлайн,
  переписку и сам заказ. Владение таблицей режется только целиком.
* КОРЗИНА-ГРУППА (order_groups) + ДОСТАВКА (delivery_types). Белый список
  _GROUP_FIELDS — это поля шапки группы, и почти все они про доставку, а
  group_anchor_request_id существует ровно затем, чтобы у стоимости доставки был ОДИН
  детерминированный носитель (иначе клиент платит за неё дважды). Отдельный модуль
  доставки означал бы двусторонний импорт с группой.
* САМОПЕЧАТЬ МАСТЕРА (self_print). create_self_print_order — цепочка «заказ ↔ запись
  журнала»: add_request -> UPDATE requests.self_print -> set_print_journal_fields.
* ПЕРСОНАЛЬНЫЕ СКИДКИ (user_discounts). Держатся за requests через list_customers —
  пикер клиентов строится из заказов, не из отдельного справочника.
* «НЕПРОСМОТРЕННЫЕ» для LCD-дашборда — колонка requests.admin_seen_at.
* ЛЕГАСИ-ТАБЛИЦА orders (история расчётов) — add_order, единственный её писатель.
* ДВА АГРЕГАТА по requests — daily_revenue (выручка по дням) и queue_load (загрузка
  очереди); queue_load — единственный здесь потребитель складских литералов активных
  статусов (_RESERVING_STATUSES/_RES_PH живут в store/plastics с волны 2; переносить их
  сюда нельзя — склад ими пользуется сам, получился бы обратный импорт).

Выделено из app/db.py волной 4 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ: ни ленивых импортов, ни global, ни
квалификаций — замыкание по зависимостям оказалось пустым (ни одно тело не ссылается на
остающееся в db.py top-level имя). Логгер не заводится: ни одно из 40 тел не пишет в лог.

ЛОЖНАЯ ЗАВИСИМОСТЬ, не повторять: наивный обход ast.Name даёт «add_request ->
promised_date», но promised_date у add_request — kw-ПАРАМЕТР, а не одноимённая функция
db.py. Тащить её сюда нельзя вдвойне: top-level promised_date рядом с
add_request(..., promised_date=...) — ровно та коллизия параметр↔модульное имя, из-за
которой волна 2 звала хэндл настроек store_settings (ruff F811). Замыкание считать
scope-aware (symtable), а не обходом Name.

МИГРАЦИЯ: backfill_order_groups зовётся из db.init() на шаге user_version < 6. Тело init
не правилось — имя разрешается через фасадную строку в db.py, и она обязана существовать
(прецедент: init так же зовёт уехавшие _canon_color и _seed_service_items).

ЛОВУШКА ДЛЯ ТЕСТОВ: delete_request, delete_test_requests (через delete_request) и
get_status_history зовут get_request ГОЛЫМ именем, поэтому
`monkeypatch.setattr(db, "get_request", ...)` до этих внутренних вызовов больше НЕ
достаёт — патчить app.store.orders. Сегодняшние пять мест подмены (test_admin_orderfile,
test_desk_chat_api, test_media_delete) ни одну из трёх функций не дергают и остаются
зелёными; ловушка латентная, для будущих тестов.

ДЕНЬГИ ЗДЕСЬ НЕ ЖИВУТ: в графе вызовов между этим модулем и app/store/money.py нет ни
одного ребра (add_payment не зовёт adjust_balance — обе стороны сшивает app/payments.py
снаружи). Если ребро появится, это признак, что домен захватил чужое.
"""
from __future__ import annotations

import json
import time

import aiosqlite

from .. import config
from .core import _abs, _abs_list, _conn, _jload, _rel, _rel_list
from .deferred import _cancel_jobs_nocommit
from .models import Request
from .plastics import _RES_PH, _RESERVING_STATUSES
from .printjournal import set_print_journal_fields


async def create_self_print_order(admin_id: int, *, plastic_name: str, grams: float, minutes: float,
                                  price: float, label: str, breakdown: dict | None = None,
                                  jid: int | None = None) -> int:
    """Самопечать мастера как «заказ админа» (слоистость 0, status='self' — ВНЕ клиентской выручки).
    price = во сколько обошлось бы заказом (граммы+время по прайсу). Привязывает запись журнала
    (jid) к заказу, НЕ трогая её source='self'. Возвращает id заказа."""
    rid = await add_request(
        user_id=int(admin_id), user_name="мастер", model_label=label or "печать для себя",
        printer="", plastic_name=plastic_name or "", quantity=1, quality="слоистость 0",
        strength="", grams_total=float(grams or 0), minutes_total=float(minutes or 0),
        price=float(price or 0), preview_path=None, is_custom=1, status="self",
        breakdown=breakdown, created_by_admin_id=int(admin_id))
    await _conn().execute("UPDATE requests SET self_print=1 WHERE id=?", (rid,))
    await _conn().commit()
    if jid:
        await set_print_journal_fields(jid, request_id=rid)
    return rid


async def admin_would_spend(admin_id: int) -> float:
    """«Было бы потрачено» мастером — сумма цен его самопечатей (self_print=1), т.е. сколько эти
    печати стоили бы заказом, будь у мастера не свой принтер."""
    cur = await _conn().execute(
        "SELECT COALESCE(SUM(price),0) AS s FROM requests WHERE self_print=1 AND user_id=?",
        (int(admin_id),))
    return round(float((await cur.fetchone())["s"] or 0.0), 2)


async def is_self_print_order(rid: int) -> bool:
    """Заказ rid — учтённая самопечать мастера (self_print=1).

    Нужен как признак «эту печать уже разобрали как самопечать»: сам по себе request_id у
    записи журнала таким признаком НЕ является — printwatch проставляет его ещё при создании
    записи, когда печать шла по обычному заказу. Без этой проверки повторный выбор «Для себя»
    создавал ВТОРОЙ заказ-самопечать с полной ценой, а admin_would_spend суммировал оба."""
    if not rid:
        return False
    cur = await _conn().execute("SELECT self_print FROM requests WHERE id = ?", (int(rid),))
    row = await cur.fetchone()
    return bool(row and row["self_print"])


# ---- персональные скидки клиентам ----
async def set_user_discount(user_id: int, discount_pct: float, name: str | None = None) -> None:
    await _conn().execute(
        "INSERT INTO user_discounts(user_id, discount_pct, name, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET discount_pct=excluded.discount_pct, name=excluded.name",
        (user_id, discount_pct, name, int(time.time())))
    await _conn().commit()


async def get_user_discount(user_id: int) -> float:
    cur = await _conn().execute("SELECT discount_pct FROM user_discounts WHERE user_id = ?", (user_id,))
    r = await cur.fetchone()
    return float(r["discount_pct"]) if r else 0.0


async def list_user_discounts() -> list[tuple]:
    cur = await _conn().execute("SELECT user_id, discount_pct, name FROM user_discounts ORDER BY created_at DESC")
    return [(r["user_id"], r["discount_pct"], r["name"]) for r in await cur.fetchall()]


async def delete_user_discount(user_id: int) -> None:
    await _conn().execute("DELETE FROM user_discounts WHERE user_id = ?", (user_id,))
    await _conn().commit()


async def list_customers(limit: int = 200) -> list[dict]:
    """Известные клиенты для пикера персональных скидок (OpenTodo 51c8097e): уникальные
    user_id из заказов с самым свежим именем, числом заказов и текущей персональной скидкой
    (если уже назначена). Сортировка — по свежести последнего заказа."""
    cur = await _conn().execute(
        "SELECT r.user_id AS uid, "
        "  (SELECT user_name FROM requests r2 WHERE r2.user_id = r.user_id "
        "   AND r2.user_name IS NOT NULL AND r2.user_name != '' "
        "   ORDER BY r2.created_at DESC, r2.id DESC LIMIT 1) AS name, "
        "  COUNT(*) AS orders, MAX(r.created_at) AS last_at, "
        "  (SELECT discount_pct FROM user_discounts ud WHERE ud.user_id = r.user_id) AS pct "
        "FROM requests r WHERE r.user_id IS NOT NULL "
        "GROUP BY r.user_id ORDER BY last_at DESC LIMIT ?", (limit,))
    return [{"uid": row["uid"], "name": row["name"] or "", "orders": row["orders"],
             "pct": row["pct"]} for row in await cur.fetchall()]


# ---- заказы (requests) со статусами ----
def _to_request(r: aiosqlite.Row) -> Request:
    return Request(
        id=r["id"], user_id=r["user_id"], user_name=r["user_name"], model_label=r["model_label"],
        printer=r["printer"], plastic_name=r["plastic_name"], quantity=r["quantity"],
        quality=r["quality"], strength=r["strength"], grams_total=r["grams_total"],
        minutes_total=r["minutes_total"], price=r["price"], status=r["status"],
        pending_price=r["pending_price"], preview_path=_abs(r["preview_path"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
        price_comment=(r["price_comment"] if "price_comment" in r.keys() else None),
        dims=(r["dims"] if "dims" in r.keys() else "") or "",
        multicolor=(r["multicolor"] if "multicolor" in r.keys() else 0) or 0,
        plates=(r["plates"] if "plates" in r.keys() else 1) or 1,
        hash=(r["hash"] if "hash" in r.keys() else "") or "",
        rating=(r["rating"] if "rating" in r.keys() else None),
        review=(r["review"] if "review" in r.keys() else None),
        done_photo=_abs(r["done_photo"] if "done_photo" in r.keys() else None),
        src_url=(r["src_url"] if "src_url" in r.keys() else "") or "",
        file_path=_abs((r["file_path"] if "file_path" in r.keys() else "") or "") or "",
        promo=(r["promo"] if "promo" in r.keys() else "") or "",
        promo_ok=(r["promo_ok"] if "promo_ok" in r.keys() else 0) or 0,
        quality_id=(r["quality_id"] if "quality_id" in r.keys() else None),
        strength_id=(r["strength_id"] if "strength_id" in r.keys() else None),
        is_custom=(r["is_custom"] if "is_custom" in r.keys() else 0) or 0,
        description=(r["description"] if "description" in r.keys() else None),
        photos=_abs_list(r["photos"] if "photos" in r.keys() else None),
        breakdown=_jload(r["breakdown"] if "breakdown" in r.keys() else None, None),
        due_date=(r["due_date"] if "due_date" in r.keys() else "") or "",
        dev_fee=(r["dev_fee"] if "dev_fee" in r.keys() else 0.0) or 0.0,
        slicer_shot=_abs(r["slicer_shot"] if "slicer_shot" in r.keys() else None),
        timelapse=_abs(r["timelapse"] if "timelapse" in r.keys() else None),
        created_by_admin_id=(r["created_by_admin_id"] if "created_by_admin_id" in r.keys() else None),
        paid=int((r["paid"] if "paid" in r.keys() else 0) or 0),
        paid_at=int((r["paid_at"] if "paid_at" in r.keys() else 0) or 0),
        pay_method=(r["pay_method"] if "pay_method" in r.keys() else "") or "",
        reject_reason=(r["reject_reason"] if "reject_reason" in r.keys() else None),
        unsliced=int((r["unsliced"] if "unsliced" in r.keys() else 0) or 0),
        cogs_actual=(r["cogs_actual"] if "cogs_actual" in r.keys() else None),
        product_id=(r["product_id"] if "product_id" in r.keys() else None),
        urgency=(r["urgency"] if "urgency" in r.keys() else "") or "",
        promised_date=(r["promised_date"] if "promised_date" in r.keys() else "") or "",
        order_group=(r["order_group"] if "order_group" in r.keys() else "") or "",
        price_pending=int((r["price_pending"] if "price_pending" in r.keys() else 0) or 0))


async def add_request(*, user_id: int, user_name: str, model_label: str, printer: str,
                      plastic_name: str, quantity: int, quality: str, strength: str,
                      grams_total: float, minutes_total: float, price: float,
                      preview_path: str | None, dims: str = "",
                      multicolor: int = 0, plates: int = 1, hash: str = "",
                      order_group: str | None = None, src_url: str = "",
                      file_path: str = "", promo: str = "", promo_ok: int = 0,
                      quality_id: int | None = None, strength_id: int | None = None,
                      is_custom: int = 0, description: str | None = None,
                      photos: list | None = None, breakdown: dict | None = None,
                      status: str = "new", pending_price: float | None = None,
                      price_comment: str | None = None,
                      created_by_admin_id: int | None = None, unsliced: int = 0,
                      product_id: int | None = None,
                      urgency: str = "", promised_date: str = "",
                      source: str = "", price_pending: int = 0,
                      commit: bool = True) -> int:
    """Новая строка заказа + начальная точка таймлайна.

    commit=False — только для того, кто сразу следом пишет связанную строку и сам делает
    commit (очередь досчёта: заказ и его задание должны появиться ОДНИМ commit-ом, иначе
    рестарт между ними оставит заказ «цена считается» без задания, а повтор события
    приёмной — второй такой же заказ)."""
    now = int(time.time())
    # Заказ мастера самому себе (клиент — админ) сразу «просмотрен»: незачем гонять его в
    # «непросмотренные» на LCD-дашборде — мастер и так только что его оформил (OpenTodo 040525a2).
    admin_seen_at = now if config.is_admin(user_id) else None
    cur = await _conn().execute(
        "INSERT INTO requests(user_id,user_name,model_label,printer,plastic_name,quantity,quality,"
        "strength,grams_total,minutes_total,price,status,pending_price,preview_path,created_at,updated_at,"
        "dims,multicolor,plates,hash,order_group,src_url,file_path,promo,promo_ok,quality_id,strength_id,"
        "is_custom,description,photos,breakdown,price_comment,created_by_admin_id,unsliced,product_id,"
        "urgency,promised_date,admin_seen_at,source,price_pending) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, user_name, model_label, printer, plastic_name, quantity, quality, strength,
         grams_total, minutes_total, price, status, pending_price, _rel(preview_path), now, now, dims,
         int(multicolor), int(plates), hash, order_group, src_url, _rel(file_path), promo, int(promo_ok),
         quality_id, strength_id, int(is_custom), description,
         _rel_list(photos) if photos else None,
         json.dumps(breakdown) if breakdown else None, price_comment, created_by_admin_id,
         int(unsliced), product_id, (urgency or ""), (promised_date or ""), admin_seen_at,
         (source or ""), int(price_pending or 0)))
    rid = cur.lastrowid
    await _conn().execute(            # начальная точка таймлайна
        "INSERT INTO req_status_history(request_id, status, at) VALUES(?,?,?)", (rid, status, now))
    if commit:
        await _conn().commit()
    return rid


# ─────────────────────────── Заказ-корзина: группа (B5) ───────────────────────────
async def create_order_group(gid: str, user_id: int, *, comment: str = "",
                             delivery_method: str = "", delivery_address: str = "",
                             total_snapshot: float = 0.0, batch: int = 0,
                             delivery_type_id: int | None = None, delivery_type_name: str = "",
                             delivery_cost: float = 0.0,
                             delivery_cost_source: str = "auto") -> None:
    """Шапка заказа над позициями (requests с общим order_group=gid). INSERT OR IGNORE:
    повторный вызов на существующий gid не перезаписывает. Итог группы всё равно ДЕРИВИРУЕТСЯ
    из суммы цен позиций при выдаче + delivery_cost — total_snapshot лишь фиксирует сумму на
    момент оформления. delivery_cost считает сервер по типу доставки (A2), не клиент.

    Заводится не только корзиной: одиночный заказ с доставкой тоже получает шапку из ОДНОЙ
    позиции — иначе адресу, стоимости доставки и трек-номеру негде жить."""
    if not gid:
        return
    now = int(time.time())
    await _conn().execute(
        "INSERT OR IGNORE INTO order_groups(id, user_id, comment, delivery_method, "
        "delivery_address, total_snapshot, batch, created_at, delivery_type_id, "
        "delivery_type_name, delivery_cost, delivery_cost_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (gid, int(user_id), (comment or None), (delivery_method or None),
         (delivery_address or None), float(total_snapshot or 0), int(batch), now,
         (int(delivery_type_id) if delivery_type_id else None),
         (delivery_type_name or None), float(delivery_cost or 0),
         (delivery_cost_source or "auto")))
    await _conn().commit()


_GROUP_FIELDS = ("comment", "delivery_method", "delivery_address", "delivery_type_id",
                 "delivery_type_name", "delivery_cost", "delivery_cost_source", "delivery_paid",
                 "track_carrier", "track_number", "track_url", "shipped_at", "delivered_at",
                 "total_snapshot")


async def update_order_group(gid: str, **fields) -> bool:
    """Правка шапки группы по белому списку полей (доставка/отправление). False — нечего писать
    или группы нет. Белый список обязателен: сюда приходят значения из админских ручек."""
    data = {k: v for k, v in fields.items() if k in _GROUP_FIELDS}
    if not gid or not data:
        return False
    assignments = ", ".join(f"{k} = ?" for k in data)
    cur = await _conn().execute(f"UPDATE order_groups SET {assignments} WHERE id = ?",
                                (*data.values(), gid))
    await _conn().commit()
    return cur.rowcount > 0


async def get_order_group(gid: str) -> dict | None:
    """Шапка группы как dict или None. None — легальный случай (старый cart-заказ до бэкфилла
    v6): вызывающий обязан иметь fallback (итог = сумма позиций, доставка пуста)."""
    if not gid:
        return None
    cur = await _conn().execute("SELECT * FROM order_groups WHERE id = ?", (gid,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def list_requests_by_group(gid: str) -> list[Request]:
    """Все позиции заказа-корзины по общему order_group (по возрастанию id). Пусто при пустом gid —
    защита от NULL-группировки (иначе вернулись бы ВСЕ одиночные заказы)."""
    if not gid:
        return []
    cur = await _conn().execute(
        "SELECT * FROM requests WHERE order_group = ? ORDER BY id ASC", (gid,))
    return [_to_request(r) for r in await cur.fetchall()]


async def backfill_order_groups() -> None:
    """Разовый бэкфилл шапок для cart-заказов, у которых позиции уже несут order_group, но строки
    order_groups нет (появились до B5). Идемпотентно: INSERT OR IGNORE не тронет уже заведённые.
    ФИЛЬТР order_group IS NOT NULL AND <> '' обязателен — иначе NULL-группа (все одиночные заказы)
    схлопнулась бы в одну фейковую мега-группу."""
    # Защита от синтетических/урезанных схем requests (напр. миграционные тесты, где в таблице нет
    # price/created_at): бэкфиллить нечего и незачем ронять init. На боевой БД колонки всегда есть.
    cur = await _conn().execute("PRAGMA table_info(requests)")
    cols = {r["name"] for r in await cur.fetchall()}
    if not {"order_group", "price", "created_at", "user_id"} <= cols:
        return
    await _conn().execute(
        "INSERT OR IGNORE INTO order_groups(id, user_id, total_snapshot, created_at) "
        "SELECT order_group, MIN(user_id), SUM(price), MIN(created_at) FROM requests "
        "WHERE order_group IS NOT NULL AND order_group <> '' GROUP BY order_group")
    await _conn().commit()


async def group_id_of_request(rid: int) -> str:
    """order_group позиции ('' — одиночный заказ). Точечный запрос: платёжному тракту нужна
    только группа, тащить ради неё весь Request незачем."""
    cur = await _conn().execute("SELECT order_group FROM requests WHERE id = ?", (int(rid),))
    r = await cur.fetchone()
    return (r["order_group"] or "") if r else ""


async def group_anchor_request_id(gid: str) -> int | None:
    """Якорная позиция группы = МИНИМАЛЬНЫЙ id среди её позиций. На неё вешается стоимость
    доставки в онлайн-счёте (A2): группа платится по позициям, а доставка одна на заказ —
    ей нужен ровно один, детерминированный носитель, иначе клиент заплатит за неё дважды."""
    if not gid:
        return None
    cur = await _conn().execute("SELECT MIN(id) AS a FROM requests WHERE order_group = ?", (gid,))
    r = await cur.fetchone()
    return int(r["a"]) if r and r["a"] is not None else None


# ─────────────────────────── Типы доставки (A2) ───────────────────────────
async def list_delivery_types(active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM delivery_types"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort ASC, id ASC"
    cur = await _conn().execute(sql)
    return [dict(r) for r in await cur.fetchall()]


async def get_delivery_type(tid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM delivery_types WHERE id = ?", (int(tid or 0),))
    r = await cur.fetchone()
    return dict(r) if r else None


async def add_delivery_type(name: str, price: float, *, free_from: float = 0.0, eta: str = "",
                            comment: str = "", sort: int = 0) -> int:
    cur = await _conn().execute(
        "INSERT INTO delivery_types(name, price, free_from, eta, comment, sort, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (name, float(price), float(free_from or 0), eta or "", comment or "", int(sort),
         int(time.time())))
    await _conn().commit()
    return cur.lastrowid


async def update_delivery_type(tid: int, **fields) -> None:
    allowed = {"name", "price", "free_from", "eta", "comment", "is_active", "sort"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    assignments = ", ".join(f"{k} = ?" for k in data)
    await _conn().execute(f"UPDATE delivery_types SET {assignments} WHERE id = ?",
                          (*data.values(), int(tid)))
    await _conn().commit()


async def delete_delivery_type(tid: int) -> None:
    await _conn().execute("DELETE FROM delivery_types WHERE id = ?", (int(tid),))
    await _conn().commit()


async def get_request(rid: int) -> Request | None:
    cur = await _conn().execute("SELECT * FROM requests WHERE id = ?", (rid,))
    r = await cur.fetchone()
    return _to_request(r) if r else None


async def list_user_requests(user_id: int, limit: int = 20) -> list[Request]:
    cur = await _conn().execute(
        "SELECT * FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
    return [_to_request(r) for r in await cur.fetchall()]


async def count_user_requests(user_id: int) -> int:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM requests WHERE user_id = ?", (user_id,))
    return (await cur.fetchone())["c"]


async def pending_price_count(user_id: int) -> int:
    """Сколько заказов клиента ждут подтверждения новой цены или выставленного счёта."""
    cur = await _conn().execute(
        "SELECT COUNT(*) AS c FROM requests WHERE user_id = ? "
        "AND status IN ('price_changed', 'invoiced') AND pending_price IS NOT NULL", (user_id,))
    return (await cur.fetchone())["c"]


def is_self_order(r) -> bool:
    """Заказ мастера самому себе: status='self' ИЛИ заказчик — админ. Питон-пара к
    _self_print_predicate (тот же критерий, что делит списки на клиентские и самопечать).

    Нужен, чтобы не гонять мастера по КЛИЕНТСКОМУ сценарию его же заказа: не слать ему
    «ваш заказ напечатан — оцените», не просить отзыв и не рапортовать админам «клиент
    оценил» (клиент и есть админ)."""
    if r is None:
        return False
    if getattr(r, "status", "") == "self":
        return True
    uid = getattr(r, "user_id", None)
    try:
        return bool(uid) and int(uid) in [int(x) for x in (config.ADMIN_IDS or [])]
    except (TypeError, ValueError):
        return False


def _self_print_predicate() -> tuple[str, list]:
    """SQL-предикат «печать для себя» мастера: заказ-самопечать (status='self') ИЛИ заказчик —
    сам админ (user_id ∈ config.ADMIN_IDS). Совпадает с логикой auto-seen в add_request
    («заказ мастера самому себе»). Возвращает (SQL, параметры)."""
    ids = [int(x) for x in (config.ADMIN_IDS or [])]
    if ids:
        ph = ",".join("?" for _ in ids)
        return f"(status = 'self' OR user_id IN ({ph}))", list(ids)
    return "(status = 'self')", []


async def list_requests(limit: int = 30, search: str = "", kind: str = "all") -> list[Request]:
    """Последние заказы; search — сквозной поиск (CRM этап 2): по #id, имени клиента,
    user_id и названию модели. Объёмы малы — LIKE достаточно.
    kind — какой список отдать (админка держит два разных: клиентские заказы и самопечать мастера):
      'client' — только клиентские заказы (без «печати для себя»);
      'self'   — только «печать для себя» (status='self' ИЛИ клиент-админ);
      'all'    — всё (обратная совместимость: CSV-экспорт и прочие вызовы)."""
    q = (search or "").strip()
    where: list[str] = []
    params: list = []
    if kind in ("client", "self"):
        pred, pp = _self_print_predicate()
        where.append(pred if kind == "self" else f"NOT {pred}")
        params += pp
    if q:
        like = f"%{q}%"
        rid = int(q.lstrip("#")) if q.lstrip("#").isdigit() else -1
        where.append("(id = ? OR user_id = ? OR user_name LIKE ? OR model_label LIKE ?)")
        params += [rid, rid if rid > 0 else -1, like, like]
    sql = "SELECT * FROM requests"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cur = await _conn().execute(sql, params)
    return [_to_request(r) for r in await cur.fetchall()]


# Поля requests, хранящие путь под DATA_DIR (C2): пишем их только через rel_data.
_PATH_FIELDS = ("done_photo", "preview_path", "slicer_shot", "timelapse", "file_path")


async def _storable_request_sets(rid: int, sets: dict) -> dict:
    """Значения полей заказа → вид, в котором они лежат в БД (общее для update_request и
    apply_client_edit): масштаб переносится в новый снимок сметы, снимок — JSON-строкой,
    пути — относительно DATA_DIR."""
    sets = dict(sets)
    bd_new = sets.get("breakdown")
    if isinstance(bd_new, dict) and "scale_pct" not in bd_new:
        # Масштаб — свойство заказа, но живёт в снимке сметы. Пересборка сметы (цена «по граммам
        # и времени», смена параметров клиентом, пересчёт мастера) собирает снимок из
        # PriceBreakdown, где масштаба нет, — и молча сбрасывала заказ в 100%: следующий
        # пересчёт/допечатка слайсили не в том размере. Переносим масштаб из прежнего снимка.
        cur = await _conn().execute("SELECT breakdown FROM requests WHERE id = ?", (rid,))
        row = await cur.fetchone()
        old = _jload(row[0], None) if row else None
        if isinstance(old, dict) and old.get("scale_pct"):
            sets["breakdown"] = {**bd_new, "scale_pct": old["scale_pct"]}
    if isinstance(sets.get("breakdown"), (dict, list)):   # breakdown хранится JSON-строкой
        sets["breakdown"] = json.dumps(sets["breakdown"])
    for k in _PATH_FIELDS:      # C2: в БД пути только относительно DATA_DIR
        if k in sets:
            sets[k] = _rel(sets[k])
    return sets


# Что меняет клиент в «Изменить» (OpenTodo 42ce4724): параметры печати и пересчитанная цена.
_CLIENT_EDIT_FIELDS = ("plastic_name", "quality", "strength", "quantity", "quality_id", "strength_id",
                       "grams_total", "minutes_total", "price", "dims", "preview_path", "breakdown")


async def apply_client_edit(rid: int, **fields) -> bool:
    """Записать правку заказа клиентом («Изменить»: пластик/цвет/качество/прочность/тираж и
    новая цена) — ОДНИМ условным UPDATE и одним commit-ом (review:e0#3).

    Пересчёт правки идёт долго, а заказ за это время могли увести: очередь досчёта (Э0) прислала
    цену на подтверждение, мастер назвал свою. Проверка «заказ ещё new» отдельным чтением перед
    записью оставляла окно: результат сборщика ложился между чтением и записью, и клиент потом
    «принимал» цену, посчитанную по СТАРОМУ выбору, при новых параметрах в карточке. Здесь
    условие и запись — один оператор (status='new', не индивидуальный заказ): кто первым, тот и
    прав, второй увидит rowcount 0. Цену назвал сам клиент — ждать ПК больше нечего: флаг
    ожидания гасится тем же оператором, задания очереди отменяются в той же транзакции.

    True — правка записана; False — заказ уже не «Новый» (вызывающему: «обновите список»)."""
    sets = {k: v for k, v in fields.items() if k in _CLIENT_EDIT_FIELDS}
    sets = await _storable_request_sets(rid, sets)
    sets.update(price_pending=0, updated_at=int(time.time()))
    assignments = ", ".join(f"{k} = ?" for k in sets)
    cur = await _conn().execute(
        f"UPDATE requests SET {assignments} WHERE id = ? AND status = 'new' "
        "AND COALESCE(is_custom, 0) = 0", (*sets.values(), int(rid)))
    if cur.rowcount != 1:
        await _conn().commit()      # закрыть неявную транзакцию UPDATE, ничего не изменившего
        return False
    await _cancel_jobs_nocommit(rid, "клиент пересчитал заказ сам («Изменить»)")
    await _conn().commit()
    return True


async def update_request(rid: int, **fields) -> None:
    allowed = {"status", "pending_price", "price", "price_comment", "rating", "review", "done_photo",
               "due_date", "grams_total", "minutes_total", "breakdown",
               # правка параметров заказа клиентом до взятия в работу (OpenTodo 42ce4724)
               "plastic_name", "quality", "strength", "quantity", "quality_id", "strength_id",
               "dims", "preview_path", "printer",
               # счёт-расчёт индивидуального заказа (OpenTodo 6aaa6a19); multicolor — счёт
               # в многоцветной печати помечает заказ цветным (OpenTodo efca76c3)
               "dev_fee", "slicer_shot", "multicolor",
               "timelapse",      # таймлапс печати (OpenTodo 70b5a590)
               "paid",           # галочка «оплачен» (рефералка, OpenTodo 7f257a33)
               "paid_at", "pay_method",   # онлайн-оплата (A1): когда и как оплачен
               "unsliced",       # заказ во время слайса досчитался → снимаем флаг «без сметы» (order_now)
               "price_pending",  # цену считает ПК (очередь досчёта, Э0) — см. ниже про уход из 'new'
               "reject_reason",  # причина отказа — аналитика воронки (crm-roadmap.md G6)
               # Шапка заказа для одиночного заказа с доставкой (доработка A2 31.07): адресу,
               # стоимости доставки и трек-номеру негде жить, кроме order_groups, поэтому
               # такой заказ задним числом привязывается к группе из ОДНОЙ позиции.
               "order_group"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    sets = await _storable_request_sets(rid, sets)
    # Заказ уходит из 'new' (мастер назначил цену, выставил счёт, взял в работу, клиент
    # отменил…) — ожидание досчёта ПК тем же commit-ом снимается: флаг гасим, задания
    # очереди отменяем. Применение результата и так условное (status='new' AND
    # price_pending=1), но без этого задание, ждущее в очереди, ожило бы, верни мастер заказ
    # в «Новый» позже, — и молча переписало бы его цену. Одна точка вместо правки в каждой
    # ручке, которая двигает статус.
    leaves_new = "status" in sets and sets["status"] != "new"
    if leaves_new:
        sets.setdefault("price_pending", 0)
    now = int(time.time())
    sets["updated_at"] = now
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE requests SET {assignments} WHERE id = ?", (*sets.values(), rid))
    if "status" in sets:   # фиксируем смену статуса для таймлайна
        await _conn().execute(
            "INSERT INTO req_status_history(request_id, status, at) VALUES(?,?,?)",
            (rid, sets["status"], now))
    if leaves_new:
        await _cancel_jobs_nocommit(rid, f"заказ ушёл из «Новый» (статус {sets['status']})")
    await _conn().commit()


# ---- «непросмотренные» заказы для LCD-дашборда (design_handoff_lcd) ----
async def mark_request_seen(rid: int) -> None:
    """Мастер открыл карточку заказа — заказ больше не «непросмотренный»."""
    await _conn().execute(
        "UPDATE requests SET admin_seen_at = ? WHERE id = ? AND admin_seen_at IS NULL",
        (int(time.time()), int(rid)))
    await _conn().commit()


async def count_unseen_requests() -> int:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM requests WHERE admin_seen_at IS NULL")
    return (await cur.fetchone())["c"]


async def mark_all_requests_seen() -> int:
    """Пометить ВСЕ непросмотренные заказы просмотренными (кнопка в админке, OpenTodo 040525a2).
    Возвращает число заказов, которых это коснулось."""
    now = int(time.time())
    cur = await _conn().execute(
        "UPDATE requests SET admin_seen_at = ? WHERE admin_seen_at IS NULL", (now,))
    await _conn().commit()
    return cur.rowcount if cur.rowcount is not None else 0


async def delete_request(rid: int) -> Request | None:
    """Полностью удалить заказ из памяти вместе с дочерними записями (таймлайн статусов,
    переписка). Возвращает удалённый Request (или None, если такого не было) — чтобы вызывающий
    мог подчистить связанные файлы (фото результата, фото индивидуального заказа)."""
    r = await get_request(rid)
    if not r:
        return None
    await _conn().execute("DELETE FROM req_status_history WHERE request_id = ?", (rid,))
    await _conn().execute("DELETE FROM order_messages WHERE request_id = ?", (rid,))
    await _conn().execute("DELETE FROM requests WHERE id = ?", (rid,))
    await _conn().commit()
    return r


async def delete_test_requests() -> list[Request]:
    """Массовая чистка тестовых заказов: индивидуальные заказы без цены (price<=0) и заказы
    с нулевыми граммами/минутами — типичный след тестовых заявок. Возвращает удалённые
    Request (для подчистки файлов). Реальные заказы с ценой/расчётом не трогаем."""
    cur = await _conn().execute(
        "SELECT id FROM requests WHERE COALESCE(price,0) <= 0 "
        "AND COALESCE(grams_total,0) <= 0 AND COALESCE(minutes_total,0) <= 0")
    ids = [r["id"] for r in await cur.fetchall()]
    deleted: list[Request] = []
    for rid in ids:
        r = await delete_request(rid)
        if r:
            deleted.append(r)
    return deleted


async def get_status_history(rid: int) -> list[tuple[str, int]]:
    """Таймлайн смен статуса заказа [(status, at), ...]. Для старых заказов (без записей)
    синтезируем по самому заказу: «new» при создании + текущий статус по updated_at."""
    cur = await _conn().execute(
        "SELECT status, at FROM req_status_history WHERE request_id = ? ORDER BY id", (rid,))
    rows = [(r["status"], r["at"]) for r in await cur.fetchall()]
    r = await get_request(rid)
    if not r:
        return rows
    synth = [("new", r.created_at)]
    if r.status != "new":
        synth.append((r.status, r.updated_at))
    # реальная история приоритетна; если её нет — синтетическая
    return rows if rows else synth


async def add_order(*, user_id: int, model_label: str, quantity: int, quality: str, strength: str,
                    plastic_id: int, printer: str, grams_total: float, minutes_total: float,
                    total_price: float) -> int:
    cur = await _conn().execute(
        "INSERT INTO orders(user_id, model_label, quantity, quality, strength, plastic_id, printer, "
        "grams_total, minutes_total, total_price, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, model_label, quantity, quality, strength, plastic_id, printer,
         grams_total, minutes_total, total_price, int(time.time())),
    )
    await _conn().commit()
    return cur.lastrowid


async def daily_revenue(days: int = 30) -> list[tuple[str, int, float]]:
    """[(дата 'YYYY-MM-DD', число заказов, выручка), ...] за N дней — для графика.
    Выручка — только по напечатанным заказам (status='printed')."""
    since = int(time.time()) - days * 86400
    cur = await _conn().execute(
        "SELECT date(created_at,'unixepoch','localtime') AS d, COUNT(*) AS n, "
        "COALESCE(SUM(CASE WHEN status='printed' THEN price END),0) AS s "
        "FROM requests WHERE created_at >= ? GROUP BY d ORDER BY d",
        (since,))
    return [(r["d"], r["n"], float(r["s"] or 0)) for r in await cur.fetchall()]


async def queue_load(live_progress: float | None = None) -> dict:
    """Текущая загрузка: число активных заказов (new/in_progress) и суммарное время их печати.
    Если задан live_progress (доля 0..1 текущей печати из телеметрии) — вычитаем уже отпечатанную
    часть идущей работы, чтобы ETA был ЖИВЫМ (idea #8). printing — инфо о текущей печати клиенту."""
    cur = await _conn().execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(minutes_total),0) AS m FROM requests "
        f"WHERE status IN ({_RES_PH})", _RESERVING_STATUSES)
    r = await cur.fetchone()
    minutes = float(r["m"] or 0)
    printing = None
    if live_progress is not None and 0.0 <= live_progress < 1.0:
        c2 = await _conn().execute(
            "SELECT COALESCE(minutes_total,0) AS m FROM requests WHERE status='in_progress' "
            "ORDER BY queue_pos IS NULL, queue_pos LIMIT 1")
        row = await c2.fetchone()
        jm = float(row["m"] or 0) if row else 0.0
        minutes = max(0.0, minutes - jm * live_progress)
        if jm > 0:
            printing = {"progress": round(live_progress * 100),
                        "remaining_min": int(round(jm * (1.0 - live_progress)))}
    return {"count": r["n"], "minutes": minutes, "printing": printing}
