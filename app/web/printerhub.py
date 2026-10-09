"""Общий дом драйвера принтера в веб-слое: выбор драйвера, снимок состояния, отказы.

Один предмет: КАК веб-слой разговаривает с принтером — выбор драйвера по карточке
(`_printer_driver`), единая точка «печатает ли машина» (`_live_view`), разбор `?printer_id=`
(`_printer_id_arg`), ключ «чей это принтер» (`_driver_printer_id`), эндпоинт для ссылок
(`_printer_ep`), URL камеры (`_camera_url`) и единый отказ «пульта нет» (`_no_printer`).
Сами ручки `printer_*` остаются в webapp до своих волн (камера и пульт — волна 4, статус и
сетевая печать — волна 11): они держатся за `_active_print` и `_bot`.

Выделено из app/webapp.py волной 2 endgame-плана (docs/Перспективные разработки/
refactoring-webapp-endgame-2026-08-12.md). `_printer_driver` — самый горячий узел файла
(двадцать вызывающих из трёх доменов): без этого дома ни экраны студии (волна 3), ни камера
(волна 4), ни принтерный блок (волны 10–11) уехать не могут — каждый тянул бы обратный
импорт webapp ради выбора драйвера.

Модуль НЕ импортирует webapp. Держателя бота здесь нет (волна 12). Волной 10 сюда переехал
и УКАЗАТЕЛЬ АКТИВНОЙ ПЕЧАТИ (`_active_print` + его функции): словарь только мутируется,
фасад webapp реэкспортирует ТОТ ЖЕ объект, а tests/test_printflow.py чистит его идиомой
`webapp._active_print.clear()` (подмена словаря целиком изолировала бы копию).

app/printwatch.py берёт `_printer_ep` и `_active_print_header` ОТСЮДА, capture_bed_shot —
из web/printerlive: отложенных `from . import webapp` у наблюдателя не осталось вовсе.

Тела перенесены ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from .. import db, elegoo, printerdrv
from ..websign import _file_ver
from .common import signed_url

log = logging.getLogger(__name__)


async def _printer_driver(printer_id: int | None = None):
    """Драйвер принтера мастерской (OpenTodo 0613014e).

    `printer_id=None` — принтер по умолчанию: так это работало, когда принтер был один, и так
    отвечают все ручки, которым принтера не назвали. Смысл не только в выборе, но и в том, что
    ручки перестали звать `elegoo.*` напрямую: у драйвера спрашивают способности, и кнопка,
    которой у этого принтера нет, не показывается вместо того, чтобы молча не работать.

    Несуществующий id — НЕ повод молча подсунуть принтер по умолчанию: ручка камеры получила бы
    чужой стол на запрос «покажи принтер №7». Отдаём драйвер-пустышку, он честно скажет «пульта
    нет»."""
    try:
        printer = await db.get_printer(int(printer_id)) if printer_id else \
            await db.get_default_printer()
    except Exception:  # noqa: BLE001
        printer = None
    return printerdrv.for_printer(printer)


async def _live_view(printer_id: int | None = None, max_age: float = 6.0):
    """Снимок принтера ЧЕРЕЗ ДРАЙВЕР: (state, printing, progress, drv). state=None — связи нет.

    Единственная точка, где веб-слой спрашивает «печатает ли машина». До волны 6 этот вопрос
    задавался в трёх местах своими руками — `elegoo.cached_status` → `enrich_status` →
    `print_stats.state == "printing"`, — и все три знали ровно один принтер и ровно один диалект.
    `print_stats` вдобавок ключ СИНТЕТИЧЕСКИЙ: прошивка его не шлёт, его достраивает enrich, и
    любая копия этой цепочки, забывшая enrich, молча считала боевой принтер простаивающим —
    именно так и было в printwatch (разбор 11.08).

    `job_state` драйвера отвечает на тот же вопрос для Elegoo, Klipper и симулятора разом и
    считает паузу и прогрев ЗАНЯТОСТЬЮ — экран не должен звать мастера снимать модель со стола,
    пока печать стоит на паузе."""
    drv = await _printer_driver(printer_id)
    try:
        cs = await asyncio.to_thread(drv.status, max_age)
    except Exception:  # noqa: BLE001
        cs = None
    if not cs or not cs.get("ok"):
        return None, False, None, drv
    state = dict(cs.get("state") or {})
    state.update(drv.enrich(state))
    js = drv.job_state(state)
    prog = js.get("progress")
    return (state, bool(js.get("active")),
            (float(prog) if isinstance(prog, (int, float)) else None), drv)


def _printer_id_arg(request: web.Request) -> int | None:
    """`?printer_id=` из запроса. Пусто/мусор → None = принтер по умолчанию (прежнее поведение
    всех фронтов, которые про второй принтер ещё не знают)."""
    try:
        pid = int(request.query.get("printer_id") or 0)
    except (TypeError, ValueError):
        return None
    return pid or None


def _driver_printer_id(drv) -> int | None:
    """Карточка, с которой говорит драйвер, или None. Ключ состояния «чей это принтер»
    в веб-слое — тот же, что у наблюдателя (printwatch._printer_id)."""
    return getattr(getattr(drv, "printer", None), "id", None)


def _printer_ep(drv) -> elegoo.Endpoint | None:
    """Эндпоинт драйвера для ссылок, которые строит САМ вебапп (роут /download принтера).

    Не `getattr(drv, "endpoint", None)`: это ручка ИМЕННО SDCP, и спрашивать её у драйвера
    другого вендора нечестно — у Klipper эндпоинт свой и формы другой. None означает «строить
    ссылку не из чего, кроме принтера по умолчанию» — ровно прежнее поведение."""
    return drv.endpoint() if isinstance(drv, printerdrv.ElegooDriver) else None


async def _camera_url(printer_id: int | None = None) -> str:
    """URL камеры принтера — через драйвер, а не напрямую из .env.

    Драйвер без камеры (Klipper, «только профиль слайсера») отдаёт пустую строку — и все
    вызывающие уже трактуют пустоту как «камеры нет»."""
    return (await _printer_driver(printer_id)).camera_url()


def _no_printer(drv) -> web.Response:
    """Единый отказ «пульта нет»: с честной причиной — не настроен или так не умеет."""
    if drv.kind == "none":
        return web.json_response(
            {"ok": False, "error": "У этого принтера нет пульта — он заведён только "
                                   "как профиль слайсера"})
    return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) не настроен"})


# ── Указатель активной печати (волна 10 endgame): дом переехал сюда из webapp ──────────────
# Активная печать → заказ (OpenTodo 09e9ee22): источник live-данных для «богатой шапки» экрана
# принтера. Заполняется, когда бот сам отправляет заказ на печать по сети (мы знаем request_id).
# Печать, запущенная мимо бота (с экрана принтера/ElegooSlicer), сюда не попадает — шапка тогда
# показывает только телеметрию. Хранится в памяти: переживать рестарт не обязательно.
#
# Ключ — МАШИНА (0 = «машина не названа»). Один указатель на процесс означал бы, что вторая
# машина, начав печатать заказ, забирает шапку у первой: у первой она погасла бы, а у второй
# показала бы чужой заказ, его превью и его вес филамента.
#
# Словарь только МУТИРУЕТСЯ (никогда не пере-присваивается) — фасад webapp реэкспортирует
# ТОТ ЖЕ объект, и tests/test_printflow.py чистит его через webapp._active_print.clear().
_active_print: dict[int, int] = {}

async def _active_print_key(printer_id: int | None = None) -> int:
    """Ключ указателя «богатой шапки» — ТОТ ЖЕ, что у привязки печати (`db.binding_key`).

    Разные ключи у двух подсистем, отвечающих на один вопрос «кто печатает этот заказ», уже
    разъезжались: привязка ложилась под id карточки, а указатель под 0, и шапка сетевой печати
    не зажигалась вовсе. 0 остаётся только там, где карточек нет совсем."""
    try:
        return await db.binding_key(printer_id)
    except Exception:  # noqa: BLE001
        return int(printer_id or 0)

def _set_active_print(rid: int, key: int = 0) -> None:
    """Запомнить заказ, который бот только что отправил на печать (для шапки экрана принтера).

    `key` — УЖЕ разрешённый ключ машины (`_active_print_key`), а не сырой printer_id: функция
    синхронная, а разрешение ключа ходит в БД."""
    _active_print[int(key or 0)] = int(rid)

async def _request_printer_id(rid: int) -> int | None:
    """Машина, на которой печатался заказ. Сначала живая привязка (печать ещё идёт), потом
    последняя запись журнала этого заказа. None — не знаем, и выдумывать нельзя.

    Нужна там, где действие адресовано ЗАКАЗУ, а состояние живёт по машинам: разбор печати на
    одной машине не должен гасить «модель на столе» у второй."""
    try:
        b = next((x for x in await db.list_print_bindings()
                  if x.get("request_id") == int(rid)), None)
        if b and b.get("printer_id"):
            return int(b["printer_id"])
        for e in reversed(await db.list_print_journal_for_request(int(rid))):
            if e.get("printer_id"):
                return int(e["printer_id"])
    except Exception:  # noqa: BLE001
        log.debug("printer of request %s unknown", rid, exc_info=True)
    return None

async def _active_print_header(printer_id: int | None = None) -> dict | None:
    """Live-данные для «богатой шапки» принтера (OpenTodo 09e9ee22): связываем активную печать с
    заказом в БД. Связь даёт сетевая печать, запущенная ботом из заказа (известен request_id).
    Возвращает {order_id, label, plastic, grams, thumb} или None (печать не из бота / заказ
    закрыт). Превью отдаём подписанным /reqprev, как в списке заказов."""
    key = await _active_print_key(printer_id)
    rid = _active_print.get(key)
    if rid is None and key and 0 in _active_print and len(_active_print) == 1:
        # Совместимость с указателем, поставленным до этой правки (под ключом 0). Только когда
        # он ЕДИНСТВЕННЫЙ на всю студию: иначе шапка одной машины досталась бы другой.
        rid = _active_print.get(0)
    if not rid:
        return None
    r = await db.get_request(rid)
    if not r or r.status in ("printed", "rejected", "cancelled"):
        return None
    thumb = (signed_url("req", r.id, {"v": _file_ver(r.preview_path)})
             if r.preview_path and Path(r.preview_path).exists() else "")
    # Граммы в плитке «Филамент на модель» — это вес ТЕКУЩЕГО печатающегося файла, а не заказа.
    # Web UI принтера знает его как «Filament Consumption» (оценка слайсера в gcode); берём тем же
    # источником по имени файла из телеметрии (OpenTodo ed689ac0). Это чинит и много-, и
    # одностольные, и печать без заказа: для флекси-осьминога на 7 столов больше не «20 из 643».
    grams = None
    try:
        drv = await _printer_driver(printer_id)
        cs = await asyncio.to_thread(drv.status, 30.0)
        ps = ((cs or {}).get("state") or {}).get("print_status") or {}
        cur = ps.get("filename") or ps.get("task_name") or ps.get("name") or ""
        if cur:
            # file_grams блокирующий (может сходить в сеть) и сам троттлит активный 1044 — в поток.
            # Спрашиваем ТУ машину, что печатает: имена файлов на двух принтерах совпадают, и
            # чужой вес — враньё в плитке «Филамент на модель».
            fg = await asyncio.to_thread(drv.file_grams, cur)
            if fg and fg > 0:
                grams = round(float(fg), 1)
    except Exception:  # noqa: BLE001
        grams = None
    if grams is None and int(getattr(r, "plates", 1) or 1) <= 1 and (r.grams_total or 0) > 0:
        # Приблизительный фолбэк ТОЛЬКО для одностольного заказа: файла на принтере не нашли, но
        # для одной пластины граммы заказа = граммы печати. Для многостольного так нельзя — граммы
        # всего заказа (643 г) ≠ граммы текущей пластины (11 г), это и есть исходный баг.
        grams = round(float(r.grams_total), 1)
    return {"order_id": r.id, "label": r.model_label, "plastic": r.plastic_name,
            "grams": grams, "thumb": thumb}
