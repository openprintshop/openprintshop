"""restart_bot.ps1: HEAD для напоминания про relay/ не роняет -Update (ревью buyer#0, 04.10.2026).

Напоминание «выложите relay/ на VPS» (e2910dc) брало HEAD при каждом -Update/-Promote. В копии
без .git (перенос, архив) Windows PowerShell 5.1 под $ErrorActionPreference='Stop' превращает
stderr git'а в NativeCommandError, а без git в PATH падает любой PowerShell — и -Update
отказывался работать там, где раньше делал pip и перезапуск. restart_bot.cmd запускает именно
Windows PowerShell 5.1, поэтому проверяем в нём (и в pwsh, если 5.1 нет).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "restart_bot.ps1"

_RUN = r"""
$ErrorActionPreference = 'Stop'
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:RB_PS1, [ref]$null, [ref]$errs)
if ($errs.Count) { foreach ($e in $errs) { [Console]::Error.WriteLine("PARSE: $e") }; exit 3 }
$f = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Get-Head' }, $true)
Invoke-Expression $f.Extent.Text
$root = $env:RB_ROOT
$h = Get-Head
[Console]::Out.Write("HEAD=[$h]")
"""


def _shells() -> list[str]:
    found = [s for s in (shutil.which("powershell"), shutil.which("pwsh")) if s]
    if not found:
        pytest.skip("нет PowerShell")
    return found


def _get_head(shell: str, root: Path, path_env: str | None = None) -> subprocess.CompletedProcess:
    # GIT_* не наследуем: хук pre-push выставляет GIT_DIR, а в git worktree это абсолютный путь —
    # git внутри «копии без .git» находил репозиторий, и тест падал только при пуше из worktree.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(RB_PS1=str(SCRIPT), RB_ROOT=str(root))
    if path_env is not None:
        env["PATH"] = path_env
    return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", _RUN], env=env,
                          capture_output=True, timeout=60, encoding="utf-8", errors="replace")


def test_get_head_without_git_dir_does_not_throw(tmp_path):
    for shell in _shells():
        res = _get_head(shell, tmp_path)
        assert res.returncode == 0, (shell, res.stdout, res.stderr)
        assert "HEAD=[]" in res.stdout, (shell, res.stdout)


def test_get_head_without_git_in_path_does_not_throw(tmp_path):
    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for shell in _shells():
        path = os.pathsep.join([system, os.path.dirname(shell)])
        res = _get_head(shell, tmp_path, path_env=path)
        assert res.returncode == 0, (shell, res.stdout, res.stderr)
        assert "HEAD=[]" in res.stdout, (shell, res.stdout)


def test_get_head_in_repo_returns_sha():
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("нет git или репозитория")
    for shell in _shells():
        res = _get_head(shell, ROOT)
        assert re.search(r"HEAD=\[[0-9a-f]{40}\]", res.stdout), (shell, res.stdout, res.stderr)


def test_head_for_reminder_only_in_relay_mode():
    """Обычной установке (TG_SOURCE=poll) напоминание не нужно — и git ради него не трогаем."""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    line = next(ln for ln in src.splitlines() if ln.startswith("$headBefore ="))
    assert "Get-TgSource" in line and "'relay'" in line
