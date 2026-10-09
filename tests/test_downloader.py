"""Загрузчик: кэш загрузок по ссылке (срок хранения) и ссылка MakerWorld без личного модуля.

Тесты самого MakerWorld (варианты, оценка, кэш по варианту, пул аккаунтов) — в
test_makerworld.py и test_mw_pool.py: они идут вместе с модулем app/makerworld.py.
"""
import json
import time

import pytest

from app import downloader


def _fake_downloader(tmp_path, calls):
    """Подделка скачивания: создаёт реальный файл и считает число обращений к сети."""
    async def fake(url):
        calls["n"] += 1
        src = tmp_path / f"src{calls['n']}"
        src.mkdir()
        f = src / "model.stl"
        f.write_text("solid x")
        return [f], "Model"
    return fake


async def test_url_cache_hit_skips_redownload(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_CACHE_AGE_DAYS", 30)
    calls = {"n": 0}
    monkeypatch.setattr(downloader, "_fetch_from_url_uncached", _fake_downloader(tmp_path, calls))

    p1, l1 = await downloader.fetch_from_url("https://example.com/m.stl")
    p2, l2 = await downloader.fetch_from_url("https://example.com/m.stl")

    assert calls["n"] == 1                       # второй раз по тому же URL не качаем
    assert l1 == l2 == "Model"
    assert all(p.exists() for p in p2)
    cache_root = str(tmp_path / "cache")
    assert all(cache_root in str(p) for p in p2)  # файлы отдаются из стабильного кэша


async def test_url_cache_expiry_redownloads(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_CACHE_AGE_DAYS", 30)
    calls = {"n": 0}
    monkeypatch.setattr(downloader, "_fetch_from_url_uncached", _fake_downloader(tmp_path, calls))

    await downloader.fetch_from_url("https://example.com/m.stl")
    # состарим запись кэша за пределы срока хранения
    key = downloader._url_key("https://example.com/m.stl")
    meta = tmp_path / "cache" / key / "meta.json"
    m = json.loads(meta.read_text(encoding="utf-8"))
    m["created_at"] = time.time() - 31 * 86400
    meta.write_text(json.dumps(m), encoding="utf-8")

    await downloader.fetch_from_url("https://example.com/m.stl")
    assert calls["n"] == 2                       # просрочено → скачали заново


MW_LINK = "https://makerworld.com/en/models/40146-benchy"


async def test_makerworld_link_without_module_asks_for_file(monkeypatch):
    """Открытая версия: модуля app/makerworld.py нет — ссылка честно просит прислать файл, а
    пикер вариантов и мгновенная оценка молчат (заказ идёт обычным путём со слайсом)."""
    monkeypatch.setattr(downloader, "_mw", None)
    assert downloader.makerworld_available() is False
    assert await downloader.fetch_makerworld_variants(MW_LINK) is None
    assert await downloader.fetch_makerworld_estimate(MW_LINK) is None
    with pytest.raises(downloader.ModelFileError, match="MakerWorld"):
        await downloader._fetch_from_url_uncached(MW_LINK)
    with pytest.raises(downloader.ModelFileError, match="MakerWorld"):
        await downloader.download_makerworld_instance(MW_LINK, 1)
    assert downloader.label_from_url(MW_LINK) == "Benchy"     # имя из ссылки всё равно достаём
