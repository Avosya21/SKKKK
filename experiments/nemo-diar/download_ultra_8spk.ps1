# Download Ultra Sortformer 8spk v1 (.nemo, ~471 MB) into project cache.
# Manual fallback URL:
# https://huggingface.co/devsy0117/ultra_diar_streaming_sortformer_8spk_v1/resolve/main/ultra_diar_streaming_sortformer_8spk_v1.nemo
# Save as:
#   D:\Primary_Analitics\data\nemo-diar-cache\ultra_8spk_v1\ultra_diar_streaming_sortformer_8spk_v1.nemo

$ErrorActionPreference = "Stop"
$Py = "D:\Primary_Analitics\venvs\nemo-diar\Scripts\python.exe"
& $Py "D:\Primary_Analitics\experiments\nemo-diar\download_ultra_8spk.py"
