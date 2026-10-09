"""Регрессия на расплющивание профиля процесса (_flatten_process / _build_process_json).

Баг: лист профиля качества (напр. «0.12mm Fine») содержит лишь дельты (layer_height,
wall_loops…), а скорости и ускорения (outer_wall_speed=160, default_acceleration=10000 у CC2)
лежат на 3–5 уровней глубже по цепочке `inherits`. Раньше бот копировал ТОЛЬКО лист и полагался
на резолв наследования в CLI `--load-settings`; для временной копии резолв молча не срабатывал →
скорости/ускорение падали в дефолт → расчётное время печати ×2 (заказ #20: 15ч38м вместо ~8ч,
цена машинного времени задиралась вдвое). Теперь профиль собирается целиком и ни от какого
резолва не зависит.
"""
import json
from pathlib import Path

from app import slicer


def _write(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d), encoding="utf-8")


def _chain(tmp_path: Path) -> Path:
    """Синтетическая цепочка profiles/Vendor/process: leaf -> mid -> root. Возвращает лист."""
    proc = tmp_path / "profiles" / "Vendor" / "process"
    proc.mkdir(parents=True)
    _write(proc / "root.json", {"name": "root", "default_acceleration": "10000",
                                "outer_wall_speed": "160", "travel_speed": "500",
                                "sparse_infill_speed": "200"})
    _write(proc / "mid.json", {"name": "mid", "inherits": "root",
                               "outer_wall_speed": "150"})        # перекрывает root
    leaf = proc / "leaf.json"
    _write(leaf, {"name": "leaf", "inherits": "mid", "layer_height": "0.12",
                  "wall_loops": "3", "setting_id": "SYS123"})
    return leaf


def test_flatten_pulls_speeds_from_parent_chain(tmp_path):
    merged = slicer._flatten_profile(_chain(tmp_path))
    # суть фикса: скорости/ускорение дотянуты из дальних родителей
    assert merged["default_acceleration"] == "10000"
    assert merged["travel_speed"] == "500"
    assert merged["sparse_infill_speed"] == "200"
    # ближний родитель перекрывает дальнего
    assert merged["outer_wall_speed"] == "150"
    # дельты листа сохранены
    assert merged["layer_height"] == "0.12" and merged["wall_loops"] == "3"
    # профиль самодостаточен и не выдаёт себя за системный пресет
    assert "inherits" not in merged and "setting_id" not in merged


def test_flatten_is_not_just_the_leaf(tmp_path):
    """Прямая страховка от регресса «скопировали только лист»: ключей заметно больше, чем в листе."""
    leaf = _chain(tmp_path)
    leaf_keys = set(json.loads(leaf.read_text(encoding="utf-8")))
    merged_keys = set(slicer._flatten_profile(leaf))
    assert {"default_acceleration", "travel_speed", "sparse_infill_speed"} <= merged_keys
    assert len(merged_keys) > len(leaf_keys)


def test_flatten_pulls_machine_kinematics_from_parent(tmp_path):
    """Вторая половина бага: машинные лимиты (max_accel/max_speed), по которым слайсер СЧИТАЕТ
    время, лежат в родителе машинного профиля. Без расплющивания расчётное время раздувалось ~×1.7."""
    mdir = tmp_path / "profiles" / "Vendor" / "machine"
    mdir.mkdir(parents=True)
    _write(mdir / "fdm_machine_common.json", {
        "name": "fdm_machine_common",
        "machine_max_acceleration_x": ["20000", "20000"],
        "machine_max_acceleration_y": ["20000", "20000"],
        "machine_max_speed_x": ["500", "200"], "machine_max_speed_y": ["500", "200"]})
    leaf = mdir / "Printer 0.4 nozzle.json"
    _write(leaf, {"name": "Printer 0.4 nozzle", "inherits": "fdm_machine_common",
                  "machine_max_jerk_x": ["9", "9"], "setting_id": "GM001"})
    m = slicer._flatten_profile(leaf)
    assert m["machine_max_acceleration_x"] == ["20000", "20000"]
    assert m["machine_max_speed_x"] == ["500", "200"]
    assert m["machine_max_jerk_x"] == ["9", "9"]          # лист сохранён
    assert "inherits" not in m and "setting_id" not in m


def test_build_process_json_keeps_profile_settings_no_override(tmp_path):
    """Предвыбор убран (16.07): бот НЕ навязывает layer_height/infill/walls — берёт настройки
    процесс-профиля КАК ЕСТЬ (расплющенные из цепочки inherits), админ выставляет их сам."""
    leaf = _chain(tmp_path)
    out = tmp_path / "proc.json"
    slicer._build_process_json(leaf, out, machine_name="CC2")
    d = json.loads(out.read_text(encoding="utf-8"))
    # расплющивание сохранено: унаследованные скорости/ускорение на месте (без них время ×2)
    assert d["default_acceleration"] == "10000" and d["outer_wall_speed"] == "150"
    # СОБСТВЕННЫЕ значения профиля (из листа), а не навязанные пресетом
    assert d["layer_height"] == "0.12" and d["wall_loops"] == "3"
    # чего в профиле не было (sparse_infill_density) — бот больше не досыпает
    assert "sparse_infill_density" not in d
    assert d["from"] == "User" and d["type"] == "process" and d["name"] == "bot_override"
    assert d["compatible_printers"] == ["CC2"]
    assert "inherits" not in d and "setting_id" not in d
