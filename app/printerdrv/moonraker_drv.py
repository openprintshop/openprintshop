"""Драйвер Klipper (Moonraker) — обёртка над `app/moonraker.py`.

Умеет заметно меньше Elegoo, и это главная ценность драйверного слоя: раньше «меньше»
означало «фича молча не работает», теперь — честные способности, по которым интерфейс
прячет кнопки, а printwatch пропускает шаги (журнал ведётся по статусу, без скринов стола
и таймлапсов).

Moonraker — асинхронный (aiohttp), а интерфейс драйвера синхронный там же, где синхронным
был `elegoo.*`. Мост делаем через отдельный событийный цикл в потоке вызывающего: звать
драйвер и так положено из `asyncio.to_thread`, поэтому своего цикла в этом потоке нет.
"""
from __future__ import annotations

import asyncio
import logging

from .. import config, moonraker
from .base import Caps, PrinterDriver

log = logging.getLogger(__name__)


def _norm(url: str) -> str:
    """Адрес к сравнимому виду. Хвостовой слэш незначим — app/moonraker.py:29 его срезает,
    и «тот же принтер со слэшем» не должен читаться как чужой."""
    return url.strip().rstrip("/")


def _run(coro):
    """Выполнить корутину из синхронного контекста (мы уже внутри to_thread)."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # Кто-то позвал из потока с живым циклом — не ломаемся, честно говорим «нет данных».
        log.debug("moonraker driver: вызов из потока с активным циклом", exc_info=True)
        return {"ok": False, "error": "Нет связи с принтером"}


class MoonrakerDriver(PrinterDriver):
    kind = "moonraker"
    title = "Klipper (Moonraker)"
    # Способности объявляем ТОЛЬКО те, что реально работают сегодня. Раньше здесь стояли
    # start=True и exclude_object=True «на вырост»: старт всегда отказывал (у Moonraker печать
    # начинается заливкой, см. start ниже), а метода exclude_object нет ни в одном драйвере и
    # даже в интерфейсе. Обещание флагом — это UX «выясняем нажатием», против которого
    # способности и заводились: сервер кнопку пропускал, а принтер отвечал отказом.
    caps = Caps(status=True, upload=True)

    def _base(self) -> str:
        return _norm(getattr(self.printer, "address", "") or config.MOONRAKER_URL or "")

    def _api_key(self) -> str:
        """Ключ API ЭТОГО принтера: поле карточки `access_code` с фолбэком на .env.

        Одно поле карточки на оба вендора — это не экономия, а суть: и там и там это «секрет,
        которым мы авторизуемся у машины». Отправить ключ первой машины на вторую значит выдать
        секрет наружу, поэтому он обязан ехать вместе с адресом (см. moonraker._headers)."""
        return (getattr(self.printer, "access_code", "") or config.MOONRAKER_API_KEY or "").strip()

    def _serial(self) -> str:
        """Есть для симметрии с Elegoo: у Klipper машину адресует URL, серийник не участвует."""
        return (getattr(self.printer, "serial", "") or "").strip()

    def endpoint(self) -> moonraker.Endpoint:
        """Кому этот драйвер отправляет запросы: база API карточки и ключ ИМЕННО к ней."""
        return moonraker.Endpoint(url=self._base(), api_key=self._api_key())

    def is_the_default_printer(self) -> bool:
        """Ведёт ли карточка на тот же Klipper, что описан в .env. Раньше — ПРЕДОХРАНИТЕЛЬ,
        гасивший пульт чужой карточке; снят вместе с елегувским (цикл 3, волна 6).

        У Klipper своего фонового канала в процессе не было вовсе: `status()` ходит по сети на
        КАЖДЫЙ вопрос и уже несёт эндпоинт карточки. Единственное, что держало предохранитель, —
        симметрия с Elegoo, у которого канал был один. Пустой MOONRAKER_URL = ходить некуда,
        адрес есть только у карточки."""
        configured = _norm(config.MOONRAKER_URL or "")
        return not configured or self._base() == configured

    def enabled(self) -> bool:
        return bool(self._base())

    def status(self, max_age: float = 6.0) -> dict | None:
        """Телеметрия по запросу: у Klipper нет нашего фонового подписчика, поэтому
        max_age здесь не про кэш, а про то, что данные всегда свежие."""
        r = _run(moonraker.query(ep=self.endpoint()))
        if not r.get("ok"):
            return None
        # Приводим к форме cached_status: {ok, state} — её ждут экран принтера и printwatch.
        state = dict(r.get("overlay") or {})
        state["print_status"] = {"status": r.get("state", "")}
        return {"ok": True, "state": state, "summary": r.get("summary") or {}}

    def enrich(self, state: dict) -> dict:
        # Форма уже нормализована в moonraker.query под elegoo.enrich_status — переиспользуем
        # его же, чтобы экран принтера не различал вендоров.
        from .. import elegoo
        return elegoo.enrich_status(state)

    async def upload(self, path, *, name: str | None = None) -> dict:
        return await moonraker.upload(path, start=False, ep=self.endpoint())

    def start(self, filename: str, **kw) -> dict:
        """У Moonraker старт — это заливка с print=true; отдельного «печатай файл из памяти»
        в нашем слое нет, поэтому честно говорим, что умеем стартовать только при заливке."""
        return {"ok": False,
                "error": "Klipper печатает файл при заливке — отправьте задание заново"}
