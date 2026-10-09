"""Волна 7 разреза db.py: хвост «без домена» уехал в app/store/{timepred,analytics},
а сам фасад собран в ОДИН блок в конце файла.

Контракты волны:
  * фасад db реэкспортирует ТОТ ЖЕ объект на каждое из 10 имён (потребители зовут `db.<имя>`,
    tests/test_order_now.py подменяет `webapp.db.eta_date`);
  * в db.py не осталось повторных определений этих имён;
  * доменные модули НЕ импортируют app.db (разрез односторонний) и не прячут ленивых `from .`;
  * СОСТАВ db.py достигнут целевой: докстринг-карта, настоящие импорты, log/SCHEMA_VERSION/
    _SCHEMA, init-путь (12 def-ов) и единый фасадный блок — больше ничего;
  * фасад стоит ПОСЛЕ кода и это безопасно: модульных операторов, которые бы трогали его
    имена, ровно три (log, SCHEMA_VERSION, _SCHEMA), а init/миграции зовут голые имена в
    рантайме, когда модуль уже импортирован целиком;
  * осиротевшие маркеры «продолжение выреза волны N» и заголовок «# ---- заказы ----» (его
    секция уехала волной 4, над блоком предсказания он врал) сняты;
  * хрупкий инвариант: pricing/keyboards/texts делают ВЕРХНЕУРОВНЕВЫЙ `from .db import ...`,
    и переезд блока models из шапки в конец файла их не ломает только потому, что импорт
    app.db к ним не приводит. Закрепляем subprocess-ом.
"""
import ast
import pathlib
import re
import subprocess
import sys

import pytest

from app import db
from app.store import analytics, timepred

REPO = pathlib.Path(__file__).resolve().parents[1]

TIMEPRED_NAMES = ("add_print_sample", "record_time_sample", "print_sample_count",
                  "list_print_samples", "predict_minutes", "bigdata_stats")
ANALYTICS_NAMES = ("parse_due_date", "eta_date", "promised_date", "dashboard_stats")

MOVED = ([(timepred, n) for n in TIMEPRED_NAMES]
         + [(analytics, n) for n in ANALYTICS_NAMES])
ALL_NAMES = {n for _, n in MOVED}

# То, что обязано остаться в db.py навсегда: жизненный цикл БД, схема и миграции.
DB_KEEPS = ("_ensure_column", "_open", "connect_existing", "init",
            "_migrate_paths_to_relative",      # шаг схемы v8 (C2): пути в БД → относительные
            "_migrate_profiles_to_relative",   # шаг схемы v9 (хвост C2): профили слайсера
            "_backfill_printer_ownership", "_split_service_items_per_printer",
            "_split_print_binding_per_printer", "_seed_printers", "_backfill_printer_nozzles",
            "_seed_presets", "_seed_default_plastic", "close")


def _db_src() -> str:
    return (REPO / "app" / "db.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(("module", "name"), MOVED, ids=[n for _, n in MOVED])
def test_facade_reexports_the_same_object(module, name):
    assert hasattr(db, name), f"имя {name} потеряно в фасаде app/db.py"
    assert getattr(db, name) is getattr(module, name)


def test_all_10_names_are_covered():
    assert len(MOVED) == len(ALL_NAMES) == 10


def test_module_handles_are_reachable_from_db():
    assert db.timepred is timepred and db.analytics is analytics
    assert db.store_settings.__name__ == "app.store.settings", "псевдоним настроек обязан жить"


def test_no_leftover_definitions_in_dbpy():
    tree = ast.parse(_db_src())
    defined = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Assign):
            defined |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            defined.add(n.target.id)
    dupes = defined & ALL_NAMES
    assert not dupes, f"в db.py остались повторные определения: {sorted(dupes)}"


def test_dbpy_composition_is_final():
    """Целевой состав достигнут: кроме init-пути в db.py не осталось НИ ОДНОГО def-а,
    а модульных операторов ровно три — на них и держится безопасность фасада в конце файла."""
    tree = ast.parse(_db_src())
    defs = [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert tuple(defs) == DB_KEEPS, f"состав db.py изменился: {defs}"
    assigns = [t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)]
    assert assigns == ["log", "SCHEMA_VERSION", "_SCHEMA"]


def test_facade_is_one_block_after_the_code():
    """Фасад собран в один блок в конце файла: до первой его строки — только код, после —
    ни одного определения. Без этого «импорт ниже по использованию» был бы опасен."""
    src = _db_src()
    lines = src.split("\n")
    facade = [i for i, ln in enumerate(lines)
              if ln.startswith("from .store") and "# noqa" in ln]
    close_at = next(i for i, ln in enumerate(lines) if ln.startswith("async def close"))
    assert facade, "фасадные строки исчезли"
    assert min(facade) > close_at, "фасад обязан стоять ПОСЛЕ кода"
    tree = ast.parse(src)
    tail = [n for n in tree.body
            if n.lineno > min(facade) and not isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not tail, f"после начала фасада появился код: {tail}"
    # Инвентарь на закрытии программы — 458 строк; фасад РАСТЁТ с каждой новой функцией слоя
    # данных (B1 22.08: +4), поэтому проверяем нижнюю границу, а не точное число.
    assert len(facade) >= 458, f"инвентарь фасада съехал: {len(facade)} строк"


def test_orphaned_wave_markers_and_orders_header_are_gone():
    """Однострочные маркеры «продолжение выреза волны N» были указателями «фасад выше» внутри
    разорванного файла — после сборки фасада в один блок они бессмысленны. Заголовок
    «# ---- заказы ----» осиротел ещё волной 4 и врал над блоком предсказания времени."""
    src = _db_src()
    assert "# (продолжение выреза волны" not in src
    assert "# ---- заказы ----" not in src


def test_head_imports_are_still_all_alive():
    """После выреза десяти тел ни один импорт шапки не осиротел (ruff F401 это ловит, но
    инвариант стоит закрепить явно): time держит _seed_default_plastic, json/aiosqlite/Path —
    init и _open, config/materials/nozzles/presets/calibration — миграции и сиды,
    profilepaths/slicers — шаг v9 (профили слайсера в относительный вид)."""
    src = _db_src()
    for alive in ("import json", "import logging", "import time", "import aiosqlite",
                  "from pathlib import Path",
                  "from . import calibration, config, materials, nozzles, presets, "
                  "profilepaths, slicers",
                  "from .store import core"):
        assert re.search(rf"^{re.escape(alive)}$", src, re.M), f"{alive} пропал из шапки db.py"


@pytest.mark.parametrize("mod", ["timepred", "analytics"])
def test_store_module_does_not_import_db(mod):
    """Разрез односторонний. Для timepred это ещё и проверка цепочки .printjournal/.plastics:
    два ребра наружу — единственные межмодульные связи волны."""
    code = f"import app.store.{mod}, sys; print('app.db' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False", f"app.store.{mod} подтянул app.db"


@pytest.mark.parametrize("mod", ["timepred", "analytics"])
def test_no_lazy_relative_imports_inside_bodies(mod):
    """Ленивый `from .X import y` внутри функции после переезда указывал бы на пакет store,
    а не на app (грабли endgame). В волне 7 таких не было: ленивые `import datetime`/
    `import math` в сроках — АБСОЛЮТНЫЕ stdlib-импорты, правилом не задеты."""
    src = (REPO / "app" / "store" / f"{mod}.py").read_text(encoding="utf-8")
    assert not re.search(r"^[ \t]+from \.", src, re.M), f"в app/store/{mod}.py есть ленивый `from .`"


def test_top_level_db_consumers_still_import_from_a_clean_process():
    """pricing/keyboards/texts делают верхнеуровневый `from .db import ...`. Раньше их спасало
    то, что блок models стоял в ШАПКЕ db.py; теперь — то, что `import app.db` до них не
    доходит, и частично проинициализированный модуль им не подсовывается. Инвариант хрупкий:
    начни app.db (через store/*) тянуть эти три модуля — здесь будет ImportError."""
    code = ("import app.pricing, app.keyboards, app.texts\n"
            "from app.db import Plastic, REQ_STATUS_LABELS\n"
            "print('ok')")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_timepred_edges_are_explicit_store_to_store():
    """Ребра наружу импортируются ЯВНО, а не резолвятся через фасад db."""
    src = (REPO / "app" / "store" / "timepred.py").read_text(encoding="utf-8")
    assert "from .printjournal import add_print_journal" in src
    assert "from .plastics import cogs_summary, inventory_summary" in src
    from app.store import plastics, printjournal
    assert timepred.add_print_journal is printjournal.add_print_journal
    assert timepred.inventory_summary is plastics.inventory_summary


async def test_print_sample_is_stored_and_listed(tmp_db):
    """Смок копилки замеров: единственная своя таблица модуля — print_samples."""
    was = await timepred.print_sample_count()
    sid = await timepred.add_print_sample(request_id=None, model_hash="w7-hash",
                                          est_minutes=10.0, actual_minutes=15.0, grams=12.0,
                                          material_type="W7PLA", source="test")
    assert sid > 0
    assert await timepred.print_sample_count() == was + 1
    assert await db.print_sample_count() == was + 1          # тот же путь через фасад
    top = (await timepred.list_print_samples(1))[0]
    assert top["model_hash"] == "w7-hash" and top["actual_minutes"] == 15.0


async def test_prediction_sleeps_until_the_sample_grows(tmp_db):
    """Механизм СПИТ, пока выборка мала: factor=1.0, applied=False — коррекцию на паре
    замеров не выдумываем (план print-time-prediction §6/§8)."""
    res = await timepred.predict_minutes(60.0, min_samples=10 ** 6)
    assert res["applied"] is False and res["factor"] == 1.0 and res["predicted"] == 60.0
    assert (await timepred.predict_minutes(0))["predicted"] == 0.0    # нулевая оценка — не трогаем


async def test_record_time_sample_reaches_the_journal_across_modules(tmp_db):
    """Смок единственного ребра волны наружу: record_time_sample без request_id создаёт
    событие через store.printjournal.add_print_journal, и коррекция это событие видит."""
    await timepred.record_time_sample(None, 10.0, 15.0, material_type="W7MAT",
                                      model_hash="w7-rec")
    got = await timepred.predict_minutes(10.0, "W7MAT", min_samples=1)
    assert got["applied"] is True and got["factor"] == 1.5 and got["predicted"] == 15.0
    assert (await db.predict_minutes(10.0, "W7MAT", min_samples=1))["factor"] == 1.5


async def test_bigdata_stats_pulls_two_foreign_domains(tmp_db):
    """Сводка тянет склад из store/plastics и собственную коррекцию — оба ребра живые."""
    out = await timepred.bigdata_stats()
    assert {"samples", "materials", "collection", "totals", "inventory", "cogs",
            "prediction"} <= set(out)
    assert out["prediction"]["factor"] > 0
    assert set(out["inventory"]) and set(out["cogs"])


def test_due_date_parsing_rejects_impossible_dates():
    assert analytics.parse_due_date("10.6") == "10.06"
    assert analytics.parse_due_date("10.06.2026") == "10.06"
    assert analytics.parse_due_date("29.02") == "29.02"      # 2000 — високосный
    assert analytics.parse_due_date("31.02") is None
    assert analytics.parse_due_date("-") is None and analytics.parse_due_date("") is None
    assert db.parse_due_date("10/06") == "10.06"             # тот же путь через фасад


def test_dates_are_computed_from_today():
    import datetime
    today = datetime.date.today()
    assert analytics.eta_date(1.0) == (today + datetime.timedelta(days=1)).strftime("%d.%m")
    assert db.promised_date(0.0, "rush", {}) == (today + datetime.timedelta(days=1)).strftime("%d.%m")
    long = analytics.promised_date(20 * 60.0, None, {"term_split_hours": 12})
    assert long == (today + datetime.timedelta(days=2)).strftime("%d.%m")
    assert analytics.promised_date(20 * 60.0, "rush", {"term_split_hours": 12}) != long


async def test_dashboard_stats_aggregates_foreign_tables(tmp_db):
    """dashboard_stats не владеет ни одной таблицей — считает по requests/orders."""
    out = await analytics.dashboard_stats()
    assert {"calcs", "orders", "printed", "conv", "revenue_printed", "series14",
            "status_mix", "sources_30d", "clients_30d"} <= set(out)
    assert len(out["series14"]) == 14
    assert (await db.dashboard_stats())["orders"] == out["orders"]   # тот же путь через фасад
