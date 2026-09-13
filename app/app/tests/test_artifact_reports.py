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
        transcription_text="Текст спецификации для проверки страницы.",
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
    assert 'data-format="doc-summary-v2"' in body
    assert "Итоговое резюме ещё не собрано" in body
    assert 'id="doc-summary"' in body
    assert 'id="doc-source"' in body
    assert "class=\"summary\"" in body or "class='summary'" in body or 'class="summary"' in body
    assert "Текст спецификации для проверки страницы." in body
    assert "Полный текст" in body
    assert "Пересобрать выжимку" in body
    assert "Выжимка Word" in body
    assert "Артефакт:" not in body
    assert "нормализован" not in body.lower()
    assert "Согласовать архитектуру" not in body
    assert "Краткая итоговая выжимка" not in body


def test_ensure_document_report_uses_doc_summary_markdown(tmp_path):
    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path)
    artifact = Artifact(
        artifact_id="art_doc_3",
        project_id=7,
        user_id=1,
        original_filename="tz.docx",
        stored_filename="tz.docx",
        file_path=str(tmp_path / "tz.docx"),
        file_type="docx",
        source_type="document",
        sha256="d" * 64,
        status="done",
        transcription_text="Полный текст ТЗ, который не должен быть основным экраном.",
    )
    parsed = tmp_path / "projects" / "7" / "parsed" / "documents"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "art_doc_3_doc_summary.md").write_text(
        "## Суть документа\n\nПротокол по интеграции 1С.\n",
        encoding="utf-8",
    )
    report_path = ensure_document_report(settings, artifact)
    body = report_path.read_text(encoding="utf-8")
    assert "Протокол по интеграции 1С." in body
    assert "Итоговое резюме ещё не собрано" not in body
    assert "Полный текст ТЗ" in body
    source_at = body.index('id="doc-source"')
    summary_at = body.index('id="doc-summary"')
    assert summary_at < source_at
    assert "Артефакт:" not in body


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
