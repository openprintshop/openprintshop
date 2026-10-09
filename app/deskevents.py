"""Шина событий для настольной админки (этап 3 редизайна ПК-админки).

Зачем отдельная шина. Все админские уведомления уходят в Telegram через две функции
`notify.notify_admins()` и `notify.alert()`. Когда мастер сидит за компьютером, тянуться
к телефону ради «пришёл новый заказ» — глупо, поэтому те же события зеркалим сюда, а
оболочка (desktop_admin/shell.py) забирает их и показывает Windows-тостами.

Почему в памяти, а не в БД. Это именно всплывающие уведомления: показали — и забыли.
Всё, что должно пережить перезапуск, уже лежит в таблице `admin_notifications`
(черновики release notes, заявки на семейный счёт). Дублировать её незачем, а лишняя
запись в SQLite на каждый чих била бы по общему с ботом event loop.

Реалтайма в проекте нет вообще (ни SSE, ни WebSocket), поэтому оболочка опрашивает
`GET /api/admin/events?since=<id>`. Курсор `since` делает опрос идемпотентным: перезапуск
оболочки не приводит к повторному показу уже увиденного.
"""
from __future__ import annotations

import html as _html
import re
import time
from collections import deque

# Держим последние события: на глаз это несколько часов работы мастерской. Больше не нужно —
# кто не забрал, тот и не покажет, а история живёт в Telegram.
_MAX = 200
_events: deque = deque(maxlen=_MAX)
_seq = 0

# Ссылка вида .../app/?v=..&go=adminorder&id=42 — её кладёт keyboards.open_app в кнопку
# уведомления. Разбираем её здесь, чтобы не трогать ~27 мест вызова notify_admins().
_GO_RE = re.compile(r"[?&]go=([a-zA-Z_]+)")
_ID_RE = re.compile(r"[?&]id=(\d+)")

# Телеграм-сообщения размечены HTML (<b>, <code>, <a href>). В Windows-тосте разметки нет,
# и теги показались бы буквально — вычищаем их здесь, а не в оболочке: шина отдаёт текст
# всем не-телеграмным потребителям, и чистым он нужен каждому.
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    return _html.unescape(_TAG_RE.sub("", text or "")).strip()


def _extract_target(reply_markup) -> tuple[str, int]:
    """Достать (go, id) из кнопки-вебапки уведомления. Пусто — тост будет без перехода."""
    if reply_markup is None:
        return "", 0
    try:
        for row in getattr(reply_markup, "inline_keyboard", []) or []:
            for btn in row:
                wa = getattr(btn, "web_app", None)
                url = getattr(wa, "url", None) or getattr(btn, "url", None) or ""
                if not url:
                    continue
                go = _GO_RE.search(url)
                oid = _ID_RE.search(url)
                if go:
                    return go.group(1), int(oid.group(1)) if oid else 0
    except Exception:  # noqa: BLE001
        pass
    return "", 0


def push(text: str, *, kind: str = "info", go: str = "", go_id: int = 0,
         operator_text: str | None = None) -> int:
    """Положить событие в шину. Возвращает его id (он же курсор для оболочки).

    operator_text — тот же текст БЕЗ денег, который `notify.notify_admins` шлёт оператору
    в Telegram. Храним оба варианта, а выбирает уже ручка по правам смотрящего: шина не
    знает ни про роли, ни про запрос. До личного входа выбирать было не из чего — тосты
    видел только владелец, — а с ним за тем же компьютером может сидеть оператор, и
    обходить роли через тост так же нельзя, как через Telegram.
    """
    global _seq
    _seq += 1
    _events.append({
        "id": _seq,
        "at": int(time.time()),
        "kind": kind,                       # info | alert
        # В тосте Windows заголовок и текст разделены, а у нас единая строка из Telegram:
        # первая строка становится заголовком, остальное — телом.
        "text": _plain(text)[:600],
        # Внутренний ключ: наружу его не отдаёт даже владелец (см. since) — это не второе
        # поле ответа, а замена первому для того, кому деньги не полагаются.
        "text_op": _plain(operator_text)[:600] if operator_text else "",
        "go": go,
        "go_id": go_id,
    })
    return _seq


def push_from_telegram(text: str, *, kind: str = "info", reply_markup=None,
                       operator_text: str | None = None) -> int:
    """Зеркало телеграм-уведомления: цель перехода берём из кнопки «Открыть в приложении»."""
    go, go_id = _extract_target(reply_markup)
    return push(text, kind=kind, go=go, go_id=go_id, operator_text=operator_text)


def since(cursor: int, limit: int = 50, *, money: bool = True) -> list[dict]:
    """События новее курсора. Оболочка передаёт последний виденный id.

    money=False — смотрящему деньги не полагаются (оператор): отдаём урезанный вариант
    текста. Правило то же, что в notify_admins: варианта нет — значит и денег в событии
    нет, отдаём как есть.
    """
    out = []
    for e in _events:
        if e["id"] <= cursor:
            continue
        ev = {k: v for k, v in e.items() if k != "text_op"}
        if not money and e["text_op"]:
            ev["text"] = e["text_op"]
        out.append(ev)
    return out[-limit:]


def last_id() -> int:
    """Текущая вершина. Оболочка берёт её при старте, чтобы не показать старое разом."""
    return _seq


def reset() -> None:
    """Только для тестов."""
    global _seq
    _events.clear()
    _seq = 0
