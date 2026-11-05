# PrimaryAnalitics — установка служб Windows через NSSM (запуск от администратора)
param(
    [string]$Nssm = "nssm"
)
$ErrorActionPreference = "Stop"
$Root = (Split-Path -Parent $PSScriptRoot)
$Py = Join-Path $Root "venv\Scripts\python.exe"
$Logs = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Install-PrimaryAnaliticsService {
    param([string]$Name, [string]$AppArgs)
    & $Nssm install $Name $Py $AppArgs
    & $Nssm set $Name AppDirectory $Root
    & $Nssm set $Name AppStdout (Join-Path $Logs "$Name.stdout.log")
    & $Nssm set $Name AppStderr (Join-Path $Logs "$Name.stderr.log")
    & $Nssm set $Name AppRestartDelay 5000
    & $Nssm set $Name Start SERVICE_AUTO_START
}

Install-PrimaryAnaliticsService "PrimaryAnalitics-API" "-m app.app.main api --use-saved-llm --llm-provider cursor"
Install-PrimaryAnaliticsService "PrimaryAnalitics-GPU" "-m app.app.main worker --use-saved-llm"
Write-Host "Services PrimaryAnalitics-API and PrimaryAnalitics-GPU installed. Start with: nssm start PrimaryAnalitics-API"
