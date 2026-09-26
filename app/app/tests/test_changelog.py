import json

from app.app.routers import pages


def test_changelog_newest_first_and_ru_date(tmp_path, monkeypatch):
    path = tmp_path / "changelog.json"
    path.write_text(
        json.dumps(
            [
                {"date": "2026-09-15", "text": "старое"},
                {"date": "2026-09-26", "text": "новое"},
                {"date": "", "text": "без даты"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pages, "CHANGELOG_PATH", path)
    assert pages.load_changelog() == [
        {"date": "26.09.2026", "text": "новое"},
        {"date": "15.09.2026", "text": "старое"},
    ]


def test_changelog_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "CHANGELOG_PATH", tmp_path / "nope.json")
    assert pages.load_changelog() == []


def test_real_changelog_is_valid():
    assert pages.load_changelog()


def test_projects_page_has_changelog_button_and_modal(cookie_auth_client):
    html = cookie_auth_client.get("/").text
    assert 'data-bs-target="#changelogModal"' in html
    assert 'id="changelogModal"' in html
    # кнопка стоит в шапке левее бейджа LLM
    assert html.index("nav-changelog") < html.index('id="nav-llm"')
    assert "26.09.2026" in html
