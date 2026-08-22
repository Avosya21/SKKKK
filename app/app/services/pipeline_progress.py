"""Пошаговый прогресс обработки артефакта для UI (чеклист ASR + фазы knowledge)."""

from __future__ import annotations

import json
from pathlib import Path

from app.app.config import Settings
from app.app.llm_labels import asr_step_labels, knowledge_steps_audio, knowledge_steps_doc
from app.app.models.artifact import Artifact
from app.app.schemas.artifact import ArtifactProgressOut, PipelineStepOut
from app.app.services.storage import ensure_artifact_workspace

StepStatus = str

POST_ASR_STEPS: list[tuple[str, str]] = [
    ("speakers_match", "Match Sortformer → профили спикеров"),
    ("speakers_view", "HTML с именами спикеров"),
    ("voice_audit", "Voice audit (проверка голосов)"),
    ("transcript_finalize", "Финализация стенограммы"),
]

def _knowledge_steps_audio(settings: Settings) -> list[tuple[str, str]]:
    return knowledge_steps_audio(settings.llm_provider)


def _knowledge_steps_doc(settings: Settings) -> list[tuple[str, str]]:
    return knowledge_steps_doc(settings.llm_provider)


def _read_asr_checklist(asr_dir: Path) -> dict | None:
    path = asr_dir / "pipeline_checklist.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _map_checklist_status(raw: str | None) -> StepStatus:
    if raw == "ok":
        return "ok"
    if raw == "running":
        return "running"
    if raw == "fail":
        return "fail"
    return "pending"


def _steps_from_template(
    template: list[tuple[str, str]],
    *,
    active_idx: int,
    running_idx: int | None = None,
) -> list[PipelineStepOut]:
    steps: list[PipelineStepOut] = []
    for i, (step_id, label) in enumerate(template):
        if i < active_idx:
            st = "ok"
        elif running_idx is not None and i == running_idx:
            st = "running"
        elif i == active_idx:
            st = "running"
        else:
            st = "pending"
        steps.append(PipelineStepOut(id=step_id, label=label, status=st))
    return steps


def _has_call_summary(ws: Path) -> bool:
    for rel in ("reports/call_summary.md", "asr/call_summary.md"):
        p = ws / rel
        if p.is_file() and p.read_text(encoding="utf-8").strip():
            return True
    return False


def reconcile_artifact_status(db, settings: Settings, artifact: Artifact) -> bool:
    """Подтянуть status в БД, если knowledge уже завершён, а статус откатился (rerun merge и т.п.)."""
    if artifact.status in {"done", "error", "uploaded", "preprocessing", "asr_running", "asr_completed"}:
        return False

    from sqlalchemy import func, select

    from app.app.models.entity import Chunk, Entity
    from app.app.services.status import set_status

    chunk_n = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.artifact_id == artifact.id)) or 0
    entity_n = db.scalar(select(func.count()).select_from(Entity).where(Entity.artifact_id == artifact.id)) or 0
    if entity_n == 0:
        return False

    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)

    if chunk_n > 0:
        if artifact.file_type == "audio" and not _has_call_summary(ws):
            target = "indexed"
        else:
            target = "done"
    else:
        target = "entities_extracted"

    rank = {
        "transcript_ready": 0,
        "entities_extracted": 1,
        "indexed": 2,
        "graph_seeded": 3,
        "done": 4,
    }
    if rank.get(target, 0) <= rank.get(artifact.status, 0):
        return False

    set_status(db, artifact, target)
    if target == "done" and artifact.file_type == "audio":
        from app.app.services.storage import artifact_report_url

        artifact.transcript_html_url = artifact_report_url(artifact.artifact_id)
        db.commit()
    return True


def build_artifact_progress(settings: Settings, artifact: Artifact) -> ArtifactProgressOut | None:
    if artifact.status in {"done", "error"}:
        return None

    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)

    if artifact.status == "uploaded":
        return ArtifactProgressOut(
            phase="queue",
            current_label="В очереди GPU-воркера",
            steps=[PipelineStepOut(id="queue", label="Ожидание воркера", status="running")],
        )

    if artifact.file_type == "audio":
        return _audio_progress(settings, artifact, ws)

    return _document_progress(settings, artifact)


def _audio_progress(settings: Settings, artifact: Artifact, ws: Path) -> ArtifactProgressOut:
    status = artifact.status
    checklist = _read_asr_checklist(ws / "asr")
    steps: list[PipelineStepOut] = []

    if status in {"preprocessing", "asr_running"} and checklist:
        step_labels = asr_step_labels(settings.llm_provider)
        for step_id, label in step_labels.items():
            row = (checklist.get("steps") or {}).get(step_id) or {}
            raw = row.get("status")
            detail = row.get("detail") or row.get("reason")
            if raw == "ok" and isinstance(detail, str) and "WARN" in detail.upper():
                mapped = "warn"
            else:
                mapped = _map_checklist_status(raw)
            steps.append(PipelineStepOut(id=step_id, label=label, status=mapped, detail=detail))
        current = next((s for s in steps if s.status == "running"), None)
        if current is None:
            pending = next((s for s in steps if s.status == "pending"), None)
            current_label = pending.label if pending else "ASR-пайплайн"
        else:
            current_label = current.detail or current.label
        return ArtifactProgressOut(phase="asr", current_label=current_label, steps=steps)

    if status in {"preprocessing", "asr_running"}:
        return ArtifactProgressOut(
            phase="asr",
            current_label="ASR-пайплайн (старт…)",
            steps=[
                PipelineStepOut(id="asr_boot", label="Запуск ASR-пайплайна", status="running"),
            ],
        )

    if status == "asr_completed":
        steps = _steps_from_template(POST_ASR_STEPS, active_idx=0, running_idx=0)
        return ArtifactProgressOut(
            phase="speakers",
            current_label="Спикеры и финальная стенограмма",
            steps=steps,
        )

    template = _knowledge_steps_audio(settings)
    running_idx = 0
    active_idx = 0

    if status == "transcript_ready":
        running_idx = 0
        active_idx = 0
    elif status == "entities_extracted":
        running_idx = 1
        active_idx = 1
    elif status == "indexed":
        if _has_call_summary(ws):
            running_idx = 3
            active_idx = 3
        else:
            running_idx = 2
            active_idx = 2
    elif status == "graph_seeded":
        running_idx = 4
        active_idx = 4

    steps = _steps_from_template(template, active_idx=active_idx, running_idx=running_idx)
    current = next((s for s in steps if s.status == "running"), steps[0] if steps else None)
    return ArtifactProgressOut(
        phase="knowledge",
        current_label=current.label if current else "Knowledge pipeline",
        steps=steps,
    )


def _document_progress(settings: Settings, artifact: Artifact) -> ArtifactProgressOut:
    template = _knowledge_steps_doc(settings)
    running_idx = 0
    active_idx = 0

    if artifact.status == "preprocessing":
        return ArtifactProgressOut(
            phase="text",
            current_label="Извлечение текста",
            steps=[PipelineStepOut(id="preprocess", label="Подготовка документа", status="running")],
        )
    if artifact.status == "text_extracted":
        running_idx = 0
        active_idx = 0
    elif artifact.status == "entities_extracted":
        running_idx = 1
        active_idx = 1
    elif artifact.status == "indexed":
        running_idx = 2
        active_idx = 2
    elif artifact.status == "graph_seeded":
        running_idx = 3
        active_idx = 3

    steps = _steps_from_template(template, active_idx=active_idx, running_idx=running_idx)
    current = next((s for s in steps if s.status == "running"), None)
    return ArtifactProgressOut(
        phase="knowledge",
        current_label=current.label if current else artifact.status,
        steps=steps,
    )
