"""Match Sortformer SPEAKER_XX → speaker_profiles (проект + global).

python asr/match_sortformer_speakers.py
python asr/match_sortformer_speakers.py --project-id 1 --artifact-id art_2026-08-19_211842_54f88c
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))
sys.path.insert(0, str(ROOT))

from speaker_registry import (  # noqa: E402
    ProfileMatch,
    apply_label_map,
    best_clip_per_speaker,
    ensure_merge_map,
    load_sortformer_segments,
    merge_duplicate_labels,
    merge_dup_map_by_profile_id,
    pack_embedding,
)

GIGAAM = "http://127.0.0.1:18102"
JOB_HOST = ROOT / "data" / "gigaam" / "embed_job.json"
RESULT_HOST = ROOT / "data" / "gigaam" / "embed_result.json"
DEFAULT_SF = ROOT / "experiments/nemo-diar/out/pilot_22_07_26/sortformer_gguf_vulkan_input_16k_report.json"
DEFAULT_WAV = ROOT / "asr/work/input_16k.wav"


def wait_gigaam() -> None:
    subprocess.run(["docker", "start", "skkkk-gigaam"], check=False)
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{GIGAAM}/health", timeout=5).read()
            return
        except Exception:
            time.sleep(3)
    raise SystemExit("skkkk-gigaam не поднялся")


def docker_embed(audio: Path, clips: dict[str, dict]) -> dict[str, dict]:
    docker_audio = "/work/input_16k.wav"
    if audio != DEFAULT_WAV:
        docker_audio = f"/work/{audio.name}"
    job = {"audio": docker_audio, "clips": clips}
    JOB_HOST.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    proc = subprocess.run(
        ["docker", "exec", "skkkk-gigaam", "python", "/data/embed_voice.py"],
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"embed_voice failed: {proc.returncode}")
    if not RESULT_HOST.is_file():
        raise SystemExit(f"нет {RESULT_HOST}")
    return json.loads(RESULT_HOST.read_text(encoding="utf-8")).get("speakers") or {}


def load_confirmed_links(project_id: int, artifact_id: str) -> dict[str, dict]:
    """Подтверждённые привязки diar_label → {id, display_name}."""
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile

    db = SessionLocal()
    try:
        links = (
            db.query(ArtifactSpeakerLink)
            .filter(
                ArtifactSpeakerLink.artifact_id == artifact_id,
                ArtifactSpeakerLink.status == "confirmed",
                ArtifactSpeakerLink.profile_id.isnot(None),
            )
            .all()
        )
        if not links:
            return {}
        profile_ids = {link.profile_id for link in links if link.profile_id}
        profiles = {
            p.id: p
            for p in db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(profile_ids)).all()
        }
        out: dict[str, dict] = {}
        for link in links:
            prof = profiles.get(link.profile_id)
            if not prof:
                continue
            row = {"id": prof.id, "display_name": prof.display_name}
            out[link.diar_label] = row
        return out
    finally:
        db.close()


def apply_confirmed_matches(
    matches: dict[str, ProfileMatch],
    dup_map: dict[str, str],
    confirmed: dict[str, dict],
) -> None:
    """Подтверждённые имена пользователя важнее cosine-подсказки."""
    for label in list(matches):
        canon = dup_map.get(label, label)
        if canon != label:
            continue
        row = confirmed.get(label) or confirmed.get(canon)
        if not row:
            continue
        m = matches[label]
        m.profile_id = row["id"]
        m.profile_name = row["display_name"]
        m.score = 1.0
        m.status = "confirmed"


def load_profiles(project_id: int) -> list[dict]:
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import SpeakerProfile

    db = SessionLocal()
    try:
        rows = (
            db.query(SpeakerProfile)
            .filter(
                (SpeakerProfile.scope == "global")
                | ((SpeakerProfile.scope == "project") & (SpeakerProfile.project_id == project_id))
            )
            .all()
        )
        return [
            {
                "id": r.id,
                "display_name": r.display_name,
                "embedding": r.embedding,
                "scope": r.scope,
            }
            for r in rows
        ]
    finally:
        db.close()


def _match_to_dict(m: ProfileMatch) -> dict:
    return {
        "diar_label": m.diar_label,
        "clip_start": m.clip_start,
        "clip_end": m.clip_end,
        "clip_duration": m.clip_duration,
        "profile_id": m.profile_id,
        "profile_name": m.profile_name,
        "score": m.score,
        "status": m.status,
        "suggested_profile_id": m.suggested_profile_id,
        "suggested_profile_name": m.suggested_profile_name,
        "suggested_score": m.suggested_score,
    }


def save_links(
    *,
    project_id: int,
    artifact_id: str,
    matches: dict[str, ProfileMatch],
    clips: dict[str, dict],
) -> None:
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import ArtifactSpeakerLink

    db = SessionLocal()
    try:
        db.query(ArtifactSpeakerLink).filter(ArtifactSpeakerLink.artifact_id == artifact_id).delete()
        for label, match in matches.items():
            clip = clips.get(label, {})
            confirmed = match.status == "confirmed" and match.profile_id
            db.add(
                ArtifactSpeakerLink(
                    artifact_id=artifact_id,
                    project_id=project_id,
                    diar_label=label,
                    suggested_profile_id=match.suggested_profile_id or match.profile_id,
                    profile_id=match.profile_id if confirmed else None,
                    match_score=match.score if match.score is not None else match.suggested_score,
                    status=match.status,
                    clip_start_sec=clip.get("start"),
                    clip_end_sec=clip.get("end"),
                )
            )
        db.commit()
    finally:
        db.close()


def _match_from_embeddings(
    *,
    segments: list,
    all_speaker_labels: list[str],
    embeddings: dict[str, list[float]],
    clips_payload: dict[str, dict],
    project_id: int,
    artifact_id: str,
    match_threshold: float,
    merge_threshold: float,
    confirmed: dict[str, dict],
) -> tuple[dict[str, str], dict[str, ProfileMatch], list, list]:
    dup_map = ensure_merge_map(
        all_speaker_labels,
        merge_duplicate_labels(list(embeddings.keys()), embeddings, threshold=merge_threshold),
    )
    canonical = sorted({dup_map[k] for k in dup_map if k in embeddings})

    from speaker_registry import match_profiles

    profiles = load_profiles(project_id)
    matches: dict[str, ProfileMatch] = {}
    for label in canonical:
        emb = embeddings[label]
        clip = clips_payload.get(label, {})
        partial = match_profiles({label: emb}, profiles, threshold=match_threshold)[label]
        partial.clip_start = clip.get("start", 0.0)
        partial.clip_end = clip.get("end", 0.0)
        partial.clip_duration = clip.get("duration", 0.0)
        matches[label] = partial
        for alias, canon in dup_map.items():
            if canon == label and alias != label:
                alias_clip = clips_payload.get(alias, clip)
                matches[alias] = ProfileMatch(
                    diar_label=alias,
                    clip_start=alias_clip.get("start", partial.clip_start),
                    clip_end=alias_clip.get("end", partial.clip_end),
                    clip_duration=alias_clip.get("duration", partial.clip_duration),
                    profile_id=partial.profile_id,
                    profile_name=partial.profile_name,
                    score=partial.score,
                    status="merged_duplicate",
                    suggested_profile_id=partial.suggested_profile_id,
                    suggested_profile_name=partial.suggested_profile_name,
                    suggested_score=partial.suggested_score,
                )

    apply_confirmed_matches(matches, dup_map, confirmed)
    for label, canon in list(dup_map.items()):
        if canon not in matches:
            continue
        primary = matches[canon]
        if primary.status != "confirmed":
            continue
        if label == canon:
            continue
        clip = clips_payload.get(label) or clips_payload.get(canon, {})
        matches[label] = ProfileMatch(
            diar_label=label,
            clip_start=clip.get("start", primary.clip_start),
            clip_end=clip.get("end", primary.clip_end),
            clip_duration=clip.get("duration", primary.clip_duration),
            profile_id=primary.profile_id,
            profile_name=primary.profile_name,
            score=primary.score,
            status="merged_duplicate",
        )

    merged_segments = apply_label_map(segments, dup_map)
    return dup_map, matches, merged_segments, profiles


def run(
    *,
    sortformer_json: Path,
    audio: Path,
    project_id: int,
    artifact_id: str,
    out_json: Path,
    match_threshold: float = 0.72,
    merge_threshold: float = 0.95,
    reuse_embeddings: dict[str, list[float]] | None = None,
    ignore_confirmed: bool = False,
    prev_payload: dict | None = None,
) -> dict:
    segments = load_sortformer_segments(sortformer_json)
    all_speaker_labels = sorted({s.speaker for s in segments})
    clips_map = best_clip_per_speaker(segments)
    if not clips_map and not reuse_embeddings:
        raise SystemExit("нет сегментов ≥2.5 с для embedding")

    clips_payload = {
        sp: {"start": seg.start, "end": seg.end, "duration": seg.duration}
        for sp, seg in clips_map.items()
    }
    if reuse_embeddings:
        embeddings = reuse_embeddings
        prev_matches = (prev_payload or {}).get("matches") or {}
        for label in reuse_embeddings:
            if label in clips_payload:
                continue
            prev_match = prev_matches.get(label) or {}
            if prev_match.get("clip_start") is None:
                continue
            clips_payload[label] = {
                "start": prev_match["clip_start"],
                "end": prev_match["clip_end"],
                "duration": prev_match.get("clip_duration", 0),
            }
    else:
        wait_gigaam()
        embedded = docker_embed(audio, clips_payload)
        embeddings = {sp: row["embedding"] for sp, row in embedded.items()}

    confirmed = {} if ignore_confirmed else load_confirmed_links(project_id, artifact_id)
    dup_map, matches, merged_segments, profiles = _match_from_embeddings(
        segments=segments,
        all_speaker_labels=all_speaker_labels,
        embeddings=embeddings,
        clips_payload=clips_payload,
        project_id=project_id,
        artifact_id=artifact_id,
        match_threshold=match_threshold,
        merge_threshold=merge_threshold,
        confirmed=confirmed,
    )
    for label in all_speaker_labels:
        if label in matches:
            continue
        canon = dup_map.get(label, label)
        segs = [s for s in segments if s.speaker == label]
        seg = max(segs, key=lambda s: s.duration) if segs else None
        matches[label] = ProfileMatch(
            diar_label=label,
            clip_start=seg.start if seg else 0.0,
            clip_end=seg.end if seg else 0.0,
            clip_duration=seg.duration if seg else 0.0,
            profile_id=None,
            profile_name=None,
            score=None,
            status="new",
        )
    canonical = sorted({dup_map[k] for k in dup_map if k in embeddings})
    display_names: dict[str, str] = {}
    for label in all_speaker_labels:
        canon = dup_map.get(label, label)
        m = matches.get(label) or matches.get(canon)
        if not m:
            display_names[label] = label
            continue
        if m.status in ("confirmed", "merged_duplicate") and m.profile_name:
            display_names[label] = m.profile_name
        else:
            display_names[label] = label

    def _segment_label(raw: str) -> str:
        return display_names.get(raw, raw)

    segments_labeled = [
        {
            "start": s.start,
            "end": s.end,
            "speaker": _segment_label(s.speaker),
            "diar_label": s.speaker,
        }
        for s in merged_segments
    ]

    payload = {
        "artifact_id": artifact_id,
        "project_id": project_id,
        "sortformer_json": str(sortformer_json),
        "audio": str(audio),
        "merge_map": dup_map,
        "match_threshold": match_threshold,
        "merge_threshold": merge_threshold,
        "profiles_in_db": len(profiles),
        "matches": {k: _match_to_dict(v) for k, v in matches.items()},
        "segments_merged": [
            {"start": s.start, "end": s.end, "speaker": s.speaker} for s in merged_segments
        ],
        "segments_labeled": segments_labeled,
        "display_names": display_names,
        "embeddings": {label: embeddings[label] for label in canonical},
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    save_links(project_id=project_id, artifact_id=artifact_id, matches=matches, clips=clips_payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int, default=1)
    parser.add_argument("--artifact-id", default="art_2026-08-19_211842_54f88c")
    parser.add_argument("--sortformer-json", type=Path, default=DEFAULT_SF)
    parser.add_argument("--audio", type=Path, default=DEFAULT_WAV)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT
        / "data/projects/1/artifacts/art_2026-08-19_211842_54f88c/reports/sortformer_speaker_match.json",
    )
    parser.add_argument(
        "--reuse-embeddings",
        action="store_true",
        help="пересчитать match из embeddings в существующем --out JSON (без docker)",
    )
    parser.add_argument(
        "--ignore-confirmed",
        action="store_true",
        help="не подтягивать confirmed links из БД (сброс авто-имён)",
    )
    args = parser.parse_args()
    prev_payload = None
    reuse = None
    if args.reuse_embeddings:
        if not args.out.is_file():
            raise SystemExit(f"--reuse-embeddings: нет {args.out}")
        prev_payload = json.loads(args.out.read_text(encoding="utf-8"))
        reuse = prev_payload.get("embeddings") or {}
        if not reuse:
            raise SystemExit("нет embeddings в JSON")
    payload = run(
        sortformer_json=args.sortformer_json,
        audio=args.audio,
        project_id=args.project_id,
        artifact_id=args.artifact_id,
        out_json=args.out,
        reuse_embeddings=reuse,
        ignore_confirmed=args.ignore_confirmed,
        prev_payload=prev_payload,
    )
    print("matches:")
    for label in sorted(set(payload["merge_map"].values())):
        m = payload["matches"].get(label, {})
        print(
            f"  {label}: status={m.get('status')} profile={m.get('profile_name')} "
            f"score={m.get('score')} clip={m.get('clip_duration')}s"
        )
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
