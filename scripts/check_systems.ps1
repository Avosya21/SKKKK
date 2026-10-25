# PrimaryAnalitics — только проверка всех систем, без запуска
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\venv\Scripts\Activate.ps1
python -m app.app.main check
exit $LASTEXITCODE
