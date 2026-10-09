"""Access-лог не выдаёт токены: `?initData=<токен>` в bot.log превращается в `?initData=…`.

ЗАЧЕМ ЭТОТ ФАЙЛ. Штатный aiohttp пишет первую строку запроса как `request.path_qs`, а фронт
возит авторизацию в query — и в data/logs/bot.log ложились живые ключи: машинный
ADMIN_PANEL_TOKEN, личные сессии деска (месяц жизни, app/desklogin.py) и подписанный Telegram
initData. Разбор и способ маскировки — в докстринге app/weblog.py.

Проверяются ЧЕТЫРЕ разных вещи, и ни одна не покрывает остальные:
  1. сама функция `mask_query` — что маскирует и, не менее важно, чего НЕ трогает;
  2. НАСТОЯЩИЙ путь записи: `MaskingAccessLogger.log()` целиком, через aiohttp-форматтер, с
     реальным запросом. Без этого тест был бы зелёным и у наследника, переопределившего
     `_format_r`, — а тот не работает вовсе (см. app/weblog.py, «ПОЧЕМУ `_format_line`»);
  3. допущение, на котором это построено, — вдруг в новой версии aiohttp оно перестанет быть
     верным;
  4. ПРОВОДКА: что боевой сервер поднимается именно с этим классом. Забытый kwarg — самый
     тихий отказ здесь: маскировка есть, тесты зелёные, в логе токен.
"""
import ast
import logging
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_log import AccessLogger

from app import weblog

TOKEN = "UjcfyubUv70oSuPeRsEcReT"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Где в репозитории вообще может подниматься сервер. Перечислены корни, а не rglob по всему
# дереву: в корне лежат и .venv, и data (джанкшн на второй диск) — обход стоил бы десятки
# секунд на каждом прогоне и ничего бы не проверил.
_SOURCE_ROOTS = ("app", "docs", "tools", "prod")


# ──────────────────────────── 1. сама маскировка ────────────────────────────

@pytest.mark.parametrize("raw, expect", [
    # то самое, что нашлось в bot.log
    ("/api/admin/hub?initData=" + TOKEN, "/api/admin/hub?initData=…"),
    # путь и остальные параметры обязаны уцелеть — иначе лог перестанет быть логом
    (f"/api/admin/orderfile?rid=136&initData={TOKEN}&v=7",
     "/api/admin/orderfile?rid=136&initData=…&v=7"),
    # регистр имени поля не важен
    (f"/api/feed?INITDATA={TOKEN}", "/api/feed?INITDATA=…"),
    # лаунчер ПК-админки (admin_panel.py:64) — токен в первом же GET страницы
    (f"http://127.0.0.1:8081/?admintoken={TOKEN}", "http://127.0.0.1:8081/?admintoken=…"),
    # k — это LCD_KEY (app/web/screens.py), s — подпись ссылки на медиа (app/websign.py)
    ("/api/lcd/data?k=lcd-secret&v=12", "/api/lcd/data?k=…&v=12"),
    ("/preview/42?s=abcdef012345", "/preview/42?s=…"),
    # поле, которого сегодня нет: ловится по подстроке, без правки списка имён
    ("/x?desk_token=abc&client_secret=def", "/x?desk_token=…&client_secret=…"),
    # …и то, что трогать НЕЛЬЗЯ
    ("/api/feed", "/api/feed"),
    ("/api/products?category=boxes&limit=20", "/api/products?category=boxes&limit=20"),
    ("/api/order?id=136&rid=7&uid=99", "/api/order?id=136&rid=7&uid=99"),
    # пустое значение: скрывать нечего, «…» соврал бы, будто токен был
    ("/x?initData=", "/x?initData="),
])
def test_mask_query(raw, expect):
    assert weblog.mask_query(raw) == expect


def test_mask_query_keeps_percent_encoding():
    """Строка пересобирается по кускам, без urllib-разбора: то, что не секрет, обязано попасть
    в лог ровно таким, каким пришло, — иначе строка лога перестанет совпадать с тем, что
    видел сервер."""
    q = "%D0%BA%D0%BE%D1%80%D0%BE%D0%B1%D0%BA%D0%B0"
    assert weblog.mask_query(f"/api/search?q={q}&initData={TOKEN}") == f"/api/search?q={q}&initData=…"


# ─────────────────── 2. настоящий путь записи в лог ───────────────────

def _log_record(caplog, path, headers=None):
    """Прогнать запрос через НАСТОЯЩИЙ MaskingAccessLogger со штатным форматом aiohttp."""
    logger = logging.getLogger("tests.access")
    request = make_mocked_request("GET", path, headers=headers or {})
    response = web.Response(status=200, body=b"{}")
    with caplog.at_level(logging.INFO, logger="tests.access"):
        weblog.MaskingAccessLogger(logger, AccessLogger.LOG_FORMAT).log(request, response, 0.01)
    # ровно одна запись: AccessLogger.log глушит собственные исключения через logger.exception,
    # поэтому «две записи» или «не та запись» — это отказ маскировки, а не мелочь.
    assert len(caplog.records) == 1, [r.getMessage() for r in caplog.records]
    return caplog.records[0]


def test_access_log_hides_token_but_keeps_route(caplog):
    line = _log_record(caplog, f"/api/admin/hub?initData={TOKEN}&id=7").getMessage()
    assert TOKEN not in line, line
    assert "GET /api/admin/hub?initData=…&id=7 HTTP/1.1" in line, line
    assert " 200 " in line, line          # код ответа на месте — по нему и читают лог


def test_access_log_hides_token_in_referer(caplog):
    """Referer — вторая дверь: после лаунчера страница открыта как `/?admintoken=…`, и этот
    адрес браузер прикладывает к последующим запросам (%{Referer}i в формате aiohttp)."""
    line = _log_record(caplog, "/webui/app.js",
                       headers={"Referer": f"http://127.0.0.1:8081/?admintoken={TOKEN}"}).getMessage()
    assert TOKEN not in line, line
    assert "admintoken=…" in line, line


def test_access_log_extra_fields_masked_too(caplog):
    """`AccessLogger.log` кладёт те же значения ещё и в `extra` записи. Форматтер студии их не
    печатает, но любой другой handler (или будущий JSON-формат) достанет их оттуда —
    маскировка обязана накрывать и их."""
    record = _log_record(caplog, f"/api/feed?initData={TOKEN}")
    assert TOKEN not in record.first_request_line
    assert TOKEN not in repr(vars(record))


def test_access_log_survives_non_string_atoms(caplog):
    """`%s` и `%b` приходят в `_format_line` int'ами вопреки аннотации базового класса. Если
    маскировка забудет про isinstance, AccessLogger.log проглотит TypeError и запишет
    «Error in logging» — то есть лог молча превратится в мусор."""
    record = _log_record(caplog, "/health")
    assert record.levelno == logging.INFO
    assert "Error in logging" not in record.getMessage()


# ─────────────── 3. допущение, на котором стоит app/weblog.py ───────────────

def test_compile_format_still_hardcodes_base_class():
    """`AccessLogger.compile_format` берёт форматтеры через `getattr(AccessLogger, ...)` —
    с ЖЁСТКО ЗАШИТЫМ базовым классом, поэтому наследник с собственным `_format_r` был бы молча
    проигнорирован, и маскировать приходится в `_format_line`. Если в новой версии aiohttp это
    станет не так, пусть скажет тест, а не тишина."""
    assert "AccessLogger" in AccessLogger.compile_format.__code__.co_names, (
        "compile_format больше не адресуется к AccessLogger по имени — проверьте, "
        "не пора ли упростить app/weblog.py")


# ──────────────────────────── 4. проводка ────────────────────────────
#
# Проверка СТАТИЧЕСКАЯ, по исходникам, а не «поднимем сервер и посмотрим». Настоящий
# webapp.start() ради одного kwarg тащит в тест сборку всего приложения и раздачу бота по
# вееру _BOT_FAN, то есть трогает модульное состояние веб-слоя, которое достанется соседям по
# прогону, — цена, несопоставимая с проверяемым фактом. Что класс вообще годится в
# access_log_class, отдельно проверяет тест ниже.

def _apprunner_calls(path: Path) -> list[ast.Call]:
    """Вызовы web.AppRunner(...) в файле — узлами AST, чтобы смотреть на аргументы."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name == "AppRunner":
            out.append(node)
    return out


def _source_files() -> list[Path]:
    files = [p for p in PROJECT_ROOT.glob("*.py")]
    for root in _SOURCE_ROOTS:
        files += [p for p in (PROJECT_ROOT / root).rglob("*.py")
                  if "__pycache__" not in p.parts and ".venv" not in p.parts]
    return files


def test_masking_logger_is_valid_for_aiohttp():
    """AppRunner проверяет `issubclass(access_log_class, AbstractAccessLogger)` и падает прямо
    на старте, если это не так. Пусть падение случится здесь, а не у владельца в мастерской."""
    assert issubclass(weblog.MaskingAccessLogger, AbstractAccessLogger)


def test_every_server_start_masks_access_log():
    """Ни один AppRunner в репозитории не поднимается без маскирующего логгера.

    Файлы намеренно не перечислены руками: следующий стенд или утилита, поднявшая сервер
    штатным AppRunner, уронит этот тест, а не тихо начнёт писать токены в свой лог. Забытый
    kwarg — самый тихий отказ здесь: маскировка на месте, тесты зелёные, в логе токен."""
    seen, bad = 0, []
    for path in _source_files():
        try:
            calls = _apprunner_calls(path)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in calls:
            seen += 1
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords if k.arg}
            if not kw.get("access_log_class", "").endswith("MaskingAccessLogger"):
                bad.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
    assert seen, "не найден НИ ОДИН AppRunner — тест перестал что-либо проверять"
    assert not bad, ("AppRunner без access_log_class=…MaskingAccessLogger — в лог поедут "
                     f"токены из query (см. app/weblog.py): {bad}")


def test_production_server_is_covered():
    """…и отдельно — что боевой сервер бота вообще попал в выборку выше."""
    calls = _apprunner_calls(PROJECT_ROOT / "app" / "webapp.py")
    assert len(calls) == 1, "webapp.py поднимает сервер не одним местом — проверьте выборку"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords if k.arg}
    assert kw.get("access_log_class") == "weblog.MaskingAccessLogger", kw
