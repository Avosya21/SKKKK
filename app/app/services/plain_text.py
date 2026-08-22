"""Convert common LLM LaTeX / math markup into plain Unicode for HTML display.

Gemma often emits `$\\rightarrow$`, `$\\le$`, `$\\times$` even when prompts forbid it.
We do not load KaTeX — these are typographic shortcuts, not real formulas.
"""

from __future__ import annotations

import re

# Longer names first so \\rightarrow wins over \\to / \\rightarrowme-style misses.
_COMMANDS: tuple[tuple[str, str], ...] = (
    ("longrightarrow", "→"),
    ("longleftarrow", "←"),
    ("leftrightarrow", "↔"),
    ("Leftrightarrow", "⇔"),
    ("Rightarrow", "⇒"),
    ("Leftarrow", "⇐"),
    ("rightarrow", "→"),
    ("leftarrow", "←"),
    ("uparrow", "↑"),
    ("downarrow", "↓"),
    ("hookrightarrow", "↪"),
    ("mapsto", "↦"),
    ("leqslant", "≤"),
    ("geqslant", "≥"),
    ("subseteq", "⊆"),
    ("supseteq", "⊇"),
    ("emptyset", "∅"),
    ("varnothing", "∅"),
    ("checkmark", "✓"),
    ("partial", "∂"),
    ("forall", "∀"),
    ("exists", "∃"),
    ("subset", "⊂"),
    ("supset", "⊃"),
    ("approx", "≈"),
    ("simeq", "≃"),
    ("equiv", "≡"),
    ("propto", "∝"),
    ("infty", "∞"),
    ("times", "×"),
    ("cdot", "·"),
    ("bullet", "•"),
    ("ldots", "…"),
    ("cdots", "⋯"),
    ("dots", "…"),
    ("degree", "°"),
    ("nabla", "∇"),
    ("angle", "∠"),
    ("perp", "⊥"),
    ("parallel", "∥"),
    ("notin", "∉"),
    ("leq", "≤"),
    ("geq", "≥"),
    ("neq", "≠"),
    ("sum", "∑"),
    ("prod", "∏"),
    ("sqrt", "√"),
    ("cap", "∩"),
    ("cup", "∪"),
    ("sim", "∼"),
    ("pm", "±"),
    ("mp", "∓"),
    ("ll", "≪"),
    ("gg", "≫"),
    ("le", "≤"),
    ("ge", "≥"),
    ("ne", "≠"),
    ("to", "→"),
    ("in", "∈"),
)

_CMD_MAP = {name.lower(): glyph for name, glyph in _COMMANDS}
# Stable unique order: longest command names first.
_CMD_NAMES = sorted(_CMD_MAP.keys(), key=len, reverse=True)
_CMD_ALT = "|".join(re.escape(name) for name in _CMD_NAMES)

_CMD_RE = re.compile(
    rf"(?:\$\s*)?\\({_CMD_ALT})(?![A-Za-z])(?:\s*\$)?|\\\(\s*\\({_CMD_ALT})(?![A-Za-z])\s*\\\)",
    re.IGNORECASE,
)
_DOLLAR_WRAP_RE = re.compile(r"\$([^$\n]{1,120})\$")
_PAREN_WRAP_RE = re.compile(r"\\\(((?:\\.|[^\\])*?)\\\)")
_BRACK_WRAP_RE = re.compile(r"\\\[((?:\\.|[^\\])*?)\\\]")

_ARTIFACT_RE = re.compile(
    rf"\$\\|\\({_CMD_ALT})(?![A-Za-z])|\\\(|\\\[|\$[^$\n]{{1,80}}\$",
    re.IGNORECASE,
)

# Gemma иногда ставит emoji и markdown-чекбоксы [ ] в выжимках.
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U00002600-\U000026FF"
    "]+",
    flags=re.UNICODE,
)
_LIST_CHECKBOX_RE = re.compile(
    r"^(\s*(?:[-*]|\d+\.)\s+)\[[ xX]\]\s+",
    re.MULTILINE,
)
_STANDALONE_CHECKBOX_RE = re.compile(r"^\s*\[[ xX]\]\s+", re.MULTILINE)


def _glyph(name: str | None) -> str | None:
    if not name:
        return None
    return _CMD_MAP.get(name.lower())


def _sub_command(match: re.Match[str]) -> str:
    glyph = _glyph(match.group(1)) or _glyph(match.group(2))
    return glyph if glyph is not None else match.group(0)


def _plain_inner(match: re.Match[str]) -> str:
    inner = match.group(1).strip()
    return _CMD_RE.sub(_sub_command, inner)


def plainify_llm_markup(text: str | None) -> str:
    """Replace typographic LaTeX scraps with Unicode; leave normal text alone."""
    if not text:
        return ""
    out = str(text)
    out = _CMD_RE.sub(_sub_command, out)
    out = _DOLLAR_WRAP_RE.sub(_plain_inner, out)
    out = _PAREN_WRAP_RE.sub(_plain_inner, out)
    out = _BRACK_WRAP_RE.sub(_plain_inner, out)
    for src, dst in (
        (r"\%", "%"),
        (r"\&", "&"),
        (r"\#", "#"),
        (r"\_", "_"),
        (r"\{", "{"),
        (r"\}", "}"),
    ):
        out = out.replace(src, dst)
    # Stray dollars around short symbols: $→$ / $×$
    out = re.sub(r"\$([^\w\s$]{1,4})\$", r"\1", out)
    return out


def sanitize_summary_markdown(text: str | None) -> str:
    """Убрать emoji и markdown-чекбоксы [ ] из выжимок (созвон / проект)."""
    if not text:
        return ""
    out = str(text)
    out = _EMOJI_RE.sub("", out)
    out = _LIST_CHECKBOX_RE.sub(r"\1", out)
    out = _STANDALONE_CHECKBOX_RE.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +\n", "\n", out)
    return out.strip()


def clean_summary_output(text: str | None) -> str:
    """LaTeX → Unicode, затем emoji/checkbox scrub для итоговых выжимок."""
    return sanitize_summary_markdown(plainify_llm_markup(text))


def has_llm_markup_artifacts(text: str | None) -> bool:
    if not text:
        return False
    return bool(_ARTIFACT_RE.search(text))
