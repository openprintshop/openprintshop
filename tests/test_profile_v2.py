"""Профиль v2: просмотренные достижения (ach_seen), лента XP, пороги уровней."""
from app import gamify


def test_build_profile_has_level_thresholds():
    s = {"orders": 7, "printed": 4, "spent": 92, "reviews": 1, "materials": 3, "referrals": 0}
    p = gamify.build_profile(s)                    # 695 XP → уровень 3 (350..750)
    assert p["xp_floor"] == 350 and p["xp_next"] == 750
    top = gamify.build_profile({"printed": 100, "orders": 100, "reviews": 50, "referrals": 50,
                                "materials": 10, "spent": 9999})
    assert top["xp_next"] is None                  # максимальный уровень — порога выше нет


def test_xp_log_entry_mapping():
    assert gamify.xp_log_entry("printed", 7) == ("Заказ #7 напечатан", 100)
    assert gamify.xp_log_entry("referral", 42) == ("Приглашён друг", 60)
    assert gamify.xp_log_entry("unknown", 1) is None
    # суммы ленты согласованы с весами xp_from_stats
    assert gamify.xp_log_entry("order", 1)[1] * 1 == gamify.xp_from_stats({"orders": 1})
    assert gamify.xp_log_entry("review", 1)[1] == gamify.xp_from_stats({"reviews": 1})


async def test_ach_seen_roundtrip(tmp_db):
    db = tmp_db
    assert await db.seen_achievements(5) == set()
    await db.mark_achievements_seen(5, ["first_order", "orders_5"])
    assert await db.seen_achievements(5) == {"first_order", "orders_5"}
    await db.mark_achievements_seen(5, ["first_order"])            # идемпотентно
    assert await db.seen_achievements(5) == {"first_order", "orders_5"}
    assert await db.seen_achievements(6) == set()                  # другой пользователь — пусто


async def test_xp_events_feed(tmp_db):
    db = tmp_db
    rid = await db.add_request(
        user_id=9, user_name="t", model_label="m", printer="p", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None)
    await db.update_request(rid, status="printed")
    await db.add_referral(referee_id=100, referrer_id=9)
    events = await db.xp_events(9, 10)
    kinds = {e["kind"] for e in events}
    assert {"order", "printed", "referral"} <= kinds
    assert all(e["at"] for e in events)
    # отменённые заказы не попадают в ленту
    rid2 = await db.add_request(
        user_id=9, user_name="t", model_label="m2", printer="p", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1, preview_path=None)
    await db.update_request(rid2, status="cancelled")
    refs = [e["ref"] for e in await db.xp_events(9, 10) if e["kind"] == "order"]
    assert rid2 not in refs
