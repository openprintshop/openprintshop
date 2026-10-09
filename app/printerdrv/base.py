"""Драйвер принтера: единственный слой, ниже которого живут протоколы (OpenTodo 0613014e).

Задача, которую он решает, — не «поддержать Bambu», а убрать скрытую связку: сегодня
`printwatch` и половина ручек `webapp` зовут `app/elegoo.py` НАПРЯМУЮ, то есть вся автоматика
мастерской (журнал печатей, привязка к заказам, скрины стола, таймлапсы, автофиниш) молча
считает, что принтер — это Centauri Carbon. Пока принтер один, это незаметно; второй принтер
превращает каждое такое место в развилку по вендору.

Здесь описан ОДИН интерфейс и — главное — честный набор способностей (`Caps`). Способности
важнее методов: у Klipper нет таймлапсов нашего вида, у «принтера только для слайса» нет
вообще ничего, и правильный ответ на это — не заглушка, которая делает вид, что работает, а
явное «не умею», по которому интерфейс прячет кнопку, а printwatch пропускает шаг.

Драйверы НЕ переписывают протоколы: `elegoo.py` и `moonraker.py` остаются как есть, драйвер —
тонкая обёртка над ними. Это сознательно: переписывание разведанного протокола ради красоты
архитектуры — верный способ потерять то, что годами работало.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace  # noqa: F401  (replace — для драйверов)
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Caps:
    """Что этот принтер умеет. Всё по умолчанию False: новый драйвер, забывший объявить
    способность, ничего не сломает — интерфейс просто не покажет кнопку."""
    status: bool = False          # телеметрия (температуры, прогресс, слой)
    files: bool = False           # список файлов в памяти принтера
    upload: bool = False          # заливка задания
    start: bool = False           # старт печати файла
    control: bool = False         # пауза/продолжить/стоп/свет
    move: bool = False            # ручное управление: оси, прогрев, вентиляторы
    filament: bool = False        # загрузка/выгрузка филамента
    camera: bool = False          # видеопоток
    history: bool = False         # история заданий принтера
    timelapse: bool = False       # таймлапсы (сборка и скачивание)
    exclude_object: bool = False  # снять деталь со стола, не останавливая печать (см. docs)
    # УДАЛЕНИЕ файла — отдельно от чтения списка. Раньше ручка удаления гейтилась флагом
    # `files`, то есть способностью ПОКАЗАТЬ список: принтер, умеющий показать, но не умеющий
    # удалить, пропускал кнопку и отвечал отказом. Правило: каждый новый флаг в том же коммите
    # объявляется у Elegoo как True, иначе у мастера пропадёт рабочая кнопка.
    delete: bool = False
    # Стоковые команды CC2 из официального SDK вендора (elegoo-link): 1031 и 2004. Флешка —
    # второй носитель файлов (список, старт, удаление с storage_media «u-disk»).
    speed_mode: bool = False      # режим скорости печати: тихий / баланс / спорт / безумный
    auto_refill: bool = False     # автодозаправка хаба при кончившейся катушке
    usb: bool = False             # файлы на флешке принтера
    # Только с прошивкой сообщества (Neme77/centauri-carbon-2-community, app/cc2community.py):
    # у стоковой прошивки таких команд нет вовсе. Elegoo включает их сам, когда находит её API,
    # поэтому правило «каждый флаг — True у Elegoo» к ним не относится: они честно False, пока
    # прошивки нет. exclude_object выше — из той же группы.
    bed_mesh: bool = False        # карта стола, калибровка сетки, замер винтов
    live_tune: bool = False       # живой Z-offset, скорость и поток в % во время печати
    gcode: bool = False           # консоль G-code, моторы, ручная экструзия, аварийный M112
    slot_edit: bool = False       # правка материала, цвета и температур слота хаба
    thumbs: bool = False          # превью из самого G-code на принтере (их /api/gcode-files/thumbnail)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


UNSUPPORTED = {"ok": False, "error": "Этот принтер так не умеет"}


def _sub(d: dict, key: str) -> dict:
    """Вложенный словарь или пустой. Телеметрия приходит от чужой прошивки — под ключом может
    оказаться что угодно, включая None и строку."""
    v = (d or {}).get(key)
    return v if isinstance(v, dict) else {}


# Слова прошивок, означающие «машина занята заданием». Сравнение по началу строки: у вендоров
# встречается и 'printing', и 'PRINTING', и 'printing (paused)', и 'pausing'.
BUSY_WORDS = ("printing", "paused", "pausing", "resuming", "heating", "preheat", "busy")


def _state_word(d: dict, key: str) -> str:
    """Слово состояния из блока: у одних вендоров оно в 'state', у других в 'status'."""
    b = _sub(d, key)
    return str(b.get("state") or b.get("status") or "").strip().lower()


class PrinterDriver:
    """База: всё «не умею», кроме того, что драйвер переопределит.

    Синхронные методы блокирующие (сеть) — звать через asyncio.to_thread, ровно как звался
    `elegoo.*` до появления этого слоя. Асинхронной делаем только заливку: у Moonraker она
    уже async и переписывать её в поток было бы вредно."""

    kind = "none"
    title = "Без пульта"
    caps = Caps()
    # Какие расширения принимает заливка. Это не флаг «да/нет», а свойство протокола: Centauri
    # ест .gcode, Bambu — .3mf/.gcode.3mf. Раньше расширение было ЖЁСТКО зашито в ручке заливки,
    # то есть второй вендор упирался бы в проверку, к нему не относящуюся.
    upload_exts: tuple[str, ...] = (".gcode",)

    def __init__(self, printer=None):
        self.printer = printer          # запись db.Printer (может быть None — глобальный режим)

    # ── связь ──────────────────────────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        """Настроен ли принтер (есть адрес/ключи). False → пульт скрыт целиком."""
        return False

    def status(self, max_age: float = 6.0) -> dict | None:
        """Последний известный статус или None. Форма — как у elegoo.cached_status."""
        return None

    def enrich(self, state: dict) -> dict:
        """Статус → OVERLAY ДЛЯ СЛИЯНИЯ с сырым состоянием (прогресс, слой, ETA, флаг печати).

        Контракт именно такой: возвращается ДОБАВКА, а не полная картина, и потребитель обязан
        мержить её поверх сырого состояния (`state.update(drv.enrich(state))`) — так делает
        экран принтера. Читать один только возврат нельзя: у Klipper прогресс лежит в сыром
        состоянии, а overlay пуст, и «читатель возврата» вечно видит простой.

        Именно на этом разъехались потребители (разбор 11.08): экран мержил, а автоматика
        читала возврат. Чтобы больше не выяснять это на живом принтере, для вопроса «печатает
        ли и как далеко» заведён отдельный метод job_state ниже — его и надо звать."""
        return {}

    def job_state(self, state: dict) -> dict:
        """«Идёт ли на машине задание и как далеко» — единственный вопрос для автоматики.

        Возвращает {active, phase, progress, name, minutes}: progress — доля 0..1 или None,
        minutes — сколько уже печатает, или None, phase — слово прошивки как есть.

        ACTIVE ВКЛЮЧАЕТ ПАУЗУ И ПРОГРЕВ, и это осознанно. Все три потребителя спрашивают одно
        и то же — «машина занята заданием»: гард «пока занята, по сети не трогаем» (на паузе
        принтер так же слаб и так же отдаёт камеру), детектор финиша (пауза не должна
        выглядеть как «печать закончилась» и заводить запись в журнал), сторож связи (обрыв на
        паузе — та же беда). Кому нужна деталь «сейчас сопло движется» — тот смотрит phase.
        Разводить это на два флага значило бы повторить ошибку, из-за которой три копии
        одного выражения разъехались с источником данных (разбор 11.08).

        Ошибка печати активной НЕ считается: задание встало, и для журнала это финиш.

        Источников признака НЕСКОЛЬКО, потому что у драйверов они разные и все настоящие:

          • overlay `print_stats.state` — так отдаёт elegoo.enrich_status (ключ синтетический,
            прошивка Centauri его не шлёт, он достраивается из остатка времени и прогресса);
          • сырой `print_stats.state` — родное поле Klipper, если драйвер отдаёт его как есть;
          • сырой `print_status.status` — форма, в которую MoonrakerDriver.status и симулятор
            приводят состояние.

        Ни один из трёх не «на всякий случай»: выкинь любой — и соответствующий принтер
        перестанет считаться печатающим. Сравнение по подстроке: у вендоров встречается и
        'printing', и 'PRINTING', и 'printing (paused)'."""
        raw = state or {}
        merged = dict(raw)
        try:
            merged.update(self.enrich(raw) or {})
        except Exception:  # noqa: BLE001 — вопрос «печатает ли» не должен падать из-за overlay
            log.debug("enrich failed in job_state", exc_info=True)

        # Сырое слово важнее достроенного: overlay умеет сказать только «printing», а прошивка
        # различает паузу, прогрев и ошибку. Порядок — от точного к общему.
        phase = (_state_word(raw, "print_status") or _state_word(raw, "print_stats")
                 or _state_word(merged, "print_stats"))
        active = any(phase.startswith(w) for w in BUSY_WORDS)

        prog = _sub(merged, "virtual_sdcard").get("progress")
        if not isinstance(prog, (int, float)):
            prog = None

        ps_merged = _sub(merged, "print_stats")
        ps_raw = _sub(raw, "print_status")
        name = str(ps_merged.get("filename") or ps_raw.get("filename")
                   or ps_raw.get("task_name") or "").strip()

        secs = ps_raw.get("print_duration")
        if not isinstance(secs, (int, float)):
            secs = ps_raw.get("print_time")
        minutes = round(float(secs) / 60.0, 2) if isinstance(secs, (int, float)) and secs else None

        return {"active": active, "phase": phase,
                "progress": (float(prog) if prog is not None else None),
                "name": name, "minutes": minutes}

    # ── файлы и печать ─────────────────────────────────────────────────────────────────────
    def files(self, timeout: float = 8.0) -> dict:
        return dict(UNSUPPORTED)

    async def upload(self, path: str | Path, *, name: str | None = None) -> dict:
        return dict(UNSUPPORTED)

    def start(self, filename: str, **kw) -> dict:
        return dict(UNSUPPORTED)

    def delete_file(self, filename: str) -> dict:
        return dict(UNSUPPORTED)

    def file_grams(self, filename: str) -> float | None:
        return None

    def normalize_job_name(self, name: str | None) -> str:
        """Каноничный ключ имени задания — по нему сходятся журнал, история и файлы.
        Реализация общая (elegoo.normalize_job_name): формат имён от вендора не зависит."""
        from .. import elegoo
        return elegoo.normalize_job_name(name)

    # ── управление ─────────────────────────────────────────────────────────────────────────
    def control(self, action: str, *, on: bool = True) -> dict:
        return dict(UNSUPPORTED)

    def set_temperature(self, target: str, value: int) -> dict:
        return dict(UNSUPPORTED)

    def home(self, axes: str = "xyz") -> dict:
        return dict(UNSUPPORTED)

    def move_axis(self, axis: str, distance: float) -> dict:
        return dict(UNSUPPORTED)

    def set_fan(self, which: str, pct: int) -> dict:
        return dict(UNSUPPORTED)

    def load_filament(self, slot: int) -> dict:
        return dict(UNSUPPORTED)

    def unload_filament(self, slot: int) -> dict:
        return dict(UNSUPPORTED)

    def set_speed_mode(self, mode: int) -> dict:
        return dict(UNSUPPORTED)

    def set_auto_refill(self, on: bool) -> dict:
        return dict(UNSUPPORTED)

    def file_info(self, filename: str, *, storage: str = "local") -> dict:
        """Детали файла на принтере: филаменты (color_map), время, слои. Для диалога старта."""
        return dict(UNSUPPORTED)

    def thumbnail(self, path: str, *, storage: str = "local") -> tuple[bytes, str] | None:
        """Превью из самого G-code на принтере: (байты, Content-Type) или None — его нет."""
        return None

    def files_on(self, storage: str, timeout: float = 8.0) -> dict:
        """Список файлов на ДРУГОМ носителе (флешка). Внутренняя память — files()."""
        return dict(UNSUPPORTED)

    def delete_files(self, names: list[str], *, storage: str = "local") -> dict:
        """Удалить несколько файлов одной командой. По умолчанию — по одному через delete_file."""
        if storage != "local":
            return dict(UNSUPPORTED)
        last: dict = {"ok": True}
        for n in names:
            last = self.delete_file(n)
            if not last.get("ok"):
                return last
        return last

    # ── прошивка сообщества (caps.bed_mesh / live_tune / gcode / slot_edit / exclude_object) ──
    def community(self) -> dict | None:
        """Сведения о найденной прошивке сообщества или None. Сети не трогает."""
        return None

    def community_host(self) -> str:
        """Адрес, по которому ходить к службе прошивки сообщества."""
        return ""

    # ── глаза ──────────────────────────────────────────────────────────────────────────────
    def camera_url(self) -> str:
        return ""

    def history(self, timeout: float = 8.0) -> dict:
        return dict(UNSUPPORTED)

    def latest_timelapse_url(self, job_name: str | None = None) -> dict:
        return dict(UNSUPPORTED)

    def generate_timelapse(self, url: str) -> dict:
        return dict(UNSUPPORTED)

    # ── служебное ──────────────────────────────────────────────────────────────────────────
    def describe(self) -> dict:
        """Карточка драйвера для интерфейса и статуса: чем говорим и что умеем."""
        out = {"kind": self.kind, "title": self.title, "enabled": self.enabled(),
               "caps": self.caps.as_dict(), "upload_exts": list(self.upload_exts)}
        com = self.community()
        if com:
            out["community"] = com
        return out


class NoneDriver(PrinterDriver):
    """«Принтер только для слайса»: профиль для расчёта, без пульта.

    Это не заглушка на будущее — так в таблице уже живут вторые принтеры, у которых есть
    профили слайсера и нет никакой связи. Раньше это состояние нигде не было названо."""

    kind = "none"
    title = "Только профиль слайсера"
