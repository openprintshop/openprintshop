"""Тесты #6/463c5e9e: пересохранение .3mf под наш принтер (на синтетических zip)."""
import json
import zipfile

from app import inspect3mf, reslice


def _mk_project(tmp_path, cfg, name="proj.3mf"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(cfg))
        z.writestr("3D/3dmodel.model", "<model/>")
    return p


def test_resave_keeps_native_settings(tmp_path, monkeypatch):
    """463c5e9e: родные параметры печати НЕ трогаем, подменяется только принтер/филамент."""
    monkeypatch.setattr(reslice.config, "CACHE_DIR", tmp_path)   # не писать в реальный кэш
    src = _mk_project(tmp_path, {"layer_height": "0.2", "sparse_infill_density": "8%",
                                 "wall_loops": "3", "printer_settings_id": "чужой принтер"})
    out = reslice.resave_with_preset(src, 0.12, 25, 5, printer_name="Наш CC2",
                                     filament_name="Elegoo PLA @ECC2")
    assert out is not None and out.exists()
    with zipfile.ZipFile(out) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    assert cfg["layer_height"] == "0.2"                   # авторские настройки сохранены
    assert cfg["sparse_infill_density"] == "8%"
    assert cfg["wall_loops"] == "3"
    assert cfg["printer_settings_id"] == "Наш CC2"        # принтер — наш
    assert cfg["filament_settings_id"] == ["Elegoo PLA @ECC2"]


def test_has_project_settings(tmp_path):
    """Детектор проекта-3mf для нативного слайса (463c5e9e)."""
    src = _mk_project(tmp_path, {"layer_height": "0.2"})
    assert inspect3mf.has_project_settings(src) is True
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    assert inspect3mf.has_project_settings(plain) is False
    assert inspect3mf.has_project_settings(tmp_path / "нет.3mf") is False


def test_resave_non_project_3mf_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(reslice.config, "CACHE_DIR", tmp_path)
    p = tmp_path / "plain.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")   # нет project_settings.config
    assert reslice.resave_with_preset(p, 0.2, 15, 2) is None


def test_resave_non_3mf_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(reslice.config, "CACHE_DIR", tmp_path)
    p = tmp_path / "x.stl"
    p.write_bytes(b"solid")
    assert reslice.resave_with_preset(p, 0.2, 15, 2) is None
