"""Жизненный цикл процессов бота: поиск, убийство, дубликаты, возврат памяти ОС.

Разбор инцидента 10.08 (владелец увидел в диспетчере задач python-процесс на 15.87 ГБ,
простаивающий, и 88% занятой памяти). Ни на одну из этих механик тестов не было вовсе,
поэтому все три дефекта жили молча:

1. Поиск процессов бота фильтровал по Name='python.exe'. venv собран поверх Store-Python,
   где .venv\\Scripts\\python.exe — редиректор: настоящий интерпретатор и все его
   multiprocessing-дети называются python3.13.exe. Фильтр видел только обёртку в 1 МБ.
2. Сторож перезапускал бота, не проверив, что старый умер, — и поднимал второй экземпляр
   поверх живого. Тот не мог занять порт, оставался без /health навсегда, сторож считал его
   мёртвым и плодил следующих. Каждый — со своим пулом и своим torch-воркером.
3. Рендер-воркер был единственным пулом без ресайклинга и потому единственным процессом,
   который не возвращал память ОС никогда.
"""
import re
from pathlib import Path

import pytest

import watchdog
from app import cpupool

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _watchdog_log_to_tmp(tmp_path, monkeypatch):
    """Журнал сторожа — во временную папку. Без этого тесты, дёргающие restart(), писали
    строки в боевой data/logs/watchdog.log: рабочий каталог проекта тесты трогать не должны."""
    monkeypatch.setattr(watchdog, "WD_LOG", tmp_path / "watchdog.log")


# ---------- 1. поиск процессов не должен пиновать имя python.exe ----------

def test_watchdog_filter_matches_versioned_python():
    """WQL-фильтр сторожа обязан ловить python3.13.exe, а не только python.exe.

    Это регресс-тест на главную причину инцидента: вернётся Name='python.exe' — сторож снова
    ослепнет и будет бить по процессу-обёртке, оставляя настоящего бота с его памятью жить."""
    src = Path(watchdog.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _procs("):src.index("def healthy(")]
    # Только исполняемая строка запроса: в комментарии рядом старый фильтр упомянут нарочно,
    # чтобы будущий читатель понимал, почему тут нельзя писать Name='python.exe'.
    query = next(ln for ln in body.splitlines() if "Get-CimInstance" in ln)
    assert "Name='python.exe'" not in query, "фильтр снова видит только обёртку Store-Python"
    assert "LIKE 'python%'" in query


def test_restart_script_filter_matches_versioned_python():
    """То же для restart_bot.ps1: там ДВА места (бот и сторож), оба фильтруют по имени."""
    src = (ROOT / "restart_bot.ps1").read_text(encoding="utf-8")
    filters = re.findall(r"Get-CimInstance Win32_Process -Filter \"([^\"]+)\"", src)
    assert filters, "фильтры процессов из скрипта пропали — тест устарел"
    for f in filters:
        assert "python%" in f, f"фильтр {f!r} не увидит python3.13.exe"


# ---------- 2. сторож не имеет права плодить экземпляры ----------

def test_restart_does_not_start_when_kill_failed(monkeypatch):
    """Не убедились в смерти старого — новый НЕ запускаем.

    Ровно этот шаг отсутствовал 10.08: kill() промахнулся, старый бот остался жив с портом,
    а сторож всё равно позвал start() — и получил безголовый дубликат."""
    started = []
    monkeypatch.setattr(watchdog, "kill", lambda bot: None)
    monkeypatch.setattr(watchdog, "_procs_checked", lambda match: [4242])   # никто не умер
    monkeypatch.setattr(watchdog, "start", lambda bot: started.append(bot))
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    monkeypatch.setattr(watchdog, "KILL_GRACE", 0)

    watchdog.restart({"name": "bot", "match": "x*run.py"}, "тест")
    assert started == [], "сторож запустил второй экземпляр поверх живого первого"


def test_restart_does_not_start_when_check_failed(monkeypatch):
    """«Не смог проверить» ≠ «никого нет».

    Ровно эта подмена и разрешала дубликаты: на забитой машине PowerShell не укладывался в
    таймаут, _procs отдавал [] на любое исключение — и защита сама себя отключала."""
    started = []
    monkeypatch.setattr(watchdog, "kill", lambda bot: None)
    monkeypatch.setattr(watchdog, "_procs_checked", lambda match: None)     # опрос не удался
    monkeypatch.setattr(watchdog, "start", lambda bot: started.append(bot))
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    monkeypatch.setattr(watchdog, "KILL_GRACE", 0)

    watchdog.restart({"name": "bot", "match": "x*run.py"}, "тест")
    assert started == [], "сторож стартовал, не зная, жив ли старый"


def test_restart_starts_when_processes_gone(monkeypatch):
    """Обратный случай: старый действительно умер — перезапуск идёт как обычно."""
    started = []
    monkeypatch.setattr(watchdog, "kill", lambda bot: None)
    monkeypatch.setattr(watchdog, "_procs_checked", lambda match: [])
    monkeypatch.setattr(watchdog, "start", lambda bot: started.append(bot))
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)

    watchdog.restart({"name": "bot", "match": "x*run.py"}, "тест")
    assert len(started) == 1


def test_watchdog_logs_to_file(tmp_path):
    """Решения сторожа обязаны сохраняться. Раньше он писал только print(), а запущенный
    Планировщиком через wscript консоли не имеет — весь его вывод уходил в никуда, и инцидент
    пришлось восстанавливать по косвенным уликам. (Путь подменён фикстурой выше.)"""
    watchdog.say("тестовая строка")
    assert "тестовая строка" in (tmp_path / "watchdog.log").read_text(encoding="utf-8")


def test_rotate_moves_oversized_log(tmp_path, monkeypatch):
    """Ротация: лог подопечного рос вечно (на 11.08 накопился 1.05 ГБ рядом с базой)."""
    monkeypatch.setattr(watchdog, "LOG_MAX_MB", 0.001)
    p = tmp_path / "bot.out"
    p.write_bytes(b"x" * 5000)
    watchdog._rotate(p)
    assert not p.exists() and (tmp_path / "bot.out.1").exists()


# ---------- 3. дубликат бота должен умирать сразу и громко ----------

def test_second_instance_refuses_to_start(monkeypatch):
    """Занятый порт = я дубликат. Раньше исключение bind'а глушилось как «мини-апка не
    запустилась», и экземпляр жил дальше без веб-сервера — вечно «мёртвый» для сторожа."""
    import socket

    import run as runmod

    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    try:
        monkeypatch.setattr(runmod.config, "WEBAPP_ENABLED", True)
        monkeypatch.setattr(runmod.config, "WEBAPP_PORT", port)
        with pytest.raises(SystemExit) as e:
            runmod._claim_single_instance()
        assert str(port) in str(e.value)
    finally:
        held.close()


def test_pid_file_written(tmp_path, monkeypatch):
    """data/bot.pid читали и restart_bot.ps1, и документация, а писать его не писал НИКТО —
    ветка поиска бота по pid-файлу была мёртвой."""
    import os

    import run as runmod

    monkeypatch.setattr(runmod.config, "WEBAPP_ENABLED", False)
    monkeypatch.setattr(runmod.config, "DATA_DIR", tmp_path)
    runmod._claim_single_instance()
    assert (tmp_path / "bot.pid").read_text(encoding="utf-8") == str(os.getpid())


async def test_shutdown_keeps_someone_elses_pid_file(tmp_path, monkeypatch):
    """Остановка убирает ТОЛЬКО свой pid-файл.

    Поймано на живой машине: tests/test_errorlog.py зовёт run.shutdown() без подмены DATA_DIR,
    и наивный unlink снёс якорь РАБОТАЮЩЕГО боевого бота. В бою тот же случай возможен, когда
    до shutdown доходит второй процесс: разоружать первый он не вправе — по этому файлу его
    находит restart_bot.ps1."""
    import run as runmod

    monkeypatch.setattr(runmod.config, "DATA_DIR", tmp_path)
    (tmp_path / "bot.pid").write_text("999999", encoding="utf-8")   # якорь чужого процесса

    bot = type("B", (), {"session": type("S", (), {"close": _anop})()})()
    monkeypatch.setattr(runmod.tunnel, "stop", _anop)
    monkeypatch.setattr(runmod.webapp, "stop", _anop)
    monkeypatch.setattr(runmod.errorlog, "flush_once", _anop)
    monkeypatch.setattr(runmod.db, "close", _anop)
    monkeypatch.setattr(runmod.cpupool, "shutdown", lambda *a, **k: None)

    await runmod.shutdown(bot)
    assert (tmp_path / "bot.pid").exists(), "остановка снесла чужой pid-файл"


async def _anop(*a, **k):
    return None


# ---------- 4. пулы обязаны возвращать память ОС ----------

def test_render_pool_recycles_workers(monkeypatch):
    """Рендер-воркер должен создаваться с max_tasks_per_child. Без него он единственный из
    процессов бота не отдавал ОС ничего: кэш аллокатора torch копился до перезапуска."""
    seen = {}

    class _FakePool:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(cpupool, "_render_pool", None)
    monkeypatch.setattr(cpupool, "_render_broken", False)
    monkeypatch.setattr(cpupool, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(cpupool.config, "RENDER_POOL_MAX_TASKS", 200)

    cpupool._get_render_pool()
    assert seen.get("max_tasks_per_child") == 200
    assert seen.get("max_workers") == 1


async def test_task_failure_does_not_kill_the_pool(monkeypatch):
    """Упавшая ЗАДАЧА (битый меш) не должна навсегда выключать пул.

    Раньше здесь стоял `_broken = True` на любое исключение, и один плохой файл уводил всю
    тяжёлую геометрию и растеризацию в процесс бота — без пула, без ресайклинга и без возврата
    памяти ОС. Сброса флага не было нигде: до перезапуска."""
    class _FakeLoop:
        async def run_in_executor(self, pool, func, *args):
            raise ValueError("битая модель")

    monkeypatch.setattr(cpupool, "_broken", False)
    monkeypatch.setattr(cpupool, "_get_pool", lambda: object())
    monkeypatch.setattr(cpupool.asyncio, "get_running_loop", lambda: _FakeLoop())

    with pytest.raises(ValueError):
        await cpupool.run(_boom)
    assert cpupool._broken is False, "одна битая модель выключила пул до перезапуска бота"


def _boom():
    raise ValueError("битая модель")


async def test_broken_pool_recovers_until_limit(monkeypatch):
    """Сломанный пул пересоздаётся; сдаёмся только после серии поломок подряд."""
    from concurrent.futures import BrokenExecutor

    class _FakeLoop:
        async def run_in_executor(self, pool, func, *args):
            raise BrokenExecutor("воркер убит")

    monkeypatch.setattr(cpupool, "_broken", False)
    monkeypatch.setattr(cpupool, "_pool_breaks", 0)
    monkeypatch.setattr(cpupool, "_get_pool", lambda: object())
    monkeypatch.setattr(cpupool.asyncio, "get_running_loop", lambda: _FakeLoop())
    monkeypatch.setattr(cpupool.asyncio, "to_thread", _noop_thread)

    for _ in range(cpupool.POOL_BREAK_LIMIT - 1):
        await cpupool.run(_ok)
        assert cpupool._broken is False
    await cpupool.run(_ok)
    assert cpupool._broken is True


async def _noop_thread(func, *args):
    return "поток"


def _ok():
    return "ok"


def test_shutdown_can_wait_for_workers(monkeypatch):
    """selfupdate уходит через os._exit(0), который пропускает atexit-хук concurrent.futures —
    а именно он штатно рассылает воркерам сентинелы. Поэтому там нужен wait=True, иначе
    воркеры остаются жить с дохлым родителем (EOF они не увидят никогда)."""
    calls = []

    class _FakePool:
        def shutdown(self, wait=False, cancel_futures=False):
            calls.append(wait)

    monkeypatch.setattr(cpupool, "_pool", _FakePool())
    monkeypatch.setattr(cpupool, "_render_pool", _FakePool())
    cpupool.shutdown(wait=True)
    assert calls == [True, True]


def test_selfupdate_waits_and_stops_avatar_worker():
    """Проверяем сам путь автообновления: он обязан звать shutdown(wait=True) и гасить
    воркер аватарок (он daemon=True, а demon'ов убирает atexit — который os._exit пропускает)."""
    src = Path(__file__).resolve().parent.parent / "app" / "selfupdate.py"
    body = src.read_text(encoding="utf-8")
    die = body[body.index("def _die("):]
    die = die[:die.index("os._exit(0)")]
    assert "cpupool.shutdown(wait=True)" in die
    assert "avatars_gpu.unload" in die


# ---------- 5. внешние процессы не должны переживать таймаут ----------

async def test_reap_process_kills_survivor():
    """asyncio.wait_for отменяет ОЖИДАНИЕ, но не процесс. Без добивания каждый таймаут
    оставляет чужой exe жить — а nvidia-smi дёргает экран LCD раз в ~2.5 с."""
    import asyncio
    import sys

    from app import webapp

    # Спим НЕДОЛГО: venv на Store-Python, где sys.executable — редиректор, и kill() забирает
    # только его. Настоящий спящий ребёнок остаётся сиротой до конца сна и держит дерево
    # процессов прогона — с sleep(30) это выглядело как зависший pytest (потерял на этом время).
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(3)",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    assert proc.returncode is None
    await webapp.reap_process(proc)
    assert proc.returncode is not None, "процесс пережил добивание"


async def test_reap_process_tolerates_finished_and_none():
    """Добивание уже мёртвого (и None) не должно ничего ронять — оно стоит в finally."""
    from app import webapp
    await webapp.reap_process(None)


def test_slicer_gui_is_detached_from_bot():
    """GUI слайсера не должен быть ребёнком бота: иначе taskkill /T сторожа при перезапуске
    закроет мастеру слайсер с несохранённой работой. Промежуточный `cmd /c start` мгновенно
    завершается, и слайсер выпадает из дерева бота.

    Файл не прибит гвоздями к app/webapp.py: волна «Заказ на верстаке» цикла 2 увезла запуск в
    app/web/orderbench.py, и прибор от этого покраснел на ровном месте. Ищем держателя по всему
    веб-слою и требуем, чтобы он был РОВНО ОДИН — иначе проверка тихо станет вакуумной, когда
    следующая волна перенесёт код ещё раз."""
    holders = [p for p in [ROOT / "app" / "webapp.py", *sorted((ROOT / "app" / "web").glob("*.py"))]
               if "config.SLICER_PATH.exists()" in p.read_text(encoding="utf-8")]
    assert len(holders) == 1, f"запуск слайсера потерян или размножен: {[p.name for p in holders]}"
    src = holders[0].read_text(encoding="utf-8")
    launch = src[src.index("config.SLICER_PATH.exists()"):][:700]
    assert '"start"' in launch, "слайсер снова запускается прямым ребёнком бота"


# ---------- 6. память должна быть измерима ----------

def test_memory_report_shape():
    """Единственным измерением памяти в проекте было общесистемное virtual_memory().
    Теперь есть поимённое — свой RSS и RSS каждого ребёнка."""
    from app import maintenance
    rep = maintenance.memory_report()
    assert rep["self_gb"] > 0
    assert isinstance(rep["children"], list)
    assert 0 <= rep["system_percent"] <= 100
