"""Симулятор принтера: тестовое окружение без железа (OpenTodo 0613014e).

Зачем он нужен по-настоящему: вся автоматика мастерской — журнал печатей, привязка печати к
заказу, скрин стола на финише, свип таймлапсов, автофиниш заказа — срабатывает ТОЛЬКО в
момент, когда принтер заканчивает печатать. Проверить это на живом CC2 значит ждать час и
потратить пластик; проверить на втором принтере, которого нет, — вообще никак. Поэтому здесь
принтер, который печатает по команде «прошло N минут».

Симулятор не притворяется протоколом: он реализует тот же интерфейс драйвера, что и
настоящие. Это осознанный размен — мы проверяем НАШУ логику (что журнал завёлся, что печать
привязалась к заказу, что способности уважаются), а не чужой MQTT. Разбор протокола
проверяется отдельно, на захватах в docs/captures.

    p = FakePrinter()
    p.begin("ECC2_0.4_Кот_PLA_0.2.gcode", minutes=42, grams=13.5)
    p.advance(minutes=42)          # печать прошла
    p.finish()                     # принтер снова в простое — printwatch увидит финиш
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .base import Caps, PrinterDriver


@dataclass
class FakeState:
    """Состояние железа, которым управляет тест."""
    printing: bool = False
    # Что прошивка пишет в статусе, пока задание не закончено. Половина сценариев автоматики —
    # это НЕ happy path: пауза мастером, отвал термистора, долгий прогрев перед первым слоем.
    # Без фаз их нечем выразить, и проверялась только идеальная печать.
    phase: str = "printing"       # printing | paused | heating | error
    job: str = ""
    minutes_total: float = 0.0
    minutes_done: float = 0.0
    grams: float = 0.0
    layer: int = 0
    layers_total: int = 0
    nozzle: float = 25.0
    nozzle_target: float = 0.0
    bed: float = 25.0
    bed_target: float = 0.0
    light: bool = False
    files: dict = field(default_factory=dict)     # имя → {grams, print_time}
    history: list = field(default_factory=list)
    clock: float = 1_700_000_000.0                # «часы принтера»: растут только по finish()
    commands: list = field(default_factory=list)  # что мы принтеру наприказывали (для проверок)
    speed_mode: int = 1                           # 1031: 0 тихий … 3 безумный
    auto_refill: bool = False                     # 2004
    usb_files: dict = field(default_factory=dict)  # флешка: имя → {grams, print_time, colors}


class FakePrinter(PrinterDriver):
    """Принтер, у которого время идёт по команде теста.

    По умолчанию умеет ВСЁ, что умеет Elegoo: так тест ловит регрессии автоматики. Чтобы
    проверить изящную деградацию на бедном принтере, способности сужаются:

        FakePrinter(caps=Caps(status=True, upload=True, start=True))
    """

    kind = "fake"
    title = "Симулятор принтера"

    def __init__(self, printer=None, *, caps: Caps | None = None, connected: bool = True):
        super().__init__(printer)
        self.caps = caps or Caps(status=True, files=True, upload=True, start=True, control=True,
                                 move=True, filament=True, camera=True, history=True,
                                 timelapse=True, delete=True, speed_mode=True,
                                 auto_refill=True, usb=True)
        self.st = FakeState()
        self.connected = connected
        # Прошивка сообщества «на симуляторе»: тест кладёт сюда ответ discovery (as_dict) и
        # подменяет сеть app/cc2community — так ручки проверяются без настоящей службы.
        self.community_info: dict | None = None

    # ── управление симуляцией (зовёт ТЕСТ, не продакшен-код) ───────────────────────────────
    def begin(self, job: str, *, minutes: float = 30.0, grams: float = 10.0,
              layers: int = 100) -> None:
        """Принтер начал печатать задание."""
        self.st.printing = True
        self.st.phase = "printing"
        self.st.job = job
        self.st.minutes_total = float(minutes)
        self.st.minutes_done = 0.0
        self.st.grams = float(grams)
        self.st.layers_total = int(layers)
        self.st.layer = 0
        self.st.nozzle, self.st.nozzle_target = 215.0, 215.0
        self.st.bed, self.st.bed_target = 60.0, 60.0
        self.st.files.setdefault(job, {"grams": grams, "print_time": minutes * 60})

    def pause(self) -> None:
        """Мастер поставил печать на паузу: задание не закончено, но время не идёт."""
        if self.st.printing:
            self.st.phase = "paused"

    def resume(self) -> None:
        if self.st.printing:
            self.st.phase = "printing"

    def heating(self) -> None:
        """Прогрев перед первым слоем: задание уже принято, но печать ещё не пошла."""
        if self.st.printing:
            self.st.phase = "heating"

    def error(self, note: str = "термистор") -> None:
        """Принтер встал с ошибкой, задание не завершено. Печать НЕ считается активной."""
        if self.st.printing:
            self.st.phase = "error"
            self.st.commands.append(("error", note))

    def advance(self, minutes: float) -> None:
        """Прошло время. Прогресс и слой двигаются пропорционально — как в телеметрии.

        На паузе, прогреве и ошибке время НЕ идёт: иначе тест «пауза не двигает журнал»
        проверял бы противоположное тому, что написано в его названии."""
        if not self.st.printing or self.st.phase != "printing":
            return
        self.st.minutes_done = min(self.st.minutes_total, self.st.minutes_done + float(minutes))
        if self.st.minutes_total > 0:
            share = self.st.minutes_done / self.st.minutes_total
            self.st.layer = int(self.st.layers_total * share)

    def finish(self, *, ok: bool = True) -> None:
        """Печать закончилась: принтер в простое. Именно этот переход ловит printwatch."""
        if self.st.job:
            # Поля end_time и time_lapse_* — не украшение: свип таймлапсов ищет запись по
            # времени окончания и решает по статусу видео, готово ли оно. Без них половина
            # автоматики финиша на симуляторе невыразима.
            self.st.clock += 1.0
            self.st.history.insert(0, {"name": self.st.job, "ok": ok,
                                       "duration_sec": int(self.st.minutes_done * 60),
                                       "status": "done" if ok else "failed",
                                       "end_time": int(self.st.clock),
                                       "time_lapse_video_url":
                                           f"http://127.0.0.1:9/tl/{self.st.job}.mp4" if ok else "",
                                       "time_lapse_video_status": 1 if ok else 0})
        self.st.printing = False
        self.st.phase = "printing"
        self.st.nozzle_target = self.st.bed_target = 0.0

    def disconnect(self) -> None:
        """Принтер пропал из сети (проверка, что бот это переживает)."""
        self.connected = False

    # ── интерфейс драйвера ─────────────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        return True

    def status(self, max_age: float = 6.0) -> dict | None:
        if not self.connected or not self.caps.status:
            return None
        s = self.st
        progress = (s.minutes_done / s.minutes_total) if s.minutes_total else 0.0
        # В простое остаток НУЛЕВОЙ, а фактическое время печати сохраняется — ровно так
        # выглядят кадры прошивки после законченной печати в docs/captures (print_duration
        # 2991, remaining_time_sec 0, пустое имя файла). Если оставить остаток ненулевым,
        # enrich_status продолжит считать печать активной и финиш не наступит никогда.
        left = max(0.0, s.minutes_total - s.minutes_done) if s.printing else 0.0
        return {"ok": True, "state": {
            "print_status": {
                "status": s.phase if s.printing else "idle",
                "filename": s.job if s.printing else "",
                # Имена полей — как у настоящего SDCP: тест проверяет наш разбор, а не себя.
                "total_time": int(s.minutes_total * 60),
                "print_time": int(s.minutes_done * 60),
                # print_duration/remaining_time_sec — ТЕ САМЫЕ поля, по которым
                # elegoo.enrich_status решает, что печать активна. Без них симулятор печатал,
                # а вся автоматика видела простой: проверять на нём было нечего (разбор 11.08).
                "print_duration": int(s.minutes_done * 60),
                "remaining_time_sec": int(left * 60),
                "current_layer": s.layer,
                "total_layer": s.layers_total,
                "filament_used": s.grams,
            },
            "virtual_sdcard": {"progress": round(progress, 4)},
            "info": {"current_layer": s.layer, "total_layer": s.layers_total},
            "temperature": {"nozzle": s.nozzle, "nozzle_target": s.nozzle_target,
                            "bed": s.bed, "bed_target": s.bed_target},
            "led": {"status": 1 if s.light else 0},
        }}

    def enrich(self, state: dict) -> dict:
        """Overlay — тем же кодом, что и у настоящих драйверов.

        Раньше здесь возвращалась ПЛОСКАЯ сводка (`{printing, filename, progress, …}`) — своя
        форма, которой нет ни у одного реального драйвера. Симулятор, отличающийся от
        оригинала контрактом, проверяет не продукт, а себя: автоматика, читающая overlay,
        на нём вела бы себя иначе, чем на живом принтере. Теперь состояние симулятора
        SDCP-образное (см. status), и разбирает его тот же elegoo.enrich_status."""
        from .. import elegoo
        return elegoo.enrich_status(state or {})

    def files(self, timeout: float = 8.0) -> dict:
        if not self.caps.files:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        return {"ok": True, "files": [
            {"name": n, "grams": v.get("grams", 0), "print_time": v.get("print_time", 0)}
            for n, v in self.st.files.items()]}

    async def upload(self, path, *, name: str | None = None) -> dict:
        if not self.caps.upload:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        import os
        nm = name or os.path.basename(str(path))
        self.st.files[nm] = {"grams": 0, "print_time": 0}
        self.st.commands.append(("upload", nm))
        return {"ok": True, "name": nm}

    def start(self, filename: str, **kw) -> dict:
        if not self.caps.start:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        if self.st.printing:
            return {"ok": False, "error": "Принтер занят"}
        self.st.commands.append(("start", filename, kw))
        meta = self.st.files.get(filename) or {}
        self.begin(filename, minutes=(meta.get("print_time") or 1800) / 60,
                   grams=meta.get("grams") or 10)
        return {"ok": True}

    def delete_file(self, filename: str) -> dict:
        if not self.caps.delete:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.files.pop(filename, None)
        self.st.commands.append(("delete", filename))
        return {"ok": True}

    def file_grams(self, filename: str) -> float | None:
        v = self.st.files.get(filename)
        return float(v["grams"]) if v and v.get("grams") else None

    def control(self, action: str, *, on: bool = True) -> dict:
        if not self.caps.control:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("control", action, on))
        if action == "stop":
            self.finish(ok=False)
        elif action == "light":
            self.st.light = bool(on)
        return {"ok": True}

    def set_temperature(self, target: str, value: int) -> dict:
        if not self.caps.move:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("temp", target, value))
        if target == "extruder":
            self.st.nozzle_target = float(value)
        else:
            self.st.bed_target = float(value)
        return {"ok": True}

    def home(self, axes: str = "xyz") -> dict:
        if not self.caps.move:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("home", axes))
        return {"ok": True}

    def move_axis(self, axis: str, distance: float) -> dict:
        if not self.caps.move:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("move", axis, distance))
        return {"ok": True}

    def set_fan(self, which: str, pct: int) -> dict:
        if not self.caps.move:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("fan", which, pct))
        return {"ok": True}

    def load_filament(self, slot: int) -> dict:
        if not self.caps.filament:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("load", slot))
        return {"ok": True}

    def unload_filament(self, slot: int) -> dict:
        if not self.caps.filament:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("unload", slot))
        return {"ok": True}

    def set_speed_mode(self, mode: int) -> dict:
        if not self.caps.speed_mode:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("speed_mode", int(mode)))
        self.st.speed_mode = int(mode)
        return {"ok": True}

    def set_auto_refill(self, on: bool) -> dict:
        if not self.caps.auto_refill:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("auto_refill", bool(on)))
        self.st.auto_refill = bool(on)
        return {"ok": True}

    def _store(self, storage: str) -> dict:
        return self.st.usb_files if storage == "u-disk" else self.st.files

    def file_info(self, filename: str, *, storage: str = "local") -> dict:
        meta = self._store(storage).get(filename)
        if meta is None:
            return {"ok": False, "error": "Файл для печати не найден на принтере (код 1021)"}
        return {"ok": True, "file": {"name": filename, "grams": meta.get("grams", 0),
                                     "print_time": meta.get("print_time", 0),
                                     "layer": meta.get("layer", 0),
                                     "colors": list(meta.get("colors") or [])}}

    def files_on(self, storage: str, timeout: float = 8.0) -> dict:
        if storage != "u-disk" or not self.caps.usb:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        return {"ok": True, "files": [
            {"name": n, "grams": v.get("grams", 0), "print_time": v.get("print_time", 0),
             "colors": list(v.get("colors") or [])}
            for n, v in self.st.usb_files.items()]}

    def delete_files(self, names: list[str], *, storage: str = "local") -> dict:
        if not self.caps.delete:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        store = self._store(storage)
        for n in names:
            store.pop(n, None)
        self.st.commands.append(("delete_many", storage, list(names)))
        return {"ok": True}

    def community(self) -> dict | None:
        return self.community_info

    def community_host(self) -> str:
        return "fake-cc2.local" if self.community_info else ""

    def camera_url(self) -> str:
        return "http://127.0.0.1:9/fake.mjpeg" if self.caps.camera else ""

    def history(self, timeout: float = 8.0) -> dict:
        if not self.caps.history:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        # Ключ ИМЕННО "jobs": так его читает printwatch (свип таймлапсов и привязка). Раньше
        # симулятор отдавал "items", и весь этот путь на нём молча получал пустой список —
        # то есть проверялся как «ничего не нашлось», а не как «нашлось и обработано».
        return {"ok": True, "jobs": list(self.st.history)}

    def latest_timelapse_url(self, job_name: str | None = None) -> dict:
        if not self.caps.timelapse:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        if not self.st.history:
            return {"ok": False, "error": "Заданий не было"}
        return {"ok": True, "url": f"http://127.0.0.1:9/tl/{self.st.history[0]['name']}.mp4"}

    def generate_timelapse(self, url: str) -> dict:
        if not self.caps.timelapse:
            return {"ok": False, "error": "Этот принтер так не умеет"}
        self.st.commands.append(("timelapse", url))
        return {"ok": True, "url": url}
