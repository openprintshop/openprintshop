"""Счётчики сметы: payload расчёта, выбранные столы, стол принтера, DFM-вердикт.

Один предмет: ЧИСТЫЕ помощники, из которых каждый путь сметы собирает ответ клиенту —
строки/итоги (`_result_payload`), разбор выбранных столов (`_parse_plates_sel`), стол
конкретного принтера (`_bed_of`/`_bed_xy`) и вердикт печатопригодности (`_apply_dfm`).
Ни маршрутов, ни состояния: пять функций без побочных эффектов.

Выделено из app/webapp.py волной 9 endgame-плана. Вызывающие (конвейер сметы, /api/calc,
заказ в один тап) остаются в webapp до волны 15 и находят имена через фасад.

Тела перенесены ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import html
import logging

from aiohttp import web

from .. import bedspec, config, db, downloader, keyboards, notify, ratelimit, texts
from ..webauth import _admin_body, _admin_forbidden, _verify_admin, _verify_user
from .common import _ago, _bad, _toint

log = logging.getLogger(__name__)


_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


def _parse_plates_sel(raw, total_plates: int) -> list[int] | None:
    """Выбранные столы из тела /api/calc (OpenTodo 77dc9b01): отсортированный неполный набор
    валидных 0-базных индексов, иначе None (= считать целиком). Дубли схлопываются."""
    if not isinstance(raw, list) or total_plates <= 1:
        return None
    uniq = sorted({_toint(x, -1) for x in raw})
    uniq = [i for i in uniq if 0 <= i < total_plates]
    if uniq and len(uniq) < total_plates:
        return uniq
    return None

def _result_payload(bd, settings, cur, plates, cached) -> dict:
    # строки берём из единого билдера texts.breakdown_rows (тот же источник, что у сметы
    # сохранённого заказа), сворачивая label+sub в старый формат [текст, сумма] для этого экрана
    lines = []
    for row in texts.breakdown_rows(bd, settings, cur):
        label = f"{row['label']} ({row['sub']})" if row.get("sub") else row["label"]
        lines.append([label, row["value"]])
    # скидка заметной: цена ДО скидки (зачёркнутая) + % (OpenTodo 07451408). Только если скидка
    # есть и не сработала минимальная цена (иначе «до» бессмысленно).
    show_disc = bool(bd.discount_pct) and not bd.min_applied
    return {
        "total": texts.fmt_money(bd.total, cur),
        # Голая печатная база БЕЗ опций (B4-инстант): итог печати ДО скрытого округления и ДО
        # плоских услуг постобработки (срочность в ней уже учтена — она множитель печати). Клиент
        # считает ceil10(total_unrounded + Σ выбранных услуг) локально при тапе по услуге — без
        # переслайса. Тождество pure_base = total_unrounded − options_cost верно на любом пути
        # (apply_options держит его), поэтому число одно и то же независимо от выбора на момент calc.
        "total_unrounded": round(bd.total_unrounded - bd.options_cost, 2),
        "discount_pct": round(bd.discount_pct) if bd.discount_pct else 0,
        "total_before": texts.fmt_money(bd.total + bd.discount_amount, cur) if show_disc else "",
        "grams_unit": f"{bd.grams_unit:.1f}", "grams_total": f"{bd.grams_total:.1f}",
        "time_unit": texts.fmt_minutes(bd.minutes_unit), "time_total": texts.fmt_minutes(bd.minutes_total),
        "purge": f"{bd.purge_grams_unit:.1f}" if bd.purge_grams_unit else "",
        "extra_colors": bd.extra_colors, "plates": plates, "cached": cached, "lines": lines,
        # B2 (DFM): дефолт — «печатается без проблем». _apply_dfm перезапишет на каждом пути сметы;
        # дефолт страхует, если будущий путь забудет вызвать _apply_dfm (плашки просто не будет).
        "verdict": "ok", "dfm": [],
    }

def _bed_of(printer) -> bedspec.BedSpec:
    """Стол КОНКРЕТНОГО принтера одним объектом (app.bedspec) — вместо самодельного разбора
    профиля здесь. Тем же BedSpec считают цену calcservice.compute и fastcalc, поэтому вердикт
    печатопригодности и смета судят об одном и том же столе. Принтера нет / профиль не
    разобрался → DEFAULT_BED (консервативный CC2), как раньше."""
    if printer is None:
        return bedspec.DEFAULT_BED
    return bedspec.BedSpec.from_machine_profile(getattr(printer, "machine_profile", None))

def _bed_xy(printer) -> float:
    """Печатаемая сторона стола КОНКРЕТНОГО принтера (XY, мм) — у разных принтеров стол разный,
    статичный config.BED_SIZE_MM тут врал бы. Принтера нет / профиль не разобрали —
    падаем на config.BED_SIZE_MM, а НЕ на дефолт BedSpec.

    Разница видна только тем, кто выставил переменную окружения BED_SIZE_MM: дефолты совпадают
    (оба 256). Но если её выставили, значит стол у машины другой, и молча подставлять сюда
    константу CC2 — как раз то враньё, о котором говорит первая строка докстринга."""
    try:
        if not printer:
            return float(config.BED_SIZE_MM)
        spec = _bed_of(printer)
        return spec.xy_mm if spec != bedspec.DEFAULT_BED else float(config.BED_SIZE_MM)
    except Exception:  # noqa: BLE001
        return float(config.BED_SIZE_MM)

def _apply_dfm(payload: dict, *, dims: str, printer, meas, settings: dict) -> None:
    """B2 (DFM): проставить payload['verdict'] ∈ {ok,risky,wont_fit} + payload['dfm'] на КАЖДОМ
    пути сметы (иначе плашка мигала бы при фоновом уточнении). Реальный стол берём из профиля
    принтера (не из статичной константы). meas — по-детальный замер из res['dfm']/m['dfm_meas']
    (None на быстрых MW/кэш-снимках без габаритов: тогда судим только по dims, что при dims=''
    даёт тихий 'ok'). НИКОГДА не блокирует заказ — только предупреждает."""
    meas = meas or {}
    verdict, dfm = texts.printability_verdict(
        dims or "", bed_mm=_bed_xy(printer),
        n_parts=meas.get("n_parts"),
        min_extent_mm=float(meas.get("min_extent_mm") or 0.0),
        max_part_mm=float(meas.get("max_part_mm") or 0.0),
        wall_proxy_mm=float(meas.get("wall_proxy_mm") or 0.0),
        min_wall_mm=float(settings.get("dfm_min_wall_mm") or 0.8))
    payload["verdict"] = verdict
    payload["dfm"] = dfm
    # Число деталей модели — для локального пересчёта «за-детальных» услуг на смете (accee7a9):
    # клиент множит ставку × parts × тираж, ровно как apply_options на сервере. 0 = не мерили.
    try:
        payload["parts"] = int(meas.get("n_parts") or 0)
    except (TypeError, ValueError):
        payload["parts"] = 0


async def _api_calc_queue(request: web.Request) -> web.Response:
    """Очередь разбора (OpenTodo 26196ac1): модель не загрузилась — клиент отправляет ссылку
    мастеру, тот подготовит её руками и ответит. Кнопка в оверлее ошибки загрузки."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    uid = int(u["id"])
    if not ratelimit.allow(f"cqueue:{uid}", 5, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    url = (body.get("url") or "").strip()
    if not downloader.is_http_url(url):
        return web.json_response({"ok": False, "error": "Нет ссылки для разбора"})
    uname = ("@" + u["username"]) if u.get("username") else \
        (" ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x) or f"id {uid}")
    qid = await db.add_calc_queue(uid, uname, url, (body.get("error") or "").strip())
    await db.log_event(uid, "отправил в очередь разбора", url[:120], user_name=uname)
    if _bot is not None:
        try:
            await notify.notify_admins(
                _bot, f"🛠 <b>Очередь разбора</b> #{qid}\nКлиент {html.escape(uname)} не смог "
                      f"посчитать модель:\n{html.escape(url)}\n\nОшибка: "
                      f"{html.escape((body.get('error') or '—')[:200])}",
                reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL, go="adminqueuefix",
                                                text="Открыть очередь разбора"))
        except Exception:  # noqa: BLE001
            log.debug("calc queue notify failed", exc_info=True)
    return web.json_response({"ok": True, "id": qid})

async def _api_admin_calc_queue(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    items = await db.list_calc_queue(include_done=bool(request.query.get("all")))
    return web.json_response({"ok": True, "items": [
        {"id": r["id"], "user": r["user_name"] or ("id " + str(r["user_id"])),
         "user_id": r["user_id"], "url": r["url"], "error": r["error"] or "",
         "status": r["status"], "ago": _ago(r["created_at"])} for r in items]})

async def _api_admin_calc_queue_resolve(request: web.Request) -> web.Response:
    """Мастер разобрал позицию: status=done (+опц. сообщение клиенту) или delete (тихо)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    q = await db.get_calc_queue(_toint(body.get("id")))
    if not q:
        return web.json_response({"ok": False, "error": "Позиция не найдена"})
    note = (body.get("note") or "").strip()
    if body.get("action") == "delete":
        await db.resolve_calc_queue(q["id"], note, status="dismissed")
        return web.json_response({"ok": True})
    await db.resolve_calc_queue(q["id"], note, status="done")
    if _bot is not None:
        try:
            msg = ("🛠 Мастер разобрал вашу ссылку:\n" + q["url"]
                   + ("\n\n💬 " + note if note else "\n\nПопробуйте посчитать её ещё раз!"))
            await notify.notify_user(_bot, q["user_id"], msg,
                                     reply_markup=keyboards.open_app(config.WEBAPP_PUBLIC_URL))
        except Exception:  # noqa: BLE001
            log.debug("calc queue resolve notify failed", exc_info=True)
    return web.json_response({"ok": True})
