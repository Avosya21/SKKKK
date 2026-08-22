from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.orm import Session

from app.app.config import ROOT, Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.models.artifact import Artifact
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.artifact_audio import (
    ensure_audio_playback,
    ensure_audio_report,
    ensure_audio_wav,
    ensure_raw_diar_report,
    ensure_sortformer_report,
    ensure_speaker_match_report,
    ensure_two_voice_diar_report,
)
from app.app.services.artifact_download import resolve_artifact_download
from app.app.services.artifact_reports import ensure_document_report
from app.app.services.report_embed import apply_embed_mode
from app.app.services.transcript_export import resolve_summary_docx, resolve_transcript_txt
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _attachment_headers(filename: str) -> dict[str, str]:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download"
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
        )
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"title": "Вход"})


@router.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(request, "index.html", {"title": "Проекты"})


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(
    request: Request,
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    title = f"Проект {project_id} — {project.name}"
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "title": title,
            "project_id": project_id,
            "project_name": project.name,
        },
    )


@router.get("/projects/{project_id}/graph", response_class=HTMLResponse)
def graph_page(request: Request, project_id: int):
    return templates.TemplateResponse(
        request,
        "graph.html",
        {"title": f"Граф {project_id}", "project_id": project_id},
    )


@router.get("/projects/{project_id}/tz", response_class=HTMLResponse)
def tz_page(request: Request, project_id: int):
    return templates.TemplateResponse(
        request,
        "tz.html",
        {"title": f"ТЗ {project_id}", "project_id": project_id},
    )


@router.get("/projects/{project_id}/chat", response_class=HTMLResponse)
def chat_page(request: Request, project_id: int):
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"title": f"Чат {project_id}", "project_id": project_id},
    )


@router.get("/admin/conflicts", response_class=HTMLResponse)
def conflicts_page(request: Request):
    return templates.TemplateResponse(request, "conflicts.html", {"title": "Конфликты"})


def _inline_audio_response(path: Path, media_type: str, download_name: str) -> FileResponse:
    # filename alone defaults to Content-Disposition: attachment — mobile browsers
    # then refuse to stream into <audio>. Keep a name for Save-As, but play inline.
    return FileResponse(
        path,
        media_type=media_type,
        filename=download_name,
        content_disposition_type="inline",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/artifacts/{artifact_id}/audio")
def artifact_audio_file(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    clip: Annotated[float | None, Query()] = None,
    fmt: Annotated[str | None, Query(alias="format")] = None,
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact or artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Audio artifact not found")
    if clip is not None and clip > 0:
        clip_path = ROOT / "asr" / "work" / "input_16k_4min.wav"
        if clip_path.is_file() and clip <= 240:
            return _inline_audio_response(
                clip_path,
                "audio/wav",
                f"{artifact.artifact_id}_4min.wav",
            )
    want_wav = (fmt or "").lower() in {"wav", "pcm"}
    if want_wav:
        path = ensure_audio_wav(settings, artifact)
        if not path:
            raise HTTPException(status_code=404, detail="Audio file not found")
        return _inline_audio_response(path, "audio/wav", f"{artifact.artifact_id}.wav")

    playback = ensure_audio_playback(settings, artifact)
    if not playback:
        raise HTTPException(status_code=404, detail="Audio file not found")
    path, media_type = playback
    suffix = ".m4a" if media_type == "audio/mp4" else ".wav"
    return _inline_audio_response(path, media_type, f"{artifact.artifact_id}{suffix}")


@router.get("/artifacts/{artifact_id}/download")
def artifact_download_file(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    resolved = resolve_artifact_download(settings, artifact)
    if not resolved:
        raise HTTPException(status_code=404, detail="Download not available yet")
    path, media_type, download_name = resolved
    return FileResponse(
        path,
        media_type=media_type,
        filename=download_name,
        content_disposition_type="attachment",
    )


@router.get("/artifacts/{artifact_id}/download/transcript.txt")
def artifact_transcript_txt_download(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact or artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Transcript not found")
    resolved = resolve_transcript_txt(settings, artifact)
    if not resolved:
        raise HTTPException(status_code=404, detail="Transcript not available")
    body, filename = resolved
    return Response(
        content=body.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers=_attachment_headers(filename),
    )


@router.get("/artifacts/{artifact_id}/download/summary.docx")
def artifact_summary_docx_download(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact or artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Summary not found")
    resolved = resolve_summary_docx(settings, artifact)
    if not resolved:
        raise HTTPException(status_code=404, detail="Summary not available")
    body, filename = resolved
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.officedocument.wordprocessingml.document",
        headers=_attachment_headers(filename),
    )


@router.get("/artifacts/{artifact_id}/download/summary.doc")
def artifact_summary_doc_legacy(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
):
    """Старый URL → тот же docx."""
    return artifact_summary_docx_download(artifact_id, db, settings, _user)


@router.get("/artifacts/{artifact_id}/report", response_class=HTMLResponse)
def artifact_report_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.file_type == "audio":
        try:
            report_path = ensure_audio_report(settings, artifact)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Report not found") from exc
        html = report_path.read_text(encoding="utf-8")
        if embed:
            html = apply_embed_mode(html, kind="audio")
        return HTMLResponse(html)

    report_path = ensure_document_report(settings, artifact)
    html = report_path.read_text(encoding="utf-8")
    if embed:
        html = apply_embed_mode(html, kind="document")
    return HTMLResponse(html)


@router.get("/artifacts/{artifact_id}/report-sortformer", response_class=HTMLResponse)
def artifact_sortformer_report_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Sortformer report is for audio artifacts only")
    try:
        report_path = ensure_sortformer_report(settings, artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    if embed:
        html = apply_embed_mode(html, kind="audio")
    return HTMLResponse(html)


@router.get("/artifacts/{artifact_id}/report-diar-raw", response_class=HTMLResponse)
def artifact_raw_diar_report_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Raw diarization report is for audio artifacts only")
    try:
        report_path = ensure_raw_diar_report(settings, artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    if embed:
        html = apply_embed_mode(html, kind="audio")
    return HTMLResponse(html)


@router.get("/artifacts/{artifact_id}/report-speaker-match", response_class=HTMLResponse)
def artifact_speaker_match_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Speaker match report is for audio artifacts only")
    try:
        report_path = ensure_speaker_match_report(settings, artifact, db)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    if embed:
        html = apply_embed_mode(html, kind="audio")
    return HTMLResponse(html)


@router.get("/artifacts/{artifact_id}/report-diar-two-voice", response_class=HTMLResponse)
def artifact_two_voice_diar_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Two-voice diar report is for audio artifacts only")
    try:
        report_path = ensure_two_voice_diar_report(settings, artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    if embed:
        html = apply_embed_mode(html, kind="audio")
    return HTMLResponse(html)
