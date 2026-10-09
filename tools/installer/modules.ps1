<#
.SYNOPSIS
    Включение и выключение дополнительных модулей OpenPrintShop Bot ПОСЛЕ установки.

.DESCRIPTION
    В мастере установки модули — снятые галочки: покупатель получает ядро (~130 МБ) и не
    ждёт час ради того, чем, возможно, никогда не воспользуется. Но снятая галочка не должна
    быть приговором: этот скрипт доставляет модуль на уже работающую установку — ставит
    недостающие пакеты и переключает флаг в `.env`, после чего бот перезапускается.

    Обратная операция (`-Disable`) только гасит флаг: пакеты остаются на диске. Так задумано —
    удаление пакетов pip'ом рискует зацепить общие зависимости ядра (например `numpy`,
    который нужен и trimesh, и torch). Освободить место надёжнее переустановкой.

.EXAMPLE
    .\modules.ps1
    Показать, что включено, а что нет.

.EXAMPLE
    .\modules.ps1 -Enable ai
    Поставить ИИ-стек (~1.3 ГБ) и включить аватарки. Нужна видеокарта NVIDIA.

.EXAMPLE
    .\modules.ps1 -Enable makerworld
    Поставить playwright и браузер Chromium, включить автозагрузку с MakerWorld.
#>
[CmdletBinding()]
param(
    # ai | smartsearch | makerworld
    [string]$Enable,
    [string]$Disable,
    [string]$InstallDir = "C:\OpenPrintShop\bot",
    [string]$TaskName = 'pcb_watchdog'
)

$ErrorActionPreference = 'Stop'

function Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok  ([string]$m) { Write-Host "    $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "    $m" -ForegroundColor Yellow }

# Модуль = флаг в .env + список пакетов. `ai` и `smartsearch` делят ОДИН список: оба стоят на
# torch, и ставить его дважды разными наборами — верный способ получить «transformers есть,
# torch нет» и молча деградировавший поиск.
$MODULES = @{
    ai           = @{ flag = 'AVATAR_GPU';         req = 'requirements-ai.txt';
                      human = 'аватарки клиентов, нарисованные ИИ';
                      note  = 'нужна видеокарта NVIDIA; ~1.3 ГБ пакетов + модель с HuggingFace при первом вызове' }
    smartsearch  = @{ flag = 'SEMSEARCH_ENABLED';  req = 'requirements-ai.txt';
                      human = 'умный поиск по каталогу (по смыслу, а не по буквам)';
                      note  = 'работает на процессоре; ~1.3 ГБ пакетов + модель ~0.5 ГБ при первом поиске' }
    makerworld   = @{ flag = 'MW_BROWSER_ENABLED'; req = 'requirements-makerworld.txt';
                      human = 'автозагрузка моделей с MakerWorld настоящим браузером';
                      note  = '~107 МБ пакета + ~150 МБ браузера Chromium' }
}
# MakerWorld — личный модуль владельца: в открытой версии проекта его файлов нет, и предлагать
# его установку там нечем (pip упал бы на отсутствующем requirements-makerworld.txt).
if (-not (Test-Path (Join-Path $InstallDir 'requirements-makerworld.txt'))) { $MODULES.Remove('makerworld') }

$envFile = Join-Path $InstallDir '.env'
$venvPy  = Join-Path $InstallDir '.venv\Scripts\python.exe'
if (-not (Test-Path $envFile)) { throw "Не найден $envFile — укажите свой путь через -InstallDir." }
if (-not (Test-Path $venvPy))  { throw "Не найден $venvPy — установка повреждена?" }

# .env читаем ЯВНО в UTF-8: Windows PowerShell без -Encoding берёт системную ANSI (cp1251) и
# портит кириллицу — на этом уже горели названия студии и валюта (28.08.2026).
function Read-Env { Get-Content $envFile -Raw -Encoding UTF8 }
function Write-Env([string]$text) {
    [IO.File]::WriteAllText($envFile, $text, (New-Object Text.UTF8Encoding($false)))
}
function Get-Flag([string]$text, [string]$key) {
    if ($text -match "(?m)^\s*$key\s*=\s*(\S*)\s*$") {
        return ($Matches[1] -notin @('0', 'false', 'no', ''))
    }
    return $null      # ключа нет вовсе — значит работает умолчание из app/config.py
}
function Set-Flag([string]$text, [string]$key, [string]$value) {
    if ($text -match "(?m)^\s*#?\s*$key=.*$") {
        return ($text -replace "(?m)^\s*#?\s*$key=.*$", "$key=$value")
    }
    return ($text.TrimEnd() + "`r`n$key=$value`r`n")
}

$envText = Read-Env

# ------------------------------------------------------------------------------ показать
if (-not $Enable -and -not $Disable) {
    Step 'Дополнительные модули'
    foreach ($k in $MODULES.Keys | Sort-Object) {
        $m = $MODULES[$k]
        $on = Get-Flag $envText $m.flag
        $state = if ($on -eq $true) { 'ВКЛЮЧЁН' } elseif ($on -eq $false) { 'выключен' } else { 'по умолчанию' }
        Write-Host ("  {0,-12} {1,-13} {2}" -f $k, $state, $m.human)
        Write-Host ("  {0,-12} {1,-13} {2}" -f '', '', $m.note) -ForegroundColor DarkGray
    }
    Write-Host ''
    Write-Host '  Включить:  .\modules.ps1 -Enable ai' -ForegroundColor Gray
    Write-Host '  Выключить: .\modules.ps1 -Disable ai   (пакеты остаются на диске)' -ForegroundColor Gray
    return
}

# ----------------------------------------------------------------------------- изменить
$name = if ($Enable) { $Enable } else { $Disable }
$name = $name.ToLower()
if (-not $MODULES.ContainsKey($name)) {
    throw "Неизвестный модуль «$name». Есть: $($MODULES.Keys -join ', ')"
}
$m = $MODULES[$name]

if ($Disable) {
    $envText = Set-Flag $envText $m.flag '0'
    Write-Env $envText
    Ok "модуль «$($m.human)» выключен (пакеты остались на диске)"
}
else {
    $req = Join-Path $InstallDir $m.req
    if (-not (Test-Path $req)) { throw "Нет файла $($m.req) — обновите установку до свежей версии." }
    Step "Ставлю пакеты модуля «$($m.human)»"
    Warn $m.note
    & $venvPy -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw "pip вернул $LASTEXITCODE — модуль НЕ включён, .env не тронут." }

    if ($name -eq 'makerworld') {
        Step 'Качаю браузер (playwright install chromium)'
        & $venvPy -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'браузер не скачался — модуль НЕ включён, .env не тронут.' }
    }

    # Флаг переключаем ТОЛЬКО после успешной установки: иначе бот при старте пообещает
    # фичу, которой нет, и покупатель увидит не «модуль выключен», а «модуль сломан».
    $envText = Set-Flag $envText $m.flag '1'
    Write-Env $envText
    Ok "модуль «$($m.human)» включён"
}

Step 'Перезапускаю бота, чтобы настройка вступила в силу'
try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Ok 'бот перезапущен'
} catch {
    Warn "не удалось перезапустить задачу «$TaskName»: $($_.Exception.Message)"
    Warn 'перезапустите бота вручную — настройка подхватится при старте'
}
