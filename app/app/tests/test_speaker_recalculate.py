import json
import struct

from app.app.services.speaker_profiles import (
    _restore_confirmed_matches,
    dedupe_all_speaker_profiles,
    load_universal_voice_profiles,
)


def _vec(*values: float) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


def test_restore_confirmed_matches_overwrites_auto_suggestions():
    prev = {
        "matches": {
            "SPEAKER_00": {
                "diar_label": "SPEAKER_00",
                "profile_id": 45,
                "profile_name": "девушка 1",
                "status": "confirmed",
            },
            "SPEAKER_03": {
                "diar_label": "SPEAKER_03",
                "profile_id": 43,
                "profile_name": "Мужчина 1",
                "status": "confirmed",
            },
        },
        "merge_map": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_03": "SPEAKER_03"},
        "voice_aliases": {"SPEAKER_03": "Мужчина 1"},
    }
    payload = {
        "matches": {
            "SPEAKER_00": {
                "diar_label": "SPEAKER_00",
                "profile_id": 43,
                "profile_name": "Мужчина 1",
                "status": "matched",
                "score": 0.72,
            },
            "SPEAKER_03": {
                "diar_label": "SPEAKER_03",
                "profile_id": 45,
                "profile_name": "девушка 1",
                "status": "matched",
                "score": 0.68,
            },
        },
        "merge_map": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_03": "SPEAKER_03"},
        "display_names": {},
    }

    _restore_confirmed_matches(payload, prev)

    assert payload["matches"]["SPEAKER_00"]["profile_id"] == 45
    assert payload["matches"]["SPEAKER_00"]["profile_name"] == "девушка 1"
    assert payload["matches"]["SPEAKER_00"]["status"] == "confirmed"
    assert payload["matches"]["SPEAKER_03"]["profile_id"] == 43
    assert payload["matches"]["SPEAKER_03"]["profile_name"] == "Мужчина 1"


def test_dedupe_all_speaker_profiles_merges_by_name(db_engine):
    from sqlalchemy.orm import sessionmaker

    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.models.user import User

    Session = sessionmaker(bind=db_engine)
    db = Session()
    try:
        user = User(name="dedupe-user", telegram_id="tg-dedupe", role="user", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="p", description="", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(Artifact(
            artifact_id="art_x", project_id=project.id, user_id=user.id,
            original_filename="x.wav", stored_filename="x.wav", file_path="x.wav",
            file_type="audio", source_type="call", sha256="x" * 64,
        ))
        db.flush()

        keep = SpeakerProfile(
            scope="global",
            display_name="Елена Соколова",
            embedding=_vec(1.0, 0.0, 0.0),
            embedding_dim=3,
        )
        dup = SpeakerProfile(
            scope="global",
            display_name="Елена Соколова",
            embedding=_vec(0.0, 1.0, 0.0),
            embedding_dim=3,
        )
        db.add_all([keep, dup])
        db.flush()
        db.add(
            ArtifactSpeakerLink(
                artifact_id="art_x",
                project_id=project.id,
                diar_label="SPEAKER_01",
                profile_id=dup.id,
                status="confirmed",
            )
        )
        db.commit()
        dup_id = dup.id
        keep_id = keep.id

        result = dedupe_all_speaker_profiles(db)
        assert result["removed"] == 1
        survivor_id = dup_id  # профиль с link_count побеждает
        assert db.get(SpeakerProfile, keep_id) is None
        assert db.get(SpeakerProfile, survivor_id) is not None
        link = db.query(ArtifactSpeakerLink).filter_by(artifact_id="art_x").one()
        assert link.profile_id == survivor_id

        pool = load_universal_voice_profiles(db, auto_backfill=False)
        elena = [r for r in pool if r["display_name"] == "Елена Соколова"]
        assert len(elena) == 1
    finally:
        db.close()


def test_cleanup_voice_pool_merges_aliases_and_similar_voices(db_engine):
    from sqlalchemy.orm import sessionmaker

    from app.app.models.project import Project
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.models.user import User
    from app.app.services.speaker_profiles import (
        cleanup_voice_profile_pool,
        list_profiles,
        speaker_names_are_aliases,
    )

    assert speaker_names_are_aliases("Игорь (Пилот)", "Игорь Пилот")
    assert speaker_names_are_aliases("Директор Пилот", "Директор Пилота")
    assert not speaker_names_are_aliases("Олег", "Олег (Акме)")

    Session = sessionmaker(bind=db_engine)
    db = Session()
    try:
        user = User(name="cleanup-user", telegram_id="tg-cleanup", role="user", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="p", description="", created_by=user.id)
        db.add(project)
        db.flush()

        db.add_all(
            [
                SpeakerProfile(
                    scope="global",
                    display_name="Игорь (Пилот)",
                    embedding=_vec(1.0, 0.0, 0.0, 0.0),
                    embedding_dim=4,
                ),
                SpeakerProfile(
                    scope="project",
                    project_id=project.id,
                    display_name="Игорь Пилот",
                    embedding=_vec(0.99, 0.01, 0.0, 0.0),
                    embedding_dim=4,
                ),
                SpeakerProfile(
                    scope="global",
                    display_name="Мужчина 1",
                    embedding=_vec(0.0, 0.0, 0.0, 1.0),
                    embedding_dim=4,
                ),
                SpeakerProfile(
                    scope="global",
                    display_name="Павел (Пилот)",
                    embedding=_vec(0.0, 0.0, 0.0, 1.0),
                    embedding_dim=4,
                ),
                SpeakerProfile(
                    scope="global",
                    display_name="Олег",
                    embedding=_vec(0.0, 1.0, 0.0, 0.0),
                    embedding_dim=4,
                ),
                SpeakerProfile(
                    scope="global",
                    display_name="Олег (Акме)",
                    embedding=_vec(0.0, 0.0, 1.0, 0.0),
                    embedding_dim=4,
                ),
            ]
        )
        db.commit()

        result = cleanup_voice_profile_pool(db, refresh_views=False)
        assert result["alias_removed"] >= 1
        assert result["voice_removed"] >= 1

        names = {row.display_name for row in db.query(SpeakerProfile).all()}
        assert "Игорь Пилот" not in names or "Игорь (Пилот)" not in names
        assert "Мужчина 1" not in names
        assert "Павел (Пилот)" in names
        assert "Олег" in names and "Олег (Акме)" in names

        listed = list_profiles(db, project.id)
        listed_names = [row["display_name"] for row in listed]
        assert listed_names.count("Игорь (Пилот)") + listed_names.count("Игорь Пилот") == 1
    finally:
        db.close()


def test_lab_prev_payload_drops_speaker_xx():
    from app.app.services.speaker_profiles import _lab_prev_payload

    prev = {
        "source": "lab",
        "matches": {
            "SPEAKER_00": {"status": "new", "diar_label": "SPEAKER_00"},
            "speaker_0": {
                "status": "confirmed",
                "profile_id": 9,
                "profile_name": "Елена Соколова",
                "diar_label": "speaker_0",
            },
        },
        "merge_map": {
            "SPEAKER_00": "SPEAKER_00",
            "speaker_0": "speaker_0",
            "speaker_3": "speaker_2",
        },
        "embeddings": {"SPEAKER_00": [0.0], "speaker_0": [1.0, 0.0, 0.0]},
        "display_names": {"SPEAKER_00": "SPEAKER_00", "speaker_0": "Елена Соколова"},
    }
    out = _lab_prev_payload(prev)
    assert "SPEAKER_00" not in out["matches"]
    assert "SPEAKER_00" not in out["merge_map"]
    assert out["matches"]["speaker_0"]["profile_id"] == 9
    assert out["merge_map"]["speaker_3"] == "speaker_2"


def test_default_sortformer_json_stays_inside_artifact(tmp_data, db_engine):
    from app.app.config import get_settings
    from app.app.models.artifact import Artifact
    from app.app.services.artifact_audio import _default_sortformer_json

    get_settings.cache_clear()
    settings = get_settings()
    art = Artifact(
        artifact_id="art_no_sf",
        project_id=1,
        user_id=1,
        original_filename="call.webm",
        stored_filename="call.webm",
        file_path="x.webm",
        file_type="audio",
        source_type="call",
        sha256="a" * 64,
        status="speakers_pending",
    )
    path = _default_sortformer_json(settings, art)
    assert path is None or "experiments" not in path.as_posix()


def test_recalculate_lab_keeps_speaker_n_and_confirmed(tmp_data, db_engine, monkeypatch):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import speaker_profiles as sp
    from app.app.services.storage import artifact_dir
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    get_settings.cache_clear()
    settings = get_settings()
    db = SessionLocal()
    try:
        user = User(name="U", telegram_id="tg-recalc", role="user", password_hash="x")
        db.add(user)
        db.flush()
        db.add(
            sp.SpeakerProfile(
                id=7, scope="global", display_name="Елена Соколова",
                embedding=_vec(1.0, 0.0, 0.0), embedding_dim=3,
            )
        )
        db.flush()
        project = Project(name="lab recalc", created_by=user.id)
        db.add(project)
        db.flush()
        artifact_id = "art_lab_recalc"
        db.add(
            Artifact(
                artifact_id=artifact_id,
                project_id=project.id,
                user_id=user.id,
                original_filename="call.webm",
                stored_filename="call.webm",
                file_path="call.webm",
                file_type="audio",
                source_type="call",
                sha256="b" * 64,
                status="speakers_pending",
            )
        )
        db.commit()

        root = artifact_dir(settings, project.id, artifact_id)
        (root / "audio").mkdir(parents=True, exist_ok=True)
        wav = root / "audio" / "input_16k.wav"
        wav.write_bytes(b"RIFFfake")
        run = LabRun(root, new_run_id())
        init_run(run, artifact_id=artifact_id, project_id=project.id, source_audio=wav)
        lab_match = {
            "source": "lab",
            "run_id": run.run_id,
            "artifact_id": artifact_id,
            "project_id": project.id,
            "embeddings": {
                "speaker_0": [1.0, 0.0, 0.0],
                "speaker_1": [0.0, 1.0, 0.0],
            },
            "merge_map": {"speaker_0": "speaker_0", "speaker_1": "speaker_1"},
            "matches": {
                "speaker_0": {
                    "diar_label": "speaker_0",
                    "clip_start": 1.0,
                    "clip_end": 3.0,
                    "clip_duration": 2.0,
                    "status": "new",
                },
                "speaker_1": {
                    "diar_label": "speaker_1",
                    "clip_start": 4.0,
                    "clip_end": 6.0,
                    "clip_duration": 2.0,
                    "status": "new",
                },
            },
            "display_names": {"speaker_0": "speaker_0", "speaker_1": "speaker_1"},
        }
        voice = run.root / "voice"
        voice.mkdir(parents=True, exist_ok=True)
        (voice / "match.json").write_text(json.dumps(lab_match), encoding="utf-8")

        mixed = {
            "source": "sortformer",
            "matches": {
                "SPEAKER_00": {"diar_label": "SPEAKER_00", "status": "new"},
                "speaker_0": {
                    "diar_label": "speaker_0",
                    "status": "confirmed",
                    "profile_id": 7,
                    "profile_name": "Елена Соколова",
                },
                "speaker_1": {"diar_label": "speaker_1", "status": "new"},
            },
            "merge_map": {
                "SPEAKER_00": "SPEAKER_00",
                "speaker_0": "speaker_0",
                "speaker_1": "speaker_1",
            },
            "embeddings": {"SPEAKER_00": [0.0, 0.0, 1.0], "speaker_0": [1.0, 0.0, 0.0]},
            "display_names": {"speaker_0": "Елена Соколова"},
        }
        prod = root / "reports" / "sortformer_speaker_match.json"
        prod.parent.mkdir(parents=True, exist_ok=True)
        prod.write_text(json.dumps(mixed), encoding="utf-8")

        monkeypatch.setattr(
            sp,
            "match_json_path",
            lambda pid, aid: artifact_dir(get_settings(), pid, aid)
            / "reports"
            / "sortformer_speaker_match.json",
        )
        monkeypatch.setattr(sp, "rebuild_match_report_html", lambda *a, **k: None)
        monkeypatch.setattr(sp, "rebuild_transcript_report_html", lambda *a, **k: None)

        result = sp.recalculate_speaker_match(
            db, artifact_id=artifact_id, project_id=project.id
        )
        payload = json.loads(prod.read_text(encoding="utf-8"))
        assert "SPEAKER_00" not in payload["matches"]
        assert payload["source"] == "lab"
        assert payload["matches"]["speaker_0"]["status"] == "confirmed"
        assert payload["matches"]["speaker_0"]["profile_name"] == "Елена Соколова"
        assert "speaker_1" in payload["matches"]
        assert "SPEAKER_00" not in result["matches"]
    finally:
        db.close()
