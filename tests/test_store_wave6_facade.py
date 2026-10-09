"""Волна 6 разреза db.py: мастерское живёт в app/store/{crm,ops,team,products,calib,kb}.

Контракты волны:
  * фасад db реэкспортирует ТОТ ЖЕ объект на каждое из 102 имён (потребители зовут `db.<имя>`,
    тесты подменяют `monkeypatch.setattr(db, ...)`);
  * модульные константы (CRM_SEGMENT_LABELS, _CRM_AGG_SQL, _BACKUP_RUNS_KEEP) реэкспортируются
    тем же объектом, а не копией; пере-присваиваемых модульных переменных в волне нет вовсе;
  * в db.py не осталось повторных определений этих имён;
  * доменные модули НЕ импортируют app.db (разрез односторонний) и не прячут ленивых `from .`;
  * get_text_setting дописана в СУЩЕСТВУЮЩИЙ store/settings.py (её домен — настройки, а не
    деск-вход, в хвосте секции которого она физически лежала), а фасадная строка стоит в
    алфавитной группе settings (волной 7 весь фасад собран в один блок в конце db.py);
  * имя модуля калибровки — `calib`, а не `calibration`: последнее занято top-level импортом
    app/calibration.py, и `from .store import calibration` дал бы F811;
  * поведение не изменилось — четыре транзакционные связки волны проверяются смоками:
    атомарный захват права на реферальную выплату, антидубль публикации release note,
    одноразовость кода входа в деск и гонка двух покупок последней штуки с витрины.
"""
import ast
import pathlib
import re
import subprocess
import sys

import pytest

from app import db
from app.store import calib, crm, kb, ops, products, team
from app.store import settings as store_settings

REPO = pathlib.Path(__file__).resolve().parents[1]

CRM_NAMES = ("CRM_SEGMENT_LABELS", "crm_segments", "_CRM_AGG_SQL", "_crm_row",
             "_crm_manual_maps", "list_customers_crm", "customer_card", "get_customer",
             "get_user_last_color", "set_user_last_color", "upsert_customer",
             "ensure_birthday_tasks", "crm_broadcast_targets", "add_crm_task", "list_crm_tasks",
             "set_crm_task_done", "delete_crm_task", "crm_tasks_due", "mark_crm_task_notified",
             "crm_dashboard", "log_event", "list_activity", "add_promo", "list_promos",
             "get_promo", "delete_promo", "validate_promo", "add_referral", "get_referral",
             "award_referral_if_due", "redeem_promo")
OPS_NAMES = ("bump_endpoint_hit", "usage_stats", "_BACKUP_RUNS_KEEP", "add_backup_run",
             "list_backup_runs", "last_backup_run", "upsert_error", "list_errors",
             "count_new_errors", "mark_errors_seen", "purge_old_errors",
             "add_admin_notification", "list_admin_notifications", "get_admin_notification",
             "update_admin_notification", "delete_admin_notification",
             "count_new_admin_notifications", "try_mark_published")
TEAM_NAMES = ("list_staff", "get_staff", "set_staff", "remove_staff", "list_desk_sessions",
              "add_desk_session", "touch_desk_session", "drop_desk_session",
              "drop_desk_sessions_of", "add_desk_code", "drop_desk_codes", "take_desk_code",
              "prune_desk_auth")
PRODUCT_NAMES = ("_to_product", "add_product", "update_product", "delete_product", "get_product",
                 "list_products", "count_products", "buy_product")
CALIB_NAMES = ("add_calib_run", "seed_calib_steps", "list_calib_runs", "get_calib_run",
               "update_calib_run", "delete_calib_run", "list_calib_steps", "add_calib_measure",
               "list_calib_measures", "delete_calib_measure", "save_calib_sample",
               "list_calib_samples", "get_calib_sample", "set_calib_sample_photo",
               "delete_calib_sample", "save_calib_passport", "_passport_row",
               "list_calib_passports", "get_calib_passport", "get_calib_passport_by_id",
               "delete_calib_passport", "snapshot_calib_presets", "list_calib_preset_latest",
               "list_calib_preset_history", "get_calib_preset_snapshot", "calib_overview")
KB_NAMES = ("_now_txt", "kb_list", "kb_get", "kb_save", "kb_delete")
SETTINGS_NAMES = ("get_text_setting",)

MOVED = ([(crm, n) for n in CRM_NAMES]
         + [(ops, n) for n in OPS_NAMES]
         + [(team, n) for n in TEAM_NAMES]
         + [(products, n) for n in PRODUCT_NAMES]
         + [(calib, n) for n in CALIB_NAMES]
         + [(kb, n) for n in KB_NAMES]
         + [(store_settings, n) for n in SETTINGS_NAMES])
ALL_NAMES = {n for _, n in MOVED}
NEW_MODULES = ["crm", "ops", "team", "products", "calib", "kb"]


@pytest.mark.parametrize(("module", "name"), MOVED, ids=[n for _, n in MOVED])
def test_facade_reexports_the_same_object(module, name):
    assert hasattr(db, name), f"имя {name} потеряно в фасаде app/db.py"
    assert getattr(db, name) is getattr(module, name)


def test_all_102_names_are_covered():
    """Волна заявлена на 102 имени — список в тесте не должен молча похудеть."""
    assert len(MOVED) == len(ALL_NAMES) == 102


def test_module_handles_are_reachable_from_db():
    """Хэндлы модулей в db (метод endgame). Хэндл калибровки — `calib`, а НЕ `calibration`:
    последнее в db.py занято top-level импортом app/calibration.py (F811)."""
    assert db.crm is crm and db.ops is ops and db.team is team
    assert db.products is products and db.calib is calib and db.kb is kb
    assert db.calibration is not calib, "имя calibration должно остаться за app/calibration.py"
    assert db.store_settings is store_settings


def test_module_constants_are_the_same_objects():
    """Модульные константы реэкспортируются тем же объектом, а не копией.
    Пере-присваиваемых модульных переменных (global X) в волне нет ни одной."""
    assert db.CRM_SEGMENT_LABELS is crm.CRM_SEGMENT_LABELS
    assert db._CRM_AGG_SQL is crm._CRM_AGG_SQL and isinstance(crm._CRM_AGG_SQL, str)
    assert db._BACKUP_RUNS_KEEP is ops._BACKUP_RUNS_KEEP == 200


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
    dupes = defined & ALL_NAMES
    assert not dupes, f"в db.py остались повторные определения: {sorted(dupes)}"


def test_get_text_setting_landed_in_settings_module_not_in_team():
    """Единственное место волны, где правился уже существующий store-модуль. Функция физически
    лежала в хвосте секции деск-входа, но её домен — настройки: DEFAULT_TEXT_SETTINGS живёт
    в том же модуле, и импортировать его в team.py значило бы расщепить домен настроек."""
    src = (REPO / "app" / "db.py").read_text(encoding="utf-8")
    assert "from .store.settings import get_text_setting  # noqa" in src
    idx_one = src.index("from .store.settings import get_text_setting ")
    idx_many = src.index("from .store.settings import get_text_settings ")
    assert idx_one < idx_many, "строка должна стоять в алфавитной группе settings общего фасада"
    team_tree = ast.parse((REPO / "app" / "store" / "team.py").read_text(encoding="utf-8"))
    team_defs = {n.name for n in team_tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "get_text_setting" not in team_defs, "функция уехала не в тот модуль"
    assert store_settings.get_text_setting.__module__ == "app.store.settings"


def test_jload_facade_line_survived_without_callers_in_dbpy():
    """Все три вызова _jload внутри db.py уехали волной 6 (уведомления + паспорт калибровки),
    но фасадная строка остаётся: это публичный интерфейс слоя данных, и wave2-тест требует
    `db._jload is core._jload`. Шапка над строкой обязана была перестать врать."""
    src = (REPO / "app" / "db.py").read_text(encoding="utf-8")
    assert "from .store.core import _jload" in src
    assert "_jload(" not in src, "внутри db.py вызовов _jload больше нет"
    assert "ПОКА ЭТИ ВЫЗОВЫ ЖИВЫ" not in src, "ложная шапка фасада _jload не переписана"


@pytest.mark.parametrize("mod", NEW_MODULES)
def test_store_module_does_not_import_db(mod):
    """Разрез односторонний: доменный модуль не тянет app.db ни на каком уровне.
    Для crm это ещё и проверка цепочки .profile -> .money/.orders, для calib — .printjournal."""
    code = f"import app.store.{mod}, sys; print('app.db' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False", f"app.store.{mod} подтянул app.db"


@pytest.mark.parametrize("mod", [*NEW_MODULES, "settings"])
def test_no_lazy_relative_imports_inside_bodies(mod):
    """Ленивый `from .X import y` внутри функции после переезда указывал бы на пакет store,
    а не на app (грабли endgame). В волне 6 таких не было ни одного — закрепляем.
    `import datetime as _dt` внутри crm.ensure_birthday_tasks — stdlib, правилом не задет."""
    src = (REPO / "app" / "store" / f"{mod}.py").read_text(encoding="utf-8")
    assert not re.search(r"^[ \t]+from \.", src, re.M), f"в app/store/{mod}.py есть ленивый `from .`"


def test_calib_takes_step_row_from_printjournal_not_from_db():
    """Долг волны 3: `_calib_step_row` уехал в printjournal вместе с мостом журнал<->калибровка.
    list_calib_steps обязана импортировать его store->store, а не через фасад db."""
    src = (REPO / "app" / "store" / "calib.py").read_text(encoding="utf-8")
    assert "from .printjournal import _calib_step_row" in src
    from app.store import printjournal
    assert calib._calib_step_row is printjournal._calib_step_row is db._calib_step_row


async def test_referral_bonus_is_paid_exactly_once(tmp_db):
    """Смок инцидентной зоны: право на выплату захватывается ОДНИМ условным UPDATE
    `WHERE rewarded = 0`, поэтому второй вызов не платит ничего (до 2026-07-16 платил)."""
    referee, referrer = 6001, 6002
    assert await crm.add_referral(referee, referrer) is True
    assert await crm.add_referral(referee, 6003) is False        # повторная привязка запрещена
    assert await crm.award_referral_if_due(referee, 5.0) is None  # заказа ещё нет — не время
    rid = await tmp_db.add_request(
        user_id=referee, user_name="реферал", model_label="волна 6", printer="",
        plastic_name="PLA", quantity=1, quality="q", strength="s", grams_total=1,
        minutes_total=1, price=1.0, preview_path=None)
    await tmp_db.update_request(rid, status="printed", paid=1)
    assert await crm.award_referral_if_due(referee, 5.0) == referrer
    assert await db.award_referral_if_due(referee, 5.0) is None   # тот же путь через фасад
    from app.store import money
    assert await money.get_balance(referee) == 5.0
    assert await money.get_balance(referrer) == 5.0
    assert (await crm.get_referral(referee))["rewarded"] == 1
    await tmp_db.delete_request(rid)


async def test_release_note_publishes_only_once(tmp_db):
    """Смок антидубля: try_mark_published переводит new -> published одним условным UPDATE,
    поэтому повторный тап «Опубликовать» рассылку не запустит."""
    nid = await ops.add_admin_notification("release", title="волна 6", body="смок",
                                           payload={"k": 1})
    assert await ops.try_mark_published(nid) is True
    assert await ops.try_mark_published(nid) is False
    assert await db.try_mark_published(nid) is False              # тот же путь через фасад
    got = await ops.get_admin_notification(nid)
    assert got["status"] == "published" and got["payload"] == {"k": 1}   # _jload из core
    await ops.delete_admin_notification(nid)


async def test_desk_login_code_is_single_use(tmp_db):
    """Смок одноразовости кода входа в деск: «прочитал и сразу удалил» условным DELETE."""
    await team.add_desk_code("W6CODE", 6100, 1_000, 9_999_999_999)
    row = await team.take_desk_code("W6CODE", 1_001)
    assert row is not None and row["user_id"] == 6100
    assert await team.take_desk_code("W6CODE", 1_001) is None
    assert await db.take_desk_code("W6CODE", 1_001) is None       # тот же путь через фасад
    await team.add_desk_code("W6STALE", 6100, 1_000, 1_500)
    assert await team.take_desk_code("W6STALE", 2_000) is None, "просроченный код не пускает"
    await team.drop_desk_codes(6100)


async def test_last_item_cannot_be_sold_twice(tmp_db):
    """Смок гонки витрины: наличие проверяется условием `stock >= qty` прямо в UPDATE,
    поэтому второй покупатель последней штуки получает False, а не отрицательный остаток."""
    pid = await products.add_product(title="Смок-изделие", price=10.0, stock=1)
    assert await products.buy_product(pid) is True
    assert await products.buy_product(pid) is False
    assert await db.buy_product(pid) is False                     # тот же путь через фасад
    item = await products.get_product(pid)
    assert item.stock == 0 and item.sold == 1
    await products.delete_product(pid)


async def test_calib_run_seeds_steps_and_overview_counts(tmp_db):
    """Смок связки кампания+шаги: add_calib_run сразу заводит шаги через seed_calib_steps,
    а delete_calib_run уносит их вместе с кампанией."""
    rid = await calib.add_calib_run(title="Смок-кампания", reason="волна 6",
                                    steps=["flow", "pa"])
    steps = await calib.list_calib_steps(rid)
    assert [s["test_key"] for s in steps] == ["flow", "pa"]
    assert all("values" in s for s in steps), "_calib_step_row из printjournal не отработал"
    assert (await calib.calib_overview())["active"] >= 1
    assert await calib.seed_calib_steps(rid, ["flow", "pa"]) == 0        # идемпотентно
    assert await calib.seed_calib_steps(rid, ["temp"]) == 1
    assert [s["test_key"] for s in await db.list_calib_steps(rid)] == ["temp", "flow", "pa"]
    await calib.delete_calib_run(rid)
    assert await calib.get_calib_run(rid) is None
    assert await calib.list_calib_steps(rid) == []


async def test_kb_article_roundtrip_and_drafts_hidden(tmp_db):
    """Смок базы знаний: черновик не виден на экране, публикация его показывает."""
    aid = await kb.kb_save(None, "Смок-статья", "текст", sort=3, published=False)
    assert [a["id"] for a in await kb.kb_list(published_only=True)] == []
    assert aid in [a["id"] for a in await kb.kb_list(published_only=False)]
    assert await kb.kb_save(aid, "Смок-статья", "текст", sort=3, published=True) == aid
    assert aid in [a["id"] for a in await db.kb_list(published_only=True)]   # через фасад
    got = await kb.kb_get(aid)
    assert got["title"] == "Смок-статья" and got["sort"] == 3
    await kb.kb_delete(aid)
    assert await kb.kb_get(aid) is None


async def test_text_setting_falls_back_to_default(tmp_db):
    """Смок дозаписанной функции: значение из text_settings, иначе DEFAULT_TEXT_SETTINGS,
    иначе переданный default. Константа живёт в том же модуле — импорта не потребовалось."""
    key = next(iter(store_settings.DEFAULT_TEXT_SETTINGS))
    await store_settings.set_text_setting(key, "смок-значение")
    assert await store_settings.get_text_setting(key) == "смок-значение"
    assert await db.get_text_setting(key) == "смок-значение"      # тот же путь через фасад
    assert await store_settings.get_text_setting("нет-такого-ключа", "запасное") == "запасное"


async def test_crm_card_pulls_referrals_and_balance_across_modules(tmp_db):
    """Смок кросс-модульных рёбер карточки: count_referrals из store/profile и get_balance
    из store/money теперь импортируются ЯВНО, а не резолвятся через фасад db."""
    uid = 6200
    rid = await tmp_db.add_request(
        user_id=uid, user_name="клиент", model_label="волна 6", printer="", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=12.0, preview_path=None)
    await crm.upsert_customer(uid, name="Смок-клиент", note="волна 6")
    card = await crm.customer_card(uid)
    assert card is not None and card["name"] == "Смок-клиент"
    assert card["orders"] == 1 and card["referrals"] == 0 and card["balance"] == 0.0
    assert "new" in card["segments"]
    assert (await db.customer_card(uid))["note"] == "волна 6"     # тот же путь через фасад
    assert uid in [c["uid"] for c in await crm.list_customers_crm()]
    await tmp_db.delete_request(rid)
