<#
  SKKKK stage 0 environment check.
  Run:  powershell -ExecutionPolicy Bypass -File C:\1C_NEW_SKKKK\scripts\check_env.ps1
  Writes a report to C:\1C_NEW_SKKKK\data\logs\env-check.txt and exits 1 if anything required is missing.
#>

$ErrorActionPreference = 'Continue'
$root = (Split-Path -Parent $PSScriptRoot)
$logDir = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$report = Join-Path $logDir 'env-check.txt'
$lines = @("SKKKK env check  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')", "")
$failed = @()

function Add-Check {
    param($Name, $Ok, $Detail, [switch]$Optional)
    $mark = if ($Ok) { 'OK  ' } elseif ($Optional) { 'WARN' } else { 'FAIL' }
    $script:lines += "[$mark] $Name : $Detail"
    if (-not $Ok -and -not $Optional) { $script:failed += $Name }
    Write-Host "[$mark] $Name : $Detail"
}

# --- GPU ---
try {
    $gpu = (nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader) 2>$null
    $vramMb = [int](nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits)
    Add-Check 'GPU' ($vramMb -ge 20000) "$gpu"
    $used = [int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
    Add-Check 'GPU free at rest' ($used -lt 4000) "used ${used} MB of ${vramMb} MB"
} catch { Add-Check 'GPU' $false 'nvidia-smi not found' }

# --- Python ---
try {
    $pv = (python --version) 2>&1
    Add-Check 'Python' ($pv -match '3\.(11|10)\.') "$pv (need 3.11 or 3.10)"
} catch { Add-Check 'Python' $false 'python not on PATH' }

# --- Git ---
try { Add-Check 'Git' $true ((git --version) 2>&1) } catch { Add-Check 'Git' $false 'not found' }

# --- FFmpeg ---
try {
    $ff = (ffmpeg -version 2>&1 | Select-Object -First 1)
    Add-Check 'FFmpeg' ($ff -match 'ffmpeg version') "$ff"
} catch { Add-Check 'FFmpeg' $false 'not on PATH' }

# --- Tesseract + russian ---
try {
    $ts = (tesseract --version 2>&1 | Select-Object -First 1)
    $langs = (tesseract --list-langs 2>&1) -join ' '
    Add-Check 'Tesseract' ($ts -match 'tesseract') "$ts"
    Add-Check 'Tesseract rus' ($langs -match '\brus\b') "langs: $langs"
} catch { Add-Check 'Tesseract' $false 'not on PATH' }

# --- LM Studio ---
$lmModel = 'google/gemma-4-31b-qat'
try {
    $models = Invoke-RestMethod -Uri 'http://127.0.0.1:1234/v1/models' -TimeoutSec 10
    $ids = @($models.data | ForEach-Object { $_.id })
    Add-Check 'LM Studio API' $true ($ids -join ', ')
    Add-Check 'LM Studio model' ([bool]($ids -match $lmModel)) "expected $lmModel"
} catch { Add-Check 'LM Studio API' $false "unreachable on 127.0.0.1:1234 - $($_.Exception.Message)" }

# --- LM Studio chat smoke ---
try {
    $body = @{ model = $lmModel; messages = @(@{ role = 'user'; content = 'Reply with one word: ok' }); max_tokens = 16 } | ConvertTo-Json -Depth 5
    $chat = Invoke-RestMethod -Uri 'http://127.0.0.1:1234/v1/chat/completions' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 180
    Add-Check 'LM Studio chat' ([bool]$chat.choices[0].message.content) $chat.choices[0].message.content
} catch { Add-Check 'LM Studio chat' $false $_.Exception.Message }

# --- lms CLI (needed to unload/load around ASR) ---
try { Add-Check 'lms CLI' $true ((lms version) 2>&1 | Select-Object -First 1) }
catch { Add-Check 'lms CLI' $false 'lms not on PATH - install from LM Studio > Developer' }

# --- Ollama ---
try {
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 10
    $names = @($tags.models | ForEach-Object { $_.name })
    Add-Check 'Ollama API' $true ($names -join ', ')
    Add-Check 'Ollama bge-m3' ([bool]($names -match 'bge-m3')) 'expected bge-m3'
} catch { Add-Check 'Ollama API' $false "unreachable on 127.0.0.1:11434 - $($_.Exception.Message)" }

# --- Ollama embedding dimension, must be 1024 and must not take VRAM ---
try {
    $vramBefore = [int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
    $eb = @{ model = 'bge-m3'; prompt = 'vector check'; options = @{ num_gpu = 0 } } | ConvertTo-Json -Depth 5
    $emb = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/embeddings' -Method Post -ContentType 'application/json' -Body $eb -TimeoutSec 120
    $dim = $emb.embedding.Count
    Add-Check 'bge-m3 dim' ($dim -eq 1024) "dim=$dim (expected 1024)"
    Start-Sleep -Seconds 2
    $vramAfter = [int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
    Add-Check 'bge-m3 stays on CPU' (($vramAfter - $vramBefore) -lt 500) "delta ${($vramAfter - $vramBefore)} MB"
} catch { Add-Check 'bge-m3 dim' $false $_.Exception.Message }

# --- Disk space ---
try {
    $free = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
    Add-Check 'Free disk C:' ($free -ge 100) "$free GB free (need 100+)"
} catch { Add-Check 'Free disk C:' $false 'cannot read' }

# --- Project tree ---
foreach ($p in @('docs', 'app', 'data', '.cursor\rules', '.cursor\skills')) {
    $full = Join-Path $root $p
    Add-Check "Path $p" (Test-Path $full) $full
}

$lines += ""
if ($failed.Count -eq 0) {
    $lines += "RESULT: PASS"
    $lines | Set-Content -Path $report -Encoding UTF8
    Write-Host "`nPASS. Report: $report" -ForegroundColor Green
    exit 0
} else {
    $lines += "RESULT: FAIL -> $($failed -join ', ')"
    $lines | Set-Content -Path $report -Encoding UTF8
    Write-Host "`nFAIL: $($failed -join ', '). Report: $report" -ForegroundColor Red
    exit 1
}
