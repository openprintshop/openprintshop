"""Драйвер Elegoo (SDCP поверх MQTT) — обёртка над разведанным `app/elegoo.py`.

Ничего не переписывает: протокол разведан захватами (docs/captures) и работает годами.
Драйвер лишь называет его способности вслух, чтобы остальной код перестал звать `elegoo.*`
напрямую и научился работать с принтером, который умеет меньше.

Адрес и код доступа сегодня живут в .env (ELEGOO_PRINTER_IP/ELEGOO_ACCESS_CODE) — карточка
принтера в БД их перекрывает, если заполнена. Это нужно для white-label: у покупателя
продукта не должно быть ручной правки .env (см. multi-printer-support.md §4).

«Перекрывает» теперь означает буквально это: `endpoint()` собирает адрес, код и серийник
карточки и едет ПАРАМЕТРОМ в каждый вызов протокола. Раньше поля карточки не покидали драйвера,
а `app/elegoo.py` брал всё из .env — то есть карточка второго принтера управляла первым.

Предохранитель `_is_the_live_printer` СНЯТ (цикл 3, волна 6): он гасил пульт карточке с чужим
адресом, пока живой канал в процессе был один — подписчик MQTT, ретранслятор камеры и printwatch
знали ровно один принтер. Теперь у каждого адреса своя сессия (`elegoo.session_for`), у каждой
камеры свой ретранслятор, а журнал, привязка и наработка ТО ведутся по машинам. От него осталась
справка `is_the_default_printer()` — там, где вопрос «та ли это машина из .env» по существу.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import cc2community, config, elegoo
from .base import Caps, PrinterDriver, replace

log = logging.getLogger(__name__)

# Нормализация адреса переехала в app/elegoo.py: по ней же теперь ключуется реестр сессий, и
# две реализации «тот же ли это принтер» разъехались бы молча — карточка считалась бы своей,
# а сессию получала бы отдельную (два подписчика на один брокер = война реконнектов).
_norm_host = elegoo.norm_host


class ElegooDriver(PrinterDriver):
    kind = "elegoo"
    title = "Elegoo (SDCP)"
    # Полный набор: это тот принтер, под который писалась вся автоматика мастерской.
    # exclude_object=False — в стоковом протоколе команды «пропусти объект» нет. Klipper внутри
    # CC2 есть, но его сокет доступен только с рутом; с прошивкой сообщества флаг включается
    # в __init__ (см. ниже и app/cc2community.py).
    caps = Caps(status=True, files=True, upload=True, start=True, control=True, move=True,
                filament=True, camera=True, history=True, timelapse=True, delete=True,
                speed_mode=True, auto_refill=True, usb=True)

    def __init__(self, printer=None):
        super().__init__(printer)
        # Драйвер создаётся на каждый вызов (printerdrv.for_printer), поэтому способности
        # прошивки сообщества берутся из кэша автоопределения здесь, без сети. Сама проба идёт
        # фоном из status() — так кнопки появляются в течение одного опроса после её установки.
        extra = cc2community.driver_caps(cc2community.cached(self._address()))
        if extra:
            self.caps = replace(type(self).caps, **extra)

    def enabled(self) -> bool:
        # Адрес из карточки принтера важнее .env — но пока карточка пуста, работает .env,
        # то есть у боевой установки ничего не меняется. Спрашиваем СВОИ поля, а не
        # elegoo.enabled(): «настроен ли пульт этой карточки» — вопрос к карточке, и модульная
        # функция, которую подменяют тесты и половина ручек, отвечать на него не должна.
        #
        # ПРЕДОХРАНИТЕЛЬ СНЯТ (цикл 3, волна 6). Он гасил пульт карточке, чей адрес не совпал с
        # адресом единственного живого канала, и держался не на адресе — тот ехал параметром
        # с волны 1, — а на том, что живой канал в процессе был ОДИН: подписчик MQTT, ретранслятор
        # камеры и printwatch знали ровно один принтер, и вторая карточка получила бы правдивые
        # команды и лживую телеметрию. Теперь у каждого адреса своя сессия (elegoo.session_for),
        # у каждой камеры свой ретранслятор, а наблюдатель обходит все карточки и пишет журнал,
        # привязку и наработку ТО по машинам. Гасить стало нечего.
        return bool(self._address()) and bool(self._code())

    def _address(self) -> str:
        return (getattr(self.printer, "address", "") or config.ELEGOO_PRINTER_IP or "").strip()

    def _code(self) -> str:
        return (getattr(self.printer, "access_code", "") or config.ELEGOO_ACCESS_CODE or "").strip()

    def _serial(self) -> str:
        """Серийник КАРТОЧКИ, без фолбэка на .env.

        Фолбэк не убран, а ПЕРЕЕХАЛ в `elegoo.card_endpoint`: только там на руках оба адреса и
        видно, та же это машина или чужая. Подставь .env-серийник здесь — и карточка с чужим
        адресом получила бы тему ПЕРВОГО принтера: команда не выполнится нигде, а вызов
        отчитается об успехе."""
        return (getattr(self.printer, "serial", "") or "").strip()

    def endpoint(self) -> elegoo.Endpoint:
        """Кому этот драйвер отправляет байты: адрес, код и серийник ЭТОЙ карточки.

        Собирается на каждый вызов, а не в __init__: карточку правят из админки, а .env-фолбэк
        читается через config, который тесты и настройки подменяют на лету.

        Сборка — общая с реестром сессий (`elegoo.card_endpoint`): драйвер и реестр обязаны
        получать из одной карточки один и тот же эндпоинт, иначе подписчик будет висеть на одном
        адресе, а команды уходить на другой."""
        return elegoo.card_endpoint(self._address(), self._code(), self._serial())

    def is_the_default_printer(self) -> bool:
        """Ведёт ли эта карточка на тот же принтер, что описан в .env.

        Раньше это был ПРЕДОХРАНИТЕЛЬ (`_is_the_live_printer`), гасивший пульт чужой карточке.
        Сейчас — просто справка: сессия, камера, журнал и привязка разведены по машинам, гасить
        нечего. Остаётся полезной там, где вопрос действительно про «ту самую» машину: например
        `elegoo.camera_url` отдаёт PRINTER_CAMERA_URL (RTSP-мост) только принтеру по умолчанию.

        Пустой адрес в .env означает «принтер описан только карточкой» (установка white-label):
        путать не с чем. Серийник в сравнение НЕ входит: у боевой установки он автоопределяется
        из топика и в .env может быть пуст."""
        configured = _norm_host((config.ELEGOO_PRINTER_IP or "").strip())
        return not configured or _norm_host(self._address()) == configured

    def status(self, max_age: float = 6.0) -> dict | None:
        """Кэш подписчика СВОЕЙ сессии — той, что ключуется адресом этой карточки.

        Раньше здесь стоял модульный `elegoo.cached_status()`, то есть телеметрия принтера по
        умолчанию: карточка с чужим адресом показывала бы чужие температуры как свои (от этого
        и был предохранитель). Теперь у карточки со своим адресом либо свои показания, либо
        честный None — пока её подписчика не подняли.

        Заодно (фоном, без ожидания) перепроверяет, не стоит ли на машине прошивка сообщества:
        статус опрашивают раз в несколько секунд все экраны, а проба сама держит свой интервал.

        С прошивкой сообщества во время печати дописывает в снимок то, чего стоковый MQTT о
        задании не знает (остаток по оценке слайсера, всего слоёв, прогресс Klipper) — см.
        cc2community.merge_job. Здесь, а не в enrich: так это видят все потребители статуса,
        включая тех, кто читает сырой print_status (LCD, деск). В простое к службе не ходим."""
        host = self._address()
        cc2community.ensure_probe(host)
        cs = elegoo.cached_status(max_age=max_age, ep=self.endpoint())
        st = (cs or {}).get("state")
        if isinstance(st, dict) and elegoo.job_running(st):
            cc2community.merge_job(st, cc2community.job(host))
        return cs

    def enrich(self, state: dict) -> dict:
        return elegoo.enrich_status(state)

    # ── файлы через службу сообщества (их file_manager) ──
    # Их служба читает файловую систему принтера сама: память и флешку разом, вложенные папки,
    # файлы из Orca мимо вендорской базы. Стоковый 1044 отвечает только «привязанному» слайсеру.
    # Отказ службы — не тупик: дальше прежний путь по MQTT.
    def _cc2_has(self, cap: str) -> bool:
        info = cc2community.cached(self._address())
        return bool(info and info.caps.get(cap))

    def _cc2_files(self, storage: str) -> dict | None:
        if not self._cc2_has("file_manager"):
            return None
        try:
            r = cc2community.gcode_files(self._address(), storage)
        except cc2community.CommunityError as e:
            log.info("CC2 %s: список файлов службы не получен (%s) — по MQTT", self._address(), e)
            return None
        if r.get("ok"):
            # Граммы, время и филамент файла их список не несёт — берём из пассивного сбора
            # обмена слайсер↔принтер (уже в памяти, к принтеру не ходим), если он есть.
            hv = elegoo.harvested_files(ep=self.endpoint()) or {}
            meta = {str(f.get("name") or ""): f for f in hv.get("files") or []}
            for f in r["files"]:
                m = meta.get(f["name"]) or meta.get(f["name"].rsplit("/", 1)[-1])
                if m:
                    f.update({k: m[k] for k in ("print_time", "layer", "grams", "colors") if m.get(k)})
        return r

    def files(self, timeout: float = 8.0) -> dict:
        r = self._cc2_files("local")
        if r is not None:
            return r
        ep = self.endpoint()
        r = elegoo.list_files(timeout, ep=ep)
        if not r.get("ok"):
            # Активный 1044 принтер отдаёт только «привязанному» слайсеру; пассивно собранный
            # список честнее ошибки — печатать с него всё равно можно. Берём его из СВОЕЙ
            # сессии: имена файлов на двух машинах совпадают сплошь и рядом.
            r = elegoo.harvested_files(ep=ep) or r
        return r

    async def upload(self, path, *, name: str | None = None) -> dict:
        ep = self.endpoint()
        return await asyncio.to_thread(
            lambda: elegoo.upload_gcode(path, name=name or Path(path).name, ep=ep))

    def start(self, filename: str, **kw) -> dict:
        # С картой слотов старт ведёт их служба (cc2community.gcode_print): сверяет карту с
        # файлом, флешку копирует в память и калибрует стол по-настоящему — стоковый 1020
        # bedlevel_force принимает, но не выполняет. Без карты (внешняя катушка, «продолжить»),
        # без связи со службой или со службой старше нужного формата — прежний 1020.
        if kw.get("slot_map") and self._cc2_has("file_manager"):
            r = self._cc2_start(filename, kw)
            if r is not None:
                return r
        return elegoo.start_print(filename, ep=self.endpoint(), **kw)

    def _cc2_start(self, filename: str, kw: dict) -> dict | None:
        """Старт через службу; None — запрос до неё не дошёл или формат ей незнаком, и печать
        можно спокойно запустить по 1020: службой ничего не начато."""
        host, storage = self._address(), kw.get("storage") or "local"
        sm = [(int(m["t"]), int(m["tray_id"])) for m in kw["slot_map"]]
        # Флажок «полная калибровка» — full: полная сетка в профиль стороны листа перед печатью.
        # Без него — сохранённая сетка стороны (нет её — служба сама снимет полную).
        # Таймлапс пишется всегда, как и прежним 1020 (delay_video).
        plate = str(kw.get("plate") or "A").strip().upper()
        leveling = "full" if kw.get("bedlevel") else "saved"
        for attempt in (1, 2):
            try:
                r = cc2community.gcode_print(host, storage, filename, slot_map=sm, plate=plate,
                                             leveling=leveling, timelapse=True)
            except cc2community.CommunityUnreachable as e:
                log.warning("CC2 %s: служба недоступна (%s) — «%s» стартует по MQTT", host, e, filename)
                return None
            except cc2community.CommunityError as e:
                msg = str(e)
                if msg == "Invalid bed leveling mode":
                    # Служба до флага таймлапса (1.1.31 и раньше) читает его как режим стола и
                    # отказывает, ничего не начав. Наш режим стола всегда из их списка.
                    log.info("CC2 %s: служба старше формата старта — «%s» по MQTT", host, filename)
                    return None
                if msg == "Canvas mapping does not match this G-code" and attempt == 1:
                    trimmed = self._cc2_trim_map(storage, filename, sm)
                    if trimmed:
                        sm = trimmed
                        continue
                return {"ok": False, "error": cc2community.print_error_text(msg)}
            return {"ok": True, "source": "cc2", "filename": str(r.get("filename") or filename),
                    "imported_from_usb": bool(r.get("imported_from_usb"))}
        return {"ok": False, "error": cc2community.print_error_text("")}

    def _cc2_trim_map(self, storage: str, filename: str,
                      sm: list[tuple[int, int]]) -> list[tuple[int, int]] | None:
        """Карта слотов шире файла (допечатка без детали унесла инструмент) — оставить в ней
        только инструменты файла. Их служба требует ровно их, а 1020 лишнее молча пропускал.
        None — урезать нечего или карте не хватает инструмента: тогда это ошибка мастера."""
        try:
            tools = set(cc2community.gcode_inspect(self._address(), storage, filename)["file"]["tools"])
        except (cc2community.CommunityError, KeyError, TypeError):
            return None
        have = {t for t, _ in sm}
        if not tools or not tools < have:
            return None
        return [(t, tray) for t, tray in sm if t in tools]

    def delete_file(self, filename: str) -> dict:
        return self.delete_files([filename])

    def file_grams(self, filename: str) -> float | None:
        """Граммы файла из кэшей СВОЕЙ сессии — пассивного сбора и снимка list_files.

        Раньше здесь стояла развилка «свой принтер — кэшем, чужой — мимо кэшей»: кэши были одни
        на процесс, и отдать чужому принтеру граммы одноимённого файла с первой машины значило
        соврать в стоимости заказа. Обход стоил троттлинга: экран «Печать» поллит статус каждые
        ~3 с, и каждый кадр превращался бы в сетевой 1044 — тот самый шторм, ради защиты от
        которого кэш и написан. С сессиями развилка не нужна: у каждого принтера свой кэш и свой
        троттлинг."""
        return elegoo.file_grams(filename, ep=self.endpoint())

    def control(self, action: str, *, on: bool = True) -> dict:
        return elegoo.control(action, on=on, ep=self.endpoint())

    def set_temperature(self, target: str, value: int) -> dict:
        return elegoo.set_temperature(target, value, ep=self.endpoint())

    def home(self, axes: str = "xyz") -> dict:
        return elegoo.home(axes, ep=self.endpoint())

    def move_axis(self, axis: str, distance: float) -> dict:
        return elegoo.move_axis(axis, distance, ep=self.endpoint())

    def set_fan(self, which: str, pct: int) -> dict:
        return elegoo.set_fan(which, pct, ep=self.endpoint())

    def load_filament(self, slot: int) -> dict:
        return elegoo.load_filament(slot, ep=self.endpoint())

    def unload_filament(self, slot: int) -> dict:
        return elegoo.unload_filament(slot, ep=self.endpoint())

    def set_speed_mode(self, mode: int) -> dict:
        return elegoo.set_speed_mode(mode, ep=self.endpoint())

    def set_auto_refill(self, on: bool) -> dict:
        return elegoo.set_auto_refill(on, ep=self.endpoint())

    def file_info(self, filename: str, *, storage: str = "local") -> dict:
        # Инструменты и филамент — из самого G-code (их /inspect): файл из Orca во вложенной
        # папке или на флешке вендорская база (1046) может и не знать.
        if self._cc2_has("file_manager"):
            try:
                return cc2community.gcode_inspect(self._address(), storage, filename)
            except cc2community.CommunityError as e:
                log.info("CC2 %s: файл «%s» службой не разобран (%s) — по MQTT",
                         self._address(), filename, e)
        return elegoo.file_info(filename, ep=self.endpoint(), storage=storage)

    def files_on(self, storage: str, timeout: float = 8.0) -> dict:
        r = self._cc2_files(storage)
        if r is not None:
            return r
        return elegoo.list_files(timeout, ep=self.endpoint(), storage=storage)

    def thumbnail(self, path: str, *, storage: str = "local") -> tuple[bytes, str] | None:
        """Превью из самого G-code на принтере (их /thumbnail). Только с прошивкой сообщества."""
        if not self.caps.thumbs:
            return None
        return cc2community.gcode_thumbnail(self._address(), storage, path)

    def delete_files(self, names: list[str], *, storage: str = "local") -> dict:
        # 1047 принимает массив имён — одна команда вместо N (docs/captures, 20.07).
        # Имя с папкой бывает только в списке их службы, а 1047 знает имена без каталога: такие
        # файлы удаляет их служба, по одному. Остальные — прежним путём, в том числе во время
        # печати (их /delete работает только в простое).
        names = list(names)
        nested = [n for n in names if "/" in n] if self._cc2_has("file_manager") else []
        flat = [n for n in names if n not in nested]
        res: dict = {"ok": True}
        if flat:
            res = elegoo.delete_file(flat, storage=storage, ep=self.endpoint())
        for n in nested:
            try:
                cc2community.gcode_delete(self._address(), storage, n)
            except cc2community.CommunityError as e:
                return {"ok": False, "error": str(e)}
        return res

    def community(self) -> dict | None:
        info = cc2community.cached(self._address())
        return info.as_dict() if info else None

    def community_host(self) -> str:
        return self._address()

    def camera_url(self) -> str:
        return elegoo.camera_url(self.endpoint())

    def history(self, timeout: float = 8.0) -> dict:
        return elegoo.job_history(timeout, ep=self.endpoint())

    def latest_timelapse_url(self, job_name: str | None = None) -> dict:
        return elegoo.latest_timelapse_url(job_name, ep=self.endpoint())

    def generate_timelapse(self, url: str) -> dict:
        return elegoo.generate_timelapse(url, ep=self.endpoint())
