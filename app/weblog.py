"""Access-лог веб-слоя: путь и код ответа видны, секреты из query — нет.

ЗАЧЕМ. Стандартный `aiohttp.web_log.AccessLogger` печатает первую строку запроса как есть —
`request.path_qs`, то есть ВМЕСТЕ со строкой запроса. А фронт возит авторизацию именно в
query: `?initData=…` у любой ручки (app/webui/api.js), `?admintoken=…` у лаунчера ПК-админки
(admin_panel.py:64), `?k=…` у экрана в корпусе (LCD_KEY, app/web/screens.py), `?s=…` у
подписанных ссылок на медиа (app/websign.py). В data/logs/bot.log из-за этого ложились живые
ключи:

    aiohttp.access: 127.0.0.1 [30/Aug/2026:11:11:39 +0300] "GET /api/admin/camera?initData=<токен> HTTP/1.1" 200

Утекало ровно то, что закрывал личный вход: машинный `ADMIN_PANEL_TOKEN` (живёт, пока его не
сменят руками) и ЛИЧНЫЕ токены сессий сотрудников из app/desklogin.py — а те живут месяц
(`SESSION_TTL_SEC = 30 * 86400`) и каждым обращением продлеваются. Рядом ложился подписанный
Telegram initData, годный сутки (`webauth._AUTH_TTL`).

Удалённо это не эксплуатируется: `webauth.is_local_request` принимает оба локальных токена
только с петлевого адреса и без заголовков прокси. Но лог живёт своей жизнью — ротируется
пятью файлами по 5 МБ, попадает в архивы, в отладочную переписку «пришлите bot.log» и в любой
каталог, который однажды окажется в `BOT_BACKUP_EXTRA` (backup_offsite.py). Копия лога плюс
доступ к машине студии = вход под чужим именем, то есть ровно та дыра, которую закрывал
личный вход.

КАК. Значение секретного поля заменяется на «…»; имя поля, путь, метод, код ответа и все
остальные параметры остаются. Строка по-прежнему годится для разбора («какая ручка, с каким
id, с каким кодом ответа»), но ключом больше не является.

ПОЧЕМУ ПЕРЕОПРЕДЕЛЁН `_format_line`, А НЕ `_format_r`. Очевидный способ — наследник с
собственным `_format_r` — НЕ РАБОТАЕТ И МОЛЧИТ ОБ ЭТОМ. `AccessLogger.compile_format` берёт
форматтеры через `getattr(AccessLogger, "_format_%s" % atom)` — с жёстко зашитым именем
БАЗОВОГО класса вместо cls; кэш скомпилированных форматов (`AccessLogger._FORMAT_CACHE`) тоже
адресуется по базовому классу и общий на всех наследников. Наследник получил бы штатные
форматтеры, в логе остался бы голый токен, а тест на сам переопределённый метод при этом
проходил бы. `_format_line` же зовётся через self (`fmt_info = self._format_line(...)` в
`AccessLogger.log`) — это единственная точка расширения, которая действительно срабатывает.
Заодно она перекрывает и `extra`-поля записи: `log()` раскладывает по ним ТЕ ЖЕ значения, что
и в текст строки.

Маскируются ВСЕ строковые атомы, а не только `%r`. Query приезжает в лог ещё и заголовком
Referer (после лаунчера это `"http://127.0.0.1:8081/?admintoken=…"`), а маскировка обязана
пережить смену формата строки лога без правок здесь. Для значения без «?» функция —
тождество, поэтому %a/%t/User-Agent не страдают.
"""
from __future__ import annotations

from collections.abc import Iterable

from aiohttp.web_log import AccessLogger
from aiohttp.web_request import BaseRequest
from aiohttp.web_response import StreamResponse

MASK = "…"

# Поля, значение которых в лог не пишем никогда — по точному имени, регистр не важен (фронт
# шлёт `initData`, а руками легко набрать `initdata`). Однобуквенные `k` и `s` здесь не «на
# всякий случай»: k — это LCD_KEY (app/web/screens.py:222), s — короткая HMAC-подпись ссылки
# на медиа (app/websign.py, проверка в app/web/common.py:184). Обе пускают предъявителя внутрь.
_SECRET_EXACT = frozenset({
    "initdata", "admintoken", "token", "hash", "sig", "signature", "auth", "session",
    "secret", "password", "passwd", "pwd", "code", "key", "apikey", "k", "s",
})
# …и по вхождению подстроки — чтобы следующее поле вида `desk_token` или `client_secret`
# попало под маскировку само, без правки списка выше.
_SECRET_PARTS = ("token", "secret", "password", "passwd", "apikey", "api_key")


def _is_secret(name: str) -> bool:
    n = name.strip().lower()
    return n in _SECRET_EXACT or any(part in n for part in _SECRET_PARTS)


def mask_query(value: str) -> str:
    """`/api/admin/hub?initData=ABC&id=7` → `/api/admin/hub?initData=…&id=7`.

    Работает по СЫРОЙ строке, без urllib-разбора и обратной сборки: путь и уцелевшие
    параметры должны попасть в лог ровно такими, какими пришли (percent-encoding в том
    числе), иначе строка лога перестанет совпадать с тем, что видел сервер. Значение без «?»
    и значение без секретных полей возвращаются как есть.

    Пустое значение (`?initData=`) не трогаем: скрывать нечего, а `initData=…` на его месте
    соврал бы, будто токен был.
    """
    head, sep, query = value.partition("?")
    if not sep or not query:
        return value
    masked: list[str] = []
    for pair in query.split("&"):
        name, eq, val = pair.partition("=")
        masked.append(f"{name}={MASK}" if eq and val and _is_secret(name) else pair)
    return f"{head}?{'&'.join(masked)}"


class MaskingAccessLogger(AccessLogger):
    """AccessLogger, у которого значения секретных полей query не доезжают до строки лога."""

    def _format_line(
        self, request: BaseRequest, response: StreamResponse, time: float
    ) -> Iterable[tuple[str | tuple[str, str], str]]:
        # isinstance-проверка обязательна: вопреки аннотации базового класса, `%s` и `%b`
        # приходят сюда int'ами (`_format_s`/`_format_b` возвращают response.status и
        # body_length как есть) — str-функция на них упала бы.
        return [(key, mask_query(value) if isinstance(value, str) else value)
                for key, value in super()._format_line(request, response, time)]
