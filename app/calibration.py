"""Калибровка филамента: реестр тестов, правила диагностики, пресеты слайсера.

Зачем модуль. Калибровка новой катушки/сопла — это неделя тестовых печатей, десяток
физических образцов и восемь чисел на выходе. Числа живут в пользовательских пресетах
ElegooSlicer (%APPDATA%\\ElegooSlicer\\user\\default), то есть ВНЕ бэкапа проекта, и слетают
при обновлении слайсера. Всё остальное — какой тест что показал, почему выбрано именно это
значение, чем верхняя рябь отличается от нижней — живёт только в голове мастера.

Модуль — доменное ядро этого знания, БЕЗ доступа к БД (иначе цикл импорта: db импортирует
calibration ради detect_test). Здесь три независимых части:

  1. Реестр тестов (TESTS) — что за тест, что калибрует, в каком пресете лежит результат,
     и по каким кускам имени файла его узнать в журнале печатей. Журнал ведётся автоматически
     по имени задания принтера (`ECC2_0.4_temperature_tower_<филамент>_0.2_28m13s.gcode`),
     поэтому распознавание типа теста — чистая функция от строки и работает задним числом
     на всей накопленной истории.
  2. Правила диагностики (DIAGNOSTICS) + вердикты по замерам (verdict) — то, что обычно
     остаётся в переписке: недоэкструзия vs резонанс, звон vs VFA, торчащие углы = PA.
     Правило записано один раз и применяется к цифрам, а не к воспоминаниям.
  3. Работа с пресетами: чтение (в т.ч. со слиянием inherits через пользовательские И
     системные профили), запись значения с сохранением формы (пер-экструдерные параметры —
     массивы строк, скалярные — строки) и снимки для истории изменений.

Плюс analyze_tower() — грубая CV-подсказка по фото башни: где на ней рябь. Это ПОДСКАЗКА,
а не вердикт: решение всегда за замером штангенциркулем (см. DIAGNOSTICS['underextrusion']).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from . import config, slicers

log = logging.getLogger(__name__)

PRESET_KINDS = ("filament", "machine", "process")


# ── Реестр калибровочных тестов ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CalibParam:
    """Один параметр слайсера, который настраивает тест.

    pct=True — слайсер хранит значение строкой с процентом (`"19%"`, глажка). Форму надо
    сохранять при записи, иначе слайсер не подхватит голое число.
    """
    key: str
    label: str
    unit: str
    scope: str                   # filament | machine | process
    pct: bool = False


@dataclass(frozen=True)
class CalibTest:
    """Один шаг калибровки.

    params — параметры, которые тест настраивает. Обычно один (температура, поток, PA), но
    бывает и два: глажку нельзя настроить одним числом — поток и скорость меняют результат
    вместе, при большом потоке и высокой скорости борозды остаются, при малом — следы витков.
    Пустой params = тест ничего не пишет в пресет (проверка размеров, механика), только вывод.

    scope остаётся отдельным полем: у тестов без параметров он говорит, где вообще живут их
    настройки (первый слой — процесс, Input Shaping — принтер).
    """
    key: str
    label: str
    order: int
    scope: str
    unit: str
    aliases: tuple[str, ...]
    method: str
    look: str
    hint: str
    params: tuple[CalibParam, ...] = ()
    depends: tuple[str, ...] = ()
    formula: str = ""
    kind: str = "value"          # value — даёт число; check — проверка; mech — механика

    @property
    def param(self) -> str:
        """Ключ ОСНОВНОГО параметра — тот, чьё значение лежит в calib_steps.value."""
        return self.params[0].key if self.params else ""


# Порядок имеет значение: каждый следующий тест опирается на результат предыдущего
# (температура → поток → PA → откат → расход → VFA). Менять порядок — значит пересчитывать всё.
_TESTS: tuple[CalibTest, ...] = (
    CalibTest(
        key="temp", label="Температура", order=10,
        scope="filament", unit="°C",
        params=(CalibParam("nozzle_temperature", "Температура", "°C", "filament"),),
        aliases=("temperature_tower", "temp_tower", "temptower", "температур", "темп_башн"),
        method="Температурная башня по диапазону производителя, шаг 5 °C.",
        look="Слои, где нет ни недоплава (щели, слабая спайка), ни перегрева (нити, «сопли», "
             "оплывшие нависания). Проверить пальцами прочность спайки между секциями.",
        hint="Собственный тест имеет приоритет над стоковым значением профиля. Косвенное "
             "подтверждение верной температуры — широкое чистое окно в тесте откатов.",
    ),
    CalibTest(
        key="flow", label="Коэффициент потока", order=20,
        scope="filament", unit="",
        params=(CalibParam("filament_flow_ratio", "Коэффициент потока", "", "filament"),),
        aliases=("flowrate", "flow_rate", "flowratio", "поток"),
        method="Метод YOLO — одна печать. Число на плашке = ДОБАВКА к текущему значению, "
               "а не множитель.",
        look="Верхняя поверхность под косым светом: щели между витками = недолив, "
             "гребни и волна = перелив.",
        hint="Новое значение = текущее + добавка с лучшей плашки.",
        depends=("temp",), formula="flow = flow_текущий + добавка",
    ),
    CalibTest(
        key="pa", label="Pressure Advance", order=30,
        scope="filament", unit="",
        params=(CalibParam("pressure_advance", "Pressure Advance", "", "filament"),),
        aliases=("pa_pattern", "pa_tower", "pa_line", "pressure_advance", "pa_test"),
        method="Метод «Шаблон» (PA Pattern) на диапазоне 0 → 0.1.",
        look="Углы шаблона: горбы и наплывы на выходе из угла = PA занижен, "
             "провал и утоньшение сразу после угла = PA завышен.",
        hint="Проверять кубом: с верным PA куб приходит в номинал без компенсации XY. "
             "Адаптивный PA (beta) без отдельной таблицы из нескольких прогонов бесполезен.",
        depends=("temp", "flow"),
    ),
    CalibTest(
        key="retract", label="Откат", order=40,
        scope="machine", unit="мм",
        params=(CalibParam("retraction_length", "Откат", "мм", "machine"),),
        aliases=("retraction_tower", "retraction_test", "retract", "откат"),
        method="Башня откатов 0 → 2 мм, шаг 0.1 мм.",
        look="Ступени без нитей между колоннами. Возврат нитей после верхних ступеней — "
             "переретракт: расплав затягивается в термобарьер, подсасывается воздух.",
        hint="Брать СЕРЕДИНУ чистого окна, а не его край: отсчёт ступеней неоднозначен "
             "(нижняя ступень = 0 или = 1), середина попадает в окно при обоих вариантах. "
             "Это параметр ПРИНТЕРА, не филамента.",
        depends=("temp",),
    ),
    CalibTest(
        key="mvs", label="Максимальный объёмный расход", order=50,
        scope="filament", unit="мм³/с",
        params=(CalibParam("filament_max_volumetric_speed", "Максимальный объёмный расход", "мм³/с", "filament"),),
        aliases=("speedteststructure", "max_volumetric", "volumetric", "mvs", "vol_speed",
                 "расход"),
        method="Клин 5 → 20 мм³/с, шаг 0.5 мм³/с НА МИЛЛИМЕТР высоты. Печатается слоем "
               "0.8 × диаметр сопла и намеренно обходит ограничение слоя из профиля принтера.",
        look="Высота, на которой начинается рябь. Зон ряби может быть ДВЕ — нижняя и верхняя; "
             "между ними чистое окно.",
        hint="Нижняя рябь к расходу отношения не имеет (при малом расходе хотенд работает "
             "вполсилы). Верхнюю обязательно проверить замером толщины стенки: упала = "
             "недоэкструзия и это реальный потолок (ставить −10 %), не изменилась = резонанс, "
             "предел не найден. MVS — ограничитель, а не задание: «в стоке было 16 и всё "
             "печаталось» ничего не доказывает.",
        depends=("temp", "flow"), formula="MVS = основание + высота_мм × шаг",
    ),
    CalibTest(
        key="vfa", label="VFA — вертикальные полосы", order=60,
        scope="process", unit="мм/с",
        params=(CalibParam("outer_wall_speed", "VFA — вертикальные полосы", "мм/с", "process"),),
        aliases=("vfa", "vfa_test", "vfa_tower"),
        method="VFA-тест: башня с секциями на разных скоростях наружного периметра.",
        look="Скорости, на которых полосы выражены сильнее всего. Тест даёт не настройку, "
             "а СПИСОК ПЛОХИХ СКОРОСТЕЙ, которых надо избегать.",
        hint="До теста бесплатно проверить механику: натяжение обоих ремней, затяжку стопорных "
             "винтов шкивов на валах моторов, свободный ход роликов без биения. VFA убирается "
             "хуже всех артефактов, причина механическая — обычно удаётся только ослабить.",
        depends=("pa",), kind="mech",
    ),
    CalibTest(
        key="cube", label="Куб — размерная точность", order=70,
        scope="", unit="мм",
        aliases=("калибровочный куб", "calibration_cube", "xyz_cube", "xyzcube", "куб", "cube"),
        method="Куб 20 × 20 × 20 мм после каждой правки PA.",
        look="Замер штангенциркулем НА СЕРЕДИНЕ ВЫСОТЫ, пропуская нижние 2–3 слоя (слоновья "
             "нога) и грань со швом.",
        hint="При торчащих углах замер бессмыслен — губки ложатся на наплывы: сначала PA. "
             "Куб в номинале = компенсация XY не нужна.",
        depends=("pa",), kind="check",
    ),
    CalibTest(
        key="first_layer", label="Первый слой", order=80,
        scope="process", unit="",
        aliases=("first_layer", "первый слой", "bed_level", "первый_слой"),
        method="Правки по ОДНОЙ, между правками — печать куба.",
        look="Тонкая линия на старте (дальняя грань, где начинается печать) — давление в сопле "
             "не набрано после перегона и отката. Микродырки на стыке периметра с заполнением.",
        hint="Тонкая линия лечится ЮБКОЙ в 2–3 петли, не Z-офсетом. Микродырки: сначала "
             "Z-офсет −0.02 мм, если осталось — наложение заполнения и стенок до 25 %. Обе "
             "правки сразу не дадут понять, что сработало, а перезажатый первый слой даст "
             "слоновью ногу и испортит достигнутую размерную точность.",
        depends=("cube",), kind="check",
    ),
    CalibTest(
        key="ironing", label="Глажка (разглаживание)", order=90,
        scope="process", unit="%",
        # Два параметра: одним числом глажку не настроить. Поток решает, СКОЛЬКО материала
        # добавляется в проглаживающий проход, скорость — сколько времени сопло разравнивает
        # уже выдавленное. Итог зависит от их пары, поэтому и правило смотрит на обе.
        params=(CalibParam("ironing_flow", "Поток глажки", "%", "process", pct=True),
                CalibParam("ironing_speed", "Скорость глажки", "мм/с", "process")),
        aliases=("熨烫测试", "ironing", "глажка", "разглаживание", "iron_test"),
        method="Плоская деталь с большой верхней поверхностью (или сам тест глажки), режется "
               "готовым профилем. Тип глажки — «только верхние поверхности» (ironing_type=top): "
               "гладить все сплошные слои значит греть то, чего никто не увидит, и терять часы. "
               "Менять по одному параметру за печать, шаг у потока 3–5 %, у скорости 10 мм/с.",
        look="Верхняя поверхность под косым светом. Следы витков и мелкие ямки между ними — "
             "материала не хватает, поднять поток. Наплывы, «пропаханные» борозды, задиры и "
             "скатывание в валик перед соплом — материала много, поток снизить. Матовая "
             "шершавость при уже ровной геометрии — слишком быстро, снизить скорость.",
        hint="Глажка НЕ лечит плохой поток и неверную высоту слоя — она их размазывает: сначала "
             "должны быть готовы температура, поток и PA. Шаг глажки (ironing_spacing) обычно "
             "не трогают, 0.1–0.15 мм. Печать заметно дольше: проглаживающий проход идёт по "
             "всей площади верха.",
        depends=("flow", "pa"), kind="value",
    ),
    CalibTest(
        key="input_shaping", label="Input Shaping", order=100,
        scope="machine", unit="Гц",
        aliases=("ringing_tower", "ringing", "input_shaping", "shaping"),
        method="Полный тест (Ringing Tower), затухание выставить 0, диапазон 15–110 Гц. "
               "Сначала проверить, нет ли встроенной калибровки с акселерометром в меню "
               "самого принтера — она точнее.",
        look="Затухающая волна ПОСЛЕ УГЛА (звон), а не равномерная рябь по всей стенке (VFA).",
        hint="Для VFA не поможет — делать только если появился звон от углов.",
        kind="mech",
    ),
)

TESTS: dict[str, CalibTest] = {t.key: t for t in _TESTS}
TEST_ORDER: list[str] = [t.key for t in sorted(_TESTS, key=lambda t: t.order)]

# Шаги, из которых собирается кампания по умолчанию (механика и проверки — по желанию).
DEFAULT_STEPS: tuple[str, ...] = ("temp", "flow", "pa", "retract", "mvs", "cube", "ironing", "vfa")

# Служебные куски имени задания принтера, которые не должны участвовать в распознавании:
# `ECC2_0.4_<модель>_<филамент>_<слой>_<время>.gcode`. Иначе «0.4» и расширение шумят.
_LABEL_TRIM = re.compile(r"^ecc2[_-]?[\d.]+[_-]|\.gcode$|\.3mf$", re.I)


def test_info(key: str) -> CalibTest | None:
    return TESTS.get((key or "").strip().lower())


def detect_test(label: str) -> str:
    """Тип калибровочного теста по имени печати из журнала ('' — обычная печать).

    Журнал печатей заполняется автоматически именем задания принтера, поэтому вся накопленная
    история распознаётся задним числом одной пересборкой, без ручного ввода. Сначала проверяем
    длинные (более специфичные) алиасы: `retraction_tower` не должен победить `retract` в
    случайном имени модели.
    """
    s = _LABEL_TRIM.sub("", (label or "").strip().lower())
    if not s:
        return ""
    best, best_len = "", 0
    for t in _TESTS:
        for a in t.aliases:
            if a in s and len(a) > best_len:
                best, best_len = t.key, len(a)
    return best


def label_model(label: str) -> str:
    """«Человеческая» часть имени задания: ECC2_0.4_<это>_<филамент>_… → <это>."""
    s = re.sub(r"\.gcode$|\.3mf$", "", (label or "").strip(), flags=re.I)
    s = re.sub(r"^ECC2[_-]?[\d.]+[_-]", "", s, flags=re.I)
    return s.split("_")[0] if "_" in s else s


# ── Правила диагностики ────────────────────────────────────────────────────────────────────
# Выработаны по ходу калибровки Elegoo PLA Matte (27.07.2026) и годятся для любого филамента.
# Формат один: симптом → чем отличается от похожего → причина → что делать.
DIAGNOSTICS: tuple[dict, ...] = (
    {
        "key": "underextrusion",
        "title": "Недоэкструзия или резонанс?",
        "symptom": "Рябь на стенке в верхней части теста расхода.",
        "distinguish": "Недоэкструзия физически убирает материал: линии тоньше, щели между "
                       "витками, матовая шершавая поверхность. Резонанс материал не трогает — "
                       "правильная регулярная волна при том же количестве пластика.",
        "cause": "Хотенд не успевает плавить (недоэкструзия) либо механика попала в резонанс.",
        "fix": "Различать ЗАМЕРОМ толщины стенки штангенциркулем, не на глаз. Толщина упала → "
               "это реальный потолок расхода, ставить −10 % от него. Толщина та же → резонанс, "
               "предел не найден: сделать Input Shaping и перегенерировать тест с более широким "
               "диапазоном.",
        "tests": ("mvs",),
    },
    {
        "key": "ringing_vs_vfa",
        "title": "Звон или VFA?",
        "symptom": "Волны/полосы на вертикальной стенке.",
        "distinguish": "Звон начинается сразу после угла и затухает по мере удаления, длина "
                       "волны — единицы миллиметров, реагирует на PA. VFA равномерен по всей "
                       "стенке, шаг — доли миллиметра, на PA не реагирует вообще.",
        "cause": "Звон — инерция головы (лечится Input Shaping). VFA — механика привода.",
        "fix": "Замерить шаг полос (сколько укладывается в 10 мм): ≈2 мм → шаг зубьев ремня "
               "(натяжение, износ, биение шкива, стопорный винт); ≈0.2 мм и мельче, регулярно → "
               "зубцовые пульсации шаговиков (лечится подбором скорости); шаг гуляет → это не "
               "VFA, а разброс диаметра прутка или люфт по Z.",
        "tests": ("vfa", "input_shaping"),
    },
    {
        "key": "corners_pa",
        "title": "Торчащие углы = заниженный PA",
        "symptom": "Наплывы и горбы на углах детали.",
        "distinguish": "Обратное — провал и утоньшение стенки сразу после угла — означает "
                       "завышенный PA.",
        "cause": "Давление в сопле не успевает стравиться перед углом.",
        "fix": "Поднять Pressure Advance и перепечатать куб. Признак верного PA — куб приходит "
               "в номинал без компенсации XY.",
        "tests": ("pa", "cube"),
    },
    {
        "key": "cube_measure",
        "title": "Как мерить куб",
        "symptom": "Размеры куба «плавают» от замера к замеру.",
        "distinguish": "",
        "cause": "Замер по низу ловит слоновью ногу, замер по грани со швом ловит шов, "
                 "а при торчащих углах губки штангенциркуля вообще ложатся на наплывы.",
        "fix": "Мерить на середине высоты, пропуская нижние 2–3 слоя и грань со швом. При "
               "торчащих углах сначала чинить PA, потом мерить.",
        "tests": ("cube",),
    },
    {
        "key": "batch_bound",
        "title": "Что переносится между катушками, а что нет",
        "symptom": "Новая катушка того же филамента печатает иначе.",
        "distinguish": "",
        "cause": "MVS и коэффициент потока привязаны к конкретной партии и к температуре.",
        "fix": "При смене температуры или партии переснимать расход и поток. PA — тоже "
               "свойство филамента. Температуру и откат обычно можно переносить между "
               "катушками одного типа.",
        "tests": ("mvs", "flow", "pa"),
    },
    {
        "key": "first_layer_start",
        "title": "Тонкая линия в начале первого слоя",
        "symptom": "На дальней грани, где начинается печать, первая линия тоньше остальных.",
        "distinguish": "Дефект строго на СТАРТЕ печати, а не по всему периметру.",
        "cause": "Давление в сопле не набрано после перегона и отката.",
        "fix": "Юбка в 2–3 петли. Z-офсетом НЕ лечить: перезажатый первый слой даст слоновью "
               "ногу и испортит размерную точность.",
        "tests": ("first_layer",),
    },
    {
        "key": "first_layer_pinholes",
        "title": "Микродырки на стыке периметра с заполнением",
        "symptom": "Точечные просветы по линии стыка первого слоя, обычно не на всех гранях.",
        "distinguish": "",
        "cause": "Не хватает материала ровно в шве между периметром и заполнением.",
        "fix": "Сначала Z-офсет −0.02 мм. Если осталось — наложение заполнения и стенок до 25 %. "
               "Правки строго по одной, между ними — печать куба.",
        "tests": ("first_layer",),
    },
    {
        "key": "nozzle_drip",
        "title": "Капля на сопле после печати",
        "symptom": "Капля пластика на кончике сопла по окончании печати.",
        "distinguish": "Ненормально другое: пластик идёт ИЗ-ПОД сопла по резьбе — тёмный нагар "
                       "на блоке, который копится от печати к печати.",
        "cause": "Норма: остаточное давление стравливается на остывании.",
        "fix": "Если течёт по резьбе — тянуть сопло ТОЛЬКО горячим, 240–250 °C, придерживая "
               "нагревательный блок ключом.",
        "tests": (),
    },
    {
        "key": "overretract",
        "title": "Переретракт",
        "symptom": "В башне откатов нити возвращаются на верхних ступенях, хотя в середине "
                   "было чисто.",
        "distinguish": "",
        "cause": "Расплав затягивается в термобарьер, подсасывается воздух.",
        "fix": "Не гнаться за длинным откатом: брать середину чистого окна.",
        "tests": ("retract",),
    },
    {
        "key": "ironing_flow",
        "title": "Глажка: борозды или следы витков",
        "symptom": "После включения глажки верх стал хуже, а не лучше: борозды, наплывы "
                   "или наоборот — видны витки и ямки между ними.",
        "distinguish": "Следы витков с ямками = материала НЕ хватает (поднять поток). Борозды, "
                       "задиры и валик, который сопло катит перед собой, = материала МНОГО "
                       "(снизить поток). Ровная геометрия при матовой шершавости = слишком "
                       "быстро (снизить скорость).",
        "cause": "Поток и скорость глажки работают в паре: поток задаёт, сколько материала "
                 "добавить в проглаживающий проход, скорость — сколько времени сопло его "
                 "разравнивает.",
        "fix": "Менять по ОДНОМУ параметру за печать: поток шагом 3–5 %, скорость шагом "
               "10 мм/с. Тип глажки держать «только верхние поверхности»: гладить все сплошные "
               "слои — это часы на то, чего не видно. Шаг глажки 0.1–0.15 мм обычно не трогают.",
        "tests": ("ironing",),
    },
    {
        "key": "ironing_masks",
        "title": "Глажка маскирует, а не чинит",
        "symptom": "Верх выглядит прилично только с глажкой, без неё — рябь и щели.",
        "distinguish": "",
        "cause": "Глажка размазывает уже выдавленный материал; недолив, перелив и неверный PA "
                 "она не устраняет, а прячет — и вылезают они на первой же детали без верха.",
        "fix": "Сначала довести температуру, поток и PA до состояния, когда верх хорош БЕЗ "
               "глажки, и только потом включать её как финишный слой.",
        "tests": ("ironing", "flow"),
    },
    {
        "key": "mvs_pointless",
        "title": "Даст ли повышение MVS что-нибудь вообще",
        "symptom": "Непонятно, стоит ли бороться за лишние мм³/с.",
        "distinguish": "",
        "cause": "MVS — ограничитель, а не задание: если модель его не требовала, настройка ни "
                 "разу не сработала.",
        "fix": "Нарезать одну и ту же деталь с двумя значениями MVS и сравнить расчётное время. "
               "Время не изменилось — упор не в хотенд, а в профильные скорости и ускорения.",
        "tests": ("mvs",),
    },
)


def diagnostics_for(test_key: str) -> list[dict]:
    k = (test_key or "").strip().lower()
    return [d for d in DIAGNOSTICS if not k or k in d["tests"]]


# ── Вердикты по замерам ────────────────────────────────────────────────────────────────────
def mvs_at_height(height_mm: float, base: float = 5.0, step: float = 0.5) -> float:
    """Расход на заданной высоте клина: MVS = основание + высота × шаг."""
    return round(float(base) + float(height_mm) * float(step), 2)


def verdict(kind: str, data: dict) -> dict:
    """Применить правило диагностики к цифрам замера.

    Возвращает {verdict, value, unit, detail, rule} — value заполняется, когда правило даёт
    готовое значение параметра (его можно применить в пресет одной кнопкой).
    """
    k = (kind or "").strip().lower()
    try:
        if k == "wall_thickness":
            # Верхняя рябь в тесте расхода: недоэкструзия или резонанс (DIAGNOSTICS.underextrusion)
            clean = float(data["clean"])
            ripple = float(data["ripple"])
            top = float(data.get("top_mvs") or 0)
            drop = (clean - ripple) / clean * 100 if clean else 0
            if drop >= 3:
                val = round(top * 0.9, 1) if top else None
                return {"verdict": "недоэкструзия", "value": val, "unit": "мм³/с", "rule":
                        "underextrusion",
                        "detail": f"Стенка тоньше на {drop:.1f} % — материал реально убыл. "
                                  f"Это потолок расхода" + (f", ставим {val} (−10 %)." if val
                                                            else ".")}
            return {"verdict": "резонанс", "value": None, "unit": "", "rule": "underextrusion",
                    "detail": f"Толщина та же (расхождение {drop:.1f} %) — материал на месте, "
                              "предел расхода НЕ найден. Нужен Input Shaping и более широкий "
                              "диапазон теста."}
        if k == "cube_xy":
            x, y = float(data["x"]), float(data["y"])
            nom = float(data.get("nominal") or 20.0)
            dx, dy = x - nom, y - nom
            worst = max(abs(dx), abs(dy))
            if worst <= 0.05:
                return {"verdict": "в норме", "value": None, "unit": "мм", "rule": "cube_measure",
                        "detail": f"{x} / {y} при номинале {nom} (X {dx:+.2f}, Y {dy:+.2f}) — "
                                  "компенсация XY не нужна."}
            if worst >= 0.15:
                return {"verdict": "углы торчат — сначала PA", "value": None, "unit": "мм",
                        "rule": "corners_pa",
                        "detail": f"Отклонение {worst:.2f} мм (X {dx:+.2f}, Y {dy:+.2f}). "
                                  "Крупный плюс обычно даёт заниженный PA, а не усадка: "
                                  "поднять PA и перемерить, компенсацию XY не трогать."}
            return {"verdict": "пограничный", "value": round(-((dx + dy) / 2), 3), "unit": "мм",
                    "rule": "cube_measure",
                    "detail": f"X {dx:+.2f} / Y {dy:+.2f}. Мерить на середине высоты, минуя шов "
                              "и нижние слои; если повторяется — компенсация XY на эту величину."}
        if k == "vfa_pitch":
            pitch = float(data["pitch"])       # мм между полосами
            if pitch >= 1.5:
                return {"verdict": "ремень", "value": pitch, "unit": "мм", "rule": "ringing_vs_vfa",
                        "detail": f"Шаг {pitch:.2f} мм ≈ шаг зубьев ремня: натяжение, износ, "
                                  "биение шкива, стопорный винт на валу мотора."}
            if pitch <= 0.4:
                return {"verdict": "зубцовые пульсации мотора", "value": pitch, "unit": "мм",
                        "rule": "ringing_vs_vfa",
                        "detail": f"Шаг {pitch:.2f} мм — пульсации шаговика. Лечится подбором "
                                  "скорости наружного периметра по VFA-тесту."}
            return {"verdict": "промежуточный шаг", "value": pitch, "unit": "мм",
                    "rule": "ringing_vs_vfa",
                    "detail": f"Шаг {pitch:.2f} мм не похож ни на ремень, ни на пульсации. "
                              "Проверить разброс диаметра прутка и люфт по Z."}
        if k == "mvs_window":
            lo = mvs_at_height(float(data["low_mm"]), float(data.get("base") or 5),
                               float(data.get("step") or 0.5))
            hi = mvs_at_height(float(data["high_mm"]), float(data.get("base") or 5),
                               float(data.get("step") or 0.5))
            return {"verdict": f"окно {lo}–{hi} мм³/с", "value": hi, "unit": "мм³/с",
                    "rule": "mvs_pointless",
                    "detail": f"Чистое окно с {data['low_mm']} по {data['high_mm']} мм высоты. "
                              f"Верх окна {hi} мм³/с — но сначала проверить замером стенки, "
                              "верхняя рябь это недоэкструзия или резонанс."}
        if k == "flow_yolo":
            base = float(data["base"])
            delta = float(data["delta"])
            return {"verdict": "поток", "value": round(base + delta, 3), "unit": "",
                    "rule": "", "detail": f"YOLO: число на плашке — ДОБАВКА. "
                                          f"{base} + {delta} = {round(base + delta, 3)}."}
        if k == "retract_window":
            lo, hi = float(data["low"]), float(data["high"])
            mid = round((lo + hi) / 2, 2)
            return {"verdict": "середина окна", "value": mid, "unit": "мм", "rule": "overretract",
                    "detail": f"Чистое окно {lo}–{hi} мм → берём середину {mid} мм: отсчёт "
                              "ступеней неоднозначен, середина попадает в окно при любом."}
        if k == "ironing":
            # Глажку правят по симптому, а не по формуле: правило переводит увиденное в
            # конкретную поправку к текущей паре (поток, скорость) — и строго по одному
            # изменению за печать, иначе непонятно, что сработало.
            flow = float(data["flow"])
            speed = float(data.get("speed") or 0)
            sym = str(data.get("symptom") or "").strip().lower()
            if sym.startswith("нед") or sym in ("витки", "ямки", "мало"):
                nv = round(flow + 4, 1)
                return {"verdict": "мало материала", "value": nv, "unit": "%", "rule": "ironing",
                        "detail": f"Видны следы витков и ямки между ними — поток глажки "
                                  f"{flow:g} → {nv:g} %. Скорость {speed:g} мм/с пока не трогаем: "
                                  "меняем по одному параметру за печать."}
            if sym.startswith("пер") or sym in ("борозды", "наплывы", "валик", "много"):
                nv = round(flow - 4, 1)
                return {"verdict": "много материала", "value": max(nv, 1.0), "unit": "%",
                        "rule": "ironing",
                        "detail": f"Борозды и наплывы перед соплом — поток глажки {flow:g} → "
                                  f"{max(nv, 1.0):g} %. Если после снижения осталась шершавость, "
                                  f"следующим шагом сбавить скорость {speed:g} мм/с на 10."}
            if sym.startswith("шерш") or sym in ("быстро", "матово"):
                ns = round(speed - 10, 1) if speed else None
                return {"verdict": "слишком быстро", "value": ns, "unit": "мм/с",
                        "rule": "ironing",
                        "detail": f"Геометрия ровная, но поверхность матовая — сопло не успевает "
                                  f"разровнять: скорость {speed:g} → {ns:g} мм/с при том же "
                                  f"потоке {flow:g} %." if speed else
                                  "Геометрия ровная, но поверхность матовая — снизить скорость."}
            return {"verdict": "поверхность в порядке", "value": flow, "unit": "%",
                    "rule": "ironing",
                    "detail": f"Пара {flow:g} % и {speed:g} мм/с даёт ровный верх — фиксируем."}
        if k == "speed_from_mvs":
            mvs = float(data["mvs"])
            lh = float(data.get("layer_height") or 0.2)
            lw = float(data.get("line_width") or 0.42)
            v = mvs / (lh * lw) if lh and lw else 0
            return {"verdict": "потолок скорости", "value": round(v), "unit": "мм/с", "rule": "",
                    "detail": f"{mvs} мм³/с на слое {lh} мм и ширине линии {lw} мм ≈ "
                              f"{round(v)} мм/с."}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {"verdict": "", "value": None, "unit": "", "rule": "",
                "detail": "Не хватает данных для вывода."}
    return {"verdict": "", "value": None, "unit": "", "rule": "", "detail": ""}


VERDICT_KINDS: tuple[dict, ...] = (
    {"key": "wall_thickness", "label": "Толщина стенки: чистая зона vs рябь",
     "fields": ({"k": "clean", "l": "Чистая зона, мм"}, {"k": "ripple", "l": "Зона ряби, мм"},
                {"k": "top_mvs", "l": "Расход на ряби, мм³/с"}), "test": "mvs"},
    {"key": "cube_xy", "label": "Куб: X и Y",
     "fields": ({"k": "x", "l": "X, мм"}, {"k": "y", "l": "Y, мм"},
                {"k": "nominal", "l": "Номинал, мм"}), "test": "cube"},
    {"key": "vfa_pitch", "label": "Шаг полос VFA",
     "fields": ({"k": "pitch", "l": "Шаг, мм"},), "test": "vfa"},
    {"key": "mvs_window", "label": "Клин расхода: границы чистого окна",
     "fields": ({"k": "low_mm", "l": "Низ окна, мм высоты"},
                {"k": "high_mm", "l": "Верх окна, мм высоты"},
                {"k": "base", "l": "Основание, мм³/с"}, {"k": "step", "l": "Шаг, мм³/с на мм"}),
     "test": "mvs"},
    {"key": "flow_yolo", "label": "YOLO: текущий поток + добавка",
     "fields": ({"k": "base", "l": "Текущий поток"}, {"k": "delta", "l": "Добавка с плашки"}),
     "test": "flow"},
    {"key": "retract_window", "label": "Откат: границы чистого окна",
     "fields": ({"k": "low", "l": "Низ окна, мм"}, {"k": "high", "l": "Верх окна, мм"}),
     "test": "retract"},
    {"key": "ironing", "label": "Глажка: что видно на верхней поверхности",
     "fields": ({"k": "flow", "l": "Текущий поток, %"}, {"k": "speed", "l": "Текущая скорость, мм/с"},
                {"k": "symptom", "l": "Симптом: недолив / перелив / шершаво / ок"}), "test": "ironing"},
    {"key": "speed_from_mvs", "label": "Во что расход превращается по скорости",
     "fields": ({"k": "mvs", "l": "Расход, мм³/с"}, {"k": "layer_height", "l": "Слой, мм"},
                {"k": "line_width", "l": "Ширина линии, мм"}), "test": "mvs"},
)


# ── Пресеты слайсера ───────────────────────────────────────────────────────────────────────
def user_root() -> Path:
    """Корень ПОЛЬЗОВАТЕЛЬСКИХ пресетов слайсера (там живёт результат калибровки)."""
    return Path(config.SLICER_USER_DIR)


def preset_dirs(kind: str) -> list[Path]:
    """Папки пресетов вида kind: сначала пользовательские (калибровки), потом системные."""
    k = (kind or "").strip().lower()
    if k not in PRESET_KINDS:
        return []
    out = [user_root() / k]
    sys_dir = {"filament": config.FILAMENT_DIR, "process": config.PROCESS_DIR,
               "machine": Path(config.MACHINE_PROFILE).parent}[k]
    out.append(Path(sys_dir))
    return [d for d in out if d.exists()]


def read_preset(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.debug("preset read failed: %s", path, exc_info=True)
        return {}


def preset_get(data: dict, key: str):
    """Значение параметра из пресета. Пер-экструдерные параметры — массив строк (берём первый),
    скалярные — строка. Наружу отдаём как есть (строкой), число получает вызывающий."""
    v = data.get(key)
    if isinstance(v, list):
        return v[0] if v else None
    return v


def preset_num(data: dict, key: str) -> float | None:
    """Число из значения пресета. Часть параметров слайсер хранит строкой с процентом
    (`ironing_flow: "19%"`) — процент снимаем, единица подразумевается самим параметром."""
    v = preset_get(data, key)
    try:
        return float(str(v).strip().rstrip("%").strip())
    except (TypeError, ValueError):
        return None


def preset_is_pct(data: dict, key: str) -> bool:
    v = preset_get(data, key)
    return isinstance(v, str) and v.strip().endswith("%")


_index_memo: dict = {}   # {"key": <снимок корней>, "index": {имя: файл}} — см. _index_presets


def _index_presets() -> dict[str, Path]:
    """Индекс «имя пресета → файл» по пользовательским И системным профилям.

    Нужен для слияния inherits: пользовательский пресет — это лист-дельта из 8 строк, который
    наследует системный (`"inherits": "Elegoo PLA Matte @ECC2"`). slicer._flatten_profile ищет
    родителей только в пределах папки вендора и пользовательские листы не разрешает.
    """
    # Третий источник (хвост C2) — обнаруженные корни движка, включая %APPDATA%/<Слайсер>/system:
    # туда слайсер кладёт ОБНОВЛЁННЫЕ профили поставки, и на машине, где установочный каталог
    # другой версии (или его нет вовсе), родитель пользовательского листа найдётся только там.
    roots = [user_root(), Path(config.PROFILES_DIR), *slicers.profile_roots().values()]
    # Обход стоит дорого: с корнями движка это ~11 тысяч .json (у system и resources содержимое
    # дублируется), 0.3 с на вызов, а зовётся он на КАЖДУЮ запись значения пресета. Держим
    # разобранный индекс в памяти и пересобираем, когда сменился набор корней или mtime любого
    # из них (слайсер обновился/поставили пресет) — иначе платили бы за одно и то же.
    key = tuple((str(r), r.stat().st_mtime_ns if r.exists() else 0) for r in roots)
    if _index_memo.get("key") == key:
        return _index_memo["index"]
    index: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*.json"):
            index.setdefault(f.stem, f)
    _index_memo.update(key=key, index=index)
    return index


def flatten_preset(path: str | Path, _index: dict[str, Path] | None = None) -> dict:
    """Расплющить цепочку inherits пресета в самодостаточный набор значений."""
    index = _index if _index is not None else _index_presets()
    chain: list[dict] = []
    node: Path | None = Path(path)
    seen: set[str] = set()
    while node is not None and Path(node).exists():
        d = read_preset(node)
        if not d:
            break
        chain.append(d)
        parent = d.get("inherits")
        if not parent or parent in seen:
            break
        seen.add(str(parent))
        node = index.get(str(parent))
    merged: dict = {}
    for d in reversed(chain):
        merged.update(d)
    merged.pop("inherits", None)
    return merged


def list_presets(kind: str | None = None, user_only: bool = False) -> list[dict]:
    """Пресеты слайсера: {kind, name, path, user, mtime}. Пользовательские — первыми."""
    out: list[dict] = []
    for k in ([kind] if kind else list(PRESET_KINDS)):
        for i, d in enumerate(preset_dirs(k or "")):
            is_user = i == 0 and str(d).startswith(str(user_root()))
            if user_only and not is_user:
                continue
            for f in sorted(d.glob("*.json")):
                try:
                    mtime = int(f.stat().st_mtime)
                except OSError:
                    mtime = 0
                out.append({"kind": k, "name": f.stem, "path": str(f), "user": 1 if is_user else 0,
                            "mtime": mtime})
    out.sort(key=lambda x: (-x["user"], x["kind"], x["name"].lower()))
    return out


def set_preset_value(path: str | Path, key: str, value) -> bool:
    """Записать значение в пресет, сохранив форму файла.

    Пер-экструдерные параметры слайсер хранит массивом строк (`["0.5"]`), скалярные — строкой.
    Ключа может не быть вовсе (пользовательский лист содержит только правленые параметры) —
    тогда решаем по тому, как параметр выглядит в РОДИТЕЛЕ: иначе слайсер молча не подхватит
    значение неверной формы. Формат файла — 4 пробела, сортировка ключей, как пишет сам слайсер.
    """
    p = Path(path)
    data = read_preset(p)
    if not data:
        return False
    cur = data.get(key)
    if cur is None:
        parent = flatten_preset(p)
        cur = parent.get(key)
    # Процентные параметры (глажка: ironing_flow="19%"): голое «19» слайсер прочитает как
    # 19 мм³ или проигнорирует — знак процента обязателен, если он был у прежнего значения.
    sample = cur[0] if isinstance(cur, list) and cur else cur
    pct = isinstance(sample, str) and sample.strip().endswith("%")
    txt = str(value)
    if pct and not txt.endswith("%"):
        txt += "%"
    data[key] = [txt] if isinstance(cur, list) else txt
    try:
        p.write_text(json.dumps(data, indent=4, ensure_ascii=False, sort_keys=True) + "\n",
                     encoding="utf-8")
        return True
    except OSError:
        log.warning("preset write failed: %s", p, exc_info=True)
        return False


# ── Паспорт филамента ──────────────────────────────────────────────────────────────────────
# Поле паспорта → (вид пресета, ключ параметра слайсера). Паспорт — это проекция трёх пресетов
# в одну карточку: пресеты остаются рабочим форматом слайсера, паспорт — тем, что переживёт
# обновление слайсера и попадёт в бэкап вместе с БД.
PASSPORT_FIELDS: dict[str, tuple[str, str]] = {
    "temp": ("filament", "nozzle_temperature"),
    "temp_first": ("filament", "nozzle_temperature_initial_layer"),
    "bed": ("filament", "hot_plate_temp"),
    # У CC2 помимо горячей плиты в ходу ДВА ключа стола: PLA Matte калибровался с типом стола
    # «Cool Plate» (45 °C в cool_plate_temp, 27.07.2026), Elegoo PETG — с «Textured PEI»
    # (80 °C в textured_plate_temp, 03.08.2026). Без любого из ключей паспорт показывал 0 °C.
    "bed_cool": ("filament", "cool_plate_temp"),
    "bed_textured": ("filament", "textured_plate_temp"),
    "flow": ("filament", "filament_flow_ratio"),
    "pa": ("filament", "pressure_advance"),
    "mvs": ("filament", "filament_max_volumetric_speed"),
    "retract": ("machine", "retraction_length"),
    "retract_speed": ("machine", "retraction_speed"),
    "outer_speed": ("process", "outer_wall_speed"),
    # Глажка — часть результата калибровки: поток и скорость настраиваются вместе, шаг и тип
    # дописываем рядом, чтобы паспорт восстанавливал режим целиком, а не два числа из четырёх.
    "ironing_flow": ("process", "ironing_flow"),
    "ironing_speed": ("process", "ironing_speed"),
    "ironing_spacing": ("process", "ironing_spacing"),
}

PASSPORT_LABELS: dict[str, tuple[str, str]] = {
    "temp": ("Температура сопла", "°C"),
    "temp_first": ("Температура 1-го слоя", "°C"),
    "bed": ("Стол (горячая плита)", "°C"),
    "bed_cool": ("Стол (холодная плита)", "°C"),
    "bed_textured": ("Стол (текстурированная PEI)", "°C"),
    "flow": ("Коэффициент потока", ""),
    "pa": ("Pressure Advance", ""),
    "mvs": ("Макс. объёмный расход", "мм³/с"),
    "retract": ("Откат", "мм"),
    "retract_speed": ("Скорость отката", "мм/с"),
    "outer_speed": ("Скорость наружного периметра", "мм/с"),
    "ironing_flow": ("Поток глажки", "%"),
    "ironing_speed": ("Скорость глажки", "мм/с"),
    "ironing_spacing": ("Шаг глажки", "мм"),
}


def passport_from_presets(paths: dict[str, str | Path]) -> dict:
    """Собрать паспорт из путей к пресетам {filament, machine, process}.

    Значения берём ИЗ РАСПЛЮЩЕННОГО пресета: пользовательский лист содержит только правленые
    параметры, а паспорт должен показывать полную картину — что откалибровано лично и что
    досталось от системного родителя. Поэтому рядом с числом отдаём source: own | inherited.
    """
    index = _index_presets()
    flat: dict[str, dict] = {}
    own: dict[str, dict] = {}
    for kind, p in (paths or {}).items():
        if not p:
            continue
        flat[kind] = flatten_preset(p, index)
        own[kind] = read_preset(p)
    out: dict = {"values": {}, "source": {}, "presets": {k: str(v) for k, v in (paths or {}).items() if v}}
    for fkey, (kind, pkey) in PASSPORT_FIELDS.items():
        if kind not in flat:
            continue
        val = preset_num(flat[kind], pkey)
        if val is None:
            continue
        out["values"][fkey] = val
        out["source"][fkey] = "own" if preset_get(own.get(kind, {}), pkey) is not None else "inherited"
    return out


# ── Снимки пресетов (история изменений) ────────────────────────────────────────────────────
def snapshot(path: str | Path) -> dict | None:
    """Снимок файла пресета: {kind, name, path, sha, body, mtime}.

    Тело храним целиком: пресет — это десяток строк, зато снимок переживает и обновление
    слайсера (которое возвращает пресеты к стоку), и случайное «Сбросить».
    """
    p = Path(path)
    try:
        body = p.read_text(encoding="utf-8")
        mtime = int(p.stat().st_mtime)
    except OSError:
        return None
    kind = ""
    for k in PRESET_KINDS:
        if p.parent.name.lower() == k:
            kind = k
            break
    return {"kind": kind, "name": p.stem, "path": str(p), "mtime": mtime,
            "sha": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16], "body": body}


def snapshot_user_presets() -> list[dict]:
    """Снимки ВСЕХ пользовательских пресетов — ровно то, что теряется при обновлении слайсера."""
    out = []
    for item in list_presets(user_only=True):
        s = snapshot(item["path"])
        if s:
            s["kind"] = s["kind"] or item["kind"]
            out.append(s)
    return out


def diff_presets(old_body: str, new_body: str) -> list[dict]:
    """Построчный дифф двух снимков по КЛЮЧАМ пресета (а не по тексту): [{key, old, new}]."""
    try:
        a = json.loads(old_body or "{}")
        b = json.loads(new_body or "{}")
    except (TypeError, ValueError):
        return []

    def _v(d, k):
        v = d.get(k)
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return None if v is None else str(v)

    keys = sorted(set(a) | set(b))
    out = []
    for k in keys:
        va, vb = _v(a, k), _v(b, k)
        if va != vb:
            out.append({"key": k, "old": va, "new": vb})
    return out


# ── Подсказка по фото башни ────────────────────────────────────────────────────────────────
def analyze_tower(photo: str | Path, bands: int = 20, base: float | None = None,
                  step: float | None = None, height_mm: float | None = None) -> dict:
    """Грубая оценка «где на башне рябь» по фотографии.

    Считаем по полосам снизу вверх горизонтальную «шершавость»: средний модуль второй разности
    яркости вдоль строки. Ровная стенка даёт низкое значение, рябь (что недоэкструзия, что
    резонанс) — высокое. РАЗЛИЧИТЬ ИХ ЭТО НЕ МОЖЕТ и не пытается: различает только замер
    толщины стенки (см. DIAGNOSTICS['underextrusion']). Задача метрики — подсказать, ГДЕ мерить.

    base/step/height_mm заданы → к каждой полосе добавляется расход по формуле клина.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:      # numpy/Pillow — базовые зависимости, но не делаем модуль обязательным
        return {"ok": False, "error": "нужны numpy и Pillow"}
    p = Path(photo)
    if not p.exists():
        return {"ok": False, "error": "файл не найден"}
    try:
        img = Image.open(p).convert("L")
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "не удалось прочитать изображение"}
    if img.width > 900:                      # мельче — быстрее и меньше шума матрицы
        img = img.resize((900, max(1, int(img.height * 900 / img.width))))
    a = np.asarray(img).astype("float32")
    h, w = a.shape
    if h < bands * 2 or w < 8:
        return {"ok": False, "error": "слишком маленькое изображение"}
    # вторая разность вдоль строки — «шершавость» без реакции на плавный градиент освещения
    rough = np.abs(a[:, 2:] - 2 * a[:, 1:-1] + a[:, :-2]).mean(axis=1)
    edges = np.linspace(0, h, bands + 1).astype(int)
    vals = [float(rough[edges[i]:edges[i + 1]].mean()) if edges[i + 1] > edges[i] else 0.0
            for i in range(bands)]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    med = float(sorted(vals)[len(vals) // 2])
    thr = med + 0.35 * rng                   # порог «здесь заметно шершавее среднего»
    out_bands = []
    for i, v in enumerate(vals):
        # полосы фото идут сверху вниз, башня растёт снизу вверх → инвертируем
        frac_lo = (bands - 1 - i) / bands
        frac_hi = (bands - i) / bands
        item = {"band": bands - i, "from_pct": round(frac_lo * 100), "to_pct": round(frac_hi * 100),
                "rough": round(v, 2), "norm": round((v - lo) / rng, 3), "ripple": bool(v > thr)}
        if height_mm:
            item["height_mm"] = round(frac_lo * float(height_mm), 1)
            if base is not None and step is not None:
                item["mvs"] = mvs_at_height(item["height_mm"], base, step)
        out_bands.append(item)
    out_bands.reverse()                      # снизу вверх, как на самой башне
    clean = [b for b in out_bands if not b["ripple"]]
    window = {}
    if clean:
        window = {"from_pct": clean[0]["from_pct"], "to_pct": clean[-1]["to_pct"]}
        if height_mm:
            window["from_mm"] = clean[0].get("height_mm")
            window["to_mm"] = clean[-1].get("height_mm")
            if base is not None and step is not None:
                window["mvs_from"] = clean[0].get("mvs")
                window["mvs_to"] = clean[-1].get("mvs")
    return {"ok": True, "bands": out_bands, "window": window, "median": round(med, 2),
            "threshold": round(thr, 2),
            "note": "Метрика показывает, ГДЕ рябь, но не отвечает, недоэкструзия это или "
                    "резонанс — это решает замер толщины стенки."}


# ── Дрейф (регресс-эталон) ─────────────────────────────────────────────────────────────────
def drift_summary(rows: list[dict]) -> dict:
    """Сводка по повторяющимся замерам эталона: тренд и разброс.

    rows: [{at, value}] по одному виду замера. Даёт (первое, последнее, дельта, размах, sigma) —
    этого достаточно, чтобы увидеть уползание размеров до того, как оно станет браком.
    """
    vals = [float(r["value"]) for r in rows if r.get("value") is not None]
    if not vals:
        return {}
    n = len(vals)
    mean = sum(vals) / n
    sigma = math.sqrt(sum((v - mean) ** 2 for v in vals) / n) if n > 1 else 0.0
    return {"n": n, "first": vals[0], "last": vals[-1], "delta": round(vals[-1] - vals[0], 3),
            "min": min(vals), "max": max(vals), "mean": round(mean, 3), "sigma": round(sigma, 4)}
