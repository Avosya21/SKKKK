<#
.SYNOPSIS
    Установка окружения после переноса папки / с нуля: Python из tools\Python310 или живой py/python 3.10; venv/venvs; пути в .env.

.DESCRIPTION
    Папка проекта переносится целиком, но Python-окружения (venv, venvs\*) привязаны к абсолютному пути
    Python и после переноса не работают — их надо пересоздать. Скрипт:
      1. находит Python 3.10: сначала tools\Python310\python.exe, иначе живой py/python (битый py-лаунчер после переноса пропускается);
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

function Test-Python310Command([string[]]$cmd) {
    # py -3.10 после переноса папки может ссылаться на мёртвый путь («Unable to create process»).
    try {
        if ($cmd.Count -gt 1) {
            $v = & $cmd[0] @($cmd[1..($cmd.Count - 1)] + '--version') 2>&1
        } else {
            $v = & $cmd[0] '--version' 2>&1
        }
        $text = "$v"
        if ($text -match 'Unable to create process|cannot find|не удается найти') { return $false }
        return ($text -match 'Python 3\.10\.')
    } catch { return $false }
}

function Find-Python310 {
    # Сначала портативный интерпретатор в проекте — не зависит от сломанного py-лаунчера.
    $bundled = Join-Path $root 'tools\Python310\python.exe'
    if ((Test-Path -LiteralPath $bundled) -and (Test-Python310Command @($bundled))) {
        Write-Host "[OK  ] Python 3.10: $bundled"
        return , @($bundled)
    }
    foreach ($c in @(@('py', '-3.10'), @('python'), @('python3'))) {
        if (Test-Python310Command $c) {
            Write-Host ("[OK  ] Python 3.10: {0}" -f ($c -join ' '))
            return , $c
        }
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
    if (-not $py) { throw 'Python 3.10 не найден. Установите: winget install Python.Python.3.10 (или положите tools\Python310)' }
    Write-Host "[....] создаю $dir"
    $venvArgs = @('-m', 'venv', $dir)
    if ($py.Count -gt 1) {
        & $py[0] @($py[1..($py.Count - 1)] + $venvArgs)
    } else {
        & $py[0] @($venvArgs)
    }
    $vpy = Join-Path $dir 'Scripts\python.exe'
    & $vpy -m pip install --upgrade pip | Out-Null
    & $vpy -m pip install -r $requirements @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $dir : pip завершился с ошибкой" -ForegroundColor Red
        Write-Host "       При обрыве на cursor-sdk (большой wheel): повторите pip или поставьте пакет отдельно — см. INSTALL.md" -ForegroundColor Yellow
        return
    }
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
