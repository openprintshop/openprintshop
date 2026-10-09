"""Железо мастерской: карточки принтеров, обслуживание (ТО) и пластины стола с Z-offset.

Один модуль на всю машину — три секции db.py, которые нельзя развести:

* КАРТОЧКИ (таблица printers) — CRUD плюс два обязательных последействия любой правки:
  кэш ёмкости для сроков (refresh_active_printers -> config.set_active_printers) и фоновые
  подписчики телеметрии (refresh_printer_sessions). Оба спрятаны за refresh_printers, чтобы
  половину нельзя было забыть.
* ОБСЛУЖИВАНИЕ (service_items + service_log) — наработка по узлам и журнал ТО. Связка
  accrue_service -> mark_serviced -> list_service_log пишет обе таблицы одним смыслом, а внутри
  секции дважды зовётся get_default_printer из соседней — резать по этой границе нельзя.
* ПЛАСТИНЫ СТОЛА (calib_plates) — save_calib_plate + set_default_calib_plate держат
  эксклюзивность is_default в пределах принтера; это свойство железа, а не калибровки.

Отдельный модуль `plates` НЕВОЗМОЖЕН: имя `plates` в app/db.py занято параметром/kwarg-ом
(add_request, save_model, save_calc_cache, add_collection_model), и фасадная строка
`from .store import plates` дала бы ruff F811 — та же ловушка, что `settings` в волне 2.

Выделено из app/db.py волной 3 разреза (план refactoring-db-plan-2026-08-21.md). Тела
перенесены ДОСЛОВНО. Правка ровно одна, и она поведенческая:

  refresh_printer_sessions держала ленивый `from . import bambu, elegoo, printerdrv` внутри
  тела; по правилу метода ленивые `from .` внутри store-модуля запрещены (после переезда
  точка указывала бы на пакет store), поэтому импорт ПОДНЯТ на верхний уровень как
  `from .. import bambu, elegoo, printerdrv`. Цикла нет — проверено запуском: цепочка
  config->elegoo->bambu->moonraker->printerdrv не тянет app.db. Последствие: `import app.db`
  теперь грузит драйверы жадно, а не при первом вызове (в боевом процессе они всё равно уже
  импортированы через app/printwatch.py).

Ещё изменилось: модульный `log` теперь app.store.printers, а не app.db — в боевом логе
поменяется префикс строк «Подписчики Elegoo/Bambu подняты для адресов».

ЛОВУШКА ДЛЯ ТЕСТОВ: _seed_service_items зовётся из db.init(), а get_default_printer/
refresh_printers зовутся соседними телами ГОЛЫМ именем. Подмена `monkeypatch.setattr(db, ...)`
до этих внутренних вызовов больше не достаёт — патчить app.store.printers.
"""
from __future__ import annotations

import logging
import time

import aiosqlite

from .. import bambu, config, elegoo, printerdrv
from ..profilepaths import abs_profile_str, rel_profile
from .core import _conn
from .models import Printer, ServiceItem

log = logging.getLogger(__name__)


# ---- обслуживание принтера (ТО): наработка по узлам + журнал ----
_DEFAULT_SERVICE_ITEMS = [
    ("nozzle", "Замена сопла", "hours", 800),
    ("bed_level", "Калибровка стола", "hours", 150),
    ("bed_surface", "Чистка/замена покрытия стола", "prints", 250),
    ("lube", "Смазка осей", "hours", 200),
    ("clean", "Чистка от пыли", "days", 30),
    ("belt", "Натяжение ремней", "hours", 500),
    ("extruder", "Чистка экструдера", "prints", 400),
]


def _to_service_item(r: aiosqlite.Row) -> ServiceItem:
    return ServiceItem(
        id=r["id"], key=r["key"], label=r["label"], trigger_type=r["trigger_type"],
        threshold=r["threshold"] or 0.0, hours_since=r["hours_since"] or 0.0,
        prints_since=r["prints_since"] or 0, last_service_at=r["last_service_at"] or 0,
        is_active=r["is_active"], notified=r["notified"], sort=r["sort"] or 0)


async def _seed_service_items(printer_id: int | None = None) -> None:
    """Завести стандартные пункты ТО. У КАЖДОЙ машины свои (цикл 3, волна 5).

    printer_id=None на старте бота означает «основному принтеру»: так себя вёл прежний сид,
    когда машина была одна."""
    if printer_id is None:
        p = await get_default_printer()
        printer_id = p.id if p else None
    cur = await _conn().execute(
        "SELECT COUNT(*) AS c FROM service_items WHERE printer_id IS ?", (printer_id,))
    if (await cur.fetchone())["c"]:
        return
    now = int(time.time())
    for i, (key, label, tt, thr) in enumerate(_DEFAULT_SERVICE_ITEMS):
        await _conn().execute(
            "INSERT OR IGNORE INTO service_items(key,label,trigger_type,threshold,last_service_at,"
            "sort,created_at,printer_id) VALUES(?,?,?,?,?,?,?,?)",
            (key, label, tt, thr, now, i, now, printer_id))
    await _conn().commit()


async def list_service_items(active_only: bool = False,
                             printer_id: int | None = None) -> list[ServiceItem]:
    """Пункты ТО. printer_id задан → только этой машины; None → ВСЕ (экран обслуживания
    показывает парк целиком)."""
    sql = "SELECT * FROM service_items"
    where = []
    args: list = []
    if active_only:
        where.append("is_active = 1")
    if printer_id is not None:
        where.append("printer_id IS ?")
        args.append(int(printer_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort, id"
    cur = await _conn().execute(sql, args)
    return [_to_service_item(r) for r in await cur.fetchall()]


async def get_service_item(item_id: int) -> ServiceItem | None:
    cur = await _conn().execute("SELECT * FROM service_items WHERE id = ?", (item_id,))
    r = await cur.fetchone()
    return _to_service_item(r) if r else None


async def accrue_service(minutes: float, prints: int,
                         printer_id: int | None = None) -> list[ServiceItem]:
    """Прибавляет наработку (часы/печати) активным пунктам ТО ОДНОЙ машины. Возвращает пункты,
    которые ИМЕННО этим начислением пересекли порог (для разового алерта) — как deduct_stock.

    printer_id со ЗНАЧЕНИЕМ ПО УМОЛЧАНИЮ намеренно: у функции две точки вызова, обе завёрнуты в
    молчаливый `except`, и обязательный аргумент означал бы, что забытый вызов даёт TypeError,
    который никто не увидит, — начисление ТО просто перестало бы происходить (цикл 3, волна 5).

    None → основная машина: так себя вела функция, пока принтер был один. Пункты машины, у
    которой их ещё нет (только что заведённая карточка), заводятся здесь же."""
    if printer_id is None:
        p = await get_default_printer()
        printer_id = p.id if p else None
    await _seed_service_items(printer_id)
    now = int(time.time())
    crossed: list[ServiceItem] = []
    for it in await list_service_items(active_only=True, printer_id=printer_id):
        before = it.is_due(now)
        await _conn().execute(
            "UPDATE service_items SET hours_since = hours_since + ?, prints_since = prints_since + ? "
            "WHERE id = ?", (max(0.0, minutes) / 60.0, max(0, prints), it.id))
        it2 = await get_service_item(it.id)
        if it2 and it2.is_due(now) and not before and not it2.notified:
            crossed.append(it2)
    await _conn().commit()
    return crossed


async def mark_service_notified(item_id: int, value: int = 1) -> None:
    await _conn().execute("UPDATE service_items SET notified = ? WHERE id = ?", (int(value), item_id))
    await _conn().commit()


async def mark_serviced(item_id: int, note: str = "") -> None:
    """Зафиксировать выполненное ТО: запись в журнал + обнуление счётчиков + снять notified."""
    it = await get_service_item(item_id)
    if not it:
        return
    now = int(time.time())
    await _conn().execute(
        "INSERT INTO service_log(item_id,item_label,at,hours_at,prints_at,note) VALUES(?,?,?,?,?,?)",
        (item_id, it.label, now, it.hours_since, it.prints_since, note))
    await _conn().execute(
        "UPDATE service_items SET hours_since=0, prints_since=0, last_service_at=?, notified=0 "
        "WHERE id=?", (now, item_id))
    await _conn().commit()


async def list_service_log(item_id: int | None = None, limit: int = 50) -> list[aiosqlite.Row]:
    if item_id is not None:
        cur = await _conn().execute(
            "SELECT * FROM service_log WHERE item_id = ? ORDER BY id DESC LIMIT ?", (item_id, limit))
    else:
        cur = await _conn().execute("SELECT * FROM service_log ORDER BY id DESC LIMIT ?", (limit,))
    return list(await cur.fetchall())


async def add_service_item(label: str, trigger_type: str = "hours", threshold: float = 0.0) -> int:
    now = int(time.time())
    cur = await _conn().execute(
        "INSERT INTO service_items(key,label,trigger_type,threshold,last_service_at,sort,created_at) "
        "VALUES(NULL,?,?,?,?,?,?)", (label, trigger_type, max(0.0, threshold), now, 999, now))
    await _conn().commit()
    return cur.lastrowid


async def update_service_item(item_id: int, **fields) -> None:
    allowed = {"label", "trigger_type", "threshold", "is_active"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "threshold" in sets or "trigger_type" in sets:   # сменили порог/тип → старый алерт сможет повториться
        sets["notified"] = 0
    cols = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE service_items SET {cols} WHERE id = ?", (*sets.values(), item_id))
    await _conn().commit()


async def delete_service_item(item_id: int) -> None:
    await _conn().execute("DELETE FROM service_items WHERE id = ?", (item_id,))
    await _conn().commit()


# ---- принтеры ----
def _to_printer(r: aiosqlite.Row) -> Printer:
    keys = r.keys()

    def _opt(name: str) -> str:
        return (r[name] if name in keys else "") or ""

    # Профили слайсера хранятся в БД маркером корня (@elegoo/resources/...), а наружу — и в
    # расчёт, и в карточку деска — идут АБСОЛЮТНЫМ путём: так их видит мастер и так их читают
    # slicer/bedspec/fastcalc. Движок и его exe берём из этой же строки: у карточки с
    # портативной сборкой корни профилей лежат рядом с её exe, а не в Program Files.
    _kind, _exe = _opt("slicer"), _opt("slicer_path")
    return Printer(id=r["id"], name=r["name"],
                   machine_profile=abs_profile_str(r["machine_profile"], _kind, _exe),
                   process_dir=abs_profile_str(r["process_dir"], _kind, _exe),
                   filament_dir=abs_profile_str(r["filament_dir"], _kind, _exe),
                   is_active=r["is_active"], is_default=r["is_default"],
                   kind=_opt("kind"), address=_opt("address"),
                   access_code=_opt("access_code"), serial=_opt("serial"),
                   slicer=_opt("slicer"), slicer_path=_opt("slicer_path"),
                   time_factor=_time_factor(r["time_factor"] if "time_factor" in keys else None))


# Коэффициент времени машины (B1). Коридор нарочно широкий: ×0.2…×5 покрывает и «в пять раз
# быстрее», и совсем медленную машину; за его пределами значение — опечатка, а не калибровка,
# и цена клиенту улетела бы в абсурд. Вне коридора — 1.0 при чтении, отказ при записи.
TIME_FACTOR_MIN = 0.2
TIME_FACTOR_MAX = 5.0


def _time_factor(v) -> float:
    try:
        f = float(v if v is not None else 1.0)
    except (TypeError, ValueError):
        return 1.0
    return f if TIME_FACTOR_MIN <= f <= TIME_FACTOR_MAX else 1.0


def valid_time_factor(v) -> float | None:
    """Число из формы → коэффициент в коридоре; None — не число или вне коридора."""
    try:
        f = round(float(str(v).replace(",", ".").strip()), 3)
    except (TypeError, ValueError):
        return None
    return f if TIME_FACTOR_MIN <= f <= TIME_FACTOR_MAX else None


async def list_printers(active_only: bool = False) -> list[Printer]:
    """Карточки принтеров. Порядок ОБЯЗАН совпадать с get_default_printer.

    Раньше здесь была сортировка по имени, а там — по id. Пока is_default расставлен, оба
    отдают одно и то же; но флаг ставится единственной функцией set_default_printer и в белый
    список update_printer не входит, так что «ни у кого не основной» — достижимое состояние.
    Тогда мини-аппа брала первого ПО ИМЕНИ (а «Bambu» по алфавиту раньше «Elegoo»), а
    автоматика и пульт — первого ПО ID: расчёт клиенту уехал бы на чужой профиль и стол, а
    журнал остался бы на прежней машине. Без единой ошибки в логе (разбор 11.08)."""
    sql = "SELECT * FROM printers"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY is_default DESC, id"
    cur = await _conn().execute(sql)
    return [_to_printer(r) for r in await cur.fetchall()]


async def get_printer(printer_id: int) -> Printer | None:
    cur = await _conn().execute("SELECT * FROM printers WHERE id = ?", (printer_id,))
    r = await cur.fetchone()
    return _to_printer(r) if r else None


async def refresh_active_printers() -> int:
    """Обновить кэш «сколько машин печатает» (config.ACTIVE_PRINTERS).

    Считаем активные карточки. Звать после init() и после любой правки принтеров — иначе срок
    клиенту не заметит появления второй машины до перезапуска бота."""
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM printers WHERE is_active = 1")
    n = int((await cur.fetchone())["c"] or 0)
    config.set_active_printers(n)
    return max(1, n)


async def refresh_printers() -> None:
    """Всё, что обязано случиться после ЛЮБОЙ правки карточек принтеров: кэш ёмкости для сроков
    и фоновые подписчики. Один вызов, чтобы половину нельзя было забыть."""
    await refresh_active_printers()
    await refresh_printer_sessions()


async def refresh_printer_sessions() -> list[str]:
    """Поднять фоновых подписчиков под текущие карточки принтеров и погасить лишних.

    Звать там же, где `refresh_active_printers` — после init() и после любой правки принтеров.
    Не позовёшь: удалённая карточка навсегда оставит поток, вечно ломящийся в машину, которой
    больше нет, а у новой не будет телеметрии до перезапуска бота.

    Сессия принтера ПО УМОЛЧАНИЮ сюда не входит — ею управляет `elegoo.start_subscriber()`.
    Карточка без собственного адреса даёт тот же эндпоинт, что и .env, и реестр её отбрасывает:
    у боевой установки с одним принтером здесь не поднимается ничего.

    Пустой `kind` в боевой БД означает «как было до драйверов» — разбираем его тем же
    автоопределением, что и фабрика драйверов, иначе карточка Moonraker с пустым типом получила
    бы MQTT-подписчика Elegoo."""
    cur = await _conn().execute(
        "SELECT kind, address, access_code, serial FROM printers WHERE is_active = 1")
    rows = await cur.fetchall()
    eps, bambu_eps = [], []
    for r in rows:
        kind = (r["kind"] or "").strip().lower() or printerdrv.autodetect_kind()
        if kind == "elegoo":
            eps.append(elegoo.card_endpoint(r["address"] or "", r["access_code"] or "",
                                            r["serial"] or ""))
        elif kind == "bambu":
            # У Bambu фолбэка на .env нет вовсе: его поля живут только в карточке, и серийник
            # обязателен — он в ТЕМЕ MQTT, а не в теле запроса.
            bambu_eps.append(bambu.Endpoint(ip=(r["address"] or "").strip(),
                                            code=(r["access_code"] or "").strip(),
                                            serial=(r["serial"] or "").strip()))
    keys = elegoo.sessions_sync(eps)
    if keys:
        log.info("Подписчики Elegoo подняты для адресов: %s", ", ".join(keys))
    bkeys = bambu.sessions_sync(bambu_eps)
    if bkeys:
        log.info("Подписчики Bambu подняты для адресов: %s", ", ".join(bkeys))
    return keys + bkeys


async def get_default_printer() -> Printer | None:
    cur = await _conn().execute(
        "SELECT * FROM printers WHERE is_active = 1 ORDER BY is_default DESC, id LIMIT 1")
    r = await cur.fetchone()
    return _to_printer(r) if r else None


async def add_printer(name: str, machine_profile: str, process_dir: str, filament_dir: str,
                      is_active: int = 0) -> int:
    """Завести карточку принтера. По умолчанию НЕАКТИВНОЙ.

    Раньше карточка создавалась сразу активной, а протокол связи, адрес и код доступа
    выставлялись ОТДЕЛЬНЫМ действием — то есть между «завёл» и «настроил» она уже была боевой.
    За эту секунду: selfcheck начинал жаловаться на отсутствующие слайс-профили при каждом
    старте, а бот — показывать КЛИЕНТУ лишний шаг «на каком принтере печатаем?» с названиями
    машин на кнопках (см. printer_choice_enabled). Первый принтер студии активируется
    миграцией/сидом, второй мастер включает сам, когда карточка готова (разбор 11.08)."""
    cur = await _conn().execute(
        "INSERT INTO printers(name, machine_profile, process_dir, filament_dir, is_active, is_default) "
        "VALUES(?,?,?,?,?,0)",
        (name, rel_profile(machine_profile), rel_profile(process_dir),
         rel_profile(filament_dir), 1 if is_active else 0),
    )
    await _conn().commit()
    # Кэши обновляем ЗДЕСЬ, а не в ручке админки: так их нельзя забыть в новом месте,
    # где заводят принтер, и ни срок клиенту, ни подписчики не отстанут от парка машин.
    await refresh_printers()
    return cur.lastrowid


async def update_printer(printer_id: int, **fields) -> None:
    allowed = {"name", "machine_profile", "process_dir", "filament_dir", "is_active",
               "kind", "address", "access_code", "serial", "slicer", "slicer_path", "time_factor"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    # Профили — в относительный вид; exe движка (slicer_path) НЕ трогаем: это не профиль, и
    # относить его к корню профилей значило бы потерять «слайсер не найден» как диагноз.
    for key in ("machine_profile", "process_dir", "filament_dir"):
        if key in sets:
            sets[key] = rel_profile(sets[key], sets.get("slicer"), sets.get("slicer_path", ""))
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    await _conn().execute(f"UPDATE printers SET {assignments} WHERE id = ?", (*sets.values(), printer_id))
    await _conn().commit()
    await refresh_printers()


async def set_default_printer(printer_id: int) -> None:
    await _conn().execute("UPDATE printers SET is_default = 0")
    await _conn().execute("UPDATE printers SET is_default = 1, is_active = 1 WHERE id = ?", (printer_id,))
    await _conn().commit()
    await refresh_printers()


async def delete_printer(printer_id: int) -> None:
    await _conn().execute("DELETE FROM printers WHERE id = ?", (printer_id,))
    await _conn().execute("DELETE FROM printer_nozzles WHERE printer_id = ?", (printer_id,))
    await _conn().commit()
    await refresh_printers()


# ---- пластины стола и Z-offset (OpenTodo 1028ab67) ----
PLATE_KINDS: tuple[tuple[str, str], ...] = (
    ("textured", "Текстурированная PEI"),
    ("smooth", "Гладкая PEI"),
    ("cool", "Холодная (Cool Plate)"),
    ("high", "Высокотемпературная"),
    ("other", "Другая"),
)


async def list_calib_plates(printer_id: int | None = None) -> list[dict]:
    """Пластины с их Z-offset. Текущая (is_default) первой — её мастер ищет чаще всего."""
    sql = ("SELECT c.*, pr.name AS printer FROM calib_plates c "
           "LEFT JOIN printers pr ON pr.id = c.printer_id")
    args: tuple = ()
    if printer_id:
        sql += " WHERE c.printer_id = ?"
        args = (printer_id,)
    sql += " ORDER BY c.is_default DESC, c.name COLLATE NOCASE"
    cur = await _conn().execute(sql, args)
    return [dict(r) for r in await cur.fetchall()]


async def get_calib_plate(plate_id: int) -> dict | None:
    cur = await _conn().execute("SELECT * FROM calib_plates WHERE id = ?", (int(plate_id),))
    r = await cur.fetchone()
    return dict(r) if r else None


async def save_calib_plate(*, plate_id: int | None = None, printer_id: int | None = None,
                           name: str, kind: str = "", z_offset: float | None = None,
                           note: str = "") -> int:
    """Создать/переименовать пластину и записать её Z-offset.

    Ключ — (принтер, имя): одна и та же пластина у двух принтеров живёт отдельными записями,
    потому что и вылет у них разный. Пустой z_offset — законное значение «ещё не мерил»."""
    nm = (name or "").strip()[:80]
    if not nm:
        raise ValueError("Введите имя пластины")
    now = int(time.time())
    if plate_id:
        await _conn().execute(
            "UPDATE calib_plates SET printer_id=?, name=?, kind=?, z_offset=?, note=?, "
            "updated_at=? WHERE id=?",
            (printer_id, nm, (kind or "")[:20], z_offset, (note or "")[:500], now, int(plate_id)))
        await _conn().commit()
        return int(plate_id)
    cur = await _conn().execute(
        "SELECT id FROM calib_plates WHERE printer_id IS ? AND name = ?", (printer_id, nm))
    row = await cur.fetchone()
    if row:            # такая пластина уже есть — это правка, а не дубль
        return await save_calib_plate(plate_id=int(row["id"]), printer_id=printer_id, name=nm,
                                      kind=kind, z_offset=z_offset, note=note)
    cur = await _conn().execute(
        "SELECT COUNT(*) AS c FROM calib_plates WHERE printer_id IS ?", (printer_id,))
    first = (await cur.fetchone())["c"] == 0
    cur = await _conn().execute(
        "INSERT INTO calib_plates(printer_id, name, kind, z_offset, note, is_default, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (printer_id, nm, (kind or "")[:20], z_offset, (note or "")[:500],
         1 if first else 0, now, now))
    await _conn().commit()
    return cur.lastrowid


async def set_default_calib_plate(plate_id: int) -> None:
    """Отметить пластину как ту, что лежит на столе сейчас (одна на принтер)."""
    cur = await _conn().execute("SELECT printer_id FROM calib_plates WHERE id = ?", (int(plate_id),))
    row = await cur.fetchone()
    if not row:
        return
    await _conn().execute("UPDATE calib_plates SET is_default = 0 WHERE printer_id IS ?",
                          (row["printer_id"],))
    await _conn().execute("UPDATE calib_plates SET is_default = 1 WHERE id = ?", (int(plate_id),))
    await _conn().commit()


async def delete_calib_plate(plate_id: int) -> None:
    """Удалить пластину. Паспорта, которые на неё ссылались, остаются — просто без пластины."""
    await _conn().execute("DELETE FROM calib_plates WHERE id = ?", (int(plate_id),))
    await _conn().execute("UPDATE calib_passport SET plate_id = NULL WHERE plate_id = ?",
                          (int(plate_id),))
    await _conn().commit()


# ---- пластины, которые ведёт CC2 Control (app/cc2plates.py) ----
# Связанная пластина (cc2_plate_id) принадлежит принтеру: имя, Z-offset и «на столе» — из его
# библиотеки, наши здесь только тип покрытия и заметка. Связь — только этой колонкой: по имени
# ничего не сопоставляется. Удалённая на принтере пластина у нас остаётся записью без связи:
# на неё могли сослаться паспорта калибровки.

async def _free_plate_name(printer_id: int | None, name: str, except_id: int = 0) -> str:
    """Имя для пластины принтера, не занятое другой нашей записью того же принтера (ключ —
    принтер и имя). Занято — с пометкой «(CC2)», затем с номером."""
    base = (name or "").strip()[:72] or "Пластина"
    for cand in [base, f"{base} (CC2)"] + [f"{base} (CC2 {i})" for i in range(2, 20)]:
        cur = await _conn().execute(
            "SELECT 1 FROM calib_plates WHERE printer_id IS ? AND name = ? AND id != ?",
            (printer_id, cand, int(except_id)))
        if not await cur.fetchone():
            return cand
    return f"{base} ({int(time.time())})"


async def calib_plate_cc2(plate_id: int) -> str:
    """id пластины в CC2 Control или пусто — пластину ведёт не принтер."""
    cur = await _conn().execute("SELECT cc2_plate_id FROM calib_plates WHERE id = ?", (int(plate_id),))
    r = await cur.fetchone()
    return (r["cc2_plate_id"] or "") if r else ""


async def cc2_plates_apply(printer_id: int | None, snap: dict) -> int:
    """Сверить пластины принтера со снимком библиотеки CC2 Control; пишет только изменения
    (страницы базы раз в минуту уезжают копией на VPS). Возвращает число изменённых строк.

    * связанные — имя и Z из принтера;
    * пластины принтера без пары — заводятся у нас (решение владельца 08.10.2026);
    * связь с удалённой на принтере пластиной снимается, запись остаётся;
    * «на столе»: смонтированная на принтере пластина — единственная на столе у этого
      принтера; ничего не смонтировано — с наших связанных пластин отметка снимается, а
      несвязанные остаются как отметил мастер."""
    if not snap.get("available"):
        return 0
    plates = {p["id"]: p for p in snap.get("plates") or []}
    # Связанные пластины ЭТОГО принтера (и «любого» — связь делает их пластинами принтера):
    # у второй машины своя библиотека, и её пластины чужой снимок не отвязывает.
    cur = await _conn().execute(
        "SELECT * FROM calib_plates WHERE cc2_plate_id IS NOT NULL "
        "AND (printer_id IS ? OR printer_id IS NULL)", (printer_id,))
    linked = {r["cc2_plate_id"]: dict(r) for r in await cur.fetchall()}
    now = int(time.time())
    changed = 0
    for cid, row in linked.items():
        p = plates.get(cid)
        if p is None:
            await _conn().execute(
                "UPDATE calib_plates SET cc2_plate_id = NULL, is_default = 0, updated_at = ? "
                "WHERE id = ?", (now, row["id"]))
            changed += 1
            continue
        name = await _free_plate_name(printer_id, p["name"], except_id=row["id"])
        if (row["name"], row["z_offset"], row["printer_id"]) != (name, p["z"], printer_id):
            await _conn().execute(
                "UPDATE calib_plates SET name = ?, z_offset = ?, printer_id = ?, updated_at = ? "
                "WHERE id = ?", (name, p["z"], printer_id, now, row["id"]))
            changed += 1
    for cid, p in plates.items():
        if cid in linked:
            continue
        cur = await _conn().execute("SELECT 1 FROM calib_plates WHERE cc2_plate_id = ?", (cid,))
        if await cur.fetchone():          # связана с пластиной другого принтера — не дублируем
            continue
        await _conn().execute(
            "INSERT INTO calib_plates(printer_id, name, kind, z_offset, note, is_default, "
            "created_at, updated_at, cc2_plate_id) VALUES(?,?,?,?,?,0,?,?,?)",
            (printer_id, await _free_plate_name(printer_id, p["name"]), "", p["z"],
             f"сторона {p['side']}, из CC2 Control", now, now, cid))
        changed += 1
    mounted = snap.get("current") or ""
    cur = await _conn().execute(
        "SELECT id, is_default, cc2_plate_id FROM calib_plates WHERE printer_id IS ?", (printer_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    if mounted in plates:
        want = {r["id"]: int(r["cc2_plate_id"] == mounted) for r in rows}
    else:
        want = {r["id"]: (0 if r["cc2_plate_id"] else int(r["is_default"] or 0)) for r in rows}
    for r in rows:
        if int(r["is_default"] or 0) != want[r["id"]]:
            await _conn().execute("UPDATE calib_plates SET is_default = ? WHERE id = ?",
                                  (want[r["id"]], r["id"]))
            changed += 1
    if changed:
        await _conn().commit()
    return changed


async def cc2_plate_link(plate_id: int, cc2_id: str) -> str:
    """Связать нашу пластину с пластиной CC2 Control (разово, по карте мастера). Пусто — готово,
    иначе причина отказа. Прежний Z остаётся в заметке: дальше он придёт из принтера.

    Пластину принтера сверка могла уже завести у нас сама (экран калибровки открыли раньше,
    чем дошла карта). Такую копию без ссылок паспортов убираем: связь важнее."""
    row = await get_calib_plate(plate_id)
    if not row:
        return "Пластина не найдена"
    if (row.get("cc2_plate_id") or "") == cc2_id:
        return ""
    if row.get("cc2_plate_id"):
        return f"«{row['name']}» уже связана с другой пластиной принтера"
    cur = await _conn().execute("SELECT id, name FROM calib_plates WHERE cc2_plate_id = ?", (cc2_id,))
    other = await cur.fetchone()
    if other:
        cur = await _conn().execute("SELECT COUNT(*) AS c FROM calib_passport WHERE plate_id = ?",
                                    (other["id"],))
        if (await cur.fetchone())["c"]:
            return f"Эта пластина принтера уже связана с «{other['name']}», и на неё ссылаются паспорта"
        await _conn().execute("DELETE FROM calib_plates WHERE id = ?", (other["id"],))
    z = row.get("z_offset")
    note = (row.get("note") or "").strip()
    if z is not None:
        was = f"Z до связи с CC2 Control: {z:+.3f}"
        note = f"{note}\n{was}" if note else was
    await _conn().execute(
        "UPDATE calib_plates SET cc2_plate_id = ?, note = ?, updated_at = ? WHERE id = ?",
        (cc2_id, note[:500], int(time.time()), int(plate_id)))
    await _conn().commit()
    return ""
