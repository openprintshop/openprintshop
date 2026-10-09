"""B6: конвертер STEP/STP → STL через FreeCAD (freecadcmd, headless).

Инженерные форматы клиенты присылают регулярно (DigiFabster/PrintQuote их берут), а
trimesh их не читает. Конвертирует FreeCAD: подпроцесс freecadcmd с одноразовым скриптом
(Part.Shape.read → MeshPart.meshFromShape → Mesh.write). Санкция владельца на зависимость —
21.08.2026 («качай»), портативная сборка живёт вне репозитория.

Путь к freecadcmd.exe — настройка FREECAD_CMD в .env; без неё пробуем типовое место этой
машины. Конвертера нет/сломался → downloader честно отказывает прежним CAD_MSG: деградация
до поведения «до B6», а не молчаливая поломка (урок opentodo-batch-2026-07-29 про .step).

Параметры меша (LinearDeflection=0.1 мм, AngularDeflection=0.35 рад ≈ 20°) — стандартные
у FreeCAD; проверены запуском 21.08: куб с цилиндрическим вырезом → 160 граней, водонепро-
ницаемый STL. Точность выше не нужна: STL уходит в тот же конвейер, что клиентские STL.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

CONVERT_TIMEOUT_S = 180   # STEP тяжёлой сборки мешится десятки секунд; зависший FreeCAD — убить

# Где искать freecadcmd, если в .env не задано. Раньше здесь стоял АБСОЛЮТНЫЙ путь к
# портативной сборке на машине разработчика: у чужой студии он не существует, а на этой
# «находился» даже из чужой копии проекта — смок вёл себя по-разному в зависимости от
# машины (поймано прогоном стенда 23.08.2026). Теперь ищем: портативная сборка рядом с
# проектом → обычная установка → PATH.
_CMD_GLOBS = ("slicers/freecad/*/bin/freecadcmd.exe", "slicers/freecad/bin/freecadcmd.exe")
_CMD_INSTALLED = ("FreeCAD*/bin/freecadcmd.exe",)


def _discover_cmd() -> str:
    """Первый существующий freecadcmd: рядом с проектом → обычная установка → PATH."""
    for base in (config.BASE_DIR, config.BASE_DIR.parent):
        for pat in _CMD_GLOBS:
            for hit in sorted(base.glob(pat)):
                if hit.is_file():
                    return str(hit)
    for root in (os.environ.get('ProgramFiles', ''), os.environ.get('ProgramFiles(x86)', '')):
        if not root:
            continue
        for pat in _CMD_INSTALLED:
            for hit in sorted(Path(root).glob(pat)):
                if hit.is_file():
                    return str(hit)
    return shutil.which("freecadcmd") or ""

_SCRIPT = """\
import sys
import Part
import MeshPart
src, dst = sys.argv[-2], sys.argv[-1]
shape = Part.Shape()
shape.read(src)
mesh = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.1, AngularDeflection=0.35,
                              Relative=False)
if not mesh.CountFacets:
    raise RuntimeError("empty mesh")
mesh.write(dst)
print("STEPCONV-OK", mesh.CountFacets)
"""


class ConvertError(Exception):
    """STEP не сконвертировался (битый файл, таймаут, FreeCAD упал)."""


def freecad_cmd() -> str:
    """Путь к freecadcmd.exe: настройка бьёт типовое место; пустая строка = нет конвертера."""
    p = (getattr(config, "FREECAD_CMD", "") or "").strip()
    if p:
        return p if Path(p).is_file() else ""
    return _discover_cmd()


def available() -> bool:
    return bool(freecad_cmd())


def convert_file(src: Path | str, out_dir: Path | str | None = None) -> Path:
    """STEP/STP → STL рядом с исходником (или в out_dir). Блокирующая — звать через
    asyncio.to_thread. Возвращает путь STL, бросает ConvertError."""
    cmd = freecad_cmd()
    if not cmd:
        raise ConvertError("конвертер STEP не настроен")
    src = Path(src)
    dst = Path(out_dir or src.parent) / (src.stem + ".stl")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as f:
        f.write(_SCRIPT)
        script = f.name
    t0 = time.monotonic()
    try:
        # freecadcmd пишет прогресс сохранения в stdout процентами — глушим, ловим маркер
        r = subprocess.run([cmd, script, str(src), str(dst)],
                           capture_output=True, text=True, timeout=CONVERT_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise ConvertError(f"FreeCAD не уложился в {CONVERT_TIMEOUT_S} с") from e
    finally:
        Path(script).unlink(missing_ok=True)
    if "STEPCONV-OK" not in (r.stdout or "") or not dst.is_file() or not dst.stat().st_size:
        tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["<пусто>"])[-1][:200]
        raise ConvertError(f"FreeCAD не разобрал файл: {tail}")
    log.info("STEP→STL: %s за %.1f c", src.name, time.monotonic() - t0)
    return dst
