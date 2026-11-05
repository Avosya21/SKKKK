"""Слепки голосов лаборатории: клипы из слов, WeSpeaker, match.json прогона.

Не стадия ``05_voice``: замороженный прогон её пропускал. Этап 4 пишет только
``voice/`` — как ``sense/`` и ``proof/``. Боевой
``reports/sortformer_speaker_match.json`` не трогает.

Клипы — прогоны уверенных слов (снимок 1) плюс слова оверлея этапа 2.
Эмбеддинги считает тот же WeSpeaker в ``primaryanalitics-gigaam``, иначе косинусы
несопоставимы с ``speaker_profiles``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .paths import ROOT, LabRun
from .sense import apply_overlay
from .stages.s01_audio import clean_wav
from .stages.s04_paint import ASSIGNED, paint_path

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "asr") not in sys.path:
    sys.path.insert(0, str(ROOT / "asr"))

VOICE_DIR = "voice"
VOICE_JSON = "voice.json"
MATCH_JSON = "match.json"
EMBED_RAW_JSON = "embed_raw.json"

GIGAAM_CONTAINER = "primaryanalitics-gigaam"
JOB_HOST = ROOT / "data" / "gigaam" / "embed_job.json"
RESULT_HOST = ROOT / "data" / "gigaam" / "embed_result.json"

DEFAULT_MAX_GAP = 0.6
DEFAULT_EDGE_TRIM = 0.25
DEFAULT_MIN_DURATION = 2.0
DEFAULT_MAX_DURATION = 12.0
DEFAULT_PER_SPEAKER = 3
DEFAULT_TOP_CANDIDATES = 3


def voice_dir(run: LabRun) -> Path:
    path = run.resolve(VOICE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def match_path(run: LabRun) -> Path:
    return run.resolve(f"{VOICE_DIR}/{MATCH_JSON}")


def voice_json_path(run: LabRun) -> Path:
    return run.resolve(f"{VOICE_DIR}/{VOICE_JSON}")


def embed_raw_path(run: LabRun) -> Path:
    return run.resolve(f"{VOICE_DIR}/{EMBED_RAW_JSON}")


def speaker_label(index: int) -> str:
    return f"speaker_{int(index)}"


def _countable(word: dict[str, Any], *, include_sense: bool) -> bool:
    if word.get("speaker_index") is None:
        return False
    if word.get("verdict") == ASSIGNED:
        return True
    return include_sense and bool(word.get("sense"))


def word_runs(
    words: list[dict[str, Any]],
    *,
    max_gap: float,
    include_sense: bool = False,
) -> list[dict[str, Any]]:
    """Подряд идущие покрашенные слова одного спикера.

    По умолчанию только ``assigned`` (стадия 05). С ``include_sense`` в прогон
    попадают и слова оверлея этапа 2: у них вердикт снимка остаётся спорным,
    но спикер уже известен.
    """
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for word in words:
        if not _countable(word, include_sense=include_sense):
            current = None
            continue
        speaker = int(word["speaker_index"])
        start, end = float(word["start"]), float(word["end"])
        if (
            current is not None
            and current["speaker_index"] == speaker
            and start - current["end"] <= max_gap
        ):
            current["end"] = end
            current["words"] += 1
            current["text"] += " " + word["word"]
        else:
            current = {
                "speaker_index": speaker,
                "start": start,
                "end": end,
                "words": 1,
                "text": word["word"],
            }
            runs.append(current)
    for row in runs:
        row["duration"] = round(row["end"] - row["start"], 3)
    return runs


def clips_from_runs(
    runs: list[dict[str, Any]],
    *,
    edge_trim: float,
    min_duration: float,
    max_duration: float,
    per_speaker: int,
) -> dict[str, dict[str, Any]]:
    """По ``per_speaker`` самых длинных прогонов на спикера, обрезанных с краёв."""
    by_speaker: dict[int, list[dict[str, Any]]] = {}
    for row in runs:
        start = row["start"] + edge_trim
        end = row["end"] - edge_trim
        if end - start < min_duration:
            continue
        if end - start > max_duration:
            middle = (start + end) / 2.0
            start, end = middle - max_duration / 2.0, middle + max_duration / 2.0
        by_speaker.setdefault(row["speaker_index"], []).append(
            {
                "speaker_index": row["speaker_index"],
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "words": row["words"],
                "text": row["text"][:160],
            }
        )

    clips: dict[str, dict[str, Any]] = {}
    for speaker in sorted(by_speaker):
        rows = sorted(by_speaker[speaker], key=lambda r: (-r["duration"], r["start"]))
        for order, row in enumerate(rows[:per_speaker]):
            clips[f"spk{speaker}_{order}"] = row
    return clips


def overlay_words(run: LabRun) -> tuple[list[dict[str, Any]], bool]:
    """Слова снимка 1, поверх — решения этапа 2, если файл есть."""
    paint_file = paint_path(run)
    if not paint_file.is_file():
        raise FileNotFoundError(f"нет {paint_file} — сначала 04_paint")
    paint = json.loads(paint_file.read_text(encoding="utf-8"))
    words = list(paint.get("words") or [])
    sense_file = run.resolve("sense/sense.json")
    if not sense_file.is_file():
        return words, False
    sense = json.loads(sense_file.read_text(encoding="utf-8"))
    return apply_overlay(words, sense.get("decisions") or []), True


def stage_embed_audio(run: LabRun, audio: Path) -> Path:
    """Копия WAV в asr/work с run_id в имени — чужой файл так не подставится."""
    from docker_stage import stage_docker_work_file

    return stage_docker_work_file(audio, f"lab_{run.run_id}_clean_16k.wav")


def docker_embed(staged: Path, clips: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from match_sortformer_speakers import wait_gigaam

    wait_gigaam()
    job = {
        "audio": f"/work/{staged.name}",
        "clips": {key: {"start": row["start"], "end": row["end"]} for key, row in clips.items()},
    }
    JOB_HOST.parent.mkdir(parents=True, exist_ok=True)
    RESULT_HOST.unlink(missing_ok=True)
    JOB_HOST.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    proc = subprocess.run(
        ["docker", "exec", GIGAAM_CONTAINER, "python", "/data/embed_voice.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"embed_voice.py вернул {proc.returncode}: {proc.stderr.strip()[:500]}")
    if not RESULT_HOST.is_file():
        raise RuntimeError(f"контейнер не создал {RESULT_HOST}")

    payload = json.loads(RESULT_HOST.read_text(encoding="utf-8"))
    speakers = payload.get("speakers") or {}
    missing = sorted(set(clips) - set(speakers))
    extra = sorted(set(speakers) - set(clips))
    if missing or extra:
        raise RuntimeError(
            "ответ embed_voice не соответствует заданию: "
            f"нет {missing}, лишние {extra} — вероятно, параллельный прогон перезаписал "
            f"{RESULT_HOST.name}"
        )
    return payload


def clip_embeddings(payload: dict[str, Any]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for key, row in (payload.get("speakers") or {}).items():
        vector = row.get("embedding")
        if isinstance(vector, list) and vector:
            out[key] = [float(v) for v in vector]
    return out


def centroids_from_clips(
    clips: dict[str, dict[str, Any]],
    embeddings: dict[str, list[float]],
) -> dict[int, list[float]]:
    from speaker_registry import mean_embedding

    centroids: dict[int, list[float]] = {}
    speakers = sorted({int(row["speaker_index"]) for row in clips.values()})
    for speaker in speakers:
        vectors = [
            embeddings[key]
            for key, row in clips.items()
            if int(row["speaker_index"]) == speaker and key in embeddings
        ]
        centroid = mean_embedding(vectors)
        if centroid:
            centroids[speaker] = centroid
    return centroids


def longest_clip(clips: dict[str, dict[str, Any]], speaker: int) -> dict[str, Any] | None:
    rows = [row for row in clips.values() if int(row["speaker_index"]) == speaker]
    if not rows:
        return None
    return max(rows, key=lambda row: (float(row.get("duration") or 0), -float(row.get("start") or 0)))


def load_profiles(project_id: int) -> list[dict[str, Any]]:
    """Профили голосов только на чтение: проектные плюс глобальные."""
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import SpeakerProfile

    db = SessionLocal()
    try:
        rows = (
            db.query(SpeakerProfile)
            .filter(
                (SpeakerProfile.scope == "global") | (SpeakerProfile.project_id == project_id)
            )
            .all()
        )
        return [
            {
                "id": row.id,
                "display_name": row.display_name,
                "scope": row.scope,
                "embedding": row.embedding,
            }
            for row in rows
        ]
    finally:
        db.close()


def profile_candidates(
    centroids: dict[int, list[float]],
    profiles: list[dict[str, Any]],
    *,
    top: int,
) -> dict[str, list[dict[str, Any]]]:
    from speaker_registry import cosine, unpack_embedding

    candidates: dict[str, list[dict[str, Any]]] = {}
    for speaker, centroid in centroids.items():
        scored = [
            {
                "profile_id": profile["id"],
                "display_name": profile["display_name"],
                "scope": profile["scope"],
                "cosine": round(cosine(centroid, unpack_embedding(profile["embedding"])), 4),
            }
            for profile in profiles
        ]
        scored.sort(key=lambda row: -row["cosine"])
        candidates[speaker_label(speaker)] = scored[:top]
    return candidates


def cross_speaker_cosine(centroids: dict[int, list[float]]) -> list[dict[str, Any]]:
    from speaker_registry import cosine

    speaker_ids = sorted(centroids)
    return [
        {
            "a": speaker_label(a),
            "b": speaker_label(b),
            "cosine": round(cosine(centroids[a], centroids[b]), 4),
        }
        for index, a in enumerate(speaker_ids)
        for b in speaker_ids[index + 1 :]
    ]


def build_match_payload(
    *,
    run_id: str,
    artifact_id: str,
    project_id: int,
    clips: dict[str, dict[str, Any]],
    centroids: dict[int, list[float]],
    candidates: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Контракт как у прод match JSON, метки ``speaker_N``, источник lab."""
    embeddings = {speaker_label(index): vector for index, vector in centroids.items()}
    merge_map = {label: label for label in embeddings}
    matches: dict[str, dict[str, Any]] = {}
    display_names: dict[str, str] = {}
    for speaker, _vector in centroids.items():
        label = speaker_label(speaker)
        clip = longest_clip(clips, speaker) or {}
        top = (candidates.get(label) or [{}])[0]
        matches[label] = {
            "diar_label": label,
            "clip_start": clip.get("start", 0.0),
            "clip_end": clip.get("end", 0.0),
            "clip_duration": clip.get("duration", 0.0),
            "profile_id": None,
            "profile_name": None,
            "score": top.get("cosine"),
            "status": "new",
            "suggested_profile_id": top.get("profile_id"),
            "suggested_profile_name": top.get("display_name"),
            "suggested_score": top.get("cosine"),
        }
        display_names[label] = label
    return {
        "source": "lab",
        "run_id": run_id,
        "artifact_id": artifact_id,
        "project_id": project_id,
        "embeddings": embeddings,
        "merge_map": merge_map,
        "matches": matches,
        "display_names": display_names,
        "profile_candidates": candidates,
        "profiles_in_db": sum(len(rows) for rows in candidates.values()),
        "written_to_db": False,
    }


def build_voice_payload(
    *,
    run_id: str,
    artifact_id: str,
    project_id: int,
    sense_applied: bool,
    runs: list[dict[str, Any]],
    clips: dict[str, dict[str, Any]],
    embeddings: dict[str, list[float]],
    centroids: dict[int, list[float]],
    candidates: dict[str, list[dict[str, Any]]],
    profiles_compared: int,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "overlay": True,
        "stage": VOICE_DIR,
        "artifact_id": artifact_id,
        "project_id": project_id,
        "sense_applied": sense_applied,
        "written_to_db": False,
        "clip_count": len(clips),
        "run_count": len(runs),
        "embedding_dim": len(next(iter(embeddings.values()))) if embeddings else 0,
        "clips": [
            {"key": key, **row, "embedded": key in embeddings}
            for key, row in clips.items()
        ],
        "cross_speaker_cosine": cross_speaker_cosine(centroids),
        "profile_candidates": candidates,
        "profiles_compared": profiles_compared,
    }


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
