# SKKKK — Caddy как HTTPS-прокси перед порталом (запуск от администратора).
# Скачивает бинарник, открывает 80/443, ставит службу SKKKK-CADDY через NSSM.
param(
    [string]$Version = "latest",
    [string]$Nssm = "nssm"
)
$ErrorActionPreference = "Stop"

$Root = "C:\1C_NEW_SKKKK"
$CaddyDir = Join-Path $Root "tools\caddy"
$CaddyExe = Join-Path $CaddyDir "caddy.exe"
$Caddyfile = Join-Path $CaddyDir "Caddyfile"
$Logs = Join-Path $Root "data\logs"
$Storage = Join-Path $Root "data\caddy"
$ServiceName = "SKKKK-CADDY"
$MinVersion = [version]"2.10.1"   # раньше Caddy не умеет сертификаты на IP
$DefaultHttpsPort = "777"
$DefaultAcmeHttpPort = "8088"
$DefaultAcmeCa = "https://acme-v02.api.letsencrypt.org/directory"
$DefaultWebPort = "1777"

function Get-EnvValue {
    param([string]$Key, [string]$Fallback = "")
    $file = Join-Path $Root ".env"
    if (-not (Test-Path $file)) { return $Fallback }
    $line = Select-String -Path $file -Pattern "^\s*$Key\s*=" | Select-Object -First 1
    if (-not $line) { return $Fallback }
    $value = $line.Line.Split("=", 2)[1].Trim()
    if ($value) { return $value } else { return $Fallback }
}

function Resolve-CaddyAsset {
    param([string]$Tag)
    $api = "https://api.github.com/repos/caddyserver/caddy/releases"
    $url = if ($Tag -eq "latest") { "$api/latest" } else { "$api/tags/v$Tag" }
    $release = Invoke-RestMethod -Uri $url -Headers @{ "User-Agent" = "skkkk" }
    $asset = $release.assets | Where-Object { $_.name -like "caddy_*_windows_amd64.zip" } | Select-Object -First 1
    if (-not $asset) { throw "В релизе $($release.tag_name) нет сборки windows_amd64" }
    $resolved = [version]($release.tag_name.TrimStart("v"))
    if ($resolved -lt $MinVersion) { throw "Caddy $resolved не умеет сертификаты на IP, нужна $MinVersion+" }
    return [pscustomobject]@{ Version = $resolved; Url = $asset.browser_download_url }
}

function Install-CaddyBinary {
    param([string]$Url)
    $tmp = Join-Path $env:TEMP "caddy_skkkk"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp "caddy.zip"
    Invoke-WebRequest -Uri $Url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
    Copy-Item (Join-Path $tmp "caddy.exe") $CaddyExe -Force
    Remove-Item $tmp -Recurse -Force
}

function Assert-PortsFree {
    param([int[]]$Ports)
    foreach ($port in $Ports) {
        $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if (-not $busy) { continue }
        $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
        if ($owner -eq "caddy") { continue }
        throw "Порт $port занят процессом '$owner'."
    }
}

function Open-FirewallPorts {
    param([int]$Port)
    $name = "SKKKK Caddy TCP $Port"
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) { return }
    New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port | Out-Null
}

function Install-CaddyService {
    param([hashtable]$ServiceEnv)
    if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
        & $Nssm stop $ServiceName
        & $Nssm remove $ServiceName confirm
    }
    & $Nssm install $ServiceName $CaddyExe "run --config `"$Caddyfile`""
    & $Nssm set $ServiceName AppDirectory $CaddyDir
    & $Nssm set $ServiceName AppStdout (Join-Path $Logs "$ServiceName.stdout.log")
    & $Nssm set $ServiceName AppStderr (Join-Path $Logs "$ServiceName.stderr.log")
    & $Nssm set $ServiceName AppRestartDelay 5000
    & $Nssm set $ServiceName Start SERVICE_AUTO_START
    $pairs = $ServiceEnv.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
    & $Nssm set $ServiceName AppEnvironmentExtra @pairs
}

$publicHost = Get-EnvValue "SKKKK_PUBLIC_HOST"
if (-not $publicHost) { throw "SKKKK_PUBLIC_HOST не задан в .env — некому выписывать сертификат" }

$serviceEnv = @{
    SKKKK_PUBLIC_HOST   = $publicHost
    SKKKK_ACME_EMAIL    = Get-EnvValue "SKKKK_ACME_EMAIL"
    SKKKK_ACME_CA       = Get-EnvValue "SKKKK_ACME_CA" $DefaultAcmeCa
    SKKKK_WEB_PORT         = Get-EnvValue "WEB_PORT" $DefaultWebPort
    SKKKK_HTTPS_PORT       = Get-EnvValue "SKKKK_HTTPS_PORT" $DefaultHttpsPort
    SKKKK_CADDY_STORAGE    = $Storage
    SKKKK_CADDY_LOG        = Join-Path $Logs "caddy.log"
    SKKKK_ACME_HTTP_PORT   = Get-EnvValue "SKKKK_ACME_HTTP_PORT" $DefaultAcmeHttpPort
}

New-Item -ItemType Directory -Force -Path $Logs, $Storage | Out-Null
Assert-PortsFree -Ports @([int]$serviceEnv.SKKKK_HTTPS_PORT, [int]$serviceEnv.SKKKK_ACME_HTTP_PORT)

if (Test-Path $CaddyExe) {
    $installed = & $CaddyExe version
    Write-Host "Caddy уже есть: $installed — скачивание пропускаю"
} else {
    $asset = Resolve-CaddyAsset $Version
    Write-Host "Caddy $($asset.Version) — скачиваю"
    Install-CaddyBinary $asset.Url
}

$serviceEnv.GetEnumerator() | ForEach-Object { Set-Item "env:$($_.Key)" $_.Value }
& $CaddyExe validate --config $Caddyfile
if ($LASTEXITCODE -ne 0) { throw "Caddyfile не прошёл проверку" }

Open-FirewallPorts -Port ([int]$serviceEnv.SKKKK_HTTPS_PORT)
Install-CaddyService $serviceEnv

Write-Host ""
Write-Host "Служба $ServiceName установлена. Запуск: nssm start $ServiceName"
Write-Host "Роутер: внешний :80 → $($serviceEnv.SKKKK_ACME_HTTP_PORT); внешний :$($serviceEnv.SKKKK_HTTPS_PORT) → $($serviceEnv.SKKKK_HTTPS_PORT)"
Write-Host "Проверка: https://${publicHost}:$($serviceEnv.SKKKK_HTTPS_PORT)/health ; журнал — $($serviceEnv.SKKKK_CADDY_LOG)"
