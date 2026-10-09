"""Напоминания о задачах мастера (CRM этап 4, crm-roadmap.md, OpenTodo a8b591cf).

Фоновый цикл в духе printwatch/maintenance: раз в POLL_SEC проверяет crm_tasks с наступившим
сроком и шлёт мастеру напоминание с кнопкой «Сделано». Антидубль — флаг notified в строке
задачи (повторно не напоминаем, пока задачу не переоткроют).
"""
from __future__ import annotations

import asyncio
import logging

from . import db, keyboards, notify, texts

log = logging.getLogger(__name__)

POLL_SEC = 600


async def _tick(bot) -> None:
    # Дни рождения клиентов (настольная админка): за 3 дня заводим задачу «поздравить +
    # промокод». Идемпотентно — год вшит в заголовок, поэтому прогон каждые 10 минут
    # дублей не плодит.
    try:
        await db.ensure_birthday_tasks()
    except Exception:  # noqa: BLE001
        log.debug("birthday tasks failed", exc_info=True)

    for t in await db.crm_tasks_due():
        try:
            # Задачи мастера оператору уже открыты ручкой /api/admin/tasks — пусть и напоминание
            # доходит. Денег в тексте нет (texts.crm_task_reminder — тема, срок и id клиента).
            await notify.notify_admins(bot, texts.crm_task_reminder(t), audience=notify.ALL_STAFF,
                                       reply_markup=keyboards.crm_task_done(t["id"]))
            await db.mark_crm_task_notified(t["id"])
        except Exception:  # noqa: BLE001
            log.debug("crm task reminder failed", exc_info=True)


async def loop(bot) -> None:
    """Фоновый цикл; инертен, пока нет задач с наступившим сроком."""
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await _tick(bot)
        except Exception:  # noqa: BLE001
            log.debug("crmtasks tick failed", exc_info=True)
