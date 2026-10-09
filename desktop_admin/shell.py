"""Оболочка ПК-админки: нативное окно (pywebview + WebView2) поверх мини-аппа бота.

Этап 1 редизайна админки (план 2026-07-18). Чем лучше Edge --app (admin_panel.py):
- токен уезжает в localStorage через мост, а не светится в URL;
- оверлей «бот перезапускается» + автопереподключение — сторож рестартует бота по
  расписанию, окно админки при этом не должно превращаться в страницу ошибки;
- один экземпляр: повторный двойной клик поднимает существующее окно;
- Ctrl+N — ещё одно окно той же админки (мультиокна вместо вкладок до этапа 4).

Запуск: admin_panel.bat (сам готовит venv desktop_admin\\.venv при первом запуске)
или desktop_admin\\.venv\\Scripts\\pythonw.exe desktop_admin\\shell.py.
Самопроверка без юзера: ...\\python.exe desktop_admin\\shell.py --smoke — открывает
окно, ждёт загрузку мини-аппа и токен в localStorage, печатает вердикт и закрывается.

Отдельный venv: у бота свои зависимости, pywebview к ним не подмешиваем.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# BASE_URL — общий префикс мини-аппа и настольной админки: по нему проверяем, что в окне
# «наша» страница (иначе токен не инъектится и SPA молча уходит в MOCK-режим на выдуманных
# данных). START_URL — что открываем: настольная админка живёт в подкаталоге /app/desk/.
BASE_URL = "http://127.0.0.1:8081/app/"
START_URL = BASE_URL + "desk/"
APP_URL = START_URL          # совместимость: имя используется в логах и внешних скриптах
# Проба «сервер жив»: /health (app/webapp.py::_health) отдаёт короткое "ok" и БД не трогает.
# Раньше поллили BASE_URL + "favicon.ico" — статика, но FileResponse со stat+sendfile, и главное
# ответ никто не дочитывал: сокет закрывался с непрочитанным телом, Windows слал RST, и боевой
# лог каждые 5 секунд получал ложный ERROR asyncio ConnectionResetError [WinError 10054]
# (гигиена 22.08.2026). Ответ ниже дочитывается — именно это и лечит шум.
HEALTH_URL = "http://127.0.0.1:8081/health"
# Зеркало админских уведомлений для Windows-тостов (этап 3). Отдаётся только локально.
API_EVENTS = "http://127.0.0.1:8081/api/admin/events"
API_HUB = "http://127.0.0.1:8081/api/admin/hub"     # счётчики для бейджа в трее
TITLE = "Print Studio — админка"
ICON = ROOT / "app" / "webui" / "favicon.ico"
# Идентификатор приложения для Windows: им подписываются тосты и по нему группируется
# кнопка в панели задач. Должен совпадать в _set_app_id, _register_aumid и тостере.
AUMID = "PrintStudio.Admin"
TOAST_ICON = Path(__file__).resolve().parent / "appicon.png"   # PNG: центр уведомлений не любит .ico
# WebView2-профиль (в нём localStorage с токеном и сессией деска). Живёт в DATA_DIR, а не
# рядом со скриптом (гигиена 22.08.2026, продолжение C2): при переносе установки профиль едет
# вместе с данными — деск не разлогинивается — и попадает в те копии данных, что делает владелец.
LEGACY_PROFILE_DIR = Path(__file__).resolve().parent / "profile"   # где лежал до 22.08.2026


def _data_dir() -> Path:
    """Каталог данных: DATA_DIR из окружения/.env, иначе <папка бота>/data (C2).

    Свой парсер .env, а не app.config: у админки отдельный venv без зависимостей бота.
    Логика намеренно повторяет backup_offsite.py::_data_dir по той же причине.
    """
    raw = (os.getenv("DATA_DIR") or "").strip().strip('"').strip("'")
    if not raw:
        try:
            for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "DATA_DIR":
                    raw = v.strip().strip('"').strip("'")
                    break
        except OSError:
            raw = ""
    return Path(raw) if raw else ROOT / "data"


PROFILE_DIR = _data_dir() / "desk_profile"


def _migrate_profile() -> Path:
    """Разовый переезд профиля из desktop_admin/profile в DATA_DIR/desk_profile.

    Возвращает каталог, с которым работать В ЭТОМ запуске. Переносим, только когда нового ещё
    нет, а старый есть; звать СТРОГО после _already_running() (мьютекс — единственная гарантия,
    что каталог не держит вторая копия админки) и ДО mkdir нового, иначе условие сломается.
    Каталог могут держать открытым и сироты msedgewebview2.exe от упавшей оболочки — тогда
    move падает WinError 32; в этом случае молча работаем на СТАРОМ каталоге (лучше не переехать,
    чем не запуститься и разлогинить владельца ровно там, ради чего переезд и делается).
    Каталог тяжёлый (сотни МБ) и может ехать между томами, поэтому move идёт во ВРЕМЕННОЕ имя
    рядом с целью: обрыв на середине не оставит полупустой desk_profile, который следующий запуск
    примет за готовый.
    """
    if PROFILE_DIR.exists() or not LEGACY_PROFILE_DIR.exists():
        return PROFILE_DIR
    staging = PROFILE_DIR.with_name(PROFILE_DIR.name + ".moving")
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        PROFILE_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(LEGACY_PROFILE_DIR), str(staging))
        os.replace(str(staging), str(PROFILE_DIR))
    except Exception as e:  # noqa: BLE001 — любая беда переноса не должна мешать запуску
        print(f"профиль деска не переехал в {PROFILE_DIR} ({e}); работаем на {LEGACY_PROFILE_DIR}")
        return LEGACY_PROFILE_DIR if LEGACY_PROFILE_DIR.exists() else PROFILE_DIR
    print(f"профиль деска перенесён: {LEGACY_PROFILE_DIR} -> {PROFILE_DIR}")
    return PROFILE_DIR


sys.path.insert(0, str(ROOT))
import admin_panel  # noqa: E402  — переиспользуем чтение/генерацию ADMIN_PANEL_TOKEN из .env

try:
    import webview  # noqa: E402
except Exception:  # noqa: BLE001  — pywebview не встал → браузерный режим, как раньше
    sys.exit(admin_panel.main())

_TOKEN = ""
_auth_refused = ""     # токен, который сервер уже отверг: см. _auth_token
_mutex = None          # хэндл Win32-мьютекса единственного экземпляра — держим живым
_smoke_exit = 1


def _already_running() -> bool:
    """Второй запуск → поднять окно первого и выйти (Win32 named mutex)."""
    global _mutex
    try:
        k32 = ctypes.windll.kernel32
        _mutex = k32.CreateMutexW(None, False, "print_cost_bot_admin_shell")
        if k32.GetLastError() != 183:   # ERROR_ALREADY_EXISTS
            return False
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)     # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


def _server_up(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
            ok = r.status == 200
            r.read()      # ДОЧИТАТЬ обязательно: иначе закрытие сокета с непрочитанным телом
            return ok     # даёт RST и ложный ERROR asyncio в боевом логе (см. HEALTH_URL)
    except Exception:  # noqa: BLE001
        return False


def _boot_js(tok: str) -> str:
    """Выполняется в окне после каждой загрузки мини-апп/деска: в режиме token кладёт
    .env-токен в localStorage (ключ pcb_admin_token — его читает api.js; не совпал →
    reload, MOCK-режим надо снять) и вешает Ctrl+N → новое окно.

    СНАЧАЛА спрашиваем сервер (/api/desk/whoami): при `desk_login_mode = personal`
    токеном владеет ЛИЧНЫЙ вход (login.jsx), и безусловная перезапись зацикливала экран
    входа — сотрудник вводил код, страница перезагружалась, оболочка затирала свежую
    сессию .env-токеном, и всё начиналось заново (поймано у владельца 21.08.2026).
    Живую авторизацию (authed) не трогаем ни в каком режиме. Сервер не ответил —
    ведём себя по-старому: это прежний контракт token-режима."""
    return """
(function () {
  function keys() {
    if (!window.__psShellKeys) {
      window.__psShellKeys = 1;
      document.addEventListener('keydown', function (e) {
        if (e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === 'n' || e.key === 'N')) {
          e.preventDefault();
          try { window.pywebview.api.new_window(); } catch (err) {}
        }
      });
    }
  }
  keys();
  var t = %s;
  var cur = '';
  try { cur = localStorage.getItem('pcb_admin_token') || ''; } catch (e) { return; }
  function inject() {
    if (cur !== t) {
      try { localStorage.setItem('pcb_admin_token', t); location.reload(); } catch (e) {}
    }
  }
  try {
    fetch('/api/desk/whoami?initData=' + encodeURIComponent(cur))
      .then(function (r) { return r.json(); })
      .then(function (w) {
        if (w && w.authed) return;                 // живая сессия или рабочий токен
        if (w && w.mode === 'personal') return;    // вход кодом — токен ставит login.jsx
        inject();
      })
      .catch(inject);
  } catch (e) { inject(); }
})();
""" % json.dumps(tok)


_OVERLAY_JS = """
(function () {
  if (document.getElementById('ps-shell-overlay')) return;
  var d = document.createElement('div');
  d.id = 'ps-shell-overlay';
  d.style.cssText = 'position:fixed;inset:0;z-index:99999;display:flex;align-items:center;' +
    'justify-content:center;background:rgba(10,14,18,.9);color:#e8eef4;font:600 17px system-ui;text-align:center';
  d.textContent = 'Бот перезапускается — окно обновится само…';
  document.body.appendChild(d);
})();
"""

_LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Print Studio</title></head>
<body style="margin:0;display:flex;align-items:center;justify-content:center;height:100vh;
background:#0e1217;color:#e8eef4;font:600 18px system-ui">
Подключаюсь к боту&hellip;&nbsp;<span style="opacity:.55">(он ещё запускается)</span></body></html>"""


def _set_app_id() -> None:
    """Своя личность приложения в панели задач.

    Без явного AppUserModelID Windows считает окно частью pythonw.exe: кнопка в панели
    задач берёт иконку интерпретатора и группируется с другими python-окнами, а WM_SETICON
    на это почти не влияет. Вызывать нужно ДО создания окна.
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:  # noqa: BLE001
        pass


def _register_aumid() -> None:
    """Представить приложение системе уведомлений.

    Центр уведомлений подписывает тост НЕ тем именем, что передано в библиотеку, а тем, что
    зарегистрировано за AppUserModelID. Без этой записи Windows берёт владельца процесса-хоста
    — и уведомления приходили от «Командной строки», хотя текст был наш.

    Ключ пользовательский (HKCU\\Software\\Classes\\AppUserModelId) — ровно то, что заводят
    установщики ради атрибуции уведомлений; система и права машины не затрагиваются.
    """
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"SOFTWARE\Classes\AppUserModelId\{AUMID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, TITLE)
            if TOAST_ICON.exists():
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(TOAST_ICON))
    except Exception:  # noqa: BLE001
        pass


def _set_icon(title: str) -> None:
    """Иконка окна/таскбара из favicon.ico мини-аппа (WM_SETICON; best-effort)."""
    try:
        u32 = ctypes.windll.user32
        hwnd = 0
        for _ in range(60):
            hwnd = u32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd or not ICON.exists():
            return
        img_icon, wm_seticon, lr_loadfromfile = 1, 0x80, 0x10
        # Размеры берём системные (SM_CXICON / SM_CXSMICON): на HiDPI это 40-64 px, и
        # фиксированные 32/16 Windows растягивал из мелкого кадра — оттого иконка мылила.
        big = u32.GetSystemMetrics(11) or 32      # SM_CXICON
        small = u32.GetSystemMetrics(49) or 16    # SM_CXSMICON
        for size, which in ((big, 1), (small, 0)):
            h = u32.LoadImageW(None, str(ICON), img_icon, size, size, lr_loadfromfile)
            if h:
                u32.SendMessageW(hwnd, wm_seticon, which, h)
    except Exception:  # noqa: BLE001
        pass


def _on_loaded_factory(w):
    def _on_loaded(*_a) -> None:
        try:
            url = w.get_current_url() or ""
            # Проверяем по BASE_URL, а не по START_URL: токен нужен и мини-аппу, и админке,
            # а промах здесь самый коварный — окно откроется, но данные будут выдуманными.
            if url.startswith(BASE_URL):
                w.evaluate_js(_boot_js(_TOKEN))
        except Exception:  # noqa: BLE001
            pass
    return _on_loaded


class _Api:
    """Мост для страницы (window.pywebview.api)."""

    def new_window(self) -> None:   # Ctrl+N из мини-аппа
        _create_window(len(webview.windows) + 1)


def _create_window(n: int):
    title = TITLE if n <= 1 else f"{TITLE} ({n})"
    up = _server_up()
    # Окно под настольную админку: master-detail с чатом разворачивается в три колонки
    # примерно с 1700px. Меньше 1500 раскладка честно схлопывается в вертикальную, но
    # открывать окно сразу в «узком» режиме на широком мониторе смысла нет.
    w = webview.create_window(title, url=START_URL if up else None,
                              html=None if up else _LOADING_HTML,
                              width=2000, height=1200, min_size=(1180, 720), js_api=_Api())
    w.events.loaded += _on_loaded_factory(w)
    w.events.closing += _on_closing_factory(w)
    threading.Thread(target=_set_icon, args=(title,), daemon=True).start()
    return w, up


def _watch(initial_up: bool) -> None:
    """Фон: поллит сервер. Упал → оверлей во всех окнах; вернулся → перезагрузка окон
    (после рестарта бота стейт SPA всё равно устаревший, честный reload лучше)."""
    up = initial_up
    while True:
        time.sleep(3 if not up else 5)
        cur = _server_up()
        if cur and not up:
            for w in list(webview.windows):
                try:
                    w.load_url(START_URL)
                except Exception:  # noqa: BLE001
                    pass
        elif not cur and up:
            for w in list(webview.windows):
                try:
                    w.evaluate_js(_OVERLAY_JS)
                except Exception:  # noqa: BLE001
                    pass
        up = cur


def _window_token() -> str:
    """Токен, которым СЕЙЧАС авторизовано окно деска (localStorage), или '' — никто не вошёл.

    В режиме личного входа тут лежит сессия конкретного сотрудника (её кладёт login.jsx после
    обмена кода), в прежнем token-режиме — .env-токен, который сама же оболочка и вписала
    (_boot_js). Ключ один и тот же — pcb_admin_token, других хранилищ у деска нет.

    Спрашиваем ЖИВОЕ окно, а не файлы профиля WebView2: профиль держит открытым
    msedgewebview2.exe, а его localStorage — чужая LevelDB, разбирать которую значило бы
    гадать по недокументированному формату и ломаться от обновления Edge.
    """
    js = ("(function(){try{return localStorage.getItem('pcb_admin_token')||''}"
          "catch(e){return ''}})()")
    for w in list(getattr(webview, "windows", None) or []):
        try:
            tok = w.evaluate_js(js)
        except Exception:  # noqa: BLE001 — окно грузится или закрывается: спросим следующий круг
            continue
        if tok:
            return str(tok)
    return ""


def _auth_token() -> str:
    """Чем оболочка подписывает фоновые опросы (тосты, бейдж трея). '' — спрашивать нечем.

    ПОЧЕМУ НЕ .env-ТОКЕН. С личным входом (desk_login_mode=personal, 21.08.2026) машинный
    токен в админские ручки больше не пускают — и правильно: он давал права первого владельца
    КОМУ УГОДНО, кто сел за этот компьютер. Оболочка же продолжала спрашивать им и получала
    403 каждые шесть секунд, то есть тосты умерли молча (872 отказа за день, 30.08.2026).
    Поэтому личность берём ту же, что у окна: тост видит тот, кто в деске залогинен, и ровно
    в его правах. Никто не вошёл — тостов нет, и это честно: на экране в этот момент форма
    входа, а не данные клиентов, и показывать их случайному человеку незачем.

    .env-токен остаётся запасным — ради прежнего token-режима (он по умолчанию) и первых
    секунд после запуска, пока окно ещё грузится. Отвергнутый токен запоминаем: иначе мы бы
    молотили сервер тем же отказом каждый круг — ровно тем, что и засорило боевой лог.
    """
    tok = _window_token() or _TOKEN
    return "" if (not tok or tok == _auth_refused) else tok


def _api_get(url: str, tok: str) -> dict | None:
    """GET к боту; None — не получилось (ответ разбирать нечего).

    Ответ 4xx/5xx приходит телом, которое urllib отдаёт исключением. Не дочитать его — значит
    закрыть сокет с данными в буфере: Windows шлёт RST, и у бота в логе появляется ложный
    ERROR asyncio (тот же источник шума, что и проба живости).
    """
    global _auth_refused
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:  # noqa: BLE001
            pass
        e.close()
        if e.code in (401, 403):
            # Вход сменился, сессия кончилась или прав не хватает. Молчим до нового входа:
            # повторять тот же отказ каждый круг бессмысленно и шумно.
            _auth_refused = tok
        return None
    except Exception:  # noqa: BLE001
        return None                        # бот мог перезапускаться — молча ждём следующий круг


def _events_round(cursor: int | None) -> tuple[int | None, list[dict]]:
    """Один круг опроса ленты: (курсор, что показать).

    Курсор None означает «вершину ещё не знаем»: следующий удачный ответ её только запомнит,
    ничего не показав. В это же состояние возвращаемся, когда доступа нет, — иначе после
    входа мастеру вывалилась бы пачкой вся очередь, накопившаяся, пока он был разлогинен.
    Обрыв связи (бот перезапускается) курсор НЕ сбрасывает: там терять нечего.
    """
    tok = _auth_token()
    if not tok:
        return None, []
    url = f"{API_EVENTS}?initData={urllib.parse.quote(tok)}"
    if cursor is not None:
        url += f"&since={cursor}"
    data = _api_get(url, tok)
    if data is None:
        return (None if _auth_refused == tok else cursor), []
    if not data.get("ok"):
        return cursor, []
    if cursor is None:                     # первый круг: только запоминаем вершину
        return int(data.get("cursor", 0)), []
    events = data.get("events") or []
    for ev in events:
        cursor = max(cursor, int(ev.get("id", cursor)))
    return cursor, events


def _toasts() -> None:
    """Windows-тосты по админским событиям (этап 3 редизайна ПК-админки).

    Бот зеркалит в /api/admin/events всё, что уходит админам в Telegram. Когда мастер за
    компьютером, тост в углу экрана удобнее телефона.

    Стартовый курсор берём с сервера БЕЗ параметра since: иначе после перезапуска оболочки
    на мастера вывалится пачка старых уведомлений. Клик по тосту фокусирует окно и уводит
    на нужный экран — адресация та же, что у телеграм-кнопок (?go=&id=). Чьим именем
    спрашиваем ленту — см. _auth_token.

    Пакет windows-toasts опционален: нет его — оболочка просто работает без тостов, а не
    падает. Это удобство, а не функциональность.
    """
    try:
        from windows_toasts import InteractableWindowsToaster, Toast, ToastActivatedEventArgs
    except Exception:  # noqa: BLE001
        print("тосты выключены: нет пакета windows-toasts (pip install windows-toasts)")
        return

    # notifierAUMID — иначе тост подпишется владельцем процесса-хоста, а не нами
    toaster = InteractableWindowsToaster(TITLE, notifierAUMID=AUMID)
    cursor = None
    while True:
        time.sleep(6)
        cursor, events = _events_round(cursor)
        for ev in events:
            _show_toast(toaster, Toast, ToastActivatedEventArgs, ev)


def _show_toast(toaster, Toast, ToastActivatedEventArgs, ev: dict) -> None:
    """Один тост. Первая строка события — заголовок, остальное — тело."""
    lines = [s for s in (ev.get("text") or "").splitlines() if s.strip()]
    if not lines:
        return
    try:
        toast = Toast()
        toast.text_fields = [lines[0][:120], " ".join(lines[1:])[:220] or TITLE]
        go, go_id = ev.get("go") or "", ev.get("go_id") or 0
        toast.on_activated = lambda _a=None, g=go, i=go_id: _focus_and_go(g, i)
        toaster.show_toast(toast)
    except Exception:  # noqa: BLE001
        pass


def _raise_window() -> None:
    """Поднять окно админки поверх остальных (тот же приём, что при повторном запуске)."""
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)     # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def _focus_and_go(go: str, go_id: int) -> None:
    """Клик по тосту: поднять окно и открыть нужный экран."""
    for w in list(webview.windows):
        try:
            if go:
                # тот же разбор ?go=&id=, что и у deep-links из телеграм-уведомлений
                url = f"{START_URL}?go={go}" + (f"&id={go_id}" if go_id else "")
                w.load_url(url)
            _raise_window()
            break
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Этап 5: трей, бейдж непрочитанного, автозапуск
# ---------------------------------------------------------------------------

_tray = None            # значок в трее (pystray.Icon)
_main_window = None     # главное окно: его закрытие прячет в трей, остальные закрываются
_hidden = False


def _tray_image(unread: int):
    """Значок трея из favicon мини-аппа. Есть непрочитанное — рисуем точку-бейдж."""
    from PIL import Image, ImageDraw
    try:
        img = Image.open(ICON).convert("RGBA").resize((64, 64))
    except Exception:  # noqa: BLE001
        img = Image.new("RGBA", (64, 64), (255, 122, 26, 255))
    if unread > 0:
        d = ImageDraw.Draw(img)
        d.ellipse((36, 2, 62, 28), fill=(255, 91, 77, 255), outline=(20, 24, 30, 255), width=3)
    return img


def _show_main() -> None:
    global _hidden
    if _main_window is not None:
        try:
            _main_window.show()
        except Exception:  # noqa: BLE001
            pass
    _hidden = False
    _raise_window()


def _quit_app() -> None:
    """Выход из трея: закрываем окна по-настоящему."""
    global _tray
    if _tray is not None:
        try:
            _tray.stop()
        except Exception:  # noqa: BLE001
            pass
        _tray = None
    for w in list(webview.windows):
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            pass


def _on_closing_factory(w):
    """Крестик прячет главное окно в трей вместо выхода.

    Смысл этапа 3 в том, что мастер получает тосты о новых заказах. Если крестик убивает
    процесс, уведомления умирают вместе с ним — поэтому окно прячется, а приложение живёт.
    Дополнительные окна (Ctrl+N) закрываются как обычно: прятать их незачем.
    Выход — из меню трея.
    """
    def _on_closing():
        global _hidden
        if _tray is None or w is not _main_window:
            return True                    # трея нет — ведём себя как раньше, иначе не выйти
        try:
            w.hide()
            _hidden = True
        except Exception:  # noqa: BLE001
            return True
        return False                        # закрытие отменяем
    return _on_closing


def _tray_loop() -> None:
    """Значок в трее + бейдж непрочитанного.

    Счётчик берём из того же /api/admin/hub, что и сайдбар админки: новые заказы плюс
    непрочитанные сообщения — ровно то, из-за чего стоит открыть окно.
    """
    global _tray
    try:
        import pystray
    except Exception:  # noqa: BLE001
        print("трей выключен: нет пакета pystray (pip install pystray Pillow)")
        return

    menu = pystray.Menu(
        pystray.MenuItem("Открыть админку", lambda *_: _show_main(), default=True),
        pystray.MenuItem("Выход", lambda *_: _quit_app()),
    )
    _tray = pystray.Icon("print_studio_admin", _tray_image(0), TITLE, menu)
    threading.Thread(target=_tray_badge_loop, daemon=True).start()
    try:
        _tray.run()
    except Exception:  # noqa: BLE001
        pass


def _tray_badge_loop() -> None:
    last = -1
    while _tray is not None:
        time.sleep(20)
        tok = _auth_token()               # тот же вход, что и у тостов: бейдж — те же данные
        if not tok:
            continue
        hub = _api_get(f"{API_HUB}?initData={urllib.parse.quote(tok)}", tok)
        if hub is None:
            continue
        att = (hub.get("attention") or {})
        n = int(att.get("new_orders") or 0) + int(att.get("unread") or 0)
        if n == last or _tray is None:
            continue
        last = n
        try:
            _tray.icon = _tray_image(n)
            _tray.title = f"{TITLE} — {n} требует внимания" if n else TITLE
        except Exception:  # noqa: BLE001
            pass


_TASK_NAME = "pcb_admin_shell"


def _autostart(install: bool) -> int:
    """Автозапуск админки при входе в систему (Планировщик задач).

    Отдельной командой, а не молча при первом запуске: приложение не должно само
    прописываться в автозагрузку — это решение владельца машины.
    """
    import subprocess
    if not install:
        r = subprocess.run(["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
                           capture_output=True, text=True)
        print("автозапуск снят" if r.returncode == 0 else f"не вышло снять: {r.stderr.strip()}")
        return 0 if r.returncode == 0 else 1
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    cmd = ["schtasks", "/Create", "/TN", _TASK_NAME, "/SC", "ONLOGON", "/F",
           "/TR", f'"{exe}" "{Path(__file__).resolve()}"']
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("автозапуск включён" if r.returncode == 0 else f"не вышло включить: {r.stderr.strip()}")
    return 0 if r.returncode == 0 else 1


def _smoke(w) -> None:
    """--smoke: самопроверка без участия человека.

    Проверяем не только «окно открылось», но и что админка реально работает на ЖИВЫХ
    данных: токен доехал в localStorage, приложение смонтировалось и баннер демо-режима
    отсутствует. Это тот самый коварный случай — окно выглядит рабочим, а показывает
    выдуманные заказы, потому что токен не инъектился.
    """
    global _smoke_exit
    ok, info = False, "timeout"
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
        try:
            url = w.get_current_url() or ""
            if not url.startswith(BASE_URL):
                info = f"url={url or '(loading)'}"
                continue
            tok = w.evaluate_js("localStorage.getItem('pcb_admin_token') ? 'yes' : 'no'")
            title = w.evaluate_js("document.title || ''")
            mounted = w.evaluate_js("document.querySelector('.sb-nav') ? 'yes' : 'no'")
            mock = w.evaluate_js("document.querySelector('.mockbar') ? 'yes' : 'no'")
            info = (f"url={url} token_in_ls={tok} mounted={mounted} demo={mock} "
                    f"title={title!r}")
            if tok == "yes" and mounted == "yes" and mock == "no":
                ok = True
                break
        except Exception as e:  # noqa: BLE001
            info = f"err={e!r}"
    print("SMOKE", "OK" if ok else "FAIL", "-", info)
    _smoke_exit = 0 if ok else 1
    for win in list(webview.windows):
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    global _TOKEN, _main_window
    _set_app_id()          # до создания окна, иначе таскбар возьмёт иконку pythonw
    _register_aumid()      # чтобы тосты приходили от нас, а не от «Командной строки»
    if "--install-autostart" in sys.argv:
        return _autostart(True)
    if "--remove-autostart" in sys.argv:
        return _autostart(False)
    smoke = "--smoke" in sys.argv
    if not smoke and _already_running():
        return 0
    _TOKEN = admin_panel._ensure_token()
    # --smoke мьютекс не проверяет (то есть админка может быть запущена) — в самопроверке
    # ничего не переносим, просто берём тот каталог, который уже есть.
    if smoke:
        profile_dir = LEGACY_PROFILE_DIR if not PROFILE_DIR.exists() and \
            LEGACY_PROFILE_DIR.exists() else PROFILE_DIR
    else:
        profile_dir = _migrate_profile()
    profile_dir.mkdir(parents=True, exist_ok=True)
    w, up = _create_window(1)
    _main_window = w
    threading.Thread(target=_watch, args=(up,), daemon=True).start()
    if smoke:
        threading.Thread(target=_smoke, args=(w,), daemon=True).start()
    else:
        # В --smoke ни тостов, ни трея: самопроверка должна быть тихой и ничего не оставлять.
        threading.Thread(target=_toasts, daemon=True).start()
        threading.Thread(target=_tray_loop, daemon=True).start()
    webview.start(private_mode=False, storage_path=str(profile_dir), gui="edgechromium")
    return _smoke_exit if smoke else 0


if __name__ == "__main__":
    sys.exit(main())
