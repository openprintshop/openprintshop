"""Общие фикстуры тестов.

Здесь же — общий инструментарий, чтобы каждый файл тестов не лепил свой фейк запроса, бота и
принтера (к моменту сборки таких самоделок набралось около четырёх десятков, и они успели
разъехаться между собой): фабрики запросов post_req/get_req/admin_req, FakeBot, подмена бота в
веб-слое и фикстуры, снимающие протечку состояния процесса между тестами.

Самоделки в существующих файлах при этом не тронуты: новое сюда ДОБАВЛЕНО, старое переезжает
по мере того, как эти файлы правятся по другим поводам.
"""
import asyncio
import json
import shutil

import pytest
import pytest_asyncio

from app import (
    bambu,
    config,
    db,
    elegoo,
    optional,
    printerdrv,
    printmedia,
    printwatch,
    webapp,
    webauth,
    webguard,
)
from app.web import calib as web_calib
from app.web import clientflow as web_clientflow
from app.web import crm as web_crm
from app.web import equipment as web_equipment
from app.web import gridfinity as web_gridfinity
from app.web import linkbatch as web_linkbatch
from app.web import notifhub as web_notifhub
from app.web import orderbench as web_orderbench
from app.web import orderdesk as web_orderdesk
from app.web import orderlist as web_orderlist
from app.web import ordermedia as web_ordermedia
from app.web import ordermoney as web_ordermoney
from app.web import prices as web_prices
from app.web import printerjob as web_printerjob
from app.web import printerlive as web_printerlive
from app.web import products as web_products
from app.web import quotecalc as web_quotecalc
from app.web import screens as web_screens
from app.web import shipping as web_shipping
from app.web import storefront as web_storefront
from app.web import studio as web_studio

# Модули, у которых может быть СВОЯ привязка имени проверки подписи. Порядок значения не имеет,
# важна полнота списка: см. докстринг stub_auth. Каждый доменный разрез webapp добавляет сюда
# строку: ручки уехавшего блока читают проверку из СВОИХ глобалов, и подмена в webapp до них
# уже не достаёт.
_AUTH_MODULES = (webauth, webapp, webguard, web_calib, web_equipment, web_prices,
                 web_orderbench, web_screens, web_printerlive, web_orderdesk, web_crm,
                 web_studio, web_orderlist, web_printerjob, web_storefront,
                 web_gridfinity,
                 web_linkbatch, web_notifhub, web_products, web_shipping,
                 web_ordermedia, web_quotecalc, web_clientflow, web_ordermoney)


def stub_auth(monkeypatch, **names):
    """Подменить проверку подписи СРАЗУ ВО ВСЕХ модулях, где живёт имя.

    Волна 4 цикла 2 вынесла аутентификацию в app/webauth.py, права команды — в app/webguard.py,
    а app/webapp.py оставил ФАСАД: отдельную привязку того же объекта. Привязок стало несколько,
    и подмена одной закрывает лишь часть путей:
      * хендлеры webapp читают подпись сами (`_verify_admin(request.query...)`) — им нужна
        подмена в webapp;
      * `_admin_body` живёт уже в webauth и зовёт `_verify_admin` из СВОИХ глобалов — ему
        нужна подмена в webauth;
      * `_caps_mw` живёт в webguard и зовёт `_verify_init_data` из СВОИХ глобалов — ему нужна
        подмена в webguard.
    Половинчатая подмена оставляет на пути настоящую проверку, а initData в тестах фальшивый.
    Положительный тест от этого краснеет честно (403 вместо 200), а вот отрицательный («не
    админ → 403») продолжает ПРОХОДИТЬ — но уже сам собой, а не благодаря заглушке. Поэтому
    ставим все привязки и для «пускать», и для «не пускать».

    Модуль, который такого имени не импортирует (webguard знает только `_verify_init_data`),
    пропускается — но если имени нет НИГДЕ, это опечатка, и мы падаем громко.

    Значение — либо готовая функция, либо то, что заглушка должна вернуть:
        stub_auth(monkeypatch, _verify_admin=ADMIN_ID)
        stub_auth(monkeypatch, _verify_admin=None)
    """
    for name, value in names.items():
        fn = value if callable(value) else (lambda *_a, _v=value, **_k: _v)
        hit = 0
        for mod in _AUTH_MODULES:
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, fn)
                hit += 1
        assert hit, f"stub_auth: имени {name} нет ни в одном из модулей аутентификации"


# Модули веб-слоя с СОБСТВЕННЫМ модульным `_bot` (волна 12 endgame, вариант «б»): фасадом
# `_bot` не реэкспортируется (пере-присваивается через global — вторая привязка застыла бы
# на None), поэтому подмена в одном webapp до уехавших ручек не достаёт. Каждая волна,
# увозящая домен с отправкой клиенту, добавляет сюда свой модуль ТЕМ ЖЕ коммитом; полноту
# боевого веера webapp._BOT_FAN сторожит tests/test_bot_wiring.py.
_BOT_MODULES = [webapp, web_clientflow, web_crm, web_linkbatch, web_notifhub,
                web_ordermedia, web_ordermoney, web_printerjob, web_products,
                web_quotecalc, web_shipping, web_storefront, web_gridfinity]


def stub_bot(monkeypatch, bot):
    """Подменить бота СРАЗУ ВО ВСЕХ модулях веб-слоя, которые держат модульный `_bot`.

    Замена прямому `monkeypatch.setattr(webapp, "_bot", …)`: после разреза webapp.py ручка
    может читать `_bot` из СВОЕГО модуля, и половинчатая подмена не включает (или не
    выключает) ветку отправки — тест зеленеет на тишине. Откат делает сам monkeypatch."""
    for mod in _BOT_MODULES:
        monkeypatch.setattr(mod, "_bot", bot)


@pytest.fixture(autouse=True)
def _no_gpu_avatars(monkeypatch):
    """Тесты не должны грузить SDXL в VRAM (медленно, зависит от железа): GPU-генерация аватарок
    выключена по умолчанию; тесты самого avatars_gpu включают её точечно monkeypatch-ем.

    Переводчики промта тоже глушим: gtx — сеть (в CI её нет, а с ней тест зависел бы от квоты
    Google), офлайн-модель — скачивание ~300 МБ на первом вызове. Тесты перевода подменяют
    эти две функции сами."""
    from app import avatars_gpu
    monkeypatch.setattr(config, "AVATAR_GPU", False)
    monkeypatch.setattr(avatars_gpu, "_gtx_translate", lambda p: None)
    monkeypatch.setattr(avatars_gpu, "_offline_translate", lambda p: None)
    avatars_gpu._TR_CACHE.clear()


@pytest.fixture(autouse=True)
def _printer_from_env_not_from_machine(monkeypatch):
    """Принтер тестам даёт ФИКСТУРА, а не .env машины.

    printerdrv.autodetect_kind() читает config.ELEGOO_PRINTER_IP/MOONRAKER_URL, то есть boевой
    .env разработчика. На машине владельца принтер настроен, и 18 тестов (printflow, printwatch,
    printerhub…) проходили за счёт этого; на чистой установке из install.ps1 те же тесты падали
    с «протокол принтера (none) не поддерживает…» — поймано прогоном стенда 23.08.2026.
    Ставим фиктивные Elegoo-реквизиты: они ничего не открывают (все обращения к принтеру в
    тестах и так подменяются), но делают выбор драйвера и ответ «принтер настроен»
    воспроизводимыми на любой машине. Адрес — из TEST-NET-1 (RFC 5737), гарантированно
    неживой. Драйвер считает пульт настроенным, только когда есть И адрес, И код доступа
    (printerdrv/elegoo_drv.enabled), поэтому ставим оба.
    """
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", config.ELEGOO_PRINTER_IP or "192.0.2.1")
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", config.ELEGOO_ACCESS_CODE or "test-code")
    # И состав админов. add_request помечает заказ просмотренным, если его автор — админ
    # (store/orders: admin_seen_at), а тесты заводят заказы от user_id 1, 2, 7… На установке,
    # где ADMIN_IDS из .env оказался равен такому id (у чистой установки из install.ps1 это
    # буквально «1»), счётчик непросмотренных молча уезжал. Держим ОДИН фиксированный id,
    # заведомо не совпадающий с тестовыми: десяток тестов читает config.ADMIN_IDS[0] и подделывает
    # им initData, поэтому список обязан быть непустым, а тем, кому нужен свой админ, ничто не
    # мешает подменить ADMIN_IDS у себя.
    monkeypatch.setattr(config, "ADMIN_IDS", [999_999_999])


@pytest.fixture(autouse=True)
def _timelapse_autobuild_from_test_not_from_machine(monkeypatch):
    """Автосборка таймлапсов — значение ПРОДУКТА (включена), а не .env машины.

    08.10.2026 владелец выключил её у себя (TIMELAPSE_AUTOBUILD=0): рендер грузил принтер. Без
    этой фикстуры тест сборки краснел только в основном дереве владельца — там .env есть, в
    worktree его нет, — и pre-push отменял push. Тест выключенного флага ставит его сам."""
    monkeypatch.setattr(config, "TIMELAPSE_AUTOBUILD", True)


@pytest.fixture(autouse=True)
def _offsite_dir_from_test_not_from_machine(monkeypatch):
    """Каталог офсайт-копий машины не должен просачиваться в тесты.

    На машине владельца BOT_DB_BACKUP_DIR указывает на Google Drive; тест, забывший задать
    его сам, начал бы считать живые копии мастерской (а при неудачном стечении — писать
    туда). Тест, которому переменная нужна, ставит её сам — setenv поверх этого работает."""
    monkeypatch.delenv("BOT_DB_BACKUP_DIR", raising=False)


@pytest.fixture(autouse=True)
def _clean_staff_roles():
    """Кэш ролей команды (A4) — глобальный словарь в config: без сброса роль, назначенная
    одним тестом, протекала бы в следующие (и, например, «клиент» неожиданно оказывался бы
    оператором)."""
    config.STAFF_ROLES.clear()
    yield
    config.STAFF_ROLES.clear()


@pytest.fixture(autouse=True)
def _isolate_mw_pool(tmp_path_factory, monkeypatch):
    """Пул аккаунтов MakerWorld — во временный файл для КАЖДОГО теста.

    Стор пула (data/mw_accounts.json) пишется на каждую отметку об исходе запроса — note_ok /
    note_blocked / note_error, — а их дёргают downloader и modelsearch. Без этой изоляции любой
    тест, добравшийся до такого вызова, правил БОЕВОЙ файл с реальным токеном (поймано на
    практике: у прод-аккаунта накрутились uses от прогона тестов). Тесты, которым пул нужен по
    существу, всё равно подменяют _STORE своей фикстурой — это лишь общая страховка.
    В открытой версии модуля пула нет (app/optional.py) — изолировать нечего."""
    mwtoken = optional.load("mwtoken")
    if mwtoken is None:
        return
    monkeypatch.setattr(mwtoken, "_STORE",
                        tmp_path_factory.mktemp("mwpool") / "mw_accounts.json")
    monkeypatch.setattr(mwtoken, "_mem", None)
    monkeypatch.setattr(mwtoken, "_mem_mtime", -1.0)


# ─────────────────────────────── база данных ───────────────────────────────

async def _build_db_template(path) -> None:
    """Собрать эталонный файл БД: полный db.init() и честное закрытие."""
    orig = config.DB_PATH
    config.DB_PATH = path
    try:
        await db.init()
        # Хвост WAL — в основной файл: фикстура копирует ОДИН файл, .db-wal рядом не поедет,
        # и копия осталась бы без сидов, дописанных последним commit-ом.
        await db._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await db.close()
        config.DB_PATH = orig


@pytest.fixture(scope="session")
def db_template(tmp_path_factory):
    """Готовый файл БД, собранный ОДИН раз на сессию.

    db.init() — это весь _SCHEMA, все версионные миграции и все сиды; на полутора тысячах
    тестов повторять это на каждый тест накладно. Тесты получают копию файла (см. tmp_db).

    Свой цикл событий, а не тестовый: фикстура сессионная и синхронная, а pytest-asyncio даёт
    цикл на тест. Цикл создаётся и закрывается вручную — asyncio.run() на выходе ставит
    текущим циклом None и мог бы выдернуть его из-под уже настроенного теста."""
    path = tmp_path_factory.mktemp("db_template") / "template.db"
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_build_db_template(path))
    finally:
        loop.close()
    return path


@pytest_asyncio.fixture
async def tmp_db(tmp_path, db_template):
    """Свежая БД во временном файле (схема + сиды), изолированно от прод-БД.

    Файл — копия сессионного эталона, поэтому подключаемся публичным connect_existing(), а не
    гоняем db.init() заново: состояние базы получается то же самое. Тесты САМИХ миграций
    (tests/test_integration_migration.py) сознательно живут на своей фикстуре с настоящим
    db.init() — копия готовой базы им ничего не проверила бы."""
    orig = config.DB_PATH
    path = tmp_path / "test.db"
    shutil.copyfile(db_template, path)
    config.DB_PATH = path
    await db.connect_existing(path)
    try:
        yield db
    finally:
        await db.close()
        config.DB_PATH = orig


# ─────────────────────────────── фейковый запрос ───────────────────────────────

class FakeMatchInfo(dict):
    """match_info aiohttp: словарь параметров пути ПЛЮС канонический путь роута.

    Параметры читают хендлеры (request.match_info['token']), канонический путь —
    middleware прав (request.match_info.route.resource.canonical)."""

    def __init__(self, params=None, canonical=""):
        super().__init__(params or {})
        resource = type("Res", (), {"canonical": canonical})()
        self.route = type("Route", (), {"resource": resource})()


class FakeRequest:
    """Фейк aiohttp-запроса для прямого вызова хендлеров, без поднятия сервера.

    Собран из самых полных самоделок тестов: тело (json()), query, match_info с каноническим
    путём, словарь состояния (в него middleware ролей кладёт request['caps']).

    remote, host и headers заданы ПО УМОЛЧАНИЮ — именно их отсутствие и заставляло заводить свой
    фейк в очередном файле: стоило хендлеру заглянуть в request.remote (проверка «запрос с
    этой же машины»), как чужая заглушка падала с AttributeError.

    Дефолты remote/host описывают запрос ШТАТНОГО ЛАУНЧЕРА настольной админки: он ходит на
    http://127.0.0.1:8081/ (admin_panel.py:23, desktop_admin/shell.py:32). Предикат локальности
    webauth.is_local_request читает все три поля сразу — peer, заголовки прокси и Host, — и без
    host фейк молча переставал считаться локальным."""

    def __init__(self, *, path="/", method="GET", body=None, query=None, match_info=None,
                 canonical=None, remote="127.0.0.1", host="127.0.0.1:8081", headers=None,
                 content_type="application/json"):
        self.path = path
        self.method = method
        self._body = {} if body is None else body
        self.query = dict(query or {})
        self.remote = remote
        self.host = host
        self.headers = dict(headers or {})
        self.content_type = content_type
        self._state = {}
        self.match_info = FakeMatchInfo(match_info, path if canonical is None else canonical)

    async def json(self):
        return self._body

    # ---- состояние запроса: request['caps'], request['uid'] и т. п. ----
    def __getitem__(self, key):
        return self._state[key]

    def __setitem__(self, key, value):
        self._state[key] = value

    def __contains__(self, key):
        return key in self._state

    def get(self, key, default=None):
        return self._state.get(key, default)


def post_req(body=None, **kw):
    """POST-запрос: тело отдаётся из json(). Остальные поля — как у FakeRequest."""
    kw.setdefault("method", "POST")
    return FakeRequest(body=body, **kw)


def get_req(query=None, **kw):
    """GET-запрос: параметры в query — get_req({"uid": "77"})."""
    kw.setdefault("method", "GET")
    return FakeRequest(query=query, **kw)


def admin_req(body=None, query=None, *, init_data="admin", **kw):
    """Запрос от админки: initData подставлен И в тело, И в query.

    Одни ручки читают его из тела (_admin_body), другие из query (_verify_admin(request.query))
    — фабрика кладёт в оба места, чтобы тест не гадал, какая именно перед ним. Подпись при этом
    НЕ проверяется: тест либо подменяет _verify_admin, либо кладёт сюда настоящий admin-токен."""
    method = kw.pop("method", None) or ("POST" if body is not None else "GET")
    b = dict(body or {})
    q = dict(query or {})
    b.setdefault("initData", init_data)
    q.setdefault("initData", init_data)
    return FakeRequest(method=method, body=b, query=q, **kw)


def body(resp):
    """Разобранный JSON ответа (web.json_response) — во всех тестах повторялось
    json.loads(resp.body.decode())."""
    return json.loads(resp.body.decode())


@pytest.fixture(name="post_req")
def _post_req_fixture():
    """Фабрика POST-запросов (см. функцию post_req)."""
    return post_req


@pytest.fixture(name="get_req")
def _get_req_fixture():
    """Фабрика GET-запросов (см. функцию get_req)."""
    return get_req


@pytest.fixture(name="admin_req")
def _admin_req_fixture():
    """Фабрика админских запросов (см. функцию admin_req)."""
    return admin_req


@pytest.fixture(name="body")
def _body_fixture():
    """Разбор JSON-ответа хендлера (см. функцию body)."""
    return body


# ─────────────────────────────── фейковый бот ───────────────────────────────

class FakeMessage:
    """Ответ Telegram на отправку — тестам нужен только message_id."""

    def __init__(self, message_id=1):
        self.message_id = message_id


class FakeBot:
    """Фейк бота: ничего не отправляет, всё записывает.

    Журналы раздельные (messages/photos/documents/videos/media_groups) плюс общий sent в
    порядке отправки — разные тесты проверяют разное: кому ушло, что ушло, в каком порядке.

    fail=True — «Telegram недоступен»: любая отправка бросает. Так проверяется, что вызывающий
    переживает недоступность (notify.notify_user ловит и возвращает False — клиент просто не
    запускал бота, и это нормальный исход, а не сбой)."""

    def __init__(self, *, fail=False):
        self.messages: list = []        # (chat_id, text, reply_markup)
        self.photos: list = []          # (chat_id, photo, caption)
        self.documents: list = []       # (chat_id, document, caption)
        self.videos: list = []          # (chat_id, video, caption)
        self.media_groups: list = []    # (chat_id, media)
        self.sent: list = []            # (вид, chat_id, полезное) — сплошной журнал по порядку
        self.fail = fail

    def _record(self, kind, chat_id, payload) -> FakeMessage:
        if self.fail:
            raise RuntimeError("телеграм недоступен")
        self.sent.append((kind, chat_id, payload))
        return FakeMessage(len(self.sent))

    async def send_message(self, chat_id, text="", reply_markup=None, **kw) -> FakeMessage:
        msg = self._record("text", chat_id, text)
        self.messages.append((chat_id, text, reply_markup))
        return msg

    async def send_photo(self, chat_id, photo=None, caption="", reply_markup=None,
                         **kw) -> FakeMessage:
        msg = self._record("photo", chat_id, caption)
        self.photos.append((chat_id, photo, caption))
        return msg

    async def send_document(self, chat_id, document=None, caption="", reply_markup=None,
                            **kw) -> FakeMessage:
        msg = self._record("document", chat_id, caption)
        self.documents.append((chat_id, document, caption))
        return msg

    async def send_video(self, chat_id, video=None, caption="", reply_markup=None,
                         **kw) -> FakeMessage:
        msg = self._record("video", chat_id, caption)
        self.videos.append((chat_id, video, caption))
        return msg

    async def send_media_group(self, chat_id, media=None, **kw) -> list:
        msg = self._record("group", chat_id, media)
        self.media_groups.append((chat_id, media))
        return [msg]                    # настоящий bot отдаёт СПИСОК сообщений

    @property
    def texts(self) -> list:
        """Только тексты сообщений — самая частая проверка («что написали клиенту»)."""
        return [t for _, t, _ in self.messages]


@pytest.fixture
def web_bot():
    """Фейковый бот в веб-слое на время теста.

    Ставится ПУБЛИЧНОЙ webapp.set_bot(): она возвращает прежнего бота, по нему и
    восстанавливаемся — подмена глобали руками оставляла заглушку следующему тесту.

    Доставка таймлапсов уехала в app/printmedia.py со СВОИМ модульным `_bot` (волна 3 цикла 2),
    и её ветки закрыты `if _bot is None` — без второй проводки альбом клиенту просто не
    уходит, молча и без строки в логе. В бою обоих ставит run.wire_bot()."""
    bot = FakeBot()
    prev = webapp.set_bot(bot)
    prev_media = printmedia._bot
    printmedia.set_bot(bot)
    try:
        yield bot
    finally:
        webapp.set_bot(prev)
        printmedia.set_bot(prev_media)


# ────────────────────── изоляция состояния процесса ──────────────────────

def _reset_printwatch() -> None:
    printwatch._tl_sweep_at.clear()       # гейт по времени: свип снова разрешён
    printwatch._tl_fail_until.clear()   # остывания после неудачной докачки (ключ — id записи)
    printwatch._tl_generating.clear()   # «принтер собирает mp4» — строго одна сборка за раз
    printwatch._cur.clear()              # текущая наблюдаемая печать
    printwatch._unjournaled.clear()      # финиши, ещё не легшие в журнал (повтор записи)
    printwatch._warned_at.clear()        # троттлинг WARNING — иначе тест не увидит первую запись
    printwatch._report_seen.clear()      # отчёты прошивки CC2 — алерт раз на событие


@pytest.fixture(autouse=True)
def clean_printwatch():
    """Наблюдатель печати — сплошь состояние процесса, и все эти поля тесты правят.

    Без сброса тест, дошедший до свипа таймлапсов, гасил свип у следующего (гейт по времени —
    тихий выход), а забытая «текущая печать» превращалась в чужой финиш. Раньше это чинилось
    руками в каждом таком тесте (tests/test_print_finish_media.py, tests/test_printflow.py)."""
    _reset_printwatch()
    yield
    _reset_printwatch()


@pytest.fixture(autouse=True)
def clean_elegoo_live():
    """Снимок телеметрии принтера — общий на процесс.

    Тесты его правят (притворяются, что принтер онлайн и печатает), и без восстановления
    «принтер» оставался бы онлайн у соседних тестов. Снимок и откат — как в
    tests/test_elegoo.py, только не руками."""
    with elegoo.session().live_lock:
        orig = dict(elegoo.session().live)
    yield
    with elegoo.session().live_lock:
        elegoo.session().live.clear()
        elegoo.session().live.update(orig)


@pytest.fixture(autouse=True)
def clean_models():
    """Реестр моделей веб-слоя (токен → файлы расчёта) живёт в памяти процесса и вытесняется
    по размеру: мусор от прошлых тестов и путается под ногами, и приближает вытеснение."""
    webapp._models.clear()
    yield
    webapp._models.clear()


@pytest.fixture(autouse=True)
def _never_dial_the_real_printer(monkeypatch):
    """Сторож: ни один тест не открывает соединение с НАСТОЯЩИМ принтером.

    Поймано в приёмке волны 2: тест защищался подменой `elegoo.enabled`, ворота доставки
    переехали на драйвер, подмена стала мёртвой — и полный прогон пошёл в MQTT боевого CC2.
    А `.git/hooks/pre-push` гоняет pytest, то есть это случалось внутри промоушена, по
    возможно печатающему принтеру. Лишний connect/disconnect в этом проекте уже ронял камеру.

    Ставим заглушку ПЕРВОЙ: тест, который честно подменяет сокет своим фейком, просто
    перекрывает её (его monkeypatch применяется позже). А тест, забывший это сделать,
    падает с внятным текстом вместо тихого похода к железу.

    Петлевые адреса пропускаем: на них живут тестовый сервер aiohttp и локальный прокси,
    и они к железу отношения не имеют. Подмена ставится на атрибут модуля socket, то есть
    видна всему процессу — это осознанно: уйти к принтеру можно не только из app/elegoo.py."""
    real = elegoo.socket.create_connection

    def _guard(addr, *a, **kw):
        host = str(addr[0]) if isinstance(addr, tuple) and addr else ""
        if host in ("127.0.0.1", "::1", "localhost", ""):
            return real(addr, *a, **kw)
        raise AssertionError(
            f"тест пошёл в сеть на {addr} — вероятно, к настоящему принтеру. Подмени сокет "
            f"или поставь printerdrv.use_driver(printerdrv.NoneDriver()): к железу не ходим, "
            f"лишний connect/disconnect уже ронял камеру на печатающем CC2")

    monkeypatch.setattr(elegoo.socket, "create_connection", _guard)


@pytest.fixture(autouse=True)
def _no_community_probe(monkeypatch):
    """Автопроба прошивки сообщества CC2 (app/cc2community.py) ходит фоновым потоком на :8081
    адреса карточки. В тестах адреса выдуманы, а поток пережил бы тест — гасим её, кэш чистим.
    Тест, проверяющий саму пробу, включает её обратно своим monkeypatch."""
    from app import cc2community, cc2plates, cc2spools
    monkeypatch.setattr(cc2community, "AUTO_PROBE", False)
    monkeypatch.setattr(cc2spools, "AUTO_SYNC", False)   # фоновая сверка катушек — тоже в сеть
    cc2community.forget()
    cc2spools.forget()
    cc2plates.forget()
    yield
    cc2community.forget()
    cc2spools.forget()
    cc2plates.forget()


@pytest.fixture(autouse=True)
def _no_leftover_printer_sessions():
    """Реестр сессий Elegoo живёт в модуле, то есть на процесс.

    Тест, заведший карточку принтера с собственным адресом, поднимает ей фонового подписчика —
    и без уборки его поток пережил бы тест, вечно ломясь в выдуманный адрес до конца прогона
    (сторож `_never_dial_the_real_printer` его развернёт, но крутиться он не перестанет)."""
    yield
    elegoo.sessions_stop_all()
    bambu.sessions_stop_all()


@pytest.fixture(autouse=True)
def _reset_printer_capacity():
    """Вернуть кэш «сколько машин печатает» к одной после каждого теста.

    config.ACTIVE_PRINTERS живёт в модуле, то есть на процесс, и его двигают САМИ функции
    изменения принтеров (add_printer/update_printer/delete_printer). Тест, заводящий вторую
    карточку, иначе протекал бы в чужие: срок готовности молча делился бы пополам, и падало бы
    в другом файле — именно так это и поймалось."""
    yield
    config.set_active_printers(1)


@pytest.fixture(autouse=True)
def _no_printer_override():
    """Подмена драйвера принтера живёт в модуле, то есть на процесс — снимаем её после каждого
    теста, чтобы симулятор не протёк дальше (то же tests/test_printerdrv.py делает у себя вручную).

    use_driver(None) гасит шов целиком — и подмену «на все карточки», и точечные по id, и
    фабрику, — поэтому страховка остаётся одной строкой и не отстаёт от роста шва."""
    yield
    printerdrv.use_driver(None)


@pytest.fixture
def fake_printer():
    """Симулятор принтера вместо железа на время теста.

    Фикстура ЯВНАЯ, а не autouse: выбор драйвера (for_printer → elegoo/moonraker/none) сам
    находится под тестом, и подмена, поставленная всем подряд, эти проверки бы обессмыслила."""
    printer = printerdrv.FakePrinter()
    printerdrv.use_driver(printer)
    try:
        yield printer
    finally:
        printerdrv.use_driver(None)


@pytest.fixture
def fake_printers():
    """Два симулятора на две РАЗНЫЕ карточки принтера: {id карточки: симулятор} для id 1 и 2.

    Мастерская с одним принтером скрывает целый класс ошибок: код, который берёт «принтер»
    вместо «принтер этой карточки», на одном принтере работает всегда. Здесь у карточек 1 и 2
    драйверы РАЗНЫЕ, поэтому такая ошибка становится видимой — тест сравнивает не поля, а
    тождество объекта.

    id 1 и 2 не случайны: это первые два принтера в tmp_db (боевой заведён миграцией, второй
    добавляется тестом), так что фикстуру можно класть рядом с настоящими карточками из БД."""
    printers = {1: printerdrv.FakePrinter(), 2: printerdrv.FakePrinter()}
    for pid, drv in printers.items():
        printerdrv.use_driver(drv, printer_id=pid)
    try:
        yield printers
    finally:
        printerdrv.use_driver(None)
