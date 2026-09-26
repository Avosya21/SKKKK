from speaker_registry import (
    AUDIT_AUTO_HIT,
    AUDIT_MARGIN,
    build_leave_one_out_centroids,
    decide_turn_reassign,
    score_against_centroids,
)


def test_keep_when_label_matches_best():
    d = decide_turn_reassign("A", {"A": 0.95, "B": 0.70})
    assert d.action == "keep"
    assert d.reason == "matches_label"
    assert d.new_speaker is None


def test_auto_reassign_on_high_confidence_mismatch():
    d = decide_turn_reassign(
        "A",
        {"A": 0.60, "B": 0.92},
        auto_hit=AUDIT_AUTO_HIT,
        margin=AUDIT_MARGIN,
    )
    assert d.action == "auto_reassign"
    assert d.new_speaker == "B"
    assert d.reason == "high_confidence_mismatch"


def test_keep_when_margin_too_small():
    d = decide_turn_reassign("A", {"A": 0.85, "B": 0.90}, auto_hit=0.88, margin=0.12)
    assert d.action == "keep"
    assert d.reason == "below_auto_threshold"


def test_keep_when_best_below_hit():
    d = decide_turn_reassign("A", {"A": 0.50, "B": 0.80}, auto_hit=0.88, margin=0.12)
    assert d.action == "keep"


def test_skip_without_scores():
    d = decide_turn_reassign("A", {})
    assert d.action == "skip"


def test_leave_one_out_excludes_self():
    # idx0 and idx1 are A; idx2 is B
    labeled = [
        (0, "A", [1.0, 0.0]),
        (1, "A", [0.9, 0.1]),
        (2, "B", [0.0, 1.0]),
    ]
    cents = build_leave_one_out_centroids(labeled, speakers=["A", "B"])
    # for turn 0, centroid A should be only emb of turn 1
    assert cents[0]["A"] == [0.9, 0.1]
    assert cents[0]["B"] == [0.0, 1.0]


def test_score_against_centroids():
    scores = score_against_centroids([1.0, 0.0], {"A": [1.0, 0.0], "B": [0.0, 1.0]})
    assert scores["A"] > 0.99
    assert scores["B"] < 0.01


def test_audit_turns_auto_reassigns_with_synthetic_embeddings(tmp_path):
    from speakers import Turn
    from voice_turn_audit import apply_decisions, audit_turns

    turns = [
        Turn(0.0, 4.0, "A", "раз"),
        Turn(4.0, 8.0, "A", "два"),
        Turn(8.0, 12.0, "B", "три"),
        Turn(12.0, 16.0, "B", "четыре чужой"),  # по голосу как A
    ]
    # A ≈ [1,0], B ≈ [0,1]; последняя реплика звучит как A
    embeddings = {
        0: [1.0, 0.0],
        1: [0.98, 0.02],
        2: [0.02, 0.98],
        3: [0.99, 0.01],
    }
    decisions = audit_turns(turns, embeddings, auto_hit=0.88, margin=0.12)
    assert decisions[3].action == "auto_reassign"
    assert decisions[3].new_speaker == "A"
    patched, n = apply_decisions(turns, decisions)
    assert n == 1
    assert any(t.speaker == "A" and "чужой" in t.text for t in patched)
