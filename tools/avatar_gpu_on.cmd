@echo off
chcp 65001 >nul
rem OpenTodo ac6c7e0d: vkl II-avatarki obratno (model progreetsya v fone)
rem C2: katalog dannyh - iz DATA_DIR (.env) cherez app.config; posle perenosa data/
rem tumbler dolzhen udalyat flag tam zhe, kuda smotrit bot.
set "DD=%~dp0..\data"
pushd "%~dp0.."
for /f "usebackq delims=" %%D in (`.venv\Scripts\python.exe -c "from app import config;print(config.DATA_DIR)"`) do set "DD=%%D"
popd
del /q "%DD%\avatar_gpu.off" 2>nul
curl.exe -s "http://127.0.0.1:8081/local/avatar_gpu/refresh" >nul 2>&1
echo.
echo [OK] II-generaciya avatarok VKLYUCHENA - model gruzitsya v VRAM v fone (~10 sek).
timeout /t 4 >nul
