param([string]$Json, [string]$OutDir)
Add-Type -AssemblyName System.Speech
$items = Get-Content -Raw -Encoding UTF8 $Json | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Irina Desktop')
$s.Rate = 4
$i = 0
foreach ($t in $items) {
  $s.SetOutputToWaveFile((Join-Path $OutDir ("c{0:D2}.wav" -f $i)))
  $s.Speak($t)
  $i++
}
$s.Dispose()
