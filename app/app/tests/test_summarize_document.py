from pathlib import Path

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.services.summarize_document import (
    MAP_MAX_TOKENS,
    REDUCE_MAX_TOKENS,
    _parse_prompts,
    document_summary_path,
    load_document_source_text,
    run_document_summary,
)

ROOT = Path(__file__).resolve().parents[3]
PROMPT = ROOT / "docs" / "prompts" / "doc_summary.md"


def _artifact(tmp_path: Path) -> Artifact:
    return Artifact(
        artifact_id="art_doc_sum",
        project_id=3,
        user_id=1,
        original_filename="spec.docx",
        stored_filename="spec.docx",
        file_path=str(tmp_path / "spec.docx"),
        file_type="docx",
        source_type="document",
        sha256="c" * 64,
        status="indexed",
        transcription_text="Полный нормализованный текст документа про обмен.",
    )


def test_doc_summary_prompt_has_four_blocks():
    text = PROMPT.read_text(encoding="utf-8")
    blocks = text.split("```")
    assert len(blocks) >= 8
    map_sys, map_user, reduce_sys, reduce_user = _parse_prompts()
    assert "{chunk_text}" in map_user
    assert "{drafts}" in reduce_user
    assert "Суть документа" in reduce_sys
    assert "| --- | --- | --- |" in reduce_sys
    assert "пример ≠" in map_sys.lower() or "пример ≠" in map_sys or "пример" in map_sys
    assert MAP_MAX_TOKENS == 1200
    assert REDUCE_MAX_TOKENS == 4096


def test_load_document_source_prefers_clean_file(tmp_path):
    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path)
    artifact = _artifact(tmp_path)
    parsed = tmp_path / "projects" / "3" / "parsed" / "documents"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "art_doc_sum_clean.txt").write_text("Канон из clean.txt", encoding="utf-8")
    assert load_document_source_text(settings, artifact) == "Канон из clean.txt"


def test_run_document_summary_skips_dry_run(tmp_path):
    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path, dry_run=True)
    artifact = _artifact(tmp_path)
    assert run_document_summary(settings, artifact) is False
    assert not document_summary_path(settings, artifact).is_file()


def test_run_document_summary_skips_audio(tmp_path):
    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path, dry_run=False)
    artifact = _artifact(tmp_path)
    artifact.file_type = "audio"
    artifact.source_type = "call"
    assert run_document_summary(settings, artifact) is False


def test_run_document_summary_fail_open_on_llm_error(tmp_path, monkeypatch):
    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path, dry_run=False)
    artifact = _artifact(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.app.services.summarize_document.chat_completion_no_think", _boom)
    assert run_document_summary(settings, artifact) is False
    assert not document_summary_path(settings, artifact).is_file()


def test_run_document_summary_writes_markdown(tmp_path, monkeypatch):
    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path, dry_run=False)
    artifact = _artifact(tmp_path)
    monkeypatch.setattr(
        "app.app.services.summarize_document.chat_completion_no_think",
        lambda *_a, **_k: "## Суть документа\n\nДокумент про обмен.\n",
    )
    assert run_document_summary(settings, artifact) is True
    path = document_summary_path(settings, artifact)
    assert path.is_file()
    assert "Документ про обмен" in path.read_text(encoding="utf-8")


def test_resolve_document_source_and_summary_export(tmp_path, monkeypatch):
    from app.app.services.transcript_export import resolve_document_source_txt, resolve_summary_docx

    settings = Settings(secret_key="test-secret-key", data_dir=tmp_path, dry_run=False)
    artifact = _artifact(tmp_path)
    parsed = tmp_path / "projects" / "3" / "parsed" / "documents"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / "art_doc_sum_clean.txt").write_text("Канон для скачивания", encoding="utf-8")
    (parsed / "art_doc_sum_doc_summary.md").write_text("## Суть документа\n\nКратко.\n", encoding="utf-8")
    txt = resolve_document_source_txt(settings, artifact)
    assert txt is not None
    assert txt[0].startswith("Канон для скачивания")
    assert txt[1].endswith("_текст.txt")
    docx = resolve_summary_docx(settings, artifact)
    assert docx is not None
    assert docx[1].endswith("_выжимка.docx")
    assert len(docx[0]) > 100
