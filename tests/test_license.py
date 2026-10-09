"""Лицензионные ключи (трек C, п.4): подпись, сроки, статус и гейт самообновления.

Приватных ключей в репозитории нет и быть не должно — пара генерируется ПРЯМО В ТЕСТЕ
и живёт только в памяти процесса. Подписывающая половина берётся из tools/licensing
(инструмент владельца) — так заодно проверяется, что выпуск и проверка сходятся.
"""
from __future__ import annotations

import importlib.util
import secrets
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import config, edition, license, selfcheck, selfupdate, version

_TOOLS = Path(__file__).resolve().parents[1] / "tools" / "licensing"


def _load_tool(name: str):
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


licgen = _load_tool("licgen")
signer = _load_tool("sign")


@pytest.fixture
def issuer(monkeypatch):
    """Свежая пара ключей выпуска: публичный вшит в модуль, приватный — только здесь."""
    seed = secrets.token_bytes(32)
    monkeypatch.setattr(license, "PUBLIC_KEYS", (signer.public_key_hex(seed),))
    license._cache.clear()

    def issue(expires=None, *, studio="Студия «Пример»", ed="studio",
              plan="", key_id="001", issued=None, seed_override=None):
        payload = licgen.build_payload(studio, key_id, ed,
                                       plan or ("subscription" if expires else "perpetual"),
                                       expires, issued)
        return licgen.make_key(payload, seed_override or seed)
    issue.seed = seed  # type: ignore[attr-defined]
    return issue


def _iso(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- проверка ключа

def test_perpetual_key_is_valid_forever(issuer):
    res = license.verify(issuer(None, plan="perpetual"))
    assert res["ok"] and res["state"] == "ok"
    assert res["expires"] == "" and res["days_left"] is None and res["expired"] is False
    assert res["studio"] == "Студия «Пример»" and res["plan"] == "perpetual"


def test_subscription_key_expiring_and_expired(issuer):
    soon = license.verify(issuer(_iso(5)))
    assert soon["state"] == "expiring" and soon["ok"] is True and 4 <= soon["days_left"] <= 7

    far = license.verify(issuer(_iso(200)))
    assert far["state"] == "ok"

    # Момент проверки задаём ЯВНО: границу суток модуль считает в UTC, а _iso() берёт
    # локальную дату — на настенных часах ключ «вчерашний» ещё жив до 03:00 (UTC+3),
    # и тест, опирающийся на time.time(), краснел бы каждую ночь.
    gone = _iso(-1)
    after = license._day_ts(gone, end_of_day=True) or 0.0
    dead = license.verify(issuer(gone), now=after + 1.0)
    assert dead["state"] == "expired" and dead["expired"] and not dead["ok"]
    assert "истекла" in dead["reason"]


def test_expiry_day_counts_until_its_end(issuer):
    """Ключ «до сегодня» обязан быть живым сегодня: иначе лицензия умирает на сутки раньше.

    Момент проверки задаём ЯВНО (полдень того же дня по UTC): даты в ключе локальные, а сроки
    считаются в UTC, и тест на настенных часах падал бы в часы смещения (грабля раунда 1).
    """
    day = _iso(0)
    noon = license._day_ts(day) + 12 * 3600
    assert license.verify(issuer(day), now=noon)["state"] == "expiring"


def test_broken_key_is_invalid(issuer):
    for bad in ("", "   "):
        assert license.verify(bad)["state"] == "missing"
    for bad in ("мусор", "PSB1.не-база64.тоже", "PSB1.aaa", "XXX1.aa.bb"):
        assert license.verify(bad)["state"] == "invalid"


def test_tampered_payload_with_good_signature(issuer):
    """Подменённая нагрузка при валидной (но чужой этой нагрузке) подписи."""
    good = issuer(_iso(30), studio="Студия А")
    other = issuer(_iso(3000), studio="Студия Б")
    prefix, payload, sig = good.split(".")
    forged = ".".join([prefix, other.split(".")[1], sig])       # чужая нагрузка, старая подпись
    assert license.verify(forged)["state"] == "invalid"
    swapped = ".".join([prefix, payload, other.split(".")[2]])  # своя нагрузка, чужая подпись
    assert license.verify(swapped)["state"] == "invalid"


def test_key_signed_by_foreign_private_key(issuer):
    alien = secrets.token_bytes(32)
    assert license.verify(issuer(_iso(30), seed_override=alien))["state"] == "invalid"


def test_edition_mismatch_only_when_edition_declared(issuer, monkeypatch):
    """Сверяем редакцию ТОЛЬКО с явно заданной в .env.

    У покупателя EDITION не задан, а автоопределение (по наличию .git) на git-clone-установке
    даёт «dev» — сверка с ним вешала честному покупателю вечное «проверьте EDITION в .env»
    на полностью действующем ключе, и эта же строка уезжала в стартовый алерт владельцу."""
    monkeypatch.setattr(config, "EDITION", "", raising=False)
    monkeypatch.setattr(edition, "edition", lambda: "dev")     # автоопределение у покупателя
    assert license.verify(issuer(None, ed="studio"))["edition_mismatch"] is False

    monkeypatch.setattr(config, "EDITION", "studio", raising=False)
    assert license.verify(issuer(None, ed="studio"))["edition_mismatch"] is False
    other = license.verify(issuer(None, ed="dev"))
    assert other["ok"] and other["state"] == "ok" and other["edition_mismatch"] is True


def test_clock_rolled_back_rejects_future_issue_date(issuer):
    res = license.verify(issuer(_iso(365), issued=_iso(30)))
    assert res["state"] == "invalid" and "дату" in res["reason"]


def test_build_without_public_key_cannot_verify(issuer, monkeypatch):
    monkeypatch.setattr(license, "PUBLIC_KEYS", ())
    res = license.verify(issuer(None))
    assert res["state"] == "invalid" and "публичный ключ" in res["reason"]


# ---------------------------------------------------------------- загрузка и статус

def test_key_loaded_from_env_then_from_data_dir(issuer, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LICENSE_KEY", "", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    license._cache.clear()
    assert license.load() == ("", "")

    key = issuer(None)
    (tmp_path / license.KEY_FILENAME).write_text(key + "\n", encoding="utf-8")
    assert license.load() == (key, "file")

    monkeypatch.setattr(config, "LICENSE_KEY", "PSB1.x.y", raising=False)
    assert license.load() == ("PSB1.x.y", "env")   # .env сильнее файла


def test_status_shape_and_no_secret_leak(issuer, monkeypatch):
    key = issuer(_iso(60), key_id="042")
    monkeypatch.setattr(config, "LICENSE_KEY", key, raising=False)
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "update", raising=False)
    license._cache.clear()
    st = license.status()
    assert st["state"] == "ok" and st["source"] == "env" and st["key_present"] is True
    assert st["id"] == "042" and st["enforcement"] == "update" and st["build_edition"]
    assert st["days_left"] and st["expires_at"] and st["expires"]
    dumped = repr(st)
    assert key not in dumped and key.split(".")[2] not in dumped


def test_status_survives_broken_internals(monkeypatch):
    """Ошибка проверки не имеет права погасить владельцу весь экран «Статус»."""
    monkeypatch.setattr(license, "load", lambda: (_ for _ in ()).throw(RuntimeError("бум")))
    st = license.status()
    assert st["state"] == "invalid" and "enforcement" in st


def test_enforcement_defaults(monkeypatch):
    """Умолчание политики вяжется к вшитому публичному ключу, а НЕ к edition().

    Регрессия: раньше умолчание считалось по edition(), а та у покупателя опознаёт сборку
    как «dev» (установка = git clone, .git на месте) — гейт не включился бы ни у кого,
    вопреки .env.example, докам и roadmap."""
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "", raising=False)
    monkeypatch.setattr(config, "EDITION", "", raising=False)
    monkeypatch.setattr(license, "PUBLIC_KEYS", ("00" * 32,))
    # ровно чужая установка: EDITION не задан — с 10.10.2026 это студия (и .git тут ни при чём)
    assert edition.edition() == edition.STUDIO
    assert license.enforcement_level() == "update"

    monkeypatch.setattr(license, "PUBLIC_KEYS", ())
    assert license.enforcement_level() == "off"        # сборке нечем проверять

    monkeypatch.setattr(license, "PUBLIC_KEYS", ("00" * 32,))
    monkeypatch.setattr(config, "EDITION", "dev", raising=False)
    assert license.enforcement_level() == "off"        # явная сборка разработчика

    monkeypatch.setattr(config, "EDITION", "studio", raising=False)
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "off", raising=False)
    assert license.enforcement_level() == "off"        # явная настройка сильнее всего


def test_unreadable_dates_are_rejected_symmetrically(issuer):
    """Опечатка выпускающего в дате не должна проходить молча: дату выдачи покупатель
    видит в карточке «Лицензия», и «issued: третьего дня» там неуместно."""
    bad_issued = license.verify(issuer(None, issued="не-дата"))
    assert bad_issued["state"] == "invalid" and "дата выдачи" in bad_issued["reason"]
    bad_exp = license.verify(issuer("31-12-2026"))
    assert bad_exp["state"] == "invalid" and "дата окончания" in bad_exp["reason"]


def test_absurdly_long_key_is_rejected_before_parsing(issuer):
    """Сначала аутентифицируем, потом парсим: гигантский «ключ» не должен доходить
    до base64+json (сегмент не подписан, разбирать в нём нечего)."""
    huge = "PSB1." + "A" * (license.MAX_KEY_CHARS + 10) + ".AAAA"
    res = license.verify(huge)
    assert res["state"] == "invalid" and "длинный" in res["reason"]


def test_small_order_public_key_verifies_nothing():
    """Классическая ловушка бескофакторной проверки Ed25519: против публичного ключа
    малого порядка (32 нуля) проходит любая подпись. Сейчас PUBLIC_KEYS приходит только
    из исходника, но список задуман расширяемым — закрываем заранее."""
    assert license.verify_signature(b"x", bytes(64), bytes(32)) is False


# ---------------------------------------------------------------- гейт обновлений

def _fake_git(table: dict):
    def run(*args, **kw):  # noqa: ARG001
        return table.get(args[0], (0, "", ""))
    return run


def _up_to_date_git(monkeypatch):
    monkeypatch.setattr(version, "info", lambda: {"branch": "master", "sha": "abc",
                                                  "channel": "stable", "is_test": False})
    monkeypatch.setattr(selfupdate, "_git", _fake_git({
        "status": (0, "", ""), "fetch": (0, "", ""), "rev-list": (0, "0 0", ""),
        "log": (0, "", ""), "rev-parse": (0, "1111111", "")}))


def test_selfupdate_refused_without_license(issuer, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_KEY", "", raising=False)
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "update", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", Path(__file__).parent / "нет-такого-каталога")
    license._cache.clear()
    _up_to_date_git(monkeypatch)
    res = selfupdate._apply_sync()
    assert not res["ok"] and res["license"] == "missing"
    assert "LICENSE_KEY" in res["error"]


def test_selfupdate_refused_with_expired_license(issuer, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_KEY", issuer(_iso(-2)), raising=False)
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "update", raising=False)
    license._cache.clear()
    _up_to_date_git(monkeypatch)
    res = selfupdate._apply_sync()
    assert not res["ok"] and res["license"] == "expired"
    # продукт не «выключается»: отказ касается только обновлений и прямо это говорит
    assert "работает как обычно" in res["error"]


def test_selfupdate_allowed_with_valid_license(issuer, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_KEY", issuer(_iso(400)), raising=False)
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "update", raising=False)
    license._cache.clear()
    _up_to_date_git(monkeypatch)
    res = selfupdate._apply_sync()
    # лицензия пропустила — упёрлись уже в обычный предохранитель «обновлений нет»
    assert not res["ok"] and "обновлений нет" in res["error"] and "license" not in res


def test_selfupdate_untouched_when_enforcement_off(monkeypatch):
    monkeypatch.setattr(config, "LICENSE_KEY", "", raising=False)
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "off", raising=False)
    license._cache.clear()
    _up_to_date_git(monkeypatch)
    assert "обновлений нет" in selfupdate._apply_sync()["error"]


# ---------------------------------------------------------------- самодиагностика

def test_selfcheck_silent_on_healthy_and_loud_on_problems(issuer, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "update", raising=False)
    monkeypatch.setattr(edition, "edition", lambda: "studio")
    # Редакцию из .env задаём ЯВНО: license._declared_edition() читает config.EDITION, и на
    # машине, где в .env стоит EDITION=dev (личная сборка владельца после вшивания публичного
    # ключа), ключ с edition=studio давал бы расхождение и лишнюю строку в selfcheck. Тест про
    # тишину при здоровой лицензии не должен зависеть от .env машины.
    monkeypatch.setattr(config, "EDITION", "studio", raising=False)

    monkeypatch.setattr(config, "LICENSE_KEY", issuer(_iso(400), ed="studio"), raising=False)
    license._cache.clear()
    assert selfcheck.license_checks() == []

    monkeypatch.setattr(config, "LICENSE_KEY", issuer(_iso(5), ed="studio"), raising=False)
    license._cache.clear()
    warn = selfcheck.license_checks()
    assert warn and "истекает" in warn[0]

    monkeypatch.setattr(config, "LICENSE_KEY", "", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", Path(__file__).parent / "нет-такого-каталога")
    license._cache.clear()
    assert any("LICENSE_KEY" in s for s in selfcheck.license_checks())

    # на машине разработчика (enforcement off) про отсутствие ключа молчим —
    # иначе бот ноет в стартовом алерте при каждом перезапуске
    monkeypatch.setattr(config, "LICENSE_ENFORCEMENT", "off", raising=False)
    license._cache.clear()
    assert selfcheck.license_checks() == []


def test_status_cache_does_not_reparse_every_call(issuer, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_KEY", issuer(_iso(90)), raising=False)
    license._cache.clear()
    calls = []
    real = license.verify
    monkeypatch.setattr(license, "verify", lambda k, now=None: (calls.append(k), real(k, now))[1])
    license.status()
    license.status()
    assert len(calls) == 1     # «Статус» деска поллится раз в 20 с — разбор кэшируем


def test_time_math_uses_given_now(issuer):
    key = issuer(_iso(1))
    future = time.time() + 10 * 86400
    assert license.verify(key, now=future)["state"] == "expired"
