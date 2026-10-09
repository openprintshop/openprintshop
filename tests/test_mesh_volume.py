"""Тесты память-безопасного потокового объёма тела (app.mesh_volume) на синтетических .3mf.

Проверяем: обычный inline-меш, production-extension с компонентами, влияние масштаба в
трансформах (объём × |det|), учёт ТОЛЬКО размещённых в <build> объектов, и — если рядом лежит
реальный «коала.3mf» — сквозную оценку тяжёлого AMS-проекта без загрузки всех мешей."""
import zipfile
from pathlib import Path

import pytest

from app import config, mesh_volume

# Куб 10 мм: объём = 1000 мм³ = 1.0 см³. 8 вершин, 12 треугольников (наружу).
_CUBE_V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
           (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_CUBE_T = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
           (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _mesh_xml() -> str:
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % t for t in _CUBE_V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tr for tr in _CUBE_T)
    return "<mesh><vertices>%s</vertices><triangles>%s</triangles></mesh>" % (v, t)


def _write_zip(path: Path, files: dict) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in files.items():
            z.writestr(arc, data)


_CORE_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
              'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">\n')


def test_inline_cube(tmp_path):
    """Обычный .3mf с inline-мешем: куб 10 мм → 1.0 см³."""
    core = (_CORE_HEAD + " <resources>\n"
            '  <object id="1" type="model">' + _mesh_xml() + "</object>\n"
            " </resources>\n <build>\n"
            '  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
            " </build>\n</model>\n")
    p = tmp_path / "cube.3mf"
    _write_zip(p, {"3D/3dmodel.model": core})
    v = mesh_volume.solid_volume_cm3(p)
    assert v == pytest.approx(1.0, abs=1e-6)


def test_production_component_scale(tmp_path):
    """Production-extension: компонент с масштабом 2× → объём × 8 (|det|=8) = 8.0 см³."""
    core = (_CORE_HEAD + " <resources>\n"
            '  <object id="2" type="model"><components>'
            '<component p:path="/3D/Objects/sub.model" objectid="1" '
            'transform="2 0 0 0 2 0 0 0 2 0 0 0"/></components></object>\n'
            " </resources>\n <build>\n"
            '  <item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
            " </build>\n</model>\n")
    sub = (_CORE_HEAD + ' <resources>\n  <object id="1" type="model">' + _mesh_xml()
           + "</object>\n </resources>\n <build/>\n</model>\n")
    p = tmp_path / "prod.3mf"
    _write_zip(p, {"3D/3dmodel.model": core, "3D/Objects/sub.model": sub})
    v = mesh_volume.solid_volume_cm3(p)
    assert v == pytest.approx(8.0, abs=1e-6)


def test_build_item_scale_multiplies(tmp_path):
    """Масштаб build-item тоже влияет: компонент 2× и item 2× → 8×8 = 64 см³."""
    core = (_CORE_HEAD + " <resources>\n"
            '  <object id="2" type="model"><components>'
            '<component p:path="/3D/Objects/sub.model" objectid="1" '
            'transform="2 0 0 0 2 0 0 0 2 0 0 0"/></components></object>\n'
            " </resources>\n <build>\n"
            '  <item objectid="2" transform="2 0 0 0 2 0 0 0 2 0 0 0"/>\n'
            " </build>\n</model>\n")
    sub = (_CORE_HEAD + ' <resources>\n  <object id="1" type="model">' + _mesh_xml()
           + "</object>\n </resources>\n <build/>\n</model>\n")
    p = tmp_path / "prod2.3mf"
    _write_zip(p, {"3D/3dmodel.model": core, "3D/Objects/sub.model": sub})
    v = mesh_volume.solid_volume_cm3(p)
    assert v == pytest.approx(64.0, abs=1e-5)


def test_unplaced_object_ignored(tmp_path):
    """Объект-ресурс, которого НЕТ в <build>, не печатается и в объём не входит."""
    core = (_CORE_HEAD + " <resources>\n"
            '  <object id="1" type="model">' + _mesh_xml() + "</object>\n"
            '  <object id="2" type="model">' + _mesh_xml() + "</object>\n"
            " </resources>\n <build>\n"
            '  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
            " </build>\n</model>\n")
    p = tmp_path / "partial.3mf"
    _write_zip(p, {"3D/3dmodel.model": core})
    v = mesh_volume.solid_volume_cm3(p)
    assert v == pytest.approx(1.0, abs=1e-6)   # только объект 1, не 2


def test_multi_instance_sum(tmp_path):
    """Несколько компонентов у одного объекта суммируются (2 куба = 2.0 см³)."""
    core = (_CORE_HEAD + " <resources>\n"
            '  <object id="3" type="model"><components>'
            '<component p:path="/3D/Objects/sub.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<component p:path="/3D/Objects/sub.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 20 0 0"/>'
            "</components></object>\n </resources>\n <build>\n"
            '  <item objectid="3" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n </build>\n</model>\n')
    sub = (_CORE_HEAD + ' <resources>\n  <object id="1" type="model">' + _mesh_xml()
           + "</object>\n </resources>\n <build/>\n</model>\n")
    p = tmp_path / "multi.3mf"
    _write_zip(p, {"3D/3dmodel.model": core, "3D/Objects/sub.model": sub})
    v = mesh_volume.solid_volume_cm3(p)
    assert v == pytest.approx(2.0, abs=1e-6)


def test_non_3mf_and_garbage(tmp_path):
    """Не-.3mf и битый zip → None (вызывающий откатится на trimesh/слайс)."""
    assert mesh_volume.solid_volume_cm3(tmp_path / "nope.stl") is None
    assert mesh_volume.solid_volume_cm3(tmp_path / "missing.3mf") is None
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    assert mesh_volume.solid_volume_cm3(bad) is None


def test_matches_trimesh_on_cube(tmp_path):
    """Знаковая сумма тетраэдров == объём trimesh для того же куба (страховка формулы)."""
    trimesh = pytest.importorskip("trimesh")
    import numpy as np
    core = (_CORE_HEAD + " <resources>\n"
            '  <object id="1" type="model">' + _mesh_xml() + "</object>\n"
            " </resources>\n <build>\n"
            '  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n </build>\n</model>\n')
    p = tmp_path / "cube2.3mf"
    _write_zip(p, {"3D/3dmodel.model": core})
    tm = trimesh.Trimesh(vertices=np.array(_CUBE_V, float), faces=np.array(_CUBE_T, int), process=False)
    assert mesh_volume.solid_volume_cm3(p) == pytest.approx(tm.volume / 1000.0, abs=1e-6)


# --- Реальная коала: только если файл на месте (продовый артефакт, не в репозитории) ---
_KOALA = config.DATA_DIR / "vk_catalog" / "55350717" / "7111" / "коала.3mf"


@pytest.mark.skipif(not _KOALA.exists(), reason="коала.3mf отсутствует (продовый файл)")
def test_koala_streaming_memory_safe():
    """Тяжёлый AMS-проект (~573 МБ меша) считается потоково без OOM; объём в разумном диапазоне."""
    v = mesh_volume.solid_volume_cm3(_KOALA)
    assert v is not None
    assert 240.0 < v < 247.0   # измерено 243.56 см³ (сверено с trimesh поштучно)
