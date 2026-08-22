# SKKKK — GPU-воркер очереди
$ErrorActionPreference = "Stop"
Set-Location "C:\1C_NEW_SKKKK"
& .\venv\Scripts\Activate.ps1
python -m app.app.main worker
