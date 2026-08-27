from __future__ import annotations

import re
from dataclasses import dataclass

CHUNK_TOKENS = 512
OVERLAP_TOKENS = 128
CHARS_PER_TOKEN = 4


@dataclass
class TextChunk:
    index: int
    text: str
    speaker: str | None
    token_estimate: int


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p for p in parts if p.strip()]


def chunk_plain_text(text: str, *, max_tokens: int = CHUNK_TOKENS, overlap_tokens: int = OVERLAP_TOKENS) -> list[TextChunk]:
    sentences = _split_sentences(text)
    if not sentences:
        return []
    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    chunks: list[TextChunk] = []
    buf: list[str] = []
    size = 0
    idx = 0
    for sent in sentences:
        sent_len = len(sent) + 1
        if buf and size + sent_len > max_chars:
            body = " ".join(buf).strip()
            chunks.append(TextChunk(idx, body, None, estimate_tokens(body)))
            idx += 1
            carry = body[-overlap_chars:] if overlap_chars else ""
            buf = [carry, sent] if carry else [sent]
            size = sum(len(x) + 1 for x in buf)
        else:
            buf.append(sent)
            size += sent_len
    if buf:
        body = " ".join(buf).strip()
        chunks.append(TextChunk(idx, body, None, estimate_tokens(body)))
    return chunks


def chunk_transcript(text: str, *, max_tokens: int = CHUNK_TOKENS) -> list[TextChunk]:
    """Чанки по репликам: строки «Спикер: текст» или «[таймкод] Имя: текст»."""
    turns: list[tuple[str | None, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        bare = re.sub(r"^\[[^\]]+\]\s*", "", line)
        m = re.match(r"^(Спикер\s*№\d+|[^:]+):\s*(.+)$", bare, re.I)
        if m:
            turns.append((m.group(1).strip(), m.group(2).strip()))
        else:
            turns.append((None, line))
    if not turns:
        return chunk_plain_text(text, max_tokens=max_tokens, overlap_tokens=0)
    chunks: list[TextChunk] = []
    buf: list[str] = []
    speaker: str | None = None
    size = 0
    idx = 0
    max_chars = max_tokens * CHARS_PER_TOKEN
    for sp, txt in turns:
        line = f"{sp}: {txt}" if sp else txt
        if buf and (speaker != sp or size + len(line) > max_chars):
            body = "\n".join(buf)
            chunks.append(TextChunk(idx, body, speaker, estimate_tokens(body)))
            idx += 1
            buf, size = [], 0
        speaker = sp or speaker
        buf.append(line)
        size += len(line) + 1
    if buf:
        body = "\n".join(buf)
        chunks.append(TextChunk(idx, body, speaker, estimate_tokens(body)))
    return chunks
