"""Страховка на три админские ручки, которые УДАЛЯЮТ файлы клиента и правят журнал.

Эти три ручки — единственное место, где нажатие в админке безвозвратно стирает медиа заказа
(скрин стола, таймлапс) и переписывает привязку печати. Волна 1 цикла 2 закрепляет их поведением
ДО переезда в app/printmedia.py: снимок маршрутов держит пару (метод, путь) и ловит пропажу
ручки, но НЕ ловит подмену — перенос кода, перепутавший два обработчика местами, снимок пройдёт.
Ловит подмену только проверка результата, и она здесь:

- `_api_admin_journal_media_delete` — медиа ЗАПИСИ ЖУРНАЛА (скрин стола, таймлапс печати);
- `_api_admin_order_media_delete`   — медиа ЗАКАЗА (фото результата, привязанный таймлапс);
- `_api_admin_order_attach_print`   — привязка ВЫБРАННОЙ печати из истории принтера к заказу.

Что зафиксировано по каждой: счастливый путь (файл действительно исчез/появился, поля в БД
изменились ровно как надо), отказ постороннему (403 и НИ ОДНОГО побочного эффекта), поведение на
несуществующем id. Отдельно — «надгробие» tl_deleted: удалённый таймлапс не должны воскресить ни
самолечение (_heal_journal_timelapse), ни свип принтера (printwatch._tick_timelapse_sweep), иначе
удаление по просьбе клиента отменяется само собой через десять минут.

База — настоящая (tmp_db), а не набор заглушек: подмена обработчиков местами обязана падать на
СОДЕРЖИМОМ таблиц, а не на том, какой фейк дёрнули.

Две вещи проверяются не утверждением в конце, а ловушками на весь файл (фикстура media_sandbox):
- удаление файла ВНЕ песочницы tmp_path — немедленный провал, поэтому боевые медиа в data/
  не может задеть даже ошибочно написанный тест;
- любой выход к принтеру — немедленный провал: скачивание во время печати роняет камеру
  (кейс 22.07, см. tests/test_attach_print_media.py::test_media_paths_never_call_the_printer).
"""
import hashlib
import hmac
import json
import time
import urllib.parse
from io import BytesIO
from pathlib import Path

import aiohttp
import pytest
from PIL import Image

from app import config, elegoo, printerdrv, printmedia, printwatch, webapp
from app.web import printerlive

_TOKEN = "424242:TEST-ONLY-TOKEN"      # подпись initData считается ИМ, а не боевым токеном из .env
_ADMIN = 500001
_OUTSIDER = 500002                     # обычный клиент: подпись валидна, прав нет

_real_unlink = Path.unlink             # снят до подмены — ловушка зовёт настоящее удаление


@pytest.fixture(autouse=True)
def media_sandbox(tmp_path, monkeypatch):
    """Песочница: все каталоги медиа — во временный, все выходы наружу — под ловушками.

    DATA_DIR подменяется не для красоты: `_unlink_data_file` удаляет ТОЛЬКО внутри DATA_DIR, и
    без подмены половина проверок молча превратилась бы в «файл не удалён, потому что он не в
    data/» — тест бы позеленел на сломанном коде. Ловушка на Path.unlink страхует обратное:
    промах мимо песочницы (например, забытый monkeypatch у _TIMELAPSE_DIR) валит тест, а не
    боевой каталог — бот сейчас работает из этой же папки.
    """
    root = tmp_path.resolve()
    data = tmp_path / "data"
    (data / "collection_photos").mkdir(parents=True)
    (data / "timelapses" / "auto").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "COLLECTION_DIR", data / "collection_photos")
    # Каталоги живут в printmedia (волна 3), но webapp читает их через свой фасад — связывание
    # произошло на импорте, поэтому подменять надо ОБА модуля, иначе половина путей смотрит
    # в боевой data/ (бот сейчас работает из этой же папки).
    for mod in (printmedia, webapp):
        monkeypatch.setattr(mod, "_TIMELAPSE_DIR", data / "timelapses")
        monkeypatch.setattr(mod, "_TIMELAPSE_AUTO_DIR", data / "timelapses" / "auto")
    # Права: свой тестовый токен и свой список владельцев, чтобы файл не зависел от .env машины.
    # ПК-токен админки гасим явно — он даёт права первого админа любому, кто знает строку.
    monkeypatch.setattr(config, "BOT_TOKEN", _TOKEN)
    monkeypatch.setattr(config, "ADMIN_IDS", [_ADMIN])
    monkeypatch.setattr(config, "ADMIN_PANEL_TOKEN", "")

    def _guarded_unlink(self, *a, **kw):
        p = Path(self)
        target = p.resolve() if p.exists() else p.absolute()
        if not target.is_relative_to(root):
            raise AssertionError(f"тест удаляет файл вне песочницы: {target}")
        return _real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", _guarded_unlink)
    _trap_printer(monkeypatch)
    yield


def _trap_printer(monkeypatch):
    """Каждый выход к принтеру — исключение. Ручки медиа обязаны работать на БД и локальных
    файлах: HTTP-качалка таймлапсов делит с камерой один слабый сервер принтера, и поход к нему
    во время печати роняет поток (кейс 22.07). Есть ли пульт вообще — отдельный вопрос,
    и решает его драйвер карточки (см. _printer_ready/_printer_absent)."""
    def _boom(name):
        def _fail(*_a, **_kw):
            raise AssertionError(f"поход к принтеру из ручки медиа: {name}")
        return _fail

    # Качалка таймлапсов переехала в printmedia (волна 3 цикла 2), кадр камеры — в
    # web/printerlive (волна 4 endgame); webapp зовёт их через свой фасад, а связывание
    # произошло на импорте — ловушку ставим ВО ВСЕ модули, иначе часть путей ушла бы к
    # настоящему принтеру мимо неё.
    _fetchers = ("_http_get_bytes", "_download_to", "_download_timelapse",
                 "cache_timelapse_from_url")
    for mod, prefix, names in (
        (printmedia, "printmedia", _fetchers),
        (webapp, "webapp", (*_fetchers, "_grab_camera_frame")),
        (printerlive, "web.printerlive", ("_grab_camera_frame",)),
        (elegoo, "elegoo", ("_open", "request", "_request_transient", "job_history",
                                   "get_status", "cached_status", "latest_timelapse_url")),
    ):
        for n in names:
            monkeypatch.setattr(mod, n, _boom(f"{prefix}.{n}"))
    monkeypatch.setattr(aiohttp, "ClientSession", _boom("aiohttp.ClientSession"))
    monkeypatch.setattr(printerlive.aiohttp, "ClientSession", _boom("aiohttp.ClientSession"))


def _printer_ready() -> None:
    """У мастерской есть пульт. Раньше это выражалось патчем `elegoo.enabled()`; после того как
    адрес поехал параметром (волна 2 цикла 2), «настроен ли принтер» спрашивают у драйвера
    КАРТОЧКИ, и подмена модульной функции ничего не решает. Симулятор здесь честнее живого
    ElegooDriver: тот считал бы себя настроенным по .env машины, где идёт прогон.
    Подмену снимает autouse-фикстура _no_printer_override из conftest."""
    printerdrv.use_driver(printerdrv.FakePrinter())


def _printer_absent() -> None:
    """Пульта нет: карточка заведена как «только профиль слайсера»."""
    printerdrv.use_driver(printerdrv.NoneDriver())


# ───────────────────────────── мелкий инструментарий ─────────────────────────────

def _init_data(uid: int) -> str:
    """Настоящая подпись Telegram initData тестовым токеном — проверка прав идёт полным путём
    (_verify_user → config.is_admin), а не через подменённый _verify_admin."""
    params = {"user": json.dumps({"id": uid, "first_name": "T"}, separators=(",", ":")),
              "auth_date": str(int(time.time())), "query_id": "q"}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", _TOKEN.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


class _BrokenBody:
    """Запрос, у которого тело не разбирается, — так ведёт себя aiohttp на битом JSON."""

    async def json(self):
        raise ValueError("не JSON")


def _jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (40, 30), (20, 90, 140)).save(buf, "JPEG")
    return buf.getvalue()


async def _order(db, **kw) -> int:
    return await db.add_request(
        user_id=kw.pop("user_id", 7), user_name="client", model_label="Овечка Тимми", printer="P",
        plastic_name="PLA", quantity=1, quality="Q", strength="S", grams_total=100,
        minutes_total=600, price=20, preview_path=None, plates=1, **kw)


async def _journal(db, label="plate.gcode", **kw) -> int:
    return await db.add_print_journal(label=label, plastic_id=None, plastic_name="PLA", grams=20,
                                      minutes=120, graded=1, **kw)


def _bed_shot(tmp_path, jid: int) -> Path:
    """Скрин стола записи журнала — настоящий JPEG: заказ забирает его пережимом через PIL."""
    p = config.DATA_DIR / f"jphoto_{jid}.jpg"
    p.write_bytes(_jpeg())
    return p


def _auto_cache(job: str, payload: bytes) -> Path:
    """Ролик в авто-кэше — там же и под тем же именем, что кладёт автовыгрузка после печати."""
    p = webapp._TIMELAPSE_AUTO_DIR / f"{webapp._safe_job_name(job)}.mp4"
    p.write_bytes(payload)
    assert webapp.cached_timelapse_path(job) == p, "тест положил кэш не туда, куда смотрит код"
    return p


# ═══════════════════ медиа записи журнала: _api_admin_journal_media_delete ═══════════════════

async def test_journal_photo_delete_removes_file_and_clears_field(tmp_db, tmp_path, admin_req,
                                                                  body):
    """Счастливый путь скрина стола: поле записи очищено И файл с диска исчез."""
    jid = await _journal(tmp_db)
    photo = _bed_shot(tmp_path, jid)
    await tmp_db.set_print_journal_fields(jid, done_photo=str(photo))

    resp = await webapp._api_admin_journal_media_delete(
        admin_req(body={"id": jid, "kind": "photo"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    entry = await tmp_db.get_print_journal(jid)
    assert entry["done_photo"] is None
    assert not photo.exists()
    assert not int(entry["tl_deleted"] or 0), "надгробие ставит только удаление таймлапса"


async def test_journal_timelapse_delete_tombstones_against_resurrection(tmp_db, admin_req, body):
    """Счастливый путь таймлапса плюс главный смысл ручки: удаление НЕОБРАТИМО.

    Видео печати живёт в двух местах — поле записи и авто-кэш по имени задания. Удалить надо оба,
    а на запись поставить надгробие tl_deleted=1: без него самолечение и свип принтера вернут
    ролик из кэша при первом же показе журнала, и клиент снова увидит то, что просил стереть.
    Проверяем не флаг ради флага, а его следствие — кэш ВОССОЗДАЁМ и зовём обоих воскресителей."""
    jid = await _journal(tmp_db, label="plate.gcode")
    tl = webapp._TIMELAPSE_DIR / "j.mp4"
    tl.write_bytes(b"v" * 100)
    await tmp_db.set_print_journal_fields(jid, timelapse=str(tl))
    cache = _auto_cache("plate.gcode", b"v" * 100)

    resp = await webapp._api_admin_journal_media_delete(
        admin_req(body={"id": jid, "kind": "timelapse"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    entry = await tmp_db.get_print_journal(jid)
    assert entry["timelapse"] is None and int(entry["tl_deleted"]) == 1
    assert not tl.exists() and not cache.exists()

    _auto_cache("plate.gcode", b"v" * 100)           # принтер дорисовал ролик заново
    assert await webapp._heal_journal_timelapse(await tmp_db.get_print_journal(jid)) == ""
    await printwatch._tick_timelapse_sweep()
    assert (await tmp_db.get_print_journal(jid))["timelapse"] is None, "свип воскресил удалённое"


async def test_journal_timelapse_delete_drops_order_copy_of_same_size(tmp_db, admin_req, body):
    """Копия того же видео у привязанного заказа гасится заодно — иначе клиент продолжает его
    видеть в карточке заказа. Дубль опознаётся по размеру файла (как в _order_prints_media)."""
    rid = await _order(tmp_db)
    jid = await _journal(tmp_db, request_id=rid)
    tl = webapp._TIMELAPSE_DIR / "j.mp4"
    tl.write_bytes(b"v" * 100)
    await tmp_db.set_print_journal_fields(jid, timelapse=str(tl))
    copy = webapp._TIMELAPSE_DIR / f"{rid}.mp4"
    copy.write_bytes(b"x" * 100)                     # тот же размер → это копия журнального
    await tmp_db.update_request(rid, timelapse=str(copy))

    resp = await webapp._api_admin_journal_media_delete(
        admin_req(body={"id": jid, "kind": "timelapse"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    assert not copy.exists()
    assert (await tmp_db.get_request(rid)).timelapse is None


async def test_journal_timelapse_delete_keeps_order_video_of_other_size(tmp_db, admin_req, body):
    """У заказа СВОЁ видео (размер другой) — его не трогаем: удаляли медиа одной печати, а не
    всего заказа, и многостольный заказ легко теряет чужой ролик на неаккуратном сравнении."""
    rid = await _order(tmp_db)
    jid = await _journal(tmp_db, request_id=rid)
    tl = webapp._TIMELAPSE_DIR / "j.mp4"
    tl.write_bytes(b"v" * 100)
    await tmp_db.set_print_journal_fields(jid, timelapse=str(tl))
    own = webapp._TIMELAPSE_DIR / f"{rid}.mp4"
    own.write_bytes(b"x" * 250)
    await tmp_db.update_request(rid, timelapse=str(own))

    resp = await webapp._api_admin_journal_media_delete(
        admin_req(body={"id": jid, "kind": "timelapse"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    assert own.exists()
    assert (await tmp_db.get_request(rid)).timelapse == str(own)


async def test_journal_media_delete_unknown_id_changes_nothing(tmp_db, tmp_path, admin_req, body):
    """Несуществующая запись: мягкий отказ (ok=false, HTTP 200 — админка показывает текст), и
    ни один чужой файл при этом не пострадал."""
    jid = await _journal(tmp_db)
    photo = _bed_shot(tmp_path, jid)
    await tmp_db.set_print_journal_fields(jid, done_photo=str(photo))

    resp = await webapp._api_admin_journal_media_delete(
        admin_req(body={"id": 10 ** 6, "kind": "photo"}, init_data=_init_data(_ADMIN)))

    assert resp.status == 200
    assert body(resp) == {"ok": False, "error": "Запись не найдена"}
    assert photo.exists()
    assert (await tmp_db.get_print_journal(jid))["done_photo"] == str(photo)


async def test_journal_media_delete_rejects_unknown_kind(tmp_db, tmp_path, admin_req, body):
    """Неизвестный kind — отказ, а не «удалим что-нибудь»: kind приходит из фронта, и опечатка
    в нём не должна оборачиваться стиранием не того файла."""
    jid = await _journal(tmp_db)
    photo = _bed_shot(tmp_path, jid)
    tl = webapp._TIMELAPSE_DIR / "j.mp4"
    tl.write_bytes(b"v" * 10)
    await tmp_db.set_print_journal_fields(jid, done_photo=str(photo), timelapse=str(tl))

    resp = await webapp._api_admin_journal_media_delete(
        admin_req(body={"id": jid, "kind": "video"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is False
    assert photo.exists() and tl.exists()
    entry = await tmp_db.get_print_journal(jid)
    assert entry["done_photo"] == str(photo) and entry["timelapse"] == str(tl)


async def test_journal_media_delete_forbidden_for_outsider(tmp_db, tmp_path, admin_req, body):
    """Посторонний с ВАЛИДНОЙ подписью Telegram (обычный клиент) — 403 и ноль последствий."""
    jid = await _journal(tmp_db)
    photo = _bed_shot(tmp_path, jid)
    await tmp_db.set_print_journal_fields(jid, done_photo=str(photo))

    for init in (_init_data(_OUTSIDER), "подделка"):
        resp = await webapp._api_admin_journal_media_delete(
            admin_req(body={"id": jid, "kind": "photo"}, init_data=init))
        assert resp.status == 403
        assert body(resp)["ok"] is False

    assert photo.exists()
    assert (await tmp_db.get_print_journal(jid))["done_photo"] == str(photo)


# ═══════════════════════ медиа заказа: _api_admin_order_media_delete ═══════════════════════

async def test_order_photo_delete_removes_file_and_clears_field(tmp_db, admin_req, body):
    """Счастливый путь фото результата заказа: поле пусто, файл удалён."""
    rid = await _order(tmp_db)
    photo = config.COLLECTION_DIR / f"done_{rid}.jpg"
    photo.write_bytes(_jpeg())
    await tmp_db.update_request(rid, done_photo=str(photo))

    resp = await webapp._api_admin_order_media_delete(
        admin_req(body={"id": rid, "kind": "photo"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    assert not photo.exists()
    assert not (await tmp_db.get_request(rid)).done_photo


async def test_order_timelapse_delete_removes_own_copy(tmp_db, admin_req, body):
    """Счастливый путь таймлапса заказа: у заказа файл — собственная копия timelapses/<rid>.mp4,
    её и удаляем вместе с полем."""
    rid = await _order(tmp_db)
    tl = webapp._TIMELAPSE_DIR / f"{rid}.mp4"
    tl.write_bytes(b"v" * 30)
    await tmp_db.update_request(rid, timelapse=str(tl))

    resp = await webapp._api_admin_order_media_delete(
        admin_req(body={"id": rid, "kind": "timelapse"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    assert not tl.exists()
    assert (await tmp_db.get_request(rid)).timelapse is None


async def test_order_timelapse_delete_keeps_shared_auto_cache(tmp_db, admin_req, body):
    """Поле заказа показывает на ОБЩИЙ авто-кэш журнала — файл оставляем: им владеют записи
    журнала, и удаление «своей» ссылки не должно выбивать видео у самой печати."""
    rid = await _order(tmp_db)
    jid = await _journal(tmp_db, label="plate.gcode", request_id=rid)
    cache = _auto_cache("plate.gcode", b"v" * 40)
    await tmp_db.set_print_journal_fields(jid, timelapse=str(cache))
    await tmp_db.update_request(rid, timelapse=str(cache))

    resp = await webapp._api_admin_order_media_delete(
        admin_req(body={"id": rid, "kind": "timelapse"}, init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    assert (await tmp_db.get_request(rid)).timelapse is None
    assert cache.exists(), "снесли общий кэш — у записи журнала пропало видео"
    entry = await tmp_db.get_print_journal(jid)
    assert await webapp._heal_journal_timelapse(entry) == str(cache)


async def test_order_media_delete_unknown_id_changes_nothing(tmp_db, admin_req, body):
    """Несуществующий заказ: мягкий отказ и ни одного удаления."""
    rid = await _order(tmp_db)
    photo = config.COLLECTION_DIR / f"done_{rid}.jpg"
    photo.write_bytes(_jpeg())
    await tmp_db.update_request(rid, done_photo=str(photo))

    resp = await webapp._api_admin_order_media_delete(
        admin_req(body={"id": 10 ** 6, "kind": "photo"}, init_data=_init_data(_ADMIN)))

    assert resp.status == 200
    assert body(resp) == {"ok": False, "error": "Заказ не найден"}
    assert photo.exists()
    assert (await tmp_db.get_request(rid)).done_photo == str(photo)


async def test_order_media_delete_forbidden_for_outsider(tmp_db, admin_req, body):
    """Посторонний не может стереть медиа чужого заказа даже у СВОЕГО клиента — 403."""
    rid = await _order(tmp_db, user_id=_OUTSIDER)
    tl = webapp._TIMELAPSE_DIR / f"{rid}.mp4"
    tl.write_bytes(b"v" * 30)
    await tmp_db.update_request(rid, timelapse=str(tl))

    resp = await webapp._api_admin_order_media_delete(
        admin_req(body={"id": rid, "kind": "timelapse"}, init_data=_init_data(_OUTSIDER)))

    assert resp.status == 403
    assert tl.exists()
    assert (await tmp_db.get_request(rid)).timelapse == str(tl)


# ══════════════════ привязка печати: _api_admin_order_attach_print ══════════════════

async def test_attach_print_from_cache_binds_video_journal_and_photo(tmp_db, tmp_path, admin_req,
                                                                     body, web_bot, monkeypatch):
    """Счастливый путь: ролик из авто-кэша КОПИРУЕТСЯ заказу, к заказу привязывается сама запись
    журнала и её скрин стола. Без флага send клиенту ничего не уходит (мастер часто привязывает
    печать задним числом и не хочет будить клиента)."""
    _printer_ready()
    rid = await _order(tmp_db)
    jid = await _journal(tmp_db, label="plate.gcode")
    await tmp_db.set_print_journal_fields(jid, done_photo=str(_bed_shot(tmp_path, jid)))
    cache = _auto_cache("plate.gcode", b"v" * 64)

    resp = await webapp._api_admin_order_attach_print(
        admin_req(body={"id": rid, "job_name": "plate.gcode"}, init_data=_init_data(_ADMIN)))

    got = body(resp)
    assert got["ok"] is True and got["journal_linked"] is True
    dest = webapp._TIMELAPSE_DIR / f"{rid}.mp4"
    assert dest.read_bytes() == b"v" * 64
    assert cache.exists(), "кэш журнала должен остаться — заказу отдаётся КОПИЯ"
    assert dest.resolve().is_relative_to(tmp_path.resolve()), "медиа ушли мимо песочницы"
    r = await tmp_db.get_request(rid)
    assert r.timelapse == str(dest)
    assert r.done_photo == str(config.COLLECTION_DIR / f"done_{rid}.jpg")
    assert Path(r.done_photo).exists()
    assert got["timelapse"].startswith(f"/timelapse/{rid}?")
    assert (await tmp_db.get_print_journal(jid))["request_id"] == rid
    assert not web_bot.sent, "без send клиенту ничего не отправляем"


async def test_attach_print_sends_video_when_asked(tmp_db, admin_req, body, web_bot, monkeypatch):
    """С флагом send видео сразу уходит клиенту — тем же заходом, одним сообщением."""
    _printer_ready()
    rid = await _order(tmp_db, user_id=77)
    await _journal(tmp_db, label="plate.gcode")
    _auto_cache("plate.gcode", b"v" * 64)

    resp = await webapp._api_admin_order_attach_print(
        admin_req(body={"id": rid, "job_name": "plate.gcode", "send": True},
                  init_data=_init_data(_ADMIN)))

    assert body(resp)["ok"] is True
    assert [chat for chat, _v, _c in web_bot.videos] == [77]


async def test_attach_print_without_job_name_is_refused(tmp_db, admin_req, body, monkeypatch):
    """Мастер не выбрал печать в списке — понятный отказ, а не привязка «чего-нибудь»."""
    _printer_ready()
    rid = await _order(tmp_db)

    resp = await webapp._api_admin_order_attach_print(
        admin_req(body={"id": rid, "job_name": "  "}, init_data=_init_data(_ADMIN)))

    assert body(resp) == {"ok": False, "error": "Выберите печать из истории"}
    assert (await tmp_db.get_request(rid)).timelapse is None


async def test_attach_print_unknown_order_changes_nothing(tmp_db, admin_req, body, monkeypatch):
    """Несуществующий заказ: мягкий отказ, запись журнала не перевешена и кэш не тронут."""
    _printer_ready()
    jid = await _journal(tmp_db, label="plate.gcode")
    cache = _auto_cache("plate.gcode", b"v" * 64)

    resp = await webapp._api_admin_order_attach_print(
        admin_req(body={"id": 10 ** 6, "job_name": "plate.gcode"}, init_data=_init_data(_ADMIN)))

    assert resp.status == 200
    assert body(resp) == {"ok": False, "error": "Заказ не найден"}
    assert cache.exists()
    assert not (await tmp_db.get_print_journal(jid))["request_id"]


async def test_attach_print_refused_when_printer_not_configured(tmp_db, admin_req, body,
                                                                monkeypatch):
    """Принтер Elegoo не настроен — выходим ДО всякой работы: привязывать нечего, а «история
    печати» без принтера бессмысленна. Проверка идёт раньше поиска заказа, это и фиксируем."""
    _printer_absent()
    rid = await _order(tmp_db)
    _auto_cache("plate.gcode", b"v" * 64)

    resp = await webapp._api_admin_order_attach_print(
        admin_req(body={"id": rid, "job_name": "plate.gcode"}, init_data=_init_data(_ADMIN)))

    assert body(resp) == {"ok": False, "error": "Принтер Elegoo не настроен"}
    assert (await tmp_db.get_request(rid)).timelapse is None


async def test_attach_print_forbidden_for_outsider(tmp_db, admin_req, body, monkeypatch):
    """Посторонний не может привязать печать к заказу — 403 до любых действий с медиа."""
    _printer_ready()
    rid = await _order(tmp_db)
    jid = await _journal(tmp_db, label="plate.gcode")
    _auto_cache("plate.gcode", b"v" * 64)

    resp = await webapp._api_admin_order_attach_print(
        admin_req(body={"id": rid, "job_name": "plate.gcode"}, init_data=_init_data(_OUTSIDER)))

    assert resp.status == 403
    assert (await tmp_db.get_request(rid)).timelapse is None
    assert not (await tmp_db.get_print_journal(jid))["request_id"]
    assert not (webapp._TIMELAPSE_DIR / f"{rid}.mp4").exists()


# ══════════════════════════════ общее для всех трёх ══════════════════════════════

@pytest.mark.parametrize("handler", ["_api_admin_journal_media_delete",
                                     "_api_admin_order_media_delete",
                                     "_api_admin_order_attach_print"])
async def test_broken_body_gives_400(handler, body):
    """Битое тело — 400, а не пятисотка: ручки зовут из админки, и падение обработчика
    оставило бы мастера без ответа вообще."""
    resp = await getattr(webapp, handler)(_BrokenBody())
    assert resp.status == 400
    assert body(resp)["ok"] is False
