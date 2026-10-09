"""Редакция сборки: что делалось «под себя», а что можно отдавать всем (OpenTodo f98aa1ef).

Проект вырос из инструмента для одной мастерской в открытый продукт для любых студий
(OpenPrintShop). За это время в нём накопились две РАЗНЫЕ породы возможностей:

* **продуктовые** — расчёт, заказы, склад, журнал печатей, калибровка, принтер, оплата,
  доставка, бренд. Они работают у любого, кому продукт поставили, и ничего кроме бота
  и принтера не требуют;
* **требующие особого железа или программ** — генерация аватарок на видеокарте, рендер
  OpenSCAD, дисплей TURZX на столе, поиск по внешним каталогам. У студии без них они в лучшем
  случае молча не работают (нет OpenSCAD, нет 8 ГБ VRAM), в худшем — обещают то, чего нет,
  и портят первое впечатление. Поэтому в студийной редакции они выключены, а владелец машины
  включает нужное переключателем;
* **личные модули владельца** — автоскачивание с MakerWorld (пул аккаунтов, обход анти-бота)
  и планировщик стены Multiboard. В открытую версию проекта их файлы не входят вовсе.

Раньше разделения не было вовсе: всё показывалось всем. Здесь оно появляется как ЯВНЫЙ
реестр — по одной записи на возможность, с честной причиной и требованиями к машине.

Как считается состояние
-----------------------
1. Редакция сборки (`config.EDITION`): `dev` — машина разработчика (пишет EDITION=dev в своём
   .env), всё остальное — `studio`. До 10.10.2026 без явного значения редакция угадывалась по
   наличию `.git`, но установщик сам делает git clone — и любая установка опознавалась как
   билд разработчика со всеми особыми фичами включёнными.
2. Поверх редакции — ЯВНЫЙ переключатель владельца в настройках (`feature_<ключ>` в таблице
   settings). Он сильнее дефолта в обе стороны: свою dev-возможность можно выключить, а
   студии с мощной видеокартой — включить генерацию аватарок.
3. Под всем этим — НАЛИЧИЕ кода (`Feature.requires`): нет файлов личного модуля в сборке —
   возможности нет вовсе, её не включить ни редакцией, ни переключателем, и в настройках
   её не видно.

Реестр — единственный источник правды: по нему строится и экран настроек, и закрытие ручек
на сервере (`webapp`), и подсказка «чего не хватает на этой машине».
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

DEV = "dev"
STUDIO = "studio"


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    why: str                       # зачем оно и почему такая редакция
    needs: str = ""                # что должно быть на машине (пусто — ничего особенного)
    dev_only: bool = False         # в билде студии по умолчанию выключено
    routes: tuple[str, ...] = ()   # префиксы ручек, которые закрываются вместе с фичей
    group: str = "Личное"
    requires: tuple[str, ...] = ()  # файлы сборки (от BASE_DIR); нет хоть одного — фичи нет вовсе


# Порядок = порядок показа в настройках. Продуктовые фичи в реестре НЕ перечисляем: он о том,
# что можно выключить, а не полный список того, что умеет бот.
FEATURES: tuple[Feature, ...] = (
    Feature(
        key="makerworld",
        title="MakerWorld: скачивание по ссылке и пул аккаунтов",
        why="Завязано на личные аккаунты MakerWorld и обход анти-бота (после ~9 закачек "
            "подряд прилетает 418 и блок на полтора часа). Чужой студии нужны СВОИ аккаунты, "
            "иначе фича работает через раз и утягивает за собой чужой бан.",
        needs="аккаунты MakerWorld и Chrome для входа в них",
        dev_only=True,
        routes=("/api/admin/mw_account", "/api/admin/mw_accounts"),
        requires=("app/makerworld.py", "app/mwtoken.py"),
    ),
    Feature(
        key="model_search",
        title="Поиск моделей по внешним каталогам",
        why="Ходит во внешние каталоги (Printables, Thingiverse по ключу) и опирается на "
            "локальную языковую модель для семантики — из коробки это не заработает.",
        needs="ключи каталогов и запущенная Ollama для семантического поиска",
        dev_only=True,
        routes=("/api/model_search",),
    ),
    Feature(
        key="gpu_avatars",
        title="Генерация аватарок на видеокарте",
        why="Нейросетевой рендер в 7 ГиБ VRAM. Без подходящей видеокарты честнее не обещать "
            "его вовсе: процедурные аватарки остаются у всех и от этой фичи не зависят.",
        needs="видеокарта NVIDIA с 8 ГБ памяти",
        dev_only=True,
        routes=("/local/avatar_gpu/refresh",),
    ),
    Feature(
        key="parametric",
        title="Параметрические модели (OpenSCAD)",
        why="Рендерит .scad внешним OpenSCAD. На машине без него раздел выглядит рабочим, "
            "но каждая попытка заканчивается ошибкой.",
        needs="установленный OpenSCAD",
        dev_only=True,
        routes=("/api/parametric/",),
    ),
    Feature(
        key="planner",
        title="Планировщик стены Multiboard",
        why="Инструмент под личную стену с Multiboard: чужой студии он не нужен и только "
            "занимает пункт в меню.",
        dev_only=True,
        requires=("app/webui/planner_engine.js",),
    ),
    Feature(
        key="lcd",
        title="Настольный дисплей TURZX",
        why="Отдельная железка на столе (8.8\" по USB): включайте, если такой дисплей у вас есть.",
        needs="дисплей TURZX и его рендерер",
        dev_only=True,
        # Экран — это не одна ручка данных, а весь его обвес: статика дашборда (/lcd*), данные
        # дашборда, сводка и флаг питания для рендерера и тумблер вкл/выкл из админки. Пока в
        # списке были только два префикса, в студийной сборке половина экрана оставалась живой:
        # страница /lcd отдавалась, а тумблер «TURZX-экран» писал настройку железки, которой нет.
        routes=("/lcd", "/api/lcd/", "/api/screen-status", "/api/screen-power",
                "/api/admin/screen_power"),
    ),
    Feature(
        key="selfupdate",
        title="Самообновление из git",
        why="Подтягивает свою ветку с origin (git fetch + fast-forward) и ставит новые "
            "зависимости. Установка — это git clone, поэтому обновление работает у всех; без "
            ".git (распакованный архив) обновлять нечем.",
        needs="рабочая копия репозитория",
        routes=("/api/admin/update_check", "/api/admin/update_apply", "/api/admin/update_mode"),
    ),
    Feature(
        key="slicer_tools",
        title="Инструменты слайсера: ручной слайс и «Открыть в слайсере»",
        why="Требуют ElegooSlicer НА ТОЙ ЖЕ машине, где живёт бот. Продукт это допускает, но "
            "не требует, поэтому фичу можно выключить, не ломая расчёт цены.",
        needs="ElegooSlicer на машине бота",
        routes=("/api/admin/order/slice", "/api/admin/order/open_slicer"),
        group="Требует программ на этой машине",
    ),
)

FEATURES_BY_KEY: dict[str, Feature] = {f.key: f for f in FEATURES}

# Ручки, закрытые вместе с фичей: префикс → ключ. Строится из реестра, чтобы не держать
# второй список (он бы неизбежно разъехался с первым).
ROUTE_GUARD: tuple[tuple[str, str], ...] = tuple(
    (prefix, f.key) for f in FEATURES for prefix in f.routes)

_overrides: dict[str, bool] = {}      # ключ → явный выбор владельца; наполняет refresh()


def _truthy(raw) -> bool:
    """Значение настройки → вкл/выкл.

    Числовую форму разбираем ОТДЕЛЬНО: `db.get_settings()` приводит всю таблицу к float, и
    выключенный переключатель приезжает сюда как 0.0 — строковое сравнение с «0» его не
    ловило, и «выключить» молча не срабатывало (поймано живьём на /api/parametric)."""
    try:
        return float(raw) != 0.0
    except (TypeError, ValueError):
        return str(raw).strip().lower() not in ("0", "", "false", "no", "off")


def edition() -> str:
    """Текущая редакция сборки: явное EDITION=dev — машина разработчика, иначе студия.

    Наличие .git больше не признак разработчика: установщик открытой версии сам делает
    git clone, и прежнее угадывание включало каждой установке все особые фичи."""
    val = (getattr(config, "EDITION", "") or "").strip().lower()
    return DEV if val == DEV else STUDIO


def present(f: Feature) -> bool:
    """Есть ли код фичи в этой сборке (все файлы из requires на месте)."""
    return all((config.BASE_DIR / p).exists() for p in f.requires)


def default_state(f: Feature) -> bool:
    return True if edition() == DEV else not f.dev_only


def enabled(key: str) -> bool:
    """Включена ли возможность здесь и сейчас. Неизвестный ключ — включён (не в реестре =
    обычная продуктовая фича, ограничивать её нечем)."""
    f = FEATURES_BY_KEY.get(key)
    if f is None:
        return True
    if not present(f):              # личного модуля нет в этой сборке — включать нечего
        return False
    if key in _overrides:
        return _overrides[key]
    return default_state(f)


def guard_key(path: str) -> str:
    """Ключ фичи, которой принадлежит ручка (пусто — ручка не ограничена)."""
    for prefix, key in ROUTE_GUARD:
        if path.startswith(prefix):
            return key
    return ""


def available(f: Feature) -> bool:
    """Есть ли на этой машине то, что фиче нужно. Проверяем только дешёвое и достоверное:
    неизвестность честнее показать как «есть», чем пугать ложной тревогой."""
    if f.key == "parametric":
        return Path(getattr(config, "OPENSCAD_PATH", "")).exists()
    if f.key == "selfupdate":
        return (config.BASE_DIR / ".git").exists()
    if f.key == "slicer_tools":
        return Path(getattr(config, "SLICER_PATH", "")).exists()
    return True


def snapshot() -> list[dict]:
    """Реестр для интерфейса: состояние, дефолт редакции, причина, требования."""
    return [{
        "key": f.key, "title": f.title, "why": f.why, "needs": f.needs, "group": f.group,
        "dev_only": f.dev_only, "enabled": enabled(f.key), "default": default_state(f),
        "overridden": f.key in _overrides, "available": available(f),
    } for f in FEATURES if present(f)]


def flags() -> dict[str, bool]:
    """Флаги для фронта: КАЖДАЯ фича реестра, включая отсутствующие в сборке (у них False).

    Фронт считает неизвестный ключ включённым (чтобы разделы не моргали до загрузки флагов),
    поэтому личный модуль, которого нет в открытой версии, обязан приехать явным False —
    иначе его раздел нарисовался бы и вёл в 404."""
    return {f.key: enabled(f.key) for f in FEATURES}


def setting_key(key: str) -> str:
    return f"feature_{key}"


def apply_settings(settings: dict) -> None:
    """Подхватить явные переключатели владельца из таблицы settings.

    Зовётся при старте и после сохранения настроек: реестр читается на каждом запросе, и
    ходить за ним в БД каждый раз незачем."""
    _overrides.clear()
    for f in FEATURES:
        raw = (settings or {}).get(setting_key(f.key))
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        _overrides[f.key] = _truthy(raw)
    if _overrides:
        log.info("Редакция %s; вручную переопределено: %s", edition(),
                 ", ".join(f"{k}={'вкл' if v else 'выкл'}" for k, v in sorted(_overrides.items())))
