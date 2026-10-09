@echo off
chcp 65001 >nul
rem OpenTodo ac6c7e0d: vykl II-avatarki (osvobodit VRAM dlya igr); fail-flag - istochnik istiny
rem C2: katalog dannyh - iz DATA_DIR (.env) cherez app.config; posle perenosa data/
rem tumbler dolzhen pisat flag tuda zhe, kuda smotrit bot.
set "DD=%~dp0..\data"
pushd "%~dp0.."
for /f "usebackq delims=" %%D in (`.venv\Scripts\python.exe -c "from app import config;print(config.DATA_DIR)"`) do set "DD=%%D"
popd
echo off> "%DD%\avatar_gpu.off"
curl.exe -s "http://127.0.0.1:8081/local/avatar_gpu/refresh" >nul 2>&1
echo.
echo [OK] II-generaciya avatarok VYKLYUCHENA - VRAM svobodna, mozhno igrat.
echo      Yuzery poluchayut procedurnye avatarki.
timeout /t 4 >nul
