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
    assert "Не используй каркасы" in reduce_sys
    assert "Суть созвона / Поднятые вопросы / Щепетильные моменты" in reduce_sys
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
    assert "Не используй каркасы" in blocks[5]
    assert "структуру полного протокола" in blocks[5]
    assert "{chunk_text}" in blocks[3]
    assert "{drafts}" in blocks[7]


def test_summary_token_limits():
    src = SUMMARIZE.read_text(encoding="utf-8")
    assert "MAP_MAX_TOKENS = 1200" in src
    assert "REDUCE_MAX_TOKENS = 4096" in src
    assert "BRIEF_REDUCE_MAX_TOKENS = 1024" in src
    assert "reduce_max_tokens=REDUCE_MAX_TOKENS" in src
    assert "call_brief.md" in src


def test_summary_attaches_screen_context_to_publisher(tmp_path, monkeypatch):
    import importlib.util
    import json
    from asr.speakers import Turn

    spec = importlib.util.spec_from_file_location("skkkk_summary", SUMMARIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "asr"
    run = tmp_path / "lab" / "runs" / "run-1"
    run.mkdir(parents=True)
    out.mkdir()
    (tmp_path / "lab" / "latest.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
    (run / "video_transcript.json").write_text(json.dumps({"segments": [{
        "start": 10, "end": 12, "speaker": "Анна", "identity": "member:1",
        "frame_id": "TR_1_10000", "text": "таблица заказов",
    }]}), encoding="utf-8")
    monkeypatch.setattr(module, "OUT", out)
    result = module._attach_video_context([Turn(10, 11, "Анна", "Смотрите сюда")])
    assert "На экране показывалось: таблица заказов" in result[0].text
    assert "frame_id=TR_1_10000" in result[0].text
