"""Реплай в Telegram → сообщение в чат заказа (и обратно).

Зачем. Переписка мастера с клиентом жила ТОЛЬКО в приложении: бот присылал в Telegram
уведомление «💬 Сообщение по заказу #136», а ответить на него было нечем — реплай доставался
catch-all фолбэку user_flow, и клиент получал «Нажмите /start, чтобы открыть меню». Живой
случай 13.08.2026: мастер спросил в чате заказа про сроки, клиент ответил цитатой прямо в
Telegram, ответ не дошёл никуда, и клиент сам попросил «сделать реплаи, чтоб попадали прямиком
в чатик заказа». Теперь такой ответ ложится в тред заказа и уходит второй стороне — в обе
стороны: клиент отвечает мастеру, мастер отвечает клиенту, не открывая приложение.

Как узнаём, КУДА класть ответ — по ЦИТИРУЕМОМУ тексту, а не по таблице «message_id → заказ».
Таблица знала бы только те уведомления, что отправлены после выката, а реплаят люди на то, что
уже лежит в переписке (в том числе на позавчерашнее). Разбор текста работает задним числом на
всю историю чата — ровно тот случай, из-за которого фича и попрошена.

Чем это безопасно:
  • ЧУЖОЙ заказ недостижим: у клиентской ветки стоит сверка `requests.user_id == отправитель`,
    так что подобранным номером в чужой тред не попасть;
  • за мастера отвечает не «шапка сообщения», а роль: ветка команды требует roles.CAP_ORDERS;
  • команда отвечает клиенту ТОЛЬКО реплаем на сообщение самого клиента (шапка «… от Аня:»).
    Реплай на любое другое уведомление о заказе («заказ отменён», «печать закончилась») клиенту
    НЕ уходит: мастер часто отвечает на такие уведомления сам себе — заметкой, номером, «ок», —
    и превращать заметку в сообщение клиенту нельзя. Клиенту, наоборот, разрешён реплай на
    любое уведомление о СВОЁМ заказе: его текст в любом случае попадает мастеру, утечь некуда.

Шапки уведомлений строятся и опознаются ЗДЕСЬ ЖЕ (to_client/to_staff против _RE_*): формату и
разбору разъехаться нечем, пока они в одном файле. app/webapp.py зовёт те же функции — иначе
правка текста в приложении молча ломала бы разбор реплаев.

Модуль не импортирует ни webapp, ни handlers (правило app/orders.py и app/printmedia.py) и не
держит модульного `_bot`: бот приезжает параметром от того, кто отправляет.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import db, notify, roles, texts

log = logging.getLogger(__name__)

# ── Шапки уведомлений ──────────────────────────────────────────────────────────────────────
_HEAD = "💬 Сообщение по заказу #"
_HEAD_OFF_CLIENT = "💬 Сообщение от мастера:"
_HEAD_OFF_STAFF = "💬 Сообщение вне заказа от "

HINT_TO_CLIENT = "↩️ Ответьте на это сообщение — я передам мастеру."
HINT_TO_STAFF = "↩️ Ответьте на это сообщение — ответ уйдёт клиенту в чат заказа."

# Цитата приезжает из Telegram БЕЗ разметки (<b> вырезаны), но те же тексты бывают и с ней —
# например, при разборе уже готовой строки в тестах. Поэтому теги в шаблонах необязательны.
_TAG = r"(?:</?b>\s*)*"
# Шапки ищем ТОЛЬКО с начала цитаты (match, без re.M). Внутрь уведомления попадает чужой текст —
# и клиентский, и имя клиента, — так что поиск «где угодно» превратился бы в подставу: клиенту
# достаточно было бы написать строкой «💬 Сообщение вне заказа от я (клиент id 777)», и ответ
# мастера на ЕГО сообщение ушёл бы клиенту 777. Шапку пишет бот, и она всегда первая.
_RE_TO_CLIENT = re.compile(rf"{_HEAD}(\d{{1,9}}):")
_RE_TO_STAFF = re.compile(rf"{_HEAD}(\d{{1,9}}) от .*:")
_RE_OFF_STAFF = re.compile(rf"{_HEAD_OFF_STAFF}.*\(клиент id (\d{{1,15}})\)")
# Любое другое уведомление о заказе: «🔔 Заказ #12 …», «🖨 Печать по заказу <b>#12</b> …».
# Номер берём только рядом со словом «заказ» — иначе решёткой мог бы оказаться промокод,
# задача или партия, и ответ клиента уехал бы в случайный тред с тем же номером.
_RE_ORDER = re.compile(rf"заказ\w*\s*{_TAG}#\s*(\d{{1,9}})", re.I)


@dataclass(frozen=True)
class Target:
    """Куда ложится сообщение: тред заказа (rid) или переписка вне заказа (rid=None).

    client_id — всегда КЛИЕНТ, чей это чат, кто бы ни писал; from_admin — кто пишет сейчас."""

    rid: int | None
    client_id: int
    from_admin: bool


# ── Тексты уведомлений ─────────────────────────────────────────────────────────────────────
# text/who приезжают СЫРЫМИ и экранируются здесь: бот отправляет с parse_mode=HTML, и «<3» в
# клиентском тексте иначе делает разметку битой — Telegram отвечает 400, а except вокруг
# отправки глушит сбой в отладочный лог (см. tests/test_notify_escape.py).

def to_client(rid: int, text: str, *, hint: bool = False) -> str:
    """Сообщение мастера — клиенту, в его личный чат с ботом."""
    tail = f"\n\n{HINT_TO_CLIENT}" if hint else ""
    return f"{_HEAD}{rid}:\n{texts.esc(text)}{tail}"


def to_staff(rid: int, who: str, text: str, *, hint: bool = False) -> str:
    """Сообщение клиента — команде мастерской."""
    tail = f"\n\n{HINT_TO_STAFF}" if hint else ""
    return f"{_HEAD}{rid} от {texts.esc(who)}:\n{texts.esc(text)}{tail}"


def to_client_off(text: str, *, hint: bool = False) -> str:
    """Сообщение мастера клиенту ВНЕ заказа («написать клиенту» из карточки)."""
    tail = f"\n\n{HINT_TO_CLIENT}" if hint else ""
    return f"{_HEAD_OFF_CLIENT}\n\n{texts.esc(text)}{tail}"


def to_staff_off(who: str, client_id: int, text: str, *, hint: bool = False) -> str:
    """Сообщение клиента команде ВНЕ заказа.

    Номер клиента в шапке — не украшение: у переписки вне заказа нет номера заказа, и это
    ЕДИНСТВЕННОЕ, по чему реплай мастера находит адресата. Уберёте — ответ команды на такое
    уведомление станет некуда девать (клиент его не получит вовсе)."""
    tail = f"\n\n{HINT_TO_STAFF}" if hint else ""
    return (f"{_HEAD_OFF_STAFF}{texts.esc(who)} (клиент id {int(client_id)}):"
            f"\n{texts.esc(text)}{tail}")


# ── Разбор цитаты ──────────────────────────────────────────────────────────────────────────

async def resolve(uid: int, quoted: str) -> Target | None:
    """Куда уйдёт ответ пользователя uid на процитированный текст бота.

    None — некуда: сообщение обязано идти прежним путём (подсказка меню, расчёт по ссылке,
    шаг мастера). Именно поэтому отказ здесь МОЛЧАЛИВЫЙ — это не ошибка, а «не наш случай»."""
    q = (quoted or "").strip()
    if not q:
        return None
    m = _RE_TO_CLIENT.match(q)           # «… #136:» — мастер писал КЛИЕНТУ, отвечает клиент
    if m:
        return await _client_target(uid, int(m.group(1)))
    m = _RE_TO_STAFF.match(q)            # «… #136 от Аня:» — писали КОМАНДЕ, отвечает команда
    if m:
        return await _staff_target(uid, int(m.group(1)))
    if q.startswith(_HEAD_OFF_CLIENT):   # «Сообщение от мастера:» вне заказа
        return Target(None, int(uid), False)
    m = _RE_OFF_STAFF.match(q)           # «Сообщение вне заказа от … (клиент id 77)»
    if m:
        return _staff_off_target(uid, int(m.group(1)))
    m = _RE_ORDER.search(q)              # любое другое уведомление о заказе — только клиенту
    if m:
        return await _client_target(uid, int(m.group(1)))
    return None


async def resolve_link(uid: int, chat_id: int, message_id: int) -> tuple[Target, int] | None:
    """То же самое, но ТОЧНО: по карте «сообщение Telegram → строка переписки».

    Возвращает (куда писать, id сообщения, на которое отвечают). Разбор текста этого не умеет
    и уметь не может: у реакции (апдейт message_reaction) нет ни текста, ни номера заказа —
    только чат и номер сообщения. Заодно ответ реплаем становится настоящим ответом в
    переписке, а не просто сообщением в тред.

    Работает лишь для сообщений, отправленных ПОСЛЕ появления карты (13.08.2026) — на всё,
    что старше, остаётся разбор цитаты."""
    row = await db.tg_message_link(chat_id, message_id)
    if not row:
        return None
    client_id = int(row["user_id"] or 0)
    if not client_id:
        return None
    rid = row["request_id"]
    if int(uid) == client_id:                       # свою переписку клиент открывает всегда
        return Target(rid, client_id, False), int(row["id"])
    if roles.can(uid, roles.CAP_ORDERS):            # сотрудник отвечает клиенту
        return Target(rid, client_id, True), int(row["id"])
    return None


async def _client_target(uid: int, rid: int) -> Target | None:
    r = await db.get_request(rid)
    if r is None or int(r.user_id) != int(uid):
        return None      # чужой (или исчезнувший) заказ — чужой тред подобранным номером не открыть
    return Target(rid, int(uid), False)


async def _staff_target(uid: int, rid: int) -> Target | None:
    if not roles.can(uid, roles.CAP_ORDERS):
        return None      # не сотрудник или сотрудник без права на заказы
    r = await db.get_request(rid)
    if r is None:
        return None
    return Target(rid, int(r.user_id), True)


def _staff_off_target(uid: int, client_id: int) -> Target | None:
    if not roles.can(uid, roles.CAP_ORDERS):
        return None
    return Target(None, int(client_id), True)


# ── Запись и доставка ──────────────────────────────────────────────────────────────────────

async def save(target: Target, text: str, reply_to: int | None = None) -> int:
    """Сообщение — в переписку (в тред заказа или в общий чат клиента). Возвращает id строки:
    по нему вложение кладётся файлом рядом, как и у приложения (chat_<id>.jpg).
    reply_to — на что отвечают (реплай или реакция), если это известно."""
    if target.rid:
        return await db.add_order_message(target.rid, from_admin=target.from_admin, text=text,
                                          user_id=target.client_id, reply_to=reply_to)
    return await db.add_client_message(target.client_id, from_admin=target.from_admin, text=text,
                                       reply_to=reply_to)


async def reaction_seen(target: Target, reply_to: int, emoji: str) -> bool:
    """Такая реакция этой стороны на это сообщение в переписке уже есть?

    Реакции в Telegram снимаются и ставятся заново одним тапом, и апдейт приходит на каждый
    тап. Без этой проверки одно сердечко, поставленное трижды, стало бы тремя сообщениями."""
    try:
        rows = (await db.list_order_messages(target.rid) if target.rid
                else await db.list_client_messages(target.client_id))
    except Exception:  # noqa: BLE001
        log.debug("reaction dedup check failed", exc_info=True)
        return False
    return any(m.get("reply_to") == reply_to and bool(m["from_admin"]) == target.from_admin
               and (m["text"] or "").strip() == emoji for m in rows)


async def mark_read(target: Target) -> None:
    """Ответивший сторону напротив, очевидно, прочитал — гасим её непрочитанное в треде.

    Только для треда заказа: mark_client_read гасит ВСЮ переписку клиента разом, включая
    заказы, которых мастер в глаза не видел, — из одного ответа вне заказа такого вывода не
    следует."""
    if not target.rid:
        return
    try:
        await db.mark_order_read(target.rid, by_admin=target.from_admin)
    except Exception:  # noqa: BLE001
        log.debug("mark read failed", exc_info=True)


async def is_first(target: Target) -> bool:
    """Первое сообщение ЭТОЙ стороны в переписке? Только тогда к уведомлению приклеивается
    подсказка про реплай: она нужна, чтобы о возможности вообще узнали, но висеть под каждым
    сообщением ей незачем — переписка превратилась бы в столбик служебных строк.

    Зовётся ПОСЛЕ save(), поэтому «первое» — это ровно одно."""
    try:
        if target.rid:
            rows = await db.list_order_messages(target.rid)
        else:
            rows = [m for m in await db.list_client_messages(target.client_id)
                    if not m.get("request_id")]
    except Exception:  # noqa: BLE001
        log.debug("hint check failed", exc_info=True)
        return False
    return sum(1 for m in rows if bool(m["from_admin"]) == target.from_admin) <= 1


def _linker(msg_id: int | None):
    """Колбэк «запомни, каким сообщением Telegram уехала эта строка переписки».

    Без карты не работают ни реакции (в апдейте нет ничего, кроме чата и номера сообщения),
    ни привязка ответа к конкретному сообщению. msg_id не задан — ничего не пишем."""
    if not msg_id:
        return None

    async def _link(chat_id: int, message_id: int) -> None:
        await db.link_tg_message(chat_id, message_id, int(msg_id))
    return _link


async def notify_staff(bot, target: Target, who: str, text: str, photo_path: str = "",
                       msg_id: int | None = None) -> None:
    """Сообщение клиента — команде. Зовут ОБА пути: приложение (webapp) и реплай в Telegram.

    audience=ALL_STAFF: отвечать в переписке оператору уже разрешено (webguard:
    /api/admin/order/message), значит и приходить она должна ему тоже — иначе он отвечает
    вслепую. Сбой отправки не критичен: сообщение уже в переписке, мастер увидит его в админке.

    msg_id — строка переписки, которую сейчас разослали: каждый сотрудник получает СВОЙ
    message_id, и реакция любого из них должна находить ту же строку."""
    hint = await is_first(target)
    body = (to_staff(target.rid, who, text, hint=hint) if target.rid
            else to_staff_off(who, target.client_id, text, hint=hint))
    try:
        await notify.notify_admins(bot, body, audience=notify.ALL_STAFF,
                                   photo=photo_path or None, on_sent=_linker(msg_id))
    except Exception:  # noqa: BLE001
        log.debug("order message notify failed", exc_info=True)


async def notify_client(bot, target: Target, text: str, photo_path: str = "",
                        reply_markup=None, msg_id: int | None = None) -> bool:
    """Сообщение мастера — клиенту. False, если Telegram не доставил (клиент не запускал бота
    или заблокировал его): сообщение при этом ОСТАЁТСЯ в переписке, и вызывающий говорит об
    этом человеку, а не делает вид, что всё ушло."""
    hint = await is_first(target)
    body = (to_client(target.rid, text, hint=hint) if target.rid
            else to_client_off(text, hint=hint))
    return await notify.notify_user(bot, target.client_id, body, photo=photo_path or None,
                                    reply_markup=reply_markup, on_sent=_linker(msg_id))
