"""CRM мастера: клиент как объект, задачи-напоминания, лента активности, промо и рефералка.

Всё, что мастерская знает и делает ПРО КЛИЕНТА (не про его конкретный заказ):

* КЛИЕНТ КАК ОБЪЕКТ (crm-roadmap.md, OpenTodo a8b591cf, вариант C): агрегаты LTV/кол-во/
  даты всегда считаются из requests тем же выражением, что и dashboard_stats (_CRM_AGG_SQL),
  а в таблице customers лежат ТОЛЬКО ручные поля мастера. Сегменты (visitor/new/repeat/
  vip/sleeping) вычисляются на лету по порогам из settings — «протухнуть» не могут.
  В списке есть и те, у кого заказов НЕТ ВОВСЕ: с ручной карточкой/скидкой (баг 2884565c)
  и просто открывшие бота — «посетители» из ленты активности (_crm_seen_map); сегмент
  «посетитель» даётся только по живым визитам, ручная карточка остаётся «новым».
* ЗАДАЧИ-НАПОМИНАНИЯ мастера (crm_tasks, этап 4) — ensure_birthday_tasks заводит их через
  add_crm_task, поэтому режутся только вместе.
* ЛЕНТА АКТИВНОСТИ пользователей (activity_log, OpenTodo f0602c66) — админ-экран
  «последняя активность»; log_event никогда не роняет основной поток.
* КЛИЕНТСКИЙ МАРКЕТИНГ: промокоды (promo_codes) и рефералка (referrals).
  award_referral_if_due — инцидентная зона: право на выплату захватывается ОДНИМ условным
  UPDATE `WHERE rewarded = 0` (до 2026-07-16 бонус платился дважды), поэтому блок выплат
  режется только целиком и только вместе с add_referral/get_referral/redeem_promo.

Выделено из app/db.py волной 6 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ: ни ленивых относительных импортов, ни global,
ни квалификаций. Единственный ленивый импорт в модуле — `import datetime as _dt` внутри
ensure_birthday_tasks: это stdlib, правилом волн он не задет (прецедент — `import time as _t`
в store/profile.add_idea).

РЁБРА НАРУЖУ (все однонаправленные, обратных импортов нет): models.REQ_STATUS_LABELS и
money.get_balance — карточка клиента; money.adjust_balance/family_of — выплата бонуса;
profile.count_referrals — счётчик приглашённых в карточке (долг волны 5 закрыт: раньше
customer_card звала его голым именем через фасад db, теперь импорт явный);
settings.get_settings — пороги сегментов и антиспам-окно рассылки; config.is_admin —
чтобы сам мастер не попал в свои же клиенты посетителем (_crm_own_uids).
"""
from __future__ import annotations

import json
import logging
import time

from .. import config
from .core import _abs, _conn, _rel
from .models import REQ_STATUS_LABELS
from .money import adjust_balance, family_of, get_balance
from .profile import count_referrals
from .settings import get_settings

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# CRM: клиент как объект (crm-roadmap.md, OpenTodo a8b591cf, вариант C).
# Агрегаты (LTV/кол-во/даты) всегда считаются из requests — тот же источник, что и
# dashboard_stats, поэтому цифры не разойдутся; в customers — только ручные поля.
# ════════════════════════════════════════════════════════════════════════════
CRM_SEGMENT_LABELS = {"visitor": "Посетитель", "new": "Новый", "repeat": "Повторный",
                      "vip": "VIP", "sleeping": "Спящий"}


def crm_segments(orders_count: int, total_spent: float, last_at: int, *,
                 vip_spent: float, sleep_days: float, now: float | None = None,
                 seen_at: int = 0) -> list[str]:
    """Вычисляемые сегменты клиента (этап 3): ничего не храним — «протухнуть» не могут.
    Пороги — из settings (crm_vip_spent / crm_sleep_days).

    Ступень жизненного цикла ровно одна: 1 заказ — «новый», 2+ — «повторный», а 0 заказов
    при живых визитах в бота (``seen_at``) — «посетитель». Карточка, заведённая мастером
    руками, и человек со скидкой из личной переписки визитов не имеют и остаются «новыми»,
    как было до появления сегмента: назвать их «посетителями» значило бы соврать."""
    now = int(now or time.time())
    cnt = orders_count or 0
    if cnt == 0:
        segs = ["visitor" if seen_at else "new"]
    else:
        segs = ["repeat" if cnt >= 2 else "new"]
    if vip_spent > 0 and (total_spent or 0) >= vip_spent:
        segs.append("vip")
    if last_at and sleep_days > 0 and (now - last_at) > sleep_days * 86400:
        segs.append("sleeping")
    return segs


# Базовый агрегат по клиенту из заказов; {where} — фильтр (все клиенты / один uid).
# Выручка — ТОЛЬКО по status='printed', как в dashboard_stats (единый источник правды).
_CRM_AGG_SQL = (
    "SELECT r.user_id AS uid, "
    "(SELECT user_name FROM requests r2 WHERE r2.user_id = r.user_id AND r2.user_name IS NOT NULL "
    " AND r2.user_name != '' ORDER BY r2.created_at DESC, r2.id DESC LIMIT 1) AS tg_name, "
    "COUNT(*) AS orders_count, "
    "SUM(CASE WHEN r.status = 'printed' THEN 1 ELSE 0 END) AS printed_count, "
    "COALESCE(SUM(CASE WHEN r.status = 'printed' THEN COALESCE(r.price, 0) ELSE 0 END), 0) AS total_spent, "
    "MIN(r.created_at) AS first_at, MAX(r.created_at) AS last_at "
    "FROM requests r WHERE {where} GROUP BY r.user_id")


def _crm_row(row, c_by_uid: dict, d_by_uid: dict, seen: dict | None = None) -> dict:
    uid = row["uid"]
    c = c_by_uid.get(uid) or {}
    sn = (seen or {}).get(uid) or {}
    printed = row["printed_count"] or 0
    spent = float(row["total_spent"] or 0)
    return {
        "uid": uid,
        # отображаемое имя: override из customers, иначе последнее непустое из заказов
        # (у посетителя без заказов в tg_name уже подставлено имя из ленты активности)
        "name": (c.get("name") or "").strip() or (row["tg_name"] or "") or f"id {uid}",
        "tg_name": row["tg_name"] or "",
        "orders": row["orders_count"] or 0, "printed": printed, "spent": round(spent, 2),
        "avg_check": round(spent / printed, 2) if printed else 0.0,
        "first_at": row["first_at"] or 0, "last_at": row["last_at"] or 0,
        # визиты в бота (activity_log) — отдельно от заказов: «последний заказ» ими не
        # подменяем, иначе у клиента без заказов колонка врала бы про заказ
        "first_seen_at": int(sn.get("first_at") or 0),
        "last_seen_at": int(sn.get("last_at") or 0),
        "discount_pct": d_by_uid.get(uid),
        "phone": c.get("phone") or "", "note": c.get("note") or "",
        "birthday": c.get("birthday") or "",     # «ДД.ММ», см. birthday_tasks()
        "no_broadcast": int(c.get("no_broadcast") or 0),
        "last_broadcast_at": int(c.get("last_broadcast_at") or 0),
    }


async def _crm_manual_maps() -> tuple[dict, dict, dict]:
    cur = await _conn().execute("SELECT * FROM customers")
    c_by_uid = {r["user_id"]: dict(r) for r in await cur.fetchall()}
    cur = await _conn().execute("SELECT user_id, discount_pct, name FROM user_discounts")
    rows = await cur.fetchall()
    d_by_uid = {r["user_id"]: float(r["discount_pct"]) for r in rows}
    d_names = {r["user_id"]: (r["name"] or "") for r in rows}
    return c_by_uid, d_by_uid, d_names


async def _crm_seen_map() -> dict[int, dict]:
    """Кто и когда заходил в бота — из ленты активности (activity_log). Даёт две вещи:
    «был/заходил» у обычного клиента и САМОГО клиента без единого заказа — посетителя,
    которого CRM раньше не видела вовсе (он жил только в ленте, а список строился из
    requests). Имя берём последнее непустое: /start кладёт туда full_name."""
    cur = await _conn().execute(
        "SELECT a.user_id AS uid, MIN(a.created_at) AS first_at, MAX(a.created_at) AS last_at, "
        "(SELECT user_name FROM activity_log a2 WHERE a2.user_id = a.user_id "
        " AND a2.user_name IS NOT NULL AND a2.user_name != '' "
        " ORDER BY a2.id DESC LIMIT 1) AS name "
        "FROM activity_log a WHERE a.user_id IS NOT NULL GROUP BY a.user_id")
    return {r["uid"]: {"first_at": r["first_at"] or 0, "last_at": r["last_at"] or 0,
                       "name": r["name"] or ""} for r in await cur.fetchall()}


async def _crm_own_uids() -> set[int]:
    """Приглашённые сотрудники (таблица staff). Корневых владельцев из .env тут нет — их
    ловит config.is_admin на месте вызова; берём и то и другое, потому что кэш ролей
    (config.STAFF_ROLES) наполняет roles.refresh(), а таблица верна всегда.
    В клиенты своих не заводим: мастер и оператор ходят в бота по работе. Заказ своего же
    (мастер печатает себе) прежним путём из requests в список попадёт — это не трогаем."""
    cur = await _conn().execute("SELECT user_id FROM staff")
    return {r["user_id"] for r in await cur.fetchall()}


def _crm_blank_row(uid: int, tg_name: str = "") -> dict:
    """Строка клиента без единого заказа: агрегаты по нулям, имя — какое знаем."""
    return {"uid": uid, "tg_name": tg_name or "", "orders_count": 0, "printed_count": 0,
            "total_spent": 0.0, "first_at": 0, "last_at": 0}


async def list_customers_crm(search: str = "", segment: str = "", sort: str = "last",
                             limit: int = 500, visitors: bool = True) -> list[dict]:
    """Список клиентов с агрегатами и сегментами (этапы 1–3). Фильтр по сегменту и поиск —
    в Python: объёмы крошечные, а пороги сегментов живут в settings.

    ``visitors`` — брать ли тех, кто в боте есть, а заказа ещё не сделал. По умолчанию да
    (CRM должна видеть всех, кто дошёл до бота), но ПУТИ РАССЫЛОК зовут с False: «Все» в
    рассылке — это по-прежнему заказывавшие, посетителям шлём только выбрав их сегмент."""
    cur = await _conn().execute(_CRM_AGG_SQL.format(where="r.user_id IS NOT NULL"))
    rows = [dict(r) for r in await cur.fetchall()]
    c_by_uid, d_by_uid, d_names = await _crm_manual_maps()
    seen = await _crm_seen_map()
    # клиенты БЕЗ заказов, но с персональной скидкой или ручной карточкой, — тоже клиенты
    # (раньше терялись: список строился только из requests — баг 2884565c)
    known = {row["uid"] for row in rows}
    for uid in (set(c_by_uid) | set(d_by_uid)) - known:
        rows.append(_crm_blank_row(uid, d_names.get(uid, "")))
    if visitors:
        # …и те, кто просто открыл бота/посчитал цену: раньше такой человек был виден
        # только в ленте событий и в клиентах не появлялся никогда
        skip = {row["uid"] for row in rows} | await _crm_own_uids()
        for uid, sn in seen.items():
            if uid not in skip and not config.is_admin(uid):
                rows.append(_crm_blank_row(uid, sn["name"]))
    s = await get_settings()
    vip, sleep = s.get("crm_vip_spent", 0.0), s.get("crm_sleep_days", 0.0)
    q = (search or "").strip().lower()
    out = []
    for row in rows:
        it = _crm_row(row, c_by_uid, d_by_uid, seen)
        it["segments"] = crm_segments(it["orders"], it["spent"], it["last_at"],
                                      vip_spent=vip, sleep_days=sleep,
                                      seen_at=it["last_seen_at"])
        if q and q not in it["name"].lower() and q not in it["tg_name"].lower() \
                and q not in str(it["uid"]):
            continue
        if segment and segment not in it["segments"]:
            continue
        out.append(it)
    # «Свежие» — по последнему касанию (заказ ИЛИ визит): иначе посетители с пустым
    # last_at навсегда осели бы в хвосте списка и мастер их не увидел
    key = {"ltv": (lambda x: x["spent"]), "orders": (lambda x: x["orders"])}.get(
        sort, lambda x: max(x["last_at"], x["last_seen_at"]))
    out.sort(key=key, reverse=True)
    return out[:limit]


async def customer_card(user_id: int) -> dict | None:
    """Карточка клиента (этап 1): агрегаты + ручные поля + скидка + баланс + любимый материал
    + последние заказы. None — про этого человека мы не знаем НИЧЕГО: ни заказов, ни ручной
    записи, ни единого визита в бота."""
    cur = await _conn().execute(_CRM_AGG_SQL.format(where="r.user_id = ?"), (user_id,))
    row = await cur.fetchone()
    c_by_uid, d_by_uid, d_names = await _crm_manual_maps()
    seen = await _crm_seen_map()
    if not row:
        if user_id not in c_by_uid and user_id not in d_by_uid and user_id not in seen:
            return None
        row = _crm_blank_row(user_id, d_names.get(user_id, "")
                             or (seen.get(user_id) or {}).get("name", ""))
    it = _crm_row(row, c_by_uid, d_by_uid, seen)
    s = await get_settings()
    it["segments"] = crm_segments(it["orders"], it["spent"], it["last_at"],
                                  vip_spent=s.get("crm_vip_spent", 0.0),
                                  sleep_days=s.get("crm_sleep_days", 0.0),
                                  seen_at=it["last_seen_at"])
    cur = await _conn().execute(
        "SELECT plastic_name, COUNT(*) AS n FROM requests WHERE user_id = ? "
        "AND plastic_name IS NOT NULL AND plastic_name != '' "
        "GROUP BY plastic_name ORDER BY n DESC LIMIT 1", (user_id,))
    fav = await cur.fetchone()
    it["fav_material"] = fav["plastic_name"] if fav else ""
    it["balance"] = await get_balance(user_id)
    it["referrals"] = await count_referrals(user_id)
    cur = await _conn().execute(
        "SELECT id, model_label, status, price, pending_price, created_at FROM requests "
        "WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user_id,))
    it["recent"] = [{"id": r["id"], "label": r["model_label"] or "", "status": r["status"],
                     "status_label": REQ_STATUS_LABELS.get(r["status"], r["status"]),
                     "price": r["pending_price"] if r["pending_price"] is not None else (r["price"] or 0),
                     "at": r["created_at"] or 0} for r in await cur.fetchall()]
    return it


async def get_customer(user_id: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM customers WHERE user_id = ?", (user_id,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def get_user_last_color(user_id: int) -> str:
    """Последний выбранный клиентом цвет (ux-paths-next.md, идея 4) — '' если не запомнен."""
    cur = await _conn().execute("SELECT last_color FROM customers WHERE user_id = ?", (user_id,))
    r = await cur.fetchone()
    return (r["last_color"] or "") if r else ""


async def set_user_last_color(user_id: int, color: str) -> None:
    """Запомнить последний выбранный цвет клиента (лениво создаёт строку клиента)."""
    color = (color or "").strip()
    if not color:
        return
    # как upsert_customer: сначала лениво создаём строку с created_at, потом пишем цвет —
    # так не задеваем created_at/остальные поля существующего клиента.
    await _conn().execute(
        "INSERT INTO customers(user_id, created_at) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO NOTHING", (user_id, int(time.time())))
    await _conn().execute("UPDATE customers SET last_color = ? WHERE user_id = ?", (color, user_id))
    await _conn().commit()


async def upsert_customer(user_id: int, **fields) -> None:
    """Ленивое создание строки клиента + запись ручных полей (вариант C)."""
    # birthday — «ДД.ММ», из него раз в сутки заводится задача «поздравить + промокод»
    allowed = {"name", "phone", "note", "no_broadcast", "last_broadcast_at", "birthday"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    await _conn().execute(
        "INSERT INTO customers(user_id, created_at) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO NOTHING", (user_id, int(time.time())))
    if sets:
        assignments = ", ".join(f"{k} = ?" for k in sets)
        await _conn().execute(f"UPDATE customers SET {assignments} WHERE user_id = ?",
                              (*sets.values(), user_id))
    await _conn().commit()


async def ensure_birthday_tasks(days_ahead: int = 3) -> int:
    """Завести задачи «поздравить + промокод» на ближайшие дни рождения.

    Идемпотентно: в заголовок вшит год, поэтому повторный прогон в тот же день (или после
    рестарта бота) дубля не создаёт, а на следующий год задача заведётся заново.
    Возвращает число созданных задач.
    """
    import datetime as _dt

    cur = await _conn().execute(
        "SELECT user_id, name, birthday FROM customers WHERE birthday != ''")
    rows = await cur.fetchall()
    if not rows:
        return 0

    today = _dt.date.today()
    # какие дни «ДД.ММ» попадают в окно [сегодня; сегодня+days_ahead]
    window = {}
    for i in range(days_ahead + 1):
        d = today + _dt.timedelta(days=i)
        window.setdefault(d.strftime("%d.%m"), d)

    created = 0
    for r in rows:
        day = window.get((r["birthday"] or "").strip())
        if not day:
            continue
        who = (r["name"] or "").strip() or f"id {r['user_id']}"
        # год в заголовке обязателен: на нём держится и антидубль, и то, что через год
        # задача заведётся заново
        title = f"Поздравить {who} с днём рождения ({day.strftime('%d.%m.%Y')}) + промокод"
        cur = await _conn().execute(
            "SELECT 1 FROM crm_tasks WHERE user_id = ? AND title = ?", (r["user_id"], title))
        if await cur.fetchone():
            continue
        # срок — сам день рождения, в 10 утра: напоминание придёт заранее, а не ночью
        due = int(_dt.datetime.combine(day, _dt.time(10, 0)).timestamp())
        await add_crm_task(title, due_at=due, user_id=r["user_id"])
        created += 1
    return created


async def crm_broadcast_targets(segment: str = "") -> dict:
    """Охват рассылки по сегменту (этап 5) с безопасными дефолтами: исключаем отписавшихся
    (no_broadcast) и тех, кому слали недавно (crm_broadcast_gap_days).

    Посетителей (в CRM они появились, заказов нет) в охват «Все» НЕ берём: рассылка
    необратима, и добавление их в клиенты не должно молча расширить аудиторию. Хочет мастер
    написать им — выбирает сегмент «Посетитель», тогда охват считается по ним."""
    clients = await list_customers_crm(segment=segment or "", visitors=(segment == "visitor"))
    s = await get_settings()
    gap = s.get("crm_broadcast_gap_days", 7.0) * 86400
    now = time.time()
    eligible, optout, recent = [], 0, 0
    for c in clients:
        if c["no_broadcast"]:
            optout += 1
        elif gap > 0 and c["last_broadcast_at"] and (now - c["last_broadcast_at"]) < gap:
            recent += 1
        else:
            eligible.append({"uid": c["uid"], "name": c["name"]})
    return {"total": len(clients), "eligible": eligible,
            "skipped_optout": optout, "skipped_recent": recent}


# ---- CRM этап 4: задачи-напоминания мастера ----
async def add_crm_task(title: str, due_at: int = 0, user_id: int | None = None,
                       request_id: int | None = None) -> int:
    cur = await _conn().execute(
        "INSERT INTO crm_tasks(user_id, request_id, title, due_at, created_at) VALUES(?,?,?,?,?)",
        (user_id, request_id, (title or "")[:200], int(due_at or 0), int(time.time())))
    await _conn().commit()
    return cur.lastrowid


async def list_crm_tasks(include_done: bool = False, user_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM crm_tasks"
    cond, args = [], []
    if not include_done:
        cond.append("done = 0")
    if user_id is not None:
        cond.append("user_id = ?")
        args.append(user_id)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    # открытые — ближайший срок первым (бессрочные в конце), сделанные — свежие первыми
    sql += " ORDER BY done, CASE WHEN due_at > 0 THEN due_at ELSE 9e15 END, id DESC LIMIT 200"
    cur = await _conn().execute(sql, args)
    return [dict(r) for r in await cur.fetchall()]


async def set_crm_task_done(tid: int, done: bool = True) -> bool:
    cur = await _conn().execute("UPDATE crm_tasks SET done = ? WHERE id = ?",
                                (1 if done else 0, int(tid)))
    await _conn().commit()
    return cur.rowcount > 0


async def delete_crm_task(tid: int) -> None:
    await _conn().execute("DELETE FROM crm_tasks WHERE id = ?", (int(tid),))
    await _conn().commit()


async def crm_tasks_due(now: float | None = None) -> list[dict]:
    """Открытые задачи с наступившим сроком, по которым ещё не напоминали (crmtasks.loop)."""
    cur = await _conn().execute(
        "SELECT * FROM crm_tasks WHERE done = 0 AND notified = 0 AND due_at > 0 AND due_at <= ? "
        "ORDER BY due_at LIMIT 20", (int(now or time.time()),))
    return [dict(r) for r in await cur.fetchall()]


async def mark_crm_task_notified(tid: int) -> None:
    await _conn().execute("UPDATE crm_tasks SET notified = 1 WHERE id = ?", (int(tid),))
    await _conn().commit()


async def crm_dashboard() -> dict:
    """Детализация воронки по периодам + причины отказов (этап 6, G6). «Напечатано за период» —
    по таймлайну статусов (req_status_history), а не по updated_at: точнее при поздних правках."""
    c = _conn()
    now = int(time.time())
    out: dict = {}
    for label, days in (("7d", 7), ("30d", 30)):
        t0 = now - days * 86400
        cur = await c.execute("SELECT COUNT(*) AS c FROM orders WHERE created_at >= ?", (t0,))
        calcs = (await cur.fetchone())["c"]
        cur = await c.execute("SELECT COUNT(*) AS c FROM requests WHERE created_at >= ?", (t0,))
        orders_n = (await cur.fetchone())["c"]
        cur = await c.execute(
            "SELECT COUNT(DISTINCT request_id) AS c FROM req_status_history "
            "WHERE status = 'printed' AND at >= ?", (t0,))
        printed = (await cur.fetchone())["c"]
        out[label] = {"calcs": calcs, "orders": orders_n, "printed": printed}
    cur = await c.execute(
        "SELECT reject_reason AS reason, COUNT(*) AS n FROM requests "
        "WHERE status = 'rejected' AND reject_reason IS NOT NULL AND reject_reason != '' "
        "GROUP BY reject_reason ORDER BY n DESC LIMIT 8")
    out["reject_reasons"] = [[r["reason"], r["n"]] for r in await cur.fetchall()]
    cur = await c.execute("SELECT COUNT(*) AS c FROM requests WHERE status = 'rejected'")
    out["rejected_total"] = (await cur.fetchone())["c"]
    return out


# merge_user_accounts удалён (2026-07-16): «семейный счёт через слияние» необратимо уничтожал
# историю второго клиента (инцидент @alex_demo). Замена — family_accounts (см. секцию
# «Семейный счёт» ниже): общий только баланс, всё остальное личное.


# ---- лента активности пользователей (для админ-просмотра, OpenTodo f0602c66) ----
async def log_event(user_id: int | None, action: str, detail: str = "", *,
                    user_name: str = "", calc_id: int | None = None) -> None:
    """Записать действие пользователя в ленту активности. Без уведомлений — только для
    админ-экрана «последняя активность». Ошибки глушим: лог не должен ронять основной поток.

    ``calc_id`` — ссылка на снимок сметы (calc_log), если событие про цену: по нему мастер
    открывает из ленты РОВНО ту смету, которую увидел клиент (add_calc_snapshot)."""
    try:
        if not user_name and user_id:
            # имя не передали (большинство веб-вызовов) — берём последнее известное из заказов,
            # чтобы в ленте было «@username», а не голый id (OpenTodo 7ec19dbb)
            cur = await _conn().execute(
                "SELECT user_name FROM requests WHERE user_id = ? AND user_name != '' "
                "ORDER BY id DESC LIMIT 1", (user_id,))
            r = await cur.fetchone()
            user_name = (r["user_name"] if r else "") or ""
            if not user_name:
                # заказов ещё нет — имя есть в его же прошлых событиях (/start кладёт туда
                # full_name). Без этого шага человек весь свой первый визит был «Гостем»,
                # хотя лента строкой выше поздоровалась с ним по имени
                cur = await _conn().execute(
                    "SELECT user_name FROM activity_log WHERE user_id = ? "
                    "AND user_name IS NOT NULL AND user_name != '' ORDER BY id DESC LIMIT 1",
                    (user_id,))
                r = await cur.fetchone()
                user_name = (r["user_name"] if r else "") or ""
        await _conn().execute(
            "INSERT INTO activity_log(user_id, user_name, action, detail, created_at, calc_id) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, (user_name or "")[:80], (action or "")[:40], (detail or "")[:200],
             int(time.time()), calc_id))
        # лёгкая обрезка истории, чтобы лента не росла бесконечно (держим последние ~5000)
        await _conn().execute(
            "DELETE FROM activity_log WHERE id <= (SELECT MAX(id) FROM activity_log) - 5000")
        await _conn().commit()
    except Exception:  # noqa: BLE001
        log.debug("log_event failed", exc_info=True)


async def list_activity(limit: int = 120, user_id: int | None = None) -> list[dict]:
    """Последние события, новые сверху. Можно отфильтровать по конкретному user_id.
    Пустые имена старых записей дозаполняем последним известным именем из заказов, а если
    заказов нет — из самой ленты (OpenTodo 7ec19dbb: в ленте был голый id; посетитель без
    заказов до этого показывался «Гостем»)."""
    if user_id:
        cur = await _conn().execute(
            "SELECT * FROM activity_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit))
    else:
        cur = await _conn().execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    missing = {r["user_id"] for r in rows if r["user_id"] and not r["user_name"]}
    names: dict = {}
    if missing:
        marks = ",".join("?" * len(missing))
        cur = await _conn().execute(
            f"SELECT user_id, user_name FROM requests WHERE user_id IN ({marks}) "
            "AND user_name != '' ORDER BY id", tuple(missing))
        for r in await cur.fetchall():
            names[r["user_id"]] = r["user_name"]   # последняя строка победит (свежее имя)
        rest = [u for u in missing if u not in names]
        if rest:                    # заказов нет вовсе — имя ищем в самой ленте
            marks = ",".join("?" * len(rest))
            cur = await _conn().execute(
                f"SELECT user_id, user_name FROM activity_log WHERE user_id IN ({marks}) "
                "AND user_name IS NOT NULL AND user_name != '' ORDER BY id", tuple(rest))
            for r in await cur.fetchall():
                names[r["user_id"]] = r["user_name"]
    return [{"id": r["id"], "user_id": r["user_id"],
             "name": r["user_name"] or names.get(r["user_id"], ""),
             "action": r["action"], "detail": r["detail"], "at": r["created_at"],
             # поздняя колонка — защитный паттерн: старая строка ленты снимка не знает
             "calc_id": (r["calc_id"] if "calc_id" in r.keys() else None)}
            for r in rows]


# ---- снимки смет, которые видел клиент (26.08) ----
# Лента событий хранит только строку текста, а сама смета живёт в памяти процесса (_models,
# web/registry.py) и умирает вместе с рестартом бота. Поэтому «рассчитал цену» в ленте было
# нечем открыть — а мастеру нужно ровно это: посмотреть, КАКУЮ цену человек увидел и из чего
# она сложилась. Снимок пишется в момент показа, ссылку на него несёт activity_log.calc_id.
_CALC_KEEP = 3000           # столько последних снимков держим (лента держит 5000 событий)
_CALC_PAYLOAD_MAX = 20000   # предохранитель на случай неожиданно жирного payload


async def add_calc_snapshot(user_id: int | None, *, action: str = "", source: str = "",
                            model_label: str = "", printer: str = "", plastic_name: str = "",
                            color: str = "", quality: str = "", strength: str = "",
                            qty: int = 1, grams_total: float = 0.0, minutes_total: float = 0.0,
                            total: float = 0.0, total_before: float = 0.0,
                            discount_pct: float = 0.0, approx: bool = False,
                            estimated: bool = False, preview_path: str = "",
                            payload: dict | None = None) -> int | None:
    """Сохранить смету, которую только что показали клиенту, и вернуть её id (или None).

    Ошибки глушим ровно как в log_event: снимок для мастера не имеет права уронить расчёт
    клиенту. ``source`` — каким путём получена цена (slice/cache/mw/estimate/link/requote):
    именно он объясняет мастеру, почему цифра могла разойтись с итоговой."""
    try:
        # JSON обрезать посимвольно нельзя — он перестанет разбираться, и снимок молча
        # опустеет при чтении. Не влез — оставляем только разбивку, не влезла и она — пусто.
        blob = json.dumps(payload or {}, ensure_ascii=False)
        if len(blob) > _CALC_PAYLOAD_MAX:
            blob = json.dumps({"lines": (payload or {}).get("lines") or []}, ensure_ascii=False)
        if len(blob) > _CALC_PAYLOAD_MAX:
            blob = "{}"
        cur = await _conn().execute(
            "INSERT INTO calc_log(user_id, created_at, action, source, model_label, printer, "
            "plastic_name, color, quality, strength, qty, grams_total, minutes_total, total, "
            "total_before, discount_pct, approx, estimated, preview_path, payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, int(time.time()), (action or "")[:40], (source or "")[:20],
             (model_label or "")[:200], (printer or "")[:80], (plastic_name or "")[:80],
             (color or "")[:80], (quality or "")[:80], (strength or "")[:80], int(qty or 1),
             float(grams_total or 0), float(minutes_total or 0), float(total or 0),
             float(total_before or 0), float(discount_pct or 0),
             int(bool(approx)), int(bool(estimated)), _rel(preview_path) or "", blob))
        cid = cur.lastrowid
        await _conn().execute(
            "DELETE FROM calc_log WHERE id <= (SELECT MAX(id) FROM calc_log) - ?", (_CALC_KEEP,))
        await _conn().commit()
        return cid
    except Exception:  # noqa: BLE001
        log.debug("add_calc_snapshot failed", exc_info=True)
        return None


async def mark_calc_refined(calc_id: int | None, *, total: float, grams: float,
                            minutes: float) -> None:
    """Дописать в снимок то, чем кончился ФОНОВЫЙ честный слайс (_refine_calc).

    Мгновенные ветки (MW-данные, кэш слайса, ≈оценка) показывают цену сразу и уточняют её
    в фоне. Разница между показанным и уточнённым — самый прямой ответ на вопрос «нормальную
    ли цену мы человеку назвали», поэтому она хранится рядом, а не затирает исходную."""
    if not calc_id:
        return
    try:
        await _conn().execute(
            "UPDATE calc_log SET refined_total = ?, refined_grams = ?, refined_minutes = ?, "
            "refined_at = ? WHERE id = ?",
            (float(total or 0), float(grams or 0), float(minutes or 0), int(time.time()),
             int(calc_id)))
        await _conn().commit()
    except Exception:  # noqa: BLE001
        log.debug("mark_calc_refined failed", exc_info=True)


async def get_calc_snapshot(calc_id: int) -> dict | None:
    """Снимок сметы по id. payload разворачиваем обратно в словарь, путь превью — в абсолютный
    (правило C2: в базе относительный). None — снимка нет: он мог быть вытеснен обрезкой."""
    cur = await _conn().execute("SELECT * FROM calc_log WHERE id = ?", (int(calc_id),))
    r = await cur.fetchone()
    if not r:
        return None
    it = dict(r)
    try:
        it["payload"] = json.loads(it["payload"]) if it["payload"] else {}
    except (ValueError, TypeError):
        it["payload"] = {}
    it["preview_path"] = _abs(it["preview_path"]) if it["preview_path"] else ""
    return it


# Каким путём получена показанная цена — мастеру это объясняет, почему цифра могла разойтись
# с итоговой (мгновенные ветки уточняются фоновым слайсом, см. mark_calc_refined).
CALC_SOURCE_LABELS = {
    "slice": "честный слайс",
    "cache": "кэш прошлого слайса",
    "mw": "данные MakerWorld",
    "estimate": "≈оценка по геометрии",
    "link": "быстрый расчёт по ссылке",
    "requote": "быстрая смена пластика",
}


async def calc_next_request(user_id: int | None, since_at: int, model_label: str = "") -> dict | None:
    """Ближайший заказ клиента ПОСЛЕ этого расчёта (в пределах суток) — по нему видно, согласился
    человек с названной ценой или ушёл. Связи «расчёт → заказ» в базе нет и не выдумываем:
    отдаём ровно то, что знаем, — первый заказ после расчёта и совпал ли в нём ярлык модели."""
    if not user_id or not since_at:
        return None
    since = int(since_at)
    cur = await _conn().execute(
        "SELECT id, model_label, status, price, pending_price, created_at FROM requests "
        "WHERE user_id = ? AND created_at >= ? AND created_at <= ? ORDER BY id LIMIT 1",
        (user_id, since, since + 86400))
    r = await cur.fetchone()
    if not r:
        return None
    return {"id": r["id"], "status": r["status"],
            "price": r["pending_price"] if r["pending_price"] is not None else (r["price"] or 0),
            "at": r["created_at"] or 0,
            "minutes_after": max(0, int(((r["created_at"] or 0) - since) // 60)),
            "same_model": (r["model_label"] or "").strip() == (model_label or "").strip()}


async def add_promo(code: str, discount_pct: float, expires_at: int = 0, max_uses: int = 0) -> None:
    code = code.strip().upper()
    keep = await _conn().execute("SELECT used_count FROM promo_codes WHERE code = ?", (code,))
    used = (await keep.fetchone())
    used_count = used["used_count"] if used else 0
    await _conn().execute(
        "INSERT OR REPLACE INTO promo_codes(code,discount_pct,expires_at,max_uses,used_count,created_at) "
        "VALUES(?,?,?,?,?,?)", (code, discount_pct, expires_at, max_uses, used_count, int(time.time())))
    await _conn().commit()


async def list_promos() -> list[dict]:
    cur = await _conn().execute("SELECT * FROM promo_codes ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def get_promo(code: str) -> dict | None:
    cur = await _conn().execute("SELECT * FROM promo_codes WHERE code = ?", ((code or "").strip().upper(),))
    r = await cur.fetchone()
    return dict(r) if r else None


async def delete_promo(code: str) -> None:
    await _conn().execute("DELETE FROM promo_codes WHERE code = ?", ((code or "").strip().upper(),))
    await _conn().commit()


async def validate_promo(code: str) -> float | None:
    """discount_pct, если код существует, не истёк и есть использования; иначе None."""
    r = await get_promo(code)
    if not r:
        return None
    now = int(time.time())
    if r["expires_at"] and now > r["expires_at"]:
        return None
    if r["max_uses"] and r["used_count"] >= r["max_uses"]:
        return None
    return float(r["discount_pct"])


async def add_referral(referee_id: int, referrer_id: int) -> bool:
    """Фиксирует «кого пригласил referrer». Возвращает False, если referee уже привязан."""
    if await get_referral(referee_id):
        return False
    await _conn().execute(
        "INSERT OR IGNORE INTO referrals(referee_id, referrer_id, rewarded, created_at) "
        "VALUES(?,?,0,?)", (referee_id, referrer_id, int(time.time())))
    await _conn().commit()
    return True


async def get_referral(referee_id: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM referrals WHERE referee_id = ?", (referee_id,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def award_referral_if_due(referee_id: int, amount: float) -> int | None:
    """Реферальный бонус (OpenTodo 7f257a33): когда у приглашённого появился ПЕРВЫЙ
    напечатанный И оплаченный заказ — начисляем amount НА СЧЁТ обоим (ему и пригласившему),
    ровно один раз. Возвращает id пригласившего (для уведомлений) или None (не время/уже было).
    Скидок в %% рефералка больше не даёт.

    Обе отметки ставятся ОДНИМ условным UPDATE (WHERE rewarded = 0) — это атомарный захват
    права на выплату: платит только тот вызов, чей UPDATE изменил строку. Раньше отметки шли
    двумя коммитами по разные стороны проверки семьи, а «положен ли бонус» решал отдельный
    SELECT в начале — два одновременных «заказ напечатан и оплачен» (например, отметка мастера
    и автопереход по оплате) успевали пройти проверку оба и платили бонус дважды.
    Проверка семьи переехала выше UPDATE-а именно ради этого объединения; она читающая,
    побочных эффектов у неё нет, и порядок «сначала решили, потом записали» ничего не меняет."""
    cur = await _conn().execute(
        "SELECT referrer_id FROM referrals WHERE referee_id = ? AND rewarded = 0", (referee_id,))
    r = await cur.fetchone()
    if not r:
        return None
    cur = await _conn().execute(
        "SELECT 1 FROM requests WHERE user_id = ? AND status IN ('printed','done') AND paid = 1 "
        "LIMIT 1", (referee_id,))
    if await cur.fetchone() is None:
        return None
    referrer = r["referrer_id"]
    # Внутри одной семьи рефералка не начисляется (2026-07-16): деньги и так общие, бонус
    # был бы самоначислением. rewarded=1 ставим всё равно — повторно не выстрелит;
    # bonus_paid остаётся 0 — бонус за открытие семейного счёта такой паре положен.
    fam_referee = await family_of(referee_id)
    same_family = fam_referee is not None and fam_referee == await family_of(referrer)
    # bonus_paid=1 — выплата реально состоялась: если эта пара потом откроет семейный
    # счёт, бонус за открытие уже не начислится (create_family).
    marks = "rewarded = 1" if same_family else "rewarded = 1, bonus_paid = 1"
    cur = await _conn().execute(
        f"UPDATE referrals SET {marks} WHERE referee_id = ? AND rewarded = 0", (referee_id,))
    await _conn().commit()
    if same_family or cur.rowcount != 1:
        return None
    await adjust_balance(referee_id, amount, "Реферальный бонус — первый заказ напечатан и оплачен")
    await adjust_balance(referrer, amount, "Реферальный бонус — приглашённый друг сделал заказ")
    return referrer


async def redeem_promo(code: str) -> None:
    await _conn().execute(
        "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = ?", ((code or "").strip().upper(),))
    await _conn().commit()
