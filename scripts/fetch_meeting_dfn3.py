"""Download DeepFilterNet3 browser assets. Live meeting only, not lab ASR."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "app" / "app" / "static" / "vendor" / "dfn3"
ESM_URL = "https://cdn.jsdelivr.net/npm/deepfilternet3-noise-filter@1.3.0/dist/index.esm.js"
BASE = "https://cdn.mezon.ai/AI/models/datas/noise_suppression/deepfilternet3"
LOCAL_PREFIX = "/static/vendor/dfn3"
REMOTE_PREFIX = "https://cdn.mezon.ai/AI/models/datas/noise_suppression/deepfilternet3"
FILES = (
    ("index.esm.js", ESM_URL),
    ("v3/pkg/df_bg.wasm", BASE + "/v3/pkg/df_bg.wasm"),
    ("v3/models/DeepFilterNet3_onnx.tar.gz", BASE + "/v3/models/DeepFilterNet3_onnx.tar.gz"),
)


def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print("GET", url)
    with urllib.request.urlopen(url, timeout=120) as resp:
        dest.write_bytes(resp.read())
    print("wrote", dest, dest.stat().st_size)


def patch_esm(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(REMOTE_PREFIX, LOCAL_PREFIX, 1), encoding="utf-8")


def main() -> int:
    try:
        for rel, url in FILES:
            fetch(url, DEST / rel)
        patch_esm(DEST / "index.esm.js")
    except Exception as exc:
        print("DFN3 assets skipped:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
