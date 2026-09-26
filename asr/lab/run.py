"""Контракт стадии: хеши входов и выходов, честное падение, пропуск по отпечатку.

Три правила, которые здесь закодированы:

1. Нет входа — ``MissingInput`` с именем файла. Подстановки чужого файла нет.
2. Каждый вход и выход записан с sha256 и размером, поэтому по ``stage.json``
   всегда видно, из какого байта какого файла получен результат.
3. Повтор стадии пропускается по отпечатку (хеши входов + параметры), а не по
   времени модификации: mtime врёт после копирования и восстановления.
"""

from __future__ import annotations

import hashlib
import json
import platform
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import STAGES, LabRun, PathEscape, latest_path

_HASH_CHUNK = 1 << 20


class StageFailed(RuntimeError):
    """Стадия не смогла отработать."""


class MissingInput(StageFailed):
    """Обязательного входа нет. Брать файл из другого прогона запрещено."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def require_input(path: Path, *, what: str) -> Path:
    """Вход обязателен. Отсутствие — ошибка, а не повод поискать по соседям."""
    resolved = Path(path)
    if not resolved.is_file():
        raise MissingInput(f"нет входа «{what}»: {resolved}")
    return resolved


@dataclass(frozen=True)
class FileRef:
    path: str
    sha256: str
    bytes: int
    external: bool = False

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}
        if self.external:
            row["external"] = True
        return row

    @property
    def sha8(self) -> str:
        return self.sha256[:8]


def file_ref(run: LabRun, path: Path) -> FileRef:
    """Ссылка на файл. Внутри прогона путь относительный, снаружи — абсолютный."""
    resolved = Path(path).resolve()
    try:
        rel = run.relative(resolved)
        external = False
    except (PathEscape, ValueError):
        rel = resolved.as_posix()
        external = True
    return FileRef(
        path=rel,
        sha256=sha256_file(resolved),
        bytes=resolved.stat().st_size,
        external=external,
    )


def fingerprint(params: dict[str, Any], inputs: Iterable[FileRef]) -> str:
    payload = {
        "params": params,
        "inputs": sorted(({"path": r.path, "sha256": r.sha256} for r in inputs), key=lambda r: r["path"]),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def read_stage(run: LabRun, stage: str) -> dict[str, Any] | None:
    path = run.stage_json(stage)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def outputs_intact(run: LabRun, record: dict[str, Any]) -> bool:
    """Выходы на месте и не поменялись с момента записи stage.json."""
    for row in record.get("outputs") or []:
        if row.get("external"):
            continue
        try:
            path = run.resolve(row["path"])
        except (PathEscape, ValueError, KeyError):
            return False
        if not path.is_file() or path.stat().st_size != row.get("bytes"):
            return False
        if sha256_file(path) != row.get("sha256"):
            return False
    return True


def should_skip(run: LabRun, stage: str, mark: str) -> bool:
    record = read_stage(run, stage)
    if not record or record.get("status") != "ok":
        return False
    if record.get("fingerprint") != mark:
        return False
    return outputs_intact(run, record)


@dataclass
class StageContext:
    run: LabRun
    stage: str
    params: dict[str, Any]
    inputs: dict[str, FileRef]
    outputs: list[FileRef] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    tool: dict[str, Any] = field(default_factory=dict)
    _log: list[str] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return self.run.stage_dir(self.stage)

    def out(self, name: str) -> Path:
        return self.dir / name

    def add_output(self, path: Path) -> FileRef:
        ref = file_ref(self.run, path)
        self.outputs.append(ref)
        return ref

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.out(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.add_output(path)
        return path

    def log(self, message: str) -> None:
        line = f"{_now()} {message}"
        self._log.append(line)
        text = f"[{self.stage}] {message}"
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            print(text.encode("ascii", errors="replace").decode("ascii"), flush=True)

    def flush_log(self) -> None:
        if not self._log:
            return
        path = self.run.stage_log(self.stage)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(self._log) + "\n")
        self._log.clear()


def execute_stage(
    run: LabRun,
    stage: str,
    *,
    inputs: dict[str, Path],
    params: dict[str, Any],
    work: Callable[[StageContext], None],
    force: bool = False,
) -> dict[str, Any]:
    """Прогнать стадию с записью stage.json. Пропуск — только по отпечатку."""
    if stage not in STAGES:
        raise ValueError(f"неизвестная стадия {stage!r}")
    run.ensure()

    refs = {name: file_ref(run, require_input(path, what=name)) for name, path in inputs.items()}
    mark = fingerprint(params, refs.values())

    if not force and should_skip(run, stage, mark):
        record = read_stage(run, stage) or {}
        record["skipped"] = True
        print(f"[{stage}] пропуск: отпечаток совпал", flush=True)
        return record

    ctx = StageContext(run=run, stage=stage, params=params, inputs=refs)
    started = time.monotonic()
    record: dict[str, Any] = {
        "stage": stage,
        "run_id": run.run_id,
        "status": "running",
        "started_at": _now(),
        "fingerprint": mark,
        "params": params,
        "inputs": [{"name": name, **ref.as_dict()} for name, ref in refs.items()],
        "outputs": [],
        "error": None,
    }
    _write_json(run.stage_json(stage), record)

    try:
        work(ctx)
    except Exception as exc:  # noqa: BLE001 - любая поломка стадии фиксируется
        ctx.flush_log()
        record.update(
            status="fail",
            finished_at=_now(),
            elapsed_sec=round(time.monotonic() - started, 2),
            outputs=[r.as_dict() for r in ctx.outputs],
            metrics=ctx.metrics,
            tool=ctx.tool,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8),
        )
        _write_json(run.stage_json(stage), record)
        raise

    ctx.flush_log()
    record.update(
        status="ok",
        finished_at=_now(),
        elapsed_sec=round(time.monotonic() - started, 2),
        outputs=[r.as_dict() for r in ctx.outputs],
        metrics=ctx.metrics,
        tool=ctx.tool,
    )
    _write_json(run.stage_json(stage), record)
    return record


def run_status(run: LabRun) -> dict[str, dict[str, Any]]:
    """Состояние всех стадий для отчёта и UI. Читает только stage.json."""
    rows: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        record = read_stage(run, stage)
        if record is None:
            rows[stage] = {"status": "missing"}
            continue
        rows[stage] = {
            "status": record.get("status"),
            "elapsed_sec": record.get("elapsed_sec"),
            "finished_at": record.get("finished_at"),
            "metrics": record.get("metrics") or {},
            "error": record.get("error"),
            "inputs": record.get("inputs") or [],
            "outputs": record.get("outputs") or [],
            "params": record.get("params") or {},
            "tool": record.get("tool") or {},
        }
    return rows


def init_run(run: LabRun, *, artifact_id: str, project_id: int, source_audio: Path) -> dict[str, Any]:
    """Создать каталог прогона и зафиксировать исходник с хешем."""
    run.ensure()
    source = require_input(source_audio, what="исходный WAV артефакта")
    payload = {
        "run_id": run.run_id,
        "created_at": _now(),
        "artifact_id": artifact_id,
        "project_id": project_id,
        "source": file_ref(run, source).as_dict(),
        "host": {"platform": platform.platform(), "python": platform.python_version()},
    }
    _write_json(run.run_json, payload)
    _write_json(latest_path(run.artifact_root), {"run_id": run.run_id, "updated_at": _now()})
    return payload


def read_run(run: LabRun) -> dict[str, Any]:
    if not run.run_json.is_file():
        raise MissingInput(f"нет run.json: {run.run_json}")
    return json.loads(run.run_json.read_text(encoding="utf-8"))


def source_audio(run: LabRun) -> Path:
    """Исходник прогона из run.json, с проверкой что он не подменился."""
    meta = read_run(run)
    row = meta["source"]
    path = Path(row["path"])
    require_input(path, what="исходный WAV артефакта")
    actual = sha256_file(path)
    if actual != row["sha256"]:
        raise StageFailed(
            f"исходник изменился с начала прогона: run.json={row['sha256'][:12]} диск={actual[:12]}"
        )
    return path
