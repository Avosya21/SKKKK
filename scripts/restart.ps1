# PrimaryAnalitics — безопасный перезапуск: остановка -> проверки зависимостей -> старт -> ожидание /health.
# Завершается с ненулевым кодом и называет причину, если запуск невозможен.
param(
    [ValidateSet("saved", "gemma", "cursor")]
    [string]$Llm = "saved",
    [int]$TimeoutSec = 120,
    [switch]$SkipChecks,
    [switch]$Force   # перезапустить, даже если идёт встреча (запись голоса оборвётся)
)

$ErrorActionPreference = "Stop"
$Root = (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

function Fail([string]$Reason) {
    Write-Host "ОТКАЗ ЗАПУСКА: $Reason" -ForegroundColor Red
    exit 1
}

function Get-EnvValue([string]$Key, [string]$Default = "") {
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return $Default }
    $line = Select-String -Path $envFile -Pattern "^\s*$Key\s*=(.+)$" | Select-Object -First 1
    if ($line) { return $line.Matches.Groups[1].Value.Trim() }
    return $Default
}

function Test-Http([string]$Url, [int]$Sec = 4) {
    try {
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
        return (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $Sec).StatusCode
    } catch { return 0 }
}

# Перезапуск убивает процессы записи: дорожки идущей встречи будут потеряны. Не перезапускаем без -Force.
if (-not $Force) {
    $py = Join-Path $Root "venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    $live = & $py (Join-Path $PSScriptRoot "live_meetings.py") 2>$null
    if ($LASTEXITCODE -eq 3) {
        Fail ("идёт встреча, перезапуск оборвёт запись голоса: " + ($live -join "; ") + ". Дождитесь её завершения или запустите с -Force.")
    }
}

# Процессы записи (voice_cleaner, screen_recorder) переживают перезапуск сервера, и старые копии копились: копии с одним именем участника
# выбивают друг друга из комнаты LiveKit и рвут запись. Когда встреч нет, останавливаем их все, сервер запустит одну свежую пару.
if (-not $Force) {
    $rec = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'livekit[\\/](voice_cleaner|screen_recorder)\.py' }
    if ($rec) {
        Write-Host ("Останавливаю процессы записи: " + $rec.Count + " (встреч нет)")
        $rec | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
    foreach ($name in @("voice_cleaner.pid", "screen_recorder.pid", "screen_recorder.heartbeat")) {
        Remove-Item (Join-Path $Root "data\locks\$name") -Force -ErrorAction SilentlyContinue
    }
}

$provider = switch ($Llm) {
    "gemma"  { "lmstudio" }
    "cursor" { "cursor" }
    default  { Get-EnvValue "LLM_PROVIDER" "lmstudio" }
}
$port = [int](Get-EnvValue "WEB_PORT" "1111")

if (-not $SkipChecks) {
    Write-Host "Проверки: провайдер LLM = $provider, порт = $port"
    if ((Test-Http "http://127.0.0.1:11434/api/tags") -ne 200) {
        Fail "Ollama не отвечает на 11434 (нужна для эмбеддингов bge-m3)."
    }
    if ($provider -eq "lmstudio") {
        # Модель портал загружает сам по профилю и выгружает после простоя (llm_keeper): здесь нужен только сервер
        # LM Studio и согласованный профиль. Загрузка заранее не требуется.
        $py = Join-Path $Root "venv\Scripts\python.exe"
        $raw = & $py (Join-Path $PSScriptRoot "llm_model.py") status 2>$null
        try { $st = ($raw | Select-Object -Last 1) | ConvertFrom-Json } catch { $st = $null }
        if (-not $st -or $null -eq $st.state) {
            Fail "LM Studio не отвечает на 1234. Запустите сервер LM Studio (run_server.ps1 -SkipLoad)."
        }
        if ($st.error) { Fail "Профиль модели: $($st.error)" }
        if ($st.jit) { Write-Host "Внимание: в LM Studio включена автозагрузка (JIT) — выключите: scripts\llm_model.py jit-off" -ForegroundColor Yellow }
    }
}

# Остановка делается внутри start.ps1 (PID воркера + процесс на порту), после неё порт должен освободиться.
$startArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "start.ps1"), "-SkipChecks")
if ($Llm -ne "saved") { $startArgs += @("-Llm", $Llm) }
$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "restart.log"
Remove-Item $log, "$log.err" -ErrorAction SilentlyContinue
$workerPidFile = Join-Path $Root "data\locks\gpu_worker.pid"
$oldWorker = if (Test-Path $workerPidFile) { (Get-Content $workerPidFile -TotalCount 1) } else { "" }
Write-Host "Запуск… лог: $log"
$proc = Start-Process powershell -ArgumentList $startArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $log -RedirectStandardError "$log.err"

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$scheme = if ((Get-EnvValue "SSL_CERTFILE") -or (Get-EnvValue "HTTPS" "") -match "1|true") { "https" } else { "http" }
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    foreach ($s in @($scheme, "https", "http")) {
        if ((Test-Http "${s}://127.0.0.1:$port/health") -eq 200) {
            # Старый сервер мог ещё отвечать, пока start.ps1 его останавливает: ждём нового воркера.
            $newWorker = if (Test-Path $workerPidFile) { (Get-Content $workerPidFile -TotalCount 1) } else { "" }
            if (-not $newWorker -or $newWorker -eq $oldWorker) { continue }
            Write-Host "API поднят: ${s}://127.0.0.1:$port, GPU-воркер PID $newWorker, LLM=$provider" -ForegroundColor Green
            # Git-хуки (обновление индекса, проверка карты проекта) лежат вне репозитория: ставим, если в этой копии их ещё нет.
            $hooksDir = Join-Path $PSScriptRoot '..\.git\hooks'
            $mapHook = Join-Path $hooksDir 'pre-commit'
            if ((Test-Path -LiteralPath $hooksDir) -and -not ((Test-Path -LiteralPath $mapHook) -and (Select-String -LiteralPath $mapHook -Pattern 'PrimaryAnalitics-MAP' -Quiet))) {
                try { & (Join-Path $PSScriptRoot 'install_hooks.ps1') | Out-Null; Write-Host 'Git-хуки проекта установлены (карта проекта проверяется при коммите).' -ForegroundColor Green }
                catch { Write-Host "Git-хуки не установлены: $($_.Exception.Message)" -ForegroundColor Yellow }
            }
            # Индекс проекта: подтянуть изменённые файлы в фоне (Ollama на CPU, GPU не трогает; сбой не важен для запуска).
            $idxPy = Join-Path $PSScriptRoot '..\venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $idxPy) {
                Start-Process -FilePath $idxPy -ArgumentList @((Join-Path $PSScriptRoot 'index_project_map.py'), '--changed', '--quiet') -WindowStyle Hidden | Out-Null
            }
            exit 0
        }
    }
    if ($proc.HasExited) {
        $tail = (Get-Content "$log.err" -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
        Fail "процесс запуска завершился с кодом $($proc.ExitCode).`n$tail"
    }
}
$tail = (Get-Content "$log.err" -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
Fail "API не ответил на /health за $TimeoutSec с.`n$tail"
