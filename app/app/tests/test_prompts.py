from __future__ import annotations

from pathlib import Path

import pytest

from app.app.services import chat, entities, image_extract, meeting_frames, normalize
from app.app.services.prompts import fenced_blocks, load_prompt_block

PROMPTS = Path(__file__).resolve().parents[3] / "docs" / "prompts"
# Файлы, которые целиком уходят в модель как один системный промпт (примеры JSON внутри — часть текста).
WHOLE_FILE = {"lab_proof_text.md", "lab_sense_speakers.md"}
EXPECTED_BLOCKS = {"rebuild_tz.md": 6, "call_summary.md": 4, "call_brief.md": 4, "doc_summary.md": 4}


def test_fenced_blocks_skip_header_and_prose():
    raw = "# Заголовок\n\nпояснение\n\n```\nПромпт 1\n```\n\nмежду\n\n```\nПромпт 2\n```\n"
    assert fenced_blocks(raw) == ["Промпт 1", "Промпт 2"]
    assert load_prompt_block.__name__ == "load_prompt_block"


@pytest.mark.parametrize("path", sorted(p for p in PROMPTS.glob("*.md") if p.name not in WHOLE_FILE), ids=lambda p: p.name)
def test_every_prompt_file_has_real_blocks(path):
    blocks = fenced_blocks(path.read_text(encoding="utf-8"))
    assert blocks, f"{path.name}: нет ограждённых блоков"
    for block in blocks:
        assert not block.startswith("#"), f"{path.name}: блок начинается с markdown-заголовка"
        assert len(block) > 20
    if path.name in EXPECTED_BLOCKS:
        assert len(blocks) == EXPECTED_BLOCKS[path.name], path.name


@pytest.mark.parametrize(
    "loader",
    [chat._load_prompt, entities._load_system_prompt, normalize._load_prompt, image_extract._load_vision_prompt, meeting_frames._load_prompt],
    ids=lambda f: f"{f.__module__.split('.')[-1]}.{f.__name__}",
)
def test_system_prompt_loaders_return_instructions(loader):
    text = loader()
    assert len(text) > 100
    assert not text.lstrip().startswith("#")
    assert "```" not in text


def test_chat_prompt_has_no_unfilled_user_template():
    assert "{question}" not in chat._load_prompt()


def test_report_pages_get_design_theme_without_touching_file():
    from types import SimpleNamespace

    from app.app.routers.pages import _serve_report

    html = "<html><head><style>x{}</style></head><body>отчёт</body></html>"
    resp = _serve_report(html, SimpleNamespace(role="admin"), embed=False, kind="document", role="admin")
    body = resp.body.decode("utf-8")
    assert body.index("design.css") < body.index("report-theme.css") < body.index("</head>")
    assert body.count("report-theme.css") == 1
    again = _serve_report(body, SimpleNamespace(role="admin"), embed=True, kind="audio", role="admin").body.decode("utf-8")
    assert again.count("report-theme.css") == 1  # повторная подстановка не дублирует


def test_speaker_match_page_keeps_own_theme_without_design_css():
    """design.css на /report-speaker-match ломает бейджи и таблицу базы голосов."""
    from types import SimpleNamespace

    from app.app.routers.pages import _serve_report

    html = (
        '<html><head></head><body>'
        '<article class="speaker-card h-100" data-speaker="speaker_0"></article>'
        "</body></html>"
    )
    body = _serve_report(html, SimpleNamespace(role="admin"), embed=False, kind="audio", role="admin").body.decode("utf-8")
    assert "design.css" not in body and "report-theme.css" not in body
