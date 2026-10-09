"""Сопоставление типа материала с профилем филамента принтера.

Плотность для расчёта берётся из карточки пластика; профиль филамента влияет на
геометрию слайса (поток/температуры) во вторую очередь, но выбираем подходящий,
чтобы слайсер не ругался на несовместимость и считал точнее.
"""
from __future__ import annotations

from pathlib import Path

# Типы материалов для админки (выбор при добавлении пластика).
MATERIAL_TYPES = ["PLA", "PLA+", "PLA Matte", "PETG", "ABS", "ASA", "TPU", "PLA-CF", "PA-CF", "Generic"]

# Категории каталога пластиков (3af25fc2): группировка витрины выбора цвета в мини-аппе
# (раньше группы считались от цены за кг — «базовые/стандарт/премиум»).
PLASTIC_CATEGORIES = ["Матовый декор", "Обычный", "Инженерный", "Шелковый"]


def category_hint(material_type: str, name: str = "") -> str:
    """Категория пластика по типу/названию — для бэкфилла существующих записей и
    подсказки в админке. Админ может переопределить в карточке пластика."""
    t = (material_type or "").lower()
    n = (name or "").lower()
    if "silk" in n or "silk" in t or "шелк" in n or "шёлк" in n:
        return "Шелковый"
    if "matte" in t or "matte" in n or "матов" in n:
        return "Матовый декор"
    if any(k in t for k in ("petg", "abs", "asa", "tpu", "-cf", "pa", "нейлон")):
        return "Инженерный"
    return "Обычный"

# Подсказки плотности, г/см³ (предлагаются админу).
DENSITY_HINTS = {
    "PLA": 1.24, "PLA+": 1.24, "PLA Matte": 1.24, "PETG": 1.27, "ABS": 1.04,
    "ASA": 1.07, "TPU": 1.21, "PLA-CF": 1.29, "PA-CF": 1.18, "Generic": 1.24,
}


def density_hint(material_type: str) -> float:
    return DENSITY_HINTS.get(material_type, 1.24)


# Базовые описания свойств (админ может переопределить для конкретного пластика).
DESCRIPTIONS = {
    "PLA": "Жёсткий, лёгкий в печати, биоразлагаемый. Теплостойкость ~55°C. Для декора, прототипов, игрушек.",
    "PLA+": "Улучшенный PLA: прочнее и не такой хрупкий. Так же лёгок в печати.",
    "PLA Matte": "PLA с матовой бархатистой поверхностью. Скрывает слои, приятный вид.",
    "PETG": "Прочный и слегка гибкий, влаго- и химостойкий, теплостойкость ~70°C. Для функциональных деталей.",
    "ABS": "Прочный, теплостойкий (~100°C), ударопрочный. Нужна закрытая камера, есть запах при печати.",
    "ASA": "Как ABS, но устойчив к ультрафиолету и погоде. Для уличных деталей.",
    "TPU": "Гибкий, резиноподобный, износостойкий. Для чехлов, прокладок, амортизаторов.",
    "PLA-CF": "PLA с углеволокном: жёстче и легче, матовая поверхность. Абразивен — нужно стальное сопло.",
    "PA-CF": "Нейлон с углеволокном: очень прочный и термостойкий. Требователен к печати, гигроскопичен.",
    "Generic": "Универсальный филамент.",
}


def description_hint(material_type: str) -> str:
    return DESCRIPTIONS.get(material_type, DESCRIPTIONS["Generic"])


# Цвет (название → RGB 0..1) для подкраски превью. Подстрочное совпадение.
COLOR_RGB = {
    "чёрн": (0.13, 0.13, 0.16), "черн": (0.13, 0.13, 0.16),
    "бел": (0.92, 0.92, 0.95), "сер": (0.55, 0.56, 0.60), "сир": (0.7, 0.6, 0.85),
    "красн": (0.86, 0.16, 0.16), "оранж": (1.0, 0.5, 0.12), "жёлт": (0.97, 0.8, 0.12),
    "желт": (0.97, 0.8, 0.12), "зел": (0.2, 0.7, 0.3), "син": (0.16, 0.32, 0.85),
    "голуб": (0.35, 0.65, 0.95), "бирюз": (0.1, 0.72, 0.7), "фиолет": (0.6, 0.3, 0.82),
    "роз": (0.96, 0.5, 0.72), "коричн": (0.5, 0.3, 0.16), "беж": (0.85, 0.75, 0.6),
    "золот": (0.83, 0.69, 0.24), "серебр": (0.76, 0.78, 0.82), "проз": (0.78, 0.85, 0.92),
    # палитра Elegoo PLA Matte (3af25fc2): мятный и лавандовый не ловились подстроками выше
    "мят": (0.62, 0.85, 0.68), "лаванд": (0.72, 0.62, 0.88),
}


def _norm_color(s: str) -> str:
    """Нормализация имени цвета для сравнения: регистр и ё/е не различаем."""
    return (s or "").strip().lower().replace("ё", "е")


def _resolve_one(part: str, overrides=None):
    """RGB одного цвета: сперва оттенки админа, затем встроенная таблица. None — не распознан."""
    n = _norm_color(part)
    if not n:
        return None
    if overrides:
        for key, rgb in overrides.items():
            if _norm_color(key) == n:
                return rgb
        for key, rgb in overrides.items():
            k = _norm_color(key)
            if k and k in n:
                return rgb
    for key, rgb in COLOR_RGB.items():
        if key.replace("ё", "е") in n:
            return rgb
    return None


def color_to_rgbs(name: str, overrides=None) -> list:
    """Список RGB по названию. Тире = градиент-переливание: «зелёный-красный-синий» -> 3 цвета.
    overrides: dict {имя_в_нижнем_регистре: (r,g,b)} — оттенки, заданные админом (приоритет).
    Оттенок админа, совпавший с именем целиком, побеждает до разбора по тире — иначе
    «Тёмно-синий» распадался бы на «тёмно»+«синий» и красился встроенным синим."""
    raw = (name or "").strip()
    if not raw:
        return []
    if overrides:
        full = _norm_color(raw)
        for key, rgb in overrides.items():
            if _norm_color(key) == full:
                return [rgb]
    parts = [p.strip() for p in raw.split("-") if p.strip()] or [raw]
    out = []
    for part in parts:
        rgb = _resolve_one(part, overrides)
        if rgb is not None:
            out.append(rgb)
    return out


def color_to_rgb(name: str, overrides=None):
    """RGB 0..1 по названию цвета (первый из списка) или None."""
    rgbs = color_to_rgbs(name, overrides)
    return rgbs[0] if rgbs else None


def resolve_filament(filament_dir: str | Path, material_type: str, fallback: str = "",
                     prefer: str = "") -> str:
    """Подобрать .json филамента под тип материала в папке принтера.

    prefer — профиль, ВЫБРАННЫЙ МАСТЕРОМ явно (карточка пластика / паспорт калибровки).
    Он бьёт автоподбор: откалиброванный профиль лежит в пользовательской папке слайсера, и
    автоподбор по имени в системной папке принтера его просто не увидит — бот резал бы стоком
    (MVS 16 вместо намеренных 13, поток 1.0 вместо 1.01), обесценивая всю калибровку.
    Несуществующий путь игнорируем: удалённый пресет не должен ломать расчёт.
    """
    if prefer:
        p = Path(prefer)
        if p.is_file():
            return str(p)
    d = Path(filament_dir)
    if not d.exists():
        return fallback
    files = sorted(d.glob("*.json"))
    if not files:
        return fallback
    mt = (material_type or "").lower().strip()

    def score(stem: str) -> int:
        n = stem.lower()
        s = 0
        if mt and mt in n:
            s += 2
        if mt and n.startswith(f"elegoo {mt} "):
            s += 3
        if mt and n.startswith(f"elegoo {mt}@") or n.startswith(f"elegoo {mt}"):
            s += 1
        return s

    best, best_s = None, 0
    for f in files:
        s = score(f.stem)
        if s > best_s:
            best, best_s = f, s
    if best and best_s >= 2:
        return str(best)
    if fallback:
        return fallback
    return str(files[0])
