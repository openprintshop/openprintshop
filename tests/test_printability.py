"""Печатопригодность (B2, DFM): вердикт + гейт одной детали + эвристика тонких стенок.

Ключевое требование B2: предупреждения про тонкие стенки/мелкоту срабатывают ТОЛЬКО для одной
детали (n_parts==1). Валидный набор из многих мелких деталей (n_parts>1) НЕ должен помечаться —
это и есть регрессия, из-за которой наивную версию откатывали (пугала клиентов на живом экране).
Заказ B2 никогда не блокирует: 'wont_fit'/'risky' — только advisory.
"""
from types import SimpleNamespace

from app import config, db, slicer, texts, webapp


# ─────────────────────────── габарит vs стол ───────────────────────────
def test_bed_wont_fit_single_part():
    """Одна деталь длиннее стола → 'wont_fit' + проверка bed со статусом bad."""
    v, dfm = texts.printability_verdict("300×40×40 мм", bed_mm=256.0, n_parts=1, max_part_mm=300.0)
    assert v == "wont_fit"
    bed = [d for d in dfm if d["key"] == "bed"]
    assert len(bed) == 1 and bed[0]["status"] == "bad"


def test_bed_no_warning_for_set():
    """РЕГРЕССИЯ-ГАРД: набор из 8 мелких деталей с большим общим футпринтом, но каждая деталь
    влезает (max_part_mm мал) → НЕ 'wont_fit', вердикт 'ok', проверок нет. Именно этот случай
    ломала наивная версия (сравнивала общий bbox со столом)."""
    v, dfm = texts.printability_verdict("300×300×40 мм", bed_mm=256.0, n_parts=8, max_part_mm=80.0,
                                        min_extent_mm=40.0, wall_proxy_mm=12.0)
    assert v == "ok"
    assert dfm == []


def test_bed_near_limit_is_risky():
    """Деталь почти на пределе стола (>92%) → мягкий 'risky', не запрет."""
    v, dfm = texts.printability_verdict("245×40×40 мм", bed_mm=256.0, n_parts=1, max_part_mm=245.0)
    assert v == "risky"
    assert dfm and dfm[0]["status"] == "warn"


def test_bed_overall_only_is_soft():
    """По-детального замера нет (n_parts=None, max_part_mm=0) — судим по строке dims, но мягко:
    даже при габарите больше стола максимум 'risky' (не знаем, одна деталь или набор)."""
    v, dfm = texts.printability_verdict("300×40×40 мм", bed_mm=256.0, n_parts=None)
    assert v == "risky"
    assert dfm and dfm[0]["key"] == "bed" and dfm[0]["status"] == "warn"


# ─────────────────────────── тонкие стенки (эвристика) ───────────────────────────
def test_thin_wall_single_part_risky():
    """Одна деталь-«оболочка»: прокси толщины ≈0.5 мм < порога 0.8 → 'risky' с мягкой проверкой wall."""
    v, dfm = texts.printability_verdict("60×60×2 мм", bed_mm=256.0, n_parts=1,
                                        max_part_mm=60.0, min_extent_mm=2.0,
                                        wall_proxy_mm=0.5, min_wall_mm=0.8)
    assert v == "risky"
    wall = [d for d in dfm if d["key"] == "wall"]
    assert len(wall) == 1 and wall[0]["status"] == "warn"   # никогда не 'bad' — это лишь оценка


def test_chunky_single_part_ok():
    """Одна массивная деталь: прокси толщины большой → без предупреждений, 'ok'."""
    v, dfm = texts.printability_verdict("60×60×60 мм", bed_mm=256.0, n_parts=1,
                                        max_part_mm=60.0, min_extent_mm=60.0,
                                        wall_proxy_mm=20.0, min_wall_mm=0.8)
    assert v == "ok"
    assert dfm == []


def test_thin_wall_gated_for_multipart():
    """ГЕЙТ: та же тонкая геометрия, но n_parts=8 (набор) → проверка стенок НЕ срабатывает."""
    v, dfm = texts.printability_verdict("60×60×2 мм", bed_mm=256.0, n_parts=8,
                                        max_part_mm=60.0, min_extent_mm=2.0,
                                        wall_proxy_mm=0.5, min_wall_mm=0.8)
    assert v == "ok"
    assert not any(d["key"] == "wall" for d in dfm)


def test_min_extent_triggers_thin_when_no_wall_proxy():
    """Прокси 2·V/A недоступен (0), но самый тонкий габарит детали <порога → всё равно 'risky'."""
    v, dfm = texts.printability_verdict("50×50×0.5 мм", bed_mm=256.0, n_parts=1,
                                        max_part_mm=50.0, min_extent_mm=0.5,
                                        wall_proxy_mm=0.0, min_wall_mm=0.8)
    assert v == "risky"
    assert any(d["key"] == "wall" for d in dfm)


# ─────────────────────────── уровни вердикта / крайние случаи ───────────────────────────
def test_verdict_levels():
    """oversize → wont_fit; thin → risky; normal → ok (три градации)."""
    assert texts.printability_verdict("300×10×10 мм", n_parts=1, max_part_mm=300.0)[0] == "wont_fit"
    assert texts.printability_verdict("60×60×2 мм", n_parts=1, max_part_mm=60.0,
                                      wall_proxy_mm=0.4)[0] == "risky"
    assert texts.printability_verdict("60×60×60 мм", n_parts=1, max_part_mm=60.0,
                                      wall_proxy_mm=15.0)[0] == "ok"


def test_wont_fit_dominates_thin():
    """Деталь и не влезает, и тонкая → худший уровень 'wont_fit', обе проверки в dfm."""
    v, dfm = texts.printability_verdict("300×300×1 мм", bed_mm=256.0, n_parts=1,
                                        max_part_mm=300.0, min_extent_mm=1.0, wall_proxy_mm=0.5)
    assert v == "wont_fit"
    keys = {d["key"] for d in dfm}
    assert "bed" in keys and "wall" in keys


def test_empty_dims_no_panic():
    """Нет габаритов (быстрый снимок до замера) → ('ok', []) без паники."""
    assert texts.printability_verdict("", bed_mm=256.0) == ("ok", [])
    assert texts.printability_verdict("", bed_mm=256.0, n_parts=1) == ("ok", [])


# ─────────────────────────── обратная совместимость строкового API ───────────────────────────
def test_printability_warnings_backward_compat():
    """Старый вызов (только dims[, bed]) работает как прежде — тест test_estimates.py опирается."""
    assert texts.printability_warnings("300×40×40 мм", 256.0)      # большая сторона > стола
    assert not texts.printability_warnings("60×40×40 мм", 256.0)   # влезает
    assert not texts.printability_warnings("", 256.0)              # нет габаритов — нет паники


def test_printability_warnings_gated_set():
    """Строковый API тоже уважает гейт: набор мелочи (n_parts>1) не даёт варнингов про стенки."""
    ws = texts.printability_warnings("60×60×2 мм", 256.0, n_parts=8, max_part_mm=60.0,
                                     wall_proxy_mm=0.4)
    assert ws == []


# ─────────────────────────── настройка порога стенки (dfm_min_wall_mm) ───────────────────────────
def test_dfm_min_wall_setting_default_present():
    """B2: дефолт порога печатопригодности присутствует в DEFAULT_SETTINGS и положителен."""
    assert "dfm_min_wall_mm" in db.DEFAULT_SETTINGS
    assert float(db.DEFAULT_SETTINGS["dfm_min_wall_mm"]) > 0
    # порог виден в админке (label) и в порядке настроек — не «сирота»
    assert "dfm_min_wall_mm" in db.SETTING_LABELS
    assert "dfm_min_wall_mm" in db.SETTINGS_ORDER


def test_dfm_min_wall_setting_is_used():
    """Порог реально влияет на вердикт: одна и та же геометрия ниже/выше порога даёт ok↔risky."""
    geom = dict(n_parts=1, max_part_mm=60.0, min_extent_mm=60.0, wall_proxy_mm=1.0)
    # стенка ≈1.0 мм: при пороге 0.8 — ok (толще порога), при 1.5 — risky (тоньше порога)
    assert texts.printability_verdict("60×60×60 мм", min_wall_mm=0.8, **geom)[0] == "ok"
    assert texts.printability_verdict("60×60×60 мм", min_wall_mm=1.5, **geom)[0] == "risky"


# ─────────────────────────── payload-уровень: _apply_dfm / _bed_xy ───────────────────────────
def _payload_dfm(printer, meas, dims, *, min_wall="0.8"):
    """Прогнать _apply_dfm на свежий payload и вернуть его (как это делают пути сметы в webapp)."""
    payload = {"price": 100.0, "grams_total": 5.0}   # «прочая» смета — не должна пострадать
    webapp._apply_dfm(payload, dims=dims, printer=printer, meas=meas,
                      settings={"dfm_min_wall_mm": min_wall})
    return payload


def test_apply_dfm_wont_fit_uses_printer_bed(monkeypatch):
    """Деталь больше стола КОНКРЕТНОГО принтера (slicer.bed_size застаблен) → payload.verdict=wont_fit,
    dfm с проверкой bed/bad. Сравнивается ПО-ДЕТАЛЬНЫЙ габарит (max_part_mm), не общий bbox."""
    monkeypatch.setattr(slicer, "bed_size", lambda mp: (200.0, 200.0))   # стаб-стол 200 мм
    printer = SimpleNamespace(machine_profile="dummy.json")
    p = _payload_dfm(printer, {"n_parts": 1, "max_part_mm": 300.0}, "300×40×40 мм")
    assert p["verdict"] == "wont_fit"
    bed = [d for d in p["dfm"] if d["key"] == "bed"]
    assert len(bed) == 1 and bed[0]["status"] == "bad"
    # заказ НЕ блокируется: _apply_dfm не добавляет ни блокирующих флагов, ни не трогает прочую смету
    assert "blocked" not in p and "disabled" not in p
    assert p["price"] == 100.0 and p["grams_total"] == 5.0


def test_apply_dfm_bed_threshold_actually_from_slicer(monkeypatch):
    """Тот же 300-мм деталь, но стол застаблен на 400 мм → уже НЕ wont_fit. Доказывает, что сравнение
    идёт именно с slicer.bed_size(printer.machine_profile), а не с константой."""
    monkeypatch.setattr(slicer, "bed_size", lambda mp: (400.0, 400.0))
    printer = SimpleNamespace(machine_profile="dummy.json")
    p = _payload_dfm(printer, {"n_parts": 1, "max_part_mm": 300.0}, "300×40×40 мм")
    assert p["verdict"] == "ok" and p["dfm"] == []


def test_apply_dfm_multipart_set_stays_ok(monkeypatch):
    """РЕГРЕССИЯ-ГАРД на payload-уровне: набор мелочи (n_parts>1) с большим общим bbox → verdict ok,
    без плашки. Именно этот случай ломал откаченную версию на живом экране."""
    monkeypatch.setattr(slicer, "bed_size", lambda mp: (200.0, 200.0))
    printer = SimpleNamespace(machine_profile="dummy.json")
    p = _payload_dfm(printer, {"n_parts": 8, "max_part_mm": 80.0, "min_extent_mm": 40.0,
                               "wall_proxy_mm": 12.0}, "300×300×40 мм")
    assert p["verdict"] == "ok" and p["dfm"] == []


def test_apply_dfm_legacy_no_meas_defaults_ok():
    """Легаси-смета без новых полей (meas=None, dims='') → verdict='ok', dfm=[] без падения.
    Поля вердикта всё равно проставлены (фронт сбросит прошлый вердикт)."""
    p = _payload_dfm(printer=None, meas=None, dims="")
    assert p["verdict"] == "ok" and p["dfm"] == []
    # verdict/dfm ПРИСУТСТВУЮТ в payload — консистентность на всех путях
    assert "verdict" in p and "dfm" in p


def test_apply_dfm_bed_xy_fallback_without_printer():
    """Принтер не передан → _bed_xy падает на config.BED_SIZE_MM (не паникует)."""
    assert webapp._bed_xy(None) == float(config.BED_SIZE_MM)


def test_apply_dfm_verdict_present_and_consistent(monkeypatch):
    """verdict и dfm присутствуют и согласованы: risky-деталь → verdict='risky' и есть хотя бы одна
    проверка со статусом warn (не bad)."""
    monkeypatch.setattr(slicer, "bed_size", lambda mp: (256.0, 256.0))
    printer = SimpleNamespace(machine_profile="dummy.json")
    p = _payload_dfm(printer, {"n_parts": 1, "max_part_mm": 60.0, "min_extent_mm": 2.0,
                               "wall_proxy_mm": 0.5}, "60×60×2 мм")
    assert p["verdict"] == "risky"
    assert p["dfm"] and all(d["status"] == "warn" for d in p["dfm"])
