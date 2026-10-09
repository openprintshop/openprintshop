"""Доставка: тарифы, стоимость и отслеживание отправлений (A2, competitor-gap 2026-07).

Что было до этого модуля: корзина (B5) научилась спрашивать способ получения и адрес, они
ложились в шапку заказа-корзины и уходили в пуш мастеру. Но доставка была ИНФОРМАТИВНОЙ —
без цены, тарифов и трек-номера: клиент не знал, сколько будет стоить привезти, а мастер
писал номер отправления руками в чат.

Здесь появляются две недостающие части:

  ДЕНЬГИ.  Типы доставки (таблица delivery_types) — прайс с ценой, порогом бесплатной
           доставки и сроком. Клиент выбирает тип на оформлении (или задаёт его один раз в
           профиле — тогда он применяется и к заказу без корзины), но СТОИМОСТЬ СЧИТАЕТ
           СЕРВЕР (type_cost) — цена из клиента не принимается ни в каком виде. Снимок цены
           и названия типа ложится в шапку заказа: переименование или переоценка тарифа
           задним числом не меняет уже оформленный заказ. Итог заказа = позиции + доставка,
           и эта же сумма уходит в онлайн-счёт (см. app/payments.py). Единственное, что
           пересчитывается у оформленного заказа, — ПОРОГ «бесплатно от суммы»: цену часто
           назначает мастер уже после оформления (см. recalc_group_delivery ниже).

  ТРЕКИНГ. Реестр перевозчиков со ссылками-шаблонами: мастер выбирает перевозчика и вводит
           номер, ссылка отслеживания собирается сама и уходит клиенту в уведомление.
           Перевозчик «Другое» — свободный ввод названия и ссылки (никаких шаблонов).

Перевозчики намеренно живут КОНСТАНТОЙ, а не таблицей с CRUD: их десяток на всю страну, они
не меняются от мастерской к мастерской, и лишний экран настроек тут дороже пользы.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

from . import db

log = logging.getLogger(__name__)

# id → (название, шаблон ссылки отслеживания). {n} — номер отправления.
CARRIERS: tuple[dict, ...] = (
    {"id": "cdek", "name": "СДЭК", "url": "https://www.cdek.ru/ru/tracking?order_id={n}"},
    {"id": "post_ru", "name": "Почта России",
     "url": "https://www.pochta.ru/tracking?barcode={n}"},
    {"id": "boxberry", "name": "Boxberry",
     "url": "https://boxberry.ru/tracking-page?id={n}"},
    {"id": "yandex", "name": "Яндекс Доставка",
     "url": "https://yandex.ru/tracking/{n}"},
    {"id": "dpd", "name": "DPD",
     "url": "https://www.dpd.ru/ols/trace2/basicSearch.do2?_search_type=1&orderNum={n}"},
    {"id": "fivepost", "name": "5Post", "url": "https://5post.ru/tracking?id={n}"},
    {"id": "belpost", "name": "Белпочта",
     "url": "https://belpost.by/Otsledit-otpravlenie?number={n}"},
    {"id": "europochta", "name": "Европочта",
     "url": "https://evropochta.by/services/otsledit-otpravlenie?number={n}"},
    {"id": "courier", "name": "Свой курьер", "url": ""},
    {"id": "other", "name": "Другое", "url": ""},
)

_BY_ID = {c["id"]: c for c in CARRIERS}

# Номер отправления: цифры/латиница/дефис. Всё остальное (пробелы, кириллица, мусор из
# буфера обмена) чистим — номер уходит в URL и в сообщение клиенту.
_NUM_RE = re.compile(r"[^0-9A-Za-z\-]")


def carrier(carrier_id: str) -> dict | None:
    return _BY_ID.get((carrier_id or "").strip().lower())


def carrier_name(carrier_id: str) -> str:
    c = carrier(carrier_id)
    return c["name"] if c else ""


def clean_number(number: str) -> str:
    """Нормализованный номер отправления ('' — номера нет)."""
    return _NUM_RE.sub("", (number or "").strip())[:40]


def track_url(carrier_id: str, number: str, custom_url: str = "") -> str:
    """Ссылка отслеживания: своя (для «Другое»/«Свой курьер») или по шаблону перевозчика.
    Чужую ссылку пускаем только http(s) — она уходит клиенту кликабельной."""
    custom = (custom_url or "").strip()
    if custom:
        return custom if custom.startswith(("http://", "https://")) else ""
    c = carrier(carrier_id)
    num = clean_number(number)
    if not c or not c["url"] or not num:
        return ""
    return c["url"].format(n=quote(num, safe=""))


def carriers_json() -> list[dict]:
    """Реестр для админки: id, название и есть ли автоссылка (иначе мастер вводит свою)."""
    return [{"id": c["id"], "name": c["name"], "auto_url": bool(c["url"])} for c in CARRIERS]


# ---- Деньги ----
def type_cost(dtype: dict | None, items_total: float) -> float:
    """Стоимость доставки по типу для заказа на items_total (без доставки).

    free_from > 0 и сумма позиций достигла порога → доставка бесплатна. Сравнение
    НЕСТРОГОЕ (>=): «бесплатно от 3000» означает, что ровно 3000 уже бесплатно."""
    if not dtype:
        return 0.0
    free_from = float(dtype.get("free_from") or 0)
    if free_from > 0 and float(items_total or 0) >= free_from:
        return 0.0
    return round(max(0.0, float(dtype.get("price") or 0)), 2)


def type_json(dtype: dict, items_total: float | None = None) -> dict:
    """Тип доставки для фронта. items_total задан → добавляем итоговую цену для ЭТОГО заказа
    и флаг «бесплатно» — клиенту видно не абстрактный тариф, а сколько он заплатит сейчас."""
    out = {"id": int(dtype["id"]), "name": dtype["name"] or "",
           "price": round(float(dtype["price"] or 0), 2),
           "free_from": round(float(dtype["free_from"] or 0), 2),
           "eta": dtype["eta"] or "", "comment": dtype["comment"] or "",
           "is_active": int(dtype["is_active"] or 0), "sort": int(dtype["sort"] or 0)}
    if items_total is not None:
        cost = type_cost(dtype, items_total)
        out["cost"] = cost
        out["free"] = bool(cost <= 0 and float(dtype["price"] or 0) > 0)
    return out


def group_delivery_cost(group: dict | None) -> float:
    """Стоимость доставки из шапки группы. Шапки нет (старый cart-заказ) → 0."""
    if not group:
        return 0.0
    return round(float(group.get("delivery_cost") or 0), 2)


def group_track(group: dict | None) -> dict:
    """Блок отслеживания для выдачи: перевозчик, номер, ссылка, отправлено/доставлено."""
    g = group or {}
    cid = (g.get("track_carrier") or "").strip()
    num = (g.get("track_number") or "").strip()
    url = (g.get("track_url") or "").strip()
    return {"carrier": cid, "carrier_name": carrier_name(cid) or cid, "number": num,
            "url": url, "shipped_at": int(g.get("shipped_at") or 0),
            "delivered_at": int(g.get("delivered_at") or 0)}


# ---- Порог «бесплатно от суммы» у УЖЕ ОФОРМЛЕННОГО заказа (волна 5, 09.08.2026) ----
# Стоимость доставки фиксируется в шапке заказа в момент оформления — по той сумме позиций,
# которая известна ТОГДА. Но цену назначает мастер, и часто ПОЗЖЕ: заказ без расчёта (слайс
# не удался), индивидуальный заказ, «уточнил цену вместе со счётом». У таких заказов порог
# считался по нулю, то есть доставка навсегда оставалась платной, даже если итог давно
# перевалил «бесплатно от». Ниже — пересчёт под новую цену; зовётся из мест, где мастер
# реально меняет requests.price (app/webapp.py, app/handlers/user_flow.py).

def tariff_cost(dtype: dict | None) -> float:
    """Цена тарифа как её вернул бы type_cost ниже порога (нормализованная, ≥ 0)."""
    return round(max(0.0, float((dtype or {}).get("price") or 0)), 2)


MANUAL = "manual"       # стоимость доставки задал мастер руками — автоматика её не трогает
AUTO = "auto"           # посчитал сервер (тариф/порог) — пересчитывать можно


def cost_source(group: dict | None) -> str:
    """Откуда взялась стоимость доставки в шапке. '' — старая запись, источник неизвестен."""
    return ((group or {}).get("delivery_cost_source") or "").strip().lower()


def is_gift(group: dict | None) -> bool:
    """Доставка подарена мастером (ноль, поставленный руками), а не бесплатна по порогу.

    Нужна интерфейсу: «бесплатно от 5 000» и «подарок мастера» — разные вещи, и оператору
    чужой мастерской это надо видеть, иначе он не поймёт, почему у одного заказа бесплатно,
    а у соседнего нет."""
    return cost_source(group) == MANUAL and group_delivery_cost(group) <= 0


def threshold_recalcable(dtype: dict | None, current_cost: float,
                         items_total: float | None = None, source: str = "") -> bool:
    """Разрешён ли автоматический пересчёт стоимости доставки по порогу.

    Запрещён, когда:
      • типа нет (удалён/не выбран) — тарифа, по которому считать, не существует;
      • у типа нет порога (free_from ≤ 0) — пересекать нечего, цена доставки от суммы не зависит;
      • источник = MANUAL: мастер назначил стоимость ЯВНО, и тариф не смеет её затирать;
      • (для старых записей без источника) текущая стоимость не равна НИ тарифу, НИ нулю —
        значит её тоже задали руками. Работает потому, что type_cost умеет вернуть ровно два
        значения: тариф или 0.

    Пометка источника (11.08) закрыла дыру, которая жила здесь до неё. Ноль бывает двух видов:
    автоматический (сумма перекрыла порог) и ПОДАРЕННЫЙ мастером руками, — и по самому числу
    они неразличимы. Раньше подарок считался пересчитываемым, то есть первая же смена цены
    молча выставляла клиенту счёт за доставку, которую ему подарили. Догадка тут невозможна в
    принципе: пробовали «ноль при сумме ниже порога = подарок», но это ломает ЗАКОНОМЕРНЫЙ
    обратный ход «цена упала ниже порога → доставка снова платная», ради которого порог и
    пересчитывают. Решение принималось по СТАРОЙ сумме, а на пересчёте видна только новая.
    Поэтому источник и пишется В МОМЕНТ ЗАПИСИ — фактом, а не выводом.

    Записи, сделанные до появления колонки, приходят с source='' и ведут себя как раньше:
    их подарок по-прежнему может быть отозван. Восстановить прошлое нечем — но новые заказы
    защищены. items_total не используется, принимается ради вызывающих."""
    if not dtype or float(dtype.get("free_from") or 0) <= 0:
        return False
    if (source or "").strip().lower() == MANUAL:
        return False
    cost = round(float(current_cost or 0), 2)
    return cost in (0.0, tariff_cost(dtype))


def recalc_threshold_cost(dtype: dict | None, current_cost: float,
                          items_total: float, source: str = "") -> float | None:
    """Новая стоимость доставки для суммы позиций items_total — или None, если менять нечего.

    None — и когда пересчёт запрещён (threshold_recalcable), и когда он ничего не меняет.
    Обратное направление ТОЖЕ рабочее: цена упала ниже порога → доставка снова платная
    (возвращается тариф). Это осознанно: порог — правило прайса, а не подарок задним числом."""
    if not threshold_recalcable(dtype, current_cost, items_total, source):
        return None
    new = type_cost(dtype, items_total)
    return None if abs(new - round(float(current_cost or 0), 2)) < 0.005 else new


def free_from_price(dtype: dict | None, current_cost: float, items_total: float,
                    own_price: float, source: str = "") -> float | None:
    """Цена ЭТОЙ позиции, начиная с которой доставка всего заказа станет бесплатной.

    Нужна интерфейсу мастера: он вводит цену одной позиции, а порог смотрит на сумму ВСЕГО
    заказа — считать эту арифметику во фронте значило бы завести четвёртую копию правила.
    None — пересчёт неприменим (см. threshold_recalcable). Может быть и ≤ 0: сёстры-позиции
    уже перекрыли порог сами."""
    if not threshold_recalcable(dtype, current_cost, items_total, source):
        return None
    others = round(float(items_total or 0) - float(own_price or 0), 2)
    return round(float(dtype.get("free_from") or 0) - others, 2)


async def preview_group_delivery(rid: int, new_price: float) -> dict | None:
    """Во что превратится доставка, если позиции rid назначить цену new_price. НИЧЕГО НЕ ПИШЕТ.

    Нужна там, где решение «стало дешевле → применяем сразу, подорожало → спрашиваем клиента»
    принимается ДО записи цены. Без неё это решение принималось по цене ПОЗИЦИИ, а доставка
    двигалась следом и могла увести ИТОГ в другую сторону: клиент получал «🎉 стало дешевле»
    и счёт больше прежнего. Считаем ту же арифметику, что recalc_group_delivery, но с
    подставленной ценой и без записи.

    Возвращает {'old': …, 'new': …} либо None, если пересчёта не будет."""
    try:
        gid = await db.group_id_of_request(int(rid))
        if not gid:
            return None
        ent = await db.get_order_group(gid)
        if not ent or int(ent.get("delivery_paid") or 0):
            return None
        old = group_delivery_cost(ent)
        tid = int(ent.get("delivery_type_id") or 0)
        dtype = await db.get_delivery_type(tid) if tid else None
        if not threshold_recalcable(dtype, old, source=cost_source(ent)):
            return None
        sibs = await db.list_requests_by_group(gid)
        items_total = round(sum(float(new_price if int(s.id) == int(rid) else (s.price or 0))
                                for s in sibs), 2)
        new = recalc_threshold_cost(dtype, old, items_total, cost_source(ent))
        return None if new is None else {"old": old, "new": new}
    except Exception:  # noqa: BLE001
        log.debug("preview_group_delivery failed rid=%s", rid, exc_info=True)
        return None


async def recalc_group_delivery(rid: int) -> dict | None:
    """Пересчитать доставку заказа rid под ТЕКУЩИЕ цены его позиций. None — ничего не меняли.

    Порядок обязателен: зовётся ПОСЛЕ того, как новая цена записана в requests, и ДО
    отправки счёта — иначе счёт унесёт старую сумму доставки.

    ВНИМАНИЕ: меняет сумму УЖЕ ВЫСТАВЛЕННОГО счёта. Telegram сверяет сумму на pre_checkout
    (payments.precheckout_error), поэтому висящий у клиента счёт после пересчёта отклонится —
    его нужно выставить заново. Мастера об этом предупреждает карточка заказа (блок
    delivery_recalc в _api_admin_order).

    Ошибки глушим: доставка не должна ронять назначение цены — цена важнее её стоимости."""
    try:
        gid = await db.group_id_of_request(int(rid))
        if not gid:
            return None                              # одиночный заказ без шапки — доставки нет
        ent = await db.get_order_group(gid)
        if not ent or int(ent.get("delivery_paid") or 0):
            return None                              # доставку уже оплатили — сумму не трогаем
        old = group_delivery_cost(ent)
        tid = int(ent.get("delivery_type_id") or 0)
        dtype = await db.get_delivery_type(tid) if tid else None
        if not threshold_recalcable(dtype, old, source=cost_source(ent)):
            return None      # выходим ДО чтения позиций: зовёмся на каждой смене цены
        sibs = await db.list_requests_by_group(gid)
        items_total = round(sum(float(s.price or 0) for s in sibs), 2)
        new = recalc_threshold_cost(dtype, old, items_total, cost_source(ent))
        if new is None:
            return None
        # Источник переписываем в AUTO явно: это значение посчитала автоматика, и следующий
        # пересчёт вправе его тронуть. Молча оставить прежний '' было бы неверно для записей,
        # заведённых до колонки, — они так и остались бы «источник неизвестен».
        await db.update_order_group(gid, delivery_cost=new, delivery_cost_source=AUTO,
                                    total_snapshot=round(items_total + new, 2))
        log.info("заказ #%s: доставка пересчитана по порогу «бесплатно от %s» — %s → %s "
                 "(сумма позиций %s)", rid, (dtype or {}).get("free_from"), old, new, items_total)
        return {"gid": gid, "old": old, "new": new, "items_total": items_total,
                "total": round(items_total + new, 2)}
    except Exception:  # noqa: BLE001
        log.warning("не удалось пересчитать доставку заказа #%s", rid, exc_info=True)
        return None
