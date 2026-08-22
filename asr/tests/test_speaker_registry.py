from speaker_registry import Segment, best_clip_per_speaker, cosine, merge_duplicate_labels, merge_dup_map_by_profile_id


def test_best_clip_skips_overlaps():
    segs = [
        Segment(33.5, 37.0, "SPEAKER_00"),
        Segment(33.8, 35.2, "SPEAKER_01"),
        Segment(37.4, 47.8, "SPEAKER_01"),
    ]
    best = best_clip_per_speaker(segs, min_duration=2.0)
    assert "SPEAKER_01" in best
    assert best["SPEAKER_01"].duration >= 10.0


def test_merge_duplicate_speakers():
    emb = [1.0, 0.0, 0.0]
    embs = {"A": emb, "B": emb}
    m = merge_duplicate_labels(["A", "B"], embs, threshold=0.99)
    assert m["A"] == m["B"]


def test_merge_dup_map_by_profile_id():
    dup = {"SPEAKER_01": "SPEAKER_01", "SPEAKER_03": "SPEAKER_03"}
    matches = {
        "SPEAKER_01": {
            "profile_id": 3,
            "profile_name": "Елена",
            "status": "confirmed",
        },
        "SPEAKER_03": {
            "profile_id": 3,
            "profile_name": "Елена",
            "status": "confirmed",
        },
    }
    merge_dup_map_by_profile_id(dup, matches)
    assert dup["SPEAKER_01"] == dup["SPEAKER_03"]
