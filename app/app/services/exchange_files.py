"""Посылки файлов от внешнего агента: манифест, приём частями с докачкой и проверкой sha256, выдача частями.

Границы (docs/agent-exchange/POLICY.md §10):
- право ключа files_access: write — открыть посылку и класть части (и читать); read — список, манифест, чтение принятых файлов; none — ничего;
- путь — только относительный внутри посылки: части через «/», без «..», диска, обратной косой и служебных имён Windows;
- принимаются только файлы из манифеста и ровно заявленного размера; файл принят, когда sha256 сошёлся целиком;
- только добавление: принятое не перезаписывается, новая версия — новая посылка; удаляет посылку только владелец проекта;
- место: посылка не открывается, если после неё на диске останется меньше exchange_files_reserve_gb;
- принятая целиком посылка сама раскладывается в рабочую копию проекта (exchange_unpack.schedule_unpack, фоном);
- журнал: открытие, приём целиком, отказы и удаление пишутся в agent_exchange_log (части — нет, их состояние в exchange_files).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.agent_exchange import AgentExchangeLog
from app.app.models.exchange_file import ExchangeBundle, ExchangeFile

KINDS = ("config_map", "entity_deepmap", "other")
HTTP_CHUNK_MAX = 64 * 1024 * 1024
MCP_CHUNK_MAX = 1024 * 1024
MAX_FILES = 10000
MAX_PATH = 200
MAX_PART = 120
CHUNK_MIN_FREE = 2 * 1024 ** 3  # часть не пишется, если после неё на диске останется меньше 2 ГБ
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PART_RE = re.compile(r"^[\w .()+,-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


class FilesError(ValueError):
    """Отказ с машинным кодом (detail в ответе инструмента/HTTP)."""

    def __init__(self, message: str, code: str = "bad_request", **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock(file_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(file_id, threading.Lock())


def base_dir(settings: Settings) -> Path:
    return Path(settings.exchange_files_dir) if settings.exchange_files_dir else Path(settings.data_dir) / "exchange_files"


def _bundle_dir(settings: Settings, project_id: int, bundle_id: int) -> Path:
    return base_dir(settings) / f"p{project_id}" / f"b{bundle_id}"


def _final_path(settings: Settings, f: ExchangeFile, project_id: int) -> Path:
    root = _bundle_dir(settings, project_id, f.bundle_id) / "files"
    target = root.joinpath(*f.path.split("/"))
    # путь уже проверен при открытии; здесь вторая страховка от выхода из папки посылки
    if root.resolve() not in target.resolve().parents:
        raise FilesError("путь вне посылки", "bad_path")
    return target


def _part_path(settings: Settings, f: ExchangeFile, project_id: int) -> Path:
    return _bundle_dir(settings, project_id, f.bundle_id) / "incoming" / f"{f.id}.part"


def _source(token: Any) -> str:
    return f"mcp:{getattr(token, 'id', '?')}" if token is not None else "owner"


def require_files(token: Any, need: str) -> None:
    """need: read | write. Нет права — PermissionError с кодом files_not_allowed."""
    from app.app.services.project_mcp_tokens import token_files_access

    have = token_files_access(token) if token is not None else "none"
    if have == "write" or (need == "read" and have == "read"):
        return
    exc = PermissionError("У ключа нет права на файлы (нужно «%s»). Право выдаёт владелец проекта в таблице ключей MCP." % ("приём и чтение файлов" if need == "write" else "чтение файлов"))
    exc.code = "files_not_allowed"  # type: ignore[attr-defined]
    raise exc


def _log(db: Session, project_id: int, source: str, kind: str, decision: str, *, reasons: list[str] | None = None,
         bundle: ExchangeBundle | None = None, size: int = 0, response: dict | None = None) -> None:
    db.add(AgentExchangeLog(
        project_id=project_id, source=source[:64], kind=kind, request_id=f"bundle:{bundle.id}" if bundle is not None else None,
        decision=decision, risk=0 if decision == "accepted" else 20, reasons_json=json.dumps(reasons or [], ensure_ascii=False)[:8000],
        payload_sha=(bundle.manifest_sha[:40] if bundle is not None else ""), size_bytes=min(size, 2 ** 62),
        response_json=json.dumps(response, ensure_ascii=False)[:8000] if response is not None else None,
    ))
    db.commit()


def normalize_path(raw: Any) -> str:
    """Относительный путь внутри посылки: части через «/», без «..», диска, обратной косой, служебных имён Windows."""
    if not isinstance(raw, str) or not raw:
        raise FilesError("path: пустой или не строка", "bad_path")
    if "\\" in raw or ":" in raw or raw.startswith("/") or len(raw) > MAX_PATH:
        raise FilesError(f"path: только относительный путь через «/», без диска и обратной косой, до {MAX_PATH} знаков: {raw[:80]}", "bad_path")
    parts = raw.split("/")
    for part in parts:
        if part in ("", ".", "..") or len(part) > MAX_PART or not PART_RE.match(part) or part != part.strip() or part.endswith("."):
            raise FilesError(f"path: недопустимая часть «{part[:40]}» в {raw[:80]}", "bad_path")
        if part.split(".")[0].lower() in WIN_RESERVED:
            raise FilesError(f"path: служебное имя Windows «{part}»", "bad_path")
    return raw


def _normalize_manifest(files: Any) -> list[dict[str, Any]]:
    if not isinstance(files, list) or not files:
        raise FilesError("files: нужен непустой список {path, size, sha256}", "bad_manifest")
    if len(files) > MAX_FILES:
        raise FilesError(f"files: не больше {MAX_FILES} файлов в посылке", "bad_manifest")
    out, seen, dirs = [], set(), set()
    for i, item in enumerate(files):
        if not isinstance(item, dict) or set(item) - {"path", "size", "sha256"}:
            raise FilesError(f"files[{i}]: ожидается объект {{path, size, sha256}}", "bad_manifest")
        path = normalize_path(item.get("path"))
        size, sha = item.get("size"), str(item.get("sha256") or "").lower()
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise FilesError(f"files[{i}].size: целое число байт ≥ 0", "bad_manifest")
        if not SHA_RE.match(sha):
            raise FilesError(f"files[{i}].sha256: 64 шестнадцатеричных знака", "bad_manifest")
        key = path.casefold()  # Windows не различает регистр
        if key in seen:
            raise FilesError(f"files[{i}]: путь повторяется {path}", "bad_manifest")
        seen.add(key)
        parts = key.split("/")
        dirs.update("/".join(parts[:j]) for j in range(1, len(parts)))
        out.append({"path": path, "size": size, "sha256": sha})
    clash = seen & dirs
    if clash:
        raise FilesError(f"files: путь одновременно файл и папка: {sorted(clash)[0]}", "bad_manifest")
    return sorted(out, key=lambda x: x["path"])


def _manifest_sha(files: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _free_bytes(settings: Settings) -> int:
    base = base_dir(settings)
    probe = base
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def get_bundle(db: Session, project_id: int, bundle_id: Any) -> ExchangeBundle:
    try:
        bid = int(bundle_id)
    except (TypeError, ValueError):
        raise FilesError("bundle_id: целое число", "bad_request") from None
    row = db.get(ExchangeBundle, bid)
    if not row or row.project_id != project_id or row.status == "deleted":
        raise FilesError(f"Посылка {bundle_id} не найдена", "not_found")
    return row


def _files(db: Session, bundle_id: int) -> list[ExchangeFile]:
    return list(db.scalars(select(ExchangeFile).where(ExchangeFile.bundle_id == bundle_id).order_by(ExchangeFile.path)).all())


def _file(db: Session, bundle: ExchangeBundle, path: Any) -> ExchangeFile:
    norm = normalize_path(path)
    row = db.scalar(select(ExchangeFile).where(ExchangeFile.bundle_id == bundle.id, ExchangeFile.path == norm))
    if not row:
        raise FilesError(f"Файла {norm} нет в манифесте посылки {bundle.id}", "not_in_manifest")
    return row


def bundle_dict(db: Session, bundle: ExchangeBundle, *, files: str = "all") -> dict[str, Any]:
    """files: all — весь манифест с состоянием; missing — только непринятые; none — без списка."""
    rows = _files(db, bundle.id)
    done = [f for f in rows if f.status == "complete"]
    out: dict[str, Any] = {
        "bundle_id": bundle.id, "name": bundle.name, "kind": bundle.kind, "note": bundle.note, "status": bundle.status,
        "file_count": bundle.file_count, "total_bytes": bundle.total_bytes,
        "files_complete": len(done), "bytes_received": sum(f.received_bytes for f in rows),
        "manifest_sha256": bundle.manifest_sha,
        "created_at": bundle.created_at.isoformat() if bundle.created_at else None,
        "completed_at": bundle.completed_at.isoformat() if bundle.completed_at else None,
    }
    if files != "none":
        pick = rows if files == "all" else [f for f in rows if f.status != "complete"]
        out["files"] = [{"path": f.path, "size": f.size, "sha256": f.sha256, "received": f.received_bytes, "status": f.status} for f in pick]
    return out


def open_bundle(db: Session, settings: Settings, project_id: int, token: Any, *, name: Any, files: Any,
                kind: Any = "other", note: Any = None) -> dict[str, Any]:
    """Открыть посылку по манифесту. Повтор с тем же именем и тем же манифестом возвращает её состояние (докачка)."""
    require_files(token, "write")
    source = _source(token)
    try:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise FilesError("name: латиница, цифры, . _ -, до 64 знаков, например config_map_2026-10-08", "bad_request")
        kind = kind or "other"
        if kind not in KINDS:
            raise FilesError(f"kind: одно из {', '.join(KINDS)}", "bad_request")
        manifest = _normalize_manifest(files)
        msha = _manifest_sha(manifest)
        existing = db.scalar(select(ExchangeBundle).where(ExchangeBundle.project_id == project_id, ExchangeBundle.name == name))
        if existing is not None:
            if existing.manifest_sha == msha and existing.status != "deleted":
                return {**bundle_dict(db, existing, files="missing"), "resumed": True}
            raise FilesError(f"Имя посылки {name} уже занято другим манифестом; новая версия — новое имя", "name_taken")
        total = sum(f["size"] for f in manifest)
        if total > settings.exchange_bundle_max_gb * 1024 ** 3:
            raise FilesError(f"Посылка {total} байт больше предела {settings.exchange_bundle_max_gb} ГБ", "too_large")
        free = _free_bytes(settings)
        if free - total < settings.exchange_files_reserve_gb * 1024 ** 3:
            raise FilesError(f"Мало места на сервере: свободно {free // 1024 ** 3} ГБ, посылка {total / 1024 ** 3:.1f} ГБ, "
                             f"запас {settings.exchange_files_reserve_gb} ГБ. Напиши вопрос через submit_question.", "disk_full")
    except FilesError as exc:
        _log(db, project_id, source, "files_open", "rejected", reasons=[f"{exc.code}: {exc}"])
        raise
    bundle = ExchangeBundle(project_id=project_id, name=name, kind=kind, note=(str(note)[:2000] if note else None), status="open",
                            manifest_sha=msha, total_bytes=total, file_count=len(manifest), token_id=getattr(token, "id", None), source=source)
    db.add(bundle)
    db.flush()
    for f in manifest:
        db.add(ExchangeFile(bundle_id=bundle.id, path=f["path"], size=f["size"], sha256=f["sha256"]))
    db.commit()
    (_bundle_dir(settings, project_id, bundle.id) / "incoming").mkdir(parents=True, exist_ok=True)
    # пустые файлы принимаются сразу, если их хеш — хеш пустой строки
    for f in _files(db, bundle.id):
        if f.size == 0:
            _finish_file(db, settings, project_id, bundle, f)
    _log(db, project_id, source, "files_open", "accepted", bundle=bundle, size=total,
         response={"name": name, "kind": kind, "files": len(manifest), "total_bytes": total})
    return {**bundle_dict(db, bundle, files="missing"), "resumed": False}


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(8 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def _finish_file(db: Session, settings: Settings, project_id: int, bundle: ExchangeBundle, f: ExchangeFile) -> bool:
    """Сверить sha256 принятого целиком файла; совпал — перенести на место, не совпал — сбросить приём файла."""
    part = _part_path(settings, f, project_id)
    if f.size == 0:
        part.parent.mkdir(parents=True, exist_ok=True)
        part.touch()
    if _hash_file(part) != f.sha256:
        part.unlink(missing_ok=True)
        f.received_bytes, f.status = 0, "pending"
        db.commit()
        return False
    final = _final_path(settings, f, project_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    part.replace(final)
    f.status, f.received_bytes, f.completed_at = "complete", f.size, _now()
    db.commit()
    _maybe_complete(db, settings, project_id, bundle)
    return True


def _maybe_complete(db: Session, settings: Settings, project_id: int, bundle: ExchangeBundle) -> None:
    if bundle.status != "open":
        return
    rows = _files(db, bundle.id)
    if all(f.status == "complete" for f in rows):
        bundle.status, bundle.completed_at = "complete", _now()
        db.commit()
        _log(db, project_id, bundle.source, "files_complete", "accepted", bundle=bundle, size=bundle.total_bytes,
             response={"name": bundle.name, "files": len(rows), "total_bytes": bundle.total_bytes})
        if settings.exchange_auto_unpack:
            from app.app.services.exchange_unpack import schedule_unpack

            schedule_unpack(db, settings, project_id, bundle.id)


def put_chunk(db: Session, settings: Settings, project_id: int, token: Any, bundle_id: Any, path: Any, offset: Any,
              data: bytes, *, chunk_sha256: str | None = None, max_chunk: int = HTTP_CHUNK_MAX) -> dict[str, Any]:
    """Дописать часть файла с позиции offset. offset обязан равняться уже принятому; после обрыва бери received из ответа или статуса."""
    require_files(token, "write")
    bundle = get_bundle(db, project_id, bundle_id)
    if bundle.status != "open":
        raise FilesError(f"Посылка {bundle.id} уже {bundle.status}: принятое не перезаписывается", "bundle_closed")
    f = _file(db, bundle, path)
    if f.status == "complete":
        return {"path": f.path, "received": f.size, "size": f.size, "status": "complete", "bundle_status": bundle.status}
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise FilesError("offset: целое ≥ 0", "bad_request")
    if not data:
        raise FilesError("пустая часть", "bad_request")
    if len(data) > max_chunk:
        raise FilesError(f"часть {len(data)} байт больше предела {max_chunk}", "chunk_too_large")
    if chunk_sha256 and hashlib.sha256(data).hexdigest() != str(chunk_sha256).lower():
        raise FilesError("sha256 части не совпал: часть повреждена в пути, отправь её снова", "chunk_hash_mismatch")
    with _lock(f.id):
        db.refresh(f)
        part = _part_path(settings, f, project_id)
        have = part.stat().st_size if part.exists() else 0
        if have != f.received_bytes:  # истина — файл на диске (например, после падения сервера между записью и коммитом)
            f.received_bytes = have
            db.commit()
        if offset != have:
            raise FilesError(f"offset {offset} не совпал с принятым {have}: продолжай с {have}", "offset_mismatch", expected_offset=have)
        if have + len(data) > f.size:
            raise FilesError(f"часть выходит за заявленный размер {f.size}", "beyond_size")
        if _free_bytes(settings) - len(data) < CHUNK_MIN_FREE:
            raise FilesError("на диске сервера кончается место; приём остановлен", "disk_full")
        part.parent.mkdir(parents=True, exist_ok=True)
        with part.open("ab") as fh:
            fh.write(data)
        f.received_bytes = have + len(data)
        db.commit()
        status, ok = "pending", True
        if f.received_bytes == f.size:
            ok = _finish_file(db, settings, project_id, bundle, f)
            status = "complete" if ok else "hash_mismatch"
    db.refresh(bundle)
    out = {"path": f.path, "received": f.received_bytes, "size": f.size, "status": status, "bundle_status": bundle.status}
    if not ok:
        out["note"] = "sha256 файла целиком не совпал с манифестом; приём файла сброшен, отправь его заново с offset 0"
    return out


def list_bundles(db: Session, project_id: int, token: Any = None) -> list[dict[str, Any]]:
    """Посылки проекта. Ключу только на чтение — только принятые целиком."""
    from app.app.services.project_mcp_tokens import token_files_access

    q = select(ExchangeBundle).where(ExchangeBundle.project_id == project_id, ExchangeBundle.status != "deleted")
    if token is not None and token_files_access(token) == "read":
        q = q.where(ExchangeBundle.status == "complete")
    return [bundle_dict(db, b, files="none") for b in db.scalars(q.order_by(ExchangeBundle.id.desc())).all()]


def readable_file(db: Session, settings: Settings, project_id: int, token: Any, bundle_id: Any, path: Any) -> tuple[ExchangeFile, Path]:
    """Принятый файл для чтения (проверка права read, статуса и наличия на диске)."""
    if token is not None:
        require_files(token, "read")
    bundle = get_bundle(db, project_id, bundle_id)
    f = _file(db, bundle, path)
    if f.status != "complete":
        raise FilesError(f"Файл {f.path} ещё не принят целиком ({f.received_bytes} из {f.size})", "not_ready")
    target = _final_path(settings, f, project_id)
    if not target.is_file():
        raise FilesError(f"Файл {f.path} отсутствует на диске сервера", "missing_on_disk")
    return f, target


def read_chunk(db: Session, settings: Settings, project_id: int, token: Any, bundle_id: Any, path: Any, offset: Any,
               length: Any = MCP_CHUNK_MAX) -> tuple[ExchangeFile, bytes]:
    f, target = readable_file(db, settings, project_id, token, bundle_id, path)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > f.size:
        raise FilesError(f"offset: целое от 0 до {f.size}", "bad_request")
    try:
        length = min(int(length or MCP_CHUNK_MAX), MCP_CHUNK_MAX)
    except (TypeError, ValueError):
        raise FilesError("length: целое", "bad_request") from None
    with target.open("rb") as fh:
        fh.seek(offset)
        return f, fh.read(max(length, 0))


def delete_bundle(db: Session, settings: Settings, project_id: int, bundle_id: Any, *, by: str) -> dict[str, Any]:
    """Удалить посылку с диска (решение владельца проекта, не агента). Строки остаются со статусом deleted — для журнала."""
    bundle = get_bundle(db, project_id, bundle_id)
    shutil.rmtree(_bundle_dir(settings, project_id, bundle.id), ignore_errors=True)
    bundle.status = "deleted"
    db.commit()
    _log(db, project_id, by, "files_delete", "accepted", bundle=bundle, size=bundle.total_bytes, response={"name": bundle.name})
    return {"bundle_id": bundle.id, "status": "deleted"}
