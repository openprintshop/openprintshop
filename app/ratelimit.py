"""Простой in-memory rate-limit со скользящим окном — защита слайсера от спама загрузок/расчётов.

allow(key, max_count, window_sec) -> bool: True, если в окне ещё есть «слот». Память: по списку
таймстампов на ключ (ключ обычно «действие:user_id»); старые записи отбрасываются при проверке.
"""
from __future__ import annotations

import time

_hits: dict[str, list[float]] = {}


def allow(key: str, max_count: int, window_sec: float) -> bool:
    now = time.time()
    cutoff = now - window_sec
    q = _hits.setdefault(key, [])
    while q and q[0] < cutoff:      # выкидываем устаревшие отметки
        q.pop(0)
    if len(q) >= max_count:
        return False
    q.append(now)
    return True


def retry_after(key: str, window_sec: float) -> int:
    """Через сколько секунд освободится слот (для дружелюбного сообщения)."""
    q = _hits.get(key)
    if not q:
        return 0
    return max(0, int(window_sec - (time.time() - q[0])) + 1)
