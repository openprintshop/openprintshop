"""Драйвер Bambu Lab: телеметрия и команды по MQTT/TLS, файлы по FTPS.

Что он умеет: статус и прогресс печати, пауза/продолжить/стоп, список заданий, заливка, удаление,
таймлапсы. Чего не умеет и почему — в `caps` ниже, по строке на каждый отказ.

СПОСОБНОСТИ ОБЪЯВЛЯЮТСЯ ПО ФАКТУ. Сетевая часть написана до приезда машины: MQTT прогнан через
симулятор с настоящим TLS, файловый слой — на подменённом транспорте, но живого принтера не
видел ни один байт. Поэтому пока сессия не приняла ни одного отчёта, `describe()` отдаёт ПУСТОЙ
набор: объяви мы способности заранее, обе админки нарисовали бы кнопки, экран показал бы принтер
сломанным, а журнал завёл бы пустые записи. Ответит машина — интерфейс оживёт сам, без правки
кода.

До появления этого драйвера `kind="bambu"` нельзя было даже сохранить («Неизвестный тип
принтера»), то есть владелец не мог подготовить мастерскую к приезду машины.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from .. import bambu
from .base import Caps, PrinterDriver


class BambuDriver(PrinterDriver):
    kind = "bambu"
    title = "Bambu Lab (MQTT/TLS)"
    # Что протокол умеет В ПРИНЦИПЕ. Наружу этот набор уходит только после того, как машина
    # реально ответила (см. describe): код сетевой части написан до приезда принтера и на живом
    # железе не проверен ни байтом, поэтому обещать по нему нельзя.
    #
    # Чего тут нет и почему:
    #   camera  — у Bambu не MJPEG, а свой поток chamber image по TCP, ретранслятор проекта
    #             такого не умеет; отдать http-URL значило бы вечное «камера не отвечает»;
    #   start   — команда старта требует точной формы задания (плита, слоты AMS), и выдумывать
    #             её вслепую нельзя: ошибка тут = испорченная печать, а не отказ;
    #   history — в локальном режиме Bambu историю заданий не отдаёт, она в облаке. Свип
    #             таймлапсов гейтится именно этим флагом, и ролики берутся по имени файла.
    caps = Caps(status=True, control=True, files=True, upload=True, delete=True, timelapse=True)

    def _address(self) -> str:
        return (getattr(self.printer, "address", "") or "").strip()

    def _code(self) -> str:
        return (getattr(self.printer, "access_code", "") or "").strip()

    def _serial(self) -> str:
        """Серийник обязателен: у Bambu он в ТЕМЕ MQTT, а не в теле запроса.

        Уедет адрес без серийника — команда уйдёт на правильный принтер в тему чужой машины,
        потеряется молча, а вызов отчитается об успехе. Тот же урок, что у Elegoo."""
        return (getattr(self.printer, "serial", "") or "").strip()

    def endpoint(self) -> bambu.Endpoint:
        return bambu.Endpoint(ip=self._address(), code=self._code(), serial=self._serial())

    def session(self):
        return bambu.session_for(self.endpoint())

    def enabled(self) -> bool:
        """Заполнена ли карточка настолько, чтобы к машине было куда идти.

        Серийник в этот вопрос ВХОДИТ, в отличие от Elegoo: у Bambu он в ТЕМЕ MQTT, и без него
        команда ушла бы в никуда, а подписчик не получил бы ни одного отчёта."""
        return bool(self._address() and self._code() and self._serial())

    def describe(self) -> dict:
        """Способности — ПО ФАКТУ, а не по вере.

        Сетевая часть написана до приезда машины и на живом железе не проверена. Объяви мы
        `status`/`control` заранее — обе админки нарисовали бы кнопки, экран показал бы принтер
        сломанным (телеметрия не приходит), а журнал завёл бы пустые записи. Поэтому полный
        набор отдаётся только после того, как сессия приняла ХОТЯ БЫ ОДИН отчёт. Ответит
        машина — интерфейс оживёт сам, без единой правки кода."""
        info = super().describe()
        if not (self.enabled() and self.session().seen_report):
            info["caps"] = Caps().as_dict()
            info["note"] = ("Карточка заведена; способности включатся сами, когда принтер "
                            "ответит первым отчётом. Профили и слайсер можно настроить уже сейчас")
        return info

    def enrich(self, state: dict) -> dict:
        """Отчёт Bambu → форма проекта. Работает уже сегодня — на записи, а не на живой машине."""
        return bambu.enrich_status(state or {})

    def status(self, max_age: float = 6.0) -> dict | None:
        """Снимок отчётов СВОЕЙ сессии.

        None, пока отчётов не было, — и это честный ответ, а не заглушка: словарь с `ok=True` и
        пустым состоянием наблюдатель прочитал бы как «на связи и простаивает», и сторож связи
        не поднял бы тревогу никогда."""
        if not self.enabled():
            return None
        return self.session().cached_status(max_age)

    def control(self, action: str, *, on: bool = True) -> dict:
        """Пауза / продолжить / стоп. Свет у Bambu отдельной командой не управляется тем же
        каналом — честно отказываем, а не делаем вид, что выполнили."""
        cmd = {"pause": "pause", "resume": "resume", "stop": "stop"}.get(action)
        if not cmd:
            return {"ok": False, "error": f"Bambu: команда «{action}» не поддерживается"}
        if not self.enabled():
            return {"ok": False, "error": "Принтер Bambu не настроен"}
        ses = self.session()
        return ses.send({"print": {"sequence_id": ses.next_seq(), "command": cmd}})

    # ── файловая часть: FTPS implicit TLS на :990 ──────────────────────────────────────
    def files(self, timeout: float = 8.0) -> dict:
        """Печатаемые файлы принтера из /cache и /model.

        Таймаут по умолчанию у контракта — 8 с, но FTPS-сервер принтера отвечает медленнее MQTT
        (рукопожатие TLS плюс листинг двух каталогов), поэтому поднимаем нижнюю границу: 8 с
        здесь давали бы «принтер не отвечает» на исправной машине."""
        if not self.enabled():
            return {"ok": False, "error": "Принтер Bambu не настроен"}
        return bambu.list_files(self.endpoint(), max(timeout, 20.0))

    async def upload(self, path, *, name: str | None = None) -> dict:
        """Залить задание в /cache. В потоке: FTPS блокирующий, а зовут его из event loop."""
        if not self.enabled():
            return {"ok": False, "error": "Принтер Bambu не настроен"}
        ep = self.endpoint()
        return await asyncio.to_thread(bambu.upload, ep, path, name or Path(path).name)

    def delete_file(self, filename: str) -> dict:
        if not self.enabled():
            return {"ok": False, "error": "Принтер Bambu не настроен"}
        return bambu.delete_file(self.endpoint(), filename)

    def history(self, timeout: float = 8.0) -> dict:
        """Истории заданий у Bambu в LAN-режиме НЕТ — она живёт в облаке.

        Собрать её из списка файлов нельзя: журнал печатей и свип сверяют ИМЕНА заданий, и
        «история», сложенная из чужого источника, привязала бы к печати чужое видео. Поэтому
        честный отказ, а `caps.history=False` — чтобы свип этой машины даже не пытался."""
        return {"ok": False, "error": "Bambu в локальном режиме историю заданий не отдаёт"}

    def latest_timelapse_url(self, job_name: str | None = None) -> dict:
        """Ролик этого задания (или самый свежий) в /timelapse.

        `url` здесь пуст намеренно: файл лежит на FTPS, а не за HTTP-ссылкой, и скачивается
        `fetch_timelapse`. Отдать сюда выдуманный http-адрес значило бы, что общий код попробует
        качать его обычным запросом и получит вечный таймаут."""
        if not self.enabled():
            return {"ok": False, "error": "Принтер Bambu не настроен"}
        r = bambu.timelapses(self.endpoint())
        if not r.get("ok"):
            return r
        files = r.get("files") or []
        if not files:
            return {"ok": False, "error": "на принтере нет роликов"}
        want = self.normalize_job_name(job_name) if job_name else ""
        if want:
            for f in files:
                # ВИДЕО-расширение снимаем ДО нормализации: ролик зовётся «cat.gcode.mp4», а
                # задание — «cat.gcode», и общий нормализатор снимает только расширения заданий.
                # Без этого совпадение не находилось бы никогда.
                base = re.sub(r"\.(mp4|avi)$", "", f["name"], flags=re.I)
                if self.normalize_job_name(base) == want:
                    return {"ok": True, "url": "", "name": f["name"]}
            return {"ok": False, "error": "ролик этого задания не найден"}
        return {"ok": True, "url": "", "name": files[0]["name"]}   # список уже свежими вперёд

    def fetch_timelapse(self, name: str, dest) -> dict:
        """Скачать ролик по имени в `dest`. Отдельный метод, а не URL: транспорт у Bambu — FTPS."""
        if not self.enabled():
            return {"ok": False, "error": "Принтер Bambu не настроен"}
        return bambu.fetch_timelapse(self.endpoint(), name, dest)

    def generate_timelapse(self, url: str) -> dict:
        """Сборки mp4 по запросу у Bambu нет: он пишет ролик сам по ходу печати, и к финишу файл
        уже готов. Просить нечего — честный отказ вместо тихого «ок»."""
        return {"ok": False, "error": "Bambu собирает таймлапс сам — запрашивать сборку нечего"}

    def camera_url(self) -> str:
        """У Bambu камера — не MJPEG по HTTP, а свой поток (chamber image по TCP с кодом
        доступа). Ретранслятор проекта такого не умеет, и отдавать сюда http-URL значило бы
        показать мастеру вечное «камера не отвечает» вместо честного «камеры пока нет»."""
        return ""
