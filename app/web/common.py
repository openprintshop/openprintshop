"""Общие помощники веб-слоя: числа, ответы, время, цвет, отдача картинок, подпись ссылок.

Выделено из app/webapp.py волной 4 цикла 2. Девять помощников (`_toint`, `_fnum`, `_num`,
`_bad`, `_ago`, `_color_css`, `_served_img`, `_img_headers`, `_maybe_webp` вместе с рабочей
лошадкой `_to_webp`) перенесены ДОСЛОВНО, без единой правки тела: их зовут сотни мест, и любая
«заодно поправил» здесь стоила бы дороже самого разреза.

НОВОГО кода ровно три функции — `sig`, `check_sig`, `signed_url`. До них подпись собиралась и
проверялась вручную примерно в сорока местах: строка вида `_sign("req" + str(rid))` жила рядом с
f-строкой пути, и совпадение префикса у сборщика и у проверяющего держалось на внимательности.

ПРЕФИКСЫ НЕПРИКОСНОВЕННЫ. Префикс входит в подписываемую строку, поэтому опечатка в одной букве
меняет подпись — и все уже выданные ссылки этого вида перестают открываться. Перевыпустить их
нельзя: они лежат в старых сообщениях Telegram, а `/csample` — ещё и на QR-этикетках, наклеенных
на физические образцы калибровки. Значения ниже СКОПИРОВАНЫ из app/webapp.py, а не набраны
заново, и закреплены золотым тестом tests/test_web_common_facade.py.

Модуль не импортирует webapp — на этом и держится возможность резать webapp по доменам: любой
app/web/*.py берёт помощники отсюда и цикла не заводит. Зависимости — только листовые модули
пакета (materials, websign, db, elegoo, gcodemeta), сами ничего из веб-слоя не тянущие.

ДВА ПОМОЩНИКА ПРИЕХАЛИ СЮДА ВОЛНОЙ 4 НЕ ПОТОМУ, ЧТО ЗДЕСЬ ИМ КРАСИВЕЕ. `_photo_url` и
`_print_settings_view` зовёт И то, что осталось в webapp (фото катушек, журнал печатей), И то,
что уехало в web/calib.py (карточки образцов, печати кампании). Оставь их в webapp — и калибровка
получит ребро web.calib → webapp, то есть ровно тот цикл, ради разрыва которого разрез затевался.
Тела перенесены дословно; webapp реэкспортирует оба имени фасадом, как и всё остальное отсюда.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from aiohttp import web

from .. import config, db, elegoo, gcodemeta, materials
from ..websign import _file_ver, _sign

# --------------------------------------------------------------------------- числа и ответы


def _toint(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _fnum(v, default: float | None = None) -> float | None:
    """Число из тела запроса или None. Пустая строка → None: у Z-offset и прочих
    необязательных замеров «не мерил» и «ровно 0» — РАЗНЫЕ состояния (1028ab67)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _num(body, key, default=None):
    try:
        return float(str(body.get(key)).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _bad() -> web.Response:
    return web.json_response({"ok": False, "error": "bad request"}, status=400)


# --------------------------------------------------------------------------- время и цвет


def _ago(ts: int) -> str:
    if not ts:
        return ""
    d = int(time.time()) - int(ts)
    if d < 3600:
        return "только что"
    if d < 86400:
        return f"{d // 3600} ч назад"
    days = d // 86400
    if days == 1:
        return "вчера"
    if days < 30:
        return f"{days} дн назад"
    if days < 365:
        return f"{days // 30} мес назад"
    return f"{days // 365} г назад"


def _color_css(name: str, overrides: dict) -> str:
    """CSS-цвет по названию: solid hex или linear-gradient (для переливающихся через тире)."""
    rgbs = materials.color_to_rgbs(name, overrides)
    if not rgbs:
        return ""
    hexes = ["#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)) for r, g, b in rgbs]
    return hexes[0] if len(hexes) == 1 else "linear-gradient(135deg," + ",".join(hexes) + ")"


# --------------------------------------------------------------------------- отдача картинок


def _served_img(it) -> str | None:
    """Какой файл реально отдаст /img/{cid}: фото печати приоритетнее рендера (как в _image)."""
    if it.photo_path and Path(it.photo_path).exists():
        return it.photo_path
    return it.preview_path


def _img_headers(request: web.Request, private: bool = False) -> dict:
    """Версионированный URL (?v=…) неизменен по содержимому → кэшируем на год как immutable
    (ни перекачки, ни ревалидации). Голый URL (без ?v=) — короткий кэш, на случай прямого
    доступа / устаревшего клиента, чтобы он не залип на год. Vary: Accept — ответ зависит от
    того, принимает ли клиент webp (контент-негоциация ниже)."""
    scope = "private" if private else "public"
    cc = (f"{scope}, max-age=31536000, immutable" if request.query.get("v")
          else f"{scope}, max-age=300")
    return {"Cache-Control": cc, "Vary": "Accept"}


def _to_webp(src: Path, dst: Path) -> None:
    from PIL import Image
    Image.open(str(src)).convert("RGB").save(str(dst), "WEBP", quality=82, method=4)


async def _maybe_webp(request: web.Request, path: Path) -> Path:
    """Если клиент принимает webp и файл — jpeg/png, отдаём webp-версию: конвертируем один раз
    и кэшируем рядом (foo.jpg.webp), при изменении исходника перегенерируем. Любая ошибка →
    исходный путь. Telegram (send_photo) сюда не ходит — это только веб-отдача."""
    try:
        if "image/webp" not in request.headers.get("Accept", ""):
            return path
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            return path
        webp = path.with_name(path.name + ".webp")
        if (not webp.exists()) or webp.stat().st_mtime < path.stat().st_mtime:
            await asyncio.to_thread(_to_webp, path, webp)
        return webp if webp.exists() else path
    except Exception:  # noqa: BLE001
        return path


# --------------------------------------------------------------------------- подписанные ссылки
# Ниже — ЕДИНСТВЕННОЕ место, где префикс подписи стоит рядом с путём ручки. Пара «префикс → путь»
# зафиксирована здесь именно для того, чтобы сборщик ссылки не мог разойтись с проверяющим:
# оба берут строку отсюда. Значения скопированы из app/webapp.py (см. предупреждение в шапке).
_SIGNED_ROUTES: dict[str, str] = {
    "av": "/avatar/{ident}",          # аватарка профиля
    "req": "/reqprev/{ident}",        # превью заказа
    "jtl": "/jtl/{ident}",            # таймлапс записи журнала печати
    "csample": "/csample/{ident}",    # страница образца калибровки, открывается по QR с этикетки
    "prod": "/prodphoto/{ident}",     # фото готового изделия в витрине
    "slicer": "/slicershot/{ident}",  # скрин из слайсера, приложенный к счёту
    "tl": "/timelapse/{ident}",       # таймлапс, привязанный к заказу
    "sm": "/smprev/{ident}",          # превью сохранённой модели
}

# Префиксы составных ссылок: путь ручки не выводится из одного ident, поэтому URL такие места
# собирают сами, а подпись берут через sig(). «cust» подписывает только rid, хотя путь
# /custphoto/{rid}/{idx}; «batch» — bid+idx; «aphoto» — вид фото + id (/admin/photo/{kind}/{id}).
_SIGNED_COMPOSITE = ("cust", "batch", "aphoto")

_SIG_PREFIXES = frozenset(_SIGNED_ROUTES) | frozenset(_SIGNED_COMPOSITE)


def sig(prefix: str, ident: object = "") -> str:
    """Подпись подписанной ссылки — ровно `_sign(prefix + str(ident))`, как было в webapp.

    Неизвестный префикс — это опечатка, и она обязана быть громкой: молча посчитанная «почти
    правильная» подпись дала бы ссылки, которые не открываются, без единой строчки в логе."""
    if prefix not in _SIG_PREFIXES:
        raise KeyError(f"неизвестный префикс подписи {prefix!r}: "
                       f"известны {sorted(_SIG_PREFIXES)}")
    return _sign(f"{prefix}{ident}")


def check_sig(request: web.Request, prefix: str, ident: object = "") -> bool:
    """Совпал ли параметр `?s=` запроса с подписью `prefix + ident`.

    Заменяет ручное `request.query.get("s", "") != _sign(...)`: раньше проверяющий повторял
    строку подписи за сборщиком ссылки, и расхождение отказывало ТОЛЬКО у клиента."""
    return request.query.get("s", "") == sig(prefix, ident)


def signed_url(prefix: str, ident: object, extra: dict | None = None) -> str:
    """Подписанный относительный URL: `/путь/{ident}?s=…` плюс параметры `extra` в порядке словаря.

    `extra` — это почти всегда `{"v": _file_ver(path)}`: версия файла в адресе меняет адрес при
    замене файла, поэтому картинку можно кэшировать на год (см. `_img_headers`). Значение
    подставляется как есть — экранирования тут нет и не нужно: там числа."""
    url = f"{_SIGNED_ROUTES[prefix].format(ident=ident)}?s={sig(prefix, ident)}"
    for key, val in (extra or {}).items():
        url += f"&{key}={val}"
    return url


def _photo_url(kind: str, pid: int, path: str | None) -> str | None:
    """Подписанный URL фото катушки (plastic) / печати (collection) / результата заказа (order)."""
    # C2: значение может прийти из БД относительным — сравнение существования ведём по
    # абсолютному пути, иначе URL молча станет None и картинка пропадёт без единой ошибки.
    if not path or not config.abs_data(path).exists():
        return None
    return f"/admin/photo/{kind}/{pid}?s={sig('aphoto', kind + str(pid))}&v={_file_ver(path)}"


# Три сборщика ниже приехали волной 1b endgame-плана тем же правилом, что и _photo_url выше:
# их зовёт И то, что остаётся в webapp, И то, что уедет следующими волнами (витрина и
# справочники — волна 8, CRM — волна 6). Тела перенесены ДОСЛОВНО.

def _spool_photo_path(plastic_id: int, color: str) -> Path | None:
    """Фото катушки конкретного цвета (OpenTodo eb9a9efe): файлы лежат в
    data/spool_photos/{plastic_id}/{цвет}.jpg — имя файла = цвет из справочника пластика
    с заменой не-буквенных символов на «_» (как ниже). Заменить фото — просто положить
    новый файл с тем же именем. None — фото для этого цвета нет."""
    d = config.DATA_DIR / "spool_photos" / str(plastic_id)
    if not d.is_dir():
        return None
    fn = re.sub(r"[^\w\-]+", "_", (color or "").strip()) + ".jpg"
    p = d / fn
    if p.exists():
        return p
    # регистронезависимый добор («Черный» из БД против «черный» в имени файла)
    low = fn.lower()
    for f in d.glob("*.jpg"):
        if f.name.lower() == low:
            return f
    return None


def _avatar_url(uid: int) -> str:
    """Подписанный URL аватарки профиля (OpenTodo fca4246e). Пусто, если ещё не сгенерирована."""
    p = config.AVATAR_DIR / f"{uid}.png"
    if p.exists():
        return signed_url("av", uid, {"v": _file_ver(str(p))})
    return ""


def _product_photo_url(p) -> str:
    """Публичный подписанный URL фото товара (виден клиентам в витрине)."""
    path = getattr(p, "photo_path", "") or ""
    if path and config.abs_data(path).exists():
        return signed_url("prod", p.id, {"v": _file_ver(path)})
    return ""


# ------------------------------------------------------- разделяемые доменные помощники
# Ниже — не «общее» по смыслу, а общее по факту: этот помощник зовут два разных блока веб-слоя
# (журнал печатей в webapp и карточка печати кампании в web/calib.py). Дом ему нужен такой,
# который не импортирует ни тот, ни другой, иначе разрез калибровки упирается в цикл.


async def _print_settings_view(entry: dict) -> dict | None:
    """Настройки печати для карточки журнала (OpenTodo 8e8d4873): {rows, summary, start, …}.

    Снимок ищем по ссылке settings_id, а если её нет (печать записана до этой задачи или
    привязка не сработала) — по имени задания: снимок мог появиться и позже самой записи."""
    snap = None
    if entry.get("settings_id"):
        snap = await db.get_print_settings(entry["settings_id"])
    if snap is None:
        label = entry.get("label") or ""
        snap = await db.find_print_settings(label, elegoo.normalize_job_name(label))
    if not snap:
        return None
    meta = {"values": snap.get("values") or {}}
    start = snap.get("start") or {}
    # Параметры САМОГО старта: их в gcode нет — так задание было отправлено на принтер.
    start_rows: list[dict] = []
    if start.get("plate"):
        start_rows.append({"label": "Пластина", "value": str(start["plate"])})
    if "bedlevel" in start:
        start_rows.append({"label": "Автокалибровка стола",
                           "value": "да" if start["bedlevel"] else "нет"})
    if start.get("tray_id") is not None:
        start_rows.append({"label": "Слот хаба", "value": str(start["tray_id"])})
    if start.get("filament_t") is not None:
        start_rows.append({"label": "Тип филамента (код)", "value": str(start["filament_t"])})
    if start.get("slot_map"):
        start_rows.append({"label": "Карта слотов", "value": ", ".join(
            f"{m.get('canvas_id', '?')}→{m.get('tray_id', '?')}" for m in start["slot_map"])})
    return {
        "id": snap["id"], "name": snap.get("name") or "",
        "rows": gcodemeta.rows(meta), "summary": gcodemeta.summary(meta),
        "slicer": snap.get("slicer") or "",
        "grams": snap.get("grams"), "minutes": snap.get("minutes"), "layers": snap.get("layers"),
        "uploaded_at": snap.get("uploaded_at"), "started_at": snap.get("started_at"),
        "start": start_rows,
    }

