"""Параметрические модели на OpenSCAD (OpenTodo c1cc840a).

Бандлим .scad-файлы с Customizer-параметрами (как у MakerWorld parametricModelMaker). Пользователь
задаёт значения (текст/размеры/шрифт) → рендерим в STL через OpenSCAD CLI (`openscad -o out.stl
-D name=value …`) → дальше модель идёт по ОБЫЧНОМУ конвейеру слайса/цены/заказа как загруженная.

Безопасность: значения НЕ подставляются в .scad как код. Числа валидируются через float и
зажимаются в [min:max]; строки/enum экранируются и оборачиваются в кавычки → OpenSCAD видит их
строковым литералом, а не выражением (нет инъекции в `-D`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

from . import config

log = logging.getLogger(__name__)


class ParametricError(Exception):
    pass


# Превью в мини-аппе считает геометрию в px (screens_custom.js PXMM). Все «мм»-параметры фронта
# (widthMm/thickMm) уже в миллиметрах и идут в .scad как есть; единственная px-величина — fontSize,
# поэтому её делим на этот коэффициент, переводя в мм размера шрифта/высоты таблички (как в превью).
PREVIEW_PX_PER_MM = 2.6

# Шрифт фронта (семейство) → реальный шрифт OpenSCAD. Берём СЕМЕЙСТВО Liberation: оно идёт в
# комплекте с OpenSCAD (есть всегда, не зависит от системных шрифтов) И содержит кириллицу
# (текст по умолчанию русский). Google-шрифты (Noto/Roboto) на Windows не установлены → текст
# падал в «тофу». Sans/Serif/Mono метрически совместимы с Arial/Times/Courier.
_FONT_MAP = {"round": "Liberation Sans", "classic": "Liberation Serif", "mono": "Liberation Mono"}
# Форма фронта → числовой код Shape в .scad (число безопасно для -D, без инъекции).
_SHAPE_MAP = {"rect": 0, "rounded": 1, "pill": 2}


def _keychain_to_scad(c: dict) -> dict:
    """Очищенные параметры кастомайзера (схема фронта) → значения Customizer-параметров .scad.
    Толщина/ширина уже в мм; размер шрифта и высоту таблички выводим из fontSize (px→мм), как
    делает живое превью. Цвет — косметика (в STL цвета нет), геометрию не трогает и сюда не идёт."""
    font_size_px = float(c["fontSize"])
    text_size_mm = round(font_size_px / PREVIEW_PX_PER_MM, 2)
    height_mm = round((font_size_px * 1.7 + 16) / PREVIEW_PX_PER_MM)   # высота таблички (.scad Width)
    height_mm = int(max(1, min(200, height_mm)))
    return {
        "Text": c["text"],
        "Length": c["widthMm"],          # авто-длина под текст (считает фронт), уже в мм
        "Width": height_mm,
        "Text_size": text_size_mm,
        "Thickness": c["thickMm"],
        "Shape": _SHAPE_MAP.get(c["shape"], 1),
        "Ring": 1 if c["ring"] else 0,
        "Border": 1 if c["border"] else 0,
        "Euro": 1 if c.get("euro", True) else 0,   # синяя полоса слева — только у «номера авто»
        "Font": _FONT_MAP.get(c["font"], "Liberation Sans"),
    }


# ── Реестр встроенных моделей ─────────────────────────────────────────────────────────────────
# show — какие параметры показываем пользователю в СТАРОЙ форме (остальные берутся по умолчанию
#   из .scad). customizer — схема параметров НОВОГО кастомайзера (Коллекция → «Создай своё»):
#   санитизируем по ней значения фронта и через to_scad маппим на Customizer-параметры .scad.
# overrides — наши значения по умолчанию поверх .scad (напр. кириллический шрифт).
MODELS = [
    {
        "id": "keychain_plate",
        "scad": "car_plate_keychain.scad",
        "name": "Брелок номер авто",
        "desc": "Номерной знак с вашим текстом, синей полосой слева и отверстием под кольцо",
        "label_param": "Text",            # из чего собирать имя модели для заказа
        "show": ["Text", "Length", "Width", "Text_size"],
        "labels": {"Text": "Текст надписи", "Length": "Длина, мм",
                   "Width": "Ширина, мм", "Text_size": "Размер шрифта"},
        "overrides": {"Font": "Liberation Sans"},   # кириллица + всегда в комплекте OpenSCAD
        # Схема кастомайзера C2 (screens_custom.js DEF): ключ фронта → тип + клампы/опции + дефолт.
        # widthMm — авто-длина под текст (считает фронт), широкий кламп под длинный текст; рамка ВКЛ.
        "customizer": {
            "text":     {"type": "string", "default": "А123ВС"},
            "fontSize": {"type": "number", "min": 18, "max": 46, "default": 30},
            "widthMm":  {"type": "number", "min": 30, "max": 230, "default": 80},
            "thickMm":  {"type": "number", "min": 2, "max": 6, "default": 3},
            "font":     {"type": "enum", "options": ["round", "classic", "mono"], "default": "round"},
            "shape":    {"type": "enum", "options": ["rect", "rounded", "pill"], "default": "rounded"},
            "color":    {"type": "string", "default": "#e8edf2"},   # косметика, в геометрию не идёт
            "ring":     {"type": "bool", "default": True},
            "border":   {"type": "bool", "default": True},
            "euro":     {"type": "bool", "default": True},   # синяя полоса слева (номер авто); у бирок выкл
        },
        "to_scad": _keychain_to_scad,
        "autosize": True,                 # Length подгоняется под реальную ширину текста (autosize_keychain)
        "euro": True,                     # фронт: показывать слот «Полоса слева» (евробанд); у бирок euro:false в p
        "hidden": True,                   # в старой форме не показываем — табличка живёт в кастомайзере
    },
    {
        "id": "keychain_qr",
        "name": "QR-брелок",
        "desc": "QR-код на вашу ссылку, телеграм или контакт",
        "qr": True,                       # геометрия генерится в Python (qrcode+trimesh), не .scad
        "label_param": "",                # длинный URL в название модели не тащим
        "params": [
            {"name": "Text", "type": "string", "default": "https://t.me/", "label": "Ссылка или текст",
             "min": None, "max": None, "options": None},
            {"name": "Size", "type": "number", "default": 40, "min": 26, "max": 60,
             "label": "Размер, мм", "options": None},
        ],
    },
]

MAX_TEXT_LEN = 40


def openscad_available() -> bool:
    return config.OPENSCAD_PATH.exists()


def get_model(mid: str) -> dict | None:
    return next((m for m in MODELS if m["id"] == mid), None)


def _scad_path(model: dict):
    return config.PARAMETRIC_DIR / model["scad"]


# ── Разбор Customizer-параметров: `Name = default; // подпись [подсказки]` ─────────────────────
_PARAM_LINE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*;\s*(?://+\s*(.*))?$")
_RANGE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*:\s*(-?\d+(?:\.\d+)?)\s*\]")
_OPTIONS = re.compile(r"\[([^\]]+)\]")


def parse_customizer(scad_text: str) -> list[dict]:
    """Параметры верхнего уровня .scad: только литералы (число/строка); выражения пропускаем."""
    out: list[dict] = []
    for line in scad_text.splitlines():
        m = _PARAM_LINE.match(line)
        if not m:
            continue
        name, raw, comment = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        if name.startswith("$"):
            continue
        ptype = vmin = vmax = options = None
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            ptype, default = "string", raw[1:-1]
        else:
            try:
                default = float(raw)
                default = int(default) if default == int(default) else default
                ptype = "number"
            except ValueError:
                continue                     # не литерал (str(...), выражение) — это не Customizer-параметр
        label = comment
        rng = _RANGE.search(comment)
        if rng and ptype == "number":
            vmin, vmax = float(rng.group(1)), float(rng.group(2))
            label = comment[:rng.start()].strip()
        elif ptype == "string":
            om = _OPTIONS.search(comment)
            if om and "," in om.group(1):
                options = [o.split(":")[0].strip() for o in om.group(1).split(",") if o.strip()]
                ptype = "enum"
                label = comment[:om.start()].strip()
        out.append({"name": name, "type": ptype, "default": default,
                    "label": label or name, "min": vmin, "max": vmax, "options": options})
    return out


def schema_for(model: dict) -> dict:
    """{имя: параметр} из .scad с наложенными overrides модели."""
    schema = {p["name"]: p for p in parse_customizer(_scad_path(model).read_text(encoding="utf-8"))}
    for name, val in (model.get("overrides") or {}).items():
        if name in schema:
            schema[name]["default"] = val
    return schema


def public_params(model: dict) -> list[dict]:
    """Параметры для формы пользователя — только из model['show'], в этом порядке, с нашими
    русскими подписями (labels) поверх английских из .scad."""
    if model.get("params"):
        return [dict(p) for p in model["params"]]   # не-.scad модель (QR) задаёт параметры явно
    schema = schema_for(model)
    labels = model.get("labels") or {}
    out = []
    for n in model.get("show", []):
        if n not in schema:
            continue
        p = dict(schema[n])
        if n in labels:
            p["label"] = labels[n]
        out.append(p)
    return out


def validate_values(model: dict, raw: dict) -> dict:
    """Санитизация значений по схеме: числа в [min:max], строки обрезаются, enum — только из списка.
    Неизвестные/неразрешённые ключи отбрасываем. Возвращает только параметры из model['show']."""
    schema = schema_for(model)
    allowed = set(model.get("show", []))
    clean: dict = {}
    for name in allowed:
        p = schema.get(name)
        if not p:
            continue
        if name not in raw or raw[name] in (None, ""):
            clean[name] = p["default"]
            continue
        v = raw[name]
        if p["type"] == "number":
            try:
                num = float(v)
            except (TypeError, ValueError):
                num = float(p["default"])
            if p["min"] is not None:
                num = max(p["min"], num)
            if p["max"] is not None:
                num = min(p["max"], num)
            clean[name] = int(num) if num == int(num) else round(num, 3)
        elif p["type"] == "enum":
            s = str(v)
            clean[name] = s if s in (p["options"] or []) else p["default"]
        else:
            clean[name] = str(v)[:MAX_TEXT_LEN]
    return clean


def sanitize_customizer(model: dict, raw: dict) -> dict:
    """Очистка значений по схеме кастомайзера (model['customizer']): числа в [min:max], строки
    обрезаются (анти-инъекция позже при -D, в _fmt_value), enum — только из options, bool —
    приводится к True/False. Отсутствующие/пустые ключи берут default из схемы."""
    spec = model.get("customizer") or {}
    clean: dict = {}
    for name, p in spec.items():
        v = raw.get(name, None)
        if v is None or v == "":
            clean[name] = p.get("default")
            continue
        t = p["type"]
        if t == "number":
            try:
                num = float(v)
            except (TypeError, ValueError):
                num = float(p.get("default") or 0)
            if p.get("min") is not None:
                num = max(p["min"], num)
            if p.get("max") is not None:
                num = min(p["max"], num)
            clean[name] = int(num) if num == int(num) else round(num, 3)
        elif t == "enum":
            s = str(v)
            opts = p.get("options") or []
            clean[name] = s if s in opts else p.get("default", opts[0] if opts else "")
        elif t == "bool":
            clean[name] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        else:  # string — длину режем тут, экранирование/кавычки делает _fmt_value на -D
            clean[name] = str(v)[:MAX_TEXT_LEN]
    return clean


_CUSTOMIZER_KEYS = {"text", "fontSize", "widthMm", "thickMm", "font", "shape", "color", "ring", "border"}


def is_customizer_payload(raw: dict) -> bool:
    """Похоже ли тело запроса на схему кастомайзера (ключи фронта), а не на «сырые» имена .scad."""
    return any(k in (raw or {}) for k in _CUSTOMIZER_KEYS)


def prepare_values(model: dict, raw: dict) -> dict:
    """Привести входные параметры к значениям Customizer-параметров .scad для render().
    Кастомайзер (Коллекция → «Создай своё») шлёт схему фронта (text/widthMm/…) → санитизируем и
    маппим через model['to_scad']. Старая форма шлёт «сырые» имена .scad → validate_values как есть."""
    raw = raw or {}
    if model.get("qr"):
        return _qr_sanitize(model, raw)          # QR-модель не .scad — свои значения генератора
    if model.get("customizer") and model.get("to_scad") and is_customizer_payload(raw):
        return model["to_scad"](sanitize_customizer(model, raw))
    return validate_values(model, raw)


def label_for(model: dict, values: dict) -> str:
    txt = str(values.get(model.get("label_param", ""), "")).strip()
    base = model["name"]
    return f"{base} «{txt}»" if txt else base


def _fmt_value(p: dict, value) -> str:
    """Значение → выражение OpenSCAD для -D. Строки/enum в кавычках (литерал, не код).
    Оставлено для совместимости/юнит-теста; render() теперь передаёт параметры JSON-набором."""
    if p["type"] == "number":
        try:
            return repr(float(value))
        except (TypeError, ValueError):
            return repr(float(p["default"]))
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _pset_value(p: dict, value) -> str:
    """Значение параметра для JSON-набора OpenSCAD (-p/-P): всегда строка, OpenSCAD приводит её к
    типу параметра. Числа нормализуем; строки/enum идут как есть — это ДАННЫЕ (литерал), а не
    выражение, поэтому инъекция кода невозможна (json.dumps экранирует, OpenSCAD не eval-ит)."""
    if p["type"] == "number":
        try:
            f = float(value)
            return str(int(f) if f == int(f) else f)
        except (TypeError, ValueError):
            return str(p["default"])
    return str(value)


# Версия способа рендера — в сигнатуре кэша STL. Бамп инвалидирует ВЕСЬ старый кэш, когда меняется
# сама генерация (v2: параметры через UTF-8 JSON-набор + шрифты Liberation — старые STL с -D могли
# содержать «тофу» вместо кириллицы).
_RENDER_VERSION = "2"


def _signature(model: dict, values: dict) -> str:
    if model.get("qr"):
        mt = 0   # QR-модель не из .scad-файла — версии генератора достаточно
    else:
        mt = _scad_path(model).stat().st_mtime_ns
    key = f"{model['id']}|{_RENDER_VERSION}|{json.dumps(values, sort_keys=True, ensure_ascii=False)}|{mt}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


_render_locks: dict[str, asyncio.Lock] = {}


def _lock_for(sig: str) -> asyncio.Lock:
    """Лок на сигнатуру рендера. Кастомайзер шлёт превью и расчёт почти одновременно — без лока
    два одинаковых запроса оба увидят отсутствие STL и оба запустят OpenSCAD в ОДИН файл, а
    читатель (trimesh/inspect) может поймать наполовину записанный STL. Лок сериализует только
    одинаковые сигнатуры; разные параметры по-прежнему рендерятся параллельно."""
    lk = _render_locks.get(sig)
    if lk is None:
        lk = _render_locks.setdefault(sig, asyncio.Lock())
    return lk


def _measure_text_width(stl) -> float:
    """X-габарит STL. Синхронный trimesh.load — вызывать через asyncio.to_thread, не на event loop."""
    import trimesh
    tm = trimesh.load(str(stl), force="mesh")
    return float(tm.bounds[1][0] - tm.bounds[0][0]) if len(tm.faces) else 0.0


async def render(model: dict, values: dict):
    """Отрендерить модель в STL (кэш по модели+параметрам). Возвращает Path к STL."""
    if model.get("qr"):
        return await _render_qr(model, values)   # QR-брелок генерится в Python, не через OpenSCAD
    if not openscad_available():
        raise ParametricError("OpenSCAD не установлен — параметрические модели недоступны")
    sp = _scad_path(model)
    if not sp.exists():
        raise ParametricError("Файл модели не найден")
    config.PARAM_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    sig = _signature(model, values)
    out = config.PARAM_RENDER_DIR / f"{model['id']}_{sig}.stl"
    if out.exists() and out.stat().st_size > 0:
        return out
    async with _lock_for(sig):
        # пока ждали лок, параллельный запрос мог уже отрисовать этот же STL
        if out.exists() and out.stat().st_size > 0:
            return out
        schema = schema_for(model)
        # сначала overrides модели (дефолты), затем значения пользователя
        merged = {**{n: schema[n]["default"] for n in (model.get("overrides") or {}) if n in schema}, **values}
        # Параметры передаём JSON-НАБОРОМ (-p file.json -P set), а НЕ -D на командной строке: на Windows
        # не-ASCII в -D искажается (кириллический текст → «тофу»-квадраты), а UTF-8 JSON OpenSCAD читает
        # корректно. Значения — строки (литералы), инъекция кода невозможна (param-set не eval-ит).
        pset = {name: _pset_value(schema[name], val) for name, val in merged.items() if name in schema}
        jf = out.with_suffix(".json")
        jf.write_text(json.dumps({"parameterSets": {"pcb": pset}, "fileFormatVersion": "1"},
                                 ensure_ascii=False), encoding="utf-8")
        try:
            args = [str(config.OPENSCAD_PATH), "-o", str(out), "-p", str(jf), "-P", "pcb", str(sp)]
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=config.SLICE_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                raise ParametricError("OpenSCAD: превышено время рендера") from None
            if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                msg = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()
                tail = " ".join(msg[-3:])[:300] if msg else "неизвестная ошибка"
                out.unlink(missing_ok=True)
                raise ParametricError(f"OpenSCAD не смог отрисовать модель: {tail}")
            return out
        finally:
            jf.unlink(missing_ok=True)   # param-set — временный вход OpenSCAD, не часть кэша


async def render_parts(model: dict, scad_values: dict):
    """STL по частям для многоцветного превью: корпус (Part=1), «краска» текст+рамка (Part=2),
    евробанд — синяя полоса слева (Part=3). Возвращает (body, ink|None, euro|None); пустая часть
    (нет текста/рамки/полосы) → None."""
    body = await render(model, {**scad_values, "Part": 1})

    async def _opt(part):
        try:
            return await render(model, {**scad_values, "Part": part})
        except ParametricError:
            return None       # часть пустая (нечего красить) — просто меньше цветов
    ink = await _opt(2)
    euro = await _opt(3)
    return body, ink, euro


async def autosize_keychain(model: dict, scad_values: dict) -> dict:
    """Подогнать Length таблички под РЕАЛЬНУЮ ширину текста. OpenSCAD 2021.01 без textmetrics(),
    поэтому меряем напрямую: рендерим ТОЛЬКО текст (Part=2, Border=0) и берём X-габарит его STL —
    ширина текста не зависит ни от Length, ни от позиции. Length = полоса + текст + поля по краям.
    Так номер любой длины/символов всегда влезает с отступом (раньше клиентская оценка занижала)."""
    text = str(scad_values.get("Text", "")).strip()
    if not text:
        return scad_values
    measure = {**scad_values, "Part": 2, "Border": 0, "Ring": 0}
    try:
        stl = await render(model, measure)
        text_w = await asyncio.to_thread(_measure_text_width, stl)   # trimesh.load — вне event loop
    except Exception:  # noqa: BLE001
        log.debug("autosize measure failed", exc_info=True)
        return scad_values
    if text_w <= 0:
        return scad_values
    # euro_w только если у модели есть полоса (Euro=1); у бирки (Euro=0) текст идёт во всю ширину
    euro_w = max(10.0, float(scad_values.get("Width", 15)) * 0.5) if scad_values.get("Euro", 1) else 0.0
    length = int(max(40, min(230, round(euro_w + text_w + 2 * 9.0))))   # 9 мм поля с каждой стороны
    return {**scad_values, "Length": length}


# ── QR-брелок (idea #5): геометрия генерится в Python (qrcode + trimesh), без OpenSCAD ──
def _qr_sanitize(model: dict, raw: dict) -> dict:
    """Параметры QR-брелока (форма/кастомайзер) → значения генератора, с клампами/анти-инъекцией нет
    (текст идёт ДАННЫМИ в QR, не в код)."""
    raw = raw or {}
    text = str(raw.get("Text") or raw.get("text") or "https://t.me/")[:300]

    def num(keys, default, lo, hi):
        for k in keys:
            if k in raw and raw[k] not in (None, ""):
                try:
                    return max(lo, min(hi, float(raw[k])))
                except (TypeError, ValueError):
                    pass
        return default
    rng = raw.get("Ring", raw.get("ring", 1))
    ring = 1 if (rng in (1, "1", True) or str(rng).strip().lower() in ("true", "yes", "on")) else 0
    return {"Text": text, "Size": round(num(["Size", "widthMm"], 40.0, 26.0, 60.0), 1),
            "Thickness": round(num(["Thickness", "thickMm"], 3.0, 2.0, 6.0), 1), "Ring": ring}


async def _render_qr(model: dict, values: dict):
    """STL QR-брелока: плита + поднятые тёмные модули QR + ушко. values['Part']: 0 всё / 1 корпус
    (плита+ушко) / 2 модули QR — для 2-цветного рендера. Кэш по сигнатуре, как у .scad."""
    import qrcode
    import trimesh
    config.PARAM_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    out = config.PARAM_RENDER_DIR / f"{model['id']}_{_signature(model, values)}.stl"
    if out.exists() and out.stat().st_size > 0:
        return out
    text = str(values.get("Text") or " ")
    part = int(values.get("Part", 0) or 0)
    size = float(values.get("Size", 40)); th = float(values.get("Thickness", 3))
    ring = int(values.get("Ring", 1)); mh = 0.8
    qr = qrcode.QRCode(border=2, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text); qr.make(fit=True)
    mat = qr.get_matrix(); n = len(mat); cell = size / n
    meshes = []
    if part in (0, 1):
        base = trimesh.creation.box(extents=[size, size, th]); base.apply_translation([0, 0, th / 2])
        meshes.append(base)
        if ring:
            w = trimesh.creation.annulus(r_min=2.2, r_max=4.5, height=th)
            w.apply_translation([0, size / 2 + 3.5, th / 2]); meshes.append(w)
    if part in (0, 2):
        boxes = []
        for r in range(n):
            for c in range(n):
                if mat[r][c]:
                    b = trimesh.creation.box(extents=[cell, cell, mh])
                    b.apply_translation([(c + 0.5) * cell - size / 2, size / 2 - (r + 0.5) * cell, th + mh / 2])
                    boxes.append(b)
        if boxes:
            meshes.append(trimesh.util.concatenate(boxes))
    if not meshes:
        raise ParametricError("QR пустой")
    mesh = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
    mesh.export(str(out))
    return out
