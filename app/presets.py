"""Пресеты качества (высота слоя) и прочности (заполнение + периметры).

Качество маппится на профиль процесса CC2 0.4 nozzle и на точную высоту слоя.
Прочность задаёт sparse_infill_density (%) и wall_loops (периметры).
"""
from __future__ import annotations

# key -> данные пресета
QUALITY_PRESETS: dict[str, dict] = {
    "q012": {"label": "Высокое — 0.12 мм", "layer_height": 0.12,
             "process": "0.12mm Fine @Elegoo CC2 0.4 nozzle.json"},
    "q016": {"label": "Тонкое — 0.16 мм", "layer_height": 0.16,
             "process": "0.16mm Optimal @Elegoo CC2 0.4 nozzle.json"},
    "q020": {"label": "Стандарт — 0.20 мм", "layer_height": 0.20,
             "process": "0.20mm Standard @Elegoo CC2 0.4 nozzle.json"},
    "q028": {"label": "Быстрое — 0.28 мм", "layer_height": 0.28,
             "process": "0.28mm Extra Draft @Elegoo CC2 0.4 nozzle.json"},
}
QUALITY_ORDER = ["q012", "q016", "q020", "q028"]

STRENGTH_PRESETS: dict[str, dict] = {
    "s_low":  {"label": "Низкая (декор)",  "infill": 10, "walls": 2},
    "s_med":  {"label": "Средняя",         "infill": 20, "walls": 3},
    "s_high": {"label": "Высокая",         "infill": 40, "walls": 3},
    "s_max":  {"label": "Максимальная",    "infill": 60, "walls": 4},
}
STRENGTH_ORDER = ["s_low", "s_med", "s_high", "s_max"]


def quality_label(key: str) -> str:
    p = QUALITY_PRESETS.get(key)
    return p["label"] if p else key


def strength_label(key: str) -> str:
    p = STRENGTH_PRESETS.get(key)
    return p["label"] if p else key
