# PrimaryAnalitics — только API (LLM из .env)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\venv\Scripts\python.exe -m app.app.main api --use-saved-llm --skip-checks