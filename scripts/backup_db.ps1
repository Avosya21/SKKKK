# SKKKK — резервная копия SQLite (VACUUM INTO): app.db и база личных паролей credentials.db
param(
    [int]$Keep = 7
)
$ErrorActionPreference = "Stop"
$Root = (Split-Path -Parent $PSScriptRoot)
$Db = Join-Path $Root "data\app.db"
$BackupDir = Join-Path $Root "data\backups"
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$Target = Join-Path $BackupDir "app-$Stamp.db"
$sql = "VACUUM INTO '$($Target -replace '\\','/')';"
& "$Root\venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect(r'$Db'); c.execute('''$sql'''); c.close()"
Write-Host "backup: $Target"
Get-ChildItem $BackupDir -Filter "app-*.db" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep | Remove-Item -Force

# Хеши личных паролей — отдельная база; без её копии после восстановления все пароли пришлось бы создавать заново.
$Cred = Join-Path $Root "data\credentials.db"
if (Test-Path $Cred) {
    $CredTarget = Join-Path $BackupDir "credentials-$Stamp.db"
    $credSql = "VACUUM INTO '$($CredTarget -replace '\\','/')';"
    & "$Root\venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect(r'$Cred'); c.execute('''$credSql'''); c.close()"
    Write-Host "backup: $CredTarget"
    Get-ChildItem $BackupDir -Filter "credentials-*.db" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep | Remove-Item -Force
}
