from __future__ import annotations

from pathlib import Path

import app.app.db as db_mod
from app.app.config import get_settings
from app.app.models.artifact import Artifact
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.artifact_audio import ensure_audio_playback
from app.app.services.storage import artifact_audio_path, artifact_audio_playback_path


def _seed_audio_artifact(*, artifact_id: str, telegram_id: str) -> tuple[str, int]:
    settings = get_settings()
    db = db_mod.SessionLocal()
    try:
        user = User(name="U", telegram_id=telegram_id, role="user", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="P", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()

        wav = artifact_audio_path(settings, artifact_id, project.id)
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28)

        art = Artifact(
            artifact_id=artifact_id,
            project_id=project.id,
            user_id=user.id,
            original_filename="call.wav",
            stored_filename="call.wav",
            file_path=str(wav),
            file_type="audio",
            source_type="call",
            sha256="a" * 64,
            status="ready",
        )
        db.add(art)
        db.commit()
        return artifact_id, project.id
    finally:
        db.close()


def test_ensure_audio_playback_builds_m4a(tmp_data, db_engine, monkeypatch):
    artifact_id, project_id = _seed_audio_artifact(
        artifact_id="art_play_1", telegram_id="tg-play-1"
    )
    settings = get_settings()

    monkeypatch.setattr(
        "app.app.services.artifact_audio._encode_playback_aac",
        lambda src, dst: dst.write_bytes(b"fake-aac"),
    )

    db = db_mod.SessionLocal()
    try:
        art = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).one()
        result = ensure_audio_playback(settings, art)
    finally:
        db.close()

    assert result is not None
    path, media_type = result
    assert media_type == "audio/mp4"
    assert path == artifact_audio_playback_path(settings, artifact_id, project_id)
    assert path.read_bytes() == b"fake-aac"


def test_ensure_audio_playback_falls_back_to_wav(tmp_data, db_engine, monkeypatch):
    artifact_id, project_id = _seed_audio_artifact(
        artifact_id="art_play_wav", telegram_id="tg-play-wav"
    )
    settings = get_settings()

    def boom(src: Path, dst: Path) -> None:
        raise OSError("no aac")

    monkeypatch.setattr("app.app.services.artifact_audio._encode_playback_aac", boom)

    db = db_mod.SessionLocal()
    try:
        art = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).one()
        result = ensure_audio_playback(settings, art)
    finally:
        db.close()

    assert result is not None
    path, media_type = result
    assert media_type == "audio/wav"
    assert path == artifact_audio_path(settings, artifact_id, project_id)


def test_audio_endpoint_serves_inline_aac(client, tmp_data, db_engine, monkeypatch, admin_token):
    artifact_id, project_id = _seed_audio_artifact(
        artifact_id="art_play_http", telegram_id="tg-play-http"
    )
    settings = get_settings()
    out = artifact_audio_playback_path(settings, artifact_id, project_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake-aac-http")

    monkeypatch.setattr(
        "app.app.routers.pages.ensure_audio_playback",
        lambda _settings, artifact: (
            artifact_audio_playback_path(_settings, artifact.artifact_id, artifact.project_id),
            "audio/mp4",
        ),
    )

    r = client.get(f"/artifacts/{artifact_id}/audio")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/mp4")
    disp = r.headers.get("content-disposition", "")
    assert "inline" in disp.lower()
    assert "attachment" not in disp.lower()
    assert r.content == b"fake-aac-http"


def test_audio_endpoint_format_wav(client, tmp_data, db_engine, monkeypatch, admin_token):
    artifact_id, _project_id = _seed_audio_artifact(
        artifact_id="art_play_wav_q", telegram_id="tg-play-wav-q"
    )
    monkeypatch.setattr(
        "app.app.routers.pages.ensure_audio_playback",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not encode")),
    )

    r = client.get(f"/artifacts/{artifact_id}/audio", params={"format": "wav"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/wav")
    assert "inline" in r.headers.get("content-disposition", "").lower()


def test_audio_clip_query_serves_this_artifact_not_shared_work(
    client, tmp_data, db_engine, monkeypatch, admin_token
):
    artifact_id, project_id = _seed_audio_artifact(
        artifact_id="art_clip_own", telegram_id="tg-clip-own"
    )
    playback = artifact_audio_playback_path(get_settings(), artifact_id, project_id)
    playback.parent.mkdir(parents=True, exist_ok=True)
    own = b"OWN-ARTIFACT-AUDIO"
    playback.write_bytes(own)
    monkeypatch.setattr(
        "app.app.routers.pages.ensure_audio_playback",
        lambda _settings, artifact: (
            artifact_audio_playback_path(_settings, artifact.artifact_id, artifact.project_id),
            "audio/mp4",
        ),
    )

    with_clip = client.get(f"/artifacts/{artifact_id}/audio", params={"clip": 240})
    without = client.get(f"/artifacts/{artifact_id}/audio")
    assert with_clip.status_code == 200, with_clip.text
    assert without.status_code == 200
    assert with_clip.content == own
    assert with_clip.content == without.content

