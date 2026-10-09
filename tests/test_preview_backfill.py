"""Превью заказов не теряется на тяжёлых моделях (OpenTodo c6d9cebc).

7-стольный флекси-осьминог: коллаж плейт-рендеров не собрался, а полный рендер такого меша
висит минутами — заказ оставался без картинки. Лечим встроенной в .3mf миниатюрой слайсера
(мгновенно) и бэкфиллом уже созданных заказов.
"""
import base64
import zipfile

from app import cache, maintenance, preview, webapp

# 1×1 PNG — достаточно, чтобы extract_3mf_thumbnail вернул непустой файл.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _make_3mf(path, *, entry="Metadata/plate_1.png"):
    """Минимальный .3mf (zip) со встроенной слайсером миниатюрой."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr(entry, _PNG)
    return str(path)


def test_extract_3mf_thumbnail_reads_embedded(tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev", raising=False)
    src = _make_3mf(tmp_path / "octo.3mf")
    out = preview.extract_3mf_thumbnail([src])
    assert out is not None and out.exists() and out.read_bytes() == _PNG


async def test_backfill_fills_missing_preview(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev", raising=False)
    threemf = _make_3mf(tmp_path / "octo2.3mf")

    # заказ без превью, но с .3mf на диске — ровно случай #79
    rid = await tmp_db.add_request(
        user_id=1, user_name="@c", model_label="Флекси-осьминог", printer="P",
        plastic_name="PLA", quantity=1, quality="Standard", strength="Normal",
        grams_total=10, minutes_total=30, price=5, preview_path=None,
        hash=cache.model_hash([threemf]), file_path=threemf, multicolor=1, plates=7)
    assert (await tmp_db.get_request(rid)).preview_path is None

    fixed = await maintenance.backfill_missing_previews()
    assert fixed == 1
    r = await tmp_db.get_request(rid)
    assert r.preview_path and r.preview_path.endswith(".png")


async def test_backfill_skips_orders_with_preview(tmp_db, tmp_path, monkeypatch):
    """Идемпотентность: заказ с уже проставленным превью бэкфилл не трогает."""
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev", raising=False)
    threemf = _make_3mf(tmp_path / "octo3.3mf")
    rid = await tmp_db.add_request(
        user_id=1, user_name="@c", model_label="M", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=1, minutes_total=1, price=1,
        preview_path="/already/set.jpg", file_path=threemf)
    assert await maintenance.backfill_missing_previews() == 0
    assert (await tmp_db.get_request(rid)).preview_path == "/already/set.jpg"


async def test_backfill_skips_non_3mf(tmp_db, tmp_path, monkeypatch):
    """Не .3mf (нет встроенной миниатюры) — бэкфилл его пропускает, а не падает."""
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev", raising=False)
    stl = tmp_path / "m.stl"
    stl.write_bytes(b"solid")
    await tmp_db.add_request(
        user_id=1, user_name="@c", model_label="M", printer="P", plastic_name="PLA",
        quantity=1, quality="Q", strength="S", grams_total=1, minutes_total=1, price=1,
        preview_path=None, file_path=str(stl))
    assert await maintenance.backfill_missing_previews() == 0


async def test_embedded_thumb_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path / "prev", raising=False)
    threemf = _make_3mf(tmp_path / "octo4.3mf")
    got = await webapp._embedded_thumb([threemf])
    assert got and got.endswith(".png")
    assert await webapp._embedded_thumb([]) is None
    assert await webapp._embedded_thumb([str(tmp_path / "nope.stl")]) is None
