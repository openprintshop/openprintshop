"""Кому уходят уведомления мастерской (11.08).

До этого адресата не было вовсе: notify_admins всегда шёл по config.ADMIN_IDS — то есть
только по КОРНЕВЫМ владельцам из .env. Приглашённый оператор не получал в Telegram ничего:
ни нового заказа, ни отвалившегося принтера. Он узнавал о событиях, только если у него была
открыта настольная админка. Мастерская нанимает оператора ровно затем, чтобы владельца
перестали дёргать по ночам, — и как раз этого сборка не давала.

Вторая половина задачи важнее первой: через тот же канал идут ДЕНЬГИ — оплаты, балансы,
итоги корзины, выручка. Роль оператора построена на том, что он их не видит (ROLE_CAPS даёт
ему только заказы и принтер). Поэтому здесь проверяется не только «оператор получил», но и
«оператор НЕ получил цену» — иначе роли обходились бы через Telegram.
"""
import pytest

from app import config, notify, texts

OWNER_ENV = 100         # корневой владелец из .env
OWNER_INVITED = 200     # приглашённый владелец (живёт в staff)
OPERATOR = 300


class _Rec:
    """Бот, который только запоминает, кому и что ушло."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((int(chat_id), text))

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        self.sent.append((int(chat_id), caption or ""))


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [OWNER_ENV])
    monkeypatch.setattr(config, "STAFF_ROLES",
                        {OWNER_INVITED: config.ROLE_OWNER, OPERATOR: config.ROLE_OPERATOR})
    return _Rec()


def test_recipients_split_by_role(team):
    assert notify.recipients() == [OWNER_ENV, OWNER_INVITED]
    assert notify.recipients(notify.ALL_STAFF) == [OWNER_ENV, OWNER_INVITED, OPERATOR]


async def test_default_audience_is_owners_only(team):
    """Умолчание обязано молчать в сторону оператора: забытый вызов не должен утечь
    выручкой. Все денежные уведомления (оплаты, балансы, итоги) идут именно так."""
    await notify.notify_admins(team, "💳 Заказ #7 оплачен онлайн: 1 500 ₽")
    assert [uid for uid, _ in team.sent] == [OWNER_ENV, OWNER_INVITED]


async def test_all_staff_reaches_operator(team):
    await notify.notify_admins(team, "🖨 Печать завершена", audience=notify.ALL_STAFF)
    assert [uid for uid, _ in team.sent] == [OWNER_ENV, OWNER_INVITED, OPERATOR]


async def test_operator_gets_the_money_free_variant(team):
    """Событие операционное, текст денежный → оператору уходит урезанный вариант.

    Проверка по РОЛИ, а не по «не из .env»: приглашённый владелец деньги видеть вправе."""
    await notify.notify_admins(team, "полный текст с ценой 1 500 ₽",
                               audience=notify.ALL_STAFF, operator_text="текст без цены")
    got = dict(team.sent)
    assert got[OWNER_ENV] == "полный текст с ценой 1 500 ₽"
    assert got[OWNER_INVITED] == "полный текст с ценой 1 500 ₽"
    assert got[OPERATOR] == "текст без цены"


async def test_alert_defaults_to_owners(team, monkeypatch):
    """Алерты — это стектрейсы, сбои оплаты и телеметрия: не операторское чтение.
    Исключение ровно одно (принтер пропал во время печати) и оно просит audience явно."""
    monkeypatch.setattr(notify, "_bot", team)
    monkeypatch.setattr(notify, "_last_alert", {})
    await notify.alert("Сбой веб-API", key="k1")
    assert [uid for uid, _ in team.sent] == [OWNER_ENV, OWNER_INVITED]

    team.sent.clear()
    await notify.alert("Принтер пропал со связи", key="k2", audience=notify.ALL_STAFF)
    assert [uid for uid, _ in team.sent] == [OWNER_ENV, OWNER_INVITED, OPERATOR]


class _Req:
    """Минимальная заявка для текста уведомления о новом заказе."""
    id = 42
    user_id = 777
    user_name = "vasya"
    model_label = "Ваза"
    quantity = 2
    plastic_name = "PLA"
    price = 1500.0
    description = ""
    is_custom = 0
    rating = None


def test_operator_new_order_text_has_no_money():
    """Сам текст для оператора не должен содержать цену — иначе operator_text бесполезен."""
    full = texts.admin_new_order(_Req(), "RUB")
    short = texts.admin_new_order_operator(_Req())
    assert "1 500" in full.replace(" ", " ") or "1500" in full
    assert "1500" not in short.replace(" ", "").replace(" ", "")
    assert "💰" not in short
    assert "Ваза" in short and "#42" in short and "PLA" in short


async def test_exclude_still_works(team):
    """Старое поведение exclude (не слать инициатору) адресность ломать не должна."""
    await notify.notify_admins(team, "текст", audience=notify.ALL_STAFF, exclude=OWNER_ENV)
    assert [uid for uid, _ in team.sent] == [OWNER_INVITED, OPERATOR]
