"""Сторож ботов (много-ботовый): следит за каждым из BOTS и перезапускает упавший.

Добавить ещё бота = дописать запись в список BOTS ниже (общие) или в watchdog_local.py
(личные, вне git). Для каждого бота:
  name     — имя для логов;
  cwd      — рабочая папка;
  command  — как запускать (список аргументов, полный путь к python нужного venv);
  match    — паттерн командной строки ИМЕННО этого бота вида «проект*скрипт»
             (например 'print_cost_bot*run.py') — чтобы при рестарте гасить только его,
             не задев сам сторож (watchdog.py) и других ботов;
  health   — URL /health (HTTP-проверка) ИЛИ None → бот считается живым, если его процесс есть
             (для Telegram-бота без веб-сервера);
  log      — куда писать stdout/stderr запущенного бота (или None).

Кроме ботов есть TASKS — периодические задачи обслуживания (не процессы): каждая
запускается раз в свои `every` секунд внутри основного цикла. Добавить задачу =
функция + запись в TASKS.

Запуск — через Автозагрузку (start_watchdog.vbs кладётся в shell:startup) или вручную:
  .venv\\Scripts\\python.exe watchdog.py

ПЕРЕЗАПУСК после правки этого файла (список BOTS читается только при старте).
Сначала убить сам процесс сторожа, и лишь потом трогать задачу Планировщика:
  taskkill /PID <pid watchdog.py> /F        (без /T — иначе умрут и подопечные боты)
  Stop-ScheduledTask  -TaskName pcb_watchdog
  Start-ScheduledTask -TaskName pcb_watchdog
Одного Stop-ScheduledTask НЕ хватает: он гасит обёртку wscript, а python-процесс
сторожа остаётся жить сиротой, и после Start получаются ДВА сторожа, которые
наперегонки перезапускают одних и тех же ботов. Подопечные боты переживают
перезапуск сторожа: он находит их по match в командной строке, а не по родителю.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _env(key: str) -> str:
    """Значение ключа из окружения, иначе из .env бота; «» если нигде нет.

    Свой десятистрочный парсер, а не импорт app.config: сторож принципиально живёт на одной
    стандартной библиотеке (никаких зависимостей venv) и не должен вызывать в своём процессе
    load_dotenv(override=True).
    """
    raw = (os.getenv(key) or "").strip().strip('"').strip("'")
    if raw:
        return raw
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _data_dir() -> Path:
    """Каталог данных: DATA_DIR из окружения/.env, иначе <папка бота>/data (C2)."""
    raw = _env("DATA_DIR")
    return Path(raw) if raw else ROOT / "data"


DATA_DIR = _data_dir()

CHECK_EVERY = 30      # сек между проверками
FAIL_LIMIT = 3        # подряд неудач до перезапуска
WARMUP = 20           # сек на подъём после старта (не считаем неудачей)
KILL_GRACE = 15       # сек ожидания реальной смерти процессов после kill()
LOG_MAX_MB = 64       # больше — файл лога подопечного уезжает в .1 и начинается заново

# Одиночный обрыв соединения — НЕ повод считать бота мёртвым (10.09.2026). Замер: 200 проверок
# подряд, 9 отказов (4.5%) на ЖИВОМ боте — мгновенный RST (WinError 10054) или молчание до
# таймаута, при том что соседние запросы веб-сервер в те же секунды обслуживал без пауз.
#
# Виноват НЕ бот. Рядом подняли примитивный http.server на потоках (никакого asyncio) и долбили
# его и бота одинаково — рвались ОБА, в одни и те же секунды и с одинаковой частотой. Мигают и
# все четыре подопечных сразу, на разных портах и в разных процессах. Значит рвёт что-то под
# ними: на машине стопка из пяти сетевых фильтр-драйверов (adgnetworkwfpdrv у AdGuard 8, gwdrv,
# ndisrd, ndiswgc, gameflt), и loopback идёт через них.
#
# Пока это не разобрано, сторож обязан мигания переживать: раньше три подряд давали ЛОЖНЫЙ
# перезапуск живого бота, и именно из таких состоит большинство «перезагрузок» в истории
# (30.08 — 31 штука, 02.09 — 26).
HEALTH_TRIES = 3      # попыток /health внутри ОДНОЙ проверки, прежде чем счесть бота мёртвым
HEALTH_RETRY = 4      # сек между этими попытками: отказы идут пачками по 3-6 с, надо их пересидеть
HEALTH_TIMEOUT = 5    # сек на один заход; живой /health отвечает за 0.05 с, так что запас огромный
BLIP_REPORT = 3600    # сек между сводками «сколько миганий пережили» (иначе лог зальёт)
NOTIFY_COOLDOWN = 1800  # сек между сообщениями владельцу об одной и той же беде

WD_LOG = DATA_DIR / "logs" / "watchdog.log"


def say(msg: str) -> None:
    """Сказать в консоль И в файл.

    Раньше сторож писал только print(). Запущенный Планировщиком через wscript, он не имеет
    консоли — весь его вывод уходил в никуда, и решения сторожа не сохранялись НИГДЕ (проверено
    11.08: ноль вхождений '[watchdog]' в логах за всю историю). Из-за этого инцидент 10.08 с
    двойным запуском бота пришлось восстанавливать по косвенным уликам, а не прочитать."""
    line = f"[watchdog] {msg}"
    print(line, flush=True)
    try:
        WD_LOG.parent.mkdir(parents=True, exist_ok=True)
        _rotate(WD_LOG)
        with open(WD_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:  # noqa: BLE001 — сторож не должен падать из-за логирования
        pass


def _rotate(path: Path) -> None:
    """Ротация одним поколением: файл больше LOG_MAX_MB → .1 (старый .1 затирается).

    Без неё лог подопечного растёт вечно: на 11.08 data/logs/bot_watchdog.out весил 1.05 ГБ
    на том же диске, где лежит база и бэкапы."""
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_MB * 1024 * 1024:
            backup = path.with_suffix(path.suffix + ".1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
    except OSError:
        pass


_notified: dict[str, float] = {}


def notify(key: str, text: str) -> None:
    """Написать владельцу в Telegram. Молча ничего не делает, если нечем или рано.

    Появилось после 09.09.2026: бот стоял 8 ч 58 мин, сторож каждые две минуты писал в файл
    «НЕ УМЕР … новый экземпляр НЕ запускаю» — 277 раз, и НИКТО этого не увидел. Лог, который
    читают только после жалобы, сторожем не является. Шлём напрямую через api.telegram.org, а не
    через бота: зовём как раз тогда, когда бот лежит. `key` глушит повторы про одну и ту же беду
    на NOTIFY_COOLDOWN — иначе за ночь набежало бы двести сообщений."""
    now = time.time()
    if now - _notified.get(key, 0.0) < NOTIFY_COOLDOWN:
        return
    token, ids = _env("BOT_TOKEN"), _env("ADMIN_IDS")
    if not token or not ids:
        return
    _notified[key] = now
    for chat in [c.strip() for c in ids.replace(";", ",").split(",") if c.strip()]:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": f"🐕 сторож: {text}"}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception as e:  # noqa: BLE001 — не достучались, значит не достучались
            say(f"не смог написать в Telegram {chat}: {type(e).__name__}")


def _oem(raw: bytes) -> str:
    """Вывод консольных утилит (taskkill) в человеческий вид: он в OEM-кодировке, не в UTF-8."""
    for enc in ("cp866", "cp1251", "utf-8"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").strip()


def _port_busy(url: str) -> bool | None:
    """Занят ли порт из health-URL. True — занят, False — свободен, None — не определить.

    Смысл запрета «не убедились в смерти — не запускаем» ровно один: не дать двум экземплярам
    драться за порт (инцидент 10.08). Если зависший процесс порт уже НЕ держит, драки не будет,
    и запрет только продлевает простой — как 09.09, когда он растянулся на девять часов."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
        if not host or not port:
            return None
        # БЕЗ SO_REUSEADDR: в Windows он разрешает привязку к уже занятому порту, и проверка
        # всегда говорила бы «свободен».
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return False
            except OSError:
                return True
    except Exception:  # noqa: BLE001
        return None

# ===== Боты под присмотром. =====
# Общие записи проекта — здесь; личные (другие боты, агенты пульта) — в watchdog_local.py
# рядом с этим файлом: он не в git, сторож подхватывает его при старте (см. _load_local).
BOTS = [
    {
        "name": "print_cost_bot",
        "cwd": str(ROOT),
        "command": [str(PY), "-u", "run.py"],
        "match": f"{ROOT.name}*run.py",                   # папка*скрипт — только этот бот
        "health": "http://127.0.0.1:8081/health",         # HTTP-проверка; None → по процессу
        "log": str(DATA_DIR / "logs" / "bot_watchdog.out"),
    },
    # Пример ещё одного бота (Telegram-only, без веба — живость по наличию процесса):
    # {
    #     "name": "other_bot",
    #     "cwd": r"C:\\path\\to\\other_bot",
    #     "command": [r"C:\\path\\to\\other_bot\\.venv\\Scripts\\python.exe", "-u", "main.py"],
    #     "match": "other_bot*main.py",
    #     "health": None,
    #     "log": r"C:\\path\\to\\other_bot\\data\\watchdog.out",
    # },
]


# ===== Периодические задачи обслуживания (не процессы). =====
# Запись: {"name": ..., "every": <сек>, "run": <функция без аргументов>}. Своих задач у проекта
# сейчас нет; личные задачи машины приезжают из watchdog_local.py.
TASKS: list[dict] = []

LOCAL_FILE = ROOT / "watchdog_local.py"


def _load_local() -> None:
    """Подхватить watchdog_local.py: личные подопечные и задачи этой машины (файл вне git).

    Ошибка в нём не должна оставить без присмотра самого бота: говорим и работаем дальше."""
    if not LOCAL_FILE.exists():
        return
    try:
        spec = importlib.util.spec_from_file_location("watchdog_local", LOCAL_FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.extend(sys.modules[__name__])
        say(f"watchdog_local.py: подопечных теперь {len(BOTS)}, задач {len(TASKS)}")
    except Exception as e:  # noqa: BLE001 — сторож не должен умирать из-за личного файла
        say(f"watchdog_local.py не загружен: {e}")


def _procs(match: str) -> list[int]:
    """PID python-процессов, чья командная строка подходит под '*<match>*'.

    Фильтр по ИМЕНИ обязан быть 'python%', а не 'python.exe'. Причина (разбор 11.08): venv
    собран поверх Store-Python, и .venv\\Scripts\\python.exe там — редиректор. Он запускает
    настоящий интерпретатор ОТДЕЛЬНЫМ процессом с именем python3.13.exe и той же командной
    строкой; все multiprocessing-дети наследуют это же имя. То есть Name='python.exe' видел
    только процесс-обёртку на пару мегабайт, а настоящий бот со всей его памятью в выборку не
    попадал вовсе — kill() бил по обёртке и надеялся на /T. Отбор по командной строке ниже
    не ослаблен, чужие python-процессы под него по-прежнему не подходят.

    Неудачу опроса это отражает как «никого нет» — для healthy()/kill() так и надо. Там, где
    от ответа зависит запуск нового экземпляра, звать _procs_checked."""
    return _procs_checked(match) or []


def _procs_checked(match: str) -> list[int] | None:
    """То же, но None = «спросить не удалось» (PowerShell не уложился в таймаут и т.п.).

    Разница принципиальная там, где решается судьба перезапуска: пустой список значит «никого
    нет, можно стартовать», а None — «я не знаю». Старый _procs отдавал [] на любое исключение,
    и на загруженной машине (а именно там всё и ломается) неудачный опрос читался как «старый
    бот умер» — то есть механизм, который должен был предотвратить дубликат, сам его и
    разрешал."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
          "Where-Object { $_.CommandLine -like '*" + match + "*' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        say(f"не смог опросить процессы по '{match}': {e}")
        return None
    if out.returncode != 0:
        say(f"опрос процессов по '{match}' вернул rc={out.returncode}")
        return None
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


_blips: dict[str, int] = {}          # пережитые мигания /health по ботам
_blip_said = 0.0                     # когда последний раз отчитывались сводкой
_health_err: dict[str, str] = {}     # последняя причина отказа — чтобы писать её в лог


def _probe(url: str) -> tuple[bool, str]:
    """Один заход на /health. Возвращает (жив, чем ответил при отказе)."""
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as r:
            if r.status == 200 and r.read(16).strip() == b"ok":
                return True, ""
            return False, f"ответ {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}".split("\n")[0][:120]


def healthy(bot: dict) -> bool:
    """Жив ли бот. HTTP-проверка делает до HEALTH_TRIES заходов, а не один.

    Один заход врал: замер 10.09 показал 4.5% отказов на ЖИВОМ боте — мгновенный RST
    (WinError 10054) или молчание до таймаута, при том что соседние запросы веб-сервер в эти же
    секунды обслуживал без задержки. Три таких подряд запускали ложный перезапуск, и из них
    состоит большинство «перезагрузок» бота (30.08 — 31 штука, 02.09 — 26). Настоящее зависание
    от мигания отличается тем, что не проходит: повтор через пару секунд его не спасёт."""
    url = bot.get("health")
    if not url:
        return bool(_procs(bot["match"]))   # без HTTP — живой, если процесс есть
    err = ""
    for attempt in range(HEALTH_TRIES):
        ok, err = _probe(url)
        if ok:
            if attempt:
                _blips[bot["name"]] = _blips.get(bot["name"], 0) + 1
            return True
        if attempt < HEALTH_TRIES - 1:
            time.sleep(HEALTH_RETRY)
    _health_err[bot["name"]] = err
    return False


def _say_blips() -> None:
    """Раз в BLIP_REPORT — строка о том, сколько миганий пережили. Поштучно писать нельзя: их
    десятки в час, лог бы утонул. А знать их число надо: рост означает, что бот всё хуже
    отвечает на новые соединения, и до настоящих зависаний недалеко."""
    global _blip_said
    if time.time() - _blip_said < BLIP_REPORT:
        return
    _blip_said = time.time()
    if _blips:
        parts = ", ".join(f"{n}: {c}" for n, c in sorted(_blips.items()))
        say(f"пережито миганий /health за час ({parts})")
        _blips.clear()


def kill(bot: dict) -> None:
    # /T валит ВСЁ дерево процессов бота, включая дочерние process-pool воркеры (cpupool):
    # без него рендер-воркер с torch/CUDA осиротевал при каждом рестарте и продолжал держать
    # VRAM — за десяток рестартов накапливалось до ~50% видеопамяти.
    pids = _procs(bot["match"])
    if not pids:
        say(f"{bot['name']}: убивать некого — процессов по '{bot['match']}' не найдено")
    for pid in pids:
        try:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                say(f"{bot['name']}: taskkill PID {pid} (с деревом)")
            else:
                # 09.09.2026 taskkill девять часов подряд не мог добить PID 41832, а сторож
                # писал в лог «(с деревом)», как будто всё получилось: код возврата и stderr
                # выбрасывались. Без них разбор инцидента упирался в «не убился, почему — негде
                # посмотреть».
                detail = _oem(r.stderr) or _oem(r.stdout) or "без объяснений"
                say(f"{bot['name']}: taskkill PID {pid} НЕ сработал, rc={r.returncode}: {detail[:200]}")
        except Exception as e:  # noqa: BLE001
            say(f"{bot['name']}: taskkill PID {pid} не сработал: {e}")


def start(bot: dict) -> None:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    out = subprocess.DEVNULL
    logp = bot.get("log")
    if logp:
        Path(logp).parent.mkdir(parents=True, exist_ok=True)
        _rotate(Path(logp))     # иначе растёт вечно (на 11.08 накопился 1.05 ГБ)
        out = open(logp, "ab", buffering=0)
    subprocess.Popen(bot["command"], cwd=bot["cwd"], stdout=out, stderr=out, creationflags=flags)


def restart(bot: dict, reason: str) -> None:
    """Перезапуск с ОБЯЗАТЕЛЬНОЙ проверкой, что старый экземпляр действительно умер.

    Раньше было kill() → sleep(2) → start(), без единой проверки. А kill() молча не делает
    ничего, если _procs() вернул пусто — в том числе когда PowerShell не уложился в таймаут на
    загруженной машине. 10.08 в 23:24 это и случилось: новый бот поднялся поверх живого
    старого, не смог занять порт, остался без /health навсегда — и сторож продолжил считать
    его мёртвым и плодить следующих, каждый со своим пулом и своим torch-воркером.
    Теперь: не убедились в смерти — НЕ запускаем. Лучше остаться без перезапуска и написать
    об этом в лог, чем размножать экземпляры."""
    say(f"{bot['name']}: {reason} — перезапуск")
    kill(bot)
    deadline = time.time() + KILL_GRACE
    alive = _procs_checked(bot["match"])
    while alive is None or alive:
        if time.time() >= deadline:
            break
        time.sleep(1)
        alive = _procs_checked(bot["match"])
    if alive is None:
        # Именно этот случай и разрешал дубликаты: на забитой машине PowerShell не укладывался
        # в таймаут, ответ приходил пустым — и «никого нет» было неотличимо от «не знаю».
        say(f"{bot['name']}: не удалось проверить, умер ли старый — новый НЕ запускаю")
        notify(f"{bot['name']}:unknown",
               f"{bot['name']} не отвечает, а умер ли старый процесс — проверить не удалось. "
               f"Новый не запускаю, посмотрите машину.")
        return
    if alive:
        # Запрет держится ТОЛЬКО пока старый занимает порт: драка за него и была причиной
        # инцидента 10.08. Если порт отпущен, зависший труп новому экземпляру не мешает, и
        # девятичасовой простой 09.09 обменивается на лишний процесс в памяти — обмен выгодный.
        url = bot.get("health")
        if url and _port_busy(url) is False:
            say(f"{bot['name']}: старый (PID {alive}) не умер, НО порт свободен — запускаю новый. "
                f"Труп добить руками: taskkill /PID {alive[0]} /T /F")
            notify(f"{bot['name']}:zombie",
                   f"{bot['name']} поднят в обход зависшего процесса PID {alive[0]}: taskkill его "
                   f"не берёт. Бот работает, но труп висит в памяти — добейте руками.")
            start(bot)
            time.sleep(WARMUP)
            return
        say(f"{bot['name']}: НЕ УМЕР (PID {alive}) — новый экземпляр НЕ запускаю. "
            f"Разберитесь руками: taskkill /PID {alive[0]} /T /F")
        notify(f"{bot['name']}:stuck",
               f"{bot['name']} лежит и не перезапускается: старый процесс PID {alive[0]} не умирает "
               f"от taskkill и держит порт. Нужны руки: taskkill /PID {alive[0]} /T /F")
        return
    start(bot)
    time.sleep(WARMUP)


def main() -> None:
    _load_local()
    say(f"старт сторожа, PID {os.getpid()}, подопечных {len(BOTS)}")
    fails = {b["name"]: 0 for b in BOTS}
    task_last = {t["name"]: 0.0 for t in TASKS}   # 0 → выполнить и при старте
    for b in BOTS:                       # холодный старт: поднять тех, кто не отвечает
        if not healthy(b):
            restart(b, "при старте не отвечает")
    while True:
        for b in BOTS:
            try:
                if healthy(b):
                    fails[b["name"]] = 0
                else:
                    fails[b["name"]] += 1
                    why = _health_err.get(b["name"], "")
                    say(f"{b['name']}: не отвечает ({fails[b['name']]}/{FAIL_LIMIT})"
                        + (f" — {why}" if why else ""))
                    if fails[b["name"]] >= FAIL_LIMIT:
                        restart(b, f"{fails[b['name']]} неудач подряд")
                        fails[b["name"]] = 0
            except Exception as e:  # noqa: BLE001  — сторож не должен умирать от случайной ошибки
                say(f"{b['name']}: ошибка цикла: {e}")
        for t in TASKS:
            try:
                if time.time() - task_last[t["name"]] >= t["every"]:
                    task_last[t["name"]] = time.time()
                    t["run"]()
            except Exception as e:  # noqa: BLE001
                say(f"задача {t['name']}: ошибка: {e}")
        _say_blips()
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
