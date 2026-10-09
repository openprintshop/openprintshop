"""Автообновления (OpenTodo 644367fb): парсинг git-сверки и предохранители apply."""
import pytest

from app import config, db, selfupdate, version


@pytest.fixture(autouse=True)
def _license_off(monkeypatch):
    """Лицензия здесь не предмет проверки (её гейт живёт в tests/test_license.py).
    Сейчас на машине разработчика enforcement и так 'off', но как только в сборку вшьют
    публичный ключ выпуска, без этой заглушки половина файла посыплется на «нет лицензии»."""
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "off", raising=False)


def _fake_git(table: dict):
    """_git-заглушка: ключ — первое слово команды (или пара), значение — (rc, out, err)."""
    def run(*args, **kw):  # noqa: ARG001
        key = args[0]
        return table.get(key, (0, "", ""))
    return run


def test_check_reports_behind_and_commits(monkeypatch):
    monkeypatch.setattr(version, "info", lambda: {"branch": "master", "sha": "abc", "channel": "stable", "is_test": False})
    monkeypatch.setattr(selfupdate, "_git", _fake_git({
        "fetch": (0, "", ""),
        "rev-list": (0, "0\t2".replace("\t", " "), ""),
        "log": (0, "1111111 фикс\n2222222 фича", ""),
        "rev-parse": (0, "2222222", ""),
    }))
    res = selfupdate._check_sync()
    assert res["ok"] and res["behind"] == 2 and res["ahead"] == 0
    assert res["commits"] == ["1111111 фикс", "2222222 фича"]
    assert res["remote_sha"] == "2222222"


def test_check_fetch_failure(monkeypatch):
    monkeypatch.setattr(version, "info", lambda: {"branch": "master", "sha": "abc", "channel": "stable", "is_test": False})
    monkeypatch.setattr(selfupdate, "_git", _fake_git({"fetch": (128, "", "no network")}))
    res = selfupdate._check_sync()
    assert not res["ok"] and "no network" in res["error"]


def test_apply_refuses_dirty_tree(monkeypatch):
    monkeypatch.setattr(version, "info", lambda: {"branch": "master", "sha": "abc", "channel": "stable", "is_test": False})
    monkeypatch.setattr(selfupdate, "_git", _fake_git({"status": (0, " M app/db.py", "")}))
    res = selfupdate._apply_sync()
    assert not res["ok"] and "незакоммиченные" in res["error"]


def test_apply_refuses_local_commits_ahead(monkeypatch):
    """ahead > 0 — машина разработчика: ff-only невозможен, автообновление не рискует."""
    monkeypatch.setattr(version, "info", lambda: {"branch": "test", "sha": "abc", "channel": "test", "is_test": True})
    monkeypatch.setattr(selfupdate, "_git", _fake_git({
        "status": (0, "", ""),
        "fetch": (0, "", ""),
        "rev-list": (0, "15 3", ""),
        "log": (0, "1111111 x", ""),
        "rev-parse": (0, "1111111", ""),
    }))
    res = selfupdate._apply_sync()
    assert not res["ok"] and "впереди origin" in res["error"]


def test_apply_noop_when_up_to_date(monkeypatch):
    monkeypatch.setattr(version, "info", lambda: {"branch": "master", "sha": "abc", "channel": "stable", "is_test": False})
    monkeypatch.setattr(selfupdate, "_git", _fake_git({
        "status": (0, "", ""),
        "fetch": (0, "", ""),
        "rev-list": (0, "0 0", ""),
    }))
    res = selfupdate._apply_sync()
    assert not res["ok"] and "уже на последней" in res["error"]


async def test_mode_default_and_validation(monkeypatch):
    async def fake_settings():
        return {"selfupdate_mode": "чушь"}
    monkeypatch.setattr(db, "get_text_settings", fake_settings)
    assert await selfupdate.mode() == selfupdate.DEFAULT_MODE

    async def fake_settings2():
        return {"selfupdate_mode": "AUTO"}
    monkeypatch.setattr(db, "get_text_settings", fake_settings2)
    assert await selfupdate.mode() == "auto"
