# Полный эксперимент wav2vec2 + тестовая HTML-страница.
# GPU: py -3 (torch+cu121). Модель: data/models/wav2vec2-xls-r-1b-russian

param(
    [string]$Artifact = "art_2026-08-24_154408_f02d28",
    [int]$ProjectId = 12,
    [string]$Backend = "hf",
    [string]$Device = "cuda",
    [string]$ModelDir = "D:\Primary_Analitics\data\models\wav2vec2-xls-r-1b-russian",
    [switch]$Full,
    [string[]]$Range = @(),
    [switch]$Serve,
    [int]$Port = 18105
)

Set-Location $PSScriptRoot\..

$py = "py"
if ($Backend -eq "hf") {
    $py = "py -3"
}

$runArgs = @(
    "asr/experiments/wav2vec2_full_run.py",
    "--artifact", $Artifact,
    "--project-id", $ProjectId,
    "--backend", $Backend,
    "--device", $Device,
    "--model-dir", $ModelDir
)
if ($Full) { $runArgs += "--full" }
foreach ($r in $Range) { $runArgs += @("--range", $r) }

Write-Host "=== wav2vec2 full run ===" -ForegroundColor Cyan
Invoke-Expression "$py $($runArgs -join ' ')"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$pageArgs = @(
    "asr/experiments/wav2vec2_test_page.py",
    "--artifact", $Artifact,
    "--project-id", $ProjectId
)
if ($Serve) { $pageArgs += @("--serve", "--port", $Port) }

Write-Host "=== build test page ===" -ForegroundColor Cyan
Invoke-Expression "$py $($pageArgs -join ' ')"
