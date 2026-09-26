from asr.lab.proof import apply_picks, spread_tokens


def test_spread_tokens_same_count():
    assert spread_tokens(["a", "b", "c", "d", "e"], 5) == ["a", "b", "c", "d", "e"]


def test_spread_tokens_fewer_than_slots():
    assert spread_tokens(["a", "b"], 5) == ["a", "", "b", "", ""]


def test_spread_tokens_more_than_slots():
    parts = spread_tokens(["a", "b", "c", "d", "e", "f", "g"], 3)
    assert " ".join(p for p in parts if p).split() == ["a", "b", "c", "d", "e", "f", "g"]
    assert len(parts) == 3


def test_spread_tokens_empty_fills_slots():
    assert spread_tokens([], 4) == ["", "", "", ""]


def test_apply_picks_spreads_giga_tokens_across_parakeet_slots():
    words = [
        {"i": 0, "start": 1.0, "end": 1.2, "word": "один", "speaker": "A"},
        {"i": 1, "start": 1.2, "end": 1.4, "word": "два", "speaker": "A"},
        {"i": 2, "start": 1.4, "end": 1.6, "word": "три", "speaker": "A"},
    ]
    diffs = [{"pick": "giga", "word_from": 0, "word_to": 2, "giga": "раз два три четыре"}]
    out = apply_picks(words, diffs)
    assert [row["start"] for row in out] == [1.0, 1.2, 1.4]
    tokens = " ".join(row["final"] for row in out if row["final"]).split()
    assert tokens == ["раз", "два", "три", "четыре"]
