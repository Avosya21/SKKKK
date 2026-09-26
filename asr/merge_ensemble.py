"""Склейка трёх ASR через Gemma по окнам 120–180 с.

Текст уже есть в asr/out. Спикеров Gemma не ставит — их вешает PyAnnote после склейки.

  python asr/merge_ensemble.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from hallucination_guard import (  # noqa: E402
    cap_text_to_speech_budget,
    looks_like_loop,
    pick_fallback_engine_text,
    prepare_engine_texts,
    sanitize_transcript_turns,
    scrub_engine_text,
    scrub_merged_text,
    window_speech_sec,
)
from silence_energy import effective_speech_sec, resolve_speech_budget_sec  # noqa: E402
from speakers import (  # noqa: E402
    Turn,
    apply_merged_windows,
    format_turns_timecodes,
    format_turns_txt,
    load_pyannote_segments,
    parse_timecode_lines,
    parse_whisper_segments,
    smooth_islands,
    turns_to_json,
)
from transcript_view import write_transcript_html  # noqa: E402

from paths import OUT, WORK, default_audio_url  # noqa: E402
from llm_client import chat, ensure_llm_ready, llm_name  # noqa: E402

PROMPT = ROOT / "docs" / "prompts" / "merge_transcript.md"
MIN_WIN = 120.0
MAX_WIN = 180.0
TARGET = 150.0


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_prompt() -> tuple[str, str, str]:
    text = _read(PROMPT)
    sys_msg = text.split("```", 2)[1].strip()
    user_tmpl = text.split("```", 4)[3].strip()
    repair_tmpl = (
        "Предыдущая стенограмма этого окна слишком короткая: пропущены фразы из ASR.\n"
        "Сохрани уже написанный текст и ДОБАВЬ недостающие фразы из вариантов ниже.\n"
        "Не пересказывай и не сокращай.\n\n"
        "Уже собрано:\n{partial}\n\n"
        "Вариант 1 (Whisper):\n{whisper_text}\n\n"
        "Вариант 2 (T-one):\n{tone_text}\n\n"
        "Вариант 3 (GigaAM):\n{gigaam_text}"
    )
    return sys_msg, user_tmpl, repair_tmpl


def pack_windows(turns: list[Turn], duration: float) -> list[tuple[float, float]]:
    """Окна 120–180 с по границам реплик (тишина / смена говорящего), не по ровной сетке часов."""
    if duration <= 0:
        return []
    ordered = sorted(turns, key=lambda t: (t.start, t.end)) if turns else []
    if not ordered:
        t = 0.0
        out: list[tuple[float, float]] = []
        while t < duration - 1:
            nxt = min(duration, t + TARGET)
            out.append((t, nxt))
            t = nxt
        return out

    windows: list[tuple[float, float]] = []
    w_start = 0.0
    w_end = ordered[0].end
    for turn in ordered[1:]:
        candidate = turn.end
        span = candidate - w_start
        current = w_end - w_start
        if span <= MAX_WIN and not (current >= TARGET and span > TARGET):
            w_end = candidate
            continue
        windows.append((w_start, w_end))
        w_start = turn.start
        w_end = turn.end
    windows.append((w_start, max(w_end, duration)))
    if len(windows) >= 2:
        prev, last = windows[-2], windows[-1]
        if (last[1] - last[0]) < 90 and (last[1] - prev[0]) <= MAX_WIN:
            windows[-2] = (prev[0], last[1])
            windows.pop()
    windows[0] = (0.0, windows[0][1])
    windows[-1] = (windows[-1][0], max(windows[-1][1], duration))
    return windows


def _in_window(segments: list[Turn], start: float, end: float) -> str:
    parts = [t.text for t in segments if t.end > start and t.start < end]
    return " ".join(p for p in parts if p).strip()


def _looks_tripled(text: str) -> bool:
    """Совместимость со старыми тестами; канон — looks_like_loop."""
    return looks_like_loop(text)


def _load_segments(path: Path) -> list[Turn]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return parse_whisper_segments(payload)


def _whisper_segments() -> list[Turn]:
    """CUDA Whisper на целом файле может петлить — тогда берём чанковый sherpa."""
    chunked = _load_segments(OUT / "input_raw_whisper.json")
    docker = _load_segments(OUT / "docker_whisper.json")
    docker_text = " ".join(t.text for t in docker)
    if docker and not _looks_tripled(docker_text):
        return docker
    if docker and _looks_tripled(docker_text):
        print(
            "whisper REJECT loop in docker_whisper.json — используем chunked/raw или без Whisper",
            flush=True,
        )
    return chunked


def _tone_segments() -> list[Turn]:
    segs = _load_segments(OUT / "docker_tone.json")
    if segs:
        return segs
    return _load_segments(OUT / "input_raw_tone.json")


def _engine_span(segs: list[Turn]) -> float:
    if not segs:
        return 0.0
    return max(t.end for t in segs)


from clock_guard import fallback_budget_sec  # noqa: E402
from engine_timeline import tone_for_align  # noqa: E402
from merge_guard import finalize_window_text, recover_failed_audit  # noqa: E402
from transcript_integrity import audit_and_repair_merge_plain, write_transcript_integrity  # noqa: E402

MAX_MERGE_ATTEMPTS = 2


def _chat(system: str, user: str) -> tuple[str, dict]:
    return chat(system, user, max_tokens=2048, temperature=0.1, timeout=180)


def _gigaam_turns() -> list:
    diar = OUT / "docker_gigaam_diar_timecodes.txt"
    if diar.is_file():
        turns = parse_timecode_lines(_read(diar))
        if turns:
            return smooth_islands(turns)
    raw = OUT / "docker_gigaam.json"
    if raw.is_file():
        payload = json.loads(raw.read_text(encoding="utf-8"))
        segs = parse_whisper_segments(payload)
        timed = payload.get("transcription_with_timecodes") or ""
        if not segs and timed:
            segs = parse_timecode_lines(timed)
        if segs:
            return segs
    return []


def _wav_duration() -> float:
    import wave

    wav = WORK / "input_16k.wav"
    if not wav.is_file():
        return 0.0
    with wave.open(str(wav), "rb") as fh:
        return fh.getnframes() / float(fh.getframerate())


def _speech_timeline_turns(gigaam_turns: list[Turn]) -> list[Turn]:
    """Интервалы «где говорят» для бюджета: Sortformer/PyAnnote, не 18 с чанки GigaAM."""
    sortformer = OUT / "sortformer_segments.json"
    if sortformer.is_file():
        try:
            from build_sortformer_transcript_view import load_sortformer_segments

            turns = load_sortformer_segments(sortformer)
            if turns:
                return turns
        except Exception as exc:  # noqa: BLE001
            print(f"sortformer timeline fallback: {exc}", flush=True)
    pyannote = OUT / "pyannote_segments.json"
    if pyannote.is_file():
        turns = load_pyannote_segments(pyannote)
        if turns:
            return turns
    return gigaam_turns


def main() -> int:
    system, user_tmpl, repair_tmpl = _parse_prompt()
    gigaam_turns = _gigaam_turns()
    duration = max((t.end for t in gigaam_turns), default=0.0) or _wav_duration()
    whisper_segs = _whisper_segments()
    tone_segs = tone_for_align(_tone_segments(), whisper_segs, gigaam_turns, duration)
    speech_turns = _speech_timeline_turns(gigaam_turns)

    try:
        ensure_llm_ready()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    llm = llm_name()
    print(f"merge LLM={llm}", flush=True)
    windows = pack_windows(gigaam_turns, duration)
    wav16 = WORK / "input_16k.wav"
    print(
        f"windows {len(windows)} duration {duration:.0f}s speech_timeline={len(speech_turns)}",
        flush=True,
    )
    merged_parts: list[str] = []
    window_payload: list[tuple[float, float, str]] = []
    log: list[dict] = []
    for i, (start, end) in enumerate(windows):
        giga_raw = _in_window(gigaam_turns, start, end)
        whis_raw = _in_window(whisper_segs, start, end)
        tone_raw = _in_window(tone_segs, start, end)
        diar_sec = window_speech_sec(speech_turns, start, end)
        energy_sec = effective_speech_sec(diar_sec, wav16 if wav16.is_file() else None, start, end)
        alive_n = sum(
            1
            for raw in (whis_raw, tone_raw, giga_raw)
            if scrub_engine_text(raw)
        )
        speech_sec = resolve_speech_budget_sec(
            diar_sec=diar_sec,
            energy_aware_sec=energy_sec,
            engine_alive=alive_n,
        )
        whis, tone, giga, skip_reason = prepare_engine_texts(
            whis_raw,
            tone_raw,
            giga_raw,
            speech_sec=speech_sec,
            window_span=end - start,
        )
        if skip_reason:
            fallback = pick_fallback_engine_text(
                whis_raw, tone_raw, giga_raw, speech_sec=speech_sec, window_span=end - start
            )
            if fallback:
                merged_parts.append(fallback)
                window_payload.append((start, end, fallback))
                log.append(
                    {
                        "window": i,
                        "start": start,
                        "end": end,
                        "fallback": "engine_after_skip",
                        "skipped_gemma": skip_reason,
                        "chars": len(fallback),
                        "text": fallback,
                        "speech_sec": round(speech_sec, 2),
                        "energy_sec": round(energy_sec, 2),
                        "diar_speech_sec": round(diar_sec, 2),
                        "src_chars_raw": {
                            "whisper": len(whis_raw),
                            "tone": len(tone_raw),
                            "gigaam": len(giga_raw),
                        },
                    }
                )
                print(
                    f"window {i} {start:.0f}-{end:.0f}s engine_fallback "
                    f"after {skip_reason} chars={len(fallback)}",
                    flush=True,
                )
                continue
            log.append(
                {
                    "window": i,
                    "start": start,
                    "end": end,
                    "skipped": skip_reason,
                    "speech_sec": round(speech_sec, 2),
                    "energy_sec": round(energy_sec, 2),
                    "diar_speech_sec": round(diar_sec, 2),
                    "src_chars_raw": {
                        "whisper": len(whis_raw),
                        "tone": len(tone_raw),
                        "gigaam": len(giga_raw),
                    },
                }
            )
            print(
                f"window {i} {start:.0f}-{end:.0f}s skip {skip_reason} "
                f"speech={speech_sec:.1f}s energy={energy_sec:.1f}s diar={diar_sec:.1f}s",
                flush=True,
            )
            continue
        print(f"window {i} {start:.0f}-{end:.0f}s sending ({llm})...", flush=True)
        t0 = time.time()
        text = ""
        usage: dict = {}
        merge_meta: dict = {}
        for attempt in range(MAX_MERGE_ATTEMPTS):
            if attempt == 0:
                user = user_tmpl.format(
                    whisper_text=whis or "(вариант недоступен)",
                    tone_text=tone or "(вариант недоступен)",
                    gigaam_text=giga or "(вариант недоступен)",
                )
            else:
                user = repair_tmpl.format(
                    partial=text or "(пусто)",
                    whisper_text=whis or "(вариант недоступен)",
                    tone_text=tone or "(вариант недоступен)",
                    gigaam_text=giga or "(вариант недоступен)",
                )
                print(
                    f"window {i} retry {attempt} "
                    f"recall={merge_meta.get('audit_after', {}).get('word_recall')}",
                    flush=True,
                )
            text, usage = _chat(system, user)
            text, merge_meta = finalize_window_text(
                text,
                whis,
                tone,
                giga,
                speech_sec=speech_sec,
                window_span=end - start,
                alive_n=alive_n,
            )
            if merge_meta.get("audit_after", {}).get("ok"):
                break
        if not merge_meta.get("audit_after", {}).get("ok"):
            text, merge_meta = recover_failed_audit(
                text,
                whis,
                tone,
                giga,
                whisper_raw=whis_raw,
                tone_raw=tone_raw,
                gigaam_raw=giga_raw,
                speech_sec=speech_sec,
                window_span=end - start,
                alive_n=alive_n,
                merge_meta=merge_meta,
            )
            if merge_meta.get("recovery") == "engine_fallback":
                print(
                    f"window {i} engine_fallback after audit_fail "
                    f"recall={merge_meta.get('audit_after', {}).get('word_recall')} "
                    f"chars={merge_meta.get('fallback_chars')}",
                    flush=True,
                )
            elif merge_meta.get("recovery") not in ("none", "supplement_recovered"):
                print(
                    f"window {i} audit recovery={merge_meta.get('recovery')} "
                    f"recall={merge_meta.get('audit_after', {}).get('word_recall')}",
                    flush=True,
                )
        budget_sec = float(merge_meta.get("budget_sec") or speech_sec)
        if not text or looks_like_loop(text):
            fallback = pick_fallback_engine_text(
                whis, tone, giga, speech_sec=budget_sec, window_span=end - start
            )
            if fallback:
                text = fallback
                log.append(
                    {
                        "window": i,
                        "start": start,
                        "end": end,
                        "fallback": "engine_after_gemma_reject",
                        "skipped_gemma": "merge_over_budget_or_loop",
                        "chars": len(text),
                        "text": text,
                        "speech_sec": round(speech_sec, 2),
                        "elapsed_sec": round(time.time() - t0, 1),
                        **usage,
                    }
                )
                merged_parts.append(text)
                window_payload.append((start, end, text))
                print(
                    f"window {i} {start:.0f}-{end:.0f}s engine_fallback "
                    f"after merge_reject chars={len(text)}",
                    flush=True,
                )
                continue
            log.append(
                {
                    "window": i,
                    "start": start,
                    "end": end,
                    "skipped": "merge_over_budget_or_loop",
                    "speech_sec": round(speech_sec, 2),
                    "elapsed_sec": round(time.time() - t0, 1),
                    **usage,
                }
            )
            print(f"window {i} {start:.0f}-{end:.0f}s skip merge_over_budget_or_loop", flush=True)
            continue
        merged_parts.append(text)
        window_payload.append((start, end, text))
        log.append(
            {
                "window": i,
                "start": start,
                "end": end,
                "elapsed_sec": round(time.time() - t0, 1),
                "chars": len(text),
                "text": text,
                "speech_sec": round(speech_sec, 2),
                "energy_sec": round(energy_sec, 2),
                "diar_speech_sec": round(diar_sec, 2),
                "src_chars": {"whisper": len(whis), "tone": len(tone), "gigaam": len(giga)},
                "src_chars_raw": {
                    "whisper": len(whis_raw),
                    "tone": len(tone_raw),
                    "gigaam": len(giga_raw),
                },
                "merge_audit": merge_meta,
                **usage,
            }
        )
        print(
            f"window {i} {start:.0f}-{end:.0f}s chars={len(text)} speech={speech_sec:.1f}s "
            f"energy={energy_sec:.1f}s diar={diar_sec:.1f}s in {log[-1]['elapsed_sec']}s "
            f"audit={merge_meta.get('action')} recall={merge_meta.get('audit_after', {}).get('word_recall')}",
            flush=True,
        )

    labeled = sanitize_transcript_turns(apply_merged_windows(gigaam_turns, window_payload))
    plain = "\n\n".join(merged_parts).strip() + "\n"
    ref_chars = sum(
        len((e.get("text") or ""))
        for e in log
        if e.get("text") and not e.get("skipped")
    )
    audit_lines = [
        f"windows_total={len(windows)} ok={sum(1 for e in log if e.get('text') and not e.get('skipped'))}",
        f"plain_chars={len(plain.strip())} gemma_windows_chars={ref_chars}",
    ]
    recovered = 0
    still_fail = 0
    for e in log:
        if e.get("skipped") or not e.get("merge_audit"):
            continue
        ma = e["merge_audit"]
        recovery = ma.get("recovery") or "none"
        if recovery in ("engine_fallback", "supplement_recovered"):
            recovered += 1
            audit_lines.append(
                f"W{e.get('window')} RECOVERED recovery={recovery} "
                f"action={ma.get('action')} recall={ma.get('audit_after', {}).get('word_recall')}"
            )
            continue
        aa = ma.get("audit_after") or {}
        if aa.get("ok"):
            continue
        still_fail += 1
        audit_lines.append(
            f"W{e.get('window')} STILL_FAIL {e.get('start')}-{e.get('end')} "
            f"recovery={recovery} action={ma.get('action')} recall={aa.get('word_recall')} "
            f"ratio={aa.get('char_ratio')} reason={aa.get('reason')}"
        )
    status = "ok" if still_fail == 0 else "degraded"
    audit_lines.insert(2, f"merge_status={status} recovered={recovered} still_fail={still_fail}")
    (OUT / "merge_loss_audit.txt").write_text("\n".join(audit_lines) + "\n", encoding="utf-8")
    checklist = OUT / "pipeline_checklist.json"
    if checklist.is_file():
        try:
            payload = json.loads(checklist.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        payload["merge_audit"] = {
            "status": status,
            "recovered": recovered,
            "still_fail": still_fail,
        }
        checklist.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    giga_plain = _read(OUT / "docker_gigaam.txt") if (OUT / "docker_gigaam.txt").is_file() else ""
    tone_plain = _read(OUT / "docker_tone.txt") if (OUT / "docker_tone.txt").is_file() else ""
    whisper_plain = _read(OUT / "docker_whisper.txt") if (OUT / "docker_whisper.txt").is_file() else ""
    plain, merge_integrity = audit_and_repair_merge_plain(
        plain,
        giga_plain=giga_plain,
        tone_plain=tone_plain,
        whisper_plain=whisper_plain,
        log=log,
        duration=duration,
    )
    if merge_integrity.get("repair", {}).get("added_chars", 0) > 0:
        print(
            f"integrity global_backfill +{merge_integrity['repair']['added_chars']} chars "
            f"status={merge_integrity.get('status')}",
            flush=True,
        )
    integrity_payload = write_transcript_integrity(OUT, merge_audit=merge_integrity)
    if checklist.is_file():
        try:
            payload = json.loads(checklist.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        payload["transcript_integrity"] = {
            "status": integrity_payload.get("status"),
            "merge": merge_integrity.get("status"),
        }
        checklist.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "docker_merged_plain.txt").write_text(plain, encoding="utf-8")
    (OUT / "docker_merged_windows.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "docker_merged.txt").write_text(format_turns_txt(labeled), encoding="utf-8")
    (OUT / "docker_merged_timecodes.txt").write_text(format_turns_timecodes(labeled), encoding="utf-8")
    (OUT / "docker_merged_speakers.json").write_text(
        json.dumps(turns_to_json(labeled), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_transcript_html(
        OUT / "transcript.html",
        title="Созвон — склейка Whisper + T-one + GigaAM, спикеры PyAnnote",
        audio_url=default_audio_url(),
        turns=labeled,
        mapping={},
    )
    print("wrote", OUT / "docker_merged.txt", "windows", len(log), "turns", len(labeled), "chars", len(plain), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
