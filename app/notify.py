"""Уведомления администраторам (о новых расчётах, а также алерты о сбоях)."""
from __future__ import annotations

import html
import logging
import time
from pathlib import Path

from aiogram.types import FSInputFile

from . import config, deskevents

log = logging.getLogger(__name__)

_bot = None                 # ставится из run.py — чтобы alert() работал без передачи bot
_last_alert: dict = {}      # ключ -> время последней отправки (троттлинг от спама)


def set_bot(bot) -> None:
    global _bot
    _bot = bot


OWNERS = "owners"       # только владельцы (по умолчанию — безопасная сторона)
ALL_STAFF = "all"       # владельцы + операторы


def recipients(audience: str = OWNERS) -> list[int]:
    """Кому слать. OWNERS — владельцы (корневые из .env + приглашённые с ролью owner),
    ALL_STAFF — вся команда.

    Раньше адресата не было вовсе: notify_admins всегда шёл по config.ADMIN_IDS, то есть
    ТОЛЬКО по корневым владельцам из .env. Приглашённый оператор не получал в Telegram
    НИЧЕГО — ни нового заказа, ни отвалившегося принтера; он узнавал о событиях, только если
    у него была открыта настольная админка. Мастерская нанимает оператора именно затем, чтобы
    владельца перестали дёргать, — и как раз этого сборка не давала.

    Почему по умолчанию OWNERS: через этот же канал идут деньги — оплаты, балансы, выручка,
    итоги корзины. Роль оператора построена на том, чтобы он их не видел (roles.ROLE_CAPS
    даёт ему только заказы и принтер). Забытый вызов обязан ошибаться в сторону молчания,
    а не утечки выручки."""
    if (audience or OWNERS) != ALL_STAFF:
        return [uid for uid in config.staff_ids() if config.is_owner(uid)]
    return config.staff_ids()


async def alert(text: str, *, key: str | None = None, throttle_sec: int = 300,
                audience: str = OWNERS) -> None:
    """Алерт админам о сбое (из любого модуля — bot берётся из set_bot). Троттлинг по key
    (или по началу текста): одинаковая ошибка чаще раза в throttle_sec не шлётся.

    Текст ЭКРАНИРУЕТСЯ: бот отправляет с parse_mode=HTML, а сюда приезжает то, что мы не
    писали руками — str(исключения) («'<' not supported between…») и клиентский текст из
    /api/jserr. Без escape Telegram отвечает 400 «can't parse entities», и алерт молча
    теряется в notify_admins, а через мини-апп владельцу можно было бы подсунуть
    кликабельную ссылку. Намеренной разметки ни один вызов alert() не передаёт; зеркало в
    настольную админку не страдает — deskevents._plain() делает html.unescape()."""
    if _bot is None:
        return
    k = key or text[:80]
    now = time.time()
    if now - _last_alert.get(k, 0) < throttle_sec:
        return
    _last_alert[k] = now
    try:
        await notify_admins(_bot, "⚠️ " + html.escape(text), kind="alert", audience=audience)
    except Exception:  # noqa: BLE001
        log.debug("alert failed", exc_info=True)


def _photo(photo):
    """Путь к файлу → FSInputFile; всё остальное (file_id, готовый FSInputFile) — как есть.

    Вызывающие честно передавали сюда и то, и другое. Для aiogram голая СТРОКА — это file_id
    или ссылка, поэтому путь вроде `data/collection_photos/chat_12.jpg` уезжал в Telegram как
    идентификатор, тот отвечал 400, и `except` вокруг отправки писал строчку в отладочный лог.
    Так молча терялись вложения ЧАТА заказа в обе стороны (webapp: `photo=photo_path or None`):
    в приложении фото было, в Telegram не приходило ничего. Чинится здесь, а не по местам
    вызова, — иначе следующий такой вызов повторит ошибку ровно так же тихо."""
    if isinstance(photo, (str, Path)):
        try:
            p = config.abs_data(photo)      # C2: из БД путь приходит относительным
            if p.exists():
                return FSInputFile(str(p))
        except OSError:      # слишком длинная/битая строка — значит это не путь, а file_id
            pass
    return photo


async def _track(on_sent, chat_id: int, msg) -> None:
    """Сообщить вызывающему, каким message_id уехало отправленное (если тому это нужно).

    Нужно переписке заказа: по паре (чат, сообщение) находится строка чата, когда клиент
    ставит на сообщение бота реакцию — в апдейте message_reaction нет ни текста, ни номера
    заказа, только эти два числа. Сбой учёта не должен ронять саму отправку: сообщение уже
    доставлено, и «не записали связь» — потеря реакции, а не потеря сообщения."""
    if on_sent is None or msg is None:
        return
    mid = getattr(msg, "message_id", None)
    if not mid:
        return
    try:
        await on_sent(int(chat_id), int(mid))
    except Exception:  # noqa: BLE001
        log.debug("on_sent failed", exc_info=True)


async def notify_admins(bot, text: str, *, exclude: int | None = None, photo=None,
                        reply_markup=None, kind: str = "info",
                        audience: str = OWNERS, operator_text: str | None = None,
                        on_sent=None) -> None:
    """Разослать сообщение команде (кроме exclude). reply_markup — например кнопка
    «Открыть в приложении» с адресацией на заказ. Ошибки не критичны.

    audience — кому (см. recipients): по умолчанию владельцам. Это НЕ ровно прежнее поведение:
    раньше рассылка шла по config.ADMIN_IDS, и приглашённый владелец (роль owner в staff) не
    получал ничего — теперь получает. Оператор по умолчанию по-прежнему не получает ничего.
    operator_text — что увидит ОПЕРАТОР вместо основного текста. Нужен там, где событие
    операционное («новый заказ»), а текст денежный: оператору полагается знать, что заказ
    пришёл, но не за сколько. Не задан — оператор получает тот же текст, что владелец.

    Заодно зеркалим событие в шину настольной админки: когда мастер за компьютером, тост
    в углу экрана удобнее, чем лезть в телефон. Цель перехода берётся из той же кнопки,
    поэтому ~27 мест вызова править не пришлось. Зеркало не должно ронять отправку в
    Telegram — оно вторично. `operator_text` уезжает в шину вместе с основным текстом:
    с личным входом (21.08.2026) за настольной админкой сидит конкретный человек, и тост
    оператору обязан быть таким же безденежным, как его телеграм.
    """
    try:
        deskevents.push_from_telegram(text, kind=kind, reply_markup=reply_markup,
                                      operator_text=operator_text)
    except Exception:  # noqa: BLE001
        log.debug("desk event mirror failed", exc_info=True)
    for aid in recipients(audience):
        if exclude is not None and aid == exclude:
            continue
        # Оператору — урезанный текст, если вызывающий его дал. Проверка по роли, а не по
        # «не из ADMIN_IDS»: приглашённый ВЛАДЕЛЕЦ тоже живёт в staff и деньги видеть вправе.
        body = text
        if operator_text is not None and not config.is_owner(aid):
            body = operator_text
        try:
            if photo is not None:
                msg = await bot.send_photo(aid, _photo(photo), caption=body,
                                           reply_markup=reply_markup)
            else:
                msg = await bot.send_message(aid, body, reply_markup=reply_markup)
            await _track(on_sent, aid, msg)
        except Exception:  # noqa: BLE001
            # Не debug: недоставленное мастеру уведомление — это потерянное сообщение
            # клиента (Telegram отвечает 400 на «сырую» разметку в тексте), и в обычном
            # логе оно обязано быть видно, а не тонуть в отладочном уровне.
            log.warning("notify admin %s failed", aid, exc_info=True)


async def notify_user(bot, user_id: int, text: str, *, reply_markup=None, photo=None,
                      on_sent=None) -> bool:
    """Сообщение конкретному пользователю (photo задан → фото с подписью, как у
    notify_admins — нужно рассылке «модель дня», 6fbed3ad). True при успехе.
    on_sent — см. _track: кому нужен message_id отправленного (переписка заказа)."""
    try:
        if photo is not None:
            msg = await bot.send_photo(user_id, _photo(photo), caption=text,
                                       reply_markup=reply_markup)
        else:
            msg = await bot.send_message(user_id, text, reply_markup=reply_markup)
        await _track(on_sent, user_id, msg)
        return True
    except Exception:  # noqa: BLE001
        log.info("notify user %s failed (мог не запускать бота)", user_id)
        return False
