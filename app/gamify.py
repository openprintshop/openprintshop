"""Геймификация: уровни и достижения пользователя (OpenTodo 7a5be39c).

Чистая логика без БД: на вход — статистика клиента (db.user_profile_stats), на выход —
профиль для мини-аппы. Уровень и XP ВЫВОДЯТСЯ из реальных данных (заказы/печати/отзывы/
рефералы/материалы), поэтому ничего отдельно копить и мигрировать не нужно — профиль всегда
согласован с фактической историей.
"""
from __future__ import annotations

# Пороги уровней: (минимальный XP, название). XP считается из активности (xp_from_stats).
LEVELS = [
    (0, "Новичок"), (120, "Любитель"), (350, "Энтузиаст"),
    (750, "Мастер печати"), (1500, "Гуру"), (3000, "Легенда"),
]

# Достижения: key, заголовок, описание, иконка (имя из icons.js), метрика и порог.
ACHIEVEMENTS = [
    {"key": "first_order", "title": "Первый заказ", "desc": "Оформить первый заказ", "icon": "sparkle", "metric": "orders", "need": 1},
    {"key": "orders_5", "title": "Постоянный клиент", "desc": "5 заказов", "icon": "receipt", "metric": "orders", "need": 5},
    {"key": "orders_20", "title": "Завсегдатай", "desc": "20 заказов", "icon": "star", "metric": "orders", "need": 20},
    {"key": "printed_10", "title": "Десятка в пластике", "desc": "10 напечатанных заказов", "icon": "check", "metric": "printed", "need": 10},
    {"key": "materials_3", "title": "Материаловед", "desc": "Попробовать 3 пластика", "icon": "spool", "metric": "materials", "need": 3},
    {"key": "reviewer", "title": "Критик", "desc": "Оставить отзыв", "icon": "chat", "metric": "reviews", "need": 1},
    {"key": "referral", "title": "Амбассадор", "desc": "Пригласить друга", "icon": "gift", "metric": "referrals", "need": 1},
    {"key": "big_spender", "title": "Меценат", "desc": "Заказов на 100", "icon": "coin", "metric": "spent", "need": 100},
    {"key": "family", "title": "Семейный счёт", "desc": "Открыть общий счёт с близкими", "icon": "heart", "metric": "family", "need": 1},
]


def xp_from_stats(s: dict) -> int:
    """XP из активности: печать ценится дороже оформления; отзывы и приглашения поощряются."""
    return int(s.get("printed", 0) * 100 + s.get("orders", 0) * 30
               + s.get("reviews", 0) * 25 + s.get("referrals", 0) * 60
               + s.get("materials", 0) * 20)


# Подписи и суммы для ленты «Как начисляется XP» (профиль v2). Суммы согласованы
# с весами xp_from_stats — лента объясняет ровно те начисления, из которых растёт XP.
XP_EVENTS = {
    "printed": ("Заказ #{ref} напечатан", 100),
    "order": ("Заказ #{ref} оформлен", 30),
    "review": ("Отзыв о заказе #{ref}", 25),
    "referral": ("Приглашён друг", 60),
}


def xp_log_entry(kind: str, ref) -> tuple[str, int] | None:
    """(человекочитаемая причина, XP) для события из db.xp_events; None — незнакомый тип."""
    t = XP_EVENTS.get(kind)
    if not t:
        return None
    return t[0].format(ref=ref), t[1]


def _level(xp: int):
    idx = 0
    for i, (need, _name) in enumerate(LEVELS):
        if xp >= need:
            idx = i
    cur_need, name = LEVELS[idx]
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    return idx, name, cur_need, nxt


def build_profile(stats: dict) -> dict:
    """Полный профиль для UI: уровень, прогресс до следующего, сетка достижений."""
    xp = xp_from_stats(stats)
    idx, name, cur_need, nxt = _level(xp)
    if nxt:
        span = nxt[0] - cur_need
        progress = round((xp - cur_need) / span * 100) if span else 100
        to_next = nxt[0] - xp
    else:
        progress, to_next = 100, 0
    ach, unlocked = [], 0
    for a in ACHIEVEMENTS:
        have = stats.get(a["metric"], 0) or 0
        done = have >= a["need"]
        unlocked += 1 if done else 0
        ach.append({"key": a["key"], "title": a["title"], "desc": a["desc"], "icon": a["icon"],
                    "unlocked": done, "have": int(have), "need": a["need"]})
    return {"xp": xp, "level": idx + 1, "level_title": name,
            "next_title": nxt[1] if nxt else None, "to_next_xp": max(0, to_next),
            "progress_pct": max(0, min(100, progress)),
            # профиль v2: подписи у прогресс-бара (порог текущего и следующего уровня)
            "xp_floor": cur_need, "xp_next": nxt[0] if nxt else None,
            "achievements": ach, "unlocked": unlocked, "total": len(ACHIEVEMENTS),
            "stats": stats}
