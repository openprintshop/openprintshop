"""Допечатать без сбойной детали — Путь A (app/reprint.py, docs/.../exclude-object.md).

Что здесь проверяется и почему именно это:
  * детали набора СОБИРАЮТСЯ (иначе выбирать было бы нечего) — на синтетическом .3mf, без
    единого запуска слайсера;
  * переслайс без детали отдаёт задание с МЕНЬШИМ числом деталей, и в слайсер уезжает именно
    остаток (выкинуть не ту деталь — главный способ этой фичи навредить);
  * КАЖДАЯ причина недоступности отрабатывает отдельно: нет привязки к заказу, нет файла
    модели, нет снимка настроек, мультиплейт, деталь одна, драйвер не умеет upload/start/control
    и принимает не тот формат. «Не додумывать за мастера» — это и есть вот эти семь отказов;
  * ручка отвечает 403 не-админу;
  * событие попадает в ленту активности, а имя задания в привязке переставляется на новое
    (иначе финиш печати и таймлапс сойдутся на СТАРОМ имени, то есть не сойдутся).

Слайсер здесь НЕ ЗАПУСКАЕТСЯ: `slice_to_gcode` подменяется модулем (как `slice_unit` в
tests/test_fast_only.py), а `slice_unit` подменяется взрывом — производящий путь обязан идти
мимо измерителя с его кэшем. Реальный принтер не трогается вовсе: драйвер — printerdrv.fake.
"""

import time
import zipfile

import pytest

from app import config, printerdrv, reprint, webapp
from app.printerdrv import Caps
from app.web import printerhub, printerjob
from tests.conftest import admin_req, body, get_req, stub_auth

pytestmark = pytest.mark.asyncio

_CUBE_V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
           (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_CUBE_T = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
           (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _cube_xml(side: float) -> str:
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % (x * side / 10, y * side / 10, z * side / 10)
                for x, y, z in _CUBE_V)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tr for tr in _CUBE_T)
    return f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>"


def _set_3mf(path, sides=(10, 20)):
    """Набор из нескольких кубов на ОДНОМ столе — «пять брелоков», ради которых всё затевалось."""
    objs = "".join(f'<object id="{i + 1}" type="model">{_cube_xml(s)}</object>'
                   for i, s in enumerate(sides))
    items = "".join(f'<item objectid="{i + 1}" transform="1 0 0 0 1 0 0 0 1 {i * 60} 0 0"/>'
                    for i in range(len(sides)))
    core = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            f'<resources>{objs}</resources><build>{items}</build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", core)
    return path


async def _bench(tmp_db, tmp_path, *, sides=(10, 20), plates=1, bind=True, snapshot=True,
                 file_ok=True):
    """Стенд: заказ с моделью-набором, привязка печати к нему и снимок настроек задания."""
    reprint._PENDING.clear()        # залитое-но-не-запущенное живёт в памяти процесса
    model = _set_3mf(tmp_path / "set.3mf", sides)
    printer = await tmp_db.get_default_printer()
    plastic = (await tmp_db.list_plastics(active_only=True))[0]
    quality = (await tmp_db.list_quality(active_only=True))[0]
    strength = (await tmp_db.list_strength(active_only=True))[0]
    rid = await tmp_db.add_request(
        user_id=7, user_name="u", model_label="Набор", printer=printer.name,
        plastic_name=plastic.name, quantity=1, quality=quality.label, strength=strength.label,
        grams_total=20, minutes_total=60, price=100, preview_path=None, plates=plates,
        file_path=str(model) if file_ok else "", quality_id=quality.id, strength_id=strength.id,
        breakdown={"scale_pct": 100})
    if bind:
        await tmp_db.set_print_binding(rid, "plate_1.gcode", None)
    if snapshot:
        await tmp_db.save_print_settings("plate_1.gcode", values={"Сопло": "0.4"},
                                         start={"plate": "textured"}, uploaded=True)
    return rid, model


def _full_drv(printing=True):
    """Драйвер-заглушка. По умолчанию ПЕЧАТАЕТ: допечатка гейтится живым статусом принтера
    (печать могла кончиться, пока мастер выбирал детали), и простаивающая машина её закрывает."""
    drv = printerdrv.FakePrinter(caps=Caps(status=True, files=True, upload=True, start=True,
                                           control=True, delete=True))
    if printing:
        drv.st.printing = True
        drv.st.phase = "printing"
        drv.st.job = "plate_1.gcode"
        drv.st.minutes_total, drv.st.minutes_done = 100.0, 30.0
    return drv


def _fake_slice(monkeypatch, seen: list):
    """Подмена ПРОИЗВОДЯЩЕГО слайса + запрет на измеритель: файл обязан рождаться мимо кэша.

    Записываем не только имена файлов, но и СУММАРНЫЙ ОБЪЁМ уехавшей в слайсер геометрии:
    `_placed_to_stls` склеивает остаток в один файл, поэтому число файлов не доказывает ничего —
    «выкинули не ту деталь» видно только по объёму."""
    import trimesh

    async def _slice_to_gcode(parts, out_dir, **kw):
        vol = 0.0
        for f in parts:
            try:
                vol += abs(float(trimesh.load(str(f), force="mesh").volume))
            except Exception:  # noqa: BLE001
                pass
        seen.append({"files": [str(p) for p in parts], "mm3": vol})
        out_dir.mkdir(parents=True, exist_ok=True)
        gp = out_dir / "plate_1.gcode"
        gp.write_text("; fake gcode\n", encoding="utf-8")
        return gp, 12.5, 90.0

    async def _boom(*a, **k):
        raise AssertionError("допечатка не должна ходить в slice_unit (там кэш и rmtree)")

    monkeypatch.setattr(reprint.slicer, "slice_to_gcode", _slice_to_gcode)
    monkeypatch.setattr(reprint.slicer, "slice_unit", _boom)


async def test_parts_of_a_set_are_collected(tmp_db, tmp_path):
    """Детали набора видны по-отдельности — без них выбирать сбойную нечем."""
    await _bench(tmp_db, tmp_path)
    res = await reprint.availability(None, drv=_full_drv())
    assert res["available"] is True, res.get("reason")
    names = [p["name"] for p in res["parts"]]
    assert names == ["part0", "part1"], names
    assert all(p["volume_cm3"] > 0 for p in res["parts"])


async def test_reprint_leaves_the_job_without_the_bad_part(tmp_db, tmp_path, monkeypatch):
    """Переслайс без детали: остаток меньше на одну, и в слайсер уезжает ИМЕННО остаток.

    Объём проверяется по факту: сводка kept/dropped считается из тех же индексов, что и фильтр,
    и «выкинули не ту деталь» она бы не поймала. Ловит — геометрия, уехавшая в слайсер."""
    seen: list = []
    _fake_slice(monkeypatch, seen)
    monkeypatch.setattr(config, "SLICE_DIR", tmp_path / "slices")
    rid, _ = await _bench(tmp_db, tmp_path, sides=(10, 20, 30))
    drv = _full_drv()
    parts = (await reprint.availability(None, drv=drv))["parts"]
    want_mm3 = sum(p["volume_cm3"] * 1000.0 for p in parts if p["index"] != 1)
    res = await reprint.reprint_without(drv, None, [1], uid=1)
    assert res["ok"] is True, res
    assert len(res["kept"]) == 2 and [p["name"] for p in res["dropped"]] == ["part1"]
    assert seen and seen[0]["files"], "слайс не получил ни одного файла остатка"
    assert abs(seen[0]["mm3"] - want_mm3) < 0.02 * want_mm3, \
        f"в слайсер уехало {seen[0]['mm3']:.0f} мм³ вместо {want_mm3:.0f} — выкинута не та деталь"
    assert res["grams"] > 0 and res["minutes"] > 0, "сводка обязана быть честной, а не пустой"
    # шаг 1 НЕ запускает печать: на столе лежит недопечатанное, старт — отдельное действие
    assert res["needs_start"] is True and res["stopped"] is True
    assert drv.st.printing is False, "печать остановлена, но не запущена заново"
    assert (await tmp_db.get_print_binding(None))["job_name"] == "plate_1.gcode"
    # журнал: что переслайсили, какие детали выкинуты
    ev = [e for e in await tmp_db.list_activity() if e["action"] == "допечатка без детали"]
    assert ev and f"#{rid}" in ev[0]["detail"] and "part1" in ev[0]["detail"], ev

    # шаг 2 — старт по отдельному подтверждению мастера у пустого стола
    st = await reprint.start_reprint(drv, None, res["filename"], uid=1)
    assert st["ok"] is True and drv.st.printing is True
    binding = await tmp_db.get_print_binding(None)
    assert binding["job_name"] == res["filename"] != "plate_1.gcode"
    assert binding["request_id"] == rid and not binding["finished"] and not binding["was_active"]


async def test_nothing_is_stopped_when_the_choice_is_bad(tmp_db, tmp_path, monkeypatch):
    """Отказ по выбору НЕ стоит мастеру печати: проверка идёт ДО остановки.

    Раньше стоп шёл первым, и «Список деталей изменился — откройте его заново» приходил уже
    поверх убитой печати."""
    seen: list = []
    _fake_slice(monkeypatch, seen)
    await _bench(tmp_db, tmp_path)
    drv = _full_drv()
    calls: list = []
    monkeypatch.setattr(drv, "control", lambda *a, **k: (calls.append(a), {"ok": True})[1])
    for drop, expect, word in (([], None, "ни одной"), ([0, 1], None, "все детали"),
                               ([9], None, "нет"), ([0], {0: "чужой-id"}, "изменил")):
        with pytest.raises(reprint.ReprintUnavailable, match=word):
            await reprint.reprint_without(drv, None, drop, uid=1, expect_ids=expect)
    assert not calls, "печать остановлена ради запроса, который всё равно отклонён"
    assert not seen, "режем по непроверенному выбору"


async def test_unavailable_when_the_print_is_over(tmp_db, tmp_path):
    """Печать кончилась, пока мастер выбирал детали, — допечатывать нечего.

    Наблюдатель ставит finished лишь на своём тике (POLL_SEC = 60), поэтому проверок две:
    флаг в БД и ЖИВОЙ статус принтера."""
    await _bench(tmp_db, tmp_path)
    idle = await reprint.availability(None, drv=_full_drv(printing=False))
    assert "не печатает" in idle["reason"], idle
    await tmp_db.update_print_binding(None, finished=1)
    assert "уже завершена" in (await reprint.availability(None, drv=_full_drv()))["reason"]


async def test_start_rebinds_even_if_the_binding_was_cleared(tmp_db, tmp_path, monkeypatch):
    """Наблюдатель успел закрыть прерванную печать и снять привязку — старт ставит её ЗАНОВО.

    Нарезка идёт дольше тика printwatch, и штатным ходом строка print_binding исчезает.
    UPDATE по ней обновил бы ноль строк, и новая печать осталась бы без заказа."""
    _fake_slice(monkeypatch, [])
    monkeypatch.setattr(config, "SLICE_DIR", tmp_path / "slices")
    rid, _ = await _bench(tmp_db, tmp_path)
    drv = _full_drv()
    res = await reprint.reprint_without(drv, None, [0], uid=1)
    assert res["ok"] is True, res
    await tmp_db.clear_print_binding(None)                  # это и делает printwatch на финише
    st = await reprint.start_reprint(drv, None, res["filename"], uid=1)
    assert st["ok"] is True
    binding = await tmp_db.get_print_binding(None)
    assert binding and binding["request_id"] == rid
    assert binding["job_name"] == res["filename"] and not binding["finished"]


async def test_start_without_a_pending_job_refuses(tmp_db, tmp_path):
    """Старт без залитого задания — отказ с внятной причиной, а не запуск чего попало."""
    await _bench(tmp_db, tmp_path)
    with pytest.raises(reprint.ReprintUnavailable, match="не найдено"):
        await reprint.start_reprint(_full_drv(), None, "o1_reprint.gcode", uid=1)


async def test_stop_failure_does_not_start_anything(tmp_db, tmp_path, monkeypatch):
    """Стоп не удался → дальше не идём: старт поверх идущей печати — худшее, что можно сделать."""
    seen: list = []
    _fake_slice(monkeypatch, seen)
    await _bench(tmp_db, tmp_path)
    drv = _full_drv()
    monkeypatch.setattr(drv, "control", lambda *a, **k: {"ok": False, "error": "нет кода команды"})
    res = await reprint.reprint_without(drv, None, [0], uid=1)
    assert res["ok"] is False and "нет кода команды" in res["error"]
    assert not res.get("stopped"), "стоп не удался — говорить «печать остановлена» нельзя"
    assert not seen, "печать не остановлена, а мы уже режем"


async def test_unavailable_says_why(tmp_db, tmp_path):
    """Каждая причина недоступности — своя, названная вслух. Никаких «попробуем угадать»."""
    drv = _full_drv()
    # 1. печать не привязана к заказу
    assert "не привязана" in (await reprint.availability(None, drv=drv))["reason"]
    # 2. файла модели нет на диске
    await _bench(tmp_db, tmp_path, file_ok=False)
    assert "модели" in (await reprint.availability(None, drv=drv))["reason"]


async def test_unavailable_without_snapshot(tmp_db, tmp_path):
    """Нет снимка настроек — чем резали то, что на столе, неизвестно."""
    await _bench(tmp_db, tmp_path, snapshot=False)
    assert "снимка" in (await reprint.availability(None, drv=_full_drv()))["reason"]


async def test_unavailable_for_multiplate(tmp_db, tmp_path):
    """Мультиплейт: какой стол сейчас печатается, нигде не сохранено."""
    await _bench(tmp_db, tmp_path, plates=3)
    assert "мультиплейт" in (await reprint.availability(None, drv=_full_drv()))["reason"].lower()


async def test_unavailable_for_single_part(tmp_db, tmp_path):
    """Одна деталь — допечатывать без неё нечего."""
    await _bench(tmp_db, tmp_path, sides=(15,))
    assert "одна деталь" in (await reprint.availability(None, drv=_full_drv()))["reason"]


@pytest.mark.parametrize("caps, word", [
    (Caps(status=True, upload=True, start=True), "останавливать"),
    (Caps(status=True, control=True, start=True), "принимать задания"),
    (Caps(status=True, control=True, upload=True), "запускать"),
])
async def test_unavailable_when_driver_cannot(tmp_db, tmp_path, caps, word):
    """Гейт по способностям драйвера — три штуки, и ни одна не подменяется догадкой."""
    await _bench(tmp_db, tmp_path)
    res = await reprint.availability(None, drv=printerdrv.FakePrinter(caps=caps))
    assert res["available"] is False and word in res["reason"], res


async def test_unavailable_when_printer_wants_another_format(tmp_db, tmp_path):
    """Bambu ждёт .3mf, а переслайс даёт .gcode — честный отказ, а не «зальём как есть»."""
    await _bench(tmp_db, tmp_path)
    drv = _full_drv()
    drv.upload_exts = (".3mf", ".gcode.3mf")
    res = await reprint.availability(None, drv=drv)
    assert res["available"] is False and ".gcode" in res["reason"]


async def test_selection_is_checked(tmp_db, tmp_path, monkeypatch):
    """Отметили все детали / чужую деталь / список устарел — режем только по проверенному выбору."""
    _fake_slice(monkeypatch, [])
    await _bench(tmp_db, tmp_path)
    ctx = await reprint.job_context(None, drv=_full_drv())
    for drop, expect, word in (([0, 1], None, "все детали"),
                               ([], None, "ни одной"),
                               ([5], None, "нет"),
                               ([0], {0: "чужой-id"}, "изменил")):
        with pytest.raises(reprint.ReprintUnavailable, match=word):
            await reprint.rebuild_without(ctx, drop, expect_ids=expect,
                                          out_dir=tmp_path / "out")


async def test_handles_reject_non_admin(tmp_db, tmp_path, monkeypatch):
    """Ручки допечатки — только админу: за ними стоп печати и запуск новой."""
    await _bench(tmp_db, tmp_path)
    stub_auth(monkeypatch, _verify_admin=None)
    assert (await webapp._api_admin_reprint_parts(get_req())).status == 403
    assert (await webapp._api_admin_reprint(admin_req({"drop": [0]}))).status == 403
    assert (await webapp._api_admin_reprint_start(
        admin_req({"filename": "x.gcode"}))).status == 403


async def test_parts_handle_answers_admin(tmp_db, tmp_path, monkeypatch):
    """Ручка списка деталей отдаёт то же, что ядро (гейты и имена деталей — из одного места)."""
    await _bench(tmp_db, tmp_path)
    stub_auth(monkeypatch, _verify_admin=1)
    drv = _full_drv()

    async def _drv(*a, **k):
        return drv
    for mod in (webapp, printerhub, printerjob):
        monkeypatch.setattr(mod, "_printer_driver", _drv)
    j = body(await webapp._api_admin_reprint_parts(get_req()))
    assert j["available"] is True and len(j["parts"]) == 2



# ───────────────────── гейты шага 2 (старт залитой допечатки) ─────────────────────

def _pend(printer_id=0, filename="o1_reprint.gcode"):
    """Залитое-но-не-запущенное задание в памяти процесса (результат шага 1)."""
    reprint._PENDING[int(printer_id)] = reprint.PendingStart(
        printer_id=printer_id, request_id=1, label="набор", filename=filename,
        start_kw={}, kept=2, dropped=1, minutes=30.0, grams=12.0, at=time.time())


async def test_start_refuses_while_printer_is_busy(tmp_db, tmp_path):
    """Карточка «задание залито» живёт часами. Если за это время на принтере началась ДРУГАЯ
    печать (руками с панели, другим заказом), старт допечатки встал бы поверх неё."""
    await _bench(tmp_db, tmp_path)
    _pend()
    drv = _full_drv(printing=True)
    with pytest.raises(reprint.ReprintUnavailable, match="печатает"):
        await reprint.start_reprint(drv, None, "o1_reprint.gcode", uid=1)
    starts = [c for c in drv.st.commands if c[0] == "start"]
    assert not starts, "чужая печать не должна была быть затоптана"


async def test_start_refuses_blank_filename(tmp_db, tmp_path):
    """Пустое имя — «запусти что-нибудь залитое»: вслепую печать не стартуем."""
    await _bench(tmp_db, tmp_path)
    _pend()
    drv = _full_drv(printing=False)
    with pytest.raises(reprint.ReprintUnavailable, match="Не указано"):
        await reprint.start_reprint(drv, None, "   ", uid=1)
    assert not [c for c in drv.st.commands if c[0] == "start"]
