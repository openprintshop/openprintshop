@echo off
rem PC admin panel, stage 1 (2026-07-18): pywebview shell (desktop_admin\shell.py).
rem First run bootstraps a separate venv for the shell; on any failure falls back
rem to the old browser app-window (admin_panel.py). ASCII only: cmd + cp866.
cd /d "%~dp0"
rem Prefer a python.org build over "py -3": under Store Python (WindowsApps) the window
rem inherits the MSIX package identity, so the taskbar shows the Python icon instead of ours,
rem ignoring both the window icon and AppUserModelID. See docs/desktop-admin.md.
set "PYEXE="
for %%P in ("%ProgramFiles%\Python313\python.exe" "%ProgramFiles%\Python312\python.exe" "%LocalAppData%\Programs\Python\Python313\python.exe") do (
  if not defined PYEXE if exist %%P set "PYEXE=%%~P"
)
if not exist "desktop_admin\.venv\Scripts\pythonw.exe" (
  echo First run: creating shell venv, installing dependencies...
  if defined PYEXE (
    "%PYEXE%" -m venv desktop_admin\.venv || goto fallback
  ) else (
    py -3 -m venv desktop_admin\.venv || goto fallback
  )
  "desktop_admin\.venv\Scripts\python.exe" -m pip install -q -r desktop_admin\requirements.txt || goto fallback
)
rem Existing venv may predate new deps (tray, toasts): install only when something is
rem missing. One short import check beats running pip on every launch.
"desktop_admin\.venv\Scripts\python.exe" -c "import webview,pystray,windows_toasts" 2>nul || (
  echo Updating shell dependencies...
  "desktop_admin\.venv\Scripts\python.exe" -m pip install -q -r desktop_admin\requirements.txt
)
start "" "desktop_admin\.venv\Scripts\pythonw.exe" "desktop_admin\shell.py"
exit /b 0
:fallback
echo pywebview shell unavailable, opening browser fallback...
".venv\Scripts\python.exe" admin_panel.py
