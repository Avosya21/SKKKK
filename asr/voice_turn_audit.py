"""Автопроверка меток спикеров по голосу (WeSpeaker) после нарезки реплик.

  python asr/voice_turn_audit.py
  python asr/voice_turn_audit.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))
sys.path.insert(0, str(ROOT))

from paths import OUT, WORK  # noqa: E402
from docker_stage import stage_gigaam_docker_input  # noqa: E402
from speaker_registry import (  # noqa: E402
    AUDIT_AUTO_HIT,
    AUDIT_CENTROID_MIN_SEC,
    AUDIT_EDGE_TRIM_SEC,
    AUDIT_MARGIN,
    AUDIT_MIN_TURN_SEC,
    TurnAuditDecision,
    build_leave_one_out_centroids,
    decide_turn_reassign,
    score_against_centroids,
)
from speakers import (  # noqa: E402
    Turn,
    format_turns_timecodes,
    format_turns_txt,
    merge_adjacent,
    turns_to_json,
)

GIGAAM_WORK = ASR / "work"
DEFAULT_SPEAKERS = OUT / "docker_gigaam_speakers.json"
DEFAULT_AUDIO = WORK / "input_16k.wav"


def load_turns(path: Path) -> list[Turn]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("turns") or []
    turns: list[Turn] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = float(row.get("start") or 0)
        end = float(row.get("end") or 0)
        if end <= start:
            continue
        turns.append(
            Turn(
                start=start,
                end=end,
                speaker=str(row.get("speaker") or "").strip() or "Спикер №?",
                text=str(row.get("text") or ""),
            )
        )
    return turns


def trim_clip(start: float, end: float, *, trim: float = AUDIT_EDGE_TRIM_SEC) -> tuple[float, float] | None:
    s = start + trim
    e = end - trim
    if e - s < 0.4:
        mid = (start + end) / 2.0
        half = max(0.2, (end - start) / 2.0 - 0.05)
        s, e = mid - half, mid + half
    if e - s < 0.3:
        return None
    return s, e


def stage_audio_for_docker(audio: Path) -> Path:
    """Контейнер skkkk-gigaam видит только asr/work → /work."""
    return stage_gigaam_docker_input(audio)


def _embed_clips(audio: Path, clips: dict[str, dict]) -> dict[str, dict]:
    from match_sortformer_speakers import docker_embed, wait_gigaam  # noqa: PLC0415

    staged = stage_audio_for_docker(audio)
    wait_gigaam()
    return docker_embed(staged, clips)


def audit_turns(
    turns: list[Turn],
    embeddings: dict[int, list[float]],
    *,
    auto_hit: float = AUDIT_AUTO_HIT,
    margin: float = AUDIT_MARGIN,
    min_turn_sec: float = AUDIT_MIN_TURN_SEC,
    centroid_min_sec: float = AUDIT_CENTROID_MIN_SEC,
) -> list[TurnAuditDecision]:
    speakers = sorted({t.speaker for t in turns})
    labeled: list[tuple[int, str, list[float]]] = []
    for i, turn in enumerate(turns):
        emb = embeddings.get(i)
        if emb is None:
            continue
        if turn.duration < centroid_min_sec and turn.duration < min_turn_sec:
            continue
        if turn.duration >= centroid_min_sec or turn.duration >= min_turn_sec:
            labeled.append((i, turn.speaker, emb))

    # центроиды только из достаточно длинных реплик
    centroid_pool = [(i, sp, emb) for i, sp, emb in labeled if turns[i].duration >= centroid_min_sec]
    if len({sp for _, sp, _ in centroid_pool}) < 2:
        return [
            TurnAuditDecision(
                turn_index=i,
                start=t.start,
                end=t.end,
                current_speaker=t.speaker,
                best_speaker=None,
                score_current=None,
                score_best=None,
                action="skip",
                reason="need_two_speaker_centroids",
            )
            for i, t in enumerate(turns)
        ]

    centroids_by_turn = build_leave_one_out_centroids(centroid_pool, speakers=speakers)
    # для коротких реплик без собственного пула — общие центроиды (все длинные)
    global_centroids = build_leave_one_out_centroids(centroid_pool, speakers=speakers)
    # любой индекс из пула даёт одинаковый набор, если exclude чужой; возьмём среднее «всех»
    fallback = next(iter(global_centroids.values()), {})

    decisions: list[TurnAuditDecision] = []
    for i, turn in enumerate(turns):
        base = TurnAuditDecision(
            turn_index=i,
            start=turn.start,
            end=turn.end,
            current_speaker=turn.speaker,
            best_speaker=None,
            score_current=None,
            score_best=None,
            action="skip",
            reason="too_short",
        )
        if turn.duration < min_turn_sec:
            decisions.append(base)
            continue
        emb = embeddings.get(i)
        if emb is None:
            base.reason = "no_embedding"
            decisions.append(base)
            continue
        centroids = centroids_by_turn.get(i) or fallback
        if len(centroids) < 2:
            base.reason = "need_two_speaker_centroids"
            decisions.append(base)
            continue
        scores = score_against_centroids(emb, centroids)
        d = decide_turn_reassign(turn.speaker, scores, auto_hit=auto_hit, margin=margin)
        d.turn_index = i
        d.start = turn.start
        d.end = turn.end
        decisions.append(d)
    return decisions


def apply_decisions(turns: list[Turn], decisions: list[TurnAuditDecision]) -> tuple[list[Turn], int]:
    by_idx = {d.turn_index: d for d in decisions}
    out: list[Turn] = []
    changed = 0
    for i, turn in enumerate(turns):
        d = by_idx.get(i)
        if d and d.action == "auto_reassign" and d.new_speaker:
            out.append(Turn(start=turn.start, end=turn.end, speaker=d.new_speaker, text=turn.text))
            changed += 1
        else:
            out.append(turn)
    return merge_adjacent(out), changed


def write_transcript_outputs(out_dir: Path, turns: list[Turn], *, mapping: dict | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "docker_gigaam_speakers.json").write_text(
        json.dumps(turns_to_json(turns, mapping), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "docker_gigaam_diar_smooth.txt").write_text(format_turns_txt(turns), encoding="utf-8")
    (out_dir / "docker_gigaam_diar_smooth_timecodes.txt").write_text(
        format_turns_timecodes(turns), encoding="utf-8"
    )
    # finalize / индекс читают merged_* — держим в синхроне со спикерной нарезкой
    (out_dir / "docker_merged.txt").write_text(format_turns_txt(turns), encoding="utf-8")
    (out_dir / "docker_merged_timecodes.txt").write_text(format_turns_timecodes(turns), encoding="utf-8")


def refresh_transcript_html(out_dir: Path, turns: list[Turn], *, mapping: dict | None = None) -> None:
    """Обновить transcript.html без пересборки диаризации (не затирать audit)."""
    from paths import default_audio_url  # noqa: PLC0415
    from speaker_labels import load_match_payload, speaker_registry_rows  # noqa: PLC0415
    from transcript_view import load_call_summary_files, write_transcript_html  # noqa: PLC0415
    import os

    match_path = os.environ.get("SKKKK_MATCH_JSON", "").strip()
    match_payload = load_match_payload(Path(match_path)) if match_path else None
    aid = os.environ.get("SKKKK_ARTIFACT_ID", "").strip() or None
    pid_raw = os.environ.get("SKKKK_PROJECT_ID", "").strip()
    pid = int(pid_raw) if pid_raw.isdigit() else None
    summary_md, brief_md = load_call_summary_files(out_dir)
    registry = speaker_registry_rows(match_payload) if match_payload else None
    write_transcript_html(
        out_dir / "transcript.html",
        title="Созвон — склейка трёх ASR (voice audit)",
        audio_url=default_audio_url(),
        turns=turns,
        mapping=mapping or {},
        summary_md=summary_md,
        brief_md=brief_md,
        artifact_id=aid,
        project_id=pid,
        registry=registry,
    )


def run(
    *,
    speakers_json: Path,
    audio: Path,
    out_dir: Path | None = None,
    apply: bool = True,
    auto_hit: float = AUDIT_AUTO_HIT,
    margin: float = AUDIT_MARGIN,
    embeddings_override: dict[int, list[float]] | None = None,
) -> dict:
    out_dir = out_dir or speakers_json.parent
    turns = load_turns(speakers_json)
    if len({t.speaker for t in turns}) < 2:
        report = {
            "ok": True,
            "skipped": True,
            "reason": "fewer_than_two_speakers",
            "auto_n": 0,
            "decisions": [],
        }
        (out_dir / "voice_audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report

    clips: dict[str, dict] = {}
    index_by_key: dict[str, int] = {}
    for i, turn in enumerate(turns):
        if turn.duration < AUDIT_MIN_TURN_SEC:
            continue
        clipped = trim_clip(turn.start, turn.end)
        if clipped is None:
            continue
        key = f"turn__{i}"
        clips[key] = {"start": clipped[0], "end": clipped[1]}
        index_by_key[key] = i

    if embeddings_override is not None:
        embeddings = dict(embeddings_override)
    else:
        if not clips:
            report = {
                "ok": True,
                "skipped": True,
                "reason": "no_auditable_turns",
                "auto_n": 0,
                "decisions": [],
            }
            (out_dir / "voice_audit.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return report
        if not audio.is_file():
            raise FileNotFoundError(f"нет аудио {audio}")
        embedded = _embed_clips(audio, clips)
        embeddings = {}
        for key, row in embedded.items():
            idx = index_by_key.get(key)
            if idx is None:
                continue
            emb = row.get("embedding")
            if isinstance(emb, list) and emb:
                embeddings[idx] = emb

    decisions = audit_turns(
        turns,
        embeddings,
        auto_hit=auto_hit,
        margin=margin,
    )
    auto_n = sum(1 for d in decisions if d.action == "auto_reassign")
    mapping = None
    if speakers_json.is_file():
        try:
            mapping = json.loads(speakers_json.read_text(encoding="utf-8")).get("mapping")
        except json.JSONDecodeError:
            mapping = None

    patched = turns
    if apply and auto_n:
        backup = out_dir / "docker_gigaam_speakers.before_audit.json"
        if not backup.is_file():
            shutil.copy2(speakers_json, backup)
        patched, auto_n = apply_decisions(turns, decisions)
        write_transcript_outputs(out_dir, patched, mapping=mapping)
        try:
            refresh_transcript_html(out_dir, patched, mapping=mapping)
        except Exception as exc:  # noqa: BLE001
            # HTML не должен валить audit текстовых файлов
            report_html_error = str(exc)
        else:
            report_html_error = None
    else:
        report_html_error = None

    report = {
        "ok": True,
        "skipped": False,
        "apply": apply,
        "auto_hit": auto_hit,
        "margin": margin,
        "turns": len(turns),
        "embedded": len(embeddings),
        "auto_n": auto_n,
        "keep_n": sum(1 for d in decisions if d.action == "keep"),
        "skip_n": sum(1 for d in decisions if d.action == "skip"),
        "speakers_before": sorted({t.speaker for t in turns}),
        "speakers_after": sorted({t.speaker for t in patched}),
        "html_error": report_html_error,
        "decisions": [asdict(d) for d in decisions if d.action != "keep" or d.reason != "matches_label"],
    }
    (out_dir / "voice_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice turn audit (WeSpeaker)")
    parser.add_argument("--speakers", type=Path, default=DEFAULT_SPEAKERS)
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="только отчёт, без правки файлов")
    parser.add_argument("--auto-hit", type=float, default=AUDIT_AUTO_HIT)
    parser.add_argument("--margin", type=float, default=AUDIT_MARGIN)
    args = parser.parse_args()
    if not args.speakers.is_file():
        raise SystemExit(f"нет {args.speakers}")
    report = run(
        speakers_json=args.speakers,
        audio=args.audio,
        out_dir=args.out_dir,
        apply=not args.dry_run,
        auto_hit=args.auto_hit,
        margin=args.margin,
    )
    print(json.dumps({k: report[k] for k in ("ok", "skipped", "auto_n", "keep_n", "skip_n") if k in report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
