"""macOS-мусор из чужих архивов (AppleDouble ._*, __MACOSX) не должен попадать в модели:
слайсер на нём падает («No layers detected», exit -6/-100) — фидбэк владельца 2026-07-11."""

from app import downloader


def test_collect_meshes_skips_appledouble(tmp_path):
    (tmp_path / "model.stl").write_bytes(b"solid x")
    (tmp_path / "._model.stl").write_bytes(b"\x00\x05\x16\x07junk")   # AppleDouble-заголовок
    macos = tmp_path / "__MACOSX"
    macos.mkdir()
    (macos / "other.stl").write_bytes(b"junk")
    got = downloader._collect_meshes(tmp_path)
    assert [p.name for p in got] == ["model.stl"]
