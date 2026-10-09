"""Тест извлечения картинок столов из .3mf (#03a5727e)."""
import zipfile

from app import preview


def _mk_3mf(tmp_path, plates, name="m.3mf"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        for i in range(1, plates + 1):
            z.writestr(f"Metadata/plate_{i}.png", b"\x89PNG\r\n\x1a\n fake")
        z.writestr("Metadata/plate_no_light_1.png", b"x")   # НЕ картинка стола — не должна попасть
        z.writestr("Metadata/plate_1_small.png", b"x")      # миниатюра — тоже мимо
    return p


def test_extract_two_plates(tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path)
    out = preview.extract_plate_thumbnails([_mk_3mf(tmp_path, 2)])
    assert len(out) == 2


def test_extract_no_plates(tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path)
    p = tmp_path / "plain.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    assert preview.extract_plate_thumbnails([p]) == []


# ---- extract_3mf_thumbnail: фолбэк-обложка из встроенного в 3mf превью ----
def _mk_3mf_with(tmp_path, entries, name="t.3mf"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        for n, data in entries.items():
            z.writestr(n, data)
    return p


def test_thumb_priority_middle_over_plate(tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev")
    p = _mk_3mf_with(tmp_path, {
        "Auxiliaries/.thumbnails/thumbnail_middle.png": b"MIDDLE",
        "Metadata/plate_1.png": b"PLATE",
    })
    out = preview.extract_3mf_thumbnail([p])
    assert out is not None and out.read_bytes() == b"MIDDLE"


def test_thumb_generic_fallback_largest(tmp_path, monkeypatch):
    # нет ни одного из приоритетных имён → берём самый крупный «превьюшный» PNG,
    # игнорируя служебные no_light / pick-маски
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev")
    p = _mk_3mf_with(tmp_path, {
        "Metadata/top_1.png": b"SMALL",
        "Metadata/top_2.png": b"BIGGER-PREVIEW-IMAGE",
        "Metadata/plate_no_light_1.png": b"XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",  # крупнее, но служебный
        "Metadata/pick_1.png": b"YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY",         # тоже мимо
    })
    out = preview.extract_3mf_thumbnail([p])
    assert out is not None and out.read_bytes() == b"BIGGER-PREVIEW-IMAGE"


def test_thumb_none_for_non_3mf_and_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev")
    assert preview.extract_3mf_thumbnail([]) is None
    assert preview.extract_3mf_thumbnail([str(tmp_path / "nope.stl")]) is None
    # 3mf вообще без картинок → None
    plain = _mk_3mf_with(tmp_path, {}, name="plain.3mf")
    assert preview.extract_3mf_thumbnail([plain]) is None


def test_thumb_cache_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev")
    p = _mk_3mf_with(tmp_path, {"Auxiliaries/.thumbnails/thumbnail_middle.png": b"IMG"})
    a = preview.extract_3mf_thumbnail([p])
    b = preview.extract_3mf_thumbnail([p])
    assert a == b and a is not None
