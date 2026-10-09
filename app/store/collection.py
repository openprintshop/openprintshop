"""Общая коллекция моделей: каталог, категории, фаззи-поиск, избранное, рекомендации.

Кросс-пользовательский каталог (таблица collection) со всем, что вокруг него выросло:

* КАТАЛОГ. add_to_collection — единственный писатель строки модели (upsert по hash с
  инкрементом uses), _to_collection — единственный читатель-конвертер в CollectionItem.
* КАТЕГОРИИ (collection_categories) — ТРАНЗАКЦИОННАЯ СВЯЗКА с каталогом:
  rename_collection_category и delete_collection_category одним commit-ом правят и
  справочник категорий, и строки collection (перепривязка/обнуление category). Резать
  «категории» отдельным модулем нельзя.
* ФАЗЗИ-ПОИСК (OpenTodo c52f32b4) — своя реализация без внешних зависимостей: _WORD_RE,
  токенизация, порог по длине токена, ограниченный Левенштейн. _tokenize и
  _levenshtein_le несут @functools.lru_cache — декораторы перенесены вместе с телами.
* ИЗБРАННОЕ (collection_fav) и РЕКОМЕНДАЦИИ (recommend_page: детерминированный по дню
  random.Random, «своё/чужое» вперемешку).
* ДВА ПРИМЫКАЮЩИХ КЭША КАТАЛОГА: очередь разбора незагрузившихся моделей (calc_queue,
  OpenTodo 26196ac1) и кэш результата слайса каталожной модели (calc_cache,
  OpenTodo ae038968) — оба живут вокруг hash модели из этой же коллекции.

Выделено из app/db.py волной 5 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО, правок тел НЕТ ВОВСЕ: ни ленивых импортов, ни global, ни
квалификаций. Замыкание пустое — ни одно тело не ссылается на остающееся в db.py
top-level имя. Логгер не заводится: ни одно из 39 тел не пишет в лог.

Модуль АВТОНОМЕН: рёбер к store/profile.py и store/chat.py (соседи по волне) нет ни
одного; из store нужны только core (_conn/_jload) и models (CollectionItem).

ШАПКА DB.PY ПОХУДЕЛА: functools/random/re были в db.py ради ЭТИХ тел и только их
(сплошной греп: functools — два lru_cache здесь, random — recommend_page, re — _WORD_RE),
поэтому три строки импорта из db.py удалены и заведены тут. `import aiosqlite` и
`from pathlib import Path` в db.py трогать нельзя, хотя symtable их «не видит»: они живут
в АННОТАЦИЯХ под `from __future__ import annotations`. По той же причине aiosqlite нужен
здесь — ради `_to_collection(r: aiosqlite.Row)`.
"""
from __future__ import annotations

import functools
import random
import re
import time

import aiosqlite

from .core import _abs, _abs_list, _conn, _rel, _rel_list
from .models import CollectionItem


# ---- общая коллекция моделей (кросс-пользовательская) ----
def _to_collection(r: aiosqlite.Row) -> CollectionItem:
    return CollectionItem(
        id=r["id"], hash=r["hash"], label=r["label"],
        paths=_abs_list(r["paths"]), preview_path=_abs(r["preview_path"]),
        photo_path=_abs(r["photo_path"]), added_by=r["added_by"], uses=r["uses"], created_at=r["created_at"],
        palette=(r["palette"] if "palette" in r.keys() else "") or "",
        dims=(r["dims"] if "dims" in r.keys() else "") or "",
        size=(r["size"] if "size" in r.keys() else 0.0) or 0.0,
        last_material=(r["last_material"] if "last_material" in r.keys() else "") or "",
        last_calc_at=(r["last_calc_at"] if "last_calc_at" in r.keys() else 0) or 0,
        multicolor=(r["multicolor"] if "multicolor" in r.keys() else 0) or 0,
        plates=(r["plates"] if "plates" in r.keys() else 1) or 1,
        last_price=(r["last_price"] if "last_price" in r.keys() else 0.0) or 0.0,
        category=(r["category"] if "category" in r.keys() else "") or "",
        tags=(r["tags"] if "tags" in r.keys() else "") or "",
        orig_label=(r["orig_label"] if "orig_label" in r.keys() else "") or "")


async def add_to_collection(*, hash: str, label: str, paths: list, preview_path: str | None,
                            added_by: int | None, dims: str = "", size: float = 0.0,
                            material: str = "", multicolor: int = 0, plates: int = 1,
                            price: float = 0.0) -> int:
    now = int(time.time())
    cur = await _conn().execute("SELECT id FROM collection WHERE hash = ?", (hash,))
    row = await cur.fetchone()
    if row:
        # счётчик +1, фиксируем «последний расчёт»; габариты/размер ставим, если ещё не было
        await _conn().execute(
            "UPDATE collection SET uses = uses + 1, last_calc_at = ?, last_material = ?, "
            "multicolor = ?, plates = ?, dims = COALESCE(NULLIF(dims, ''), ?), "
            "size = CASE WHEN size > 0 THEN size ELSE ? END, "
            "last_price = CASE WHEN ? > 0 THEN ? ELSE last_price END WHERE id = ?",
            (now, material, int(multicolor), int(plates), dims, size,
             float(price or 0), float(price or 0), row["id"]))
        await _conn().commit()
        return row["id"]
    c2 = await _conn().execute(
        "INSERT INTO collection(hash, label, paths, preview_path, photo_path, added_by, uses, "
        "created_at, dims, size, last_material, last_calc_at, multicolor, plates, last_price) "
        "VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)",
        (hash, label, _rel_list(paths), _rel(preview_path), None, added_by, now,
         dims, size, material, now, int(multicolor), int(plates), float(price or 0)))
    await _conn().commit()
    return c2.lastrowid


async def count_collection() -> int:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM collection")
    return (await cur.fetchone())["c"]


async def list_collection(limit: int = 30) -> list[CollectionItem]:
    cur = await _conn().execute(
        "SELECT * FROM collection ORDER BY uses DESC, created_at DESC LIMIT ?", (limit,))
    return [_to_collection(r) for r in await cur.fetchall()]


async def list_collection_by_category(category: str, limit: int = 60,
                                      offset: int = 0) -> list[CollectionItem]:
    """Страница раздела каталога (c9c42789): разделы из ВК содержат сотни моделей,
    поэтому пагинация обязательна."""
    cur = await _conn().execute(
        "SELECT * FROM collection WHERE category = ? "
        "ORDER BY uses DESC, created_at DESC LIMIT ? OFFSET ?",
        ((category or "").strip(), limit, offset))
    return [_to_collection(r) for r in await cur.fetchall()]


# --- Фаззи-поиск по каталогу (OpenTodo c52f32b4): опечатки в запросе не должны давать
# «пусто». Своя реализация без внешних зависимостей (rapidfuzz и т.п. — не добавляем).

_WORD_RE = re.compile(r"[^\W\d_]+")  # последовательности букв (юникод), цифры/подчёркивание — не токены


@functools.lru_cache(maxsize=20000)
def _tokenize(text: str) -> tuple[str, ...]:
    """label/tags/orig_label -> кортеж буквенных токенов в нижнем регистре (разбивка по
    любым не-буквам: пробел, дефис, запятая...). lru_cache: между запросами каталог почти
    не меняется — один и тот же label/tags встречается в результатах search_collection
    раз за разом, повторная токенизация того же текста не нужна."""
    return tuple(_WORD_RE.findall(text.lower()))


def _fuzzy_threshold(token_len: int) -> int:
    """Порог опечаток для токена запроса такой длины: короче 4 букв — только точное
    совпадение (0 = фаззи выключен), 4-7 букв — 1 правка, 8+ — 2 правки."""
    if token_len < 4:
        return 0
    if token_len <= 7:
        return 1
    return 2


@functools.lru_cache(maxsize=20000)
def _levenshtein_le(a: str, b: str, max_dist: int) -> bool:
    """True, если расстояние Левенштейна между a и b не превышает max_dist.
    Ранний выход: если |len(a)-len(b)| > max_dist, расстояние заведомо больше порога —
    полную DP-матрицу вообще не считаем. Плюс lru_cache: одни и те же токены текста
    (частые слова вроде «для», «фигурка») встречаются в сотнях строк каталога — без
    кэша на 3.5к записей выходило ~250-300мс на запрос, с кэшем — единицы мс
    (микробенчмарк временным скриптом, OpenTodo c52f32b4)."""
    if a == b:
        return True
    if max_dist <= 0:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return False
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb] <= max_dist


def _token_fuzzy_match(qtok: str, ttok: str) -> bool:
    """qtok (токен запроса) найден в ttok (токен текста) — точно или с опечаткой в пределах
    _fuzzy_threshold(len(qtok)). Префиксные опечатки («дракош»→«дракоша») ловятся тем же
    Левенштейном: расхождение — это ровно недостающий хвост, он и сравнивается с порогом."""
    if qtok == ttok:
        return True
    thr = _fuzzy_threshold(len(qtok))
    return thr > 0 and _levenshtein_le(qtok, ttok, thr)


def _fuzzy_vocab_matches(q_tokens: tuple[str, ...], vocab: set[str]) -> dict[str, set[str]]:
    """Для каждого токена запроса — множество токенов из vocab, которые ему подходят
    (точно или фаззи). Считаем ОДИН раз на весь словарь уникальных токенов каталога,
    а не на токены каждой из 3.5к строк — иначе Левенштейн гоняется сотни тысяч раз
    за один поиск (было ~250-300мс/запрос, стало единицы мс)."""
    return {qt: {vt for vt in vocab if _token_fuzzy_match(qt, vt)} for qt in q_tokens}


def _row_fuzzy_hit(q_tokens: tuple[str, ...], ok_sets: dict[str, set[str]], text: str) -> bool:
    """Строка проходит фаззи-бакет, если для КАЖДОГО токена запроса среди токенов text
    нашёлся подходящий кандидат (из ok_sets — уже посчитан заранее по словарю каталога).
    Многословный запрос: порядок и лишние слова текста не важны."""
    row_tokens = set(_tokenize(text))
    if not row_tokens:
        return False
    return all(ok_sets[qt] & row_tokens for qt in q_tokens)


async def search_collection(q: str, limit: int = 60) -> list[CollectionItem]:
    """Поиск по ВСЕЙ коллекции: label + ИИ-теги + orig_label (исходное название из
    импорта — юзер может искать по нему латиницей). Фильтр в Python: LIKE в SQLite
    складывает регистр только для ASCII, кириллицу — нет.

    Ранжирование бакетами (OpenTodo c52f32b4 — толерантность к опечаткам добавлена
    сюда; перевод тегов ru<->en и синонимы — не здесь, это семантика в semsearch.py):
      1) подстрока запроса в label
      2) подстрока запроса в tags/orig_label
      3) фаззи-совпадение (опечатка) в label — по токенам
      4) фаззи-совпадение в tags/orig_label — по токенам
    Внутри бакета — по популярности (uses DESC, порядок выборки). Строка попадает
    ровно в один бакет (elif) — дублей между бакетами нет.

    Скан идёт по узкому SELECT (id/label/tags/orig_label) — полную строку (paths,
    palette и т.п.) тянем отдельным запросом только для итоговых <= limit совпадений.
    На 3.5к записей это ×3 к скорости скана (SELECT * тянет и большие TEXT-поля, которые
    ранжированию не нужны; замерено микробенчмарком на живой data/bot.db)."""
    needle = (q or "").strip().lower()
    if not needle:
        return []
    q_tokens = _tokenize(needle)
    # Фаззи включаем, только если есть хоть один токен запроса длиной 4+ — короткие
    # («мусорные» 2-3 буквы) токены дают только точные совпадения, без фаззи-шума.
    fuzzy_on = any(len(t) >= 4 for t in q_tokens)
    cur = await _conn().execute(
        "SELECT id, label, tags, orig_label FROM collection "
        "ORDER BY uses DESC, created_at DESC")
    rows = await cur.fetchall()
    label_texts = [(r["label"] or "").lower() for r in rows]
    tag_texts = [f'{r["tags"] or ""} {r["orig_label"] or ""}'.lower() for r in rows]

    label_ok: dict[str, set[str]] = {}
    tag_ok: dict[str, set[str]] = {}
    if fuzzy_on:
        label_vocab = {t for text in label_texts for t in _tokenize(text)}
        tag_vocab = {t for text in tag_texts for t in _tokenize(text)}
        label_ok = _fuzzy_vocab_matches(q_tokens, label_vocab)
        tag_ok = _fuzzy_vocab_matches(q_tokens, tag_vocab)

    in_label, in_tags, fuzzy_label, fuzzy_tags = [], [], [], []
    for r, label_l, tags_l in zip(rows, label_texts, tag_texts):
        if needle in label_l:
            in_label.append(r["id"])
        elif needle in tags_l:
            in_tags.append(r["id"])
        elif fuzzy_on and _row_fuzzy_hit(q_tokens, label_ok, label_l):
            fuzzy_label.append(r["id"])
        elif fuzzy_on and _row_fuzzy_hit(q_tokens, tag_ok, tags_l):
            fuzzy_tags.append(r["id"])
        if len(in_label) >= limit:
            break
    ordered_ids = (in_label + in_tags + fuzzy_label + fuzzy_tags)[:limit]
    if not ordered_ids:
        return []
    ph = ",".join("?" * len(ordered_ids))
    cur2 = await _conn().execute(f"SELECT * FROM collection WHERE id IN ({ph})", ordered_ids)
    by_id = {r["id"]: r for r in await cur2.fetchall()}
    return [_to_collection(by_id[i]) for i in ordered_ids if i in by_id]


async def get_collection_item(cid: int) -> CollectionItem | None:
    cur = await _conn().execute("SELECT * FROM collection WHERE id = ?", (cid,))
    r = await cur.fetchone()
    return _to_collection(r) if r else None


# --- Избранное (сердечко на модельках каталога) ---------------------------------------------

async def set_favorite(user_id: int, col_id: int, on: bool) -> None:
    """Поставить/снять «Избранное» модели каталога. Идемпотентно: повторный тап/двойной
    запрос не ломают состояние (в отличие от toggle)."""
    if on:
        await _conn().execute(
            "INSERT INTO favorites(user_id, col_id, created_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id, col_id) DO NOTHING", (user_id, col_id, int(time.time())))
    else:
        await _conn().execute(
            "DELETE FROM favorites WHERE user_id = ? AND col_id = ?", (user_id, col_id))
    await _conn().commit()


async def list_favorite_ids(user_id: int) -> list[int]:
    """ID избранных моделей юзера, свежедобавленные первыми. Уходит фронту в /api/options —
    сердечки на любых плитках (разделы, поиск, лента) рисуются из одного источника."""
    cur = await _conn().execute(
        "SELECT f.col_id FROM favorites f JOIN collection c ON c.id = f.col_id "
        "WHERE f.user_id = ? ORDER BY f.created_at DESC, f.col_id DESC", (user_id,))
    return [r["col_id"] for r in await cur.fetchall()]


async def list_favorites(user_id: int, limit: int = 60, offset: int = 0) -> list[CollectionItem]:
    """Страница избранного юзера (виртуальный раздел каталога): карточки коллекции в порядке
    добавления, свежие первыми. Удалённые из каталога модели отпадают сами (JOIN)."""
    cur = await _conn().execute(
        "SELECT c.* FROM favorites f JOIN collection c ON c.id = f.col_id "
        "WHERE f.user_id = ? ORDER BY f.created_at DESC, f.col_id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset))
    return [_to_collection(r) for r in await cur.fetchall()]


async def list_collection_categories() -> list[tuple[str, int, int | None, int]]:
    """[(имя, число моделей, id самой популярной модели раздела, sort)] —
    разделы каталога «Модели» (c9c42789); top_id — для картинки раздела (85e8d47b).
    Порядок разделов задаёт админ (475b413e): первична cc.sort ASC (её сила сильнее
    количества — админский порядок должен побеждать); при равном sort раздел с
    бОльшим числом моделей идёт раньше; при полном равенстве — по имени."""
    cur = await _conn().execute(
        "SELECT cc.name, COUNT(c.id) AS n, "
        "  (SELECT c2.id FROM collection c2 WHERE c2.category = cc.name "
        "   ORDER BY c2.uses DESC, c2.created_at DESC LIMIT 1) AS top_id, "
        "  cc.sort AS srt "
        "FROM collection_categories cc "
        "LEFT JOIN collection c ON c.category = cc.name "
        "GROUP BY cc.name ORDER BY srt ASC, n DESC, cc.name")
    return [(r["name"], r["n"], r["top_id"], r["srt"]) for r in await cur.fetchall()]


async def upsert_collection_category(name: str, sort: int = 100) -> None:
    name = (name or "").strip()
    if not name:
        return
    await _conn().execute(
        "INSERT INTO collection_categories(name, sort, created_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO NOTHING", (name, sort, int(time.time())))
    await _conn().commit()


async def set_collection_category_sort(name: str, sort: int) -> None:
    """Админский порядок раздела каталога «Модели» (475b413e): чем меньше sort,
    тем раньше раздел; равные sort сортируются по числу моделей (list_collection_categories)."""
    name = (name or "").strip()
    if not name:
        return
    await _conn().execute(
        "UPDATE collection_categories SET sort = ? WHERE name = ?", (sort, name))
    await _conn().commit()


async def set_collection_category(cid: int, category: str) -> None:
    await _conn().execute("UPDATE collection SET category = ? WHERE id = ?",
                          ((category or "").strip(), cid))
    await _conn().commit()


async def count_collection_by_category(category: str) -> int:
    """Сколько моделей в разделе каталога (category='' — «вне каталога»). Для админ-drill-in."""
    cur = await _conn().execute(
        "SELECT COUNT(*) AS c FROM collection WHERE category = ?", ((category or "").strip(),))
    return (await cur.fetchone())["c"]


async def rename_collection_category(old: str, new: str) -> None:
    """Переименовать раздел каталога целиком (админ, OpenTodo b38e9569): все модели раздела
    переносятся на новое имя, строка раздела в collection_categories переименовывается с
    сохранением порядка sort. Пустые/совпадающие имена — no-op; если new уже есть, разделы
    сливаются (модели old добавляются к new, sort берётся от old)."""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new or old == new:
        return
    cur = await _conn().execute("SELECT sort FROM collection_categories WHERE name = ?", (old,))
    row = await cur.fetchone()
    sort = row["sort"] if row else 100
    await _conn().execute(
        "INSERT INTO collection_categories(name, sort, created_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET sort=excluded.sort", (new, sort, int(time.time())))
    await _conn().execute("UPDATE collection SET category = ? WHERE category = ?", (new, old))
    await _conn().execute("DELETE FROM collection_categories WHERE name = ?", (old,))
    await _conn().commit()


async def delete_collection_category(name: str) -> None:
    """Удалить раздел каталога (админ): его модели становятся «вне каталога» (category=''),
    сама строка раздела удаляется. Модели НЕ удаляются."""
    name = (name or "").strip()
    if not name:
        return
    await _conn().execute("UPDATE collection SET category = '' WHERE category = ?", (name,))
    await _conn().execute("DELETE FROM collection_categories WHERE name = ?", (name,))
    await _conn().commit()


async def random_collection_items(limit: int = 20) -> list[CollectionItem]:
    """Случайные модели каталога — кандидаты для «модели дня» в рассылке (6fbed3ad);
    вызывающий берёт первую, у которой реально существует картинка."""
    cur = await _conn().execute(
        "SELECT * FROM collection WHERE category != '' "
        "AND (photo_path IS NOT NULL OR preview_path IS NOT NULL) "
        "ORDER BY RANDOM() LIMIT ?", (limit,))
    return [_to_collection(r) for r in await cur.fetchall()]


async def user_fav_categories(uid: int) -> list[str]:
    """Категории каталога, которыми юзер реально пользовался (df0c9674): его заказы и
    сохранённые модели связываются с коллекцией по hash. По убыванию частоты."""
    cur = await _conn().execute(
        "SELECT c.category AS cat, COUNT(*) AS n FROM ("
        "  SELECT hash FROM requests WHERE user_id = ? AND hash != '' "
        "  UNION ALL SELECT hash FROM saved_models WHERE user_id = ? AND hash != '') u "
        "JOIN collection c ON c.hash = u.hash AND c.category != '' "
        "GROUP BY c.category ORDER BY n DESC", (uid, uid))
    return [r["cat"] for r in await cur.fetchall()]


async def recommend_page(uid: int, limit: int = 30, offset: int = 0) -> tuple[list, int | None]:
    """Страница бесконечной ленты рекомендаций (df0c9674): ~70% из частых категорий юзера,
    ~30% из остальных. Раскладка детерминирована В ПРЕДЕЛАХ дня и юзера (seed uid+дата):
    пагинация по offset не дублирует позиции, но каждый день лента начинается по-новому
    (feba4074 — «первые модели всё время одни и те же»). Раздел «Напечатанное» в ленту
    не попадает (7405a432 — это витрина уже напечатанного, не рекомендации)."""
    fav = [c for c in await user_fav_categories(uid) if c != "Напечатанное"]
    ph = ",".join("?" * len(fav)) if fav else ""
    if fav:
        cur = await _conn().execute(
            f"SELECT * FROM collection WHERE category IN ({ph}) "
            "ORDER BY uses DESC, id DESC LIMIT 500", fav)
        pool_a = [_to_collection(r) for r in await cur.fetchall()]
        cur = await _conn().execute(
            f"SELECT * FROM collection WHERE category NOT IN ('', 'Напечатанное') "
            f"AND category NOT IN ({ph}) ORDER BY uses DESC, id DESC LIMIT 500", fav)
        pool_b = [_to_collection(r) for r in await cur.fetchall()]
    else:
        pool_a = []
        cur = await _conn().execute(
            "SELECT * FROM collection WHERE category NOT IN ('', 'Напечатанное') "
            "ORDER BY uses DESC, id DESC LIMIT 500")
        pool_b = [_to_collection(r) for r in await cur.fetchall()]
    rng = random.Random(f"{uid}:{time.strftime('%Y%m%d')}")
    rng.shuffle(pool_a)
    rng.shuffle(pool_b)
    merged: list = []
    ia = ib = 0
    while ia < len(pool_a) or ib < len(pool_b):
        for _ in range(7):     # 7 «своих»…
            if ia < len(pool_a):
                merged.append(pool_a[ia]); ia += 1
        for _ in range(3):     # …затем 3 «на расширение вкуса»
            if ib < len(pool_b):
                merged.append(pool_b[ib]); ib += 1
        if ia >= len(pool_a) and ib < len(pool_b):
            merged.extend(pool_b[ib:]); break
    page = merged[offset:offset + limit]
    nxt = offset + limit if len(merged) > offset + limit else None
    return page, nxt


async def collection_last_minutes(hashes: list[str]) -> dict[str, float]:
    """Минуты печати ОДНОЙ штуки по последнему расчёту модели: hash → minutes_total/quantity
    самого свежего запроса. Для «~N ч» на карточке ленты главной (редизайн F4); моделей,
    которые ещё ни разу не считались, в словаре просто нет."""
    hs = [x for x in dict.fromkeys(hashes) if x]
    if not hs:
        return {}
    ph = ",".join("?" * len(hs))
    cur = await _conn().execute(
        f"SELECT r.hash AS hash, r.minutes_total AS m, r.quantity AS q FROM requests r "
        f"JOIN (SELECT hash, MAX(id) AS mid FROM requests "
        f"      WHERE hash IN ({ph}) AND minutes_total > 0 GROUP BY hash) x ON x.mid = r.id",
        hs)
    return {r["hash"]: float(r["m"] or 0.0) / max(1, int(r["q"] or 1))
            for r in await cur.fetchall()}


async def get_collection_id_by_hash(hash: str) -> int | None:
    """ID карточки коллекции по хэшу модели (или None). Нужен, чтобы привязать фото готовой
    печати из заказа к уже существующей карточке коллекции (OpenTodo 3fe0b3fa)."""
    if not hash:
        return None
    cur = await _conn().execute("SELECT id FROM collection WHERE hash = ?", (hash,))
    row = await cur.fetchone()
    return row["id"] if row else None


async def set_collection_photo(cid: int, photo_path: str) -> None:
    await _conn().execute("UPDATE collection SET photo_path = ? WHERE id = ?",
                          (_rel(photo_path), cid))
    await _conn().commit()


async def set_collection_label(cid: int, label: str) -> None:
    """Переименовать запись коллекции (название файла/модели). #64e63433."""
    await _conn().execute("UPDATE collection SET label = ? WHERE id = ?", (label.strip()[:120], cid))
    await _conn().commit()


async def delete_collection_item(cid: int) -> None:
    await _conn().execute("DELETE FROM collection WHERE id = ?", (cid,))
    await _conn().commit()


# ---- очередь разбора незагрузившихся моделей (OpenTodo 26196ac1) ----
async def add_calc_queue(user_id: int, user_name: str, url: str, error: str) -> int:
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO calc_queue(user_id, user_name, url, error, status, created_at, updated_at) "
        "VALUES(?,?,?,?, 'new', ?, ?)",
        (user_id, (user_name or "")[:80], (url or "")[:500], (error or "")[:300], now, now))
    await _conn().commit()
    return cur.lastrowid


async def list_calc_queue(include_done: bool = False, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM calc_queue" + ("" if include_done else " WHERE status='new'") \
        + " ORDER BY id DESC LIMIT ?"
    cur = await _conn().execute(q, (limit,))
    return [dict(r) for r in await cur.fetchall()]


async def count_calc_queue() -> int:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM calc_queue WHERE status='new'")
    return (await cur.fetchone())["c"]


async def get_calc_queue(qid: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM calc_queue WHERE id = ?", (qid,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def resolve_calc_queue(qid: int, note: str = "", *, status: str = "done") -> None:
    await _conn().execute(
        "UPDATE calc_queue SET status = ?, note = ?, updated_at = ? WHERE id = ?",
        (status, (note or "")[:300] or None, int(time.time()), qid))
    await _conn().commit()


# ---- кэш результата слайса каталожной модели (OpenTodo ae038968) ----
async def get_calc_cache(model_hash: str, printer_id: int, quality_id: int, strength_id: int,
                         scale_pct: int = 100) -> dict | None:
    """Последний слайс ЭТОЙ модели тем же принтером/качеством/прочностью/масштабом — грамм/время
    не зависят от пластика (см. комментарий у CREATE TABLE calc_cache), поэтому цена под ЛЮБОЙ
    пластик считается мгновенно штатной формулой (pricing.compute_price)."""
    cur = await _conn().execute(
        "SELECT volume_cm3, minutes, plates, created_at FROM calc_cache WHERE model_hash = ? AND "
        "printer_id = ? AND quality_id = ? AND strength_id = ? AND scale_pct = ?",
        (model_hash, printer_id, quality_id, strength_id, scale_pct))
    r = await cur.fetchone()
    return dict(r) if r else None


async def put_calc_cache(model_hash: str, printer_id: int, quality_id: int, strength_id: int,
                         scale_pct: int, volume_cm3: float, minutes: float, plates: int = 1) -> None:
    """Запомнить результат честного слайса — вызывается после КАЖДОГО успешного слайса в
    calc-путях мини-апки (_remember_calc_cache в webapp.py). UPSERT: новый слайс той же
    комбинации перезаписывает старый (модель могли поправить — геометрия/время могли измениться)."""
    if volume_cm3 <= 0 or minutes <= 0:
        return
    await _conn().execute(
        "INSERT INTO calc_cache(model_hash, printer_id, quality_id, strength_id, scale_pct, "
        "volume_cm3, minutes, plates, created_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(model_hash, printer_id, quality_id, strength_id, scale_pct) DO UPDATE SET "
        "volume_cm3=excluded.volume_cm3, minutes=excluded.minutes, plates=excluded.plates, "
        "created_at=excluded.created_at",
        (model_hash, printer_id, quality_id, strength_id, scale_pct,
         float(volume_cm3), float(minutes), max(1, int(plates)), int(time.time())))
    await _conn().commit()
