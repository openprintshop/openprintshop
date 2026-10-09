"""Карточка приветствия /start рисуется под бренд мастерской (app/menucard.py)."""
from PIL import Image

from app import brand, downloader, menucard


def _settings(monkeypatch, title: str):
    async def get_text_settings():
        return {"brand_title": title}
    monkeypatch.setattr(menucard.db, "get_text_settings", get_text_settings)


def test_render_draws_png(tmp_path):
    out = menucard.render("ФабЛаб Минск", menucard.sources_text(False), tmp_path / "c.png")
    with Image.open(out) as im:
        assert im.format == "PNG" and im.width == menucard.W and im.height > 400


def test_sources_follow_makerworld_module():
    """В подсказке только площадки, ссылки с которых в этой сборке реально скачиваются."""
    assert "MakerWorld" not in menucard.sources_text(False)
    assert "MakerWorld" in menucard.sources_text(True)


async def test_card_cached_per_brand(monkeypatch, tmp_path):
    monkeypatch.setattr(brand, "LOGO_DIR", tmp_path / "brand")
    monkeypatch.setattr(downloader, "_mw", None)
    _settings(monkeypatch, "ФабЛаб Минск")
    first = await menucard.card_path()
    assert first.exists() and first.parent == tmp_path / "brand"
    assert await menucard.card_path() == first          # тот же бренд — та же картинка
    _settings(monkeypatch, "Другая мастерская")
    second = await menucard.card_path()
    assert second != first and second.exists()
    assert not first.exists()                            # прежний бренд убран


async def test_render_failure_falls_back_to_static_card(monkeypatch, tmp_path):
    monkeypatch.setattr(brand, "LOGO_DIR", tmp_path / "brand")
    _settings(monkeypatch, "")

    def boom(*a, **k):
        raise OSError("нет шрифта")
    monkeypatch.setattr(menucard, "render", boom)
    assert await menucard.card_path() == menucard.FALLBACK
    assert menucard.FALLBACK.exists()
