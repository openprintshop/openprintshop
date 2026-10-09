"""Волна 3 разреза db.py: принтеры и печать живут в app/store/{printers,printjournal,printjob,printqueue}.

Контракты волны:
  * фасад db реэкспортирует ТОТ ЖЕ объект на каждое из 72 имён (потребители зовут `db.<имя>`,
    тесты подменяют `monkeypatch.setattr(db, ...)` и `printwatch.db.list_printers`);
  * в db.py не осталось повторных определений этих имён;
  * доменные модули НЕ импортируют app.db (разрез односторонний) и не прячут ленивых `from .`
    — у printers ленивый `from . import bambu, elegoo, printerdrv` ПОДНЯТ на верхний уровень
    как `from .. import ...`, это единственная поведенческая правка волны;
  * поведение не изменилось: карточки принтеров, ТО, журнал печатей вместе с мостом в
    калибровку и очередь печати работают через store-модули ровно как раньше через db.
"""
import ast
import pathlib
import re
import subprocess
import sys

import pytest

from app import db
from app.store import printers, printjob, printjournal, printqueue

REPO = pathlib.Path(__file__).resolve().parents[1]

PRINTERS_NAMES = ("_DEFAULT_SERVICE_ITEMS", "_to_service_item", "_seed_service_items",
                  "list_service_items", "get_service_item", "accrue_service",
                  "mark_service_notified", "mark_serviced", "list_service_log", "add_service_item",
                  "update_service_item", "delete_service_item", "_to_printer", "list_printers",
                  "get_printer", "refresh_active_printers", "refresh_printers",
                  "refresh_printer_sessions", "get_default_printer", "add_printer",
                  "update_printer", "set_default_printer", "delete_printer", "PLATE_KINDS",
                  "list_calib_plates", "get_calib_plate", "save_calib_plate",
                  "set_default_calib_plate", "delete_calib_plate")
PRINTJOURNAL_NAMES = ("add_self_print", "_fail_pct_norm", "add_print_journal",
                      "add_printer_telemetry", "prune_printer_telemetry", "list_printer_telemetry",
                      "get_print_event", "grade_print_journal", "set_print_journal_fields",
                      "get_print_journal", "apply_journal_stock", "update_print_journal",
                      "list_print_journal", "list_print_journal_for_request",
                      "delete_print_journal", "print_journal_stats", "active_calib_run",
                      "_calib_step_row", "get_calib_step", "update_calib_step",
                      "set_journal_calib", "list_calib_journal", "backfill_calib_types",
                      "attach_journal_to_calib")
PRINTJOB_NAMES = ("binding_key", "set_print_binding", "get_print_binding", "list_print_bindings",
                  "update_print_binding", "clear_print_binding", "clear_print_binding_for_request",
                  "_settings_row", "save_print_settings", "get_print_settings",
                  "find_print_settings", "list_print_settings", "prune_print_settings")
PRINTQUEUE_NAMES = ("_queue_active_rows", "queue_normalize", "queue_items", "lcd_print_queue",
                    "queue_move", "queue_position")

MOVED = ([(printers, n) for n in PRINTERS_NAMES]
         + [(printjournal, n) for n in PRINTJOURNAL_NAMES]
         + [(printjob, n) for n in PRINTJOB_NAMES]
         + [(printqueue, n) for n in PRINTQUEUE_NAMES])


@pytest.mark.parametrize(("module", "name"), MOVED, ids=[n for _, n in MOVED])
def test_facade_reexports_the_same_object(module, name):
    assert hasattr(db, name), f"имя {name} потеряно в фасаде app/db.py"
    assert getattr(db, name) is getattr(module, name)


def test_module_handles_are_reachable_from_db():
    """Хэндл модуля в db (метод endgame). Коллизий с параметрами db.py у этих четырёх нет —
    в отличие от `plates`, из-за чего пластины стола и уехали внутрь printers."""
    assert db.printers is printers and db.printjournal is printjournal
    assert db.printjob is printjob and db.printqueue is printqueue


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
    dupes = defined & (set(PRINTERS_NAMES) | set(PRINTJOURNAL_NAMES) | set(PRINTJOB_NAMES)
                       | set(PRINTQUEUE_NAMES))
    assert not dupes, f"в db.py остались повторные определения: {sorted(dupes)}"


@pytest.mark.parametrize("mod", ["printers", "printjournal", "printjob", "printqueue"])
def test_store_module_does_not_import_db(mod):
    """Разрез односторонний: доменный модуль не тянет app.db ни на каком уровне.

    Для printers это ещё и проверка поднятого импорта драйверов: цепочка
    config→elegoo→bambu→moonraker→printerdrv не должна приводить обратно в app.db."""
    code = f"import app.store.{mod}, sys; print('app.db' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False", f"app.store.{mod} подтянул app.db"


@pytest.mark.parametrize("mod", ["printers", "printjournal", "printjob", "printqueue"])
def test_no_lazy_relative_imports_inside_bodies(mod):
    """Ленивый `from .X import y` внутри функции после переезда указывал бы на пакет store,
    а не на app (грабли endgame). Единственный такой импорт волны — драйверы в
    refresh_printer_sessions — поднят на верхний уровень; закрепляем, что новых не завелось."""
    src = (REPO / "app" / "store" / f"{mod}.py").read_text(encoding="utf-8")
    assert not re.search(r"^[ \t]+from \.", src, re.M), f"в app/store/{mod}.py есть ленивый `from .`"


def test_printer_drivers_are_imported_at_module_level():
    """Единственная поведенческая правка волны: подписчики телеметрии берутся из top-level
    импорта, а не из ленивого `from . import` внутри тела."""
    src = (REPO / "app" / "store" / "printers.py").read_text(encoding="utf-8")
    assert "from .. import bambu, config, elegoo, printerdrv" in src
    assert printers.bambu is not None and printers.elegoo is not None


async def test_printer_card_roundtrip(tmp_db):
    """Смок железа: карточка заводится, читается списком и становится принтером по умолчанию."""
    pid = await printers.add_printer("Смок", "profile.json", "proc", "fila", is_active=1)
    await printers.set_default_printer(pid)
    assert pid in [p.id for p in await printers.list_printers(active_only=True)]
    cur = await printers.get_default_printer()
    assert cur is not None and cur.id == pid
    assert await tmp_db.get_default_printer() is not None      # тот же объект через фасад
    assert await printjob.binding_key() == pid                  # printjob → printers, один путь


async def test_service_accrual_and_log(tmp_db):
    """Связка ТО: наработка капает пунктам машины, mark_serviced обнуляет и пишет журнал."""
    pid = await printers.add_printer("ТО-смок", "p.json", "proc", "fila", is_active=1)
    await printers.accrue_service(120.0, 1, printer_id=pid)
    items = await printers.list_service_items(printer_id=pid)
    assert items, "пункты ТО не засеялись для новой карточки"
    it = next(i for i in items if i.trigger_type == "hours")
    assert it.hours_since == pytest.approx(2.0)
    await printers.mark_serviced(it.id, note="смок")
    assert (await printers.get_service_item(it.id)).hours_since == 0
    assert [r["note"] for r in await printers.list_service_log(limit=5)][0] == "смок"


async def test_print_journal_roundtrip_with_calib_bridge(tmp_db):
    """Смок журнала: запись создаётся, читается, и мост в калибровку не роняет её."""
    jid = await printjournal.add_print_journal(
        label="смок волны 3", plastic_id=None, plastic_name="PLA",
        grams=10.0, minutes=30.0, graded=1, source="self")
    row = await printjournal.get_print_journal(jid)
    assert row and row["label"] == "смок волны 3" and row["grams"] == 10.0
    assert row["calib_run_id"] in (None, 0)      # открытой кампании нет — привязки не случилось
    await printjournal.attach_journal_to_calib(jid)          # повторный вызов моста безвреден
    assert (await printjournal.get_print_journal(jid))["label"] == "смок волны 3"
    assert (await tmp_db.get_print_journal(jid))["id"] == jid
    sid = await printjournal.add_self_print("PLA", 5.0, 3.0, label="самопечать")
    assert (await printjournal.get_print_journal(sid))["source"] == "self"


async def test_queue_normalize_and_position(tmp_db):
    """Смок очереди: активные заказы получают сплошные позиции 1..N, queue_position их видит."""
    ids = [await tmp_db.add_request(
        user_id=1, user_name="t", model_label=f"m{i}", printer="", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=10, minutes_total=60,
        price=1.0, preview_path=None) for i in range(3)]
    await printqueue.queue_normalize()
    pos = [(await printqueue.queue_position(r))["pos"] for r in ids]
    assert sorted(pos) == list(range(1, len(pos) + 1))
    assert (await printqueue.queue_position(ids[0]))["total"] >= 3
