"""Доставка писем внешнему агенту через MCP: только наши письма, только своего проекта, без путей из запроса."""
from __future__ import annotations

import pytest

from app.app.services import agent_letters as al


@pytest.fixture()
def letters(tmp_path, monkeypatch):
    d = tmp_path / "letters"
    d.mkdir()
    (d / "003_kb_to_analysis_agent.md").write_text("# Письмо три\n\nВерсия 1.0 · от: агент базы знаний проекта #18 · кому: агент\n\nТекст", encoding="utf-8")
    (d / "004_kb_to_analysis_agent.md").write_text("# Письмо четыре\n\nВерсия 1.0 · от: агент базы знаний проекта #18\n", encoding="utf-8")
    (d / "005_kb_to_analysis_agent.md").write_text("# Чужой проект\n\nот: агент базы знаний проекта #7\n", encoding="utf-8")
    (d / "002_analysis_to_kb_agent.md").write_text("# Входящее\n\nот: агент базы знаний проекта #18\n", encoding="utf-8")  # не наше исходящее по имени
    (d / "notes.md").write_text("# Не письмо\n", encoding="utf-8")
    monkeypatch.setattr(al, "LETTERS_DIR", d)
    return d


def test_only_own_project_kb_letters_are_listed(letters):
    names = [r["name"] for r in al.list_letters(18)]
    assert names == ["003_kb_to_analysis_agent.md", "004_kb_to_analysis_agent.md"]
    assert [r["name"] for r in al.list_letters(7)] == ["005_kb_to_analysis_agent.md"]
    assert al.list_letters(99) == []


def test_get_letter_returns_text_and_rejects_paths(letters):
    got = al.get_letter(18, "contract/letters/003_kb_to_analysis_agent.md")
    assert "Текст" in got["content"] and got["title"] == "Письмо три"
    for bad in ("contract/letters/../../.env", "contract/letters/005_kb_to_analysis_agent.md", "contract/letters/notes.md", "contract/letters/002_analysis_to_kb_agent.md"):
        with pytest.raises(ValueError):
            al.get_letter(18, bad)


def test_summary_and_mcp_listing(letters, client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.services import agent_gateway as gw
    from app.app.services import project_mcp_data as data
    from app.app.tests.test_contract import _seed

    db, pid, _art, _ = _seed(client, manager_token, tmp_data)
    assert al.summary(18)["count"] == 2 and al.summary(18)["latest"] == "004_kb_to_analysis_agent.md"
    assert gw.exchange_status(db, pid)["letters_from_kb"]["count"] == 0  # у этого проекта писем нет
    assert not [r for r in data.mcp_list_artifacts(db, pid, settings=get_settings(), prefix="contract/letters/")]
