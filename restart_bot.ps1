<#
.SYNOPSIS
    Перезапуск Telegram-бота расчёта печати. Удобно при обновлении версии и после сбоев.

.DESCRIPTION
    Бота держит живым СТОРОЖ (watchdog.py), запускаемый Планировщиком задач (задача
    'pcb_watchdog'): сторож пингует http://127.0.0.1:8081/health и сам поднимает упавшего бота.
    Поэтому правильный перезапуск — ЧЕРЕЗ сторожа, иначе два менеджера дерутся за порт 8081:
      0) ПРЕДПОЛЁТНЫЕ ПРОВЕРКИ (Assert-ChannelPreconditions) — строго ДО остановки чего-либо:
         всё, что способно отменить операцию (грязное дерево, отсутствующая ветка, конфликт),
         проверяется, пока бот ещё работает, — отказ не должен оставлять сервис лежать;
      1) останавливаем задачу сторожа;
      2) гасим процесс бота (run.py) — по cmdline из этой папки и по data\bot.pid;
      3) (опц. -Update) git pull + pip install в .venv;
      4) запускаем задачу сторожа заново — её ХОЛОДНЫЙ старт поднимает бота с НОВЫМ кодом
         (процесс корректно отвязан Планировщиком и переживает выход из сессии);
      5) ждём, пока бот ответит 'ok' на /health.
    Если задачи сторожа нет — откат на прямой запуск run.py (Start-Process, скрытое окно).

.PARAMETER Update
    Перед перезапуском подтянуть новую версию: git pull (если есть .git) + pip install.

.PARAMETER NoStart
    Только остановить (и бота, и сторожа), не запускать заново.

.PARAMETER Channel
    Канал выпуска (OpenTodo 31485676): 'stable' (ветка master) или 'test' (ветка test).
    Переключает git-ветку и перезапускает бота с её кодом. Тестовую версию так запускают
    ВМЕСТО боевой по просьбе. Требует, чтобы не было незакоммиченных правок в ОТСЛЕЖИВАЕМЫХ
    файлах (иначе откажет, чтобы их не потерять). Неотслеживаемые файлы (новые заметки в docs/
    и т.п.) переключению НЕ мешают: git checkout их не трогает. Проверка выполняется ДО
    остановки бота, поэтому отказ не приводит к простою. data/ и .env в git не отслеживаются,
    поэтому БД и секреты при переключении не трогаются (миграции аддитивны — boevая БД
    совместима с обоими каналами).

.PARAMETER Promote
    Одобрить тест: влить ветку test в stable (master, merge --no-ff) и перезапустить боевую
    версию. Это и есть «если я одобрю, test вливается в stable».

.PARAMETER Status
    Показать текущий канал/ветку и последний коммит и выйти (ничего не перезапуская).

.EXAMPLE
    .\restart_bot.ps1            # стоп + старт через сторожа
.EXAMPLE
    .\restart_bot.ps1 -Update    # обновить код/зависимости, затем перезапустить
.EXAMPLE
    .\restart_bot.ps1 -Channel test     # запустить ТЕСТОВУЮ версию вместо боевой
.EXAMPLE
    .\restart_bot.ps1 -Channel stable   # вернуть боевую версию
.EXAMPLE
    .\restart_bot.ps1 -Promote          # одобрить тест → влить в stable и поднять боевую
.EXAMPLE
    .\restart_bot.ps1 -Status           # какой канал сейчас запущен
#>
[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$NoStart,
    [ValidateSet('stable', 'test')]
    [string]$Channel,
    [switch]$Promote,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
$root      = $PSScriptRoot

# Каталог данных (C2): DATA_DIR из окружения/.env, иначе <папка бота>\data. Тот же порядок,
# что у app/config.py и watchdog.py — иначе бот пишет pid и логи в один каталог, а скрипт
# ищет их в другом и рапортует «бот не найден» при живом боте.
function Get-DataDir([string]$projectRoot) {
    if ($env:DATA_DIR -and $env:DATA_DIR.Trim()) { return $env:DATA_DIR.Trim().Trim('"').Trim("'") }
    $envFile = Join-Path $projectRoot '.env'
    if (Test-Path $envFile) {
        foreach ($ln in Get-Content $envFile -Encoding UTF8) {
            if ($ln -match '^\s*DATA_DIR\s*=\s*(.*)$') {
                $v = $Matches[1].Trim().Trim('"').Trim("'")
                if ($v) { return $v }
            }
        }
    }
    return (Join-Path $projectRoot 'data')
}

$dataDir   = Get-DataDir $root
$venvPy    = Join-Path $root '.venv\Scripts\python.exe'
$runPy     = Join-Path $root 'run.py'
$pidFile   = Join-Path $dataDir 'bot.pid'
$logDir    = Join-Path $dataDir 'logs'
$wdTask    = 'pcb_watchdog'                          # задача Планировщика со сторожем
$healthUrl = 'http://127.0.0.1:8081/health'

function Write-Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Write-Bad($m)  { Write-Host "    $m" -ForegroundColor Yellow }

if (-not (Test-Path $venvPy)) {
    throw "Не найден интерпретатор venv: $venvPy`nСоздайте окружение: python -m venv .venv ; .\.venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path $runPy)) { throw "Не найден run.py в $root" }

# ---- найти процессы бота: по PID-файлу И по командной строке run.py из этой папки ----
function Get-BotProcessIds {
    $ids = New-Object System.Collections.Generic.List[int]
    if (Test-Path $pidFile) {
        $saved = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($saved -match '^\d+$') {
            $p = Get-Process -Id ([int]$saved) -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -like 'python*') { [void]$ids.Add([int]$saved) }
        }
    }
    # Name LIKE 'python%', а не 'python.exe': venv собран поверх Store-Python, где
    # .venv\Scripts\python.exe — редиректор. Настоящий интерпретатор живёт ОТДЕЛЬНЫМ процессом
    # с именем python3.13.exe и той же командной строкой, и все его multiprocessing-дети
    # называются так же. Старый фильтр видел только обёртку на пару мегабайт, а бота со всей
    # его памятью — нет: «(запущенный бот не найден)» при живом боте (разбор 11.08).
    $cim = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue
    foreach ($proc in $cim) {
        $cl = $proc.CommandLine
        if (-not $cl) { continue }
        if ($cl -match 'run\.py' -and ($cl -like "*$root*" -or $proc.ExecutablePath -like "*$root*")) {
            if (-not $ids.Contains([int]$proc.ProcessId)) { [void]$ids.Add([int]$proc.ProcessId) }
        }
    }
    return $ids
}

function Stop-Bot {
    $targets = Get-BotProcessIds
    if ($targets.Count -eq 0) { Write-Host '    (запущенный бот не найден)'; return }
    foreach ($procId in $targets) {
        # /T — валим ВСЁ дерево (process-pool воркеры cpupool), иначе torch/CUDA-воркеры
        # осиротеют и продолжат держать VRAM (накапливалось до ~50% видеопамяти).
        Write-Host "    стоп бота PID $procId (с деревом)"
        taskkill /PID $procId /T /F 2>$null | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 400
        $alive = @($targets | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    } while ($alive.Count -gt 0 -and (Get-Date) -lt $deadline)
    foreach ($procId in $alive) {
        Write-Bad "PID $procId не завершился сам — принудительно (с деревом)"
        taskkill /PID $procId /T /F 2>$null | Out-Null
    }
    if (Test-Path $pidFile) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
}

# ---- добить процесс(ы) сторожа watchdog.py этой папки ----
# Stop-ScheduledTask гасит обёртку wscript, но дочерний python watchdog.py может осиротеть
# и продолжить жить (тогда два сторожа дерутся за бота). Поэтому валим его явно по cmdline.
function Stop-Watchdog {
    $cim = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue
    foreach ($proc in $cim) {
        $cl = $proc.CommandLine
        if (-not $cl) { continue }
        if ($cl -match 'watchdog\.py' -and ($cl -like "*$root*" -or $proc.ExecutablePath -like "*$root*")) {
            # /T — заодно валим дерево (run.py + воркеры, если сторож успел их поднять)
            Write-Host "    стоп сторожа PID $($proc.ProcessId) (с деревом)"
            taskkill /PID $($proc.ProcessId) /T /F 2>$null | Out-Null
        }
    }
}

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 5 -UseBasicParsing
        return ($r.StatusCode -eq 200 -and "$($r.Content)".Trim() -eq 'ok')
    } catch { return $false }
}

function Wait-Health([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health) { return $true }
        Start-Sleep -Seconds 2
    }
    return (Test-Health)
}

function Invoke-Update {
    Write-Step 'Обновляю код и зависимости...'
    if (Test-Path (Join-Path $root '.git')) { & git -C $root pull --ff-only }
    else { Write-Bad '.git не найден — git pull пропущен' }
    & $venvPy -m pip install -r (Join-Path $root 'requirements.txt')
}

function Show-FailTail {
    Write-Bad 'Бот не ответил на /health. Последние строки логов:'
    foreach ($f in @((Join-Path $logDir 'bot.log'), (Join-Path $logDir 'bot_watchdog.out'))) {
        if (Test-Path $f) { Write-Host "    --- $f ---"; Get-Content $f -Tail 15 }
    }
}

# ---- каналы выпуска (stable=master / test=test) — OpenTodo 31485676 ----
function Get-Branch { (& git -C $root rev-parse --abbrev-ref HEAD 2>$null) }
function Get-Channel([string]$b) {
    if ($b -eq 'master') { 'stable' } elseif ($b -eq 'test') { 'test' } else { $b }
}
function Assert-CleanTree {
    # Переключение/слияние веток с незакоммиченными правками в ОТСЛЕЖИВАЕМЫХ файлах может их
    # затереть — отказываемся, чтобы ничего не потерять.
    # --untracked-files=no: неотслеживаемые файлы поводом для отказа НЕ считаем — git checkout
    # их не трогает, они просто остаются на месте. Раньше считали, и случайный новый файл
    # (docs/Перспективные разработки/competitor-feature-gap-2026-07.md, 27.07.2026) отменял
    # переключение канала уже ПОСЛЕ остановки бота — бот лежал, канал не переключался.
    # data/ и .env в .gitignore, поэтому БД/секреты сюда не попадают в любом случае.
    $dirty = & git -C $root status --porcelain --untracked-files=no
    if ($dirty) {
        Write-Bad 'В рабочем дереве есть незакоммиченные изменения — операция с каналом отменена.'
        Write-Bad 'Сначала закоммитьте или уберите их:'
        & git -C $root status --short --untracked-files=no
        throw 'рабочее дерево не чистое'
    }
}
function Test-BranchExists([string]$branch) {
    & git -C $root show-ref --verify --quiet "refs/heads/$branch"
    return ($LASTEXITCODE -eq 0)
}
function Assert-NoUntrackedClash([string[]]$branches) {
    # Неотслеживаемые файлы мы игнорируем (см. Assert-CleanTree), но одно исключение git всё же
    # не прощает: если в целевой ветке есть ОТСЛЕЖИВАЕМЫЙ файл с тем же путём, checkout откажет
    # («would be overwritten by checkout»). Ловим это здесь, до остановки бота.
    # -c core.quotepath=off — чтобы кириллические пути читались, а не как \320\237...
    $untracked = @(& git -C $root -c core.quotepath=off ls-files --others --exclude-standard)
    if (-not $untracked) { return }
    $clash = @()
    foreach ($b in $branches) {
        $inBranch = @(& git -C $root -c core.quotepath=off ls-tree -r --name-only $b)
        $clash += @($untracked | Where-Object { $inBranch -contains $_ })
    }
    $clash = @($clash | Select-Object -Unique)
    if ($clash.Count) {
        Write-Bad "Неотслеживаемые файлы столкнутся с содержимым ветки $($branches -join '/') — операция отменена."
        Write-Bad 'Уберите или закоммитьте их:'
        foreach ($f in $clash) { Write-Host "    $f" }
        throw 'неотслеживаемые файлы конфликтуют с целевой веткой'
    }
}
function Assert-MergeCleanly([string]$into, [string]$from) {
    # git merge-tree сливает В ПАМЯТИ (git >= 2.38) и рабочее дерево не трогает, поэтому
    # конфликт видно ДО остановки бота, а не посреди перезапуска с полурассыпанным деревом.
    # 2>$null, а не 2>&1: в Windows PowerShell 5.1 при $ErrorActionPreference='Stop' поток
    # stderr нативной команды, слитый в 2>&1, сам по себе валит скрипт (NativeCommandError).
    $out  = & git -C $root merge-tree --write-tree $into $from 2>$null
    $code = $LASTEXITCODE
    if ($code -eq 0) { return }
    if ($code -ne 1) {
        Write-Bad "предпроверка слияния недоступна (git merge-tree вернул $code) — пропускаю"
        return
    }
    Write-Bad "Слияние '$from' → '$into' даст конфликты — операция отменена (бот не тронут)."
    Write-Bad 'Разрулите конфликты вручную, затем повторите -Promote.'
    # из вывода merge-tree берём только человекочитаемую часть (в начале идут OID и режимы файлов)
    $msgs = @($out | Where-Object { $_ -match '^(CONFLICT|Auto-merging|Failed|error|warning)' })
    if (-not $msgs) { $msgs = @($out | Select-Object -Last 10) }
    $msgs | ForEach-Object { Write-Host "    $_" }
    throw 'merge конфликт (предпроверка)'
}
function Assert-ChannelPreconditions {
    # ВСЁ, что способно отменить операцию с каналом, проверяется ЗДЕСЬ — до остановки сторожа
    # и бота. Иначе отказ проверки роняет сервис и ничего не переключает (инцидент 27.07.2026).
    # Ничего не меняет: только читает git.
    if (-not ($Promote -or $Channel)) { return }
    Write-Step 'Предполётные проверки git (бот пока работает)...'
    if (-not (Test-Path (Join-Path $root '.git'))) {
        throw "В $root нет git-репозитория — операции с каналами недоступны."
    }
    $targets = if ($Promote) { @('master', 'test') }
               elseif ($Channel -eq 'stable') { @('master') }
               else { @('test') }
    foreach ($b in $targets) {
        if (-not (Test-BranchExists $b)) {
            throw "Ветка '$b' не найдена. Создайте её: git -C `"$root`" branch $b"
        }
    }
    Assert-CleanTree
    Assert-NoUntrackedClash $targets
    if ($Promote) { Assert-MergeCleanly 'master' 'test' }
    Write-Ok 'проверки пройдены — можно останавливать бота'
}
function Switch-Channel([string]$name) {
    # Предусловия (ветка есть, дерево чистое, конфликтов нет) уже проверены
    # Assert-ChannelPreconditions ДО остановки бота — здесь только само переключение.
    $branch = if ($name -eq 'stable') { 'master' } else { 'test' }
    Write-Step "Переключаю канал → $name (ветка '$branch')..."
    & git -C $root checkout $branch
    if ($LASTEXITCODE -ne 0) { throw "git checkout $branch не удался" }
    Write-Ok "теперь на ветке '$branch' (канал $name)"
}
function Invoke-Promote {
    # Предусловия проверены в Assert-ChannelPreconditions (до остановки бота).
    Write-Step 'Одобрение теста: вливаю test → stable (master, merge --no-ff)...'
    & git -C $root checkout master
    if ($LASTEXITCODE -ne 0) { throw 'git checkout master не удался' }
    & git -C $root merge --no-ff test -m 'merge test -> stable (одобрено)'
    if ($LASTEXITCODE -ne 0) {
        Write-Bad 'Слияние с конфликтами — разрулите вручную (git status), затем git commit.'
        throw 'merge конфликт'
    }
    Write-Ok 'test влит в stable; запускаю боевую версию'
}
function Invoke-ChannelOps {
    # Выполняется после остановки бота и до старта — сторож поднимет уже нужную ветку.
    # Всё, что могло отказать, проверено раньше (Assert-ChannelPreconditions), поэтому здесь
    # остаются только сами git-операции: сюда мы доходим уже с остановленным ботом.
    if ($Promote) { Invoke-Promote }
    elseif ($Channel) { Switch-Channel $Channel }
}
function Publish-DistMirror {
    # Зеркало обновлений (VPS): покупатели и стенд тянут код ОТТУДА, поэтому одобренный
    # stable обязан уезжать на зеркало сразу при промоушене — иначе «Обновления» у клиентов
    # продолжат предлагать прошлую версию. Ремоут 'dist' существует только на машине
    # владельца; у любой другой установки его нет, и тогда функция молча не делает ничего.
    # Зовётся ПОСЛЕ старта бота: pre-push гоняет полный pytest, и держать мастерскую
    # выключенной эти минуты незачем — публикация зеркала не влияет на локальный запуск.
    if (-not $Promote) { return }
    & git -C $root remote get-url dist 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return }
    Write-Step 'Публикую одобренный stable на зеркало обновлений (git push dist master)...'
    Write-Host '    pre-push гоняет полный pytest — это несколько минут; бот уже работает.'
    & git -C $root push dist master:master
    if ($LASTEXITCODE -eq 0) { Write-Ok 'зеркало обновлений получило новый stable' }
    else {
        Write-Bad 'push на зеркало не прошёл — клиенты остаются на прежней версии.'
        Write-Bad 'Повторить вручную: git push dist master'
    }
}

# ---- приёмная на VPS (Э1): напоминание выложить relay/ ----
# При TG_SOURCE=relay бот слушает Telegram через приёмную на VPS, а её код (каталог relay/)
# едет туда ОТДЕЛЬНО — архивом через tools/edge/edge.ps1, не git'ом. Обновили ПК, а relay/
# в новом коде другой — без напоминания VPS остался бы на старой версии. Сам скрипт на VPS
# ничего не выкладывает: это отдельное решение владельца (и порядок: сначала ПК, потом VPS —
# приёмная понимает протокол на версию назад).
function Get-Head {
    # HEAD или $null — и никогда не исключение. Копия без .git (перенос, архив) или машина без
    # git в PATH — законные случаи (Invoke-Update их пропускает), а под скриптовым
    # $ErrorActionPreference='Stop' Windows PowerShell 5.1 превращает stderr git'а («not a git
    # repository») в NativeCommandError: -Update падал ещё до pip и перезапуска.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $null }
    $ErrorActionPreference = 'Continue'
    $h = & git -C $root rev-parse HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $h) { return ([string]$h).Trim() }
    return $null
}
function Get-TgSource {
    $v = ''
    if ($env:TG_SOURCE) { $v = $env:TG_SOURCE }
    $envFile = Join-Path $root '.env'
    if (Test-Path $envFile) {
        foreach ($ln in Get-Content $envFile -Encoding UTF8) {
            # .env бот читает с override=True — значение из файла главнее окружения
            if ($ln -match '^\s*TG_SOURCE\s*=\s*(.*)$') { $v = $Matches[1] }
        }
    }
    return $v.Trim().Trim('"').Trim("'").Trim().ToLower()
}
function Show-RelayReminder([string]$before) {
    if (-not ($Promote -or $Update) -or -not $before) { return }
    if ((Get-TgSource) -ne 'relay') { return }
    $after = Get-Head
    if (-not $after -or $after -eq $before) { return }
    & git -C $root diff --quiet $before $after -- relay/ 2>$null
    if ($LASTEXITCODE -eq 1) {
        Write-Step 'Напоминание: код приёмной (relay/) изменился, а бот слушает Telegram через неё (TG_SOURCE=relay).'
        Write-Bad 'Выложите его на VPS, когда бот поднимется:  .\tools\edge\edge.ps1 -Action deploy'
        Write-Bad 'Порядок — сначала ПК, потом VPS: приёмная понимает протокол на версию назад.'
    }
}

# ---- проверка аргументов: до всего остального, ничего ещё не остановлено ----
if ($Promote -and $Channel) { throw 'Укажите либо -Channel, либо -Promote, не оба сразу.' }

if ($Status) {
    $b = Get-Branch
    Write-Step "Текущий канал: $(Get-Channel $b) (ветка '$b')"
    & git -C $root log -1 --oneline
    exit 0
}

# ---- ПРЕДПОЛЁТНЫЕ ПРОВЕРКИ: строго ДО остановки сторожа и бота ----
# Если что-то не так (грязное дерево, нет ветки, конфликт слияния) — падаем здесь, пока бот
# продолжает обслуживать пользователей. Ниже этой строки любой throw = простой сервиса.
Assert-ChannelPreconditions
# HEAD до обновления/промоушена — чтобы после них сравнить relay/ (напоминание про VPS). Только
# при TG_SOURCE=relay: обычной установке напоминание ни к чему, и git ради него не трогаем.
$headBefore = if (($Promote -or $Update) -and (Get-TgSource) -eq 'relay') { Get-Head } else { $null }

# ---- есть ли задача сторожа? ----
$wd = Get-ScheduledTask -TaskName $wdTask -ErrorAction SilentlyContinue

if ($wd) {
    Write-Step "Перезапуск через сторожа (задача '$wdTask')..."
    Write-Host '    останавливаю сторожа...'
    try { Stop-ScheduledTask -TaskName $wdTask -ErrorAction Stop } catch { Write-Bad "Stop-ScheduledTask: $($_.Exception.Message)" }
    Stop-Watchdog            # сначала валим сторожа (чтобы он не поднял бота во время паузы)
    Stop-Bot                 # затем самого бота
    if ($Update) { Invoke-Update }
    Invoke-ChannelOps        # переключение канала / promote (если заданы) — до старта
    Show-RelayReminder $headBefore
    if ($NoStart) { Write-Step 'Готово: бот и сторож остановлены (-NoStart).'; exit 0 }

    # дождаться, пока экземпляр задачи освободится (MultipleInstancesPolicy=IgnoreNew)
    $deadline = (Get-Date).AddSeconds(10)
    while (((Get-ScheduledTask -TaskName $wdTask).State -eq 'Running') -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 400
    }
    Write-Host '    запускаю сторожа заново (он поднимет бота с новым кодом)...'
    Start-ScheduledTask -TaskName $wdTask
    Write-Step 'Жду, пока бот ответит на /health (до ~70 c)...'
    if (Wait-Health 70) {
        $b = Get-BotProcessIds
        Write-Ok ("бот запущен и отвечает /health → ok" + ($(if ($b.Count) { ", PID $($b -join ', ')" } else { '' })))
        Write-Host "    логи: $(Join-Path $logDir 'bot.log')"
        Publish-DistMirror       # только при -Promote и только на машине с ремоутом 'dist'
        exit 0
    } else { Show-FailTail; exit 1 }
}
else {
    Write-Step "Задача сторожа '$wdTask' не найдена — прямой перезапуск run.py."
    Write-Step 'Останавливаю бота...'
    Stop-Bot
    if ($Update) { Invoke-Update }
    Invoke-ChannelOps        # переключение канала / promote (если заданы) — до старта
    Show-RelayReminder $headBefore
    if ($NoStart) { Write-Step 'Готово: бот остановлен (-NoStart).'; exit 0 }

    Write-Step 'Запускаю бота...'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $proc = Start-Process -FilePath $venvPy -ArgumentList '-u', 'run.py' -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Set-Content -Path $pidFile -Value $proc.Id -Encoding ascii
    Write-Step 'Жду, пока бот ответит на /health (до ~40 c)...'
    if (Wait-Health 40) {
        Write-Ok "бот запущен, PID $($proc.Id), отвечает /health → ok"
        Write-Host "    логи: $(Join-Path $logDir 'bot.log')"
        Publish-DistMirror       # только при -Promote и только на машине с ремоутом 'dist'
        exit 0
    } elseif (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
        Write-Bad "процесс PID $($proc.Id) жив, но /health пока не отвечает (возможно, веб-сервер ещё поднимается)."
        Publish-DistMirror       # промоушен уже случился — зеркало не должно отстать
        exit 0
    } else { Show-FailTail; Remove-Item $pidFile -ErrorAction SilentlyContinue; exit 1 }
}
