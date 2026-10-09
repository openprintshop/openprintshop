"""Пути к профилям слайсера в БД — относительные, с маркером корня (хвост задачи C2).

Зачем. C2 отвязал от машины владельца ДАННЫЕ (DATA_DIR, `config.rel_data/abs_data`), но вторая
привязка осталась в тех же строках БД: профили слайсера лежали абсолютными путями вида
«C:/Program Files/ElegooSlicer/resources/profiles/Elegoo/machine/ECC2/…json» и
«%APPDATA%/ElegooSlicer/user/default/filament/…json». У чужой студии слайсер стоит в другом
каталоге, под другим пользователем и другой версии — база из бэкапа или из установщика
указывает в пустоту, а расчёт при этом НЕ падает: `slicer.slice_unit` молча берёт стоковый
филамент, `materials.resolve_filament` молча игнорирует несуществующий `prefer`, `fastcalc`
откатывается на дефолтные настройки. То есть поломка выглядит не как ошибка, а как «цена стала
другой». Маркер корня делает промах диагностируемым и в большинстве случаев — самоизлечимым.

Формат значения в БД: `@<движок>/<корень>/<хвост в POSIX>`, например

    @elegoo/resources/Elegoo/machine/ECC2/Elegoo Centauri Carbon 2 0.4 nozzle.json
    @elegoo/user/filament/0.6 Elegoo PETG.json

Движок в маркере обязателен, потому что у `calib_presets.path` при чтении НЕТ контекста
принтера: «@user/filament/x.json» без движка одинаково подошёл бы и ElegooSlicer, и Orca, а это
разные каталоги. Бескиндовый вид «@resources/…»/«@user/…» на ЧТЕНИИ принимается как движок по
умолчанию — чтобы значение, записанное чужой (более старой) сборкой, не осталось нерезолвимым.

Что НЕ является маркером и потому проходит запись-чтение ДОСЛОВНО:
  * пустая строка и None (колонки nullable, «очистить поле» обязано остаться очисткой);
  * относительный путь без маркера («mp.json», «proc») — так пишут десятки тестов и, возможно,
    ручные карточки; развернуть его к корню слайсера значило бы молча увести расчёт в
    Program Files. Единственный триггер разворота — символ «@» в начале;
  * абсолютный путь вне известных корней (профиль, который мастер положил куда захотел).

`@` в начале — надёжный дискриминатор: у абсолютного пути Windows его не бывает, у
относительного пути данных (C2) — тоже.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import slicers

log = logging.getLogger(__name__)

MARK = "@"

# Приоритет корней при ЗАПИСИ. Один и тот же файл может лежать под двумя корнями
# (resources — поставка, system — её OTA-зеркало в %APPDATA%), и без фиксированного порядка
# `rel_profile` перестала бы быть детерминированной: миграция давала бы разный результат при
# повторных прогонах, а `calib_presets.path` (по нему группируются версии снимков) — разные
# группы для одного пресета.
_WRITE_ORDER = ("user", "resources", "system")

# Взаимозаменяемые корни при ЧТЕНИИ. Хвосты у `resources` и `system` совпадают посегментно
# («Elegoo/machine/ECC2/x.json»), поэтому перекоренение между ними точное, а не гадательное:
# не нашли файл под корнем поставки — пробуем тот же хвост в OTA-зеркале, и наоборот.
_TWINS: dict[str, tuple[str, ...]] = {
    "resources": ("resources", "system"),
    "system": ("system", "resources"),
    "user": ("user",),
}


def _kinds(kind: str | None) -> tuple[str, ...]:
    """Какие движки перебирать. Известное имя — только его; иначе все, по умолчанию первым."""
    k = (kind or "").strip().lower()
    if k in slicers.BACKENDS:
        return (k,)
    return (slicers.DEFAULT_KIND,
            *(x for x in slicers.BACKENDS if x != slicers.DEFAULT_KIND))


def _norm(p: str | Path) -> str:
    return str(p).replace("\\", "/").rstrip("/")


def _exists(p: Path) -> bool:
    try:
        return p.exists()
    except OSError:
        return False


def is_marker(value: str | Path | None) -> bool:
    return bool(value) and str(value).startswith(MARK)


def parse_marker(value: str) -> tuple[str, str, str] | None:
    """«@elegoo/user/filament/x.json» → ('elegoo', 'user', 'filament/x.json'). Не маркер → None."""
    if not value.startswith(MARK):
        return None
    parts = value[1:].split("/")
    if parts and parts[0] in slicers.ROOT_NAMES:          # бескиндовый вид: движок по умолчанию
        kind, root, tail = slicers.DEFAULT_KIND, parts[0], "/".join(parts[1:])
    elif len(parts) >= 2 and parts[1] in slicers.ROOT_NAMES:
        kind, root, tail = parts[0].strip().lower(), parts[1], "/".join(parts[2:])
    else:
        return None
    return (kind, root, tail) if tail else None


def rel_profile(path: str | Path | None, kind: str | None = None,
                exe_override: str = "") -> str | None:
    """Путь под известным корнем слайсера → маркер; всё прочее — как есть.

    Применяется на ЗАПИСИ в БД. Идемпотентна: маркер остаётся маркером, путь вне корней —
    дословным, пустое значение — пустым.
    """
    if path is None:
        return None
    s = str(path)
    if not s or s.startswith(MARK):
        return s
    if not Path(s).anchor:
        # Относительное не-маркерное значение («mp.json») — чужая система координат, не наша.
        return s
    norm = _norm(s)
    low = norm.casefold()
    for k in _kinds(kind):
        roots = slicers.profile_roots(k, exe_override)
        for name in _WRITE_ORDER:
            r = roots.get(name)
            if r is None:
                continue
            rl = _norm(r).casefold()
            if low.startswith(rl + "/"):
                return f"{MARK}{k}/{name}/{norm[len(rl) + 1:]}"
    return s


# Опознаваемые хвосты абсолютного пути — страховка для БД, приехавшей с чужой машины БЕЗ
# миграции (её же логику повторяет config.abs_data): «…/resources/profiles/<хвост>»,
# «…/<Слайсер>/system/<хвост>», «…/user/default/<хвост>».
def _tail_of(parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    for i, seg in enumerate(parts):
        low = seg.casefold()
        if low == "profiles" and i > 0 and parts[i - 1].casefold() == "resources":
            return ("resources", parts[i + 1:]) if i + 1 < len(parts) else None
        if low == "system" and i > 0 and i + 1 < len(parts):
            return "system", parts[i + 1:]
        if low == "user" and i + 1 < len(parts) and parts[i + 1].casefold() == "default":
            return ("user", parts[i + 2:]) if i + 2 < len(parts) else None
    return None


def _resolve(s: str, kind: str | None, exe_override: str) -> str | None:
    """Новая строка пути или None, если значение надо отдать ДОСЛОВНО."""
    marker = parse_marker(s) if s.startswith(MARK) else None
    if marker:
        k, name, tail = marker
        best: Path | None = None
        for kk in _kinds(k if k in slicers.BACKENDS else kind):
            roots = slicers.profile_roots(kk, exe_override)
            for nm in _TWINS.get(name, (name,)):
                r = roots.get(nm)
                if r is None:
                    continue
                cand = r.joinpath(*tail.split("/"))
                if best is None:
                    best = cand
                if _exists(cand):
                    return str(cand)
        # Корня нет вовсе (слайсер не установлен) — отдаём маркер как есть: selfcheck скажет
        # ПОЧЕМУ, а склеенный из ниоткуда путь только запутал бы.
        return str(best) if best is not None else None
    p = Path(s)
    if not p.anchor or _exists(p):
        return None
    info = _tail_of(p.parts)
    if info is None:
        return None
    name, segs = info
    if not segs:
        return None
    for kk in _kinds(kind):
        roots = slicers.profile_roots(kk, exe_override)
        for nm in _TWINS.get(name, (name,)):
            r = roots.get(nm)
            if r is None:
                continue
            cand = r.joinpath(*segs)
            # Условие «существует» обязательно: иначе чужой путь с похожим хвостом увёл бы
            # расчёт в боевую установку слайсера — ровно та же дисциплина, что у abs_data.
            if _exists(cand):
                return str(cand)
    return None


def abs_profile_str(value: str | Path | None, kind: str | None = None,
                    exe_override: str = "") -> str:
    """Значение из БД → путь для файловых операций и для показа в интерфейсе (строкой).

    Значение, которое мы не переписывали, возвращается ПОБАЙТНО тем же (не через Path): иначе
    «proc» и «a/b.json» поехали бы разделителями, а карточки принтеров сравниваются строкой.
    """
    if value is None:
        return ""
    s = str(value)
    if not s:
        return s
    new = _resolve(s, kind, exe_override)
    return new if new is not None else s


def abs_profile(value: str | Path | None, kind: str | None = None,
                exe_override: str = "") -> Path:
    """То же, но Path — для файловых операций."""
    return Path(abs_profile_str(value, kind, exe_override))


def explain(value: str | Path | None, kind: str | None = None,
            exe_override: str = "", *, want_dir: bool = False) -> str:
    """ПОЧЕМУ профиль не резолвится. Пустая строка = всё на месте (или значение пустое).

    Три разных диагноза вместо прежнего «нет профиля»: движка нет вовсе; корень есть, а файла
    под ним нет (обновление слайсера сбросило пресет мастера); путь вообще не из установки
    слайсера. Владельцу это три разных действия, а не одно.
    """
    if not value:
        return ""
    resolved = abs_profile(value, kind, exe_override)
    try:
        ok = resolved.is_dir() if want_dir else resolved.is_file()
    except OSError:
        ok = False
    if ok:
        return ""
    roots = slicers.profile_roots(kind, exe_override)
    be = slicers.for_kind(kind)
    if not roots:
        exe = be.path(exe_override)
        return (f"{be.title} не найден (профили брать неоткуда"
                + (f", exe: {exe}" if exe else ", путь не задан") + ")")
    s = str(value)
    m = parse_marker(s) if s.startswith(MARK) else None
    if m:
        _k, name, tail = m
        root = roots.get(name)
        where = f" (корень {root})" if root is not None else " (такого корня у движка нет)"
        return f"нет файла под корнем «{name}»{where}: {tail}"
    if rel_profile(s, kind, exe_override) != s:
        return f"файла нет: {resolved}"
    return (f"путь не из установки слайсера и файла нет: {resolved}. "
            f"Корни движка: " + ", ".join(f"{n}={p}" for n, p in sorted(roots.items())))
