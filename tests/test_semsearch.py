"""Семантический поиск по коллекции (OpenTodo b56041c9): ранжирование, пороги, кэш, фолбэк.
Модель в тестах не грузим — эмбеддер замокан детерминированными векторами."""
import math

from app import semsearch

# «эмбеддинги»: 3-мерные, нормированные; близость задана руками
_VECS = {
    "дракон": (1.0, 0.0, 0.0),
    "Яйцо дракона · PLA": (0.95, 0.31, 0.0),     # cos ~0.95 — сильный хит
    "Кот · PLA": (0.0, 1.0, 0.0),                # cos 0 к дракону
    "Ящер · PETG": (0.85, 0.53, 0.0),            # cos 0.85 — в пределах 0.25 от лучшего
    "Полка · PETG": (0.5, 0.87, 0.0),            # cos 0.5 — дальше 0.25 от лучшего → отрезается
}


def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _fake_embed(texts):
    return [_norm(_VECS[t]) for t in texts]


def test_search_ranks_and_cuts_noise(monkeypatch, tmp_path):
    monkeypatch.setattr(semsearch, "_VEC_FILE", tmp_path / "semvec.json")
    monkeypatch.setattr(semsearch, "_CACHE", None)
    monkeypatch.setattr(semsearch, "_embed", _fake_embed)
    items = [{"id": 1, "label": "Яйцо дракона", "material": "PLA"},
             {"id": 2, "label": "Кот", "material": "PLA"},
             {"id": 3, "label": "Ящер", "material": "PETG"},
             {"id": 4, "label": "Полка", "material": "PETG"}]
    ids = semsearch.search("дракон", items)
    assert ids == [1, 3]          # сильный хит + близкий; «Полка» (0.5) и «Кот» (0) отрезаны


def test_search_empty_query_and_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(semsearch, "_VEC_FILE", tmp_path / "semvec.json")
    monkeypatch.setattr(semsearch, "_CACHE", None)
    assert semsearch.search("", [{"id": 1, "label": "x"}]) is None
    assert semsearch.search("дракон", []) is None
    monkeypatch.setattr(semsearch, "_embed", lambda texts: None)   # модель недоступна
    assert semsearch.search("дракон", [{"id": 1, "label": "x"}]) is None   # фолбэк на подстроку


def test_vector_disk_cache_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(semsearch, "_VEC_FILE", tmp_path / "semvec.json")
    monkeypatch.setattr(semsearch, "_CACHE", None)
    calls = []

    def counting_embed(texts):
        calls.append(list(texts))
        return _fake_embed(texts)

    monkeypatch.setattr(semsearch, "_embed", counting_embed)
    items = [{"id": 1, "label": "Яйцо дракона", "material": "PLA"}]
    semsearch.search("дракон", items)
    semsearch.search("дракон", items)
    # запрос эмбеддится каждый раз, тексты коллекции — только в первый (дальше дисковый кэш)
    flat = [t for batch in calls for t in batch]
    assert flat.count("Яйцо дракона · PLA") == 1
    # кэш реально лёг на диск
    assert (tmp_path / "semvec.json").exists()
