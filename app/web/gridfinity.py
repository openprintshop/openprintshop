"""«Мастерская органайзеров»: проекты Gridfinity, спецификация деталей и частичный заказ.

Конфигуратор перестал быть одноразовым черновиком. Теперь это мини-приложение внутри
мини-приложения, и весь его серверный контур собран здесь:

* ПРОЕКТ — физический ящик клиента. Раскладка хранится ЦЕЛИКОМ строкой JSON движка
  ({v, base, modules, seq}); сервер её не пересчитывает, только валидирует форму и
  ограничивает размер. Правда по геометрии — ТОЛЬКО эта строка.
* СПЕЦИФИКАЦИЯ — проекция раскладки на таблицу деталей (`_gf_reconcile`): у детали есть
  количество, имя, заметка «что кладём», состояние и номер заявки, чего в геометрии нет.
* ЧАСТИЧНЫЙ ЗАКАЗ — партия из выбранных деталей: заявка мастеру ровно на них, снимок
  геометрии каждой детали замораживается в `spec_json`.
* ПРАВКА ЗАКАЗАННОГО — единственный нетривиальный переход: пока заявку не взяли в работу,
  правка проходит насквозь; после — заказанная версия замораживается, а модуль форкается
  в v2 (см. `_gf_reconcile`).
* ОЦЕНКА «≈» — из объёма меша, который считает 3D-движок на клиенте (`m.vol`, см³).
  Слайсинг в клиентских расчётах выключен намеренно и здесь не появляется: время берём
  существующей моделью fastcalc.print_minutes, цену — pricing.compute_price.

Сюда же переехал ДОСЛОВНО старый контур (черновик `/api/gridfinity/config` и заявка
целиком `/api/gridfinity/order`): фронт у клиентов обновляется не мгновенно, и старые
ручки обязаны отвечать. Черновик теперь — зеркало последнего открытого проекта.

Выделено из app/web/storefront.py: витрина осталась витриной, а конфигуратор дорос до
самостоятельного модуля с собственной таблицей маршрутов (как calib/prices/equipment).
Карточка конфигуратора в ленте (`_GF_FEED_SHOWN`) осталась в storefront — это логика ленты.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import math
import secrets
import time

from aiohttp import web

from .. import bedspec, calcservice, config, db, fastcalc, keyboards, notify, pricing, ratelimit, texts
from ..webauth import _admin_forbidden, _verify_admin, _verify_init_data, _verify_user
from .common import _bad, _toint
from .orderdefaults import _apply_default_delivery

log = logging.getLogger(__name__)
routes = web.RouteTableDef()

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


_GF_MAX_BYTES = 256 * 1024             # потолок сериализованной конфигурации (черновик и заказ)
_GF_MAX_PROJECTS = 20                  # активных проектов на юзера (архив не в счёт)
_GF_MAX_PARTS = 200                    # деталей в проекте: раскладка 11×6 даёт максимум 66
_GF_VERSIONS_KEEP = 10                 # снимков раскладки на проект

# Константы домена — ДОСЛОВНО из state.js прототипа (выверены с замерами, не менять):
# шаг сетки 42, бокс N×M = 42N−0.62, серии царг Blum, подписи генераторов/вставок/цветов.
_GF_PITCH, _GF_CLR = 42, 0.62
_GF_SERIES_TOP = {"M": 83, "K": 115, "C": 192, "D": 224}
_GF_TYPE = {"shelf": "полка", "drawer": "шуфлядка", "tandem": "Blum Tandembox"}
_GF_GEN = {"tray": "лоток", "comp": "отсеки", "grid": "сетка", "comb": "гребёнка",
           "free": "своя разметка"}
_GF_ITEM = {"scoop": "скос дна", "platform": "подиум", "pins": "штырьки",
            "label": "полочка-этикетка", "cups": "гнёзда", "vslot": "V-ложемент"}
_GF_COLORS = ["оранж", "графит", "бирюза", "лайм", "лаванда", "песок"]
_GF_COLOR_HEX = ["#ff7a1a", "#3d4a5e", "#3fb6c9", "#2dd47e", "#a98bff", "#d4b183"]

# Состояния детали. Жизнь у неё НЕ своя: пока деталь в заявке, показываемое состояние
# наследуется от статуса заявки (_gf_live_state) — иначе спецификация и «Мои заказы»
# начали бы противоречить друг другу.
_GF_STATE_LABEL = {"draft": "черновик", "hold": "отложено", "ordered": "в заявке",
                   "printed": "напечатано", "dropped": "снята с раскладки"}
# Статусы заявки, при которых заказанная деталь ЗАМОРОЖЕНА: правка форкает её в v2.
# Не только «в работе»: как только цена НАЗВАНА (счёт выставлен, ждём оплаты), правка
# геометрии означала бы «мастер печатает втрое больше пластика за те же деньги».
_GF_FROZEN_STATUS = ("awaiting_payment", "invoiced", "price_changed", "in_progress", "printed")
# Статусы, при которых заявка деталь больше не держит — она возвращается в черновик.
_GF_DEAD_STATUS = ("rejected", "cancelled")


# ─────────────────────────── безопасные касты и разбор ───────────────────────────

def _gf_f(v, d: float = 0.0) -> float:
    """Безопасный float: битое поле конфигурации портит одну строку сводки, а не всю заявку.

    Не-конечные значения (`1e400` разбирается json-ом в inf, `NaN`) отбрасываются вместе с
    мусором: одна такая цифра в габаритах ящика навсегда роняла бы и проект клиента, и
    карточку заявки у мастера — `int(inf)` бросает OverflowError уже в сетке."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return d
    return f if math.isfinite(f) else d


def _gf_i(v, d: int = 0) -> int:
    """Безопасный int через float: '480.5' строкой — это 480, а не молчаливый 0 (_toint
    падал бы на дробной строке в default)."""
    f = _gf_f(v, float(d))
    try:
        return int(f)
    except (TypeError, ValueError, OverflowError):
        return d


# Границы ящика. Те же, что у «моих мерок»: без них валидатор мерок был декоративным —
# base кладут прямо в config, и 50 000 мм ширины доезжали до генератора PDF-шаблона,
# который на них рисует десятки тысяч листов в память.
_GF_W_MIN, _GF_W_MAX = 50.0, 2000.0
_GF_H_MIN, _GF_H_MAX = 10.0, 600.0
_GF_MAX_MODULES = 400          # 11×6 = 66 ячеек; 400 — с большим запасом на огромные ящики


def _gf_valid(cfg) -> bool:
    """Проверка формы: конфигурация движка, а не произвольный JSON, и с ЖИВЫМИ габаритами."""
    if not (isinstance(cfg, dict) and isinstance(cfg.get("base"), dict)
            and isinstance(cfg.get("modules"), list)):
        return False
    if len(cfg["modules"]) > _GF_MAX_MODULES:
        return False
    b = cfg["base"]
    w, d, hh = _gf_f(b.get("W")), _gf_f(b.get("D")), _gf_f(b.get("H"))
    return (_GF_W_MIN <= w <= _GF_W_MAX and _GF_W_MIN <= d <= _GF_W_MAX
            and _GF_H_MIN <= hh <= _GF_H_MAX)


def _gf_modules(cfg) -> list:
    """Модули — только словари: список мусора вида [0] не должен рождать пустую заявку."""
    return [m for m in (cfg.get("modules") or []) if isinstance(m, dict)]


def _gf_grid(base: dict) -> dict:
    """gridInfo() из state.js: сетка spacerless — остаток стороны уходит в крайние сокеты."""
    w, d = _gf_f(base.get("W")), _gf_f(base.get("D"))
    cols, rows = max(1, int(w // _GF_PITCH)), max(1, int(d // _GF_PITCH))
    lx, ly = w - cols * _GF_PITCH, d - rows * _GF_PITCH
    fx, fy = base.get("fitX") or "sym", base.get("fitY") or "sym"
    ml = lx if fx == "left" else lx / 2 if fx == "sym" else 0.0
    mb = ly if fy == "back" else ly / 2 if fy == "sym" else 0.0
    return {"cols": cols, "rows": rows, "mL": ml, "mR": lx - ml, "mB": mb, "mF": ly - mb}


def _gf_inner_h(base: dict) -> float:
    """innerH() движка: у Tandembox полезная высота — не меньше верха серии."""
    hh = _gf_f(base.get("H"))
    if str(base.get("type") or "") == "tandem":
        return max(hh, float(_GF_SERIES_TOP.get(str(base.get("series") or "M"), 83)))
    return hh


def _gf_dims(m: dict, g: dict) -> tuple[float, float, float]:
    """Габариты детали В МИЛЛИМЕТРАХ, как их напечатают: бокс 42N−0.62 плюс доборы.

    Единственное число, по которому клиент проверяет, влезет ли модуль в ящик, а мастер —
    влезет ли он на стол. Формулы — из state.js, не упрощать."""
    hmm = _gf_f(m.get("hmm"))
    if m.get("kind") == "wing":
        gap = g["mL"] if m.get("side") == "L" else g["mR"]
        return (max(0.0, gap - 0.3), max(1, _gf_i(m.get("d"))) * _GF_PITCH - _GF_CLR, hmm)
    mw, md = max(1, _gf_i(m.get("w"))), max(1, _gf_i(m.get("d")))
    ext = m.get("ext") if isinstance(m.get("ext"), dict) else {}
    el = g["mL"] - .3 if ext.get("L") and _gf_i(m.get("cx")) == 0 and g["mL"] > 2.3 else 0
    er = g["mR"] - .3 if ext.get("R") and _gf_i(m.get("cx")) + mw == g["cols"] and g["mR"] > 2.3 else 0
    eb = g["mB"] - .3 if ext.get("B") and _gf_i(m.get("cy")) == 0 and g["mB"] > 2.3 else 0
    ef = g["mF"] - .3 if ext.get("F") and _gf_i(m.get("cy")) + md == g["rows"] and g["mF"] > 2.3 else 0
    return (mw * _GF_PITCH - _GF_CLR + el + er, md * _GF_PITCH - _GF_CLR + eb + ef, hmm)


def _gf_mod_line(i: int, m: dict, g: dict) -> str:
    """Строка одного модуля сводки. Вынесена, чтобы битый модуль портил СВОЮ строку."""
    hmm = _gf_f(m.get("hmm"))
    color = _GF_COLORS[_gf_i(m.get("colorIdx")) % len(_GF_COLORS)]
    if m.get("kind") == "wing":
        side = "левый" if m.get("side") == "L" else "правый"
        gap = g["mL"] if m.get("side") == "L" else g["mR"]
        return (f"{i}. Пристенный {side} (профиль под царгу): зазор {gap:.1f} мм, "
                f"длина {_gf_i(m.get('d'))} яч, высота {hmm:.0f} мм, "
                f"отсеков {_gf_i(m.get('comp')) or 1} · {color}")
    mw, md = max(1, _gf_i(m.get("w"))), max(1, _gf_i(m.get("d")))
    ow, od, _ = _gf_dims(m, g)
    ext = m.get("ext") if isinstance(m.get("ext"), dict) else {}
    el = g["mL"] - .3 if ext.get("L") and _gf_i(m.get("cx")) == 0 and g["mL"] > 2.3 else 0
    er = g["mR"] - .3 if ext.get("R") and _gf_i(m.get("cx")) + mw == g["cols"] and g["mR"] > 2.3 else 0
    eb = g["mB"] - .3 if ext.get("B") and _gf_i(m.get("cy")) == 0 and g["mB"] > 2.3 else 0
    ef = g["mF"] - .3 if ext.get("F") and _gf_i(m.get("cy")) + md == g["rows"] and g["mF"] > 2.3 else 0
    gen = str(m.get("gen") or "tray")
    det = _GF_GEN.get(gen, "лоток")   # неизвестный генератор не тащит клиентскую строку в описание
    if gen == "comp":
        det += f" ×{_gf_i(m.get('compN')) or 3}"
    elif gen == "grid":
        det += f" {_gf_i(m.get('gridX')) or 2}×{_gf_i(m.get('gridY')) or 2}"
    elif gen == "comb":
        det += f" ×{_gf_i(m.get('combN')) or 6}, полоса {_gf_f(m.get('combW'), 16):g} мм"
    elif gen == "free":
        walls, islands, items = (m.get("walls") or []), (m.get("islands") or []), (m.get("items") or [])
        parts = []
        if walls:
            parts.append(f"стен {len(walls)}")
        if islands:
            parts.append(f"островов {len(islands)}")
        if items:
            kinds = ", ".join(dict.fromkeys(
                _GF_ITEM.get(str(it.get("t")), "вставка")
                for it in items if isinstance(it, dict)))
            parts.append(f"вставки: {kinds}")
        det += f" ({'; '.join(parts)})" if parts else " (пустой)"
    exts = "".join(k for k, v in (("←", el), ("→", er), ("↑", eb), ("↓", ef)) if v)
    return (f"{i}. Бокс {mw}×{md} яч · {ow:.1f}×{od:.1f}×{hmm:.1f} мм · {det}"
            + (f" · добор {exts}" if exts else "") + f" · {color}"
            + f" · позиция ({_gf_i(m.get('cx'))},{_gf_i(m.get('cy'))})")


def _gf_head_lines(cfg: dict, g: dict) -> list[str]:
    """Шапка сводки: ящик, сетка, остатки. Общая у полной раскладки и у партии."""
    base = cfg.get("base") or {}
    btype = str(base.get("type") or "drawer")
    w, d, hh = (_gf_i(base.get("W")), _gf_i(base.get("D")), _gf_i(base.get("H")))
    head = f"🧩 Gridfinity-органайзер: {_GF_TYPE.get(btype, 'шуфлядка')} · внутри {w}×{d} мм"
    if btype == "tandem":
        series = str(base.get("series") or "M")
        top = _GF_SERIES_TOP.get(series, 83)
        # полезная высота = max(H, верх серии) — ровно как innerH() движка; без неё
        # мастер видел бы «верх 83 мм» рядом с модулем высотой 150 и не понял бы ящик
        inner = max(hh, top)
        head += f" · серия {series if series in _GF_SERIES_TOP else 'M'} (верх {top} мм) · полезная высота {inner} мм"
    else:
        head += f" · высота {hh} мм"
    return [head,
            f"Сетка {g['cols']}×{g['rows']} (шаг 42, spacerless) · остатки: "
            f"←{g['mL']:.1f} →{g['mR']:.1f} ↑{g['mB']:.1f} ↓{g['mF']:.1f} мм"]


def _gf_summary(cfg: dict) -> str:
    """Читаемая сводка конфигурации для описания заявки: мастер должен видеть раскладку
    целиком, не открывая JSON. Формулы размеров — из state.js (бокс = 42N−0.62 + доборы).
    Режется ПО СТРОКАМ с явным хвостом: молча обрезанный посреди строки модуль хуже, чем
    честное «ещё N — в снимке» (снимок отдаёт /api/admin/gridfinity/{rid})."""
    base = cfg.get("base") or {}
    mods = _gf_modules(cfg)
    g = _gf_grid(base)
    lines = _gf_head_lines(cfg, g) + [f"Модулей: {len(mods)}", ""]
    budget = 3500 - sum(len(x) + 1 for x in lines)
    shown = 0
    for i, m in enumerate(mods, 1):
        try:
            line = _gf_mod_line(i, m, g)
        except Exception:  # noqa: BLE001
            line = f"{i}. (модуль не распознан)"
        if budget - len(line) - 1 < 60:   # оставить место под хвост «…и ещё N»
            break
        lines.append(line)
        budget -= len(line) + 1
        shown += 1
    if shown < len(mods):
        lines.append(f"…и ещё {len(mods) - shown} мод. — полная раскладка в снимке заявки")
    return "\n".join(lines)


def _gf_batch_summary(cfg: dict, parts: list[dict], mods: dict, title: str) -> str:
    """Сводка ПАРТИИ: мастеру уезжают только заказанные детали, а не вся раскладка.

    Именно этим частичный заказ и отличается от прежнего «всё или ничего»: заявка обязана
    описывать то, что печатают, иначе мастер напечатает лишнее."""
    g = _gf_grid(cfg.get("base") or {})
    lines = _gf_head_lines(cfg, g)
    if title:
        lines.insert(0, f"📦 Проект «{title}»")
    lines += [f"В партии деталей: {len(parts)} "
              f"({sum(max(1, _gf_i(p.get('qty'), 1)) for p in parts)} шт)", ""]
    budget = 3400 - sum(len(x) + 1 for x in lines)
    shown = 0
    for i, p in enumerate(parts, 1):
        m = mods.get(_gf_i(p.get("mod_id")))
        try:
            body = _gf_mod_line(i, m, g) if m else f"{i}. (деталь снята с раскладки)"
        except Exception:  # noqa: BLE001
            body = f"{i}. (модуль не распознан)"
        qty = max(1, _gf_i(p.get("qty"), 1))
        name = str(p.get("name") or "").strip()
        line = body + (f" · ×{qty}" if qty > 1 else "")
        if name:
            line += f" · «{name}»"
        note = str(p.get("note") or "").strip()
        if note:
            line += f" · кладут: {note}"
        if budget - len(line) - 1 < 60:
            break
        lines.append(line)
        budget -= len(line) + 1
        shown += 1
    if shown < len(parts):
        lines.append(f"…и ещё {len(parts) - shown} дет. — полная раскладка в снимке заявки")
    return "\n".join(lines)


# ─────────────────────────── подпись, имя и превью ───────────────────────────

_SIG_SKIP = ("id", "colorIdx", "vol", "volb", "name", "_new")


def _gf_sig(m: dict) -> str:
    """Подпись ГЕОМЕТРИИ модуля: расходится с деталью — модуль правили после заказа.

    Цвет и объём в подпись не входят намеренно: перекраска не должна форкать напечатанную
    деталь в v2 (цвет заказанной версии всё равно заморожен в spec_json), а объём —
    производная геометрии, которую считает движок. Служебный флаг «модуль только что
    поставлен» (`_new`) — тем более: из-за него деталь форкалась в v2 сама по себе."""
    try:
        clean = {k: v for k, v in m.items() if k not in _SIG_SKIP}
        raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001 — модуль с несериализуемым мусором: считаем «изменился»
        raw = repr(m)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]   # noqa: S324 — не криптография


def _gf_auto_name(m: dict, g: dict) -> str:
    """Имя детали по умолчанию: «Бокс 4×2 · лоток · 67 мм». Клиент его переименует."""
    hmm = _gf_f(m.get("hmm"))
    if m.get("kind") == "wing":
        side = "левый" if m.get("side") == "L" else "правый"
        return f"Пристенный {side} · {hmm:.0f} мм"
    gen = _GF_GEN.get(str(m.get("gen") or "tray"), "лоток")
    return (f"Бокс {max(1, _gf_i(m.get('w')))}×{max(1, _gf_i(m.get('d')))} · "
            f"{gen} · {hmm:.0f} мм")


def _gf_units(hmm: float) -> int:
    """Высота в юнитах Gridfinity: H(u) = 7u + 4.85. Кто знает систему — думает юнитами."""
    return max(1, int(round((_gf_f(hmm) - 4.85) / 7.0)))


def _gf_preview(cfg: dict, ordered: set | None = None) -> dict:
    """Компактный план ящика для списков: клиент рисует его CSS-сеткой, а не 3D.

    Возвращать в списке проектов саму раскладку нельзя — 20 проектов по 256 КБ, и лента
    карточек весит мегабайты. План читается размером с ноготь и стоит сотню байт."""
    base = cfg.get("base") or {}
    g = _gf_grid(base)
    ordered = ordered or set()
    mods = []
    for m in _gf_modules(cfg):
        mid = _gf_i(m.get("id"))
        if m.get("kind") == "wing":
            mods.append({"id": mid, "wing": str(m.get("side") or "L"),
                         "y": _gf_i(m.get("cy")), "d": max(1, _gf_i(m.get("d"))),
                         "c": _gf_i(m.get("colorIdx")) % len(_GF_COLORS),
                         "o": 1 if mid in ordered else 0})
            continue
        mods.append({"id": mid, "x": _gf_i(m.get("cx")), "y": _gf_i(m.get("cy")),
                     "w": max(1, _gf_i(m.get("w"))), "d": max(1, _gf_i(m.get("d"))),
                     "c": _gf_i(m.get("colorIdx")) % len(_GF_COLORS),
                     "o": 1 if mid in ordered else 0})
    return {"cols": g["cols"], "rows": g["rows"], "type": str(base.get("type") or "drawer"),
            "W": _gf_i(base.get("W")), "D": _gf_i(base.get("D")), "H": _gf_i(base.get("H")),
            "series": str(base.get("series") or ""), "mods": mods}


# ─────────────────────────── стандарт мастерской ───────────────────────────
#
# Gridfinity продаётся как НАРОДНАЯ вещь: один обкатанный пластик, один толстый слой,
# никаких позаказных статей. Смысл не в щедрости, а в предсказуемости: пластик известен и не
# преподносит сюрпризов, деталь уезжает в общий поток без отдельной настройки.
#
# ПРО СКОРОСТЬ НЕ ОБЕЩАТЬ. Первая редакция этого блока утверждала, что сопло 0.6 со слоем 0.3
# печатает лоток за 70 минут вместо 113, — цифры пришли из модели времени fastcalc. Настоящий
# слайс той же геометрии (меш выгружен из движка, ElegooSlicer, Elegoo PETG) показал другое:
# лоток 2×2 — 64 мин при 0.4/0.2 против 67 мин при 0.6/0.3, и на 9% больше пластика. Толстый
# слой выигрывает только на мелочи (1×1: 30 → 27 мин).
#
# Всё, от чего зависит обещание клиенту, стоит здесь ОДНИМ блоком. Цена, время и текст
# «за стандарт −25%» обязаны считаться от одних и тех же чисел: разъедутся — клиент увидит
# одну цифру, а мастер выставит другую.
_GF_STD_PLASTIC = "Elegoo PETG"     # чем печатаем; закрепить другой — gfx_standard_plastic_id
_GF_STD_COLOR = "белый"
_GF_STD_LAYER = 0.3                 # мм — ровно под сопло 0.6
_GF_STD_NOZZLE = "0.6"
_GF_STD_DISCOUNT = 25.0             # %, перекрывается настройкой gfx_standard_discount_pct


def _gf_std_discount(settings: dict) -> float:
    """Скидка за стандартные настройки, %. Ноль и отрицательные — выключено."""
    return max(0.0, _gf_f(settings.get("gfx_standard_discount_pct"), _GF_STD_DISCOUNT))


def _gf_std_text(plastic=None) -> str:
    """Одна строка про стандарт — её видит и клиент в мастерской, и мастер в заявке.

    Имя пластика берём у ТОГО, по которому реально посчитана цена (`_gf_price_ctx`), а не у
    константы: стандарт могли выключить или переименовать в прайсе, и тогда обещание обязано
    назвать замену, а не то, чем мы печатать не будем. Про цвет здесь молчим намеренно —
    он у каждой детали свой (палитра раскладки), а стандарт — это материал и слой."""
    name = str(getattr(plastic, "name", "") or "").strip() or _GF_STD_PLASTIC
    return f"{name} · слой {_GF_STD_LAYER} мм (сопло {_GF_STD_NOZZLE})"


# ─────────────────────────── оценка «≈» ───────────────────────────


# Доля пластика в СПЛОШНЫХ телах (ножки, подиумы, штырьки, острова): в слайсере они
# печатаются инфиллом с парой сплошных слоёв сверху и снизу, а не литым куском. 0.45 —
# инфилл 15% плюс сплошные оболочки тонкой плиты дна; на боксе 2×2×6u даёт ~32 г, что
# сходится с реально напечатанным образцом (литой объём давал бы ~55 г).
_GF_BULK_FILL = 0.45


def _gf_filament(m: dict) -> float:
    """Объём ФИЛАМЕНТА детали, см³: тонкие стенки как есть, сплошные тела — с инфиллом.

    Движок присылает и полный объём меша (`vol`), и объём сплошных тел внутри него
    (`volb`). Старые раскладки без `volb` считаются как раньше — целиком."""
    vol = max(0.0, _gf_f(m.get("vol")))
    bulk = min(vol, max(0.0, _gf_f(m.get("volb"))))
    return round(vol - bulk + bulk * _GF_BULK_FILL, 2)


def _gf_minutes(vol_cm3: float, h_mm: float) -> float:
    """Время печати детали существующей моделью fastcalc — без слайсинга.

    Считаем СТАНДАРТОМ мастерской (слой 0.3 под сопло 0.6), а не машинным профилем из
    настроек: gridfinity печатается только так, и оценка обязана совпадать с тем, что
    реально встанет в очередь. Раньше здесь стоял зашитый слой 0.2 и сопло из настроек —
    оценка была на 60% длиннее правды и незаметно дорожала машинным временем.

    У Gridfinity-бокса нет инфилла: стенка 0.8 мм — это периметры, то есть весь объём меша
    и есть оболочка. Поэтому весь объём кладём в `shell`, а не выдумываем свою формулу."""
    fil = {"shell": max(0.0, _gf_f(vol_cm3)) * 1000.0, "infill": 0.0, "support": 0.0,
           "flush": 0.0}
    s = {"layer_height": _GF_STD_LAYER, "nozzle": _GF_STD_NOZZLE}
    try:
        return float(fastcalc.print_minutes(fil, {"part_heights": [max(1.0, _gf_f(h_mm))]}, s))
    except Exception:  # noqa: BLE001 — модель времени не обязана держать мусорный объём
        return 0.0


_price_cache: dict = {}


async def _gf_price_ctx() -> tuple:
    """Контекст оценки: (пластик, настройки). Пластик — СТАНДАРТ мастерской: показываем цену
    того, чем на самом деле напечатаем, иначе обещанная скидка считалась бы от чужого прайса.

    Порядок поиска: закреплённый настройкой `gfx_standard_plastic_id` → активный пластик с
    именем `_GF_STD_PLASTIC` → дефолтный из настроек → самый дешёвый активный (правило,
    работавшее до стандарта). Две последние ступени — честный фолбэк, а не перестраховка:
    стандарт можно переименовать или временно снять с производства, и оценка не должна
    из-за этого пропасть совсем.

    Кэш на минуту: список проектов собирает спецификацию КАЖДОГО проекта, и без кэша
    двадцать проектов давали бы сорок лишних запросов к БД на одно открытие мастерской.
    Прайс-лист за минуту не меняется, а цифра всё равно оценочная."""
    key = int(time.time()) // 60
    if _price_cache.get("key") == key:
        return _price_cache["v"]
    settings = await db.get_settings()
    plastics = await db.list_plastics(active_only=True)
    out = (None, settings)
    if plastics:
        pinned = _gf_i(settings.get("gfx_standard_plastic_id"))
        did = _gf_i(settings.get("default_plastic_id"))
        std = _GF_STD_PLASTIC.strip().lower()
        by_pin = by_name = by_def = cheapest = None
        for p in plastics:
            pid = int(getattr(p, "id", 0) or 0)
            if pinned and pid == pinned:
                by_pin = p
            if by_name is None and str(getattr(p, "name", "")).strip().lower() == std:
                by_name = p
            if did and pid == did:
                by_def = p
            if cheapest is None or (float(getattr(p, "price_per_kg", 0))
                                    < float(getattr(cheapest, "price_per_kg", 0))):
                cheapest = p
        out = (by_pin or by_name or by_def or cheapest, settings)
    _price_cache.update(key=key, v=out)
    return out


# ПОЗАКАЗНЫХ статей у gridfinity нет ВООБЩЕ (решено 01.09.2026): ни подготовки,
# ни доплаты за стол, ни минимальной цены заказа. На лотке за три рубля позаказные полтора
# рубля были половиной чека и убивали саму идею народного органайзера: человек должен иметь
# право заказать одну недостающую ячейку, а не «добирать до минимума».
# Раньше эти статьи брались один раз на партию, а из строки спецификации вычитались —
# отсюда прежнее имя `_gf_line_settings`. Теперь настройки одни на всю мастерскую, и суммы
# строк с итогом партии расходятся только на округление до гривенника.
_GF_ZEROED = ("setup_fee", "plate_fee", "min_price")


def _gf_settings(settings: dict) -> dict:
    out = dict(settings)
    for k in _GF_ZEROED:
        out[k] = 0.0
    return out


_disc_cache: dict = {}


async def _gf_discount(uid, qty: int, settings: dict) -> float:
    """Итоговая скидка на gridfinity, % — клиентская и стандартная СКЛАДЫВАЮТСЯ.

    Клиентская считается как везде (`calcservice.effective_discount` — максимум из тиражной,
    персональной и промокода), стандартная добавляется сверху: это не альтернатива личной
    скидке, а награда за то, что деталь уедет в общий поток обкатанным пластиком и толстым
    слоем. Тот же процент уезжает строкой в описание заявки — иначе названная клиенту цифра
    и выставленный мастером счёт разъедутся, а разъезд в деньгах замечают последним.

    Кэш на минуту: `_gf_batches` зовёт смету на КАЖДУЮ партию проекта, и без него ящик с
    десятью партиями стоил бы двадцати лишних запросов к базе на одно открытие."""
    std = _gf_std_discount(settings)
    if not uid:
        return std
    minute = int(time.time()) // 60
    if _disc_cache.get("m") != minute:
        _disc_cache.clear()
        _disc_cache["m"] = minute
    key = (int(uid), max(1, _gf_i(qty, 1)))
    if key not in _disc_cache:
        try:
            _disc_cache[key] = float(await calcservice.effective_discount(key[0], key[1]))
        except Exception:  # noqa: BLE001 — личные скидки недоступны: стандарт всё равно наш
            log.debug("gridfinity client discount failed", exc_info=True)
            _disc_cache[key] = 0.0
    return _disc_cache[key] + std


def _gf_estimate(vol_cm3: float, h_mm: float, qty: int, plastic, settings: dict,
                 discount_pct: float = 0.0) -> dict:
    """{grams, minutes, price} на весь тираж ДЕТАЛИ — материал, время, брак, наценка, скидка.

    Скидка приходит готовым процентом (`_gf_discount`) и обязана быть той же, что у итога
    партии: строка спецификации — это цена ЭТОЙ детали, и клиент проверяет её сложением."""
    vol_cm3, qty = max(0.0, _gf_f(vol_cm3)), max(1, _gf_i(qty, 1))
    if plastic is None or vol_cm3 <= 0:
        return {"grams": 0.0, "minutes": 0.0, "price": 0.0}
    minutes = _gf_minutes(vol_cm3, h_mm)
    try:
        bd = pricing.compute_price(vol_cm3, minutes, qty, plastic, _gf_settings(settings),
                                   discount_pct=discount_pct)
        price, grams, mins = bd.total, bd.grams_total, bd.minutes_total
    except Exception:  # noqa: BLE001 — прайс-лист не настроен: показываем расход без денег
        log.debug("gridfinity estimate failed", exc_info=True)
        grams = vol_cm3 * float(getattr(plastic, "density", 1.24) or 1.24) * qty
        price, mins = 0.0, minutes * qty
    return {"grams": round(grams, 1), "minutes": round(mins, 1), "price": round(price, 2)}


async def _gf_money(view: list[dict], ids, uid=None) -> dict:
    """Смета ПАРТИИ одним расчётом: граммы и минуты складываем, цену считаем как заявку.

    Ровно то, что сделает мастер, выставляя счёт. Скидку считаем от количества штук ИМЕННО
    в этой партии: тиражная ступень зависит от тиража, и обещать её по всему проекту было бы
    обманом — заказывают-то часть.

    `discount_pct` в ответе нужен интерфейсу и описанию заявки: мастерская пишет клиенту,
    из чего сложилась цифра, а не показывает голый итог."""
    sel = [v for v in view if v["id"] in ids]
    grams = round(sum(v["grams"] for v in sel), 1)
    minutes = round(sum(v["minutes"] for v in sel), 1)
    qty = sum(v["qty"] for v in sel)
    out = {"n": len(sel), "qty": qty, "grams": grams, "minutes": minutes, "price": 0.0,
           "discount_pct": 0.0, "std_pct": 0.0}
    if not sel:
        return out
    plastic, settings = await _gf_price_ctx()
    out["std_pct"] = _gf_std_discount(settings)
    if plastic is None:
        return out
    disc = await _gf_discount(uid, qty, settings)
    out["discount_pct"] = round(disc, 2)
    try:
        bd = pricing.compute_price_manual(
            grams, minutes, float(getattr(plastic, "price_per_kg", 0) or 0),
            _gf_settings(settings), discount_pct=disc,
            failure_rate_pct=getattr(plastic, "failure_rate_pct", None))
        out["price"] = round(bd.total, 2)
    except Exception:  # noqa: BLE001
        log.debug("gridfinity batch price failed", exc_info=True)
        out["price"] = round(sum(v["price"] for v in sel), 2)
    return out


# ─────────────────────────── сверка раскладки со спецификацией ───────────────────────────

def _gf_frozen(part: dict, status: str) -> bool:
    """Деталь заморожена: её уже печатают или напечатали, менять заказанное нельзя."""
    st = str(part.get("state") or "")
    return st == "printed" or (st == "ordered" and status in _GF_FROZEN_STATUS)


def _gf_free(part: dict, status: str) -> bool:
    """Деталь свободна: её можно выбрать в партию и править. Не то же, что state == draft —
    заявка могла умереть (отказ/отмена), и тогда деталь снова свободна, хотя в БД у неё
    ещё стоит `ordered`. Без этого клиент не смог бы перезаказать отклонённую деталь."""
    return _gf_live_state(part, status) in ("draft", "hold")


def _gf_live_state(part: dict, status: str) -> str:
    """Показываемое состояние: у детали в заявке оно наследуется от статуса заявки."""
    st = str(part.get("state") or "draft")
    if st != "ordered":
        return st
    if status in _GF_DEAD_STATUS:
        return "draft"        # заявка умерла — деталь снова можно заказать
    return {"in_progress": "in_progress", "printed": "printed",
            "awaiting_payment": "awaiting_payment", "invoiced": "awaiting_payment",
            "price_changed": "awaiting_payment"}.get(status, "ordered")


_GF_LIVE_LABEL = dict(_GF_STATE_LABEL,
                      in_progress="в работе", awaiting_payment="ждёт оплаты")


async def _gf_statuses(parts: list[dict]) -> dict:
    """Статусы заявок, на которых висят детали: {request_id: status}. Один проход по БД."""
    out: dict[int, str] = {}
    for rid in {_gf_i(p.get("request_id")) for p in parts if _gf_i(p.get("request_id"))}:
        req = await db.get_request(rid)
        out[rid] = str(getattr(req, "status", "") or "") if req else "cancelled"
    return out


async def _gf_reconcile(project_id: int, cfg: dict) -> dict:
    """Свести спецификацию с раскладкой. Раскладка — правда, детали — её проекция.

    Три случая, и каждый — не про красоту, а про деньги клиента:
      * модуль появился → новая деталь-черновик;
      * модуль исчез → деталь удаляется, НО заказанная не удаляется никогда: mod_id = 0,
        состояние остаётся. Иначе заявка ссылается в пустоту;
      * модуль изменился под заказанной деталью → пока заявку не взяли в работу, правка
        проходит насквозь (только помечаем `changed_at`, чтобы предупредить мастера);
        после — заказанная версия ЗАМОРОЖЕНА, а модуль форкается в новую деталь v2.

    Возвращает {added, dropped, forked, changed} — движку и клиенту это нужно, чтобы
    честно сказать, что произошло, а не молча переписать спецификацию."""
    parts = await db.gf_parts(project_id)
    statuses = await _gf_statuses(parts)
    mods = {_gf_i(m.get("id")): m for m in _gf_modules(cfg) if _gf_i(m.get("id"))}
    seen: set[int] = set()
    added = dropped = forked = changed = 0
    now = int(time.time())

    for p in parts:
        mid = _gf_i(p.get("mod_id"))
        if not mid:
            continue                       # деталь уже снята с раскладки — она в истории
        m = mods.get(mid)
        status = statuses.get(_gf_i(p.get("request_id")), "")
        if m is None:
            if str(p.get("state")) in ("ordered", "printed"):
                await db.gf_part_update(int(p["id"]), mod_id=0)
                dropped += 1
            else:
                await db.gf_part_delete(int(p["id"]))
            continue
        seen.add(mid)
        sig = _gf_sig(m)
        vol = _gf_filament(m)
        color = _gf_i(m.get("colorIdx"))
        if sig == str(p.get("sig") or ""):
            patch = {}
            if abs(vol - _gf_f(p.get("vol_cm3"))) > 1e-6:
                patch["vol_cm3"] = vol     # объём считает движок и присылает с раскладкой
            if color != _gf_i(p.get("color_idx")):
                patch["color_idx"] = color
            if patch:
                await db.gf_part_update(int(p["id"]), **patch)
            continue
        # геометрия под деталью изменилась
        if _gf_frozen(p, status):
            # v1 остаётся в истории со своей заявкой и своим снимком, модуль уходит к v2
            await db.gf_part_update(int(p["id"]), mod_id=0)
            await db.gf_part_add(
                project_id=project_id, mod_id=mid, name=str(p.get("name") or ""),
                note=str(p.get("note") or ""), qty=max(1, _gf_i(p.get("qty"), 1)),
                state="draft", color_idx=color, sig=sig, vol_cm3=vol,
                version=_gf_i(p.get("version"), 1) + 1, parent_id=int(p["id"]))
            forked += 1
        else:
            patch = {"sig": sig, "vol_cm3": vol, "color_idx": color}
            # уведомляем мастера ТОЛЬКО на переходе «не менял → изменил»: без этого условия
            # клиент, полчаса подгоняющий уже заказанный лоток, слал бы десятки одинаковых
            # пушей всему персоналу — их перестают читать вместе с настоящими
            if str(p.get("state")) == "ordered" and not _gf_i(p.get("changed_at")):
                patch["changed_at"] = now
                changed += 1
            await db.gf_part_update(int(p["id"]), **patch)

    room = _GF_MAX_PARTS - sum(1 for p in parts if _gf_i(p.get("mod_id")))
    skipped = 0
    for mid, m in mods.items():
        if mid in seen:
            continue
        if room <= 0:
            skipped += 1        # молча терять модуль нельзя: клиент не сможет его заказать
            continue
        room -= 1
        await db.gf_part_add(
            project_id=project_id, mod_id=mid, name="", state="draft",
            color_idx=_gf_i(m.get("colorIdx")), sig=_gf_sig(m), vol_cm3=_gf_filament(m))
        added += 1
    return {"added": added, "dropped": dropped, "forked": forked, "changed": changed,
            "skipped": skipped}


# ─────────────────────────── сборка ответов ───────────────────────────

def _gf_part_view(p: dict, m: dict | None, g: dict, status: str, plastic, settings: dict,
                  discount_pct: float = 0.0) -> dict:
    """Строка спецификации для клиента: геометрия из раскладки, всё остальное — из детали."""
    frozen = _gf_frozen(p, status)
    spec = None
    if not m and p.get("spec_json"):
        try:
            spec = json.loads(p["spec_json"])
        except Exception:  # noqa: BLE001
            spec = None
    src = m or spec or {}
    w, d, h = _gf_dims(src, g) if src else (0.0, 0.0, 0.0)
    vol = _gf_f(p.get("vol_cm3"))
    qty = max(1, _gf_i(p.get("qty"), 1))
    est = _gf_estimate(vol, h, qty, plastic, settings, discount_pct)
    live = _gf_live_state(p, status)
    color = _gf_i(p.get("color_idx")) % len(_GF_COLORS)
    return {
        "id": int(p["id"]), "mod_id": _gf_i(p.get("mod_id")),
        "name": str(p.get("name") or "") or (_gf_auto_name(src, g) if src else "Деталь"),
        "auto_name": _gf_auto_name(src, g) if src else "",
        "custom_name": bool(str(p.get("name") or "").strip()),
        "note": str(p.get("note") or ""), "qty": qty,
        "state": str(p.get("state") or "draft"), "live_state": live,
        "state_label": _GF_LIVE_LABEL.get(live, live),
        "sel": 1 if _gf_i(p.get("sel")) else 0,
        "request_id": _gf_i(p.get("request_id")), "req_status": status,
        "version": _gf_i(p.get("version"), 1), "parent_id": _gf_i(p.get("parent_id")),
        "frozen": 1 if frozen else 0,
        "changed": 1 if _gf_i(p.get("changed_at")) else 0,
        "on_plan": 1 if m else 0,
        "color_idx": color, "color": _GF_COLORS[color], "color_hex": _GF_COLOR_HEX[color],
        "gen": str(src.get("gen") or ("wing" if src.get("kind") == "wing" else "tray")),
        "gen_label": ("пристенный" if src.get("kind") == "wing"
                      else _GF_GEN.get(str(src.get("gen") or "tray"), "лоток")),
        "cells": f"{max(1, _gf_i(src.get('w')))}×{max(1, _gf_i(src.get('d')))}" if src else "",
        "w_mm": round(w, 1), "d_mm": round(d, 1), "h_mm": round(h, 1),
        "units": _gf_units(h), "vol_cm3": round(vol, 2),
        "grams": est["grams"], "minutes": est["minutes"], "price": est["price"],
        "printed_at": _gf_i(p.get("printed_at")),
    }


async def _gf_parts_view(project_id: int, cfg: dict, money: bool = True,
                         uid=None) -> tuple[list[dict], list[dict]]:
    """(строки спецификации, сырые детали) — общий сбор для всех читающих ручек.

    money=False — без прайс-листа (список проектов показывает счётчики, а не цены).

    uid нужен ради скидки: тиражная и персональная складываются со стандартной, и строка
    спецификации обязана показывать тот же процент, что итог партии. Считаем его ОДИН раз
    на весь вид — по количеству штук, УЖЕ выбранных в партию: именно эту партию клиент и
    оформит, если нажмёт «заказать» сейчас. uid=None (мастер смотрит чужой проект без
    контекста клиента) — остаётся только стандартная скидка."""
    parts = await db.gf_parts(project_id)
    statuses = await _gf_statuses(parts)
    # Заявку отклонили или отменили — деталь освобождается САМА, при первом же чтении.
    # Иначе она осталась бы «в заявке» навсегда, и заказать её заново было бы нечем,
    # кроме как удалить модуль и нарисовать заново.
    for p in parts:
        rid = _gf_i(p.get("request_id"))
        if rid and str(p.get("state")) == "ordered" and statuses.get(rid) in _GF_DEAD_STATUS:
            await db.gf_part_update(int(p["id"]), state="draft", request_id=0)
            p["state"], p["request_id"] = "draft", 0
    g = _gf_grid(cfg.get("base") or {})
    mods = {_gf_i(m.get("id")): m for m in _gf_modules(cfg) if _gf_i(m.get("id"))}
    plastic, settings = await _gf_price_ctx() if money else (None, {})
    disc = 0.0
    if money:
        in_batch = sum(max(1, _gf_i(p.get("qty"), 1)) for p in parts if _gf_i(p.get("sel")))
        disc = await _gf_discount(uid, in_batch, settings)
    view = [_gf_part_view(p, mods.get(_gf_i(p.get("mod_id"))), g,
                          statuses.get(_gf_i(p.get("request_id")), ""), plastic, settings, disc)
            for p in parts]
    return view, parts


def _gf_counts(view: list[dict]) -> dict:
    """Сводка состояний для карточки проекта: полоска «2 напечатано · 3 в работе · 7 черновик»."""
    out = {"total": len(view), "draft": 0, "hold": 0, "ordered": 0, "printed": 0, "sel": 0}
    for v in view:
        live = v["live_state"]
        if live == "printed":
            out["printed"] += 1
        elif live in ("ordered", "in_progress", "awaiting_payment"):
            out["ordered"] += 1
        elif live == "hold":
            out["hold"] += 1
        elif live != "dropped":
            out["draft"] += 1
        out["sel"] += v["sel"]
    return out


def _gf_cfg_of(row: dict) -> dict:
    """Раскладка проекта из строки БД; битая — пустая, но проект открывается."""
    try:
        cfg = json.loads(row.get("config") or "{}")
    except Exception:  # noqa: BLE001
        log.warning("gridfinity project %s unreadable", row.get("id"))
        cfg = {}
    return cfg if _gf_valid(cfg) else {"base": {}, "modules": []}


def _gf_dump(cfg) -> str:
    return json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))


def _gf_too_big(request: web.Request) -> bool:
    """Отбить негабарит ПО ЗАГОЛОВКУ, до request.json(): приложение поднято с client_max_size
    под загрузку моделей (~сотни МБ), и без этой проверки 200-МБ JSON целиком парсился бы в
    память раньше валидации и ратлимита — прямой путь в OOM на машине владельца."""
    return (request.content_length or 0) > _GF_MAX_BYTES + 4096   # +запас на initData в теле


def _gf_oversize(raw: str) -> bool:
    return len(raw.encode("utf-8")) > _GF_MAX_BYTES


# ─────────────────── кто спрашивает и в каких границах ───────────────────

_GF_DESK_TTL = 7 * 24 * 3600      # неделя: ящик собирают не за вечер


async def _gf_actor(request: web.Request, body: dict | None = None) -> tuple[int, int]:
    """(uid, scope): кто это и что ему открыто.

    scope = 0 — обычный клиент из Telegram: границ нет, все его проекты его.
    scope = pid — СЕССИЯ ПК, ссылка «продолжить на компьютере». Она даёт править РОВНО
    один проект, поэтому каждая ручка обязана сверить свой pid со scope, а ручки, которые
    к проекту не относятся (список проектов, мерки, заготовки), обязаны требовать scope = 0.

    Ключ сознательно НЕ подмешан в `_verify_init_data`: там он открыл бы весь аккаунт —
    заказы, профиль, чат, — а обещано было «доступ к проекту». Цена этому решению — явная
    проверка в каждой ручке; она дешевле, чем один забытый роут с чужими данными."""
    src = body if body is not None else request.query
    uid = _verify_init_data(str((src or {}).get("initData") or ""))
    if uid:
        return int(uid), 0
    key = str((src or {}).get("gfxkey") or "").strip()
    if not key:
        return 0, 0
    ses = await db.gf_desk_get(key)          # просроченную get не отдаёт и сам убирает
    if not ses:
        return 0, 0
    return int(ses["user_id"]), int(ses["project_id"])


async def _gf_uname(uid: int, u: dict | None) -> str:
    """Подпись клиента в заявке.

    Из Telegram имя приезжает в initData. В сессии ПК initData нет вовсе, поэтому берём то,
    чем клиент уже подписывался в прошлых заявках: мастеру в списке нужна привычная строка,
    а не голый id, — иначе заказ «с компьютера» выглядит как заказ от незнакомца."""
    if u:
        if u.get("username"):
            return "@" + str(u["username"])
        got = " ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x)
        if got:
            return got
    return (await db.gf_user_name(uid)) or f"id {uid}"


def _gf_denied(scope: int, pid: int) -> bool:
    """Сессия ПК полезла не в свой проект (или туда, где проекта нет вовсе)."""
    return bool(scope) and scope != int(pid or 0)


async def _gf_body(request: web.Request):
    """(body, uid, scope) или (None, ответ-ошибка). Один разбор тела на все пишущие ручки.

    Тело читаем САМИ с потолком, а не через request.json(): заголовок Content-Length
    легко не прислать вовсе (Transfer-Encoding: chunked), и тогда проверка `_gf_too_big`
    пропускала бы к парсеру сотни мегабайт — ровно тот OOM, который она обещает закрыть."""
    if _gf_too_big(request):
        return None, web.json_response({"ok": False, "error": "Конфигурация слишком большая"})
    try:
        raw = await request.content.read(_GF_MAX_BYTES + 8192)
        if len(raw) > _GF_MAX_BYTES + 4096:
            return None, web.json_response({"ok": False,
                                            "error": "Конфигурация слишком большая"})
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            return None, _bad()
    except Exception:  # noqa: BLE001
        return None, _bad()
    uid, scope = await _gf_actor(request, body)
    if not uid:
        return None, web.json_response({"ok": False, "error": "auth"}, status=403)
    return (body, uid, scope), None


async def _gf_owned(pid: int, uid: int):
    """Проект юзера или None: чужой проект — это 403, а не «не найдено»."""
    row = await db.gf_project(pid)
    if not row or int(row.get("user_id") or 0) != uid:
        return None
    return row


# ─────────────────────────── preflight ───────────────────────────

_bed_cache: dict = {}


async def _gf_beds() -> list[tuple[str, float, float]]:
    """(имя, столXY, столZ) активных принтеров. Профиль читается с диска — кэшируем:
    preflight зовут перед каждым заказом, а стол принтера меняется раз в год."""
    key = int(time.time()) // 300
    if _bed_cache.get("key") == key:
        return _bed_cache["v"]
    out = []
    try:
        for p in await db.list_printers(active_only=True):
            bed = bedspec.BedSpec.from_machine_profile(getattr(p, "machine_profile", "") or "")
            out.append((str(getattr(p, "name", "") or "принтер"), bed.fit_xy_mm, bed.z_mm))
    except Exception:  # noqa: BLE001 — без списка принтеров проверка стола просто не делается
        log.debug("gridfinity beds failed", exc_info=True)
    _bed_cache.update(key=key, v=out)
    return out


async def _gf_color_need(view: list[dict], ids: set) -> list[dict]:
    """Сколько граммов какого цвета нужно партии и хватает ли их на складе.

    Склад ведётся по НАЗВАНИЯМ цветов катушек, а конфигуратор — по своей палитре из шести;
    сверяем по вхождению названия, а не по равенству: «Оранжевый» и «оранж» — один цвет."""
    need: dict[int, float] = {}
    for v in view:
        if v["id"] in ids:
            need[v["color_idx"]] = need.get(v["color_idx"], 0.0) + v["grams"]
    if not need:
        return []
    stock: dict[str, float] = {}
    try:
        for p in await db.list_plastics(active_only=True):
            avail = await db.color_availability(int(p.id))
            for name, info in (avail or {}).items():
                got = info.get("stock") if isinstance(info, dict) else info
                stock[str(name).lower()] = stock.get(str(name).lower(), 0.0) + _gf_f(got)
    except Exception:  # noqa: BLE001 — склад не настроен: просто не предупреждаем
        log.debug("gridfinity stock failed", exc_info=True)
        return []
    out = []
    for idx, grams in sorted(need.items()):
        cname = _GF_COLORS[idx % len(_GF_COLORS)]
        have = sum(v for k, v in stock.items() if cname[:5] in k)
        out.append({"color": cname, "color_hex": _GF_COLOR_HEX[idx % len(_GF_COLOR_HEX)],
                    "need": round(grams, 1), "have": round(have, 1),
                    "short": 1 if stock and have < grams else 0})
    return out


async def _gf_preflight(cfg: dict, view: list[dict], ids: set) -> list[dict]:
    """Чек-лист перед заказом: два пункта блокируют, остальные предупреждают.

    Смысл в том, чтобы клиент узнал о проблеме ДО того, как заплатил: деталь выше ящика и
    деталь, не влезающая на стол принтера, — это не «предупреждение», это брак."""
    out: list[dict] = []
    base = cfg.get("base") or {}
    inner = _gf_inner_h(base)
    beds = await _gf_beds()
    fit_xy = max((b[1] for b in beds), default=0.0)
    fit_z = max((b[2] for b in beds), default=0.0)
    sel = [v for v in view if v["id"] in ids]
    for v in sel:
        if inner and v["h_mm"] > inner + 0.5:
            out.append({"level": "block", "part_id": v["id"],
                        "text": f"«{v['name']}» — {v['h_mm']:.0f} мм выше полезной высоты "
                                f"{inner:.0f} мм: крышка не закроется"})
        if fit_xy and max(v["w_mm"], v["d_mm"]) > fit_xy:
            out.append({"level": "block", "part_id": v["id"],
                        "text": f"«{v['name']}» — {max(v['w_mm'], v['d_mm']):.0f} мм длиннее "
                                f"стола принтера ({fit_xy:.0f} мм): разбейте на две детали"})
        elif fit_z and v["h_mm"] > fit_z:
            out.append({"level": "block", "part_id": v["id"],
                        "text": f"«{v['name']}» выше стола принтера по Z ({fit_z:.0f} мм)"})
        if v["vol_cm3"] <= 0:
            out.append({"level": "warn", "part_id": v["id"],
                        "text": f"«{v['name']}» — объём не посчитан, оценка будет неточной"})
        if v["gen"] == "tray" and v["cells"] not in ("", "1×1") and v["h_mm"] > 60:
            out.append({"level": "warn", "part_id": v["id"],
                        "text": f"«{v['name']}» — глубокий пустой лоток: точно без разметки?"})
    g = _gf_grid(base)
    if max(g["mL"], g["mR"], g["mB"], g["mF"]) > 8:
        out.append({"level": "warn", "part_id": 0,
                    "text": f"По краям остаётся до {max(g['mL'], g['mR'], g['mB'], g['mF']):.0f} мм — "
                            "добор у крайних модулей уберёт щель"})
    seen: dict[str, int] = {}
    for v in sel:
        key = f"{v['cells']}|{v['h_mm']:.1f}|{v['gen']}"
        seen[key] = seen.get(key, 0) + 1
    for v in view:
        if v["id"] in ids or v["live_state"] not in ("ordered", "in_progress"):
            continue
        if f"{v['cells']}|{v['h_mm']:.1f}|{v['gen']}" in seen:
            out.append({"level": "warn", "part_id": v["id"],
                        "text": f"Похожая деталь уже печатается в заявке #{v['request_id']} — "
                                "заказываем вторую?"})
            break
    for c in await _gf_color_need(view, ids):
        if c["short"]:
            out.append({"level": "warn", "part_id": 0,
                        "text": f"{c['color'].capitalize()}: на складе {c['have']:.0f} г из "
                                f"нужных {c['need']:.0f} г — мастер предложит замену"})
    return out


# ─────────────────────────── ручки: проекты ───────────────────────────

async def _gf_migrate_draft(uid: int) -> None:
    """Единственный черновик старого конфигуратора → проект «Мой органайзер».

    Молча и без вопросов: у людей уже лежат незаказанные раскладки, и потерять их при
    переходе на проекты нельзя. Срабатывает ровно один раз — когда проектов ещё нет вовсе
    (после первого же сохранения проект-зеркало пишет тот же черновик обратно)."""
    if await db.gf_project_count(uid, archived=-1):
        return
    raw = await db.gridfinity_config_get(uid)
    if not raw:
        return
    try:
        cfg = json.loads(raw)
    except Exception:  # noqa: BLE001
        return
    if not _gf_valid(cfg) or not _gf_modules(cfg):
        return       # пустой черновик заворачивать не во что
    pid = await db.gf_project_add(uid, "Мой органайзер", raw)
    await _gf_reconcile(pid, cfg)
    log.info("gridfinity: черновик uid=%s превращён в проект #%s", uid, pid)


@routes.get("/api/gridfinity/projects")
async def _api_gf_projects(request: web.Request) -> web.Response:
    """GET — «Мастерская»: карточки проектов с планом и сводкой состояний деталей."""
    uid, scope = await _gf_actor(request)
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    await _gf_migrate_draft(uid)
    archived = 1 if request.query.get("archived") in ("1", "true") else 0
    rows = await db.gf_projects(uid, archived=archived)
    out = []
    for row in rows:
        cfg = _gf_cfg_of(row)
        view, _ = await _gf_parts_view(int(row["id"]), cfg, money=False)
        ordered = {v["mod_id"] for v in view
                   if v["live_state"] in ("ordered", "in_progress", "printed") and v["mod_id"]}
        out.append({"id": int(row["id"]), "title": str(row.get("title") or "Без названия"),
                    "updated_at": _gf_i(row.get("updated_at")),
                    "archived": 1 if _gf_i(row.get("archived_at")) else 0,
                    "preview": _gf_preview(cfg, ordered), "counts": _gf_counts(view)})
    return web.json_response({"ok": True, "projects": out,
                              "active": await db.gf_project_count(uid, archived=0),
                              "archived_n": await db.gf_project_count(uid, archived=1),
                              "limit": _GF_MAX_PROJECTS,
                              "measures": await db.gf_measures(uid)})


@routes.post("/api/gridfinity/projects")
async def _api_gf_project_add(request: web.Request) -> web.Response:
    """POST {title, config?, from_id?, measure_id?} — создать проект, в т.ч. дублированием.

    Дублирование — главное действие мастерской, а не удаление: второй такой же ящик,
    соседний ящик тумбы, «то же самое, но глубже». Детали в копию НЕ переносятся: копия
    ничего не заказывала."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    if not ratelimit.allow(f"gfproj:{uid}", 20, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    if await db.gf_project_count(uid, archived=0) >= _GF_MAX_PROJECTS:
        return web.json_response({"ok": False, "error": (
            f"Больше {_GF_MAX_PROJECTS} активных проектов не держим — заархивируйте лишние")})
    title = str(body.get("title") or "").strip()[:80] or "Новый ящик"
    cfg = body.get("config")
    src = _gf_i(body.get("from_id"))
    if src:
        row = await _gf_owned(src, uid)
        if not row:
            return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
        cfg = _gf_cfg_of(row)
        if not str(body.get("title") or "").strip():
            title = f"{str(row.get('title') or 'Ящик')} — копия"[:80]
    if not _gf_valid(cfg):
        cfg = {"v": 4, "base": {"type": "drawer", "W": 480, "D": 270, "H": 120,
                                "series": "M", "fitX": "sym", "fitY": "sym"},
               "modules": [], "seq": 1}
    mid = _gf_i(body.get("measure_id"))
    if mid:
        for mrow in await db.gf_measures(uid):
            if int(mrow["id"]) == mid:
                cfg["base"] = dict(cfg.get("base") or {}, type=str(mrow["btype"]),
                                   W=_gf_f(mrow["w"]), D=_gf_f(mrow["d"]), H=_gf_f(mrow["h"]),
                                   series=str(mrow["series"] or "M"))
                break
    raw = _gf_dump(cfg)
    if _gf_oversize(raw):
        return web.json_response({"ok": False, "error": "Конфигурация слишком большая"})
    pid = await db.gf_project_add(uid, title, raw)
    await _gf_reconcile(pid, cfg)
    await db.log_event(uid, "создал gridfinity-проект", f"#{pid}: {title}")
    return web.json_response({"ok": True, "id": pid})


@routes.get("/api/gridfinity/project/{pid}")
async def _api_gf_project(request: web.Request) -> web.Response:
    """GET — проект целиком: раскладка, спецификация, партии, ссылка «показать»."""
    uid, scope = await _gf_actor(request)
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if _gf_denied(scope, _toint(request.match_info.get("pid"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"},
                                 status=403)
    row = await _gf_owned(_toint(request.match_info.get("pid")), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
    cfg = _gf_cfg_of(row)
    view, _ = await _gf_parts_view(int(row["id"]), cfg, uid=uid)
    std_plastic, std_settings = await _gf_price_ctx()
    return web.json_response({
        "ok": True,
        "money": await _gf_money(view, {v["id"] for v in view if v["sel"]}, uid),
        # стандарт показываем ВСЕГДА, даже когда в партию ничего не выбрано: это условие
        # мастерской, а не строчка сметы
        "standard": {"text": _gf_std_text(std_plastic), "pct": _gf_std_discount(std_settings)},
        "project": {"id": int(row["id"]), "title": str(row.get("title") or ""),
                    "archived": 1 if _gf_i(row.get("archived_at")) else 0,
                    "updated_at": _gf_i(row.get("updated_at"))},
        "config": cfg, "parts": view, "counts": _gf_counts(view),
        "batches": await _gf_batches(int(row["id"]), view, uid),
        "versions": await db.gf_versions(int(row["id"])),
        "share": await db.gf_share_of(int(row["id"])) or "",
        "desk": await _gf_desk_info(int(row["id"])),
        "colors": [{"name": n, "hex": h} for n, h in zip(_GF_COLORS, _GF_COLOR_HEX)],
    })


@routes.post("/api/gridfinity/project/{pid}")
async def _api_gf_project_save(request: web.Request) -> web.Response:
    """POST {config} — сохранить раскладку (движок шлёт с дебаунсом ~1.5 с) и свести
    спецификацию. Ответ несёт итог сверки: клиент обязан сказать, что деталь форкнулась."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if _gf_denied(scope, _toint(request.match_info.get("pid"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"},
                                 status=403)
    # 60/60: дебаунс движка 1.5 с физически даёт до 40 сохранений в минуту при активном
    # редактировании — прежний лимит 30 молча ронял часть из них
    if not ratelimit.allow(f"gfsave:{uid}", 60, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    row = await _gf_owned(_toint(request.match_info.get("pid")), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
    cfg = body.get("config")
    if not _gf_valid(cfg):
        return web.json_response({"ok": False, "error": "Непонятная конфигурация"})
    raw = _gf_dump(cfg)
    if _gf_oversize(raw):
        return web.json_response({"ok": False, "error": "Конфигурация слишком большая"})
    await db.gf_project_set_config(int(row["id"]), raw)
    await db.gridfinity_config_set(uid, raw)   # зеркало для старого фронта
    diff = await _gf_reconcile(int(row["id"]), cfg)
    if diff["forked"] or diff["changed"]:
        parts = await db.gf_parts(int(row["id"]))
        rids = [_gf_i(p.get("request_id")) for p in parts if _gf_i(p.get("request_id"))]
        await _gf_notify_change(uid, row, diff, rids)
    return web.json_response({"ok": True, "diff": diff})


@routes.post("/api/gridfinity/project/{pid}/patch")
async def _api_gf_project_patch(request: web.Request) -> web.Response:
    """POST {title?|archived?|delete?|snapshot?|restore?|share?} — правки самого проекта.

    Удаление проекта, из которого что-то заказывали, запрещено: заявка ссылалась бы в
    пустоту. Такой проект уходит в архив — это и есть «удалить» в интерфейсе."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if _gf_denied(scope, _toint(request.match_info.get("pid"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"},
                                 status=403)
    # Из сессии ПК правят раскладку и спецификацию — но не распоряжаются самим проектом:
    # удаление, архив и раздача ссылок остаются за тем, кто вошёл через Telegram.
    if scope and (body.get("delete") or "archived" in body or "share" in body):
        return web.json_response({"ok": False, "error": (
            "Удаление, архив и ссылки — из Telegram")}, status=403)
    if not ratelimit.allow(f"gfpatch:{uid}", 120, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    row = await _gf_owned(_toint(request.match_info.get("pid")), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
    pid = int(row["id"])
    if "title" in body:
        await db.gf_project_rename(pid, str(body.get("title") or "").strip()[:80] or "Без названия")
    if "archived" in body:
        await db.gf_project_archive(pid, bool(body.get("archived")))
        if body.get("archived"):
            # публичная ссылка не должна пережить «удаление»: клиент прочитал «ушёл в
            # архив» и считает вопрос закрытым, а страница с планом и именами деталей
            # продолжала бы отвечать всем, кому он её когда-то отправил
            await db.gf_share_drop(pid)
    if body.get("snapshot"):
        await db.gf_version_add(pid, str(row.get("config") or "{}"),
                                str(body.get("note") or "")[:80], keep=_GF_VERSIONS_KEEP)
    restored = None
    if body.get("restore"):
        ver = await db.gf_version_get(_gf_i(body.get("restore")))
        if not ver or int(ver["project_id"]) != pid:
            return web.json_response({"ok": False, "error": "Версия не найдена"}, status=404)
        # текущая раскладка сама становится снимком: откат не должен быть дорогой без обратного
        await db.gf_version_add(pid, str(row.get("config") or "{}"), "перед откатом",
                                keep=_GF_VERSIONS_KEEP)
        cfg = json.loads(ver["config"]) if ver.get("config") else {}
        if _gf_valid(cfg):
            # ts «сейчас»: у движка арбитром свежести между серверной и локальной копией
            # служит именно он, и со старой меткой откат молча проиграл бы localStorage.
            cfg = dict(cfg, ts=int(time.time() * 1000))
            await db.gf_project_set_config(pid, _gf_dump(cfg))
            await _gf_reconcile(pid, cfg)
            restored = cfg
    if "share" in body:
        if body.get("share"):
            token = secrets.token_urlsafe(9)
            await db.gf_share_set(pid, uid, token)
        else:
            await db.gf_share_drop(pid)
    if "desk" in body:
        # Ссылку выпускают и гасят ТОЛЬКО из Telegram: перевыпустить её по уже утёкшей
        # ссылке значило бы продлевать утечку её же руками. Погасить — тоже оттуда: иначе
        # тот, кто ссылку нашёл, мог бы отобрать доступ у хозяина.
        if scope:
            return web.json_response({"ok": False, "error": (
                "Ссылку «на компьютере» выпускают из Telegram")}, status=403)
        if body.get("desk"):
            # 18 байт энтропии против 9 у «показать»: та открывает чтение одной раскладки,
            # эта — правку проекта и оформление заявок. Перебирать её не должно быть смысла.
            token = secrets.token_urlsafe(18)
            exp = await db.gf_desk_set(pid, uid, token, _GF_DESK_TTL)
            await _gf_desk_notify(uid, str(row.get("title") or ""), token, exp)
        else:
            await db.gf_desk_drop(pid)
    if body.get("delete"):
        parts = await db.gf_parts(pid)
        if any(str(p.get("state")) in ("ordered", "printed") for p in parts):
            await db.gf_project_archive(pid, True)
            await db.gf_share_drop(pid)
            return web.json_response({"ok": True, "archived": 1, "note": (
                "Из проекта уже что-то заказывали — он ушёл в архив, а не удалён")})
        await db.gf_project_delete(pid)
        # зеркало старого черновика сносим вместе с ПОСЛЕДНИМ проектом: иначе миграция
        # воскресила бы только что удалённый ящик при следующем заходе в мастерскую
        if not await db.gf_project_count(uid, archived=-1):
            await db.gf_drop_legacy_draft(uid)
        return web.json_response({"ok": True, "deleted": 1})
    fresh = await db.gf_project(pid)
    # Откат обязан ДОЕХАТЬ до сцены: без возврата конфигурации клиент видел бы зелёный
    # тост, прежнюю раскладку в 3D — и через полторы секунды автосохранение вернуло бы
    # всё как было, попутно пересобрав спецификацию.
    return web.json_response({"ok": True, "share": await db.gf_share_of(pid) or "",
                              "desk": await _gf_desk_info(pid),
                              "config": restored,
                              "title": str((fresh or {}).get("title") or "")})


# ─────────────────────────── ручки: детали ───────────────────────────

@routes.post("/api/gridfinity/part/{id}")
async def _api_gf_part_patch(request: web.Request) -> web.Response:
    """POST {name?, note?, qty?, state?, sel?} — правка одной детали спецификации.

    Геометрию здесь не трогают: она живёт в раскладке. Заказанной детали меняем только
    заметку — количество и состояние уже уехали мастеру."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if not ratelimit.allow(f"gfpart:{uid}", 120, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    part = await db.gf_part(_toint(request.match_info.get("id")))
    if not part:
        return web.json_response({"ok": False, "error": "Деталь не найдена"}, status=404)
    # pid в адресе этой ручки нет — границы сессии ПК сверяем по проекту САМОЙ детали
    if _gf_denied(scope, _gf_i(part.get("project_id"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"}, status=403)
    row = await _gf_owned(int(part["project_id"]), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Деталь не найдена"}, status=404)
    statuses = await _gf_statuses([part])
    free = _gf_free(part, statuses.get(_gf_i(part.get("request_id")), ""))
    patch: dict = {}
    if "name" in body:
        patch["name"] = str(body.get("name") or "").strip()[:60]
    if "note" in body:
        patch["note"] = str(body.get("note") or "").strip()[:200]
    if free:
        if "qty" in body:
            patch["qty"] = max(1, min(50, _gf_i(body.get("qty"), 1)))
        if "sel" in body:
            patch["sel"] = 1 if body.get("sel") else 0
        st = str(body.get("state") or "")
        if st in ("draft", "hold"):
            patch["state"] = st
            if st == "hold":
                patch["sel"] = 0    # отложенная деталь не может остаться в партии
    elif any(k in body for k in ("qty", "sel", "state")):
        return web.json_response({"ok": False, "error": (
            "Деталь уже в заявке — измените её в раскладке, и мы сделаем новую версию")})
    if patch:
        await db.gf_part_update(int(part["id"]), **patch)
    cfg = _gf_cfg_of(row)
    view, _ = await _gf_parts_view(int(row["id"]), cfg, uid=uid)
    return web.json_response({"ok": True, "parts": view, "counts": _gf_counts(view),
                              "money": await _gf_money(view, {v["id"] for v in view if v["sel"]},
                                                       uid)})


@routes.post("/api/gridfinity/project/{pid}/select")
async def _api_gf_parts_select(request: web.Request) -> web.Response:
    """POST {ids, sel} — групповой выбор «в партию»: «всё незаказанное», «по цвету», «по ряду».

    Групповые действия считает клиент (он же рисует фильтры), сервер принимает готовый
    список — и всё равно проверяет, что деталь из ЭТОГО проекта и её можно выбирать."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if _gf_denied(scope, _toint(request.match_info.get("pid"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"},
                                 status=403)
    if not ratelimit.allow(f"gfsel:{uid}", 90, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    row = await _gf_owned(_toint(request.match_info.get("pid")), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
    ids = {_gf_i(x) for x in (body.get("ids") or []) if _gf_i(x)}
    parts = await db.gf_parts(int(row["id"]))
    statuses = await _gf_statuses(parts)
    ok_ids = [int(p["id"]) for p in parts if int(p["id"]) in ids
              and _gf_free(p, statuses.get(_gf_i(p.get("request_id")), ""))]
    sel = 1 if body.get("sel") else 0
    # выбор снимает «отложено» и отвязывает деталь от умершей заявки — ОДНИМ запросом:
    # раньше это был commit на каждую деталь, а «выбрать всё» жмут по шестьдесят штук
    await db.gf_parts_select(int(row["id"]), ok_ids, sel, free=bool(sel))
    cfg = _gf_cfg_of(row)
    view, _ = await _gf_parts_view(int(row["id"]), cfg, uid=uid)
    return web.json_response({"ok": True, "parts": view, "counts": _gf_counts(view),
                              "money": await _gf_money(view, {v["id"] for v in view if v["sel"]},
                                                       uid)})


# ─────────────────────────── ручки: партия ───────────────────────────

async def _gf_batches(project_id: int, view: list[dict], uid=None) -> list[dict]:
    """Партии проекта: карточка на заявку с её деталями и статусом."""
    by_req: dict[int, list[dict]] = {}
    for v in view:
        if v["request_id"]:
            by_req.setdefault(v["request_id"], []).append(v)
    out = []
    for rid, items in sorted(by_req.items()):
        req = await db.get_request(rid)
        done = sum(1 for v in items if v["live_state"] == "printed")
        est = await _gf_money(items, {v["id"] for v in items}, uid)
        out.append({
            "request_id": rid, "status": str(getattr(req, "status", "") or ""),
            "status_label": db.REQ_STATUS_LABELS.get(str(getattr(req, "status", "") or ""), ""),
            "created_at": _gf_i(getattr(req, "created_at", 0)),
            "price": _gf_f(getattr(req, "price", 0)),
            "n": len(items), "qty": sum(v["qty"] for v in items), "printed": done,
            "mod_ids": [v["mod_id"] for v in items if v["mod_id"]],
            "est": est["price"],
        })
    return out


@routes.get("/api/gridfinity/project/{pid}/preflight")
async def _api_gf_preflight(request: web.Request) -> web.Response:
    """GET ?ids=1,2,3 — чек-лист и смета партии до оформления."""
    uid, scope = await _gf_actor(request)
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if _gf_denied(scope, _toint(request.match_info.get("pid"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"},
                                 status=403)
    row = await _gf_owned(_toint(request.match_info.get("pid")), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
    cfg = _gf_cfg_of(row)
    view, _ = await _gf_parts_view(int(row["id"]), cfg, uid=uid)
    raw_ids = str(request.query.get("ids") or "")
    ids = ({_gf_i(x) for x in raw_ids.split(",") if _gf_i(x)} if raw_ids
           else {v["id"] for v in view if v["sel"]})
    sel = [v for v in view if v["id"] in ids]
    checks = await _gf_preflight(cfg, view, ids)
    std_plastic, std_settings = await _gf_price_ctx()
    return web.json_response({
        "ok": True, "parts": sel, "checks": checks,
        "standard": {"text": _gf_std_text(std_plastic), "pct": _gf_std_discount(std_settings)},
        "colors": await _gf_color_need(view, ids),
        "blocked": 1 if any(c["level"] == "block" for c in checks) else 0,
        "total": await _gf_money(view, ids, uid)})


@routes.post("/api/gridfinity/project/{pid}/order")
async def _api_gf_order(request: web.Request) -> web.Response:
    """POST {ids, comment} — оформить партию из выбранных деталей.

    Заявка — обычный индивидуальный заказ (is_custom, цену ставит мастер), но описание в
    ней ТОЛЬКО про выбранные детали. Партии одного проекта связаны НЕ через order_group:
    это поле сворачивает позиции в ОДНУ карточку заказа-корзины (B5), а партии,
    оформленные с разницей в неделю, — разные заказы с разной доставкой и разным счётом.
    Связь партий держат сами детали (`gridfinity_parts.request_id`) и шапка описания
    «Проект «X» · партия 2», которую видит мастер."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if _gf_denied(scope, _toint(request.match_info.get("pid"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"},
                                 status=403)
    u = _verify_user(body.get("initData", "")) if not scope else None
    if not u and not scope:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"gforder:{uid}", 5, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    row = await _gf_owned(_toint(request.match_info.get("pid")), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
    pid = int(row["id"])
    cfg = _gf_cfg_of(row)
    view, parts = await _gf_parts_view(pid, cfg, uid=uid)
    by_id = {int(p["id"]): p for p in parts}
    ids = {_gf_i(x) for x in (body.get("ids") or []) if _gf_i(x)}
    if not ids:
        ids = {v["id"] for v in view if v["sel"]}
    sel = [v for v in view if v["id"] in ids and v["live_state"] in ("draft", "hold")]
    if not sel:
        return web.json_response({"ok": False, "error": "Выберите хотя бы одну деталь"})
    checks = await _gf_preflight(cfg, view, {v["id"] for v in sel})
    blocks = [c["text"] for c in checks if c["level"] == "block"]
    if blocks:
        return web.json_response({"ok": False, "error": blocks[0], "checks": checks})

    mods = {_gf_i(m.get("id")): m for m in _gf_modules(cfg) if _gf_i(m.get("id"))}
    ordered_parts = [by_id[v["id"]] for v in sel]
    comment = str(body.get("comment") or "").strip()[:500]
    title = str(row.get("title") or "")
    try:
        summary = _gf_batch_summary(cfg, ordered_parts, mods, title)
    except Exception:  # noqa: BLE001
        log.warning("gridfinity batch summary failed", exc_info=True)
        summary = "🧩 Gridfinity-органайзер (сводку построить не удалось — откройте снимок заявки)"
    prev = await _gf_batches(pid, view, uid)
    batch_no = len(prev) + 1
    head = f"📦 Проект «{title or 'Органайзер'}» · партия {batch_no}"
    # Условия сделки — В ОПИСАНИИ, а не только в интерфейсе клиента. Цену этой заявки ставит
    # мастер руками (is_custom), и без этой строки он не узнал бы ни про стандарт, ни про
    # обещанную скидку: клиент увидел бы в мастерской одну цифру, а в счёте другую.
    est = await _gf_money(view, {v["id"] for v in sel}, uid)
    std_plastic, _ = await _gf_price_ctx()
    deal = f"⚙️ Стандарт мастерской: {_gf_std_text(std_plastic)}"
    if est["discount_pct"] > 0:
        own = round(est["discount_pct"] - est["std_pct"], 2)
        parts_txt = f"{est['std_pct']:g}% за стандарт" + (f" + {own:g}% личная" if own > 0 else "")
        deal += (f"\n💰 Клиенту названо ≈{est['price']:.2f} со скидкой "
                 f"{est['discount_pct']:g}% ({parts_txt})")
    desc = head + "\n" + deal + "\n" + summary + (f"\n\n💬 {comment}" if comment else "")
    uname = await _gf_uname(uid, u)
    grams = round(sum(v["grams"] for v in sel), 1)
    minutes = round(sum(v["minutes"] for v in sel), 1)
    rid = await db.add_request(
        user_id=uid, user_name=uname,
        model_label=f"Органайзер: {title}" if title else "Gridfinity-органайзер",
        printer="", plastic_name="", quantity=sum(v["qty"] for v in sel), quality="",
        strength="", grams_total=grams, minutes_total=minutes, price=0, preview_path=None,
        is_custom=1, description=desc, source="gridfinity")
    specs = {}
    for v in sel:
        m = mods.get(v["mod_id"])
        if m is not None:
            specs[v["id"]] = _gf_dump(dict(m, colorIdx=v["color_idx"]))
    taken = await db.gf_parts_order(pid, [v["id"] for v in sel], rid, specs)
    if not taken:
        # гонка: детали уже уехали в другую партию (ретрай по таймауту, второй мини-апп).
        # Заявку-пустышку мастеру не отдаём — гасим её сразу.
        await db.update_request(rid, status="cancelled")
        return web.json_response({"ok": False, "error": (
            "Эти детали уже уехали в другую партию — обновите спецификацию")})
    if taken < len(sel):
        log.warning("gridfinity: партия #%s захватила %s из %s деталей", rid, taken, len(sel))
    await db.gridfinity_order_config_add(rid, uid, _gf_dump(
        {"v": 4, "base": cfg.get("base") or {}, "title": title,
         "modules": [mods[v["mod_id"]] for v in sel if v["mod_id"] in mods],
         "parts": [{"id": v["id"], "name": v["name"], "note": v["note"], "qty": v["qty"],
                    "mod_id": v["mod_id"], "color": v["color"], "color_hex": v["color_hex"],
                    "w_mm": v["w_mm"], "d_mm": v["d_mm"], "h_mm": v["h_mm"],
                    "grams": v["grams"], "minutes": v["minutes"]} for v in sel]}))
    await _apply_default_delivery(uid, rid, 0.0)
    await db.log_event(uid, "оформил gridfinity-партию", f"#{rid}: {len(sel)} дет.")
    await _gf_notify_order(uid, uname, rid, summary, comment, head)
    return web.json_response({"ok": True, "order_id": rid, "n": taken})


async def _gf_notify_order(uid: int, uname: str, rid: int, summary: str, comment: str,
                           head: str) -> None:
    """Уведомить мастера о новой партии (и зеркалом — настольную админку)."""
    if _bot is None:
        return
    try:
        # esc ПОСЛЕ обрезки сырого текста: срез экранированной строки мог разрезать
        # HTML-сущность, и Telegram отбивал бы всё уведомление битой разметкой
        cap = (f"🧩 <b>Заявка из Gridfinity-конфигуратора #{rid}</b>\n"
               f"👤 {texts.esc(uname)} (id <code>{uid}</code>)\n\n"
               + texts.esc((head + "\n" + summary
                            + (f"\n\n💬 {comment}" if comment else ""))[:3400]))
        kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=rid,
                                text="Открыть заказ в приложении")
        # notify_admins, а не свой цикл: он зеркалит событие в шину настольной админки
        # (тост у мастера за компом); свой цикл оправдан только там, где альбомы фото
        await notify.notify_admins(_bot, cap, reply_markup=kb, kind="order",
                                   audience=notify.ALL_STAFF)
    except Exception:  # noqa: BLE001
        log.debug("gridfinity notify failed", exc_info=True)


async def _gf_notify_change(uid: int, row: dict, diff: dict, rids: list) -> None:
    """Клиент правит уже заказанное — мастер должен узнать об этом сам, а не из чата.

    Троттлинг на проект: клиент подгоняет лоток десятками мелких правок, и без него
    персонал получил бы серию одинаковых пушей — их перестают читать вместе с настоящими.
    В тексте — номера заявок и кнопка «открыть заказ»: иначе непонятно, что открывать."""
    if _bot is None:
        return
    if not ratelimit.allow(f"gfchg:{_gf_i(row.get('id'))}", 1, 900):
        return
    try:
        title = texts.esc(str(row.get("title") or "органайзер"))
        what = []
        if diff.get("changed"):
            what.append(f"изменил {diff['changed']} дет. в заявке (ещё не в работе)")
        if diff.get("forked"):
            what.append(f"переделал {diff['forked']} дет. после печати — это новая версия")
        nums = sorted({_gf_i(r) for r in rids if _gf_i(r)})
        kb = keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminorder", oid=nums[0],
                                text="Открыть заказ в приложении") if nums else None
        tail = ("\n📄 заявки: " + texts.esc(", ".join(f"#{n}" for n in nums[:6]))) if nums else ""
        await notify.notify_admins(
            _bot, f"✏️ <b>Правка Gridfinity-проекта «{title}»</b>\n"
                  f"👤 id <code>{uid}</code>: " + texts.esc("; ".join(what)) + tail,
            reply_markup=kb, kind="order", audience=notify.ALL_STAFF)
    except Exception:  # noqa: BLE001
        log.debug("gridfinity change notify failed", exc_info=True)


# ─────────────────────────── ручки: мерки и заготовки ───────────────────────────

@routes.post("/api/gridfinity/measures")
async def _api_gf_measure_add(request: web.Request) -> web.Response:
    """POST {title, type, W, D, H, series} или {delete: id} — «мои мерки» мебели."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    if not ratelimit.allow(f"gfmeas:{uid}", 30, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    if body.get("delete"):
        await db.gf_measure_delete(_gf_i(body.get("delete")), uid)
        return web.json_response({"ok": True, "measures": await db.gf_measures(uid)})
    w, d, h = _gf_f(body.get("W")), _gf_f(body.get("D")), _gf_f(body.get("H"))
    if not (50 <= w <= 2000 and 50 <= d <= 2000 and 10 <= h <= 600):
        return web.json_response({"ok": False, "error": "Проверьте замеры: ящик слишком мал или велик"})
    await db.gf_measure_add(uid, str(body.get("title") or "Ящик").strip()[:60],
                            str(body.get("type") or "drawer")[:16], w, d, h,
                            str(body.get("series") or "")[:2])
    return web.json_response({"ok": True, "measures": await db.gf_measures(uid)})


@routes.get("/api/gridfinity/presets")
async def _api_gf_presets(request: web.Request) -> web.Response:
    """GET — свои заготовки модулей: удачная деталь переносится в другой проект."""
    uid, scope = await _gf_actor(request)
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    return web.json_response({"ok": True, "presets": await db.gf_presets(uid)})


@routes.post("/api/gridfinity/presets")
async def _api_gf_preset_add(request: web.Request) -> web.Response:
    """POST {title, mod} или {delete: id} — сохранить/удалить заготовку."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    if not ratelimit.allow(f"gfpreset:{uid}", 30, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    if body.get("delete"):
        await db.gf_preset_delete(_gf_i(body.get("delete")), uid)
        return web.json_response({"ok": True, "presets": await db.gf_presets(uid)})
    mod = body.get("mod")
    if not isinstance(mod, dict):
        return web.json_response({"ok": False, "error": "Нечего сохранять"})
    raw = _gf_dump(mod)
    if len(raw) > 32 * 1024:
        return web.json_response({"ok": False, "error": "Заготовка слишком большая"})
    if len(await db.gf_presets(uid)) >= 40:
        return web.json_response({"ok": False, "error": "Больше 40 заготовок не держим"})
    await db.gf_preset_add(uid, str(body.get("title") or "Заготовка").strip()[:60], raw)
    return web.json_response({"ok": True, "presets": await db.gf_presets(uid)})


# ─────────────────────────── старый контур (совместимость) ───────────────────────────

@routes.get("/api/gridfinity/config")
async def _api_gridfinity_config(request: web.Request) -> web.Response:
    """GET — черновик конфигурации юзера ({config: null}, если ещё не сохранял).

    Ручка живёт ради старого фронта: мини-апп у клиентов обновляется не мгновенно, и до
    перезапуска бота новых ручек он не знает. Черновик — зеркало последнего проекта."""
    uid, scope = await _gf_actor(request)
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    raw = await db.gridfinity_config_get(uid)
    cfg = None
    if raw:
        try:
            cfg = json.loads(raw)
        except Exception:  # noqa: BLE001
            log.warning("gridfinity config unreadable uid=%s", uid)
    return web.json_response({"ok": True, "config": cfg})


@routes.post("/api/gridfinity/config")
async def _api_gridfinity_config_set(request: web.Request) -> web.Response:
    """POST {initData, config} — сохранить черновик (старый фронт, без проектов)."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    if not ratelimit.allow(f"gfsave:{uid}", 60, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    cfg = body.get("config")
    if not _gf_valid(cfg):
        return web.json_response({"ok": False, "error": "Непонятная конфигурация"})
    raw = _gf_dump(cfg)
    if _gf_oversize(raw):
        return web.json_response({"ok": False, "error": "Конфигурация слишком большая"})
    await db.gridfinity_config_set(uid, raw)
    return web.json_response({"ok": True})


@routes.post("/api/gridfinity/order")
async def _api_gridfinity_order(request: web.Request) -> web.Response:
    """POST {initData, config, comment?} — заявка мастеру ЦЕЛИКОМ (старый фронт).

    Новый клиент заказывает партиями (`/api/gridfinity/project/{pid}/order`); эта ручка
    остаётся для тех, у кого мини-апп ещё не обновился."""
    got, err = await _gf_body(request)
    if err is not None:
        return err
    body, uid, scope = got
    if scope:
        return web.json_response({"ok": False, "error": (
            "Ссылка «на компьютере» открывает только один проект — остальное из Telegram")}, status=403)
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"gforder:{uid}", 5, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    cfg = body.get("config")
    mods = _gf_modules(cfg) if _gf_valid(cfg) else []
    if not mods:   # список мусора вида [0] — не модули, пустых заявок мастеру не шлём
        return web.json_response({"ok": False, "error": "Добавьте хотя бы один модуль"})
    raw = _gf_dump(cfg)
    if _gf_oversize(raw):
        return web.json_response({"ok": False, "error": "Конфигурация слишком большая"})
    comment = str(body.get("comment") or "").strip()[:500]
    try:
        summary = _gf_summary(cfg)
    except Exception:  # noqa: BLE001
        log.warning("gridfinity summary failed", exc_info=True)
        summary = "🧩 Gridfinity-органайзер (сводку построить не удалось — откройте снимок заявки)"
    desc = summary + (f"\n\n💬 {comment}" if comment else "")
    uname = ("@" + u["username"]) if u.get("username") else \
        (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {uid}")
    rid = await db.add_request(
        user_id=uid, user_name=uname, model_label="Gridfinity-органайзер", printer="",
        plastic_name="", quantity=1, quality="", strength="", grams_total=0, minutes_total=0,
        price=0, preview_path=None, is_custom=1, description=desc, source="gridfinity")
    await db.gridfinity_order_config_add(rid, uid, raw)
    await db.gridfinity_config_set(uid, raw)   # черновик = последняя заказанная версия
    await _apply_default_delivery(uid, rid, 0.0)
    await db.log_event(uid, "оформил gridfinity-заявку", f"#{rid}: {len(mods)} мод.")
    await _gf_notify_order(uid, uname, rid, summary, comment, "")
    return web.json_response({"ok": True, "order_id": rid})


# ─────────────────────────── ссылка «показать» ───────────────────────────

def _gf_share_html(title: str, cfg: dict, parts: list[dict]) -> str:
    """Страница проекта для чтения без авторизации: план ящика и список деталей.

    Отдельная страница, а не мини-апп: ссылку открывают в обычном браузере на компьютере
    («покажи жене», «покажи мастеру»), где Telegram WebApp просто не запустится."""
    g = _gf_grid(cfg.get("base") or {})
    base = cfg.get("base") or {}
    cells = []
    for m in _gf_modules(cfg):
        c = _GF_COLOR_HEX[_gf_i(m.get("colorIdx")) % len(_GF_COLOR_HEX)]
        if m.get("kind") == "wing":
            continue
        cells.append(
            f'<i style="grid-column:{_gf_i(m.get("cx")) + 1}/span {max(1, _gf_i(m.get("w")))};'
            f'grid-row:{_gf_i(m.get("cy")) + 1}/span {max(1, _gf_i(m.get("d")))};background:{c}"></i>')
    rows = "".join(
        f'<tr><td><b style="background:{html.escape(p["color_hex"])}"></b>'
        f'{html.escape(p["name"])}</td><td>{p["w_mm"]:.0f}×{p["d_mm"]:.0f}×{p["h_mm"]:.0f} мм</td>'
        f'<td>×{p["qty"]}</td><td>{html.escape(p["state_label"])}</td></tr>' for p in parts)
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title or "Органайзер")}</title><style>
:root{{color-scheme:light dark}}
body{{margin:0;padding:24px;font:16px/1.5 system-ui,sans-serif;background:#f6f7f7;color:#12181a}}
main{{max-width:760px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}}
.sub{{color:#67787c;font-size:14px;margin-bottom:18px}}
.plan{{display:grid;gap:3px;background:#e3e8e7;border-radius:8px;padding:4px;margin-bottom:20px;
grid-template-columns:repeat({g["cols"]},1fr);grid-template-rows:repeat({g["rows"]},1fr);
aspect-ratio:{max(1, g["cols"])}/{max(1, g["rows"])}}}
.plan i{{border-radius:4px;display:block}}
table{{width:100%;border-collapse:collapse;font-size:14px;background:#fff;border-radius:8px;overflow:hidden}}
td{{padding:8px 10px;border-bottom:1px solid #eceeee}}
td b{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:8px}}
@media(prefers-color-scheme:dark){{body{{background:#0e1315;color:#e7edec}}
.plan{{background:#1b2427}}table{{background:#161d20}}td{{border-color:#243033}}.sub{{color:#8a9b9d}}}}
</style></head><body><main>
<h1>{html.escape(title or "Органайзер")}</h1>
<div class="sub">{_GF_TYPE.get(str(base.get("type") or "drawer"), "шуфлядка")} ·
внутри {_gf_i(base.get("W"))}×{_gf_i(base.get("D"))} мм · сетка {g["cols"]}×{g["rows"]} ·
деталей {len(parts)}</div>
<div class="plan">{"".join(cells)}</div>
<table>{rows}</table>
</main></body></html>"""


@routes.get("/gfx/{token}")
async def _gf_share_page(request: web.Request) -> web.Response:
    """GET /gfx/{token} — проект только на чтение по ссылке (без авторизации)."""
    token = str(request.match_info.get("token") or "")
    # Ссылку клиент отправляет в открытый чат, где её дёргают превьюшники мессенджеров и
    # краулеры: без потолка это бесплатный способ гонять сборку страницы в цикле.
    if not token or not ratelimit.allow(f"gfshare:{token}", 30, 60):
        return web.Response(text="Слишком часто — попробуйте через минуту", status=429,
                            content_type="text/html", charset="utf-8")
    share = await db.gf_share_get(token)
    row = await db.gf_project(int(share["project_id"])) if share else None
    if row and _gf_i(row.get("archived_at")):
        row = None      # архив = «удалён» с точки зрения клиента: ссылка обязана погаснуть
    if not row:
        return web.Response(text="Ссылка не действует", status=404,
                            content_type="text/html", charset="utf-8")
    cfg = _gf_cfg_of(row)
    # без uid: страница по ссылке открыта БЕЗ авторизации, и личная скидка владельца
    # проекта в ней светиться не должна — остаётся только стандартная
    view, _ = await _gf_parts_view(int(row["id"]), cfg)
    return web.Response(text=_gf_share_html(str(row.get("title") or ""), cfg, view),
                        content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})



# ─────────────────── сессия «продолжить на компьютере» ───────────────────

def _gf_desk_url(token: str) -> str:
    base = str(getattr(config, "WEBAPP_PUBLIC_URL", "") or "").rstrip("/")
    return f"{base}/gfx/desk/{token}"


async def _gf_desk_info(pid: int) -> dict:
    """Что показать в мастерской про сессию ПК. Пусто — ссылки нет или срок вышел."""
    ses = await db.gf_desk_of(pid)
    if not ses:
        return {}
    return {"url": _gf_desk_url(str(ses["token"])),
            "expires_at": int(ses.get("expires_at") or 0),
            "used_at": int(ses.get("used_at") or 0)}


async def _gf_desk_notify(uid: int, title: str, token: str, exp: int) -> None:
    """Ссылка уезжает В ЛИЧНЫЙ ЧАТ клиента, а не просто рисуется на экране телефона.

    Так она сразу оказывается на всех его устройствах: Telegram Desktop на компьютере
    открывает её одним кликом, и переписывать длинный адрес руками не нужно. Заодно у
    ссылки появляется место, где её всегда можно найти заново, — история чата.

    Про риск говорим прямо: ссылка И ЕСТЬ ключ, отдать её кому-то — отдать правку."""
    if _bot is None:
        return
    try:
        days = max(1, int((int(exp) - time.time()) // 86400))
        text = (
            "🖥 <b>Органайзер на компьютере</b>\n"
            f"Проект «{texts.esc(title or 'органайзер')}» — откройте эту ссылку "
            "в браузере на ПК:\n"
            f"{texts.esc(_gf_desk_url(token))}\n\n"
            f"Ссылка живёт {days} дн. и открывает правку ТОЛЬКО этого проекта. "
            "Кому перешлёте — тот сможет менять раскладку и оформлять заявки, поэтому "
            "не публикуйте её. Погасить — там же, где выдали.")
        await notify.notify_user(_bot, int(uid), text)
    except Exception:  # noqa: BLE001
        log.debug("gridfinity desk link notify failed", exc_info=True)


def _gf_desk_html(token: str, pid: int, title: str) -> str:
    """Страница сессии ПК: тот же экран проекта, что в мини-аппе, и больше ничего.

    Отдельная страница, а НЕ `/app/` с ключом в адресе, — сознательно. В мини-аппе живут
    корзина, заказы, профиль и админка; их ручки этот ключ не принимают и правильно делают,
    но клиент получил бы экран, наполовину состоящий из отказов. Здесь грузится ровно то,
    что нужно мастерской: React, слой данных, загрузчик движка и экраны проекта.

    Ключ кладём в `window.__GFX_KEY__` ДО api.js: тот решает по нему две вещи — не включать
    мок-домен (без initData он считает, что нас открыли для предпросмотра) и подписывать
    ключом запросы, но ТОЛЬКО к `/api/gridfinity/`."""
    v = config.webui_version()
    esc = html.escape
    q = '"'
    return (
        "<!doctype html><html lang=" + q + "ru" + q + "><head><meta charset=" + q + "utf-8" + q + ">"
        "<meta name=" + q + "viewport" + q + " content=" + q
        + "width=device-width,initial-scale=1" + q + ">"
        f"<title>{esc(title or 'Органайзер')} — на компьютере</title>"
        "<link rel=" + q + "icon" + q + " href=" + q + "/app/favicon.svg" + q + ">"
        "<link rel=" + q + "stylesheet" + q + " href=" + q + f"/app/vendor/fonts/fonts.css?v={v}" + q + ">"
        "<link rel=" + q + "stylesheet" + q + " href=" + q + f"/app/app.css?v={v}" + q + ">"
        # На ПК экран отдаётся студии ЦЕЛИКОМ: колонка по центру с полями по бокам —
        # привычка мини-аппа, где ширина телефонная. Здесь чем шире сцена, тем лучше.
        # Селекторы через `body`, потому что таблицу движка внедряют ПОЗЖЕ этой:
        # при равной силе победила бы она, и высота осталась бы телефонной.
        "<style>html,body,#app-root{margin:0;height:100%}"
        "body{background:#0e1217;color:#e8edf2}"
        "body .screen{max-width:none;margin:0;padding:10px 12px;box-sizing:border-box}"
        # 78px = верхнее поле 10 + ряд вкладок 46 + его отступ 12 + нижнее поле 10
        "body .gfx{height:calc(100vh - 78px);min-height:0}</style>"
        f"<script>window.__GFX_KEY__={_gf_dump(token)};window.__GFX_PID__={int(pid)};"
        f"window.__APPVER__={_gf_dump(v)};</script>"
        + _gf_script(f"/app/vendor/react.production.min.js?v={v}")
        + _gf_script(f"/app/vendor/react-dom.production.min.js?v={v}")
        + _gf_script(f"/app/api.js?v={v}")
        + _gf_script(f"/app/screens_gridfinity.js?v={v}")
        + _gf_script(f"/app/screens_workshop.js?v={v}")
        + "</head><body><div id=" + q + "app-root" + q + "></div>"
        + _gf_script(f"/app/gfxdesk.js?v={v}")
        + "</body></html>")


def _gf_script(src: str) -> str:
    q = '"'
    return "<script src=" + q + src + q + "></script>"


@routes.get("/gfx/desk/{token}")
async def _gf_desk_page(request: web.Request) -> web.Response:
    """GET /gfx/desk/{token} — экран проекта в обычном браузере, без Telegram.

    Потолок частоты тот же, что у страницы «показать»: адрес уходит в чат, а по чатам
    ходят превьюшники мессенджеров и краулеры."""
    token = str(request.match_info.get("token") or "")
    if not token or not ratelimit.allow(f"gfdesk:{token}", 30, 60):
        return web.Response(text="Слишком часто — попробуйте через минуту", status=429,
                            content_type="text/html", charset="utf-8")
    ses = await db.gf_desk_get(token)
    row = await db.gf_project(int(ses["project_id"])) if ses else None
    if row and _gf_i(row.get("archived_at")):
        row = None      # архив = «удалён» для клиента: ссылка гаснет вместе с проектом
    if not row:
        return web.Response(text="Ссылка не действует — выпустите новую в мастерской",
                            status=404, content_type="text/html", charset="utf-8")
    await db.gf_desk_touch(token)
    return web.Response(text=_gf_desk_html(token, int(row["id"]), str(row.get("title") or "")),
                        content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})

# ─────────────────────────── печатный шаблон дна ───────────────────────────

@routes.get("/api/gridfinity/project/{pid}/template")
async def _api_gf_template(request: web.Request) -> web.Response:
    """GET — PDF-шаблон дна 1:1: распечатать дома, положить в ящик, проверить замеры.

    Самая дешёвая проверка во всей мастерской: лист бумаги вместо заказа на пять тысяч."""
    uid, scope = await _gf_actor(request)
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if _gf_denied(scope, _toint(request.match_info.get("pid"))):
        return web.json_response({"ok": False, "error": "Это не ваш проект"},
                                 status=403)
    row = await _gf_owned(_toint(request.match_info.get("pid")), uid)
    if not row:
        return web.json_response({"ok": False, "error": "Проект не найден"}, status=404)
    if not ratelimit.allow(f"gftpl:{uid}", 5, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    cfg = _gf_cfg_of(row)
    base = cfg.get("base") or {}
    try:
        from .. import gfxpdf
        # Сборка PDF — единственная тяжёлая CPU-работа модуля, и она СИНХРОННАЯ: в потоке
        # событий она подвесила бы бота целиком (ни заявок, ни чата, ни админки).
        loop = asyncio.get_running_loop()
        pdf = await loop.run_in_executor(
            None, gfxpdf.template_pdf, str(row.get("title") or "Органайзер"), base,
            _gf_grid(base))
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)})
    except Exception:  # noqa: BLE001 — reportlab не обязан быть на всякой машине
        log.warning("gridfinity template failed", exc_info=True)
        return web.json_response({"ok": False, "error": "Шаблон собрать не удалось"})
    return web.Response(body=pdf, content_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="gridfinity.pdf"'})


# ─────────────────────────── админские ручки ───────────────────────────

@routes.get("/api/admin/gridfinity/{rid}")
async def _api_admin_gridfinity_config(request: web.Request) -> web.Response:
    """GET /api/admin/gridfinity/{rid} — снимок заявки для мастера: раскладка + печать-лист.

    Сводка в описании режется по строкам, а здесь раскладка целиком и детали партии с
    габаритами, цветом, расходом и отметкой «напечатано»."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    rid = _toint(request.match_info.get("rid"))
    raw = await db.gridfinity_order_config_get(rid) if rid else None
    if not raw:
        return web.json_response({"ok": False, "error": "Снимка нет"}, status=404)
    try:
        cfg = json.loads(raw)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "Снимок не читается"})
    try:
        summary = _gf_summary(cfg)
    except Exception:  # noqa: BLE001
        summary = ""
    parts = await db.gf_parts_by_request(rid)
    sheet, pcfg, owner = [], cfg, 0
    if parts:
        row = await db.gf_project(int(parts[0]["project_id"]))
        pcfg = _gf_cfg_of(row) if row else cfg
        # скидка считается по ВЛАДЕЛЬЦУ проекта, а не по мастеру: цифра в печать-листе
        # должна совпадать с той, что клиенту назвали в мастерской
        owner = _gf_i(row.get("user_id")) if row else 0
        view, _ = await _gf_parts_view(int(parts[0]["project_id"]), pcfg, uid=owner)
        by_id = {v["id"]: v for v in view}
        sheet = [by_id[int(p["id"])] for p in parts if int(p["id"]) in by_id]
    # План рисуем по раскладке ПРОЕКТА, а не по снимку партии: мастеру нужен контекст —
    # что уже стоит рядом и какая у соседей посадка. Детали этой партии подсвечены.
    return web.json_response({"ok": True, "config": cfg, "summary": summary,
                              "preview": _gf_preview(pcfg, {v["mod_id"] for v in sheet}),
                              "parts": sheet,
                              "money": await _gf_money(sheet, {v["id"] for v in sheet}, owner),
                              "title": str(cfg.get("title") or "")})


@routes.post("/api/admin/gridfinity/part/{id}")
async def _api_admin_gf_part_printed(request: web.Request) -> web.Response:
    """POST {printed} — мастер отметил деталь готовой; клиенту летит уведомление.

    Единственное новое действие мастера во всей мастерской: остальное (статус заявки,
    цена, комментарий) он делает как обычно."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    if not _verify_admin(body.get("initData", "")):
        return _admin_forbidden()
    part = await db.gf_part(_toint(request.match_info.get("id")))
    if not part:
        return web.json_response({"ok": False, "error": "Деталь не найдена"}, status=404)
    printed = bool(body.get("printed", True))
    was = str(part.get("state") or "")
    if was not in ("ordered", "printed"):
        return web.json_response({"ok": False, "error": (
            "Деталь не в заявке — отмечать нечего")})
    if (was == "printed") == printed:
        return web.json_response({"ok": True, "same": 1})   # повтор клика: без второго пинга
    await db.gf_part_set_printed(int(part["id"]), printed)
    row = await db.gf_project(int(part["project_id"]))
    if printed and _bot is not None and row:
        try:
            rid = _gf_i(part.get("request_id"))
            mates = await db.gf_parts_by_request(rid) if rid else []
            done = sum(1 for p in mates if str(p.get("state")) == "printed")
            name = str(part.get("name") or "").strip() or "Деталь"
            await notify.notify_user(
                _bot, int(row["user_id"]),
                f"✅ <b>{texts.esc(name)}</b> готова!\n"
                f"Проект «{texts.esc(str(row.get('title') or 'органайзер'))}» · "
                f"{done} из {len(mates)} в заявке #{rid}")
        except Exception:  # noqa: BLE001
            log.debug("gridfinity printed notify failed", exc_info=True)
    return web.json_response({"ok": True})


@routes.post("/api/admin/gridfinity/release/{rid}")
async def _api_admin_gf_release(request: web.Request) -> web.Response:
    """POST — заявка закрыта отказом/отменой: вернуть её детали в черновик.

    Без этого деталь навсегда осталась бы «в заявке», и клиент не смог бы заказать её
    заново — единственный способ был бы удалить модуль и нарисовать заново."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    if not _verify_admin(body.get("initData", "")):
        return _admin_forbidden()
    rid = _toint(request.match_info.get("rid"))
    n = await db.gf_parts_release_request(rid) if rid else 0
    return web.json_response({"ok": True, "released": n})
