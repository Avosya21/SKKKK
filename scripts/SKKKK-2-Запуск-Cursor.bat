@echo off
setlocal EnableExtensions
chcp 65001 >nul
title SKKKK — запуск (Cursor API)

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

echo.
echo ========================================
echo   Primary Analytics — запуск
echo   LLM: Cursor API
echo ========================================
echo.

if not exist "%ROOT%\venv\Scripts\python.exe" (
    echo [ОШИБКА] Не найден venv: %ROOT%\venv
    pause
    exit /b 1
)

if not exist "%ROOT%\.env" (
    echo [ОШИБКА] Нет файла .env — сначала один раз запустите проект из корня.
    pause
    exit /b 1
)

echo [1/3] Остановка предыдущего экземпляра...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\stop.ps1"
if errorlevel 1 echo   [ПРЕДУПРЕЖДЕНИЕ] stop.ps1 завершился с кодом %errorlevel%
powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'app\.app\.main' }; foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo [2/3] ASR-контейнер GigaAM (docker)...
where docker >nul 2>&1
if errorlevel 1 (
    echo   docker не в PATH — пропуск
) else (
    pushd "%ROOT%\asr"
    docker compose --env-file "..\.env" up -d gigaam
    if errorlevel 1 echo   [ПРЕДУПРЕЖДЕНИЕ] docker compose завершился с ошибкой
    popd
)

echo.
echo [3/3] Портал + GPU-воркер (проверка систем, LLM=cursor)...
echo   Окно держите открытым — здесь логи API и воркера.
echo   Остановка: SKKKK-1-Остановить.bat
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\start.ps1" -Llm cursor

echo.
echo Процесс завершился.
pause
endlocal
