<#
.SYNOPSIS
    Установка OpenPrintShop на чистую Windows-машину «в одну команду».

.DESCRIPTION
    Bootstrap-установщик: ставит зависимости, разворачивает код, создаёт окружение и сторожа.
    Идемпотентен — повторный запуск ДОУСТАНАВЛИВАЕТ недостающее и обновляет код, ничего не
    ломая (data/ и .env не трогаются). Именно его вызывает и MSI (см. OpenPrintShop.wxs) —
    вся логика живёт здесь, чтобы «через MSI» и «руками» ставилось одинаково.

    Шаги:
      1) winget: Python 3.13 и Git, если их нет (при -SkipDeps — пропустить);
      2) git clone/pull кода в -InstallDir (нужен доступ к репозиторию: публичный URL,
         deploy-токен в URL или заранее настроенный ключ);
      3) python -m venv .venv + pip install -r requirements.txt;
      4) .env из .env.example; BOT_TOKEN/ADMIN_IDS/DATA_DIR — из параметров, а в
         интерактивном сеансе спрашиваются (DATA_DIR по умолчанию <InstallDir>\data);
      5) генерирует start_watchdog.vbs с ПРАВИЛЬНЫМИ путями (в репозитории пути
         зашиты под машину разработчика) и регистрирует задачу Планировщика pcb_watchdog
         (запуск при входе в систему + вручную; сторож сам поднимает бота и держит его живым);
      6) стартует сторожа и ждёт ответ бота на http://127.0.0.1:8081/health.

.EXAMPLE
    .\install.ps1 -BotToken "123:ABC" -AdminIds "111,222"
.EXAMPLE
    .\install.ps1 -DataDir D:\OpenPrintShopData    # данные на отдельном диске (C2)
.EXAMPLE
    # постоянный адрес студии через именованный туннель Cloudflare (C3)
    .\install.ps1 -TunnelMode named -PublicUrl https://app.mystudio.ru -CfTunnelToken "eyJhIjoi..."
.EXAMPLE
    .\install.ps1 -InstallDir D:\OpenPrintShop -RepoUrl https://github.com/<вы>/openprintshop.git -NoStart   # свой форк
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\OpenPrintShop\bot",
    [string]$RepoUrl = "https://github.com/openprintshop/openprintshop.git",
    [string]$Branch = "main",
    [string]$BotToken = "",
    [string]$AdminIds = "",
    # Каталог данных (C2): база, кэши, фото, логи, бэкапы. Пусто — <InstallDir>\data, как
    # раньше. Отдельный диск/раздел задаётся здесь и уезжает в .env строкой DATA_DIR=.
    [string]$DataDir = "",
    # Вход наружу (C3): quick — временный туннель Cloudflare (адрес меняется при каждом
    # перезапуске бота), named — именованный туннель по токену студии (постоянный адрес),
    # none — адрес обеспечивает владелец сам (Funnel/свой обратный прокси).
    [ValidateSet("", "quick", "named", "none")]
    [string]$TunnelMode = "",
    # Токен именованного туннеля Cloudflare — СЕКРЕТ, в вывод установщика не печатается.
    [string]$CfTunnelToken = "",
    # Постоянный публичный https-адрес мини-апки (обязателен для named и none).
    [string]$PublicUrl = "",
    # Лицензионный ключ (необязательно): открытой версии ключи не нужны — механизм спит, пока в
    # сборке нет ключа выпуска (app/license.py). Оставлен для сборок, которые их выдают.
    [string]$LicenseKey = "",
    # Редакция сборки. Пустой EDITION определяется по наличию .git (app/edition.py), а
    # установка — это git clone, поэтому покупатель без этой строки опознавался бы как dev
    # и видел бы личные инструменты владельца. Пишется ТОЛЬКО если в .env ключа ещё нет:
    # явно выставленную редакцию (EDITION=dev у разработчика) установщик не перетирает.
    [string]$Edition = "studio",
    # Подпись к суммам в интерфейсе («р.», «руб.», «$»). Курсов и пересчёта нет — это
    # ровно надпись рядом с числом. Пусто — оставить как в .env.example.
    [string]$Currency = "",
    # Название студии в мини-приложении (white-label). Живёт не в .env, а в НАСТРОЙКАХ
    # базы (text_settings.brand_title), поэтому записывается после первого старта бота.
    [string]$StudioName = "",
    # ---- Опциональные модули. "1" — ставить, всё остальное — нет. ----------------------
    # Строки, а НЕ [switch]: значения приходят из реестра через Set-Variable, а присвоение
    # строки переключателю делает его истинным ВСЕГДА и ложным никогда.
    #
    # «Аватарки ИИ» и «умный поиск»: ~1.3 ГБ и 15–40 минут, работают только с видеокартой
    # NVIDIA. Без них бот полноценен — аватарка рисуется процедурно, поиск подстрочный.
    [string]$WithAvatarAi = "0",
    [string]$WithSmartSearch = "0",
    # Автозагрузка моделей с MakerWorld: пакет + отдельная закачка браузера (~250 МБ).
    [string]$WithMakerWorld = "0",
    # Инструменты разработчика (pytest/ruff/mypy). Покупателю не нужны никогда.
    [switch]$Dev,
    [switch]$SkipDeps,     # не трогать Python/Git (уже стоят или ставятся политикой компании)
    [switch]$NoStart,      # только установить, не запускать сторожа
    # Стенд/вторая установка на той же машине. Задача Планировщика ГЛОБАЛЬНА: регистрация с
    # обычным именем перенацелила бы уже работающего сторожа на новый каталог, то есть тихо
    # увела бы боевого бота. -NoSchedule ставит всё, кроме задачи; -TaskName даёт своё имя.
    [switch]$NoSchedule,
    [string]$TaskName = 'pcb_watchdog',
    # Служебный ключ для custom action MSI — НЕ ставить, а заказать установку (см. ниже).
    [switch]$Handoff,
    # Служебный: файл с ответами мастера, который оставляет эстафета (см. ниже).
    [string]$ArgsFile = "",
    # Установка без человека: вопросы не задаются, берутся значения по умолчанию.
    # Ставится автоматически при запуске из MSI — там отвечать некому.
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$taskName = $TaskName
$healthUrl = 'http://127.0.0.1:8081/health'

# ---- ответы мастера из реестра ----------------------------------------------------------
# Их кладёт туда MSI (компонент C_SetupProps). Через командную строку custom action их
# передавать НЕЛЬЗЯ: кириллица проходит ANSI-кодировку системы и «р.» приезжает как «СЂ.»
# (поймано сквозным прогоном 28.08). Ключ содержит токен бота и пароль доступа к зеркалу,
# поэтому читаем и СРАЗУ стираем значения.
$SetupKey = 'HKLM:\SOFTWARE\OpenPrintShop\Setup'
if (Test-Path $SetupKey) {
    $reg = Get-ItemProperty -Path $SetupKey -ErrorAction SilentlyContinue
    foreach ($name in 'RepoUrl', 'Branch', 'BotToken', 'AdminIds', 'Currency', 'StudioName', 'DataDir',
                      'LicenseKey', 'WithAvatarAi', 'WithSmartSearch', 'WithMakerWorld') {
        $v = $reg.$name
        if ("$v".Trim()) { Set-Variable -Name $name -Value ([string]$v) }
    }
    try { Remove-Item -Path $SetupKey -Recurse -Force -ErrorAction Stop }
    catch { }   # снести может только повышенный процесс; значения затрёт следующая установка
}

# ---- ответы мастера, оставленные эстафетой ----------------------------------------------
# Файл пишет сам этот скрипт при -Handoff. В нём лежат секреты (токен бота, ключ доступа
# к зеркалу), поэтому читаем и СРАЗУ удаляем — на диске он живёт секунды.
if ($ArgsFile -and (Test-Path $ArgsFile)) {
    try {
        $saved = Get-Content $ArgsFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($prop in $saved.PSObject.Properties) {
            if (-not "$($prop.Value)".Trim()) { continue }
            # Unattended — переключатель, а не строка: присвоение строки сделало бы его
            # истинным всегда, но и ложным никогда не станет — поэтому обрабатываем явно.
            if ($prop.Name -eq 'Unattended') { $Unattended = $true; continue }
            Set-Variable -Name $prop.Name -Value ([string]$prop.Value)
        }
    } catch {
        Write-Host "    не удалось прочитать ответы мастера ($ArgsFile): $($_.Exception.Message)" -ForegroundColor Yellow
    }
    Remove-Item $ArgsFile -Force -ErrorAction SilentlyContinue
}

# ---- эстафета из MSI --------------------------------------------------------------------
# Изнутри custom action ставить зависимости НЕЛЬЗЯ: Windows Installer держит глобальную
# блокировку (_MSIExecute), и любой вложенный установщик — winget, python.org, что угодно
# на базе MSI — падает с кодом 1618 «другая установка уже идёт» (поймано стендом 27.08).
# Поэтому CA передаёт эстафету: разовая задача Планировщика запускает этот же скрипт в
# сеансе пользователя ПОСЛЕ завершения нашего MSI — с полным окружением и видимым окном.
if ($Handoff) {
    $self = $MyInvocation.MyCommand.Path
    # Ответы мастера едут ФАЙЛОМ, а не в аргументах задачи: кириллица (валюта, название
    # студии) в командной строке Планировщика портится, и установщик получал мусор. До
    # этой правки эстафета вообще пробрасывала ОДИН -RepoUrl — все ответы клиента
    # терялись, и .env создавался из шаблона (поймано сквозным прогоном 28.08).
    $keep = 'InstallDir', 'RepoUrl', 'Branch', 'BotToken', 'AdminIds', 'DataDir', 'TunnelMode',
            'PublicUrl', 'CfTunnelToken', 'LicenseKey', 'Edition', 'Currency', 'StudioName', 'TaskName',
            'WithAvatarAi', 'WithSmartSearch', 'WithMakerWorld'
    $data = @{}
    foreach ($name in $keep) {
        $v = Get-Variable -Name $name -ValueOnly -ErrorAction SilentlyContinue
        if ($v -is [string] -and $v.Trim()) { $data[$name] = $v }
    }
    # Запуск из MSI — всегда без человека: ответы уже собраны мастером, консоли для
    # вопросов нет, и Read-Host повесил бы установку насмерть.
    $data['Unattended'] = '1'
    $dir = Join-Path $env:ProgramData 'OpenPrintShop'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $af = Join-Path $dir 'bootstrap-args.json'
    [IO.File]::WriteAllText($af, ($data | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -ArgsFile `"$af`""
    $a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine `
         -WorkingDirectory (Split-Path $self -Parent)
    # Задачу создаёт ПОВЫШЕННЫЙ msiexec, а сам bootstrap работает без повышения и удалить
    # её потом не может («отказано в доступе», поймано стендом 27.08) — иначе разовая
    # задача осталась бы мусором в Планировщике клиента. Поэтому уборку делает сам
    # Планировщик: срок годности триггера + DeleteExpiredTaskAfter.
    $t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(20)
    $t.EndBoundary = (Get-Date).AddHours(6).ToString('s')
    $set = New-ScheduledTaskSettingsSet -DeleteExpiredTaskAfter ([TimeSpan]::FromMinutes(5)) `
           -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::FromHours(3))
    Register-ScheduledTask -TaskName 'OpenPrintShopBootstrap' -Action $a -Trigger $t -Settings $set -Force | Out-Null
    exit 0
}

# MSI передаёт -RepoUrl всегда, но значение может оказаться пустым (сборка «для себя»,
# без адреса зеркала). Пустая строка иначе перебила бы умолчание параметра, и клон ушёл
# бы в никуда с невнятным «repository not found».
if (-not $RepoUrl.Trim()) { $RepoUrl = 'https://github.com/openprintshop/openprintshop.git' }

# Журнал установки. Из MSI скрипт запускается custom action'ом с Return=ignore: провал
# bootstrap НЕ откатывает установку и на экране не виден вовсе — клиент получает «успешно»
# и нерабочего бота. Пишем стенограмму на диск, чтобы было что прислать в поддержку.
$logDir = Join-Path $env:ProgramData 'OpenPrintShop'
try {
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    $logFile = Join-Path $logDir ("install-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
    Start-Transcript -Path $logFile -Force | Out-Null
    $transcript = $true
} catch { $transcript = $false }   # нет прав на ProgramData — ставим без журнала, это не повод падать

# Отметка времени у каждого шага. Стенограмма установки — единственное, что остаётся от
# неудачной установки у клиента; без часов по ней нельзя сказать, ГДЕ встало: скачивание
# Python, winget или pip. Секунды от старта, а не время суток, — сразу читается как «сколько
# это заняло» (29.08.2026, при разборе шести минут до создания venv).
$script:StepClock = Get-Date
function Step($m) {
    $el = ((Get-Date) - $script:StepClock).TotalSeconds
    Write-Host ("==> [{0,6:N1}s] {1}" -f $el, $m) -ForegroundColor Cyan
}
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }

# Стенограмма записывает командную строку запуска ЦЕЛИКОМ (заголовок Host Application),
# а в ней — пароль зеркала из -RepoUrl. Маскировать на выходе — единственный способ:
# заголовок пишет сам Start-Transcript, и до Stop-Transcript файл не редактируется.
function Complete-InstallLog {
    # Разовая задача-эстафета больше не нужна. Обычно удалить её не выйдет (создана
    # повышенным msiexec) — тогда её уберёт сам Планировщик по DeleteExpiredTaskAfter.
    try { Unregister-ScheduledTask -TaskName 'OpenPrintShopBootstrap' -Confirm:$false -ErrorAction SilentlyContinue } catch { }
    if (-not $script:transcript) { return }
    $script:transcript = $false
    try { Stop-Transcript | Out-Null } catch { }
    try {
        (Get-Content $logFile -Raw) -replace '//[^/@\s"]+@', '//***@' |
            Set-Content -Path $logFile -Encoding utf8
    } catch { }
}
# throw в любом месте ниже не должен оставить журнал с паролем: trap чистит и пробрасывает.
trap { Complete-InstallLog; break }

# ---- 0. PATH: собрать заново из реестра -------------------------------------------------
# Custom action MSI получает окружение СЛУЖБЫ msiexec, а не пользователя: в его PATH нет
# ни %LOCALAPPDATA%\Microsoft\WindowsApps (winget!), ни пользовательских дополнений —
# ровно поэтому прогон стенда 27.08 умирал с «winget недоступен» на машине, где winget есть.
$env:Path = ([Environment]::GetEnvironmentVariable('Path', 'Machine'),
             [Environment]::GetEnvironmentVariable('Path', 'User'),
             (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps') -join ';')

# ---- 1. Зависимости: winget, при его отказе — прямая загрузка ---------------------------
# «Команда есть в PATH» ≠ «инструмент установлен»: на чистой Windows 11 python.exe в PATH —
# это ЗАГЛУШКА Microsoft Store (открывает магазин и жалуется в stderr). Проверяем ЗАПУСКОМ.
function Test-RealTool([string]$cmd, [string]$expect) {
    try {
        $out = (& $cmd --version 2>&1 | Out-String)
        return ($LASTEXITCODE -eq 0 -and $out -match $expect)
    } catch { return $false }
}

function Update-SessionPath {
    $env:Path = ([Environment]::GetEnvironmentVariable('Path', 'Machine'),
                 [Environment]::GetEnvironmentVariable('Path', 'User'),
                 (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps') -join ';')
}

# Windows Installer однозадачен: пока где-то идёт другая установка, любая новая падает с
# 1618. Ждём освобождения глобального мьютекса — он существует ТОЛЬКО во время установки.
function Wait-MsiIdle([int]$maxMin = 10) {
    $t0 = Get-Date
    while ($true) {
        $busy = $true
        try {
            $m = [System.Threading.Mutex]::OpenExisting('Global\_MSIExecute')
            $m.Dispose()
        } catch [System.Threading.WaitHandleCannotBeOpenedException] { $busy = $false } catch { }
        if (-not $busy) { return }
        if (((Get-Date) - $t0).TotalMinutes -ge $maxMin) {
            Warn "Windows Installer занят чужой установкой дольше $maxMin мин — пробую всё равно"
            return
        }
        Warn 'Windows Installer занят другой установкой — жду...'
        Start-Sleep -Seconds 15
    }
}

# Прямая загрузка — на случай, когда winget недоступен или отказал (например, winget
# под чужим токеном известно капризен). Версии закреплены.
function Install-PythonDirect {
    $ver = '3.13.7'
    $url = "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
    $exe = Join-Path $env:TEMP "python-$ver-amd64.exe"
    Step "Качаю Python $ver напрямую с python.org..."
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    Step 'Ставлю Python (тихо, для текущего пользователя)...'
    # InstallAllUsers=0: bootstrap работает без повышения — машинная установка упала бы
    # по правам. Три попытки против 1618: чужая установка может начаться в любой момент.
    for ($try = 1; $try -le 3; $try++) {
        Wait-MsiIdle
        $p = Start-Process $exe -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=1','Include_test=0' -Wait -PassThru
        if ($p.ExitCode -eq 0) { break }
        if ($p.ExitCode -ne 1618 -or $try -eq 3) { throw "установщик Python вернул $($p.ExitCode)" }
        Warn "попытка ${try}: Windows Installer занят (1618) — повторю"
        Start-Sleep -Seconds 20
    }
    Remove-Item $exe -ErrorAction SilentlyContinue
    Update-SessionPath
}

function Install-GitDirect {
    $ver = '2.47.1'
    $url = "https://github.com/git-for-windows/git/releases/download/v$ver.windows.1/MinGit-$ver-64-bit.zip"
    $zip = Join-Path $env:TEMP 'mingit.zip'
    $dst = Join-Path $env:LOCALAPPDATA 'Programs\MinGit'
    Step "Качаю MinGit $ver (git-for-windows)..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $dst -Force
    Remove-Item $zip -ErrorAction SilentlyContinue
    $bin = Join-Path $dst 'cmd'
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$bin*") {
        [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $bin), 'User')
    }
    Update-SessionPath
}

function Ensure-Dependency([string]$cmd, [string]$wingetId, [string]$human, [string]$expect, [scriptblock]$direct) {
    if (Test-RealTool $cmd $expect) { Ok "$human уже есть"; return }
    if ($SkipDeps) { throw "$human не найден, а -SkipDeps запрещает установку" }
    $viaWinget = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Step "Ставлю $human (winget $wingetId)..."
        Wait-MsiIdle
        winget install --id $wingetId --exact --silent --accept-package-agreements --accept-source-agreements
        Update-SessionPath
        $viaWinget = (Test-RealTool $cmd $expect)
        if (-not $viaWinget) { Warn "winget не справился — пробую прямую загрузку" }
    } else {
        Warn "winget недоступен в этом окружении — прямая загрузка"
    }
    if (-not $viaWinget) {
        & $direct
        if (-not (Test-RealTool $cmd $expect)) {
            throw "$human поставлен, но '$cmd --version' не отвечает — откройте новую консоль и повторите установку"
        }
    }
    Ok "$human установлен"
}

Step 'Проверяю зависимости...'
Ensure-Dependency 'python' 'Python.Python.3.13' 'Python 3.13' '^Python 3' ${function:Install-PythonDirect}
Ensure-Dependency 'git' 'Git.Git' 'Git' 'git version' ${function:Install-GitDirect}

# ---- 2. Код -----------------------------------------------------------------------------
if (Test-Path (Join-Path $InstallDir '.git')) {
    Step "Код уже развёрнут — обновляю ($Branch)..."
    git -C $InstallDir fetch origin $Branch
    if ($LASTEXITCODE -ne 0) { throw 'git fetch не удался — проверьте доступ к репозиторию' }
    git -C $InstallDir checkout $Branch
    git -C $InstallDir merge --ff-only "origin/$Branch"
    if ($LASTEXITCODE -ne 0) { Warn 'ff-merge не прошёл (локальные правки?) — код НЕ обновлён' }
} else {
    # URL печатаем БЕЗ userinfo: логин и пароль зеркала иначе осели бы открытым текстом
    # в журнале установки (%ProgramData%\OpenPrintShop) — том самом, который клиент шлёт
    # в поддержку. Сам git тоже не должен их разглашать в подсказках об ошибках.
    $printUrl = $RepoUrl -replace '//[^/@]+@', '//***@'
    Step "Клонирую $printUrl → $InstallDir..."
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir -Parent) | Out-Null
    git clone --branch $Branch $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { throw 'git clone не удался — проверьте URL/доступ (deploy-токен?)' }
}
Ok "код: $(git -C $InstallDir log -1 --oneline)"

# ---- 3. Окружение Python ----------------------------------------------------------------
$venvPy = Join-Path $InstallDir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Step 'Создаю виртуальное окружение .venv...'
    python -m venv (Join-Path $InstallDir '.venv')
}
Step 'Ставлю зависимости Python (ядро)...'
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $InstallDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install не прошёл — см. вывод выше' }
Ok 'ядро установлено (~130 МБ)'

# ---- 3б. Опциональные модули ------------------------------------------------------------
# Ставятся ТОЛЬКО по явной галочке. До разделения (28.08.2026) всё это уезжало каждому
# покупателю: установка занимала около часа, и большая часть груза не могла заработать
# в принципе — диффузионный стек гейтится на наличии видеокарты NVIDIA.
function Install-Extra([string]$file, [string]$human) {
    $path = Join-Path $InstallDir $file
    if (-not (Test-Path $path)) { Warn "нет файла $file — модуль «$human» пропущен"; return $false }
    Step "Ставлю модуль «$human» ($file)..."
    & $venvPy -m pip install -r $path
    if ($LASTEXITCODE -ne 0) {
        # Провал ДОПОЛНИТЕЛЬНОГО модуля не должен рушить установку: ядро уже стоит,
        # бот заработает, а недостающая фича честно скажет о себе «недоступна».
        Warn "модуль «$human» не установился — бот будет работать без него"
        return $false
    }
    Ok "модуль «$human» установлен"
    return $true
}

$aiWanted = ($WithAvatarAi -eq '1') -or ($WithSmartSearch -eq '1')
if ($aiWanted) {
    Warn 'Модули с ИИ весят ~1.3 ГБ — это надолго (15–40 минут).'
    Warn 'Первый вызов дополнительно скачает саму модель с HuggingFace (несколько ГБ).'
    Install-Extra 'requirements-ai.txt' 'ИИ: аватарки и умный поиск' | Out-Null
}
if ($WithMakerWorld -eq '1' -and -not (Test-Path (Join-Path $InstallDir 'requirements-makerworld.txt'))) {
    Warn 'автозагрузка с MakerWorld не входит в эту версию — модуль пропущен'
} elseif ($WithMakerWorld -eq '1') {
    if (Install-Extra 'requirements-makerworld.txt' 'автозагрузка с MakerWorld') {
        # Без браузера пакет бесполезен: до разделения playwright ехал всем, а chromium
        # не ставился вовсе — то есть 107 МБ приезжали заведомо нерабочими.
        Step 'Качаю браузер для автозагрузки (playwright install chromium)...'
        & $venvPy -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { Warn 'браузер не скачался — автозагрузка с MakerWorld работать не будет' }
        else { Ok 'браузер установлен' }
    }
}
if ($Dev) { Install-Extra 'requirements-dev.txt' 'инструменты разработчика' | Out-Null }

# ---- 4. Конфигурация (.env) -------------------------------------------------------------
$envFile = Join-Path $InstallDir '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $InstallDir '.env.example') $envFile
    Ok '.env создан из .env.example'
}
# Спросить владельца, если есть кого спрашивать. [Environment]::UserInteractive ловит службы и
# custom action MSI, но НЕ ловит powershell -NonInteractive: там Read-Host бросает исключение и
# установка падает на середине (поймано прогоном стенда 23.08.2026). Поэтому ещё и try/catch.
function Ask([string]$prompt, [string]$default = '') {
    # UserInteractive недостаточно: задача Планировщика с интерактивным входом считается
    # интерактивной, и Read-Host там ждёт ввода ВЕЧНО — установка из MSI просто зависала
    # после «.env создан» (поймано сквозным прогоном 28.08). Признак «человека нет»
    # должен быть явным.
    if ($Unattended -or -not [Environment]::UserInteractive) { return $default }
    try { $v = Read-Host $prompt } catch { return $default }
    if ([string]::IsNullOrWhiteSpace($v)) { return $default }
    return $v.Trim()
}

# То же для секрета: значение не печатается и не попадает в историю консоли.
function AskSecret([string]$prompt) {
    if ($Unattended -or -not [Environment]::UserInteractive) { return '' }
    try { $sec = Read-Host $prompt -AsSecureString } catch { return '' }
    if (-not $sec -or $sec.Length -eq 0) { return '' }
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

if (-not $BotToken) {
    $BotToken = Ask 'Токен бота от @BotFather (пусто — впишете в .env позже)'
}
if (-not $AdminIds) {
    $AdminIds = Ask 'Telegram ID администраторов через запятую (пусто — позже)'
}
if (-not $DataDir) {
    $DataDir = Ask "Каталог данных (пусто — $(Join-Path $InstallDir 'data'))"
}
# -Encoding UTF8 обязателен: скрипт исполняет Windows PowerShell 5.1, где Get-Content без
# явной кодировки читает файл в ANSI системы (у нас cp1251). Файл .env.example лежит в
# UTF-8 БЕЗ BOM, поэтому «руб.» превращалось в «СЂСѓР±.» ещё на ЧТЕНИИ — и уезжало
# искажённым в .env, а оттуда в интерфейс клиента (поймано на стенде 27.08).
$envText = Get-Content $envFile -Raw -Encoding UTF8
if ($BotToken) { $envText = $envText -replace '(?m)^BOT_TOKEN=.*$', "BOT_TOKEN=$BotToken" }
if ($AdminIds) { $envText = $envText -replace '(?m)^ADMIN_IDS=.*$', "ADMIN_IDS=$AdminIds" }
if (-not $TunnelMode) {
    $TunnelMode = (Ask 'Вход наружу: quick (временный адрес) / named (постоянный, Cloudflare) / none (свой адрес). Пусто — quick').ToLower()
}
if ($TunnelMode -in @('named', 'none') -and -not $PublicUrl) {
    $PublicUrl = Ask 'Постоянный адрес мини-приложения, с https:// (обязателен для этого режима)'
}
if ($TunnelMode -eq 'named' -and -not $CfTunnelToken) {
    # Токен читаем как секрет: он даёт полный контроль над туннелем студии.
    $CfTunnelToken = AskSecret 'Токен туннеля Cloudflare (Zero Trust -> Networks -> Tunnels)'
}
if ($DataDir) {
    # «Заменить ИЛИ дописать»: -replace молча ничего не делает, если ключа в файле нет,
    # а в старых .env (и в .env.example до C2) строки DATA_DIR= не было вовсе.
    # Образец в .env.example лежит ЗАКОММЕНТИРОВАННЫМ (пустое DATA_DIR= затирает
    # переменную окружения), поэтому «#» перед ключом тоже считаем за место для подстановки.
    if ($envText -match '(?m)^\s*#?\s*DATA_DIR=.*$') {
        $envText = $envText -replace '(?m)^\s*#?\s*DATA_DIR=.*$', "DATA_DIR=$DataDir"
    } else {
        $envText = $envText.TrimEnd() + "`r`nDATA_DIR=$DataDir`r`n"
    }
}
# Ключи входа наружу: те же правила, что у DATA_DIR — «заменить ИЛИ дописать», причём
# образцы WEBAPP_PUBLIC_URL/CF_TUNNEL_TOKEN лежат в .env.example ЗАКОММЕНТИРОВАННЫМИ.
function Set-EnvKey([string]$text, [string]$key, [string]$value) {
    if ($text -match "(?m)^\s*#?\s*$key=.*$") {
        # $ в подстановке -replace означает ссылку на группу — удваиваем, чтобы значение
        # (например токен) попало в .env буквально.
        $repl = "$key=" + ($value -replace '\$', '$$$$')
        return ($text -replace "(?m)^\s*#?\s*$key=.*$", $repl)
    }
    return ($text.TrimEnd() + "`r`n$key=$value`r`n")
}
if ($TunnelMode)    { $envText = Set-EnvKey $envText 'TUNNEL_MODE' $TunnelMode }
if ($PublicUrl)     { $envText = Set-EnvKey $envText 'WEBAPP_PUBLIC_URL' $PublicUrl }
if ($CfTunnelToken) { $envText = Set-EnvKey $envText 'CF_TUNNEL_TOKEN' $CfTunnelToken }
if ($LicenseKey)    { $envText = Set-EnvKey $envText 'LICENSE_KEY' $LicenseKey }
# Редакция: только ДОПИСАТЬ отсутствующий ключ, никогда не менять существующий —
# иначе повторный запуск установщика на машине разработчика затирал бы EDITION=dev.
if ($Edition -and $envText -notmatch '(?m)^\s*EDITION=') {
    $envText = Set-EnvKey $envText 'EDITION' $Edition
    Ok "редакция сборки: EDITION=$Edition"
}
if ($Currency) {
    $envText = Set-EnvKey $envText 'CURRENCY' $Currency
    Ok "валюта: $Currency"
}
# Выключатели модулей приводим в соответствие с галочками. Без этого .env.example включает
# их по умолчанию (AVATAR_GPU=1, MW_BROWSER_ENABLED=1) и бот делает вид, что путь есть:
# лог зашумлён, а отказ приходит окольным путём вместо честного «функция не установлена».
$envText = Set-EnvKey $envText 'AVATAR_GPU' $(if ($WithAvatarAi -eq '1') { '1' } else { '0' })
$envText = Set-EnvKey $envText 'MW_BROWSER_ENABLED' $(if ($WithMakerWorld -eq '1') { '1' } else { '0' })
$envText = Set-EnvKey $envText 'SEMSEARCH_ENABLED' $(if ($WithSmartSearch -eq '1') { '1' } else { '0' })
# Пишем UTF-8 БЕЗ BOM: в Windows PowerShell 5.1 «-Encoding utf8» ставит BOM, а .env
# читают и python-dotenv, и наши же скрипты — единообразие с .env.example надёжнее.
[IO.File]::WriteAllText($envFile, $envText, (New-Object Text.UTF8Encoding($false)))
if ($TunnelMode -in @('named', 'none') -and -not $PublicUrl) {
    Warn "режим входа $TunnelMode задан без адреса — впишите WEBAPP_PUBLIC_URL в .env, иначе мини-апка снаружи не откроется"
}
if ($TunnelMode -eq 'named' -and $CfTunnelToken) { Ok 'токен туннеля записан в .env (в вывод не печатается)' }
if ($LicenseKey) { Ok 'лицензионный ключ записан в .env (в вывод не печатается)' }

# Каталог данных: заданный -DataDir или прежний <InstallDir>\data.
$dataDir = if ($DataDir) { $DataDir } else { Join-Path $InstallDir 'data' }
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dataDir 'logs') | Out-Null
Ok "каталог данных: $dataDir"

# ---- 4б. Ярлыки настольной админки ------------------------------------------------------
# Ярлыки делает установщик, а НЕ MSI: пакет кладёт в Program Files только сам себя, а код
# бота приезжает клоном позже и в каталог, которого MSI не знает (-InstallDir). Заодно это
# работает и при ручной установке без MSI.
function New-DeskShortcuts {
    $bat = Join-Path $InstallDir 'admin_panel.bat'
    if (-not (Test-Path $bat)) { Warn 'admin_panel.bat не найден — ярлыки админки не создаю'; return }

    # Ярлык на .bat показал бы иконку командной строки. Иконки в комплекте только .png,
    # поэтому один раз собираем из неё .ico средствами самой Windows.
    $ico = Join-Path $InstallDir 'desktop_admin\appicon.ico'
    $png = Join-Path $InstallDir 'desktop_admin\appicon.png'
    if ((-not (Test-Path $ico)) -and (Test-Path $png)) {
        try {
            Add-Type -AssemblyName System.Drawing
            $src = [System.Drawing.Image]::FromFile($png)
            $bmp = New-Object System.Drawing.Bitmap($src, 256, 256)
            $h = $bmp.GetHicon()
            $icon = [System.Drawing.Icon]::FromHandle($h)
            $fs = [IO.File]::Create($ico)
            try { $icon.Save($fs) } finally { $fs.Dispose() }
            $icon.Dispose(); $bmp.Dispose(); $src.Dispose()
        } catch { Warn "иконку собрать не вышло ($($_.Exception.Message)) — ярлык будет со стандартной" }
    }

    $sh = New-Object -ComObject WScript.Shell
    $targets = @(
        (Join-Path ([Environment]::GetFolderPath('Desktop')) 'OpenPrintShop — админка.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Programs')) 'OpenPrintShop — админка.lnk')
    )
    foreach ($lnkPath in $targets) {
        try {
            $lnk = $sh.CreateShortcut($lnkPath)
            $lnk.TargetPath       = $bat
            $lnk.WorkingDirectory = $InstallDir
            $lnk.Description      = 'Настольная админка OpenPrintShop: заказы, склад, принтеры, статус'
            $lnk.WindowStyle      = 7          # свернуть окно cmd: оболочка открывается своим окном
            if (Test-Path $ico) { $lnk.IconLocation = $ico }
            $lnk.Save()
        } catch { Warn "ярлык $lnkPath не создан: $($_.Exception.Message)" }
    }
    Ok 'ярлыки «OpenPrintShop — админка» на рабочем столе и в меню «Пуск»'
}
New-DeskShortcuts

# ---- 5. Сторож в Планировщике задач -----------------------------------------------------
# vbs генерируем с путями ЭТОЙ установки (в репозитории лежит копия с путями разработчика).
# Лежит он в InstallDir, а НЕ в data/: это артефакт установки, и на съёмном/сетевом data/
# задача при входе в систему стартовала бы раньше монтирования тома (C2).
# ВНИМАНИЕ: файл пишется в ASCII (WScript разборчив к кодировкам), поэтому текст внутри —
# ТОЛЬКО латиница: кириллица превращается в «?» (поймано прогоном стенда 23.08.2026).
$vbs = Join-Path $InstallDir 'start_watchdog.vbs'
@"
' Generated by install.ps1 - starts the OpenPrintShop Bot watchdog with no window.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$InstallDir"
sh.Run """$venvPy"" watchdog.py", 0, False
"@ | Set-Content -Path $vbs -Encoding ascii

if ($NoSchedule) {
    Ok "задача Планировщика не трогается (-NoSchedule); сторож запускается вручную: wscript `"$vbs`""
    Step 'Готово (-NoSchedule): установка собрана, автозапуск не настроен'
    Complete-InstallLog
    exit 0
}
Step "Регистрирую задачу Планировщика '$taskName'..."
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`"" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Ok "задача '$taskName' зарегистрирована (старт при входе в систему)"

if ($NoStart) {
    Step "Готово (-NoStart): запустить можно командой Start-ScheduledTask $taskName"
    Complete-InstallLog
    exit 0
}

# ---- 6. Первый запуск -------------------------------------------------------------------
Step 'Запускаю сторожа (он поднимет бота)...'
Start-ScheduledTask -TaskName $taskName
Step 'Жду ответ бота на /health (до ~90 с; первый старт дольше — прогрев кэшей)...'
$deadline = (Get-Date).AddSeconds(90)
$alive = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 5 -UseBasicParsing
        if ($r.StatusCode -eq 200 -and "$($r.Content)".Trim() -eq 'ok') { $alive = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}
if ($alive) {
    Ok 'бот запущен и отвечает /health → ok'

    # Название студии — не в .env, а в текстовых настройках базы. База рождается при
    # ПЕРВОМ старте бота, поэтому пишем её только сейчас, когда /health уже ответил.
    if ($StudioName) {
        # Пишем ПРЯМЫМ SQL, а не через `from app import db`: импорт приложения тянет
        # конфиг и асинхронный слой, и любая мелочь там роняет запись (поймано прогоном
        # 28.08). Таблица к этому моменту уже создана — бот успел подняться.
        # Значение едет ПЕРЕМЕННОЙ ОКРУЖЕНИЯ, а не аргументом: кириллица в командной
        # строке дочернего процесса портится, в окружении — нет.
        $py = @'
import os, sqlite3
db = os.environ["PSB_DB"]
val = os.environ["PSB_BRAND"]
c = sqlite3.connect(db, timeout=20)
c.execute("CREATE TABLE IF NOT EXISTS text_settings (key TEXT PRIMARY KEY, value TEXT)")
c.execute("INSERT INTO text_settings(key, value) VALUES('brand_title', ?) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (val,))
c.commit()
c.close()
'@
        $pyFile = Join-Path $env:TEMP 'psb_brand.py'
        [IO.File]::WriteAllText($pyFile, $py, (New-Object Text.UTF8Encoding($false)))
        $dbPath = Join-Path $dataDir 'bot.db'
        try {
            if (-not (Test-Path $dbPath)) { throw "базы ещё нет: $dbPath" }
            $env:PSB_DB = $dbPath
            $env:PSB_BRAND = $StudioName
            & $venvPy $pyFile 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { Ok "название студии: $StudioName" }
            else { Warn 'название студии записать не удалось — задайте его в админке (Настройки → Бренд)' }
        } catch {
            Warn "название студии записать не удалось ($($_.Exception.Message)) — задайте в админке"
        } finally {
            Remove-Item Env:\PSB_DB, Env:\PSB_BRAND -ErrorAction SilentlyContinue
            Remove-Item $pyFile -ErrorAction SilentlyContinue
        }
    }

    Write-Host ''
    Write-Host 'Установка завершена. Дальше:' -ForegroundColor Cyan
    Write-Host ' - напишите боту /start в Telegram (админ получает админ-меню);'
    Write-Host " - настройки принтера/слайсера — в $envFile и в админке;"
    Write-Host ' - обновления: админка → Статус → «Обновления» (режим notify/auto).'
} else {
    Warn 'бот пока не ответил на /health. Логи:'
    Warn "  $(Join-Path $dataDir 'logs\bot.log')"
    Warn 'частая причина — не заполнен BOT_TOKEN в .env'
    Complete-InstallLog
    exit 1
}
if ($transcript) { Ok "журнал установки: $logFile" }
Complete-InstallLog
