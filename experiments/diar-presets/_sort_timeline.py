import json
from pathlib import Path

sf = json.loads(
    Path(r"D:\Primary_Analitics\experiments\nemo-diar\out\pilot_22_07_26\sortformer_gguf_vulkan_input_16k_report.json").read_text(
        encoding="utf-8"
    )
)
print("Sortformer 30-120s:")
for s in sf["segments"]:
    if 30 <= s["start"] < 120:
        print(f"  {s['start']:6.1f}-{s['end']:6.1f}  {s['speaker']}")
