# GPU probe через PyTorch (py -3, torch+cu121).
# Модель: git clone https://moark.com/hf-models/wav2vec2-xls-r-1b-russian
# Ожидаемый путь: ~\wav2vec2-xls-r-1b-russian\pytorch_model.bin (~3.7 GB)

param(
    [string]$Artifact = "art_2026-08-24_154408_f02d28",
    [int]$ProjectId = 12,
    [string]$ModelDir = "$env:USERPROFILE\wav2vec2-xls-r-1b-russian",
    [string[]]$Range = @()
)

Set-Location $PSScriptRoot\..

$weights = Join-Path $ModelDir "pytorch_model.bin"
if (-not (Test-Path $weights)) {
    Write-Host "Waiting for $weights ..."
    while (-not (Test-Path $weights)) {
        $lfs = Get-ChildItem "$ModelDir\.git\lfs" -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum
        $mb = [math]::Round($lfs.Sum / 1MB, 0)
        Write-Host "$(Get-Date -Format HH:mm:ss) LFS cache ~${mb}MB / ~3700MB"
        Start-Sleep -Seconds 10
    }
}

py -3 -m pip install -q soundfile transformers huggingface_hub

$probeArgs = @(
    "asr/experiments/wav2vec2_timing_probe.py",
    "--artifact", $Artifact,
    "--project-id", $ProjectId,
    "--backend", "hf",
    "--device", "cuda",
    "--model-dir", $ModelDir
)
foreach ($r in $Range) { $probeArgs += @("--range", $r) }

py -3 @probeArgs