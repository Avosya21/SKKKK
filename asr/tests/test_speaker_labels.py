from speaker_labels import display_name_for_diar, suggested_hint, speaker_registry_rows


def test_transcript_shows_speaker_id_until_confirmed():
    payload = {
        "merge_map": {"SPEAKER_00": "SPEAKER_00"},
        "matches": {
            "SPEAKER_00": {
                "status": "new",
                "suggested_profile_name": "Елена",
                "suggested_score": 0.96,
            }
        },
    }
    assert display_name_for_diar("SPEAKER_00", payload) == "SPEAKER_00"


def test_transcript_shows_name_after_confirm():
    payload = {
        "merge_map": {"SPEAKER_00": "SPEAKER_00"},
        "matches": {
            "SPEAKER_00": {
                "status": "confirmed",
                "profile_name": "Елена Соколова",
            }
        },
    }
    assert display_name_for_diar("SPEAKER_00", payload) == "Елена Соколова"


def test_registry_hint_from_suggestion():
    payload = {
        "merge_map": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01"},
        "matches": {
            "SPEAKER_00": {
                "status": "new",
                "suggested_profile_name": "Елена",
                "suggested_score": 0.991,
            }
        },
        "segments_merged": [{"start": 0, "end": 1, "speaker": "SPEAKER_01"}],
    }
    rows = speaker_registry_rows(payload)
    by_id = {r["primary"]: r for r in rows}
    assert by_id["SPEAKER_00"]["suggested_hint"] == "Елена (99%)"
    assert by_id["SPEAKER_00"]["input_value"] == ""
    assert suggested_hint(payload["matches"]["SPEAKER_00"]) == "Елена (99%)"
