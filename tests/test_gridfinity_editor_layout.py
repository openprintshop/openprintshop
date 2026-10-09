"""Редактор модуля на узком экране: сцена не должна схлопываться под панелью.

31.08.2026 редактор открыли в мини-аппе (окно 500 px) и не увидели САМ МОДУЛЬ:
над списком вставок не было ничего. Замер в браузере: `grid-template-rows` тела редактора
посчитался как `0px 667px` — вся высота ушла строке панели, сцене осталось ноль.

Причина — `max-height:44%` у панели. Процент НЕ участвует в измерении `auto`-строки грида
(высота строки в этот момент и вычисляется, считать процент не от чего), поэтому строка
бралась по содержимому панели (688 px против 667 px тела) и `1fr` сцены обнулялся. Вторым
проходом панель всё же обрезалась до 44 %, так что под ней оставалась пустота, а 3D не было
вовсе — ни ошибки, ни строки в консоли.

Прибор дешёвый и грубый, как tests/test_webui_tip_emoji.py: он стережёт ровно ту подмену,
которая уже один раз стоила фичи. Настоящая проверка вёрстки требует браузера с раскладкой
(измерялось вручную в iframe 500×760 и 360×640) — здесь фиксируется условие, при котором
измерение вообще имеет смысл.
"""
import re
from pathlib import Path

from app import config

_ENGINE = Path(config.__file__).parent / "webui" / "gridfinity_engine.js"


def _mobile_rules() -> str:
    """Блок @media (max-width:820px) целиком — от `{` медиазапроса до парной `}`.

    Считаем скобки, а не ищем известную строку: правил в блоке становится больше (полоса
    сворачивания добавила три), и привязка к «последнему известному селектору» ломала бы
    прибор при каждой такой правке.
    """
    src = _ENGINE.read_text(encoding="utf-8")
    i = src.index("@media (max-width:820px){.g-ed-body")
    depth, j = 0, src.index("{", i)
    for j in range(j, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("медиазапрос редактора не закрыт")


def test_editor_panel_height_is_capped_in_absolute_units():
    rules = _mobile_rules()
    m = re.search(r"\.g-ed-side\{[^}]*max-height:([^;}]+)", rules)
    assert m, "у панели редактора пропал потолок высоты — строка съест всю сцену"
    cap = m.group(1).strip()
    assert not cap.endswith("%"), (
        f"потолок панели снова в процентах ({cap}): в измерении auto-строки он не"
        " участвует, и сцена схлопнется в 0 — так модуль и пропал 31.08.2026")


def test_editor_stage_row_has_a_floor():
    rules = _mobile_rules()
    m = re.search(r"grid-template-rows:([^;}]+)", rules)
    assert m, "у тела редактора пропали строки грида"
    rows = m.group(1).strip()
    assert rows.startswith("minmax("), (
        f"строка сцены снова без пола ({rows}): голый 1fr обнуляется, как только панель"
        " дорастёт до высоты тела")


# ───────────────────────────── полоса сворачивания панели ─────────────────────────────

def test_fold_bar_is_mobile_only():
    """На ПК панель — колонка справа, сцене она не мешает: полосы там быть не должно.

    Правило `display:none` обязано стоять в БАЗОВОМ объявлении, а не в медиазапросе:
    сетка широкого макета — две колонки, и третий видимый ребёнок уехал бы в новую строку
    под сценой, отъев у неё высоту (ровно тот же класс поломки, что чинится выше)."""
    src = _ENGINE.read_text(encoding="utf-8")
    base = src[:src.index("@media (max-width:820px){.g-ed-body")]
    assert re.search(r"\.g-ed-fold\{display:none", base), \
        "полоса сворачивания перестала прятаться на широком экране"
    assert ".g-ed-fold{display:flex}" in _mobile_rules(), \
        "на узком экране полосу нечем нажать — она не показывается"


def test_folded_panel_leaves_its_row_to_the_scene():
    rules = _mobile_rules()
    assert ".g-ed-body.fold .g-ed-side{display:none}" in rules, (
        "свёрнутая панель обязана уходить из потока: иначе её строка `auto` останется"
        " в сетке и сцена не получит освободившееся место")


def test_fold_button_lives_outside_the_repainted_panel():
    """edPanel() перерисовывает #gfxE3Side целиком — кнопка внутри теряла бы обработчик."""
    src = _ENGINE.read_text(encoding="utf-8")
    markup = src[src.index("'<div class=\"g-ed-body\">"):src.index("document.body.appendChild(edHost)")]
    assert 'id="gfxE3Fold"' in markup, "кнопка сворачивания пропала из разметки редактора"
    assert markup.index('id="gfxE3Fold"') < markup.index('id="gfxE3Side"'), \
        "кнопка должна стоять ДО панели — она её заголовок, а не содержимое"
