"""Аутентификация после выноса в app/webauth.py: тождественность фасада и единая метка локальности.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна 4 цикла 2 вынесла сквозной слой аутентификации из app/webapp.py в
app/webauth.py, оставив в webapp ФАСАД — построчный реэкспорт. У такого переноса есть ровно два
способа сломаться молча, и оба закрыты здесь:

  * ЗАБЫТОЕ ИМЯ В ФАСАДЕ или ОСТАВШАЯСЯ В webapp ВТОРАЯ КОПИЯ функции. Ни то, ни другое не
    ломает импорт: код продолжает звать webapp._verify_admin, просто это уже другой объект.
    Проверка `getattr(webapp, n) is getattr(webauth, n)` ловит оба случая разом.

  * ДВА ЭКЗЕМПЛЯРА `_REQ_IS_LOCAL`. Это contextvars.ContextVar: `_local_mw` в него пишет на
    входе каждого запроса, `_verify_user` из него читает, решая, принимать ли токен ПК-админки.
    Заведи вторую переменную — и запись пойдёт в одну, чтение из другой. Ошибки не будет НИ
    ОДНОЙ: ни исключения, ни строчки в логе, ни красного теста где-либо ещё. Настольная админка
    просто перестанет пускать владельца, отдавая 403 на всё подряд, и заметит это владелец,
    а не разработчик.

Плюс здесь же закреплено ужесточение предиката локальности: два прежних предиката слиты в один
по правилу «и то, и другое», и теперь публичный Host отсекается ВЕЗДЕ, а не только на трёх
локальных ручках TURZX/LCD.
"""
import pytest
from aiohttp import web
from conftest import stub_auth

from app import config, webapp, webauth

# Имена, которые app/webapp.py обязан реэкспортировать из app/webauth.py. Список ведётся руками
# именно потому, что он и есть контракт: пропажу строки в фасаде должен ловить тест, а не клиент.
FACADE = ("_AUTH_TTL", "_REQ_IS_LOCAL", "_admin_body", "_admin_forbidden", "_local_mw",
          "_local_request", "_request_is_local", "_verify_admin", "_verify_init_data",
          "_verify_user", "is_local_request")


class _Req:
    """Минимальный носитель трёх полей, которые читает предикат локальности."""

    def __init__(self, remote, host, headers):
        self.remote, self.host, self.headers = remote, host, headers


def _r(remote="127.0.0.1", host="127.0.0.1:8081", headers=None):
    return _Req(remote, host, headers or {})


@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и webauth.<имя>, а не копия и не остаток старого кода."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(webauth, name), (
        f"webapp.{name} и webauth.{name} — РАЗНЫЕ объекты: либо в webapp осталась вторая копия, "
        f"либо фасад собран копированием значения вместо реэкспорта")


def test_req_is_local_is_one_context_var():
    """Метка локальности — ОДИН объект на оба модуля, и видна с обеих сторон.

    Пишем через webauth, читаем через webapp и наоборот. Именно этот тест краснеет, если
    кто-нибудь заведёт вторую ContextVar: предыдущий тест такое поймает только на реэкспорте,
    а живой отказ настольной админки не поймает вообще ничто."""
    token = webauth._REQ_IS_LOCAL.set(True)
    try:
        assert webapp._REQ_IS_LOCAL.get() is True
        webapp._REQ_IS_LOCAL.set(False)
        assert webauth._REQ_IS_LOCAL.get() is False
    finally:
        webauth._REQ_IS_LOCAL.reset(token)


# ─────────────────── сквозной путь ПК-токена: middleware → метка → проверка ───────────────────

def _token_env(monkeypatch):
    """Боевое окружение ПК-режима: токен в .env и один админ. Метку локальности НЕ ставим —
    её обязан выставить сам _local_mw, иначе проверка вырождается в саму себя."""
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "sekret-tok", raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [900], raising=False)
    webauth._REQ_IS_LOCAL.set(False)


async def test_pc_token_opens_admin_for_local_request(tmp_db, monkeypatch, admin_req, body):
    """ПК-токен + локальный запрос → владелец. Цепочка идёт целиком и через границу модулей:
    webauth._local_mw пишет метку → webapp._verify_user (через фасад) её читает → 200."""
    _token_env(monkeypatch)
    resp = await webapp._local_mw(admin_req(init_data="sekret-tok"), webapp._api_admin_orders)
    assert body(resp)["ok"] is True
    # и это именно первый админ, а не «кто-то как-то прошёл»
    assert webapp._verify_user("sekret-tok")["id"] == 900


@pytest.mark.parametrize("kw,why", [
    ({"headers": {"Cf-Connecting-Ip": "1.2.3.4"}}, "cloudflared: peer loopback, но заголовок прокси"),
    ({"headers": {"X-Forwarded-For": "1.2.3.4"}}, "любой обратный прокси"),
    ({"host": "bot.example.ts.net"}, "туннель без заголовка прокси: выдаёт себя публичным Host"),
    ({"remote": "203.0.113.7"}, "запрос прямо из сети"),
])
async def test_pc_token_refused_from_outside(tmp_db, monkeypatch, admin_req, kw, why):
    """Тот же самый токен, но запрос НЕ с этой машины → 403. Токен ПК-админки даёт полные права
    владельца, поэтому каждый способ притвориться локальным проверяется отдельно."""
    _token_env(monkeypatch)
    resp = await webapp._local_mw(admin_req(init_data="sekret-tok", **kw),
                                  webapp._api_admin_orders)
    assert resp.status == 403, why


# ───────────────────────── слияние двух предикатов локальности ─────────────────────────

def test_both_names_are_aliases_of_one_predicate():
    """`_request_is_local` и `_local_request` — псевдонимы одной функции: разъехаться, как это
    было до слияния, они больше не могут."""
    for req in (_r(), _r(host="bot.example.ts.net"), _r(headers={"X-Forwarded-For": "1.2.3.4"}),
                _r(remote="10.0.0.5")):
        assert webapp._request_is_local(req) == webapp._local_request(req)
        assert webapp._request_is_local(req) == webauth.is_local_request(req)


@pytest.mark.parametrize("req,expected,why", [
    (_r(), True, "штатный лаунчер: admin_panel.py и desktop_admin/shell.py ходят на 127.0.0.1:8081"),
    (_r(host="localhost:8081"), True, "тот же адрес по имени"),
    (_r(remote="::1", host="localhost:8081"), True, "localhost разрешился в IPv6-петлю"),
    (_r(remote="192.168.1.5"), False, "сосед по локальной сети — уже не эта машина"),
    (_r(headers={"Cf-Connecting-Ip": "1.2.3.4"}), False, "cloudflared проксирует с 127.0.0.1"),
    (_r(headers={"X-Forwarded-For": "1.2.3.4"}), False, "любой обратный прокси"),
    (_r(host="bot.example.ts.net"), False, "туннель без заголовка прокси: публичный Host"),
    (_r(host=""), False, "Host не разобрался — считаем чужим (отказ в сторону закрытости)"),
    (_r(remote="::1", host="[::1]:8081"), False,
     "УНАСЛЕДОВАНО от _local_request: Host режется по первому двоеточию, у [::1] это «[». "
     "Ни один штатный лаунчер такой адрес не использует — оба ходят на 127.0.0.1"),
])
def test_merged_predicate_requires_all_three_conditions(req, expected, why):
    """Правило «и то, и другое»: петлевой peer И отсутствие заголовков прокси И локальный Host.

    До слияния каждое из двух условий проверялось только на своей половине ручек: `/app/desk/`
    и ПК-токен смотрели peer+заголовки и пускали `curl -H "Host: example.com"` из туннеля, а
    `/api/lcd/data` смотрел peer+Host и не видел заголовков прокси."""
    assert webauth.is_local_request(req) is expected, why


async def test_desk_mirror_closed_for_public_host(tmp_db, monkeypatch, get_req, body):
    """Ужесточение доехало до ручки, а не осталось в предикате: зеркало админских уведомлений
    (его читает оболочка настольной админки) с публичным Host отдаёт 404 — как и с заголовком
    прокси. Раньше `_request_is_local` про Host не знал вовсе."""
    stub_auth(monkeypatch, _verify_admin=900)
    ok = await webapp._api_admin_events(get_req({"initData": "x"}))
    assert body(ok)["ok"] is True
    with pytest.raises(web.HTTPNotFound):
        await webapp._api_admin_events(get_req({"initData": "x"}, host="bot.example.ts.net"))
