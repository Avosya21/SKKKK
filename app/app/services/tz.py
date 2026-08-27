from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Changelog, Chunk, Entity, Link, Resolution, UserNote
from app.app.services.llm import chat_completion
from app.app.services.retrieval import artifact_sort_key
from app.app.services.storage import artifact_knowledge_text, parsed_dir, project_root, structured_dir

logger = logging.getLogger("skkkk.tz")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "rebuild_tz.md"
MAP_CHUNK_CHARS = 6000
_ART_ID_IN_PARENS = re.compile(r"\((art_\d{4}-\d{2}-\d{2}_\d{6}_[a-f0-9]+)\)", re.IGNORECASE)


def _artifact_filename_map(db: Session, project_id: int) -> dict[str, str]:
    rows = db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()
    return {a.artifact_id: a.original_filename for a in rows}


def _humanize_artifact_refs(text: str, id_to_name: dict[str, str]) -> str:
    """Заменить (art_…) на (имя файла) в готовой выжимке."""

    def repl(match: re.Match[str]) -> str:
        name = id_to_name.get(match.group(1))
        return f"({name})" if name else match.group(0)

    return _ART_ID_IN_PARENS.sub(repl, text)


def _source_label(art: Artifact, detail: str) -> str:
    return f"{art.original_filename} ({detail})"


def tz_paths(settings: Settings, project_id: int) -> tuple[Path, Path]:
    base = project_root(settings, project_id) / "project_state"
    base.mkdir(parents=True, exist_ok=True)
    versions = base / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    return base / "current_tz.md", versions


def _parse_prompts() -> tuple[str, str, str, str]:
    """rebuild_tz.md: map-sys, map-user, reduce-sys, reduce-user (4 code blocks)."""
    blocks = _read_prompt().split("```")
    if len(blocks) < 8:
        raise RuntimeError(f"{PROMPT_PATH}: expected 4 prompt blocks")
    return tuple(blocks[i].strip() for i in (1, 3, 5, 7))  # type: ignore[return-value]


def _read_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _load_reduce_system() -> str:
    """Legacy: first code block only (dry-run / tests)."""
    raw = _read_prompt()
    blocks = [b.strip() for b in raw.split("```") if b.strip() and not b.strip().startswith("#")]
    return blocks[2] if len(blocks) >= 3 else (blocks[0] if blocks else raw)


def _split_oversized(title: str, body: str, max_chars: int) -> list[tuple[str, str]]:
    if len(body) <= max_chars:
        return [(title, body)]
    paras = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
    if not paras:
        paras = [body]
    normalized: list[str] = []
    for para in paras:
        if len(para) <= max_chars:
            normalized.append(para)
        else:
            normalized.extend(para[i : i + max_chars] for i in range(0, len(para), max_chars))
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    size = 0
    part = 0
    for para in normalized:
        extra = len(para) + (2 if buf else 0)
        if buf and size + extra > max_chars:
            part += 1
            out.append((f"{title} (ч.{part})", "\n\n".join(buf)))
            buf, size = [], 0
        buf.append(para)
        size += extra
    if buf:
        part += 1
        suffix = f" (ч.{part})" if part > 1 or len(out) else ""
        out.append((f"{title}{suffix}", "\n\n".join(buf)))
    return out


def _pack_map_chunks(parts: list[tuple[str, str]], *, max_chars: int = MAP_CHUNK_CHARS) -> list[str]:
    """Собрать фрагменты для map-прохода (~6000 символов)."""
    expanded: list[tuple[str, str]] = []
    for title, body in parts:
        expanded.extend(_split_oversized(title, body.strip(), max_chars))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for title, body in expanded:
        block = f"## {title}\n\n{body}\n"
        if current and size + len(block) > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        if len(block) > max_chars:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(block)
            continue
        current.append(block)
        size += len(block)
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]


def _collect_project_material(db: Session, settings: Settings, project_id: int) -> list[tuple[str, str]]:
    """Материалы для ТЗ: lab ``_final.txt`` для аудио, чанки для документов, сущности, заметки."""
    parts: list[tuple[str, str]] = []

    artifacts = db.scalars(
        select(Artifact)
        .where(Artifact.project_id == project_id, Artifact.status == "done")
    ).all()
    artifacts.sort(key=artifact_sort_key)

    for art in artifacts:
        chunks = db.scalars(
            select(Chunk)
            .where(Chunk.artifact_id == art.id)
            .order_by(Chunk.chunk_index)
        ).all()
        if art.file_type == "audio":
            lab_text = artifact_knowledge_text(settings, art)
            if lab_text:
                parts.append(
                    (
                        _source_label(art, f"стенограмма lab, вес {art.priority_weight}"),
                        lab_text,
                    )
                )
            elif chunks:
                lines = [
                    f"[{c.speaker}] {c.text.strip()}" if c.speaker else c.text.strip() for c in chunks
                ]
                parts.append(
                    (
                        _source_label(art, f"индекс, {len(chunks)} чанков, вес {art.priority_weight}"),
                        "\n\n".join(lines),
                    )
                )
        elif chunks:
            lines = [
                f"[{c.speaker}] {c.text.strip()}" if c.speaker else c.text.strip() for c in chunks
            ]
            parts.append(
                (
                    _source_label(art, f"индекс, {len(chunks)} чанков, вес {art.priority_weight}"),
                    "\n\n".join(lines),
                )
            )
        elif art.transcription_text and art.transcription_text.strip():
            label = _source_label(art, f"текст, без индекса, вес {art.priority_weight}")
            parts.append((label, art.transcription_text.strip()))

        if art.file_type == "audio":
            summary_path = parsed_dir(settings, art.project_id, art.source_type) / f"{art.artifact_id}_call_summary.md"
            if summary_path.is_file():
                summary = summary_path.read_text(encoding="utf-8").strip()
                if summary:
                    parts.append((_source_label(art, "выжимка созвона"), summary))

    entities = db.scalars(
        select(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True))
        .order_by(Entity.id)
    ).all()
    if entities:
        ent_lines = [f"- [{e.entity_type}] {e.value_json}" for e in entities]
        parts.append(("Сущности проекта (БД)", "\n".join(ent_lines)))

    notes = db.scalars(
        select(UserNote).where(UserNote.project_id == project_id).order_by(UserNote.id)
    ).all()
    if notes:
        note_lines = [f"- (note #{n.id}) {n.text.strip()}" for n in notes if n.text.strip()]
        if note_lines:
            parts.append(("Заметки пользователей", "\n".join(note_lines)))

    conflicts = db.scalars(
        select(Link).where(Link.project_id == project_id, Link.link_type == "POTENTIAL_CONFLICT")
    ).all()
    open_lines: list[str] = []
    resolved_lines: list[str] = []
    for lnk in conflicts:
        src = db.get(Entity, lnk.source_entity_id)
        tgt = db.get(Entity, lnk.target_entity_id)
        row = (
            f"entities {lnk.source_entity_id} vs {lnk.target_entity_id}: "
            f"{src.value_json if src else '?'} <> {tgt.value_json if tgt else '?'}"
        )
        res = db.scalar(select(Resolution).where(Resolution.link_id == lnk.id))
        if res:
            resolved_lines.append(f"- {row}; решение: {res.reason}")
        else:
            open_lines.append(f"- {row}")
    if open_lines:
        parts.append(("Открытые конфликты", "\n".join(open_lines)))
    if resolved_lines:
        parts.append(("Разрешённые конфликты", "\n".join(resolved_lines)))

    return parts


def _collect_context(db: Session, settings: Settings, project_id: int) -> str:
    """Плоский контекст для dry-run / отладки."""
    parts = _collect_project_material(db, settings, project_id)
    return "\n\n".join(f"## {title}\n\n{body}" for title, body in parts)


def _build_tz_body(
    settings: Settings,
    *,
    map_chunks: list[str],
    previous_tz: str,
) -> str:
    map_sys, map_user_tmpl, reduce_sys, reduce_user_tmpl = _parse_prompts()

    if len(map_chunks) == 1:
        drafts = map_chunks
    else:
        drafts = []
        for i, chunk in enumerate(map_chunks):
            user = map_user_tmpl.format(i=i + 1, n=len(map_chunks), chunk_text=chunk)
            log_event(logger, "tz.map", chunk=i + 1, total=len(map_chunks), chars=len(chunk))
            drafts.append(
                chat_completion(settings, system=map_sys, user=user, max_tokens=1200, temperature=0.2)
            )

    reduce_user = reduce_user_tmpl.format(
        drafts="\n\n---\n\n".join(drafts),
        previous_tz=previous_tz.strip() or "(нет)",
    )
    log_event(logger, "tz.reduce", drafts=len(drafts), prev_chars=len(previous_tz))
    return chat_completion(settings, system=reduce_sys, user=reduce_user, max_tokens=8192, temperature=0.2)


def rebuild_tz(db: Session, settings: Settings, project_id: int, *, manual: bool = False, summary: str = "") -> Path:
    chunk_count = db.scalar(
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.project_id == project_id)
    ) or 0
    log_event(
        logger,
        "tz.rebuild.start",
        project_id=project_id,
        manual=manual,
        dry_run=settings.dry_run,
        indexed_chunks=chunk_count,
    )
    current, versions = tz_paths(settings, project_id)
    prev = current.read_text(encoding="utf-8") if current.is_file() else ""

    material = _collect_project_material(db, settings, project_id)
    if not material:
        log_event(logger, "tz.rebuild.empty", project_id=project_id)
        body = "# Итоговая выжимка по проекту\n\nМатериалов для сборки пока нет (нет готовых артефактов с индексом).\n"
    elif settings.dry_run:
        context = _collect_context(db, settings, project_id)
        body = f"# Итоговая выжимка по проекту (dry run)\n\n{context[:8000]}\n"
    else:
        map_chunks = _pack_map_chunks(material)
        log_event(
            logger,
            "tz.rebuild.material",
            project_id=project_id,
            sections=len(material),
            map_chunks=len(map_chunks),
            indexed_chunks=chunk_count,
        )
        body = _build_tz_body(settings, map_chunks=map_chunks, previous_tz=prev)

    from app.app.services.plain_text import clean_summary_output

    body = clean_summary_output(body)
    body = _humanize_artifact_refs(body, _artifact_filename_map(db, project_id))
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    if current.is_file() and prev.strip():
        archive = versions / f"current_tz_{stamp}.md"
        archive.write_text(prev, encoding="utf-8")
    current.write_text(body.strip() + "\n", encoding="utf-8")
    arts = db.scalars(select(Artifact.artifact_id).where(Artifact.project_id == project_id)).all()
    db.add(
        Changelog(
            project_id=project_id,
            version_label=stamp,
            artifact_ids_json=str(list(arts)),
            summary_of_changes=summary or ("manual rebuild" if manual else "auto rebuild"),
            tz_path=str(current),
        )
    )
    db.commit()
    log_event(
        logger,
        "tz.rebuild.done",
        project_id=project_id,
        version=stamp,
        chars=len(body),
        path=current.name,
    )
    return current


def maybe_auto_rebuild_tz(db: Session, settings: Settings, artifact: Artifact) -> None:
    from app.app.services.project_tasks import TASK_TZ_REBUILD, get_running_task, track_project_task

    if get_running_task(db, task_type=TASK_TZ_REBUILD, project_id=artifact.project_id):
        log_event(
            logger,
            "tz.rebuild.skipped",
            project_id=artifact.project_id,
            artifact=artifact.artifact_id,
            reason="already_running",
        )
        return

    if artifact.file_type != "audio":
        with track_project_task(
            db,
            task_type=TASK_TZ_REBUILD,
            project_id=artifact.project_id,
        ):
            rebuild_tz(
                db,
                settings,
                artifact.project_id,
                summary=f"auto after document {artifact.artifact_id}",
            )
        return

    n = settings.tz_rebuild_every_n
    count = db.scalar(
        select(func.count())
        .select_from(Artifact)
        .where(Artifact.project_id == artifact.project_id, Artifact.status == "done")
    )
    if count and count % n == 0:
        with track_project_task(
            db,
            task_type=TASK_TZ_REBUILD,
            project_id=artifact.project_id,
        ):
            rebuild_tz(db, settings, artifact.project_id, summary=f"auto after {count} done artifacts")
    else:
        log_event(
            logger,
            "tz.rebuild.skipped",
            project_id=artifact.project_id,
            artifact=artifact.artifact_id,
            done_artifacts=count or 0,
            every_n=n,
        )
