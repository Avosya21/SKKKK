# SKKKK — установка служб Windows через NSSM (запуск от администратора)
param(
    [string]$Nssm = "nssm"
)
$ErrorActionPreference = "Stop"
$Root = "C:\1C_NEW_SKKKK"
$Py = Join-Path $Root "venv\Scripts\python.exe"
$Logs = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Install-SkkkkService {
    param([string]$Name, [string]$AppArgs)
    & $Nssm install $Name $Py $AppArgs
    & $Nssm set $Name AppDirectory $Root
    & $Nssm set $Name AppStdout (Join-Path $Logs "$Name.stdout.log")
    & $Nssm set $Name AppStderr (Join-Path $Logs "$Name.stderr.log")
    & $Nssm set $Name AppRestartDelay 5000
    & $Nssm set $Name Start SERVICE_AUTO_START
}

Install-SkkkkService "SKKKK-API" "-m app.app.main api --use-saved-llm --llm-provider cursor"
Install-SkkkkService "SKKKK-GPU" "-m app.app.main worker --use-saved-llm"
Write-Host "Services SKKKK-API and SKKKK-GPU installed. Start with: nssm start SKKKK-API"
