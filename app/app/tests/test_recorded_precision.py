"""Точность даты материала «день / минута» (PLATFORM-FOUNDATION-PLAN, ПФ6)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.app.services import recorded_at as ra


@pytest.mark.parametrize("source,expected", [
    ("meeting", "minute"), ("media", "minute"), ("manual_time", "minute"), ("filename_time", "minute"),
    ("manual", "day"), ("filename", "day"), ("doc_text", "day"), ("doc_meta", "day"), ("upload", "day"), (None, None), ("", None),
])
def test_precision_follows_source(source, expected):
    assert ra.precision_for_source(source) == expected


def test_recorded_precision_prefers_stored_then_source_then_day():
    assert ra.recorded_precision(SimpleNamespace(recorded_at_precision="minute", recorded_at_source="manual")) == "minute"
    assert ra.recorded_precision(SimpleNamespace(recorded_at_precision=None, recorded_at_source="media")) == "minute"
    assert ra.recorded_precision(SimpleNamespace(recorded_at_precision="мусор", recorded_at_source="manual")) == "day"
    assert ra.recorded_precision(SimpleNamespace()) == "day"  # нет ни даты, ни источника


def test_set_recorded_writes_all_three_together():
    art = SimpleNamespace(recorded_at=None, recorded_at_source=None, recorded_at_precision=None)
    ra.set_recorded(art, datetime(2026, 8, 27, 12, 0), ra.SOURCE_MANUAL)
    assert (art.recorded_at_source, art.recorded_at_precision) == ("manual", "day")
    ra.set_recorded(art, datetime(2026, 8, 27, 9, 30), ra.SOURCE_MANUAL_TIME)
    assert (art.recorded_at_source, art.recorded_at_precision) == ("manual_time", "minute")
    ra.set_recorded(art, datetime(2026, 8, 27, 9, 30), ra.SOURCE_MESSAGES, precision="day")  # копия чата без времени
    assert art.recorded_at_precision == "day"


def test_refine_from_text_sets_day(tmp_path):
    art = SimpleNamespace(recorded_at=None, recorded_at_source=None, recorded_at_precision=None, file_type="docx", upload_date=datetime(2026, 9, 1))
    assert ra.refine_from_text(art, "Протокол от 19.08.2026\nТекст") is True
    assert art.recorded_at_source == ra.SOURCE_DOC_TEXT and art.recorded_at_precision == "day"


def test_migration_backfills_precision_from_source(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    import app.app.db as db_mod

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE artifacts (id INTEGER PRIMARY KEY, recorded_at_source TEXT)")
    con.executemany("INSERT INTO artifacts (id, recorded_at_source) VALUES (?, ?)",
                    [(1, "meeting"), (2, "manual"), (3, "upload"), (4, "messages"), (5, "filename_time"), (6, None)])
    con.commit()
    con.close()
    monkeypatch.setattr(db_mod, "engine", create_engine(f"sqlite:///{path.as_posix()}"))
    monkeypatch.setattr(db_mod.settings, "database_url", f"sqlite:///{path.as_posix()}")
    db_mod.apply_schema_migrations()
    db_mod.apply_schema_migrations()  # повтор безопасен
    got = dict(sqlite3.connect(path).execute("SELECT id, recorded_at_precision FROM artifacts").fetchall())
    assert got == {1: "minute", 2: "day", 3: "day", 4: "minute", 5: "minute", 6: None}


def test_api_returns_precision_and_page_marks_day_only(client, admin_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    user = db.query(User).first()
    project = Project(name="Точность", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    db.add(Artifact(artifact_id="art_prec_1", project_id=project.id, user_id=user.id, original_filename="созвон.webm",
                    stored_filename="x.webm", file_path="/x", file_type="audio", source_type="call", sha256="prec1", status="done",
                    upload_date=datetime(2026, 8, 27, 10, 0)))
    db.commit()
    pid = project.id
    db.close()
    h = {"Authorization": f"Bearer {admin_token}"}

    def row():
        return next(a for a in client.get(f"/projects/{pid}/artifacts", headers=h).json() if a["artifact_id"] == "art_prec_1")

    day = client.patch(f"/projects/{pid}/artifacts/art_prec_1/recorded-at", json={"date": "2026-08-26"}, headers=h).json()
    assert day["material_date_precision"] == "day" and row()["material_date_precision"] == "day"
    timed = client.patch(f"/projects/{pid}/artifacts/art_prec_1/recorded-at", json={"datetime_local": "2026-08-26T11:30"}, headers=h).json()
    assert timed["material_date_precision"] == "minute" and row()["material_date_precision"] == "minute"
    back = client.patch(f"/projects/{pid}/artifacts/art_prec_1/recorded-at", json={"date": None}, headers=h).json()
    assert back["material_date_source"] == "upload" and back["material_date_precision"] == "day"
    assert "только день" in client.get(f"/projects/{pid}", headers=h).text  # разметка для интерфейса есть на странице проекта


def test_day_only_report_lists_same_day_calls(client, admin_token, tmp_data):
    import importlib.util
    from pathlib import Path

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    user = db.query(User).first()
    project = Project(name="Отчёт", created_by=user.id, is_active=True)
    db.add(project)
    db.flush()
    for i, (name, source, at) in enumerate([("первый.webm", "manual", datetime(2026, 8, 27, 12, 0)),
                                             ("второй.webm", "manual", datetime(2026, 8, 27, 12, 0)),
                                             ("со временем.webm", "manual_time", datetime(2026, 8, 20, 7, 0))]):
        db.add(Artifact(artifact_id=f"art_rep_{i}", project_id=project.id, user_id=user.id, original_filename=name, stored_filename=f"{i}.webm",
                        file_path="/x", file_type="audio", source_type="call", sha256=f"rep{i}", status="done", recorded_at=at,
                        recorded_at_source=source, recorded_at_precision=ra.precision_for_source(source),
                        upload_date=datetime(2026, 8, 27, 10, 0), duration_sec=1800))
    db.commit()
    path = Path(__file__).resolve().parents[3] / "scripts" / "material_dates.py"
    spec = importlib.util.spec_from_file_location("material_dates_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    lines: list[str] = []
    assert script.day_only_report(db, project.id, out=lines.append) == 2  # «со временем» в отчёт не попадает
    text = "\n".join(lines)
    assert "2 созвона в один день" in text and "первый.webm" in text and "второй.webm" in text and "со временем" not in text
    assert "начало не позже 27.08.2026 12:30" in text  # загрузка 13:00 местного минус 30 минут
    db.close()
