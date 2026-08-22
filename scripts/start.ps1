# SKKKK — запуск API + GPU-воркер с выбором LLM
param(
    [ValidateSet("cursor", "gemma", "")]
    [string]$Llm = "",
    [switch]$SkipChecks,
    [switch]$NoStop
)

$ErrorActionPreference = "Stop"
$Root = "C:\1C_NEW_SKKKK"
Set-Location $Root

function Get-EnvValue {
    param([string]$Key, [string]$Default = "")
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return $Default }
    $line = Select-String -Path $envFile -Pattern "^\s*$Key\s*=(.+)$" | Select-Object -First 1
    if ($line) { return $line.Matches.Groups[1].Value.Trim() }
    return $Default
}

function Stop-Skkkk {
    param([int]$Port)
    $lockDir = Join-Path $Root "data\locks"
    $pidFile = Join-Path $lockDir "gpu_worker.pid"
    if (Test-Path $pidFile) {
        $workerPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($workerPid -match '^\d+$') {
            Write-Host "Останавливаю GPU-воркер PID $workerPid"
            Stop-Process -Id ([int]$workerPid) -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }
    }
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            Write-Host "Останавливаю процесс на порту $Port (PID $($_.OwningProcess))"
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    Start-Sleep -Seconds 1
    foreach ($name in @("gpu_worker.lock", "gpu_worker.pid")) {
        $path = Join-Path $lockDir $name
        if (Test-Path $path) {
            Remove-Item $path -Force -ErrorAction SilentlyContinue
        }
    }
}

$port = [int](Get-EnvValue -Key "WEB_PORT" -Default "1111")
if (-not $NoStop) { Stop-Skkkk -Port $port }

$py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "Не найден venv: $py`nСоздайте окружение в $Root\venv"
}

$args = @("-m", "app.app.main", "all")
if ($SkipChecks) { $args += "--skip-checks" }
if ($Llm -eq "cursor") {
    $args += @("--llm", "cursor")
    Write-Host "LLM: Cursor API"
}
elseif ($Llm -eq "gemma") {
    $args += @("--llm", "gemma")
    Write-Host "LLM: Gemma (LM Studio)"
}
else {
    $args += "--use-saved-llm"
    Write-Host "LLM: из .env ($(Get-EnvValue -Key 'LLM_PROVIDER' -Default 'lmstudio'))"
}

Write-Host "Порт: $port"
& $py @args
