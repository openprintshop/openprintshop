"""Выбор слайсера: ElegooSlicer / Bambu Studio / Orca Slicer.

Три движка — одна семья (Orca форк BambuStudio, ElegooSlicer форк Orca), поэтому CLI у них общий
по происхождению, и сборка аргументов одна на всех. Проверяется здесь не «правильные ли флаги» —
это выясняется запуском у железа, — а то, чем смена движка опасна ТИХО:

* кэш расчёта не знал, каким слайсером считали, — сменил движок и получил старые cm³/минуты как
  свои. Деньги клиента, ошибки в логе нет;
* санитайзер bambu-3mf, оставленный включённым при родном движке, подменяет авторские настройки
  проекта шаблоном Centauri Carbon 2 — необратимо, файл правится на месте и без бэкапа;
* неизвестное имя движка (сборка покупателя из будущей версии) не должно ронять расчёт.
"""
from pathlib import Path

import pytest

from app import calcservice, slicers


class _Card:
    def __init__(self, slicer="", slicer_path=""):
        self.id, self.name = 1, "тестовый"
        self.slicer, self.slicer_path = slicer, slicer_path


# ─── реестр ──────────────────────────────────────────────────────────────────────────────

def test_three_engines_are_offered_and_elegoo_is_the_default():
    kinds = {b["kind"] for b in slicers.describe()}
    assert kinds == {"elegoo", "bambu", "orca"}
    assert slicers.DEFAULT_KIND == "elegoo"
    assert [b["kind"] for b in slicers.describe() if b["default"]] == ["elegoo"]


def test_unknown_engine_degrades_instead_of_crashing():
    """Имя движка из будущей версии не должно ронять расчёт: у покупателя сборка может приехать
    с настройкой, которой эта версия не знает."""
    assert slicers.for_kind("prusaslicer").kind == "elegoo"
    assert slicers.for_kind("").kind == "elegoo"
    assert slicers.for_kind(None).kind == "elegoo"


def test_card_chooses_the_engine_and_the_path():
    """Движок и путь к нему — свойство КАРТОЧКИ: у Bambu свои профили, и резать их
    ElegooSlicer'ом значит считать клиенту цену по чужой машине."""
    assert slicers.for_printer(_Card()).kind == "elegoo"          # пусто = как было
    assert slicers.for_printer(_Card("orca")).kind == "orca"
    assert slicers.for_printer(None).kind == "elegoo"
    exe = r"D:\Tools\OrcaSlicer\orca-slicer.exe"
    assert slicers.path_for_printer(_Card("orca", exe)) == exe


def test_env_path_belongs_to_the_default_engine_only(monkeypatch):
    """SLICER_PATH из .env — путь к ДВИЖКУ ПО УМОЛЧАНИЮ, а не «к слайсеру вообще».

    Иначе выбор Orca молча запускал бы ElegooSlicer по старому пути: аргументы приняты, gcode
    получен, цена посчитана — и всё это не тем движком."""
    from app import config

    monkeypatch.setattr(config, "SLICER_PATH", Path(r"C:\X\elegoo-slicer.exe"))
    assert slicers.for_kind("elegoo").path() == r"C:\X\elegoo-slicer.exe"
    assert slicers.for_kind("orca").path() != r"C:\X\elegoo-slicer.exe"


# ─── аргументы CLI ───────────────────────────────────────────────────────────────────────

def test_slice_args_shape_is_the_family_contract():
    """Форма командной строки семейства Orca: профили склеены через `;` в ОДНОМ аргументе.

    Это единственное место, где argv слайсера вообще зафиксирован тестом: до сих пор любая
    правка сборки аргументов уезжала в прод непроверенной (сквозной слайс — opt-in по сети)."""
    be = slicers.for_kind("elegoo")
    args = be.slice_args("S.exe", settings=["m.json", "p.json"], filament="f.json",
                         arrange="1", outdir="OUT", inputs=["a.3mf", "b.stl"])
    assert args == ["S.exe", "--allow-newer-file",
                    "--load-settings", "m.json;p.json",
                    "--load-filaments", "f.json",
                    "--slice", "0", "--arrange", "1",
                    "--outputdir", "OUT", "a.3mf", "b.stl"]


def test_native_slice_never_rearranges_the_project():
    """Проект-3mf режется с АВТОРСКОЙ раскладкой: `--arrange` не передаётся вовсе.

    Передай его — слайсер переразложит плиты автора, и cm³/время перестанут соответствовать
    тому, что напечатается."""
    args = slicers.for_kind("orca").slice_args(
        "S.exe", settings=["m.json"], arrange=None, outdir="OUT", inputs=["proj.3mf"])
    assert "--arrange" not in args
    assert "--load-filaments" not in args        # филамент проекта — свой
    assert args[-2:] == ["OUT", "proj.3mf"]


# ─── деньги: кэш обязан знать движок ─────────────────────────────────────────────────────

def test_cache_key_depends_on_the_engine(monkeypatch, tmp_path):
    """САМОЕ ДОРОГОЕ. Ключ кэша обязан различать движки.

    Кэш хранит cm³ и минуты по хэшу «модель + параметры». Не знай он о движке — смена слайсера
    вернула бы клиенту старую цену как свою, без единой строчки в логе."""
    from app import cache

    model = tmp_path / "m.stl"
    model.write_bytes(b"solid x\nendsolid x\n")
    seen = []

    def spy(paths, **kw):
        seen.append(kw.get("extra", ""))
        return "k" + str(len(seen))

    monkeypatch.setattr(cache, "make_key", spy)
    # вызываем ту же сборку ключа, что и slice_unit: важен факт присутствия движка в extra
    for kind in ("elegoo", "orca"):
        be = slicers.for_kind(kind)
        cache.make_key([model], machine_profile="m", process="p", layer_height=0.2,
                       infill=15, walls=2, filament="f", extra=f"logic:9;slicer:{be.kind}")
    assert seen == ["logic:9;slicer:elegoo", "logic:9;slicer:orca"]
    assert seen[0] != seen[1], "два движка дали бы один ключ — цена уехала бы молча"


def test_slice_unit_puts_the_engine_into_the_cache_key():
    """Барьер к предыдущему: строка ключа собирается ИМЕННО в slice_unit, а не в тесте."""
    src = Path("app/slicer.py").read_text(encoding="utf-8")
    assert src.count("slicer:{be.kind}") == 2, (
        "ключ кэша должен нести движок на ОБОИХ путях — нативном и с пресетами")


# ─── санитайзер bambu-3mf ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,wants", [("elegoo", True), ("bambu", False), ("orca", False)])
def test_sanitizer_is_only_for_elegoo(kind, wants):
    """Для ElegooSlicer санитайзер — лекарство (иначе бамбу-проект не режется вовсе), для
    родного движка — порча авторских настроек."""
    assert slicers.for_kind(kind).wants_bambu_sanitizer is wants


async def test_sanitizer_skipped_when_the_native_engine_slices(monkeypatch, tmp_path):
    """И это соблюдается на деле: с Bambu Studio файл не трогается ВООБЩЕ.

    Правка идёт на месте и без бэкапа (владелец отказался от .bambu.bak) — восстановить
    авторский проект было бы нечем."""
    touched = []
    monkeypatch.setattr(calcservice.bambu3mf, "is_bambu_project", lambda p: True)
    monkeypatch.setattr(calcservice.bambu3mf, "sanitize",
                        lambda p, **kw: touched.append(p))

    async def _resolver():
        return lambda m: ""
    monkeypatch.setattr(calcservice, "_bambu_filament_resolver", _resolver)

    f = tmp_path / "proj.3mf"
    f.write_bytes(b"PK")
    await calcservice._sanitize_bambu_models([f], enabled=True, printer=_Card("bambu"))
    assert touched == [], "авторский проект Bambu испорчен шаблоном Centauri Carbon 2"

    await calcservice._sanitize_bambu_models([f], enabled=True, printer=_Card("elegoo"))
    assert touched == [f], "для ElegooSlicer санитайзер обязан работать как раньше"


# ─── проверка движка до первого заказа ────────────────────────────────────────────────────
# Способ подсмотрен у самого движка (снято живьём с ElegooSlicer 1.5.x): подаём наши ключи БЕЗ
# значений, и он отвечает за доли секунды, до всякой работы —
#   незнакомое имя  → «Invalid option --что-то»
#   имя знакомо     → «Invalid value for option --slice»
# Второе и есть доказательство поддержки: движок дошёл до разбора значения.


async def test_check_reports_a_missing_engine_plainly():
    """Не установлен — внятная причина, а не «ok: False» без объяснения."""
    res = await slicers.check("orca", r"D:\нет\такого\orca-slicer.exe")
    assert res["ok"] is False and res["installed"] is False
    assert "не найден" in res["error"]
    assert res["kind"] == "orca" and res["title"] == "Orca Slicer"


async def test_check_believes_the_engine_that_parsed_our_flags(tmp_path, monkeypatch):
    """«Invalid value for option» = ключ ПОДДЕРЖИВАЕТСЯ: движок дошёл до разбора значения."""
    exe = tmp_path / "orca-slicer.exe"
    exe.write_bytes(b"")

    async def _probe(_exe, _args, _timeout):
        return -2, "\n".join(["[trace] Initializing StaticPrintConfigs",
                              "Invalid value for option --slice"])
    monkeypatch.setattr(slicers, "_probe", _probe)

    res = await slicers.check("orca", str(exe))
    assert res["ok"] is True and res["flags_checked"] is True
    assert res["missing_flags"] == [] and res["error"] == ""


async def test_check_catches_an_engine_that_does_not_know_our_flags(tmp_path, monkeypatch):
    """САМОЕ ЦЕННОЕ. Флаги Bambu Studio и Orca взяты от общего предка и живьём не проверены.

    «Orca не понимает --arrange» обязано выясняться за секунду, а не ценой заказа: непринятый
    ключ расчёт НЕ роняет — calcservice молча отдаёт приблизительную оценку, и расхождение видно
    только по деньгам."""
    exe = tmp_path / "orca-slicer.exe"
    exe.write_bytes(b"")

    async def _probe(_exe, _args, _timeout):
        return -2, "\n".join(["Invalid option --arrange",
                              "Invalid option --load-filaments"])
    monkeypatch.setattr(slicers, "_probe", _probe)

    res = await slicers.check("orca", str(exe))
    assert res["ok"] is False
    assert res["missing_flags"] == ["--load-filaments", "--arrange"]
    assert "--arrange" in res["error"] and "app/slicers.py" in res["error"]


async def test_check_admits_when_it_could_not_verify(tmp_path, monkeypatch):
    """Движок запустился, но про аргументы промолчал — сознаёмся, а не выдаём «всё хорошо».

    Иначе проверка стала бы штампом: зелёная галочка при непроверенном контракте хуже её
    отсутствия, потому что на неё будут ссылаться."""
    exe = tmp_path / "s.exe"
    exe.write_bytes(b"")

    async def _probe(_exe, _args, _timeout):
        return 0, "готов к работе"
    monkeypatch.setattr(slicers, "_probe", _probe)

    res = await slicers.check("orca", str(exe))
    assert res["ok"] is True and res["flags_checked"] is False
    assert "ничего не ответил" in res["error"]


async def test_check_survives_a_gui_that_never_answers(tmp_path, monkeypatch):
    """Открылось окно вместо CLI — отвал по таймауту с внятной причиной, а не зависание."""
    exe = tmp_path / "s.exe"
    exe.write_bytes(b"")

    async def _probe(_exe, _args, _timeout):
        return None, ""
    monkeypatch.setattr(slicers, "_probe", _probe)

    res = await slicers.check("orca", str(exe), timeout=0.2)
    assert res["ok"] is False and "не ответил" in res["error"]


@pytest.mark.skipif(not slicers.for_kind("elegoo").installed(),
                    reason="ElegooSlicer не установлен — контракт CLI проверять нечем")
async def test_the_real_elegoo_slicer_confirms_our_flags():
    """Единственная проверка НАСТОЯЩИМ движком: боевой ElegooSlicer подтверждает наши ключи.

    Она же — образец того, чего не хватает Bambu Studio и Orca: их флаги взяты от общего предка,
    и до установки этот тест по ним не пройдёт (он просто скипнется)."""
    res = await slicers.check("elegoo")
    assert res["installed"] is True
    assert res["missing_flags"] == [], res["error"]
    assert res["flags_checked"] is True, "контракт CLI не подтверждён — сверьте app/slicers.py"
    assert res["ok"] is True


# ─── сводка в gcode: у каждого движка своя ────────────────────────────────────────────────
# Строки ниже сняты с НАСТОЯЩЕГО вывода движков 12.08.2026 (кубик 13 мм). Это не выдумка о
# формате: до установки Bambu Studio наш разборщик его gcode не понимал вовсе, и слайс молча
# подменялся приблизительной оценкой.

_ELEGOO_TAIL = """
; filament used [mm] = 419.24
; filament used [cm3] = 1.24
; estimated printing time (normal mode) = 4m 48s
"""

_BAMBU_TAIL = """
; model printing time: 10m 14s; total estimated time: 10m 14s
; total layer number: 65
; total filament length [mm] : 419.24
; total filament volume [cm^3] : 1008.40
; total filament weight [g] : 0.00
"""


def _parse(text: str, kind: str, tmp_path):
    from app import slicer

    d = tmp_path / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / "plate_1.gcode").write_text(text, encoding="utf-8")
    return slicer._parse_gcode_dir(d, slicers.for_kind(kind))


def test_elegoo_summary_is_read_as_before(tmp_path):
    vol, minutes = _parse(_ELEGOO_TAIL, "elegoo", tmp_path)
    assert vol == 1.24
    assert abs(minutes - 4.8) < 0.01


def test_orca_reads_the_same_summary_as_elegoo(tmp_path):
    """Orca — предок ElegooSlicer, сводку пишет так же. Проверено настоящим слайсом."""
    vol, minutes = _parse(_ELEGOO_TAIL, "orca", tmp_path)
    assert vol == 1.24 and abs(minutes - 4.8) < 0.01


def test_bambu_summary_needs_its_own_names_and_its_own_UNIT(tmp_path):
    """САМОЕ ДОРОГОЕ ОТКРЫТИЕ ПРОВЕРКИ. У Bambu Studio и другие подписи, и другая ЕДИНИЦА.

    Поле подписано «[cm^3]», а содержит МИЛЛИМЕТРЫ кубические: длина 419.24 мм при прутке
    1.75 мм (сечение 2.4053 мм²) даёт ровно 1008.4 — напечатанное в поле число. Принять его за
    см³ значит завысить расход и цену клиенту в ТЫСЯЧУ раз."""
    vol, minutes = _parse(_BAMBU_TAIL, "bambu", tmp_path)
    assert abs(vol - 1.0084) < 1e-6, "объём кубика 13 мм не может быть 1008 см³"
    assert abs(minutes - (10 + 14 / 60)) < 0.01

    # а сводку Elegoo движок Bambu не поймёт — и это правильно: подписи у них разные
    import pytest as _p

    from app import slicer
    with _p.raises(slicer.SlicerError):
        _parse(_ELEGOO_TAIL, "bambu", tmp_path / "cross")


def test_per_colour_volumes_follow_the_same_unit(tmp_path):
    """Разбивка склада по цветам обязана считать в тех же единицах, что и общий объём — иначе
    списание филамента разъедется с ценой ровно в тысячу раз."""
    from app import slicer

    d = tmp_path / "multi"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plate_1.gcode").write_text(
        "; total filament volume [cm^3] : 1008.40, 500.00\n", encoding="utf-8")
    vals = slicer._parse_filament_cm3_list(d, slicers.for_kind("bambu"))
    assert [round(v, 4) for v in vals] == [1.0084, 0.5]
