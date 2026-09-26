from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.app.services.project_deadlines import (
    _parse_absolute_date,
    artifact_reference_date,
    parse_deadline_range,
    reference_is_explicit,
)


def _art(name: str, uploaded: str = "2026-09-18"):
    return SimpleNamespace(original_filename=name, upload_date=datetime.fromisoformat(uploaded).replace(tzinfo=timezone.utc))


def test_reference_from_filename_is_explicit():
    art = _art("Протокол 19.08.docx")
    assert reference_is_explicit(art) is True
    assert artifact_reference_date(art).isoformat() == "2026-08-19"


def test_reference_from_upload_date_is_not_explicit():
    art = _art("sandbox_protocol.txt")
    assert reference_is_explicit(art) is False
    assert artifact_reference_date(art).isoformat() == "2026-09-18"  # пятница загрузки


def test_relative_deadline_needs_reference_and_is_approximate_without_explicit_date():
    art = _art("sandbox_protocol.txt")
    start, _ = parse_deadline_range("до пятницы", reference=artifact_reference_date(art))
    assert start is not None and _parse_absolute_date("до пятницы") is None
    assert reference_is_explicit(art) is False  # значит date_approximate=True в панели
    assert parse_deadline_range("до пятницы", reference=None) == (None, None)


def test_explicit_date_is_never_approximate():
    assert _parse_absolute_date("к 2026-10-01").isoformat() == "2026-10-01"
