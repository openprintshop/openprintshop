"""Канал выпуска по .git/HEAD (OpenTodo 31485676) — без запуска git."""
from __future__ import annotations

import pytest

from app import version


def _mkgit(tmp_path, head: str, refs: dict | None = None, packed: str | None = None):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head, encoding="utf-8")
    for name, sha in (refs or {}).items():
        (git / "refs" / "heads" / name).write_text(sha + "\n", encoding="utf-8")
    if packed is not None:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return git


@pytest.fixture(autouse=True)
def _reset_memo():
    version._memo = None
    yield
    version._memo = None


def test_master_is_stable(tmp_path, monkeypatch):
    git = _mkgit(tmp_path, "ref: refs/heads/master\n", {"master": "a" * 40})
    monkeypatch.setattr(version, "_GIT_DIR", git)
    i = version.info()
    assert i["branch"] == "master"
    assert i["channel"] == "stable"
    assert i["is_test"] is False
    assert i["sha"] == "a" * 7


def test_main_is_stable(tmp_path, monkeypatch):
    """Открытая версия публикуется веткой main — установка не должна выглядеть тестовой."""
    git = _mkgit(tmp_path, "ref: refs/heads/main\n", {"main": "e" * 40})
    monkeypatch.setattr(version, "_GIT_DIR", git)
    i = version.info()
    assert i["channel"] == "stable"
    assert i["is_test"] is False


def test_test_branch_is_test(tmp_path, monkeypatch):
    git = _mkgit(tmp_path, "ref: refs/heads/test\n", {"test": "b" * 40})
    monkeypatch.setattr(version, "_GIT_DIR", git)
    i = version.info()
    assert i["channel"] == "test"
    assert i["is_test"] is True
    assert version.label().startswith("TEST · ")


def test_other_branch_shows_branch_name(tmp_path, monkeypatch):
    git = _mkgit(tmp_path, "ref: refs/heads/feature-x\n", {"feature-x": "c" * 40})
    monkeypatch.setattr(version, "_GIT_DIR", git)
    i = version.info()
    assert i["channel"] == "feature-x"
    assert i["is_test"] is True          # всё, что не stable, считаем тестовым


def test_detached_head_raw_sha(tmp_path, monkeypatch):
    git = _mkgit(tmp_path, "d" * 40 + "\n")
    monkeypatch.setattr(version, "_GIT_DIR", git)
    i = version.info()
    assert i["branch"] == ""
    assert i["sha"] == "d" * 7
    assert i["channel"] == "unknown"


def test_packed_refs_fallback(tmp_path, monkeypatch):
    # Loose-файла refs/heads/master нет — sha должен взяться из packed-refs.
    git = _mkgit(tmp_path, "ref: refs/heads/master\n",
                 packed="# pack-refs with: peeled\n" + "e" * 40 + " refs/heads/master\n")
    monkeypatch.setattr(version, "_GIT_DIR", git)
    i = version.info()
    assert i["sha"] == "e" * 7
    assert i["channel"] == "stable"
