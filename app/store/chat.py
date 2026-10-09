"""Переписка мастер<->клиент вместе с картой «сообщение переписки <-> сообщение в Telegram».

Две секции db.py, слитые в один модуль намеренно:

* ПЕРЕПИСКА (order_messages, 9d222a19; сообщения вне заказа и вложения — 2026-07-19):
  треды заказов и треды «вне заказа» (request_id NULL), цитаты-реплаи через LEFT JOIN
  таблицы самой на себя (_MSG_COLS), отметки прочтения раздельно для клиента и мастера,
  поиск по тексту, счётчики непрочитанного для списка заказов и списка клиентов.
* КАРТА TG (tg_chat_msgs, 13.08.2026) — чем именно уехала строка переписки в Telegram;
  без неё реакции ставить не на что (в апдейте message_reaction нет ни текста, ни заказа).

Резать между ними нельзя: tg_message_link читает ОБЕ таблицы одним JOIN-ом, а
prune_tg_links чистит карту по возрасту строк переписки (order_messages.created_at).

Выделено из app/db.py волной 5 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ. Самый лёгкий модуль волны: из внешнего мира
нужны только core._conn и stdlib time — ни моделей, ни config, ни соседних доменов.
Логгер не заводится: ни одно из 18 тел не пишет в лог.

Имя модуля `chat` — про ХРАНИЛИЩЕ переписки; транспорт и склейка с Telegram живут в
app/orderchat.py, столкновения имён в db.py нет (проверено AST-сканом: идентификатора
chat там не встречается вовсе, только строки SQL).
"""
from __future__ import annotations

import time

from .core import _abs, _conn, _rel


# ---- Переписка мастер↔клиент (9d222a19; вне заказа и вложения — 2026-07-19) ----
async def _msg_user_id(request_id: int | None) -> int | None:
    if not request_id:
        return None
    cur = await _conn().execute("SELECT user_id FROM requests WHERE id=?", (request_id,))
    row = await cur.fetchone()
    return row["user_id"] if row else None


async def add_order_message(request_id: int, from_admin: bool, text: str,
                            image_path: str | None = None, user_id: int | None = None,
                            reply_to: int | None = None) -> int:
    """Сообщение в тред заказа. Отправитель сразу «прочитал» своё сообщение.

    user_id заполняем всегда: по нему собирается общий чат клиента (сплошная лента из
    тредов всех заказов плюс сообщений вне заказа).
    reply_to — id сообщения этой же переписки, на которое отвечают (реплай или реакция из
    Telegram); проверку «то ли это сообщение» делает вызывающий."""
    now = int(time.time())
    uid = user_id if user_id is not None else await _msg_user_id(request_id)
    cur = await _conn().execute(
        "INSERT INTO order_messages(request_id, user_id, from_admin, text, image_path, "
        "created_at, read_user, read_admin, reply_to) VALUES(?,?,?,?,?,?,?,?,?)",
        (request_id, uid, 1 if from_admin else 0, text, _rel(image_path) or None, now,
         0 if from_admin else 1,    # read_user: 1, если отправитель — клиент (своё прочитано)
         1 if from_admin else 0,    # read_admin: 1, если отправитель — админ
         reply_to or None))
    await _conn().commit()
    return cur.lastrowid


async def add_client_message(user_id: int, from_admin: bool, text: str,
                             image_path: str | None = None, reply_to: int | None = None) -> int:
    """Сообщение клиенту ВНЕ заказа (request_id NULL). Раньше «написать клиенту» просто слало
    в Telegram и нигде не сохранялось — переписка в админке обрывалась."""
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO order_messages(request_id, user_id, from_admin, text, image_path, "
        "created_at, read_user, read_admin, reply_to) VALUES(NULL,?,?,?,?,?,?,?,?)",
        (user_id, 1 if from_admin else 0, text, _rel(image_path) or None, now,
         0 if from_admin else 1, 1 if from_admin else 0, reply_to or None))
    await _conn().commit()
    return cur.lastrowid


# Цитата ответа и отметки прочтения едут вместе с сообщением: q.* — то, на что отвечали
# (LEFT JOIN самой на себя), read_user/read_admin — кто из сторон это уже видел. Считать
# «прочитано» на фронте нельзя: у клиента и у мастера разные стороны, и знать, чья отметка
# чья, обязан сервер.
_MSG_COLS = ("m.id, m.from_admin, m.text, m.image_path, m.created_at, m.read_user, m.read_admin, "
             "m.reply_to, q.text AS q_text, q.from_admin AS q_from_admin, "
             "q.image_path AS q_image")


def _img_row(r) -> dict:
    """dict(строка) с абсолютным image_path (C2): значение уходит в FSInputFile/FileResponse."""
    d = dict(r)
    if "image_path" in d:
        d["image_path"] = _abs(d["image_path"])
    return d


def _msg_row(r) -> dict:
    return {"id": r["id"], "from_admin": r["from_admin"], "text": r["text"],
            "image_path": _abs(r["image_path"]), "at": r["created_at"],
            "read_user": r["read_user"], "read_admin": r["read_admin"],
            "reply_to": r["reply_to"],
            "reply_text": r["q_text"], "reply_from_admin": r["q_from_admin"],
            "reply_image": _abs(r["q_image"])}


async def list_order_messages(request_id: int) -> list[dict]:
    cur = await _conn().execute(
        f"SELECT {_MSG_COLS} FROM order_messages m "
        "LEFT JOIN order_messages q ON q.id = m.reply_to "
        "WHERE m.request_id=? ORDER BY m.id", (request_id,))
    return [_msg_row(r) for r in await cur.fetchall()]


async def list_client_messages(user_id: int, limit: int = 400) -> list[dict]:
    """Сплошная переписка с клиентом: треды всех его заказов плюс сообщения вне заказа.
    request_id в ответе позволяет нарисовать разделители «— по заказу #N —»."""
    cur = await _conn().execute(
        f"SELECT {_MSG_COLS}, m.request_id, r.model_label AS order_label "
        "FROM order_messages m "
        "LEFT JOIN order_messages q ON q.id = m.reply_to "
        "LEFT JOIN requests r ON r.id = m.request_id "
        "WHERE m.user_id=? ORDER BY m.id DESC LIMIT ?", (user_id, limit))
    rows = [dict(_msg_row(r), request_id=r["request_id"], order_label=r["order_label"])
            for r in await cur.fetchall()]
    rows.reverse()          # в БД брали свежие, показываем по возрастанию
    return rows


async def mark_order_read(request_id: int, by_admin: bool) -> None:
    """Пометить прочитанными сообщения противоположной стороны в треде заказа."""
    col = "read_admin" if by_admin else "read_user"
    await _conn().execute(
        f"UPDATE order_messages SET {col}=1 WHERE request_id=? AND {col}=0", (request_id,))
    await _conn().commit()


async def mark_client_read(user_id: int, by_admin: bool) -> None:
    """Пометить прочитанной ВСЮ переписку с клиентом (общий чат в карточке клиента)."""
    col = "read_admin" if by_admin else "read_user"
    await _conn().execute(
        f"UPDATE order_messages SET {col}=1 WHERE user_id=? AND {col}=0", (user_id,))
    await _conn().commit()


async def clients_unread_admin() -> dict[int, int]:
    """{user_id: число непрочитанных админом сообщений клиента} — бейдж в списке клиентов.
    Считает и сообщения вне заказа, поэтому идёт по user_id, а не через JOIN с requests."""
    cur = await _conn().execute(
        "SELECT user_id AS uid, COUNT(*) AS c FROM order_messages "
        "WHERE from_admin=0 AND read_admin=0 AND user_id IS NOT NULL GROUP BY user_id")
    return {r["uid"]: r["c"] for r in await cur.fetchall()}


async def get_message(msg_id: int) -> dict | None:
    """Одно сообщение — нужно для отдачи вложения по подписанному URL."""
    cur = await _conn().execute(
        "SELECT id, request_id, user_id, from_admin, text, image_path, created_at "
        "FROM order_messages WHERE id=?", (msg_id,))
    r = await cur.fetchone()
    return _img_row(r) if r else None


async def set_message_image(msg_id: int, image_path: str) -> None:
    await _conn().execute("UPDATE order_messages SET image_path=? WHERE id=?",
                          (_rel(image_path), msg_id))
    await _conn().commit()


# ---- Карта «сообщение переписки ↔ сообщение в Telegram» (реакции и ответы, 13.08.2026) ----

async def link_tg_message(chat_id: int, message_id: int, msg_id: int) -> None:
    """Запомнить, каким сообщением в чате chat_id уехала строка переписки msg_id.

    REPLACE, а не IGNORE: пара (чат, сообщение) в Telegram уникальна навсегда, и если она
    встретилась второй раз — значит прошлая строка про неё была враньём."""
    await _conn().execute(
        "INSERT OR REPLACE INTO tg_chat_msgs(chat_id, message_id, msg_id, created_at) "
        "VALUES(?,?,?,?)", (int(chat_id), int(message_id), int(msg_id), int(time.time())))
    await _conn().commit()


async def tg_message_link(chat_id: int, message_id: int) -> dict | None:
    """Строка переписки, которой соответствует сообщение Telegram (или None).

    JOIN, а не два запроса: сообщение переписки могли удалить вместе с заказом
    (delete_request чистит order_messages), и висячая ссылка не должна выглядеть живой."""
    cur = await _conn().execute(
        "SELECT m.id, m.request_id, m.user_id, m.from_admin, m.text, m.image_path "
        "FROM tg_chat_msgs t JOIN order_messages m ON m.id = t.msg_id "
        "WHERE t.chat_id=? AND t.message_id=?", (int(chat_id), int(message_id)))
    r = await cur.fetchone()
    return _img_row(r) if r else None


async def prune_tg_links(days: int = 180) -> int:
    """Убрать старые связи: смысл они имеют, пока на сообщение реально могут ответить."""
    cutoff = int(time.time()) - days * 86400
    cur = await _conn().execute("DELETE FROM tg_chat_msgs WHERE created_at < ?", (cutoff,))
    await _conn().commit()
    return cur.rowcount or 0


async def search_messages(q: str, limit: int = 20) -> list[dict]:
    """Поиск по тексту переписки — для командной палитры (Ctrl+K)."""
    like = f"%{q}%"
    cur = await _conn().execute(
        "SELECT m.id, m.request_id, m.user_id, m.from_admin, m.text, m.created_at, "
        "       COALESCE(c.name, r.user_name, '') AS who "
        "FROM order_messages m "
        "LEFT JOIN requests r ON r.id = m.request_id "
        "LEFT JOIN customers c ON c.user_id = m.user_id "
        "WHERE m.text LIKE ? ORDER BY m.id DESC LIMIT ?", (like, limit))
    return [{"id": r["id"], "request_id": r["request_id"], "user_id": r["user_id"],
             "from_admin": r["from_admin"], "text": r["text"], "at": r["created_at"],
             "who": r["who"]} for r in await cur.fetchall()]


async def orders_unread_user(user_id: int) -> dict[int, int]:
    """{request_id: число непрочитанных клиентом ответов админа} по заказам клиента."""
    cur = await _conn().execute(
        "SELECT m.request_id AS rid, COUNT(*) AS c FROM order_messages m "
        "JOIN requests r ON r.id = m.request_id "
        "WHERE r.user_id=? AND m.from_admin=1 AND m.read_user=0 GROUP BY m.request_id", (user_id,))
    return {r["rid"]: r["c"] for r in await cur.fetchall()}


async def orders_unread_admin() -> dict[int, int]:
    """{request_id: число непрочитанных админом сообщений клиента}.

    Сообщения вне заказа (request_id NULL) сюда не попадают — они считаются по клиенту
    в clients_unread_admin(), иначе NULL-группа испортила бы счётчики строк списка заказов."""
    cur = await _conn().execute(
        "SELECT request_id AS rid, COUNT(*) AS c FROM order_messages "
        "WHERE from_admin=0 AND read_admin=0 AND request_id IS NOT NULL GROUP BY request_id")
    return {r["rid"]: r["c"] for r in await cur.fetchall()}
