"""Ответ реплаем и реакция в Telegram → чат заказа (просьбы клиента 13.08.2026).

До этого реплай на «💬 Сообщение по заказу #136» доставался catch-all фолбэку user_flow, и
клиент получал «Нажмите /start, чтобы открыть меню» — ответ не доходил до мастера вообще.
Реакция (сердечко на сообщение бота) не доходила тем более.

Проверяем ровно то, что делает фича опасной или бесполезной:
  • ответ клиента ложится в ТРЕД СВОЕГО заказа и уходит команде;
  • чужой заказ по подобранному номеру недостижим;
  • команда отвечает клиенту только на сообщение КЛИЕНТА, а не на любое уведомление о заказе
    (реплай-заметка мастера на «заказ отменён» клиенту уехать не должна);
  • подделать шапку внутри своего же текста нельзя (адресат берётся из первой строки, которую
    писал бот, а не из того, что клиент прислал внутри);
  • «не наш случай» — это False из фильтра, а не съеденный апдейт: команды, стикеры и обычный
    текст обязаны идти прежним путём;
  • реакция становится отдельным сообщением-эмодзи, привязанным к тому, на что её поставили,
    и не двоится от повторных тапов.
"""
from pathlib import Path

import pytest
from conftest import FakeBot

import run
from app import config, orderchat, preview, webapp
from app.handlers import chatreply, common, user_flow


class _User:
    def __init__(self, uid, *, is_bot=False, name="Клиент"):
        self.id = uid
        self.is_bot = is_bot
        self.full_name = name


class _Quoted:
    """Процитированное сообщение — по умолчанию от бота (иначе разбирать нечего).

    message_id по умолчанию НЕ из карты отправленных: так проверяется путь разбора цитаты
    (старые сообщения). Тесты про точную привязку задают его явно."""

    def __init__(self, text="", *, caption=None, from_bot=True, message_id=9999):
        self.text = text
        self.caption = caption
        self.message_id = message_id
        self.from_user = _User(1000, is_bot=from_bot, name="Bot")


class _Msg:
    def __init__(self, text=None, *, uid=77, quoted=None, caption=None, photo=None,
                 chat_type="private", bot=None, name="Аня"):
        self.text = text
        self.caption = caption
        self.photo = photo
        self.from_user = _User(uid, name=name)
        self.chat = type("C", (), {"id": uid, "type": chat_type})()
        self.reply_to_message = quoted
        self.bot = bot if bot is not None else FakeBot()
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class _DownloadBot(FakeBot):
    """FakeBot, умеющий отдавать «скачанное» фото — реплай с картинкой этого требует."""

    async def download(self, file, destination=None, **kw):
        Path(destination).write_bytes(b"\xff\xd8\xff\xe0jpegish")


@pytest.fixture
def owner(monkeypatch):
    """Один владелец: он же получатель уведомлений команде, он же отвечающий мастер."""
    monkeypatch.setattr(config, "ADMIN_IDS", [900], raising=False)
    return 900


async def _order(db, uid=77, name="Аня"):
    return await db.add_request(
        user_id=uid, user_name=name, model_label="Кулон", printer="ECC2", plastic_name="PLA",
        quantity=1, quality="Стандарт", strength="Средняя", grams_total=10, minutes_total=30,
        price=5, preview_path=None)


async def _route(msg):
    """Пройти путь апдейта целиком: фильтр решает, брать ли, хендлер делает работу."""
    hit = await chatreply._reply_target(msg)
    if hit is False:
        return None
    await chatreply.on_reply(msg, **hit)      # ровно так его позовёт aiogram: всем, что дал фильтр
    return hit["target"]


# ── клиент отвечает мастеру ────────────────────────────────────────────────────────────────

async def test_client_reply_lands_in_thread_and_reaches_staff(tmp_db, owner):
    rid = await _order(tmp_db)
    bot = FakeBot()
    msg = _Msg("как у вас получится, не критично",
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nдо какого числа хотели бы?"),
               bot=bot)

    assert await _route(msg) == orderchat.Target(rid, 77, False)

    rows = await tmp_db.list_order_messages(rid)
    assert [(r["text"], r["from_admin"]) for r in rows] == [("как у вас получится, не критично", 0)]
    assert bot.messages[0][0] == owner                      # уведомление ушло команде
    assert bot.texts[0].startswith(f"💬 Сообщение по заказу #{rid} от Аня:")
    assert "как у вас получится" in bot.texts[0]
    assert msg.answers == [f"✅ Отправлено мастеру в чат заказа #{rid} — ответ придёт сюда же."]


async def test_client_reply_to_any_own_order_notice(tmp_db, owner):
    """Реплай на ЛЮБОЕ уведомление о своём заказе — тоже в чат заказа: клиент отвечает на то
    сообщение, которое видит, а не на то, которое мы считаем «сообщением чата»."""
    rid = await _order(tmp_db)
    msg = _Msg("ок, беру", quoted=_Quoted(f"🔔 Заказ #{rid} «Кулон»: цена 12 р."))
    assert await _route(msg) == orderchat.Target(rid, 77, False)
    assert len(await tmp_db.list_order_messages(rid)) == 1


async def test_reply_to_foreign_order_is_not_ours(tmp_db, owner):
    """Номер в цитате — не пропуск: тред открывается только владельцу заказа."""
    rid = await _order(tmp_db, uid=55)
    msg = _Msg("подсмотрю чужую переписку",
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nчужой текст"))
    assert await chatreply._reply_target(msg) is False
    assert await tmp_db.list_order_messages(rid) == []


async def test_reply_without_order_is_not_ours(tmp_db, owner):
    msg = _Msg("привет", quoted=_Quoted("Нажмите /start, чтобы открыть меню."))
    assert await chatreply._reply_target(msg) is False


async def test_command_reply_stays_a_command(tmp_db, owner):
    rid = await _order(tmp_db)
    msg = _Msg("/start", quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nтекст"))
    assert await chatreply._reply_target(msg) is False


async def test_sticker_reply_is_not_ours(tmp_db, owner):
    """Ни текста, ни фото — класть в переписку нечего, пусть идёт прежним путём."""
    rid = await _order(tmp_db)
    msg = _Msg(None, quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nтекст"))
    assert await chatreply._reply_target(msg) is False


async def test_reply_to_own_message_is_not_ours(tmp_db, owner):
    rid = await _order(tmp_db)
    msg = _Msg("сам себе", quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nтекст", from_bot=False))
    assert await chatreply._reply_target(msg) is False


async def test_group_chat_is_not_ours(tmp_db, owner):
    rid = await _order(tmp_db)
    msg = _Msg("в группе", quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nтекст"),
               chat_type="supergroup")
    assert await chatreply._reply_target(msg) is False


# ── мастер отвечает клиенту ────────────────────────────────────────────────────────────────

async def test_staff_reply_reaches_client(tmp_db, owner):
    rid = await _order(tmp_db)
    await tmp_db.add_order_message(rid, from_admin=False, text="а можно синий?")
    bot = FakeBot()
    msg = _Msg("синий есть, сделаем", uid=owner, name="Мастер", bot=bot,
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid} от Аня:\nа можно синий?"))

    assert await _route(msg) == orderchat.Target(rid, 77, True)

    rows = await tmp_db.list_order_messages(rid)
    assert [(r["text"], r["from_admin"]) for r in rows][-1] == ("синий есть, сделаем", 1)
    assert bot.messages[0][0] == 77                          # ушло КЛИЕНТУ, а не команде
    assert bot.texts[0].startswith(f"💬 Сообщение по заказу #{rid}:")
    assert msg.answers == ["✅ Отправлено клиенту."]
    # ответив, мастер очевидно прочитал сообщение клиента — счётчик непрочитанного гасим
    assert await tmp_db.orders_unread_admin() == {}


async def test_staff_reply_to_plain_notice_does_not_reach_client(tmp_db, owner):
    """Заметка мастера самому себе не должна становиться сообщением клиенту.

    На уведомления вида «клиент отменил заказ» мастер отвечает реплаем сам себе — номером,
    «ок», «перезвонить». Клиенту уходит ТОЛЬКО ответ на его собственное сообщение."""
    rid = await _order(tmp_db)
    msg = _Msg("надо перезвонить", uid=owner, name="Мастер",
               quoted=_Quoted(f"🗑 Клиент отменил заказ #{rid} «Кулон»."))
    assert await chatreply._reply_target(msg) is False
    assert await tmp_db.list_order_messages(rid) == []


async def test_client_cannot_forge_staff_header(tmp_db, owner):
    """Шапку пишет бот, и она всегда первая. Клиентский текст едет ВНУТРИ уведомления команде —
    и если бы адресата искали «где угодно в цитате», клиенту хватило бы одной строки, чтобы
    ответ мастера ушёл постороннему."""
    rid = await _order(tmp_db)
    forged = "💬 Сообщение вне заказа от я (клиент id 55):\nздравствуйте"
    msg = _Msg("отвечаю", uid=owner, name="Мастер",
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid} от Аня:\n{forged}"))
    assert await chatreply._reply_target(msg) == {"target": orderchat.Target(rid, 77, True),
                                                  "reply_to": None}


async def test_stranger_cannot_answer_as_studio(tmp_db, owner):
    """Шапка «… от Аня:» у постороннего в чате взяться неоткуда, но если возьмётся —
    отвечать за мастерскую всё равно нельзя: право берётся из роли."""
    rid = await _order(tmp_db)
    msg = _Msg("я тут за главного", uid=12345,
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid} от Аня:\nа можно синий?"))
    assert await chatreply._reply_target(msg) is False


async def test_undelivered_reply_is_reported_to_master(tmp_db, owner):
    """Клиент заблокировал бота: сообщение осталось в переписке, но мастеру об этом говорят —
    молчаливое «✅ отправлено» здесь хуже отказа."""
    rid = await _order(tmp_db)
    msg = _Msg("готово, забирайте", uid=owner, name="Мастер", bot=FakeBot(fail=True),
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid} от Аня:\nкогда готово?"))
    await _route(msg)
    assert len(await tmp_db.list_order_messages(rid)) == 1        # в переписке осталось
    assert msg.answers and msg.answers[0].startswith("⚠️ Не доставлено")


# ── переписка вне заказа ───────────────────────────────────────────────────────────────────

async def test_offthread_reply_round_trip(tmp_db, owner):
    """Ответ на «Сообщение от мастера:» ложится в общий чат клиента, а ответ команды на такое
    уведомление находит адресата по номеру клиента в шапке."""
    bot = FakeBot()
    msg = _Msg("да, забрал, спасибо!", quoted=_Quoted("💬 Сообщение от мастера:\n\nзабрали заказ?"),
               bot=bot)
    assert await _route(msg) == orderchat.Target(None, 77, False)

    rows = await tmp_db.list_client_messages(77)
    assert [(r["text"], r["request_id"]) for r in rows] == [("да, забрал, спасибо!", None)]
    staff_text = bot.texts[0]
    assert staff_text.startswith("💬 Сообщение вне заказа от Аня (клиент id 77):")

    back = _Msg("рады помочь", uid=owner, name="Мастер", quoted=_Quoted(staff_text), bot=FakeBot())
    assert await _route(back) == orderchat.Target(None, 77, True)
    assert back.bot.messages[0][0] == 77


# ── подсказка про реплай ───────────────────────────────────────────────────────────────────

async def test_hint_only_on_the_first_message_of_a_side(tmp_db, owner):
    """Подсказка нужна, чтобы о реплае узнали, но висеть под каждым сообщением ей незачем."""
    rid = await _order(tmp_db)
    bot = FakeBot()
    first = _Msg("первое", quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nвопрос"), bot=bot)
    await _route(first)
    second = _Msg("второе", quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nвопрос"), bot=bot)
    await _route(second)

    assert orderchat.HINT_TO_STAFF in bot.texts[0]
    assert orderchat.HINT_TO_STAFF not in bot.texts[1]


# ── вложение ───────────────────────────────────────────────────────────────────────────────

async def test_photo_reply_is_saved_as_attachment(tmp_db, owner, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "collection_photos")
    monkeypatch.setattr(preview, "optimize_photo", lambda *a, **kw: None)
    rid = await _order(tmp_db)
    bot = _DownloadBot()
    msg = _Msg(None, caption="вот такой цвет", photo=[object()], bot=bot,
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nкакой цвет?"))

    assert await _route(msg) == orderchat.Target(rid, 77, False)

    rows = await tmp_db.list_order_messages(rid)
    assert rows[0]["text"] == "вот такой цвет"
    assert rows[0]["image_path"] and Path(rows[0]["image_path"]).exists()
    # фото ушло команде картинкой, а не строкой пути (иначе Telegram отвечает 400)
    assert bot.photos and bot.photos[0][0] == owner
    assert getattr(bot.photos[0][1], "path", None) == rows[0]["image_path"]


# ── реакции ────────────────────────────────────────────────────────────────────────────────

class _Emoji:
    def __init__(self, emoji):
        self.emoji = emoji


class _Reaction:
    """Апдейт message_reaction: ни текста, ни номера заказа в нём нет — только чат и номер
    сообщения. Ровно поэтому реакции работают по карте отправленных сообщений."""

    def __init__(self, *, uid=77, chat_id=None, message_id=500, emoji="❤️", old=(),
                 chat_type="private"):
        self.user = _User(uid)
        self.chat = type("C", (), {"id": uid if chat_id is None else chat_id, "type": chat_type})()
        self.message_id = message_id
        self.old_reaction = [_Emoji(e) for e in old]
        self.new_reaction = [_Emoji(emoji)] if emoji else []


async def _master_wrote(db, rid, text="готово, забирайте", tg_chat=77, tg_msg=500):
    """Сообщение мастера в треде, УЖЕ уехавшее клиенту в Telegram (связь записана)."""
    mid = await db.add_order_message(rid, from_admin=True, text=text, user_id=77)
    await db.link_tg_message(tg_chat, tg_msg, mid)
    return mid


async def test_client_reaction_becomes_a_reply_in_the_thread(tmp_db, owner):
    rid = await _order(tmp_db)
    mid = await _master_wrote(tmp_db, rid)
    bot = FakeBot()

    await chatreply.on_reaction(_Reaction(uid=77, message_id=500, emoji="❤️"), bot)

    rows = await tmp_db.list_order_messages(rid)
    assert [(r["text"], r["from_admin"], r["reply_to"]) for r in rows][-1] == ("❤️", 0, mid)
    assert bot.messages[0][0] == owner                       # команде — как обычное сообщение
    assert bot.texts[0].startswith(f"💬 Сообщение по заказу #{rid} от Аня:")
    assert "❤️" in bot.texts[0]
    # реакция — это тоже «увидел»: непрочитанное мастера гаснет
    assert await tmp_db.orders_unread_user(77) == {}


async def test_same_reaction_twice_does_not_double(tmp_db, owner):
    """Реакция снимается и ставится одним тапом — апдейт приходит на каждый."""
    rid = await _order(tmp_db)
    await _master_wrote(tmp_db, rid)
    for _ in range(3):
        await chatreply.on_reaction(_Reaction(uid=77, message_id=500, emoji="❤️"), FakeBot())
    texts = [r["text"] for r in await tmp_db.list_order_messages(rid)]
    assert texts.count("❤️") == 1


async def test_removed_reaction_is_ignored(tmp_db, owner):
    rid = await _order(tmp_db)
    await _master_wrote(tmp_db, rid)
    bot = FakeBot()
    await chatreply.on_reaction(_Reaction(uid=77, message_id=500, emoji="", old=("❤️",)), bot)
    assert [r["text"] for r in await tmp_db.list_order_messages(rid)] == ["готово, забирайте"]
    assert bot.messages == []


async def test_only_added_emoji_goes_to_the_chat(tmp_db, owner):
    """Премиум ставит несколько реакций сразу: в переписку едет ТО, ЧТО ДОБАВИЛИ, а не весь
    список заново."""
    rid = await _order(tmp_db)
    await _master_wrote(tmp_db, rid)
    await chatreply.on_reaction(
        _Reaction(uid=77, message_id=500, emoji="🔥", old=("❤️",)), FakeBot())
    assert [r["text"] for r in await tmp_db.list_order_messages(rid)][-1] == "🔥"


async def test_staff_reaction_reaches_the_client(tmp_db, owner):
    """Обратная сторона: мастер ставит 👍 на сообщение клиента в СВОЁМ чате с ботом."""
    rid = await _order(tmp_db)
    mid = await tmp_db.add_order_message(rid, from_admin=False, text="спасибо!", user_id=77)
    await tmp_db.link_tg_message(owner, 700, mid)            # уведомление уехало владельцу
    bot = FakeBot()

    await chatreply.on_reaction(_Reaction(uid=owner, message_id=700, emoji="👍"), bot)

    rows = await tmp_db.list_order_messages(rid)
    assert [(r["text"], r["from_admin"], r["reply_to"]) for r in rows][-1] == ("👍", 1, mid)
    assert bot.messages[0][0] == 77                          # клиенту
    assert await tmp_db.orders_unread_admin() == {}


async def test_stranger_reaction_is_ignored(tmp_db, owner):
    """Связь нашлась, но реагирующий — ни клиент этого заказа, ни сотрудник."""
    rid = await _order(tmp_db)
    await _master_wrote(tmp_db, rid, tg_chat=12345, tg_msg=500)
    bot = FakeBot()
    await chatreply.on_reaction(_Reaction(uid=12345, message_id=500, emoji="❤️"), bot)
    assert len(await tmp_db.list_order_messages(rid)) == 1
    assert bot.messages == []


async def test_reaction_on_unlinked_message_is_ignored(tmp_db, owner):
    """Сообщение отправлено до появления карты: угадывать заказ нечем — молчим."""
    rid = await _order(tmp_db)
    await _master_wrote(tmp_db, rid)
    bot = FakeBot()
    await chatreply.on_reaction(_Reaction(uid=77, message_id=4242, emoji="❤️"), bot)
    assert len(await tmp_db.list_order_messages(rid)) == 1
    assert bot.messages == []


async def test_reaction_subscription_is_registered():
    """Telegram шлёт message_reaction, только если тип апдейта в allowed_updates, а список
    aiogram собирает по ЗАРЕГИСТРИРОВАННЫМ хендлерам. Уедет хендлер — тихо отключится и
    подписка, без единой ошибки в логе."""
    dp = run.build_dispatcher()
    for r in list(dp.sub_routers):
        r._parent_router = None
    assert "message_reaction" in dp.resolve_used_update_types()


# ── ответ реплаем становится настоящим ответом ─────────────────────────────────────────────

async def test_reply_to_linked_message_is_a_real_reply(tmp_db, owner):
    """На сообщение, про которое известно, каким оно уехало, ответ привязывается точно —
    без разбора текста цитаты."""
    rid = await _order(tmp_db)
    mid = await _master_wrote(tmp_db, rid, tg_msg=501)
    msg = _Msg("да, подойдёт", quoted=_Quoted("💬 Сообщение по заказу #999:\nчужой текст",
                                              message_id=501), bot=FakeBot())

    assert await _route(msg) == orderchat.Target(rid, 77, False)

    last = (await tmp_db.list_order_messages(rid))[-1]
    assert (last["text"], last["reply_to"]) == ("да, подойдёт", mid)
    # заказ взят ИЗ СВЯЗИ, а не из текста цитаты — номер #999 в ней ни на что не влияет
    assert last["reply_text"] == "готово, забирайте"


async def test_app_message_records_the_link(tmp_db, owner, web_bot, monkeypatch, post_req, body):
    """Связь пишется и для сообщений, отправленных ИЗ ПРИЛОЖЕНИЯ, — иначе реакция на них
    (самый частый случай) не находила бы заказ."""
    from conftest import stub_auth
    stub_auth(monkeypatch, _verify_init_data=owner)
    rid = await _order(tmp_db)

    resp = await webapp._api_admin_order_message(
        post_req({"initData": "adm", "id": rid, "text": "готово"}))
    assert body(resp)["ok"] is True

    link = await tmp_db.tg_message_link(77, 1)     # FakeBot нумерует отправленные с единицы
    assert link and link["request_id"] == rid and link["from_admin"] == 1


# ── что видит фронт ────────────────────────────────────────────────────────────────────────

async def test_msgs_json_carries_quote_and_read_mark(tmp_db, owner):
    rid = await _order(tmp_db)
    mid = await tmp_db.add_order_message(rid, from_admin=True, text="какой цвет?", user_id=77)
    await tmp_db.add_order_message(rid, from_admin=False, text="❤️", user_id=77, reply_to=mid)

    view = webapp._msgs_json(await tmp_db.list_order_messages(rid), mine_is_admin=True)
    assert view[0]["mine"] is True and view[0]["read"] is False      # клиент ещё не открывал
    assert view[1]["reply"] == {"id": mid, "text": "какой цвет?", "image": False, "mine": True}

    await tmp_db.mark_order_read(rid, by_admin=False)                 # клиент открыл переписку
    view = webapp._msgs_json(await tmp_db.list_order_messages(rid), mine_is_admin=True)
    assert view[0]["read"] is True
    # у клиента «своё» — другое, и цитата мастера для него чужая
    mine = webapp._msgs_json(await tmp_db.list_order_messages(rid), mine_is_admin=False)
    assert mine[1]["mine"] is True and mine[1]["reply"]["mine"] is False


# ── порядок роутеров ───────────────────────────────────────────────────────────────────────

async def test_aiogram_wiring_passes_target_to_the_handler(tmp_db, owner):
    """Стыковка с aiogram, а не с нашими самоделками: адресат приезжает в хендлер ИЗ ФИЛЬТРА.

    Прямой вызов on_reply(msg, target) в тестах выше проходит мимо этого шва — а он и есть
    самое хрупкое место: фильтр возвращает словарь, и связывает его с аргументом хендлера
    aiogram, по СИГНАТУРЕ. Переименуйте аргумент — в бою будет TypeError на каждом реплае,
    а все тесты выше останутся зелёными."""
    rid = await _order(tmp_db)
    msg = _Msg("проверка стыковки",
               quoted=_Quoted(f"💬 Сообщение по заказу #{rid}:\nвопрос"), bot=FakeBot())

    handler = chatreply.router.message.handlers[0]
    ok, kwargs = await handler.check(msg)
    assert ok and kwargs.get("target") == orderchat.Target(rid, 77, False)
    await handler.call(msg, **kwargs)          # так его позовёт диспетчер
    assert len(await tmp_db.list_order_messages(rid)) == 1


def test_router_included_before_fallbacks():
    """Роутер обязан стоять до common и user_flow: catch-all фолбэк user_flow (StateFilter(None))
    иначе съедает реплай и отвечает «Нажмите /start» — ровно то, из-за чего фичу и просили.
    Смотрим СОБРАННЫЙ диспетчер, а не текст run.py (как в tests/test_payments.py)."""
    dp = run.build_dispatcher()
    subs = list(dp.sub_routers)
    for r in subs:
        r._parent_router = None
    assert subs.index(chatreply.router) < subs.index(common.router)
    assert subs.index(chatreply.router) < subs.index(user_flow.router)
