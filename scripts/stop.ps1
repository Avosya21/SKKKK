# SKKKK — остановить API и GPU-воркер
$ErrorActionPreference = "Stop"
$Root = "C:\1C_NEW_SKKKK"
$envFile = Join-Path $Root ".env"
$port = 1111
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*WEB_PORT\s*=(.+)$' | Select-Object -First 1
    if ($line) { $port = [int]$line.Matches.Groups[1].Value.Trim() }
}
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
Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "Останавливаю PID $($_.OwningProcess) на порту $port"
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 1
foreach ($name in @("gpu_worker.lock", "gpu_worker.pid")) {
    $path = Join-Path $lockDir $name
    if (Test-Path $path) { Remove-Item $path -Force -ErrorAction SilentlyContinue }
}
