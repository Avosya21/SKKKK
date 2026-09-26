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
