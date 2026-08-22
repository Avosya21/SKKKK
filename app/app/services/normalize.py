from __future__ import annotations

import logging
import re
from pathlib import Path

from app.app.config import Settings
from app.app.services.llm import chat_completion

logger = logging.getLogger("skkkk.normalize")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "normalize_document.md"


def _load_prompt() -> str:
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    blocks = [b.strip() for b in raw.split("```") if b.strip()]
    return blocks[0] if blocks else raw


def _split_sections(text: str, max_chars: int = 10000) -> list[str]:
    parts = re.split(r"(?=^#{1,3}\s)", text, flags=re.M)
    chunks: list[str] = []
    buf = ""
    for part in parts:
        if len(buf) + len(part) > max_chars and buf:
            chunks.append(buf.strip())
            buf = part
        else:
            buf += part
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text]


def normalize_document(settings: Settings, raw: str) -> str:
    system = _load_prompt()
    sections = _split_sections(raw)
    cleaned_parts: list[str] = []
    for section in sections:
        user = f"Нормализуй текст, не теряя числа, даты и суммы:\n\n{section}"
        part = chat_completion(settings, system=system, user=user, max_tokens=8192)
        cleaned_parts.append(part.strip())
    return "\n\n".join(cleaned_parts).strip() + "\n"


def light_normalize(raw: str) -> str:
    """Без LLM: пробелы и пустые строки."""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(ln)
    return "\n".join(out).strip() + "\n"
