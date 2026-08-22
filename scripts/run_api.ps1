# SKKKK — только API (LLM из .env)
$ErrorActionPreference = "Stop"
Set-Location "C:\1C_NEW_SKKKK"
& .\venv\Scripts\python.exe -m app.app.main api --use-saved-llm --skip-checks