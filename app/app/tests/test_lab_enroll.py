"""Лабораторный enroll пишет voice/match.json и не трогает боевой match."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.app.services.speaker_profiles import enroll_speaker, merge_speakers


def _seed_artifact(artifact_id: str = "art_lab_enroll") -> tuple[int, str]:
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    try:
        user = db.query(User).first()
        assert user is not None
        project = Project(name="Лаборатория голоса", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(
            Artifact(
                artifact_id=artifact_id,
                project_id=project.id,
                user_id=user.id,
                original_filename="call.wav",
                stored_filename="call.wav",
                file_path="call.wav",
                file_type="audio",
                source_type="call",
                sha256="0" * 64,
                status="done",
            )
        )
        db.commit()
        return project.id, artifact_id
    finally:
        db.close()


def _make_run(tmp_data: Path, project_id: int, artifact_id: str):
    from app.app.config import get_settings
    from app.app.services.storage import artifact_dir
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    root = artifact_dir(get_settings(), project_id, artifact_id)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    source = root / "audio" / "input_16k.wav"
    source.write_bytes(b"RIFFfake")
    run = LabRun(root, new_run_id())
    init_run(run, artifact_id=artifact_id, project_id=project_id, source_audio=source)
    (run.stage_dir("01_audio") / "clean_16k.wav").write_bytes(b"RIFFclean")
    return run


def _lab_match(run, *, project_id: int, artifact_id: str) -> Path:
    payload = {
        "source": "lab",
        "run_id": run.run_id,
        "artifact_id": artifact_id,
        "project_id": project_id,
        "written_to_db": False,
        "embeddings": {
            "speaker_0": [1.0, 0.0, 0.0],
            "speaker_1": [0.0, 1.0, 0.0],
        },
        "merge_map": {"speaker_0": "speaker_0", "speaker_1": "speaker_1"},
        "matches": {
            "speaker_0": {
                "diar_label": "speaker_0",
                "clip_start": 1.0,
                "clip_end": 5.0,
                "clip_duration": 4.0,
                "status": "new",
            },
            "speaker_1": {
                "diar_label": "speaker_1",
                "clip_start": 10.0,
                "clip_end": 14.0,
                "clip_duration": 4.0,
                "status": "new",
            },
        },
        "display_names": {"speaker_0": "speaker_0", "speaker_1": "speaker_1"},
    }
    path = run.resolve("voice/match.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_lab_enroll_writes_lab_match_not_prod(
    client: TestClient, db_engine, tmp_data: Path, admin_token: str, monkeypatch
):
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.services import speaker_profiles as sp

    project_id, artifact_id = _seed_artifact()
    run = _make_run(tmp_data, project_id, artifact_id)
    lab_path = _lab_match(run, project_id=project_id, artifact_id=artifact_id)

    prod_path = tmp_data / "prod_sortformer_speaker_match.json"
    prod_path.write_text(
        json.dumps({"sentinel": True, "embeddings": {"SPEAKER_00": [0.0, 1.0, 0.0]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sp, "match_json_path", lambda *_args, **_kwargs: prod_path)

    db = SessionLocal()
    try:
        profile = enroll_speaker(
            db,
            project_id=project_id,
            artifact_id=artifact_id,
            diar_label="speaker_0",
            display_name="Елена Соколова",
            match_path=lab_path,
            rebuild_views=False,
        )
        assert profile.display_name == "Елена Соколова"
        link = (
            db.query(ArtifactSpeakerLink)
            .filter(
                ArtifactSpeakerLink.artifact_id == artifact_id,
                ArtifactSpeakerLink.diar_label == "speaker_0",
            )
            .one()
        )
        assert link.status == "confirmed"
        assert db.query(ArtifactSpeakerLink).filter(
            ArtifactSpeakerLink.artifact_id == artifact_id,
            ArtifactSpeakerLink.diar_label == "SPEAKER_00",
        ).count() == 0
        profiles = db.query(SpeakerProfile).filter(SpeakerProfile.display_name == "Елена Соколова").all()
        assert {p.scope for p in profiles} == {"global"}
    finally:
        db.close()

    prod = json.loads(prod_path.read_text(encoding="utf-8"))
    assert prod["sentinel"] is True
    assert "SPEAKER_00" in prod["embeddings"]

    lab = json.loads(lab_path.read_text(encoding="utf-8"))
    assert lab["source"] == "lab"
    assert lab["written_to_db"] is True
    assert lab["matches"]["speaker_0"]["status"] == "confirmed"
    assert lab["matches"]["speaker_0"]["profile_name"] == "Елена Соколова"
    assert lab["matches"]["speaker_1"]["status"] == "new"


def test_lab_merge_keeps_prod_json(
    client: TestClient, db_engine, tmp_data: Path, admin_token: str, monkeypatch
):
    from app.app.db import SessionLocal
    from app.app.services import speaker_profiles as sp

    project_id, artifact_id = _seed_artifact("art_lab_merge")
    run = _make_run(tmp_data, project_id, artifact_id)
    lab_path = _lab_match(run, project_id=project_id, artifact_id=artifact_id)
    prod_path = tmp_data / "prod_merge_match.json"
    prod_path.write_text(json.dumps({"sentinel": True}), encoding="utf-8")
    monkeypatch.setattr(sp, "match_json_path", lambda *_args, **_kwargs: prod_path)

    db = SessionLocal()
    try:
        enroll_speaker(
            db,
            project_id=project_id,
            artifact_id=artifact_id,
            diar_label="speaker_0",
            display_name="Елена Соколова",
            match_path=lab_path,
            rebuild_views=False,
        )
        result = merge_speakers(
            db,
            project_id=project_id,
            artifact_id=artifact_id,
            source_diar_label="speaker_1",
            target_diar_label="speaker_0",
            match_path=lab_path,
            rebuild_views=False,
        )
        assert result["display_name"] == "Елена Соколова"
    finally:
        db.close()

    assert json.loads(prod_path.read_text(encoding="utf-8"))["sentinel"] is True
    lab = json.loads(lab_path.read_text(encoding="utf-8"))
    assert lab["matches"]["speaker_1"]["status"] == "merged_duplicate"
    assert lab["merge_map"]["speaker_1"] == "speaker_0"
    assert lab["display_names"]["speaker_1"] == "Елена Соколова"


def test_lab_enroll_api_uses_run_match(client: TestClient, tmp_data: Path, admin_token: str, monkeypatch):
    from app.app.services import speaker_profiles as sp

    project_id, artifact_id = _seed_artifact("art_lab_api")
    run = _make_run(tmp_data, project_id, artifact_id)
    lab_path = _lab_match(run, project_id=project_id, artifact_id=artifact_id)
    prod_path = tmp_data / "prod_api_match.json"
    prod_path.write_text(json.dumps({"sentinel": True}), encoding="utf-8")
    monkeypatch.setattr(sp, "match_json_path", lambda *_args, **_kwargs: prod_path)

    missing = client.post(
        f"/api/artifacts/{artifact_id}/lab/{run.run_id}/speaker-enroll",
        json={"diar_label": "speaker_0", "display_name": "Иван"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # match.json already exists — 200. Separate 404 below.
    assert missing.status_code == 200, missing.text
    body = missing.json()
    assert body["display_name"] == "Иван"
    assert body["run_id"] == run.run_id
    assert json.loads(prod_path.read_text(encoding="utf-8"))["sentinel"] is True
    lab = json.loads(lab_path.read_text(encoding="utf-8"))
    assert lab["matches"]["speaker_0"]["status"] == "confirmed"


def test_lab_enroll_api_404_without_match(client: TestClient, tmp_data: Path, admin_token: str):
    _project_id, artifact_id = _seed_artifact("art_lab_nomatch")
    run = _make_run(tmp_data, _project_id, artifact_id)
    response = client.post(
        f"/api/artifacts/{artifact_id}/lab/{run.run_id}/speaker-enroll",
        json={"diar_label": "speaker_0", "display_name": "Иван"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 404


def test_lab_skips_voice_reaudit(client: TestClient, db_engine, tmp_data: Path, admin_token: str):
    from app.app.db import SessionLocal
    from app.app.services.speaker_profiles import reaudit_voices_and_reindex

    project_id, artifact_id = _seed_artifact("art_lab_skip_audit")
    _make_run(tmp_data, project_id, artifact_id)
    db = SessionLocal()
    try:
        result = reaudit_voices_and_reindex(db, project_id=project_id, artifact_id=artifact_id)
        assert result["ok"] is True
        assert result.get("skipped") == "lab"
        assert result.get("auto_n") == 0
    finally:
        db.close()
