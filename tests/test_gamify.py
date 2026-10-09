"""Геймификация: уровни и достижения (OpenTodo 7a5be39c)."""
from app import gamify


def test_xp_and_level_progression():
    assert gamify.xp_from_stats({}) == 0
    p0 = gamify.build_profile({"orders": 0, "printed": 0, "spent": 0, "reviews": 0,
                               "materials": 0, "referrals": 0})
    assert p0["level"] == 1 and p0["level_title"] == "Новичок" and p0["unlocked"] == 0
    # 4 печати(400)+7 заказов(210)+1 отзыв(25)+3 материала(60) = 695 XP → уровень 3 (≥350)
    s = {"orders": 7, "printed": 4, "spent": 92, "reviews": 1, "materials": 3, "referrals": 0}
    p = gamify.build_profile(s)
    assert p["xp"] == 695 and p["level"] == 3 and p["level_title"] == "Энтузиаст"
    assert 0 <= p["progress_pct"] <= 100 and p["to_next_xp"] == 750 - 695
    keys = {a["key"]: a for a in p["achievements"]}
    assert keys["first_order"]["unlocked"] and keys["orders_5"]["unlocked"]
    assert not keys["orders_20"]["unlocked"] and keys["materials_3"]["unlocked"]
    assert not keys["big_spender"]["unlocked"]          # потрачено 92 < 100


def test_max_level_caps_progress():
    p = gamify.build_profile({"printed": 100, "orders": 100, "reviews": 50, "referrals": 50,
                              "materials": 10, "spent": 9999})
    assert p["level_title"] == "Легенда" and p["next_title"] is None and p["progress_pct"] == 100
