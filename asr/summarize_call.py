"""Итоговая выжимка созвона: только из текста финальной стенограммы, без аудио/видео.

Нужен `docker_gigaam_speakers.json` с подписанными спикерами — запускать после
match + voice audit + индекса в sqlite-vec (воркер: `run_knowledge_pipeline`).
Длинные созвоны режутся на фрагменты ~6000 символов по границам реплик (map),
черновые заметки по фрагментам сшиваются в одну выжимку (reduce). Короткие
созвоны (один фрагмент) идут в reduce напрямую, map пропускается.

Требует загруженную в LM Studio Gemma (фаза LLM, ASR-контейнеры не должны
работать одновременно, см. .cursor/skills/gpu-orchestrator).

  python asr/summarize_call.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from speakers import Turn, format_turns_txt  # noqa: E402

from paths import OUT  # noqa: E402
from llm_client import chat, ensure_llm_ready, llm_name  # noqa: E402

PROMPT = ROOT / "docs" / "prompts" / "call_summary.md"
CHUNK_CHARS = 6000


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_prompts() -> tuple[str, str, str, str]:
    """docs/prompts/call_summary.md: 4 кодовых блока по порядку —
    map-система, map-пользователь, reduce-система, reduce-пользователь."""
    blocks = _read(PROMPT).split("```")
    if len(blocks) < 8:
        raise SystemExit(f"{PROMPT}: ожидалось 4 кодовых блока, найдено {len(blocks) // 2}")
    return tuple(blocks[i].strip() for i in (1, 3, 5, 7))  # type: ignore[return-value]


def _load_turns() -> list[Turn]:
    path = OUT / "docker_gigaam_speakers.json"
    if not path.is_file():
        raise SystemExit(f"нет {path}: сначала python asr/build_transcript_view.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        Turn(start=t["start"], end=t["end"], speaker=t["speaker"], text=t["text"])
        for t in payload.get("turns", [])
    ]


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


def main() -> int:
    map_sys, map_user_tmpl, reduce_sys, reduce_user_tmpl = _parse_prompts()
    turns = [t for t in _load_turns() if t.text.strip()]
    if not turns:
        raise SystemExit("нет реплик с текстом — нечего суммировать")

    try:
        ensure_llm_ready()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    llm = llm_name()
    print(f"summary LLM={llm}", flush=True)
    chunks = _chunk_text(turns, CHUNK_CHARS)
    print(f"chunks {len(chunks)}", flush=True)
    if len(chunks) == 1:
        drafts = chunks
    else:
        drafts = []
        for i, chunk in enumerate(chunks):
            user = map_user_tmpl.format(i=i + 1, n=len(chunks), chunk_text=chunk)
            print(f"map {i + 1}/{len(chunks)}...", flush=True)
            drafts.append(_chat(map_sys, user, max_tokens=800))

    reduce_user = reduce_user_tmpl.format(drafts="\n\n---\n\n".join(drafts))
    print("reduce...", flush=True)
    summary = _chat(reduce_sys, reduce_user, max_tokens=1500)
    if not summary.strip():
        raise SystemExit(f"{llm} вернула пустую выжимку")

    try:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.app.services.plain_text import clean_summary_output

        summary = clean_summary_output(summary)
    except Exception:
        pass

    out_path = OUT / "call_summary.md"
    out_path.write_text(summary.strip() + "\n", encoding="utf-8")
    print("wrote", out_path, "chars", len(summary))

    from build_transcript_view import build  # noqa: E402

    map_path = OUT / "speaker_map.json"
    result = build(map_path if map_path.is_file() else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
