"""Открыть админку бота как ПК-приложение (OpenTodo e075a6f3).

Мини-апп и так живёт на локальном веб-сервере бота — отдельное приложение не нужно:
открываем его в app-окне браузера (без вкладок/адресной строки) с токен-доступом вместо
Telegram initData. Токен ADMIN_PANEL_TOKEN берётся из .env; если его там нет — генерируем
и дописываем (после этого нужен перезапуск бота, чтобы он подхватил токен).

Запуск:  двойной клик по admin_panel.bat  (или .venv/Scripts/python admin_panel.py)
Токен = полный доступ мастера. Никому не пересылать; сменить = удалить строку из .env
и запустить лаунчер заново (+ перезапуск бота).
"""
from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
URL_BASE = "http://127.0.0.1:8081/app/"


def _read_token() -> str:
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("ADMIN_PANEL_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _ensure_token() -> str:
    tok = _read_token()
    if tok:
        return tok
    tok = secrets.token_urlsafe(32)
    with open(ENV, "a", encoding="utf-8") as f:
        f.write(f"\nADMIN_PANEL_TOKEN={tok}\n")
    print("Токен ПК-доступа создан и записан в .env.")
    print("ВАЖНО: перезапустите бота, чтобы он подхватил токен (иначе будет 403).")
    return tok


def _app_window(url: str) -> bool:
    """Окно-приложение без вкладок: Edge → Chrome → обычный браузер."""
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            subprocess.Popen([exe, f"--app={url}"], close_fds=True)
            return True
    return False


def main() -> int:
    tok = _ensure_token()
    url = f"{URL_BASE}?admintoken={tok}"
    if not _app_window(url):
        webbrowser.open(url)
    print("Админка открывается: " + URL_BASE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
