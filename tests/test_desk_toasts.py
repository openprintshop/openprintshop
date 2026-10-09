"""Чьим именем оболочка настольной админки спрашивает ленту тостов (desktop_admin/shell.py).

ЗАЧЕМ ЭТОТ ФАЙЛ. 21.08.2026 включили личный вход в деск (app/desklogin.py): машинный токен
из .env перестал пускать в админские ручки — он давал права первого владельца кому угодно,
кто сел за компьютер студии. Оболочка же продолжала опрашивать /api/admin/events этим самым
токеном, получала 403 каждые шесть секунд и молчала. Тосты умерли в тот же день, а заметили
через девять: отказ ничем себя не проявлял, кроме строк в боевом логе (872 отказа за сутки).

Здесь проверяется контракт, потеря которого именно так и выглядит — тихо:
  • оболочка подписывается сессией ТОГО, КТО ВОШЁЛ в деск, а не машинным токеном;
  • никто не вошёл — тостов нет (на экране форма входа, показывать чужие заказы нечего)
    и сервер не долбим одним и тем же отказом каждый круг;
  • новый вход подхватывается без перезапуска оболочки;
  • прежний token-режим (он по умолчанию) работает как работал.
"""
import importlib.util
import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest
from conftest import FakeRequest

from app import config, db, deskevents, desklogin, roles, webapp, webauth

ROOT = Path(__file__).resolve().parent.parent
OWNER_ID = 5001
OPERATOR_ID = 5002


class _Win:
    """Окно деска: отдаёт то, что лежит в его localStorage (пусто — никто не вошёл)."""

    def __init__(self, token=""):
        self.token = token

    def evaluate_js(self, js):
        assert "pcb_admin_token" in js, "оболочка обязана спрашивать именно ключ входа деска"
        return self.token


def _load_shell(monkeypatch):
    """Импорт desktop_admin/shell.py.

    Заглушка pywebview обязательна: в venv бота его нет (у оболочки свой), а на неудачном
    импорте модуль уходит в браузерный режим через sys.exit — то есть сбор тестов открыл бы
    владельцу окно админки. Модуль грузим заново на каждый тест: у него есть состояние
    (_auth_refused), и оно не должно течь между проверками.
    """
    stub = types.ModuleType("webview")
    stub.windows = []
    monkeypatch.setitem(sys.modules, "webview", stub)
    spec = importlib.util.spec_from_file_location(
        "desk_shell_under_test", ROOT / "desktop_admin" / "shell.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "desk_shell_under_test", mod)
    spec.loader.exec_module(mod)
    mod._TOKEN = "env-token-123"          # то, что оболочка читает из .env при запуске
    return mod


@pytest.fixture
def local():
    """Запрос «с этой же машины»: ровно то, что ставит webapp._local_mw на каждом входе."""
    token = webauth._REQ_IS_LOCAL.set(True)
    yield
    webauth._REQ_IS_LOCAL.reset(token)


@pytest.fixture
async def team(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [OWNER_ID], raising=False)
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "env-token-123", raising=False)
    # Режим входа живёт в кэше настроек (его читает синхронный _verify_user), а desklogin.refresh
    # ниже перепишет кэш на personal. Через monkeypatch — чтобы прежнее значение вернулось на
    # разборке: иначе «личный вход» протёк бы в чужие файлы и уронил там ПК-токен.
    monkeypatch.setattr(config, "DESK_LOGIN_MODE", "token", raising=False)
    await roles.set_role(OPERATOR_ID, config.ROLE_OPERATOR, name="Оператор Оля")
    await db.set_text_setting("desk_login_mode", "personal")
    await desklogin.refresh()
    deskevents.reset()
    yield
    desklogin._SESSIONS.clear()
    deskevents.reset()


async def _session_for(uid: int) -> str:
    """Личная сессия сотрудника — как после ввода кода из бота на экране входа."""
    code = await desklogin.issue_code(uid)
    got = await desklogin.redeem_code(code, device="тест")
    return got[0]


def _http_403(url):
    return urllib.error.HTTPError(url, 403, "Forbidden", {},
                                  io.BytesIO(b'{"ok": false, "error": "\\u0414"}'))


async def test_shell_asks_events_with_the_session_of_whoever_is_logged_in(team, local, monkeypatch):
    """Сквозной смысл починки: токеном оболочки лента ОТКРЫВАЕТСЯ, машинным — нет.

    Обе половины в одном тесте намеренно: врозь каждая выглядит здоровой (сервер честно
    отказывает машинному токену, оболочка честно шлёт запрос) — умирают тосты именно на стыке.
    """
    shell = _load_shell(monkeypatch)
    deskevents.push("Новый заказ #7")

    # как было до починки: оболочка подписывалась машинным токеном
    assert webauth._verify_admin("env-token-123") is None

    token = await _session_for(OWNER_ID)
    shell.webview.windows = [_Win(token)]
    assert shell._auth_token() == token

    resp = await webapp._api_admin_events(FakeRequest(
        path="/api/admin/events", query={"initData": shell._auth_token(), "since": "0"}))
    assert resp.status == 200
    assert [e["text"] for e in json.loads(resp.body.decode())["events"]] == ["Новый заказ #7"]


async def test_nobody_logged_in_means_no_toasts_and_no_hammering(monkeypatch):
    """Разлогинены — тостов нет, и сервер не получает один и тот же отказ каждые 6 секунд.

    Второе не косметика: именно повтор отказа кругами и был единственным следом поломки —
    боевой лог за сутки, в котором 872 строки 403 и ни одной удачной."""
    shell = _load_shell(monkeypatch)
    shell.webview.windows = [_Win("")]            # окно на экране входа
    asked = []

    def _urlopen(url, timeout=0):
        asked.append(url)
        raise _http_403(url)

    monkeypatch.setattr(shell.urllib.request, "urlopen", _urlopen)

    assert shell._events_round(None) == (None, [])
    assert len(asked) == 1                        # первый круг: пробуем запасной .env-токен
    assert shell._events_round(None) == (None, [])
    assert len(asked) == 1                        # второй: отвергнутым токеном не стучимся
    assert shell._auth_token() == ""


async def test_login_is_picked_up_without_restarting_the_shell(monkeypatch):
    """Сотрудник вошёл — оболочка подхватывает его сессию сама: перезапускать окно на весь
    экран ради тостов владелец не должен."""
    shell = _load_shell(monkeypatch)
    shell.webview.windows = [_Win("")]
    monkeypatch.setattr(shell.urllib.request, "urlopen",
                        lambda url, timeout=0: (_ for _ in ()).throw(_http_403(url)))
    shell._events_round(None)                     # машинный токен отвергнут и забыт
    assert shell._auth_token() == ""

    shell.webview.windows = [_Win("сессия-оли")]
    assert shell._auth_token() == "сессия-оли"


async def test_old_token_mode_works_as_before(monkeypatch):
    """Режим `token` (по умолчанию) не тронут: в окне лежит .env-токен, им и спрашиваем."""
    shell = _load_shell(monkeypatch)
    shell.webview.windows = [_Win("env-token-123")]
    assert shell._auth_token() == "env-token-123"


async def test_lost_access_resets_the_cursor(monkeypatch):
    """Потеряли доступ — курсор обнуляем: иначе после входа мастеру вывалится пачкой всё,
    что накопилось, пока он был разлогинен. Обрыв связи (бот перезапускается) — другое дело:
    там курсор ценен и остаётся."""
    shell = _load_shell(monkeypatch)
    shell.webview.windows = [_Win("сессия")]
    monkeypatch.setattr(shell.urllib.request, "urlopen",
                        lambda url, timeout=0: (_ for _ in ()).throw(_http_403(url)))
    assert shell._events_round(120) == (None, [])

    shell2 = _load_shell(monkeypatch)
    shell2.webview.windows = [_Win("сессия")]
    monkeypatch.setattr(shell2.urllib.request, "urlopen",
                        lambda url, timeout=0: (_ for _ in ()).throw(OSError("бот перезапускается")))
    assert shell2._events_round(120) == (120, [])


async def test_first_round_only_remembers_the_top(monkeypatch):
    """Курсора нет — запоминаем вершину и молчим: перезапуск оболочки не должен вываливать
    на мастера пачку старых уведомлений."""
    shell = _load_shell(monkeypatch)
    shell.webview.windows = [_Win("сессия")]
    answers = [{"ok": True, "cursor": 41, "events": []},
               {"ok": True, "cursor": 42, "events": [{"id": 42, "text": "Новый заказ"}]}]

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(shell.urllib.request, "urlopen",
                        lambda url, timeout=0: _Resp(answers.pop(0)))
    cursor, events = shell._events_round(None)
    assert (cursor, events) == (41, [])
    cursor, events = shell._events_round(cursor)
    assert cursor == 42 and [e["text"] for e in events] == ["Новый заказ"]
