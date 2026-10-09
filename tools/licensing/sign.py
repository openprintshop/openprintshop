"""Подпись лицензий Ed25519 — часть инструментов ВЛАДЕЛЬЦА (в продукте её нет).

Примитивы кривой переиспользуются из app/license.py: там они уже есть для проверки,
и вторая копия арифметики — гарантированный источник расхождений. В клиентской сборке
живёт только проверка; подписывать без приватного seed всё равно невозможно.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.license import _B, _L, _point_mul, encode_point  # noqa: E402


def _secret_scalar(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("seed приватного ключа обязан быть 32 байта (64 hex-символа)")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8          # clamp по RFC 8032
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _secret_scalar(seed)
    return encode_point(_point_mul(_B, a))


def public_key_hex(seed: bytes) -> str:
    return public_key(seed).hex()


def sign(msg: bytes, seed: bytes) -> bytes:
    a, prefix = _secret_scalar(seed)
    pub = encode_point(_point_mul(_B, a))
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % _L
    rp = encode_point(_point_mul(_B, r))
    k = int.from_bytes(hashlib.sha512(rp + pub + msg).digest(), "little") % _L
    s = (r + k * a) % _L
    return rp + int.to_bytes(s, 32, "little")


def load_seed(path: str | Path) -> bytes:
    """Приватный ключ из файла (hex в одну строку). Никаких значений по умолчанию:
    путь всегда приходит параметром, чтобы seed не искался внутри дерева проекта."""
    raw = Path(path).expanduser().read_text(encoding="ascii").strip()
    try:
        seed = bytes.fromhex(raw)
    except ValueError as e:
        raise ValueError(f"{path}: это не hex-строка приватного ключа") from e
    if len(seed) != 32:
        raise ValueError(f"{path}: приватный ключ должен быть 32 байта (64 hex-символа)")
    return seed
