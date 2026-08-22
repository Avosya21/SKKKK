# Sortformer v2.1 GGUF diarization via OpenResearchTools Engine.
# GPU: Vulkan (driver 577+) or CUDA (driver 580+, CUDA 13 runtime in engine bundle).

param(
    [string]$Audio = "C:\1C_NEW_SKKKK\asr\work\input_16k.wav",
    [double]$ClipSec = 0,
    [string]$OutDir = "C:\1C_NEW_SKKKK\experiments\nemo-diar\out",
    [ValidateSet("auto", "cuda", "vulkan", "cpu")]
    [string]$Prefer = "auto"
)

$ErrorActionPreference = "Stop"
$Gguf = "$env:USERPROFILE\.lmstudio\models\openresearchtools\diar_streaming_sortformer_4spk-v2.1-gguf\diar_streaming_sortformer_4spk-v2.1.gguf"
$Py = "C:\1C_NEW_SKKKK\venvs\nemo-diar\Scripts\python.exe"
$PickScript = "C:\1C_NEW_SKKKK\experiments\nemo-diar\pick_engine_backend.ps1"

if (-not (Test-Path $Gguf)) {
    throw "Sortformer GGUF not found: $Gguf"
}
if (-not (Test-Path $Audio)) {
    throw "Audio not found: $Audio"
}

$backend = & powershell -ExecutionPolicy Bypass -File $PickScript -Prefer $Prefer | ConvertFrom-Json
$EngineCli = Join-Path $backend.engine_dir "example-cli.exe"
if (-not (Test-Path $EngineCli)) {
    throw "Engine CLI not found: $EngineCli"
}

Write-Host "Backend:" $backend.backend "device:" $backend.device
if ($backend.note) { Write-Host "Note:" $backend.note }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$RunAudio = $Audio
if ($ClipSec -gt 0) {
    $RunAudio = Join-Path $OutDir ("clip_{0}s.wav" -f [int]$ClipSec)
    & $Py -c "import soundfile as sf; from pathlib import Path; audio=Path(r'$Audio'); out=Path(r'$RunAudio'); s,sr=sf.read(audio, dtype='float32', always_2d=True); keep=min(len(s), int($ClipSec*sr)); sf.write(out, s[:keep], sr)"
}

$Stem = [IO.Path]::GetFileNameWithoutExtension($RunAudio)
$Tag = if ($backend.backend -eq "cpu") { "cpu" } else { $backend.backend }
$Spans = Join-Path $OutDir ("sortformer_gguf_{0}_{1}_spans.md" -f $Tag, $Stem)
$Report = Join-Path $OutDir ("sortformer_gguf_{0}_{1}_report.json" -f $Tag, $Stem)

$argsList = @(
    "bridge", "audio-session",
    "--audio-file", $RunAudio,
    "--diarization-model-path", $Gguf,
    "--diarization-offline",
    "--diarization-backend", "native_cpp",
    "--diarization-device", $backend.device,
    "--no-transcription",
    "--out", $Spans
)
if ($backend.backend -eq "cpu") {
    $argsList += @("--devices", "none")
} elseif ($null -ne $backend.gpu_index) {
    $argsList += @("--gpu", [string]$backend.gpu_index)
}

Push-Location $backend.engine_dir
$sw = [System.Diagnostics.Stopwatch]::StartNew()
& $EngineCli @argsList
$sw.Stop()
Pop-Location

$Elapsed = [math]::Round($sw.Elapsed.TotalSeconds, 1)
$clipArg = if ($ClipSec -gt 0) { $ClipSec } else { 999999 }
& $Py C:\1C_NEW_SKKKK\experiments\nemo-diar\parse_gguf_spans.py `
    --spans $Spans `
    --audio $RunAudio `
    --out $Report `
    --elapsed-sec $Elapsed `
    --baseline C:\1C_NEW_SKKKK\asr\out\pyannote_segments.json `
    --clip-sec $clipArg

Write-Host "`nDone."
Write-Host "Backend: $($backend.backend) ($($backend.device))"
Write-Host "Spans:   $Spans"
Write-Host "Report:  $Report"
Write-Host "Elapsed: ${Elapsed}s"
