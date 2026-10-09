"""Тесты текстового поиска моделей на моках/фикстурах — без реальной сети.

Покрываем: мапперы ответ→SearchHit (Printables / Thingiverse) на сохранённых JSON,
агрегатор при падении/таймауте одного источника (не валит выдачу), фолбэк перевода, пустой запрос.
"""
import asyncio

from app import modelsearch


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeClient:
    """Мок httpx.AsyncClient: и post (Printables/GraphQL), и get (Thingiverse/MakerWorld)."""
    def __init__(self, payload):
        self._p = payload
        self.posts = []
        self.gets = []

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return _FakeResp(self._p)

    async def get(self, url, params=None):
        self.gets.append((url, params))
        return _FakeResp(self._p)

    async def aclose(self):
        return None


# ============================== Printables ==============================
async def test_search_printables_parses():
    payload = {"data": {"r": {"items": [
        {"id": "613682", "name": "Booenchy Benchy", "slug": "booenchy", "price": None,
         "user": {"publicUsername": "alice"}, "license": {"name": "CC-BY"},
         "image": {"filePath": "media/prints/613682/images/x.jpg"}},
        {"id": "100", "name": "No Image", "slug": "", "image": None},
    ]}}}
    cli = _FakeClient(payload)
    hits = await modelsearch.search_printables("benchy", client=cli)
    assert len(hits) == 2
    h0 = hits[0]
    assert h0["source"] == "printables"
    assert h0["ext_id"] == "613682"
    assert h0["page_url"] == "https://www.printables.com/model/613682-booenchy"
    assert h0["thumb"] == "https://media.printables.com/media/prints/613682/images/x.jpg"
    assert h0["author"] == "alice"
    assert h0["license"] == "CC-BY"
    assert h0["free"] is True
    # full SearchHit contract keys present
    assert set(h0) == {"source", "ext_id", "title", "author", "thumb", "page_url", "license", "free"}
    assert hits[1]["page_url"] == "https://www.printables.com/model/100"   # без slug
    assert hits[1]["thumb"] == ""
    assert hits[1]["author"] == "" and hits[1]["license"] is None
    assert "printables.com/graphql" in cli.posts[0][0]                     # запрос ушёл туда


async def test_search_printables_empty_query_no_network():
    # пустой запрос → [] до создания клиента (сеть не трогаем)
    assert await modelsearch.search_printables("   ") == []


# ============================== Thingiverse ==============================
async def test_search_thingiverse_parses(monkeypatch):
    monkeypatch.setattr(modelsearch.config, "THINGIVERSE_TOKEN", "tok")
    payload = {"total": 1, "hits": [
        {"id": 763622, "name": "Benchy", "public_url": "https://www.thingiverse.com/thing:763622",
         "thumbnail": "https://cdn.thingiverse.com/x.jpg", "creator": {"name": "CreativeTools"}},
        {"id": 0},   # без id (id==0 falsy) — отбрасываем
    ]}
    cli = _FakeClient(payload)
    hits = await modelsearch.search_thingiverse("benchy", client=cli)
    assert len(hits) == 1
    h = hits[0]
    assert h["source"] == "thingiverse"
    assert h["ext_id"] == "763622"
    assert h["page_url"] == "https://www.thingiverse.com/thing:763622"   # понимает is_thingiverse
    assert h["thumb"] == "https://cdn.thingiverse.com/x.jpg"
    assert h["author"] == "CreativeTools"
    assert "search/benchy" in cli.gets[0][0]
    assert cli.gets[0][1]["type"] == "things"


async def test_search_thingiverse_hidden_without_token(monkeypatch):
    monkeypatch.setattr(modelsearch.config, "THINGIVERSE_TOKEN", "")
    # нет токена → площадка прячется (пустой результат, сеть не трогаем)
    assert await modelsearch.search_thingiverse("benchy") == []


# ============================== Перевод запроса ==============================
async def test_translate_noop_for_latin():
    # латиница → перевод не нужен, отдаём как есть, сеть не трогаем
    assert await modelsearch.translate_query("phone stand") == "phone stand"


async def test_translate_fallback_on_error(monkeypatch):
    """Сбой/таймаут перевода → возвращаем исходный (кириллический) запрос, не падаем."""
    class _BoomClient:
        async def get(self, *a, **k):
            raise RuntimeError("network down")

        async def aclose(self):
            return None

    modelsearch._tr_cache.clear()
    out = await modelsearch.translate_query("подставка", client=_BoomClient())
    assert out == "подставка"


async def test_translate_uses_endpoint_and_caches():
    payload = [[["phone stand", "подставка для телефона", None, None]], None, "ru"]
    cli = _FakeClient(payload)
    modelsearch._tr_cache.clear()
    out = await modelsearch.translate_query("подставка для телефона", client=cli)
    assert out == "phone stand"
    # повторный вызов берётся из кэша (новый клиент не дёргается)
    cli2 = _FakeClient([[["WRONG", "x", None, None]]], )
    out2 = await modelsearch.translate_query("подставка для телефона", client=cli2)
    assert out2 == "phone stand"
    assert cli2.gets == []      # кэш-хит → сети не было


# ============================== Агрегатор ==============================
async def test_search_one_source_error_does_not_kill_aggregate(monkeypatch):
    """Одна площадка падает/таймаутит — выдача собирается из остальных, источник всё равно
    числится опрошенным (queried)."""
    monkeypatch.setattr(modelsearch.config, "THINGIVERSE_TOKEN", "tok")
    monkeypatch.setattr(modelsearch, "_mw", None)   # модуля MakerWorld нет (открытая версия)
    monkeypatch.setattr(modelsearch, "translate_query", lambda q, **k: _async(q))

    async def ok_printables(q, limit):
        return [modelsearch._hit(source="printables", ext_id="1", title="A",
                                 page_url="https://www.printables.com/model/1")]

    async def boom_thingiverse(q, limit):
        raise RuntimeError("thingiverse down")

    monkeypatch.setattr(modelsearch, "search_printables", ok_printables)
    monkeypatch.setattr(modelsearch, "search_thingiverse", boom_thingiverse)

    hits, queried = await modelsearch.search("benchy")
    assert [h["ext_id"] for h in hits] == ["1"]            # выдача не пустая
    assert set(queried) == {"printables", "thingiverse"}   # оба опрошены, MW спрятан


async def test_search_timeout_one_source(monkeypatch):
    monkeypatch.setattr(modelsearch.config, "THINGIVERSE_TOKEN", "tok")
    monkeypatch.setattr(modelsearch, "_mw", None)
    monkeypatch.setattr(modelsearch.config, "MODEL_SEARCH_TIMEOUT", 0.05)
    monkeypatch.setattr(modelsearch, "translate_query", lambda q, **k: _async(q))

    async def ok_printables(q, limit):
        return [modelsearch._hit(source="printables", ext_id="1", title="A",
                                 page_url="https://www.printables.com/model/1")]

    async def slow_thingiverse(q, limit):
        await asyncio.sleep(1.0)      # дольше таймаута → wait_for его срежет
        return [modelsearch._hit(source="thingiverse", ext_id="9", title="Z",
                                 page_url="https://www.thingiverse.com/thing:9")]

    monkeypatch.setattr(modelsearch, "search_printables", ok_printables)
    monkeypatch.setattr(modelsearch, "search_thingiverse", slow_thingiverse)

    hits, queried = await modelsearch.search("benchy")
    assert [h["ext_id"] for h in hits] == ["1"]            # медленный источник просто пуст
    assert set(queried) == {"printables", "thingiverse"}


async def test_search_empty_query():
    assert await modelsearch.search("   ") == ([], [])


async def test_search_disabled(monkeypatch):
    monkeypatch.setattr(modelsearch.config, "MODEL_SEARCH_ENABLED", False)
    assert await modelsearch.search("benchy") == ([], [])


def _async(value):
    """Хелпер: обернуть значение в готовую корутину (для monkeypatch translate_query)."""
    async def _c():
        return value
    return _c()
