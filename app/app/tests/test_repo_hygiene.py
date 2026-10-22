"""Проверки гигиены репозитория: БД и данные проектов не попадают в git."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location("check_no_data_files", ROOT / "scripts" / "check_no_data_files.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_no_data_files"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_data_and_documents_are_forbidden():
    mod = _load()
    for path in (
        "data/app.db",
        "data/projects/1/raw/meeting.m4a",
        "private/Companies.txt",
        "onec/base.1cd",
        "Companies.txt",
        "Login_up.txt",
        "Panel_users.txt",
        ".env",
        "report.pdf",
        "notes.docx",
        "model.safetensors",
        "meeting.wav",
    ):
        assert mod.reason(path), path


def test_fixtures_and_examples_are_allowed():
    mod = _load()
    for path in (
        "app/app/tests/fixtures/meeting/baseline_track.wav",
        "scripts/qa_data_speech_a.wav",
        ".env.example",
        "tools/lightrag/.env.example",
        "asr/whisper.env",
        "app/app/services/project_purge.py",
        "app/app/static/brand/favicon.ico",
    ):
        assert mod.reason(path) is None, path


def test_tracked_tree_has_no_project_data():
    mod = _load()
    assert mod.violations(False) == []


def test_gitignore_covers_working_data():
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for pattern in ("data/", "private/", "*.db", "*.sqlite3", "*.m4a", "*.mp4", "*.pdf"):
        assert pattern in lines, pattern
