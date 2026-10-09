"""Телеметрия ошибок (OpenTodo f4de8627).

Ошибки бота раньше жили только в data/logs/bot.log — файл ротируется за часы, и разобрать
жалобу «вчера не работало» было не по чему. Теперь всё стекается в таблицу error_log через
буфер app/errorlog.py, а владелец видит сбои экраном в админке и получает алерт в Telegram.

Проверяем ровно те инварианты, ради которых фича делалась:

- отпечаток (fingerprint) стабилен для одинаковых сбоев и различается для разных — иначе
  один баг размножится на сотни строк, а разные схлопнутся в одну;
- повтор наращивает count у ОТКРЫТОЙ записи, а после «прочитано» тот же сбой заводит НОВУЮ
  строку: рецидив после разбора не должен выглядеть просто подросшим счётчиком;
- TelemetryHandler ловит ERROR+ из logging, но не свои записи и не помеченные notelemetry
  (иначе _error_mw записал бы одну веб-ошибку дважды);
- владельцу шлётся алерт на КАЖДЫЙ НОВЫЙ сбой и на вехи повторов (50/500) — но не на каждое
  повторение, иначе шторм ошибок превращается в шторм сообщений;
- ровно ОДИН алерт на сбой: где вызывающий шлёт своё сообщение сам (_error_mw, backup_once),
  телеметрия молчит (capture(alert=False));
- текст алерта экранируется: бот шлёт с parse_mode=HTML, а в алерт едет str(исключения) и
  клиентский текст из /api/jserr;
- ручки админки отдают список и отмечают прочитанным, /api/jserr режет длину и троттлится.
"""
import datetime
import json
import logging
import time
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, ErrorEvent, Message, Update, User
from conftest import stub_auth

import run
from app import config, deskevents, errorlog, notify, ratelimit, roles, webapp
from app.store import core as store_core


class _Res:
    def __init__(self, canonical):
        self.canonical = canonical


class _MatchInfo:
    """Заглушка request.match_info: у aiohttp отсюда берётся канонический путь маршрута
    (/img/{cid}), и именно он — контекст отпечатка ошибки."""

    def __init__(self, canonical):
        self.route = type("Route", (), {"resource": _Res(canonical) if canonical else None})()


class _Req:
    """Минимальный aiohttp-подобный запрос: query для GET, json() для POST."""

    def __init__(self, query=None, body=None, path="/api/test", canonical=None):
        self.query = query or {}
        self._body = body if body is not None else {}
        self.path = path
        self.match_info = _MatchInfo(path if canonical is None else canonical)

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _clean_buffer():
    """Буфер телеметрии — модульная глобалка: без очистки записи текут между тестами."""
    errorlog._BUF.clear()
    yield
    errorlog._BUF.clear()


@pytest.fixture
def alerts(monkeypatch):
    """Перехват notify.alert: телеграма в тестах нет, а важно ЧТО и СКОЛЬКО раз шлётся."""
    sent = []

    async def _alert(text, *, key=None, throttle_sec=300):
        sent.append({"text": text, "key": key})

    monkeypatch.setattr(errorlog.notify, "alert", _alert)
    return sent


# ---- отпечаток -----------------------------------------------------------------------

def test_fingerprint_is_stable_and_discriminating():
    fp = errorlog.fingerprint("web", "/api/calc", "KeyError", "'plastic_id'")
    assert fp == errorlog.fingerprint("web", "/api/calc", "KeyError", "'plastic_id'")
    assert len(fp) == 16 and all(c in "0123456789abcdef" for c in fp)

    # разный источник / контекст / тип исключения / текст — разные записи
    assert fp != errorlog.fingerprint("tg", "/api/calc", "KeyError", "'plastic_id'")
    assert fp != errorlog.fingerprint("web", "/api/order", "KeyError", "'plastic_id'")
    assert fp != errorlog.fingerprint("web", "/api/calc", "ValueError", "'plastic_id'")
    assert fp != errorlog.fingerprint("web", "/api/calc", "KeyError", "'quality_id'")


def test_fingerprint_ignores_message_tail():
    """В ключ идут только первые 200 символов: хвост обычно меняющиеся id и пути, из-за
    которых один баг превратился бы в сотни «уникальных» ошибок."""
    head = "Сбой слайсера: " + "x" * 200
    assert errorlog.fingerprint("log", "app.slicer", "RuntimeError", head + " заказ 1") == \
        errorlog.fingerprint("log", "app.slicer", "RuntimeError", head + " заказ 2")


# ---- слой БД -------------------------------------------------------------------------

async def test_upsert_aggregates_then_reopens_after_seen(tmp_db):
    st, cnt = await tmp_db.upsert_error("fp1", "web", "error", "KeyError: 'x'",
                                        "Traceback…", "/api/calc", "2026-08-02 10:00:00")
    assert (st, cnt) == ("new", 1)

    # повтор — та же строка, счётчик растёт, last_at/message обновляются
    st, cnt = await tmp_db.upsert_error("fp1", "web", "error", "KeyError: 'x' (ещё раз)",
                                        "Traceback 2", "/api/calc", "2026-08-02 10:05:00")
    assert (st, cnt) == ("inc", 2)
    st, cnt = await tmp_db.upsert_error("fp1", "web", "error", "KeyError: 'x'",
                                        "", "/api/calc", "2026-08-02 10:07:00")
    assert (st, cnt) == ("inc", 3)

    rows = await tmp_db.list_errors()
    assert len(rows) == 1
    r = rows[0]
    assert r["count"] == 3 and r["seen"] == 0
    assert r["first_at"] == "2026-08-02 10:00:00" and r["last_at"] == "2026-08-02 10:07:00"
    assert r["source"] == "web" and r["context"] == "/api/calc"

    # после «прочитано» тот же отпечаток заводит НОВУЮ строку — это рецидив, а не +1
    assert await tmp_db.mark_errors_seen() == 1
    st, cnt = await tmp_db.upsert_error("fp1", "web", "error", "KeyError: 'x'",
                                        "", "/api/calc", "2026-08-02 12:00:00")
    assert (st, cnt) == ("new", 1)
    rows = await tmp_db.list_errors()
    assert len(rows) == 2
    assert rows[0]["count"] == 1 and rows[0]["seen"] == 0   # свежая сверху
    assert rows[1]["count"] == 3 and rows[1]["seen"] == 1


async def test_count_and_mark_seen_one_or_all(tmp_db):
    assert await tmp_db.count_new_errors() == 0
    for i in range(3):
        await tmp_db.upsert_error(f"fp{i}", "log", "error", f"сбой {i}", "", "app.x")
    assert await tmp_db.count_new_errors() == 3

    one = (await tmp_db.list_errors())[0]
    assert await tmp_db.mark_errors_seen(one["id"]) == 1
    assert await tmp_db.count_new_errors() == 2
    assert await tmp_db.mark_errors_seen(one["id"]) == 0     # повтор ничего не меняет

    assert await tmp_db.list_errors(only_new=True) == [
        r for r in await tmp_db.list_errors() if not r["seen"]]
    assert len(await tmp_db.list_errors(only_new=True)) == 2

    assert await tmp_db.mark_errors_seen() == 2
    assert await tmp_db.count_new_errors() == 0


async def test_list_errors_sorted_by_last_repeat(tmp_db):
    """Свежие сверху — по ВРЕМЕНИ ПОСЛЕДНЕГО повтора, а не по id: при схлопывании id остаётся
    от первого появления, поэтому льющийся прямо сейчас сбой уезжал вниз, а при упоре в limit
    вовсе выпадал из выдачи — владелец видел наверху вчерашний разовый мусор."""
    await tmp_db.upsert_error("hot", "log", "error", "льётся", "", "app.x", "2026-07-01 09:00:00")
    for i, ts in enumerate(("2026-08-02 10:00:00", "2026-08-02 11:00:00", "2026-08-02 12:00:00")):
        await tmp_db.upsert_error(f"one{i}", "log", "error", f"разовая {i}", "", "app.x", ts)
    await tmp_db.upsert_error("hot", "log", "error", "льётся", "", "app.x", "2026-08-02 23:59:00")

    rows = await tmp_db.list_errors()
    assert [r["fingerprint"] for r in rows] == ["hot", "one2", "one1", "one0"]
    assert rows[0]["count"] == 2
    # и при обрезке по limit из выдачи уходит самое старое, а не самое активное
    assert [r["fingerprint"] for r in await tmp_db.list_errors(limit=2)] == ["hot", "one2"]


async def test_purge_keeps_unread_and_fresh(tmp_db):
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60 * 86400))
    fresh = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    await tmp_db.upsert_error("old-seen", "log", "error", "старая разобранная", "", "", old)
    await tmp_db.upsert_error("old-new", "log", "error", "старая непрочитанная", "", "", old)
    await tmp_db.mark_errors_seen((await tmp_db.list_errors())[1]["id"])   # только old-seen
    await tmp_db.upsert_error("fresh-seen", "log", "error", "свежая разобранная", "", "", fresh)
    await tmp_db.mark_errors_seen((await tmp_db.list_errors())[0]["id"])

    assert await tmp_db.purge_old_errors(30) == 1
    left = {r["fingerprint"] for r in await tmp_db.list_errors()}
    assert left == {"old-new", "fresh-seen"}   # непрочитанную и свежую не трогаем


# ---- capture / TelemetryHandler ---------------------------------------------------------

def test_capture_fills_traceback_and_truncates():
    try:
        raise ValueError("бум")
    except ValueError as exc:
        errorlog.capture("task", "x" * 5000, exc, context="фоновая задача")
    rec = errorlog._BUF[-1]
    assert rec["source"] == "task" and rec["context"] == "фоновая задача"
    assert len(rec["message"]) == errorlog._MAX_MESSAGE
    assert "ValueError: бум" in rec["traceback"]
    assert len(rec["traceback"]) <= errorlog._MAX_TRACEBACK


def test_capture_never_raises():
    """Телеметрия зовётся из аварийных путей — падать ей нельзя ни при каких входах."""
    class _Bad:
        def __str__(self):
            raise RuntimeError("нечитаемое сообщение")

    errorlog.capture("web", _Bad())      # не должно бросить
    errorlog.capture(None, None)         # и на мусорных аргументах тоже


def test_handler_takes_errors_only():
    h = errorlog.TelemetryHandler()
    log = logging.getLogger("app.tests.telemetry")
    log.addHandler(h)
    log.setLevel(logging.DEBUG)
    try:
        log.info("это не ошибка")
        log.warning("и это тоже")
        assert len(errorlog._BUF) == 0

        log.error("слайсер не ответил")
        assert len(errorlog._BUF) == 1
        rec = errorlog._BUF[-1]
        assert rec["source"] == "log" and rec["context"] == "app.tests.telemetry"
        assert rec["message"] == "слайсер не ответил" and rec["level"] == "error"

        # exc_info попадает в трейсбек
        try:
            raise KeyError("ключ")
        except KeyError:
            log.exception("разбор упал")
        assert "KeyError" in errorlog._BUF[-1]["traceback"]

        # помеченные notelemetry пропускаем: их источник уже записал ошибку сам
        before = len(errorlog._BUF)
        log.error("уже записано как web", extra={"notelemetry": True})
        assert len(errorlog._BUF) == before
    finally:
        log.removeHandler(h)


def test_handler_ignores_connection_reset_noise():
    """Обрыв соединения клиентом — не сбой бота (OpenTodo 03514860).

    На Windows asyncio валит ConnectionResetError из _call_connection_lost каждый раз, когда
    Telegram обрывает long-poll или клиент закрывает вкладку: в первый же день на боевой этим
    забило экран «Ошибки». Глушим точечно — только транспортные исключения логгера asyncio."""
    h = errorlog.TelemetryHandler()
    log = logging.getLogger("asyncio")
    log.addHandler(h)
    try:
        for exc in (ConnectionResetError(10054, "разрыв"), ConnectionAbortedError()):
            try:
                raise exc
            except OSError:
                log.exception("Exception in callback _ProactorBasePipeTransport"
                              "._call_connection_lost()")
        assert len(errorlog._BUF) == 0

        # прочие сбои asyncio по-прежнему доезжают: глушим шум, а не весь логгер
        try:
            raise RuntimeError("Task exception was never retrieved")
        except RuntimeError:
            log.exception("сбой фоновой задачи")
        assert len(errorlog._BUF) == 1
    finally:
        log.removeHandler(h)
        errorlog._BUF.clear()

    # у чужого логгера тот же обрыв — настоящая ошибка (например, падение нашего запроса)
    h2 = errorlog.TelemetryHandler()
    other = logging.getLogger("app.tests.transport")
    other.addHandler(h2)
    other.setLevel(logging.DEBUG)
    try:
        try:
            raise ConnectionResetError(10054, "принтер оборвал соединение")
        except OSError:
            other.exception("камера отвалилась")
        assert len(errorlog._BUF) == 1
    finally:
        other.removeHandler(h2)


def test_handler_ignores_own_logger():
    """Сбой самой телеметрии не должен зацикливаться на себя."""
    h = errorlog.TelemetryHandler()
    log = logging.getLogger("app.errorlog")
    log.addHandler(h)
    try:
        log.error("upsert_error failed")
        assert len(errorlog._BUF) == 0
    finally:
        log.removeHandler(h)


# ---- flush_once: буфер → БД + алерты ---------------------------------------------------

async def test_flush_writes_rows_and_alerts_on_new(tmp_db, alerts):
    errorlog.capture("web", "KeyError: 'plastic_id'", context="/api/calc")
    errorlog.capture("tg", "TelegramBadRequest: message is not modified", context="callback_query uid 7")
    assert await errorlog.flush_once() == 2
    assert len(errorlog._BUF) == 0            # буфер осушён

    rows = await tmp_db.list_errors()
    assert {r["source"] for r in rows} == {"web", "tg"}
    assert await tmp_db.count_new_errors() == 2

    # алерт владельцу — на каждый НОВЫЙ сбой, с ключом по отпечатку (троттлинг notify)
    assert len(alerts) == 2
    assert any("Ошибка web" in a["text"] for a in alerts)
    assert all(a["key"].startswith("err_") for a in alerts)


async def test_flush_alerts_only_on_milestones_for_repeats(tmp_db, alerts):
    """Повторяющийся сбой не должен превращаться в шторм сообщений: алерт на первый раз
    и на вехи 50/500, между ними — тишина."""
    for _ in range(60):
        errorlog.capture("log", "Слайсер не ответил за 120 с", context="app.slicer")
        await errorlog.flush_once()

    rows = await tmp_db.list_errors()
    assert len(rows) == 1 and rows[0]["count"] == 60

    texts = [a["text"] for a in alerts]
    assert len(texts) == 2, texts                    # первый + веха 50, и всё
    assert "повторилась" not in texts[0]
    assert "повторилась 50 раз" in texts[1]
    # веха уходит своим ключом — иначе троттлинг notify.alert съел бы её как дубль
    assert alerts[0]["key"] != alerts[1]["key"]


async def test_flush_survives_closed_db(alerts, monkeypatch):
    """БД ещё не поднята (ошибка на старте) или уже закрыта (ошибка на выходе) — flush
    обязан промолчать, а не свалиться сам. Соединение живёт в app/store/core (волна 0
    разреза db.py) — подменяем ТАМ: db._db больше ни до чего не достаёт."""
    monkeypatch.setattr(store_core, "_db", None)
    errorlog.capture("task", "ошибка без базы")
    assert await errorlog.flush_once() == 0
    assert alerts == []


async def test_flush_returns_record_to_buffer_when_db_is_down(alerts, monkeypatch, tmp_db):
    """Недоступность БД чаще всего секундная (локи, момент старта). Запись обязана вернуться
    в буфер и дождаться следующего захода, а не пропасть: раньше вся пачка молча терялась."""
    live = store_core.db_or_none()
    monkeypatch.setattr(store_core, "_db", None)
    try:
        errorlog.capture("task", "первая", context="app.x")
        errorlog.capture("task", "вторая", context="app.x")
        assert await errorlog.flush_once() == 0
        assert [r["message"] for r in errorlog._BUF] == ["первая", "вторая"]   # порядок сохранён
    finally:
        # вернуть соединение обязательно и при падении: иначе фикстура не закроет его, а
        # рабочий поток aiosqlite не даст процессу pytest завершиться
        monkeypatch.setattr(store_core, "_db", live)
    assert await errorlog.flush_once() == 2
    assert not errorlog._BUF
    assert {r["message"] for r in await tmp_db.list_errors()} == {"первая", "вторая"}


async def test_capture_can_skip_alert(tmp_db, alerts):
    """capture(alert=False) — для мест, которые шлют владельцу своё сообщение сами
    (_error_mw, backup_once): запись в БД есть, второго уведомления нет."""
    errorlog.capture("backup", "Бэкап БД не удался: диск отвалился", context="backup_once",
                     alert=False)
    assert await errorlog.flush_once() == 1
    assert len(await tmp_db.list_errors()) == 1
    assert alerts == []


# ---- ручки админки ---------------------------------------------------------------------

async def test_admin_errors_endpoints(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [900], raising=False)
    stub_auth(monkeypatch, _verify_admin=900)

    await tmp_db.upsert_error("a", "web", "error", "первая", "TB-A", "/api/calc")
    await tmp_db.upsert_error("b", "tg", "error", "вторая", "", "message uid 5")

    resp = await webapp._api_admin_errors(_Req({"initData": "x"}))
    j = json.loads(resp.body.decode())
    assert j["ok"] is True and j["new_count"] == 2
    assert [e["message"] for e in j["errors"]] == ["вторая", "первая"]
    assert j["errors"][1]["traceback"] == "TB-A"

    # отметить одну прочитанной → она уходит из ?new=1 и из счётчика
    eid = j["errors"][1]["id"]
    resp = await webapp._api_admin_errors_seen(_Req(body={"initData": "x", "id": eid}))
    assert json.loads(resp.body.decode()) == {"ok": True, "marked": 1}

    j = json.loads((await webapp._api_admin_errors(_Req({"initData": "x", "new": "1"}))).body.decode())
    assert j["new_count"] == 1 and [e["message"] for e in j["errors"]] == ["вторая"]

    # без id — пометить все
    resp = await webapp._api_admin_errors_seen(_Req(body={"initData": "x"}))
    assert json.loads(resp.body.decode())["marked"] == 1
    assert await tmp_db.count_new_errors() == 0


async def test_admin_errors_requires_admin(tmp_db, monkeypatch):
    stub_auth(monkeypatch, _verify_admin=None)
    resp = await webapp._api_admin_errors(_Req({"initData": "x"}))
    assert resp.status == 403
    resp = await webapp._api_admin_errors_seen(_Req(body={"initData": "x"}))
    assert resp.status == 403


def test_admin_errors_route_is_owner_only():
    """В _OPERATOR_ROUTES телеметрию не вносим: карта закрыта по умолчанию, значит наёмный
    оператор эти ручки не откроет (трейсбеки — это внутренности мастерской)."""
    assert webapp._route_cap("/api/admin/errors") == roles.CAP_SETTINGS
    assert webapp._route_cap("/api/admin/errors/seen") == roles.CAP_SETTINGS


# ---- /api/jserr ------------------------------------------------------------------------

async def test_jserr_captures_and_truncates(monkeypatch):
    monkeypatch.setattr(webapp, "_verify_init_data", lambda s: 4471)
    ratelimit._hits.pop("jserr:4471", None)

    resp = await webapp._api_jserr(_Req(body={"initData": "x", "text": "y" * 900}))
    assert json.loads(resp.body.decode()) == {"ok": True}
    rec = errorlog._BUF[-1]
    assert rec["source"] == "js" and rec["context"] == "uid 4471"
    assert len(rec["message"]) == 500            # обрезано ручкой до 500

    # пустой текст не пишем
    resp = await webapp._api_jserr(_Req(body={"initData": "x", "text": "   "}))
    assert json.loads(resp.body.decode())["ok"] is False
    assert len(errorlog._BUF) == 1


async def test_jserr_throttles_per_user(monkeypatch):
    """3 за 5 минут: цикл «ошибка в обработчике ошибок» иначе завалит телеметрию за секунды.
    Лимит проверяется ДО разбора текста, поэтому слот тратит любой запрос пользователя."""
    monkeypatch.setattr(webapp, "_verify_init_data", lambda s: 4472)
    ratelimit._hits.pop("jserr:4472", None)

    for i in range(3):
        assert json.loads((await webapp._api_jserr(
            _Req(body={"initData": "x", "text": f"ошибка {i}"}))).body.decode())["ok"] is True
    resp = await webapp._api_jserr(_Req(body={"initData": "x", "text": "четвёртая"}))
    assert json.loads(resp.body.decode()) == {"ok": False, "error": "too many"}
    assert len(errorlog._BUF) == 3


async def test_jserr_requires_signature(monkeypatch):
    monkeypatch.setattr(webapp, "_verify_init_data", lambda s: None)
    resp = await webapp._api_jserr(_Req(body={"text": "чужая ошибка"}))
    assert resp.status == 403
    assert len(errorlog._BUF) == 0


# ---- бейдж в хабе ----------------------------------------------------------------------

async def test_hub_counts_new_errors(tmp_db, monkeypatch):
    """Бейдж раздела в обеих админках берётся из /api/admin/hub — без него экран пришлось бы
    открывать «на всякий случай»."""
    monkeypatch.setattr(config, "ADMIN_IDS", [900], raising=False)
    stub_auth(monkeypatch, _verify_admin=900)
    await tmp_db.upsert_error("h1", "web", "error", "сбой", "", "/api/x")

    j = json.loads((await webapp._api_admin_hub(_Req({"initData": "x"}))).body.decode())
    assert j["counts"]["errors"] == 1

    await tmp_db.mark_errors_seen()
    j = json.loads((await webapp._api_admin_hub(_Req({"initData": "x"}))).body.decode())
    assert j["counts"]["errors"] == 0


# ---- совместное поведение с _error_mw ---------------------------------------------------

async def test_web_middleware_records_once(alerts):
    """_error_mw пишет ошибку через capture('web') и глушит свой log.exception меткой
    notelemetry — иначе TelemetryHandler завёл бы вторую строку source='log' на тот же сбой.
    (alerts заодно подменяет notify.alert — модуль notify общий для errorlog и webapp.)"""
    handler_log = logging.getLogger("app.webapp")
    h = errorlog.TelemetryHandler()
    handler_log.addHandler(h)

    async def _boom(_request):
        raise RuntimeError("веб-хендлер упал")

    try:
        resp = await webapp._error_mw(_Req(path="/api/calc"), _boom)
    finally:
        handler_log.removeHandler(h)
    assert resp.status == 500

    # ровно одна запись, и она из веб-источника (с путём запроса в контексте)
    assert len(errorlog._BUF) == 1
    assert errorlog._BUF[0]["source"] == "web"
    assert errorlog._BUF[0]["context"] == "/api/calc"
    assert "RuntimeError" in errorlog._BUF[0]["traceback"]


async def test_web_middleware_folds_by_route_not_by_url(tmp_db, alerts):
    """Контекст отпечатка — КАНОНИЧЕСКИЙ путь маршрута, а не сырой URL. Иначе одна поломка
    превью (/img/105, /img/104, …) заводила бы строку и алерт на каждую карточку каталога, а
    непрочитанные строки purge_old_errors не удаляет никогда."""
    async def _boom(_request):
        raise RuntimeError("нет файла превью")

    for cid in (105, 104, 103):
        resp = await webapp._error_mw(_Req(path=f"/img/{cid}", canonical="/img/{cid}"), _boom)
        assert resp.status == 500

    assert {r["context"] for r in errorlog._BUF} == {"/img/{cid}"}
    assert await errorlog.flush_once() == 3
    rows = await tmp_db.list_errors()
    assert len(rows) == 1 and rows[0]["count"] == 3, rows

    # ключ троттлинга тоже канонический — иначе владелец получил бы алерт на каждую карточку;
    # сырой путь остаётся в тексте, по нему сбой воспроизводят
    assert {a["key"] for a in alerts} == {"web_/img/{cid}"}
    assert "/img/105" in alerts[0]["text"]


async def test_unmatched_route_does_not_break_error_mw(alerts):
    """Сканеры бьют по несуществующим путям: маршрута нет — контекст сводим в '<unmatched>',
    как это делает _usage_mw, а не роняем сам обработчик ошибок."""
    async def _boom(_request):
        raise RuntimeError("бум")

    resp = await webapp._error_mw(_Req(path="/wp-login.php", canonical=""), _boom)
    assert resp.status == 500
    assert errorlog._BUF[0]["context"] == "<unmatched>"


async def test_web_error_alerts_owner_once(tmp_db, alerts):
    """На один сбой — одно сообщение владельцу. Алерт шлёт _error_mw (сразу, не дожидаясь
    дренажа буфера), телеметрия по такой записи молчит: раньше приходил дубль «⚠️ Сбой
    веб-API …» + «⚠️ ⚠️ Ошибка web: …»."""
    async def _boom(_request):
        raise KeyError("plastic_id")

    await webapp._error_mw(_Req(path="/api/calc"), _boom)
    assert len(alerts) == 1 and alerts[0]["key"] == "web_/api/calc"

    assert await errorlog.flush_once() == 1
    assert len(await tmp_db.list_errors()) == 1        # в телеметрии сбой есть…
    assert len(alerts) == 1, alerts                    # …а второго сообщения нет


# ---- экранирование алертов ---------------------------------------------------------------

async def test_alert_escapes_html_before_telegram(monkeypatch):
    """Бот создан с parse_mode=HTML, а в alert() едет то, что мы не писали руками: str()
    исключения (сплошь «'<' not supported between…») и клиентский текст из /api/jserr.
    Без экранирования Telegram отвечает 400 «can't parse entities» — алерт молча теряется
    в notify_admins, — а через мини-апп владельцу можно подсунуть кликабельную ссылку."""
    sent = []

    class _Bot:
        async def send_message(self, uid, text, reply_markup=None):
            sent.append(text)

    monkeypatch.setattr(config, "ADMIN_IDS", [900], raising=False)
    monkeypatch.setattr(notify, "_bot", _Bot(), raising=False)
    notify._last_alert.clear()
    deskevents.reset()

    await notify.alert('Ошибка js: <a href="https://evil.example/pay">Оплатите</a>', key="k1")
    await notify.alert("Ошибка log: TypeError: '<' not supported between", key="k2")

    assert "<a href" not in sent[0] and "&lt;a href" in sent[0]
    assert "'<'" not in sent[1] and "&lt;" in sent[1]
    # зеркало настольной админки не страдает: deskevents._plain() делает html.unescape()
    assert "'<' not supported" in deskevents.since(0)[1]["text"]
    notify._last_alert.clear()


# ---- сбои хендлеров бота ------------------------------------------------------------------

def _dispatcher():
    """Настоящий диспетчер бота — тот же, что собирает run.main().

    Роутеры хендлеров — модульные синглтоны, и aiogram запоминает в каждом родителя: второй
    build_dispatcher() в том же процессе упал бы «Router is already attached» (а его зовёт ещё
    и tests/test_payments.py). Сразу отвязываем — глобальное состояние остаётся как до теста."""
    dp = run.build_dispatcher()
    for r in dp.sub_routers:
        r._parent_router = None
    return dp


async def test_bot_handler_failure_reaches_telemetry():
    """Необработанное исключение хендлера бота едет в телеметрию: перехватчик регистрируется
    в run.build_dispatcher(), и до задачи f4de8627 таких ловцов не было вовсе — aiogram печатал
    трейсбек в лог, и след обрывался. Берём НАСТОЯЩИЙ диспетчер и прогоняем через его observer
    ошибок настоящий ErrorEvent."""
    dp = _dispatcher()
    msg = Message(message_id=1, date=datetime.datetime.now(datetime.UTC),
                  chat=Chat(id=7, type="private"),
                  from_user=User(id=7, is_bot=False, first_name="Клиент"))
    event = ErrorEvent(update=Update(update_id=1, message=msg), exception=KeyError("plastic_id"))

    # True — апдейт считается обработанным, polling продолжается (иначе aiogram уронит цикл)
    assert await dp.errors.trigger(event) is True

    assert len(errorlog._BUF) == 1
    rec = errorlog._BUF[0]
    assert rec["source"] == "tg" and rec["context"] == "message uid 7"
    assert "KeyError" in rec["message"] and "KeyError" in rec["traceback"]


# ---- остановка бота ----------------------------------------------------------------------

async def test_shutdown_flushes_buffer_before_closing_db(tmp_db, alerts, monkeypatch):
    """В run.shutdown() дренаж буфера обязан идти ДО db.close(): сбой за секунды до остановки —
    обычно и есть её причина, а flush_loop сливает раз в 5 с. После close писать уже некуда, и
    запись терялась молча.

    Гасим настоящую последовательность остановки, подменив её внешние эффекты. db.close()
    подменён снимком таблицы: он показывает, что успело доехать до БД к моменту закрытия —
    переставь дренаж за close, и снимок окажется пустым."""
    order: list[str] = []
    at_close: dict = {}

    def _rec(name):
        async def _f(*_a, **_kw):
            order.append(name)
        return _f

    async def _close():
        order.append("db.close")
        at_close["rows"] = await tmp_db.list_errors()   # соединение ещё живо

    monkeypatch.setattr(run.tunnel, "stop", _rec("tunnel.stop"))
    monkeypatch.setattr(run.webapp, "stop", _rec("webapp.stop"))
    monkeypatch.setattr(run.db, "close", _close)
    monkeypatch.setattr(run.cpupool, "shutdown", lambda: order.append("cpupool.shutdown"))
    bot = SimpleNamespace(session=SimpleNamespace(close=_rec("session.close")))

    errorlog.capture("tg", "TelegramNetworkError: соединение потеряно", context="polling")
    await run.shutdown(bot)

    assert not errorlog._BUF                            # буфер осушён
    assert [r["message"] for r in at_close["rows"]] == ["TelegramNetworkError: соединение потеряно"]
    # веб-слой и туннель гасим до БД: их обработчики в неё ещё ходят
    assert order.index("webapp.stop") < order.index("db.close")
    assert order.index("tunnel.stop") < order.index("db.close")
    assert order[-1] == "cpupool.shutdown"
