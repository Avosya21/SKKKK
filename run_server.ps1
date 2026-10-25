#requires -Version 5.1
<#
.SYNOPSIS
    Запуск сервера LM Studio и загрузка локальной модели строго по профилю PrimaryAnalitics.

.DESCRIPTION
    Единственный источник настроек модели — tools\lmstudio\qwen-profile.json
    (окно, два потока, flash-attn, KV q8_0, MTP, выборка). Загрузку делает тот же
    код, что и портал (app\app\services\llm_keeper.py, через scripts\llm_model.py):
    записывает профиль в конфиг модели LM Studio, грузит её флагами
    `lms load --context-length --parallel --ttl` и сверяет результат по `lms ps`.
    Модель с другими настройками не используется.

    Портал сам загружает модель, когда она нужна, и выгружает после простоя
    (LMSTUDIO_IDLE_UNLOAD_SEC). Этот скрипт нужен, чтобы поднять сервер LM Studio
    и, при желании, загрузить модель заранее. План: docs\plans\LLM-LANES-PLAN.md.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_server.ps1

.EXAMPLE
    # только сервер, модель загрузит портал по первому запросу
    powershell -ExecutionPolicy Bypass -File .\run_server.ps1 -SkipLoad
#>
[CmdletBinding()]
param(
    [int]$Port = 1234,
    [switch]$NoStart,
    [switch]$SkipLoad
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$Root = $PSScriptRoot
$Python = Join-Path $Root 'venv\Scripts\python.exe'
$LmStudioRoot = Join-Path $env:USERPROFILE '.lmstudio'

function Get-LmsPath {
    $cmd = Get-Command lms -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = Join-Path $LmStudioRoot 'bin\lms.exe'
    if (Test-Path -LiteralPath $fallback) { return $fallback }
    throw "lms не найден. LM Studio -> Developer -> Install CLI, либо положите lms.exe в $LmStudioRoot\bin."
}
$Lms = Get-LmsPath

# Автозагрузка LM Studio поднимала модель не по профилю (один поток, выгрузка через час) — выключаем.
& $Python (Join-Path $Root 'scripts\llm_model.py') jit-off

if (-not $NoStart) {
    $status = (& $Lms server status 2>&1) -join ' '
    if ($status -match "running on port\s+$Port") {
        Write-Host "Сервер уже слушает :$Port"
    } else {
        Write-Host "Запуск сервера на :$Port"
        & $Lms server start --port $Port | Out-Null
    }
}

if ($SkipLoad) {
    Write-Host "SkipLoad: модель загрузит портал по первому запросу."
    return
}

& $Python (Join-Path $Root 'scripts\llm_model.py') load
if ($LASTEXITCODE -ne 0) { throw "Модель не загрузилась по профилю (см. вывод выше)." }
& $Lms ps
