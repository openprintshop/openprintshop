"""Тесты кэша слайсинга: ключи и мемоизация хэша файла."""
from app import cache


def _write(p, data: bytes):
    p.write_bytes(data)
    return p


def test_hash_file_memo_and_correctness(tmp_path):
    f = _write(tmp_path / "m.stl", b"solid abc" * 100)
    cache._hash_memo.clear()
    h1 = cache._hash_file(f)
    h2 = cache._hash_file(f)          # из мемо
    assert h1 == h2
    assert len(cache._hash_memo) == 1
    # содержимое поменялось → хэш меняется
    import time
    time.sleep(0.01)
    _write(f, b"solid xyz" * 100)
    h3 = cache._hash_file(f)
    assert h3 != h1


def test_model_hash_order_independent(tmp_path):
    a = _write(tmp_path / "a.stl", b"AAA")
    b = _write(tmp_path / "b.stl", b"BBB")
    assert cache.model_hash([a, b]) == cache.model_hash([b, a])


def test_make_key_deterministic_and_param_sensitive(tmp_path):
    m = _write(tmp_path / "m.stl", b"data123")
    base = dict(machine_profile="MP", process="proc.json", layer_height=0.2,
                infill=20, walls=3, filament="fil.json")
    k1 = cache.make_key([m], **base)
    k2 = cache.make_key([m], **base)
    assert k1 == k2
    k3 = cache.make_key([m], **{**base, "layer_height": 0.3})
    assert k3 != k1   # другой параметр → другой ключ


def test_cache_put_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cache.config, "CACHE_DIR", tmp_path)
    cache.put("key1", {"volume_cm3": 12.5, "minutes": 30.0})
    got = cache.get("key1")
    assert got == {"volume_cm3": 12.5, "minutes": 30.0}
    assert cache.get("nope") is None
