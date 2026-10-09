"""Автоматический пробный период (решение владельца 23.08.2026).

Проверяем ровно то, ради чего проба сделана двухместной: отметка о первом запуске лежит и
в БД, и файлом в каталоге данных, побеждает самая ранняя, удаление одного места пробу не
сбрасывает, часы назад её не продлевают, а действующий ключ сильнее пробы.

ВРЕМЯ ВЕЗДЕ ЯВНОЕ. В проекте уже наступали на грабли «тест на границе суток падает от
настенных часов»: `now` пробрасывается и в ensure_trial(), и в status().

DATA_DIR ТОЖЕ ВЕЗДЕ ПОДМЕНЁН. Он в тестах не изолирован автофикстурой, а проба пишет файл
в корень каталога данных — без подмены прогон нагадил бы в боевой каталог владельца.
"""
from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path

import pytest

from app import config, license

_TOOLS = Path(__file__).resolve().parents[1] / "tools" / "licensing"


def _load_tool(name: str):
    """Инструменты выпуска владельца (tools/licensing) — ими же и подписываем в тесте:
    приватных ключей в репозитории нет, пара рождается прямо здесь."""
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


licgen = _load_tool("licgen")
signer = _load_tool("sign")

pytestmark = pytest.mark.asyncio

DAY = 86400.0
T0 = 1_756_000_000.0        # ~24.08.2026, фиксированная точка отсчёта


@pytest.fixture
def trial_env(tmp_path, monkeypatch):
    """Изолированный каталог данных + включённый гейт лицензии, ключа нет."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "LICENSE_KEY", "", raising=False)
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "update", raising=False)
    monkeypatch.setattr(config, "TRIAL_DAYS", 0, raising=False)   # умолчание 90
    license._cache.clear()
    yield data
    license._cache.clear()


def _file(data):
    return json.loads((data / license.TRIAL_FILENAME).read_text(encoding="utf-8"))


async def _db_marks(db):
    s = await db.get_settings()
    return s.get(license.TRIAL_STARTED_KEY), s.get(license.TRIAL_SEEN_KEY)


async def test_first_run_writes_both_marks(trial_env, tmp_db):
    assert not (trial_env / license.TRIAL_FILENAME).exists()
    info = await license.ensure_trial(now=T0)
    assert info and info["started_at"] == T0
    assert _file(trial_env)["started_at"] == T0
    assert await _db_marks(tmp_db) == (T0, T0)
    st = license.status(now=T0)
    assert st["state"] == "trial"
    # Запасная ветка карточки в деске печатает именно reason — он обязан быть фразой.
    assert st["reason"].startswith("Пробный период: осталось 90 дн., до ")
    assert st["days_left"] == 90.0


async def test_second_run_does_not_move_the_start(trial_env, tmp_db):
    await license.ensure_trial(now=T0)
    await license.ensure_trial(now=T0 + 10 * DAY)
    assert _file(trial_env)["started_at"] == T0
    started, seen = await _db_marks(tmp_db)
    assert started == T0 and seen == T0 + 10 * DAY
    assert license.status(now=T0 + 10 * DAY)["days_left"] == 80.0


async def test_trial_is_over_on_the_ninety_first_day(trial_env, tmp_db):
    await license.ensure_trial(now=T0)
    # последняя секунда 90-го дня — проба ещё жива
    assert license.status(now=T0 + 90 * DAY - 1)["state"] == "trial"
    st = license.status(now=T0 + 90 * DAY + 1)
    assert st["state"] == "trial_over" and st["ok"] is False
    assert st["reason"].startswith("Пробный период закончился ")
    ok, why = license.update_allowed(now=T0 + 91 * DAY)
    assert not ok and "LICENSE_KEY" in why
    ok, _ = license.update_allowed(now=T0 + 89 * DAY)
    assert ok


async def test_clock_rollback_does_not_extend_the_trial(trial_env, tmp_db):
    await license.ensure_trial(now=T0)
    await license.ensure_trial(now=T0 + 89 * DAY)      # водяная отметка уехала вперёд
    # часы перевели на год назад — считаем по отметке, а не по часам
    assert license.status(now=T0 - 365 * DAY)["days_left"] == 1.0
    await license.ensure_trial(now=T0 - 365 * DAY)     # и перевод не сбрасывает отметку
    assert _file(trial_env)["seen_at"] == T0 + 89 * DAY
    assert license.status(now=T0 - 365 * DAY)["state"] == "trial"
    assert license.status(now=T0 + 90 * DAY)["state"] == "trial_over"


async def test_deleting_the_file_does_not_reset_the_trial(trial_env, tmp_db):
    await license.ensure_trial(now=T0)
    (trial_env / license.TRIAL_FILENAME).unlink()
    # дата пришла из БД — и файл восстановился на следующем запуске той же датой
    assert license.status(now=T0 + 80 * DAY)["days_left"] == 10.0
    await license.ensure_trial(now=T0 + 80 * DAY)
    assert _file(trial_env)["started_at"] == T0


async def test_clearing_the_db_row_does_not_reset_the_trial(trial_env, tmp_db):
    await license.ensure_trial(now=T0)
    await tmp_db.delete_setting(license.TRIAL_STARTED_KEY)
    await tmp_db.delete_setting(license.TRIAL_SEEN_KEY)
    assert license.status(now=T0 + 80 * DAY)["days_left"] == 10.0
    await license.ensure_trial(now=T0 + 80 * DAY)
    assert (await _db_marks(tmp_db))[0] == T0


async def test_earliest_mark_wins(trial_env, tmp_db):
    """Откат БД из data/backups (файл .trial туда не попадает) не двигает начало пробы."""
    await license.ensure_trial(now=T0)
    await tmp_db.set_setting(license.TRIAL_STARTED_KEY, T0 + 60 * DAY)   # «свежая» БД
    assert license.trial_info(now=T0 + 60 * DAY)["started_at"] == T0
    await license.ensure_trial(now=T0 + 60 * DAY)
    assert (await _db_marks(tmp_db))[0] == T0


async def test_valid_key_beats_the_trial(trial_env, tmp_db, monkeypatch):
    """Действующий ключ сильнее пробы — даже если проба давно кончилась."""
    seed = secrets.token_bytes(32)
    monkeypatch.setattr(license, "PUBLIC_KEYS", (signer.public_key_hex(seed),))
    payload = licgen.build_payload("Студия «Пример»", "001", "studio", "perpetual", None,
                                   "2026-08-01")
    monkeypatch.setattr(config, "LICENSE_KEY", licgen.make_key(payload, seed), raising=False)
    await license.ensure_trial(now=T0)
    license._cache.clear()
    st = license.status(now=T0 + 500 * DAY)      # проба кончилась бы на 90-й день
    assert st["state"] == "ok" and st["ok"] is True
    ok, _ = license.update_allowed(now=T0 + 500 * DAY)
    assert ok


async def test_expired_trial_yields_to_a_key(trial_env, tmp_db, monkeypatch):
    """Ключ (любой распознанный) сильнее пробы: проба накладывается ТОЛЬКО на 'missing'."""
    await license.ensure_trial(now=T0 + 200 * DAY)     # проба заведомо кончилась к T0+400
    monkeypatch.setattr(config, "LICENSE_KEY", "мусор-не-ключ", raising=False)
    license._cache.clear()
    st = license.status(now=T0 + 400 * DAY)
    assert st["state"] == "invalid"                    # не 'trial_over'


async def test_enforcement_off_leaves_no_marks(trial_env, tmp_db, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "off", raising=False)
    assert await license.ensure_trial(now=T0) is None
    assert not (trial_env / license.TRIAL_FILENAME).exists()
    assert await _db_marks(tmp_db) == (None, None)
    assert license.status(now=T0)["state"] == "missing"


async def test_status_creates_nothing(trial_env, tmp_db):
    """Чтение состояния не имеет побочных эффектов: пробу начинает только ensure_trial()."""
    assert license.status(now=T0)["state"] == "missing"
    assert license.trial_info(now=T0) is None
    assert not (trial_env / license.TRIAL_FILENAME).exists()
    assert await _db_marks(tmp_db) == (None, None)


async def test_trial_days_from_env(trial_env, tmp_db, monkeypatch):
    monkeypatch.setattr(config, "TRIAL_DAYS", 7, raising=False)
    await license.ensure_trial(now=T0)
    assert license.status(now=T0)["days_left"] == 7.0
    assert license.status(now=T0 + 8 * DAY)["state"] == "trial_over"
    monkeypatch.setattr(config, "TRIAL_DAYS", "мусор", raising=False)
    assert license.trial_days() == license.TRIAL_DAYS


async def test_broken_file_does_not_kill_status(trial_env, tmp_db):
    await license.ensure_trial(now=T0)
    (trial_env / license.TRIAL_FILENAME).write_text("{не json", encoding="utf-8")
    assert license.status(now=T0 + DAY)["state"] == "trial"     # выручила отметка в БД


async def test_selfcheck_quiet_during_trial_and_loud_at_the_end(trial_env, tmp_db,
                                                                monkeypatch):
    from app import selfcheck
    await license.ensure_trial(now=T0)
    monkeypatch.setattr(time, "time", lambda: T0 + 10 * DAY)
    assert selfcheck.license_checks() == []
    monkeypatch.setattr(time, "time", lambda: T0 + 80 * DAY)
    left = selfcheck.license_checks()
    assert len(left) == 1 and "Пробный период" in left[0]
    monkeypatch.setattr(time, "time", lambda: T0 + 100 * DAY)
    over = selfcheck.license_checks()
    assert len(over) == 1 and "LICENSE_KEY" in over[0]


async def test_typo_in_key_explains_how_to_get_the_trial_back(trial_env, tmp_db, monkeypatch):
    """Опечатка в ключе гасит живую пробу — и это верно: сломанный ключ надо ЗАМЕТИТЬ, а не
    прикрыть пробой. Но текст обязан подсказать выход — убрать строку и вернуться к пробе."""
    await license.ensure_trial(now=T0)
    monkeypatch.setattr(config, "LICENSE_KEY", "PSB1.мусор.мусор", raising=False)
    license._cache.clear()

    st = license.status(now=T0 + 10 * DAY)
    assert st["state"] == "invalid" and not st["ok"]          # ключ по-прежнему не принят
    assert "LICENSE_KEY" in st["reason"] and "проб" in st["reason"].lower()
    assert not license.update_allowed(now=T0 + 10 * DAY)[0]   # и обновления по-прежнему закрыты
