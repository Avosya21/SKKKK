"""Download Ultra Sortformer 8spk v1 .nemo into project cache."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "nemo-diar-cache" / "ultra_8spk_v1"
OUT_FILE = OUT_DIR / "ultra_diar_streaming_sortformer_8spk_v1.nemo"
MODEL_REPO = "devsy0117/ultra_diar_streaming_sortformer_8spk_v1"
MODEL_FILE = "ultra_diar_streaming_sortformer_8spk_v1.nemo"


def _load_hf_token() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"} and val:
            os.environ.setdefault("HF_TOKEN", val)
            os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", val)
            return


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_FILE.is_file() and OUT_FILE.stat().st_size > 400_000_000:
        print(f"Already present: {OUT_FILE} ({OUT_FILE.stat().st_size} bytes)")
        return 0

    _load_hf_token()
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        MODEL_REPO,
        MODEL_FILE,
        local_dir=str(OUT_DIR),
    )
    print("OK", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
