from app.app.services.entities import _split_for_extraction


def test_split_for_extraction_single_newline_stenogram():
    lines = [f"[00:{i:02d}] SPEAKER_00: line {i}" for i in range(300)]
    text = "\n".join(lines)
    chunks = _split_for_extraction(text, max_chars=6000)
    assert len(chunks) >= 2
    assert all(len(c) <= 6000 for c in chunks)


def test_split_for_extraction_long_call_like_project10():
    lines = [f"[{i // 60:02d}:{i % 60:02d}] SPEAKER_{i % 4:02d}: utterance {i}" for i in range(900)]
    text = "\n".join(lines)
    chunks = _split_for_extraction(text, max_chars=6000)
    assert len(chunks) >= 6
    assert max(len(c) for c in chunks) <= 6000
    # Full 71-min transcript scale must not stay a single chunk.
    big = "\n".join(f"[00:{i:02d}] SPK: {'x' * 120}" for i in range(800))
    big_chunks = _split_for_extraction(big, max_chars=6000)
    assert len(big_chunks) >= 12
