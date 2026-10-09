"""Прямая отправка задания на принтер через Moonraker (Klipper) — опционально (c6e4f855).

Включается, когда задан config.MOONRAKER_URL (например http://192.168.0.50:7125). Пусто →
функции возвращают «выключено», а кнопка в админке скрыта. Загружаем файл в раздел gcodes
и (по желанию) сразу стартуем печать. Поддерживается X-Api-Key.

ВАЖНО: Moonraker — это API Klipper-принтеров (Voron, Ender+Klipper, Prusa+Moonraker, …).
Проприетарные принтеры (Bambu, и Elegoo Centauri Carbon, который ходит через свой ElegooSlicer-
протокол) Moonraker НЕ предоставляют — там этот путь не активируется. Реальную отправку
включает пользователь, задав MOONRAKER_URL; здесь — гейт + юнит-тесты на моках.

АДРЕСАТ ВЫЗОВА — тот же приём, что в app/elegoo.py: необязательный `ep: Endpoint` (база API и
ключ к ней). `ep=None` = принтер из .env, то есть для сегодняшних вызовов ничего не меняется.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from . import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Endpoint:
    """Один Moonraker-принтер: база его API и ключ ИМЕННО к ней."""

    url: str = ""
    api_key: str = ""


def _ep(ep: Endpoint | None = None) -> Endpoint:
    """Эндпоинт вызова: явный — как есть; None — принтер по умолчанию из настроек.
    Настройки читаем на каждый вызов: их подменяют на лету админка и тесты."""
    if ep is not None:
        return ep
    return Endpoint(url=(config.MOONRAKER_URL or "").strip(),
                    api_key=(config.MOONRAKER_API_KEY or "").strip())


def enabled(ep: Endpoint | None = None) -> bool:
    return bool(_ep(ep).url)


def _base(ep: Endpoint | None = None) -> str:
    return _ep(ep).url.rstrip("/")


def _headers(ep: Endpoint | None = None) -> dict:
    """Ключ того принтера, к которому идём. Один на модуль он быть не может: ключ первой машины,
    отправленный на вторую, — это выданный наружу секрет, а не просто лишний заголовок."""
    e = _ep(ep)
    return {"X-Api-Key": e.api_key} if e.api_key else {}


async def upload(file_path, start: bool = False, *, session=None,
                 ep: Endpoint | None = None) -> dict:
    """Загрузить файл на принтер (POST /server/files/upload, root=gcodes). start=True → печать.
    Возвращает {ok, error?, filename?, started?}. session можно подменить (тесты/переиспользование)."""
    if not enabled(ep):
        return {"ok": False, "error": "Сетевая печать не настроена (MOONRAKER_URL пуст)"}
    p = Path(file_path)
    if not p.exists():
        return {"ok": False, "error": "Файл задания не найден"}
    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        form = aiohttp.FormData()
        form.add_field("root", "gcodes")
        if start:
            form.add_field("print", "true")
        form.add_field("file", p.read_bytes(), filename=p.name,
                       content_type="application/octet-stream")
        async with session.post(f"{_base(ep)}/server/files/upload", data=form,
                                headers=_headers(ep),
                                timeout=aiohttp.ClientTimeout(total=180)) as r:
            if r.status in (200, 201):
                return {"ok": True, "filename": p.name, "started": bool(start)}
            txt = (await r.text())[:200]
            return {"ok": False, "error": f"Принтер ответил {r.status}: {txt}"}
    except Exception as e:  # noqa: BLE001
        log.warning("moonraker upload failed", exc_info=True)
        return {"ok": False, "error": f"Нет связи с принтером: {e}"}
    finally:
        if own:
            await session.close()


async def status(*, session=None, ep: Endpoint | None = None) -> dict:
    """Проверка связи: GET /printer/info. {ok, state?, error?}."""
    if not enabled(ep):
        return {"ok": False, "error": "Сетевая печать не настроена"}
    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(f"{_base(ep)}/printer/info", headers=_headers(ep),
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return {"ok": False, "error": f"Принтер ответил {r.status}"}
            data = await r.json()
            res = data.get("result", data)
            return {"ok": True, "state": res.get("state", "?"), "hostname": res.get("hostname", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Нет связи: {e}"}
    finally:
        if own:
            await session.close()


async def query(*, session=None, ep: Endpoint | None = None) -> dict:
    """Живая телеметрия Klipper (этап 1 docs/printer-integration.md): GET /printer/objects/query.
    Нормализует ответ в форму, близкую к elegoo.enrich_status ({ok, state, summary, overlay}),
    чтобы экран принтера мог показывать и Klipper-принтер. Активна только при MOONRAKER_URL."""
    if not enabled(ep):
        return {"ok": False, "error": "Сетевая печать не настроена"}
    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        url = (f"{_base(ep)}/printer/objects/query"
               "?print_stats&virtual_sdcard&extruder&heater_bed&fan")
        async with session.get(url, headers=_headers(ep),
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return {"ok": False, "error": f"Принтер ответил {r.status}"}
            data = await r.json()
            st = (data.get("result", data) or {}).get("status") or {}
            ps = st.get("print_stats") or {}
            vs = st.get("virtual_sdcard") or {}
            ext = st.get("extruder") or {}
            bed = st.get("heater_bed") or {}
            fan = st.get("fan") or {}
            overlay: dict = {}
            if isinstance(vs.get("progress"), (int, float)):
                overlay["virtual_sdcard"] = {"progress": max(0.0, min(1.0, float(vs["progress"])))}
            info = {}
            cl = (ps.get("info") or {}).get("current_layer")
            tl = (ps.get("info") or {}).get("total_layer")
            if isinstance(cl, (int, float)):
                info["current_layer"] = int(cl)
            if isinstance(tl, (int, float)) and tl:
                info["total_layer"] = int(tl)
            if info:
                overlay["info"] = info
            if isinstance(fan.get("speed"), (int, float)):
                overlay["fan"] = round(float(fan["speed"]) * 100)
            summary = {"extruder_temp": ext.get("temperature"), "extruder_target": ext.get("target"),
                       "bed_temp": bed.get("temperature"), "bed_target": bed.get("target")}
            return {"ok": True, "state": ps.get("state", "?"), "overlay": overlay, "summary": summary}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Нет связи: {e}"}
    finally:
        if own:
            await session.close()
