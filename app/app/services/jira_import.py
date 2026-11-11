"""Jira в портале: личный токен пользователя, выбор проектов и загрузка задач с вложениями как материалов проекта.

Этап 2 плана docs/plans/JIRA-INTEGRATION-PLAN.md. Пользователь вставляет свой токен Jira (``connect``), выбирает проекты
(``visible_projects`` → ``import_projects``); каждая задача Jira становится документом проекта (Markdown: поля, описание,
комментарии), каждое вложение — файлом. Регистрация идёт тем же путём, что обычная загрузка (``create_artifact_record`` →
очередь ``jobs``), поэтому дальше работает общий конвейер: текст, сущности, индекс, чат.

Доступ: проект из Jira видит тот, чей токен видит его в Jira (``refresh_access`` выдаёт и снимает участие, таблица
``jira_grants``). Ключ проекта Jira привязан не более чем к одному проекту портала; влить Jira в существующий проект может
только admin. Обновления: ``sync_link`` по кнопке и по расписанию (``JIRA_SYNC_MINUTES``) докачивает изменённые задачи —
прежний документ задачи заменяется новым. Токен хранится зашифрованным (ключ — из ``SECRET_KEY``), в ответы и журнал не попадает.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.jira import JiraAccount, JiraGrant, JiraItem, JiraProjectLink
from app.app.models.project import Project
from app.app.models.project_member import ProjectMember
from app.app.models.user import User
from app.app.services import jira_render
from app.app.services.jira_client import JiraClient, JiraError, JiraNotConfigured

logger = logging.getLogger("primaryanalitics.jira")

SOURCE_TYPE = "document"
_lock = threading.Lock()
_running: set[int] = set()  # привязки, которые сейчас загружаются


class JiraImportError(RuntimeError):
    """Ошибка подключения или загрузки с текстом для пользователя."""


# --- токен ---------------------------------------------------------------------------------------------------------

def _fernet(settings: Settings) -> Fernet:
    digest = hashlib.sha256(b"primaryanalitics-jira-token:" + (settings.secret_key or "").encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(settings: Settings, token: str) -> str:
    return _fernet(settings).encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_token(settings: Settings, token_enc: str) -> str:
    try:
        return _fernet(settings).decrypt(token_enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise JiraImportError("Сохранённый токен Jira не читается (сменился SECRET_KEY): подключите Jira заново") from exc


def server_configured(settings: Settings) -> bool:
    """Задан ли адрес Jira: без него подключать нечего."""
    return bool((settings.jira_base_url or "").strip())


ClientFactory = Callable[[Settings, str], JiraClient]


def make_client(settings: Settings, token: str) -> JiraClient:
    return JiraClient(settings.jira_base_url, token, email=settings.jira_email, verify_tls=settings.jira_verify_tls)


def account_of(db: Session, user_id: int) -> JiraAccount | None:
    return db.scalar(select(JiraAccount).where(JiraAccount.user_id == user_id))


def client_for(db: Session, settings: Settings, user_id: int, factory: ClientFactory = make_client) -> JiraClient:
    account = account_of(db, user_id)
    if account is None:
        raise JiraImportError("Jira не подключена: вставьте свой токен на странице проектов")
    return factory(settings, decrypt_token(settings, account.token_enc))


def connect(db: Session, settings: Settings, user: User, token: str, factory: ClientFactory = make_client) -> dict[str, Any]:
    """Проверить токен в Jira и сохранить его зашифрованным. Заодно выдать доступ к уже загруженным проектам."""
    token = (token or "").strip()
    if not server_configured(settings):
        raise JiraImportError("На сервере не задан адрес Jira (JIRA_BASE_URL в .env)")
    if not token:
        raise JiraImportError("Вставьте токен Jira")
    try:
        with factory(settings, token) as client:
            me = client.myself()
            keys = {row["key"] for row in client.projects()}
    except JiraError as exc:
        raise JiraImportError(str(exc)) from exc
    account = account_of(db, user.id)
    if account is None:
        account = JiraAccount(user_id=user.id, token_enc="")
        db.add(account)
    account.token_enc = encrypt_token(settings, token)
    account.jira_name = me.get("name") or me.get("account")
    account.checked_at = datetime.now(timezone.utc)
    account.last_error = None
    db.flush()
    refresh_access(db, user, keys)
    db.commit()
    log_event(logger, "jira.connected", user_id=user.id, projects=len(keys))
    return {"connected": True, "name": account.jira_name, "projects": len(keys)}


def disconnect(db: Session, user: User) -> None:
    """Удалить токен и снять доступ, выданный по видимости в Jira."""
    refresh_access(db, user, set())
    account = account_of(db, user.id)
    if account is not None:
        db.delete(account)
    db.commit()
    log_event(logger, "jira.disconnected", user_id=user.id)


# --- доступ --------------------------------------------------------------------------------------------------------

def refresh_access(db: Session, user: User, visible_keys: set[str]) -> dict[str, int]:
    """Привести участие пользователя в проектах из Jira к тому, что его токен видит в Jira сейчас.

    Видит проект в Jira — получает участие (и запись в ``jira_grants``); перестал видеть — участие, выданное этим
    механизмом, снимается. Участие, выданное вручную («Выдать доступ»), и свои проекты не трогаются.
    """
    links = db.scalars(select(JiraProjectLink)).all()
    wanted = {link.project_id for link in links if link.jira_key in visible_keys}
    account = account_of(db, user.id)
    if account is not None:
        account.visible_keys = ",".join(sorted(visible_keys))
    grants = {g.project_id: g for g in db.scalars(select(JiraGrant).where(JiraGrant.user_id == user.id)).all()}
    added = removed = 0
    role = "manager" if user.role in ("admin", "manager") else "user"
    for project_id in wanted - set(grants):
        member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id))
        project = db.get(Project, project_id)
        if project is None:
            continue
        if member is None and project.created_by != user.id:
            db.add(ProjectMember(project_id=project_id, user_id=user.id, role=role, granted_by=user.id))
            db.add(JiraGrant(user_id=user.id, project_id=project_id))
            added += 1
    for project_id, grant in grants.items():
        if project_id in wanted:
            continue
        member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id))
        if member is not None:
            db.delete(member)
        db.delete(grant)
        removed += 1
    db.flush()
    return {"added": added, "removed": removed}


def refresh_access_cached(db: Session) -> None:
    """Появилась или снята привязка: пересчитать доступ всех подключивших Jira по ключам, которые их токены видели в прошлый раз."""
    for account in db.scalars(select(JiraAccount)).all():
        user = db.get(User, account.user_id)
        if user is not None:
            refresh_access(db, user, {k for k in (account.visible_keys or "").split(",") if k})


# --- список проектов Jira и привязки -----------------------------------------------------------------------------------

def link_by_key(db: Session, jira_key: str) -> JiraProjectLink | None:
    return db.scalar(select(JiraProjectLink).where(JiraProjectLink.jira_key == jira_key.strip().upper()))


def links_of_project(db: Session, project_id: int) -> list[JiraProjectLink]:
    return list(db.scalars(select(JiraProjectLink).where(JiraProjectLink.project_id == project_id).order_by(JiraProjectLink.id)).all())


def jira_keys_by_project(db: Session, project_ids: set[int] | None = None) -> dict[int, str]:
    """Ключи Jira по проектам портала (через запятую, если их несколько) — для пометки в списке проектов."""
    out: dict[int, list[str]] = {}
    for link in db.scalars(select(JiraProjectLink).order_by(JiraProjectLink.id)).all():
        if project_ids is None or link.project_id in project_ids:
            out.setdefault(link.project_id, []).append(link.jira_key)
    return {pid: ", ".join(keys) for pid, keys in out.items()}


def link_out(db: Session, link: JiraProjectLink) -> dict[str, Any]:
    project = db.get(Project, link.project_id)
    items = db.scalars(select(JiraItem).where(JiraItem.link_id == link.id)).all()
    return {"jira_key": link.jira_key, "jira_name": link.jira_name, "project_id": link.project_id,
            "project_name": project.name if project else None, "status": link.status, "status_text": link.status_text,
            "last_sync_at": link.last_sync_at.isoformat() if link.last_sync_at else None, "issues_total": link.issues_total,
            "issues": sum(1 for i in items if i.kind == "issue" and i.artifact_id), "files": sum(1 for i in items if i.kind == "attachment" and i.artifact_id),
            "not_loaded": [{"issue": i.issue_key, "reason": i.note or "не загружено"} for i in items if i.kind == "attachment" and not i.artifact_id]}


def visible_projects(db: Session, settings: Settings, user: User, factory: ClientFactory = make_client) -> list[dict[str, Any]]:
    """Проекты Jira, которые видит токен пользователя, и что с каждым в портале: уже привязан или нет."""
    try:
        with client_for(db, settings, user.id, factory) as client:
            rows = client.projects()
    except JiraError as exc:
        raise JiraImportError(str(exc)) from exc
    refresh_access(db, user, {row["key"] for row in rows})
    db.commit()
    links = {link.jira_key: link for link in db.scalars(select(JiraProjectLink)).all()}
    out = []
    for row in rows:
        link = links.get(row["key"])
        out.append({"key": row["key"], "name": row["name"], "linked": link_out(db, link) if link else None})
    return out


def create_link(db: Session, project: Project, jira_key: str, jira_name: str | None, user: User) -> JiraProjectLink:
    """Привязать проект Jira к проекту портала. Ключ Jira может быть привязан только к одному проекту."""
    key = (jira_key or "").strip().upper()
    if not key or not re.fullmatch(r"[A-Z0-9_]+", key):
        raise JiraImportError(f"Неверный ключ проекта Jira: {jira_key!r}")
    existing = link_by_key(db, key)
    if existing is not None:
        if existing.project_id == project.id:
            return existing
        raise JiraImportError(f"Проект Jira {key} уже привязан к другому проекту портала")
    link = JiraProjectLink(project_id=project.id, jira_key=key, jira_name=(jira_name or "").strip() or None, created_by=user.id, status="idle")
    db.add(link)
    db.flush()
    return link


def new_portal_project(db: Session, settings: Settings, user: User, name: str, jira_key: str) -> Project:
    """Проект портала для проекта Jira — тем же способом, что «Новый проект»: настройки агента, реестр людей, каталоги."""
    from app.app.services import agent_settings
    from app.app.services.storage import create_project_tree, normalize_project_name

    project = Project(name=normalize_project_name(name) or jira_key, description=f"Проект из Jira ({jira_key})", created_by=user.id, is_active=True)
    agent_settings.init_new_project(project)
    project.set_settings({**project.settings(), "memory_platform": True, "source": "jira"})
    db.add(project)
    db.flush()
    create_project_tree(settings, project.id)
    return project


def import_projects(db: Session, settings: Settings, user: User, picks: list[dict[str, Any]],
                    factory: ClientFactory = make_client) -> list[JiraProjectLink]:
    """Привязать выбранные проекты Jira. ``picks``: ``{"key": ..., "project_id": ...}``; без ``project_id`` — новый проект.

    Влить в существующий проект может только admin. Токен пользователя должен видеть каждый выбранный проект.
    """
    if user.role not in ("admin", "manager"):
        raise JiraImportError("Загружать проекты из Jira может менеджер или администратор")
    try:
        with client_for(db, settings, user.id, factory) as client:
            visible = {row["key"]: row["name"] for row in client.projects()}
    except JiraError as exc:
        raise JiraImportError(str(exc)) from exc
    links: list[JiraProjectLink] = []
    for pick in picks:
        key = str(pick.get("key") or "").strip().upper()
        if key not in visible:
            raise JiraImportError(f"Ваш токен не видит проект Jira {key}")
        link = link_by_key(db, key)
        if link is None:
            target_id = pick.get("project_id")
            if target_id:
                if user.role != "admin":
                    raise JiraImportError("Влить Jira в существующий проект может только администратор")
                project = db.get(Project, int(target_id))
                if project is None or not project.is_active:
                    raise JiraImportError("Проект портала не найден")
            else:
                project = new_portal_project(db, settings, user, visible[key], key)
            link = create_link(db, project, key, visible[key], user)
            log_event(logger, "jira.linked", jira_key=key, project_id=project.id, user_id=user.id, new_project=not target_id)
        link.status = "queued"
        link.status_text = "в очереди на загрузку"
        links.append(link)
    refresh_access(db, user, set(visible))
    refresh_access_cached(db)  # остальные, чей токен видит эти проекты, получают доступ сразу
    db.commit()
    return links


def unlink(db: Session, link: JiraProjectLink) -> None:
    """Снять привязку: материалы, уже загруженные из Jira, остаются в проекте; выданный по Jira доступ снимается."""
    for item in db.scalars(select(JiraItem).where(JiraItem.link_id == link.id)).all():
        db.delete(item)
    others = {l.project_id for l in db.scalars(select(JiraProjectLink).where(JiraProjectLink.id != link.id)).all()}
    if link.project_id not in others:
        for grant in db.scalars(select(JiraGrant).where(JiraGrant.project_id == link.project_id)).all():
            member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == grant.project_id, ProjectMember.user_id == grant.user_id))
            if member is not None:
                db.delete(member)
            db.delete(grant)
    db.delete(link)
    db.flush()
    refresh_access_cached(db)
    db.commit()


# --- задача Jira -> документ ---------------------------------------------------------------------------------------------

def jira_time(value: Any) -> datetime | None:
    """Время Jira (``2026-11-02T10:00:00.000+0300``) в datetime с часовым поясом."""
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def safe_filename(text: str, limit: int = 120) -> str:
    """Имя файла без символов, недопустимых в Windows, и без путей."""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(text or "")).strip(" .")
    return re.sub(r"\s+", " ", name)[:limit].strip(" .") or "файл"


def issue_markdown(raw: dict[str, Any], base_url: str, project_name: str | None = None) -> str:
    """Задача Jira как документ (полный вид — ``jira_render.issue_document``)."""
    return jira_render.issue_document(raw, base_url, project_name)


# --- регистрация файла как материала проекта -----------------------------------------------------------------------------------

def allowed_extensions(settings: Settings) -> set[str]:
    from app.app.services import upload

    ext = set(upload.WORD_EXT) | set(upload.SHEET_EXT) | set(upload.DOC_EXT) | set(upload.IMAGE_EXT) | {".html", ".htm"}
    if settings.jira_import_media:
        ext |= set(upload.AUDIO_EXT)
    return ext


def register_file(db: Session, settings: Settings, project: Project, user_id: int, original: str, dest: Path,
                  recorded: datetime | None = None) -> tuple[Artifact, bool]:
    """Зарегистрировать файл на диске как материал проекта — тем же способом, что обычная загрузка.

    Возвращает (артефакт, новый ли): файл с тем же содержимым уже есть в проекте — возвращается прежний артефакт.
    """
    from app.app.services.upload import (create_artifact_record, default_priority, detect_file_type, ffprobe_duration, find_duplicate,
                                         hash_file, reconcile_source_type)

    sha256, size = hash_file(dest)
    duplicate = find_duplicate(db, project.id, sha256)
    if duplicate is not None and duplicate.status != "error":
        dest.unlink(missing_ok=True)
        return duplicate, False
    if duplicate is not None:  # прежний разбор этого же файла упал — убираем его и разбираем заново
        from app.app.services.project_purge import purge_artifact

        purge_artifact(db, settings, duplicate)
    if recorded is not None:
        try:
            os.utime(dest, (recorded.timestamp(), recorded.timestamp()))  # дата материала — дата в Jira, а не момент скачивания
        except OSError:
            pass
    file_type = detect_file_type(original, None)
    source_type = reconcile_source_type(SOURCE_TYPE, file_type)
    duration = ffprobe_duration(dest) if file_type == "audio" else None
    artifact, job = create_artifact_record(
        db, project_id=project.id, user_id=user_id, original_filename=original, stored_filename=dest.name, file_path=dest,
        file_type=file_type, source_type=source_type, sha256=sha256, priority_weight=default_priority(source_type, project.settings()),
        duration_sec=duration)
    stamp_jira_date(artifact, recorded)
    log_event(logger, "artifact.created", artifact=artifact.artifact_id, file=original, file_type=file_type, status=artifact.status,
              bytes=size, source="jira", job_id=job.id if job else None)
    return artifact, True


def stamp_jira_date(artifact: Artifact | None, when: datetime | None) -> None:
    """Дата материала — дата из Jira (последнее изменение задачи, загрузка вложения), а не момент загрузки в портал.
    Дату, которую человек поставил вручную, не трогаем."""
    from app.app.services import recorded_at as ra

    if artifact is None or when is None or artifact.recorded_at_source in (ra.SOURCE_MANUAL, ra.SOURCE_MANUAL_TIME):
        return
    moment = when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when
    if artifact.recorded_at != moment or artifact.recorded_at_source != ra.SOURCE_JIRA:
        ra.set_recorded(artifact, moment, ra.SOURCE_JIRA)


def _dest(settings: Settings, project_id: int, original: str) -> Path:
    from app.app.services.storage import generate_stored_filename, raw_input_dir

    folder = raw_input_dir(settings, project_id, SOURCE_TYPE)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / generate_stored_filename(original)


def _item(db: Session, link_id: int, kind: str, ref: str) -> JiraItem | None:
    return db.scalar(select(JiraItem).where(JiraItem.link_id == link_id, JiraItem.kind == kind, JiraItem.ref == ref))


def _artifact_failed(db: Session, artifact_id: int | None) -> bool:
    """Материала нет или его разбор завершился ошибкой — такой документ при следующей проверке загружается заново."""
    artifact = db.get(Artifact, artifact_id) if artifact_id else None
    return artifact is None or artifact.status == "error"


def _replace_artifact(db: Session, settings: Settings, item: JiraItem, artifact: Artifact) -> None:
    """Задача изменилась: новый документ на месте, прежний удаляется со всем, что из него извлечено."""
    old_id = item.artifact_id
    item.artifact_id = artifact.id
    db.flush()
    if old_id and old_id != artifact.id:
        old = db.get(Artifact, old_id)
        if old is not None:
            from app.app.services.project_purge import purge_artifact

            purge_artifact(db, settings, old)


# --- загрузка проекта ----------------------------------------------------------------------------------------------

def _write_doc(settings: Settings, project_id: int, original: str, text: str) -> Path:
    dest = _dest(settings, project_id, original)
    dest.write_text(text, encoding="utf-8", newline="\n")
    return dest


def _load_attachment(db: Session, settings: Settings, project: Project, link: JiraProjectLink, client: JiraClient, user_id: int,
                     key: str, attachment: dict[str, Any], allowed: set[str], max_bytes: int, stats: dict[str, int]) -> None:
    """Одно вложение задачи: скачать и зарегистрировать как материал, либо записать, почему оно не загружено."""
    ref = str(attachment["id"])
    record = _item(db, link.id, "attachment", ref)
    if record is not None and record.artifact_id and _artifact_failed(db, record.artifact_id):
        record.artifact_id = None  # разбор файла упал: скачиваем и разбираем заново (register_file уберёт прежний)
    elif record is not None and (record.artifact_id or not _retry_note(record.note)):
        if record.artifact_id:
            stamp_jira_date(db.get(Artifact, record.artifact_id), jira_time(attachment.get("created")))
        return
    name = safe_filename(str(attachment.get("filename") or "файл"))
    if record is None:
        record = JiraItem(link_id=link.id, kind="attachment", ref=ref, issue_key=key)
        db.add(record)
    record.updated = str(attachment.get("created") or "")
    record.fmt = jira_render.FORMAT_VERSION
    ext = Path(name).suffix.lower()
    if ext not in allowed:
        record.note = f"{name}: вид файла {ext or 'без расширения'} портал не разбирает"
        return
    if int(attachment.get("size") or 0) > max_bytes:
        record.note = f"{name}: файл больше допустимого размера"
        return
    file_original = safe_filename(f"Jira {key} — {Path(name).stem}") + ext
    file_dest = _dest(settings, project.id, file_original)
    try:
        client.download(str(attachment.get("content") or ""), file_dest, max_bytes=max_bytes)
    except JiraError as exc:
        record.note = f"{name}: не скачалось — {str(exc)[:160]}"
        return
    artifact, created = register_file(db, settings, project, user_id, file_original, file_dest, jira_time(attachment.get("created")))
    stamp_jira_date(artifact, jira_time(attachment.get("created")))
    record.artifact_id, record.note = artifact.id, None
    stats["files_new" if created else "files_same"] += 1


def _retry_note(note: str | None) -> bool:
    """Вложение не загрузилось из-за сбоя сети — при следующей проверке попробовать ещё раз (вид файла и размер — не сбой)."""
    return bool(note and "не скачалось" in note)


def skipped_files(db: Session, link_id: int) -> list[dict[str, str]]:
    """Вложения Jira, которых нет среди материалов проекта, и почему."""
    rows = db.scalars(select(JiraItem).where(JiraItem.link_id == link_id, JiraItem.kind == "attachment", JiraItem.artifact_id.is_(None))).all()
    return [{"issue": row.issue_key, "reason": row.note or "не загружено"} for row in rows]


def sync_link(db: Session, settings: Settings, link: JiraProjectLink, client: JiraClient, user_id: int) -> dict[str, int]:
    """Докачать проект Jira в проект портала: сводка проекта, новые и изменённые задачи целиком, новые вложения."""
    project = db.get(Project, link.project_id)
    if project is None or not project.is_active:
        raise JiraImportError("Проект портала удалён")
    stats = {"issues_total": 0, "issues_new": 0, "issues_updated": 0, "issues_same": 0, "files_new": 0, "files_same": 0}
    link.status, link.status_text = "running", "читаю список задач"
    db.commit()
    max_bytes = int(settings.max_upload_bytes)
    allowed = allowed_extensions(settings)
    listing = client.issues(link.jira_key)  # все задачи проекта: по списку строится сводка
    stats["issues_total"] = len(listing)
    link.issues_total = len(listing)
    cap = int(settings.jira_import_max_issues or 0)
    to_load = listing[:cap] if cap > 0 else listing
    for number, row in enumerate(to_load, 1):
        key, updated = row["key"], str(row["updated"] or "")
        item = _item(db, link.id, "issue", key)
        if (item is not None and item.updated == updated and item.fmt == jira_render.FORMAT_VERSION and item.artifact_id
                and not _artifact_failed(db, item.artifact_id)):
            stamp_jira_date(db.get(Artifact, item.artifact_id), jira_time(updated))
            for file_row in db.scalars(select(JiraItem).where(JiraItem.link_id == link.id, JiraItem.kind == "attachment",
                                                              JiraItem.issue_key == key, JiraItem.artifact_id.is_not(None))).all():
                stamp_jira_date(db.get(Artifact, file_row.artifact_id), jira_time(file_row.updated))
            stats["issues_same"] += 1
            continue
        raw = client.issue_full(key)
        fields = raw.get("fields") or {}
        original = safe_filename(f"Jira {key} — {row['summary']}") + ".md"
        dest = _write_doc(settings, project.id, original, jira_render.issue_document(raw, client.base_url, link.jira_name))
        artifact, created = register_file(db, settings, project, user_id, original, dest, jira_time(fields.get("updated")))
        if item is None:
            item = JiraItem(link_id=link.id, kind="issue", ref=key, issue_key=key)
            db.add(item)
            stats["issues_new"] += 1
        else:
            stats["issues_updated" if created else "issues_same"] += 1
        item.updated, item.fmt = updated, jira_render.FORMAT_VERSION
        stamp_jira_date(artifact, jira_time(fields.get("updated")))
        _replace_artifact(db, settings, item, artifact)
        for attachment in fields.get("attachment") or []:
            if isinstance(attachment, dict) and attachment.get("id"):
                _load_attachment(db, settings, project, link, client, user_id, key, attachment, allowed, max_bytes, stats)
        link.status_text = f"задач обработано {number} из {len(to_load)}"
        db.commit()
    # сводка проекта: сведения о проекте и таблица всех задач — для обзорных вопросов
    try:
        info = client.project_info(link.jira_key)
    except JiraError:
        info = {"key": link.jira_key, "name": link.jira_name or link.jira_key}
    summary_name = safe_filename(f"Jira {link.jira_key} — сводка проекта") + ".md"
    summary_text = jira_render.project_document(info, listing, client.base_url, loaded=len(to_load))
    summary_dest = _write_doc(settings, project.id, summary_name, summary_text)
    newest = max((jira_time(r["updated"]) for r in listing if jira_time(r["updated"])), default=None)
    summary_artifact, _created = register_file(db, settings, project, user_id, summary_name, summary_dest, newest)
    summary_item = _item(db, link.id, "summary", link.jira_key)
    if summary_item is None:
        summary_item = JiraItem(link_id=link.id, kind="summary", ref=link.jira_key, issue_key=link.jira_key)
        db.add(summary_item)
    summary_item.fmt = jira_render.FORMAT_VERSION
    stamp_jira_date(summary_artifact, newest)
    _replace_artifact(db, settings, summary_item, summary_artifact)
    skipped = skipped_files(db, link.id)
    loaded_files = db.scalars(select(JiraItem).where(JiraItem.link_id == link.id, JiraItem.kind == "attachment",
                                                     JiraItem.artifact_id.is_not(None))).all()
    stats["files_same"] = max(len(loaded_files) - stats["files_new"], 0)
    link.status = "idle"
    link.status_text = (f"в Jira задач {stats['issues_total']}, загружено {len(to_load)}"
                        + (f" (ограничение JIRA_IMPORT_MAX_ISSUES={cap})" if cap > 0 and len(to_load) < len(listing) else "")
                        + f": новых {stats['issues_new']}, обновлено {stats['issues_updated']}, без изменений {stats['issues_same']}; "
                        f"файлов: новых {stats['files_new']}, уже были {stats['files_same']}, не загружено {len(skipped)}")
    link.last_sync_at = datetime.now(timezone.utc)
    db.commit()
    log_event(logger, "jira.synced", jira_key=link.jira_key, project_id=link.project_id, skipped=len(skipped), **stats)
    return stats


def _who_and_when(raw: dict[str, Any], stored: str) -> tuple[int, list[str]]:
    """Независимая от сборки документа проверка «кто и когда»: автор и время создания задачи, исполнитель, автор и время каждого
    комментария, каждой записи трудозатрат, каждого вложения (с именем файла) и каждой записи истории должны быть в тексте.
    Возвращает (сколько сведений проверено, чего не хватает)."""
    fields = raw.get("fields") or {}
    person, when = jira_render.person, jira_render.when
    wanted: list[tuple[str, list[str]]] = [("автор задачи", [person(fields.get("reporter"))]), ("исполнитель", [person(fields.get("assignee"))]),
                                           ("кто создал", [person(fields.get("creator"))]), ("время создания", [when(fields.get("created"))]),
                                           ("время изменения", [when(fields.get("updated"))])]
    for c in (fields.get("comment") or {}).get("comments") or []:
        wanted.append((f"комментарий от {when(c.get('created'))}", [person(c.get("author")), when(c.get("created"))]))
    for w in (fields.get("worklog") or {}).get("worklogs") or []:
        wanted.append((f"трудозатраты от {when(w.get('started'))}", [person(w.get("author")), when(w.get("started"))]))
    for a in fields.get("attachment") or []:
        wanted.append((f"вложение «{a.get('filename')}»", [str(a.get("filename") or ""), person(a.get("author")), when(a.get("created"))]))
    for h in (raw.get("changelog") or {}).get("histories") or []:
        wanted.append((f"изменение от {when(h.get('created'))}", [person(h.get("author")), when(h.get("created"))]))
    missing = [what for what, parts in wanted if any(part and part != "—" and part not in stored for part in parts)]
    return len(wanted), missing


def check_link(db: Session, settings: Settings, link: JiraProjectLink, client: JiraClient) -> dict[str, Any]:
    """Сверка «Jira ↔ портал» по одной привязке, ничего не меняет: все ли задачи и вложения на месте, совпадает ли документ
    каждой задачи с тем, что Jira отдаёт сейчас (буква в букву), разобраны ли материалы конвейером."""
    from app.app.models.entity import Chunk

    listing = client.issues(link.jira_key)
    items = db.scalars(select(JiraItem).where(JiraItem.link_id == link.id)).all()
    issues = {i.ref: i for i in items if i.kind == "issue"}
    files = {i.ref: i for i in items if i.kind == "attachment"}
    problems: list[str] = []
    report: dict[str, Any] = {"jira_key": link.jira_key, "jira_issues": len(listing), "portal_issues": 0, "comments": 0, "worklogs": 0,
                              "history": 0, "who_when_checked": 0, "extra_fields": 0, "extra_field_names": [], "omitted_field_names": [], "jira_attachments": 0, "portal_files": 0,
                              "not_loaded": skipped_files(db, link.id),
                              "pipeline": {}, "problems": problems}
    cap = int(settings.jira_import_max_issues or 0)
    expected = listing[:cap] if cap > 0 else listing
    if len(expected) < len(listing):
        problems.append(f"загружены не все задачи: {len(expected)} из {len(listing)} (ограничение JIRA_IMPORT_MAX_ISSUES={cap})")
    artifact_ids: list[int] = []
    for row in expected:
        key = row["key"]
        item = issues.get(key)
        if item is None or not item.artifact_id:
            problems.append(f"{key}: задачи нет в портале")
            continue
        artifact = db.get(Artifact, item.artifact_id)
        if artifact is None:
            problems.append(f"{key}: документ задачи удалён из проекта вручную")
            continue
        report["portal_issues"] += 1
        artifact_ids.append(artifact.id)
        raw = client.issue_full(key)
        numbers = jira_render.counts(raw)
        for name in ("comments", "worklogs", "history", "extra_fields"):
            report[name] += numbers[name]
        report["extra_field_names"] = sorted(set(report["extra_field_names"]) | {label for label, _value in jira_render.extra_fields(raw)})
        report["omitted_field_names"] = sorted(set(report["omitted_field_names"]) | set(jira_render.omitted_fields(raw)))
        if numbers["comments"] != numbers["comments_total"]:
            problems.append(f"{key}: Jira отдала {numbers['comments']} комментариев из {numbers['comments_total']}")
        if numbers["worklogs"] != numbers["worklogs_total"]:
            problems.append(f"{key}: Jira отдала {numbers['worklogs']} записей трудозатрат из {numbers['worklogs_total']}")
        if numbers["history"] != numbers["history_total"]:
            problems.append(f"{key}: Jira отдала {numbers['history']} записей истории из {numbers['history_total']}")
        try:
            stored = Path(artifact.file_path).read_text(encoding="utf-8")
        except OSError:
            problems.append(f"{key}: файл документа не читается")
            stored = None
        if stored is not None and stored != jira_render.issue_document(raw, client.base_url, link.jira_name):
            problems.append(f"{key}: документ в портале отличается от Jira (задача изменилась или документ устарел) — нужна проверка обновлений")
        elif stored is not None:
            checked, missing = _who_and_when(raw, stored)
            report["who_when_checked"] += checked
            problems.extend(f"{key}: в документе нет сведений — {what}" for what in missing[:5])
        if artifact.recorded_at_source != "jira":
            problems.append(f"{key}: дата материала взята не из Jira — нужна проверка обновлений")
        for attachment in (raw.get("fields") or {}).get("attachment") or []:
            if not isinstance(attachment, dict) or not attachment.get("id"):
                continue
            report["jira_attachments"] += 1
            record = files.get(str(attachment["id"]))
            name = str(attachment.get("filename") or "файл")
            if record is None:
                problems.append(f"{key}: вложение «{name}» не учтено")
            elif record.artifact_id:
                file_artifact = db.get(Artifact, record.artifact_id)
                if file_artifact is None:
                    continue  # материал удалён вручную — осознанное действие человека
                report["portal_files"] += 1
                artifact_ids.append(file_artifact.id)
                try:
                    size = Path(file_artifact.file_path).stat().st_size
                except OSError:
                    problems.append(f"{key}: файл вложения «{name}» пропал с диска")
                    continue
                if int(attachment.get("size") or 0) and size != int(attachment.get("size")):
                    problems.append(f"{key}: вложение «{name}» — на диске {size} байт, в Jira {attachment.get('size')}")
    summary = next((i for i in items if i.kind == "summary"), None)
    if summary is None or not summary.artifact_id:
        problems.append("нет сводки проекта — нужна проверка обновлений")
    elif db.get(Artifact, summary.artifact_id) is not None:
        artifact_ids.append(summary.artifact_id)
    for artifact_id in dict.fromkeys(artifact_ids):
        artifact = db.get(Artifact, artifact_id)
        report["pipeline"][artifact.status] = report["pipeline"].get(artifact.status, 0) + 1
        if artifact.status == "error" or artifact.error_message:
            problems.append(f"«{artifact.original_filename}»: ошибка разбора — {str(artifact.error_message or '')[:160]}")
        elif artifact.status == "done" and db.scalar(select(Chunk.id).where(Chunk.artifact_id == artifact.id).limit(1)) is None:
            problems.append(f"«{artifact.original_filename}»: разобран, но в поиске нет ни одного фрагмента")
    report["ok"] = not problems
    return report


def _sync_one(settings: Settings, link_id: int, user_id: int, factory: ClientFactory) -> None:
    from app.app.db import SessionLocal

    with _lock:
        if link_id in _running:
            return
        _running.add(link_id)
    db = SessionLocal()
    try:
        link = db.get(JiraProjectLink, link_id)
        if link is None:
            return
        try:
            with client_for(db, settings, user_id, factory) as client:
                sync_link(db, settings, link, client, user_id)
        except (JiraError, JiraImportError) as exc:
            db.rollback()
            link = db.get(JiraProjectLink, link_id)
            if link is not None:
                link.status, link.status_text = "error", str(exc)[:500]
                db.commit()
            log_event(logger, "jira.sync_failed", level=logging.WARNING, link_id=link_id, error=str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 — фоновый поток: ошибка не должна пропасть молча
            db.rollback()
            link = db.get(JiraProjectLink, link_id)
            if link is not None:
                link.status, link.status_text = "error", f"внутренняя ошибка: {exc.__class__.__name__}"
                db.commit()
            logger.exception("jira sync crashed link_id=%s", link_id)
    finally:
        db.close()
        with _lock:
            _running.discard(link_id)


def start_sync(settings: Settings, link_ids: list[int], user_id: int, factory: ClientFactory = make_client) -> None:
    """Запустить загрузку привязок в фоне: по очереди, одним потоком, чтобы не нагружать Jira."""
    ids = list(dict.fromkeys(link_ids))
    if not ids:
        return

    def run() -> None:
        for link_id in ids:
            _sync_one(settings, link_id, user_id, factory)

    threading.Thread(target=run, name="jira-sync", daemon=True).start()


def sync_user_for_link(db: Session, link: JiraProjectLink) -> int | None:
    """Чьим токеном обновлять привязку по расписанию: кто привязал, иначе любой, кому доступ выдан по Jira."""
    if account_of(db, link.created_by) is not None:
        return link.created_by
    for grant in db.scalars(select(JiraGrant).where(JiraGrant.project_id == link.project_id)).all():
        if account_of(db, grant.user_id) is not None:
            return grant.user_id
    return None


def scheduled_pass(settings: Settings, factory: ClientFactory = make_client) -> dict[str, int]:
    """Один проход расписания: перепроверить доступ по каждому токену и докачать все привязки."""
    from app.app.db import SessionLocal

    db = SessionLocal()
    done = {"accounts": 0, "links": 0}
    try:
        for account in db.scalars(select(JiraAccount)).all():
            user = db.get(User, account.user_id)
            if user is None:
                continue
            try:
                with factory(settings, decrypt_token(settings, account.token_enc)) as client:
                    keys = {row["key"] for row in client.projects()}
                refresh_access(db, user, keys)
                account.checked_at, account.last_error = datetime.now(timezone.utc), None
                done["accounts"] += 1
            except (JiraError, JiraImportError) as exc:
                account.last_error = str(exc)[:500]
            db.commit()
        pairs = [(link.id, sync_user_for_link(db, link)) for link in db.scalars(select(JiraProjectLink)).all()]
    finally:
        db.close()
    for link_id, user_id in pairs:
        if user_id is not None:
            _sync_one(settings, link_id, user_id, factory)
            done["links"] += 1
    return done


def start_scheduler(settings: Settings) -> bool:
    """Фоновая проверка обновлений Jira каждые ``JIRA_SYNC_MINUTES`` минут (0 — выключено). В тестах не запускается."""
    minutes = int(settings.jira_sync_minutes or 0)
    if minutes <= 0 or not server_configured(settings) or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(minutes * 60):
            try:
                scheduled_pass(settings)
            except Exception:  # noqa: BLE001
                logger.exception("jira scheduled pass crashed")

    threading.Thread(target=loop, name="jira-scheduler", daemon=True).start()
    log_event(logger, "jira.scheduler_started", minutes=minutes)
    return True


__all__ = ["JiraImportError", "JiraNotConfigured", "connect", "disconnect", "visible_projects", "import_projects", "sync_link",
           "start_sync", "scheduled_pass", "start_scheduler", "refresh_access", "issue_markdown", "jira_keys_by_project", "check_link", "skipped_files"]


def forget_project(db: Session, project_id: int) -> None:
    """Проект портала удаляется: убрать его привязки к Jira, список скачанного и выданный по Jira доступ (без commit)."""
    from sqlalchemy import delete

    link_ids = list(db.scalars(select(JiraProjectLink.id).where(JiraProjectLink.project_id == project_id)))
    if link_ids:
        db.execute(delete(JiraItem).where(JiraItem.link_id.in_(link_ids)))
    db.execute(delete(JiraProjectLink).where(JiraProjectLink.project_id == project_id))
    db.execute(delete(JiraGrant).where(JiraGrant.project_id == project_id))
