"""ПК-режим админки (OpenTodo e075a6f3): секретный токен вместо Telegram initData —
мини-апп в обычном браузере работает как аккаунт первого админа. Токен действует только
для запросов с локальной машины (_REQ_IS_LOCAL): бот опубликован cloudflared-тоннелем,
который проксирует на localhost, поэтому внешние запросы отличаем по заголовкам прокси.

Запросы собираются общими фабриками conftest (get_req/admin_req), а не своим фейком: у них
есть и remote, и headers, поэтому цепочку «запрос → _local_mw → _REQ_IS_LOCAL → _verify_user»
можно проверить целиком, а не только её последнее звено с меткой, выставленной руками.
"""
from app import config, webapp


def _token_env(monkeypatch, *, local=True, admin_ids=(999,)):
    """Окружение ПК-режима: токен в .env, список админов и метка локальности запроса.

    Метку здесь ставим руками — тесты ниже проверяют САМУ проверку токена, для них важно
    только значение _REQ_IS_LOCAL. Кто и как его выставляет в бою, проверяет сквозной тест
    в конце файла (там метку ставит настоящий _local_mw)."""
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "sekret-tok", raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", list(admin_ids), raising=False)
    webapp._REQ_IS_LOCAL.set(local)


def test_token_grants_admin(monkeypatch):
    _token_env(monkeypatch)
    u = webapp._verify_user("sekret-tok")
    assert u and u["id"] == 999
    assert webapp._verify_admin("sekret-tok") == 999
    uid, err = webapp._admin_body({"initData": "sekret-tok"})
    assert uid == 999 and err is None


def test_wrong_or_missing_token_denied(monkeypatch):
    _token_env(monkeypatch)
    assert webapp._verify_user("wrong") is None            # не токен и не валидный initData
    assert webapp._verify_user("") is None
    assert webapp._verify_admin("wrong") is None


def test_token_mode_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "", raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [999], raising=False)
    webapp._REQ_IS_LOCAL.set(True)
    assert webapp._verify_user("sekret-tok") is None       # выключено → только Telegram-подпись


def test_token_needs_admin_ids(monkeypatch):
    _token_env(monkeypatch, admin_ids=())
    assert webapp._verify_user("sekret-tok") is None       # некому выдавать доступ


def test_token_denied_for_remote_request(monkeypatch):
    """Правильный токен, но запрос НЕ с локальной машины (тоннель/прокси) → отказ."""
    _token_env(monkeypatch, local=False)
    assert webapp._verify_user("sekret-tok") is None
    assert webapp._verify_admin("sekret-tok") is None


def test_request_is_local_predicate(get_req):
    """Локальный = loopback-remote И без заголовков прокси (cloudflared их добавляет всегда)."""
    assert webapp._request_is_local(get_req(remote="127.0.0.1")) is True
    assert webapp._request_is_local(get_req(remote="::1")) is True
    assert webapp._request_is_local(get_req(remote="192.168.1.5")) is False
    # внешний запрос через cloudflared: remote = loopback, но есть заголовки прокси
    assert webapp._request_is_local(get_req(headers={"Cf-Connecting-Ip": "1.2.3.4"})) is False
    assert webapp._request_is_local(get_req(headers={"X-Forwarded-For": "1.2.3.4"})) is False


async def test_admin_endpoint_works_with_token(tmp_db, monkeypatch, admin_req, body):
    """Сквозная проверка: реальный админ-эндпоинт пускает по токену без monkeypatch верификации.

    Метку локальности ставит не тест, а сам _local_mw — первый middleware в make_app. Поэтому
    под проверкой вся цепочка: remote/headers запроса → _request_is_local → _REQ_IS_LOCAL →
    _verify_user. Начинаем с local=False, чтобы зелёный результат нельзя было получить меткой,
    оставшейся от теста."""
    _token_env(monkeypatch, local=False)
    resp = await webapp._local_mw(admin_req(init_data="sekret-tok"), webapp._api_admin_orders)
    assert body(resp)["ok"] is True

    # тот же токен, но запрос пришёл снаружи через тоннель (заголовок прокси) → 403
    denied = await webapp._local_mw(
        admin_req(init_data="sekret-tok", headers={"Cf-Connecting-Ip": "1.2.3.4"}),
        webapp._api_admin_orders)
    assert denied.status == 403
