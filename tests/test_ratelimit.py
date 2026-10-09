"""Тесты rate-limit (скользящее окно)."""
import app.ratelimit as rl


def test_allow_blocks_after_limit():
    rl._hits.clear()
    assert all(rl.allow("k:1", 3, 60) for _ in range(3))
    assert rl.allow("k:1", 3, 60) is False


def test_keys_independent():
    rl._hits.clear()
    assert rl.allow("a", 1, 60) is True
    assert rl.allow("a", 1, 60) is False
    assert rl.allow("b", 1, 60) is True


def test_window_expiry(monkeypatch):
    rl._hits.clear()
    t = [1000.0]
    monkeypatch.setattr(rl.time, "time", lambda: t[0])
    assert rl.allow("k", 2, 10)
    assert rl.allow("k", 2, 10)
    assert rl.allow("k", 2, 10) is False
    t[0] += 11
    assert rl.allow("k", 2, 10) is True
