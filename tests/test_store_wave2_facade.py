"""Волна 2 разреза db.py: справочники живут в app/store/{settings,plastics,catalog} (+ _jload в core).

Контракты волны:
  * фасад db реэкспортирует ТОТ ЖЕ объект на каждое из 96 имён (потребители зовут `db.<имя>`,
    тесты подменяют `monkeypatch.setattr(db, ...)`, webapp зовёт `db._canon_color`);
  * в db.py не осталось повторных определений этих имён;
  * доменные модули НЕ импортируют app.db (разрез односторонний) и не прячут ленивых `from .`;
  * поведение не изменилось: dual-write склад↔катушки и идемпотентность apply_order_stock
    работают через store-модуль ровно как раньше через db.
"""
import ast
import pathlib
import re
import subprocess
import sys

import pytest

from app import db
from app.store import catalog, core, plastics, settings

REPO = pathlib.Path(__file__).resolve().parents[1]

SETTINGS_NAMES = ("DEFAULT_SETTINGS", "SETTING_LABELS", "SETTINGS_ORDER", "DEFAULT_TEXT_SETTINGS",
                  "TEXT_SETTING_LABELS", "TEXT_SETTINGS_ORDER", "BRAND_SETTINGS_ORDER",
                  "PAY_SETTINGS_ORDER", "OWNERS_SETTINGS_ORDER", "ACCESS_SETTINGS_ORDER",
                  "get_settings", "set_setting", "delete_setting", "get_text_settings",
                  "set_text_setting")
PLASTICS_NAMES = ("_to_plastic", "list_plastics", "get_plastic", "get_plastic_by_name", "add_plastic",
                  "update_plastic", "delete_plastic", "set_default_plastic", "get_default_plastic",
                  "recent_plastics_for_user", "popular_plastics", "increment_plastic_uses",
                  "_deduct_stock_legacy", "deduct_stock", "get_color_stock", "set_color_stock",
                  "deduct_color_stock", "deduct_color_stock_split", "_canon_color", "_to_spool",
                  "_spool_recalc", "add_stock_ledger", "add_spool", "get_spool", "list_spools",
                  "spool_ledger", "set_spool_status", "spool_stock_by_color", "spool_stock_total",
                  "deduct_from_spools", "_dual_write_spool", "color_availability",
                  "compute_request_cogs", "inventory_summary", "cogs_summary", "set_order_stock_plan",
                  "get_order_stock_plan", "apply_order_stock", "_RESERVING_STATUSES", "_RES_PH",
                  "reserved_grams_by_plastic", "list_color_shades", "set_color_shade",
                  "delete_color_shade", "_hex_to_rgb", "get_color_overrides")
CATALOG_NAMES = ("_to_nozzle", "list_printer_nozzles", "add_printer_nozzle", "update_printer_nozzle",
                 "set_default_printer_nozzle", "delete_printer_nozzle", "machine_profile_for",
                 "known_nozzles", "_to_quality", "nozzle_of", "list_quality", "get_quality",
                 "add_quality", "update_quality", "delete_quality", "set_default_quality",
                 "_to_strength", "list_strength", "get_strength", "add_strength", "update_strength",
                 "delete_strength", "set_default_strength", "list_discounts", "add_discount",
                 "delete_discount", "best_discount", "_to_postproc", "list_postproc_options",
                 "add_postproc_option", "update_postproc_option", "delete_postproc_option",
                 "set_request_options", "get_request_options")
CORE_NAMES = ("_jload",)

MOVED = ([(settings, n) for n in SETTINGS_NAMES] + [(plastics, n) for n in PLASTICS_NAMES]
         + [(catalog, n) for n in CATALOG_NAMES] + [(core, n) for n in CORE_NAMES])


@pytest.mark.parametrize(("module", "name"), MOVED, ids=[n for _, n in MOVED])
def test_facade_reexports_the_same_object(module, name):
    assert hasattr(db, name), f"имя {name} потеряно в фасаде app/db.py"
    assert getattr(db, name) is getattr(module, name)


def test_module_handles_are_reachable_from_db():
    """Хэндл модуля в db (метод endgame). У настроек он под псевдонимом: голое `settings`
    столкнулось бы с параметром promised_date(..., settings: dict) — ruff F811."""
    assert db.store_settings is settings and db.plastics is plastics and db.catalog is catalog
    assert db.core is core


def test_no_leftover_definitions_in_dbpy():
    tree = ast.parse((REPO / "app" / "db.py").read_text(encoding="utf-8"))
    defined = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Assign):
            defined |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            defined.add(n.target.id)
    dupes = defined & (set(SETTINGS_NAMES) | set(PLASTICS_NAMES) | set(CATALOG_NAMES) | set(CORE_NAMES))
    assert not dupes, f"в db.py остались повторные определения: {sorted(dupes)}"


@pytest.mark.parametrize("mod", ["settings", "plastics", "catalog"])
def test_store_module_does_not_import_db(mod):
    """Разрез односторонний: доменный модуль не тянет app.db ни на каком уровне."""
    code = (f"import app.store.{mod}, sys; print('app.db' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False", f"app.store.{mod} подтянул app.db"


@pytest.mark.parametrize("mod", ["settings", "plastics", "catalog", "core"])
def test_no_lazy_relative_imports_inside_bodies(mod):
    """Ленивый `from .X import y` внутри функции после переезда указывал бы на пакет store,
    а не на app (грабли endgame). Их в волне не было — закрепляем."""
    src = (REPO / "app" / "store" / f"{mod}.py").read_text(encoding="utf-8")
    assert not re.search(r"^[ \t]+from \.", src, re.M), f"в app/store/{mod}.py есть ленивый `from .`"


def test_settings_constants_are_one_family():
    """Дефолты и подписи — один объект по обе стороны фасада (иначе прайс разъедется)."""
    assert db.DEFAULT_SETTINGS is settings.DEFAULT_SETTINGS
    assert db.SETTING_LABELS is settings.SETTING_LABELS
    assert db.SETTINGS_ORDER is settings.SETTINGS_ORDER
    assert set(db.SETTINGS_ORDER) <= set(db.DEFAULT_SETTINGS)


def test_canon_color_still_called_as_db_attribute():
    """app/webapp.py:368 и tests/test_spools.py зовут db._canon_color атрибутом."""
    assert db._canon_color(" Чёрный ") == "черный"


async def test_dual_write_through_store_module(tmp_db):
    """Смок связки: списание цвета уменьшает и legacy-остаток, и катушку, и пишет журнал."""
    pid = (await plastics.list_plastics())[0].id
    await plastics.set_color_stock(pid, "Оранжевый", 400, 50)
    sid = await plastics.add_spool(pid, "оранжевый", 400)
    await plastics.deduct_color_stock(pid, "Оранжевый", 120)
    assert (await plastics.get_color_stock(pid))["Оранжевый"]["stock"] == 280
    assert (await plastics.get_spool(sid))["remaining_g"] == 280
    assert [r["reason"] for r in await plastics.spool_ledger(sid)][-1] == "print"
    assert (await tmp_db.get_color_stock(pid))["Оранжевый"]["stock"] == 280   # тот же объект через фасад


async def test_apply_order_stock_is_idempotent(tmp_db):
    """Захват stock_deducted: второй «напечатано» не списывает склад повторно."""
    pid = (await plastics.list_plastics())[0].id
    await plastics.set_color_stock(pid, "Синий", 500, 0)
    rid = await tmp_db.add_request(
        user_id=1, user_name="t", model_label="m", printer="", plastic_name="x", quantity=1,
        quality="q", strength="s", grams_total=100, minutes_total=10, price=1.0, preview_path=None)
    await plastics.set_order_stock_plan(rid, [{"plastic_id": pid, "name": "x", "grams": 100,
                                               "colors": ["Синий"]}])
    assert await plastics.get_order_stock_plan(rid) == [
        {"plastic_id": pid, "name": "x", "grams": 100, "colors": ["Синий"]}]
    await plastics.apply_order_stock(rid)
    left = (await plastics.get_color_stock(pid))["Синий"]["stock"]
    assert left == 400
    assert await plastics.apply_order_stock(rid) == []
    assert (await plastics.get_color_stock(pid))["Синий"]["stock"] == 400
