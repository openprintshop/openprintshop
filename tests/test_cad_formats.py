"""Инженерные форматы (.step/.stp): конвертация в STL на приёме (B6), без конвертера — отказ.

До 29.07.2026 .step/.stp числились в downloader.MESH_EXTS — файл принимался, а геометрию
никто прочитать не мог. 29.07 ловушку закрыли честным отказом; 21.08 появился конвертер
(app/stepconv.py, FreeCAD headless) — теперь .step мешится в STL на каждом входе, а отказ
остаётся ПОВЕДЕНИЕМ ДЕГРАДАЦИИ, когда freecadcmd недоступен. Тесты держат обе ветки:
фикстура _no_converter выключает конвертер для «отказных» случаев, конвертационные ветки
подменяют stepconv.convert_file фейком; один настоящий смок гоняет FreeCAD по-настоящему.
"""
import zipfile
from pathlib import Path

import pytest

from app import downloader, stepconv


@pytest.fixture()
def _no_converter(monkeypatch):
    """Мир до B6: конвертера нет — все прежние отказы обязаны работать как раньше."""
    monkeypatch.setattr(stepconv, "available", lambda: False)


@pytest.fixture()
def _fake_converter(monkeypatch):
    """Конвертер есть, но FreeCAD в юнит-тестах не гоняем: фейк пишет валидный мини-STL."""
    monkeypatch.setattr(stepconv, "available", lambda: True)

    def _convert(src, out_dir=None):
        src = Path(src)
        dst = Path(out_dir or src.parent) / (src.stem + ".stl")
        dst.write_text("solid converted\nendsolid converted\n")
        return dst
    monkeypatch.setattr(stepconv, "convert_file", _convert)
    # downloader зовёт через свою обёртку — она читает stepconv из модуля, подмены достаточно


class _Doc:
    """Минимальный aiogram-Document: загрузчику нужны только имя и id."""
    def __init__(self, name: str) -> None:
        self.file_name = name
        self.file_unique_id = "u1"


class _Bot:
    def __init__(self) -> None:
        self.downloads = 0

    async def download(self, document, destination):   # noqa: ANN001
        self.downloads += 1
        open(destination, "wb").write(b"ISO-10303-21;")


def test_cad_not_a_mesh_format():
    """Гейт один: CAD и меши не пересекаются, MESH_EXTS — ровно то, что обещают тексты бота."""
    assert downloader.MESH_EXTS == {".stl", ".3mf", ".obj"}
    assert downloader.CAD_EXTS == {".step", ".stp"}
    assert not (downloader.MESH_EXTS & downloader.CAD_EXTS)


@pytest.mark.parametrize("name", ["part.step", "part.stp", "PART.STEP"])
async def test_web_upload_rejects_cad(tmp_path, monkeypatch, name, _no_converter):
    """Мини-апка: отказ с внятным текстом и БЕЗ записи файла на диск."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    with pytest.raises(downloader.DownloadError) as exc:
        await downloader.save_web_upload(name, b"ISO-10303-21;")
    assert ".step" in str(exc.value) and ".stl" in str(exc.value)
    assert not (tmp_path / "dl").exists()      # ни каталога задания, ни файла


async def test_telegram_upload_rejects_cad_before_download(tmp_path, monkeypatch, _no_converter):
    """Бот: у него нет клиентского фильтра мини-апки, поэтому именно тут ловушка и жила.
    Отказ уходит ДО скачивания файла из Telegram."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    bot = _Bot()
    with pytest.raises(downloader.DownloadError) as exc:
        await downloader.handle_upload(bot, _Doc("mount.STP"))
    assert str(exc.value) == downloader.CAD_MSG
    assert bot.downloads == 0


async def test_telegram_upload_still_takes_meshes(tmp_path, monkeypatch):
    """Обратная сторона гейта: обычный .stl из бота по-прежнему принимается."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    meshes, name = await downloader.handle_upload(_Bot(), _Doc("cube.stl"))
    assert name == "cube.stl" and len(meshes) == 1 and meshes[0].exists()


async def test_direct_link_rejects_cad_without_network(tmp_path, monkeypatch, _no_converter):
    """Прямая ссылка на .step: отказ без единого запроса. Регрессия на подмену расширения —
    незнакомому суффиксу загрузчик дописывает «.stl», и без гейта «part.step» уехал бы
    дальше как «part.step.stl», то есть ровно той же нечитаемой моделью."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")

    async def boom(*a, **k):
        raise AssertionError("сеть дёрнули ради формата, который мы не считаем")

    monkeypatch.setattr(downloader, "_stream_to_file", boom)
    with pytest.raises(downloader.DownloadError) as exc:
        await downloader._download_direct("https://cdn.example.com/part.step?v=2")
    assert str(exc.value) == downloader.CAD_MSG
    # ссылка при этом остаётся «файловой» — иначе клиент получил бы «ссылка не поддерживается»
    assert downloader._looks_like_file_url("https://cdn.example.com/part.step") is True


async def test_zip_of_cad_only_says_why(tmp_path, monkeypatch, _no_converter):
    """Архив из одних .step: причина — формат, а не «моделей не найдено»."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    z = tmp_path / "cad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("body.step", "ISO-10303-21;")
        f.writestr("readme.txt", "hi")
    with pytest.raises(downloader.DownloadError) as exc:
        downloader._extract_zip(z, tmp_path / "out")
    assert str(exc.value) == downloader.CAD_MSG


async def test_zip_without_models_keeps_old_message(tmp_path, monkeypatch):
    """А архив вообще без моделей отвечает как раньше — CAD-текст туда не протёк."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    z = tmp_path / "docs.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("readme.txt", "hi")
    with pytest.raises(downloader.DownloadError) as exc:
        downloader._extract_zip(z, tmp_path / "out")
    assert "В архиве не найдено моделей" in str(exc.value)


async def test_zip_with_cad_and_mesh_takes_mesh(tmp_path, monkeypatch, _no_converter):
    """Смешанный архив (частый случай: автор кладёт и STL, и исходник CAD) — берём STL,
    CAD молча игнорируем, отказа нет."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    z = tmp_path / "mix.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("body.step", "ISO-10303-21;")
        f.writestr("body.stl", "solid x")
    meshes = downloader._extract_zip(z, tmp_path / "out")
    assert [p.name for p in meshes] == ["body.stl"]


# ─────────────────────────── B6: конвертер ЕСТЬ ───────────────────────────

async def test_web_upload_converts_step(tmp_path, monkeypatch, _fake_converter):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    meshes, name = await downloader.save_web_upload("part.step", b"ISO-10303-21;")
    assert name == "part.step"
    assert len(meshes) == 1 and meshes[0].suffix == ".stl" and meshes[0].exists()


async def test_telegram_upload_converts_step(tmp_path, monkeypatch, _fake_converter):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    bot = _Bot()
    meshes, name = await downloader.handle_upload(bot, _Doc("mount.STP"))
    assert bot.downloads == 1                       # файл скачали — конвертеру нужно тело
    assert len(meshes) == 1 and meshes[0].suffix == ".stl"


async def test_zip_of_cad_converts_all(tmp_path, monkeypatch, _fake_converter):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    z = tmp_path / "cad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("body.step", "ISO-10303-21;")
        f.writestr("lid.stp", "ISO-10303-21;")
    meshes = downloader._extract_zip(z, tmp_path / "out")
    assert sorted(p.name for p in meshes) == ["body.stl", "lid.stl"]


async def test_zip_mixed_takes_both(tmp_path, monkeypatch, _fake_converter):
    """Смешанный архив: STL берём как есть, STEP конвертируем — клиент получает всё."""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    z = tmp_path / "mix.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("body.step", "ISO-10303-21;")
        f.writestr("base.stl", "solid x")
    meshes = downloader._extract_zip(z, tmp_path / "out")
    assert sorted(p.name for p in meshes) == ["base.stl", "body.stl"]


async def test_convert_failure_speaks_client_language(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    monkeypatch.setattr(stepconv, "available", lambda: True)

    def _boom(src, out_dir=None):
        raise stepconv.ConvertError("сломался")
    monkeypatch.setattr(stepconv, "convert_file", _boom)
    with pytest.raises(downloader.DownloadError) as exc:
        await downloader.save_web_upload("part.step", b"mangled")
    assert "STEP" in str(exc.value) and "stl" in str(exc.value).lower()


@pytest.mark.skipif(not stepconv.available(), reason="freecadcmd на машине нет")
async def test_real_freecad_smoke(tmp_path):
    """ОДИН настоящий прогон FreeCAD (~5-10 c): фикстурный STEP (куб с отверстием,
    сгенерирован самим FreeCAD 21.08) мешится в непустой бинарный/ASCII STL."""
    src = Path(__file__).parent / "data" / "step_box_with_hole.step"
    dst = stepconv.convert_file(src, tmp_path)
    assert dst.suffix == ".stl" and dst.stat().st_size > 500
