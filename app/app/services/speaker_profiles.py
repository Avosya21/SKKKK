"""Голоса и спикеры: профили и эмбеддинги голосов, сопоставление имён, подтверждение спикеров, слияние профилей и синхронизация подтверждённых в глобальные."""

from __future__ import annotations

import json
import logging
import re
import struct
from pathlib import Path

from sqlalchemy import func
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
from speaker_registry import (  # noqa: E402
    GLOBAL_VOICE_MERGE_THRESHOLD,
    average_embeddings,
    cosine,
    unpack_embedding,
)


_PLACEHOLDER_NAME = re.compile(
    r"^(мужчина|девушка|заказчик|спикер|speaker)\s*\d+$",
    re.IGNORECASE,
)


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def fold_speaker_display_name(name: str) -> str:
    """Игорь (Пилот) и Игорь Пилот — один ключ."""
    folded = (name or "").strip().lower()
    folded = re.sub(r"[()\[\]«»\"'`]", " ", folded)
    folded = re.sub(r"[-–—_/.,;:]+", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded


def speaker_names_are_aliases(left: str, right: str) -> bool:
    """Одинаковые имена и пары вроде «Директор Пилот» / «Директор Пилота»."""
    a = fold_speaker_display_name(left)
    b = fold_speaker_display_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    extra = longer[len(shorter) :]
    return longer.startswith(shorter) and extra in {"а", "я", "у", "е"}


def _is_placeholder_name(name: str) -> bool:
    return bool(_PLACEHOLDER_NAME.match((name or "").strip()))


def match_json_path(project_id: int, artifact_id: str) -> Path:
    return artifact_dir_path(project_id, artifact_id) / "reports" / "sortformer_speaker_match.json"


def artifact_dir_path(project_id: int, artifact_id: str) -> Path:
    return ROOT / "data" / "projects" / str(project_id) / "artifacts" / artifact_id


def _confirmed_profile_ids(db: Session) -> set[int]:
    return {
        int(row[0])
        for row in db.query(ArtifactSpeakerLink.profile_id)
        .filter(
            ArtifactSpeakerLink.status == "confirmed",
            ArtifactSpeakerLink.profile_id.isnot(None),
        )
        .distinct()
        if row[0] is not None
    }


def load_universal_voice_profiles(db: Session, *, auto_backfill: bool = True) -> list[dict]:
    """Подтверждённые голоса из всех проектов + global — для cosine-match."""
    if auto_backfill:
        global_n = db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").count()
        if global_n == 0 and _confirmed_profile_ids(db):
            backfill_global_speaker_profiles(db)

    global_rows = db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").all()
    confirmed_ids = _confirmed_profile_ids(db)
    confirmed_rows = (
        db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(confirmed_ids)).all()
        if confirmed_ids
        else []
    )

    global_by_name: dict[str, SpeakerProfile] = {}
    for row in global_rows:
        key = row.display_name.strip().lower()
        prev = global_by_name.get(key)
        if prev is None or row.id < prev.id:
            global_by_name[key] = row
    seen_ids = {r.id for r in global_by_name.values()}
    out: list[dict] = [
        {
            "id": r.id,
            "display_name": r.display_name,
            "embedding": r.embedding,
            "scope": r.scope,
        }
        for r in sorted(global_by_name.values(), key=lambda x: x.id)
    ]
    for row in confirmed_rows:
        if row.id in seen_ids:
            continue
        name_key = row.display_name.strip().lower()
        if name_key in global_by_name:
            continue
        seen_ids.add(row.id)
        out.append(
            {
                "id": row.id,
                "display_name": row.display_name,
                "embedding": row.embedding,
                "scope": row.scope,
            }
        )
    return out


def sync_confirmed_to_global(db: Session, profile: SpeakerProfile, *, by_voice: bool = True) -> SpeakerProfile:
    """Поднять подтверждённый голос в общую базу (scope=global).

    ``by_voice=False`` — человек явно сказал «это новый человек»: сливать только с общим голосом того же имени,
    а не с похожим по звучанию (иначе голос молча уходит под чужое имя)."""
    if profile.scope == "global":
        return profile

    vec = unpack_embedding(profile.embedding)
    name_key = profile.display_name.strip().lower()
    globals_ = db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").all()

    best: SpeakerProfile | None = None
    best_score = -1.0
    for row in globals_:
        score = cosine(vec, unpack_embedding(row.embedding)) if by_voice else -1.0
        if row.display_name.strip().lower() == name_key:
            score = max(score, GLOBAL_VOICE_MERGE_THRESHOLD)
        if score > best_score:
            best_score = score
            best = row

    if best is not None and best_score >= GLOBAL_VOICE_MERGE_THRESHOLD:
        _blend_embedding(best, vec)
        if best.user_id is None and profile.user_id is not None:
            best.user_id = profile.user_id
        db.commit()
        db.refresh(best)
        return best

    global_prof = SpeakerProfile(
        scope="global",
        project_id=None,
        user_id=profile.user_id,
        display_name=profile.display_name,
        embedding=profile.embedding,
        embedding_dim=profile.embedding_dim,
        source_artifact_id=profile.source_artifact_id,
        source_start_sec=profile.source_start_sec,
        source_end_sec=profile.source_end_sec,
    )
    db.add(global_prof)
    db.commit()
    db.refresh(global_prof)
    return global_prof


def backfill_global_speaker_profiles(db: Session) -> dict[str, int]:
    """Один раз поднять все confirmed-профили в global."""
    confirmed_ids = _confirmed_profile_ids(db)
    if not confirmed_ids:
        return {"synced": 0, "globals": 0}

    synced = 0
    for profile_id in sorted(confirmed_ids):
        profile = db.get(SpeakerProfile, profile_id)
        if profile is None or profile.scope == "global":
            continue
        sync_confirmed_to_global(db, profile)
        synced += 1

    global_n = db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").count()
    return {"synced": synced, "globals": global_n}


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
    unique: dict[str, SpeakerProfile] = {}
    for row in rows:
        key = fold_speaker_display_name(row.display_name) or f"id:{row.id}"
        prev = unique.get(key)
        if prev is None:
            unique[key] = row
            continue
        prefer = (0 if row.scope == "global" else 1, row.id)
        had = (0 if prev.scope == "global" else 1, prev.id)
        if prefer < had:
            unique[key] = row
    return [
        {"id": r.id, "display_name": r.display_name, "scope": r.scope}
        for r in sorted(unique.values(), key=lambda x: x.display_name.lower())
    ]


def resolve_match_path(
    project_id: int,
    artifact_id: str,
    *,
    match_path: Path | None = None,
) -> Path:
    return Path(match_path) if match_path is not None else match_json_path(project_id, artifact_id)


def load_match_payload(
    project_id: int,
    artifact_id: str,
    *,
    match_path: Path | None = None,
) -> dict:
    path = resolve_match_path(project_id, artifact_id, match_path=match_path)
    if not path.is_file():
        raise FileNotFoundError(f"match json missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_match_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _profile_link_counts(db: Session, profile_ids: list[int]) -> dict[int, int]:
    if not profile_ids:
        return {}
    counts: dict[int, int] = {}
    for pid, n in (
        db.query(ArtifactSpeakerLink.profile_id, func.count())
        .filter(ArtifactSpeakerLink.profile_id.in_(profile_ids))
        .group_by(ArtifactSpeakerLink.profile_id)
        .all()
    ):
        if pid:
            counts[int(pid)] = int(n)
    return counts


def _keep_rank(profile: SpeakerProfile, link_counts: dict[int, int]) -> tuple:
    return (
        0 if profile.scope == "global" else 1,
        1 if _is_placeholder_name(profile.display_name) else 0,
        -link_counts.get(profile.id, 0),
        -len((profile.display_name or "").strip()),
        profile.id,
    )


def _absorb_profile(db: Session, keep: SpeakerProfile, dup: SpeakerProfile) -> int:
    """Слить dup в keep: embedding, links, удалить dup. Возвращает число перепривязанных links."""
    if keep.id == dup.id:
        return 0
    _blend_embedding(keep, unpack_embedding(dup.embedding))
    if _is_placeholder_name(keep.display_name) and not _is_placeholder_name(dup.display_name):
        keep.display_name = dup.display_name
    if keep.scope != "global":
        keep.scope = "global"
        keep.project_id = None
    relinked = int(
        db.query(ArtifactSpeakerLink)
        .filter(ArtifactSpeakerLink.profile_id == dup.id)
        .update(
            {
                ArtifactSpeakerLink.profile_id: keep.id,
                ArtifactSpeakerLink.suggested_profile_id: keep.id,
            },
            synchronize_session=False,
        )
        or 0
    )
    db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.suggested_profile_id == dup.id).update(
        {ArtifactSpeakerLink.suggested_profile_id: keep.id},
        synchronize_session=False,
    )
    db.delete(dup)
    return relinked


def _merge_profile_groups(db: Session, groups: list[list[SpeakerProfile]]) -> dict[str, int]:
    rows = [p for g in groups for p in g]
    link_counts = _profile_link_counts(db, [r.id for r in rows])
    removed = 0
    merged_groups = 0
    relinked = 0
    for group in groups:
        if len(group) < 2:
            continue
        merged_groups += 1
        group.sort(key=lambda p: _keep_rank(p, link_counts))
        keep = group[0]
        if keep.scope != "global":
            keep.scope = "global"
            keep.project_id = None
        for dup in group[1:]:
            relinked += _absorb_profile(db, keep, dup)
            removed += 1
    if removed:
        db.commit()
    return {"removed": removed, "merged_groups": merged_groups, "relinked": relinked}


def dedupe_all_speaker_profiles(db: Session) -> dict[str, int]:
    """Слить все speaker_profiles с одинаковым display_name в один (prefer global, затем links)."""
    rows = db.query(SpeakerProfile).order_by(SpeakerProfile.id).all()
    groups: dict[str, list[SpeakerProfile]] = {}
    for row in rows:
        groups.setdefault(row.display_name.strip().lower(), []).append(row)
    return _merge_profile_groups(db, list(groups.values()))


def _dedupe_alias_name_profiles(db: Session) -> dict[str, int]:
    """Слить «Игорь (Пилот)» и «Игорь Пилот», «Директор Пилот» / «Директор Пилота»."""
    rows = db.query(SpeakerProfile).order_by(SpeakerProfile.id).all()
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, left in enumerate(rows):
        for j in range(i + 1, len(rows)):
            if speaker_names_are_aliases(left.display_name, rows[j].display_name):
                a, b = find(i), find(j)
                if a != b:
                    parent[b] = a
    buckets: dict[int, list[SpeakerProfile]] = {}
    for i, row in enumerate(rows):
        buckets.setdefault(find(i), []).append(row)
    return _merge_profile_groups(db, list(buckets.values()))


def _dedupe_similar_voice_profiles(db: Session, *, threshold: float) -> dict[str, int]:
    """Один WeSpeaker-отпечаток под разными именами → один профиль."""
    rows = db.query(SpeakerProfile).order_by(SpeakerProfile.id).all()
    vecs = [unpack_embedding(r.embedding) for r in rows]
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, left in enumerate(rows):
        for j in range(i + 1, len(rows)):
            if len(vecs[i]) != len(vecs[j]) or not vecs[i]:
                continue
            if cosine(vecs[i], vecs[j]) >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[b] = a
    buckets: dict[int, list[SpeakerProfile]] = {}
    for i, row in enumerate(rows):
        buckets.setdefault(find(i), []).append(row)
    return _merge_profile_groups(db, list(buckets.values()))


def purge_orphan_project_profiles(db: Session) -> dict[str, int]:
    """Удалить project-копии, на которые уже никто не ссылается (после подъёма в global)."""
    linked_ids = {
        int(row[0])
        for row in db.query(ArtifactSpeakerLink.profile_id)
        .filter(ArtifactSpeakerLink.profile_id.isnot(None))
        .distinct()
        if row[0] is not None
    }
    q = db.query(SpeakerProfile).filter(SpeakerProfile.scope == "project")
    if linked_ids:
        q = q.filter(~SpeakerProfile.id.in_(linked_ids))
    removed = q.count()
    if removed:
        q.delete(synchronize_session=False)
        db.commit()
    return {"removed": removed}


def _refresh_match_views_after_cleanup(db: Session) -> int:
    pairs = {
        (row.project_id, row.artifact_id)
        for row in db.query(ArtifactSpeakerLink.project_id, ArtifactSpeakerLink.artifact_id).distinct()
    }
    refreshed = 0
    for project_id, artifact_id in sorted(pairs):
        try:
            sync_match_json_from_links(db, project_id=project_id, artifact_id=artifact_id)
            rebuild_match_report_html(db, project_id=project_id, artifact_id=artifact_id)
            try:
                rebuild_transcript_report_html(db, project_id=project_id, artifact_id=artifact_id)
            except Exception:
                logger.exception("voice cleanup: transcript refresh skipped for %s", artifact_id)
            refreshed += 1
        except Exception:
            logger.exception("voice cleanup: skip views for %s", artifact_id)
    return refreshed


def cleanup_voice_profile_pool(
    db: Session,
    *,
    cosine_threshold: float | None = None,
    refresh_views: bool = True,
) -> dict[str, int]:
    """Полная уборка пула: точные имена, варианты написания, похожие голоса, сироты."""
    threshold = GLOBAL_VOICE_MERGE_THRESHOLD if cosine_threshold is None else cosine_threshold
    exact = dedupe_all_speaker_profiles(db)
    aliases = _dedupe_alias_name_profiles(db)
    voices = _dedupe_similar_voice_profiles(db, threshold=threshold)
    orphans = purge_orphan_project_profiles(db)
    views = _refresh_match_views_after_cleanup(db) if refresh_views else 0
    return {
        "exact_removed": exact.get("removed", 0),
        "alias_removed": aliases.get("removed", 0),
        "voice_removed": voices.get("removed", 0),
        "orphans_removed": orphans.get("removed", 0),
        "removed": (
            exact.get("removed", 0)
            + aliases.get("removed", 0)
            + voices.get("removed", 0)
            + orphans.get("removed", 0)
        ),
        "views_refreshed": views,
    }


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
    del project_id  # универсальный пул — все подтверждённые голоса
    return load_universal_voice_profiles(db)


def _touch_voice_aliases(payload: dict, *, old_names: list[str | None], new_name: str) -> None:
    aliases = payload.setdefault("voice_aliases", {})
    canon = new_name.strip()
    if canon:
        aliases[canon] = canon
    for raw in old_names:
        old = (raw or "").strip()
        if old and old != canon:
            aliases[old] = canon


def _platform_on(db: Session, project_id: int) -> bool:
    """Платформа памяти включена для проекта (PLATFORM-FOUNDATION-PLAN): голос обязан принадлежать человеку из реестра."""
    from app.app.config import get_settings
    from app.app.models.project import Project
    from app.app.services import memory_platform

    return memory_platform.enabled(get_settings(), db.get(Project, project_id))


def _registry_person_for_voice(db: Session, project_id: int, artifact_id: str, name: str):
    """Человек проекта по введённому имени голоса. Нет в реестре — вопрос «Кто это?» и понятный отказ;
    внешний человек (без учётки) — только с отметкой менеджера «согласие на голос получено» (Д5)."""
    from app.app.services import people

    match = people.resolve(db, project_id, name)
    if match is None:
        art = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
        people.note_unknown(db, project_id, "speaker_label", name, artifact_id=art.id if art else None)
        db.commit()
        raise ValueError(
            f"«{name}» нет в списке «Люди и стороны». Добавьте человека там (или выберите из подсказки) и подтвердите голос снова."
        )
    person = match.person
    if person.user_id is None and person.voice_consent_at is None:
        raise ValueError(
            f"Для «{person.display_name}» не отмечено согласие на запись голоса. "
            "Отметьте его в карточке «Люди и стороны» и подтвердите голос снова."
        )
    return person


def enroll_speaker(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    diar_label: str,
    display_name: str,
    match_path: Path | None = None,
    rebuild_views: bool = True,
    profile_id: int | None = None,
    new_voice: bool = False,
    rename: bool = False,
) -> SpeakerProfile:
    """Запомнить голос метки ``diar_label`` под именем.

    ``profile_id`` — человек выбрал голос из списка: привязать к нему (имя — его). ``new_voice`` — «новый человек»:
    не подставлять похожий голос из базы. Другое имя на уже подтверждённой карточке — перепривязка к голосу с этим именем
    (или новому), прежний голос не переименовывается; переименовать — только ``rename`` (то же ``profile_id``)."""
    import sys

    asr = str(ROOT / "asr")
    if asr not in sys.path:
        sys.path.insert(0, asr)
    from speaker_registry import VOICE_AUTO_LINK_THRESHOLD, best_profile_match  # noqa: PLC0415

    out_path = resolve_match_path(project_id, artifact_id, match_path=match_path)
    payload = load_match_payload(project_id, artifact_id, match_path=out_path)
    canonical, emb = _resolve_embedding(payload, diar_label)
    match = (payload.get("matches") or {}).get(canonical) or (payload.get("matches") or {}).get(diar_label) or {}
    name = display_name.strip()
    chosen: SpeakerProfile | None = None
    if profile_id is not None:
        chosen = db.get(SpeakerProfile, profile_id)
        if chosen is None or not (chosen.scope == "global" or chosen.project_id == project_id):
            raise ValueError("выбранный голос не найден")
        if not rename:
            name = chosen.display_name
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

    person = None
    external_voice = False
    if _platform_on(db, project_id):
        person = _registry_person_for_voice(db, project_id, artifact_id, name)
        name = person.display_name  # «Анна» и «Анна Пример» — один человек, в голосах одно имя
        external_voice = person.user_id is None

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
    current = db.get(SpeakerProfile, link.profile_id) if link and link.profile_id else None
    same = fold_speaker_display_name
    profile: SpeakerProfile | None = None
    if chosen is not None:
        profile = chosen
        if rename:
            if current is None or current.id != chosen.id:
                raise ValueError("переименовать можно только голос этой карточки")
            profile.display_name = name
    elif current is not None and same(current.display_name) == same(name):
        profile = current
    else:
        # другое имя — к голосу с этим именем, иначе новый; прежний голос карточки не переименовываем
        profile = None if new_voice else _visible_profile_by_name(db, project_id, name)
        if profile is None and not new_voice and best_id and best_score is not None and best_score >= VOICE_AUTO_LINK_THRESHOLD:
            best = db.get(SpeakerProfile, best_id)
            if best is not None and same(best.display_name) == same(name):
                profile = best

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
    if person is not None and profile.scope == "project" and profile.project_id == project_id:
        profile.person_id = person.id  # локальный слепок принадлежит человеку проекта (глобальный связывается через учётку)
    db.commit()
    db.refresh(profile)
    consolidate_duplicate_name_profiles(db, project_id)

    # Голос внешнего человека (заказчик без учётки) остаётся только в проекте (Д5): в общие голоса он не переносится
    global_prof = profile if external_voice else sync_confirmed_to_global(db, profile, by_voice=not new_voice)
    if global_prof.id != profile.id:
        leftover_id = profile.id
        link.profile_id = global_prof.id
        link.suggested_profile_id = global_prof.id
        profile = global_prof
        db.flush()
        leftover = db.get(SpeakerProfile, leftover_id)
        still_linked = (
            db.query(ArtifactSpeakerLink)
            .filter(ArtifactSpeakerLink.profile_id == leftover_id)
            .count()
        )
        if leftover is not None and leftover.scope == "project" and still_linked == 0:
            db.delete(leftover)
        db.commit()
        db.refresh(profile)
    cleanup_project_speaker_profiles(db, project_id)

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
        row.pop("needs_human", None)  # голос проверил человек
        display[lbl] = profile.display_name
    _touch_voice_aliases(
        payload,
        old_names=[
            match.get("profile_name"),
            match.get("suggested_profile_name"),
            display.get(canonical),
            display.get(diar_label),
        ],
        new_name=profile.display_name,
    )
    if payload.get("source") == "lab":
        payload["written_to_db"] = True
    _write_match_payload(out_path, payload)
    if rebuild_views:
        _safe_rebuild_artifact_views(db, project_id=project_id, artifact_id=artifact_id)
        if not was_already_confirmed:
            _maybe_reaudit_voices_after_names(
                db, project_id=project_id, artifact_id=artifact_id, payload=payload
            )
    return profile


def _visible_profile_by_name(db: Session, project_id: int, name: str) -> SpeakerProfile | None:
    """Голос с тем же именем (``fold_speaker_display_name``), видимый проекту: сначала общий, затем проекта."""
    key = fold_speaker_display_name(name)
    if not key:
        return None
    rows = db.query(SpeakerProfile).filter(
        (SpeakerProfile.scope == "global") | ((SpeakerProfile.scope == "project") & (SpeakerProfile.project_id == project_id))
    ).all()
    hits = sorted((r for r in rows if fold_speaker_display_name(r.display_name) == key), key=lambda r: (r.scope != "global", r.id))
    return hits[0] if hits else None


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
    match_path: Path | None = None,
    rebuild_views: bool = True,
) -> dict:
    """Ручное объединение двух Sortformer ID в один профиль."""
    if source_diar_label == target_diar_label:
        raise ValueError("нельзя объединить спикера с самим собой")

    out_path = resolve_match_path(project_id, artifact_id, match_path=match_path)
    payload = load_match_payload(project_id, artifact_id, match_path=out_path)
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
    source_link = (
        db.query(ArtifactSpeakerLink)
        .filter(ArtifactSpeakerLink.artifact_id == artifact_id,
                ArtifactSpeakerLink.diar_label.in_([source_diar_label, source_canon]),
                ArtifactSpeakerLink.status == "confirmed", ArtifactSpeakerLink.profile_id.isnot(None))
        .first()
    )
    source_profile = db.get(SpeakerProfile, source_link.profile_id) if source_link is not None else None

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
    people_merged = _merge_label_people(db, project_id, keep_profile=profile, drop_profile=source_profile)

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
    for lbl in {source_diar_label, source_canon}:
        display[lbl] = profile.display_name
    _apply_profile_merge_to_payload(payload)

    if payload.get("source") == "lab":
        payload["written_to_db"] = True
    _write_match_payload(out_path, payload)
    if rebuild_views:
        _safe_rebuild_artifact_views(db, project_id=project_id, artifact_id=artifact_id)
        _maybe_reaudit_voices_after_names(
            db, project_id=project_id, artifact_id=artifact_id, payload=payload
        )
    return {
        "source": source_diar_label,
        "target": target_diar_label,
        "profile_id": profile.id,
        "display_name": profile.display_name,
        "people_merged": people_merged,
    }


def split_speaker(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    diar_label: str,
    match_path: Path | None = None,
    rebuild_views: bool = True,
) -> dict:
    """«Отделить» во вкладке «Голоса»: метка, присоединённая к другой (вручную или по голосу), снова отдельный спикер
    без имени — её подписывают заново. Голос основной карточки и её человек не меняются."""
    out_path = resolve_match_path(project_id, artifact_id, match_path=match_path)
    payload = load_match_payload(project_id, artifact_id, match_path=out_path)
    merge_map = payload.setdefault("merge_map", {k: k for k in (payload.get("embeddings") or {})})
    canon = merge_map.get(diar_label, diar_label)
    if canon == diar_label:
        raise ValueError("метка не объединена с другой — отделять нечего")
    merge_map[diar_label] = diar_label
    row = payload.setdefault("matches", {}).setdefault(diar_label, {"diar_label": diar_label})
    for key in ("profile_id", "profile_name", "score", "needs_human", "auto"):
        row.pop(key, None)
    row["status"] = "new"
    payload.setdefault("display_names", {}).pop(diar_label, None)
    db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.artifact_id == artifact_id,
                                         ArtifactSpeakerLink.diar_label == diar_label).delete(synchronize_session=False)
    db.commit()
    _write_match_payload(out_path, payload)
    if rebuild_views:
        _safe_rebuild_artifact_views(db, project_id=project_id, artifact_id=artifact_id)
    return {"label": diar_label, "from": canon}


def _label_person(db: Session, project_id: int, profile: SpeakerProfile | None):
    """Человек проекта за голосом: свой слепок проекта, человек с учёткой голоса, иначе по написанию имени голоса."""
    from app.app.models.person import Person
    from app.app.services import people

    if profile is None:
        return None
    if profile.person_id:
        person = db.get(Person, profile.person_id)
        if person is not None and person.project_id == project_id:
            return people.canonical(db, person)
    if profile.user_id:
        own = db.query(Person).filter(Person.project_id == project_id, Person.user_id == profile.user_id,
                                      Person.merged_into.is_(None)).first()
        if own is not None:
            return own
    hit = people.resolve(db, project_id, profile.display_name, allow_first_name=False)
    return hit.person if hit is not None else None


def _merge_label_people(db: Session, project_id: int, *, keep_profile: SpeakerProfile,
                        drop_profile: SpeakerProfile | None) -> bool:
    """Две метки одного созвона — один человек (кнопка объединения во вкладке «Голоса»). Если обе были подписаны разными
    людьми проекта, они объединяются в «Люди и стороны» (``people.merge``: написания, реплики, голоса, события — к
    оставшемуся, PEOPLE-FLOW-PLAN). Разные учётки не объединяются — тогда только метки, как раньше."""
    from app.app.services import people

    if drop_profile is None or drop_profile.id == keep_profile.id:
        return False
    keep, drop = _label_person(db, project_id, keep_profile), _label_person(db, project_id, drop_profile)
    if keep is None or drop is None or keep.id == drop.id:
        return False
    try:
        people.merge(db, project_id, keep.id, drop.id)
        db.commit()
    except people.PeopleError:
        db.rollback()
        return False
    return True


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
        if row.get("auto") and row.get("profile_id") not in (None, prof.id):
            row.pop("auto", None)  # человек переназвал автопринятый голос — это уже его решение
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
    from app.app.services.artifact_lab import latest_run_id

    settings = get_settings()
    artifact = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == artifact_id, Artifact.project_id == project_id)
        .first()
    )
    if artifact is None:
        return
    if latest_run_id(settings, artifact):
        from app.app.services.pipeline_lab import apply_confirmed_speaker_names  # noqa: PLC0415

        try:
            apply_confirmed_speaker_names(db, settings, artifact)
        except FileNotFoundError:
            logger.warning("lab transcript republish skipped for %s: no proof", artifact_id)
            return
        if artifact.status == "done":
            # Знания уже построены: имена в сущностях обновил rebind, а индекс и выжимку пересоберёт фоновая задача.
            from app.app.services.queue import KIND_REFRESH, enqueue_knowledge_refresh, running_worker_kinds  # noqa: PLC0415

            if KIND_REFRESH in running_worker_kinds(settings.data_dir):
                enqueue_knowledge_refresh(db, artifact.id)
            else:
                logger.warning("refresh_knowledge не поставлен для %s: запущенный воркер старой версии", artifact_id)
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


def _is_lab_match(payload: dict | None) -> bool:
    return str((payload or {}).get("source") or "").strip().lower() == "lab"


def _maybe_reaudit_voices_after_names(
    db: Session, *, project_id: int, artifact_id: str, payload: dict | None
) -> None:
    """WeSpeaker-audit всех реплик — только legacy. Lab уже размечен Sortformer."""
    if _is_lab_match(payload):
        logger.info("skip voice reaudit after name save for lab %s", artifact_id)
        return
    try:
        reaudit_voices_and_reindex(db, project_id=project_id, artifact_id=artifact_id)
    except Exception:
        logger.exception("voice reaudit after name save failed for %s", artifact_id)


def reaudit_voices_and_reindex(db: Session, *, project_id: int, artifact_id: str) -> dict:
    """Повторный voice audit после смены имён; при правках — обновить текст и индекс."""
    import sys

    from app.app.config import get_settings
    from app.app.services.artifact_lab import latest_run_id
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
    if latest_run_id(settings, artifact):
        logger.info("skip voice reaudit for lab artifact %s", artifact_id)
        return {"ok": True, "auto_n": 0, "reindexed": False, "skipped": "lab"}

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
    """Пересобрать выжимку созвона или документа и обновить HTML (LLM)."""
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
        from app.app.services.artifact_reports import load_document_payload, write_document_report
        from app.app.services.summarize_document import load_document_source_text, run_document_summary

        if not run_document_summary(settings, artifact):
            raise RuntimeError(f"Не удалось собрать выжимку ({llm_unavailable_hint(settings.llm_provider)})")
        payload = load_document_payload(settings, artifact)
        text = load_document_source_text(settings, artifact) or (artifact.transcription_text or "")
        write_document_report(settings, artifact, payload, text)
        return {"ok": True, "artifact_id": artifact_id, "kind": "document"}
    from app.app.services.artifact_lab import latest_run_id  # noqa: PLC0415

    if latest_run_id(settings, artifact):
        from app.app.services.pipeline_lab import apply_confirmed_speaker_names  # noqa: PLC0415

        apply_confirmed_speaker_names(db, settings, artifact)
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
    )


def _seed_voice_aliases_from_experiments(ws: Path, payload: dict) -> None:
    """Дополнить voice_aliases из match rows (suggested → confirmed)."""
    del ws
    aliases = payload.setdefault("voice_aliases", {})
    for row in (payload.get("matches") or {}).values():
        if row.get("status") not in ("confirmed", "merged_duplicate"):
            continue
        current = str(row.get("profile_name") or "").strip()
        if not current:
            continue
        aliases[current] = current
        for key in ("profile_name", "suggested_profile_name"):
            val = row.get(key)
            if val:
                old = str(val).strip()
                if old and old != current:
                    aliases[old] = current


def _is_human_confirmed_match(row: dict | None) -> bool:
    """Подтвердил человек (кнопка на карточке), а не только автопо высокому cosine (``needs_human``)."""
    if not isinstance(row, dict):
        return False
    if row.get("status") not in ("confirmed", "merged_duplicate"):
        return False
    if row.get("needs_human"):
        return False
    return bool(row.get("profile_id"))


def _restore_confirmed_matches(payload: dict, prev_payload: dict | None) -> None:
    """После auto-match: вернуть только подтверждённые человеком имена (пересчитать не меняет их).

    Автопринятые с ``needs_human`` не восстанавливаем — после «Пересчитать подсказки» они снова
    обычные подсказки по сходству, пока человек не нажмёт «Подтвердить».
    """
    if not prev_payload:
        return
    prev_matches = prev_payload.get("matches") or {}
    matches = payload.setdefault("matches", {})
    merge_map = payload.get("merge_map") or {}
    display = payload.setdefault("display_names", {})

    def _labels_for(canon: str) -> set[str]:
        return {canon} | {lbl for lbl, tgt in merge_map.items() if tgt == canon}

    restored_canons: set[str] = set()
    for label, prev_row in prev_matches.items():
        if not _is_human_confirmed_match(prev_row):
            continue
        pid = prev_row.get("profile_id")
        pname = (prev_row.get("profile_name") or "").strip()
        canon = merge_map.get(label, label)
        if canon in restored_canons:
            continue
        restored_canons.add(canon)
        for lbl in _labels_for(canon):
            row = dict(matches.get(lbl) or {"diar_label": lbl})
            row.update(
                {
                    "profile_id": pid,
                    "profile_name": pname or row.get("profile_name"),
                    "score": 1.0,
                    "status": "merged_duplicate" if lbl != canon else "confirmed",
                }
            )
            row.pop("needs_human", None)
            row.pop("auto", None)
            matches[lbl] = row
            if pname:
                display[lbl] = pname

    prev_aliases = prev_payload.get("voice_aliases") or {}
    if prev_aliases:
        aliases = payload.setdefault("voice_aliases", {})
        for old, new in prev_aliases.items():
            aliases.setdefault(old, new)


def _restore_merge_map(payload: dict, prev_payload: dict | None) -> None:
    """Вернуть ручные слияния Sortformer ID после автоматического пересчёта."""
    if not prev_payload:
        return
    merge_map = payload.setdefault("merge_map", {k: k for k in (payload.get("matches") or {})})
    prev = prev_payload.get("merge_map") or {}
    labels = set(merge_map) | set(payload.get("matches") or {})
    for src, tgt in prev.items():
        if src == tgt or src not in labels:
            continue
        merge_map[src] = tgt
    for src in list(merge_map):
        canon = merge_map[src]
        seen: set[str] = set()
        while canon in merge_map and merge_map[canon] != canon and canon not in seen:
            seen.add(canon)
            canon = merge_map[canon]
        merge_map[src] = canon


def _propagate_merge_to_matches(payload: dict) -> None:
    merge_map = payload.get("merge_map") or {}
    matches = payload.setdefault("matches", {})
    for src, tgt in merge_map.items():
        if src == tgt:
            continue
        primary = matches.get(tgt) or matches.get(merge_map.get(tgt, tgt)) or {}
        if not primary.get("profile_id"):
            continue
        row = dict(matches.get(src) or {"diar_label": src})
        row.update(
            {
                "profile_id": primary.get("profile_id"),
                "profile_name": primary.get("profile_name"),
                "score": primary.get("score"),
                "status": "merged_duplicate",
            }
        )
        matches[src] = row


def _save_links_from_payload(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    payload: dict,
) -> None:
    from app.app.models.speaker_profile import ArtifactSpeakerLink

    matches = payload.get("matches") or {}
    db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.artifact_id == artifact_id).delete()
    for label, row in matches.items():
        if not isinstance(row, dict):
            continue
        status = row.get("status") or "new"
        pid = row.get("profile_id")
        # автопринятие с needs_human — не confirmed-связь в БД (иначе «Пересчитать» снова закрепит)
        linked = bool(pid) and _is_human_confirmed_match(row)
        link_status = "confirmed" if linked else ("suggested" if row.get("needs_human") else status)
        db.add(
            ArtifactSpeakerLink(
                artifact_id=artifact_id,
                project_id=project_id,
                diar_label=label,
                suggested_profile_id=row.get("suggested_profile_id") or pid,
                profile_id=pid if linked else None,
                match_score=row.get("score") if row.get("score") is not None else row.get("suggested_score"),
                status=link_status,
                clip_start_sec=row.get("clip_start"),
                clip_end_sec=row.get("clip_end"),
            )
        )
    db.commit()


def _prune_non_human_speaker_links(
    db: Session, *, artifact_id: str, payload: dict
) -> None:
    """Убрать из БД confirmed-связи, которых нет среди подтверждённых человеком в payload.

    Иначе ``sync_match_json_from_links`` снова поднимет автопринятые голоса после пересчёта.
    """
    from app.app.models.speaker_profile import ArtifactSpeakerLink

    human = {
        label
        for label, row in (payload.get("matches") or {}).items()
        if _is_human_confirmed_match(row)
    }
    q = db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.artifact_id == artifact_id)
    if human:
        q.filter(~ArtifactSpeakerLink.diar_label.in_(human)).delete(synchronize_session=False)
    else:
        q.delete(synchronize_session=False)
    db.commit()


def _finalize_match_payload(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    prev_payload: dict | None,
) -> dict:
    """После auto-match: восстановить merge, confirmed человеком имена и DB links."""
    path = match_json_path(project_id, artifact_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    _restore_merge_map(payload, prev_payload)
    _restore_confirmed_matches(payload, prev_payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _prune_non_human_speaker_links(db, artifact_id=artifact_id, payload=payload)
    sync_match_json_from_links(db, project_id=project_id, artifact_id=artifact_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    _restore_merge_map(payload, prev_payload)
    _restore_confirmed_matches(payload, prev_payload)
    _apply_profile_merge_to_payload(payload)
    _propagate_merge_to_matches(payload)
    _seed_voice_aliases_from_experiments(
        artifact_dir_path(project_id, artifact_id),
        payload,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_links_from_payload(db, project_id=project_id, artifact_id=artifact_id, payload=payload)
    return payload


_LAB_DIAR_RE = re.compile(r"^speaker_\d+$")


def _is_lab_diar_label(label: str) -> bool:
    return bool(_LAB_DIAR_RE.match(str(label or "")))


def _lab_prev_payload(prev: dict | None) -> dict | None:
    """Для restore после пересчёта lab: только speaker_N, без SPEAKER_00 с чужого эталона."""
    if not prev:
        return None

    def _keep(mapping: dict | None) -> dict:
        return {key: value for key, value in (mapping or {}).items() if _is_lab_diar_label(str(key))}

    merge_map = {
        key: value
        for key, value in (prev.get("merge_map") or {}).items()
        if _is_lab_diar_label(str(key)) and _is_lab_diar_label(str(value))
    }
    return {
        **{
            key: value
            for key, value in prev.items()
            if key not in {"matches", "merge_map", "embeddings", "display_names"}
        },
        "matches": _keep(prev.get("matches")),
        "merge_map": merge_map,
        "embeddings": _keep(prev.get("embeddings")),
        "display_names": _keep(prev.get("display_names")),
    }


def _drop_legacy_speaker_links(db: Session, artifact_id: str) -> None:
    db.query(ArtifactSpeakerLink).filter(
        ArtifactSpeakerLink.artifact_id == artifact_id,
        ArtifactSpeakerLink.diar_label.like("SPEAKER_%"),
    ).delete(synchronize_session=False)
    db.commit()


def _lab_run_for_voice(settings, artifact: Artifact):
    from app.app.services.artifact_lab import latest_run_id, resolve_run

    run_id = latest_run_id(settings, artifact)
    if not run_id:
        return None
    try:
        run = resolve_run(settings, artifact, run_id)
    except FileNotFoundError:
        return None
    if run.resolve("voice/match.json").is_file() or run.resolve("voice/embed_raw.json").is_file():
        return run
    return None


def _rebuild_lab_match_from_embeddings(db: Session, *, artifact: Artifact, run) -> dict:
    """Пересчитать подсказки speaker_N по базе голосов; клипы lab не трогать."""
    from asr.lab.voice import build_match_payload, match_path as lab_match_path, profile_candidates

    lab_path = lab_match_path(run)
    if not lab_path.is_file():
        raise FileNotFoundError(f"нет {lab_path}")
    lab = json.loads(lab_path.read_text(encoding="utf-8"))
    embeddings = {
        key: value
        for key, value in (lab.get("embeddings") or {}).items()
        if _is_lab_diar_label(key) and isinstance(value, list) and value
    }
    if not embeddings:
        raise FileNotFoundError("в lab voice/match.json нет эмбеддингов speaker_N")
    centroids = {int(key.split("_")[1]): value for key, value in embeddings.items()}
    clips: dict = {}
    for label, row in (lab.get("matches") or {}).items():
        if not _is_lab_diar_label(label):
            continue
        clips[label] = {
            "speaker_index": int(label.split("_")[1]),
            "start": row.get("clip_start") or 0.0,
            "end": row.get("clip_end") or 0.0,
            "duration": row.get("clip_duration") or 0.0,
        }
    candidates = profile_candidates(
        centroids,
        load_universal_voice_profiles(db, auto_backfill=False),
        top=3,
    )
    payload = build_match_payload(
        run_id=run.run_id,
        artifact_id=artifact.artifact_id,
        project_id=artifact.project_id,
        clips=clips,
        centroids=centroids,
        candidates=candidates,
    )
    payload["source"] = "lab"
    payload["written_to_db"] = True
    return payload


def _merge_lab_prev(*parts: dict | None) -> dict | None:
    acc: dict | None = None
    for part in parts:
        if not part:
            continue
        if acc is None:
            acc = {
                "matches": dict(part.get("matches") or {}),
                "merge_map": dict(part.get("merge_map") or {}),
                "embeddings": dict(part.get("embeddings") or {}),
                "display_names": dict(part.get("display_names") or {}),
                "voice_aliases": dict(part.get("voice_aliases") or {}),
            }
            continue
        acc["matches"].update(part.get("matches") or {})
        acc["merge_map"].update(part.get("merge_map") or {})
        acc["embeddings"].update(part.get("embeddings") or {})
        acc["display_names"].update(part.get("display_names") or {})
        acc.setdefault("voice_aliases", {}).update(part.get("voice_aliases") or {})
    return acc


def _recalculate_lab_speaker_match(db: Session, artifact: Artifact, run) -> dict:
    from asr.lab.voice import match_path as lab_match_path

    out_json = match_json_path(artifact.project_id, artifact.artifact_id)
    prod_prev = json.loads(out_json.read_text(encoding="utf-8")) if out_json.is_file() else None
    lab_path = lab_match_path(run)
    lab_prev = json.loads(lab_path.read_text(encoding="utf-8")) if lab_path.is_file() else None
    prev = _merge_lab_prev(_lab_prev_payload(lab_prev), _lab_prev_payload(prod_prev))
    payload = _rebuild_lab_match_from_embeddings(db, artifact=artifact, run=run)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lab_match_path(run).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _drop_legacy_speaker_links(db, artifact.artifact_id)
    payload = _finalize_match_payload(
        db,
        project_id=artifact.project_id,
        artifact_id=artifact.artifact_id,
        prev_payload=prev,
    )
    lab_match_path(run).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rebuild_match_report_html(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)
    rebuild_transcript_report_html(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)
    canonical = sorted({payload["merge_map"][k] for k in payload["merge_map"]})
    return {
        "source": "lab",
        "profiles_in_db": payload.get("profiles_in_db", 0),
        "matches": {label: payload["matches"].get(label, {}) for label in canonical},
    }


def recalculate_speaker_match(
    db: Session,
    *,
    artifact_id: str,
    project_id: int,
    sortformer_json: Path | None = None,
    audio: Path | None = None,
) -> dict:
    """Пересчитать подсказки match. Lab — speaker_N; legacy — Sortformer SPEAKER_XX."""
    from app.app.config import get_settings

    artifact = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == artifact_id, Artifact.project_id == project_id)
        .first()
    )
    if artifact is None:
        raise FileNotFoundError(f"нет артефакта {artifact_id}")
    settings = get_settings()
    run = _lab_run_for_voice(settings, artifact)
    if run is not None:
        return _recalculate_lab_speaker_match(db, artifact, run)

    import sys

    asr_dir = str(ROOT / "asr")
    if asr_dir not in sys.path:
        sys.path.insert(0, asr_dir)
    from match_sortformer_speakers import run as match_run  # noqa: PLC0415

    if sortformer_json is None or not Path(sortformer_json).is_file():
        raise FileNotFoundError("Sortformer JSON not found for artifact")
    if audio is None or not Path(audio).is_file():
        raise FileNotFoundError("Audio WAV not found for artifact")

    out_json = match_json_path(project_id, artifact_id)
    prev_payload = None
    reuse_embeddings = None
    if out_json.is_file():
        prev_payload = json.loads(out_json.read_text(encoding="utf-8"))
        reuse_embeddings = prev_payload.get("embeddings") or None

    match_run(
        sortformer_json=sortformer_json,
        audio=audio,
        project_id=project_id,
        artifact_id=artifact_id,
        out_json=out_json,
        reuse_embeddings=reuse_embeddings,
        prev_payload=prev_payload,
    )
    payload = _finalize_match_payload(
        db,
        project_id=project_id,
        artifact_id=artifact_id,
        prev_payload=prev_payload,
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


def canonical_match_labels(project_id: int, artifact_id: str) -> list[str]:
    """Канонические Sortformer-метки из match JSON (после merge_map)."""
    path = match_json_path(project_id, artifact_id)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    merge_map = payload.get("merge_map") or {}
    matches = payload.get("matches") or {}
    seen: set[str] = set()
    labels: list[str] = []
    for label in matches:
        canon = merge_map.get(label, label)
        if canon in seen:
            continue
        seen.add(canon)
        labels.append(canon)
    return sorted(labels)


def speakers_confirm_status(db: Session, *, project_id: int, artifact_id: str) -> dict:
    """Статус подтверждения спикеров (метки speaker_N после lab)."""
    labels = canonical_match_labels(project_id, artifact_id)
    if not labels:
        return {
            "ready": False,
            "labels": [],
            "confirmed": [],
            "pending": [],
            "reason": "sortformer_match_missing",
        }

    merge_map: dict[str, str] = {}
    rows: dict = {}
    path = match_json_path(project_id, artifact_id)
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            merge_map = stored.get("merge_map") or {}
            rows = stored.get("matches") or {}
        except json.JSONDecodeError:
            merge_map = {}

    links = {
        row.diar_label: row
        for row in db.query(ArtifactSpeakerLink).filter(
            ArtifactSpeakerLink.artifact_id == artifact_id,
            ArtifactSpeakerLink.project_id == project_id,
        )
    }

    confirmed: list[str] = []
    pending: list[str] = []
    for canon in labels:
        aliases = {canon} | {lbl for lbl, tgt in merge_map.items() if tgt == canon}
        link = next((links[a] for a in aliases if a in links), None)
        # узнан по голосу автоматически, но человек карточку ещё не проверил (speaker_auto, needs_human)
        unchecked = any(isinstance(rows.get(a), dict) and rows[a].get("needs_human") for a in aliases)
        if link and link.status == "confirmed" and link.profile_id and not unchecked:
            confirmed.append(canon)
        else:
            pending.append(canon)

    return {
        "ready": len(pending) == 0,
        "labels": labels,
        "confirmed": confirmed,
        "pending": pending,
        "reason": None if not pending else "speakers_unconfirmed",
    }


SPEAKER_MATCH_UI_STATUSES = frozenset(
    {
        "speakers_pending",
        "speakers_confirmed",
        "asr_completed",
        "transcript_ready",
        "entities_extracted",
        "indexed",
        "graph_seeded",
        "done",
    }
)


def speaker_action_flags(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    status: str,
    file_type: str,
) -> dict[str, bool]:
    """Флаги кнопок таблицы: match только когда есть метки, confirm — когда все названы."""
    empty = {
        "speakers_match_available": False,
        "speakers_match_ready": False,
        "speakers_confirm_ready": False,
    }
    if file_type != "audio":
        return empty
    labels = canonical_match_labels(project_id, artifact_id)
    if not labels:
        return empty
    if status not in SPEAKER_MATCH_UI_STATUSES:
        return empty
    ready = bool(speakers_confirm_status(db, project_id=project_id, artifact_id=artifact_id)["ready"])
    if status != "speakers_pending":
        # Мягкий шлагбаум: созвон разобран, но неназванные голоса остались — кнопка «Голоса» напоминает (оранжевая).
        return {
            "speakers_match_available": True,
            "speakers_match_ready": ready,
            "speakers_confirm_ready": False,
        }
    return {
        "speakers_match_available": True,
        "speakers_match_ready": ready,
        "speakers_confirm_ready": ready,
    }


def artifact_speakers_match_ready(
    db: Session,
    *,
    project_id: int,
    artifact_id: str,
    status: str,
    file_type: str,
) -> bool | None:
    """None — кнопка match ещё не показывается; иначе все спикеры заполнены."""
    flags = speaker_action_flags(
        db,
        project_id=project_id,
        artifact_id=artifact_id,
        status=status,
        file_type=file_type,
    )
    if not flags["speakers_match_available"]:
        return None
    return flags["speakers_match_ready"]


def confirm_speakers_and_enqueue(db: Session, settings, artifact: Artifact) -> dict:
    """Все спикеры named → speakers_confirmed + job knowledge (имена + выжимка)."""
    from app.app.models.job import Job
    from app.app.services.status import set_status

    if artifact.status != "speakers_pending":
        raise ValueError(f"artifact status is {artifact.status}, expected speakers_pending")

    status = speakers_confirm_status(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)
    if not status["ready"]:
        pending = ", ".join(status["pending"])
        raise ValueError(f"не все спикеры подтверждены: {pending}")

    sync_match_json_from_links(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)
    rebuild_match_report_html(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)

    links = (
        db.query(ArtifactSpeakerLink)
        .filter(
            ArtifactSpeakerLink.artifact_id == artifact.artifact_id,
            ArtifactSpeakerLink.status == "confirmed",
            ArtifactSpeakerLink.profile_id.isnot(None),
        )
        .all()
    )
    from app.app.services.speaker_auto import auto_labels  # noqa: PLC0415

    match_path = match_json_path(artifact.project_id, artifact.artifact_id)
    auto = auto_labels(json.loads(match_path.read_text(encoding="utf-8")) if match_path.is_file() else None)
    for link in links:
        if link.diar_label in auto:
            continue  # принят автоматически по слепку: профиль уже существующий, в global не синхронизируем и не дообучаем
        prof = db.get(SpeakerProfile, link.profile_id)
        if prof is None:
            continue
        global_prof = sync_confirmed_to_global(db, prof)
        if global_prof.id != link.profile_id:
            link.profile_id = global_prof.id
            link.suggested_profile_id = global_prof.id
    db.commit()
    sync_match_json_from_links(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)

    set_status(db, artifact, "speakers_confirmed")
    job = Job(
        artifact_id=artifact.id,
        kind="process_artifact",
        status="queued",
        phase="knowledge",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"ok": True, "job_id": job.id, "status": artifact.status}
