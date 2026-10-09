"""Дисковый кэш геометрии превью действительно ПИШЕТСЯ на диск (гигиена 22.08.2026).

Пробел, который закрывают эти тесты: trimesh выбирает экспортёр ПО РАСШИРЕНИЮ имени файла, а
атомарная запись шла во временное имя `<stem>.<hex>.tmp` — экспортёра для «tmp» нет, вылетал
`ValueError: ('%s exporter not available!', 'tmp')`, и его глотал `except`. Кэш не писался НИ
РАЗУ ни в одной из двух точек, каждое превью дециировало модель заново (секунды на модель на
горячем пути клиента), а единственный существовавший тест проверял лишь состав ключа.

Модели тут синтетические (кубы по 12 граней) — тяжёлых .3mf в тестах не гоняем.
"""
from __future__ import annotations

import trimesh

from app import preview
from app.bedspec import DEFAULT_BED


def _model_file(tmp_path):
    """Любой файл, от содержимого которого считается ключ кэша (разбирать его никто не будет —
    _load_dedup подменён)."""
    f = tmp_path / "model.3mf"
    f.write_bytes(b"not a real 3mf, hashed only")
    return f


def test_load_decimated_writes_cache_and_reuses_it(tmp_path, monkeypatch):
    """Первый вызов ОСТАВЛЯЕТ файл кэша, второй читает его с диска, а не разбирает модель."""
    monkeypatch.setattr(preview.config, "CACHE_DIR", tmp_path / "cache")
    calls = []

    def fake_dedup(paths, bed=DEFAULT_BED):
        calls.append(bed)
        return trimesh.creation.box(extents=(10, 10, 10))

    monkeypatch.setattr(preview, "_load_dedup", fake_dedup)
    paths = [_model_file(tmp_path)]

    m1 = preview._load_decimated(paths, preview._MAX_FACES)
    assert m1 is not None and len(m1.faces) == 12
    cf = preview._geo_cache_path(paths, preview._MAX_FACES)
    assert cf is not None and cf.exists(), "кэш геометрии не записался на диск"

    m2 = preview._load_decimated(paths, preview._MAX_FACES)
    assert m2 is not None and len(m2.faces) == 12
    assert len(calls) == 1, "второй вызов заново разобрал модель — кэш не прочитан"
    # временные файлы записи за собой не оставляем
    assert not list(cf.parent.glob("*.part")) and not list(cf.parent.glob("*.tmp"))


def test_plate_mesh_cache_writes_and_reuses(tmp_path, monkeypatch):
    """Вторая точка записи (кэш меша ОДНОГО стола): та же ловушка, тот же контракт —
    после первого вызова файл есть, второй обходится без группы деталей."""
    monkeypatch.setattr(preview.config, "CACHE_DIR", tmp_path / "cache")
    group = [trimesh.creation.box(extents=(10, 10, 10))]

    m1 = preview._plate_mesh_cached([], "deadbeef", 0, group)
    assert m1 is not None and len(m1.faces) == 12
    cf = tmp_path / "cache" / "geo" / "plate_deadbeef_0_v2.stl"
    assert cf.exists(), "кэш меша стола не записался на диск"

    # group=None: единственный источник меша теперь — диск
    m2 = preview._plate_mesh_cached([], "deadbeef", 0, None)
    assert m2 is not None and len(m2.faces) == 12
    assert not list(cf.parent.glob("*.part")) and not list(cf.parent.glob("*.tmp"))
