from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROMPT = ROOT / "docs" / "prompts" / "call_summary.md"
SUMMARIZE = ROOT / "asr" / "summarize_call.py"


def test_summary_prompt_is_meeting_minutes():
    text = PROMPT.read_text(encoding="utf-8")
    blocks = text.split("```")
    assert len(blocks) >= 8
    reduce_sys = blocks[5].strip()
    assert "протокол" in text.lower()
    assert "Не закрыто" in text
    assert "Кто" in text and "Что" in text
    assert "Не закрыто" in reduce_sys
    assert "Не используй обязательный каркас «Суть созвона" in reduce_sys
    assert "## Суть созвона" not in reduce_sys
    assert "набросать как строки" not in text
    assert "| --- | --- | --- |" in reduce_sys
    assert "| --- | --- | --- |" in blocks[1]
    assert "{chunk_text}" in blocks[3]
    assert "{drafts}" in blocks[7]


BRIEF_PROMPT = ROOT / "docs" / "prompts" / "call_brief.md"


def test_brief_prompt_has_four_blocks_and_essence():
    text = BRIEF_PROMPT.read_text(encoding="utf-8")
    blocks = text.split("```")
    assert len(blocks) >= 8
    assert "Отрази просто суть созвона" in blocks[5]
    assert "{chunk_text}" in blocks[3]
    assert "{drafts}" in blocks[7]


def test_summary_token_limits():
    src = SUMMARIZE.read_text(encoding="utf-8")
    assert "MAP_MAX_TOKENS = 1200" in src
    assert "REDUCE_MAX_TOKENS = 4096" in src
    assert "BRIEF_REDUCE_MAX_TOKENS = 1024" in src
    assert "reduce_max_tokens=REDUCE_MAX_TOKENS" in src
    assert "call_brief.md" in src
