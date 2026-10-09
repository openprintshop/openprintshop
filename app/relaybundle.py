"""Пакет каталога для приёмной (Э3-копия, контракт §7): каталог, лента, витрина и поиск без ПК.

Ответы каталога строит логика ПК — сортировки, картинки с версией файла с диска ПК, нечёткий
поиск, лента 70/30. Повторять её на VPS — значит разъехаться при первой же правке. Поэтому
ПК раз в BUNDLE_EVERY собирает ровно те же карточки теми же функциями, что отвечают онлайн
(storefront._collection_item_json, build_catalog_categories, feed_item_json,
products.list_products_json), и отправляет приёмной одним gzip-JSON, если что-то изменилось.
Приёмная (relay/catalog.py) только режет страницы, ищет подстрокой и подставляет личное
(«Избранное», скидку) из копии базы.

Без ПК не персонально только лента: она одна на всех (раскладка «для никого», uid 0) — и
поиск: подстрока по названию, ИИ-тегам и исходному названию, без опечаток и семантики.
"""
from __future__ import annotations

import gzip
import hashlib
import json

from . import config, db

#: Версия формата пакета (relay/catalog.py принимает её и предыдущую).
FORMAT = 1
#: Лента «для никого» — та же раскладка, что recommend_page даёт клиенту без истории.
FEED_UID = 0
FEED_MAX = 500
#: Все модели каталога разом (сейчас ~3,7 тыс.).
ALL = 1_000_000


async def build_bundle(now: float) -> dict:
    # Веб-слой — лениво, как в relaylink.build_web_section: webapp сам импортирует relaylink.
    from .web import products, storefront

    everything = await db.list_collection(limit=ALL)
    items = {str(it.id): storefront._collection_item_json(it) for it in everything}
    top = [it.id for it in await db.list_collection(limit=120)]
    by_category: dict[str, list[int]] = {}
    for name, _n, _top_id, _srt in await db.list_collection_categories():
        rows = await db.list_collection_by_category(name, limit=ALL, offset=0)
        by_category[name] = [it.id for it in rows]
    feed_rows, _ = await db.recommend_page(FEED_UID, limit=FEED_MAX, offset=0)
    minutes = await db.collection_last_minutes([it.hash for it in feed_rows])
    search = {str(it.id): " ".join(x for x in (it.label, it.tags, it.orig_label) if x).lower()
              for it in everything}
    return {
        "v": FORMAT,
        "generated_at": now,
        "currency": config.CURRENCY,
        "items": items,
        "top": top,
        "categories": await storefront.build_catalog_categories(),
        "by_category": by_category,
        "feed": [storefront.feed_item_json(it, minutes) for it in feed_rows],
        "search": search,
        "products": await products.list_products_json(),
    }


def digest(bundle: dict) -> str:
    """Отпечаток содержимого без времени сборки: тот же каталог — та же сумма, не шлём."""
    body = {k: v for k, v in bundle.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def pack(bundle: dict) -> bytes:
    return gzip.compress(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def warm_urls(bundle: dict, feed_first: int = 60) -> list[str]:
    """Картинки, которые клиент без ПК увидит первыми: топ каталога, обложки разделов, витрина и
    начало ленты. Их ПК заранее прогоняет через сайт на VPS, чтобы они легли в кэш nginx (всё
    превью каталога — ~380 МБ — на VPS не держим)."""
    urls: list[str] = []
    items = bundle.get("items") or {}
    for cid in bundle.get("top") or []:
        it = items.get(str(cid))
        if it and it.get("preview"):
            urls.append(it["preview"])
    urls += [c["preview"] for c in bundle.get("categories") or [] if c.get("preview")]
    urls += [p["photo"] for p in bundle.get("products") or [] if p.get("photo")]
    urls += [f["preview"] for f in (bundle.get("feed") or [])[:feed_first] if f.get("preview")]
    return list(dict.fromkeys(urls))
