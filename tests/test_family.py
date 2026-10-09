"""Семейный счёт (редизайн 2026-07-16, после инцидента со слиянием) + центр уведомлений.

Ключевые инварианты:
- общий ТОЛЬКО баланс: при создании личные остатки переезжают в общий + бонус за открытие;
- get_balance/adjust_balance члена семьи прозрачно работают с общим счётом,
  история family_tx хранит «чьё действие» и номер заказа;
- заказы/модели/скидки НЕ трогаются (в отличие от старого merge_user_accounts);
- рефералка внутри одной семьи не начисляется (и не «висит» невыстрелившей);
- достижение family в профиле обоих участников.
"""
import pytest

from app import config, gamify
from app import db as db_mod


async def _mk(db, uid, name, status="new", price=10.0, paid=False):
    rid = await db.add_request(
        user_id=uid, user_name=name, model_label=f"Модель {uid}", printer="P",
        plastic_name="PLA", quantity=1, quality="0.2", strength="норм",
        grams_total=10, minutes_total=30, price=price, preview_path=None)
    if status != "new":
        await db.update_request(rid, status=status)
    if paid:
        await db.update_request(rid, paid=1)
    return rid


async def test_create_family_moves_balances_and_bonus(tmp_db):
    db = tmp_db
    await _mk(db, 1, "@anna")
    await _mk(db, 2, "@boris")
    await db.adjust_balance(1, 40.6, "Пополнение мастером")
    fam = await db.create_family([(1, "@anna"), (2, "@boris")], opening_bonus=5.0)
    assert fam["balance"] == 45.6
    assert {m["user_id"] for m in fam["members"]} == {1, 2}
    # личных остатков больше нет, get_balance обоих смотрит на общий счёт
    assert await db.get_personal_balance(1) == 0.0
    assert await db.get_balance(1) == 45.6 and await db.get_balance(2) == 45.6
    txs = await db.list_family_tx(fam["id"])
    reasons = [t["reason"] for t in txs]
    assert "Бонус за открытие семейного счёта" in reasons
    move = [t for t in txs if t["reason"] == "Перенос личного баланса"][0]
    assert move["user_id"] == 1 and move["user_name"] == "@anna" and move["amount"] == 40.6


async def test_family_leaves_personal_stuff_alone(tmp_db):
    db = tmp_db
    rid = await _mk(db, 1, "@anna")
    await _mk(db, 2, "@boris")
    await db.set_user_discount(1, 20.0, "@anna")
    await db.create_family([(1, "@anna"), (2, "@boris")])
    # заказы и скидки остались личными — семейный счёт общий только по деньгам
    assert (await db.get_request(rid)).user_id == 1
    assert (1, 20.0, "@anna") in await db.list_user_discounts()


async def test_adjust_balance_routes_to_family_with_attribution(tmp_db):
    db = tmp_db
    await _mk(db, 1, "@anna")
    await _mk(db, 2, "@boris")
    fam = await db.create_family([(1, "@anna"), (2, "@boris")], opening_bonus=5.0)
    new = await db.adjust_balance(2, -3.0, "Оплата заказа #7")
    assert new == 2.0 and await db.get_balance(1) == 2.0
    tx = (await db.list_family_tx(fam["id"]))[0]
    # подпись «чьё действие» + номер заказа распознан из reason
    assert tx["user_id"] == 2 and tx["user_name"] == "@boris" and tx["request_id"] == 7
    # request_id, переданный явно, важнее распознанного
    await db.adjust_balance(1, -1.0, "Оплата заказа #7", request_id=9)
    assert (await db.list_family_tx(fam["id"]))[0]["request_id"] == 9
    # личная история не растёт — движения идут по общему счёту
    assert await db.list_balance_tx(1) == [] and await db.list_balance_tx(2) == []


async def test_family_membership_rules(tmp_db):
    db = tmp_db
    for uid, name in ((1, "@a"), (2, "@b"), (3, "@c")):
        await _mk(db, uid, name)
    fam = await db.create_family([(1, "@a"), (2, "@b")])
    with pytest.raises(ValueError):
        await db.create_family([(1, "@a"), (3, "@c")])   # 1 уже в семье
    with pytest.raises(ValueError):
        await db.create_family([(3, "@c"), (3, "@c")])   # после дедупа остался один
    # третий участник (не «только два юзера»): личный остаток тоже переезжает
    await db.adjust_balance(3, 7.0, "Пополнение мастером")
    fam2 = await db.add_family_member(fam["id"], 3, "@c")
    assert len(fam2["members"]) == 3 and fam2["balance"] == 7.0
    with pytest.raises(ValueError):
        await db.add_family_member(fam["id"], 3, "@c")
    with pytest.raises(ValueError):
        await db.add_family_member(999, 3, "@c")


async def test_referral_suppressed_inside_family(tmp_db):
    db = tmp_db
    await _mk(db, 2, "@boris")
    # рефералка привязывается ДО первого заказа приглашённой
    assert await db.add_referral(referee_id=1, referrer_id=2)
    rid = await _mk(db, 1, "@anna", price=12.0)
    await db.create_family([(1, "@anna"), (2, "@boris")], opening_bonus=5.0)
    # создание семьи гасит рефералку между участниками навсегда; выплаты не было —
    # bonus_paid остаётся 0 (бонус за открытие такой паре положен, и он уже начислен)
    ref = await db.get_referral(1)
    assert ref["rewarded"] == 1 and ref["bonus_paid"] == 0
    await db.update_request(rid, status="printed", paid=1)
    assert await db.award_referral_if_due(1, 5.0) is None
    fam = await db.get_family(1)
    assert fam["balance"] == 5.0   # только бонус за открытие, без реферальных +5/+5
    assert all("Реферальный" not in t["reason"] for t in await db.list_family_tx(fam["id"]))


async def test_referral_outside_family_still_works(tmp_db):
    db = tmp_db
    assert await db.add_referral(referee_id=1, referrer_id=2)
    rid = await _mk(db, 1, "@anna", price=12.0, status="printed", paid=True)
    await _mk(db, 2, "@boris")
    assert rid and await db.award_referral_if_due(1, 5.0) == 2
    assert await db.get_balance(1) == 5.0 and await db.get_balance(2) == 5.0
    assert (await db.get_referral(1))["bonus_paid"] == 1


async def test_no_opening_bonus_after_paid_referral(tmp_db):
    # рефералка между будущими членами семьи уже ВЫПЛАЧЕНА → бонус за открытие не капает
    db = tmp_db
    await db.add_referral(referee_id=1, referrer_id=2)
    await _mk(db, 1, "@anna", price=12.0, status="printed", paid=True)
    await _mk(db, 2, "@boris")
    await db.award_referral_if_due(1, 5.0)               # оба получили по +5
    fam = await db.create_family([(1, "@anna"), (2, "@boris")], opening_bonus=5.0)
    # общий баланс = только перенесённые личные +5/+5, без бонуса за открытие
    assert fam["balance"] == 10.0
    assert all(t["reason"] != "Бонус за открытие семейного счёта"
               for t in await db.list_family_tx(fam["id"]))


async def test_opening_bonus_when_referral_paid_to_outsider(tmp_db):
    # выплаченная рефералка с ТРЕТЬИМ человеком (не членом семьи) бонусу не мешает
    db = tmp_db
    await db.add_referral(referee_id=3, referrer_id=1)   # 1 пригласил постороннего 3
    await _mk(db, 3, "@third", price=12.0, status="printed", paid=True)
    await _mk(db, 1, "@anna")
    await _mk(db, 2, "@boris")
    await db.award_referral_if_due(3, 5.0)
    fam = await db.create_family([(1, "@anna"), (2, "@boris")], opening_bonus=5.0)
    # +5 личный бонус анны переехал в общий + 5 бонус за открытие
    assert fam["balance"] == 10.0
    assert any(t["reason"] == "Бонус за открытие семейного счёта"
               for t in await db.list_family_tx(fam["id"]))


async def test_family_achievement_in_profile(tmp_db):
    db = tmp_db
    await _mk(db, 1, "@anna")
    await _mk(db, 2, "@boris")
    stats = await db.user_profile_stats(1)
    assert stats["family"] == 0
    prof = gamify.build_profile(stats)
    assert not [a for a in prof["achievements"] if a["key"] == "family"][0]["unlocked"]
    await db.create_family([(1, "@anna"), (2, "@boris")])
    for uid in (1, 2):
        prof = gamify.build_profile(await db.user_profile_stats(uid))
        assert [a for a in prof["achievements"] if a["key"] == "family"][0]["unlocked"]


async def test_remove_member_and_dissolve(tmp_db):
    db = tmp_db
    for uid, name in ((1, "@a"), (2, "@b"), (3, "@c")):
        await _mk(db, uid, name)
    await db.adjust_balance(1, 30.0, "Пополнение мастером")
    fam = await db.create_family([(1, "@a"), (2, "@b"), (3, "@c")], opening_bonus=5.0)
    assert fam["balance"] == 35.0
    with pytest.raises(ValueError):
        await db.remove_family_member(fam["id"], 99)          # не участник
    # исключение при >2 участниках: деньги остаются семье, исключённый уходит с нулём
    left = await db.remove_family_member(fam["id"], 2)
    assert left and len(left["members"]) == 2 and left["balance"] == 35.0
    assert await db.family_of(2) is None and await db.get_balance(2) == 0.0
    # исключение до одного оставшегося: роспуск, остаток — на личный баланс последнего
    assert await db.remove_family_member(fam["id"], 1) is None
    assert await db.family_of(3) is None and await db.get_family(3) is None
    assert await db.get_personal_balance(3) == 35.0
    assert any(t["reason"] == "Возврат с семейного счёта"
               for t in await db.list_balance_tx(3))


async def test_bonus_granted_flag(tmp_db):
    db = tmp_db
    await db.add_referral(referee_id=1, referrer_id=2)
    await _mk(db, 1, "@anna", price=12.0, status="printed", paid=True)
    await _mk(db, 2, "@boris")
    await _mk(db, 3, "@third")
    await db.award_referral_if_due(1, 5.0)
    fam = await db.create_family([(1, "@anna"), (2, "@boris")], opening_bonus=5.0)
    assert fam["bonus_granted"] is False                      # рефералка уже выплачена
    fam2 = await db.create_family([(3, "@third"), (4, "@dana")], opening_bonus=5.0)
    assert fam2["bonus_granted"] is True


async def test_publish_is_idempotent(tmp_db):
    db = tmp_db
    nid = await db.add_admin_notification("release_note", title="Что нового", body="Т")
    assert await db.try_mark_published(nid) is True            # первый тап публикует
    assert await db.try_mark_published(nid) is False           # повторный — отказ, без дублей
    assert (await db.get_admin_notification(nid))["status"] == "published"


async def test_admin_notifications_lifecycle(tmp_db):
    db = tmp_db
    nid = await db.add_admin_notification(
        "family_request", title="Заявка", body="От @anna",
        payload={"from_id": 1, "from_name": "@anna", "partner": "@boris"})
    items = await db.list_admin_notifications()
    assert items[0]["id"] == nid and items[0]["payload"]["partner"] == "@boris"
    assert await db.count_new_admin_notifications() == 1
    assert await db.update_admin_notification(nid, status="done")
    assert await db.list_admin_notifications() == []             # new-фильтр
    assert await db.count_new_admin_notifications() == 0
    assert (await db.list_admin_notifications(include_done=True))[0]["status"] == "done"
    # release note: правка текста; удаление возвращает путь картинки для подчистки файла
    rn = await db.add_admin_notification("release_note", title="Что нового", body="Текст")
    await db.update_admin_notification(rn, title="Новое", body="Текст 2", image_path="x.jpg")
    got = await db.get_admin_notification(rn)
    # C2: в БД путь лежит ОТНОСИТЕЛЬНО DATA_DIR, наружу store отдаёт абсолютный — значение
    # уходит в unlink и в FSInputFile, и относительным там быть не может.
    abs_img = str(config.DATA_DIR / "x.jpg")
    assert got["title"] == "Новое" and got["image_path"] == abs_img
    assert await db.delete_admin_notification(rn) == abs_img
    assert await db.get_admin_notification(rn) is None


async def test_seed_migration_skips_foreign_db(tmp_db):
    # гард v4: на БД без Татьяны/Алексея заявка не сеется, user_version проставлен
    db = tmp_db
    assert await db.count_new_admin_notifications() == 0
    cur = await db_mod._conn().execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] >= 4
