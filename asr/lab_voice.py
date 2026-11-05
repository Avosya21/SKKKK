"""Этап 4 — слепки голосов лаборатории в voice/, без записи в БД.

Клипы из покраски снимка и оверлея sense/, эмбеддинги WeSpeaker в primaryanalitics-gigaam,
подсказки из speaker_profiles. Кнопки «Запомнить» / «Слить» на report-lab
пишут в ту же БД; этот CLI только готовит voice/match.json.

    venv\\Scripts\\python.exe asr\\lab_voice.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.paths import LabRun  # noqa: E402
from asr.lab.stages.s01_audio import clean_wav  # noqa: E402
from asr.lab.voice import (  # noqa: E402
    DEFAULT_EDGE_TRIM,
    DEFAULT_MAX_DURATION,
    DEFAULT_MAX_GAP,
    DEFAULT_MIN_DURATION,
    DEFAULT_PER_SPEAKER,
    DEFAULT_TOP_CANDIDATES,
    GIGAAM_CONTAINER,
    VOICE_DIR,
    build_match_payload,
    build_voice_payload,
    clip_embeddings,
    clips_from_runs,
    centroids_from_clips,
    docker_embed,
    embed_raw_path,
    load_profiles,
    match_path,
    overlay_words,
    profile_candidates,
    stage_embed_audio,
    voice_json_path,
    word_runs,
    write_json,
)


def artifact_paths(project_id: int, artifact_id: str) -> tuple[Path, Path]:
    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    root = data_dir / "projects" / str(project_id) / "artifacts" / artifact_id
    audio = root / "audio" / "input_16k.wav"
    return root, audio


def run_voice(
    lab_run: LabRun,
    *,
    reuse_embeddings: bool = False,
    max_gap: float = DEFAULT_MAX_GAP,
    edge_trim: float = DEFAULT_EDGE_TRIM,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_duration: float = DEFAULT_MAX_DURATION,
    per_speaker: int = DEFAULT_PER_SPEAKER,
    top_candidates: int = DEFAULT_TOP_CANDIDATES,
) -> dict[str, Any]:
    meta = json.loads(lab_run.run_json.read_text(encoding="utf-8"))
    artifact_id = str(meta.get("artifact_id") or "")
    project_id = int(meta.get("project_id") or 0)
    words, sense_applied = overlay_words(lab_run)
    runs = word_runs(words, max_gap=max_gap, include_sense=True)
    clips = clips_from_runs(
        runs,
        edge_trim=edge_trim,
        min_duration=min_duration,
        max_duration=max_duration,
        per_speaker=per_speaker,
    )
    if not clips:
        raise RuntimeError(
            "не нашлось ни одного прогона слов длиннее "
            f"{min_duration} с — нечего превращать в слепок голоса"
        )

    raw_path = embed_raw_path(lab_run)
    if reuse_embeddings:
        if not raw_path.is_file():
            raise FileNotFoundError(f"нет {raw_path} — сначала прогон без --reuse-embeddings")
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        audio = clean_wav(lab_run)
        if not audio.is_file():
            raise FileNotFoundError(f"нет {audio} — сначала 01_audio")
        staged = stage_embed_audio(lab_run, audio)
        try:
            payload = docker_embed(staged, clips)
        finally:
            staged.unlink(missing_ok=True)
        write_json(raw_path, payload)

    embeddings = clip_embeddings(payload)
    if not embeddings:
        raise RuntimeError("контейнер вернул клипы без эмбеддингов")
    centroids = centroids_from_clips(clips, embeddings)
    if not centroids:
        raise RuntimeError("не собрались центроиды спикеров")

    try:
        profiles = load_profiles(project_id)
    except Exception as exc:  # pragma: no cover - CLI без приложения
        print(f"профили БД недоступны ({exc}), подсказки пустые", file=sys.stderr)
        profiles = []
    candidates = profile_candidates(centroids, profiles, top=top_candidates)

    match = build_match_payload(
        run_id=lab_run.run_id,
        artifact_id=artifact_id,
        project_id=project_id,
        clips=clips,
        centroids=centroids,
        candidates=candidates,
    )
    voice = build_voice_payload(
        run_id=lab_run.run_id,
        artifact_id=artifact_id,
        project_id=project_id,
        sense_applied=sense_applied,
        runs=runs,
        clips=clips,
        embeddings=embeddings,
        centroids=centroids,
        candidates=candidates,
        profiles_compared=len(profiles),
    )
    write_json(match_path(lab_run), match)
    write_json(voice_json_path(lab_run), voice)
    return {
        "run_id": lab_run.run_id,
        "overlay": VOICE_DIR,
        "sense_applied": sense_applied,
        "runs": len(runs),
        "clips": len(clips),
        "embedded": len(embeddings),
        "speakers": sorted(match["embeddings"]),
        "profiles_compared": len(profiles),
        "embedder": GIGAAM_CONTAINER,
        "written_to_db": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Слепки голосов лаборатории в voice/")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--reuse-embeddings",
        action="store_true",
        help="пересчитать match из voice/embed_raw.json без Docker",
    )
    parser.add_argument("--max-gap", type=float, default=DEFAULT_MAX_GAP)
    parser.add_argument("--edge-trim", type=float, default=DEFAULT_EDGE_TRIM)
    parser.add_argument("--min-duration", type=float, default=DEFAULT_MIN_DURATION)
    parser.add_argument("--max-duration", type=float, default=DEFAULT_MAX_DURATION)
    parser.add_argument("--clips-per-speaker", type=int, default=DEFAULT_PER_SPEAKER)
    parser.add_argument("--top-candidates", type=int, default=DEFAULT_TOP_CANDIDATES)
    args = parser.parse_args()

    root, _audio = artifact_paths(args.project_id, args.artifact_id)
    run = LabRun(root, args.run_id)
    if not run.run_json.is_file():
        print(f"нет прогона {args.run_id} в {root}", file=sys.stderr)
        return 3
    summary = run_voice(
        run,
        reuse_embeddings=args.reuse_embeddings,
        max_gap=args.max_gap,
        edge_trim=args.edge_trim,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        per_speaker=args.clips_per_speaker,
        top_candidates=args.top_candidates,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    os.chdir(ROOT)
    raise SystemExit(main())
