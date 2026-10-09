"""Какой слайсер режет: ElegooSlicer, Bambu Studio или Orca Slicer.

Зачем слой. Слайсер в проекте был ОДИН — `config.SLICER_PATH` из .env, и всё, что о нём знает
код (флаги CLI, коды отказа, имена выходных файлов, потребность в санитайзере bambu-3mf),
разъехалось по `app/slicer.py`, `app/calcservice.py` и `app/webapp.py`. Второй принтер это
ломает не гипотетически: у Bambu Lab свои профили, и резать их ElegooSlicer'ом означает считать
клиенту цену по чужой машине.

**Все три — одна семья.** Orca Slicer — форк BambuStudio, ElegooSlicer — форк Orca, поэтому CLI
у них общий по происхождению: `--load-settings "machine.json;process.json"`, `--load-filaments`,
`--slice 0`, `--arrange 0|1`, `--outputdir`, `--allow-newer-file`. Именно поэтому сборка
аргументов здесь ОДНА на всех, а различия вынесены полями — их и править, когда они найдутся.

**Что тут проверено, а что нет — честно.** Флаги, коды выхода (−101/−100/−50) и формат
gcode-комментариев сняты живьём с ElegooSlicer 1.5.1.6/1.5.2.2. Для Bambu Studio и Orca они
взяты из общего предка и НЕ проверены запуском на этой машине: ни того, ни другого здесь не
установлено. Первое, что стоит сделать у железа, — прогнать `tests/test_slicer_backends.py`
и один настоящий слайс каждым движком; расхождения лечатся правкой таблицы ниже, а не кода.

Чего слой сознательно НЕ делает: не трогает калибровку мастера (она физически лежит в
%APPDATA%\\<Слайсер>\\user\\default и в карточках пластиков абсолютными путями) и не пытается
конвертировать профили между движками. Профили — дело карточки принтера.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlicerBackend:
    """Один движок слайсера: где он лежит и чем отличается от родственников."""

    kind: str
    title: str
    # Типовые пути установки на Windows — по ним движок находится, если путь не задан явно.
    default_paths: tuple[str, ...] = ()
    # Санитайзер bambu-3mf (app/bambu3mf.py) подменяет project_settings.config целиком на шаблон
    # Centauri Carbon 2. Для ElegooSlicer это лекарство: проекты BambuStudio он иначе не режет
    # вовсе (версионный гейт + бамбу-специфичные значения). Для самого Bambu Studio и для Orca
    # это ПОРЧА авторских настроек, причём необратимая: sanitize правит файл на месте.
    wants_bambu_sanitizer: bool = True
    # Папка пользовательских пресетов (калибровка мастера). Нужна только для диагностики и
    # снимков calibration.snapshot_user_presets — слайсеру не передаётся.
    user_dir_name: str = ""
    # Как этот движок подписывает сводку в gcode. РАЗНАЯ у семейств, и это выяснено запуском
    # (12.08.2026), а не предположением:
    #   ElegooSlicer/Orca: «; filament used [cm3] = 0.95»
    #                      «; estimated printing time (normal mode) = 4m 12s»
    #   Bambu Studio:      «; total filament volume [cm^3] : 1008.40»
    #                      «; model printing time: 10m 14s; total estimated time: 10m 14s»
    cm3_pattern: str = r"filament used \[cm3\]\s*=\s*([0-9.,\s]+)"
    time_pattern: str = r"estimated printing time \(normal mode\)\s*=\s*([0-9hms\s]+)"
    time_pattern_any: str = r"estimated printing time[^=\n]*=\s*([0-9hms\s]+)"
    # МНОЖИТЕЛЬ ОБЪЁМА. У Bambu Studio поле подписано «[cm^3]», а содержит МИЛЛИМЕТРЫ кубические:
    # проверено арифметикой на кубике 13 мм — «total filament length [mm] : 419.24» при прутке
    # 1.75 мм (сечение 2.4053 мм²) даёт ровно 1008.4, то есть напечатанное в поле число. Принять
    # его за см³ значит завысить расход и цену в ТЫСЯЧУ раз.
    cm3_scale: float = 1.0

    def path(self, override: str = "") -> str:
        """Путь к exe: явный override карточки → .env (для движка по умолчанию) → автопоиск.

        Пустая строка означает «движок не установлен»: вызывающий обязан отличать это от
        «слайсинг выключен», иначе неверный путь превратится не в ошибку, а в тихий откат на
        приблизительную оценку — и цена у клиента просто станет другой."""
        if (override or "").strip():
            return override.strip()
        env = (str(config.SLICER_PATH) or "").strip()
        if env and self.kind == DEFAULT_KIND:
            return env
        for p in self.default_paths:
            if Path(p).exists():
                return p
        found = shutil.which(Path(self.default_paths[0]).name) if self.default_paths else None
        return found or ""

    def installed(self, override: str = "") -> bool:
        p = self.path(override)
        return bool(p) and Path(p).exists()

    def slice_args(self, exe: str, *, settings: list[str], filament: str = "",
                   arrange: str | None = None, outdir: str, inputs: list[str]) -> list[str]:
        """Полная командная строка одного слайса.

        Одна на все три движка — они родственники, и разъезд аргументов был бы выдумкой, а не
        фактом. `settings` склеиваются через `;` В ОДНОМ аргументе (так требует CLI семейства);
        `arrange=None` означает «не передавать вовсе» — так режется проект-3mf с авторской
        раскладкой, которую переразмещать нельзя."""
        args = [exe, "--allow-newer-file", "--load-settings", ";".join(settings)]
        if filament:
            args += ["--load-filaments", filament]
        args += ["--slice", "0"]
        if arrange is not None:
            args += ["--arrange", arrange]
        args += ["--outputdir", outdir, *inputs]
        return args


DEFAULT_KIND = "elegoo"

BACKENDS: dict[str, SlicerBackend] = {
    "elegoo": SlicerBackend(
        kind="elegoo", title="ElegooSlicer",
        default_paths=(r"C:\Program Files\ElegooSlicer\elegoo-slicer.exe",),
        wants_bambu_sanitizer=True, user_dir_name="ElegooSlicer"),
    "bambu": SlicerBackend(
        kind="bambu", title="Bambu Studio",
        default_paths=(r"C:\Program Files\Bambu Studio\bambu-studio.exe",),
        # Свой же формат проекта — санировать нечего и нельзя: шаблон Centauri Carbon 2 стёр бы
        # авторские настройки Bambu, ради которых проект и открывают этим движком.
        wants_bambu_sanitizer=False, user_dir_name="BambuStudio",
        # Своя сводка в gcode и своя единица объёма — снято запуском 12.08.2026, см. cm3_scale.
        cm3_pattern=r"total filament volume \[cm\^3\]\s*:\s*([0-9.,\s]+)",
        time_pattern=r"model printing time:\s*([0-9hms\s]+)",
        time_pattern_any=r"total estimated time:\s*([0-9hms\s]+)",
        cm3_scale=0.001),
    "orca": SlicerBackend(
        kind="orca", title="Orca Slicer",
        default_paths=(r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
                       r"C:\Program Files\Orca Slicer\orca-slicer.exe"),
        # Orca читает проекты BambuStudio родным образом (он их предок по формату).
        wants_bambu_sanitizer=False, user_dir_name="OrcaSlicer"),
}


def for_kind(kind: str | None) -> SlicerBackend:
    """Бэкенд по имени. Неизвестное имя — НЕ повод падать: сборка покупателя могла приехать с
    движком из будущей версии. Работаем движком по умолчанию и говорим об этом в лог."""
    k = (kind or "").strip().lower()
    if not k:
        return BACKENDS[DEFAULT_KIND]
    b = BACKENDS.get(k)
    if b is None:
        log.warning("Неизвестный слайсер «%s» — режем %s", kind, BACKENDS[DEFAULT_KIND].title)
        return BACKENDS[DEFAULT_KIND]
    return b


def for_printer(printer) -> SlicerBackend:
    """Каким движком резать задания ЭТОЙ карточки принтера.

    Пустое поле карточки = движок по умолчанию, то есть ровно сегодняшнее поведение установки с
    одним принтером. Карточку Bambu без явного выбора мы намеренно НЕ угадываем по `kind`:
    мастер вправе резать Bambu в Orca, и молчаливая подмена движка означала бы другую цену."""
    return for_kind(getattr(printer, "slicer", "") if printer is not None else "")


def path_for_printer(printer) -> str:
    """Путь к exe для карточки: её override → общие правила бэкенда."""
    return for_printer(printer).path(getattr(printer, "slicer_path", "") or "")


def installed_kinds(overrides: dict[str, str] | None = None) -> list[str]:
    """Какие движки реально стоят на этой машине — для админки, чтобы список выбора не предлагал
    того, чего нет, и чтобы «слайсер не отвечает» не выяснялось ценой заказа.

    `overrides` — пути exe из карточек принтеров ({kind: path}). Без них портативная сборка
    (движок лежит рядом с проектом, а не в Program Files) считалась бы неустановленной, хотя
    именно ею и режут: список выбора терял бы рабочий движок."""
    ov = overrides or {}
    return [k for k, b in BACKENDS.items() if b.installed(ov.get(k, ""))]


def describe(overrides: dict[str, str] | None = None) -> list[dict]:
    """Список движков для интерфейса: {kind, title, installed, path, roots}.

    `roots` — обнаруженные корни профилей (см. profile_roots): по ним видно, откуда движок
    возьмёт профили поставки и калибровку мастера, и почему профиль «не найден»."""
    ov = overrides or {}
    out = []
    for k, b in BACKENDS.items():
        p = b.path(ov.get(k, ""))
        out.append({"kind": k, "title": b.title, "path": p,
                    "installed": bool(p and Path(p).exists()),
                    "roots": {n: str(v) for n, v in profile_roots(k, ov.get(k, "")).items()},
                    "default": k == DEFAULT_KIND})
    return out


# ---- Корни профилей движка (C2, хвост: отвязка профилей слайсера от машины владельца) ----
# ТРИ корня, а не два. Третий («system») в коде раньше не упоминался вовсе, а он и есть тот,
# куда слайсер кладёт ОБНОВЛЁННЫЕ профили поставки: %APPDATA%\<Слайсер>\system\<Вендор>\…
# — полное зеркало resources\profiles\<Вендор>. На чужой машине версия там может быть свежее
# установочной, а хвост пути у обоих корней ОДИНАКОВ («Elegoo/machine/ECC2/x.json») — именно
# поэтому перекоренение resources↔system получается точным, а не гадательным.
ROOT_NAMES = ("user", "resources", "system")


def profile_roots(kind: str | None = None, exe_override: str = "") -> dict[str, Path]:
    """Где у ЭТОГО движка лежат профили: {'user'|'resources'|'system': каталог}.

    Возвращаются только СУЩЕСТВУЮЩИЕ каталоги; движка нет — пустой словарь (вызывающий обязан
    отличать «корней нет» от «файла под корнем нет»: первое лечится установкой слайсера,
    второе — калибровкой, и владельцу это разные новости).

    Корень `resources` — именно «<каталог exe>/resources/profiles», УРОВЕНЬ ВЕНДОРА ВНУТРИ:
    так относительный хвост начинается с имени вендора и совпадает с тем, что ищет
    slicer._flatten_profile (он поднимается до предка, чей родитель зовётся `profiles`).
    Унаследованный config.PROFILES_DIR указывает на вендора («…/profiles/Elegoo») — поднимаем
    его на уровень вверх, иначе маркеры разъедутся на один сегмент.

    Кэша нет сознательно: три Path.exists() стоят копейки, а любой кэш без ключа перепутал бы
    корни между тестами (они подменяют APPDATA/SLICER_PATH/PROFILES_DIR) и застрял бы на
    неделю в живом боте, пережившем переустановку слайсера.
    """
    be = for_kind(kind)
    out: dict[str, Path] = {}

    res: Path | None = None
    if be.kind == DEFAULT_KIND:
        pd = Path(config.PROFILES_DIR)
        cand = pd.parent if pd.parent.name.lower() == "profiles" else pd
        if _dir_ok(cand):
            res = cand
    if res is None:
        exe = be.path(exe_override)
        if exe:
            cand = Path(exe).parent / "resources" / "profiles"
            if _dir_ok(cand):
                res = cand
    if res is not None:
        out["resources"] = res

    if be.user_dir_name:
        appdata = os.getenv("APPDATA", "")
        if appdata:
            sysroot = Path(appdata) / be.user_dir_name / "system"
            if _dir_ok(sysroot):
                out["system"] = sysroot
    # Пользовательский корень: для движка по умолчанию уважаем SLICER_USER_DIR из .env —
    # его же читает app/calibration.py, и два источника правды здесь разъехались бы молча.
    udir = Path(config.SLICER_USER_DIR) if be.kind == DEFAULT_KIND else user_presets_dir(be.kind)
    if udir is not None and _dir_ok(udir):
        out["user"] = udir
    return out


def _dir_ok(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


# Флаги, на которых держится ВСЯ сборка команды (см. SlicerBackend.slice_args). Для ElegooSlicer
# они сняты живьём; для Bambu Studio и Orca взяты от общего предка и НЕ проверены запуском —
# ни того, ни другого на машине разработки нет. Проверка ниже превращает это допущение в факт
# ровно в тот момент, когда движок появится.
REQUIRED_FLAGS = ("--load-settings", "--load-filaments", "--slice", "--arrange", "--outputdir")

# Как движок семейства Orca отвечает на разбор командной строки (снято живьём с ElegooSlicer
# 1.5.x, exit -2 в обоих случаях):
#   неизвестное имя ключа      → «Invalid option --что-то»
#   имя знакомо, значение нет  → «Invalid value for option --slice»
# Второе — и есть доказательство, что ключ ПОДДЕРЖИВАЕТСЯ: движок дошёл до разбора его значения.
# Оба ответа приходят за доли секунды и до всякой работы, поэтому проверка дешёвая и безопасная:
# ни одного файла не читается, ни одного слайса не запускается.
_UNKNOWN_OPT = re.compile(r"invalid option\s+(--[\w-]+)", re.I)
_KNOWN_OPT = re.compile(r"invalid value for option", re.I)


async def _probe(exe: str, args: list[str], timeout: float) -> tuple[int | None, str]:
    """Запустить движок и вернуть (код, вывод). None в коде — не ответил за timeout."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return None, ""
    return proc.returncode, (out + err).decode("utf-8", "ignore")


async def check(kind: str | None = None, path_override: str = "", timeout: float = 25.0) -> dict:
    """Проверить движок ДО первого заказа: запускается ли exe и знает ли он наши ключи.

    Зачем отдельная проверка, а не «узнаем на первом расчёте». Неверный путь или непринятый ключ
    НЕ роняют расчёт: `calcservice` ловит `SlicerError` и молча отдаёт приблизительную оценку. То
    есть ошибка выглядит не как ошибка, а как «цена стала другой» — и замечается по деньгам, а не
    по логу. Эта проверка делает отказ громким и предметным.

    Способ подсмотрен у самого движка: подаём ему наши ключи БЕЗ значений. Незнакомое имя он
    назовёт «Invalid option», знакомое — «Invalid value for option», и то и другое за доли
    секунды, до всякой работы. Ни файла не прочитано, ни слайса не запущено.

    ЧЕГО ПРОВЕРКА НЕ ДОКАЗЫВАЕТ (и врать об этом не надо): формат gcode-комментариев, коды
    отказов лестницы фолбэков и совпадение результата с ElegooSlicer. Это выясняется настоящим
    слайсом одной и той же модели каждым движком."""
    be = for_kind(kind)
    exe = be.path(path_override)
    out: dict = {"kind": be.kind, "title": be.title, "path": exe, "ok": False,
                 "installed": bool(exe and Path(exe).exists()), "missing_flags": [],
                 "flags_checked": False, "error": ""}
    if not out["installed"]:
        out["error"] = ("не найден: укажите путь в карточке принтера"
                        if exe else "путь не задан и по типовым местам ничего нет")
        return out
    try:
        rc, text = await _probe(exe, list(REQUIRED_FLAGS), timeout)
    except OSError as exc:
        out["error"] = f"не запускается: {exc}"
        return out
    if rc is None:
        out["error"] = (f"не ответил за {timeout:g} с — возможно, вместо CLI открылось окно "
                        f"или движок начал работу")
        return out

    unknown = {m.group(1) for m in _UNKNOWN_OPT.finditer(text)}
    out["missing_flags"] = [f for f in REQUIRED_FLAGS if f in unknown]
    if out["missing_flags"]:
        out["error"] = ("движок не знает ключей: " + ", ".join(out["missing_flags"])
                        + ". Сборка команды под него не подойдёт — правьте app/slicers.py")
        return out
    if _KNOWN_OPT.search(text) or unknown:
        # движок разбирает командную строку так же, как семейство Orca, и наши ключи ему знакомы
        out["flags_checked"] = True
        out["ok"] = True
        return out
    # Движок запустился, но об аргументах ничего не сказал — значит проверить ключи этим способом
    # не вышло. Честнее сознаться, чем выдать «всё хорошо».
    out["ok"] = True
    out["error"] = ("движок запускается, но про наши ключи ничего не ответил — проверить их этим "
                    "способом не вышло, нужен настоящий пробный слайс")
    return out


def user_presets_dir(kind: str | None = None) -> Path | None:
    """Папка пользовательских пресетов движка (%APPDATA%\\<Имя>\\user\\default).

    Нужна снимкам калибровки: значения мастера (MVS, поток, Z-offset) живут там, а не в профилях
    репозитория, и при смене движка молча заменяются стоковыми — система при этом выглядит
    исправной. None — движок не именует свою папку."""
    name = for_kind(kind).user_dir_name
    if not name:
        return None
    appdata = os.getenv("APPDATA", "")
    return Path(appdata) / name / "user" / "default" if appdata else None
