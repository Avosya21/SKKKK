from __future__ import annotations

import json

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.services.artifact_reports import document_report_url, ensure_document_report


def test_ensure_document_report_builds_html_from_entities_file(tmp_path):
    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path)
    artifact = Artifact(
        artifact_id="art_doc_1",
        project_id=7,
        user_id=1,
        original_filename="spec.docx",
        stored_filename="spec.docx",
        file_path=str(tmp_path / "spec.docx"),
        file_type="docx",
        source_type="document",
        sha256="a" * 64,
        status="done",
        transcription_text="Нормализованный текст документа.",
    )

    structured = tmp_path / "projects" / "7" / "structured"
    structured.mkdir(parents=True, exist_ok=True)
    (structured / "art_doc_1_entities.json").write_text(
        json.dumps(
            {
                "artifact_id": "art_doc_1",
                "source_type": "document",
                "summary": "Краткая итоговая выжимка",
                "decisions": ["Согласовать архитектуру"],
                "tasks": [{"description": "Подготовить план", "assignee": "Аналитик", "deadline": None, "status": "open"}],
                "facts": {"Система": "1С + Битрикс24"},
                "mentions": [],
                "participants": [{"name": "Иван", "role": "аналитик"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report_path = ensure_document_report(settings, artifact)

    body = report_path.read_text(encoding="utf-8")
    assert report_path.is_file()
    assert "Краткая итоговая выжимка" in body
    assert "Согласовать архитектуру" in body
    assert "1С + Битрикс24" in body
    assert "Нормализованный текст документа." in body


def test_document_report_url_uses_artifact_id():
    artifact = Artifact(
        artifact_id="art_doc_2",
        project_id=1,
        user_id=1,
        original_filename="note.txt",
        stored_filename="note.txt",
        file_path="note.txt",
        file_type="txt",
        source_type="document",
        sha256="b" * 64,
    )
    assert document_report_url(artifact) == "/artifacts/art_doc_2/report"
