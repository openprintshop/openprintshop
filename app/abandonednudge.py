"""Догон брошенных расчётов (A5): клиент посчитал модель, но не оформил заказ → ОДНО
напоминание в Telegram с кнопкой «Открыть приложение» (Мои модели).

Пара к maintenance.cart_nudge_loop (брошенная корзина, idea #10): там ловим тех, кто добавил
модель в корзину и не оформил; здесь — тех, кто даже до корзины не дошёл, а просто сделал
расчёт (строка в orders-логе) и пропал. Клиентов с активной корзиной НЕ трогаем — их ведёт
cart_nudge_loop, иначе получится два пинга (см. db.abandoned_calcs, guard по user_cart).

Фича по умолчанию ВЫКЛЮЧЕНА (settings nudge_abandoned_enabled=0) — владелец включит из админки,
посмотрев на счётчик кандидатов. Ночью (22:00–09:00 по серверу, Минск) молчим — переносим на
разрешённое окно (просто пропускаем тик; расчёты не «протухнут», пока в пределах max_days).
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import config, db, keyboards, notify, texts

log = logging.getLogger(__name__)

POLL_SEC = 1800   # раз в полчаса — как cart_nudge, реагировать мгновенно незачем


def _in_quiet_hours(hour: int) -> bool:
    """Тихие часы 22:00–09:00 по локальному времени сервера (машина в Минске). Хардкод для v1,
    чтобы не плодить настройки; time.localtime — как везде в кодовой базе (без tz-библиотек)."""
    return hour >= 22 or hour < 9


async def _tick(bot) -> None:
    s = await db.get_settings()
    if not s.get("nudge_abandoned_enabled"):
        return                                # фича выключена — тихо выходим
    if _in_quiet_hours(time.localtime().tm_hour):
        return                                # ночь — не будим, догоним в разрешённое окно
    min_age_s = int((s.get("nudge_abandoned_hours") or 24) * 3600)
    max_age_s = int((s.get("nudge_abandoned_max_days") or 7) * 86400)
    cur = config.CURRENCY
    try:
        rows = await db.abandoned_calcs(min_age_s=min_age_s, max_age_s=max_age_s)
    except Exception:  # noqa: BLE001
        log.debug("abandoned_calcs query failed", exc_info=True)
        return
    for r in rows:
        try:
            txt = texts.abandoned_calc_nudge(r["model_label"], r["total_price"], cur)
            kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="models")
            await notify.notify_user(bot, r["user_id"], txt, reply_markup=kb)
            # Помечаем ВСЕГДА, даже когда notify_user вернул False (клиент не запускал бота):
            # иначе на каждом тике повторная попытка — ретрай-хаммер. Как в cart_nudge_loop.
            await db.mark_calc_nudged(r["user_id"])
        except Exception:  # noqa: BLE001
            log.debug("abandoned nudge failed for %s", r.get("user_id"), exc_info=True)


async def loop(bot) -> None:
    """Фоновый цикл; инертен, пока фича выключена или нет кандидатов."""
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await _tick(bot)
        except Exception:  # noqa: BLE001
            log.debug("abandonednudge tick failed", exc_info=True)
