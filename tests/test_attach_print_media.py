"""Печать связывается с заказом ЦЕЛИКОМ, а не одним видео (OpenTodo 71523f3f).

До задачи привязка печати давала заказу только файл таймлапса: скрин стола оставался в записи
журнала, а при ручной привязке из истории принтера сама запись журнала вообще не привязывалась —
и медиа заказа (_order_prints_media) её не видели. Контракты, которые тут закреплены:

- copy_journal_photo_to_order — единая точка «скрин печати → фото результата заказа»: копирует
  только существующий файл и только если у заказа своего фото ещё нет (ручное не затираем);
- _link_attached_print_journal — ручная привязка находит запись журнала по каноничному имени
  задания, берёт свежайшую свободную, чужую привязку не крадёт, идемпотентна;
- _deliver_timelapses — скрины стола едут клиенту ТЕМ ЖЕ альбомом, что и таймлапсы, но никогда
  не выпихивают видео из лимита 10; без фото поведение прежнее (одно видео — send_video).

Ни один из путей не ходит к принтеру по сети: скачивание во время печати роняет камеру.
"""
from io import BytesIO
from types import SimpleNamespace

import aiohttp
import pytest
from conftest import stub_bot
from PIL import Image

from app import config, elegoo, printerdrv, printmedia, webapp
from app.handlers import user_flow
from app.web import printerlive


def _use_bot(monkeypatch, bot) -> None:
    """Бот — и веб-слою, и printmedia.

    Доставка альбома клиенту уехала в app/printmedia.py (волна 3 цикла 2) и держит СВОЙ
    модульный `_bot`; её ветки закрыты `if _bot is None`, поэтому одной подмены в webapp мало —
    альбом молча не уходит. В бою обоих ставит run.wire_bot()."""
    stub_bot(monkeypatch, bot)
    monkeypatch.setattr(printmedia, "_bot", bot)


def _jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (48, 36), (30, 40, 50)).save(buf, "JPEG")
    return buf.getvalue()


async def _order(db, **kw) -> int:
    return await db.add_request(
        user_id=kw.pop("user_id", 7), user_name="client", model_label="Овечка Тимми", printer="P",
        plastic_name="PLA", quantity=1, quality="Q", strength="S",
        grams_total=100, minutes_total=600, price=20, preview_path=None, plates=1, **kw)


async def _print(db, tmp_path, label, *, rid=None, photo=True, fail_pct=0.0,
                 photo_exists=True) -> int:
    jid = await db.add_print_journal(label=label, plastic_id=None, plastic_name="PLA",
                                     grams=20, minutes=120, graded=1, request_id=rid,
                                     fail_pct=fail_pct)
    if photo:
        p = tmp_path / f"jphoto_{jid}.jpg"
        if photo_exists:
            p.write_bytes(_jpeg())
        await db.set_print_journal_fields(jid, done_photo=str(p))
    return jid


class _Bot:
    """Собирает вызовы Bot API вместо отправки в Telegram."""

    def __init__(self):
        self.videos, self.groups, self.messages = [], [], []

    async def send_video(self, chat_id, video, caption=None):
        self.videos.append((chat_id, caption))

    async def send_media_group(self, chat_id, media):
        self.groups.append((chat_id, media))

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))


# ───────────────────────── скрин печати → фото результата заказа ─────────────────────────

@pytest.mark.asyncio
async def test_copy_photo_fills_empty_order_photo(tmp_db, tmp_path, monkeypatch):
    """У заказа фото нет — забираем скрин стола печати (копией, оригинал у печати остаётся)."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid = await _order(tmp_db)
    jid = await _print(tmp_db, tmp_path, "plate.gcode", rid=rid)

    assert await webapp.copy_journal_photo_to_order(jid, rid) is True
    r = await tmp_db.get_request(rid)
    dest = tmp_path / "col" / f"done_{rid}.jpg"
    assert r.done_photo == str(dest) and dest.exists()
    assert (await tmp_db.get_print_journal(jid))["done_photo"] == str(tmp_path / f"jphoto_{jid}.jpg")


@pytest.mark.asyncio
async def test_copy_photo_keeps_manual_order_photo(tmp_db, tmp_path, monkeypatch):
    """Фото заказа уже загружено мастером вручную — не затираем."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid = await _order(tmp_db)
    manual = tmp_path / "manual.jpg"
    manual.write_bytes(_jpeg())
    await tmp_db.update_request(rid, done_photo=str(manual))
    jid = await _print(tmp_db, tmp_path, "plate.gcode", rid=rid)

    assert await webapp.copy_journal_photo_to_order(jid, rid) is False
    assert (await tmp_db.get_request(rid)).done_photo == str(manual)
    assert not (tmp_path / "col" / f"done_{rid}.jpg").exists()


@pytest.mark.asyncio
async def test_copy_photo_skips_missing_file(tmp_db, tmp_path, monkeypatch):
    """Путь в записи есть, а файла нет (мастер удалил скрин) — не воскрешаем и не падаем."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid = await _order(tmp_db)
    jid = await _print(tmp_db, tmp_path, "plate.gcode", rid=rid, photo_exists=False)

    assert await webapp.copy_journal_photo_to_order(jid, rid) is False
    assert not (await tmp_db.get_request(rid)).done_photo


# ───────────────────────── привязка печати к заказу из чата ─────────────────────────

@pytest.mark.asyncio
async def test_attach_print_to_order_moves_photo(tmp_db, tmp_path, monkeypatch):
    """Классификация печати «в заказ» проставляет request_id И отдаёт заказу скрин стола."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid = await _order(tmp_db)
    jid = await _print(tmp_db, tmp_path, "plate.gcode")

    ok, text = await user_flow._attach_print_to_order(jid, rid)
    assert ok and f"#{rid}" in text
    assert (await tmp_db.get_print_journal(jid))["request_id"] == rid
    assert (await tmp_db.get_request(rid)).done_photo == str(tmp_path / "col" / f"done_{rid}.jpg")


# ───────────────────────── ручная привязка печати из истории принтера ─────────────────────────

@pytest.mark.asyncio
async def test_link_journal_matches_normalized_name(tmp_db, tmp_path, monkeypatch):
    """Имя из истории принтера приходит без «.gcode», в другом регистре и с двойным пробелом —
    матчим каноничным ключом (elegoo.normalize_job_name), как это делает свип."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid = await _order(tmp_db)
    jid = await _print(tmp_db, tmp_path, "ECC2_Body_Elegoo PLA Matte _0.12.gcode")

    assert await webapp._link_attached_print_journal(rid, "ecc2_body_elegoo  pla matte _0.12") is True
    assert (await tmp_db.get_print_journal(jid))["request_id"] == rid
    assert (await tmp_db.get_request(rid)).done_photo == str(tmp_path / "col" / f"done_{rid}.jpg")


@pytest.mark.asyncio
async def test_link_journal_does_not_steal_foreign_print(tmp_db, tmp_path, monkeypatch):
    """Печать уже отнесена к ДРУГОМУ заказу — не перевешиваем (иначе тот заказ теряет печать)."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    other, rid = await _order(tmp_db), await _order(tmp_db)
    jid = await _print(tmp_db, tmp_path, "plate.gcode", rid=other)

    assert await webapp._link_attached_print_journal(rid, "plate.gcode") is False
    assert (await tmp_db.get_print_journal(jid))["request_id"] == other
    assert not (await tmp_db.get_request(rid)).done_photo


@pytest.mark.asyncio
async def test_link_journal_takes_freshest_free_entry(tmp_db, tmp_path, monkeypatch):
    """Одно имя задания печаталось трижды: берём самую свежую СВОБОДНУЮ запись, занятую чужим
    заказом пропускаем (list_print_journal отдаёт свежие первыми)."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    other, rid = await _order(tmp_db), await _order(tmp_db)
    old = await _print(tmp_db, tmp_path, "plate.gcode")
    mid = await _print(tmp_db, tmp_path, "plate.gcode")
    taken = await _print(tmp_db, tmp_path, "plate.gcode", rid=other)

    assert await webapp._link_attached_print_journal(rid, "plate.gcode") is True
    assert (await tmp_db.get_print_journal(mid))["request_id"] == rid, "свежайшая свободная"
    assert not (await tmp_db.get_print_journal(old))["request_id"]
    assert (await tmp_db.get_print_journal(taken))["request_id"] == other


@pytest.mark.asyncio
async def test_link_journal_is_idempotent(tmp_db, tmp_path, monkeypatch):
    """Повторная привязка той же печати к тому же заказу — успех без дублей и без перезаписи
    уже скопированного фото (мастер жмёт «Прикрепить» дважды)."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid = await _order(tmp_db)
    jid = await _print(tmp_db, tmp_path, "plate.gcode")

    assert await webapp._link_attached_print_journal(rid, "plate.gcode") is True
    assert await webapp._link_attached_print_journal(rid, "plate.gcode") is True
    assert [e["id"] for e in await tmp_db.list_print_journal_for_request(rid)] == [jid]
    assert (await tmp_db.get_request(rid)).done_photo == str(tmp_path / "col" / f"done_{rid}.jpg")


@pytest.mark.asyncio
async def test_link_journal_returns_false_when_nothing_matches(tmp_db, tmp_path, monkeypatch):
    """Печати в журнале нет (запись удалили) — ручка честно вернёт journal_linked=false."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid = await _order(tmp_db)
    await _print(tmp_db, tmp_path, "другая.gcode")

    assert await webapp._link_attached_print_journal(rid, "plate.gcode") is False
    assert await webapp._link_attached_print_journal(rid, "  ") is False


# ───────────────────────── скрины стола в альбоме таймлапсов ─────────────────────────

@pytest.mark.asyncio
async def test_bed_photos_skip_full_fails_and_dead_files(tmp_db, tmp_path):
    """В альбом клиенту идут скрины живых печатей: полный брак мастер перепечатывает (клиент его
    не видит), удалённый файл не воскрешаем."""
    rid = await _order(tmp_db)
    good = await _print(tmp_db, tmp_path, "p1.gcode", rid=rid)
    await _print(tmp_db, tmp_path, "p2.gcode", rid=rid, fail_pct=100)       # полный брак
    await _print(tmp_db, tmp_path, "p3.gcode", rid=rid, photo_exists=False)  # файла нет
    also = await _print(tmp_db, tmp_path, "p4.gcode", rid=rid)
    await _print(tmp_db, tmp_path, "p5.gcode", rid=rid, photo=False)         # скрина не было

    photos = await webapp._collect_order_bed_photos(rid)
    assert [p.name for p in photos] == [f"jphoto_{good}.jpg", f"jphoto_{also}.jpg"]


@pytest.mark.asyncio
async def test_album_mixes_videos_and_photos(tmp_path, monkeypatch):
    """Видео и скрины стола уходят ОДНИМ альбомом: сначала видео, подпись — на первом элементе."""
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    vids = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for v in vids:
        v.write_bytes(b"v" * 10)
    photos = [tmp_path / "p1.jpg", tmp_path / "p2.jpg"]
    for p in photos:
        p.write_bytes(_jpeg())
    r = SimpleNamespace(id=5, user_id=7, model_label="Овечка Тимми")

    await webapp._deliver_timelapses(r, vids, photos)

    assert not bot.videos and len(bot.groups) == 1
    chat, media = bot.groups[0]
    assert chat == 7
    assert [m.type for m in media] == ["video", "video", "photo", "photo"]
    assert media[0].caption and "#5" in media[0].caption
    assert all(m.caption is None for m in media[1:]), "подпись — только на первом элементе"
    assert not bot.messages, "всё влезло в альбом — ссылки в мини-апп не нужно"


@pytest.mark.asyncio
async def test_album_single_video_with_photo_becomes_group(tmp_path, monkeypatch):
    """Одна печать со скрином: раньше уходило send_video — теперь альбом, но подпись сохраняется."""
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    vid = tmp_path / "only.mp4"
    vid.write_bytes(b"v" * 10)
    photo = tmp_path / "only.jpg"
    photo.write_bytes(_jpeg())
    r = SimpleNamespace(id=5, user_id=7, model_label="Овечка Тимми")

    await webapp._deliver_timelapses(r, [vid], [photo])

    assert not bot.videos and len(bot.groups) == 1
    media = bot.groups[0][1]
    assert [m.type for m in media] == ["video", "photo"]
    assert "Таймлапс печати" in media[0].caption


@pytest.mark.asyncio
async def test_single_video_without_photos_keeps_old_flow(tmp_path, monkeypatch):
    """Без скринов поведение ПОЛНОСТЬЮ прежнее: одно видео — обычным сообщением с подписью."""
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    vid = tmp_path / "only.mp4"
    vid.write_bytes(b"v" * 10)
    r = SimpleNamespace(id=5, user_id=7, model_label="Овечка Тимми")

    await webapp._deliver_timelapses(r, [vid])
    await webapp._deliver_timelapses(r, [vid], [])

    assert not bot.groups and len(bot.videos) == 2
    assert all("Таймлапс печати" in cap for _c, cap in bot.videos)


@pytest.mark.asyncio
async def test_photos_never_push_videos_out_of_album(tmp_path, monkeypatch):
    """Слотов в альбоме Bot API десять: видео приоритетнее — при 10 видео фото не едут вовсе."""
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    vids = []
    for i in range(10):
        v = tmp_path / f"v{i}.mp4"
        v.write_bytes(b"v" * 10)
        vids.append(v)
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg())

    await webapp._deliver_timelapses(SimpleNamespace(id=5, user_id=7, model_label="М"),
                                     vids, [photo])

    media = bot.groups[0][1]
    assert len(media) == 10 and all(m.type == "video" for m in media)
    assert not bot.messages, "ровно десять видео — ничего не осталось за бортом"


@pytest.mark.asyncio
async def test_extra_videos_still_go_by_link(tmp_path, monkeypatch):
    """Видео больше десяти: первые десять — альбомом, остальное — ссылкой в мини-апп (ветка
    существовала до задачи и не должна была сломаться), фото при этом не отправляем."""
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    vids = []
    for i in range(12):
        v = tmp_path / f"v{i}.mp4"
        v.write_bytes(b"v" * 10)
        vids.append(v)
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg())

    await webapp._deliver_timelapses(SimpleNamespace(id=5, user_id=7, model_label="М"),
                                     vids, [photo])

    media = bot.groups[0][1]
    assert len(media) == 10 and all(m.type == "video" for m in media)
    assert len(bot.messages) == 1 and "приложении" in bot.messages[0][1]


@pytest.mark.asyncio
async def test_no_videos_no_album(tmp_path, monkeypatch):
    """Скрины без таймлапсов сами по себе клиенту не шлём (вне задачи): пустой список видео —
    тихий выход, как и раньше."""
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_jpeg())

    await webapp._deliver_timelapses(SimpleNamespace(id=5, user_id=7, model_label="М"), [], [photo])

    assert not bot.groups and not bot.videos and not bot.messages


@pytest.mark.asyncio
async def test_finish_delivery_attaches_bed_shots(tmp_db, tmp_path, monkeypatch):
    """Сквозной путь финиша многостольного заказа: клиент получает альбом «видео печатей +
    скрины их столов». Принтер при этом не дёргаем."""
    # Пульта нет — значит за свежайшим таймлапсом в принтер не полезем, всё берём из журнала.
    # Раньше это выражалось патчем elegoo.enabled(); теперь «есть ли пульт» решает драйвер
    # карточки, и подмена модульной функции больше ничего не гасит (волна 2 цикла 2).
    printerdrv.use_driver(printerdrv.NoneDriver())
    # каталог живёт в printmedia (волна 3), webapp читает его через фасад — подменяем оба
    monkeypatch.setattr(printmedia, "_TIMELAPSE_DIR", tmp_path / "tl_order")
    monkeypatch.setattr(webapp, "_TIMELAPSE_DIR", tmp_path / "tl_order")
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    rid = await _order(tmp_db)
    for n, size in ((1, 100), (2, 200)):
        jid = await _print(tmp_db, tmp_path, f"plate_{n}.gcode", rid=rid)
        tl = tmp_path / f"tl_{jid}.mp4"
        tl.write_bytes(b"v" * size)
        await tmp_db.set_print_journal_fields(jid, timelapse=str(tl))

    assert await webapp._attach_and_deliver_timelapse(await tmp_db.get_request(rid)) is True

    media = bot.groups[0][1]
    assert [m.type for m in media] == ["video", "video", "photo", "photo"]


@pytest.mark.asyncio
async def test_late_delivery_sends_only_shots_of_delivered_prints(tmp_db, tmp_path, monkeypatch):
    """Дослыл поздних видео берёт скрины ТОЛЬКО тех печатей, чьи видео едут сейчас.

    У заказа печатей бывает много, и первый альбом (при подтверждении) их скрины уже увёз:
    сбор «по всему заказу» слал бы клиенту те же фото повторно при каждом дослыле."""
    bot = _Bot()
    _use_bot(monkeypatch, bot)
    rid = await _order(tmp_db)
    await tmp_db.update_request(rid, status="printed")

    early = await _print(tmp_db, tmp_path, "plate_1.gcode", rid=rid)      # уже отправлена ранее
    late = await _print(tmp_db, tmp_path, "plate_2.gcode", rid=rid)       # видео дорисовалось
    tl = tmp_path / f"tl_{late}.mp4"
    tl.write_bytes(b"v" * 100)
    await tmp_db.set_print_journal_fields(late, timelapse=str(tl))

    entry = await tmp_db.get_print_journal(late)
    await webapp.deliver_late_timelapses([entry])

    media = bot.groups[0][1]
    assert [m.type for m in media] == ["video", "photo"]                  # ровно одна печать
    early_photo = (await tmp_db.get_print_journal(early))["done_photo"]
    assert all(early_photo not in str(getattr(m.media, "path", "")) for m in media)


@pytest.mark.asyncio
async def test_media_paths_never_call_the_printer(tmp_db, tmp_path, monkeypatch):
    """Скачивание с принтера во время печати роняет камеру — пути «медиа печати → заказ» к нему
    не ходят: работают только с БД и локальными файлами.

    Проверяем ВЫЗОВОМ, а не чтением исходника: каждый выход наружу (сокет принтера, HTTP-качалка
    таймлапсов, кадр камеры, aiohttp) подменён ловушкой, которая запоминает обращение и бросает.
    Функции ловят исключения сами, поэтому поход к принтеру виден дважды: в списке net и в
    результате — вместо True вернётся False."""
    net: list[str] = []

    def _trap(name):
        def _boom(*_a, **_kw):
            net.append(name)
            raise AssertionError(f"поход к принтеру: {name}")
        return _boom

    # Качалка таймлапсов переехала в printmedia (волна 3 цикла 2), кадр камеры — в
    # web/printerlive (волна 4 endgame); webapp зовёт их через свой фасад, а связывание
    # произошло на импорте — ловушку ставим ВО ВСЕ модули.
    _fetchers = ("_http_get_bytes", "_download_to", "_download_timelapse")
    for mod, prefix, names in (
        (printmedia, "printmedia", _fetchers),
        (webapp, "webapp", (*_fetchers, "_grab_camera_frame")),
        (printerlive, "web.printerlive", ("_grab_camera_frame",)),
        (elegoo, "elegoo", ("_open", "request", "_request_transient", "job_history",
                                   "get_status", "cached_status", "latest_timelapse_url")),
    ):
        for n in names:
            monkeypatch.setattr(mod, n, _trap(f"{prefix}.{n}"))
    monkeypatch.setattr(aiohttp, "ClientSession", _trap("aiohttp.ClientSession"))
    monkeypatch.setattr(printerlive.aiohttp, "ClientSession", _trap("aiohttp.ClientSession"))

    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")
    rid, other = await _order(tmp_db), await _order(tmp_db)
    jid = await _print(tmp_db, tmp_path, "plate.gcode")
    linked = await _print(tmp_db, tmp_path, "shelf.gcode", rid=other)

    assert await webapp.copy_journal_photo_to_order(jid, rid) is True
    assert await webapp._link_attached_print_journal(other, "shelf.gcode") is True
    photos = await webapp._collect_order_bed_photos(other)
    assert [p.name for p in photos] == [f"jphoto_{linked}.jpg"]
    assert net == [], f"новый поход к принтеру: {net}"
