"""Медиа по итогам печати: скрин стола на финише и добор таймлапса свипом.

Живьём это проверяется только на реальном завершении печати, поэтому поведение
зафиксировано тестами: скрин стола обязан появляться сразу, таймлапс — добираться
периодическим свипом (принтер отдаёт файл только когда полностью допечатал, а
одноразовая отложенная задача гибла при рестарте бота — кейс «Овечка Тимми»).
"""
import asyncio
from io import BytesIO

import pytest
from conftest import stub_bot
from PIL import Image

from app import config, printerdrv, printmedia, printwatch, webapp
from app.web import printerlive

# Медиа таймлапсов уехали в app/printmedia.py (волна 3 цикла 2): свип printwatch зовёт их
# ОТТУДА, поэтому подмены качалки и доставки ставятся на printmedia. webapp продолжает отдавать
# те же объекты через фасад — там, где путь идёт через него (_attach_and_deliver_timelapse),
# подменяем ОБА модуля: связывание фасада произошло на импорте.


def _jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _printer_ready(monkeypatch, address: str = "10.77.0.5") -> None:
    """Пульт есть, и ведёт он на принтер фонового подписчика.

    Раньше здесь хватало `elegoo.enabled → True`. После волны 2 цикла 2 историю и сборку
    таймлапсов свип берёт у ДРАЙВЕРА карточки, а тот считает себя настроенным, только если
    адрес карточки совпадает с адресом подписчика — поэтому настраиваем адрес, а не флаг.

    Адрес — сентинел, а не .env машины, на которой идёт прогон: тест не должен ни зависеть от
    того, настроен ли там боевой принтер, ни тем более к нему постучаться. Сами вызовы
    протокола всё равно подменены в каждом тесте (job_history / generate_timelapse)."""
    monkeypatch.setattr(config, "ELEGOO_PRINTER_IP", address)
    monkeypatch.setattr(config, "ELEGOO_ACCESS_CODE", "SWEEP-CODE")
    printerdrv.use_driver(printerdrv.ElegooDriver())      # снимет autouse-фикстура conftest


@pytest.mark.asyncio
async def test_bed_shot_saved_to_journal(tmp_db, tmp_path, monkeypatch):
    """Кадр камеры на финише становится фото записи журнала."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")

    async def _frame(printer_id=None):
        return _jpeg()
    # Подмена в ОБА модуля: caller capture_bed_shot живёт в web/printerlive (волна 4 endgame)
    # и резолвит _grab_camera_frame из СВОИХ глобалов; webapp — лишь фасад.
    monkeypatch.setattr(webapp, "_grab_camera_frame", _frame)
    monkeypatch.setattr(printerlive, "_grab_camera_frame", _frame)

    jid = await tmp_db.add_print_journal(label="тестовая печать", plastic_id=None,
                                         plastic_name="PLA", grams=12.5, minutes=30, graded=0)
    assert await webapp.capture_bed_shot(jid) is True
    row = await tmp_db.get_print_journal(jid)
    assert row["done_photo"], "скрин стола не привязался к записи журнала"
    assert (tmp_path / "col" / f"jphoto_{jid}.jpg").exists()


@pytest.mark.asyncio
async def test_bed_shot_survives_dead_camera(tmp_db, tmp_path, monkeypatch):
    """Камера недоступна — тихо возвращаем False, без исключения: это фон печати,
    падение здесь утащило бы за собой обработку финиша."""
    monkeypatch.setattr(config, "COLLECTION_DIR", tmp_path / "col")

    async def _none(printer_id=None):
        return None
    monkeypatch.setattr(webapp, "_grab_camera_frame", _none)
    monkeypatch.setattr(printerlive, "_grab_camera_frame", _none)

    jid = await tmp_db.add_print_journal(label="без камеры", plastic_id=None, plastic_name="",
                                         grams=1, minutes=1, graded=0)
    assert await webapp.capture_bed_shot(jid) is False
    row = await tmp_db.get_print_journal(jid)
    assert not row["done_photo"]


@pytest.mark.asyncio
async def test_timelapse_sweep_attaches_from_printer_history(tmp_db, tmp_path, monkeypatch):
    """Свип (заменил одноразовую фоновую задачу, гибнувшую при рестарте бота — кейс «Овечка
    Тимми», 2bc57e3e): свежая запись без видео → url из истории принтера → скачали → привязали.
    Повторный тик раньше периода принтер не дёргает."""
    jid = await tmp_db.add_print_journal(label="plate.gcode", plastic_id=None, plastic_name="",
                                         grams=1, minutes=1, graded=0)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()             # изоляция: jid'ы сбрасываются с tmp_db у каждого теста
    _printer_ready(monkeypatch)
    monkeypatch.setattr(printwatch.elegoo, "job_history", lambda timeout=8.0, ep=None: {"ok": True, "jobs": [
        {"name": "plate.gcode", "time_lapse_video_url": "video/plate.mp4", "end_time": 100},
        {"name": "другая.gcode", "time_lapse_video_url": "video/other.mp4", "end_time": 90},
    ]})
    fetched: list[tuple] = []

    async def _fetch(job_name, url, ep=None, printer_id=None):
        fetched.append((job_name, url))
        p = tmp_path / "auto.mp4"
        p.write_bytes(b"v" * 10)
        return p
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _fetch)

    await printwatch._tick_timelapse_sweep()
    assert fetched == [("plate.gcode", "video/plate.mp4")], "качаем только нужное задание"
    row = await tmp_db.get_print_journal(jid)
    assert row["timelapse"] and row["timelapse"].endswith("auto.mp4")

    def _boom(timeout=8.0, ep=None):
        raise AssertionError("принтер дёрнут раньше периода свипа")
    monkeypatch.setattr(printwatch.elegoo, "job_history", _boom)
    await printwatch._tick_timelapse_sweep()          # гейт по времени — тихий выход


@pytest.mark.asyncio
async def test_timelapse_sweep_generates_unbuilt_video(tmp_db, tmp_path, monkeypatch):
    """Кейс «журнал без таймлапса, хотя они есть» (4c6cd9e8): кадры у задания сняты, но mp4 НЕ
    собран (time_lapse_video_status=1) — файла не существует, скачивание молчало бы до таймаута.
    Свип просит принтер собрать ролик (generate_timelapse), качает возвращённый путь, привязывает
    и досылает клиенту. Задания со статусом 0/3 (кадров нет / сборка провалилась) не трогаем."""
    jid = await tmp_db.add_print_journal(label="unbuilt.gcode", plastic_id=None, plastic_name="",
                                         grams=1, minutes=1, graded=0)
    jid2 = await tmp_db.add_print_journal(label="nocap.gcode", plastic_id=None, plastic_name="",
                                          grams=1, minutes=1, graded=0)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()             # изоляция: jid'ы сбрасываются с tmp_db у каждого теста
    printwatch._tl_generating.clear()
    _printer_ready(monkeypatch)
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": {"print_status": {
                            "print_duration": 0, "remaining_time_sec": 0, "filename": ""}}})
    gen: list[str] = []

    def _hist(timeout=8.0, ep=None):
        # принтер отвечает на 1051 сразу, а РЕНДЕРИТ минуты: до команды статус 1 (не собран),
        # после — 2 (готов) и путь mp4 в истории (живое поведение 24.07)
        built = bool(gen)
        return {"ok": True, "jobs": [
            {"name": "unbuilt.gcode",
             "time_lapse_video_url": "video/unbuilt.mp4" if built else "picture/unbuilt",
             "end_time": 100, "time_lapse_video_status": 2 if built else 1},
            {"name": "nocap.gcode", "time_lapse_video_url": "picture/nocap",
             "end_time": 90, "time_lapse_video_status": 0},
        ]}
    monkeypatch.setattr(printwatch.elegoo, "job_history", _hist)

    def _gen(url, timeout=150.0, ep=None):
        gen.append(url)
        return {"ok": True, "url": "video/unbuilt.mp4"}
    monkeypatch.setattr(printwatch.elegoo, "generate_timelapse", _gen)
    fetched: list[tuple] = []

    async def _fetch(job_name, url, ep=None, printer_id=None):
        fetched.append((job_name, url))
        p = tmp_path / "gen.mp4"
        p.write_bytes(b"v" * 10)
        return p
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _fetch)
    delivered: list[list[int]] = []

    async def _deliver(entries):
        delivered.append([e["id"] for e in entries])
    monkeypatch.setattr(printmedia, "deliver_late_timelapses", _deliver)

    await printwatch._tick_timelapse_sweep()
    for _ in range(50):                           # сборка идёт фоновой задачей — даём ей дожить
        await asyncio.sleep(0.01)
        if delivered:
            break
    assert gen == ["picture/unbuilt"], "сборку просим ровно для несобранного ролика"
    assert fetched == [("unbuilt.gcode", "video/unbuilt.mp4")], "качаем путь из ответа сборки"
    row = await tmp_db.get_print_journal(jid)
    assert row["timelapse"] and row["timelapse"].endswith("gen.mp4")
    assert not (await tmp_db.get_print_journal(jid2))["timelapse"], "статус 0 — видео не существует"
    assert delivered == [[jid]], "собранное видео досылается клиенту"
    assert printwatch._tl_generating.get(0) is not True, "флаг сборки обязан сброситься"


@pytest.mark.asyncio
async def test_timelapse_sweep_autobuild_off_never_asks_render(tmp_db, monkeypatch):
    """TIMELAPSE_AUTOBUILD=0 (08.10.2026): рендер mp4 грузит принтер минутами — свип НЕ просит
    сборку (1051) и не качает несобранное; готовые ролики (статус 2) докачивает как раньше."""
    await tmp_db.add_print_journal(label="unbuilt.gcode", plastic_id=None, plastic_name="",
                                   grams=1, minutes=1, graded=0)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()
    printwatch._tl_generating.clear()
    _printer_ready(monkeypatch)
    monkeypatch.setattr(config, "TIMELAPSE_AUTOBUILD", False)
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": {"print_status": {
                            "print_duration": 0, "remaining_time_sec": 0, "filename": ""}}})
    monkeypatch.setattr(printwatch.elegoo, "job_history", lambda timeout=8.0, ep=None: {
        "ok": True, "jobs": [{"name": "unbuilt.gcode", "time_lapse_video_url": "picture/unbuilt",
                              "end_time": 100, "time_lapse_video_status": 1}]})
    gen: list[str] = []
    monkeypatch.setattr(printwatch.elegoo, "generate_timelapse",
                        lambda url, timeout=150.0, ep=None: gen.append(url) or {"ok": True})
    fetched: list[str] = []

    async def _fetch(job_name, url, ep=None, printer_id=None):
        fetched.append(url)
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _fetch)
    await printwatch._tick_timelapse_sweep()
    await asyncio.sleep(0.05)
    assert gen == [] and fetched == []
    assert printwatch._tl_generating.get(0) is not True


@pytest.mark.asyncio
async def test_timelapse_sweep_skips_printer_while_printing(tmp_db, monkeypatch):
    """Во время печати свип НЕ ходит к принтеру по сети: скачивание таймлапса — большой HTTP-файл
    с того же встроенного сервера принтера, что отдаёт MJPEG-камеру, и во время печати оно роняет
    камеру (ConnectionReset-шторм, кейс 22.07 на осьминоге). Ни job_history, ни докачки — отложим
    до простоя (видео уже на принтере). Из локального кэша привязка при этом идёт (ФС, не принтер)."""
    jid = await tmp_db.add_print_journal(label="plate.gcode", plastic_id=None, plastic_name="",
                                         grams=1, minutes=1, graded=0)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()             # изоляция: jid'ы сбрасываются с tmp_db у каждого теста
    _printer_ready(monkeypatch)
    # ВАЖНО: снимок в форме ПРОШИВКИ. Раньше тут лежало {"print_stats": {"state": "printing"}} —
    # ключ, которого принтер не присылает вовсе (его достраивает elegoo.enrich_status). Тест
    # проходил против выдуманной формы и потому не замечал, что в бою гард мёртв: свип ходил к
    # печатающему принтеру каждые 10 минут и ронял камеру — ровно тот кейс 22.07, от которого
    # гард и написан. Разбор 11.08.
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": {"print_status": {
                            "print_duration": 600, "remaining_time_sec": 1200,
                            "filename": "plate.gcode"}}})

    def _boom(*a, **k):
        raise AssertionError("принтер дёрнут во время печати — пострадает камера")
    monkeypatch.setattr(printwatch.elegoo, "job_history", _boom)
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _boom)

    await printwatch._tick_timelapse_sweep()          # не должно бросить и не должно дёрнуть принтер
    assert not (await tmp_db.get_print_journal(jid))["timelapse"]


@pytest.mark.asyncio
async def test_timelapse_sweep_backs_off_after_failed_download(tmp_db, monkeypatch):
    """Принтер отказал в скачивании (RemoteDisconnected — встроенный HTTP-сервер слаб/занят): свип
    остывает 2 ч по этой записи и НЕ долбит принтер повторной докачкой каждые 10 мин (иначе мешает
    камере — кейс 22.07). После остывания — снова пробует."""
    jid = await tmp_db.add_print_journal(label="plate.gcode", plastic_id=None, plastic_name="",
                                         grams=1, minutes=1, graded=0)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()
    _printer_ready(monkeypatch)
    monkeypatch.setattr(printwatch.elegoo, "cached_status",
                        lambda *a, **k: {"ok": True, "state": {"print_status": {
                            "print_duration": 0, "remaining_time_sec": 0, "filename": ""}}})
    monkeypatch.setattr(printwatch.elegoo, "job_history", lambda timeout=8.0, ep=None: {"ok": True, "jobs": [
        {"name": "plate.gcode", "time_lapse_video_url": "video/plate.mp4", "end_time": 100}]})
    calls: list[str] = []

    async def _fail(job_name, url, ep=None, printer_id=None):
        calls.append(job_name)
        return None                              # принтер отказал в скачивании
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _fail)

    await printwatch._tick_timelapse_sweep()
    assert calls == ["plate.gcode"] and printwatch._tl_fail_until.get(jid, 0) > 0

    printwatch._tl_sweep_at.clear()                # снимаем гейт по времени свипа

    def _boom(*a, **k):
        raise AssertionError("докачка не должна вызываться в период остывания")
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _boom)
    await printwatch._tick_timelapse_sweep()     # запись отфильтрована по остыванию — принтер не тронут
    assert calls == ["plate.gcode"]              # новых попыток нет


@pytest.mark.asyncio
async def test_timelapse_sweep_local_cache_and_stale(tmp_db, tmp_path, monkeypatch):
    """Видео уже в авто-кэше → привязка БЕЗ обращения к принтеру; записи старше lookback
    свип не трогает (истории на принтере уже нет)."""
    import time as _t
    old = await tmp_db.add_print_journal(label="old.gcode", plastic_id=None, plastic_name="",
                                         grams=1, minutes=1, graded=0)
    await tmp_db._conn().execute(
        "UPDATE print_journal SET created_at = ? WHERE id = ?",
        (int(_t.time() - (printwatch.TIMELAPSE_LOOKBACK_H + 12) * 3600), old))
    await tmp_db._conn().commit()
    fresh = await tmp_db.add_print_journal(label="cached.gcode", plastic_id=None, plastic_name="",
                                           grams=1, minutes=1, graded=0)
    monkeypatch.setattr(printmedia, "_TIMELAPSE_AUTO_DIR", tmp_path)
    # авто-кэш назван КАНОНИЧНЫМ именем (elegoo.normalize_job_name: без .gcode, lower) —
    # запись/чтение кэша идут через один ключ, см. webapp._safe_job_name
    (tmp_path / "cached.mp4").write_bytes(b"v" * 5)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()             # изоляция: jid'ы сбрасываются с tmp_db у каждого теста
    _printer_ready(monkeypatch)
    called: list[int] = []
    monkeypatch.setattr(printwatch.elegoo, "job_history",
                        lambda timeout=8.0, ep=None: called.append(1) or {"ok": True, "jobs": []})

    await printwatch._tick_timelapse_sweep()
    assert (await tmp_db.get_print_journal(fresh))["timelapse"].endswith("cached.mp4")
    assert not (await tmp_db.get_print_journal(old))["timelapse"], "старьё не трогаем"
    assert not called, "кандидатов не осталось — история принтера не запрашивается"


@pytest.mark.asyncio
async def test_timelapse_sweep_matches_normalized_name(tmp_db, tmp_path, monkeypatch):
    """Корень «Овечки Тимми»: label записи журнала несёт «.gcode», а task_name истории принтера
    может прийти без расширения, в другом регистре и с двойным пробелом. Точное равенство их не
    сматчило бы — нормализация (elegoo.normalize_job_name) матчит."""
    jid = await tmp_db.add_print_journal(
        label="ECC2_0.4_Body_Elegoo PLA Matte _0.12_3h49m.gcode",
        plastic_id=None, plastic_name="", grams=1, minutes=1, graded=0)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()             # изоляция: jid'ы сбрасываются с tmp_db у каждого теста
    # изоляция от БОЕВОГО авто-кэша: label — имя реальной печати, и когда живой свип скачает её
    # видео, cached_timelapse_path нашёл бы файл в data/ и тест бы флакал (поймано 24.07 на пуше)
    monkeypatch.setattr(printmedia, "_TIMELAPSE_AUTO_DIR", tmp_path / "auto")
    _printer_ready(monkeypatch)
    # без .gcode, нижний регистр, двойной пробел — всё равно то же задание
    monkeypatch.setattr(printwatch.elegoo, "job_history", lambda timeout=8.0, ep=None: {"ok": True, "jobs": [
        {"name": "ecc2_0.4_body_elegoo  pla matte _0.12_3h49m",
         "time_lapse_video_url": "picture/whatever", "end_time": 100},
    ]})

    async def _fetch(job_name, url, ep=None, printer_id=None):
        p = tmp_path / "norm.mp4"
        p.write_bytes(b"v" * 10)
        return p
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _fetch)

    await printwatch._tick_timelapse_sweep()
    row = await tmp_db.get_print_journal(jid)
    assert row["timelapse"] and row["timelapse"].endswith("norm.mp4"), \
        "нормализованное имя (без .gcode/регистр/пробелы) должно сматчиться"


@pytest.mark.asyncio
async def test_late_timelapse_redelivered_once(tmp_db, tmp_path, monkeypatch):
    """Заказ уже «напечатано» (альбом ушёл клиенту ДО того, как принтер дорисовал видео), а свип
    привязал таймлапс позже — клиенту его дошлём в Telegram, и РОВНО ОДИН раз (идемпотентно)."""
    rid = await tmp_db.add_request(
        user_id=7, user_name="client", model_label="Овечка Тимми", printer="P",
        plastic_name="PLA", quantity=1, quality="Q", strength="S",
        grams_total=100, minutes_total=600, price=20, preview_path=None, plates=3)
    await tmp_db.update_request(rid, status="printed")
    await tmp_db.add_print_journal(label="plate.gcode", plastic_id=None, plastic_name="PLA",
                                   grams=20, minutes=120, graded=0, request_id=rid)
    printwatch._tl_sweep_at.clear()
    printwatch._tl_fail_until.clear()             # изоляция: jid'ы сбрасываются с tmp_db у каждого теста
    _printer_ready(monkeypatch)
    monkeypatch.setattr(printwatch.elegoo, "job_history", lambda timeout=8.0, ep=None: {"ok": True, "jobs": [
        {"name": "plate.gcode", "time_lapse_video_url": "video/plate.mp4", "end_time": 100}]})

    async def _fetch(job_name, url, ep=None, printer_id=None):
        p = tmp_path / "late.mp4"
        p.write_bytes(b"v" * 100)
        return p
    monkeypatch.setattr(printmedia, "cache_timelapse_from_url", _fetch)
    sent: list = []

    class _Bot:
        async def send_video(self, chat_id, video, caption=None):
            sent.append(chat_id)

        async def send_media_group(self, chat_id, media):
            sent.append(chat_id)
    bot = _Bot()                           # доставка держит СВОЙ _bot в printmedia
    stub_bot(monkeypatch, bot)
    monkeypatch.setattr(printmedia, "_bot", bot)

    await printwatch._tick_timelapse_sweep()
    assert sent == [7], "клиенту дослан таймлапс уже напечатанного заказа"

    printwatch._tl_sweep_at.clear()            # запись уже с видео → второй свип не пересылает
    await printwatch._tick_timelapse_sweep()
    assert sent == [7], "повторной пересылки нет — идемпотентно"


# ── несколько печатей на заказ: медиа заказа = агрегат журнальных (таймлапс+скрин per-печать) ──

async def _order(db) -> int:
    return await db.add_request(
        user_id=7, user_name="client", model_label="Пятистольный", printer="P",
        plastic_name="PLA", quantity=1, quality="Q", strength="S",
        grams_total=100, minutes_total=600, price=20, preview_path=None, plates=5)


async def _journal_print(db, rid, tmp_path, n, *, tl_bytes=b"", photo=False, fail_pct=0.0) -> int:
    jid = await db.add_print_journal(label=f"plate_{n}.gcode", plastic_id=None, plastic_name="PLA",
                                     grams=20, minutes=120, graded=1, request_id=rid,
                                     fail_pct=fail_pct)
    if tl_bytes:
        p = tmp_path / f"tl_{jid}.mp4"
        p.write_bytes(tl_bytes)
        await db.set_print_journal_fields(jid, timelapse=str(p))
    if photo:
        p = tmp_path / f"ph_{jid}.jpg"
        p.write_bytes(_jpeg())
        await db.set_print_journal_fields(jid, done_photo=str(p))
    return jid


@pytest.mark.asyncio
async def test_order_media_aggregates_all_prints(tmp_db, tmp_path):
    """Клиент видит медиа ВСЕХ печатей заказа; полный брак скрыт от клиента, но виден мастеру."""
    rid = await _order(tmp_db)
    await _journal_print(tmp_db, rid, tmp_path, 1, tl_bytes=b"a" * 100, photo=True)
    await _journal_print(tmp_db, rid, tmp_path, 2, tl_bytes=b"b" * 200, photo=True)
    await _journal_print(tmp_db, rid, tmp_path, 3, tl_bytes=b"c" * 300, fail_pct=100)

    items, sizes = await webapp._order_prints_media(rid)
    assert [i["label"] for i in items] == ["plate_1.gcode", "plate_2.gcode"]
    assert all(i["timelapse"] for i in items), "у каждой печати — свой подписанный URL таймлапса"
    assert [bool(i["photo"]) for i in items] == [True, True]
    assert sizes == {100, 200}, "размеры нужны для дедупа с одиночным requests.timelapse"

    admin_items, _ = await webapp._order_prints_media(rid, for_admin=True)
    assert len(admin_items) == 3, "мастеру показываем и полностью бракованную печать"


@pytest.mark.asyncio
async def test_deliver_all_timelapses_as_album(tmp_db, tmp_path, monkeypatch):
    """Финиш многостольного заказа: клиенту уходят таймлапсы всех печатей одним альбомом,
    журнальные видео шлются и без связи с принтером, копии одного видео не дублируются."""
    rid = await _order(tmp_db)
    await _journal_print(tmp_db, rid, tmp_path, 1, tl_bytes=b"a" * 100)
    await _journal_print(tmp_db, rid, tmp_path, 2, tl_bytes=b"b" * 200)
    await _journal_print(tmp_db, rid, tmp_path, 3, tl_bytes=b"x" * 100)   # копия первой (размер)
    await _journal_print(tmp_db, rid, tmp_path, 4, tl_bytes=b"f" * 300, fail_pct=100)

    printerdrv.use_driver(printerdrv.NoneDriver())     # пульта нет → в принтер не лезем
    for _m in (printmedia, webapp):        # каталог у printmedia, webapp читает через фасад
        monkeypatch.setattr(_m, "_TIMELAPSE_DIR", tmp_path / "tl_order")
    sent = {"groups": [], "videos": []}

    class _Bot:
        async def send_video(self, chat_id, video, caption=None):
            sent["videos"].append((chat_id, caption))

        async def send_media_group(self, chat_id, media):
            sent["groups"].append((chat_id, media))
    bot = _Bot()                           # доставка держит СВОЙ _bot в printmedia
    stub_bot(monkeypatch, bot)
    monkeypatch.setattr(printmedia, "_bot", bot)

    r = await tmp_db.get_request(rid)
    assert await webapp._attach_and_deliver_timelapse(r) is True

    assert len(sent["groups"]) == 1, "несколько видео — одним альбомом"
    assert len(sent["groups"][0][1]) == 2, "дубль по размеру и полный брак не отправляются"
    assert sent["groups"][0][0] == 7
    assert not sent["videos"]
    r = await tmp_db.get_request(rid)
    assert r.timelapse and (tmp_path / "tl_order" / f"{rid}.mp4").exists(), \
        "одиночное поле заказа заполнено копией для совместимости"


@pytest.mark.asyncio
async def test_deliver_single_timelapse_keeps_old_flow(tmp_db, tmp_path, monkeypatch):
    """Одна печать на заказ — прежнее поведение: обычное видео-сообщение, не альбом."""
    rid = await _order(tmp_db)
    await _journal_print(tmp_db, rid, tmp_path, 1, tl_bytes=b"a" * 100)

    printerdrv.use_driver(printerdrv.NoneDriver())     # пульта нет → в принтер не лезем
    for _m in (printmedia, webapp):        # каталог у printmedia, webapp читает через фасад
        monkeypatch.setattr(_m, "_TIMELAPSE_DIR", tmp_path / "tl_order")
    sent = {"groups": [], "videos": []}

    class _Bot:
        async def send_video(self, chat_id, video, caption=None):
            sent["videos"].append((chat_id, caption))

        async def send_media_group(self, chat_id, media):
            sent["groups"].append((chat_id, media))
    bot = _Bot()                           # доставка держит СВОЙ _bot в printmedia
    stub_bot(monkeypatch, bot)
    monkeypatch.setattr(printmedia, "_bot", bot)

    r = await tmp_db.get_request(rid)
    assert await webapp._attach_and_deliver_timelapse(r) is True
    assert len(sent["videos"]) == 1 and not sent["groups"]
    assert "Таймлапс печати" in sent["videos"][0][1]
