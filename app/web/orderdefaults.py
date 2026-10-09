"""Доставка по умолчанию у заказа без корзины и приписка о доставке в подтверждении.

Один предмет: как заказ, оформленный В ОДИН ТАП (без корзины — «Оставить заказ», заказ из
чата, покупка готового изделия), получает доставку из профиля клиента, и как об этом
говорится клиенту в подтверждении. Сама арифметика тарифов и пересчёт порога «бесплатно от
суммы» живут в app/delivery.py — здесь только применение умолчаний к конкретному заказу.

Выделено из app/webapp.py волной 1c endgame-плана (docs/Перспективные разработки/
refactoring-webapp-endgame-2026-08-12.md): эти два помощника зовут ВОСЕМЬ мест конвейера
заказа, и пока у них не было дома вне webapp, ни оформление (волна 15), ни деньги заказа
(волна 16) не могли уехать без обратного импорта.

Модуль НЕ импортирует webapp; держателя бота здесь нет и заводить его НЕЛЬЗЯ — сообщения
клиенту шлют вызывающие, у которых `_bot` есть. app/webapp.py реэкспортирует оба имени
фасадом, поэтому и код, и тесты находят их как webapp.*.

Тела перенесены ДОСЛОВНО, без единой правки. Комментарии внутри дороже кода: разбор 12.08
про два пути, куда порог не доставал (заказ, заведённый мастером за клиента, и второй заход
у заказа, оформленного во время слайса), переехал буква в букву.
"""
from __future__ import annotations

import logging
import uuid

from .. import config, db, delivery, texts

log = logging.getLogger(__name__)


async def _delivery_note(rid: int) -> str:
    """Приписка про доставку к подтверждению заказа ('' — доставки нет). Читает шапку заказа,
    поэтому одинаково работает и для корзины, и для одиночного заказа с умолчаниями клиента."""
    try:
        gid = await db.group_id_of_request(rid)
        if not gid:
            return ""
        ent = await db.get_order_group(gid)
        if not ent or (ent.get("delivery_method") or "") != "delivery":
            return ""
        r = await db.get_request(rid)
        return texts.user_order_delivery_note(
            delivery_type=ent.get("delivery_type_name") or "",
            address=ent.get("delivery_address") or "",
            cost=delivery.group_delivery_cost(ent),
            total=float(getattr(r, "price", 0) or 0), cur=config.CURRENCY)
    except Exception:  # noqa: BLE001
        log.debug("delivery note failed", exc_info=True)
        return ""


async def _apply_default_delivery(user_id: int, rid: int, items_total: float) -> dict | None:
    """Доставка по умолчанию для заказа БЕЗ корзины (доработка A2 31.07).

    В корзине клиент выбирает способ получения руками. У заказа в один тап («Оставить заказ»,
    заказ из чата, покупка готового изделия) такого шага нет — спрашивать адрес там значило бы
    сломать весь смысл быстрого заказа. Поэтому берём то, что клиент один раз задал в профиле:
    тип доставки + адрес. Не задал — заказ остаётся как раньше, без шапки и без доставки.

    Шапку заводим ту же самую (order_groups) из ОДНОЙ позиции: иначе адресу, стоимости и
    трек-номеру негде жить, а вся денежная и трековая машинерия (счёт, PDF, отправление)
    уже завязана на gid. Идемпотентно: у заказа, который уже в группе, стоимость пересчитывает
    delivery.recalc_group_delivery — это нужно заказу, оформленному во время слайса, где на
    момент создания цена ещё 0 и порог «бесплатно от суммы» посчитать не по чему.

    Возвращает {"gid", "cost", "type", "address"} или None, если доставка не применилась."""
    try:
        gid = await db.group_id_of_request(rid)
        if gid:
            # Заказ УЖЕ в шапке — значит цена стала известна позже создания (оформлен во время
            # слайса). Пересчёт ведёт ОБЩИЙ модуль, а не своя арифметика по профилю клиента:
            #   • тариф он берёт ИЗ ГРУППЫ — мастер мог сменить способ доставки, и профиль клиента
            #     тут уже не власть;
            #   • он знает про ПОДАРЕННУЮ мастером доставку (delivery_cost_source=manual) и про
            #     уже оплаченную. Своя копия правила этого не знала и на первом же пересчёте
            #     выставляла клиенту счёт за доставку, которую ему подарили;
            #   • он не зависит от профиля вовсе — клиент мог стереть адрес между заказом и
            #     концом слайса, и тогда прежний код молча оставлял стоимость, посчитанную по
            #     НУЛЕВОЙ сумме, то есть полный тариф вместо бесплатной доставки.
            await delivery.recalc_group_delivery(rid)
            ent = await db.get_order_group(gid)
            if not ent:
                return None
            return {"gid": gid, "cost": delivery.group_delivery_cost(ent),
                    "type": ent.get("delivery_type_name") or "",
                    "address": ent.get("delivery_address") or ""}
        prefs = await db.get_user_prefs(int(user_id))
        tid = int(prefs.get("delivery_type_id") or 0)
        addr = (prefs.get("delivery_address") or "").strip()
        if not tid or not addr:
            return None
        dtype = await db.get_delivery_type(tid)
        if not dtype or not int(dtype["is_active"] or 0):
            return None            # тип выключили/удалили после того, как клиент его выбрал
        cost = delivery.type_cost(dtype, float(items_total or 0))
        gid = uuid.uuid4().hex[:12]
        await db.create_order_group(
            gid, int(user_id), delivery_method="delivery", delivery_address=addr,
            total_snapshot=round(float(items_total or 0) + cost, 2),
            delivery_type_id=int(dtype["id"]), delivery_type_name=dtype["name"] or "",
            delivery_cost=cost)
        await db.update_request(rid, order_group=gid)
        return {"gid": gid, "cost": cost, "type": dtype["name"] or "", "address": addr}
    except Exception:  # noqa: BLE001
        # Доставка не должна ронять оформление заказа: заказ важнее адреса.
        log.warning("не удалось применить доставку по умолчанию к заказу #%s", rid, exc_info=True)
        return None
