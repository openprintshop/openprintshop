"""Гигиена кэша: суточная чистка обязана видеть ВСЕ производные файлы каталога geo.

Разовая ревизия 27.08.2026 нашла в боевом data/cache/geo 98 файлов .glb на 520 МиБ — все
устаревших версий ключа (v1…v5 при текущей v6). Читать их было уже некому, а чистка их не
видела: в фильтре расширений стояли только .stl и .part. STL той же природы вычищался
исправно, поэтому перекос заметили лишь по размеру каталога.

Обратная сторона той же чистки — не снести лишнего: папки загрузок защищены ссылками
коллекции, и непрочитанные ссылки обязаны отменять чистку, а не разрешать её (04.10.2026).
"""
import os
import sqlite3
import time

import pytest

from app import config, maintenance


async def test_geo_cleanup_covers_glb_not_only_stl(tmp_db, tmp_path, monkeypatch):
    """Мёртвый GLB уходит вместе с мёртвым STL, свежий — остаётся.

    Свежесть проверяем отдельно и намеренно: пересборка GLB тяжёлого меша занимает минуты,
    и чистка «всё подряд по расширению» стоила бы мастеру ожидания у 3D-вьюера."""
    data = tmp_path / "studio data"
    geo = data / "cache" / "geo"
    geo.mkdir(parents=True)
    (data / "previews").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CACHE_DIR", data / "cache")
    monkeypatch.setattr(config, "PREVIEW_DIR", data / "previews")
    monkeypatch.setattr(config, "DOWNLOAD_CACHE_DIR", data / "dl_cache")
    monkeypatch.setattr(config, "DOWNLOAD_DIR", data / "downloads")

    stale_glb = geo / "glb_deadbeef_300000_v1.glb"
    stale_stl = geo / "deadbeef_55000_v9.stl"
    fresh_glb = geo / "glb_deadbeef_300000_v6.glb"
    for f in (stale_glb, stale_stl, fresh_glb):
        f.write_bytes(b"x")
    old = time.time() - (maintenance.CLEAN_AGE_DAYS + 1) * 86400
    for f in (stale_glb, stale_stl):
        os.utime(f, (old, old))

    res = await maintenance.cleanup_files()

    assert not stale_glb.exists(), "мёртвый GLB не убран — каталог geo растёт без предела"
    assert not stale_stl.exists()
    assert fresh_glb.exists(), "свежий GLB трогать нельзя: пересборка тяжёлого меша — минуты"
    assert res["geo"] == 2


# ───── ссылки коллекции на модели: «прочитать не вышло» ≠ «ссылок нет» (04.10.2026) ─────
class _Cur:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _Conn:
    """Соединение, у которого запрос к таблице `table` падает с ошибкой `err`."""

    def __init__(self, table: str, err: Exception, rows: list):
        self._table, self._err, self._rows = table, err, rows

    async def execute(self, sql, *args):
        if f"FROM {self._table} " in sql:
            raise self._err
        return _Cur(self._rows)


async def test_model_refs_missing_column_means_no_refs_from_that_table(monkeypatch):
    """Старая БД без колонки paths: эта таблица ссылок хранить не может — остальные читаем."""
    conn = _Conn("collection", sqlite3.OperationalError("no such column: paths"),
                 [('["downloads/dl_x/m.3mf"]',)])
    monkeypatch.setattr(maintenance.db, "_conn", lambda: conn)
    refs = await maintenance._referenced_model_paths()
    assert refs == {str(config.abs_data("downloads/dl_x/m.3mf"))}


async def test_model_refs_other_db_error_is_not_swallowed(monkeypatch):
    """«database is locked» раньше превращался в пустое множество = «защищать нечего»."""
    conn = _Conn("saved_models", sqlite3.OperationalError("database is locked"), [])
    monkeypatch.setattr(maintenance.db, "_conn", lambda: conn)
    with pytest.raises(sqlite3.OperationalError):
        await maintenance._referenced_model_paths()


async def test_download_cleanup_skipped_when_model_refs_unreadable(tmp_db, tmp_path, monkeypatch):
    """Ссылки не прочитались — папки загрузок в этот раз не трогаем вовсе.

    Контрольный прогон в конце — та же папка без поломки уходит: значит, выше её спасла
    именно пропущенная чистка, а не, скажем, свежая дата файла."""
    data = tmp_path / "studio data"
    (data / "previews").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CACHE_DIR", data / "cache")
    monkeypatch.setattr(config, "PREVIEW_DIR", data / "previews")
    monkeypatch.setattr(config, "DOWNLOAD_CACHE_DIR", data / "dl_cache")
    monkeypatch.setattr(config, "DOWNLOAD_DIR", data / "downloads")
    folder = data / "downloads" / "dl_live"
    folder.mkdir(parents=True)
    model = folder / "model.3mf"
    model.write_bytes(b"3mf")
    old = time.time() - 365 * 86400
    os.utime(model, (old, old))

    real = maintenance._referenced_model_paths

    async def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(maintenance, "_referenced_model_paths", locked)
    res = await maintenance.cleanup_files()
    assert folder.exists(), "чистка снесла папку модели, не прочитав ссылок коллекции"
    assert res["downloads"] == 0

    monkeypatch.setattr(maintenance, "_referenced_model_paths", real)
    res = await maintenance.cleanup_files()
    assert not folder.exists() and res["downloads"] == 1
