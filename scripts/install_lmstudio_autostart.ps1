#requires -Version 5.1
<#
.SYNOPSIS
    Автозапуск сервера LM Studio (:1234) при входе в Windows для PrimaryAnalitics.

.DESCRIPTION
    Регистрирует задачу планировщика PrimaryAnalitics-LMStudio, которая вызывает
    run_server.ps1 -SkipLoad (только сервер; модель грузит портал по запросу).
    Сервер LM Studio сам main/preflight не поднимает — без него при LLM_PROVIDER=lmstudio
    старт портала падает на проверке :1234.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install_lmstudio_autostart.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\install_lmstudio_autostart.ps1 -Unregister
    powershell -ExecutionPolicy Bypass -File .\scripts\install_lmstudio_autostart.ps1 -LoadModel
#>
[CmdletBinding()]
param(
    [switch]$Unregister,
    [switch]$LoadModel,   # без -SkipLoad: сразу грузить модель по профилю (VRAM с входа)
    [switch]$RunNow       # сразу выполнить задачу после регистрации
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = 'PrimaryAnalitics-LMStudio'
$RunServer = Join-Path $Root 'run_server.ps1'

if (-not (Test-Path -LiteralPath $RunServer)) {
    throw "Не найден $RunServer"
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Задача $TaskName снята (если была)."
    return
}

$skip = if ($LoadModel) { '' } else { ' -SkipLoad' }
$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunServer`"$skip"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$desc = 'Автозапуск сервера LM Studio (:1234) для PrimaryAnalitics.'
if (-not $LoadModel) { $desc += ' Модель грузит портал (-SkipLoad).' }

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description $desc `
    -Force | Out-Null

Write-Host "Задача $TaskName зарегистрирована (при входе: $env:USERNAME)."
Write-Host "Снять: powershell -ExecutionPolicy Bypass -File scripts\install_lmstudio_autostart.ps1 -Unregister"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Задача запущена сейчас."
}
