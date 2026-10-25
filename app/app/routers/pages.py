"""HTML-страницы портала и отчёты: вход, проекты, встречи, отчёты артефактов, отдача аудио и скачивание."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user, require_html_user
from app.app.services.meeting_recordings import recording_enabled, screen_recording_enabled
from app.app.services.meetings import (
    get_meeting,
    meeting_by_token,
    ongoing_meetings,
    ongoing_payload,
    require_meeting_staff,
)
from app.app.services.project_access import (
    can_grant_access,
    staff_ui_role,
    require_admin,
    require_artifact_project,
    require_project,
    require_project_staff,
)
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
from app.app.services.artifact_lab import ensure_lab_report, lab_file
from app.app.services.meeting_lab import RELOAD_MS, build_meeting_lab, child_belongs_to_parent
from app.app.services.pipeline_meeting import is_meeting_artifact
from app.app.services.artifact_reports import ensure_document_report
from app.app.services.report_embed import apply_embed_mode, apply_role_chrome
from app.app.services.transcript_export import (
    resolve_brief_docx,
    resolve_summary_docx,
    resolve_transcript_txt,
)
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
MANIFEST_MEDIA = "application/manifest+json"
MANIFEST_NAME = "Встреча PrimaryAnalitics"
MANIFEST_DISPLAY = "standalone"
MANIFEST_ICON = "/static/meeting-icon.svg"


def _page_role(db: Session, user: User, project: Project) -> str:
    return staff_ui_role(db, user, project)


def _artifact_role(db: Session, user: User, artifact: Artifact) -> str:
    project = db.get(Project, artifact.project_id)
    if not project:
        return user.role
    return _page_role(db, user, project)


REPORT_THEME_LINKS = (
    '<link rel="stylesheet" href="/static/design.css?v=6">'
    '<link rel="stylesheet" href="/static/report-theme.css?v=1">'
)
REPORT_JUMP_SCRIPT = '<script src="/static/report-jump.js?v=1" defer></script>'


def _people_datalist(db: Session, project_id: int) -> str:
    """Подсказка имён из реестра людей проекта для полей «имя спикера» на страницах подтверждения голосов.
    Пусто, если реестр пуст. Имена экранируются; скрипт только подставляет list в поля .name-inp."""
    import html as _html

    from app.app.services import people

    names = [p.display_name for p in people.list_people(db, project_id)]
    if not names:
        return ""
    options = "".join(f'<option value="{_html.escape(n, quote=True)}">' for n in names)
    return (
        f'<datalist id="pp-people">{options}</datalist>'
        "<script>(function(){var f=function(){document.querySelectorAll('.name-inp:not([list])').forEach(function(i){i.setAttribute('list','pp-people');});};"
        "f();new MutationObserver(f).observe(document.body,{childList:true,subtree:true});})();</script>"
    )


def _speaker_cards_html(db: Session, artifact: Artifact) -> str:
    """Поля компании, стороны и должности на карточках голосов (services/speaker_cards.py, static/speaker_cards.js)."""
    from app.app.services.speaker_cards import page_context

    ctx = json.dumps(page_context(db, artifact), ensure_ascii=False).replace("</", "<\\/")
    return (f'<script id="pp-cards" type="application/json">{ctx}</script>'
            f'<link rel="stylesheet" href="/static/speaker_cards.css?v=2">'
            f'<script src="/static/speaker_cards.js?v=2"></script>')


def _serve_report(html: str, user: User, *, embed: bool, kind: str, role: str | None = None, people_html: str = "") -> HTMLResponse:
    if people_html and "name-inp" in html and "</body>" in html and "pp-people" not in html:
        html = html.replace("</body>", people_html + "</body>", 1)
    # Единая дизайн-система для отчётов: подключается при отдаче, сохранённые файлы не меняются.
    if "</head>" in html and "report-theme.css" not in html:
        html = html.replace("</head>", REPORT_THEME_LINKS + "</head>", 1)
    # Переход по ссылке к месту источника: ?t= момент созвона, ?m= сообщение, ?q= цитата (SOURCE-ANCHORS-PLAN этап 4)
    if "</body>" in html and "report-jump.js" not in html:
        html = html.replace("</body>", REPORT_JUMP_SCRIPT + "</body>", 1)
    html = apply_role_chrome(html, role or user.role)
    if embed:
        html = apply_embed_mode(html, kind=kind)
    return HTMLResponse(html)


def _attachment_headers(filename: str) -> dict[str, str]:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download"
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
        )
    }


def meeting_voice_flags(settings: Settings) -> dict[str, bool]:
    return {
        "dfn3": bool(settings.meeting_voice_dfn3),
        "loudness": bool(settings.meeting_voice_loudness),
        "server": bool(settings.meeting_voice_server),
    }


def _manifest_start(raw: str) -> str:
    path = (raw or "/").strip() or "/"
    if not path.startswith("/") or "//" in path or "\\" in path:
        return "/"
    return path


@router.get("/meeting.webmanifest")
def meeting_webmanifest(start: str = "/"):
    payload = {
        "name": MANIFEST_NAME,
        "short_name": MANIFEST_NAME,
        "display": MANIFEST_DISPLAY,
        "start_url": _manifest_start(start),
        "icons": [{"src": MANIFEST_ICON, "sizes": "any", "type": "image/svg+xml"}],
    }
    return Response(json.dumps(payload, ensure_ascii=False), media_type=MANIFEST_MEDIA)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    # Telegram ID на форме скрыт (решение владельца 2026-09-28): эта версия сервера принимает вход без него. Признак даёт
    # маршрут, а не шаблон: шаблон подхватывается с диска сразу, код — после перезапуска, и старый код без поля не пустил бы.
    return templates.TemplateResponse(request, "login.html", {"title": "Вход", "hide_telegram": True})


@router.get("/m/{token}", response_class=HTMLResponse)
def public_meeting_page(
    request: Request,
    token: str,
    db: Annotated[Session, Depends(get_db)],
):
    meeting = meeting_by_token(db, token)
    from sqlalchemy import select

    from app.app.models.person import ProjectParty

    # Р7: гость выбирает свою компанию из сторон проекта (только названия компаний, без людей и проекта)
    parties = sorted(db.scalars(select(ProjectParty.company_name).where(ProjectParty.project_id == meeting.project_id)).all(),
                     key=str.casefold)
    return templates.TemplateResponse(
        request,
        "meeting_public.html",
        {
            "title": "Встреча",
            "token": token,
            "guest_companies": parties,
            "meeting_voice": meeting_voice_flags(get_settings()),
            "record_audio": recording_enabled(),
            "record_screen": screen_recording_enabled(),
        },
    )


@router.get("/projects/{project_id}/meetings/{meeting_id}/room", response_class=HTMLResponse)
def meeting_room_page(
    request: Request,
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_html_user)],
):
    require_meeting_staff(db, user, project_id)
    get_meeting(db, project_id, meeting_id)
    return templates.TemplateResponse(
        request,
        "meeting_room.html",
        {
            "title": "Комната",
            "project_id": project_id,
            "meeting_id": meeting_id,
            "meeting_voice": meeting_voice_flags(get_settings()),
        },
    )


CHANGELOG_PATH = Path(__file__).resolve().parents[1] / "changelog.json"


def load_changelog() -> list[dict[str, str]]:
    """Список «Исправлено» для шапки: новые сверху, дата ДД.ММ.ГГГГ."""
    try:
        items = json.loads(CHANGELOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = [i for i in items if isinstance(i, dict) and i.get("date") and i.get("text")]
    rows.sort(key=lambda i: i["date"], reverse=True)
    return [{"date": ".".join(reversed(r["date"].split("-"))), "text": r["text"]} for r in rows]


@router.get("/", response_class=HTMLResponse)
def index_page(
    request: Request,
    user: Annotated[User, Depends(require_html_user)],
):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"title": "Проекты", "current_role": user.role, "changelog": load_changelog()},
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(
    request: Request,
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_html_user)],
):
    project = require_project(db, user, project_id)
    title = f"Проект {project_id} — {project.name}"
    role = _page_role(db, user, project)
    # число материалов нужно странице, чтобы заранее зарезервировать высоту списка (иначе всё ниже «уезжает» после загрузки)
    import re

    from sqlalchemy import select

    from app.app.models.artifact import Artifact

    names = db.scalars(select(Artifact.original_filename).where(Artifact.project_id == project_id)).all()
    artifact_count = sum(1 for n in names if not re.match(r"^analysis_(reply|request|question)_\d+\.json$", n or "", re.IGNORECASE))
    # Полоска «Идёт созвон» рисуется сразу с сервера: появись она после загрузки — страница ниже «прыгнула» бы
    live_meetings = []
    if role in ("admin", "manager"):
        live_meetings = [ongoing_payload(m, people) for m, people in ongoing_meetings(db, project_id)]
    # «Что нового с прошлого захода» (PORTAL-ACCURACY-MANAGER-PLAN Ш5): рисуется сервером, как полоска созвона, — без прыжка страницы
    from app.app.config import get_settings
    from app.app.services.project_digest import digest_for_page

    try:
        digest = digest_for_page(db, get_settings(), project, user, role)
    except Exception:  # полоска не должна ломать страницу проекта
        logging.getLogger("primaryanalitics.pages").exception("project digest failed project=%s", project_id)
        digest = {"lines": []}
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "artifact_count": artifact_count,
            "live_meetings": live_meetings,
            "digest": digest,
            "title": title,
            "project_id": project_id,
            "project_name": project.name,
            "current_role": role,
            "can_grant_access": can_grant_access(db, user, project),
        },
    )


@router.get("/projects/{project_id}/graph", response_class=HTMLResponse)
def graph_page(
    request: Request,
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_html_user)],
):
    project = require_project(db, user, project_id)
    return templates.TemplateResponse(
        request,
        "graph.html",
        {"title": f"Граф {project_id}", "project_id": project_id, "current_role": _page_role(db, user, project)},
    )


@router.get("/projects/{project_id}/tz", response_class=HTMLResponse)
def tz_page(
    request: Request,
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_html_user)],
):
    project = require_project(db, user, project_id)
    return templates.TemplateResponse(
        request,
        "tz.html",
        {"title": f"ТЗ {project_id}", "project_id": project_id, "current_role": _page_role(db, user, project)},
    )


@router.get("/projects/{project_id}/chat", response_class=HTMLResponse)
def chat_page(
    request: Request,
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_html_user)],
):
    project = require_project(db, user, project_id)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"title": f"Чат {project_id}", "project_id": project_id, "current_role": _page_role(db, user, project)},
    )


@router.get("/admin/conflicts", response_class=HTMLResponse)
def conflicts_page(
    request: Request,
    user: Annotated[User, Depends(require_html_user)],
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
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
    user: Annotated[User, Depends(get_current_user)],
    fmt: Annotated[str | None, Query(alias="format")] = None,
):
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Audio artifact not found")
    # ?clip= is ignored on purpose: old HTML asked for a shared asr/work WAV.
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
    user: Annotated[User, Depends(get_current_user)],
):
    artifact = require_artifact_project(db, user, artifact_id)
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
    user: Annotated[User, Depends(get_current_user)],
):
    artifact = require_artifact_project(db, user, artifact_id)
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
    user: Annotated[User, Depends(get_current_user)],
):
    artifact = require_artifact_project(db, user, artifact_id)
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
    user: Annotated[User, Depends(get_current_user)],
):
    """Старый URL → тот же docx."""
    return artifact_summary_docx_download(artifact_id, db, settings, user)


@router.get("/artifacts/{artifact_id}/download/brief.docx")
def artifact_brief_docx_download(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Brief summary not found")
    resolved = resolve_brief_docx(settings, artifact)
    if not resolved:
        raise HTTPException(status_code=404, detail="Brief summary not available")
    body, filename = resolved
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=_attachment_headers(filename),
    )


@router.get("/artifacts/{artifact_id}/report", response_class=HTMLResponse)
def artifact_report_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type == "audio":
        try:
            report_path = ensure_audio_report(settings, artifact)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Report not found") from exc
        html = report_path.read_text(encoding="utf-8")
        return _serve_report(html, user, embed=embed, kind="audio", role=_artifact_role(db, user, artifact),
                         people_html=_people_datalist(db, artifact.project_id) if _artifact_role(db, user, artifact) in ("admin", "manager") else "")

    report_path = ensure_document_report(settings, artifact)
    html = report_path.read_text(encoding="utf-8")
    return _serve_report(html, user, embed=embed, kind="document", role=_artifact_role(db, user, artifact))


@router.get("/artifacts/{artifact_id}/report-sortformer", response_class=HTMLResponse)
def artifact_sortformer_report_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Sortformer report is for audio artifacts only")
    try:
        report_path = ensure_sortformer_report(settings, artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    return _serve_report(html, user, embed=embed, kind="audio", role=_artifact_role(db, user, artifact),
                         people_html=_people_datalist(db, artifact.project_id) if _artifact_role(db, user, artifact) in ("admin", "manager") else "")


@router.get("/artifacts/{artifact_id}/report-diar-raw", response_class=HTMLResponse)
def artifact_raw_diar_report_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Raw diarization report is for audio artifacts only")
    try:
        report_path = ensure_raw_diar_report(settings, artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    return _serve_report(html, user, embed=embed, kind="audio", role=_artifact_role(db, user, artifact),
                         people_html=_people_datalist(db, artifact.project_id) if _artifact_role(db, user, artifact) in ("admin", "manager") else "")


@router.get("/artifacts/{artifact_id}/report-speaker-match", response_class=HTMLResponse)
def artifact_speaker_match_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = require_artifact_project(db, user, artifact_id)
    require_project_staff(db, user, artifact.project_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Speaker match report is for audio artifacts only")
    try:
        report_path = ensure_speaker_match_report(settings, artifact, db)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    staff = _artifact_role(db, user, artifact) in ("admin", "manager")
    people_html = _speaker_cards_html(db, artifact) if staff else ""  # список «Кто это» вместо datalist имён
    return _serve_report(html, user, embed=embed, kind="audio", role=_artifact_role(db, user, artifact), people_html=people_html)


@router.get("/artifacts/{artifact_id}/report-diar-two-voice", response_class=HTMLResponse)
def artifact_two_voice_diar_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
    embed: Annotated[bool, Query()] = False,
):
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Two-voice diar report is for audio artifacts only")
    try:
        report_path = ensure_two_voice_diar_report(settings, artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    return _serve_report(html, user, embed=embed, kind="audio", role=_artifact_role(db, user, artifact),
                         people_html=_people_datalist(db, artifact.project_id) if _artifact_role(db, user, artifact) in ("admin", "manager") else "")


LAB_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".npy": "application/octet-stream",
}


def _audio_artifact(db: Session, user: User, artifact_id: str, what: str) -> Artifact:
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail=f"{what} is for audio artifacts only")
    return artifact


@router.get("/artifacts/{artifact_id}/report-lab", response_class=HTMLResponse)
def artifact_lab_report_page(
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
    run: Annotated[str | None, Query()] = None,
    embed: Annotated[bool, Query()] = False,
):
    artifact = _audio_artifact(db, user, artifact_id, "Lab report")
    require_admin(user)
    try:
        report_path = ensure_lab_report(settings, artifact, run)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    html = report_path.read_text(encoding="utf-8")
    return _serve_report(html, user, embed=embed, kind="audio", role=_artifact_role(db, user, artifact),
                         people_html=_people_datalist(db, artifact.project_id) if _artifact_role(db, user, artifact) in ("admin", "manager") else "")


@router.get("/artifacts/{artifact_id}/lab/{run_id}/file/{relative:path}")
def artifact_lab_file(
    artifact_id: str,
    run_id: str,
    relative: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    artifact = _audio_artifact(db, user, artifact_id, "Lab files")
    require_admin(user)
    try:
        path = lab_file(settings, artifact, run_id, relative)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type=LAB_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        filename=path.name,
        content_disposition_type="inline",
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"},
    )


@router.get("/artifacts/{artifact_id}/lab/{run_id}/frame/{relative:path}")
def artifact_lab_frame(
    artifact_id: str,
    run_id: str,
    relative: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
):
    """Кадр экрана для стенограммы: только .jpg внутри frames/ прогона.

    Права такие же, как у отчёта (не только админ): картинки экрана видны
    всем, кто смотрит расшифровку. Выход за ``frames/`` или не-jpg — 404.
    """
    if not relative.lower().endswith(".jpg") or not relative.replace("\\", "/").startswith("frames/"):
        raise HTTPException(status_code=404, detail="Frame not found")
    artifact = require_artifact_project(db, user, artifact_id)
    if artifact.file_type != "audio":
        raise HTTPException(status_code=404, detail="Frames are for audio artifacts only")
    try:
        path = lab_file(settings, artifact, run_id, relative)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="image/jpeg",
        content_disposition_type="inline",
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"},
    )


@router.get("/artifacts/{artifact_id}/report-meeting-lab", response_class=HTMLResponse)
def meeting_lab_report_page(
    request: Request,
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_html_user)],
):
    artifact = _audio_artifact(db, user, artifact_id, "Meeting lab")
    require_admin(user)
    if not is_meeting_artifact(artifact):
        raise HTTPException(status_code=404, detail="Meeting lab is for meeting artifacts only")
    return templates.TemplateResponse(
        request,
        "meeting_lab.html",
        {
            "title": "Лаборатория созвона",
            "lab": build_meeting_lab(settings, artifact),
            "project_id": artifact.project_id,
            "reload_ms": RELOAD_MS,
        },
    )


@router.get("/artifacts/{artifact_id}/meeting-lab/{child_id}/lab/{run_id}/file/{relative:path}")
def meeting_lab_file(
    artifact_id: str,
    child_id: str,
    run_id: str,
    relative: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    artifact = _audio_artifact(db, user, artifact_id, "Meeting lab files")
    require_admin(user)
    if not is_meeting_artifact(artifact) or not child_belongs_to_parent(artifact_id, child_id):
        raise HTTPException(status_code=404, detail="unknown meeting track")
    child = SimpleNamespace(project_id=artifact.project_id, artifact_id=child_id)
    try:
        path = lab_file(settings, child, run_id, relative)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type=LAB_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        filename=path.name,
        content_disposition_type="inline",
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"},
    )
