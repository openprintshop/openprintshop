"""Разбор общей памяти MSI Afterburner (app/afterburner.py) на синтетических снимках.

Раскладка повторяет MAHMSharedMemory.h v2.0 — снятую 24.09.2026 с живого Afterburner владельца:
Ryzen 9 7950X даёт общий `CPU temperature` и 32 поточных `CPUn temperature`, все равны Tctl.
"""
import struct

from app import afterburner

NOW = 1_790_000_000
FLT_MAX = 3.4028234663852886e38


def _entry(name, units, value, src_id, gpu=0, size=1324):
    s = lambda t: t.encode("cp1251").ljust(260, b"\0")  # noqa: E731
    e = s(name) + s(units) + s(name) + s(units) + s("%.0f")
    e += struct.pack("<fffI", value, 0.0, 100.0, 0)
    if size >= 1324:
        e += struct.pack("<II", gpu, src_id)
    return e[:size]


def _snap(entries, sig=b"MHAM", polled=NOW, esz=1324):
    hdr = struct.pack("<4sIIIIiII", sig, 0x20000, 32, len(entries), esz, polled, 0, 0)
    return hdr + b"".join(entries)


def test_max_over_cpu_temperature_sources():
    buf = _snap([
        _entry("GPU1 temperature", "°C", 95.0, 0x00),          # GPU — не CPU
        _entry("CPU1 temperature", "°C", 61.0, 0x80, gpu=0),
        _entry("CPU2 temperature", "°C", 72.5, 0x80, gpu=1),
        _entry("CPU temperature", "°C", 70.0, 0x80, gpu=0xFFFFFFFF),
        _entry("CPU usage", "%", 99.0, 0x90),
        _entry("CPU3 temperature", "°C", FLT_MAX, 0x80, gpu=2),  # «нет данных»
    ])
    assert afterburner.parse_cpu_temp(buf, now=NOW) == 72.5


def test_stale_or_closed_afterburner_is_none():
    entries = [_entry("CPU temperature", "°C", 70.0, 0x80)]
    assert afterburner.parse_cpu_temp(_snap(entries, polled=NOW - 600), now=NOW) is None
    dead = struct.pack("<I", 0xDEAD)
    assert afterburner.parse_cpu_temp(_snap(entries, sig=dead), now=NOW) is None


def test_v1_entries_matched_by_name():
    """Старый формат без dwSrcId (запись 1316 байт) — CPU узнаём по имени источника."""
    buf = _snap([_entry("CPU temperature", "°C", 64.0, 0, size=1316),
                 _entry("CPU4 temperature", "°C", 69.0, 0, size=1316),
                 _entry("GPU temperature", "°C", 88.0, 0, size=1316)], esz=1316)
    assert afterburner.parse_cpu_temp(buf, now=NOW) == 69.0


def test_fahrenheit_units():
    buf = _snap([_entry("CPU temperature", "°F", 158.0, 0x80)])
    assert afterburner.parse_cpu_temp(buf, now=NOW) == 70.0


def test_garbage_is_none():
    assert afterburner.parse_cpu_temp(b"", now=NOW) is None
    assert afterburner.parse_cpu_temp(b"\0" * 64, now=NOW) is None
    # заголовок обещает 10 записей, а в снимке ни одной — не падаем
    hdr = struct.pack("<4sIIIIiII", b"MHAM", 0x20000, 32, 10, 1324, NOW, 0, 0)
    assert afterburner.parse_cpu_temp(hdr, now=NOW) is None
