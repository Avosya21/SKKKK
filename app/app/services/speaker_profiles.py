from __future__ import annotations

import json
import logging
import struct
from pathlib import Path

from sqlalchemy.orm import Session

from app.app.config import ROOT
from app.app.models.artifact import Artifact
from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile

logger = logging.getLogger(__name__)

# Re-use embedding helpers from ASR layer
import sys

_ASR = ROOT / "asr"
if str(_ASR) not in sys.path:
    sys.path.insert(0, str(_ASR))
from speaker_registry import average_embeddings, unpack_embedding  # noqa: E402


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def match_json_path(project_id: int, artifact_id: str) -> Path:
    return artifact_dir_path(project_id, artifact_id) / "reports" / "sortformer_speaker_match.json"


def artifact_dir_path(project_id: int, artifact_id: str) -> Path:
    return ROOT / "data" / "projects" / str(project_id) / "artifacts" / artifact_id


def list_profiles(db: Session, project_id: int) -> list[dict]:
    rows = (
        db.query(SpeakerProfile)
        .filter(
            (SpeakerProfile.scope == "global")
            | ((SpeakerProfile.scope == "project") & (SpeakerProfile.project_id == project_id))
        )
        .order_by(SpeakerProfile.display_name)
        .all()
    )
    return [{"id": r.id, "display_name": r.display_name, "scope": r.scope} for r in rows]


def load_match_payload(project_id: int, artifact_id: str) -> dict:
    path = match_json_path(project_id, artifact_id)
    if not path.is_file():
        raise FileNotFoundError(f"match json missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_embedding(payload: dict, diar_label: str) -> tuple[str, list[float]]:
    merge_map = payload.get("merge_map") or {}
    embeddings = payload.get("embeddings") or {}
    canon = merge_map.get(diar_label, diar_label)
    if canon in embeddings:
        return canon, embeddings[canon]
    if diar_label in embeddings:
        return diar_label, embeddings[diar_label]
    for alias, target in merge_map.items():
        if target == diar_label and alias in embeddings:
            return alias, embeddings[alias]
    raise ValueError(f"нет embedding для {diar_label}")


def _patch_match_payload(payload: dict, *, canonical: str, profile: SpeakerProfile, status: str = "confirmed") -> None:
    matches = payload.setdefault("matches", {})
    row = matches.setdefault(canonical, {})
    row.update(
        {
            "diar_label": canonical,
            "profile_id": profile.id,
            "profile_name": profile.display_name,
            "score": 1.0,
            "status": status,
        }
    )
    display = payload.setdefault("display_names", {})
    display[canonical] = profile.display_name


def _find_profile_by_name(db: Session, project_id: int, display_name: str) -> SpeakerProfile | None:
    name = display_name.strip()
    return (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.scope == "project",
            SpeakerProfile.project_id == project_id,
            SpeakerProfile.display_name == name,
        )
        .order_by(SpeakerProfile.id)
        .first()
    )


def _blend_embedding(profile: SpeakerProfile, new_vec: list[float]) -> None:
    old = unpack_embedding(profile.embedding)
    blended = average_embeddings(old, new_vec) if old else new_vec
    profile.embedding = pack_embedding(blended)
    profile.embedding_dim = len(blended)


def consolidate_duplicate_name_profiles(db: Session, project_id: int) -> dict[str, int]:
    """Один display_name → один profile_id; дубликаты сливаются."""
    rows = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.scope == "project", SpeakerProfile.project_id == project_id)
        .order_by(SpeakerProfile.id)
        .all()
    )
    by_name: dict[str, list[SpeakerProfile]] = {}
    for row in rows:
        by_name.setdefault(row.display_name.strip(), []).append(row)

    merged = 0
    for group in by_name.values():
        if len(group) < 2:
            continue
        keep = group[0]
        for dup in group[1:]:
            _blend_embedding(keep, unpack_embedding(dup.embedding))
            (
                db.query(ArtifactSpeakerLink)
                .filter(ArtifactSpeakerLink.profile_id == dup.id)
                .update(
                    {
                        ArtifactSpeakerLink.profile_id: keep.id,
                        ArtifactSpeakerLink.suggested_profile_id: keep.id,
                        ArtifactSpeakerLink.status: "confirmed",
                        ArtifactSpeakerLink.match_score: 1.0,
                    },
                    synchronize_session=False,
                )
            )
            db.delete(dup)
            merged += 1
    if merged:
        db.commit()
    return {"merged": merged}


def _apply_profile_merge_to_payload(payload: dict) -> None:
    import sys as _sys

    asr = str(ROOT / "asr")
    if asr not in _sys.path:
        _sys.path.insert(0, asr)
    from speaker_registry import merge_dup_map_by_profile_id  # noqa: PLC0415

    merge_map = payload.setdefault("merge_map", {k: k for k in (payload.get("embeddings") or {})})
    matches = payload.get("matches") or {}
    merge_dup_map_by_profile_id(merge_map, matches)
    payload["merge_map"] = merge_map


def _load_voice_profiles(db: Session, project_id: int) -> list[dict]:
    rows = (
        db.query(SpeakerProfile)
        .filter(
            (SpeakerProfile.scope == "global")
            | ((SpeakerProfile.scope == "project") & (SpeakerProfile.project_id == project_id))
        )
        .all()
    )
    return [{"id": r.id, "display_name": r.display_name, "embedding": r.embedding} for r in rows]


def enroll_speaker(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    diar_label: str,
    display_name: str,
) -> SpeakerProfile:
    import sys

    asr = str(ROOT / "asr")
    if asr not in sys.path:
        sys.path.insert(0, asr)
    from speaker_registry import VOICE_AUTO_LINK_THRESHOLD, best_profile_match  # noqa: PLC0415

    payload = load_match_payload(project_id, artifact_id)
    canonical, emb = _resolve_embedding(payload, diar_label)
    match = (payload.get("matches") or {}).get(canonical) or (payload.get("matches") or {}).get(diar_label) or {}
    name = display_name.strip()
    best_id, _best_name, best_score = best_profile_match(emb, _load_voice_profiles(db, project_id))
    if not name:
        if best_id and best_score is not None and best_score >= VOICE_AUTO_LINK_THRESHOLD:
            prof = db.get(SpeakerProfile, best_id)
            name = prof.display_name if prof else ""
        if not name:
            sug = match.get("suggested_profile_name")
            if sug:
                name = str(sug).strip()
    if not name:
        raise ValueError("имя не может быть пустым")

    link = (
        db.query(ArtifactSpeakerLink)
        .filter(
            ArtifactSpeakerLink.artifact_id == artifact_id,
            ArtifactSpeakerLink.diar_label.in_([diar_label, canonical]),
        )
        .first()
    )
    was_already_confirmed = bool(
        link is not None and link.profile_id and (link.status or "") == "confirmed"
    )
    profile: SpeakerProfile | None = None
    if link and link.profile_id:
        profile = db.get(SpeakerProfile, link.profile_id)

    if profile is None:
        if best_id and best_score is not None and best_score >= VOICE_AUTO_LINK_THRESHOLD:
            profile = db.get(SpeakerProfile, best_id)
            if profile:
                name = profile.display_name
        if profile is None:
            profile = SpeakerProfile(
                scope="project",
                project_id=project_id,
                display_name=name,
                embedding=pack_embedding(emb),
                embedding_dim=len(emb),
                source_artifact_id=artifact_id,
                source_start_sec=match.get("clip_start"),
                source_end_sec=match.get("clip_end"),
            )
            db.add(profile)
            db.flush()
        else:
            _blend_embedding(profile, emb)
            profile.source_artifact_id = artifact_id
            profile.source_start_sec = match.get("clip_start")
            profile.source_end_sec = match.get("clip_end")
    else:
        profile.display_name = name
        _blend_embedding(profile, emb)
        profile.source_artifact_id = artifact_id
        profile.source_start_sec = match.get("clip_start")
        profile.source_end_sec = match.get("clip_end")

    if link is None:
        link = ArtifactSpeakerLink(
            artifact_id=artifact_id,
            project_id=project_id,
            diar_label=diar_label if diar_label in (payload.get("embeddings") or {}) else canonical,
        )
        db.add(link)
    link.diar_label = diar_label if diar_label in (payload.get("matches") or {}) else canonical
    link.profile_id = profile.id
    link.suggested_profile_id = profile.id
    link.match_score = 1.0
    link.status = "confirmed"
    link.clip_start_sec = match.get("clip_start")
    link.clip_end_sec = match.get("clip_end")
    db.commit()
    db.refresh(profile)
    consolidate_duplicate_name_profiles(db, project_id)

    _patch_match_payload(payload, canonical=canonical, profile=profile)
    merge_map = payload.get("merge_map") or {}
    matches = payload.setdefault("matches", {})
    display = payload.setdefault("display_names", {})
    # Только этот Sortformer ID (и уже склеенные с ним) — не все с тем же profile_id.
    group = {canonical} | {lbl for lbl, tgt in merge_map.items() if tgt == canonical}
    for lbl in group:
        row = matches.setdefault(lbl, {"diar_label": lbl})
        row.update(
            {
                "profile_id": profile.id,
                "profile_name": profile.display_name,
                "score": 1.0,
                "status": "merged_duplicate" if lbl != canonical else "confirmed",
            }
        )
        display[lbl] = profile.display_name
    match_json_path(project_id, artifact_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _safe_rebuild_artifact_views(db, project_id=project_id, artifact_id=artifact_id)
    if not was_already_confirmed:
        try:
            reaudit_voices_and_reindex(db, project_id=project_id, artifact_id=artifact_id)
        except Exception:
            logger.exception("voice reaudit after enroll failed for %s", artifact_id)
    return profile


def _upsert_confirmed_link(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    diar_label: str,
    profile_id: int,
    clip_start: float | None = None,
    clip_end: float | None = None,
) -> ArtifactSpeakerLink:
    link = (
        db.query(ArtifactSpeakerLink)
        .filter(
            ArtifactSpeakerLink.artifact_id == artifact_id,
            ArtifactSpeakerLink.diar_label == diar_label,
        )
        .first()
    )
    if link is None:
        link = ArtifactSpeakerLink(
            artifact_id=artifact_id,
            project_id=project_id,
            diar_label=diar_label,
        )
        db.add(link)
    link.profile_id = profile_id
    link.suggested_profile_id = profile_id
    link.match_score = 1.0
    link.status = "confirmed"
    if clip_start is not None:
        link.clip_start_sec = clip_start
    if clip_end is not None:
        link.clip_end_sec = clip_end
    return link


def merge_speakers(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    source_diar_label: str,
    target_diar_label: str,
) -> dict:
    """Ручное объединение двух Sortformer ID в один профиль."""
    if source_diar_label == target_diar_label:
        raise ValueError("нельзя объединить спикера с самим собой")

    payload = load_match_payload(project_id, artifact_id)
    matches = payload.setdefault("matches", {})
    merge_map = payload.setdefault("merge_map", {k: k for k in (payload.get("embeddings") or {})})

    known = set(matches) | set(merge_map)
    if source_diar_label not in known:
        raise ValueError(f"нет {source_diar_label}")
    if target_diar_label not in known:
        raise ValueError(f"нет {target_diar_label}")

    source_canon = merge_map.get(source_diar_label, source_diar_label)
    target_canon = merge_map.get(target_diar_label, target_diar_label)
    if source_canon == target_canon:
        raise ValueError("спикеры уже объединены")

    target_match = matches.get(target_diar_label) or matches.get(target_canon) or {}
    target_profile_id = target_match.get("profile_id")
    target_link = (
        db.query(ArtifactSpeakerLink)
        .filter(
            ArtifactSpeakerLink.artifact_id == artifact_id,
            ArtifactSpeakerLink.diar_label.in_([target_diar_label, target_canon]),
            ArtifactSpeakerLink.status == "confirmed",
            ArtifactSpeakerLink.profile_id.isnot(None),
        )
        .first()
    )
    if target_profile_id is None and target_link:
        target_profile_id = target_link.profile_id
    if target_profile_id is None:
        raise ValueError("сначала запомните целевого спикера (в него объединяем)")

    profile = db.get(SpeakerProfile, target_profile_id)
    if profile is None:
        raise ValueError("профиль не найден")

    try:
        _, source_emb = _resolve_embedding(payload, source_diar_label)
        _blend_embedding(profile, source_emb)
    except ValueError:
        pass

    for lbl in {source_diar_label, source_canon}:
        sm = matches.get(lbl) or matches.get(source_canon) or {}
        _upsert_confirmed_link(
            db,
            project_id=project_id,
            artifact_id=artifact_id,
            diar_label=lbl,
            profile_id=profile.id,
            clip_start=sm.get("clip_start"),
            clip_end=sm.get("clip_end"),
        )

    db.commit()
    db.refresh(profile)

    for key in list(merge_map):
        if merge_map[key] == source_canon or key in {source_diar_label, source_canon}:
            merge_map[key] = target_canon
    merge_map[source_diar_label] = target_canon
    merge_map[source_canon] = target_canon

    for lbl in {source_diar_label, source_canon}:
        if lbl not in matches and lbl in (payload.get("embeddings") or {}):
            src = matches.get(source_canon) or target_match
            matches[lbl] = {
                "diar_label": lbl,
                "clip_start": src.get("clip_start"),
                "clip_end": src.get("clip_end"),
                "clip_duration": src.get("clip_duration"),
            }
        if lbl in matches:
            matches[lbl].update(
                {
                    "profile_id": profile.id,
                    "profile_name": profile.display_name,
                    "score": 1.0,
                    "status": "merged_duplicate",
                }
            )

    _patch_match_payload(payload, canonical=target_canon, profile=profile)
    display = payload.setdefault("display_names", {})
    display[target_canon] = profile.display_name
    _apply_profile_merge_to_payload(payload)

    match_json_path(project_id, artifact_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _safe_rebuild_artifact_views(db, project_id=project_id, artifact_id=artifact_id)
    try:
        reaudit_voices_and_reindex(db, project_id=project_id, artifact_id=artifact_id)
    except Exception:
        logger.exception("voice reaudit after merge failed for %s", artifact_id)
    return {
        "source": source_diar_label,
        "target": target_diar_label,
        "profile_id": profile.id,
        "display_name": profile.display_name,
    }


def cleanup_project_speaker_profiles(db: Session, project_id: int) -> dict[str, int]:
    """Удалить профили проекта, не привязанные ни к одному artifact_speaker_link."""
    linked_ids: set[int] = set()
    for row in db.query(ArtifactSpeakerLink.profile_id).filter(
        ArtifactSpeakerLink.project_id == project_id,
        ArtifactSpeakerLink.profile_id.isnot(None),
    ):
        if row[0]:
            linked_ids.add(row[0])

    if not linked_ids:
        return {"removed": 0, "kept": 0}
    q = db.query(SpeakerProfile).filter(
        SpeakerProfile.scope == "project",
        SpeakerProfile.project_id == project_id,
        ~SpeakerProfile.id.in_(linked_ids),
    )
    removed = q.count()
    q.delete(synchronize_session=False)
    db.commit()
    return {"removed": removed, "kept": len(linked_ids)}


def set_confirmed_links(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    mapping: dict[str, int],
) -> None:
    """Жёстко выставить confirmed-привязки diar_label → profile_id."""
    for diar_label, profile_id in mapping.items():
        profile = db.get(SpeakerProfile, profile_id)
        if profile is None:
            raise ValueError(f"profile {profile_id} not found")
        link = (
            db.query(ArtifactSpeakerLink)
            .filter(
                ArtifactSpeakerLink.artifact_id == artifact_id,
                ArtifactSpeakerLink.diar_label == diar_label,
            )
            .first()
        )
        if link is None:
            link = ArtifactSpeakerLink(
                artifact_id=artifact_id,
                project_id=project_id,
                diar_label=diar_label,
            )
            db.add(link)
        link.profile_id = profile_id
        link.suggested_profile_id = profile_id
        link.match_score = 1.0
        link.status = "confirmed"
    db.commit()


def sync_match_json_from_links(db: Session, *, project_id: int, artifact_id: str) -> None:
    """Обновить matches/display_names в JSON из confirmed links."""
    if not match_json_path(project_id, artifact_id).is_file():
        return
    payload = load_match_payload(project_id, artifact_id)
    links = (
        db.query(ArtifactSpeakerLink)
        .filter(
            ArtifactSpeakerLink.artifact_id == artifact_id,
            ArtifactSpeakerLink.status == "confirmed",
            ArtifactSpeakerLink.profile_id.isnot(None),
        )
        .all()
    )
    profiles = {
        p.id: p
        for p in db.query(SpeakerProfile).filter(
            SpeakerProfile.id.in_([link.profile_id for link in links if link.profile_id])
        )
    }
    matches = payload.setdefault("matches", {})
    display = payload.setdefault("display_names", {})
    merge_map = payload.get("merge_map") or {}
    for link in links:
        prof = profiles.get(link.profile_id)
        if not prof:
            continue
        canon = merge_map.get(link.diar_label, link.diar_label)
        row = matches.setdefault(canon, {})
        row.update(
            {
                "diar_label": canon,
                "profile_id": prof.id,
                "profile_name": prof.display_name,
                "score": 1.0,
                "status": "confirmed",
            }
        )
        display[canon] = prof.display_name
        if link.diar_label != canon and link.diar_label in matches:
            matches[link.diar_label].update(
                {
                    "profile_id": prof.id,
                    "profile_name": prof.display_name,
                    "score": 1.0,
                    "status": "confirmed",
                }
            )
    match_json_path(project_id, artifact_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def rebuild_transcript_report_html(db: Session, *, project_id: int, artifact_id: str) -> None:
    """Пересобрать reports/transcript.html после enroll/merge/recalculate."""
    from app.app.config import get_settings
    from app.app.services.artifact_audio import _rebuild_transcript_report

    settings = get_settings()
    artifact = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == artifact_id, Artifact.project_id == project_id)
        .first()
    )
    if artifact is None:
        return
    _rebuild_transcript_report(settings, artifact)


def _safe_rebuild_artifact_views(db: Session, *, project_id: int, artifact_id: str) -> None:
    """HTML после enroll/merge — best effort, не валим API при сбое subprocess."""
    try:
        rebuild_match_report_html(db, project_id=project_id, artifact_id=artifact_id)
    except Exception:
        logger.exception("speaker_match HTML rebuild failed for %s", artifact_id)
    try:
        rebuild_transcript_report_html(db, project_id=project_id, artifact_id=artifact_id)
    except Exception:
        logger.exception("transcript HTML rebuild failed for %s", artifact_id)


def reaudit_voices_and_reindex(db: Session, *, project_id: int, artifact_id: str) -> dict:
    """Повторный voice audit после смены имён; при правках — обновить текст и индекс."""
    import sys

    from app.app.config import get_settings
    from app.app.services.indexing import index_artifact
    from app.app.services.storage import ensure_artifact_workspace

    settings = get_settings()
    artifact = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == artifact_id, Artifact.project_id == project_id)
        .first()
    )
    if artifact is None:
        return {"ok": False, "reason": "no_artifact"}

    ws = ensure_artifact_workspace(settings, project_id, artifact_id)
    speakers = ws / "asr" / "docker_gigaam_speakers.json"
    audio = ws / "audio" / "input_16k.wav"
    if not speakers.is_file() or not audio.is_file():
        return {"ok": False, "reason": "no_speakers_or_audio", "auto_n": 0}

    asr = str(ROOT / "asr")
    if asr not in sys.path:
        sys.path.insert(0, asr)
    from voice_turn_audit import run as voice_audit_run  # noqa: PLC0415

    report = voice_audit_run(speakers_json=speakers, audio=audio, out_dir=ws / "asr", apply=True)
    auto_n = int(report.get("auto_n") or 0)
    if not auto_n:
        return {"ok": True, "auto_n": 0, "reindexed": False}

    # синхронизировать финальный текст для чанков
    from app.app.services.artifact_audio import finalize_audio_outputs

    try:
        final_path, transcript_url = finalize_audio_outputs(settings, artifact)
        text = final_path.read_text(encoding="utf-8")
        artifact.transcription_file_path = str(final_path)
        artifact.transcription_text = text
        artifact.transcript_html_url = transcript_url
        db.commit()
    except FileNotFoundError:
        text = artifact.transcription_text or ""

    reindexed = False
    if artifact.status in {"indexed", "graph_seeded", "done"} and text:
        index_artifact(db, settings, artifact, text)
        db.commit()
        reindexed = True
    return {"ok": True, "auto_n": auto_n, "reindexed": reindexed, "audit": report}


def rebuild_artifact_call_summary(db: Session, *, project_id: int, artifact_id: str) -> dict:
    """Пересобрать call_summary.md и обновить HTML (LLM, фаза knowledge)."""
    from app.app.config import get_settings
    from app.app.llm_labels import llm_unavailable_hint
    from app.app.models.artifact import Artifact
    from app.app.services.pipeline_audio import run_call_summary

    settings = get_settings()
    artifact = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == artifact_id, Artifact.project_id == project_id)
        .first()
    )
    if artifact is None:
        raise FileNotFoundError("Artifact not found")
    if artifact.file_type != "audio":
        raise ValueError("Call summary rebuild is for audio artifacts only")
    if not run_call_summary(settings, artifact):
        raise RuntimeError(f"Не удалось собрать выжимку ({llm_unavailable_hint(settings.llm_provider)})")
    return {"ok": True, "artifact_id": artifact_id}


def refresh_artifact_project_index(db: Session, *, project_id: int, artifact_id: str) -> dict:
    """Обновить финальный текст из asr и переиндексировать чанки в sqlite-vec."""
    from app.app.config import get_settings
    from app.app.models.artifact import Artifact
    from app.app.services.artifact_audio import finalize_audio_outputs
    from app.app.services.indexing import index_artifact

    settings = get_settings()
    artifact = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == artifact_id, Artifact.project_id == project_id)
        .first()
    )
    if artifact is None:
        raise FileNotFoundError("Artifact not found")
    if artifact.file_type != "audio":
        raise ValueError("Reindex is for audio artifacts only")

    rebuild_transcript_report_html(db, project_id=project_id, artifact_id=artifact_id)
    final_path, transcript_url = finalize_audio_outputs(settings, artifact)
    text = final_path.read_text(encoding="utf-8")
    artifact.transcription_file_path = str(final_path)
    artifact.transcription_text = text
    artifact.transcript_html_url = transcript_url

    chunks = 0
    if not settings.dry_run:
        chunks = index_artifact(db, settings, artifact, text)
    db.commit()
    return {"ok": True, "artifact_id": artifact_id, "chunks": chunks, "chars": len(text)}


def rebuild_match_report_html(db: Session, *, project_id: int, artifact_id: str) -> None:
    """Пересобрать speaker_match_test.html после enroll."""
    import sys

    from app.app.config import get_settings
    from app.app.services.storage import artifact_speaker_match_path

    settings = get_settings()
    match_json = match_json_path(project_id, artifact_id)
    if not match_json.is_file():
        return
    report = artifact_speaker_match_path(settings, artifact_id, project_id)
    asr_dir = str(ROOT / "asr")
    if asr_dir not in sys.path:
        sys.path.insert(0, asr_dir)
    import build_speaker_match_view  # noqa: PLC0415
    import importlib

    importlib.reload(build_speaker_match_view)

    report.parent.mkdir(parents=True, exist_ok=True)
    build_speaker_match_view.build(
        match_json=match_json,
        out_html=report,
        audio_url=f"/artifacts/{artifact_id}/audio",
        profiles=list_profiles(db, project_id),
        title="Sortformer + запоминание голосов",
    )


def recalculate_speaker_match(
    db: Session,
    *,
    artifact_id: str,
    project_id: int,
    sortformer_json: Path,
    audio: Path,
) -> dict:
    """Пересчитать match Sortformer → speaker_profiles и обновить HTML."""
    import sys

    asr_dir = str(ROOT / "asr")
    if asr_dir not in sys.path:
        sys.path.insert(0, asr_dir)
    from match_sortformer_speakers import run  # noqa: PLC0415

    out_json = match_json_path(project_id, artifact_id)
    payload = run(
        sortformer_json=sortformer_json,
        audio=audio,
        project_id=project_id,
        artifact_id=artifact_id,
        out_json=out_json,
    )
    rebuild_match_report_html(db, project_id=project_id, artifact_id=artifact_id)
    rebuild_transcript_report_html(db, project_id=project_id, artifact_id=artifact_id)
    canonical = sorted({payload["merge_map"][k] for k in payload["merge_map"]})
    return {
        "profiles_in_db": payload.get("profiles_in_db", 0),
        "matches": {
            label: payload["matches"].get(label, {})
            for label in canonical
        },
    }
