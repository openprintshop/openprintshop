"""Канал выпуска и версия кода: stable (ветка master) vs test (ветка test).

Разделение версий на stable и test (OpenTodo 31485676): нормально работает stable
(ветка `master`); тестовую версию запускают ВМЕСТО stable по просьбе
(`restart_bot.ps1 -Channel test` → `git checkout test`), а после одобрения её вливают
в stable (`restart_bot.ps1 -Promote`). «Текущий канал» = текущая git-ветка, поэтому
определяем его по `.git/HEAD` — без запуска git и без сторонних зависимостей.

Ветка переключается только при перезапуске (сторож поднимает run.py заново), поэтому
канал/SHA постоянны на время жизни процесса — считаем один раз и кэшируем.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_GIT_DIR = BASE_DIR / ".git"

# master — это stable; test — тестовая версия; любая иная ветка показывается как есть.
# main — стабильная ветка открытой версии проекта (OpenPrintShop): установка — её git clone.
STABLE_BRANCH = "master"
STABLE_BRANCHES = (STABLE_BRANCH, "main")
TEST_BRANCH = "test"

_memo: dict | None = None


def _read_head() -> tuple[str, str]:
    """(branch, sha) из .git без запуска git. branch='' если detached/нет репозитория."""
    head = _GIT_DIR / "HEAD"
    try:
        raw = head.read_text(encoding="utf-8").strip()
    except OSError:
        return "", ""
    if raw.startswith("ref:"):
        ref = raw.split(":", 1)[1].strip()                 # refs/heads/<branch>
        branch = ref.rsplit("/", 1)[-1]
        sha = _resolve_ref(ref)
        return branch, sha
    return "", raw                                          # detached HEAD → сырой SHA


def _resolve_ref(ref: str) -> str:
    """SHA по ссылке refs/heads/<branch>: сперва loose-файл, затем packed-refs."""
    loose = _GIT_DIR / Path(ref)
    try:
        return loose.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        for line in (_GIT_DIR / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith(("#", "^")):
                sha, _, name = line.partition(" ")
                if name.strip() == ref:
                    return sha.strip()
    except OSError:
        pass
    return ""


def _compute() -> dict:
    branch, sha = _read_head()
    if not branch and not sha:                             # нет .git — пробуем git как фолбэк
        try:
            out = subprocess.run(["git", "-C", str(BASE_DIR), "rev-parse", "--abbrev-ref", "HEAD"],
                                 capture_output=True, text=True, timeout=5)
            branch = (out.stdout or "").strip()
            out2 = subprocess.run(["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=5)
            sha = (out2.stdout or "").strip()
        except Exception:  # noqa: BLE001
            branch, sha = "", ""
    short = sha[:7]
    if branch in STABLE_BRANCHES:
        channel = "stable"
    elif branch == TEST_BRANCH:
        channel = "test"
    else:
        channel = branch or "unknown"
    is_test = channel != "stable"
    return {"branch": branch, "sha": short, "channel": channel, "is_test": is_test}


def info() -> dict:
    """{'branch','sha','channel','is_test'} — определяется один раз и кэшируется."""
    global _memo
    if _memo is None:
        _memo = _compute()
    return dict(_memo)


def channel() -> str:
    return info()["channel"]


def is_test() -> bool:
    return info()["is_test"]


def label() -> str:
    """Короткая метка для логов/UI: 'STABLE · a1b2c3d' / 'TEST · a1b2c3d'."""
    i = info()
    name = i["channel"].upper()
    return f"{name} · {i['sha']}" if i["sha"] else name
