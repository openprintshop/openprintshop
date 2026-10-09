"""Гигиена Э0: документ из Telegram (downloader.handle_upload).

* файл больше 20 МБ — честный отказ ДО скачивания (getFile отдаёт ботам не больше 20 МБ);
* имя файла — чужие данные: в каталог задания пишем только безопасный basename;
* ошибки «файл непригоден» — ModelFileError (подкласс DownloadError): очередь досчёта по
  ним сразу отдаёт заказ мастеру, а не мучает ПК повторами.
"""
import pytest

from app import downloader


class _Doc:
    def __init__(self, name, size=1000, uid="uniq"):
        self.file_name = name
        self.file_size = size
        self.file_unique_id = uid


class _Bot:
    def __init__(self):
        self.downloads = []

    async def download(self, file, destination=None, **kw):
        self.downloads.append(destination)
        with open(destination, "wb") as f:
            f.write(b"solid t\nendsolid t\n")


async def test_big_document_refused_before_download(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    bot = _Bot()
    with pytest.raises(downloader.ModelFileError) as exc:
        await downloader.handle_upload(bot, _Doc("huge.stl", size=20 * 1024 * 1024 + 1))
    assert "20 МБ" in str(exc.value) and "ссылк" in str(exc.value)
    assert bot.downloads == [], "скачивать файл, который Telegram всё равно не отдаст, нельзя"
    assert not (tmp_path / "dl").exists()
    assert isinstance(exc.value, downloader.DownloadError), "старые except DownloadError ловят"


async def test_exactly_20mb_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    meshes, name = await downloader.handle_upload(_Bot(), _Doc("ok.stl", size=20 * 1024 * 1024))
    assert name == "ok.stl" and meshes[0].exists()


@pytest.mark.parametrize("raw, expect", [
    ("../../evil.stl", "evil.stl"),
    ("..\\..\\Windows\\evil.3mf", "evil.3mf"),
    ("C:\\Users\\x\\model.obj", "model.obj"),
    ("con.stl", "_con.stl"),
    ("a\x00b\x1f.stl", "ab.stl"),
    (".stl", "model_uniq.stl"),
])
async def test_unsafe_names_stay_inside_job_dir(tmp_path, monkeypatch, raw, expect):
    root = tmp_path / "dl"
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", root)
    bot = _Bot()
    meshes, name = await downloader.handle_upload(bot, _Doc(raw))
    assert name == expect
    target = meshes[0].resolve()
    assert target.is_relative_to(root.resolve()) and target.parent.parent == root.resolve()


async def test_missing_name_gets_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    with pytest.raises(downloader.ModelFileError):        # без расширения — формат неизвестен
        await downloader.handle_upload(_Bot(), _Doc(None, uid="abc"))


def test_safe_upload_name_limits_length_and_keeps_extension():
    n = downloader.safe_upload_name("x" * 300 + ".3mf")
    assert n.endswith(".3mf") and len(n) <= 64


async def test_bad_archive_is_model_file_error(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path / "dl")
    with pytest.raises(downloader.ModelFileError):
        await downloader.handle_upload(_Bot(), _Doc("broken.zip"))   # «zip» — на деле STL-текст
