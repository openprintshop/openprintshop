"""Контракт /api/admin/setting: без поля kind сервер молча отказывает.

Настольная админка полтора дня шла с этой ошибкой: слала {key, value} без kind, сервер отвечал
«Неизвестный параметр», а мастер видел только то, что кнопка «Сохранить» ничего не делает.
Ни один тест этого не ловил — бэкенд был прав, фронт был синтаксически корректен.

Поэтому проверок две: что сервер действительно требует kind (иначе тест ниже бессмыслен) и что
ни один вызов в дереве desk/ его не забыл.
"""
import re
from pathlib import Path

from conftest import stub_auth

from app import db, webapp

DESK_SRC = Path(__file__).resolve().parents[1] / "app" / "webui" / "desk" / "src"


class _Req:
    """Минимальный request: нужен только .json() и локальный адрес."""

    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.remote = "127.0.0.1"

    async def json(self):
        return self._body


async def test_server_requires_kind(monkeypatch):
    """Без kind настройка НЕ сохраняется — ради этого и написан весь файл."""
    # stub_auth, а не setattr(webapp, …): _api_admin_setting живёт в app/web/studio.py
    # (волна 7 endgame) и читает _admin_body из СВОИХ глобалов.
    stub_auth(monkeypatch, _admin_body=lambda body: (1, None))
    saved = []

    async def _save(k, v):
        saved.append((k, v))

    monkeypatch.setattr(db, "set_setting", _save)

    key = db.SETTINGS_ORDER[0]

    resp = await webapp._api_admin_setting(_Req({"key": key, "value": 42}))
    assert b'"ok": false' in resp.body.replace(b" ", b" ")
    assert not saved, "без kind значение записываться не должно"

    resp = await webapp._api_admin_setting(_Req({"key": key, "kind": "num", "value": 42}))
    assert b'"ok": true' in resp.body
    assert saved == [(key, 42)]


def test_every_desk_call_passes_kind():
    """Каждый вызов adminSetting в настольной админке передаёт kind.

    Ищем по исходникам, а не по бандлу: в собранном desk.js имена свойств сохраняются, но
    читать диагностику из минифицированного файла невозможно.
    """
    # call('adminSetting', { ... }) — забираем тело объекта до закрывающей скобки
    pat = re.compile(r"call\(\s*['\"]adminSetting['\"]\s*,\s*\{([^}]*)\}")
    calls = []
    for f in DESK_SRC.rglob("*.jsx"):
        for m in pat.finditer(f.read_text(encoding="utf-8")):
            calls.append((f.name, m.group(1)))

    assert calls, "вызовы adminSetting не найдены — проверь регулярку, а не радуйся зелёному тесту"
    bad = [(name, body.strip()) for name, body in calls if "kind" not in body]
    assert not bad, f"вызов adminSetting без kind — настройка не сохранится: {bad}"
