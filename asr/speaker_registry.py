"""Match Sortformer/PyAnnote SPEAKER_XX к speaker_profiles (WeSpeaker cosine)."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Segment:
    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class ProfileMatch:
    diar_label: str
    clip_start: float
    clip_end: float
    clip_duration: float
    profile_id: int | None
    profile_name: str | None
    score: float | None
    status: str
    suggested_profile_id: int | None = None
    suggested_profile_name: str | None = None
    suggested_score: float | None = None


# При «Запомнить»: при cosine ≥ порога — привязка к существующему profile_id.
VOICE_AUTO_LINK_THRESHOLD = 0.995
# Слияние project-профиля в global при совпадении имени или cosine.
GLOBAL_VOICE_MERGE_THRESHOLD = 0.95


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-9 or nb < 1e-9:
        return -1.0
    return dot / (na * nb)


def flatten_overlaps(segments: list[Segment]) -> list[Segment]:
    """Убрать перекрытия Sortformer: оставить более длинный сегмент."""
    if not segments:
        return []
    rows = sorted(segments, key=lambda s: (s.start, -(s.end - s.start)))
    out: list[Segment] = []
    for seg in rows:
        if any(seg.start < kept.end and seg.end > kept.start for kept in out):
            continue
        out.append(seg)
    out.sort(key=lambda s: s.start)
    return out


def best_clip_per_speaker(
    segments: list[Segment],
    *,
    min_duration: float = 2.5,
    prefer_min: float = 3.0,
) -> dict[str, Segment]:
    """Самый длинный чистый кусок на SPEAKER_XX без overlap."""
    clean = flatten_overlaps(segments)
    by_sp: dict[str, list[Segment]] = {}
    for seg in clean:
        if seg.duration < min_duration:
            continue
        by_sp.setdefault(seg.speaker, []).append(seg)
    best: dict[str, Segment] = {}
    for sp, rows in by_sp.items():
        rows.sort(key=lambda s: (-s.duration, s.start))
        long = [r for r in rows if r.duration >= prefer_min]
        best[sp] = (long or rows)[0]
    return best


def merge_duplicate_labels(
    labels: list[str],
    embeddings: dict[str, list[float]],
    *,
    threshold: float = 0.82,
) -> dict[str, str]:
    """Склеить SPEAKER_XX с одинаковым отпечатком."""
    mapping = {lb: lb for lb in labels}
    keys = list(labels)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            ea, eb = embeddings.get(a), embeddings.get(b)
            if ea is None or eb is None:
                continue
            if cosine(ea, eb) >= threshold:
                mapping[b] = mapping[a]
    return mapping


def ensure_merge_map(all_labels: list[str], dup_map: dict[str, str]) -> dict[str, str]:
    """Каждый Sortformer ID должен быть в merge_map (хотя бы identity)."""
    out = dict(dup_map)
    for lb in all_labels:
        out.setdefault(lb, lb)
    return out


def match_profiles(
    embeddings: dict[str, list[float]],
    profiles: list[dict],
    *,
    threshold: float = 0.72,
) -> dict[str, ProfileMatch]:
    """Подсказки по базе голосов — без подстановки имён в стенogramму до «Запомнить»."""
    results: dict[str, ProfileMatch] = {}
    for label, emb in embeddings.items():
        best_id: int | None = None
        best_name: str | None = None
        best_score = -1.0
        for row in profiles:
            score = cosine(emb, unpack_embedding(row["embedding"]))
            if score > best_score:
                best_score = score
                best_id = row["id"]
                best_name = row["display_name"]
        sug_score = round(best_score, 4) if best_score >= 0 else None
        if best_score >= threshold:
            results[label] = ProfileMatch(
                diar_label=label,
                clip_start=0.0,
                clip_end=0.0,
                clip_duration=0.0,
                profile_id=None,
                profile_name=None,
                score=None,
                status="new",
                suggested_profile_id=best_id,
                suggested_profile_name=best_name,
                suggested_score=sug_score,
            )
        else:
            results[label] = ProfileMatch(
                diar_label=label,
                clip_start=0.0,
                clip_end=0.0,
                clip_duration=0.0,
                profile_id=None,
                profile_name=None,
                score=None,
                status="new",
                suggested_score=sug_score,
            )
    return results


def best_profile_match(
    embedding: list[float],
    profiles: list[dict],
) -> tuple[int | None, str | None, float | None]:
    """Лучший cosine к speaker_profiles (для enroll)."""
    best_id: int | None = None
    best_name: str | None = None
    best_score = -1.0
    for row in profiles:
        score = cosine(embedding, unpack_embedding(row["embedding"]))
        if score > best_score:
            best_score = score
            best_id = row["id"]
            best_name = row["display_name"]
    if best_score < 0:
        return None, None, None
    return best_id, best_name, round(best_score, 4)


def merge_dup_map_by_profile_id(
    dup_map: dict[str, str],
    matches: dict[str, ProfileMatch | dict],
) -> dict[str, str]:
    """Склеить Sortformer ID, если пользователь подтвердил один profile_id."""
    label_to_profile: dict[str, int] = {}
    for label in set(dup_map) | set(dup_map.values()) | set(matches):
        m = matches.get(label)
        if not m:
            continue
        pid = m.profile_id if isinstance(m, ProfileMatch) else m.get("profile_id")
        status = m.status if isinstance(m, ProfileMatch) else m.get("status")
        if pid and status in ("confirmed", "merged_duplicate"):
            label_to_profile[label] = pid

    by_profile: dict[int, list[str]] = {}
    for label, pid in label_to_profile.items():
        by_profile.setdefault(pid, []).append(label)

    for labels in by_profile.values():
        uniq = sorted(set(labels))
        if len(uniq) < 2:
            continue
        primary = uniq[0]
        for label in uniq:
            for key, val in list(dup_map.items()):
                if val == label or key == label:
                    dup_map[key] = primary
            dup_map[label] = primary
    return dup_map


def average_embeddings(a: list[float], b: list[float]) -> list[float]:
    if len(a) != len(b):
        return a or b
    return [(x + y) / 2.0 for x, y in zip(a, b)]


def mean_embedding(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    dim = len(vectors[0])
    if dim < 1 or any(len(v) != dim for v in vectors):
        return None
    n = float(len(vectors))
    return [sum(v[i] for v in vectors) / n for i in range(dim)]


# Post-check реплик: почти уверенная чужая метка → auto_reassign.
AUDIT_MIN_TURN_SEC = 2.0
AUDIT_CENTROID_MIN_SEC = 2.5
AUDIT_AUTO_HIT = 0.88
AUDIT_MARGIN = 0.12
AUDIT_EDGE_TRIM_SEC = 0.15


@dataclass
class TurnAuditDecision:
    turn_index: int
    start: float
    end: float
    current_speaker: str
    best_speaker: str | None
    score_current: float | None
    score_best: float | None
    action: str
    reason: str
    new_speaker: str | None = None


def decide_turn_reassign(
    current_speaker: str,
    scores: dict[str, float],
    *,
    auto_hit: float = AUDIT_AUTO_HIT,
    margin: float = AUDIT_MARGIN,
) -> TurnAuditDecision:
    """Решение по одной реплике: keep | auto_reassign | skip.

    auto_reassign только если лучший чужой голос ≥ auto_hit и выше текущего на ≥ margin.
    """
    stub = TurnAuditDecision(
        turn_index=-1,
        start=0.0,
        end=0.0,
        current_speaker=current_speaker,
        best_speaker=None,
        score_current=None,
        score_best=None,
        action="skip",
        reason="no_scores",
    )
    if not scores:
        return stub
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_speaker, best_score = ranked[0]
    score_current = scores.get(current_speaker)
    stub.best_speaker = best_speaker
    stub.score_best = round(best_score, 4)
    stub.score_current = round(score_current, 4) if score_current is not None else None

    if best_speaker == current_speaker:
        stub.action = "keep"
        stub.reason = "matches_label"
        return stub

    current = score_current if score_current is not None else -1.0
    if best_score >= auto_hit and (best_score - current) >= margin:
        stub.action = "auto_reassign"
        stub.reason = "high_confidence_mismatch"
        stub.new_speaker = best_speaker
        return stub

    stub.action = "keep"
    stub.reason = "below_auto_threshold"
    return stub


def build_leave_one_out_centroids(
    labeled_embeddings: list[tuple[int, str, list[float]]],
    *,
    speakers: list[str],
) -> dict[int, dict[str, list[float]]]:
    """Для каждого turn_index — центроиды спикеров без эмбеддинга этой реплики."""
    by_speaker: dict[str, list[tuple[int, list[float]]]] = {s: [] for s in speakers}
    for idx, sp, emb in labeled_embeddings:
        by_speaker.setdefault(sp, []).append((idx, emb))

    out: dict[int, dict[str, list[float]]] = {}
    for idx, _sp, _emb in labeled_embeddings:
        centroids: dict[str, list[float]] = {}
        for sp, rows in by_speaker.items():
            others = [emb for j, emb in rows if j != idx]
            if not others:
                # единственная реплика спикера — центроид = она сама (leave-one-out пуст)
                only = [emb for j, emb in rows if j == idx]
                mean = mean_embedding(only)
            else:
                mean = mean_embedding(others)
            if mean is not None:
                centroids[sp] = mean
        out[idx] = centroids
    return out


def score_against_centroids(
    embedding: list[float],
    centroids: dict[str, list[float]],
) -> dict[str, float]:
    return {sp: cosine(embedding, vec) for sp, vec in centroids.items()}


def apply_label_map(segments: list[Segment], mapping: dict[str, str]) -> list[Segment]:
    out: list[Segment] = []
    for seg in segments:
        canonical = mapping.get(seg.speaker, seg.speaker)
        out.append(Segment(start=seg.start, end=seg.end, speaker=canonical))
    return out


def load_sortformer_segments(path: Path) -> list[Segment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("segments") or []
    return [
        Segment(start=float(r["start"]), end=float(r["end"]), speaker=str(r["speaker"]))
        for r in rows
        if isinstance(r, dict) and r.get("end", 0) > r.get("start", 0)
    ]
