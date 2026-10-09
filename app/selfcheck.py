"""Самодиагностика: проверка окружения при старте + данные для админ-статуса."""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import time
from pathlib import Path

from . import config, db, license, profilepaths, slicers, tunnel

START_TIME = time.time()   # момент старта процесса (для аптайма)


def disk_free_gb(path: str | None = None) -> float:
    try:
        return shutil.disk_usage(str(path or config.DATA_DIR)).free / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return -1.0


async def run_checks() -> list[str]:
    """Список проблем окружения (пустой = всё ок)."""
    issues: list[str] = []
    if not config.SLICER_PATH.exists():
        issues.append(f"слайсер не найден: {config.SLICER_PATH}")
    printers = await db.list_printers(active_only=True)
    if not printers:
        issues.append("нет активных принтеров")
    for p in printers:
        # ПОЧЕМУ профиль не резолвится, а не только «нет профиля» (хвост C2): на чужой машине
        # разница между «слайсер не установлен», «корень есть, файла под ним нет» и «путь
        # вообще не из установки слайсера» — это три разных действия владельца, а прежняя
        # формулировка не отличала их никак.
        override = (getattr(p, "slicer_path", "") or "").strip()
        be = slicers.for_printer(p)
        why = profilepaths.explain(p.machine_profile, be.kind, override)
        if why:
            issues.append(f"принтер «{p.name}»: machine-профиль — {why}")
        why = profilepaths.explain(p.process_dir, be.kind, override, want_dir=True)
        if why:
            issues.append(f"принтер «{p.name}»: папка process — {why}")
        why = profilepaths.explain(p.filament_dir, be.kind, override, want_dir=True)
        if why:
            issues.append(f"принтер «{p.name}»: папка filament — {why}")
        # Сопла (f71f4973): профиль под сопло мог не пережить обновление слайсера — это
        # тихая поломка (слайс молча уедет на профиль основного сопла), поэтому проверяем.
        for n in await db.list_printer_nozzles(p.id):
            why = profilepaths.explain(n.machine_profile, be.kind, override)
            if why:
                issues.append(f"принтер «{p.name}», сопло {n.nozzle}: machine-профиль — {why}")
        # Выбор слайсера (12.08, остаток варианта B из orcaslicer-fork-adr): exe движка
        # карточки мог переехать/удалиться — calcservice тогда ловит SlicerError и МОЛЧА
        # отдаёт приблизительную оценку, то есть поломка выглядит не как ошибка, а как
        # «цена стала другой». Движок по умолчанию без override не дублируем — его уже
        # проверила первая строка (config.SLICER_PATH).
        if (be.kind != slicers.DEFAULT_KIND or override) and not be.installed(override):
            issues.append(f"принтер «{p.name}»: слайсер {be.title} не найден"
                          + (f" ({override})" if override else " (путь не задан, типовых мест нет)"))
    # Пресет качества ссылается на сопло, которого у принтеров нет: расчёт пройдёт профилем
    # основного сопла — молча и не тем диаметром. Лучше сказать вслух.
    known = set(await db.known_nozzles())
    have = {n.nozzle for n in await db.list_printer_nozzles()}
    if have:
        for q in await db.list_quality(active_only=True):
            if db.nozzle_of(q) not in have and db.nozzle_of(q) in known:
                issues.append(f"профиль «{q.label}»: сопла {db.nozzle_of(q)} нет ни у одного принтера")
    if not await db.list_plastics(active_only=True):
        issues.append("нет активных пластиков")
    # B6: конвертер STEP настроен, но exe пропал — .step снова тихо отклоняется на приёме,
    # хотя мастер думает, что формат принимается. Пустая настройка проблемой не считается.
    if config.FREECAD_CMD and not Path(config.FREECAD_CMD).is_file():
        issues.append(f"FREECAD_CMD задан, но файла нет: {config.FREECAD_CMD} "
                      f"(приём .step отключился)")
    free = disk_free_gb()
    if 0 <= free < 1.0:
        issues.append(f"мало места на диске: {free:.1f} ГБ")
    if not data_dir_writable():
        issues.append(f"каталог данных недоступен для записи: {config.DATA_DIR} "
                      f"(задаётся DATA_DIR в .env)")
    issues += entry_checks()
    issues += license_checks()
    return issues


def license_checks() -> list[str]:
    """Лицензия студии (трек C, п.4). Строки отсюда попадают и в экран «Статус», и в
    стартовый алерт владельцу (run.py), поэтому молчим везде, где сказать нечего:
    на машине разработчика и в сборке без вшитого публичного ключа спрашивать нечего
    (enforcement 'off'), а бессрочная действующая лицензия — не новость.

    Про истечение говорим за две недели: продление у поставщика — не мгновенная операция,
    а последствие мягкое (перестают приезжать обновления, всё остальное работает)."""
    out: list[str] = []
    st = license.status()
    state = str(st.get("state") or "")
    soft = st.get("enforcement") == "off"
    where = ("впишите LICENSE_KEY в .env или положите файл license.key в "
             f"{config.DATA_DIR}")
    if state == "missing":
        if not soft:
            out.append(f"лицензионный ключ не задан — обновления кода применяться не будут; {where}")
    elif state == "invalid":
        # При enforcement=off сборка лицензию не спрашивает вовсе (публичный ключ выпуска не
        # вшит): ругаться на «ключ не проходит проверку» здесь значит слать владельцу тревогу
        # о механизме, который в этой сборке выключен.
        if not soft:
            out.append(f"лицензия: {st.get('reason') or 'ключ не проходит проверку'} ({where})")
    elif state == "expired":
        out.append(f"{st.get('reason') or 'лицензия истекла'} — обновления кода не применяются "
                   f"(если это ошибка, проверьте дату на компьютере). Приём заказов и расчёты "
                   f"работают как обычно")
    elif state == "trial":
        # Проба идёт — это норма, а не проблема: молчим, пока запас больше двух недель
        # (тот же порог, что у подписки). Иначе стартовый алерт владельцу шумел бы 90 дней.
        left = st.get("days_left")
        if left is not None and float(left) <= license.EXPIRING_DAYS:
            out.append(f"{st.get('reason')} — дальше обновления кода применяться не будут; {where}")
    elif state == "trial_over":
        # Ветка стоит ДО общего else намеренно: иначе конец пробы уехал бы в «ключ не
        # проходит проверку», что неправда — ключа просто нет.
        out.append(f"{st.get('reason') or 'пробный период закончился'}; {where}. "
                   f"Приём заказов и расчёты работают как обычно")
    elif state == "expiring":
        left = st.get("days_left")
        out.append(f"{st.get('reason')}: осталось {int(left) if left is not None else '?'} дн — "
                   f"продлите лицензию, иначе перестанут приезжать обновления")
    if st.get("edition_mismatch"):
        out.append(f"лицензия выдана на редакцию «{st.get('edition')}», а сборка опознана как "
                   f"«{st.get('build_edition')}» — ничего не отключено, но проверьте EDITION в .env")
    return out


def entry_checks() -> list[str]:
    """Вход наружу (C3): режим TUNNEL_MODE и публичный адрес мини-апки.

    Молчаливая деградация здесь дороже всего: при пустом/неверном адресе кнопки в
    сообщениях уходят без ссылки, QR на калибровочных этикетках печатается пустым, а
    картинка счёта не подгружается — и НИ ОДНА из этих поломок не видна в логе.
    Про quick говорим, когда туннель действительно поднимается: либо адрес из .env не
    задан, либо адрес в config уже проставил сам quick-туннель (tunnel.public_url()).
    У владельца со своим Funnel режим формально quick, а адрес постоянный — молчим."""
    out: list[str] = []
    mode = tunnel.mode()
    url = (config.WEBAPP_PUBLIC_URL or "").strip()
    if mode == "named":
        if not config.CF_TUNNEL_TOKEN:
            out.append("TUNNEL_MODE=named, но CF_TUNNEL_TOKEN не задан — "
                       "именованный туннель не поднимется")
        if not url:
            out.append("TUNNEL_MODE=named, но WEBAPP_PUBLIC_URL не задан — "
                       "постоянный адрес студии брать неоткуда (отката на quick НЕТ)")
    elif mode == "none":
        if not url:
            out.append("TUNNEL_MODE=none, но WEBAPP_PUBLIC_URL не задан — "
                       "мини-апка недоступна снаружи")
    elif not url or tunnel.public_url():
        # tunnel.public_url() непуст ТОЛЬКО когда адрес выдал сам quick-туннель (он же
        # пишет его в config.WEBAPP_PUBLIC_URL). Без этой ветки предупреждение замолкало
        # через секунду после старта — ровно тогда, когда владелец открывает «Статус».
        out.append("вход снаружи через временный Cloudflare-туннель (TUNNEL_MODE=quick): "
                   "адрес меняется при каждом перезапуске бота — умирают кнопки в уже "
                   "отправленных сообщениях, кнопка-меню ☰, QR на калибровочных этикетках "
                   "и картинки счетов. Для постоянного адреса — TUNNEL_MODE=named")
    # Живое состояние процесса: при неверном токене cloudflared умирает и вечно
    # перезапускается сторожем, а по одним лишь ключам .env всё выглядит настроенным —
    # ровно тот класс «студия думает, что работает», ради которого делался C3.
    err = str(tunnel.status().get("error") or "")
    if err:
        out.append(f"вход снаружи: {err} — мини-апка сейчас недоступна клиентам")
    if url:
        # Telegram принимает в web_app только https с валидным публичным сертификатом:
        # ошибка вылезет не при старте, а отказом Bot API в момент выдачи кнопки клиенту.
        if not url.lower().startswith("https://"):
            out.append(f"WEBAPP_PUBLIC_URL не https ({url}) — Telegram не примет такой "
                       f"адрес в кнопке приложения")
        else:
            host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
            if re.fullmatch(r"[0-9.]+", host):
                # Публичный IP — можно: Let's Encrypt выдаёт сертификаты и на IP (короткие, ~6 суток,
                # продлевает acme.sh) — так живёт, например, мини-апп на VPS (:8451). Частный или
                # служебный адрес (LAN, Tailscale 100.64/10, примеры из RFC) публичного сертификата
                # не получит — Telegram кнопку с ним не откроет.
                try:
                    public = ipaddress.ip_address(host).is_global
                except ValueError:
                    public = False
                if not public:
                    out.append(f"WEBAPP_PUBLIC_URL указывает на непубличный IP ({host}) — публичного "
                               f"сертификата для него не бывает, Telegram не откроет кнопку приложения")
    return out


def data_dir_writable() -> bool:
    """Можно ли писать в DATA_DIR (C2): каталог задаётся из .env и может указывать на
    несмонтированный/чужой диск — тогда бот молча теряет и базу, и логи, и фото.

    Сознательно БЕЗ пробного файла: проверка зовётся из run_checks на каждом открытии
    статуса (и из тестов), а сорить в боевом каталоге данных незачем. Настоящий признак
    беды — несоздаваемый каталог: несмонтированный том и отказ прав валят именно mkdir."""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(str(config.DATA_DIR), os.W_OK)


def uptime_str() -> str:
    s = int(time.time() - START_TIME)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    out = []
    if d:
        out.append(f"{d}д")
    if h:
        out.append(f"{h}ч")
    out.append(f"{m}м")
    return " ".join(out)


def slicer_load() -> tuple[int, int]:
    """(занято, всего) слотов слайсера — best-effort через семафор."""
    try:
        from . import slicer
        free = getattr(slicer._sem, "_value", config.MAX_CONCURRENT_SLICES)
        total = config.MAX_CONCURRENT_SLICES
        return (max(0, total - free), total)
    except Exception:  # noqa: BLE001
        return (0, config.MAX_CONCURRENT_SLICES)
