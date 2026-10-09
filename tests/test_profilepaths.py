"""C2 (хвост): профили слайсера в БД — относительные, с маркером корня установки.

Что здесь закреплено:

* обнаружение корней движка (`slicers.profile_roots`) на ПОДМЕНЁННОМ каталоге установки —
  включая портативную сборку (движок рядом с проектом) и полное отсутствие движка;
* `profilepaths.rel_profile`/`abs_profile` — round-trip, идемпотентность, дословное
  сохранение всего, что маркером не является (относительные «mp.json», чужие абсолютные
  пути, пустая строка и None), и перекоренение, которое ОБЯЗАНО срабатывать только когда
  перекоренённый файл реально существует;
* независимость от трека данных: `rel_data` профиль не трогает, `rel_profile` — данные;
* шаг схемы v9 на фикстурной БД со значениями старого вида: переписывается только то, что
  лежит под корнями, путь вне корней остаётся байт-в-байт, повтор ничего не меняет;
* сквозной смок «карточка принтера сохранена на одной машине и прочитана на другой»:
  каталог установки другой, а расчёт получает существующий путь.

Изоляция: каталоги установки и %APPDATA% подменяются ЦЕЛИКОМ на tmp_path, поэтому ни один
тест не зависит от того, какие слайсеры стоят на машине разработчика.
"""
import json
from pathlib import Path

import pytest

from app import config, db, profilepaths, slicers


def _mk(p: Path, text: str = "{}") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def install(tmp_path, monkeypatch):
    """Фикстурная установка ElegooSlicer: три корня + %APPDATA% целиком в tmp_path."""
    inst = tmp_path / "Program Files" / "ElegooSlicer"
    res = inst / "resources" / "profiles"
    exe = _mk(inst / "elegoo-slicer.exe", "")
    ad = tmp_path / "appdata"
    sysroot = ad / "ElegooSlicer" / "system"
    user = ad / "ElegooSlicer" / "user" / "default"
    _mk(res / "Elegoo" / "machine" / "ECC2" / "m.json")
    _mk(res / "Elegoo" / "filament" / "ECC2" / "f.json")
    (res / "Elegoo" / "process" / "ECC2").mkdir(parents=True, exist_ok=True)
    _mk(sysroot / "Elegoo" / "machine" / "ECC2" / "only_ota.json")
    _mk(user / "filament" / "0.6 Elegoo PETG.json")
    monkeypatch.setenv("APPDATA", str(ad))
    monkeypatch.setattr(config, "SLICER_PATH", exe)
    monkeypatch.setattr(config, "PROFILES_DIR", res / "Elegoo")   # .env указывает на ВЕНДОРА
    monkeypatch.setattr(config, "SLICER_USER_DIR", user)
    return {"exe": exe, "resources": res, "system": sysroot, "user": user, "root": tmp_path}


# ─────────────────────────────── корни движка ───────────────────────────────
def test_profile_roots_finds_three_roots(install):
    roots = slicers.profile_roots("elegoo")
    assert roots == {"resources": install["resources"], "system": install["system"],
                     "user": install["user"]}
    # config.PROFILES_DIR указывает на вендора — корень обязан подняться на уровень выше,
    # иначе маркер разъедется на один сегмент и обратно не резолвится.
    assert roots["resources"].name == "profiles"


def test_profile_roots_empty_without_slicer(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "нет такого"))
    monkeypatch.setattr(config, "SLICER_PATH", tmp_path / "нет" / "elegoo-slicer.exe")
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "нет" / "profiles" / "Elegoo")
    monkeypatch.setattr(config, "SLICER_USER_DIR", tmp_path / "нет" / "user" / "default")
    assert slicers.profile_roots("elegoo") == {}
    assert slicers.profile_roots("orca") == {}


def test_profile_roots_follows_portable_exe(tmp_path, monkeypatch):
    """Портативная сборка: корень профилей лежит рядом с exe, а не в Program Files."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "нет такого"))
    exe = _mk(tmp_path / "slicers" / "orca" / "orca-slicer.exe", "")
    res = tmp_path / "slicers" / "orca" / "resources" / "profiles"
    _mk(res / "Elegoo" / "machine" / "ECC2" / "m.json")
    assert slicers.profile_roots("orca", str(exe)) == {"resources": res}


# ─────────────────────────────── rel/abs ───────────────────────────────
def test_rel_abs_round_trip_resources_and_user(install):
    m = install["resources"] / "Elegoo" / "machine" / "ECC2" / "m.json"
    rel = profilepaths.rel_profile(m)
    assert rel == "@elegoo/resources/Elegoo/machine/ECC2/m.json"
    assert profilepaths.abs_profile(rel) == m
    f = install["user"] / "filament" / "0.6 Elegoo PETG.json"
    relf = profilepaths.rel_profile(str(f))
    assert relf == "@elegoo/user/filament/0.6 Elegoo PETG.json"
    assert profilepaths.abs_profile(relf) == f
    # идемпотентность в обе стороны
    assert profilepaths.rel_profile(rel) == rel
    assert profilepaths.rel_profile(profilepaths.abs_profile_str(rel)) == rel


def test_non_marker_values_pass_through_verbatim(install):
    for v in ("mp.json", "proc", "profiles/x.json", r"D:\чужое\slicer\x.json", ""):
        assert profilepaths.rel_profile(v) == v
        assert profilepaths.abs_profile_str(v) == v
    assert profilepaths.rel_profile(None) is None
    assert profilepaths.abs_profile_str(None) == ""


def test_bare_root_marker_is_read_as_default_kind(install):
    """Совместимость вперёд: «@resources/…» без движка — движок по умолчанию."""
    assert profilepaths.abs_profile("@resources/Elegoo/machine/ECC2/m.json") \
        == install["resources"] / "Elegoo" / "machine" / "ECC2" / "m.json"


def test_reroots_between_resources_and_system(install):
    """Хвост у поставки и её OTA-зеркала одинаков — файл найдётся во втором корне."""
    marker = "@elegoo/resources/Elegoo/machine/ECC2/only_ota.json"
    got = profilepaths.abs_profile(marker)
    assert got == install["system"] / "Elegoo" / "machine" / "ECC2" / "only_ota.json"
    assert got.exists()


def test_reroots_absolute_path_from_another_machine(install):
    """БД приехала без миграции: абсолютный путь чужой установки перекореняется на нашу."""
    alien = r"D:\Slicer64\ElegooSlicer\resources\profiles\Elegoo\machine\ECC2\m.json"
    assert profilepaths.abs_profile(alien) \
        == install["resources"] / "Elegoo" / "machine" / "ECC2" / "m.json"
    alien_user = r"E:\Users\кто-то\AppData\Roaming\ElegooSlicer\user\default\filament\0.6 Elegoo PETG.json"
    assert profilepaths.abs_profile(alien_user) \
        == install["user"] / "filament" / "0.6 Elegoo PETG.json"


def test_no_rerooting_when_target_missing(install):
    """Перекоренять можно ТОЛЬКО когда перекоренённый файл существует (дисциплина abs_data):
    иначе чужой путь с похожим хвостом увёл бы расчёт в боевую установку слайсера."""
    alien = r"D:\Slicer64\ElegooSlicer\resources\profiles\Elegoo\machine\ECC2\нет такого.json"
    assert profilepaths.abs_profile_str(alien) == alien


def test_data_and_profile_tracks_do_not_overlap(install, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    m = str(install["resources"] / "Elegoo" / "machine" / "ECC2" / "m.json")
    assert config.rel_data(m) == m, "rel_data профиль слайсера не трогает"
    photo = str(tmp_path / "data" / "previews" / "p.png")
    assert profilepaths.rel_profile(photo) == photo, "rel_profile путь данных не трогает"


def test_explain_tells_why(install):
    ok = install["resources"] / "Elegoo" / "machine" / "ECC2" / "m.json"
    assert profilepaths.explain(ok) == ""
    assert profilepaths.explain("") == ""
    gone = "@elegoo/user/filament/нет такого.json"
    assert "нет файла под корнем" in profilepaths.explain(gone)
    assert "не из установки слайсера" in profilepaths.explain(r"D:\чужое\x.json")


# ─────────────────────────────── миграция v9 ───────────────────────────────
async def test_v9_profiles_migrate_to_relative_and_is_idempotent(install, tmp_path, monkeypatch):
    """Шаг v9 переписывает ТОЛЬКО пути под корнями установки и переживает повтор."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "prof.db")
    outside = r"D:\чужие профили\slicer\my.json"
    m = str(install["resources"] / "Elegoo" / "machine" / "ECC2" / "m.json")
    f = str(install["user"] / "filament" / "0.6 Elegoo PETG.json")
    try:
        await db.init()
        c = db._conn()
        pid = (await (await c.execute("SELECT MIN(id) AS i FROM printers")).fetchone())["i"]
        await c.execute("UPDATE printers SET machine_profile=?, process_dir=?, filament_dir=? "
                        "WHERE id=?", (m, str(install["resources"] / "Elegoo" / "process" / "ECC2"),
                                       str(install["resources"] / "Elegoo" / "filament" / "ECC2"),
                                       pid))
        await c.execute("UPDATE printer_nozzles SET machine_profile=? WHERE printer_id=?",
                        (m, pid))
        await c.execute("UPDATE plastics SET filament_profile=? WHERE id=(SELECT MIN(id) "
                        "FROM plastics)", (f,))
        for i, path in enumerate((f, outside)):
            await c.execute("INSERT INTO calib_presets(kind, name, path, sha, body, mtime, gone, "
                            "at) VALUES('filament',?,?,?,'{}',0,0,0)", (f"p{i}", path, f"s{i}"))
        await c.execute(
            "INSERT INTO calib_passport(plastic_id, printer_id, nozzle, values_json, source_json, "
            "presets_json, status, calibrated_at, updated_at) VALUES(1,NULL,'0.4','{}','{}',?,"
            "'calibrated',0,0)", (json.dumps({"filament": f, "machine": outside}),))
        await c.execute("PRAGMA user_version = 8")
        await c.commit()
        await db.close()

        await db.init()

        async def one(sql, *a):
            row = await (await db._conn().execute(sql, a)).fetchone()
            return row[0] if row else None

        assert await one("PRAGMA user_version") == db.SCHEMA_VERSION
        assert await one("SELECT machine_profile FROM printers WHERE id=?", pid) \
            == "@elegoo/resources/Elegoo/machine/ECC2/m.json"
        assert await one("SELECT process_dir FROM printers WHERE id=?", pid) \
            == "@elegoo/resources/Elegoo/process/ECC2"
        assert await one("SELECT machine_profile FROM printer_nozzles WHERE printer_id=?", pid) \
            == "@elegoo/resources/Elegoo/machine/ECC2/m.json"
        assert await one("SELECT filament_profile FROM plastics WHERE filament_profile <> '' "
                         "ORDER BY id LIMIT 1") == "@elegoo/user/filament/0.6 Elegoo PETG.json"
        assert await one("SELECT path FROM calib_presets WHERE name='p0'") \
            == "@elegoo/user/filament/0.6 Elegoo PETG.json"
        assert await one("SELECT path FROM calib_presets WHERE name='p1'") == outside, \
            "путь вне корней остаётся дословным"
        pres = json.loads(await one("SELECT presets_json FROM calib_passport LIMIT 1"))
        assert pres["filament"] == "@elegoo/user/filament/0.6 Elegoo PETG.json"
        assert pres["machine"] == outside

        # повтор ничего не меняет
        await db.close()
        await db.init()
        assert await one("SELECT machine_profile FROM printers WHERE id=?", pid) \
            == "@elegoo/resources/Elegoo/machine/ECC2/m.json"
        assert await one("SELECT path FROM calib_presets WHERE name='p1'") == outside
    finally:
        await db.close()


# ─────────────────────────────── сквозной смок «другая машина» ───────────────────────────────
async def test_printer_card_survives_move_to_another_machine(install, tmp_path, monkeypatch):
    """Карточка сохранена на машине владельца, а прочитана на машине лицензиата."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "move.db")
    m = install["resources"] / "Elegoo" / "machine" / "ECC2" / "m.json"
    try:
        await db.init()
        pid = await db.add_printer("Стенд", str(m),
                                   str(install["resources"] / "Elegoo" / "process" / "ECC2"),
                                   str(install["resources"] / "Elegoo" / "filament" / "ECC2"))
        row = await (await db._conn().execute(
            "SELECT machine_profile FROM printers WHERE id=?", (pid,))).fetchone()
        assert row[0].startswith("@elegoo/resources/"), "в БД — маркер корня, не абсолютный путь"

        # ── другая машина: слайсер стоит в другом каталоге, под другим пользователем ──
        other = tmp_path / "D диск" / "ElegooSlicer"
        res2 = other / "resources" / "profiles"
        _mk(other / "elegoo-slicer.exe", "")
        _mk(res2 / "Elegoo" / "machine" / "ECC2" / "m.json")
        (res2 / "Elegoo" / "process" / "ECC2").mkdir(parents=True, exist_ok=True)
        (res2 / "Elegoo" / "filament" / "ECC2").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("APPDATA", str(tmp_path / "нет такого"))
        monkeypatch.setattr(config, "SLICER_PATH", other / "elegoo-slicer.exe")
        monkeypatch.setattr(config, "PROFILES_DIR", res2 / "Elegoo")
        monkeypatch.setattr(config, "SLICER_USER_DIR", tmp_path / "нет" / "user" / "default")

        p = [x for x in await db.list_printers(active_only=False) if x.id == pid][0]
        assert Path(p.machine_profile) == res2 / "Elegoo" / "machine" / "ECC2" / "m.json"
        assert Path(p.machine_profile).is_file(), "расчёт получает существующий файл"
        assert Path(p.process_dir).is_dir() and Path(p.filament_dir).is_dir()
        n = (await db.list_printer_nozzles(pid))
        if n:
            assert Path(n[0].machine_profile).is_file()
    finally:
        await db.close()
