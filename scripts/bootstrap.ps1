<#
.SYNOPSIS
    Полная установка проекта на новом ПК одной командой (запускается через INSTALL.bat в корне).

.DESCRIPTION
    Определяет, что уже есть, и доустанавливает недостающее — в таком порядке:
      1. Проверка ПК: Windows, свободное место, GPU/драйвер NVIDIA (драйвер скрипт не ставит — только сообщает).
      2. Системные программы через winget: Python 3.10, Git, Docker Desktop, Ollama, LM Studio.
      3. Портативные программы проекта в tools\: LibreOffice, Tesseract, ffmpeg. Если папки нет —
         копируем из системной установки, а если и её нет — ставим через winget (проект найдёт их и там).
      4. Python-окружения (scripts\setup_env.ps1): venv\ и venvs\* по зафиксированным версиям.
      5. Файл .env (из .env.example, пути под текущую папку).
      6. Модель эмбеддингов Ollama (bge-m3).
      7. Docker-образы ASR (только с ключом -Asr, долго).
      8. Итоговый отчёт и список шагов, которые делаются вручную.

.EXAMPLE
    INSTALL.bat                 # интерактивно, спрашивает перед каждой установкой
    INSTALL.bat -Yes            # без вопросов
    INSTALL.bat -Yes -Asr       # + собрать Docker-образы ASR
    INSTALL.bat -SkipSystem     # не трогать winget, только окружения/tools/.env
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$SkipSystem,
    [switch]$Asr,
    [string]$OldRoot = 'D:\1C_NEW_SKKKK'
)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$report = New-Object System.Collections.Generic.List[string]
$manual = New-Object System.Collections.Generic.List[string]

function Say($mark, $text) {
    $color = @{ 'OK' = 'Green'; 'FAIL' = 'Red'; 'WARN' = 'Yellow'; '..' = 'Cyan' }[$mark]
    Write-Host ("[{0,-4}] {1}" -f $mark, $text) -ForegroundColor $color
    $report.Add("[$mark] $text")
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Confirm-Step($question) {
    if ($Yes) { return $true }
    return ((Read-Host "$question [Y/n]") -notmatch '^(n|N|н|Н)')
}
function Winget-Install($id, $title) {
    if (-not (Have winget)) {
        Say 'FAIL' "${title}: winget не найден (обновите «Установщик приложений» в Microsoft Store) — поставьте вручную"
        $manual.Add("Установить $title (winget id: $id)"); return $false
    }
    if (-not (Confirm-Step "Установить $title через winget?")) {
        Say 'WARN' "${title}: пропущено по вашему выбору"; $manual.Add("Установить $title (winget id: $id)"); return $false
    }
    Say '..' "${title}: устанавливаю…"
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $code = $LASTEXITCODE
    Refresh-Path
    if ($code -ne 0) { Say 'WARN' "${title}: winget вернул код $code (возможно, уже стоит или нужен перезапуск)" }
    return $true
}
function Find-Python310 {
    $bundled = Join-Path $root 'tools\Python310\python.exe'
    if (Test-Path -LiteralPath $bundled) {
        try {
            $v = & $bundled --version 2>&1
            if ("$v" -match 'Python 3\.10\.') { return $true }
        } catch { }
    }
    foreach ($c in @(@('py', '-3.10'), @('python'))) {
        try {
            $v = & $c[0] @($c[1..($c.Count - 1)] + '--version') 2>&1
            if ("$v" -match 'Unable to create process|cannot find|не удается найти') { continue }
            if ("$v" -match 'Python 3\.10\.') { return $true }
        } catch { }
    }
    return $false
}

Write-Host "`n=== Установка проекта: $root ===`n" -ForegroundColor Cyan

# ---------- 1. Проверка ПК ----------
Say 'OK' ('Windows: ' + [Environment]::OSVersion.VersionString)
$drive = (Get-Item $root).PSDrive
$freeGb = [math]::Round($drive.Free / 1GB, 1)
Say $(if ($freeGb -ge 60) { 'OK' } else { 'WARN' }) "Свободно на диске $($drive.Name): $freeGb ГБ (рекомендуется 60+ для окружений и моделей)"
if (Have nvidia-smi) {
    $gpu = (nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader) 2>$null
    Say 'OK' "GPU NVIDIA: $gpu"
} else {
    Say 'WARN' 'nvidia-smi не найден: драйвер NVIDIA не установлен или нет GPU. Без GPU не будут работать локальные ASR/LLM.'
    $manual.Add('Установить драйвер NVIDIA (https://www.nvidia.com/drivers), перезагрузить ПК')
}

# ---------- 2. Системные программы ----------
if (-not $SkipSystem) {
    Refresh-Path
    if (Find-Python310) { Say 'OK' 'Python 3.10 найден' } else { [void](Winget-Install 'Python.Python.3.10' 'Python 3.10') }
    if (Have git) { Say 'OK' 'Git найден' } else { [void](Winget-Install 'Git.Git' 'Git') }
    if (Have docker) { Say 'OK' 'Docker найден' }
    else {
        [void](Winget-Install 'Docker.DockerDesktop' 'Docker Desktop')
        $manual.Add('Запустить Docker Desktop один раз, принять лицензию, включить WSL2 (может потребоваться перезагрузка)')
    }
    $ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if ((Have ollama) -or (Test-Path $ollamaExe)) { Say 'OK' 'Ollama найден' } else { [void](Winget-Install 'Ollama.Ollama' 'Ollama') }
    $lmsDir = Join-Path $env:LOCALAPPDATA 'Programs\LM Studio'
    if ((Have lms) -or (Test-Path $lmsDir)) { Say 'OK' 'LM Studio найден' } else { [void](Winget-Install 'ElementLabs.LMStudio' 'LM Studio') }
} else {
    Say 'WARN' 'Системные программы пропущены (-SkipSystem)'
}

# ---------- 3. Портативные программы в tools\ ----------
$portable = @(
    @{ Name = 'LibreOffice'; Exe = 'tools\libreoffice\program\soffice.exe'; From = 'C:\Program Files\LibreOffice';    To = 'tools\libreoffice'; Winget = 'TheDocumentFoundation.LibreOffice' },
    @{ Name = 'Tesseract';   Exe = 'tools\tesseract\tesseract.exe';         From = 'C:\Program Files\Tesseract-OCR'; To = 'tools\tesseract';   Winget = 'UB-Mannheim.TesseractOCR' },
    @{ Name = 'ffmpeg';      Exe = 'tools\ffmpeg\bin\ffmpeg.exe';           From = $null;                            To = 'tools\ffmpeg\bin';  Winget = 'Gyan.FFmpeg' }
)
foreach ($p in $portable) {
    if (Test-Path (Join-Path $root $p.Exe)) { Say 'OK' "$($p.Name): tools\ (внутри проекта)"; continue }
    if ($p.From -and (Test-Path $p.From)) {
        Say '..' "$($p.Name): копирую из $($p.From) в проект"
        robocopy $p.From (Join-Path $root $p.To) /E /XC /XN /XO /NFL /NDL /NJH /NJS /NP | Out-Null
        continue
    }
    if ($p.Name -eq 'ffmpeg') {
        $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if ($ff) {
            New-Item -ItemType Directory -Force (Join-Path $root $p.To) | Out-Null
            Copy-Item $ff.Source (Join-Path $root 'tools\ffmpeg\bin\ffmpeg.exe') -Force
            $pr = Join-Path (Split-Path $ff.Source) 'ffprobe.exe'
            if (Test-Path $pr) { Copy-Item $pr (Join-Path $root 'tools\ffmpeg\bin\ffprobe.exe') -Force }
            Say 'OK' 'ffmpeg: скопирован из PATH в tools\'
            continue
        }
    }
    if (-not $SkipSystem -and (Winget-Install $p.Winget $p.Name)) {
        Say 'OK' "$($p.Name): установлен в систему (проект найдёт его через PATH / Program Files)"
    } else {
        Say 'FAIL' "$($p.Name): нет ни в tools\, ни в системе"
    }
}

# ---------- 4. Python-окружения ----------
Say '..' 'Python-окружения (setup_env.ps1)…'
& (Join-Path $PSScriptRoot 'setup_env.ps1') -OldRoot $OldRoot

# ---------- 5. .env ----------
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    $tpl = Join-Path $root '.env.example'
    if (Test-Path $tpl) {
        $t = (Get-Content $tpl -Raw -Encoding UTF8).Replace($OldRoot, $root).Replace($OldRoot.Replace('\', '/'), $root.Replace('\', '/'))
        Set-Content $envFile $t -Encoding UTF8 -NoNewline
        Say 'OK' '.env создан из .env.example'
        $manual.Add('Заполнить .env: SECRET_KEY, хеши паролей (ADMIN/MANAGER/USER_PASSWORD_HASH), HF_TOKEN; CURSOR_API_KEY / TELEGRAM_BOT_TOKEN — по необходимости')
    } else {
        Say 'FAIL' 'Нет ни .env, ни .env.example'
    }
} else {
    Say 'OK' '.env уже есть'
}

# ---------- 6. Ollama: модель эмбеддингов ----------
Refresh-Path
$ollama = $null
if (Have ollama) { $ollama = 'ollama' }
elseif (Test-Path (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe')) { $ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
if ($ollama) {
    try {
        $have = (& $ollama list 2>$null) -join ' '
        if ($have -match 'bge-m3') { Say 'OK' 'Ollama: bge-m3 уже есть' }
        else {
            Say '..' 'Ollama: скачиваю bge-m3 (~1,2 ГБ)…'
            & $ollama pull bge-m3
            Say $(if ($LASTEXITCODE -eq 0) { 'OK' } else { 'WARN' }) 'Ollama: bge-m3'
        }
    } catch {
        Say 'WARN' 'Ollama не отвечает — запустите приложение Ollama и выполните: ollama pull bge-m3'
        $manual.Add('ollama pull bge-m3')
    }
} else {
    Say 'WARN' 'Ollama не установлен — эмбеддинги (индекс базы знаний) работать не будут'
    $manual.Add('Установить Ollama и выполнить: ollama pull bge-m3')
}

# ---------- 7. Модели ASR и Docker ----------
$asrModels = Join-Path $root 'asr\models'
if (Test-Path $asrModels) { Say 'OK' 'Модели ASR (asr\models) на месте' } else { Say 'WARN' 'Нет asr\models — копируйте проект целиком вместе с моделями' }
foreach ($d in 'data\models\onnx-diar', 'data\models\onnx-asr', 'data\models\huggingface') {
    if (-not (Test-Path (Join-Path $root $d))) { Say 'WARN' "Нет папки модели: $d" }
}
if ($Asr) {
    if (Have docker) {
        Say '..' 'Docker: собираю образы ASR (долго)…'
        docker compose -f (Join-Path $root 'asr\docker-compose.yml') build
        Say $(if ($LASTEXITCODE -eq 0) { 'OK' } else { 'FAIL' }) 'Docker-образы ASR'
    } else {
        Say 'FAIL' 'Docker недоступен — образы ASR не собраны'
    }
} else {
    $manual.Add('Собрать Docker-образы ASR: INSTALL.bat -Asr  (или docker compose -f asr\docker-compose.yml build)')
}

# ---------- 7б. Git-хуки: индекс проекта и проверка карты при коммите ----------
if (Test-Path -LiteralPath (Join-Path $root '.git\hooks')) {
    try {
        & (Join-Path $PSScriptRoot 'install_hooks.ps1') | Out-Null
        Say 'OK' 'Git-хуки установлены: карта проекта проверяется при каждом коммите'
    } catch {
        Say 'WARN' "Git-хуки не установлены: $($_.Exception.Message)"
        $manual.Add('Поставить git-хуки: powershell -ExecutionPolicy Bypass -File scripts\install_hooks.ps1')
    }
} else {
    Say 'WARN' 'Это не git-копия (.git не найден): хуки проверки карты не поставлены'
}

# ---------- 8. Итог ----------
$manual.Add('LM Studio: скачать модель из .env (LMSTUDIO_MODEL) на вкладке Search; перед порталом — run_server.ps1 (или Start Server на :1234)')
$manual.Add('Опционально автозапуск сервера LM Studio при входе в Windows: powershell -ExecutionPolicy Bypass -File scripts\install_lmstudio_autostart.ps1')
$manual.Add('Проверка всей системы: powershell -ExecutionPolicy Bypass -File scripts\check_env.ps1')
$manual.Add('Запуск: scripts\SKKKK-3-Запуск-LM-Studio.bat (локальная модель) или scripts\SKKKK-2-Запуск-Cursor.bat (Cursor API)')
Write-Host "`n=== Осталось сделать вручную ===" -ForegroundColor Yellow
$i = 1
foreach ($m in $manual) { Write-Host ("{0}. {1}" -f $i++, $m); $report.Add("TODO: $m") }
$logDir = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$report | Set-Content (Join-Path $logDir 'install-report.txt') -Encoding UTF8
Write-Host "`nОтчёт: data\logs\install-report.txt" -ForegroundColor Cyan
