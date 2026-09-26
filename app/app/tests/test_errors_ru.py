"""Каждое английское detail в API имеет русский перевод в static/errors_ru.js (пункт плана 3.14)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JS = (ROOT / "app" / "app" / "static" / "errors_ru.js").read_text(encoding="utf-8")
MAP_KEYS = set(re.findall(r"^\s*'([^']+)':", JS, re.M))
# Технические detail, которые пользователь не видит (внутренние/для агентов/редкие):
TECHNICAL = {
    "acme upstream", "already on disk", "chunk too large", "size mismatch after assemble", "Upload payload missing",
    "unknown meeting track", "winner must be source or target", "Invalid action", "Invalid note path",
    "Invalid or missing MCP token", "Token not found", "Voice gate disabled", "Sortformer JSON not found for artifact",
    "Two-voice diar report is for audio artifacts only", "Speaker status is for audio artifacts only",
    "Speaker recalculate is for audio artifacts only", "Speaker match report is for audio artifacts only",
    "Speaker confirm is for audio artifacts only", "Sortformer report is for audio artifacts only",
    "Raw diarization report is for audio artifacts only", "Meeting lab is for meeting artifacts only",
}


def test_all_api_details_have_russian_text():
    used: set[str] = set()
    for base in ("routers", "services"):
        for path in (ROOT / "app" / "app" / base).glob("*.py"):
            used |= set(re.findall(r'detail="([A-Za-z][^"]*)"', path.read_text(encoding="utf-8")))
    used = {u for u in used if not re.search(r"[а-яА-ЯёЁ]", u)}  # уже по-русски — клиент показывает как есть
    missing = sorted(used - MAP_KEYS - TECHNICAL)
    assert not missing, f"Нет русского текста для: {missing}"


def test_meeting_states_have_clear_texts():
    for key in ("Meeting is not live", "Meeting ended", "Meeting has not started", "Meeting not found"):
        assert key in MAP_KEYS
