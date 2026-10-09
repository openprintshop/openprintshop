"""Семантический поиск по коллекции (OpenTodo b56041c9): «дракон» находит «Яйцо дракона»,
«кот» — «Springy Kitty», даже без совпадения подстроки.

Эмбеддинги — paraphrase-multilingual-MiniLM-L12-v2 (русский понимает) через transformers
напрямую (mean-pooling + L2-норма; пакет sentence-transformers не нужен). Считаем НА CPU:
текстов мало (название+материал), ~20 мс на текст — VRAM не трогаем вообще (игры/SDXL
живут спокойно). Модель грузится лениво при первом поиске (~0.5 ГБ ОЗУ) и НЕ выгружается:
в отличие от SDXL это полгига обычной памяти, а не видеопамяти.

Векторы кэшируются на диске (data/cache/semvec.json) по хэшу текста — пересчитываются
только новые/переименованные модели коллекции. Любой сбой → None, вызывающий откатывается
на обычный подстрочный поиск (поиск работает ВСЕГДА).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from . import config

log = logging.getLogger(__name__)

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_VEC_FILE = config.CACHE_DIR / "semvec.json"
_LOCK = threading.Lock()
_TOK = None
_MODEL = None
_LOAD_FAILED = False
_CACHE: dict[str, list[float]] | None = None   # {text_hash: vector}


def _load() -> bool:
    """Ленивая загрузка модели на CPU. False — недоступна (нет модели/сети/пакета)."""
    global _TOK, _MODEL, _LOAD_FAILED
    if _MODEL is not None:
        return True
    if _LOAD_FAILED:
        return False
    if not config.SEMSEARCH_ENABLED:
        # Модуль не устанавливали (галочка в установщике снята) — молчим и не тратим
        # секунды на попытку импорта при каждом запросе.
        _LOAD_FAILED = True
        return False
    try:
        import torch  # noqa: F401  (нужен transformers под капотом)
        from transformers import AutoModel, AutoTokenizer
        log.info("Семантический поиск: загружаю %s (CPU, первый раз скачает ~0.5 ГБ)", MODEL)
        _TOK = AutoTokenizer.from_pretrained(MODEL)
        _MODEL = AutoModel.from_pretrained(MODEL)
        _MODEL.eval()
        log.info("Семантический поиск готов (CPU)")
        return True
    except Exception:  # noqa: BLE001
        _LOAD_FAILED = True
        log.warning("Семантический поиск недоступен — остаётся подстрочный", exc_info=True)
        return False


def _embed(texts: list[str]) -> list[list[float]] | None:
    """Нормированные эмбеддинги (mean-pooling по токенам, как в sentence-transformers)."""
    if not _load():
        return None
    try:
        import torch
        with torch.no_grad():
            enc = _TOK(texts, padding=True, truncation=True, max_length=64, return_tensors="pt")
            out = _MODEL(**enc).last_hidden_state            # [batch, tokens, dim]
            mask = enc["attention_mask"].unsqueeze(-1).float()
            vec = (out * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            vec = torch.nn.functional.normalize(vec, dim=1)
        return [v.tolist() for v in vec]
    except Exception:  # noqa: BLE001
        log.warning("Эмбеддинг не посчитался", exc_info=True)
        return None


def _h(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _cache_load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_VEC_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _CACHE = {}
    return _CACHE


def _cache_save() -> None:
    try:
        _VEC_FILE.write_text(json.dumps(_CACHE), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("semvec cache save failed", exc_info=True)


def _vectors_for(texts: list[str]) -> list[list[float] | None] | None:
    """Векторы текстов через дисковый кэш; считаем только недостающие."""
    cache = _cache_load()
    missing = [t for t in texts if _h(t) not in cache]
    if missing:
        vecs = _embed(missing)
        if vecs is None:
            return None
        for t, v in zip(missing, vecs):
            cache[_h(t)] = v
        _cache_save()
    return [cache.get(_h(t)) for t in texts]


def search(query: str, items: list[dict], top: int = 30) -> list[int] | None:
    """Ранжирует элементы коллекции по смысловой близости к запросу.

    items: [{"id", "label", "material"?}] — текст элемента «label · material».
    Возвращает [id, ...] с косинусом >= порога (по убыванию) или None при недоступности
    модели (вызывающий откатывается на подстрочный поиск)."""
    q = (query or "").strip()
    if not q or not items:
        return None
    with _LOCK:                      # модель и кэш не потокобезопасны — поиск редкий, лок дёшев
        texts = [f"{it.get('label', '')} · {it.get('material', '')}".strip(" ·") for it in items]
        qv = _embed([q])
        iv = _vectors_for(texts)
        if qv is None or iv is None:
            return None
    qvec = qv[0]
    scored = []
    for it, v in zip(items, iv):
        if not v:
            continue
        cos = sum(a * b for a, b in zip(qvec, v))     # векторы нормированы
        scored.append((cos, it["id"]))
    scored.sort(reverse=True)
    if not scored:
        return []
    # Калибровка по живым скорам (2026-06-11): сильные совпадения 0.77–0.84, шум ≤0.54.
    # Абсолютный порог + относительный (не дальше 0.25 от лучшего) режут хвост:
    # «дракон» оставляет только «Яйцо дракона», а не полколлекции.
    cut = max(0.45, scored[0][0] - 0.25)
    return [iid for cos, iid in scored[:top] if cos >= cut]
