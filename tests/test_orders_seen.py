"""Заказы самому себе не «непросмотренные» + кнопка «всё просмотрено» (OpenTodo 040525a2)."""
from app import config


async def _mk(db, uid):
    return await db.add_request(
        user_id=uid, user_name="x", model_label="M", printer="P", plastic_name="Pl",
        quantity=1, quality="Q", strength="S", grams_total=1, minutes_total=1, price=1,
        preview_path=None)


async def test_self_order_not_counted_unseen(tmp_db, monkeypatch):
    """Заказ мастера самому себе (клиент — админ) сразу «просмотрен» и не растит счётчик."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    await _mk(tmp_db, 111)                                    # обычный клиент → непросмотрен
    assert await tmp_db.count_unseen_requests() == 1
    await _mk(tmp_db, 999)                                    # заказ самому себе → уже просмотрен
    assert await tmp_db.count_unseen_requests() == 1


async def test_mark_all_seen_clears_counter(tmp_db, monkeypatch):
    """Кнопка «всё просмотрено» гасит счётчик непросмотренных и возвращает число затронутых."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    for uid in (1, 2, 3):
        await _mk(tmp_db, uid)
    assert await tmp_db.count_unseen_requests() == 3
    assert await tmp_db.mark_all_requests_seen() == 3
    assert await tmp_db.count_unseen_requests() == 0
    assert await tmp_db.mark_all_requests_seen() == 0        # повторно — уже нечего помечать
