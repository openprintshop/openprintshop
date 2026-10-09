"""Поиск 3D-моделей по тексту (см. docs/model-search.md).

Серверный агрегатор (Вариант A доки): параллельно опрашивает площадки и нормализует ответы в
общий объект SearchHit. По тапу на карточку скачивание идёт ОБЫЧНЫМ путём —
downloader.fetch_from_url(page_url), т.е. новой логики загрузки не нужно.

Источники:
- Printables — открытый GraphQL (`searchPrints2`), без логина и токена.
- Thingiverse — официальный REST `/search/{term}?type=things`, только при THINGIVERSE_TOKEN
  (иначе площадку прячем).
- MakerWorld — только если в сборке есть личный модуль app/makerworld.py (в открытой версии
  проекта его нет): design-service search по JWT из пула аккаунтов; прячем без токена.

SearchHit (контракт фронта и /api/model_search):
  { source, ext_id, title, author, thumb, page_url, license, free } — см. _hit().

Запрос RU→EN перед поиском (площадки индексируют английский): лёгкий httpx к бесплатному
эндпоинту с graceful-фолбэком на оригинал при любой ошибке/таймауте; результат кэшируется.
Сбой перевода никогда не ломает поиск.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import config, downloader, optional

log = logging.getLogger(__name__)

# searchPrints2 — выделенный поиск Printables (морфология поля подтверждена интроспекцией):
# Query.searchPrints2(query, limit, ...) -> ListPrintType { items { ... } }.
_PR_SEARCH_Q = ("query Search($q: String!, $limit: Int!){ "
                "r: searchPrints2(query: $q, limit: $limit){ items{ id name slug price "
                "user{ publicUsername } license{ name } image{ filePath } } } }")
_PR_MEDIA = "https://media.printables.com/"

_THING_SEARCH = "https://api.thingiverse.com/search/{term}"


def _hit(*, source: str, ext_id: str, title: str, page_url: str, author: str = "",
         thumb: str = "", license: str | None = None, free: bool = True) -> dict:
    """Единая нормализованная карточка результата (контракт SearchHit)."""
    return {"source": source, "ext_id": str(ext_id), "title": title or f"{source} {ext_id}",
            "author": author or "", "thumb": thumb or "", "page_url": page_url,
            "license": license or None, "free": bool(free)}


# ============================== Printables ==============================
def _printables_hit(it: dict) -> dict:
    pid = str(it.get("id") or "")
    slug = (it.get("slug") or "").strip()
    img = ((it.get("image") or {}) or {}).get("filePath") or ""
    lic = ((it.get("license") or {}) or {}).get("name")
    return _hit(
        source="printables", ext_id=pid, title=it.get("name") or "",
        author=((it.get("user") or {}) or {}).get("publicUsername") or "",
        thumb=(_PR_MEDIA + img) if img else "",
        page_url=f"https://www.printables.com/model/{pid}" + (f"-{slug}" if slug else ""),
        license=lic, free=not it.get("price"))


async def search_printables(query: str, limit: int = 20, *, client=None) -> list[dict]:
    """Поиск по Printables. Возвращает список карточек (может быть пустым); сеть/парсинг —
    best-effort, при сбое возвращаем []."""
    query = (query or "").strip()
    if not query:
        return []
    own = client is None
    client = client or httpx.AsyncClient(headers={"User-Agent": downloader._PR_UA},
                                         timeout=config.MODEL_SEARCH_TIMEOUT, follow_redirects=True)
    try:
        r = await client.post(downloader._PR_API, json={
            "operationName": "Search", "query": _PR_SEARCH_Q,
            "variables": {"q": query, "limit": max(1, min(40, limit))}})
        r.raise_for_status()
        items = (((r.json().get("data") or {}).get("r") or {}).get("items")) or []
        return [_printables_hit(it) for it in items if it.get("id")]
    except Exception as exc:  # noqa: BLE001
        log.info("printables search failed: %s", exc)
        return []
    finally:
        if own:
            await client.aclose()


# ============================== Thingiverse ==============================
def _thingiverse_hit(it: dict) -> dict:
    tid = str(it.get("id") or "")
    # public_url у Thingiverse уже формата thingiverse.com/thing:ID (его понимает is_thingiverse);
    # на всякий случай строим канонический url из id, если поля нет.
    page_url = it.get("public_url") or f"https://www.thingiverse.com/thing:{tid}"
    return _hit(
        source="thingiverse", ext_id=tid, title=it.get("name") or "",
        author=((it.get("creator") or {}) or {}).get("name") or "",
        thumb=it.get("thumbnail") or "", page_url=page_url, license=it.get("license"), free=True)


async def search_thingiverse(query: str, limit: int = 20, *, client=None) -> list[dict]:
    """Поиск по Thingiverse REST `/search/{term}?type=things`. Нужен THINGIVERSE_TOKEN — иначе []
    (источник прячется). Best-effort: при сбое возвращаем []."""
    query = (query or "").strip()
    if not query or not config.THINGIVERSE_TOKEN:
        return []
    own = client is None
    headers = {"Authorization": f"Bearer {config.THINGIVERSE_TOKEN}", "User-Agent": "Mozilla/5.0"}
    client = client or httpx.AsyncClient(headers=headers, timeout=config.MODEL_SEARCH_TIMEOUT,
                                         follow_redirects=True)
    try:
        import urllib.parse
        url = _THING_SEARCH.format(term=urllib.parse.quote(query, safe=""))
        r = await client.get(url, params={"type": "things", "per_page": max(1, min(30, limit))})
        r.raise_for_status()
        j = r.json()
        hits = (j.get("hits") if isinstance(j, dict) else j) or []
        return [_thingiverse_hit(it) for it in hits if it.get("id")]
    except Exception as exc:  # noqa: BLE001
        log.info("thingiverse search failed: %s", exc)
        return []
    finally:
        if own:
            await client.aclose()


# ============================== Перевод запроса RU→EN ==============================
_tr_cache: dict[str, str] = {}
_TR_CACHE_MAX = 500


def _has_cyrillic(s: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in s)


async def translate_query(query: str, *, client=None) -> str:
    """RU→EN перед поиском (площадки индексируют английский). Только если в запросе есть кириллица.
    Лёгкий httpx к бесплатному эндпоинту с кэшем; на ЛЮБУЮ ошибку/таймаут — возвращаем исходный
    запрос (перевод никогда не ломает поиск)."""
    q = (query or "").strip()
    if not q or not _has_cyrillic(q):
        return q
    if q in _tr_cache:
        return _tr_cache[q]
    own = client is None
    client = client or httpx.AsyncClient(timeout=min(5.0, config.MODEL_SEARCH_TIMEOUT),
                                         follow_redirects=True)
    out = q
    try:
        # Бесплатный публичный эндпоинт Google translate (gtx) — без ключа. На сбой — фолбэк.
        r = await client.get("https://translate.googleapis.com/translate_a/single", params={
            "client": "gtx", "sl": "ru", "tl": "en", "dt": "t", "q": q})
        r.raise_for_status()
        data = r.json()
        # форма: [[["translated","orig",...], ...], ...]
        parts = [seg[0] for seg in (data[0] or []) if seg and seg[0]]
        cand = "".join(parts).strip()
        if cand:
            out = cand
    except Exception as exc:  # noqa: BLE001
        log.info("query translate failed (fallback to original): %s", exc)
        out = q
    finally:
        if own:
            await client.aclose()
    if len(_tr_cache) > _TR_CACHE_MAX:
        _tr_cache.clear()
    _tr_cache[q] = out
    return out


# ============================== Агрегатор ==============================
# Реестр источников: имя → (адаптер, доступен ли). Источник недоступен (нет токена) — прячем.
def _available_sources(sources: list[str] | None) -> list[tuple[str, object]]:
    reg = [
        ("printables", search_printables, True),
        ("thingiverse", search_thingiverse, bool(config.THINGIVERSE_TOKEN)),
    ]
    if _mw is not None:                   # личный модуль MakerWorld есть в этой сборке
        reg.append(("makerworld", _mw.search_makerworld, _mw.search_available()))
    want = set(sources) if sources else None
    return [(name, fn) for name, fn, ok in reg
            if ok and (want is None or name in want)]


def _interleave(by_source: dict[str, list[dict]], limit: int) -> list[dict]:
    """Перемежаем источники по кругу, чтобы не было «Printables ×20, потом остальные»."""
    out: list[dict] = []
    cols = [list(v) for v in by_source.values() if v]
    i = 0
    while cols and len(out) < limit:
        col = cols[i % len(cols)]
        out.append(col.pop(0))
        if not col:
            cols.remove(col)
        else:
            i += 1
    return out[:limit]


async def search(query: str, *, sources: list[str] | None = None, limit: int | None = None
                 ) -> tuple[list[dict], list[str]]:
    """Агрегатор поиска по площадкам. Возвращает (hits, queried) где queried — список источников,
    которые реально были доступны и опрошены (для чипов-фильтров на фронте).

    Запрос переводится RU→EN (graceful). Источники опрашиваются ПАРАЛЛЕЛЬНО с per-source
    таймаутом; падение/таймаут одного источника НЕ роняет агрегат (gather c return_exceptions)."""
    query = (query or "").strip()
    limit = limit or config.MODEL_SEARCH_LIMIT
    if not query or not config.MODEL_SEARCH_ENABLED:
        return [], []
    active = _available_sources(sources)
    if not active:
        return [], []

    q = await translate_query(query)

    async def _one(fn) -> list[dict]:
        try:
            return await asyncio.wait_for(fn(q, limit), timeout=config.MODEL_SEARCH_TIMEOUT)
        except Exception as exc:  # noqa: BLE001  (TimeoutError тоже сюда)
            log.info("search source timed out/failed: %s", exc)
            return []

    results = await asyncio.gather(*[_one(fn) for _, fn in active], return_exceptions=True)
    by_source: dict[str, list[dict]] = {}
    queried: list[str] = []
    for (name, _), res in zip(active, results):
        hits = res if isinstance(res, list) else []
        queried.append(name)              # источник был доступен/опрошен (даже если вернул пусто)
        if hits:
            by_source[name] = hits
    return _interleave(by_source, limit), queried


# В самом низу: app/makerworld.py импортирует этот модуль (общий конструктор карточки _hit).
_mw = optional.load("makerworld")
