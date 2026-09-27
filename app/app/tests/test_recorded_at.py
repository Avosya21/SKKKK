from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.app.services import recorded_at as ra


def test_meeting_start_wins():
    started = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    dt, source = ra.resolve_recorded_at(
        original_filename="созвон 19.08.webm", file_path=None, file_type="audio", meeting_started_at=started
    )
    assert source == ra.SOURCE_MEETING and dt == datetime(2026, 9, 21, 10, 0)


def test_filename_date_is_day_precision_noon_utc():
    dt, source = ra.resolve_recorded_at(
        original_filename="Встреча 19.08.2026.webm",
        file_path=None,
        file_type="audio",
        upload_date=datetime(2026, 9, 1),
    )
    assert source == ra.SOURCE_FILENAME and dt == datetime(2026, 8, 19, 12, 0)


def test_media_metadata_then_upload_fallback(monkeypatch, tmp_path):
    media = tmp_path / "call.mp4"
    media.write_bytes(b"x")
    monkeypatch.setattr(ra, "media_creation_time", lambda _p: datetime(2026, 9, 20, 7, 30))
    dt, source = ra.resolve_recorded_at(original_filename="call.mp4", file_path=media, file_type="audio")
    assert source == ra.SOURCE_MEDIA and dt == datetime(2026, 9, 20, 7, 30)
    monkeypatch.setattr(ra, "media_creation_time", lambda _p: None)
    uploaded = datetime(2026, 9, 21, 13, 52)
    dt, source = ra.resolve_recorded_at(
        original_filename="call.webm", file_path=Path("call.webm"), file_type="audio", upload_date=uploaded
    )
    assert source == ra.SOURCE_UPLOAD and dt == uploaded


def test_local_date_and_labels():
    art = SimpleNamespace(
        recorded_at=datetime(2026, 9, 20, 22, 30),  # UTC → 21.09 по Москве
        recorded_at_source=ra.SOURCE_MEETING,
        file_type="audio",
        original_filename="x.webm",
        upload_date=datetime(2026, 9, 22, 8, 0),
    )
    assert ra.recorded_local_date(art) == date(2026, 9, 21)
    assert ra.material_date_label(art) == "созвон 21.09.2026"
    art.recorded_at_source = ra.SOURCE_UPLOAD
    assert ra.recorded_local_date(art) is None
    assert ra.material_date_label(art) == "дата загрузки 22.09.2026 (дата созвона неизвестна)"


def test_old_artifact_without_recorded_at_keeps_old_reference():
    from app.app.services.project_deadlines import artifact_reference_date, reference_is_explicit

    art = SimpleNamespace(
        recorded_at=None, recorded_at_source=None, original_filename="созвон 19.08.webm", upload_date=datetime(2026, 9, 1)
    )
    assert artifact_reference_date(art) == date(2026, 8, 19)
    assert reference_is_explicit(art) is True
    art.original_filename = "созвон.webm"
    assert artifact_reference_date(art) == date(2026, 9, 1)
    assert reference_is_explicit(art) is False
    art.recorded_at, art.recorded_at_source = datetime(2026, 8, 30, 9, 0), ra.SOURCE_MEDIA
    assert artifact_reference_date(art) == date(2026, 8, 30)
    assert reference_is_explicit(art) is True


# --- REQUIREMENTS-PLAN этап 1: форматы дат в имени файла, ручная дата ---


import pytest


@pytest.mark.parametrize("name,expected", [
    ("ПИЛОТ встерча с директором 22_07_26.webm", date(2026, 7, 22)),
    ("созвон 22_07_2026.webm", date(2026, 7, 22)),
    ("созвон 22-07-2026.webm", date(2026, 7, 22)),
    ("созвон 2026-07-22.webm", date(2026, 7, 22)),
    ("созвон 22.07.26.webm", date(2026, 7, 22)),
    ("Встреча 19.08.2026.webm", date(2026, 8, 19)),
    ("созвон 19.08.webm", date(2026, 8, 19)),
    ("созвон 5.09.webm", date(2026, 9, 5)),
    ("Платформа 8.3 обзор.docx", None),
    ("1С 8.3.18 релиз.docx", None),
    ("отчёт v1_2.docx", None),
    ("созвон 31_02_26.webm", None),
])
def test_filename_date_formats(name, expected):
    assert ra.filename_date(name, datetime(2026, 9, 1)) == expected


def test_manual_date_is_explicit_and_wins():
    art = SimpleNamespace(recorded_at=ra.manual_recorded_at(date(2026, 7, 1)), recorded_at_source=ra.SOURCE_MANUAL,
                          file_type="audio", original_filename="созвон 19.08.2026.webm", upload_date=datetime(2026, 9, 1))
    assert ra.material_date(art) == (date(2026, 7, 1), ra.SOURCE_MANUAL)
    art.recorded_at_source, art.recorded_at = ra.SOURCE_UPLOAD, datetime(2026, 9, 1)
    assert ra.material_date(art) == (date(2026, 8, 19), ra.SOURCE_FILENAME)
    art.original_filename = "созвон.webm"
    assert ra.material_date(art) == (None, ra.SOURCE_UPLOAD)


def test_set_recorded_at_api(client, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    user = db.query(User).first()
    project = Project(name="Dates", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    db.add(Artifact(artifact_id="art_dates_1", project_id=project.id, user_id=user.id, original_filename="созвон.webm",
                    stored_filename="x.webm", file_path="/x", file_type="audio", source_type="call", sha256="dates1", status="done",
                    upload_date=datetime(2026, 8, 27, 10, 0)))
    db.commit()
    pid = project.id
    db.close()
    headers = {"Authorization": f"Bearer {admin_token}"}
    row = next(a for a in client.get(f"/projects/{pid}/artifacts", headers=headers).json() if a["artifact_id"] == "art_dates_1")
    assert row["material_date"] is None and row["material_date_source"] == "upload"
    r = client.patch(f"/projects/{pid}/artifacts/art_dates_1/recorded-at", json={"date": "2026-07-22"}, headers=headers)
    assert r.status_code == 200 and r.json()["material_date"] == "2026-07-22" and r.json()["material_date_source"] == "manual"
    row = next(a for a in client.get(f"/projects/{pid}/artifacts", headers=headers).json() if a["artifact_id"] == "art_dates_1")
    assert row["material_date"] == "2026-07-22"
    r = client.patch(f"/projects/{pid}/artifacts/art_dates_1/recorded-at", json={"date": None}, headers=headers)
    assert r.json()["material_date"] is None and r.json()["material_date_source"] == "upload"