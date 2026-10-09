"""Экранирование клиентских строк в уведомлениях мастеру и клиенту (веб-слой).

Бот отправляет с parse_mode=HTML, поэтому любой «<» в клиентском тексте или в имени модели
делает разметку битой: Telegram отвечает 400 «can't parse entities», а `except` вокруг
отправки глушит сбой в отладочный лог — сообщение клиента молча не доходит до мастера
(живой баг: «<3» в чате заказа). Четыре ручки подставляют такие строки в текст уведомления,
и каждая обязана гнать значение через texts.esc().

Проверяем ровно две вещи на ручку:
  • подставленное значение приехало ЭКРАНИРОВАННЫМ (&lt;, а не сырой «<»);
  • статика сообщения (эмодзи, слова, «», ×, —) осталась КАК ЕСТЬ — экранируй её заодно,
    и мастер увидел бы служебные последовательности вместо текста, а разметка в других
    уведомлениях (<code>, <b>) отобразилась бы буквально.
Поэтому сверяем текст целиком, а не только наличие подстроки.
"""
import types

import pytest
from conftest import stub_auth

from app import calcservice, config, orderchat, pricing, webapp


@pytest.fixture
def admins(monkeypatch):
    """Один админ-получатель уведомлений; он же — админ для админских ручек."""
    monkeypatch.setattr(config, "ADMIN_IDS", [900], raising=False)
    return 900


async def _order(db, *, label="Кулон <3", user_name="Клиент", status="new", user_id=77):
    return await db.add_request(
        user_id=user_id, user_name=user_name, model_label=label, printer="ECC2",
        plastic_name="PLA", quantity=1, quality="Стандарт", strength="Средняя",
        grams_total=17, minutes_total=100, price=12.34, preview_path=None, status=status)


# ---- клиент отменил заказ (уведомление мастеру) ----

async def test_order_cancel_escapes_model_label(tmp_db, admins, web_bot, monkeypatch, post_req,
                                                body):
    """Имя модели с «<» приходит из имени файла клиента — экранируется, иначе мастер вообще
    не узнает об отмене."""
    stub_auth(monkeypatch, _verify_init_data=lambda _d: 77)
    rid = await _order(tmp_db, label="Кулон <3")

    resp = await webapp._api_order_cancel(post_req({"initData": "x", "order_id": rid}))
    assert body(resp)["ok"] is True

    assert len(web_bot.messages) == 1, "уведомление мастеру не ушло вовсе"
    text = web_bot.texts[0]
    assert "«Кулон <3»" not in text                  # сырой «<» = 400 от Telegram
    assert text == f"🗑 Клиент отменил заказ #{rid} «Кулон &lt;3»."


# ---- клиент изменил параметры заказа (уведомление мастеру) ----

async def test_order_edit_escapes_model_label(tmp_db, admins, web_bot, monkeypatch, post_req,
                                              body):
    """Пересчёт подменён: тест про текст уведомления, а не про слайсер — настоящий
    recompute_request требует файла модели на диске."""
    stub_auth(monkeypatch, _verify_init_data=lambda _d: 77)
    webapp.ratelimit._hits.clear()
    rid = await _order(tmp_db, label="Держатель <A1>")

    bd = pricing.PriceBreakdown(
        qty=2, grams_unit=8.5, minutes_unit=50.0, grams_total=17.0, minutes_total=100.0,
        filament_cost=1.0, machine_cost=2.0, waste_cost=0.1, setup_fee=0.5, margin=1.0,
        discount_pct=0.0, discount_amount=0.0, subtotal=4.6, total=5.0, min_applied=False)

    async def _fake_recompute(_r, **_kw):
        # pdisp = «материал · цвет», и цвет приезжает из тела запроса без проверки
        # (webapp -> calcservice), то есть это ВТОРАЯ клиентская строка в этом уведомлении.
        return {"bd": bd, "settings": {}, "pdisp": "PLA · <3 розовый", "dims": "10×10×10",
                "preview_path": None, "qty": 2,
                "quality": types.SimpleNamespace(id=1, label="Стандарт"),
                "strength": types.SimpleNamespace(id=1, label="Средняя")}

    monkeypatch.setattr(calcservice, "recompute_request", _fake_recompute)

    resp = await webapp._api_order_edit(
        post_req({"initData": "x", "order_id": rid, "plastic_id": 1, "quality_id": 1,
                  "strength_id": 1, "qty": 2}))
    assert body(resp)["ok"] is True

    assert len(web_bot.messages) == 1, "уведомление мастеру не ушло вовсе"
    text = web_bot.texts[0]
    assert "<A1>" not in text
    assert "<3" not in text                          # цвет из тела запроса — тоже клиентская строка
    money = webapp.texts.fmt_money(5.0, config.CURRENCY)
    assert text == (f"✏️ Клиент изменил параметры заказа #{rid} «Держатель &lt;A1&gt;»: "
                    f"PLA · &lt;3 розовый, ×2 — новая цена {money}.")


# ---- клиент написал в чат заказа (уведомление мастеру) ----

async def test_order_message_escapes_client_text_and_name(tmp_db, admins, web_bot, monkeypatch,
                                                          post_req, body):
    """Ровно тот случай, ради которого всё затевалось: «<3» от клиента. Экранируются ОБА
    подставленных значения — и текст, и имя клиента (оно тоже из Telegram, не наше)."""
    stub_auth(monkeypatch, _verify_init_data=lambda _d: 77)
    rid = await _order(tmp_db, user_name="Аня <3")

    resp = await webapp._api_order_message(
        post_req({"initData": "x", "order_id": rid, "text": "<3 сделайте розовым"}))
    assert body(resp)["ok"] is True

    assert len(web_bot.messages) == 1, "уведомление мастеру не ушло вовсе"
    text = web_bot.texts[0]
    assert "<3" not in text                          # ни в тексте, ни в имени
    # Хвост — подсказка про ответ реплаем (app/orderchat.py). Она приклеивается к ПЕРВОМУ
    # сообщению стороны, а здесь заказ свежий, значит оно первое. Сама подсказка берётся
    # константой: проверяем экранирование, а не формулировку.
    assert text == (f"💬 Сообщение по заказу #{rid} от Аня &lt;3:"
                    f"\n&lt;3 сделайте розовым"
                    f"\n\n{orderchat.HINT_TO_STAFF}")

    # в базе сообщение лежит СЫРЫМ: экранирование — свойство транспорта Telegram, а не хранения
    rows = await tmp_db.list_order_messages(rid)
    assert [r["text"] for r in rows] == ["<3 сделайте розовым"]


# ---- мастер написал клиенту в чат заказа (уведомление клиенту) ----

async def test_admin_order_message_escapes_admin_text(tmp_db, admins, web_bot, monkeypatch,
                                                      post_req, body):
    """Обратное направление: текст мастера едет клиенту тем же parse_mode=HTML. Мастер
    пишет «печатаем при <220 °C» — без escape клиент не получит ничего."""
    stub_auth(monkeypatch, _verify_init_data=900)   # админ
    rid = await _order(tmp_db)

    resp = await webapp._api_admin_order_message(
        post_req({"initData": "adm", "id": rid, "text": "печатаем при <220 °C"}))
    assert body(resp)["ok"] is True

    assert len(web_bot.messages) == 1, "уведомление клиенту не ушло вовсе"
    chat_id, text, _ = web_bot.messages[0]
    assert chat_id == 77                             # клиенту заказа, а не админу
    assert "<220" not in text
    assert text == (f"💬 Сообщение по заказу #{rid}:\nпечатаем при &lt;220 °C"
                    f"\n\n{orderchat.HINT_TO_CLIENT}")
