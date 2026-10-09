"""Витрина и сквозные middleware после волны 8 endgame: фасады, границы, _storage.

Последняя волна свободной программы (1–11): клиентские страницы/каталог — в
app/web/storefront.py, три сквозные middleware — в app/webguard.py к _caps_mw/_edition_mw.
"""
import ast
import io
import pathlib
import subprocess
import sys

import pytest

from app import webapp, webguard
from app.web import storefront

REPO = pathlib.Path(__file__).resolve().parents[1]

FACADE = ("_api_collection", "_api_collection_search", "_api_favorite", "_api_feed",
          "_api_model_search", "_api_prefs", "_api_recommend", "_api_thumb", "_card",
          "_catalog_page", "_collection_item_json", "_collection_page", "_image",
          "_plastic_card", "_plastic_image", "_spool_image", "_thumb_proxy",
          # волна 13: идеи клиентов
          "_api_idea",
          # копия базы на VPS (контракт §7): сборщики, общие с пакетом каталога приёмной
          "build_catalog_categories", "feed_item_json")
# Gridfinity уехал целиком в app/web/gridfinity.py («Мастерская органайзеров»): в витрине
# от него осталось только подмешивание карточки в ленту (_GF_FEED_SHOWN внутри _api_feed) —
# это логика ЛЕНТЫ, а не конфигуратора. Фасад нового модуля сторожит test_gridfinity_facade.py.

MW = ("_error_mw", "_compress_mw", "_usage_mw")

# НЕ уехали: единственные читатели _storage — их дом решается волнами 12+.
STAYED = ("_pick", "_pick_plastic")


@pytest.mark.parametrize("name", FACADE)
def test_storefront_facade_reexports(name):
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(storefront, name)


@pytest.mark.parametrize("name", MW)
def test_middleware_moved_to_webguard(name):
    """Middleware живут в webguard (к _caps_mw/_edition_mw) и реэкспортируются фасадом —
    make_app собирает список по фасадным именам, порядок не менялся."""
    assert getattr(webapp, name) is getattr(webguard, name)


def test_facade_covers_every_public_name_of_the_module():
    src = io.open(REPO / "app" / "web" / "storefront.py", encoding="utf-8").read()
    defined = {n.name for n in ast.parse(src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined - {"set_bot"} == set(FACADE), (
        f"в модуле есть, а в фасаде нет: {sorted(defined - set(FACADE))}; "
        f"в фасаде есть, а в модуле нет: {sorted(set(FACADE) - defined)}")


def test_no_leftover_definitions_and_storage_readers_stayed():
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    defs = {n.name for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    own = defs & (set(FACADE) | set(MW))
    assert not own, f"в webapp.py остались определения: {sorted(own)}"
    missing = set(STAYED) - defs
    assert not missing, (
        f"{sorted(missing)} пропали из webapp: единственные читатели _storage, их переезд — "
        "отдельное решение (волны 12+), а не волна 8")
    for name in STAYED:
        assert not hasattr(storefront, name), f"{name} утащили в storefront вместе с _storage"


def test_storefront_does_not_import_webapp():
    code = "import app.web.storefront, sys; print(int('app.webapp' in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0"], f"app.web.storefront подтянул webapp: {out}"


def test_middleware_order_in_make_app_unchanged():
    """Порядок middleware приложения заморожен: _local_mw первым, дальше usage → error →
    compress → edition → caps. Перенос домов порядок не менял — он и есть контракт."""
    app = webapp.make_app()
    names = [getattr(m, "__name__", str(m)) for m in app.middlewares]
    assert names == ["_local_mw", "_usage_mw", "_error_mw", "_compress_mw",
                     "_edition_mw", "_caps_mw"], names


def test_module_holds_a_bot_but_no_models():
    """Волна 13: держатель бота (идея клиента уведомляет мастера в Telegram); ставит его
    webapp.set_bot веером _BOT_FAN. Конвейер сметы сюда по-прежнему не относится."""
    assert hasattr(storefront, "_bot") and callable(storefront.set_bot)
    for name in ("_storage", "_models", "_register_full", "_make_token"):
        assert not hasattr(storefront, name), (
            f"в storefront появился {name} — конвейер сметы уезжает волнами 14/15, не волной 8")
