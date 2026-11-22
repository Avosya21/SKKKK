from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROMPT = ROOT / "docs" / "prompts" / "call_summary.md"
INTERNAL_PROMPT = ROOT / "docs" / "prompts" / "call_summary_internal.md"
INTERNAL_BRIEF = ROOT / "docs" / "prompts" / "call_brief_internal.md"
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


def test_internal_summary_prompt_has_daily_structure_and_memory_slots():
    text = INTERNAL_PROMPT.read_text(encoding="utf-8")
    blocks = text.split("```")
    assert len(blocks) >= 8
    reduce_sys = blocks[5].strip()
    reduce_user = blocks[7].strip()
    assert "Не обсуждённые проекты" in reduce_sys
    assert "Тикеты" in reduce_sys or "тикет" in reduce_sys.lower()
    assert "Организация команды" in reduce_sys
    assert "Действия ПМ" in reduce_sys
    assert "Приоритет — сказанное голосом" in text or "сказанное голосом" in text.lower()
    assert "На экране (Jira)" in reduce_sys
    assert "{past_memory}" in reduce_user
    assert "{project_dictionary}" in reduce_user
    assert "{jira_ticket_base}" in reduce_user
    assert "{drafts}" in reduce_user
    assert "{chunk_text}" in blocks[3]


def test_internal_brief_prompt_mirrors_summary_schema():
    text = INTERNAL_BRIEF.read_text(encoding="utf-8")
    blocks = text.split("```")
    assert len(blocks) >= 8
    assert "Не обсуждённые проекты" in blocks[5]
    assert "{past_memory}" in blocks[7]
    assert "{drafts}" in blocks[7]


def test_summary_token_limits():
    src = SUMMARIZE.read_text(encoding="utf-8")
    assert "MAP_MAX_TOKENS = 1200" in src
    assert "REDUCE_MAX_TOKENS = 4096" in src
    assert "BRIEF_REDUCE_MAX_TOKENS = 1024" in src
    assert "reduce_max_tokens=REDUCE_MAX_TOKENS" in src
    assert "call_brief.md" in src
    assert "call_summary_internal.md" in src
    assert "PRIMARY_ANALITICS_INTERNAL_PROJECT" in src


def test_summary_prompt_paths_switch_on_internal_flag(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("primary_analitics_summary_paths", SUMMARIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("PRIMARY_ANALITICS_INTERNAL_PROJECT", raising=False)
    assert module._summary_prompt_paths() == (module.PROMPT, module.BRIEF_PROMPT)
    monkeypatch.setenv("PRIMARY_ANALITICS_INTERNAL_PROJECT", "1")
    full, brief = module._summary_prompt_paths()
    assert full.name == "call_summary_internal.md"
    assert brief.name == "call_brief_internal.md"


def test_past_memory_loads_three_previous_summaries(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("primary_analitics_summary_memory", SUMMARIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "root"
    calls = root / "data" / "projects" / "31" / "parsed" / "calls"
    calls.mkdir(parents=True)
    (calls / "art_2026-11-01_a_call_summary.md").write_text("проект Альфа", encoding="utf-8")
    (calls / "art_2026-11-02_b_call_summary.md").write_text("проект Бета", encoding="utf-8")
    (calls / "art_2026-11-03_c_call_summary.md").write_text("проект Гамма", encoding="utf-8")
    (calls / "art_2026-11-04_d_call_summary.md").write_text("проект Дельта", encoding="utf-8")
    (calls / "art_2026-11-05_current_call_summary.md").write_text("текущий", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", root)
    text = module._load_past_memory(project_id="31", exclude_artifact_id="art_2026-11-05_current")
    assert "проект Дельта" in text
    assert "проект Гамма" in text
    assert "проект Бета" in text
    assert "проект Альфа" not in text
    assert "текущий" not in text


def test_enrich_call_summary_env_sets_internal_flag(monkeypatch):
    from types import SimpleNamespace

    import app.app.db as app_db
    from app.app.services.pipeline_audio import _enrich_call_summary_env

    class _FakeProject:
        def settings(self):
            return {
                "internal_project": True,
                "workstream_dictionary": ["Альфа"],
                "jira_ticket_base": "https://jira.example/browse/{key}",
            }

    class _FakeDB:
        def get(self, *_a, **_k):
            return _FakeProject()

        def close(self):
            return None

    monkeypatch.setattr(app_db, "SessionLocal", _FakeDB)
    env: dict[str, str] = {}
    _enrich_call_summary_env(env, SimpleNamespace(project_id=31))
    assert env["PRIMARY_ANALITICS_INTERNAL_PROJECT"] == "1"
    assert "Альфа" in env["PRIMARY_ANALITICS_PROJECT_DICTIONARY"]
    assert "jira.example" in env["PRIMARY_ANALITICS_JIRA_TICKET_BASE"]


def test_summary_attaches_screen_context_to_publisher(tmp_path, monkeypatch):
    import importlib.util
    import json
    from asr.speakers import Turn

    spec = importlib.util.spec_from_file_location("primary_analitics_summary", SUMMARIZE)
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
    monkeypatch.delenv("PRIMARY_ANALITICS_INTERNAL_PROJECT", raising=False)
    result = module._attach_video_context([Turn(10, 11, "Анна", "Смотрите сюда")])
    assert "На экране показывалось: таблица заказов" in result[0].text
    assert "frame_id=TR_1_10000" in result[0].text


def test_internal_jira_screen_keeps_link_drops_comment_body():
    import importlib.util

    spec = importlib.util.spec_from_file_location("primary_analitics_summary_jira", SUMMARIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ocr = (
        "Jira Software. URL: jira.example.test/browse/DEMO-750\n"
        "Статус: В работе\n"
        "Название: Пилотный контур\n"
        "Комментарий: Нужно согласование с Иванов Пётр и Петрова Анна. "
        "Отмена невозможна, статус Возврат."
    )
    note = module._screen_note_for_summary(ocr, internal=True, frame_id="F1")
    assert note is not None
    assert "DEMO-750" in note
    assert "jira.example.test/browse/DEMO-750" in note
    assert "В работе" in note
    assert "Пилотный контур" in note
    assert "Петрова" not in note
    assert "Иванов" not in note
    assert "Отмена невозможна" not in note
    assert "frame_id=F1" in note


def test_internal_non_jira_screen_is_dropped_from_summary_notes():
    import importlib.util

    spec = importlib.util.spec_from_file_location("primary_analitics_summary_nojira", SUMMARIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._screen_note_for_summary("таблица заказов на Confluence", internal=True) is None
    full = module._screen_note_for_summary("таблица заказов на Confluence", internal=False)
    assert full and "таблица заказов" in full


def test_internal_attach_compacts_jira_for_overlapping_turn(tmp_path, monkeypatch):
    import importlib.util
    import json
    from asr.speakers import Turn

    spec = importlib.util.spec_from_file_location("primary_analitics_summary_attach_int", SUMMARIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "asr"
    run = tmp_path / "lab" / "runs" / "run-1"
    run.mkdir(parents=True)
    out.mkdir()
    (tmp_path / "lab" / "latest.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
    (run / "video_transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "start": 10,
                        "end": 20,
                        "speaker": "Анна",
                        "text": (
                            "Jira URL jira.example/browse/DEMO-750. "
                            "Комментарий: согласование с Петровой."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "OUT", out)
    monkeypatch.setenv("PRIMARY_ANALITICS_INTERNAL_PROJECT", "1")
    result = module._attach_video_context([Turn(12, 15, "Анна", "Давай 750-ю разберём")])
    assert "DEMO-750" in result[0].text
    assert "browse/DEMO-750" in result[0].text
    assert "Петровой" not in result[0].text
    assert "Давай 750-ю разберём" in result[0].text
