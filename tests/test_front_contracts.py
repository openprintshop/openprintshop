"""Контракты фронтов: статусы заказа и права команды на экранах мобильной админки.

Зачем это нужно. У продукта три независимых представления одного и того же: клиентские экраны
мини-аппы, АДМИНСКИЕ экраны мини-аппы (заморожены решением владельца — не правятся, но обязаны
оставаться корректными) и настольная админка (desk/src). Ни одно из них не читает словари
бэкенда: у каждого свой словарь статусов и своё представление о том, какие разделы кому
показывать. Разъезд такого дубля не падает и не пишется в консоль — он выглядит как пустая
ячейка, серый бейдж или молчаливый 403 в сетевой вкладке, которую никто не открывает.

Здесь два прибора. Оба — снимки (как tests/test_routes_inventory.py), а не проекты: они ничего
не требуют «как правильно», они ловят изменение, которого никто не планировал.

  1. СТАТУСЫ. Каждый ключ db.REQ_STATUS_LABELS обязан быть закрыт словарями всех трёх фронтов.
     Исходник настольной админки — desk/src/statuses.jsx, а НЕ сборка desk/desk.js: в сборке
     esbuild экранирует кириллицу, и греп по русской строке там ничего не доказывает.

  2. ПРАВА. Экран, открытый ОПЕРАТОРУ (в мобильной админке — плитка без права либо право из
     roles.ROLE_CAPS[operator]; в настольной — раздел NAV без `cap`), не должен звать ручку, на
     которую webguard._route_cap требует право, которого у оператора нет. Прибор ходит по ОБОИМ
     фронтам: мобильному (screens_admin*.js) и настольному (desk/src).

     Почему деск ТОЖЕ проверяется, хотя оператора в нём сегодня не бывает. Файлы деска сервер
     отдаёт только локальным запросам, а токен ПК-админки резолвится в ADMIN_IDS[0], то есть
     всегда во владельца: `can()` в деске сегодня возвращает true всем. Но интерфейс роли уже
     построен (hub отдаёт me.caps, NAV гейтится, экраны прячутся), и 12.08.2026 в нём нашлось
     ВОСЕМЬ мест, где оператор получил бы пустой график, ложную ленту «клиенты ничего не делали»
     или подтверждение «отменить нельзя» перед отказом сервера. Держать их руками бессмысленно:
     стоит деску научиться пускать сотрудника по его собственной подписи (а это ровно то, что
     нужно для продажи чужим студиям), как всё вернётся. Прибор фиксирует границу заранее.

Почему второй прибор — снимок с расхождениями, а не «упади при первом же расхождении». На день
написания расхождений тридцать семь, и они РАЗНОЙ природы: часть — настоящие дыры (оператор
видит кнопку, сервер отвечает 403), часть — вызовы, которые фронт уже прячет гейтом, но гейт
стоит не на вызове, а на отрисовке (`canMoney && h(...)`, `app.can('settings') && h(...)`), и
статический разбор такое не видит. Приговор по каждой строке проставлен в комментариях эталона.
Чинить код здесь нельзя: какие разделы открыть наёмному оператору — решение владельца, а не
рефакторинг. Прибор нужен, чтобы к этим тридцати семи молча не добавилась тридцать восьмая.

Что делать, когда покраснел:
  • «ПОЯВИЛОСЬ» в правах — на экран, открытый оператору, добавили вызов владельческой ручки.
    Либо закрыть экран правом (плитка в AdminMore), либо спрятать вызов гейтом, либо — если так
    и задумано — вписать строку в эталон С ПРИГОВОРОМ, а не молча;
  • «ПРОПАЛО» в правах — дыру закрыли; убрать строку из эталона;
  • статусы — в бэкенде появился статус, которого фронт не знает. Дописать его во ВСЕ словари
    (для desk — в исходник, потом `npm run build:desk`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config, db, roles, webapp, webguard

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ─────────────────────────── мини-разбор JavaScript ───────────────────────────
# Полноценный парсер тут не нужен и вреден: нам хватает структуры (скобки, запятые, ключи), а
# всё, что структурой не является — строки, комментарии, регулярки, — гасится пробелами С
# СОХРАНЕНИЕМ ДЛИНЫ. Так смещения в «очищенном» тексте совпадают со смещениями в исходном, и
# значения читаются из оригинала по границам, найденным в очищенном.

_REGEX_PREV = "(,=:[!&|?{};+-*%~^<>"


def _strip_js(src: str) -> str:
    """Заменить строки/комментарии/регулярки пробелами, сохранив длину и переводы строк."""
    out = list(src)
    n = len(src)
    i = 0
    prev = ""          # последний значимый символ — по нему отличаем регулярку от деления

    def blank(a: int, b: int) -> None:
        for k in range(max(a, 0), min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if c in "'\"`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    j += 1
                    break
                j += 1
            blank(i + 1, j - 1)
            prev = c
            i = j
            continue
        if c == "/" and (prev in _REGEX_PREV or prev == ""):
            j = i + 1
            in_class = False
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "\n":
                    break
                elif src[j] == "/" and not in_class:
                    j += 1
                    break
                j += 1
            blank(i, j)
            prev = "/"
            i = j
            continue
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


def _close(clean: str, frm: int, op: str, cl: str) -> tuple[int, int]:
    """Границы парной скобки (op, cl), начиная с первой op не раньше frm."""
    start = clean.index(op, frm)
    depth = 0
    for k in range(start, len(clean)):
        if clean[k] == op:
            depth += 1
        elif clean[k] == cl:
            depth -= 1
            if depth == 0:
                return start, k + 1
    raise AssertionError(f"не закрылась скобка {op!r} на позиции {start}")


_KEY_RE = re.compile(r"""(?:^|[{,])\s*(?:'([A-Za-z_]\w*)'|"([A-Za-z_]\w*)"|([A-Za-z_]\w*))\s*:""")


def _keys_of_object(clean: str, start: int, end: int) -> set[str]:
    """Ключи ВЕРХНЕГО уровня объектного литерала clean[start:end]."""
    body = clean[start:end]
    depth_at: list[int] = []
    depth = 0
    for ch in body:
        depth_at.append(depth)
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
    keys: set[str] = set()
    for m in _KEY_RE.finditer(body):
        idx = next(g for g in (1, 2, 3) if m.group(g) is not None)
        if depth_at[m.start(idx)] == 1:
            keys.add(m.group(idx))
    return keys


def _object_keys(clean: str, marker: str) -> set[str]:
    m = re.search(marker, clean)
    assert m is not None, f"объявление {marker!r} не найдено — прибор ослеп, чинить надо тест"
    a, b = _close(clean, m.end() - 1, "{", "}")
    return _keys_of_object(clean, a, b)


def _split_top(clean_row: str, src_row: str) -> list[str]:
    """Разбить `[a, b, c]` по запятым верхнего уровня; значения берутся из оригинала."""
    fields: list[str] = []
    depth = 0
    prev = 1
    for k, ch in enumerate(clean_row):
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch == "," and depth == 1:
            fields.append(src_row[prev:k].strip())
            prev = k + 1
    fields.append(src_row[prev:len(src_row) - 1].strip())
    return fields


def _literal(field: str) -> str | None:
    m = re.fullmatch(r"'([^']*)'", field)
    return m.group(1) if m else None


# ═════════════════════════════ 1. СТАТУСЫ ЗАКАЗА ═════════════════════════════
# (файл, имя словаря, объявление, чей фронт)
STATUS_DICTS: tuple[tuple[str, str, str, str], ...] = (
    ("app/webui/screens_more.js", "ST_CLASS", r"var\s+ST_CLASS\s*=", "клиентские экраны мини-аппы"),
    ("app/webui/screens_more.js", "ST_ICON", r"var\s+ST_ICON\s*=", "клиентские экраны мини-аппы"),
    ("app/webui/screens_admin.js", "ST_LABEL", r"var\s+ST_LABEL\s*=", "админка мини-аппы (заморожена)"),
    ("app/webui/screens_admin.js", "ST_COLOR", r"var\s+ST_COLOR\s*=", "админка мини-аппы (заморожена)"),
    ("app/webui/screens_admin.js", "ST_CLASS", r"var\s+ST_CLASS\s*=", "админка мини-аппы (заморожена)"),
    # ИСХОДНИК настольной админки. desk/desk.js — сборка esbuild, там кириллица экранирована.
    ("app/webui/desk/src/statuses.jsx", "ST", r"export\s+const\s+ST\s*=", "настольная админка"),
)

# Единственный признанный пробел. `self` — самопечать мастера: клиенту такой заказ не принадлежит
# и в его списке появиться не может (см. db.create_self_print_order — user_id = ADMIN). Увидеть
# его в КЛИЕНТСКОЙ части способен только сам мастер, зашедший в «Клиентский режим», и увидит он
# корректную подпись: ярлык приезжает с сервера (status_label), а оба словаря разыменовываются с
# запасным значением — `ST_CLASS[o.status] || 'st-new'`, `ST_ICON[o.status] || 'sparkle'`.
# Пустой ячейки не будет, будет нейтральный серый бейдж. В АДМИНСКИХ словарях `self` есть.
KNOWN_STATUS_GAPS: dict[tuple[str, str], frozenset[str]] = {
    ("app/webui/screens_more.js", "ST_CLASS"): frozenset({"self"}),
    ("app/webui/screens_more.js", "ST_ICON"): frozenset({"self"}),
}


def _status_gaps(read=None) -> dict[tuple[str, str], frozenset[str]]:
    """Статусы бэкенда, не закрытые словарями фронтов. Пустые записи не возвращаются.

    `read` разрешается на КАЖДОМ вызове, а не в значении по умолчанию: значение по умолчанию
    связалось бы с функцией один раз при импорте, и подменить источник (мутация в проверке
    «прибор кусается») стало бы невозможно — тест зеленел бы на нетронутых файлах."""
    read = read or _read
    backend = set(db.REQ_STATUS_LABELS)
    assert len(backend) >= 10, "словарь статусов бэкенда подозрительно мал — читаем не то"
    gaps: dict[tuple[str, str], frozenset[str]] = {}
    for path, name, marker, _who in STATUS_DICTS:
        src = read(path)
        keys = _object_keys(_strip_js(src), marker)
        assert len(keys) >= 8, f"{path}: словарь {name} разобрался в {len(keys)} ключей — так не бывает"
        missing = backend - keys
        if missing:
            gaps[(path, name)] = frozenset(missing)
    return gaps


def test_status_dicts_cover_backend():
    """Каждый статус из db.REQ_STATUS_LABELS закрыт словарями всех трёх фронтов.

    Незакрытый статус нигде не падает: мастер получает пустую ячейку или чужой цвет, клиент —
    серый бейдж вместо понятного. Разъезжается это молча, потому и проверяется механически."""
    gaps = _status_gaps()
    assert gaps == KNOWN_STATUS_GAPS, (
        "словари статусов разъехались с бэкендом:\n"
        + "".join(f"  {p}::{n} не знает {sorted(v)}\n" for (p, n), v in sorted(gaps.items()))
        + "  (эталон: " + str({f"{p}::{n}": sorted(v) for (p, n), v in sorted(KNOWN_STATUS_GAPS.items())}) + ")")


def test_status_contract_bites():
    """Прибор обязан краснеть. Убираем «Отложен» из ИСХОДНИКА настольной админки.

    Проверка нужна ровно потому, что проверяемое не видно глазами: тест, который не умеет
    покраснеть, здесь хуже отсутствующего — он создаёт ощущение, что дубль под присмотром."""
    desk = "app/webui/desk/src/statuses.jsx"
    broken = re.sub(r"\n\s*deferred:.*", "", _read(desk))
    assert "deferred:" not in broken, "мутация не сработала — правило подстановки устарело"

    gaps = _status_gaps(lambda p: broken if p == desk else _read(p))
    assert gaps.get((desk, "ST")) == frozenset({"deferred"}), gaps
    assert gaps != KNOWN_STATUS_GAPS, "пропажа статуса в настольной админке прошла мимо прибора"


def test_status_dicts_are_read_from_desk_source_not_bundle():
    """Настольная админка проверяется по desk/src, а сборка при этом существует.

    Если однажды кто-то решит «упростить» и почитать статусы из desk/desk.js — тест станет
    бессмысленным: в сборке esbuild кириллица экранирована в \\uXXXX."""
    assert (ROOT / "app/webui/desk/src/statuses.jsx").exists()
    assert any(p == "app/webui/desk/src/statuses.jsx" for p, _n, _m, _w in STATUS_DICTS)
    assert not any(p.endswith("desk/desk.js") for p, _n, _m, _w in STATUS_DICTS)


# ═════════════════════════ 2. ПРАВА НА ЭКРАНАХ АДМИНКИ ═════════════════════════
API_JS = "app/webui/api.js"
APP_JS = "app/webui/app.js"
ADMIN_JS = ("app/webui/screens_admin.js", "app/webui/screens_admin2.js", "app/webui/screens_admin3.js",
            "app/webui/screens_admin4.js")

_ADMIN_URL = re.compile(r"['\"](/(?:api/)?admin/[^'\"?]*)['\"]")


def _api_methods(read) -> dict[str, set[tuple[str, bool]]]:
    """Метод window.API → админские URL, которые он дёргает. bool = URL это ПРЕФИКС (склейка)."""
    src = read(API_JS)
    clean = _strip_js(src)
    out: dict[str, set[tuple[str, bool]]] = {}
    for m in re.finditer(r"^ {4}([A-Za-z_]\w*): function\s*\(", clean, re.M):
        _a, end = _close(clean, m.end() - 1, "{", "}")
        region = src[m.start():end]
        for um in _ADMIN_URL.finditer(region):
            tail = region[um.end():um.end() + 8].lstrip()
            out.setdefault(m.group(1), set()).add((um.group(1), tail.startswith("+")))
    return out


def _canonical(url: str, is_prefix: bool, canon: frozenset[str]) -> str | None:
    """Канонический путь маршрута по URL из фронта. `/api/admin/order/` + id → /api/admin/order/{id}."""
    if url in canon:
        return url
    if is_prefix:
        hits = [c for c in canon if c.startswith(url) and re.fullmatch(r"\{[^/]+\}", c[len(url):])]
        if len(hits) == 1:
            return hits[0]
    return None


def _screens_map(read) -> dict[str, str]:
    src = read(APP_JS)
    clean = _strip_js(src)
    a, b = _close(clean, re.search(r"var SCREENS = ", clean).end() - 1, "{", "}")
    return dict(re.findall(r"(\w+): window\.(\w+)", src[a:b]))


def _admin_tabs(read) -> list[str]:
    src = read(APP_JS)
    clean = _strip_js(src)
    a, b = _close(clean, re.search(r"var ADMIN_TABS = ", clean).end() - 1, "[", "]")
    return re.findall(r"key: '(\w+)'", src[a:b])


def _tab_of(read) -> dict[str, str]:
    src = read(APP_JS)
    clean = _strip_js(src)
    a, b = _close(clean, re.search(r"var ADMIN_TAB_OF = ", clean).end() - 1, "{", "}")
    return dict(re.findall(r"(\w+): '(\w+)'", src[a:b]))


def _tiles(read) -> dict[str, str]:
    """Плитки экрана «Ещё»: экран → требуемое право ('' = открыт любому сотруднику).

    Плитка объявлена как [иконка, заголовок, подпись, экран, право?] — пятого поля может не быть,
    и это в коде значит «раздел открыт всем», см. tiles() в screens_admin.js."""
    src = read(ADMIN_JS[0])
    clean = _strip_js(src)
    out: dict[str, str] = {}
    for call in re.finditer(r"\btiles\(\[", clean):
        a, b = _close(clean, call.end() - 2, "[", "]")
        depth = 0
        row_at = None
        for k in range(a, b):
            ch = clean[k]
            if ch == "[":
                depth += 1
                if depth == 2:
                    row_at = k
            elif ch == "]":
                if depth == 2 and row_at is not None:
                    fields = _split_top(clean[row_at:k + 1], src[row_at:k + 1])
                    row_at = None
                    if len(fields) >= 4 and _literal(fields[3]):
                        out[_literal(fields[3])] = (_literal(fields[4]) or "") if len(fields) >= 5 else ""
                depth -= 1
    return out


def _components(read) -> tuple[dict[str, str], dict[str, str]]:
    """Компоненты верхнего уровня админских файлов: имя → текст, плюс карта экспортных псевдонимов."""
    bodies: dict[str, str] = {}
    alias: dict[str, str] = {}
    for path in ADMIN_JS:
        src = read(path)
        clean = _strip_js(src)
        for m in re.finditer(r"^ {2}function ([A-Z]\w*)\s*\(", clean, re.M):
            _a, end = _close(clean, m.end() - 1, "{", "}")
            bodies.setdefault(m.group(1), src[m.start():end])
        # экраны-вкладки собраны обёрткой tabbed([...]) — это тоже компонент, только без function
        for m in re.finditer(r"^ {2}var ([A-Z]\w*) = tabbed\(", clean, re.M):
            _a, end = _close(clean, m.end() - 1, "(", ")")
            bodies.setdefault(m.group(1), src[m.start():end])
        for m in re.finditer(r"Object\.assign\(window, \{", clean):
            a, b = _close(clean, m.end() - 1, "{", "}")
            for exported, local in re.findall(r"(\w+): (\w+)", src[a:b]):
                alias[exported] = local
    return bodies, alias


class _Front:
    """Разобранная мобильная админка: экраны, компоненты, вызовы API."""

    def __init__(self, read=None):
        read = read or _read      # разрешаем на вызове, а не в значении по умолчанию — см. _status_gaps
        self.screens = _screens_map(read)
        self.tabs = _admin_tabs(read)
        self.tab_of = _tab_of(read)
        self.tiles = _tiles(read)
        self.bodies, self.alias = _components(read)
        self.api = _api_methods(read)
        self._clean: dict[str, str] = {}

    def local(self, name: str) -> str:
        return self.alias.get(name, name)

    def _cl(self, name: str) -> str:
        if name not in self._clean:
            self._clean[name] = _strip_js(self.bodies.get(name, ""))
        return self._clean[name]

    def children(self, name: str) -> set[str]:
        """Кого компонент рисует: h(Component, …) и window.Component (так устроены экраны-вкладки)."""
        cl = self._cl(name)
        out = {self.local(m.group(1)) for m in re.finditer(r"\bh\(\s*([A-Z]\w*)", cl)}
        out |= {self.local(m.group(1)) for m in re.finditer(r"\bwindow\.([A-Z]\w*)", cl)}
        return {n for n in out if n in self.bodies} - {name}

    def closure(self, root: str) -> set[str]:
        seen: set[str] = set()
        stack = [root]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.children(n) - seen)
        return seen

    def api_calls(self, name: str) -> set[str]:
        return set(re.findall(r"\bAPI\.([A-Za-z_]\w*)", self._cl(name)))

    def go_targets(self, name: str) -> set[str]:
        return set(re.findall(r"\b(?:go|goTab)\(\s*'(admin\w*)'", self.bodies.get(name, "")))


def _operator_view(front: _Front, canon: frozenset[str], route_cap) -> tuple[frozenset[str], frozenset[tuple]]:
    """Экраны, открытые оператору, и вызовы владельческих ручек с них.

    Экран считается открытым оператору, если он корневая вкладка админки (кроме «Клиентов» —
    её прячет app.can('clients') в app.js), либо его плитка в «Ещё» объявлена без права или с
    правом оператора, либо он дочерний к открытому экрану (ADMIN_TAB_OF) или доступен из него
    через app.go(). Вкладки экранов-обёрток (tabbed) приходят через children()."""
    caps = roles.ROLE_CAPS[config.ROLE_OPERATOR]
    roots = {t for t in front.tabs if t != "adminClients"}
    roots |= {s for s, cap in front.tiles.items() if s.startswith("admin") and (not cap or cap in caps)}

    seen: set[str] = set()
    stack = list(roots)
    while stack:
        s = stack.pop()
        if s in seen or not s.startswith("admin") or s not in front.screens:
            continue
        if front.tiles.get(s) and front.tiles[s] not in caps:
            continue           # раздел закрыт правом — оператор его не видит
        seen.add(s)
        stack += [c for c, parent in front.tab_of.items() if parent == s and c not in seen]
        comp = front.local(front.screens[s])
        if comp in front.bodies:
            for n in front.closure(comp):
                stack += [g for g in front.go_targets(n) if g not in seen]

    gaps: set[tuple] = set()
    for s in sorted(seen):
        comp = front.local(front.screens[s])
        if comp not in front.bodies:
            continue
        methods: set[str] = set()
        for n in front.closure(comp):
            methods |= front.api_calls(n)
        for meth in sorted(methods):
            for url, is_prefix in sorted(front.api.get(meth, ())):
                path = _canonical(url, is_prefix, canon)
                need = "НЕ ОПОЗНАН" if path is None else route_cap(path)
                if need not in caps:
                    gaps.add((s, meth, path or url, need))
    return frozenset(seen), frozenset(gaps)


# ─── эталон: экраны мобильной админки, доступные наёмному оператору ───
OPERATOR_SCREENS: frozenset[str] = frozenset({
    "admin", "adminCalcQueue", "adminMasterLog", "adminMore", "adminOrder", "adminOrders",
    "adminPrint", "adminPrinter", "adminQueue", "adminSelf", "adminService",
    "adminStatus", "adminTasks",
    # 02.09.2026: мобильная админка повторяет иерархию настольной (desk/src/shell.jsx NAV).
    # «План недели» и «Калибровка» открыты оператору, как и на ПК (план — очередь и заказы,
    # калибровка — ручки под правом «Принтер»); «Принтеры» стали владельческими — на ПК раздел
    # целиком под «Настройками», потому что каждая правка профиля идёт в /api/admin/printer.
    "adminPlan", "adminCalib",
    # 05.09.2026: «Инструменты» (допечатка со слоя, Путь В) — под правом «Принтер», как на ПК:
    # за экраном заливка файла на принтер и старт печати.
    "adminTools",
})

# ─── эталон: вызовы владельческих ручек с этих экранов, (экран, метод API, путь, право) ───
# Приговор по каждой группе проставлен вручную — разбор велся по коду, а не по названиям.
OPERATOR_GAPS: frozenset[tuple[str, str, str, str]] = frozenset({
    # ДЫРА. Плитка «Журнал мастера» объявлена БЕЗ права, её первая вкладка — «Разбор ссылок»,
    # и она бьёт в /api/admin/calc_queue, которого в _OPERATOR_ROUTES нет. Оператор открывает
    # раздел и видит вечное «Загружаю…».
    ("adminCalcQueue", "adminCalcQueue", "/api/admin/calc_queue", "settings"),
    ("adminCalcQueue", "adminCalcQueueResolve", "/api/admin/calc_queue_resolve", "settings"),
    ("adminMasterLog", "adminCalcQueue", "/api/admin/calc_queue", "settings"),
    ("adminMasterLog", "adminCalcQueueResolve", "/api/admin/calc_queue_resolve", "settings"),

    # ГЕЙТ ЕСТЬ, статический разбор его не видит: в карточке заказа деньги закрыты флагом
    # `canMoney = data.money !== false` (сервер ставит его в _money_visible), а «Опасная зона» —
    # `app.can('settings')`. Гейт стоит на ОТРИСОВКЕ, обработчики объявлены безусловно, поэтому
    # вызовы и попадают в разбор.
    ("adminOrder", "adminBalance", "/api/admin/balance", "finance"),
    ("adminOrder", "adminOrderBill", "/api/admin/order/bill", "finance"),
    ("adminOrder", "adminOrderDelete", "/api/admin/order/delete", "settings"),
    ("adminOrder", "adminOrderDelivery", "/api/admin/order/delivery", "finance"),
    ("adminOrder", "adminOrderInvoice", "/api/admin/order/invoice", "finance"),
    ("adminOrder", "adminOrderPaid", "/api/admin/order/paid", "finance"),
    ("adminOrder", "adminOrderPrice", "/api/admin/order/price", "finance"),
    ("adminOrder", "adminOrderPriceByGt", "/api/admin/order/price", "finance"),
    ("adminOrder", "adminOrderRecalc", "/api/admin/order/recalc", "finance"),
    ("adminOrder", "adminOrderRecalcApply", "/api/admin/order/recalc_apply", "finance"),
    # ДЫРА, и самая обидная: блок «Доставка» СПЕЦИАЛЬНО открыт всей команде («цена доставки —
    # только владельцу, трек-номер — всей команде»), но справочник перевозчиков он тянет из
    # /api/admin/delivery, а это CAP_PRICING. У оператора выпадающий список перевозчиков пуст,
    # то есть отметить отправление — его прямая работа — он не может.
    ("adminOrder", "adminDelivery", "/api/admin/delivery", "pricing"),

    # ДЫРА. Три чипа в шапке «Заказов» ничем не закрыты: «За клиента» (создание заказа со
    # скидками), «Пакет ссылок» (пакетный расчёт и рассылка цен) и «Тестовые» (чистка).
    ("adminOrders", "adminBatchCalc", "/api/admin/batch_calc", "finance"),
    ("adminOrders", "adminBatchCancel", "/api/admin/batch_cancel", "settings"),
    ("adminOrders", "adminBatchLast", "/api/admin/batch_last", "settings"),
    ("adminOrders", "adminBatchSend", "/api/admin/batch_send", "finance"),
    ("adminOrders", "adminBatchStatus", "/api/admin/batch_status", "settings"),
    ("adminOrders", "adminDiscounts", "/api/admin/discounts", "pricing"),
    ("adminOrders", "adminOrderCreate", "/api/admin/order/create", "finance"),
    ("adminOrders", "adminOrdersCleanTest", "/api/admin/orders/clean_test", "settings"),

    # ГЕЙТ ЕСТЬ (03.09.2026, «Заказы» по образцу настольных): чип «Для себя» и его загрузка
    # стоят под app.can('settings') — ровно так же, как вкладка «Для себя» в desk/orders.jsx
    # (та строка эталона деска ниже). Статический разбор видит вызов внутри функции, а не if.
    ("adminOrders", "adminSelfOrders", "/api/admin/self_orders", "settings"),

    # ДЫРА. Вкладка «Для себя» в экране «Печать» рисуется безусловно, а самопечать мастера —
    # владельческая ручка.
    ("adminPrint", "adminSelfOrders", "/api/admin/self_orders", "settings"),
    ("adminPrint", "adminSelfPrint", "/api/admin/self_print", "settings"),
    ("adminSelf", "adminSelfOrders", "/api/admin/self_orders", "settings"),
    ("adminSelf", "adminSelfPrint", "/api/admin/self_print", "settings"),

    # Плитка «Принтеры» с 02.09.2026 закрыта правом «Настройки» (как на ПК) — прежняя дыра
    # «список открыт, сохранение профиля нет» ушла вместе с плиткой из вида оператора.

    # ГЕЙТ ЕСТЬ (02.09.2026, Пульт по образцу настольного): лента событий грузится ТОЛЬКО при
    # app.can('clients') — гейт стоит на самом вызове (loadFeed), оператору вместо неё идут
    # последние печати из журнала. Статический разбор видит вызов внутри функции и не видит if.
    ("admin", "adminActivity", "/api/admin/activity", "clients"),
    # ГЕЙТ ЕСТЬ (02.09.2026, поиск на экране «Ещё» — аналог палитры Ctrl+K): клиенты ищутся
    # только при app.can('clients'), иначе Promise.resolve(null) — оператор не получает 403 на
    # каждый набранный символ. Разбор видит вызов в тернарнике и не видит условие.
    ("adminMore", "adminClients", "/api/admin/clients", "clients"),

    # Плитка «Статус» без права — и это осознанно (телеметрия нужна у станка). Бэкапы там уже
    # закрыты app.can('settings') — ГЕЙТ ЕСТЬ, разбор его не видит по той же причине.
    ("adminStatus", "adminBackupNow", "/api/admin/backup_now", "settings"),
    ("adminStatus", "adminBackupStatus", "/api/admin/backup_status", "settings"),
    # ДЫРА. А вот автообновления такого гейта не получили: кнопки видны оператору и получают
    # 403 по нажатию.
    ("adminStatus", "adminUpdateApply", "/api/admin/update_apply", "settings"),
    ("adminStatus", "adminUpdateCheck", "/api/admin/update_check", "settings"),
    ("adminStatus", "adminUpdateMode", "/api/admin/update_mode", "settings"),

    # ДЫРА. Плитка «Задачи» без права, а список клиентов для выбора «кому напомнить» грузится
    # безусловно из /api/admin/clients (CAP_CLIENTS). У оператора выбор клиента пуст.
    ("adminTasks", "adminClients", "/api/admin/clients", "clients"),
})


@pytest.fixture(scope="module")
def canon() -> frozenset[str]:
    """Канонические пути настоящей таблицы маршрутов — источник истины, а не список из головы."""
    app = webapp.make_app()
    paths = set()
    for route in app.router.routes():
        res = getattr(route, "resource", None)
        assert res is not None, f"маршрут без ресурса: {route!r}"
        paths.add(res.canonical)
    return frozenset(paths)


@pytest.fixture(scope="module")
def front() -> _Front:
    return _Front()


def test_front_parser_still_sees_the_code(front, canon):
    """Прибор обязан кричать, если разучился видеть. Без этого он «зеленеет» на пустом разборе."""
    assert len(front.api) >= 100, f"методов API разобрано {len(front.api)} — так не бывает"
    assert front.api["adminOrders"] == {("/api/admin/orders", False)}
    assert front.api["adminOrderStatus"] == {("/api/admin/order/status", False)}
    assert _canonical("/api/admin/order/", True, canon) == "/api/admin/order/{id}"
    assert len(front.tiles) >= 15, f"плиток «Ещё» разобрано {len(front.tiles)} — карта разделов уехала"
    assert front.tiles["adminDash"] == "finance" and front.tiles["adminTasks"] == ""
    assert len(front.bodies) >= 40, f"компонентов разобрано {len(front.bodies)} — файлы админки уехали"
    unresolved = sorted(s for s, comp in front.screens.items()
                        if s.startswith("admin") and front.local(comp) not in front.bodies)
    assert not unresolved, f"экраны админки без компонента: {unresolved}"


def test_operator_screens_snapshot(front, canon):
    """Список экранов мобильной админки, доступных оператору, совпадает с эталоном.

    Плитка, у которой убрали право, — это открытие раздела наёмному сотруднику. Оно должно быть
    решением, а не опечаткой, потому и снимается отдельно от вызовов."""
    screens, _gaps = _operator_view(front, canon, webguard._route_cap)
    assert screens == OPERATOR_SCREENS, (
        f"состав экранов оператора изменился: появились {sorted(screens - OPERATOR_SCREENS)}, "
        f"пропали {sorted(OPERATOR_SCREENS - screens)}")


def test_operator_screens_call_only_permitted_routes(front, canon):
    """Вызовы владельческих ручек с экранов, открытых оператору, совпадают с эталоном.

    Ровно тот разъезд, которого не видно глазами: экран открыт, кнопка нарисована, сервер молча
    отвечает 403 — ни исключения во фронте, ни строчки в логе бота."""
    _screens, gaps = _operator_view(front, canon, webguard._route_cap)
    new = sorted(gaps - OPERATOR_GAPS)
    gone = sorted(OPERATOR_GAPS - gaps)
    assert not new and not gone, (
        "права команды и экраны мобильной админки разъехались с эталоном:\n"
        + "".join(f"  ПОЯВИЛОСЬ  {r}\n" for r in new)
        + "".join(f"  ПРОПАЛО    {r}\n" for r in gone))


def test_every_snapshot_gap_names_a_real_capability(front, canon):
    """Право в каждой строке эталона — настоящее право из roles.ALL_CAPS, а путь — настоящий путь.

    Опечатка в праве («printers» вместо «printer») никого не роняет: такого права нет ни у кого,
    и ручка тихо закрывается для всех, включая владельца."""
    bad_caps = sorted({cap for _s, _m, _p, cap in OPERATOR_GAPS if cap not in roles.ALL_CAPS})
    assert not bad_caps, f"в эталоне неизвестные права: {bad_caps}"
    bad_paths = sorted({p for _s, _m, p, _c in OPERATOR_GAPS if p not in canon})
    assert not bad_paths, f"в эталоне пути, которых нет в таблице маршрутов: {bad_paths}"
    unknown = sorted({s for s, _m, _p, _c in OPERATOR_GAPS} - OPERATOR_SCREENS)
    assert not unknown, f"в эталоне экраны, которых нет среди операторских: {unknown}"


def test_rights_contract_bites_on_narrowed_capability(front, canon):
    """Прибор обязан краснеть. Сужаем право у ручки, открытой оператору сейчас.

    /api/admin/queue сегодня CAP_ORDERS. Делаем её денежной — и экран «Очередь», который
    оператору по-прежнему показан, обязан всплыть новой строкой."""
    assert webguard._route_cap("/api/admin/queue") == roles.CAP_ORDERS, "исходное право уехало"

    def narrowed(path: str) -> str:
        return roles.CAP_FINANCE if path == "/api/admin/queue" else webguard._route_cap(path)

    _screens, gaps = _operator_view(front, canon, narrowed)
    added = gaps - OPERATOR_GAPS
    assert ("adminQueue", "adminQueue", "/api/admin/queue", roles.CAP_FINANCE) in added, sorted(added)


def test_rights_contract_bites_on_ungated_tile(front, canon):
    """Прибор обязан краснеть. Снимаем право с плитки «Дашборд» — она открывается оператору.

    Это вторая половина контракта: дыру можно проделать не только со стороны сервера (сузили
    право), но и со стороны фронта (открыли раздел). Ловиться должны обе."""
    path = ADMIN_JS[0]
    broken = _read(path).replace("'adminDash', 'finance'", "'adminDash'")
    assert "'adminDash', 'finance'" not in broken, "мутация не сработала — плитка «Дашборд» переписана"

    hacked = _Front(lambda p: broken if p == path else _read(p))
    assert hacked.tiles["adminDash"] == "", "плитка не разобралась после мутации"
    screens, gaps = _operator_view(hacked, canon, webguard._route_cap)
    assert "adminDash" in screens, "открытый раздел не попал в список экранов оператора"
    assert ("adminDash", "adminDashboard", "/api/admin/dashboard", roles.CAP_FINANCE) in gaps - OPERATOR_GAPS


def test_desk_operator_is_impossible_today_and_that_is_why_the_gates_are_untested_live():
    """Почему гейты роли в ДЕСКЕ сегодня не проверить живьём — и почему они всё равно нужны.

    Файлы деска сервер отдаёт только локальным запросам (webapp._ui_file), а токен ПК-админки
    резолвится в ADMIN_IDS[0] — значит за машиной студии любой человек работает ПРАВАМИ
    ВЛАДЕЛЬЦА, и `can()` в деске возвращает true всем. Это же и есть открытый вопрос продажи
    чужим студиям: пока вход один на машину, роли в деске декоративны.

    Тест держит допущение. Научится токен отдавать чужой uid — он покраснеет, и это будет
    сигналом, что снимок DESK_GAPS из проверки «на будущее» стал проверкой боевого поведения."""
    assert config.ADMIN_IDS, "ADMIN_IDS пуст — допущение про резолв токена проверить нечем"
    src = (ROOT / "app/webauth.py").read_text(encoding="utf-8")
    assert "ADMIN_IDS[0]" in src, (
        "токен ПК-админки больше не резолвится в ADMIN_IDS[0] — в деске появился настоящий "
        "оператор: снимок DESK_GAPS теперь описывает то, что человек видит на самом деле")


# ══════════════════════ прибор 3: права в НАСТОЛЬНОЙ админке ══════════════════════

DESK_SHELL = "app/webui/desk/src/shell.jsx"
DESK_MAIN = "app/webui/desk/src/main.jsx"
DESK_SCREENS_DIR = "app/webui/desk/src/screens"

# ['ключ', 'Подпись', 'иконка', {cap: 'право'}] — четвёртый элемент необязателен
_NAV_RE = re.compile(r"\[\s*'(\w+)'\s*,\s*'[^']*'\s*,\s*'[^']*'\s*(?:,\s*(\{[^}]*\}))?\s*\]")
_DESK_CALL_RE = re.compile(r"\b(?:useApi|call|usePoll)\(\s*'([A-Za-z_]\w*)'")


def _desk_nav_caps(read) -> dict[str, str]:
    """Раздел настольной админки → право, закрывающее его в сайдбаре (или '' — открыт всем)."""
    out: dict[str, str] = {}
    for m in _NAV_RE.finditer(read(DESK_SHELL)):
        cap = re.search(r"cap:\s*'(\w+)'", m.group(2) or "")
        out[m.group(1)] = cap.group(1) if cap else ""
    return out


def _desk_screen_files(read) -> dict[str, str]:
    """Раздел → файл экрана. Обе половины из main.jsx: карта SCREENS и импорты компонентов."""
    src = read(DESK_MAIN)
    comp = {m.group(1): m.group(2) for m in
            re.finditer(r"import\s*\{\s*(\w+)\s*\}\s*from\s*'\./screens/(\w+)\.jsx'", src)}
    return {m.group(1): comp[m.group(2)]
            for m in re.finditer(r"(\w+):\s*(\w+Screen)", src) if m.group(2) in comp}


def _desk_gaps(read, canon: frozenset[str], route_cap) -> frozenset[tuple[str, str, str, str]]:
    """Вызовы владельческих ручек с разделов, открытых оператору.

    ГРАНИЦА ПРИБОРА, и её надо знать: он смотрит ФАЙЛ экрана целиком и не видит, стоит ли
    перед вызовом гейт (`can('finance') ? <Card/> : null`). Поэтому это снимок с приговорами,
    как и у мобильной админки, а не «список дыр». Общие компоненты из parts.jsx сюда тоже не
    попадают — они не привязаны к экрану."""
    caps = roles.ROLE_CAPS[config.ROLE_OPERATOR]
    nav = _desk_nav_caps(read)
    api = _api_methods(read)
    gaps: set[tuple[str, str, str, str]] = set()
    for screen, fname in sorted(_desk_screen_files(read).items()):
        if nav.get(screen):
            continue                      # раздел закрыт правом — оператор его не открывает
        src = read(f"{DESK_SCREENS_DIR}/{fname}.jsx")
        for meth in sorted(set(_DESK_CALL_RE.findall(src))):
            for url, is_prefix in sorted(api.get(meth, ())):
                path = _canonical(url, is_prefix, canon)
                need = "НЕ ОПОЗНАН" if path is None else route_cap(path)
                if need not in caps:
                    gaps.add((screen, meth, path or url, need))
    return frozenset(gaps)


# ─── эталон: владельческие вызовы с разделов деска, открытых оператору ───
# Приговор по каждой группе проставлен вручную 12.08.2026 — разбор шёл по коду.
DESK_GAPS: frozenset[tuple[str, str, str, str]] = frozenset({
    # ГЕЙТ ЕСТЬ: `can('settings')` — вкладка «Разбор ссылок» и её кнопка (journal.jsx),
    # запрос ещё и не улетает (useApi skip).
    ("journal", "adminCalcQueue", "/api/admin/calc_queue", "settings"),
    ("journal", "adminCalcQueueResolve", "/api/admin/calc_queue_resolve", "settings"),
    # ГЕЙТ ЕСТЬ: денежные секции карточки заказа рисуются по canMoney, а его СЕРВЕР выставляет
    # в false оператору (money из ответа /api/admin/order/{id}).
    ("orders", "adminBalance", "/api/admin/balance", "finance"),
    ("orders", "adminOrderBill", "/api/admin/order/bill", "finance"),
    ("orders", "adminOrderDelivery", "/api/admin/order/delivery", "finance"),
    ("orders", "adminOrderInvoice", "/api/admin/order/invoice", "finance"),
    ("orders", "adminOrderPaid", "/api/admin/order/paid", "finance"),
    ("orders", "adminOrderPrice", "/api/admin/order/price", "finance"),
    ("orders", "adminOrderPriceByGt", "/api/admin/order/price", "finance"),
    ("orders", "adminOrderRecalc", "/api/admin/order/recalc", "finance"),
    ("orders", "adminOrderRecalcApply", "/api/admin/order/recalc_apply", "finance"),
    # ГЕЙТ ЕСТЬ: `can('finance')` на кнопках «За клиента» и «Пакет ссылок» — ящики без них
    # не открываются, а всё, что внутри, живёт только в ящике.
    ("orders", "adminOrderCreate", "/api/admin/order/create", "finance"),
    ("orders", "adminBatchCalc", "/api/admin/batch_calc", "finance"),
    ("orders", "adminBatchSend", "/api/admin/batch_send", "finance"),
    ("orders", "adminBatchCancel", "/api/admin/batch_cancel", "settings"),
    ("orders", "adminBatchLast", "/api/admin/batch_last", "settings"),
    ("orders", "adminBatchStatus", "/api/admin/batch_status", "settings"),
    ("orders", "adminClients", "/api/admin/clients", "clients"),
    # ГЕЙТ ЕСТЬ: `can('settings')` — обе кнопки удаления (массовая и в меню заказа) и «Экспорт
    # CSV», «Удалить тестовые заказы», вкладка «Для себя».
    ("orders", "adminOrderDelete", "/api/admin/order/delete", "settings"),
    ("orders", "adminOrdersCleanTest", "/api/admin/orders/clean_test", "settings"),
    ("orders", "adminSelfOrders", "/api/admin/self_orders", "settings"),
    ("print", "adminSelfOrders", "/api/admin/self_orders", "settings"),
    ("print", "adminSelfPrint", "/api/admin/self_print", "settings"),
    # ОСОЗНАННО ОТКРЫТО: справочник перевозчиков нужен оператору для трек-номера, но лежит под
    # «Прайсом» (в нём цены тарифов). Запрос уходит и получает отказ; интерфейс это ПРИЗНАЁТ —
    # показывает подсказку про ручную ссылку отслеживания вместо пустого списка.
    ("orders", "adminDelivery", "/api/admin/delivery", "pricing"),
    # ГЕЙТ ЕСТЬ: `can('finance')` / `can('clients')` — обе карточки на «Пульте» подменяются.
    ("pult", "adminDashboard", "/api/admin/dashboard", "finance"),
    ("pult", "adminActivity", "/api/admin/activity", "clients"),
    # ГЕЙТ ЕСТЬ: `can('settings')` — карточки обновлений, ИИ, пробы генерации и токена
    # MakerWorld на экране «Статус».
    ("status", "adminUpdateCheck", "/api/admin/update_check", "settings"),
    ("status", "adminUpdateApply", "/api/admin/update_apply", "settings"),
    ("status", "adminUpdateMode", "/api/admin/update_mode", "settings"),
    ("status", "adminAiAction", "/api/admin/ai", "settings"),
    ("status", "adminAiTest", "/api/admin/ai_test", "settings"),
    ("status", "adminMwAccount", "/api/admin/mw_account", "settings"),
})


def test_desk_parser_still_sees_the_code(canon):
    """Прибор обязан кричать, если разучился читать деск, — иначе он зеленеет на пустом разборе."""
    nav = _desk_nav_caps(_read)
    files = _desk_screen_files(_read)
    assert nav.get("dash") == roles.CAP_FINANCE, "NAV деска не разобрался"
    assert files.get("pult") == "pult" and files.get("orders") == "orders", "карта экранов не разобралась"
    assert len([s for s, c in nav.items() if not c]) >= 5, "открытых оператору разделов подозрительно мало"


def test_desk_operator_screens_call_only_permitted_routes(canon):
    """Снимок владельческих вызовов с разделов деска, открытых оператору."""
    now = _desk_gaps(_read, canon, webguard._route_cap)
    new = now - DESK_GAPS
    gone = DESK_GAPS - now
    msg = []
    if new:
        msg.append("ПОЯВИЛОСЬ (закрыть правом, спрятать гейтом или вписать в эталон С ПРИГОВОРОМ):\n"
                   + "\n".join(f"    {g}" for g in sorted(new)))
    if gone:
        msg.append("ПРОПАЛО (вызов убрали или право расширили — почистить эталон):\n"
                   + "\n".join(f"    {g}" for g in sorted(gone)))
    assert not msg, "\n".join(msg)


def test_desk_gaps_name_real_capabilities(canon):
    """Каждая строка эталона обязана называть НАСТОЯЩЕЕ право и настоящий маршрут."""
    for screen, meth, path, need in sorted(DESK_GAPS):
        assert need in roles.ALL_CAPS, f"{screen}/{meth}: право «{need}» не существует"
        assert path in canon, f"{screen}/{meth}: путь {path} не найден в таблице маршрутов"


def test_desk_rights_contract_bites_on_a_new_ungated_call(canon):
    """Мутация: дописали на открытый оператору экран вызов владельческой ручки — прибор краснеет."""
    path = f"{DESK_SCREENS_DIR}/tasks.jsx"
    broken = _read(path) + "\nconst leak = () => call('adminDashboard');\n"
    gaps = _desk_gaps(lambda p: broken if p == path else _read(p), canon, webguard._route_cap)
    assert ("tasks", "adminDashboard", "/api/admin/dashboard", roles.CAP_FINANCE) in gaps - DESK_GAPS


def test_desk_rights_contract_bites_when_a_section_loses_its_cap(canon):
    """Мутация: сняли право с раздела в NAV — все его владельческие вызовы обязаны всплыть."""
    broken = _read(DESK_SHELL).replace("['dash', 'Дашборд', 'chart', { cap: 'finance' }]",
                                       "['dash', 'Дашборд', 'chart']")
    assert broken != _read(DESK_SHELL), "мутация не применилась — строка NAV изменилась"
    gaps = _desk_gaps(lambda p: broken if p == DESK_SHELL else _read(p), canon, webguard._route_cap)
    assert any(g[0] == "dash" for g in gaps - DESK_GAPS), "открытый дашборд не попал в расхождения"
