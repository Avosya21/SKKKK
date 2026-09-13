from __future__ import annotations

import logging
from pathlib import Path

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.llm import chat_completion
from app.app.services.plain_text import clean_summary_output
from app.app.services.storage import parsed_dir
from app.app.services.tz import MAP_CHUNK_CHARS, _pack_map_chunks

logger = logging.getLogger("skkkk.doc_summary")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "doc_summary.md"
MAP_MAX_TOKENS = 1200
REDUCE_MAX_TOKENS = 4096


def document_summary_path(settings: Settings, artifact: Artifact) -> Path:
    out_dir = parsed_dir(settings, artifact.project_id, artifact.source_type)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{artifact.artifact_id}_doc_summary.md"


def load_document_source_text(settings: Settings, artifact: Artifact) -> str:
    """Канон для выжимки документа: ``*_clean.txt``, иначе transcription_text."""
    clean_path = parsed_dir(settings, artifact.project_id, artifact.source_type) / (
        f"{artifact.artifact_id}_clean.txt"
    )
    if clean_path.is_file():
        body = clean_path.read_text(encoding="utf-8").strip()
        if body:
            return body
    stored = getattr(artifact, "transcription_file_path", None)
    if stored:
        path = Path(stored)
        if path.is_file():
            body = path.read_text(encoding="utf-8").strip()
            if body:
                return body
    return (getattr(artifact, "transcription_text", None) or "").strip()


def load_document_summary_md(settings: Settings, artifact: Artifact) -> str:
    path = document_summary_path(settings, artifact)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _parse_prompts() -> tuple[str, str, str, str]:
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    blocks = raw.split("```")
    if len(blocks) < 8:
        raise RuntimeError(f"{PROMPT_PATH}: expected 4 prompt blocks")
    return tuple(blocks[i].strip() for i in (1, 3, 5, 7))  # type: ignore[return-value]


def _build_summary_body(settings: Settings, *, source_text: str, title: str) -> str:
    map_sys, map_user_tmpl, reduce_sys, reduce_user_tmpl = _parse_prompts()
    map_chunks = _pack_map_chunks([(title, source_text)], max_chars=MAP_CHUNK_CHARS)
    if not map_chunks:
        return ""
    if len(map_chunks) == 1:
        drafts = map_chunks
    else:
        drafts = []
        for i, chunk in enumerate(map_chunks):
            user = map_user_tmpl.format(i=i + 1, n=len(map_chunks), chunk_text=chunk)
            log_event(logger, "doc_summary.map", chunk=i + 1, total=len(map_chunks), chars=len(chunk))
            drafts.append(
                chat_completion(settings, system=map_sys, user=user, max_tokens=MAP_MAX_TOKENS, temperature=0.2)
            )
    reduce_user = reduce_user_tmpl.format(drafts="\n\n---\n\n".join(drafts))
    log_event(logger, "doc_summary.reduce", drafts=len(drafts), chars=len(source_text))
    return chat_completion(
        settings, system=reduce_sys, user=reduce_user, max_tokens=REDUCE_MAX_TOKENS, temperature=0.2
    )


def run_document_summary(settings: Settings, artifact: Artifact) -> bool:
    """Собрать ``{id}_doc_summary.md``. False при пустом тексте или сбое LLM — индекс не откатываем."""
    if artifact.file_type == "audio":
        return False
    if settings.dry_run:
        log_event(logger, "doc_summary.skipped", artifact=artifact.artifact_id, reason="dry_run")
        return False
    source = load_document_source_text(settings, artifact)
    if not source:
        log_event(logger, "doc_summary.skipped", artifact=artifact.artifact_id, reason="empty_text")
        return False
    title = artifact.original_filename or artifact.artifact_id
    try:
        body = _build_summary_body(settings, source_text=source, title=title)
    except Exception as exc:
        log_event(
            logger,
            "doc_summary.failed",
            artifact=artifact.artifact_id,
            error=str(exc)[:500],
        )
        return False
    body = clean_summary_output(body).strip()
    if not body:
        log_event(logger, "doc_summary.failed", artifact=artifact.artifact_id, error="empty_llm_output")
        return False
    path = document_summary_path(settings, artifact)
    path.write_text(body + "\n", encoding="utf-8")
    log_event(
        logger,
        "doc_summary.ok",
        artifact=artifact.artifact_id,
        chars=len(body),
        file=path.name,
    )
    return True
