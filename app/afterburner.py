"""Температура CPU из MSI Afterburner — его общая память «MAHMSharedMemory».

Зачем. Температуру камня Windows без драйвера не отдаёт (ACPI-зона на этой машине — константа
290 K), а драйвер уже стоит у того, кто держит Afterburner + RivaTuner: Afterburner опрашивает
датчики раз в секунду и выкладывает всё в именованную общую память для RTSS-оверлея. Читать её
может любой процесс той же сессии без прав администратора — бот и Afterburner оба в сессии
пользователя. Нет Afterburner → None, и экран берёт LibreHardwareMonitor (app/web/screens.py).

Формат — MAHMSharedMemory.h из SDK Afterburner (v2.0): заголовок 32 байта, затем dwNumEntries
записей по dwEntrySize байт: пять строк по MAX_PATH (имя, единицы, локализованные имя и единицы,
формат), float data/min/max, DWORD flags, dwGpu, dwSrcId. «Нет данных» у Afterburner — FLT_MAX.

Что берём: МАКСИМУМ по всем источникам «температура CPU» (dwSrcId = 0x80) — общий `CPU
temperature` и поядерные `CPU1…CPUn temperature`. На Intel поядерные различаются, у Ryzen все
повторяют Tctl — максимум честен в обоих случаях.
"""
from __future__ import annotations

import re
import struct
import sys
import time

_NAME = "MAHMSharedMemory"
_SIG = b"MHAM"                       # 'MAHM' DWORD'ом в little-endian; 0xDEAD — Afterburner вышел
_HDR = struct.Struct("<4sIIIIi")     # signature, version, header size, entries, entry size, time
_MAX_PATH = 260
_OFF_UNITS = _MAX_PATH
_OFF_DATA = 5 * _MAX_PATH            # после пяти строк — float data
_OFF_SRC_ID = _OFF_DATA + 20         # data, min, max, flags, dwGpu → dwSrcId (v2.0)
_SRC_CPU_TEMP = 0x80                 # MONITORING_SOURCE_ID_CPU_TEMPERATURE
_STALE_SEC = 60                      # время последнего опроса старее — Afterburner завис
_MAX_BYTES = 4 << 20                 # защита от мусорного заголовка


def parse_cpu_temp(buf: bytes, now: float | None = None) -> float | None:
    """Максимальная температура CPU (°C) из снимка общей памяти; нет / устарела → None."""
    if len(buf) < _HDR.size:
        return None
    sig, _ver, hdr_size, count, entry_size, polled = _HDR.unpack_from(buf)
    if sig != _SIG or hdr_size < _HDR.size or entry_size < _OFF_DATA + 4:
        return None
    if abs((time.time() if now is None else now) - polled) > _STALE_SEC:
        return None
    temps: list[float] = []
    for i in range(count):
        off = hdr_size + i * entry_size
        if off + entry_size > len(buf):
            break
        if entry_size >= _OFF_SRC_ID + 4:
            is_cpu = struct.unpack_from("<I", buf, off + _OFF_SRC_ID)[0] == _SRC_CPU_TEMP
        else:                        # v1.x: dwSrcId ещё нет — по английскому имени источника
            name = buf[off:off + _MAX_PATH].split(b"\0", 1)[0]
            is_cpu = re.fullmatch(rb"CPU\d* temperature", name) is not None
        if not is_cpu:
            continue
        t = struct.unpack_from("<f", buf, off + _OFF_DATA)[0]
        if buf[off + _OFF_UNITS:off + 2 * _MAX_PATH].split(b"\0", 1)[0].endswith(b"F"):
            t = (t - 32) * 5 / 9     # в настройках Afterburner выбраны °F
        if 0 < t < 150:              # FLT_MAX = «нет данных»
            temps.append(t)
    return max(temps) if temps else None


def _snapshot() -> bytes | None:
    """Копия общей памяти Afterburner или None, если он не запущен. Только чтение: открываем
    СУЩЕСТВУЮЩЕЕ отображение (OpenFileMapping), своё никогда не создаём — иначе Afterburner,
    стартовав позже, получил бы наше пустое."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t)
    k32.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.VirtualQuery.restype = ctypes.c_size_t

    class MBI(ctypes.Structure):     # MEMORY_BASIC_INFORMATION (x64)
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]

    k32.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t)
    FILE_MAP_READ = 4
    h = k32.OpenFileMappingW(FILE_MAP_READ, False, _NAME)
    if not h:
        return None
    try:
        view = k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not view:
            return None
        try:
            mbi = MBI()
            if not k32.VirtualQuery(view, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                return None
            size = min(int(mbi.RegionSize), _MAX_BYTES)   # дальше конца вида читать нельзя
            if size < _HDR.size:
                return None
            _sig, _ver, hdr_size, count, entry_size, _t = _HDR.unpack(ctypes.string_at(view, _HDR.size))
            return ctypes.string_at(view, min(size, hdr_size + count * entry_size))
        finally:
            k32.UnmapViewOfFile(view)
    finally:
        k32.CloseHandle(h)


def read_cpu_temp() -> float | None:
    """Температура CPU из запущенного MSI Afterburner; не Windows / не запущен / сбой → None."""
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        return None
    try:
        buf = _snapshot()
    except (OSError, ValueError, struct.error):
        return None
    return parse_cpu_temp(buf) if buf else None
