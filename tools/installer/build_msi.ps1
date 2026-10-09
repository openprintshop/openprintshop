<#
.SYNOPSIS
    Сборка OpenPrintShop.msi из OpenPrintShop.wxs.

.DESCRIPTION
    Нужен WiX CLI v5 (команда `wix`). Поставить один раз:
        winget install Microsoft.DotNet.SDK.8     # .NET SDK, если нет
        dotnet tool install --global wix --version 5.0.2
    Проверка: wix --version  (ожидается 5.0.2).
    ВЕРСИЯ ЗАКРЕПЛЕНА НАРОЧНО: начиная с WiX v6 сборка требует принять EULA «Open Source
    Maintenance Fee» (платная лицензия для коммерческого использования, error WIX7015).
    v5.0.2 — последняя без неё; схема .wxs (v4) ей понятна. Собрано 22.08.2026 на машине
    разработчика, без предупреждений.

    Выход: dist\OpenPrintShop.msi рядом с этим скриптом.
#>
[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path $PSScriptRoot 'dist'),
    # Откуда установленный бот будет брать код и обновления. Пусто = умолчание install.ps1
    # (публичный github.com/openprintshop/openprintshop). Свой форк — его адрес. Адрес с
    # логином и паролем (приватное зеркало) работает, но готовый MSI его СОДЕРЖИТ —
    # такой пакет нельзя публиковать открыто (см. предупреждение в конце сборки).
    [string]$RepoUrl = '',
    # Ветка, с которой пакет будет ставить код: main — стабильная; испытуемая ветка — для
    # прогона на VM-стенде, чтобы проверять правки до публикации.
    [string]$Branch = 'main'
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    Write-Host 'WiX CLI не найден. Установка (однократно):' -ForegroundColor Yellow
    Write-Host '  winget install Microsoft.DotNet.SDK.8   # если нет .NET SDK'
    Write-Host '  dotnet tool install --global wix --version 5.0.2   # v6+ требует платную OSMF-лицензию'
    Write-Host 'затем откройте новую консоль и повторите .\build_msi.ps1'
    exit 1
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Push-Location $PSScriptRoot
try {
    $msi = Join-Path $OutDir 'OpenPrintShop.msi'
    # Мастеру нужны UI-диалоги, а эстафете — WixQuietExec из Util. Ставятся один раз:
    #   wix extension add -g WixToolset.UI.wixext/5.0.2
    #   wix extension add -g WixToolset.Util.wixext/5.0.2
    # -culture ru-RU обязателен: без него Language пакета остаётся русским, а КНОПКИ
    # мастера (Next/Back/Cancel) и служебные надписи приходят из английской локализации
    # набора WixUI — окно выходит наполовину английским (поймано на стенде 28.08).
    wix build OpenPrintShop.wxs -arch x64 -d "RepoUrl=$RepoUrl" -d "Branch=$Branch" -culture ru-RU `
        -ext WixToolset.UI.wixext -ext WixToolset.Util.wixext -out $msi
    if ($LASTEXITCODE -ne 0) { throw "wix build вернул $LASTEXITCODE" }
    Write-Host "Готово: $msi" -ForegroundColor Green
    Write-Host "Ветка кода в пакете: $Branch" -ForegroundColor Green
    if ($Branch -ne 'main') {
        Write-Host 'ВНИМАНИЕ: пакет поставит НЕ стабильную ветку, а испытуемую.' -ForegroundColor Yellow
    }
    if ($RepoUrl) {
        $shown = $RepoUrl -replace '//[^/@]+@', '//***@'
        Write-Host "Источник кода в пакете: $shown" -ForegroundColor Green
        if ($RepoUrl -match '//[^/@]+@') {
            Write-Host 'ВНИМАНИЕ: пакет содержит логин и пароль из адреса — это секрет.' -ForegroundColor Yellow
            Write-Host 'Не публикуйте такой MSI открыто; смена пароля требует пересборки.' -ForegroundColor Yellow
        }
    } else {
        Write-Host 'Источник кода: умолчание install.ps1 (github.com/openprintshop/openprintshop).' -ForegroundColor Green
    }
    Write-Host 'Перед публикацией прогоните MSI на чистой Windows (VM).'
} finally {
    Pop-Location
}
