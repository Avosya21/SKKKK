#requires -Version 5.1
<#
.SYNOPSIS
    Запуск LM Studio и загрузка Swift-Qwen 27B на GPU 24 GB с пресетом
    swift-qwen-24gb (flash-attn, KV q8_0, MTP, один слот, temp 1.0).

.DESCRIPTION
    В установленной сборке LM Studio CLI не умеет ни `lms server start
    --limit-slots`, ни `lms load --preset` (проверено по `lms --help` и по
    строкам lms.exe). Поэтому:

      * один слот генерации делается через `--parallel 1`
        (это то же самое, что llm.load.numParallelSessions = 1);
      * поля пресета переносятся в дефолтный конфиг конкретной модели
        (%USERPROFILE%\.lmstudio\.internal\user-concrete-model-default-config\),
        который сервер применяет при загрузке. Так применяются те поля, которые
        CLi-флагами не выставляются: flashAttention, kv q8_0, draftMtp, sampling.

    Пресет-файл `%USERPROFILE%\.lmstudio\config-presets\swift-qwen-24gb.preset.json`
    остаётся источником истины: GUI показывает его как "Swift-Qwen-24GB-Optimized",
    а скрипт раскладывает его поля в конфиг модели.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_server.ps1

.EXAMPLE
    # Если загрузка падает на MTP (в GGUF нет поддержки draftMtp или рантайм старый)
    powershell -ExecutionPolicy Bypass -File .\run_server.ps1 -NoMtp
#>
[CmdletBinding()]
param(
    [string]$ModelKey,
    [int]$Port = 1234,
    [int]$ContextLength = 32000,
    [string]$PresetPath = (Join-Path $env:USERPROFILE '.lmstudio\config-presets\swift-qwen-24gb.preset.json'),
    [switch]$NoMtp,
    [switch]$SkipModelDefault,
    [switch]$NoStart,
    [switch]$SkipLoad,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$LmStudioRoot = Join-Path $env:USERPROFILE '.lmstudio'

function Get-LmsPath {
    $cmd = Get-Command lms -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = Join-Path $LmStudioRoot 'bin\lms.exe'
    if (Test-Path -LiteralPath $fallback) { return $fallback }
    throw "lms не найден. LM Studio -> Developer -> Install CLI, либо положите lms.exe в $LmStudioRoot\bin."
}

$script:LmsPath = Get-LmsPath

function Invoke-Lms {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $script:LmsPath @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) {
        throw ("lms " + ($Arguments -join ' ') + " -> код $code`n" + (($out | ForEach-Object { "$_" }) -join "`n"))
    }
    return @($out)
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

if (-not (Test-Path -LiteralPath $PresetPath)) {
    throw "Пресет не найден: $PresetPath"
}
$preset = Get-Content -LiteralPath $PresetPath -Raw -Encoding UTF8 | ConvertFrom-Json
$loadFields = @($preset.load.fields)
$opFields = @($preset.operation.fields)
Write-Host "Пресет: $PresetPath"
Write-Host ("  load-полей: {0}; operation-полей: {1}" -f $loadFields.Count, $opFields.Count)

function Find-SwiftModel {
    $raw = Invoke-Lms -Arguments @('ls', '--json')
    $models = (($raw | ForEach-Object { "$_" }) -join "`n") | ConvertFrom-Json
    return $models | Where-Object {
        ($_.modelKey -match 'swift[-_. ]?qwen') -or
        ($_.path -match 'Swift-Qwen') -or
        ($_.displayName -match 'swift.*qwen')
    } | Select-Object -First 1
}

$swift = Find-SwiftModel
if ([string]::IsNullOrWhiteSpace($ModelKey)) {
    if (-not $swift) {
        $raw = Invoke-Lms -Arguments @('ls', '--json')
        $models = (($raw | ForEach-Object { "$_" }) -join "`n") | ConvertFrom-Json
        $keys = ($models | ForEach-Object { $_.modelKey }) -join ', '
        throw "Модель Swift-Qwen не найдена среди загруженных. Доступно: $keys. Укажите -ModelKey вручную."
    }
    if ($swift.selectedVariant) { $ModelKey = [string]$swift.selectedVariant } else { $ModelKey = [string]$swift.modelKey }
    Write-Host "Модель определена автоматически: $ModelKey"
} else {
    Write-Host "Модель задана вручную: $ModelKey"
}

function Write-ModelDefaultConfig {
    param([string]$IndexedIdentifier, [bool]$IncludeMtp)
    # LM Studio хранит дефолтный конфиг по indexedModelIdentifier (путь к GGUF),
    # напр. cstr\qwen3-asr-1.7b-GGUF\qwen3-asr-1.7b-q5_k.gguf.json — а не по modelKey.
    if ([string]::IsNullOrWhiteSpace($IndexedIdentifier)) {
        throw "Не удалось определить indexedModelIdentifier (lms ls --json) — конфиг модели не записан."
    }
    $effectiveLoad = $loadFields
    if (-not $IncludeMtp) {
        $effectiveLoad = @($loadFields | Where-Object { $_.key -notmatch 'speculativeDecoding' })
    }
    $config = [ordered]@{
        preset    = ''
        operation = @{ fields = $opFields }
        load      = @{ fields = $effectiveLoad }
    }
    $rel = ($IndexedIdentifier -replace '/', '\') + '.json'
    $configPath = Join-Path $LmStudioRoot (Join-Path '.internal\user-concrete-model-default-config' $rel)
    Write-Utf8NoBom -Path $configPath -Text ($config | ConvertTo-Json -Depth 12)
    Write-Host "Конфиг модели записан: $configPath (MTP: $IncludeMtp)"
    return $configPath
}

if ($DryRun) { $SkipModelDefault = $true }

$indexedId = if ($swift) { [string]$swift.indexedModelIdentifier } else { '' }
if (-not $SkipModelDefault) {
    $null = Write-ModelDefaultConfig -IndexedIdentifier $indexedId -IncludeMtp (-not $NoMtp)
}

$loadArgs = @('load', $ModelKey, '--gpu', 'max', '--context-length', "$ContextLength", '--parallel', '1', '-y')
if ($DryRun) {
    Write-Host "DryRun: сервер и загрузка не выполняются."
    Write-Host ("  lms server start --port {0}" -f $Port)
    Write-Host ("  lms " + ($loadArgs -join ' '))
    return
}

if (-not $NoStart) {
    $status = ''
    try { $status = (Invoke-Lms -Arguments @('server', 'status')) -join ' ' } catch { $status = '' }
    if ($status -match "running on port\s+$Port") {
        Write-Host "Сервер уже слушает :$Port"
    } else {
        Write-Host "Запуск сервера на :$Port"
        Invoke-Lms -Arguments @('server', 'start', '--port', "$Port") | Out-Null
    }
}

if ($SkipLoad) {
    Write-Host "SkipLoad: загрузка модели пропущена."
    return
}

$loaded = $false
$attempt = 0
while (-not $loaded -and $attempt -lt 2) {
    $attempt++
    Write-Host ("Загрузка (попытка $attempt): lms " + ($loadArgs -join ' '))
    try {
        Invoke-Lms -Arguments $loadArgs | Out-Null
        $loaded = $true
    } catch {
        $msg = "$_"
        if ($attempt -eq 1 -and -not $NoMtp -and $msg -match 'MTP|draftMtp|speculative') {
            Write-Warning "Загрузка упала на MTP, повторяю без MTP."
            if (-not $SkipModelDefault) {
                $null = Write-ModelDefaultConfig -IndexedIdentifier $indexedId -IncludeMtp $false
            }
            $NoMtp = $true
        } else {
            throw
        }
    }
}

Write-Host "Модель загружена."
Invoke-Lms -Arguments @('ps') | ForEach-Object { Write-Host $_ }
