"""Шина событий настольной админки: зеркало админских уведомлений для Windows-тостов.

Важное свойство — зеркало НЕ должно мешать отправке в Telegram: она первична, тосты
вторичны. Плюс курсор `since` должен делать опрос идемпотентным, иначе перезапуск
оболочки вываливал бы на мастера пачку старых тостов.
"""
import json

import pytest
from conftest import FakeRequest

from app import config, deskevents, notify, roles, webapp


class _FakeRequest:
    """Host — как у оболочки настольной админки (desktop_admin/shell.py:37 ходит на
    http://127.0.0.1:8081/api/admin/events): предикат локальности проверяет и его тоже."""

    def __init__(self, query=None, remote="127.0.0.1", host="127.0.0.1:8081", headers=None):
        self.query = query or {}
        self.remote = remote
        self.host = host
        self.headers = headers or {}


class _Btn:
    def __init__(self, url):
        self.web_app = type('WA', (), {'url': url})()
        self.url = None


class _Markup:
    def __init__(self, url):
        self.inline_keyboard = [[_Btn(url)]]


class _Bot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, uid, text, reply_markup=None):
        if self.fail:
            raise RuntimeError('телеграм недоступен')
        self.sent.append((uid, text))

    async def send_photo(self, uid, photo, caption=None, reply_markup=None):
        self.sent.append((uid, caption))


@pytest.fixture(autouse=True)
def _clean():
    deskevents.reset()
    yield
    deskevents.reset()


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "tok", raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    webapp._REQ_IS_LOCAL.set(True)
    return "tok"


def _json(resp):
    return json.loads(resp.body.decode())


def test_push_and_since_cursor():
    """Курсор отдаёт только новое — иначе тосты повторялись бы на каждом опросе."""
    a = deskevents.push('первое')
    b = deskevents.push('второе')
    assert [e["text"] for e in deskevents.since(0)] == ['первое', 'второе']
    assert [e["text"] for e in deskevents.since(a)] == ['второе']
    assert deskevents.since(b) == []
    assert deskevents.last_id() == b


def test_target_parsed_from_telegram_button():
    """Цель перехода берём из кнопки уведомления — чтобы не править ~27 мест вызова."""
    deskevents.push_from_telegram(
        'Новый заказ', reply_markup=_Markup('https://x/app/?v=1&go=adminorder&id=42'))
    ev = deskevents.since(0)[0]
    assert ev["go"] == 'adminorder' and ev["go_id"] == 42


def test_no_button_means_no_navigation():
    deskevents.push_from_telegram('Просто текст', reply_markup=None)
    ev = deskevents.since(0)[0]
    assert ev["go"] == '' and ev["go_id"] == 0


def test_html_markup_stripped():
    """Телеграм шлёт HTML-разметку; в Windows-тосте теги показались бы буквально."""
    deskevents.push('<b>Запущена ТЕСТОВАЯ версия</b>\nканал: <code>test</code> &amp; ветка')
    assert deskevents.since(0)[0]["text"] == 'Запущена ТЕСТОВАЯ версия\nканал: test & ветка'


def test_buffer_is_bounded():
    """Кольцо не должно расти вечно: это всплывающие уведомления, а не история."""
    for i in range(deskevents._MAX + 50):
        deskevents.push(f'событие {i}')
    assert len(deskevents._events) == deskevents._MAX
    assert deskevents.since(0)[-1]["text"] == f'событие {deskevents._MAX + 49}'


async def test_notify_admins_mirrors_event(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    bot = _Bot()
    await notify.notify_admins(bot, 'Новый заказ #7',
                               reply_markup=_Markup('https://x/app/?go=adminorder&id=7'))
    assert bot.sent == [(999, 'Новый заказ #7')]
    ev = deskevents.since(0)[0]
    assert ev["text"] == 'Новый заказ #7' and ev["go_id"] == 7


async def test_telegram_failure_does_not_lose_event(monkeypatch):
    """Телеграм недоступен — событие всё равно в шине: мастер за компьютером его увидит."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    await notify.notify_admins(_Bot(fail=True), 'Принтер упал')
    assert [e["text"] for e in deskevents.since(0)] == ['Принтер упал']


async def test_mirror_failure_does_not_block_telegram(monkeypatch):
    """Обратное тоже верно: сломанное зеркало не должно рвать отправку в Telegram."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    monkeypatch.setattr(deskevents, "push_from_telegram",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('boom')))
    bot = _Bot()
    await notify.notify_admins(bot, 'Заказ')
    assert bot.sent == [(999, 'Заказ')]


async def test_alert_marked_as_alert(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    notify.set_bot(_Bot())
    notify._last_alert.clear()
    await notify.alert('Сбой слайсера', key='slicer')
    ev = deskevents.since(0)[0]
    assert ev["kind"] == 'alert' and 'Сбой слайсера' in ev["text"]


async def test_events_endpoint_first_call_returns_only_cursor(admin):
    """Первый запрос без since не должен вывалить накопленное: оболочка только что стартовала."""
    deskevents.push('старое событие')
    resp = await webapp._api_admin_events(_FakeRequest(query={"initData": admin}))
    d = _json(resp)
    assert d["events"] == [] and d["cursor"] == deskevents.last_id()


async def test_events_endpoint_returns_new_since_cursor(admin):
    deskevents.push('старое')
    cur = deskevents.last_id()
    deskevents.push('новое')
    resp = await webapp._api_admin_events(
        _FakeRequest(query={"initData": admin, "since": str(cur)}))
    assert [e["text"] for e in _json(resp)["events"]] == ['новое']


async def test_operator_variant_is_mirrored_into_the_bus(monkeypatch):
    """Зеркало берёт ОБА текста уведомления — денежный и урезанный.

    До личного входа выбирать было не из чего: тосты видел тот, кто сидит за компьютером, а
    это по определению был владелец. Теперь за деском конкретный человек, и оператор должен
    получить в тосте ровно то же, что получает в Telegram (notify_admins.operator_text)."""
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    await notify.notify_admins(_Bot(), 'Новый заказ #7\n💰 45.20 BYN',
                               audience=notify.ALL_STAFF, operator_text='Новый заказ #7')
    assert [e["text"] for e in deskevents.since(0)] == ['Новый заказ #7\n💰 45.20 BYN']
    assert [e["text"] for e in deskevents.since(0, money=False)] == ['Новый заказ #7']
    assert "text_op" not in deskevents.since(0)[0]      # внутренний ключ наружу не выпускаем


async def test_operator_toast_comes_without_the_price(admin):
    """Оператору лента отдаёт безденежный вариант — обходить роли через тост нельзя так же,
    как через Telegram. Права в запрос кладёт _caps_mw; здесь ставим их руками, потому что
    хендлер зовётся напрямую, без middleware."""
    deskevents.push('Новый заказ #7\n💰 45.20 BYN', operator_text='Новый заказ #7')
    req = FakeRequest(path="/api/admin/events", query={"initData": admin, "since": "0"})
    req["caps"] = frozenset({roles.CAP_ORDERS, roles.CAP_PRINTER})
    resp = await webapp._api_admin_events(req)
    assert [e["text"] for e in _json(resp)["events"]] == ['Новый заказ #7']
    assert "45.20" not in resp.body.decode("utf-8")


async def test_owner_toast_keeps_the_price(admin):
    """Владельцу — полный текст: ради цены в углу экрана тосты и заводили."""
    deskevents.push('Новый заказ #7\n💰 45.20 BYN', operator_text='Новый заказ #7')
    req = FakeRequest(path="/api/admin/events", query={"initData": admin, "since": "0"})
    req["caps"] = frozenset(roles.ALL_CAPS)
    assert [e["text"] for e in _json(await webapp._api_admin_events(req))["events"]] == [
        'Новый заказ #7\n💰 45.20 BYN']


async def test_event_without_operator_variant_goes_as_is(admin):
    """Урезанного варианта нет — значит в событии и денег нет (то же правило, что в
    notify_admins). Молча прятать такие тосты от оператора нельзя: «принтер встал» ему
    нужнее всех."""
    deskevents.push('Принтер встал')
    req = FakeRequest(path="/api/admin/events", query={"initData": admin, "since": "0"})
    req["caps"] = frozenset({roles.CAP_ORDERS, roles.CAP_PRINTER})
    assert [e["text"] for e in _json(await webapp._api_admin_events(req))["events"]] == [
        'Принтер встал']


async def test_events_endpoint_denied_through_tunnel(admin):
    """Зеркало админских уведомлений наружу не отдаётся — порт опубликован туннелем."""
    with pytest.raises(webapp.web.HTTPNotFound):
        await webapp._api_admin_events(_FakeRequest(
            query={"initData": admin, "since": "0"}, headers={"X-Forwarded-For": "203.0.113.7"}))
