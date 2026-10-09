"""Деньги заказа: статусы и счета, цена и пересчёт, инвойс, баланс, рефералка, расход.

Один предмет — всё, что двигает ДЕНЬГИ и СТАТУС заказа у мастера: смена статуса с
уведомлением клиенту, выставление счёта, отметка оплаты, назначение цены (с вопросом
«пересчитать доставку?»), пересчёт по фактическим граммам, скрин слайсера к счёту,
инвойс-документ, баланс клиента, себестоимость печати, реферальная награда и списание
расхода (заказ/журнал) со склада при подтверждении печати.

Выделено из app/webapp.py волной 16 endgame-плана — ПОСЛЕДНЯЯ волна полной программы.
_maybe_referral_reward включён сюда решением плана (лежал в щели между волнами 1c и 15;
его зовут и оставшийся в webapp confirm_print, и handlers/payments.py:88 — оба через
фасад webapp.*). Правила дома:
* модуль НЕ имеет права импортировать app.webapp (разрез односторонний; проверяется
  тестом в отдельном процессе). _CUSTOM_DIR — из web/products, указатель активной
  печати — из web/printerhub, дефолты доставки — из web/orderdefaults;
* бота ставит webapp.set_bot веером _BOT_FAN (волна 12, вариант «б»);
* регистрация маршрутов осталась в make_app строками на фасадные имена — инвариант
  NEIGHBOURS вокруг orderbench (test_orderbench_facade) не задет;
* тела перенесены ДОСЛОВНО (сверка против git show HEAD:app/webapp.py).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path

from aiohttp import web

from .. import (
    calcservice,
    config,
    db,
    delivery,
    elegoo,
    keyboards,
    moonraker,
    notify,
    payments,
    preview,
    pricing,
    texts,
    tuning,
)
from ..webauth import _admin_body
from .common import _bad, _num, _toint
from .orderdefaults import _apply_default_delivery, _delivery_note
from .printerhub import _active_print_key, _set_active_print
from .products import _CUSTOM_DIR

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


async def _maybe_referral_reward(user_id: int) -> None:
    """Рефералка (OpenTodo 7f257a33): после первого НАПЕЧАТАННОГО И ОПЛАЧЕННОГО заказа
    приглашённого — бонус на счёт обоим, один раз. Дёргается при смене статуса/оплаты."""
    try:
        amount = config.REF_REWARD_AMOUNT
        ref = await db.award_referral_if_due(user_id, amount)
        if ref and _bot is not None:
            cur = config.CURRENCY
            try:
                await notify.notify_user(
                    _bot, user_id, f"🎁 Реферальный бонус: +{amount:g} {cur} на ваш счёт — "
                                   f"ваш первый заказ напечатан и оплачен!")
            except Exception:  # noqa: BLE001
                pass
            try:
                await notify.notify_user(
                    _bot, ref, f"🎁 Ваш друг получил свой первый заказ — вам начислено "
                               f"+{amount:g} {cur} на счёт. Спасибо, что советуете нас!")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        log.debug("referral reward failed", exc_info=True)

def _sane_actual(est: float, actual: float) -> bool:
    return est > 0 and actual > 0 and 0.3 <= (actual / est) <= 3.0

def _telemetry_actual_minutes() -> float | None:
    """Фактическое время последней печати из кэша телеметрии принтера (сек→мин), если есть.

    ЧИТАТЬ ИЗ state. cached_status отдаёт {ok, serial, online, cached, connected, age, state},
    и print_status лежит ВНУТРИ state. Раньше он читался с верхнего уровня, то есть функция
    возвращала None ВСЕГДА: факт времени печати из телеметрии не собирался вовсе, оставался
    только ручной ввод мастера. А кормит эта пара ровно predict_minutes — коррекцию оценки
    времени, которая влияет на цену. Правильный образец рядом, в двух других местах файла."""
    try:
        st = elegoo.cached_status()
        dur = (((st or {}).get("state") or {}).get("print_status") or {}).get("print_duration")
        if isinstance(dur, (int, float)) and dur > 0:
            return float(dur) / 60.0
    except Exception:  # noqa: BLE001
        pass
    return None

async def _capture_print_sample(r, body) -> None:
    """Этап 0 сбора данных (docs/print-time-prediction.md): при переводе заказа в «напечатано»
    сохраняем пару (оценка слайсера → факт) на ЕДИНИЦУ. Факт — ручной ввод админа
    (actual_minutes), иначе телеметрия принтера. Цену не трогаем; всё best-effort."""
    try:
        qty = max(int(r.quantity or 1), 1)
        est = float(r.minutes_total or 0) / qty
        if est <= 0:
            return
        actual, source = None, "manual"
        raw = body.get("actual_minutes")
        if raw not in (None, ""):
            try:
                actual = float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                actual = None
        if actual is None:
            actual, source = _telemetry_actual_minutes(), "telemetry"
        if actual is None or not _sane_actual(est, actual):
            return                                  # нет факта или явный выброс — мусор не пишем
        # Материал — ТОЛЬКО справочный material_type, и подбором по имени-префиксу
        # (tuning.material_of): точное совпадение на боевых именах не срабатывает — в заказе
        # к имени приклеен цвет («Kingroon PETG · Хаки»), и прежний фолбэк на сырое
        # plastic_name засорял print_journal.material_type цветными синглтонами.
        mat = await tuning.material_of(r.plastic_name)
        grams_u = (float(r.grams_total or 0) / qty) or None
        mh = getattr(r, "hash", "") or ""
        ec = int(getattr(r, "multicolor", 0) or 0)
        await db.add_print_sample(                          # print_samples — бэкап (не читается)
            request_id=r.id, model_hash=mh, est_minutes=est, actual_minutes=actual,
            grams=grams_u, material_type=mat, extra_colors=ec, source=source)
        # print_events: замер в единый журнал (источник predict_minutes/bigdata_stats после слияния)
        await db.record_time_sample(r.id, est, actual, material_type=mat, grams=grams_u,
                                    model_hash=mh, extra_colors=ec)
    except Exception:  # noqa: BLE001
        log.debug("print sample capture failed", exc_info=True)

async def _accrue_service_on_print(r) -> None:
    """ТО (docs/maintenance-reminders.md, фаза 1): при «напечатано» начисляем наработку узлам
    принтера и шлём админам алерт по пунктам, которые этим начислением пересекли порог."""
    try:
        # Наработку начисляем ТОЙ машине, что печатала (заказ узнаёт её в момент печати).
        # None → основная: так было, пока принтер был один.
        crossed = await db.accrue_service(float(r.minutes_total or 0), int(r.quantity or 1),
                                          printer_id=getattr(r, "printer_id", None))
        if not crossed or _bot is None:
            return
        for it in crossed:
            await db.mark_service_notified(it.id, 1)
            # ТО принтера делает тот, кто у станка, — оператору это нужнее владельца.
            await notify.notify_admins(_bot, texts.service_due_alert(it),
                                       audience=notify.ALL_STAFF)
    except Exception:  # noqa: BLE001
        log.debug("service accrual failed", exc_info=True)

async def _consume_order_stock(r) -> None:
    """Списать зарезервированный заказом филамент при «напечатано» (OpenTodo a89586a2). Один раз
    (idempotent через requests.stock_deducted); алерты о низком остатке — как при оформлении."""
    try:
        for name, remaining in await db.apply_order_stock(r.id):
            await notify.alert(f"Низкий остаток филамента: «{name}» — осталось {remaining:.0f} г",
                               key=f"lowstock_{name}", throttle_sec=3600)
    except Exception:  # noqa: BLE001
        log.debug("consume order stock failed", exc_info=True)

async def _consume_journal_stock(jid: int) -> None:
    """Списать филамент печати ВНЕ ЗАКАЗА со склада её пластика при занесении/правке в журнале
    (OpenTodo 05806f15). Один раз (idempotent через print_journal.stock_deducted); печати с заказом
    пропускаются (за них списывает сам заказ). Алерты о низком остатке — как у заказа."""
    try:
        for name, remaining in await db.apply_journal_stock(jid):
            await notify.alert(f"Низкий остаток филамента: «{name}» — осталось {remaining:.0f} г",
                               key=f"lowstock_{name}", throttle_sec=3600)
    except Exception:  # noqa: BLE001
        log.debug("consume journal stock failed", exc_info=True)

async def _apply_price_or_ask(rid: int, r, new_price: float, extra_fields: dict,
                              comment: str | None, *, recalc: bool) -> str:
    """Общий хвост подорожания заказа: применить сразу или отправить на подтверждение.

    Клиент мог разрешить принимать подорожание автоматически (профиль → подтверждение цены,
    OpenTodo 0d11db78). Тогда идём тем же путём, что и ручное «Принять» в мини-аппе
    (_api_order_respond): цена применяется, заказ уходит «в работу», админам летит та же
    сводка — иначе мастер не понял бы, почему заказ вдруг сам двинулся.

    Возвращает 'accepted' (приняли за клиента) либо 'pending' (ждём клиента).
    """
    old_price = r.price or 0.0
    prefs = await db.get_user_prefs(r.user_id)
    auto = (not db.is_self_order(r)) and db.price_auto_accept(prefs, old_price, new_price)
    if auto:
        await db.update_request(rid, price=new_price, pending_price=None,
                                status="in_progress", price_comment=comment or None, **extra_fields)
        # клиент разрешил принимать подорожание сам → цена уже боевая, порог «бесплатно
        # от суммы» пересчитываем здесь же (волна 5)
        await delivery.recalc_group_delivery(rid)
        if _bot is not None:
            try:
                await notify.notify_user(
                    _bot, r.user_id,
                    texts.user_price_auto_note(r, old_price, new_price, config.CURRENCY, prefs),
                    reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders"))
            except Exception:  # noqa: BLE001
                log.debug("auto price notify failed", exc_info=True)
            try:
                await notify.notify_admins(
                    _bot, f"✅ Новая цена по заказу #{rid} "
                          f"({texts.fmt_money(new_price, config.CURRENCY)}) принята автоматически — "
                          f"клиент разрешил это в настройках.")
            except Exception:  # noqa: BLE001
                log.debug("auto price admin notify failed", exc_info=True)
        return "accepted"
    await db.update_request(rid, status="price_changed", pending_price=new_price,
                            price_comment=comment or None, **extra_fields)
    r2 = await db.get_request(rid)
    # Просить подтвердить новую цену имеет смысл только у настоящего клиента: по своему же
    # заказу мастер и выставил счёт, и «подтверждал» бы его сам себе.
    if r2 and _bot is not None and not db.is_self_order(r2):
        try:
            kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders") or keyboards.price_confirm(rid, new_price)
            await notify.notify_user(_bot, r2.user_id, texts.user_price_note(r2, config.CURRENCY),
                                     reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.debug("%s price notify failed", "recalc" if recalc else "admin", exc_info=True)
    return "pending"

async def _api_admin_order_status(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    status = (body.get("status") or "").strip()
    prev = await db.get_request(rid)        # старый статус — образец времени пишем один раз
    if prev and int(getattr(prev, "product_id", 0) or 0):
        # заказ ГОТОВОГО ИЗДЕЛИЯ: товар уже напечатан — только «Новый» и «Вручено»
        # (OpenTodo 26a44d79); печатных переходов (в работу/напечатано/отказ) у него нет
        # отложить можно и готовое изделие: «сейчас нет, но оставьте за мной»
        allowed = ("new", "handed", "deferred")
    else:
        allowed = ("new", "awaiting_payment", "in_progress", "printed", "rejected", "deferred")
    if status not in allowed:
        return web.json_response({"ok": False, "error": "Неизвестный статус"})
    fields = {"status": status}
    # «в работу» может нести срок исполнения «ДД.ММ» — попадёт в уведомление клиенту.
    # У «отложен» то же поле значит другое: когда ориентировочно к заказу вернёмся.
    if status in ("in_progress", "deferred") and (body.get("due") is not None):
        fields["due_date"] = db.parse_due_date(str(body.get("due"))) or ""
    # «отказ» может нести причину — для аналитики воронки (crm-roadmap этап 6)
    if status == "rejected" and (body.get("reason") is not None):
        fields["reject_reason"] = str(body.get("reason") or "").strip()[:200]
    await db.update_request(rid, **fields)
    r = await db.get_request(rid)
    if status == "printed" and r and (prev is None or prev.status != "printed"):
        await _capture_print_sample(r, body)
        # Этап 0 автотюнинга: замораживаем применённые ручки в строку журнала. Отдельным
        # вызовом, а НЕ внутри _capture_print_sample: тот выходит раньше, если факта времени
        # нет, — а печать без замера времени всё равно годный пример. Never-raise внутри.
        await tuning.capture_features(r)
        await _accrue_service_on_print(r)
        await _consume_order_stock(r)             # резерв → реальное списание склада (a89586a2)
        await _maybe_referral_reward(r.user_id)   # рефералка: напечатан (+оплачен?) → бонус
    if status == "handed" and r and (prev is None or prev.status != "handed"):
        await _maybe_referral_reward(r.user_id)   # вручённое готовое изделие тоже считается
    # Клиентское уведомление о статусе — только настоящему клиенту. Свой же заказ мастер
    # перевёл сам: писать ему «ваш заказ напечатан» и просить оценить самого себя незачем.
    if r and _bot is not None and not db.is_self_order(r):
        try:
            kb = (keyboards.rate_request(rid) if status in ("printed", "handed")
                  else keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders"))
            await notify.notify_user(_bot, r.user_id, texts.user_status_note(r, config.CURRENCY),
                                     reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.debug("admin status notify failed", exc_info=True)
    return web.json_response({"ok": True})

async def _api_admin_order_bill(request: web.Request) -> web.Response:
    """Выставить счёт по ОБЫЧНОМУ заказу — этап «Ждёт оплаты» (фидбэк 31.07). Мастер посмотрел
    модель и одобрил заказ (опционально уточнив цену) → статус awaiting_payment, клиенту уходит
    ОДНО сообщение-инвойс с кнопкой оплаты; контекст («мастер подтвердил», «цена уточнена:
    было → стало») вписан в описание счёта, отдельного уведомления нет. Оплата автоматически
    переведёт заказ «В работе» (payments.record_success); наличные — галочка «оплачен», она
    делает тот же переход (_api_admin_order_paid). Индивидуальные заказы сюда не ходят — у них
    свой счёт-расчёт с разбивкой (_api_admin_order_invoice)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    aid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    if int(getattr(r, "paid", 0) or 0):
        return web.json_response({"ok": False, "error": "Заказ уже оплачен"})
    if r.status not in ("new", "awaiting_payment"):
        return web.json_response(
            {"ok": False, "error": "Счёт выставляется по новому заказу "
                                   "(или повторно из «Ждёт оплаты»)"})
    if not await payments.enabled():
        return web.json_response(
            {"ok": False, "error": "Онлайн-оплата не настроена — впишите токен провайдера "
                                   "в настройках («Онлайн-оплата»)"})
    if _bot is None:
        return web.json_response({"ok": False, "error": "Бот недоступен"})
    cur = config.CURRENCY
    old_price = float(r.price or 0)
    note = "✅ Мастер подтвердил заказ"
    fields: dict = {}
    price = _num(body, "price", None)
    if price is not None:
        if price <= 0:
            return web.json_response({"ok": False, "error": "Некорректная цена"})
        if abs(price - old_price) > 0.005:
            # смена цены и счёт нарочно в одном действии и одном сообщении (фидбэк 31.07)
            fields["price"] = round(price, 2)
            note = (f"✅ Мастер подтвердил заказ. Цена уточнена: "
                    f"{texts.fmt_money(old_price, cur)} → {texts.fmt_money(price, cur)}")
    total = fields.get("price", old_price)
    if payments.amount_minor(total) < payments.MIN_AMOUNT_MINOR:
        return web.json_response({"ok": False, "error": "Сумма меньше минимума провайдера"})
    if r.status != "awaiting_payment":   # повторное выставление не дублирует таймлайн
        fields["status"] = "awaiting_payment"
    if fields:
        await db.update_request(rid, **fields)
    # Порог «бесплатно от суммы» (волна 5): цена заказа только что стала известной/другой —
    # доставка обязана пересчитаться ДО счёта, иначе счёт унесёт устаревшую сумму.
    drec = await delivery.recalc_group_delivery(rid)
    r2 = await db.get_request(rid)
    if not await payments.send_invoice(_bot, r2, note=note):
        # счёт не ушёл (кривой токен/валюта — мастеру уже улетел алерт) — не бросаем клиента
        # в «Ждёт оплаты» без счёта, откатываем ровно то, что меняли
        rollback = {}
        if "status" in fields:
            rollback["status"] = r.status
        if "price" in fields:
            rollback["price"] = old_price
        if rollback:
            await db.update_request(rid, **rollback)
        if drec:      # цена откатилась — доставка должна вернуться к старому порогу вместе с ней
            await delivery.recalc_group_delivery(rid)
        return web.json_response(
            {"ok": False, "error": "Telegram не принял счёт — проверьте токен провайдера "
                                   "и валюту в настройках «Онлайн-оплата»"})
    await db.log_event(aid, "выставил счёт", f"#{rid} {texts.fmt_money(total, cur)}")
    resp = {"ok": True, "price": total, "status": "awaiting_payment"}
    if drec:
        resp["delivery"] = drec
    return web.json_response(resp)

async def _api_admin_order_paid(request: web.Request) -> web.Response:
    """Галочка «оплачен» у заказа (OpenTodo 7f257a33). Смена оплаты может включить
    реферальный бонус (первый напечатанный И оплаченный заказ приглашённого)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    paid = 1 if body.get("paid") else 0
    # Ручная галочка мастера (нал/перевод). Снятие галочки обнуляет и способ/дату —
    # но онлайн-оплату руками «разоплатить» нельзя: деньги реально получены (леджер payments).
    if not paid and getattr(r, "pay_method", "") == "online":
        return web.json_response(
            {"ok": False, "error": "Заказ оплачен онлайн — снять отметку нельзя. "
                                   "Возврат делается в кабинете провайдера."})
    fields = {"paid": paid, "paid_at": int(time.time()) if paid else 0,
              "pay_method": "manual" if paid else ""}
    # Этап «Ждёт оплаты»: клиент рассчитался наличными — галочка делает тот же автопереход
    # «В работе», что и онлайн-оплата (payments.record_success). Единое поведение этапа.
    if paid and r.status == "awaiting_payment":
        fields["status"] = "in_progress"
    await db.update_request(rid, **fields)
    if paid:
        await _maybe_referral_reward(r.user_id)
    return web.json_response({"ok": True, "paid": paid,
                              "status": fields.get("status", r.status)})

async def _api_admin_order_price(request: web.Request) -> web.Response:
    """Смена цены заказа. Обычный режим — цена вписывается руками (body.price). OpenTodo 8c1cab08:
    альтернативный режим — граммы+минуты (body.grams/body.minutes, часы уже свёрнуты фронтом в
    минуты) — цена считается формулой ручного калькулятора (compute_price_manual) по пластику и
    тиражу заказа через calcservice.reprice_request; дальше — ТОТ ЖЕ путь применения, что и у
    ручной цены (дешевле → сразу + уведомление; дороже/равно → на подтверждение клиенту)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    comment = (body.get("comment") or "").strip()
    # режим «по граммам и времени» — оба поля присутствуют (не None/пустая строка)
    recalced = body.get("grams") not in (None, "") and body.get("minutes") not in (None, "")
    extra_fields: dict = {}
    if recalced:
        try:
            grams = float(str(body.get("grams")).replace(",", "."))
            minutes = float(str(body.get("minutes")).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Некорректные граммы/время"})
        if grams <= 0 or minutes < 0:
            return web.json_response({"ok": False, "error": "Граммы должны быть больше нуля, время — не меньше нуля"})
        try:
            res = await calcservice.reprice_request(r, grams, minutes)
        except calcservice.CalcError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        bd = res["bd"]
        price = bd.total
        extra_fields = {"grams_total": bd.grams_total, "minutes_total": bd.minutes_total,
                        "breakdown": pricing.breakdown_to_dict(bd, res["settings"])}
    else:
        try:
            price = float(str(body.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Некорректная цена"})
    old_price = r.price or 0.0
    # Решаем по ИТОГУ, а не по цене позиции. Порог «бесплатно от суммы» (волна 5) двигает
    # доставку следом за ценой, и снижение цены может УВЕЛИЧИТЬ сумму к оплате: 102 → 99
    # за позицию, но доставка 0 → 6, итого 102 → 105. По цене позиции это выглядело бы как
    # «стало дешевле», применялось бы без спроса, и клиент получил бы «🎉» вместе со счётом
    # больше прежнего. Подорожание по итогу обязано идти через подтверждение, как любое другое.
    _dprev = await delivery.preview_group_delivery(rid, price)
    _ddelta = round(_dprev["new"] - _dprev["old"], 2) if _dprev else 0.0
    if (price - old_price) + _ddelta < -1e-6:            # итог снизился → применяем сразу + уведомляем
        await db.update_request(rid, price=price, pending_price=None,
                                price_comment=comment or None, **extra_fields)
        drec = await delivery.recalc_group_delivery(rid)
        if _bot is not None and not db.is_self_order(r):   # свой заказ — цену себе не анонсируем
            try:
                kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders")
                await notify.notify_user(_bot, r.user_id,
                                         texts.user_recalc_note(r, old_price, price, config.CURRENCY),
                                         reply_markup=kb)
            except Exception:  # noqa: BLE001
                log.debug("admin price notify failed", exc_info=True)
        out: dict = {"ok": True, "applied": "direct", "price": price}
        if drec:
            out["delivery"] = drec
        return web.json_response(out)
    # дороже/равно → подтверждение клиентом (или автоприём, если он его разрешил)
    outcome = await _apply_price_or_ask(rid, r, price, extra_fields, comment, recalc=False)
    resp: dict = {"ok": True}
    if recalced:                                          # режим по граммам/времени — вернуть посчитанную цену
        resp["applied"] = "accepted" if outcome == "accepted" else "pending"
        resp["price"] = price
    return web.json_response(resp)

async def _api_admin_order_recalc(request: web.Request) -> web.Response:
    """Пересчитать заказ (повторный слайс + цена) и вернуть старое/новое БЕЗ сохранения."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    try:
        res = await calcservice.recalc_request(r)
    except calcservice.CalcError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("recalc failed")
        return web.json_response({"ok": False, "error": f"Внутренняя ошибка: {exc}"})
    bd = res["bd"]
    return web.json_response({"ok": True,
        "old": {"price": r.price or 0.0, "grams": r.grams_total or 0.0, "minutes": r.minutes_total or 0.0},
        "new": {"price": bd.total, "grams": bd.grams_total, "minutes": bd.minutes_total}})

async def _api_admin_order_recalc_apply(request: web.Request) -> web.Response:
    """Применить пересчёт. Граммы/время — итоговые, могут быть поправлены админом. dry=true →
    только пересчитать цену (предпросмотр), без сохранения. Иначе: дешевле/равно — применяем
    сразу и уведомляем клиента; дороже — уходит в обычный поток подтверждения (pending_price)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    try:
        grams = float(str(body.get("grams")).replace(",", "."))
        minutes = float(str(body.get("minutes")).replace(",", "."))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректные граммы/время"})
    if grams <= 0 or minutes <= 0:
        return web.json_response({"ok": False, "error": "Граммы и время должны быть больше нуля"})
    try:
        res = await calcservice.reprice_request(r, grams, minutes)
    except calcservice.CalcError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    bd = res["bd"]
    new_price = bd.total
    if body.get("dry"):                                  # предпросмотр цены при правке полей
        return web.json_response({"ok": True, "price": new_price})
    bd_dict = pricing.breakdown_to_dict(bd, res["settings"])
    old_price = r.price or 0.0
    # Как и в _api_admin_order_price: решаем по ИТОГУ. Доставка едет следом за ценой через
    # порог «бесплатно от суммы», и снижение цены позиции способно поднять сумму к оплате.
    _dprev = await delivery.preview_group_delivery(rid, new_price)
    _ddelta = round(_dprev["new"] - _dprev["old"], 2) if _dprev else 0.0
    if (new_price - old_price) + _ddelta <= 1e-6:       # итог дешевле/равен → сразу + уведомляем
        await db.update_request(rid, price=new_price, pending_price=None,
                                grams_total=bd.grams_total, minutes_total=bd.minutes_total,
                                breakdown=bd_dict)
        drec = await delivery.recalc_group_delivery(rid)   # порог «бесплатно от суммы» (волна 5)
        if _bot is not None and not db.is_self_order(r):   # свой заказ — пересчёт себе не шлём
            try:
                kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders")
                await notify.notify_user(_bot, r.user_id,
                                         texts.user_recalc_note(r, old_price, new_price, config.CURRENCY),
                                         reply_markup=kb)
            except Exception:  # noqa: BLE001
                log.debug("recalc notify failed", exc_info=True)
        out: dict = {"ok": True, "applied": "direct", "price": new_price}
        if drec:
            out["delivery"] = drec
        return web.json_response(out)
    # дороже → подтверждение клиентом (или автоприём, если он его разрешил)
    outcome = await _apply_price_or_ask(
        rid, r, new_price,
        {"grams_total": bd.grams_total, "minutes_total": bd.minutes_total, "breakdown": bd_dict},
        "Пересчёт по обновлённому слайсеру", recalc=True)
    return web.json_response({"ok": True, "applied": outcome, "price": new_price})

async def _save_slicer_shot(rid: int, data: str) -> str | None:
    """Сохранить приложенный к счёту скрин из слайсера (base64 dataURL, OpenTodo 6aaa6a19).
    Best-effort: при любой ошибке возвращаем None, чтобы не сломать выставление счёта."""
    if not data:
        return None
    if "," in data and data[:5].lower() == "data:":
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:  # noqa: BLE001
        raw = b""
    if not raw or len(raw) > 12 * 1024 * 1024:
        return None
    try:
        _CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        dest = _CUSTOM_DIR / f"slicer_{rid}.jpg"
        dest.write_bytes(raw)
        await asyncio.to_thread(preview.optimize_photo, dest)   # ресайз+сжатие вне event loop
        return str(dest)
    except Exception:  # noqa: BLE001
        log.warning("save slicer shot failed", exc_info=True)
        return None

async def _api_admin_order_invoice(request: web.Request) -> web.Response:
    """Счёт-расчёт по ИНДИВИДУАЛЬНОМУ заказу (OpenTodo 6aaa6a19). Мастер сам нашёл и слайснул
    модель, вводит итоговые граммы/время/пластик → считаем полный расчёт по той же формуле, что и
    обычные заказы, добавляем плату за разработку модели (может быть 0) и опционально прикладываем
    скрин из слайсера. Шлём клиенту на подтверждение (статус 'invoiced' + pending_price), он видит
    полный расчёт и скрин в приложении и принимает/отклоняет тем же потоком, что и смену цены.
    dry=true → только посчитать сумму для предпросмотра, без сохранения."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    rid = _toint(body.get("id"))
    r = await db.get_request(rid)
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"})
    try:
        minutes = float(str(body.get("minutes")).replace(",", "."))
        qty = max(1, int(float(str(body.get("qty") or r.quantity or 1))))
        dev_fee = float(str(body.get("dev_fee") or 0).replace(",", "."))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректные числа в форме"})
    if minutes <= 0:
        return web.json_response({"ok": False, "error": "Время должно быть больше нуля"})
    if dev_fee < 0:
        return web.json_response({"ok": False, "error": "Плата за разработку не может быть отрицательной"})
    # Многоцветная печать (OpenTodo efca76c3): colors=[{plastic_id, grams}] — граммы по каждому
    # цвету из слайсера мастера. Одноцвет (как раньше): plastic_id + grams.
    colors_in = body.get("colors")
    color_rows: list[tuple] = []
    if isinstance(colors_in, list) and colors_in:
        for c in colors_in:
            p = await db.get_plastic(_toint((c or {}).get("plastic_id")))
            try:
                g = float(str((c or {}).get("grams")).replace(",", "."))
            except (TypeError, ValueError):
                g = 0.0
            if not p or g <= 0:
                return web.json_response(
                    {"ok": False, "error": "В каждом цвете выберите пластик и граммы > 0"})
            color_rows.append((p, g))
        if len(color_rows) < 2:
            return web.json_response({"ok": False, "error": "Многоцвет — минимум два цвета"})
    multicolor = len(color_rows) >= 2
    if multicolor:
        pname = " + ".join(f"{p.name} · {g:g} г" for p, g in color_rows)
        r.quantity = qty
        r.breakdown = r.breakdown or {}
        try:
            res = await calcservice.reprice_request_colors(r, color_rows, minutes, user_id=r.user_id)
        except calcservice.CalcError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("invoice reprice (colors) failed")
            return web.json_response({"ok": False, "error": f"Внутренняя ошибка: {exc}"})
    else:
        plastic = await db.get_plastic(_toint(body.get("plastic_id")))
        if not plastic:
            return web.json_response({"ok": False, "error": "Выберите пластик"})
        try:
            grams = float(str(body.get("grams")).replace(",", "."))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Некорректные числа в форме"})
        if grams <= 0:
            return web.json_response({"ok": False, "error": "Граммы должны быть больше нуля"})
        pname = plastic.name
        # reprice_request резолвит пластик по имени и берёт qty/скидку из заказа — подставляем
        # выбранное в объект в памяти (для dry это не сохраняется: r — свежая копия из get_request).
        r.plastic_name = plastic.name
        r.quantity = qty
        r.breakdown = r.breakdown or {}
        try:
            res = await calcservice.reprice_request(r, grams, minutes, user_id=r.user_id)
        except calcservice.CalcError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("invoice reprice failed")
            return web.json_response({"ok": False, "error": f"Внутренняя ошибка: {exc}"})
    bd = res["bd"]
    total = round(bd.total + dev_fee, 2)            # печать + разработка
    if body.get("dry"):                            # предпросмотр суммы при вводе
        return web.json_response({"ok": True, "price": total, "print_price": bd.total,
                                  "dev_fee": dev_fee})
    bd_dict = pricing.breakdown_to_dict(bd, res["settings"])
    if multicolor:   # снимок раскладки по цветам — для прозрачности и будущего списания склада
        bd_dict["colors"] = [{"plastic_id": p.id, "name": p.name, "grams": g} for p, g in color_rows]
    shot_path = await _save_slicer_shot(rid, body.get("shot") or "")
    comment = (body.get("comment") or "").strip()
    fields = {"status": "invoiced", "pending_price": total, "plastic_name": pname,
              "quantity": qty, "grams_total": bd.grams_total, "minutes_total": bd.minutes_total,
              "breakdown": bd_dict, "dev_fee": dev_fee, "price_comment": comment or None,
              "multicolor": int(multicolor) or r.multicolor}
    if shot_path:
        fields["slicer_shot"] = shot_path
    await db.update_request(rid, **fields)
    r2 = await db.get_request(rid)
    # счёт по своему же заказу мастер выставил сам — присылать себе «подтвердите цену» незачем
    if r2 and _bot is not None and not db.is_self_order(r2):
        try:
            kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders") or keyboards.price_confirm(rid, total)
            await notify.notify_user(_bot, r2.user_id, texts.user_invoice_note(r2, config.CURRENCY),
                                     reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.debug("invoice notify failed", exc_info=True)
    return web.json_response({"ok": True, "price": total})

async def _api_admin_order_create(request: web.Request) -> web.Response:
    """Мастер оформляет заказ ЗА клиента (OpenTodo cd3258d6): иногда клиенты пишут в личку мимо
    системы, хотя имеют к ней доступ. Мастер вручную задаёт клиента/принтер/пластик/качество/
    прочность/тираж и итоговые граммы-время; цену считаем той же формулой (compute_price_manual),
    можно переопределить вручную. Создаём заказ со статусом 'invoiced' + pending_price и шлём его
    клиенту на подтверждение — он принимает/отклоняет тем же потоком, что и выставленный счёт.
    dry=true → только посчитать сумму для предпросмотра, без сохранения."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    aid, err = _admin_body(body)
    if err:
        return err
    target = _toint(body.get("user_id"))
    if not target:
        return web.json_response({"ok": False, "error": "Укажите клиента"})
    plastic = await db.get_plastic(_toint(body.get("plastic_id")))
    # Качество и прочность мастер больше не выбирает руками (8c8199d1): печать идёт профилем
    # мастера, поэтому при отсутствии в теле берём пресеты по умолчанию — как в быстром
    # расчёте по ссылке. Явно переданные значения по-прежнему уважаем.
    quality = await db.get_quality(_toint(body.get("quality_id")))
    if not quality:
        quals = await db.list_quality(active_only=True)
        quality = next((q for q in quals if q.is_default), quals[0] if quals else None)
    strength = await db.get_strength(_toint(body.get("strength_id")))
    if not strength:
        strs = await db.list_strength(active_only=True)
        strength = next((s for s in strs if s.is_default), strs[0] if strs else None)
    if not (plastic and quality and strength):
        return web.json_response({"ok": False, "error": "Выберите пластик"})
    printer = None
    for p in await db.list_printers(active_only=False):
        if p.id == _toint(body.get("printer_id")) or p.name == (body.get("printer") or ""):
            printer = p
            break
    printer = printer or await db.get_default_printer()
    try:
        grams = float(str(body.get("grams")).replace(",", "."))
        minutes = float(str(body.get("minutes")).replace(",", "."))
        qty = max(1, min(1000, int(float(str(body.get("qty") or 1)))))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректные числа в форме"})
    if grams <= 0 or minutes <= 0:
        return web.json_response({"ok": False, "error": "Граммы и время должны быть больше нуля"})
    settings = await db.get_settings()
    # НЕ дубль общей calcservice.effective_discount: заказ заводит МАСТЕР за клиента, токена
    # расчёта с промокодом у него нет — третий источник скидки тут отсутствует законно.
    discount = max(await db.best_discount(qty), await db.get_user_discount(target))
    bd = pricing.compute_price_manual(grams / qty, minutes / qty, plastic.price_per_gram * 1000.0,
                                      settings, qty=qty, discount_pct=discount,
                                      failure_rate_pct=plastic.failure_rate_pct,
                                      volume_cm3_unit=((grams / qty) / plastic.density
                                                       if plastic.density else None))
    # Цену можно переопределить вручную (договорная); иначе берём расчётную.
    price = bd.total
    manual = _num(body, "price")
    if manual is not None and manual > 0:
        price = round(manual, 2)
    if body.get("dry"):                            # предпросмотр суммы при вводе
        return web.json_response({"ok": True, "price": price, "calc_price": bd.total})
    label = (body.get("label") or "").strip()[:120] or "Заказ от мастера"
    comment = (body.get("comment") or "").strip()[:400]
    color = (body.get("color") or "").strip()
    pdisp = f"{plastic.name} · {color}" if color else plastic.name
    bd_dict = pricing.breakdown_to_dict(bd, settings)
    rid = await db.add_request(
        user_id=target, user_name=f"id {target}", model_label=label, printer=printer.name if printer else "",
        plastic_name=pdisp, quantity=qty, quality=quality.label, strength=strength.label,
        grams_total=bd.grams_total, minutes_total=bd.minutes_total, price=price,
        preview_path=None, quality_id=quality.id, strength_id=strength.id, breakdown=bd_dict,
        status="invoiced", pending_price=price, price_comment=comment or None,
        created_by_admin_id=aid)
    await db.log_event(aid, "оформил заказ за клиента", f"#{rid} для id {target}")
    # Доставка по умолчанию — ровно как у любого другого способа оформить заказ (в один тап, из
    # чата, покупка готового изделия). Без этого заказ, заведённый МАСТЕРОМ за клиента, оставался
    # единственным, у которого доставки нет вовсе: шапки не создавалось, а значит и порог
    # «бесплатно от суммы» применять было не к чему. Клиенту с настроенной доставкой её просто
    # не выставляли — мастерская везла за свой счёт и узнавала об этом при отправке.
    dlv = await _apply_default_delivery(target, rid, price)
    r = await db.get_request(rid)
    if r and _bot is not None:
        try:
            kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="orders") or keyboards.price_confirm(rid, price)
            # Приписка про доставку обязана быть В ТОМ ЖЕ сообщении: клиент видит сумму заказа и
            # жмёт «принять», а доставка приезжает отдельной строкой в счёт. Промолчать здесь
            # значит показать одну сумму, а списать другую.
            await notify.notify_user(_bot, target,
                                     texts.admin_order_created_note(r, config.CURRENCY)
                                     + await _delivery_note(rid),
                                     reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.debug("admin-created order notify failed", exc_info=True)
    return web.json_response({"ok": True, "order_id": rid, "price": price, "delivery": dlv})

async def _api_admin_balance(request: web.Request) -> web.Response:
    """Зачислить/списать баланс клиента (OpenTodo 8c428c43). Мастер зачисляет принесённые деньги,
    остаток хранится на будущее; «списать за заказ» — это отрицательная сумма с reason «заказ #N».
    Клиент получает уведомление об изменении баланса."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    target = _toint(body.get("user_id"))
    if not target:
        return web.json_response({"ok": False, "error": "Не указан клиент"})
    try:
        amount = float(str(body.get("amount")).replace(",", "."))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Некорректная сумма"})
    if abs(amount) < 0.005:
        return web.json_response({"ok": False, "error": "Сумма не может быть нулевой"})
    reason = (body.get("reason") or "").strip()[:120]
    comment = (body.get("comment") or "").strip()[:300]   # заметка мастера (ccb1e4b6)
    # У члена семьи операция уходит на общий счёт; request_id (кнопка «Оплата заказа #N»
    # в карточке заказа) даёт истории семьи точный номер заказа.
    new = await db.adjust_balance(target, amount, reason, comment=comment or None,
                                  request_id=_toint(body.get("request_id")) or None)
    if _bot is not None:
        try:
            await notify.notify_user(_bot, target,
                                     texts.user_balance_note(amount, new, config.CURRENCY, reason,
                                                             comment=comment),
                                     reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="profile"))
        except Exception:  # noqa: BLE001
            log.debug("balance notify failed", exc_info=True)
    return web.json_response({"ok": True, "balance": new})

async def _api_admin_balance_comment(request: web.Request) -> web.Response:
    """Комментарий к уже совершённой транзакции баланса (ccb1e4b6): мастер аннотирует задним
    числом (например «принёс наличными за брелоки»). Пустой текст стирает заметку."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    tx_id = _toint(body.get("tx_id"))
    if not tx_id:
        return web.json_response({"ok": False, "error": "Не указана транзакция"})
    comment = (body.get("comment") or "").strip()[:300]
    if not await db.set_balance_tx_comment(tx_id, comment):
        return web.json_response({"ok": False, "error": "Транзакция не найдена"})
    return web.json_response({"ok": True})

async def _api_admin_order_print_net(request: web.Request) -> web.Response:
    """Отправить файл заказа на принтер по сети через Moonraker (c6e4f855). start=печатать сразу."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    if not moonraker.enabled():
        return web.json_response({"ok": False, "error": "Сетевая печать не настроена (MOONRAKER_URL)"})
    r = await db.get_request(_toint(body.get("id")))
    if not r:
        return web.json_response({"ok": False, "error": "Заказ не найден"}, status=404)
    if not r.file_path or not Path(r.file_path).exists():
        return web.json_response({"ok": False, "error": "Файл задания недоступен"})
    res = await moonraker.upload(r.file_path, start=bool(body.get("start")))
    if res.get("ok") and body.get("start"):   # печать пошла из заказа → знаем, что сейчас печатается
        _set_active_print(r.id, await _active_print_key())
    return web.json_response(res)
