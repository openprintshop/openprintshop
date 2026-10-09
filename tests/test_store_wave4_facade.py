"""Волна 4 разреза db.py: заказы и деньги живут в app/store/{orders,money}.

Контракты волны:
  * фасад db реэкспортирует ТОТ ЖЕ объект на каждое из 56 имён (потребители зовут `db.<имя>`,
    тесты подменяют `monkeypatch.setattr(db, ...)`, а db.init() на шаге миграции
    user_version<6 зовёт уехавшую backfill_order_groups именно через фасад);
  * в db.py не осталось повторных определений этих имён;
  * доменные модули НЕ импортируют app.db (разрез односторонний) и не прячут ленивых `from .`;
  * поведение не изменилось: таймлайн заказа пишется теми же двумя точками, а баланс члена
    семьи по-прежнему молча уходит на общий счёт (инцидентная зона 16.07 — режется целиком).
"""
import ast
import pathlib
import re
import subprocess
import sys

import pytest

from app import db
from app.store import money, orders

REPO = pathlib.Path(__file__).resolve().parents[1]

ORDERS_NAMES = ("create_self_print_order", "admin_would_spend", "is_self_print_order",
                "set_user_discount", "get_user_discount", "list_user_discounts",
                "delete_user_discount", "list_customers", "_to_request", "add_request",
                "create_order_group", "_GROUP_FIELDS", "update_order_group", "get_order_group",
                "list_requests_by_group", "backfill_order_groups", "group_id_of_request",
                "group_anchor_request_id", "list_delivery_types", "get_delivery_type",
                "add_delivery_type", "update_delivery_type", "delete_delivery_type",
                "get_request", "list_user_requests", "count_user_requests", "pending_price_count",
                "is_self_order", "_self_print_predicate", "list_requests", "update_request",
                "mark_request_seen", "count_unseen_requests", "mark_all_requests_seen",
                "delete_request", "delete_test_requests", "get_status_history", "add_order",
                "daily_revenue", "queue_load")
MONEY_NAMES = ("get_balance", "get_personal_balance", "adjust_balance", "add_payment",
               "payments_for_request", "list_balance_tx", "set_balance_tx_comment", "family_of",
               "get_family", "_family_member_name", "adjust_family_balance", "list_family_tx",
               "create_family", "add_family_member", "remove_family_member", "list_families")

MOVED = [(orders, n) for n in ORDERS_NAMES] + [(money, n) for n in MONEY_NAMES]


@pytest.mark.parametrize(("module", "name"), MOVED, ids=[n for _, n in MOVED])
def test_facade_reexports_the_same_object(module, name):
    assert hasattr(db, name), f"имя {name} потеряно в фасаде app/db.py"
    assert getattr(db, name) is getattr(module, name)


def test_module_handles_are_reachable_from_db():
    """Хэндл модуля в db (метод endgame). Денежный модуль назван money, а НЕ payments:
    рядом живёт app/payments.py, и `from .store import payments` было бы заявкой на F811."""
    assert db.orders is orders and db.money is money


def test_group_fields_is_the_same_tuple():
    """Модульная константа реэкспортируется тем же объектом, а не копией."""
    assert db._GROUP_FIELDS is orders._GROUP_FIELDS
    assert isinstance(orders._GROUP_FIELDS, tuple)


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
    dupes = defined & (set(ORDERS_NAMES) | set(MONEY_NAMES))
    assert not dupes, f"в db.py остались повторные определения: {sorted(dupes)}"


def test_promised_date_never_lands_in_orders():
    """Ложная зависимость волны 4: наивный обход ast.Name даёт «add_request -> promised_date»,
    но у add_request это kw-ПАРАМЕТР. Волной 4 функция осталась в db.py как хвост «без
    домена», волной 7 уехала в app/store/analytics (сроки и дашборд — производные цифры без
    своей таблицы). Инвариант, который переживает обе волны: в store/orders её нет и быть не
    может — top-level promised_date рядом с add_request(..., promised_date=...) дал бы F811."""
    assert callable(db.promised_date)
    assert not hasattr(orders, "promised_date")
    assert db.promised_date.__module__ == "app.store.analytics"


@pytest.mark.parametrize("mod", ["orders", "money"])
def test_store_module_does_not_import_db(mod):
    """Разрез односторонний: доменный модуль не тянет app.db ни на каком уровне.

    Для orders это ещё и проверка цепочки .printjournal -> ..calibration -> .plastics:
    жадный импорт моста в калибровку не должен приводить обратно в app.db."""
    code = f"import app.store.{mod}, sys; print('app.db' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False", f"app.store.{mod} подтянул app.db"


@pytest.mark.parametrize("mod", ["orders", "money"])
def test_no_lazy_relative_imports_inside_bodies(mod):
    """Ленивый `from .X import y` внутри функции после переезда указывал бы на пакет store,
    а не на app (грабли endgame). В волне 4 таких не было ни одного — закрепляем."""
    src = (REPO / "app" / "store" / f"{mod}.py").read_text(encoding="utf-8")
    assert not re.search(r"^[ \t]+from \.", src, re.M), f"в app/store/{mod}.py есть ленивый `from .`"


def test_init_reaches_backfill_through_facade():
    """Тело init() не правилось: миграция user_version<6 зовёт backfill_order_groups голым
    именем, и разрешается оно только фасадной строкой. Без неё боевая БД упала бы NameError."""
    src = (REPO / "app" / "db.py").read_text(encoding="utf-8")
    assert "await backfill_order_groups()" in src
    assert "from .store.orders import backfill_order_groups" in src


async def test_order_timeline_roundtrip(tmp_db):
    """Смок заказа: add_request кладёт начальную точку таймлайна, update_request — вторую,
    delete_request сносит и заказ, и таймлайн одним commit-ом."""
    rid = await orders.add_request(
        user_id=777, user_name="смок", model_label="волна 4", printer="", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=10, minutes_total=60,
        price=5.0, preview_path=None)
    assert [s for s, _at in await orders.get_status_history(rid)] == ["new"]
    await orders.update_request(rid, status="in_progress")
    assert [s for s, _at in await orders.get_status_history(rid)] == ["new", "in_progress"]
    assert (await tmp_db.get_request(rid)).status == "in_progress"     # тот же путь через фасад
    gone = await orders.delete_request(rid)
    assert gone is not None and gone.id == rid
    assert await orders.get_request(rid) is None


async def test_delivery_type_lives_with_the_group(tmp_db):
    """Смок доставки: тип доставки заводится и попадает в шапку группы белым списком полей."""
    tid = await orders.add_delivery_type("Смок-доставка", 9.5, eta="1 день")
    rid = await orders.add_request(
        user_id=778, user_name="смок", model_label="в группе", printer="", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1.0, preview_path=None, order_group="g-w4")
    await orders.create_order_group("g-w4", 778, delivery_type_id=tid, delivery_cost=9.5)
    grp = await orders.get_order_group("g-w4")
    assert grp["delivery_type_id"] == tid and grp["delivery_cost"] == 9.5
    assert await orders.group_anchor_request_id("g-w4") == rid
    assert (await orders.get_delivery_type(tid))["name"] == "Смок-доставка"


async def test_family_balance_swallows_personal_one(tmp_db):
    """Смок инцидентной зоны: у члена семьи adjust_balance молча уходит на ОБЩИЙ счёт,
    личный остаётся нулём, а роспуск семьи возвращает остаток на личный баланс."""
    await money.adjust_balance(801, 10.0, reason="topup")
    fam = await money.create_family([(801, "Первый"), (802, "Второй")], title="Смоки")
    fid = fam["id"]
    await money.adjust_balance(801, 5.0, reason="topup")
    assert await money.get_personal_balance(801) == 0.0
    assert await money.get_balance(801) == await money.get_balance(802) == 15.0
    assert await tmp_db.get_balance(801) == 15.0                       # тот же путь через фасад
    assert await money.remove_family_member(fid, 802) is None          # остался один — распад
    assert await money.family_of(802) is None and await money.family_of(801) is None
    assert await money.get_personal_balance(801) == 15.0               # остаток вернулся личным
