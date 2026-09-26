"""Подписи Sortformer ID → имена из sortformer_speaker_match.json."""

from __future__ import annotations

import re
from pathlib import Path

from speakers import Turn


def _normalize_label(name: str) -> str:
    return re.sub(r"\s*\(\d+%\)\s*$", "", name.strip())


def load_match_payload(path: Path) -> dict | None:
    if not path.is_file():
        return None
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _match_row(payload: dict, diar: str, canon: str) -> dict:
    matches = payload.get("matches") or {}
    return matches.get(canon) or matches.get(diar) or {}


def suggested_hint(m: dict) -> str | None:
    name = m.get("suggested_profile_name")
    score = m.get("suggested_score")
    if not name or score is None:
        return None
    pct = int(round(float(score) * 100))
    return f"{name} ({pct}%)"


def display_name_for_diar(diar: str, payload: dict) -> str:
    """В стенogramme — только подтверждённые имена; иначе Sortformer ID."""
    merge_map = payload.get("merge_map") or {}
    canon = merge_map.get(diar, diar)
    m = _match_row(payload, diar, canon)
    status = m.get("status") or "new"
    if status in ("confirmed", "merged_duplicate") and m.get("profile_name"):
        return _normalize_label(str(m["profile_name"]))
    return canon


def diar_turns_from_payload(payload: dict | None) -> list[Turn]:
    """Границы Sortformer после merge_map — те же, что в списке на тестовой странице."""
    if not payload:
        return []
    rows = payload.get("segments_merged") or payload.get("segments_labeled") or []
    merge_map = payload.get("merge_map") or {}
    turns: list[Turn] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("speaker") or row.get("diar_label") or "").strip()
        if not raw:
            continue
        start = float(row.get("start") or 0.0)
        end = float(row.get("end") or start)
        if end <= start:
            continue
        turns.append(Turn(start=start, end=end, speaker=str(merge_map.get(raw, raw)), text=""))
    return turns


def apply_match_labels(turns: list[Turn], payload: dict | None) -> list[Turn]:
    if not payload:
        return turns
    merge_map = payload.get("merge_map") or {}
    out: list[Turn] = []
    for t in turns:
        raw = t.speaker
        canon = merge_map.get(raw, raw)
        label = display_name_for_diar(canon, payload)
        if label == canon and canon != raw:
            label = display_name_for_diar(raw, payload)
        out.append(Turn(start=t.start, end=t.end, speaker=label, text=t.text))
    return out


def _canonical_speakers(payload: dict) -> list[str]:
    """Все Sortformer ID после merge_map — источник истины для таблицы имён."""
    merge_map = payload.get("merge_map") or {}
    matches = payload.get("matches") or {}
    canon_set: set[str] = set()
    if merge_map:
        canon_set.update(str(v) for v in merge_map.values())
    for label in matches:
        canon_set.add(str(merge_map.get(label, label)))
    for row in payload.get("segments_merged") or payload.get("segments_labeled") or []:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("speaker") or row.get("diar_label") or "").strip()
        if raw:
            canon_set.add(str(merge_map.get(raw, raw)))
    return sorted(canon_set)


def speaker_registry_rows(payload: dict | None) -> list[dict]:
    """Строки для UI: одна карточка на canonical Sortformer ID."""
    if not payload:
        return []
    matches = payload.get("matches") or {}
    merge_map = payload.get("merge_map") or {}
    rows: list[dict] = []
    for canon in _canonical_speakers(payload):
        labels = sorted({lbl for lbl, tgt in merge_map.items() if tgt == canon} | {canon})
        primary = next((lbl for lbl in labels if lbl in matches), labels[0])
        m = matches.get(primary) or matches.get(canon) or {}
        status = m.get("status") or "new"
        confirmed_name = m.get("profile_name") if status in ("confirmed", "merged_duplicate") else None
        rows.append(
            {
                "primary": primary,
                "labels": labels,
                "display_name": display_name_for_diar(canon, payload),
                "input_value": confirmed_name or "",
                "suggested_hint": suggested_hint(m),
                "status": status,
                "score": m.get("score") or m.get("suggested_score"),
                "clip_start": m.get("clip_start"),
                "clip_end": m.get("clip_end"),
            }
        )
    return rows
