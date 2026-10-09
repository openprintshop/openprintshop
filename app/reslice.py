"""Пересохранение исходного проектного .3mf под наш принтер (задача #6 → 463c5e9e).

Слайсер не умеет экспортировать .3mf headless (--export-3mf падает с 0xC0000005 на
OpenGL-рендере превью), поэтому пересобираем .3mf сами: берём оригинальный проект
(как с сайта) и подменяем в Metadata/project_settings.config ТОЛЬКО привязку принтера/
филамента. Родные параметры печати (слой/заполнение/периметры/поддержки) НЕ трогаем
(463c5e9e: «пусть мастер сам решит — авторские настройки уже идеально выставлены»).
Открыв такой .3mf в ElegooSlicer, админ видит проект как задумал автор, но на нашем принтере.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import zipfile
from pathlib import Path

from . import cache, config

log = logging.getLogger(__name__)

_PROJ = "Metadata/project_settings.config"


def resave_with_preset(src, layer_height: float, infill: int, walls: int,
                       printer_name: str = "", filament_name: str = "") -> Path | None:
    """Копия исходного .3mf ПОД НАШ ПРИНТЕР. В Metadata/project_settings.config подменяем
    только printer_settings_id / filament_settings_id (если заданы); родные параметры
    печати проекта сохраняются (463c5e9e). Сигнатура оставлена прежней (layer/infill/walls
    игнорируются) — чтобы не трогать вызовы. None — не проектный .3mf или ошибка."""
    _ = (layer_height, infill, walls)   # больше не вшиваем (463c5e9e) — параметры авторские
    try:
        src = Path(src)
        if src.suffix.lower() != ".3mf" or not src.exists():
            return None
        try:
            key = cache.model_hash([src])
        except Exception:  # noqa: BLE001
            key = src.stem
        out_dir = config.CACHE_DIR / "reslice"
        out_dir.mkdir(parents=True, exist_ok=True)
        sig = "native"
        if printer_name or filament_name:
            sig += "_" + hashlib.sha1((printer_name + "|" + filament_name).encode()).hexdigest()[:8]
        dest = out_dir / f"{key}_{sig}.3mf"
        if dest.exists():
            return dest
        with zipfile.ZipFile(str(src)) as z:
            names = z.namelist()
            if _PROJ not in names:
                return None   # не проектный .3mf — привязывать принтер некуда
            cfg = json.loads(z.read(_PROJ).decode("utf-8", "ignore"))
            if printer_name:                       # под конкретный принтер
                cfg["printer_settings_id"] = printer_name
            if filament_name:
                cfg["filament_settings_id"] = [filament_name]
            tmp = dest.parent / (dest.stem + ".tmp.3mf")
            with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as out:
                for n in names:
                    out.writestr(n, json.dumps(cfg, ensure_ascii=False) if n == _PROJ else z.read(n))
        os.replace(str(tmp), str(dest))
        log.info("re-saved .3mf (родные настройки, printer=%s): %s",
                 printer_name or "—", dest.name)
        return dest
    except Exception:  # noqa: BLE001
        log.warning("resave_with_preset failed for %s", src, exc_info=True)
        return None
