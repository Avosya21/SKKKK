# SKKKK — только проверка всех систем, без запуска
$ErrorActionPreference = "Stop"
Set-Location "C:\1C_NEW_SKKKK"
& .\venv\Scripts\Activate.ps1
python -m app.app.main check
exit $LASTEXITCODE
