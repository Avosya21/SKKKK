# SKKKK — GPU-воркер очереди
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\venv\Scripts\Activate.ps1
python -m app.app.main worker
