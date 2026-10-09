"""Волна 5 разреза db.py: клиентское живёт в app/store/{collection,profile,chat}.

Контракты волны:
  * фасад db реэкспортирует ТОТ ЖЕ объект на каждое из 86 имён (потребители зовут `db.<имя>`,
    тесты подменяют `monkeypatch.setattr(db, ...)`);
  * модульные константы (_WORD_RE, _MSG_COLS, PRICE_MODES, PRICE_PCT_MAX, _PREFS_DEFAULT)
    реэкспортируются тем же объектом, а не копией;
  * в db.py не осталось повторных определений этих имён, а осиротевшие после выреза
    импорты шапки (functools/random/re) сняты — при этом aiosqlite/Path остались, они
    живут в аннотациях под `from __future__ import annotations`;
  * доменные модули НЕ импортируют app.db (разрез односторонний) и не прячут ленивых `from .`;
  * count_referrals доступен из db как раньше: остающаяся в db.py customer_card зовёт его
    голым именем, значит фасадная строка обязана дожить до волны 6;
  * поведение не изменилось: категории каталога переименовываются вместе со строками
    collection одним commit-ом, корзина клиента переживает круг save/get/clear, а
    tg_message_link по-прежнему сшивает обе таблицы переписки одним JOIN-ом.
"""
import ast
import pathlib
import re
import subprocess
import sys

import pytest

from app import db
from app.store import chat, collection, profile

REPO = pathlib.Path(__file__).resolve().parents[1]

COLLECTION_NAMES = ("_to_collection", "add_to_collection", "count_collection", "list_collection",
                    "list_collection_by_category", "_WORD_RE", "_tokenize", "_fuzzy_threshold",
                    "_levenshtein_le", "_token_fuzzy_match", "_fuzzy_vocab_matches",
                    "_row_fuzzy_hit", "search_collection", "get_collection_item", "set_favorite",
                    "list_favorite_ids", "list_favorites", "list_collection_categories",
                    "upsert_collection_category", "set_collection_category_sort",
                    "set_collection_category", "count_collection_by_category",
                    "rename_collection_category", "delete_collection_category",
                    "random_collection_items", "user_fav_categories", "recommend_page",
                    "collection_last_minutes", "get_collection_id_by_hash", "set_collection_photo",
                    "set_collection_label", "delete_collection_item", "add_calc_queue",
                    "list_calc_queue", "count_calc_queue", "get_calc_queue", "resolve_calc_queue",
                    "get_calc_cache", "put_calc_cache")
PROFILE_NAMES = ("set_user_avatar", "get_user_avatar", "PRICE_MODES", "PRICE_PCT_MAX",
                 "_PREFS_DEFAULT", "get_user_prefs", "set_user_delivery_prefs",
                 "set_user_price_prefs", "price_auto_accept", "count_referrals",
                 "seen_achievements", "mark_achievements_seen", "xp_events", "user_profile_stats",
                 "save_user_cart", "clear_user_cart", "get_user_cart", "stale_carts",
                 "mark_cart_nudged", "abandoned_calcs", "mark_calc_nudged", "_to_saved",
                 "count_saved_models", "list_saved_models", "get_saved_model",
                 "get_saved_model_by_hash", "delete_saved_model", "upsert_saved_model", "add_idea")
CHAT_NAMES = ("_msg_user_id", "add_order_message", "add_client_message", "_MSG_COLS", "_msg_row",
              "list_order_messages", "list_client_messages", "mark_order_read", "mark_client_read",
              "clients_unread_admin", "get_message", "set_message_image", "link_tg_message",
              "tg_message_link", "prune_tg_links", "search_messages", "orders_unread_user",
              "orders_unread_admin")

MOVED = ([(collection, n) for n in COLLECTION_NAMES]
         + [(profile, n) for n in PROFILE_NAMES]
         + [(chat, n) for n in CHAT_NAMES])
ALL_NAMES = set(COLLECTION_NAMES) | set(PROFILE_NAMES) | set(CHAT_NAMES)


@pytest.mark.parametrize(("module", "name"), MOVED, ids=[n for _, n in MOVED])
def test_facade_reexports_the_same_object(module, name):
    assert hasattr(db, name), f"имя {name} потеряно в фасаде app/db.py"
    assert getattr(db, name) is getattr(module, name)


def test_module_handles_are_reachable_from_db():
    """Хэндлы модулей в db (метод endgame). Имена collection/profile/chat в db.py ни с чем
    не сталкиваются — раньше они встречались там только внутри строк SQL."""
    assert db.collection is collection and db.profile is profile and db.chat is chat


def test_module_constants_are_the_same_objects():
    """Модульные константы реэкспортируются тем же объектом, а не копией."""
    assert db._WORD_RE is collection._WORD_RE
    assert db._MSG_COLS is chat._MSG_COLS and isinstance(chat._MSG_COLS, str)
    assert db.PRICE_MODES is profile.PRICE_MODES
    assert db.PRICE_PCT_MAX == profile.PRICE_PCT_MAX
    assert db._PREFS_DEFAULT is profile._PREFS_DEFAULT


def test_fuzzy_cache_decorators_survived_the_move():
    """@functools.lru_cache стоит СТРОКОЙ ВЫШЕ def — вырез обязан был взять и её,
    иначе кэш фаззи-поиска потерялся бы молча, а сверка дословности прошла."""
    assert hasattr(collection._tokenize, "cache_info")
    assert hasattr(collection._levenshtein_le, "cache_info")


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


def test_orphaned_header_imports_removed_but_annotation_ones_kept():
    """functools/random/re в db.py держали только уехавшие тела (lru_cache фаззи-поиска,
    random.Random в recommend_page, re.compile в _WORD_RE) — сняты. aiosqlite и Path
    удалять было НЕЛЬЗЯ: они живут в аннотациях, и symtable их «не видит»."""
    src = (REPO / "app" / "db.py").read_text(encoding="utf-8")
    for dead in ("import functools", "import random", "import re"):
        assert not re.search(rf"^{dead}$", src, re.M), f"{dead} остался в db.py мёртвым"
    assert re.search(r"^import aiosqlite$", src, re.M)
    assert re.search(r"^from pathlib import Path$", src, re.M)
    assert "aiosqlite.Connection" in src and "Path" in src


def test_count_referrals_facade_outlived_this_wave():
    """Инвариант волны 5 ВЫПОЛНЕН, и волна 6 его закрыла.

    Волной 5 рефералка была расщеплена: читающая count_referrals уехала в store/profile
    (её звал голым именем user_profile_stats), а customer_card и блок выплат остались в
    db.py — фасадная строка обязана была дожить до волны 6. Волна 6 увезла их в
    store/crm.py, и теперь count_referrals там импортируется ЯВНО из .profile, а не
    резолвится через фасад db. Сама фасадная строка в db.py остаётся: это публичный
    интерфейс слоя данных (`db.count_referrals`), а не переходный костыль."""
    src = (REPO / "app" / "db.py").read_text(encoding="utf-8")
    crm_src = (REPO / "app" / "store" / "crm.py").read_text(encoding="utf-8")
    assert "from .store.profile import count_referrals" in src
    assert "from .profile import count_referrals" in crm_src, "явного импорта нет — цикл через db"
    assert "await count_referrals(" in crm_src
    assert "async def award_referral_if_due" in crm_src, "блок выплат уехал волной 6 в crm"
    assert "async def award_referral_if_due" not in src


@pytest.mark.parametrize("mod", ["collection", "profile", "chat"])
def test_store_module_does_not_import_db(mod):
    """Разрез односторонний: доменный модуль не тянет app.db ни на каком уровне.
    Для profile это ещё и проверка цепочки .orders/.money — соседи по store не ведут обратно."""
    code = f"import app.store.{mod}, sys; print('app.db' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False", f"app.store.{mod} подтянул app.db"


@pytest.mark.parametrize("mod", ["collection", "profile", "chat"])
def test_no_lazy_relative_imports_inside_bodies(mod):
    """Ленивый `from .X import y` внутри функции после переезда указывал бы на пакет store,
    а не на app (грабли endgame). В волне 5 таких не было ни одного — закрепляем.
    `import time as _t` внутри add_idea — stdlib, правилом не задет."""
    src = (REPO / "app" / "store" / f"{mod}.py").read_text(encoding="utf-8")
    assert not re.search(r"^[ \t]+from \.", src, re.M), f"в app/store/{mod}.py есть ленивый `from .`"


async def test_category_rename_moves_rows_and_row(tmp_db):
    """Смок транзакционной связки каталога: переименование раздела одним commit-ом правит и
    справочник collection_categories, и колонку category у строк самой коллекции."""
    cid = await collection.add_to_collection(
        hash="w5-cat", label="Смок-модель", paths=["a.stl"], preview_path=None, added_by=1)
    await collection.upsert_collection_category("Смок-раздел", sort=7)
    await collection.set_collection_category(cid, "Смок-раздел")
    assert await collection.count_collection_by_category("Смок-раздел") == 1
    await collection.rename_collection_category("Смок-раздел", "Смок-раздел-2")
    assert await collection.count_collection_by_category("Смок-раздел") == 0
    assert await collection.count_collection_by_category("Смок-раздел-2") == 1
    names = {n: s for n, _cnt, _top, s in await collection.list_collection_categories()}
    assert names.get("Смок-раздел-2") == 7 and "Смок-раздел" not in names
    assert await db.get_collection_id_by_hash("w5-cat") == cid   # тот же путь через фасад
    await collection.delete_collection_item(cid)


async def test_fuzzy_search_and_favorite(tmp_db):
    """Смок фаззи-поиска и избранного: опечатка в запросе всё равно находит модель,
    а сердечко идемпотентно."""
    cid = await collection.add_to_collection(
        hash="w5-fuzzy", label="Подставка", paths=["b.stl"], preview_path=None, added_by=2)
    hits = await collection.search_collection("подствка")
    assert cid in [it.id for it in hits], "фаззи-бакет поиска потерялся при переезде"
    await collection.set_favorite(3, cid, True)
    await collection.set_favorite(3, cid, True)
    assert await collection.list_favorite_ids(3) == [cid]
    assert [it.id for it in await db.list_favorites(3)] == [cid]   # тот же путь через фасад
    await collection.set_favorite(3, cid, False)
    assert await collection.list_favorite_ids(3) == []
    await collection.delete_collection_item(cid)


async def test_cart_and_prefs_roundtrip(tmp_db):
    """Смок профиля: личные настройки цены сохраняются и читаются, серверная копия корзины
    переживает круг save/get/clear, а count_referrals виден и напрямую, и через фасад."""
    prefs = await profile.set_user_price_prefs(901, "pct", 15.0)
    assert prefs["price_mode"] == "pct" and prefs["price_pct"] == 15.0
    assert await profile.get_user_prefs(901) == prefs
    assert profile.price_auto_accept(prefs, 100.0, 110.0) is True
    assert profile.price_auto_accept(prefs, 100.0, 130.0) is False
    await profile.save_user_cart(901, [{"id": 1}], "12.50")
    cart = await profile.get_user_cart(901)
    assert cart["items"] == [{"id": 1}] and cart["total"] == "12.50"
    await profile.clear_user_cart(901)
    assert await profile.get_user_cart(901) is None
    assert await profile.count_referrals(901) == 0
    assert await db.count_referrals(901) == 0                     # тот же путь через фасад
    stats = await profile.user_profile_stats(901)
    assert stats["referrals"] == 0 and stats["orders"] == 0


async def test_saved_models_history_is_trimmed(tmp_db):
    """Смок сохранённых моделей: upsert одним commit-ом обновляет запись и режет историю
    до keep штук — связка SELECT + UPDATE|INSERT + DELETE переехала целиком."""
    for i in range(3):
        await profile.upsert_saved_model(
            user_id=902, hash=f"w5-saved-{i}", label=f"м{i}", paths=[f"{i}.stl"],
            preview_path=None, printer_id=None, plastic_id=None, quality_id=None,
            strength_id=None, quantity=1, keep=2)
    assert await profile.count_saved_models(902) == 2             # DELETE-обрезка сработала
    alive = (await profile.list_saved_models(902))[0]
    sid = await profile.upsert_saved_model(
        user_id=902, hash=alive.hash, label="м-новая", paths=["x.stl"], preview_path=None,
        printer_id=None, plastic_id=None, quality_id=None, strength_id=None, quantity=4, keep=2)
    assert await profile.count_saved_models(902) == 2             # это UPDATE, а не INSERT
    got = await profile.get_saved_model_by_hash(902, alive.hash)
    assert got is not None and got.id == sid == alive.id
    assert got.label == "м-новая" and got.quantity == 4
    assert (await db.get_saved_model(sid)).label == "м-новая"     # тот же путь через фасад
    for m in await profile.list_saved_models(902):
        await profile.delete_saved_model(m.id)


async def test_chat_thread_and_tg_link(tmp_db):
    """Смок переписки: сообщение клиента считается непрочитанным у мастера, отметка снимает
    счётчик, а карта tg_chat_msgs находит строку переписки JOIN-ом по обеим таблицам."""
    rid = await tmp_db.add_request(
        user_id=903, user_name="смок", model_label="волна 5", printer="", plastic_name="PLA",
        quantity=1, quality="q", strength="s", grams_total=1, minutes_total=1,
        price=1.0, preview_path=None)
    mid = await chat.add_order_message(rid, False, "привет, мастер")
    assert (await chat.orders_unread_admin()).get(rid) == 1
    assert [m["text"] for m in await chat.list_order_messages(rid)] == ["привет, мастер"]
    await chat.link_tg_message(-100, 555, mid)
    link = await chat.tg_message_link(-100, 555)
    assert link is not None and link["id"] == mid and link["request_id"] == rid
    assert await db.tg_message_link(-100, 555) == link            # тот же путь через фасад
    await chat.mark_order_read(rid, by_admin=True)
    assert rid not in await chat.orders_unread_admin()
    assert [m["id"] for m in await chat.search_messages("привет, мастер")] == [mid]
    await tmp_db.delete_request(rid)
    assert await chat.tg_message_link(-100, 555) is None          # висячая ссылка не «живая»
