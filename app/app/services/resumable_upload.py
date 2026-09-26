"""Загрузка файлов частями с докачкой: сессии, части и сборка на диск."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.app.config import Settings
from app.app.services.storage import ensure_inside, project_root

logger = logging.getLogger("skkkk.upload")

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
META_NAME = "meta.json"
PART_NAME = "payload.part"


@dataclass
class UploadSession:
    upload_id: str
    project_id: int
    user_id: int
    filename: str
    size: int
    source_type: str
    chunk_size: int
    received: list[int]
    client_key: str | None
    created_at: str

    @property
    def total_chunks(self) -> int:
        if self.size <= 0:
            return 0
        return (self.size + self.chunk_size - 1) // self.chunk_size

    @property
    def bytes_received(self) -> int:
        if not self.received:
            return 0
        complete = {i for i in self.received if i < self.total_chunks - 1}
        last = self.total_chunks - 1
        n = len(complete) * self.chunk_size
        if last in self.received:
            rem = self.size - last * self.chunk_size
            n += rem
        return n

    def to_dict(self) -> dict:
        return {
            "upload_id": self.upload_id,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "filename": self.filename,
            "size": self.size,
            "source_type": self.source_type,
            "chunk_size": self.chunk_size,
            "received": sorted(self.received),
            "client_key": self.client_key,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> UploadSession:
        return cls(
            upload_id=str(data["upload_id"]),
            project_id=int(data["project_id"]),
            user_id=int(data["user_id"]),
            filename=str(data["filename"]),
            size=int(data["size"]),
            source_type=str(data["source_type"]),
            chunk_size=int(data["chunk_size"]),
            received=[int(x) for x in data.get("received") or []],
            client_key=data.get("client_key"),
            created_at=str(data.get("created_at") or ""),
        )


def uploads_root(settings: Settings, project_id: int) -> Path:
    return project_root(settings, project_id) / "tmp" / "uploads"


def session_dir(settings: Settings, project_id: int, upload_id: str) -> Path:
    return uploads_root(settings, project_id) / upload_id


def _meta_path(folder: Path) -> Path:
    return folder / META_NAME


def _part_path(folder: Path) -> Path:
    return folder / PART_NAME


def _load_session(folder: Path) -> UploadSession:
    data = json.loads(_meta_path(folder).read_text(encoding="utf-8"))
    return UploadSession.from_dict(data)


def _save_session(folder: Path, session: UploadSession) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = _meta_path(folder)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def find_resumable_session(
    settings: Settings,
    *,
    project_id: int,
    user_id: int,
    client_key: str,
) -> UploadSession | None:
    root = uploads_root(settings, project_id)
    if not root.is_dir():
        return None
    for folder in root.iterdir():
        meta = _meta_path(folder)
        if not meta.is_file():
            continue
        try:
            session = _load_session(folder)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if (
            session.user_id == user_id
            and session.client_key
            and session.client_key == client_key
            and len(session.received) < session.total_chunks
        ):
            return session
    return None


def create_session(
    settings: Settings,
    *,
    project_id: int,
    user_id: int,
    filename: str,
    size: int,
    source_type: str,
    chunk_size: int,
    client_key: str | None,
    max_bytes: int,
) -> UploadSession:
    if size < 0:
        raise ValueError("invalid size")
    if size > max_bytes:
        raise ValueError("file too large")
    if chunk_size < 1024:
        raise ValueError("chunk_size too small")

    if client_key:
        existing = find_resumable_session(
            settings, project_id=project_id, user_id=user_id, client_key=client_key
        )
        if existing and existing.size == size and existing.filename == Path(filename).name:
            return existing

    upload_id = f"upl_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
    folder = ensure_inside(settings.data_dir, session_dir(settings, project_id, upload_id))
    folder.mkdir(parents=True, exist_ok=True)
    part = _part_path(folder)
    # Pre-size the part file so chunk writes can seek safely.
    with part.open("wb") as f:
        if size > 0:
            f.truncate(size)

    session = UploadSession(
        upload_id=upload_id,
        project_id=project_id,
        user_id=user_id,
        filename=Path(filename).name,
        size=size,
        source_type=source_type,
        chunk_size=chunk_size,
        received=[],
        client_key=client_key,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _save_session(folder, session)
    return session


def get_session(settings: Settings, project_id: int, upload_id: str) -> UploadSession:
    folder = ensure_inside(settings.data_dir, session_dir(settings, project_id, upload_id))
    if not _meta_path(folder).is_file():
        raise FileNotFoundError("upload session not found")
    return _load_session(folder)


def expected_chunk_size(session: UploadSession, index: int) -> int:
    if index < 0 or index >= session.total_chunks:
        raise ValueError("chunk index out of range")
    start = index * session.chunk_size
    end = min(start + session.chunk_size, session.size)
    return end - start


def write_chunk(
    settings: Settings,
    *,
    project_id: int,
    upload_id: str,
    index: int,
    data: bytes,
    user_id: int,
) -> UploadSession:
    folder = ensure_inside(settings.data_dir, session_dir(settings, project_id, upload_id))
    session = _load_session(folder)
    if session.user_id != user_id:
        raise PermissionError("upload belongs to another user")
    expected = expected_chunk_size(session, index)
    if len(data) != expected:
        raise ValueError(f"chunk size mismatch: got {len(data)}, expected {expected}")

    part = _part_path(folder)
    offset = index * session.chunk_size
    with part.open("r+b") as f:
        f.seek(offset)
        f.write(data)

    if index not in session.received:
        session.received.append(index)
        session.received = sorted(set(session.received))
        _save_session(folder, session)
    return session


def missing_chunks(session: UploadSession) -> list[int]:
    have = set(session.received)
    return [i for i in range(session.total_chunks) if i not in have]


def finalize_part_path(settings: Settings, project_id: int, upload_id: str) -> Path:
    folder = ensure_inside(settings.data_dir, session_dir(settings, project_id, upload_id))
    return _part_path(folder)


def cleanup_session(settings: Settings, project_id: int, upload_id: str) -> None:
    folder = session_dir(settings, project_id, upload_id)
    if not folder.exists():
        return
    for child in folder.iterdir():
        child.unlink(missing_ok=True)
    folder.rmdir()
