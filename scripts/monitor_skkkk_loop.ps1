$ErrorActionPreference = "Continue"
$root = (Split-Path -Parent $PSScriptRoot)
$py = Join-Path $root "venv\Scripts\python.exe"
$log = Join-Path $root "data\logs\monitor_loop.log"

function Write-Mon($msg) {
  $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
  Add-Content -Path $log -Value $line -Encoding UTF8
  Write-Output $line
}

function Test-Health {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:777/health" -UseBasicParsing -TimeoutSec 8
    return @{ ok = ($r.StatusCode -eq 200); body = $r.Content }
  } catch {
    return @{ ok = $false; body = $_.Exception.Message }
  }
}

function Ensure-Running {
  $h = Test-Health
  if ($h.ok) {
    Write-Mon "HEALTH_OK $($h.body)"
    return
  }
  Write-Mon "HEALTH_FAIL $($h.body) — restarting"
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'app\.app\.main'
  } | ForEach-Object {
    Write-Mon "kill pid=$($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
  $lock = Join-Path $root "data\locks\gpu_worker.lock"
  if (Test-Path $lock) { Remove-Item $lock -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
  Start-Process -FilePath $py -ArgumentList "-m","app.app.main","all","--use-saved-llm","--llm-provider","cursor" -WorkingDirectory $root -WindowStyle Hidden
  Start-Sleep -Seconds 6
  $h2 = Test-Health
  if ($h2.ok) { Write-Mon "RESTART_OK $($h2.body)" } else { Write-Mon "RESTART_FAIL $($h2.body)" }
}

while ($true) {
  Start-Sleep -Seconds 300
  Write-Output 'AGENT_LOOP_TICK_skkkk {"prompt":"Check SKKKK server health on :777; if down or unhealthy restart all mode; inspect recent errors in server terminal and job failures; fix and report briefly."}'
  Ensure-Running
}
