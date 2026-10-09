"""Личный вход в настольную админку (app/desklogin.py).

Зачем это существует. Вход в деск был ОДИН НА МАШИНУ: `ADMIN_PANEL_TOKEN` из `.env`
резолвился в `config.ADMIN_IDS[0]`, поэтому кто бы ни сел за компьютер студии — он работал
правами первого владельца. Роли (`app/roles.py`) в настольной админке из-за этого ничего не
значили: наёмный оператор видел выручку, цены и кнопку удаления заказа. Здесь проверяется
дверь, которая это чинит, и ровно те её свойства, потеря которых не заметна на глаз:

  • код обменивается на сессию ТОЛЬКО с этой машины (файлы деска и так наружу не отдаются,
    но ручка входа — единственная без авторизации, и на неё смотреть надо отдельно);
  • код одноразовый и живёт минуты: повторный ввод не открывает ничего;
  • токен в базе НЕ ХРАНИТСЯ — только его sha256, иначе копия файла базы = вход в админку;
  • сессия переживает перезапуск бота (кэш в памяти поднимается из базы);
  • снятие с должности гасит живые сессии — иначе уволенный остаётся внутри;
  • режим `token` (по умолчанию) не ломает прежний вход, режим `personal` его закрывает.
"""
import time

import pytest
from conftest import FakeRequest

from app import config, db, desklogin, roles, webapp, webauth

OWNER_ID = 4001
OPERATOR_ID = 4002


@pytest.fixture
def local(monkeypatch):
    """Запрос «с этой же машины»: ровно то, что ставит webapp._local_mw на каждом входе."""
    token = webauth._REQ_IS_LOCAL.set(True)
    yield
    webauth._REQ_IS_LOCAL.reset(token)


@pytest.fixture
async def team(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [OWNER_ID], raising=False)
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "env-token-123", raising=False)
    await roles.set_role(OPERATOR_ID, config.ROLE_OPERATOR, name="Оператор Оля")
    await desklogin.refresh()
    yield
    desklogin._SESSIONS.clear()


async def _login(code, *, remote="127.0.0.1", host="127.0.0.1:8081", headers=None):
    req = FakeRequest(method="POST", path="/api/desk/login", body={"code": code},
                      remote=remote, host=host, headers=headers)
    return await webapp._api_desk_login(req)


def _body(resp):
    import json
    return json.loads(resp.body.decode())


# ─────────────────────────── обмен кода на сессию ───────────────────────────
@pytest.mark.asyncio
async def test_code_from_bot_opens_a_personal_session(team, local):
    """Сквозной путь: бот выдал код → админка обменяла его на токен → токен = ЭТОТ человек."""
    code = await desklogin.issue_code(OPERATOR_ID)
    out = _body(await _login(code))
    assert out["ok"] and out["token"]
    assert out["me"]["role"] == config.ROLE_OPERATOR
    assert out["me"]["caps"] == sorted(roles.ROLE_CAPS[config.ROLE_OPERATOR])

    user = webauth._verify_user(out["token"])
    assert user and user["id"] == OPERATOR_ID, "сессия обязана резолвиться в СВОЕГО человека"
    assert user["first_name"] == "Оператор Оля"


@pytest.mark.asyncio
async def test_session_does_not_work_from_outside(team, local):
    """Токен сессии — как и прежний токен ПК: только для запросов с этой машины.

    Проверяем именно через сам предикат: снаружи `_REQ_IS_LOCAL` не выставлен, и весь локальный
    путь аутентификации обязан отключиться целиком."""
    code = await desklogin.issue_code(OPERATOR_ID)
    token = _body(await _login(code))["token"]
    reset = webauth._REQ_IS_LOCAL.set(False)
    try:
        assert webauth._verify_user(token) is None
    finally:
        webauth._REQ_IS_LOCAL.reset(reset)


@pytest.mark.asyncio
async def test_login_endpoint_is_invisible_from_outside(team, local):
    """Ручка входа — единственная без авторизации. Снаружи её быть не должно ВООБЩЕ."""
    from aiohttp import web
    code = await desklogin.issue_code(OPERATOR_ID)
    with pytest.raises(web.HTTPNotFound):
        await _login(code, remote="10.0.0.5", host="example.com",
                     headers={"X-Forwarded-For": "10.0.0.5"})


@pytest.mark.asyncio
async def test_code_is_single_use(team, local):
    """Повторный ввод того же кода не открывает ничего — иначе код из переписки живёт вечно."""
    code = await desklogin.issue_code(OPERATOR_ID)
    assert _body(await _login(code))["ok"]
    assert _body(await _login(code))["ok"] is False


@pytest.mark.asyncio
async def test_expired_code_is_refused(team, local, monkeypatch):
    code = await desklogin.issue_code(OPERATOR_ID)
    await db.add_desk_code(code, OPERATOR_ID, int(time.time()) - 999, int(time.time()) - 1)
    assert _body(await _login(code))["ok"] is False


@pytest.mark.asyncio
async def test_wrong_code_answers_the_same_way(team, local):
    """Ответ на неверный код не должен отличаться от ответа на несуществующий: разница
    превратила бы ручку в оракул «такой код существует»."""
    a = _body(await _login("000000"))
    b = _body(await _login("999999"))
    assert a["ok"] is False and b["ok"] is False and a["error"] == b["error"]


# ─────────────────────────── хранение и живучесть ───────────────────────────
@pytest.mark.asyncio
async def test_token_is_never_stored_in_the_database(team, local):
    """В базе лежит только sha256. Копия bot.db не должна быть ключом от админки."""
    code = await desklogin.issue_code(OPERATOR_ID)
    token = _body(await _login(code))["token"]
    rows = await db.list_desk_sessions()
    assert rows and all(r["token"] != token for r in rows)
    assert all(len(r["token"]) == 64 for r in rows)


@pytest.mark.asyncio
async def test_session_survives_a_restart(team, local):
    """Перезапуск бота не должен выкидывать вошедших: кэш поднимается из базы."""
    code = await desklogin.issue_code(OPERATOR_ID)
    token = _body(await _login(code))["token"]
    desklogin._SESSIONS.clear()                     # как будто процесс перезапустили
    assert webauth._verify_user(token) is None
    await desklogin.refresh()
    assert (webauth._verify_user(token) or {}).get("id") == OPERATOR_ID


@pytest.mark.asyncio
async def test_removing_from_the_team_kills_live_sessions(team, local):
    """Снять роль МАЛО: у уволенного могла остаться живая сессия на машине студии."""
    code = await desklogin.issue_code(OPERATOR_ID)
    token = _body(await _login(code))["token"]
    assert webauth._verify_user(token)
    ok, _err = await roles.remove(OPERATOR_ID)
    assert ok
    assert webauth._verify_user(token) is None
    assert await db.list_desk_sessions() == []


@pytest.mark.asyncio
async def test_logout_revokes_only_this_device(team, local):
    code1 = await desklogin.issue_code(OPERATOR_ID)
    t1 = _body(await _login(code1))["token"]
    code2 = await desklogin.issue_code(OPERATOR_ID)
    t2 = _body(await _login(code2))["token"]
    await webapp._api_desk_logout(FakeRequest(method="POST", path="/api/desk/logout",
                                              body={"initData": t1}))
    assert webauth._verify_user(t1) is None
    assert (webauth._verify_user(t2) or {}).get("id") == OPERATOR_ID


# ─────────────────────────── режимы входа ───────────────────────────
@pytest.mark.asyncio
async def test_default_mode_keeps_the_old_env_token_working(team, local):
    """По умолчанию поведение прежнее — иначе обновление кода заперло бы владельца снаружи
    его собственной мастерской."""
    assert desklogin.mode() == "token"
    user = webauth._verify_user("env-token-123")
    assert user and user["id"] == OWNER_ID


@pytest.mark.asyncio
async def test_personal_mode_closes_the_shared_door(team, local):
    """В режиме `personal` общий .env-токен больше не пускает, а личная сессия — пускает."""
    await db.set_text_setting("desk_login_mode", "personal")
    await desklogin.refresh()
    assert webauth._verify_user("env-token-123") is None

    code = await desklogin.issue_code(OWNER_ID)
    token = _body(await _login(code))["token"]
    assert (webauth._verify_user(token) or {}).get("id") == OWNER_ID


@pytest.mark.asyncio
async def test_code_of_a_dismissed_employee_does_not_open_the_door(team, local):
    """Код выдан, человека сняли с должности, код вводят. Сессию не оставляем."""
    code = await desklogin.issue_code(OPERATOR_ID)
    await roles.remove(OPERATOR_ID)
    out = _body(await _login(code))
    assert out["ok"] is False
    assert await db.list_desk_sessions() == []
