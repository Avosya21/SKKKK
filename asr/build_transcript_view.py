"""Собрать реплики: Sortformer + Gemma + (опционально) база голосов → HTML.

  python asr/build_transcript_view.py
  python asr/build_transcript_view.py --serve
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from build_sortformer_transcript_view import (  # noqa: E402
    _gigaam_text_turns,
    _merged_windows,
    _read,
    load_sortformer_segments,
)
from hallucination_guard import sanitize_transcript_turns  # noqa: E402
from paths import OUT, default_audio_url  # noqa: E402
from speaker_labels import (  # noqa: E402
    apply_match_labels,
    diar_turns_from_payload,
    load_match_payload,
    speaker_registry_rows,
)
from speakers import (  # noqa: E402
    Turn,
    apply_map,
    apply_merged_windows,
    carve_text_by_speakers,
    format_turns_timecodes,
    format_turns_txt,
    load_mapping,
    load_pyannote_segments,
    merge_adjacent,
    overlay_text,
    parse_timecode_lines,
    parse_whisper_segments,
    prepare_speaker_timeline,
    reattach_cut_letters,
    renumber_speakers,
    smooth_islands,
    talk_time,
    turns_to_json,
)
from clock_guard import assess_whisper_health, pick_clock_timed  # noqa: E402
from engine_timeline import tone_for_align  # noqa: E402
from timing_align import (  # noqa: E402
    align_merged_to_timed,
    parse_gigaam_timecoded,
    parse_gigaam_timecoded_payload,
    parse_whisper_words,
    timed_asr_turns,
)
from transcript_integrity import audit_view_turns, write_transcript_integrity  # noqa: E402
from transcript_view import load_call_summary_files, write_transcript_html  # noqa: E402


def _match_json_path() -> Path | None:
    custom = os.environ.get("PRIMARY_ANALITICS_MATCH_JSON", "").strip()
    if custom:
        return Path(custom)
    sibling = OUT.parent / "reports" / "sortformer_speaker_match.json"
    if sibling.is_file():
        return sibling
    return None


def _artifact_meta() -> tuple[str | None, int | None]:
    aid = os.environ.get("PRIMARY_ANALITICS_ARTIFACT_ID", "").strip() or None
    pid_raw = os.environ.get("PRIMARY_ANALITICS_PROJECT_ID", "").strip()
    pid = int(pid_raw) if pid_raw.isdigit() else None
    return aid, pid


def _load_whisper_payload() -> dict | None:
    path = OUT / "docker_whisper.json"
    if not path.is_file():
        path = OUT / "input_raw_whisper.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_tone_timed() -> list[Turn]:
    path = OUT / "docker_tone.json"
    if not path.is_file():
        path = OUT / "input_raw_tone.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return parse_whisper_segments(payload)


def _load_gigaam_clock_timed() -> list[Turn]:
    path = OUT / "docker_gigaam.json"
    if path.is_file():
        turns = parse_gigaam_timecoded_payload(json.loads(path.read_text(encoding="utf-8")))
        if turns:
            return turns
    diar = OUT / "docker_gigaam_diar_timecodes.txt"
    if diar.is_file():
        return parse_gigaam_timecoded(diar.read_text(encoding="utf-8"))
    return []


def _master_duration_sec() -> float:
    meta_path = OUT.parent / "audio" / "input_meta.json"
    if meta_path.is_file():
        try:
            return float(json.loads(meta_path.read_text(encoding="utf-8")).get("duration_sec") or 0.0)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return 0.0


def build(mapping_path: Path | None = None) -> dict:
    mapping = load_mapping(mapping_path)
    match_path = _match_json_path()
    match_payload = load_match_payload(match_path) if match_path else None
    artifact_id, project_id = _artifact_meta()

    sortformer_path = OUT / "sortformer_segments.json"
    pyannote_path = OUT / "pyannote_segments.json"
    windows = _merged_windows(OUT)
    whisper_payload = _load_whisper_payload()
    timed, whisper_gaps = timed_asr_turns(whisper_payload) if whisper_payload else ([], [])
    tone_raw = _load_tone_timed()
    gigaam_clock = _load_gigaam_clock_timed()
    duration = _master_duration_sec() or max(
        (t.end for t in (*timed, *gigaam_clock)), default=0.0
    )
    tone_timed = (
        tone_for_align(tone_raw, timed, gigaam_clock, duration, log=False) if tone_raw else []
    )
    clock_timed, clock_source = pick_clock_timed(tone_timed, gigaam_clock, duration=duration)
    whisper_health = assess_whisper_health(timed, whisper_gaps, duration)

    speaker_from_match = diar_turns_from_payload(match_payload)
    speaker_turns: list[Turn] = []
    if speaker_from_match or sortformer_path.is_file():
        if speaker_from_match:
            speaker_turns = speaker_from_match
        else:
            raw = load_sortformer_segments(sortformer_path)
            speaker_turns = merge_adjacent(raw, gap_sec=0.25)
            speaker_turns = prepare_speaker_timeline(speaker_turns, min_seconds=15.0)
        title = "Созвон — склейка трёх ASR, нарезка по голосам Sortformer"
    elif pyannote_path.is_file():
        speaker_turns = load_pyannote_segments(pyannote_path)
        speaker_turns = prepare_speaker_timeline(speaker_turns, min_seconds=20.0)
        speaker_turns = renumber_speakers(speaker_turns)
        title = "Созвон — склейка трёх ASR, нарезка по голосам PyAnnote"
    else:
        title = "Созвон — склейка трёх ASR (без диаризации)"

    if timed and speaker_turns and windows:
        words = parse_whisper_words(whisper_payload) if whisper_payload else None
        turns = align_merged_to_timed(
            timed,
            windows,
            speaker_turns,
            fallback_timed=clock_timed or None,
            extra_gaps=whisper_gaps or None,
            words=words or None,
            whisper_trusted=whisper_health.trusted,
            audio_end=duration,
        )
        if tone_timed and whisper_health.trusted:
            source = "Local LLM text + Whisper/Tone timing + Sortformer"
        elif tone_timed:
            source = f"Local LLM text + Tone timing ({clock_source}) + Sortformer"
        elif gigaam_clock:
            source = f"Local LLM text + GigaAM timing ({clock_source}) + Sortformer"
        else:
            source = "Local LLM text + Whisper/Tone timing + Sortformer"
    elif timed and speaker_turns:
        turns = overlay_text(timed, speaker_turns)
        source = "Whisper timing + Sortformer/PyAnnote (без окон Local LLM)"
    elif speaker_turns and windows:
        # запасной путь: старое пропорциональное carve (хуже по часам) — не норма
        if sortformer_path.is_file() or speaker_from_match:
            gemma_turns = [Turn(start=s, end=e, speaker="", text=txt) for s, e, txt in windows]
            turns = carve_text_by_speakers(gemma_turns, speaker_turns)
            source = "Sortformer + склейка Local LLM (без Whisper timing)"
        else:
            turns = apply_merged_windows(speaker_turns, windows)
            source = "PyAnnote + склейка Local LLM (без Whisper timing)"
    elif speaker_turns:
        turns = carve_text_by_speakers(_gigaam_text_turns(OUT), speaker_turns)
        source = "Sortformer/PyAnnote + carve GigaAM"
    else:
        diar_tc = OUT / "docker_gigaam_diar_timecodes.txt"
        if not diar_tc.is_file():
            raise SystemExit("нет timed ASR / sortformer / pyannote / gigaam diar")
        source = "gigaam 18s chunks"
        turns = smooth_islands(parse_timecode_lines(_read(diar_tc)))

    keep_phrase_clocks = bool(timed) and "Local LLM text" in source
    join_gap = None if keep_phrase_clocks else 0.35
    turns = reattach_cut_letters(turns, join_gap_sec=join_gap)
    # densе-cut только на legacy carve; канон Gemma не урезаем
    drop_dense = "Local LLM text" not in source and "carve" in source.lower()
    turns = sanitize_transcript_turns(turns, drop_dense=drop_dense)
    turns = apply_map(turns, mapping, join_gap_sec=join_gap) if mapping else turns

    if match_payload:
        turns = apply_match_labels(turns, match_payload)
    elif sortformer_path.is_file():
        turns = renumber_speakers(turns)

    turns = [t for t in turns if t.text.strip()]

    (OUT / "docker_gigaam_diar_smooth.txt").write_text(format_turns_txt(turns), encoding="utf-8")
    (OUT / "docker_gigaam_diar_smooth_timecodes.txt").write_text(
        format_turns_timecodes(turns), encoding="utf-8"
    )
    (OUT / "docker_gigaam_speakers.json").write_text(
        json.dumps(turns_to_json(turns, mapping), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # финальные таймкоды для UI/parsed — с часами Whisper, не пропорциональный carve
    (OUT / "docker_merged_timecodes.txt").write_text(format_turns_timecodes(turns), encoding="utf-8")
    (OUT / "docker_merged.txt").write_text(format_turns_txt(turns), encoding="utf-8")
    (OUT / "docker_merged_speakers.json").write_text(
        json.dumps(turns_to_json(turns, mapping), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    overlay_note = (
        f"timing={source}; clock={clock_source}; whisper_trusted={whisper_health.trusted}; "
        f"turns={len(turns)}; whisper_phrases={len(timed)}"
    )
    if timed and speaker_turns:
        overlaid = overlay_text(timed, speaker_turns)
        (OUT / "docker_whisper_on_pyannote.txt").write_text(format_turns_txt(overlaid), encoding="utf-8")
        overlay_note += f"; whisper_raw_overlay={len(overlaid)}"

    summary_md, brief_md = load_call_summary_files(OUT)

    registry = speaker_registry_rows(match_payload) if match_payload else None
    write_transcript_html(
        OUT / "transcript.html",
        title=title,
        audio_url=default_audio_url(),
        turns=turns,
        mapping=mapping,
        summary_md=summary_md,
        brief_md=brief_md,
        artifact_id=artifact_id,
        project_id=project_id,
        registry=registry,
    )

    summary = {
        "ok": True,
        "source": source,
        "speakers": sorted({t.speaker for t in turns}),
        "speaker_count": len({t.speaker for t in turns}),
        "turns": len(turns),
        "talk_time": talk_time(turns),
        "overlay": overlay_note,
        "timing_health": whisper_health.to_dict(),
        "clock_source": clock_source,
        "html": str(OUT / "transcript.html"),
        "match_json": str(match_path) if match_path else None,
    }
    plain_path = OUT / "docker_merged_plain.txt"
    plain_for_audit = _read(plain_path) if plain_path.is_file() else ""
    view_integrity = audit_view_turns(turns, plain=plain_for_audit, duration=duration)
    merge_integrity = None
    integrity_path = OUT / "transcript_integrity.json"
    if integrity_path.is_file():
        try:
            merge_integrity = json.loads(integrity_path.read_text(encoding="utf-8")).get("merge")
        except json.JSONDecodeError:
            merge_integrity = None
    integrity_payload = write_transcript_integrity(
        OUT,
        merge_audit=merge_integrity if isinstance(merge_integrity, dict) else None,
        view_audit=view_integrity,
    )
    summary["integrity"] = integrity_payload
    summary["ok"] = integrity_payload.get("status") != "fail"
    (OUT / "transcript_view_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "timing_health.json").write_text(
        json.dumps(
            {**whisper_health.to_dict(), "clock_source": clock_source},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return summary


class _Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ASR), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


def serve(host: str, port: int) -> None:
    import os as _os

    last_error: OSError | None = None
    httpd = None
    bound = port
    for candidate in range(port, port + 5):
        try:
            httpd = ThreadingHTTPServer((host, candidate), _Handler)
            bound = candidate
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        raise SystemExit(f"не удалось занять порт {port}–{port + 4}: {last_error}")
    url = f"http://{host}:{bound}/out/transcript.html"
    print("open", url, flush=True)
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", type=Path, default=OUT / "speaker_map.json")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18104)
    args = parser.parse_args()
    summary = build(args.map if args.map.is_file() else None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.serve:
        serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
