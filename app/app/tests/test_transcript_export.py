from __future__ import annotations

import app.app.db as db_mod
from app.app.config import get_settings
from app.app.models.artifact import Artifact
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.transcript_export import (
    build_summary_docx_bytes,
    resolve_summary_docx,
    resolve_transcript_txt,
    summary_export_available,
    transcript_export_available,
)


def _seed_audio(db, tmp_data, artifact_id: str = "art_export") -> tuple[Artifact, Settings]:
    settings = get_settings()
    user = User(name="U", telegram_id="tg-exp", role="user", password_hash="x")
    db.add(user)
    db.flush()
    project = Project(name="P", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    from app.app.services.storage import artifact_dir

    ws = artifact_dir(settings, project.id, artifact_id)
    asr = ws / "asr"
    asr.mkdir(parents=True, exist_ok=True)
    (asr / "docker_gigaam_speakers.json").write_text(
        """
        {"turns": [
          {"start": 0, "end": 5.2, "speaker": "Иван", "text": "Привет"},
          {"start": 5.2, "end": 10.0, "speaker": "Елена", "text": "Здравствуйте"}
        ]}
        """.strip(),
        encoding="utf-8",
    )
    (asr / "call_summary.md").write_text("## Итог\n\n- **Пункт один**", encoding="utf-8")
    art = Artifact(
        artifact_id=artifact_id,
        project_id=project.id,
        user_id=user.id,
        original_filename="call.mp4",
        stored_filename="call.mp4",
        file_path=str(tmp_data / "call.mp4"),
        file_type="audio",
        source_type="call",
        sha256="f" * 64,
        status="done",
    )
    db.add(art)
    db.commit()
    return art, settings


def test_resolve_transcript_txt(tmp_data, db_engine):
    db = db_mod.SessionLocal()
    try:
        art, settings = _seed_audio(db, tmp_data)
        resolved = resolve_transcript_txt(settings, art)
        assert resolved is not None
        body, name = resolved
        assert name == "call_стенограмма.txt"
        assert "[00:00 - 00:05] Иван: Привет" in body
        assert transcript_export_available(settings, art)
    finally:
        db.close()


def test_resolve_summary_docx(tmp_data, db_engine):
    db = db_mod.SessionLocal()
    try:
        art, settings = _seed_audio(db, tmp_data)
        resolved = resolve_summary_docx(settings, art)
        assert resolved is not None
        body, name = resolved
        assert name == "call_выжимка.docx"
        assert body[:2] == b"PK"
        assert summary_export_available(settings, art)
        docx2 = build_summary_docx_bytes("## Раздел\n\nОбычный и **жирный**", art)
        assert docx2.startswith(b"PK")
        table_docx = build_summary_docx_bytes(
            "## Кто / Что\n\n| Кто | Что |\n|-----|-----|\n| А | Б |\n",
            art,
        )
        from docx import Document
        import io

        parsed = Document(io.BytesIO(table_docx))
        assert len(parsed.tables) == 1
        assert parsed.tables[0].cell(0, 0).text.strip() == "Кто"
        assert parsed.tables[0].cell(1, 1).text.strip() == "Б"
    finally:
        db.close()


def test_transcript_download_endpoint(client, tmp_data, db_engine, user_token):
    db = db_mod.SessionLocal()
    try:
        _seed_audio(db, tmp_data, "art_export_http")
    finally:
        db.close()
    r = client.get(
        "/artifacts/art_export_http/download/transcript.txt",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert "Иван: Привет" in r.text


def test_summary_download_endpoint(client, tmp_data, db_engine, user_token):
    db = db_mod.SessionLocal()
    try:
        _seed_audio(db, tmp_data, "art_export_doc")
    finally:
        db.close()
    r = client.get(
        "/artifacts/art_export_doc/download/summary.docx",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert "wordprocessingml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"
