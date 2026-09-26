from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "asr") not in sys.path:
    sys.path.insert(0, str(ROOT / "asr"))

from summarize_call import lab_proof_is_canonical  # noqa: E402


def test_lab_proof_is_canonical_when_proof_exists(tmp_path: Path):
    asr_out = tmp_path / "asr"
    asr_out.mkdir()
    run_id = "20260826T212241Z_694716"
    proof = tmp_path / "lab" / "runs" / run_id / "proof"
    proof.mkdir(parents=True)
    (proof / "transcript.json").write_text("{}", encoding="utf-8")
    (tmp_path / "lab" / "latest.json").write_text(
        json.dumps({"run_id": run_id}),
        encoding="utf-8",
    )
    assert lab_proof_is_canonical(asr_out) is True


def test_lab_proof_is_canonical_false_without_run(tmp_path: Path):
    asr_out = tmp_path / "asr"
    asr_out.mkdir()
    assert lab_proof_is_canonical(asr_out) is False
