<#
.SYNOPSIS
    Установка окружения проекта после переноса папки на другой ПК (или с нуля).

.DESCRIPTION
    Папка проекта переносится целиком, но Python-окружения (venv, venvs\*) привязаны к абсолютному пути
    Python и после переноса не работают — их надо пересоздать. Скрипт:
      1. находит Python 3.10 (нужен один раз, ставится отдельно: winget install Python.Python.3.10);
      2. пересоздаёт venv\ по app\requirements.txt;
      3. пересоздаёт venvs\<имя> по scripts\requirements_lock\<имя>.txt (версии зафиксированы, torch — с нужным CUDA-индексом);
      4. проверяет портативные программы в tools\ (LibreOffice, Tesseract, ffmpeg);
      5. заменяет старый путь проекта на новый в .env.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1 -Only venv        # только основное окружение
    powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1 -Force            # пересоздать даже рабочие
#>
[CmdletBinding()]
param(
    [string]$Only = '',        # venv | имя из venvs\ ; пусто = всё
    [switch]$Force,            # пересоздавать окружения, даже если они работают
    [string]$OldRoot = 'D:\1C_NEW_SKKKK'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$lockDir = Join-Path $root 'scripts\requirements_lock'
$torchIdx = @('--extra-index-url', 'https://download.pytorch.org/whl/cu121',
              '--extra-index-url', 'https://download.pytorch.org/whl/cu124',
              '--extra-index-url', 'https://download.pytorch.org/whl/cpu')

function Find-Python310 {
    foreach ($c in @(@('py', '-3.10'), @('python'), @('python3'))) {
        try {
            $v = & $c[0] @($c[1..($c.Count - 1)] + '--version') 2>&1
            if ("$v" -match 'Python 3\.10\.') { return , $c }
        } catch { }
    }
    return $null
}

function Test-Venv($dir) {
    $py = Join-Path $dir 'Scripts\python.exe'
    if (-not (Test-Path $py)) { return $false }
    try { & $py -c 'import sys' 2>$null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

function New-Env($dir, $requirements, [string[]]$pipArgs) {
    if ((Test-Venv $dir) -and -not $Force) { Write-Host "[OK  ] $dir уже работает (-Force чтобы пересоздать)"; return }
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    $py = Find-Python310
    if (-not $py) { throw 'Python 3.10 не найден. Установите: winget install Python.Python.3.10' }
    Write-Host "[....] создаю $dir"
    & $py[0] @($py[1..($py.Count - 1)] + @('-m', 'venv', $dir))
    $vpy = Join-Path $dir 'Scripts\python.exe'
    & $vpy -m pip install --upgrade pip | Out-Null
    & $vpy -m pip install -r $requirements @pipArgs
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] $dir : pip завершился с ошибкой" -ForegroundColor Red; return }
    Write-Host "[OK  ] $dir"
}

if (-not $Only -or $Only -eq 'venv') {
    New-Env (Join-Path $root 'venv') (Join-Path $root 'app\requirements.txt') @()
}
Get-ChildItem (Join-Path $root 'venvs') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    if ($Only -and $Only -ne $_.Name) { return }
    $lock = Join-Path $lockDir "$($_.Name).txt"
    if (Test-Path $lock) { New-Env $_.FullName $lock $torchIdx }
    else { Write-Host "[WARN] нет $lock — venvs\$($_.Name) пропущен" -ForegroundColor Yellow }
}

# --- портативные программы ---
$tools = @{
    'LibreOffice (.ppt → .pptx)' = 'tools\libreoffice\program\soffice.exe'
    'Tesseract (OCR)'           = 'tools\tesseract\tesseract.exe'
    'ffmpeg'                    = 'tools\ffmpeg\bin\ffmpeg.exe'
    'ffprobe'                   = 'tools\ffmpeg\bin\ffprobe.exe'
}
foreach ($k in $tools.Keys) {
    $ok = Test-Path (Join-Path $root $tools[$k])
    Write-Host ("[{0}] {1} : {2}" -f $(if ($ok) { 'OK  ' } else { 'FAIL' }), $k, $tools[$k])
}

# --- .env: старый абсолютный путь → новый ---
$envFile = Join-Path $root '.env'
if ((Test-Path $envFile) -and ($root -ne $OldRoot)) {
    $text = Get-Content $envFile -Raw -Encoding UTF8
    $new = $text.Replace($OldRoot, $root).Replace($OldRoot.Replace('\', '/'), $root.Replace('\', '/'))
    if ($new -ne $text) {
        Copy-Item $envFile "$envFile.bak" -Force
        Set-Content $envFile $new -Encoding UTF8 -NoNewline
        Write-Host "[OK  ] .env: пути $OldRoot → $root (копия .env.bak)"
    }
}

Write-Host "`nВне проекта остаются: Python 3.10, драйвер NVIDIA/CUDA, Docker, Ollama / LM Studio (модели LLM)."
