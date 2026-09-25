"""Выжимки созвона: итоговый протокол и краткое резюме, только из текста стенограммы.

Нужен `docker_gigaam_speakers.json` с подписанными спикерами — запускать после
confirm имён и индекса в sqlite-vec (воркер: `run_knowledge_pipeline`).
Длинные созвоны режутся на фрагменты ~6000 символов по границам реплик (map),
черновые заметки сшиваются (reduce). Итог — `call_summary.md` (до 4096 tokens),
кратко — `call_brief.md` (до 1024 tokens).

Требует загруженную в LM Studio Gemma (фаза LLM, ASR-контейнеры не должны
работать одновременно, см. .cursor/skills/gpu-orchestrator) либо Cursor API.

  python asr/summarize_call.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import logging
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from speakers import Turn, format_turns_txt  # noqa: E402

from paths import OUT  # noqa: E402
from llm_client import chat, ensure_llm_ready, llm_name  # noqa: E402

logger = logging.getLogger("skkkk.call_summary")

PROMPT = ROOT / "docs" / "prompts" / "call_summary.md"
BRIEF_PROMPT = ROOT / "docs" / "prompts" / "call_brief.md"
CHUNK_CHARS = 6000
MAP_MAX_TOKENS = 1200
REDUCE_MAX_TOKENS = 4096
BRIEF_REDUCE_MAX_TOKENS = 1024


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_prompts(path: Path) -> tuple[str, str, str, str]:
    """Промпт: 4 кодовых блока по порядку —
    map-система, map-пользователь, reduce-система, reduce-пользователь."""
    blocks = _read(path).split("```")
    if len(blocks) < 8:
        raise SystemExit(f"{path}: ожидалось 4 кодовых блока, найдено {len(blocks) // 2}")
    return tuple(blocks[i].strip() for i in (1, 3, 5, 7))  # type: ignore[return-value]


def _load_turns() -> list[Turn]:
    path = OUT / "docker_gigaam_speakers.json"
    if not path.is_file():
        raise SystemExit(f"нет {path}: сначала python asr/build_transcript_view.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    turns = [
        Turn(start=t["start"], end=t["end"], speaker=t["speaker"], text=t["text"])
        for t in payload.get("turns", [])
    ]
    return _attach_video_context(turns)


def _attach_video_context(turns: list[Turn]) -> list[Turn]:
    """Add screen-share observations to the publisher's overlapping turns."""
    latest = OUT.parent / "lab" / "latest.json"
    if not latest.is_file():
        return turns
    try:
        run_id = json.loads(latest.read_text(encoding="utf-8")).get("run_id")
        path = OUT.parent / "lab" / "runs" / str(run_id) / "video_transcript.json"
        segments = json.loads(path.read_text(encoding="utf-8")).get("segments", [])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return turns
    if not isinstance(segments, list):
        return turns

    enriched: list[Turn] = []
    for turn in turns:
        notes: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") or start)
            if end < turn.start or start > turn.end:
                continue
            publisher = str(segment.get("speaker") or segment.get("identity") or "").strip()
            if publisher and publisher != turn.speaker:
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            frame_id = str(segment.get("frame_id") or "").strip()
            note = f"На экране показывалось: {text}"
            if frame_id:
                note += f" [frame_id={frame_id}]"
            if note not in notes:
                notes.append(note)
        context = "\n".join(notes)
        enriched.append(
            Turn(
                start=turn.start,
                end=turn.end,
                speaker=turn.speaker,
                text=f"{turn.text}\n{context}" if context else turn.text,
            )
        )
    logger.info("meeting.video_context.attached turns=%s segments=%s", len(turns), len(segments))
    return enriched


def lab_proof_is_canonical(asr_out: Path) -> bool:
    """Lab proof — канон реплик; build_transcript_view нарежет SPEAKER_00 поверх имён."""
    art = asr_out.resolve().parent
    latest = art / "lab" / "latest.json"
    if not latest.is_file():
        return False
    try:
        run_id = json.loads(latest.read_text(encoding="utf-8")).get("run_id")
    except (json.JSONDecodeError, OSError):
        return False
    if not run_id:
        return False
    proof = art / "lab" / "runs" / str(run_id) / "proof" / "transcript.json"
    return proof.is_file()


def _chunk_text(turns: list[Turn], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[Turn] = []
    size = 0
    for turn in turns:
        block_len = len(turn.speaker) + len(turn.text) + 4
        if current and size + block_len > max_chars:
            chunks.append(format_turns_txt(current))
            current, size = [], 0
        current.append(turn)
        size += block_len
    if current:
        chunks.append(format_turns_txt(current))
    return chunks


def _chat(system: str, user: str, *, max_tokens: int) -> str:
    text, _usage = chat(system, user, max_tokens=max_tokens, temperature=0.2, timeout=180)
    return text


def _clean_summary(text: str) -> str:
    try:
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.app.services.plain_text import clean_summary_output

        return clean_summary_output(text)
    except Exception:
        return text


def _run_map_reduce(
    turns: list[Turn],
    prompt_path: Path,
    out_path: Path,
    *,
    reduce_max_tokens: int,
    label: str,
) -> str:
    map_sys, map_user_tmpl, reduce_sys, reduce_user_tmpl = _parse_prompts(prompt_path)
    chunks = _chunk_text(turns, CHUNK_CHARS)
    print(f"{label} chunks {len(chunks)}", flush=True)
    if len(chunks) == 1:
        drafts = chunks
    else:
        drafts = []
        for i, chunk in enumerate(chunks):
            user = map_user_tmpl.format(i=i + 1, n=len(chunks), chunk_text=chunk)
            print(f"{label} map {i + 1}/{len(chunks)}...", flush=True)
            drafts.append(_chat(map_sys, user, max_tokens=MAP_MAX_TOKENS))

    reduce_user = reduce_user_tmpl.format(drafts="\n\n---\n\n".join(drafts))
    print(f"{label} reduce...", flush=True)
    summary = _chat(reduce_sys, reduce_user, max_tokens=reduce_max_tokens)
    if not summary.strip():
        raise RuntimeError(f"{llm_name()} вернула пустую выжимку ({label})")
    summary = _clean_summary(summary).strip()
    out_path.write_text(summary + "\n", encoding="utf-8")
    print("wrote", out_path, "chars", len(summary), flush=True)
    return summary


def main() -> int:
    started = time.monotonic()
    turns = [t for t in _load_turns() if t.text.strip()]
    if not turns:
        raise SystemExit("нет реплик с текстом — нечего суммировать")

    try:
        ensure_llm_ready()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"summary LLM={llm_name()}", flush=True)
    try:
        _run_map_reduce(
            turns,
            PROMPT,
            OUT / "call_summary.md",
            reduce_max_tokens=REDUCE_MAX_TOKENS,
            label="full",
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        _run_map_reduce(
            turns,
            BRIEF_PROMPT,
            OUT / "call_brief.md",
            reduce_max_tokens=BRIEF_REDUCE_MAX_TOKENS,
            label="brief",
        )
    except Exception as exc:
        print(f"brief summary skipped: {exc}", flush=True)

    if lab_proof_is_canonical(OUT):
        logger.info("meeting.call_summary.done seconds=%.2f", time.monotonic() - started)
        print("lab proof canonical — skip build_transcript_view")
        return 0

    from build_transcript_view import build  # noqa: E402

    map_path = OUT / "speaker_map.json"
    result = build(map_path if map_path.is_file() else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
