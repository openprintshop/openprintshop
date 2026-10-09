"""Детектор аварийного крэша слайсера (OpenTodo afe841b8 — коала.3mf валит ElegooSlicer
со STATUS_STACK_OVERFLOW 0xC00000FD)."""
from app.slicer import _looks_like_crash


def test_crash_codes_detected():
    assert _looks_like_crash(0xC00000FD) is True     # STATUS_STACK_OVERFLOW (крэш GL/VRAM)
    assert _looks_like_crash(0xC000041D) is True     # STATUS_FATAL_APP_EXIT (второй код коалы)
    assert _looks_like_crash(0xC0000005) is True     # ACCESS_VIOLATION
    assert _looks_like_crash(-11) is True             # POSIX: убит сигналом (SIGSEGV)


def test_normal_exits_not_crash():
    assert _looks_like_crash(0) is False              # успех
    assert _looks_like_crash(1) is False              # обычная ошибка CLI (не крэш)
    assert _looks_like_crash(None) is False           # процесс ещё не завершился


def test_cli_error_codes_not_retryable_crash():
    """Собственные коды Orca/Elegoo CLI (-1..-255 → 0xFFFFFFxx на Windows) — детерминированные
    ошибки (коллизии деталей, пустой файл), НЕ транзиентный крэш: повторять их бессмысленно."""
    assert _looks_like_crash(0xFFFFFFC1) is False    # -63: «too close/too tall» коллизия (koala --slice 0)
    assert _looks_like_crash(0xFFFFFFEF) is False    # -17: другая ошибка CLI
    assert _looks_like_crash(0xFFFFFFFF) is False    # -1
