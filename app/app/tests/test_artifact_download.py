from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import app.app.db as db_mod
from app.app.config import Settings, get_settings
from app.app.models.artifact import Artifact
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.artifact_download import artifact_download_available, resolve_artifact_download
from app.app.services.storage import artifact_audio_download_mp3_path, artifact_audio_path, raw_input_dir


def _seed_project(db, tmp_data) -> tuple[Project, User]:
    get_settings.cache_clear()
    settings = get_settings()
    user = User(name="U", telegram_id="tg-dl", role="user", password_hash="x")
    db.add(user)
    db.flush()
    project = Project(name="P", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    (settings.data_dir / "projects" / str(project.id)).mkdir(parents=True, exist_ok=True)
    return project, user


def test_resolve_audio_download_uses_mp3(tmp_data, db_engine):
    db = db_mod.SessionLocal()
    try:
        project, user = _seed_project(db, tmp_data)
        settings = get_settings()
        artifact_id = "art_dl_audio"
        wav = artifact_audio_path(settings, artifact_id, project.id)
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"x" * 5000)
        mp3 = artifact_audio_download_mp3_path(settings, artifact_id, project.id)
        mp3.write_bytes(b"ID3" + b"x" * 5000)

        art = Artifact(
            artifact_id=artifact_id,
            project_id=project.id,
            user_id=user.id,
            original_filename="GMT_recording.mp4",
            stored_filename="art.mp4",
            file_path=str(tmp_data / "gone.mp4"),
            file_type="audio",
            source_type="call",
            sha256="b" * 64,
            status="done",
        )
        db.add(art)
        db.commit()

        with patch(
            "app.app.services.artifact_download.ensure_audio_download_mp3",
            return_value=mp3,
        ):
            resolved = resolve_artifact_download(settings, art)
        assert resolved is not None
        path, media_type, name = resolved
        assert path == mp3
        assert media_type == "audio/mpeg"
        assert name == "GMT_recording.mp3"
        assert artifact_download_available(settings, art)
    finally:
        db.close()


def test_resolve_document_download_original(tmp_data, db_engine):
    db = db_mod.SessionLocal()
    try:
        project, user = _seed_project(db, tmp_data)
        settings = get_settings()
        doc_dir = raw_input_dir(settings, project.id, "document")
        doc_dir.mkdir(parents=True, exist_ok=True)
        doc = doc_dir / "art_spec.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")

        art = Artifact(
            artifact_id="art_dl_doc",
            project_id=project.id,
            user_id=user.id,
            original_filename="spec.pdf",
            stored_filename="art_spec.pdf",
            file_path=str(doc),
            file_type="pdf",
            source_type="document",
            sha256="c" * 64,
            status="done",
        )
        db.add(art)
        db.commit()

        resolved = resolve_artifact_download(settings, art)
        assert resolved is not None
        path, media_type, name = resolved
        assert path == doc
        assert name == "spec.pdf"
        assert media_type == "application/pdf"
    finally:
        db.close()


def test_download_endpoint_audio_mp3(client, tmp_data, db_engine, admin_token):
    db = db_mod.SessionLocal()
    try:
        project, user = _seed_project(db, tmp_data)
        settings = get_settings()
        artifact_id = "art_dl_http"
        mp3 = artifact_audio_download_mp3_path(settings, artifact_id, project.id)
        mp3.parent.mkdir(parents=True, exist_ok=True)
        payload = b"ID3" + b"x" * 5000
        mp3.write_bytes(payload)

        art = Artifact(
            artifact_id=artifact_id,
            project_id=project.id,
            user_id=user.id,
            original_filename="call.mkv",
            stored_filename="art.mkv",
            file_path=str(Path("missing.mkv")),
            file_type="audio",
            source_type="call",
            sha256="d" * 64,
            status="done",
        )
        db.add(art)
        db.commit()
    finally:
        db.close()

    with patch(
        "app.app.routers.pages.resolve_artifact_download",
        return_value=(mp3, "audio/mpeg", "call.mp3"),
    ):
        r = client.get(
            f"/artifacts/{artifact_id}/download",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert "attachment" in r.headers.get("content-disposition", "").lower()
    assert "call.mp3" in r.headers.get("content-disposition", "")
    assert r.content == payload


def test_download_requires_auth(client, tmp_data, db_engine):
    db = db_mod.SessionLocal()
    try:
        project, user = _seed_project(db, tmp_data)
        art = Artifact(
            artifact_id="art_dl_auth",
            project_id=project.id,
            user_id=user.id,
            original_filename="x.txt",
            stored_filename="x.txt",
            file_path=str(tmp_data / "x.txt"),
            file_type="txt",
            source_type="note",
            sha256="e" * 64,
            status="done",
        )
        db.add(art)
        db.commit()
    finally:
        db.close()

    r = client.get("/artifacts/art_dl_auth/download")
    assert r.status_code == 401


def test_download_available_does_not_extract_audio(tmp_data, monkeypatch):
    settings = Settings(secret_key="x" * 16, data_dir=tmp_data)
    src = tmp_data / "call.webm"
    src.write_bytes(b"video")
    art = MagicMock(
        file_type="audio",
        artifact_id="art_poll_no_wav",
        project_id=1,
        file_path=str(src),
    )

    def boom(*_a, **_k):
        raise AssertionError("listing must not extract wav")

    monkeypatch.setattr("app.app.services.artifact_audio.ensure_audio_wav", boom)
    monkeypatch.setattr("app.app.services.artifact_audio._extract_wav", boom)
    assert artifact_download_available(settings, art) is False
