"""Наблюдатель за завершением печати, привязанной к заказу (OpenTodo 70b5a590),
и журнал каждой печати с опросом брака (OpenTodo c587c48b).

Когда мастер привязал текущую печать к заказу (db.print_binding), этот фоновый цикл следит по
УЖЕ имеющейся телеметрии (фоновый MQTT-подписчик elegoo.cached_status) за завершением: как только
печать, которую мы видели активной, доходит до ~100 % или перестаёт быть активной — отправляет
мастеру запрос на подтверждение «всё ок → напечатано».

Вторая часть (_tick_journal) наблюдает за ЛЮБОЙ печатью, не только привязанной к заказу: пока
печать активна — копит её имя и длительность из телеметрии (после финиша Centauri перестаёт
отдавать print_status, поэтому собирать надо заранее), при завершении — присылает мастеру
статистику печати и опрос «занести в журнал печатей?» (успех / частичный брак / полный брак).

Используются ТОЛЬКО сигналы, которые уже выводит проверенный elegoo.enrich_status
(virtual_sdcard.progress, print_stats.state) — никаких домыслов о финальном статусе SDCP
(docs/printer-integration.md). Окончательное решение всегда за мастером, поэтому достаточно факта
«была активна → больше не активна»; «завершено vs остановлено» классифицирует человек.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from . import cc2community, cc2spools, config, db, elegoo, keyboards, notify, printerdrv, texts

log = logging.getLogger(__name__)

POLL_SEC = 60

class _Interpreter(printerdrv.PrinterDriver):
    """Разборщик состояния без сети: только enrich, как у Elegoo.

    Нужен как запасной, когда статус уже получен, а драйвер вызывающему не понадобился. Взять
    голый PrinterDriver нельзя: у него enrich пуст, а форма Centauri без overlay не содержит
    признака печати вовсе (прошивка шлёт print_duration/remaining_time_sec, а флаг достраивает
    enrich_status) — то есть боевой принтер молча считался бы простаивающим."""

    def enrich(self, state: dict) -> dict:
        return elegoo.enrich_status(state or {})


_BASE = _Interpreter()


async def _driver():
    """Драйвер принтера ПО УМОЛЧАНИЮ — та же точка выбора, что у ручек админки.

    Своя копия, а не вызов webapp: webapp — модуль на двенадцать тысяч строк, и тянуть его
    импортом ради четырёх строк значит менять порядок инициализации всего приложения.

    Через драйвер идёт ВСЁ, включая телеметрию (цикл 3, волна 2). Раньше телеметрия читалась
    прямым `elegoo.cached_status` — «у Elegoo это даром, а у Moonraker поход по сети». Довод
    верный, но вывод был неверный: из-за него автоматика мастерской молча работала только с
    Centauri, а Klipper-карточка не получала журнала никогда. Сеть решается потоком
    (`asyncio.to_thread` в `_status`), а не отказом от абстракции."""
    try:
        printer = await db.get_default_printer()
    except Exception:  # noqa: BLE001
        printer = None
    return printerdrv.for_printer(printer)


# Какая карточка сейчас основная. Кэш на итерацию цикла, наполняет _drivers(). Нужен ровно в
# одном месте — решить, можно ли этой карточке фолбэкать на кэш подписчика (см. _status).
_default_pid: int | None = None


async def _drivers() -> list:
    """Драйверы ВСЕХ активных карточек — по одному на машину, основная первой.

    Печать в мастерской ОДНОВРЕМЕННАЯ, поэтому наблюдатель обязан обходить машины, а не
    спрашивать «основную». Порядок берём у `list_printers(active_only=True)`: он совпадает с
    `get_default_printer()` (is_default DESC, id), и первым идёт тот же принтер, с которым
    наблюдатель работал раньше — то есть при одной машине не меняется ничего.

    Карточек нет вовсе (установка по .env) — отдаём один драйвер без карточки: ровно то, что
    возвращал прежний `_driver()`.

    ДВЕ ОГОВОРКИ, каждая куплена разбором:

    * Список не прочитался (БД занята автобэкапом с integrity_check) — это НЕ «карточек нет».
      Вернув драйвер без карточки, мы сменили бы ключ состояния с id карточки на 0: накопленная
      печать осиротела бы, тост «печать началась» ушёл бы повторно, а запись в журнал легла бы
      без машины. Честнее пропустить тик — состояние останется нетронутым.
    * Две карточки могут вести на ОДНУ машину: у карточки с пустым адресом поля падают на .env,
      да и один адрес недолго вписать дважды (под разные сопла). Считать их двумя машинами
      значит задвоить журнал, скрины стола и уведомления об одной печати. Дедуплицируем по
      физическому адресу; выигрывает первая, а первой идёт основная."""
    global _default_pid
    try:
        printers = await db.list_printers(active_only=True)
    except Exception:  # noqa: BLE001
        log.warning("printwatch: список карточек не прочитан — тик пропущен", exc_info=True)
        return []
    if not printers:
        _default_pid = None
        return [printerdrv.for_printer(None)]
    _default_pid = int(printers[0].id)
    out, seen = [], set()
    for p in printers:
        drv = printerdrv.for_printer(p)
        key = _machine_key(drv)
        if key in seen:
            log.debug("printwatch: карточка #%s ведёт на ту же машину (%s) — не наблюдаем дважды",
                      getattr(p, "id", "?"), key)
            continue
        seen.add(key)
        out.append(drv)
    return out


def _machine_key(drv) -> str:
    """Ключ ФИЗИЧЕСКОЙ машины: вид протокола + нормализованный адрес.

    Именно адрес, а не id карточки: две карточки на один принтер — обычное дело (разные сопла),
    и наблюдать его дважды значит задвоить всё, что наблюдатель делает. Драйвер без собственного
    адреса (только профиль слайсера, симулятор) дедупликации не подлежит — у него и машины нет."""
    ep = getattr(drv, "endpoint", None)
    if callable(ep):
        try:
            e = ep()
        except Exception:  # noqa: BLE001
            e = None
        addr = str(getattr(e, "ip", "") or getattr(e, "url", "") or "")
        if addr:
            return f"{getattr(drv, 'kind', '?')}:{elegoo.norm_host(addr)}"
    return f"card:{_printer_id(drv)}"


# Видели ли мы печать на прошлом тике — ПО МАШИНАМ: нужно, чтобы отличить «принтер отвалился во
# время печати» (тревога) от «принтер просто выключен» (норма), см. _tick_offline. Общий флаг
# при двух машинах врал бы в обе стороны: печать A держала бы его взведённым, обрыв B читался бы
# как обрыв печатавшей машины, а после одного алерта тревога второй проглатывалась бы.
_was_printing: dict[int, bool] = {}
DONE_PROGRESS = 0.99
MIN_JOURNAL_MINUTES = 3.0   # короче — ложный старт/отмена на прогреве, в журнал не предлагаем


def _printer_id(drv) -> int | None:
    """Карточка принтера, с которой говорит драйвер, или None.

    Источник истины о том, ЧЬЯ это печать. None — честный ответ, а не пропуск: у драйвера без
    карточки (глобальный режим по .env, фолбэк на кэш подписчика) машины действительно нет, и
    выдумывать её нельзя — иначе телеметрия одного принтера ляжет под номером другого."""
    p = getattr(drv, "printer", None)
    pid = getattr(p, "id", None)
    return int(pid) if pid else None


def _key(drv) -> int:
    """Ключ состояния наблюдателя. 0 — «машина не названа» (установка без карточек).

    Отдельно от `_printer_id`, потому что у них разные потребители: в БД уходит честный None
    («чья печать — неизвестно»), а словарям состояния нужен хешируемый ключ."""
    return _printer_id(drv) or 0


_fallback_warned: dict[int, bool] = {}


async def _status(drv=None, max_age: float = 180.0):
    """Свежий статус принтера мастерской ЧЕРЕЗ ДРАЙВЕР. Возвращает (статус, драйвер).

    Драйвер отдаём наружу, чтобы вызывающий не выбирал его второй раз: разъехавшиеся выборы —
    это ровно то, из-за чего печать одной машины попадала бы в журнал другой.

    Обращение в потоке, а не прямо в цикле: у Elegoo `status()` — чтение кэша фонового
    подписчика (даром), а у Moonraker тот же вопрос означает поход по сети. Раз в минуту это
    нормально, но в event loop бота такому не место.

    ФОЛБЭК НА КЭШ ПОДПИСЧИКА — не перестраховка, а защита от тихой потери журнала. Драйвер
    выбирается по `get_default_printer()`; пометь владелец основной карточку без пульта — и
    `status()` вернёт None, а вместе с ним замолчали бы журнал печатей, привязка к заказу,
    скрин стола, свип таймлапсов и автофиниш заказа. Без единой строки в логе. Честнее
    продолжать вести его по телеметрии живого принтера — но СКАЗАТЬ об этом вслух.

    ФОЛБЭК ТОЛЬКО ДЛЯ ОСНОВНОЙ КАРТОЧКИ. `elegoo.cached_status()` без эндпоинта — снимок
    принтера ПО УМОЛЧАНИЮ; отдать его второй машине значило бы записать печать первой в журнал
    под номером второй. Это ровно та ошибка, ради которой в журнале и появился printer_id."""
    drv = drv or await _driver()
    key = _key(drv)
    cs = None
    try:
        cs = await asyncio.to_thread(drv.status, max_age)
    except Exception:  # noqa: BLE001 — принтер не должен ронять фоновый цикл
        log.debug("driver status failed (%s)", getattr(drv, "kind", "?"), exc_info=True)
    if cs and cs.get("ok"):
        _fallback_warned[key] = False
        return cs, drv
    # Фолбэк отдаёт снимок принтера ИЗ .env, поэтому вопрос не «основная ли это карточка», а
    # «ведёт ли она на .env-машину». Пометь владелец основной карточку Bambu — прежний гард
    # пропустил бы телеметрию Elegoo в журнал под номером Bambu.
    lead = getattr(drv, "is_the_default_printer", None)
    if callable(lead):
        allowed = bool(lead())
    else:
        # У драйвера нет своего адреса (только профиль слайсера, симулятор) — фолбэк положен
        # ровно в том случае, ради которого он написан: основной пометили карточку без пульта.
        pid = _printer_id(drv)
        allowed = pid is None or pid == _default_pid
    if not allowed:
        return None, drv                     # чужая машина — снимок .env-принтера ей не подходит
    cached = elegoo.cached_status(max_age=max_age)
    if cached and cached.get("ok"):
        if not _fallback_warned.get(key):
            log.warning("Драйвер основного принтера (%s) телеметрию не отдаёт — автоматика "
                        "мастерской работает по кэшу подписчика. Проверьте, какая карточка "
                        "помечена основной.", getattr(drv, "kind", "?"))
            _fallback_warned[key] = True
        return cached, drv
    return None, drv


def _active_and_progress(state: dict, drv=None) -> tuple[bool, float | None]:
    """«Идёт ли задание и как далеко» — одним вопросом драйверу.

    Раньше здесь разбирался overlay Elegoo напрямую, и всё, что не Centauri, считалось
    простаивающим навсегда. Теперь спрашиваем `job_state`: он принимает и достроенный ключ
    Elegoo, и родное поле Klipper, и форму симулятора."""
    d = drv or _BASE
    js = d.job_state(state or {})
    prog = js.get("progress")
    return bool(js.get("active")), (float(prog) if isinstance(prog, (int, float)) else None)


def _is_printing(cs: dict | None, drv=None) -> bool:
    """Печатает ли принтер прямо сейчас — по свежему снимку фонового подписчика.

    ЧИТАТЬ ТОЛЬКО ЧЕРЕЗ ЭТУ ФУНКЦИЮ. print_stats — ключ СИНТЕТИЧЕСКИЙ: его достраивает
    elegoo.enrich_status (см. elegoo.py, «Centauri такого поля не шлёт»), а прошивка присылает
    только print_status.*. В записях протокола (docs/captures/) print_stats не встречается ни
    разу. Три места читали его напрямую из СЫРОГО состояния и потому всегда получали False:

      • сторож связи — алерт «принтер пропал со связи во время печати» не мог сработать НИКОГДА
        (_was_printing не становился True);
      • свип таймлапсов — гард «пока печатает, по сети не трогаем» не включался, и свип каждые
        10 минут тянул с принтера файл на ~5 МБ по тому же серверу, что отдаёт MJPEG-камеру.
        Ровно та регрессия, ради которой гард и написан (кейс 22.07: камера падала на печати);
      • ожидание сборки таймлапса — выход «печать пошла, дёргать перестаём» не срабатывал.

    Разбор 11.08. Правильный образец был рядом — _active_and_progress выше сначала зовёт
    enrich_status и лишь потом читает overlay.
    """
    if not cs or not cs.get("ok"):
        return False
    return _active_and_progress(cs.get("state") or {}, drv)[0]


async def _tick(bot, drv=None) -> None:
    pid = _printer_id(drv) if drv is not None else None
    b = await db.get_print_binding(pid)
    if not b or not b.get("request_id") or b.get("finished"):
        return
    cs, drv = await _status(drv)                # только свежая телеметрия, через драйвер
    if not cs or not cs.get("ok") or not cs.get("state"):
        return              # связи/данных нет (или подписчик ещё без кадров) — судить о финише нельзя
    active, prog = _active_and_progress(cs.get("state") or {}, drv)
    if active and not b.get("was_active"):
        await db.update_print_binding(pid, was_active=1)
        # пуш клиенту: печать его заказа реально началась (telemetry-driven, срабатывает ОДИН раз —
        # дальше was_active=1 и эта ветка не входит). Идея #5.
        r = await db.get_request(b["request_id"])
        if r and getattr(r, "user_id", None):
            try:
                await notify.notify_user(
                    bot, r.user_id, f"🖨 Ваш заказ «{texts.esc(r.model_label)}» сейчас печатается!")
            except Exception:  # noqa: BLE001
                log.debug("print-started notify failed", exc_info=True)
        return
    if not b.get("was_active"):
        return                                   # ещё не видели печать активной — рано судить о финише
    done = (prog is not None and prog >= DONE_PROGRESS) or not active
    if not done:
        return
    # Финиш привязанной печати: помечаем finished (анти-повтор пуша «началась»). Уведомление о
    # завершении + классификацию (заказ/для себя) + оценку шлёт _tick_journal ЕДИНЫМ опросом
    # (мастер-план, Фаза B). Здесь больше НЕ дёргаем print_confirm — чтобы не задваивать сообщения
    # и НЕ переводить заказ в «Напечатано» автоматически (на заказ бывает несколько печатей —
    # мастер переводит статус вручную).
    await db.update_print_binding(pid, finished=1)
    if not await db.get_request(b["request_id"]):
        await db.clear_print_binding(pid)


# ───── журнал каждой печати: авто-занесение + опрос результата (OpenTodo c587c48b, 5ac0ae9f) ─────
# Наблюдаемая печать КАЖДОЙ машины: ключ — id карточки, значение — {"name", "minutes", ...},
# обновляется, пока телеметрия показывает активную печать. Состояние процесса: при рестарте бота
# активная печать подхватится по живой телеметрии, а завершившиеся в оффлайне — нет (их можно
# занести вручную через «Журнал печатей»). Одна запись на всю студию означала бы, что вторая
# машина, начав печатать, стирает накопленное по первой — и печать первой не попала бы в журнал.
_cur: dict[int, dict] = {}
# Закончившаяся печать, которая ЕЩЁ НЕ легла в журнал: watch_key → задание из _cur (+ ключ
# машины). До 04.10.2026 задание выкидывалось из _cur ДО записи, и любой сбой БД в эту минуту
# («database is locked» под автобэкапом) терял печать навсегда: ни строки журнала, ни вопроса
# мастеру «заказ или для себя», ни списания пластика по ней, а цикл глотал ошибку на debug.
# Теперь задание уходит из памяти только после удачной записи и повторяется каждым тиком.
# Отдельный словарь, а не флаг в _cur: мастер успеет запустить следующую печать раньше, чем
# оживёт БД, и новая печать слилась бы со старой (минуты и граммы одной записи — из двух).
# Это всё ещё память процесса: рестарт неудачную запись теряет, поэтому каждая неудача пишется
# в лог WARNING вместе с данными печати — по ним её можно занести руками.
_unjournaled: dict[str, dict] = {}
JOURNAL_WARN_EVERY_S = 600.0   # одна и та же неудачная запись — в лог не чаще раза в 10 минут
LOOP_WARN_EVERY_S = 1800.0     # сбой подтика цикла (одна машина, один подтик) — раз в 30 минут
_warned_at: dict[str, float] = {}


def _warn_throttled(tag: str, every: float, msg: str, *args) -> bool:
    """WARNING с трассой не чаще раза в `every` секунд на тег; True — если записали.

    Тик раз в минуту (POLL_SEC) без троттлинга залил бы лог одной и той же трассой, а на
    debug, как было раньше, сбой не видел никто: потерянную печать заметили только при
    разборе кода."""
    now = time.monotonic()
    last = _warned_at.get(tag)
    if last is not None and now - last < every:
        return False
    _warned_at[tag] = now
    log.warning(msg, *args, exc_info=True)
    return True


def _job_name(state: dict) -> str:
    ps = state.get("print_status") or {}
    for k in ("filename", "task_name", "taskName", "name"):
        v = ps.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _job_minutes(state: dict) -> float | None:
    dur = (state.get("print_status") or {}).get("print_duration")
    if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
        return float(dur) / 60.0
    return None


# Фактический расход филамента с принтера (OpenTodo 921a08b8). Точное имя ключа в print_status
# Centauri НЕ задокументировано (docs/printer-integration.md: «требует разведки реальных ключей»),
# поэтому: (1) ищем по широкому списку кандидатов — прямой вес (граммы) либо длина (мм→граммы);
# (2) если не нашли — ОДИН раз логируем все ключи print_status, чтобы реальное имя проявилось в
# логе на первой же живой печати и его можно было дописать точечно. Не выдумываем — нет ключа →
# None, и журнал останется на оценке заказа (как раньше).
_GRAMS_KEYS = ("filament_weight", "used_material_weight", "material_weight", "weight_used",
               "filament_grams", "used_grams", "grams_used", "consumed_weight", "filamentWeight",
               "usedMaterialWeight")
_LENGTH_KEYS = ("filament_used", "filament_used_mm", "used_filament", "filament_length",
                "used_length", "length_used", "consumed_filament", "filamentUsed")
_FIL_DIAM_MM = 1.75
_DEFAULT_DENSITY = 1.24   # PLA — если плотность пластика неизвестна (для оценки длина→вес)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _grams_from_mm(mm: float, density: float) -> float:
    """Длина филамента (мм) → вес (г): площадь сечения × длина = объём (мм³→см³) × плотность."""
    import math
    area_mm2 = math.pi * (_FIL_DIAM_MM / 2.0) ** 2
    return mm * area_mm2 * density / 1000.0


def _job_grams(state: dict, density: float = _DEFAULT_DENSITY) -> float | None:
    """Фактический расход филамента (граммы) из телеметрии печати, если принтер его отдаёт."""
    ps = state.get("print_status") or {}
    for k in _GRAMS_KEYS:                       # прямой вес — берём как есть
        v = _num(ps.get(k))
        if v and v > 0:
            return round(v, 1)
    for k in _LENGTH_KEYS:                       # длина (мм) — переводим в граммы
        v = _num(ps.get(k))
        if v and v > 0:
            return round(_grams_from_mm(v, density), 1)
    return None


# Принтер отдаёт таймлапс только когда ПОЛНОСТЬЮ закончил печать (дорисовывает видео минуты
# после финиша телеметрии). Раньше его тянула одноразовая фоновая задача с задержкой ~10 минут —
# и ГИБЛА при рестарте бота (сторож рестартует по расписанию): у печати не оставалось видео ни
# в журнале, ни у клиента (кейс «Овечка Тимми», OpenTodo 2bc57e3e). Теперь — периодический СВИП:
# свежие записи журнала без видео → история заданий принтера → выгрузка в авто-кэш → привязка.
# Переживает рестарты и добирает старые печати, пока задание ещё есть в истории принтера.
TIMELAPSE_SWEEP_SEC = 600         # чаще незачем: видео дорисовывается минутами
# Неделя, а не 48 ч: история принтера хранит 50 заданий (это ~неделя-две печати), и несобранный
# ролик живёт там кадрами, пока задание не вытеснено. 48 ч бросали записи, чьи видео ещё можно
# добрать (кейс «журнал без таймлапсов», 4c6cd9e8). Записи вне истории отсеет сам матчинг по имени.
TIMELAPSE_LOOKBACK_H = 168.0
TIMELAPSE_FAIL_COOLDOWN_S = 7200.0   # 2 ч остывания после сбоя докачки одной записи (см. ниже)
_tl_sweep_at: dict[int, float] = {}   # машина → время последнего свипа (пусто → первый тик)
# Остывание после НЕУДАЧНОЙ докачки конкретной записи (jid → до какого времени не пробовать снова).
# Принтер иногда отказывает в скачивании таймлапса (RemoteDisconnected — встроенный HTTP-сервер
# слаб/занят), а без остывания свип долбил бы те же файлы каждые 10 минут, зря нагружая тот же
# сервер, что отдаёт камеру (кейс 22.07: 3 видео «Тимми» не качались и мешали камере). В памяти —
# сбрасывается при рестарте (тогда одна попытка и снова остывание), этого достаточно.
# Ключ — id ЗАПИСИ ЖУРНАЛА, а запись уже принадлежит машине, поэтому по принтерам не разносим:
# это не одиночка-на-студию, а справочник по записям. Не «доисправлять».
_tl_fail_until: dict[int, float] = {}
# Сборка mp4 — строго одна за раз НА МАШИНУ: рендер грузит принтер минуты, но принтеры разные.
# Общий флаг означал бы, что пока рендерит первый, у второго видео не соберётся вовсе.
_tl_generating: dict[int, bool] = {}


async def _tick_timelapse_sweep(drv=None) -> None:
    if drv is None:
        drv = await _driver()
    key = _key(drv)
    if time.time() - _tl_sweep_at.get(key, 0.0) < TIMELAPSE_SWEEP_SEC:
        return
    _tl_sweep_at[key] = time.time()
    # медиа таймлапсов — в printmedia (волна 3 цикла 2); _printer_ep — в web/printerhub
    # (волна 2 endgame). Отложенных импортов webapp у наблюдателя не осталось (волна 10).
    from . import printmedia
    from .web import printerhub
    pending = []
    fresh: list[dict] = []                       # записи, которым видео привязали ИМЕННО в этот свип
    # только печати ЭТОЙ машины: матчинг идёт по имени задания, а «plate_1.gcode» есть на обеих,
    # и без фильтра к печати первой машины уехало бы видео второй. Записи БЕЗ хозяина забирает
    # основная машина — иначе печати, сделанные до появления printer_id (и в режиме «всё из
    # .env»), навсегда остались бы без таймлапсов.
    pid = _printer_id(drv)
    mine = pid is None or _default_pid is None or pid == _default_pid
    for e in await db.list_print_journal(60, printer_id=pid, with_unowned=mine):
        if (e.get("timelapse") and Path(e["timelapse"]).exists()) \
                or not (e.get("label") or "").strip():
            continue
        # мастер удалил таймлапс этой печати (1faab6cb) — не докачивать его заново
        if int(e.get("tl_deleted") or 0):
            continue
        if time.time() - float(e.get("created_at") or 0) > TIMELAPSE_LOOKBACK_H * 3600:
            continue
        cached = printmedia.cached_timelapse_path(e["label"], _printer_id(drv))
        if cached:                               # видео уже на ПК — просто привязываем
            await db.set_print_journal_fields(e["id"], timelapse=str(cached))
            e["timelapse"] = str(cached)
            fresh.append(e)
            continue
        if _tl_fail_until.get(e["id"], 0.0) > time.time():
            continue                             # недавно не скачалось — остываем, не досим принтер
        pending.append(e)
    # Пока принтер ПЕЧАТАЕТ — не трогаем его по сети: таймлапс качается большим HTTP-файлом (~5 МБ)
    # с того же встроенного сервера принтера, что отдаёт MJPEG-камеру. Во время печати сервер слаб,
    # скачивание конкурирует с потоком камеры и роняет её (ConnectionReset-шторм, кейс 22.07 на
    # печати осьминога). Видео уже лежат на принтере и никуда не денутся — доберём в простое. Из
    # локального кэша привязка выше идёт всегда (это ФС, принтер не трогает).
    printing = False
    if pending:
        printing = _is_printing((await _status(drv))[0], drv)
    if pending and not printing and drv.enabled() and drv.caps.history:
        res = await asyncio.to_thread(drv.history, 8.0)
        if res.get("ok"):
            # имя задания сравниваем через elegoo.normalize_job_name: label записи (из телеметрии
            # print_status.filename) и task_name истории приходят из разных источников и могут
            # отличаться расширением/пробелами/регистром — без нормализации таймлапс «Тимми» никогда
            # не матчился (2bc57e3e). Ключ — каноничное имя → (end_time, url, статус) свежайшей печати.
            best: dict[str, tuple[float, str, int | None]] = {}
            for j in res.get("jobs", []):
                nm = elegoo.normalize_job_name(j.get("name"))
                url = j.get("time_lapse_video_url") or ""
                end = float(j.get("end_time") or 0)
                if nm and url and end >= best.get(nm, (0.0, "", None))[0]:
                    best[nm] = (end, url, j.get("time_lapse_video_status"))
            # диагностика свипа: какие записи ждут видео и что говорит история (jid → статус;
            # 1=кадры без mp4, 2=готов, 0/3=видео не будет, None=в истории нет или статус не пришёл)
            log.info("свип таймлапсов: ждут %s",
                     {e["id"]: best.get(elegoo.normalize_job_name(e["label"]), (0.0, "", None))[2]
                      for e in pending})
            for e in pending:
                _end, url, tl_st = best.get(elegoo.normalize_job_name(e["label"]), (0.0, "", None))
                # Статус видео из истории (разведка lan_service_web, 4c6cd9e8): mp4 существует
                # только при статусе 2. 0/3 — кадров нет/сборка провалилась, качать нечего никогда.
                # 1 — кадры на принтере ЕСТЬ, но ролик не собран: именно из-за таких записей журнал
                # оставался «без таймлапса, хотя они есть» — бот качал несуществующий файл, а принтер
                # на неверное имя молчит вечно. Просим принтер собрать mp4 (как это делает слайсер).
                if not url or tl_st in (0, 3):
                    continue
                if tl_st == 1:
                    # автосборка выключена (config.TIMELAPSE_AUTOBUILD) — рендер не просим, ждём,
                    # пока ролик соберут руками; готовый (статус 2) свип докачает сам
                    if config.TIMELAPSE_AUTOBUILD and not _tl_generating.get(key) and drv.caps.timelapse:
                        _tl_generating[key] = True   # одна сборка за свип: рендер грузит принтер минуты
                        log.info("таймлапс «%s»: mp4 не собран — прошу принтер собрать (1051), "
                                 "запись журнала #%s", e["label"], e["id"])
                        asyncio.create_task(_generate_and_attach_timelapse(e, url, drv))
                    continue
                dest = await printmedia.cache_timelapse_from_url(e["label"], url,
                                                                 printerhub._printer_ep(drv),
                                                                 _printer_id(drv))
                if dest:
                    _tl_fail_until.pop(e["id"], None)
                    await db.set_print_journal_fields(e["id"], timelapse=str(dest))
                    e["timelapse"] = str(dest)
                    fresh.append(e)
                    log.info("таймлапс «%s» добран свипом → запись журнала #%s", e["label"], e["id"])
                else:
                    # принтер отказал в скачивании — остываем 2 ч, чтобы не долбить его (и камеру)
                    _tl_fail_until[e["id"]] = time.time() + TIMELAPSE_FAIL_COOLDOWN_S
    elif pending and not printing and drv.enabled() and drv.caps.timelapse \
            and hasattr(drv, "fetch_timelapse"):
        # Машина БЕЗ истории заданий (Bambu: она живёт в облаке, локально её нет). Ролик ищем
        # прямо по имени задания и качаем ЕЁ ЖЕ транспортом — у Bambu это FTPS, и общий
        # HTTP-путь `cache_timelapse_from_url` тут не годится вовсе.
        for e in pending:
            got = await asyncio.to_thread(drv.latest_timelapse_url, e["label"])
            if not got.get("ok") or not got.get("name"):
                _tl_fail_until[e["id"]] = time.time() + TIMELAPSE_FAIL_COOLDOWN_S
                continue
            dest = printmedia.timelapse_dest(e["label"], _printer_id(drv))
            r = await asyncio.to_thread(drv.fetch_timelapse, got["name"], dest)
            if r.get("ok"):
                _tl_fail_until.pop(e["id"], None)
                await db.set_print_journal_fields(e["id"], timelapse=str(dest))
                e["timelapse"] = str(dest)
                fresh.append(e)
                log.info("таймлапс «%s» скачан с принтера (%s) → запись журнала #%s",
                         e["label"], getattr(drv, "kind", "?"), e["id"])
            else:
                log.info("таймлапс «%s» не скачался (%s) — остываю 2 ч",
                         e["label"], r.get("error"))
                _tl_fail_until[e["id"]] = time.time() + TIMELAPSE_FAIL_COOLDOWN_S
    # Проактивный дослыл: заказ уже «напечатано», а видео принтер дорисовал только сейчас — клиенту
    # в приложении они появятся сами, но и в Telegram дошлём (идемпотентно, см. deliver_late_timelapses).
    if fresh:
        await printmedia.deliver_late_timelapses(fresh)


TIMELAPSE_BUILD_WAIT_S = 240.0     # рендер mp4 на принтере обычно 1-3 мин; дольше — доберём позже
TIMELAPSE_BUILD_POLL_S = 20.0


async def _generate_and_attach_timelapse(e: dict, url: str, drv=None) -> None:
    """Собрать mp4 несобранного таймлапса на принтере (METHOD_TIMELAPSE_EXPORT) и привязать к
    записи журнала. Фоном, чтобы не тормозить цикл printwatch; одна сборка за раз (_tl_generating).

    Принтер отвечает на 1051 СРАЗУ, возвращая БУДУЩИЙ путь mp4, а рендерит ещё минуты (проверено
    живьём 24.07: мгновенный ответ → немедленная докачка ловила TimeoutError несуществующего
    файла). Поэтому ждём ГОТОВНОСТИ по истории заданий — статус станет 2 (готов) / 3 (сбой) —
    и только тогда качаем. Началась печать — прекращаем ждать (не трогаем принтер по сети,
    кейс 22.07 с камерой), доберём в простое после остывания."""
    # медиа таймлапсов — в printmedia (волна 3 цикла 2); _printer_ep — в web/printerhub
    # (волна 2 endgame). Отложенных импортов webapp у наблюдателя не осталось (волна 10).
    from . import printmedia
    from .web import printerhub
    # Драйвер приходит от свипа, а не выбирается заново: задача фоновая, и к моменту её работы
    # «основной» могла стать другая карточка — тогда и гард печати, и history() смотрели бы не
    # на ту машину, а mp4 собирался бы не там, где лежат кадры.
    if drv is None:
        drv = await _driver()
    key = _key(drv)
    try:
        g = await asyncio.to_thread(drv.generate_timelapse, url)
        if not g.get("ok"):
            log.info("сборка таймлапса «%s» не подтвердилась (%s) — остываю 2 ч",
                     e["label"], g.get("error") or "нет ответа")
            _tl_fail_until[e["id"]] = time.time() + TIMELAPSE_FAIL_COOLDOWN_S
            return
        nm = elegoo.normalize_job_name(e["label"])
        out, st = (g.get("url") or ""), None
        deadline = time.time() + TIMELAPSE_BUILD_WAIT_S
        while True:
            if _is_printing((await _status(drv))[0], drv):
                break                            # печать пошла — принтер не дёргаем, доберём позже
            res = await asyncio.to_thread(drv.history, 8.0)
            if res.get("ok"):
                j = next((x for x in res.get("jobs", [])
                          if elegoo.normalize_job_name(x.get("name")) == nm), None)
                st = j.get("time_lapse_video_status") if j else None
                if st == 2:
                    out = (j.get("time_lapse_video_url") or out)
                    break
                if st == 3:                      # рендер провалился — качать нечего
                    break
            if time.time() > deadline:
                break
            await asyncio.sleep(TIMELAPSE_BUILD_POLL_S)
        dest = (await printmedia.cache_timelapse_from_url(e["label"], out,
                                                          printerhub._printer_ep(drv),
                                                          _printer_id(drv))
                if (out and st == 2) else None)
        if dest:
            _tl_fail_until.pop(e["id"], None)
            await db.set_print_journal_fields(e["id"], timelapse=str(dest))
            e["timelapse"] = str(dest)
            log.info("таймлапс «%s» собран принтером по запросу свипа → запись журнала #%s",
                     e["label"], e["id"])
            await printmedia.deliver_late_timelapses([e])
        else:
            # рендер не успел/сорвался/печать началась — остываем, свип доберёт позже (статус
            # в истории к тому моменту будет «готов», и запись уйдёт обычной докачкой)
            log.info("сборка таймлапса «%s»: готовность не дождались (статус %s) — остываю 2 ч",
                     e["label"], st)
            _tl_fail_until[e["id"]] = time.time() + TIMELAPSE_FAIL_COOLDOWN_S
    except Exception:  # noqa: BLE001
        log.debug("timelapse generate failed", exc_info=True)
        _tl_fail_until[e["id"]] = time.time() + TIMELAPSE_FAIL_COOLDOWN_S
    finally:
        _tl_generating[key] = False


async def _capture_bed_shot(jid: int, request_id, printer_id=None) -> None:
    """Скрин стола сразу на финише: печать ещё на столе, а таймлапс подтянется позже.

    `printer_id` — чья машина печатала. Кадр отсюда уезжает в фото результата ЗАКАЗА, то есть
    клиенту: снять его не с той камеры значит показать покупателю чужую деталь."""
    try:
        # capture_bed_shot — в web/printerlive (волна 4 endgame)
        from .web import printerlive
        await printerlive.capture_bed_shot(jid, request_id, printer_id)
    except Exception:  # noqa: BLE001
        log.debug("bed shot failed", exc_info=True)


async def _notify_print_started(job_name: str, printer_id: int | None = None) -> None:
    """Windows-тост при старте печати (OpenTodo d2f67e51): печать пошла, а заказ не привязан —
    спрашиваем сразу, пока мастер помнит, что поставил. Клик по тосту открывает экран «Печать»
    (карточка «Заказ печати» с выбором заказа). Только шина настольной админки: в Telegram не
    дублируем — вопрос актуален ровно тогда, когда мастер за компьютером рядом с принтером."""
    try:
        b = await db.get_print_binding(printer_id)
        if b and b.get("request_id") and not b.get("finished"):
            return                               # печать уже привязана — спрашивать нечего
        # _active_print_header — в web/printerhub (волна 10 endgame): отложенных импортов
        # webapp у наблюдателя больше НЕ ОСТАЛОСЬ.
        from .web import printerhub
        if await printerhub._active_print_header(printer_id):
            return                               # печать запущена ботом из заказа — связь уже есть
        from . import deskevents
        nm = (job_name or "").strip() or "без имени"
        deskevents.push(f"Печать началась: {nm}\nПривязать к заказу? Клик — открыть экран «Печать».",
                        kind="info", go="print")
    except Exception:  # noqa: BLE001
        log.debug("print-started desk event failed", exc_info=True)


async def _tick_journal(bot, drv=None) -> None:
    """Любая печать: активна → копим имя/длительность, финиш → СРАЗУ в журнал (graded=0) +
    опрос результата мастеру (без истечения — запись уже в журнале, OpenTodo 5ac0ae9f).

    Финиш, который не удалось записать (сбой БД), не теряется: он ждёт в _unjournaled, и
    каждый следующий тик этой машины повторяет запись (см. _journal_finished)."""
    cs, drv = await _status(drv)
    key = _key(drv)
    # Сначала — долги: финиши этой машины, которые прошлые тики записать не смогли. ДО проверки
    # телеметрии: запись от принтера уже не зависит, а мастер нередко выключает его сразу после
    # печати — с проверкой впереди такой финиш не записался бы никогда.
    for job in [j for j in _unjournaled.values() if j["key"] == key]:
        await _journal_finished(bot, drv, job)
    if not cs or not cs.get("ok") or not cs.get("state"):
        return              # нет телеметрии (или подписчик ещё без кадров) — состояние не трогаем
    state = cs.get("state") or {}
    active, _prog = _active_and_progress(state, drv)
    cur = _cur.get(key)
    if active:
        name, minutes = _job_name(state), _job_minutes(state)
        grams = _job_grams(state)                # фактический расход филамента (если принтер отдаёт)
        started = cur is None                    # первый тик этой печати (или рестарт бота на ходу)
        if cur is None:
            cur = {"name": "", "minutes": 0.0, "grams": 0.0, "logged": False,
                   "since": time.time()}         # когда МЫ увидели старт — часть ключа наблюдения
            _cur[key] = cur
        # Чья это печать, запоминаем ПОКА ОНА ИДЁТ: на финише телеметрии уже нет, а вместе с ней
        # исчез бы и ответ. Машина печати — та, что печатала, а не та, что «основная» сейчас.
        cur["printer_id"] = _printer_id(drv) or cur.get("printer_id")
        if name:
            cur["name"] = name
        if minutes is not None:
            cur["minutes"] = max(cur["minutes"], minutes)
        if grams is not None:
            cur["grams"] = max(cur["grams"], grams)   # копим максимум ЕЩЁ В ПЕЧАТИ (после финиша телеметрии нет)
        elif not cur.get("logged"):
            # ключ расхода не распознан — один раз логируем ключи, чтобы разведать реальное имя
            log.info("printwatch: расход филамента в телеметрии не распознан; print_status ключи=%s",
                     sorted((state.get("print_status") or {}).keys()))
            cur["logged"] = True
        if started:
            await _notify_print_started(cur["name"], _printer_id(drv))
        return
    if cur is None:
        return
    # Печать закончилась. Из _cur задание уходит сразу, но не в никуда, а в очередь записи
    # (_unjournaled) — без единого await между ними: окна, в котором печати нет нигде, не бывает.
    # Из очереди её отпускает только удачная запись в журнал.
    job = _cur.pop(key)
    if job["minutes"] < MIN_JOURNAL_MINUTES:
        return
    job["key"] = key
    # Ключ наблюдения: машина + когда мы увидели старт + случайный хвост. Время старта — НАШЕ
    # (первый активный тик), а не принтера, и две печати одной машины в одну секунду — не
    # фантазия: так бывает в тестах и после быстрого рестарта бота. Хвост снимает вопрос.
    job["watch_key"] = f"{key}:{int(job.get('since') or time.time())}:{uuid.uuid4().hex[:8]}"
    _unjournaled[job["watch_key"]] = job
    await _journal_finished(bot, drv, job)


async def _journal_row(drv, job: dict) -> dict:
    """Поля записи журнала по закончившейся печати: заказ, пластик, вес и откуда он взялся."""
    # печать была привязана к заказу → предзаполняем граммы/пластик/пластик_id из заказа. sog —
    # происхождение цифры граммов (print_events, мастер-план §2.1): насколько ей можно верить.
    # est — оценка слайсера по времени (пара к факту-minutes, чтобы событие несло est/actual).
    grams, plastic_name, plastic_id, request_id, sog, est, mat = 0.0, "", None, None, None, None, None
    b = await db.get_print_binding(job.get("printer_id"))
    if b and b.get("request_id") and (not job["name"] or not (b.get("job_name") or "")
                                      or b["job_name"] == job["name"]):
        r = await db.get_request(b["request_id"])
        if r:
            request_id = r.id
            # Граммы ВСЕГО заказа как оценку берём ТОЛЬКО для одностольного заказа: у
            # многостольного плитка журнала = одна из нескольких печатей, и вес всего заказа
            # (643 г) на пластину ~11 г — враньё (ed689ac0, кейс осьминога #79). Для
            # многостольного вес возьмём ниже из ФАЙЛА принтера (file_grams, Путь A).
            if int(getattr(r, "plates", 1) or 1) <= 1:
                grams = float(r.grams_total or 0)
                if grams > 0:
                    sog = "slicer_estimate"                   # граммы из оценки заказа (слайсер)
            est = float(r.minutes_total or 0) or None         # оценка времени слайсера
            plastic_name = r.plastic_name or ""
            pl = await db.get_plastic_by_name(plastic_name) if plastic_name else None
            plastic_id = pl.id if pl else None
            mat = pl.material_type if pl else None            # тип материала → пара оценка/факт по материалу
            job["name"] = job["name"] or (b.get("job_name") or r.model_label or "")
    # фактический расход с принтера приоритетнее оценки заказа (это реально израсходованный филамент)
    if float(job.get("grams") or 0) > 0:
        grams = float(job["grams"])
        sog = "telemetry"                                     # реальный вес из телеметрии принтера
    # Путь A (мастер-план): реального расхода нет (телеметрия веса не отдаёт / вес всего заказа
    # для многостольного не годится) — берём вес ТЕКУЩЕГО файла с принтера по имени. file_grams =
    # «Filament Consumption» из Web UI принтера (total_filament_used, оценка слайсера в gcode):
    # пассивный сбор → активный list_files под троттлингом. Так пластина осьминога записывается
    # своими ~11 г, а не 643 г заказа (ed689ac0). Только когда пассивный сбор включён (в проде — да).
    if grams <= 0 and config.ELEGOO_PASSIVE_HARVEST and job.get("name"):
        # вес спрашиваем у ТОЙ машины, что печатала (drv из _status выше), а не у «основной»:
        # имена файлов на двух принтерах совпадают, и чужой вес — враньё в стоимости заказа.
        # Принтер, выключенный сразу после печати, — норма, а не повод не записать печать:
        # без веса файла запись честно уйдёт с «estimate_missing», а не застрянет в очереди.
        try:
            fg = await asyncio.to_thread(drv.file_grams, job["name"])
        except Exception:  # noqa: BLE001
            log.debug("printwatch: вес файла «%s» с принтера не получен", job["name"], exc_info=True)
            fg = None
        if fg and fg > 0:
            grams = float(fg)
            sog = "slicer_estimate"                           # оценка слайсера из gcode (Путь A)
    if grams <= 0 and sog is None:
        sog = "estimate_missing"                              # веса нет ниоткуда — честно помечаем
    return {"name": job["name"] or "печать без имени", "plastic_id": plastic_id,
            "plastic_name": plastic_name, "grams": grams, "request_id": request_id,
            "est": est, "sog": sog, "mat": mat}


async def _journal_finished(bot, drv, job: dict) -> bool:
    """Записать закончившуюся печать в журнал и только ПОСЛЕ этого отпустить её из очереди.

    Сбой (БД занята, диск, что угодно) — задание остаётся в _unjournaled, и следующий тик этой
    машины повторит запись. Дубля не будет: add_print_journal по watch_key узнаёт строку,
    которую успела вставить прошлая попытка. Всё, что после записи (снимок настроек, скрин
    стола, уведомления), — best-effort, каждое в своём try, и повтора записи оно не вызывает:
    из очереди задание уходит раньше. True — печать в журнале."""
    wk = job["watch_key"]
    try:
        if job.get("row") is None:
            # Поля считаем ОДИН раз. Повтор после сбоя БД не должен снова ходить к принтеру за
            # весом файла и перечитывать привязку, которую к тому времени мог занять уже
            # следующий заказ.
            job["row"] = await _journal_row(drv, job)
        row = job["row"]
        # СРАЗУ заносим в журнал без оценки — больше никаких «опрос устарел» (OpenTodo 5ac0ae9f)
        jid = await db.add_print_journal(
            label=row["name"], plastic_id=row["plastic_id"], plastic_name=row["plastic_name"],
            grams=row["grams"], minutes=round(job["minutes"], 1), graded=0,
            request_id=row["request_id"], est_minutes=row["est"], source_of_grams=row["sog"],
            material_type=row["mat"], printer_id=job.get("printer_id"), watch_key=wk)
    except Exception:  # noqa: BLE001 — задание остаётся в очереди, повторим следующим тиком
        job["attempts"] = int(job.get("attempts") or 0) + 1
        _warn_throttled(f"journal:{wk}", JOURNAL_WARN_EVERY_S,
                        "printwatch: печать «%s» (машина %s, %.1f мин, %g г) НЕ записана в "
                        "журнал, попытка %d — повторю на следующем тике",
                        job.get("name") or "без имени", job.get("printer_id") or "—",
                        job["minutes"], float(job.get("grams") or 0), job["attempts"])
        return False
    _unjournaled.pop(wk, None)
    _warned_at.pop(f"journal:{wk}", None)
    # Катушки CC2 Control: склад фоном не опрашивается — один снимок после печати переносит её
    # расход, граммы в эту запись журнала и склад цветов для клиентов (без CC2 — ничего).
    cc2spools.schedule_after_print(job.get("printer_id"))
    if job.get("attempts"):
        log.info("printwatch: печать «%s» записана в журнал (#%s) с попытки %d",
                 row["name"], jid, job["attempts"] + 1)
    name, grams = row["name"], row["grams"]
    plastic_name, request_id = row["plastic_name"], row["request_id"]
    # Настройки, с которыми задание уходило на принтер (OpenTodo 8e8d4873): снимок сделан при
    # заливке/старте и лежит под именем файла — привязываем его к записи журнала, пока имя
    # ещё точное. Позже (после переименования задания) связать было бы уже не по чему.
    try:
        snap = await db.find_print_settings(name, elegoo.normalize_job_name(name),
                                            printer_id=job.get("printer_id"))
        if snap:
            await db.set_print_journal_fields(jid, settings_id=snap["id"])
    except Exception:  # noqa: BLE001
        log.debug("не привязать настройки печати к записи журнала", exc_info=True)
    # Таймлапс подберёт периодический свип (_tick_timelapse_sweep): одноразовая задача с
    # задержкой гибла при рестарте бота, и печать оставалась без видео (2bc57e3e).
    # Скрин стола сразу на финише — чтобы у печати всегда была картинка, даже до таймлапса.
    asyncio.create_task(_capture_bed_shot(jid, request_id, job.get("printer_id")))
    survey = {"jid": jid, "name": name, "minutes": round(job["minutes"], 1),
              "grams": grams, "plastic_name": plastic_name, "request_id": request_id}
    # Вес в карточке уведомления: печать без заказа и без телеметрии веса давала «0 г» —
    # честнее «вес ?» в стиле «пластик ?» (OpenTodo db0f0337).
    w_txt = f"{grams:g} г" if grams > 0 else "вес ?"
    # Печать УЖЕ привязана к заказу мастером (во время печати) — переспрашивать «заказ или для
    # себя» глупо: ответ известен. Запись журнала создана с этим request_id, привязку закрываем.
    # Опрос качества (успех/брак) в Telegram ВЫПИЛЕН (OpenTodo b02bc4c6): оценка живёт в
    # «Журнале печатей» приложения, мастеру за компьютером — Windows-тост вместо чат-пинга.
    if request_id:
        try:
            # Закрываем привязку, только если она всё ещё НАША: пока запись ждала БД, мастер мог
            # запустить следующую печать и привязать её к другому заказу — её не трогаем.
            b = await db.get_print_binding(job.get("printer_id"))
            if b and b.get("request_id") == request_id:
                await db.clear_print_binding(job.get("printer_id"))
        except Exception:  # noqa: BLE001
            log.debug("clear print binding failed", exc_info=True)
        try:
            from . import deskevents
            deskevents.push(f"Печать завершена: {name}\nЗаказ #{request_id}. "
                            "Оцените результат в «Журнале печатей».",
                            kind="info", go="masterlog")
        except Exception:  # noqa: BLE001
            log.debug("print-done desk event failed", exc_info=True)
        try:
            await db.add_admin_notification(
                "print_journal", title=f"Печать завершена: {name}"[:120],
                body=(f"{w_txt} · {round(job['minutes'])} мин · "
                      f"{plastic_name or 'пластик ?'} · заказ #{request_id}. Оцените результат."),
                payload={"jid": jid, "label": name, "request_id": request_id})
        except Exception:  # noqa: BLE001
            log.debug("print-done app notification failed", exc_info=True)
        return True
    # Печать не привязана — вот тут классификация уместна: спрашиваем «заказ или для себя»
    # (мастер-план, Фаза B). Кнопка ведёт в приложение — выбор заказа со списком и картинками.
    try:
        await notify.notify_admins(
            bot, texts.print_classify(survey), audience=notify.ALL_STAFF,   # у стола стоит оператор
            reply_markup=keyboards.print_classify(jid, request_id,
                                                  webapp_url=config.WEBAPP_PUBLIC_URL))
    except Exception:  # noqa: BLE001
        log.debug("print-classify notify failed", exc_info=True)
    # Дубль в «Уведомления» приложения (интеграция по задаче 4effbc1b): если чат-пинг утонул,
    # неразобранная печать ждёт мастера в разделе с той же кнопкой выбора заказа.
    try:
        await db.add_admin_notification(
            "print_classify", title=f"Печать завершена: {name}"[:120],
            body=(f"{w_txt} · {round(job['minutes'])} мин · {plastic_name or 'пластик ?'}. "
                  "К какому заказу отнести?"),
            payload={"jid": jid, "label": name})
    except Exception:  # noqa: BLE001
        log.debug("print-classify app notification failed", exc_info=True)
    return True


# ───── история телеметрии (фидбэк 20.07: «тащить данные всегда — для аналитики») ─────
# Источник — ТОЛЬКО кэш фонового подписчика: ни одного лишнего обращения к принтеру.
# Кадры: во время печати — каждый тик (POLL_SEC), в простое — каждый TELE_IDLE_EVERY-й
# (температуры в простое почти не меняются, а БД не резиновая). Ретенция — раз в сутки.
TELE_IDLE_EVERY = 10          # простой: 1 снимок на ~10 минут
TELE_KEEP_DAYS = 90
SETTINGS_KEEP = 400           # снимков настроек заданий (8e8d4873): ~год заливок мастера
# Счётчик разрежения — СВОЙ у каждой машины: с общим фаза счётчика первой решала бы, писать ли
# кадр второй, и в простое история одной машины оказалась бы вдвое реже другой.
_tele_tick: dict[int, int] = {}
# А вот чистка — общая на таблицу и НЕ по машинам: ретенция и prune_print_settings глобальны,
# пер-принтерная версия начала бы чистить БД по разу на каждую машину в сутки. Не «доисправлять».
_tele_pruned = 0.0            # time.time() последней чистки


_last_phase: dict = {}


async def _note_phase(drv, state: dict, layer: int | None, active: bool) -> None:
    """Смена фазы печати (printing → paused → stopped…) — в print_phases с последним слоем.

    «Допечатка со слоя» подставляет отсюда слой паузы вместо ввода руками. Пишем ТОЛЬКО переходы,
    а не каждый тик: слой при этом берём текущий, а для паузы/стопа — последний виденный в печати
    (в момент паузы принтер иногда уже не вещает current_layer)."""
    try:
        phase = str((drv.job_state(state) or {}).get("phase") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return
    if not phase:
        phase = "printing" if active else "idle"
    key = _key(drv)
    prev = _last_phase.get((key, "phase"))
    if layer is not None and active:
        _last_phase[(key, "layer")] = layer
    if prev == phase:
        return
    _last_phase[(key, "phase")] = phase
    if prev is None:
        return                                   # первый кадр после старта бота — не переход
    z = (state.get("print_status") or {}).get("current_z")
    try:
        await db.add_print_phase(printer_id=_printer_id(drv), phase=phase,
                                 layer=layer if layer is not None else _last_phase.get((key, "layer")),
                                 z=float(z) if isinstance(z, (int, float)) else None,
                                 job_name=_job_name(state))
    except Exception:  # noqa: BLE001
        log.debug("фаза печати: не записать", exc_info=True)


async def _tick_telemetry(drv=None) -> None:
    cs, drv = await _status(drv)
    key = _key(drv)
    _tele_tick[key] = _tele_tick.get(key, 0) + 1
    if not cs or not cs.get("ok"):
        return                                   # принтер оффлайн — истории нечего писать
    state = cs.get("state") or {}
    if not state:
        return                                   # подписчик подключился, но кадров ещё не было
    active, prog = _active_and_progress(state, drv)
    if not active and (_tele_tick[key] % TELE_IDLE_EVERY):
        return
    # summarize/extras разбирают SDCP: слоты CANVAS, коды ошибок, вентиляторы. У другого
    # вендора этих полей нет, и выдумывать их нельзя — тот же гейт стоит в веб-слое.
    sdcp = getattr(drv, "kind", "") in ("elegoo", "", "fake")
    summ = elegoo.summarize(state) if sdcp else {}
    ex = elegoo.extras(state) if sdcp else {}
    layer = (state.get("print_status") or {}).get("current_layer")
    layer_int = int(layer) if isinstance(layer, (int, float)) and not isinstance(layer, bool) else None
    await _note_phase(drv, state, layer_int, active)
    await db.add_printer_telemetry(
        printer_id=_printer_id(drv),
        printing=1 if active else 0,
        nozzle=summ.get("extruder_temp"), nozzle_target=summ.get("extruder_target"),
        bed=summ.get("bed_temp"), bed_target=summ.get("bed_target"),
        chamber=ex.get("chamber_temp"), progress=prog,
        layer=int(layer) if isinstance(layer, (int, float)) and not isinstance(layer, bool) else None,
        filename=_job_name(state) if active else "",
        light=ex.get("light"), active_slot=ex.get("active_slot"),
        fan_model=(ex.get("fans") or {}).get("model"))


async def _tick_retention() -> None:
    """Суточная чистка истории — ОДИН раз на студию, а не на каждую машину.

    Раньше жила внутри тика телеметрии. С обходом машин это значило бы N чисток в сутки: сама
    ретенция и prune_print_settings глобальны, и повторные проходы просто зря блокировали бы БД."""
    global _tele_pruned
    if time.time() - _tele_pruned <= 86400:
        return
    _tele_pruned = time.time()
    n = await db.prune_printer_telemetry(TELE_KEEP_DAYS)
    if n:
        log.info("printer_telemetry: удалено %d снимков старше %d дней", n, TELE_KEEP_DAYS)
    # Снимки настроек заданий (8e8d4873) чистим тем же суточным тиком: файл на принтере
    # живёт недели, а таблица росла бы вечно. Снимки, привязанные к записям журнала,
    # prune не удаляет — иначе у старой печати пропало бы «чем печатали».
    n = await db.prune_print_settings(SETTINGS_KEEP)
    if n:
        log.info("print_settings: удалено %d старых снимков настроек", n)


async def _tick_offline(bot, drv=None) -> None:
    """Принтер пропал во время печати — сообщить мастеру.

    Такого события в проекте не было вовсе: телеметрия просто «замолкала», и о сорванной
    ночной печати мастер узнавал утром по холодному столу. Тревожим ТОЛЬКО если печать шла
    и связь пропала: свободный выключенный принтер — нормальное состояние, будить из-за него
    незачем. Повтор гасит троттлинг notify.alert по ключу (5 минут).
    """
    cs, drv = await _status(drv)
    key = _key(drv)
    alive = bool(cs and cs.get("ok"))
    printing = _is_printing(cs, drv)

    if printing:
        _was_printing[key] = True
    elif _was_printing.get(key) and not alive:
        # связи нет, а последнее, что мы видели, — идущая печать
        # Единственный операционный алерт из всех тринадцати: чинить идёт тот, кто в мастерской.
        # Имя машины В ТЕКСТЕ и В КЛЮЧЕ троттлинга: с одним принтером это украшение, с двумя —
        # необходимость. Общий ключ проглотил бы тревогу второй машины, если первая отвалилась
        # в те же пять минут, а без имени мастер не знает, к какой из них идти.
        who = getattr(getattr(drv, "printer", None), "name", "") or "Принтер"
        await notify.alert(f"{who}: пропал со связи во время печати — проверьте мастерскую",
                           key=f"printer_offline_{getattr(drv, 'kind', '?')}_{who}",
                           audience=notify.ALL_STAFF)
        _was_printing[key] = False


# Отчёты прошивки сообщества (CC2 Control, `printer_report`): предупреждения и аварии Klipper —
# детектор спагетти, ошибки печати. Стоковый MQTT таких событий не шлёт вовсе. Приходят тем же
# опросом /api/printer, что дописывает задание (cc2community.fetch_job, только в печати). Один
# алерт на событие: их event_id одинаков у повторов того же события подряд.
_report_seen: dict[int, str] = {}
REPORT_MAX_AGE_S = 600      # событие старше (было до старта бота) — уже не новость


async def _tick_cc2_report(drv=None) -> None:
    if drv is None:
        return
    host = drv.community_host()
    if not host or not drv.community():
        return
    hit = cc2community.report(host)
    if not hit:
        return
    got, rep = hit
    key = _key(drv)
    if _report_seen.get(key) == rep["event_id"]:
        return
    _report_seen[key] = rep["event_id"]
    # RESUME и INFO — не тревога; давнее событие (бот перезапустился) — тоже
    if rep["level"] not in (1, 2) or rep["age"] + (time.time() - got) > REPORT_MAX_AGE_S:
        return
    who = getattr(getattr(drv, "printer", None), "name", "") or "Принтер"
    level = cc2community.REPORT_LEVELS.get(rep["level"], "")
    await notify.alert(f"{who}: прошивка сообщает ({level}, код {rep['code']}): "
                       f"{rep['message'] or 'без текста'}",
                       key=f"cc2_report_{key}_{rep['event_id']}", throttle_sec=3600,
                       audience=notify.ALL_STAFF)


async def loop(bot) -> None:
    """Фоновый цикл; инертен, если ничего не привязано или нет телеметрии принтера.

    Обходит ВСЕ активные карточки: печать в мастерской одновременная, и наблюдатель, знающий
    только «основную» машину, не завёл бы журнал второй, не заметил бы её обрыва и не добрал
    таймлапс. Порядок подтиков внутри машины значим: `_tick` ставит finished=1 раньше, чем
    `_tick_journal` читает привязку и закрывает её. Каждый подтик в своём try — сбой одной
    машины не должен останавливать наблюдение за второй.

    Сбои подтиков — WARNING с троттлингом, а не debug: на debug глоталась в том числе потеря
    записи журнала (04.10.2026), и узнать о ней из лога было нельзя. Принтер вне сети сюда
    не доходит — его гасит _status, — так что сюда попадают настоящие ошибки."""
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            drivers = await _drivers()
        except Exception:  # noqa: BLE001
            _warn_throttled("loop:drivers", LOOP_WARN_EVERY_S, "printwatch: драйверы не получены")
            continue
        # пары «имя, как запустить»: корутины создаём по одной прямо перед await, иначе
        # непройденный подтик остался бы висеть несозданным-но-созданным объектом
        ticks = (("tick", lambda d: _tick(bot, d)),
                 ("offline tick", lambda d: _tick_offline(bot, d)),
                 ("cc2 report tick", _tick_cc2_report),
                 ("journal tick", lambda d: _tick_journal(bot, d)),
                 ("timelapse sweep", _tick_timelapse_sweep),
                 ("telemetry tick", _tick_telemetry))
        for drv in drivers:
            for name, tick in ticks:
                try:
                    await tick(drv)
                except Exception:  # noqa: BLE001
                    # тег — подтик + машина: сбой одной не должен глушить в логе сбой другой
                    _warn_throttled(f"loop:{name}:{_key(drv)}", LOOP_WARN_EVERY_S,
                                    "printwatch: %s упал (%s, карточка %s)", name,
                                    getattr(drv, "kind", "?"), _printer_id(drv) or "—")
        try:
            await _tick_retention()             # суточная чистка — одна на студию, не на машину
        except Exception:  # noqa: BLE001
            _warn_throttled("loop:retention", LOOP_WARN_EVERY_S, "printwatch: ретенция упала")
