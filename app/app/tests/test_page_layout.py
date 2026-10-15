"""Страницы портала: регрессии из аудита вёрстки 20.09.2026 (полная проверка в браузере: scripts/qa_pages.py)."""
from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
STATIC = Path(__file__).resolve().parents[1] / "static"


def test_standalone_pages_load_their_scripts_after_app_js():
    """Раньше inline-скрипт лежал в блоке content, то есть выполнялся ДО app.js: «SKKKK is not defined», страницы ТЗ/чата/графа/конфликтов не работали."""
    for name in ("tz", "chat", "graph", "conflicts"):
        text = (TEMPLATES / name).with_suffix(".html").read_text(encoding="utf-8")
        assert "{% block scripts %}" in text, name
        assert text.index("{% block scripts %}") < text.index("<script"), name  # весь JS страницы внутри блока scripts
        assert text.count("<script") == text[text.index("{% block scripts %}"):].count("<script"), name


def test_project_page_reserves_list_height_and_has_no_broken_cdn_css_or_english_button():
    html = (TEMPLATES / "project.html").read_text(encoding="utf-8")
    assert "reserveArtifactsHeight" in html and 'id="artifacts-body"' in html and "data-count=" in html
    assert "Загружаю материалы" in html and 'id="kpi-done">—<' in html  # не «0/0/0», пока список не пришёл
    assert "fullcalendar@6.1.15/main.min.css" not in html  # такого файла на CDN нет (404 при каждом открытии)
    assert "'match'" not in html and "'Голоса'" in html
    assert "EXCHANGE_FILE" in html  # служебные файлы обмена с агентами не показываются как материалы


def test_project_materials_collapsed_and_insights_equal_width():
    html = (TEMPLATES / "project.html").read_text(encoding="utf-8")
    panel = html.index('<div id="artifacts-panel" class="collapse">')
    assert panel < html.index('id="artifacts-body"')  # список материалов внутри свёрнутого блока
    assert 'data-bs-target="#artifacts-panel"' in html and 'id="kpi-done"' in html  # сводка остаётся в шапке
    assert "col-lg-6 insights-col-summary" in html and "col-lg-6 insights-col-chat" in html
    assert "col-lg-7" not in html and "col-lg-5" not in html
    assert "panel: 'artifacts-panel'" in (STATIC / "px_digest.js").read_text(encoding="utf-8")


def test_chat_same_look_on_project_and_chat_pages():
    """Чат без вложенных рамок и подписей «Вы/ИИ», источники свёрнуты; стили общие, вид один на обеих страницах."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".chat-message-user {" in css and ".chat-sources summary" in css
    assert ".chat-card .card-body > .chat-composer" in css  # нижний отступ поля не съедает «.card-body > :last-child»
    for name in ("project", "chat"):
        html = (TEMPLATES / name).with_suffix(".html").read_text(encoding="utf-8")
        assert "chat-card" in html and 'id="chat-log"' in html and 'id="chat-empty"' in html, name
        assert 'class="chat-sources"' in html and "<strong>Вы:</strong>" not in html and "<strong>ИИ:</strong>" not in html, name
    project = (TEMPLATES / "project.html").read_text(encoding="utf-8")
    assert 'id="chat-expand"' in project and "chat-card--full" in project


def test_mobile_layout_rules_present():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".navbar-skkkk .navbar-actions-row" in css and "flex-wrap: wrap" in css
    assert "min-height: 36px" in css and ".btn.btn-sm { min-height: 40px" in css
    assert (TEMPLATES / "error.html").is_file()
