@echo off
setlocal EnableExtensions
chcp 65001 >nul
title SKKKK — остановка

for %%I in ("%~dp0..") do set "ROOT=%%~fI"

echo.
echo ========================================
echo   Primary Analytics — остановка
echo ========================================
echo.

if not exist "%ROOT%\scripts\stop.ps1" (
    echo [ОШИБКА] Не найден %ROOT%\scripts\stop.ps1
    pause
    exit /b 1
)

echo [1/3] API, GPU-воркер, lock-файлы...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\stop.ps1"
if errorlevel 1 (
    echo [ПРЕДУПРЕЖДЕНИЕ] stop.ps1 завершился с кодом %errorlevel%
)

echo.
echo [2/3] Остаточные процессы python app.app.main...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'app\.app\.main' }; if (-not $procs) { Write-Host '  нет'; exit 0 }; foreach ($p in $procs) { Write-Host ('  kill PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo [3/3] Мониторинг monitor_skkkk_loop (если был)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -in @('powershell.exe','pwsh.exe') -and $_.CommandLine -match 'monitor_skkkk_loop' }; if (-not $procs) { Write-Host '  нет'; exit 0 }; foreach ($p in $procs) { Write-Host ('  kill PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo Готово. Сервер и фоновые задачи SKKKK остановлены.
echo.
pause
endlocal
