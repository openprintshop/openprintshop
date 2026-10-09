@echo off
chcp 65001 >nul
title Restart print-cost bot
echo.
echo   Restarting the print-cost bot (via watchdog)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_bot.ps1"
echo.
echo   Done. Press any key to close this window.
pause >nul
