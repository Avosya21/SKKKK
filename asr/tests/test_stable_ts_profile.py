"""stable-ts iron profile kwargs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "asr" / "experiments"))

from stable_ts_lib import stable_ts_transcribe_kwargs  # noqa: E402


def test_iron_profile_sequential_word_ts():
    kw = stable_ts_transcribe_kwargs(profile="iron")
    assert kw["batch_size"] is None
    assert kw["vad"] is True
    assert kw["word_timestamps"] is True
    assert kw["repetition_penalty"] == 1.2
    assert "condition_on_previous_text" not in kw
