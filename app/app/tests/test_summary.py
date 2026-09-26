import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asr"))

from speakers import Turn, format_turns_txt  # noqa: E402


def _chunk_text(turns, max_chars=6000):
    from summarize_call import _chunk_text as chunk

    return chunk(turns, max_chars)


def test_chunk_text_splits_on_turn_boundaries_not_mid_turn():
    turns = [Turn(0, 1, "Спикер №1", "a" * 3000), Turn(1, 2, "Спикер №2", "b" * 3000)]
    chunks = _chunk_text(turns, max_chars=4000)
    assert len(chunks) == 2
    assert "aaa" in chunks[0]
    assert "bbb" in chunks[1]


def test_single_chunk_skips_map_pass():
    turns = [Turn(0, 1, "Спикер №1", "коротко")]
    chunks = _chunk_text(turns, max_chars=6000)
    assert len(chunks) == 1


def test_empty_transcript_raises(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("TMP", str(tmp_path))
    missing = tmp_path / "nope.json"
    if missing.exists():
        missing.unlink()
    with pytest.raises(SystemExit):
        from pathlib import Path
        import summarize_call as sc

        monkeypatch.setattr(sc, "OUT", tmp_path)
        sc._load_turns()
