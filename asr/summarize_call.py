"""Выжимки созвона: итоговый протокол и краткое резюме, только из текста стенограммы.

Нужен `docker_gigaam_speakers.json` с подписанными спикерами — запускать после
confirm имён и индекса в sqlite-vec (воркер: `run_knowledge_pipeline`).
Длинные созвоны режутся на фрагменты ~6000 символов по границам реплик (map),
черновые заметки сшиваются (reduce). Итог — `call_summary.md` (до 4096 tokens),
кратко — `call_brief.md` (до 1024 tokens).

Для портального проекта с `internal_project` — промпты `call_summary_internal.md` /
`call_brief_internal.md` (флаг env `PRIMARY_ANALITICS_INTERNAL_PROJECT`); в reduce
подмешиваются до трёх прошлых `*_call_summary.md` того же проекта. Экран Jira
во внутренней выжимке сжимается до ссылки/ключа (и при наличии — названия и статуса),
без текста комментариев; приоритет у сказанного голосом.

Требует загруженную в LM Studio Gemma (фаза LLM, ASR-контейнеры не должны
работать одновременно, см. .cursor/skills/gpu-orchestrator) либо Cursor API.

  python asr/summarize_call.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from speakers import Turn, format_turns_txt  # noqa: E402

from paths import OUT  # noqa: E402
from llm_client import chat, ensure_llm_ready, llm_name  # noqa: E402

logger = logging.getLogger("primaryanalitics.call_summary")

PROMPT = ROOT / "docs" / "prompts" / "call_summary.md"
BRIEF_PROMPT = ROOT / "docs" / "prompts" / "call_brief.md"
INTERNAL_PROMPT = ROOT / "docs" / "prompts" / "call_summary_internal.md"
INTERNAL_BRIEF_PROMPT = ROOT / "docs" / "prompts" / "call_brief_internal.md"
CHUNK_CHARS = 6000
MAP_MAX_TOKENS = 1200
REDUCE_MAX_TOKENS = 4096
BRIEF_REDUCE_MAX_TOKENS = 1024
PAST_SUMMARY_LIMIT = 3


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_internal_project() -> bool:
    return _env_flag("PRIMARY_ANALITICS_INTERNAL_PROJECT")


def _summary_prompt_paths() -> tuple[Path, Path]:
    if _is_internal_project():
        return INTERNAL_PROMPT, INTERNAL_BRIEF_PROMPT
    return PROMPT, BRIEF_PROMPT


def _project_dictionary_text() -> str:
    raw = os.environ.get("PRIMARY_ANALITICS_PROJECT_DICTIONARY", "").strip()
    return raw or "(словарь не задан)"


def _jira_ticket_base_text() -> str:
    raw = os.environ.get("PRIMARY_ANALITICS_JIRA_TICKET_BASE", "").strip()
    return raw or "(нет)"


def _load_past_memory(*, project_id: str, exclude_artifact_id: str, limit: int = PAST_SUMMARY_LIMIT) -> str:
    """До `limit` предыдущих итоговых резюме того же портального проекта (не текущий)."""
    if not project_id:
        return "(нет прошлых резюме)"
    parsed = ROOT / "data" / "projects" / str(project_id) / "parsed" / "calls"
    if not parsed.is_dir():
        return "(нет прошлых резюме)"
    files = sorted(parsed.glob("*_call_summary.md"), key=lambda p: p.name, reverse=True)
    chunks: list[str] = []
    for path in files:
        artifact_id = path.name[: -len("_call_summary.md")]
        if exclude_artifact_id and artifact_id == exclude_artifact_id:
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        chunks.append(f"### Прошлое резюме `{artifact_id}`\n\n{text}")
        if len(chunks) >= limit:
            break
    if not chunks:
        return "(нет прошлых резюме)"
    return "\n\n".join(chunks)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_prompts(path: Path) -> tuple[str, str, str, str]:
    """Промпт: 4 кодовых блока по порядку —
    map-система, map-пользователь, reduce-система, reduce-пользователь."""
    blocks = _read(path).split("```")
    if len(blocks) < 8:
        raise SystemExit(f"{path}: ожидалось 4 кодовых блока, найдено {len(blocks) // 2}")
    return tuple(blocks[i].strip() for i in (1, 3, 5, 7))  # type: ignore[return-value]


_JIRA_BROWSE_RE = re.compile(
    r"(?:https?://)?(?:[^\s/]+/)*browse/([A-Z][A-Z0-9]+-\d+)",
    re.IGNORECASE,
)
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_JIRA_HINT_RE = re.compile(r"\bjira\b|/browse/", re.IGNORECASE)
_JIRA_TITLE_RE = re.compile(
    r"(?:^|\n)\s*(?:summary|название|тема|заголовок)\s*[:：]\s*(.+)",
    re.IGNORECASE,
)
_JIRA_STATUS_RE = re.compile(
    r"(?:^|\n)\s*(?:status|статус)\s*[:：]\s*([^\n|]+)",
    re.IGNORECASE,
)


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


def _normalize_jira_browse_url(raw: str, key: str) -> str:
    text = raw.strip().rstrip(").,;]")
    if text.lower().startswith("http://") or text.lower().startswith("https://"):
        return text
    if "/browse/" in text.lower():
        return f"https://{text.lstrip('/')}"
    base = os.environ.get("PRIMARY_ANALITICS_JIRA_TICKET_BASE", "").strip()
    if base:
        if "{key}" in base:
            return base.replace("{key}", key)
        return f"{base.rstrip('/')}/{key}"
    return key


def _extract_jira_screen_meta(text: str) -> dict[str, str] | None:
    """Из OCR/vision кадра Jira — ключ, URL, при наличии название и статус; без тела комментариев."""
    if not text or not _JIRA_HINT_RE.search(text):
        return None
    browse = _JIRA_BROWSE_RE.search(text)
    key = ""
    url = ""
    if browse:
        key = browse.group(1).upper()
        url = _normalize_jira_browse_url(browse.group(0), key)
    if not key:
        for match in _JIRA_KEY_RE.finditer(text):
            key = match.group(1).upper()
            break
    if not key:
        return None
    if not url:
        url = _normalize_jira_browse_url("", key)
    title = ""
    title_match = _JIRA_TITLE_RE.search(text)
    if title_match:
        title = title_match.group(1).strip(" \t-–—|")
    status = ""
    status_match = _JIRA_STATUS_RE.search(text)
    if status_match:
        status = status_match.group(1).strip(" \t-–—|")
    meta = {"key": key, "url": url}
    if title:
        meta["title"] = title[:200]
    if status:
        meta["status"] = status[:80]
    return meta


def _screen_note_for_summary(text: str, *, internal: bool, frame_id: str = "") -> str | None:
    """Заметка экрана для выжимки. Non-internal — полный текст; internal Jira — только мета."""
    text = (text or "").strip()
    if not text:
        return None
    if not internal:
        note = f"На экране показывалось: {text}"
        if frame_id:
            note += f" [frame_id={frame_id}]"
        return note

    meta = _extract_jira_screen_meta(text)
    if not meta:
        # Внутренние дейли: не тащим сырой OCR не-Jira (комменты, confluence и т.п.).
        return None
    parts = [f"тикет {meta['key']}"]
    if meta.get("title"):
        parts.append(meta["title"])
    if meta.get("status"):
        parts.append(f"статус: {meta['status']}")
    if meta["url"] and meta["url"] != meta["key"]:
        parts.append(meta["url"])
    note = "На экране (Jira): " + " — ".join(parts)
    if frame_id:
        note += f" [frame_id={frame_id}]"
    return note


def _attach_video_context(turns: list[Turn]) -> list[Turn]:
    """Add screen-share observations to the publisher's overlapping turns.

    Для `internal_project` кадры Jira сжимаются до ссылки/ключа (+ название/статус);
    полный OCR комментариев в выжимку не попадает. Остальные проекты — без изменений.
    """
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

    internal = _is_internal_project()
    enriched: list[Turn] = []
    attached = 0
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
            note = _screen_note_for_summary(text, internal=internal, frame_id=frame_id)
            if note and note not in notes:
                notes.append(note)
        context = "\n".join(notes)
        if context:
            attached += 1
        enriched.append(
            Turn(
                start=turn.start,
                end=turn.end,
                speaker=turn.speaker,
                text=f"{turn.text}\n{context}" if context else turn.text,
            )
        )
    logger.info(
        "meeting.video_context.attached turns=%s segments=%s with_notes=%s internal=%s",
        len(turns),
        len(segments),
        attached,
        internal,
    )
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


def _format_reduce_user(
    tmpl: str,
    *,
    drafts: str,
    past_memory: str,
    project_dictionary: str,
    jira_ticket_base: str,
) -> str:
    kwargs = {"drafts": drafts}
    if "{past_memory}" in tmpl:
        kwargs["past_memory"] = past_memory
        kwargs["project_dictionary"] = project_dictionary
        kwargs["jira_ticket_base"] = jira_ticket_base
    return tmpl.format(**kwargs)


def _run_map_reduce(
    turns: list[Turn],
    prompt_path: Path,
    out_path: Path,
    *,
    reduce_max_tokens: int,
    label: str,
    past_memory: str = "(нет прошлых резюме)",
    project_dictionary: str = "(словарь не задан)",
    jira_ticket_base: str = "(нет)",
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

    reduce_user = _format_reduce_user(
        reduce_user_tmpl,
        drafts="\n\n---\n\n".join(drafts),
        past_memory=past_memory,
        project_dictionary=project_dictionary,
        jira_ticket_base=jira_ticket_base,
    )
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

    full_prompt, brief_prompt = _summary_prompt_paths()
    internal = _is_internal_project()
    project_id = os.environ.get("PRIMARY_ANALITICS_PROJECT_ID", "").strip()
    artifact_id = os.environ.get("PRIMARY_ANALITICS_ARTIFACT_ID", "").strip()
    past_memory = (
        _load_past_memory(project_id=project_id, exclude_artifact_id=artifact_id)
        if internal
        else "(нет прошлых резюме)"
    )
    project_dictionary = _project_dictionary_text() if internal else "(словарь не задан)"
    jira_ticket_base = _jira_ticket_base_text() if internal else "(нет)"
    print(
        f"summary LLM={llm_name()} internal={internal} prompt={full_prompt.name}",
        flush=True,
    )
    try:
        _run_map_reduce(
            turns,
            full_prompt,
            OUT / "call_summary.md",
            reduce_max_tokens=REDUCE_MAX_TOKENS,
            label="full",
            past_memory=past_memory,
            project_dictionary=project_dictionary,
            jira_ticket_base=jira_ticket_base,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        _run_map_reduce(
            turns,
            brief_prompt,
            OUT / "call_brief.md",
            reduce_max_tokens=BRIEF_REDUCE_MAX_TOKENS,
            label="brief",
            past_memory=past_memory,
            project_dictionary=project_dictionary,
            jira_ticket_base=jira_ticket_base,
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
