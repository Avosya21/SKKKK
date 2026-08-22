"""Тестовая страница: Sortformer + match/запоминание голосов (Bootstrap 5)."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from speakers import format_clock, talk_time  # noqa: E402
from speaker_registry import Segment  # noqa: E402

STATUS_BADGE = {
    "confirmed": ("bg-success", "подтверждён"),
    "suggested": ("bg-warning text-dark", "подсказка"),
    "new": ("bg-secondary", "новый"),
    "merged_duplicate": ("bg-info text-dark", "дубль"),
}

BOOTSTRAP = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
FONTS = "https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;500;600&family=Poppins:wght@500;600;700&display=swap"

# Приглушённая палитра (Swiss / enterprise)
COLORS = ("#5B7FA6", "#A67B7B", "#6B9080", "#B08968", "#7C6B9E", "#9B8AA5")

TOKENS = {
    "primary": "#8B7355",
    "accent": "#5B7FA6",
    "bg": "#FAFAF9",
    "fg": "#1E293B",
    "muted": "#F1F5F9",
    "muted_fg": "#64748B",
    "border": "#E2E8F0",
    "surface": "#F4F6F8",
}

ICON_PLAY = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" '
    'viewBox="0 0 16 16" aria-hidden="true"><path d="m11.596 8.697-6.363 3.692c-.54.313-1.233-.066-1.233-.697V4.308c0-.63.692-1.01 1.233-.696l6.363 3.692a.802.802 0 0 1 0 1.393"/></svg>'
)
ICON_REFRESH = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" fill="currentColor" '
    'viewBox="0 0 16 16" aria-hidden="true"><path fill-rule="evenodd" '
    'd="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2z"/>'
    '<path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466"/></svg>'
)
ICON_CHECK = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="currentColor" '
    'viewBox="0 0 16 16" aria-hidden="true"><path d="M10.97 4.97a.75.75 0 0 1 1.07 1.05l-3.99 4.99a.75.75 0 0 1-1.08.02L4.324 8.384a.75.75 0 0 1 1.06-1.06l2.094 2.093 3.473-4.425z"/></svg>'
)
ICON_MERGE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="currentColor" '
    'viewBox="0 0 16 16" aria-hidden="true"><path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14m0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16"/>'
    '<path d="M4.646 6.646a.5.5 0 0 1 .708 0L8 9.293l2.646-2.647a.5.5 0 0 1 .708.708l-3 3a.5.5 0 0 1-.708 0l-3-3a.5.5 0 0 1 0-.708"/></svg>'
)


def _normalize_label(name: str) -> str:
    """Убрать хвост «(100%)» из подписи сегмента."""
    import re

    return re.sub(r"\s*\(\d+%\)\s*$", "", name.strip())


def _resolve_color_key(diar: str, matches: dict, merge_map: dict) -> str:
    canon = merge_map.get(diar, diar)
    m = matches.get(diar) or matches.get(canon) or {}
    pid = m.get("profile_id")
    if pid:
        return f"p{pid}"
    pname = (m.get("profile_name") or "").strip()
    if pname:
        return f"n:{pname}"
    return f"d:{canon}"


def _build_palette(keys: list[str]) -> dict[str, str]:
    def sort_key(k: str) -> tuple:
        if k.startswith("p") and k[1:].isdigit():
            return (0, int(k[1:]))
        if k.startswith("n:"):
            return (1, k[2:])
        return (2, k)

    uniq = sorted({k for k in keys if k}, key=sort_key)
    return {k: COLORS[i % len(COLORS)] for i, k in enumerate(uniq)}


def _color_map(speakers: list[str]) -> dict[str, str]:
    return {sp: COLORS[i % len(COLORS)] for i, sp in enumerate(speakers)}


def _diar_key(seg: dict) -> str:
    return str(seg.get("diar_label") or seg.get("speaker") or "")


def _badge(status: str) -> str:
    css, label = STATUS_BADGE.get(status, ("bg-secondary", status))
    return f'<span class="badge {css}">{html.escape(label)}</span>'


def _canonical_labels(matches: dict, merge_map: dict, raw_segments: list[dict]) -> list[str]:
    if matches:
        return sorted({merge_map.get(k, k) for k in matches})
    return sorted({_diar_key(s) for s in raw_segments})


def _segments_for_view(payload: dict) -> list[dict]:
    merged = payload.get("segments_merged") or []
    if not merged:
        return payload.get("segments_labeled") or []
    display_names = payload.get("display_names") or {}
    matches = payload.get("matches") or {}
    merge_map = payload.get("merge_map") or {}
    rows: list[dict] = []
    for s in merged:
        raw = str(s.get("speaker") or "")
        canon = str(merge_map.get(raw, raw))
        m = matches.get(canon) or matches.get(raw) or {}
        if m.get("status") == "confirmed" and m.get("profile_name"):
            label = _normalize_label(str(m["profile_name"]))
        else:
            raw_label = display_names.get(canon) or display_names.get(raw) or m.get("profile_name") or raw
            label = _normalize_label(str(raw_label))
        rows.append(
            {
                "start": s["start"],
                "end": s["end"],
                "speaker": label,
                "diar_label": canon,
                "color_key": _resolve_color_key(canon, matches, merge_map),
            }
        )
    return rows


def _ensure_color_keys(segments: list[dict], matches: dict, merge_map: dict) -> list[dict]:
    out: list[dict] = []
    for s in segments:
        row = dict(s)
        if not row.get("color_key"):
            diar = str(row.get("diar_label") or _diar_key(row))
            row["color_key"] = _resolve_color_key(diar, matches, merge_map)
        out.append(row)
    return out


def _speaker_card_groups(matches: dict, merge_map: dict) -> list[dict]:
    groups: list[dict] = []
    used: set[str] = set()
    by_profile: dict[int, list[str]] = {}
    for sp in sorted(matches.keys()):
        canon = merge_map.get(sp, sp)
        m = matches.get(sp) or matches.get(canon) or {}
        pid = m.get("profile_id")
        if pid and m.get("status") in ("confirmed", "merged_duplicate"):
            by_profile.setdefault(pid, []).append(sp)
    for pid in sorted(by_profile, key=lambda p: min(by_profile[p])):
        labels = sorted(set(by_profile[pid]))
        primary = labels[0]
        groups.append({"primary": primary, "labels": labels, "match": matches.get(primary, {}), "profile_id": pid})
        used.update(labels)
    for sp in sorted({merge_map.get(k, k) for k in matches}):
        if sp not in used:
            groups.append({"primary": sp, "labels": [sp], "match": matches.get(sp, {}), "profile_id": None})
    return groups


def _initials(name: str, fallback: str) -> str:
    parts = [p for p in name.replace("_", " ").split() if p]
    if not parts:
        return fallback[:2].upper()
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def _merge_target_options(primary: str, labels: list[str], groups: list[dict]) -> str:
    """Select других спикеров для ручного объединения."""
    own = set(labels)
    opts: list[str] = ['<option value="">— выбрать —</option>']
    for g in groups:
        tgt = g["primary"]
        if tgt == primary or tgt in own:
            continue
        m = g.get("match") or {}
        pname = (m.get("profile_name") or "").strip()
        title = f"{pname} ({tgt})" if pname else tgt
        opts.append(f'<option value="{html.escape(tgt, quote=True)}">{html.escape(title)}</option>')
    if len(opts) <= 1:
        return ""
    return "".join(opts)


def _speaker_cards(
    groups: list[dict], palette: dict[str, str], matches: dict, merge_map: dict
) -> str:
    parts: list[str] = []
    for group in groups:
        sp = group["primary"]
        labels = group["labels"]
        m = group["match"]
        pid = group.get("profile_id")
        ck = f"p{pid}" if pid else _resolve_color_key(sp, matches, merge_map)
        color = palette.get(ck, TOKENS["muted_fg"])
        st = m.get("status") or "new"
        pname = (m.get("profile_name") or "").strip()
        score = m.get("score")
        cs, ce = m.get("clip_start"), m.get("clip_end")
        inp_id = f"name-{sp.replace('_', '-')}"
        headline = html.escape(pname) if pname else html.escape(sp)
        enroll_btn = "Обновить" if st == "confirmed" else "Запомнить"
        labels_txt = html.escape(" · ".join(labels))
        merge_note = (
            f'<p class="speaker-merge mb-2"><span class="badge rounded-pill text-bg-light border">'
            f"Объединено: {labels_txt}</span></p>"
            if len(labels) > 1
            else f'<p class="speaker-id mb-2 font-monospace">{labels_txt}</p>'
        )
        score_txt = (
            f'<p class="small text-muted mb-2">Совпадение: <strong>{int(score * 100)}%</strong></p>'
            if score is not None and st != "confirmed"
            else ""
        )
        confirmed_icon = f'<span class="speaker-check" aria-hidden="true">{ICON_CHECK}</span>' if st == "confirmed" else ""
        merge_opts = _merge_target_options(sp, labels, groups)
        merge_block = ""
        if merge_opts and len(labels) == 1:
            merge_block = (
                f'<div class="merge-row mt-2 pt-2 border-top">'
                f'<label class="form-label small text-muted mb-1">Объединить с</label>'
                f'<div class="input-group input-group-sm">'
                f'<select class="form-select form-select-sm merge-target" aria-label="Спикер для объединения">'
                f"{merge_opts}</select>"
                f'<button type="button" class="btn btn-soft merge-btn" title="Объединить спикеров">'
                f'{ICON_MERGE}<span class="ms-1">Объединить</span></button>'
                f"</div></div>"
            )
        parts.append(
            f'<div class="col-md-6">'
            f'<article class="speaker-card h-100" data-speaker="{html.escape(sp, quote=True)}" '
            f'data-labels="{html.escape(",".join(labels), quote=True)}" style="--speaker-color:{color}">'
            f'<div class="speaker-card-inner h-100 d-flex flex-column">'
            f'<div class="d-flex align-items-start gap-3 mb-3">'
            f'<div class="speaker-avatar" aria-hidden="true">{html.escape(_initials(pname or sp, sp))}</div>'
            f'<div class="flex-grow-1 min-w-0">'
            f'<div class="d-flex justify-content-between align-items-start gap-2">'
            f'<h3 class="speaker-name h6 mb-0">{headline}</h3>'
            f"{_badge(st)}"
            f"</div>"
            f"{merge_note}"
            f"{score_txt}"
            f"</div></div>"
            f'<p class="speaker-clip small mb-3">'
            f'<span class="text-muted">Эталон</span> '
            f"{format_clock(cs or 0)}–{format_clock(ce or 0)} "
            f'<span class="text-muted">({m.get("clip_duration", 0):.1f} с)</span>'
            f"</p>"
            f'<button type="button" class="btn btn-sm btn-soft play-clip mb-2 align-self-start" '
            f'data-start="{cs}" data-end="{ce}" aria-label="Прослушать эталонный клип">'
            f'{ICON_PLAY}<span class="ms-1">Прослушать</span></button>'
            f'<div class="mt-auto pt-2 border-top">'
            f'<label class="form-label small fw-medium mb-1" for="{inp_id}">Имя спикера</label>'
            f'<div class="input-group input-group-sm">'
            f'<input id="{inp_id}" class="form-control name-inp" type="text" autocomplete="name" '
            f'placeholder="Иван Петров" value="{html.escape(pname)}" />'
            f'<button type="button" class="btn btn-soft-accent enroll">{enroll_btn}</button>'
            f"</div></div>"
            f"{merge_block}"
            f"{confirmed_icon}"
            f"</div></article></div>"
        )
    return "".join(parts)


def _stat_metrics(stats: list[dict], display_to_color: dict[str, str]) -> str:
    cards: list[str] = []
    for r in stats:
        color = display_to_color.get(r["speaker"], TOKENS["muted_fg"])
        pct = int(round(r["share"] * 100))
        name = html.escape(r["speaker"])
        secs = format_clock(r["seconds"])
        cards.append(
            f'<div class="col-6 col-md-4">'
            f'<div class="metric-card h-100" style="--metric-color:{color}">'
            f'<div class="metric-dot" aria-hidden="true"></div>'
            f'<div class="metric-label text-truncate" title="{name}">{name}</div>'
            f'<div class="metric-value font-monospace">{secs}</div>'
            f'<div class="metric-share">{pct}%</div>'
            f"</div></div>"
        )
    return "".join(cards)


def _timeline_legend(stats: list[dict], display_to_color: dict[str, str]) -> str:
    chips: list[str] = []
    for r in stats:
        color = display_to_color.get(r["speaker"], TOKENS["muted_fg"])
        name = html.escape(r["speaker"])
        chips.append(
            f'<span class="tl-chip"><span class="tl-chip-dot" style="background:{color}"></span>{name}</span>'
        )
    return "".join(chips)


def _time_axis(duration: float) -> str:
    if duration <= 0:
        return ""
    marks = [0.0, duration * 0.25, duration * 0.5, duration * 0.75, duration]
    return "".join(
        f'<span class="tl-axis-mark" style="left:{100.0 * t / duration:.1f}%">'
        f"{format_clock(t)}</span>"
        for t in marks
    )


def render_html(*, title: str, audio_url: str, payload: dict, profiles: list[dict]) -> str:
    matches = payload.get("matches") or {}
    merge_map = payload.get("merge_map") or {}
    raw_segments = _ensure_color_keys(_segments_for_view(payload), matches, merge_map)
    card_groups = _speaker_card_groups(matches, merge_map)

    color_keys: list[str] = []
    for g in card_groups:
        pid = g.get("profile_id")
        if pid:
            color_keys.append(f"p{pid}")
        else:
            color_keys.append(_resolve_color_key(g["primary"], matches, merge_map))
    for s in raw_segments:
        color_keys.append(s.get("color_key", ""))
    palette = _build_palette(color_keys)

    turns = [Segment(float(s["start"]), float(s["end"]), str(s.get("speaker") or "")) for s in raw_segments]
    seg_color_keys = [s.get("color_key", "") for s in raw_segments]

    stats = talk_time(turns)
    duration = max((t.end for t in turns), default=1.0)
    display_to_color: dict[str, str] = {}
    for s in raw_segments:
        display_to_color[str(s.get("speaker") or "")] = palette.get(s.get("color_key", ""), TOKENS["muted_fg"])
    stat_rows = "".join(
        f"<tr><td><span class='sp-dot' style='background:{display_to_color.get(r['speaker'], TOKENS['muted_fg'])}'></span>"
        f"{html.escape(r['speaker'])}</td>"
        f"<td class='text-end font-monospace'>{format_clock(r['seconds'])}</td>"
        f"<td class='text-end'>{int(round(r['share'] * 100))}%</td></tr>"
        for r in stats
    )
    stat_metrics = _stat_metrics(stats, display_to_color)
    tl_legend = _timeline_legend(stats, display_to_color)
    time_axis = _time_axis(duration)
    timeline = []
    for i, turn in enumerate(turns):
        left = 100.0 * turn.start / duration
        width = max(0.12, 100.0 * (turn.end - turn.start) / duration)
        ck = seg_color_keys[i] if i < len(seg_color_keys) else ""
        color = palette.get(ck, "#94a3b8")
        timeline.append(
            f"<div class='tl-bar' style='left:{left:.3f}%;width:{width:.3f}%;background:{color}' "
            f"title='{html.escape(turn.speaker)}'></div>"
        )

    seg_items = []
    for i, turn in enumerate(turns):
        ck = seg_color_keys[i] if i < len(seg_color_keys) else ""
        color = palette.get(ck, TOKENS["muted_fg"])
        seg_items.append(
            f"<button type='button' class='list-group-item list-group-item-action seg py-2 px-3' "
            f"data-i='{i}' data-start='{turn.start:.3f}' data-end='{turn.end:.3f}' "
            f"aria-label='Перейти к {html.escape(turn.speaker)} {format_clock(turn.start)}'>"
            f"<div class='d-flex w-100 justify-content-between align-items-center gap-3'>"
            f"<span class='seg-speaker fw-semibold' style='--seg-color:{color}'>"
            f'<span class="seg-dot" aria-hidden="true"></span>{html.escape(turn.speaker)}</span>'
            f"<span class='small text-muted font-monospace text-nowrap'>"
            f"{format_clock(turn.start)}–{format_clock(turn.end)}</span>"
            f"</div></button>"
        )

    prof_rows = "".join(
        f"<tr><td class='fw-medium'>{html.escape(p['display_name'])}</td>"
        f"<td><span class='badge rounded-pill profile-scope'>{html.escape(p['scope'])}</span></td>"
        f"<td class='text-muted font-monospace small'>{p['id']}</td></tr>"
        for p in profiles
    ) or "<tr><td colspan='3' class='text-muted fst-italic py-4 text-center'>Пока пусто</td></tr>"

    confirmed_n = len({g["profile_id"] for g in card_groups if g.get("profile_id")})
    total_n = len(card_groups)
    progress_pct = int(round(100 * confirmed_n / total_n)) if total_n else 0

    public_payload = {
        "artifact_id": payload.get("artifact_id"),
        "project_id": payload.get("project_id"),
        "matches": {k: {kk: vv for kk, vv in v.items() if kk != "embedding"} for k, v in matches.items()},
    }
    data_json = json.dumps(public_payload, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — Primary Analytics</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="{FONTS}" rel="stylesheet">
  <link href="{BOOTSTRAP}" rel="stylesheet">
  <style>
    :root {{
      --color-primary: {TOKENS["primary"]};
      --color-accent: {TOKENS["accent"]};
      --color-bg: {TOKENS["bg"]};
      --color-fg: {TOKENS["fg"]};
      --color-muted: {TOKENS["muted"]};
      --color-muted-fg: {TOKENS["muted_fg"]};
      --color-border: {TOKENS["border"]};
      --color-surface: {TOKENS["surface"]};
      --font-display: "Poppins", system-ui, sans-serif;
      --font-body: "Open Sans", system-ui, sans-serif;
      --radius: .75rem;
      --shadow-sm: 0 1px 2px rgba(15,23,42,.06);
      --shadow-md: 0 4px 16px rgba(15,23,42,.08);
      --transition: 200ms ease;
    }}
    body {{
      font-family: var(--font-body);
      color: var(--color-fg);
      background: var(--color-surface);
      line-height: 1.5;
    }}
    h1, h2, h3, .navbar-brand, .page-title {{
      font-family: var(--font-display);
    }}
    .app-nav {{
      background: linear-gradient(180deg, #1a2332 0%, var(--color-fg) 100%) !important;
      border-bottom: 1px solid rgba(255,255,255,.08);
    }}
    .page-hero {{
      background: var(--color-bg);
      border: 1px solid var(--color-border);
      border-radius: var(--radius);
      box-shadow: var(--shadow-sm);
    }}
    .progress-enroll {{
      height: .5rem;
      background: var(--color-muted);
      border-radius: 999px;
      overflow: hidden;
    }}
    .progress-enroll .progress-bar {{
      background: var(--color-accent);
      transition: width var(--transition);
    }}
    .panel {{
      background: var(--color-bg);
      border: 1px solid var(--color-border);
      border-radius: var(--radius);
      box-shadow: var(--shadow-sm);
    }}
    .panel-header {{
      padding: 1rem 1.25rem;
      border-bottom: 1px solid var(--color-border);
      font-family: var(--font-display);
      font-weight: 600;
      font-size: .95rem;
    }}
    .panel-body {{ padding: 1.25rem; }}
    .audio-panel {{
      border-left: 4px solid var(--color-accent);
    }}
    .nav-pill-link {{
      font-size: .8125rem;
      border-radius: 999px;
      padding: .35rem .85rem;
      border: 1px solid var(--color-border);
      color: var(--color-muted-fg);
      text-decoration: none;
      transition: all var(--transition);
    }}
    .nav-pill-link:hover {{
      border-color: var(--color-accent);
      color: var(--color-accent);
      background: color-mix(in srgb, var(--color-accent) 8%, white);
    }}
    .btn-soft {{
      background: var(--color-muted);
      border: 1px solid var(--color-border);
      color: var(--color-fg);
      font-weight: 500;
      transition: all var(--transition);
    }}
    .btn-soft:hover {{
      background: color-mix(in srgb, var(--color-accent) 10%, white);
      border-color: color-mix(in srgb, var(--color-accent) 35%, var(--color-border));
      color: var(--color-accent);
    }}
    .btn-soft-accent {{
      background: color-mix(in srgb, var(--color-accent) 14%, white);
      border: 1px solid color-mix(in srgb, var(--color-accent) 30%, var(--color-border));
      color: var(--color-accent);
      font-weight: 600;
    }}
    .btn-soft-accent:hover {{
      background: color-mix(in srgb, var(--color-accent) 22%, white);
      color: var(--color-accent);
    }}
    .btn-accent {{
      background: color-mix(in srgb, var(--color-accent) 88%, #1e293b);
      border-color: transparent;
      color: #fff;
      font-weight: 600;
      transition: all var(--transition);
    }}
    .btn-accent:hover {{ filter: brightness(1.06); color: #fff; }}
    .merge-row .form-select {{ max-width: 100%; }}
    .speaker-card {{
      --speaker-color: var(--color-muted-fg);
      border-radius: var(--radius);
      transition: box-shadow var(--transition), transform var(--transition);
    }}
    .speaker-card-inner {{
      background: var(--color-bg);
      border: 1px solid var(--color-border);
      border-radius: var(--radius);
      padding: 1.25rem;
      border-top: 3px solid var(--speaker-color);
      position: relative;
    }}
    .speaker-card:hover .speaker-card-inner {{
      box-shadow: var(--shadow-md);
    }}
    .speaker-avatar {{
      width: 2.75rem;
      height: 2.75rem;
      border-radius: 50%;
      background: color-mix(in srgb, var(--speaker-color) 12%, white);
      color: var(--speaker-color);
      font-family: var(--font-display);
      font-weight: 700;
      font-size: .85rem;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}
    .speaker-name {{ color: var(--color-fg); }}
    .speaker-id {{ color: var(--color-muted-fg); font-size: .75rem; }}
    .speaker-check {{
      position: absolute;
      top: .75rem;
      right: .75rem;
      color: #6B9080;
    }}
    .metric-card {{
      --metric-color: var(--color-muted-fg);
      background: var(--color-muted);
      border: 1px solid var(--color-border);
      border-radius: var(--radius);
      padding: .85rem 1rem;
      position: relative;
      overflow: hidden;
    }}
    .metric-card::before {{
      content: "";
      position: absolute;
      left: 0; top: 0; bottom: 0;
      width: 3px;
      background: var(--metric-color);
    }}
    .metric-dot {{
      width: .5rem; height: .5rem;
      border-radius: 50%;
      background: var(--metric-color);
      display: inline-block;
      margin-right: .35rem;
    }}
    .metric-label {{ font-size: .8125rem; font-weight: 600; margin-bottom: .15rem; }}
    .metric-value {{ font-size: 1.1rem; font-weight: 600; }}
    .metric-share {{ font-size: .75rem; color: var(--color-muted-fg); }}
    .tl-legend {{ display: flex; flex-wrap: wrap; gap: .5rem .75rem; margin-bottom: .75rem; }}
    .tl-chip {{
      font-size: .8125rem;
      color: var(--color-muted-fg);
      display: inline-flex;
      align-items: center;
      gap: .35rem;
    }}
    .tl-chip-dot {{
      width: .55rem; height: .55rem;
      border-radius: 50%;
      flex-shrink: 0;
    }}
    .tl-wrap {{ position: relative; margin-bottom: .35rem; }}
    .tl-track {{
      position: relative;
      height: 2.75rem;
      background: var(--color-muted);
      border: 1px solid var(--color-border);
      border-radius: var(--radius);
      overflow: hidden;
    }}
    .tl-bar {{
      position: absolute; top: 0; bottom: 0;
      min-width: 1px;
      opacity: .88;
      transition: opacity var(--transition);
    }}
    .tl-bar:hover {{ opacity: 1; }}
    .tl-axis {{
      position: relative;
      height: 1.25rem;
      font-size: .6875rem;
      color: var(--color-muted-fg);
      font-family: ui-monospace, monospace;
    }}
    .tl-axis-mark {{
      position: absolute;
      transform: translateX(-50%);
      white-space: nowrap;
    }}
    .seg-list {{
      max-height: min(52vh, 560px);
      overflow-y: auto;
      border: 1px solid var(--color-border);
      border-radius: var(--radius);
    }}
    .seg {{
      cursor: pointer;
      border-color: var(--color-border) !important;
      transition: background var(--transition);
    }}
    .seg:hover {{ background: var(--color-muted) !important; }}
    .seg.active {{
      background: color-mix(in srgb, var(--color-accent) 10%, white) !important;
      border-left: 3px solid var(--color-accent) !important;
    }}
    .seg-speaker {{ color: var(--seg-color); display: inline-flex; align-items: center; gap: .4rem; }}
    .seg-dot {{
      width: .45rem; height: .45rem;
      border-radius: 50%;
      background: var(--seg-color);
      flex-shrink: 0;
    }}
    .workflow-step {{
      display: flex;
      gap: .75rem;
      padding: .85rem 1.25rem;
      border-bottom: 1px solid var(--color-border);
    }}
    .workflow-step:last-child {{ border-bottom: 0; }}
    .workflow-num {{
      width: 1.5rem; height: 1.5rem;
      border-radius: 50%;
      background: var(--color-muted);
      color: var(--color-accent);
      font-family: var(--font-display);
      font-weight: 700;
      font-size: .75rem;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}
    .profile-scope {{
      background: var(--color-muted);
      color: var(--color-muted-fg);
      font-weight: 500;
      font-size: .7rem;
    }}
    .sidebar-sticky {{ position: sticky; top: 1rem; }}
    .play-clip, .seg, .enroll, .merge-btn, #recalc, .nav-pill-link {{ cursor: pointer; }}
    .play-clip:focus-visible, .seg:focus-visible, .enroll:focus-visible, .merge-btn:focus-visible, #recalc:focus-visible, .nav-pill-link:focus-visible {{
      outline: 2px solid var(--color-accent);
      outline-offset: 2px;
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{
        animation-duration: 0.01ms !important;
        transition-duration: 0.01ms !important;
      }}
    }}
    @media (max-width: 767.98px) {{
      .page-hero {{ padding: 1.25rem !important; }}
    }}
  </style>
</head>
<body>
<nav class="navbar navbar-dark app-nav mb-0">
  <div class="container-fluid px-3 px-lg-4" style="max-width: 1320px">
    <a class="navbar-brand fw-semibold" href="/">Primary Analytics</a>
    <span class="navbar-text text-white-50 small d-none d-sm-inline">Speaker Match · Sortformer</span>
  </div>
</nav>

<main class="container-fluid px-3 px-lg-4 py-4 py-lg-5" style="max-width: 1320px">
  <header class="page-hero p-4 p-lg-5 mb-4">
    <div class="row align-items-center g-4">
      <div class="col-lg-8">
        <p class="text-uppercase small fw-semibold mb-2" style="color:var(--color-primary);letter-spacing:.06em">Диаризация и база голосов</p>
        <h1 class="page-title h3 mb-2">{html.escape(title)}</h1>
        <p class="mb-0" style="color:var(--color-muted-fg);max-width:42rem">
          Sortformer определяет спикеров, WeSpeaker сопоставляет с базой проекта.
          Подпишите голоса — таймлайн и сегменты покажут имена.
        </p>
      </div>
      <div class="col-lg-4">
        <div class="text-lg-end">
          <div class="small fw-medium mb-2" style="color:var(--color-muted-fg)">Прогресс подписи</div>
          <div class="d-flex align-items-center justify-content-lg-end gap-3 mb-2">
            <span class="h4 mb-0 font-monospace">{confirmed_n}<span class="text-muted fs-6">/{total_n}</span></span>
            <span class="badge rounded-pill text-bg-light border">{progress_pct}%</span>
          </div>
          <div class="progress-enroll" role="progressbar" aria-valuenow="{progress_pct}" aria-valuemin="0" aria-valuemax="100">
            <div class="progress-bar" style="width:{progress_pct}%"></div>
          </div>
        </div>
      </div>
    </div>
  </header>

  <div class="row g-4">
    <div class="col-lg-8 d-flex flex-column gap-4">
      <section class="panel audio-panel" aria-labelledby="audio-heading">
        <div class="panel-header" id="audio-heading">Аудио и пересчёт</div>
        <div class="panel-body">
          <nav class="mb-4 d-flex flex-wrap gap-2" aria-label="Связанные отчёты">
            <a class="nav-pill-link" href="report-sortformer">Sortformer</a>
            <a class="nav-pill-link" href="report-diar-raw">Сырая диаризация</a>
            <a class="nav-pill-link" href="report">PyAnnote</a>
            <a class="nav-pill-link" href="report">← Стенограмма</a>
          </nav>
          <audio id="player" class="w-100 mb-4" controls preload="metadata" playsinline
            src="{html.escape(audio_url, quote=True)}">Ваш браузер не поддерживает аудио.</audio>
          <div class="d-flex flex-wrap align-items-center gap-3">
            <button id="recalc" type="button" class="btn btn-accent d-inline-flex align-items-center gap-2">
              {ICON_REFRESH}<span>Обновить и пересчитать</span>
            </button>
            <span class="small" style="color:var(--color-muted-fg)">~20 с · Docker WeSpeaker</span>
          </div>
        </div>
      </section>

      <section class="panel" aria-labelledby="speakers-heading">
        <div class="panel-header d-flex justify-content-between align-items-center" id="speakers-heading">
          <span>Голоса Sortformer</span>
          <span class="badge rounded-pill text-bg-light border fw-normal">{total_n} ID</span>
        </div>
        <div class="panel-body">
          <div class="row g-3">{_speaker_cards(card_groups, palette, matches, merge_map)}</div>
        </div>
      </section>

      <section class="panel" aria-labelledby="timeline-heading">
        <div class="panel-header p-0 border-0">
          <ul class="nav nav-tabs border-0 px-3 pt-2" role="tablist">
            <li class="nav-item"><button class="nav-link active fw-medium" data-bs-toggle="tab"
              data-bs-target="#tab-timeline" type="button" role="tab">Таймлайн</button></li>
            <li class="nav-item"><button class="nav-link fw-medium" data-bs-toggle="tab"
              data-bs-target="#tab-segs" type="button" role="tab">Сегменты ({len(seg_items)})</button></li>
          </ul>
        </div>
        <div class="panel-body tab-content pt-3">
          <div class="tab-pane fade show active" id="tab-timeline" role="tabpanel">
            <div class="tl-legend" aria-hidden="false">{tl_legend}</div>
            <div class="tl-wrap">
              <div class="tl-track" role="img" aria-label="Распределение голосов по времени">{''.join(timeline)}</div>
              <div class="tl-axis">{time_axis}</div>
            </div>
            <div class="row g-2 g-md-3 mt-3">{stat_metrics}</div>
            <details class="mt-3">
              <summary class="small" style="color:var(--color-muted-fg);cursor:pointer">Таблица времени</summary>
              <div class="table-responsive mt-2">
                <table class="table table-sm table-hover align-middle mb-0">
                  <thead><tr><th>Голос</th><th class="text-end">Время</th><th class="text-end">Доля</th></tr></thead>
                  <tbody>{stat_rows}</tbody>
                </table>
              </div>
            </details>
          </div>
          <div class="tab-pane fade" id="tab-segs" role="tabpanel">
            <div class="list-group list-group-flush seg-list">{''.join(seg_items)}</div>
          </div>
        </div>
      </section>
    </div>

    <aside class="col-lg-4">
      <div class="sidebar-sticky d-flex flex-column gap-4">
        <section class="panel" aria-labelledby="workflow-heading">
          <div class="panel-header" id="workflow-heading">Как работать</div>
          <div>
            <div class="workflow-step"><span class="workflow-num">1</span><span>Прослушайте эталонный клип каждого спикера</span></div>
            <div class="workflow-step"><span class="workflow-num">2</span><span>Введите имя и нажмите «Запомнить»</span></div>
            <div class="workflow-step"><span class="workflow-num">3</span><span>Когда все подписаны — «Обновить и пересчитать»</span></div>
          </div>
        </section>

        <section class="panel" aria-labelledby="db-heading">
          <div class="panel-header d-flex justify-content-between align-items-center" id="db-heading">
            <span>База проекта</span>
            <span class="badge rounded-pill text-bg-light border">{len(profiles)}</span>
          </div>
          <div class="table-responsive">
            <table class="table table-sm table-hover mb-0 align-middle">
              <thead><tr><th>Имя</th><th scope="col">scope</th><th class="text-end">id</th></tr></thead>
              <tbody>{prof_rows}</tbody>
            </table>
          </div>
        </section>
      </div>
    </aside>
  </div>
</main>

<div id="toast" class="toast align-items-center text-bg-dark border-0 position-fixed bottom-0 end-0 m-3" role="alert" aria-live="assertive" aria-atomic="true">
  <div class="d-flex"><div class="toast-body" id="toast-body"></div></div>
</div>

<script id="data" type="application/json">{data_json}</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
const data = JSON.parse(document.getElementById("data").textContent);
const player = document.getElementById("player");
const toastEl = document.getElementById("toast");
const toastBody = document.getElementById("toast-body");
const bsToast = bootstrap.Toast.getOrCreateInstance(toastEl, {{ delay: 3500 }});
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function showToast(msg) {{
  toastBody.textContent = msg;
  bsToast.show();
}}
function seek(sec) {{
  const t = Number(sec);
  if (!Number.isFinite(t)) return;
  const go = () => {{
    try {{ player.currentTime = t; }} catch (_) {{}}
    if (!reducedMotion) {{
      const p = player.play();
      if (p && typeof p.catch === "function") p.catch(() => {{}});
    }}
  }};
  if (player.readyState >= 1) {{
    go();
    return;
  }}
  player.addEventListener("loadedmetadata", go, {{ once: true }});
  try {{ player.load(); }} catch (_) {{}}
}}

document.querySelectorAll(".play-clip").forEach(btn => btn.addEventListener("click", () => seek(Number(btn.dataset.start))));
document.querySelectorAll(".seg").forEach(el => el.addEventListener("click", () => seek(Number(el.dataset.start))));
player.addEventListener("timeupdate", () => {{
  const t = player.currentTime;
  document.querySelectorAll(".seg").forEach(el => {{
    const a = Number(el.dataset.start), b = Number(el.dataset.end);
    el.classList.toggle("active", t >= a && t < b);
  }});
}});

document.querySelectorAll(".speaker-card .enroll").forEach(btn => {{
  btn.addEventListener("click", async () => {{
    const card = btn.closest(".speaker-card");
    const diar = card.dataset.speaker;
    const inp = card.querySelector(".name-inp");
    const name = inp.value.trim();
    if (!name) {{ showToast("Введите имя"); inp.focus(); return; }}
    btn.disabled = true;
    const prev = btn.innerHTML;
    btn.textContent = "…";
    try {{
      const res = await fetch(`/api/artifacts/${{data.artifact_id}}/speaker-enroll`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json;charset=utf-8" }},
        body: JSON.stringify({{ diar_label: diar, display_name: name }}),
      }});
      const text = await res.text();
      if (!res.ok) {{
        let detail = text;
        try {{ detail = JSON.parse(text).detail || text; }} catch (_) {{}}
        throw new Error(detail);
      }}
      showToast("Сохранено: " + name);
      location.reload();
    }} catch (err) {{
      showToast("Ошибка: " + err.message);
      btn.disabled = false;
      btn.innerHTML = prev;
    }}
  }});
}});

document.querySelectorAll(".speaker-card .merge-btn").forEach(btn => {{
  btn.addEventListener("click", async () => {{
    const card = btn.closest(".speaker-card");
    const source = card.dataset.speaker;
    const sel = card.querySelector(".merge-target");
    const target = sel ? sel.value : "";
    if (!target) {{ showToast("Выберите спикера для объединения"); sel?.focus(); return; }}
    if (!confirm(`Объединить ${{source}} с ${{target}}? Оба ID будут считаться одним человеком.`)) return;
    btn.disabled = true;
    const prev = btn.innerHTML;
    btn.textContent = "…";
    try {{
      const res = await fetch(`/api/artifacts/${{data.artifact_id}}/speaker-merge`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json;charset=utf-8" }},
        body: JSON.stringify({{ source_diar_label: source, target_diar_label: target }}),
      }});
      const text = await res.text();
      if (!res.ok) {{
        let detail = text;
        try {{ detail = JSON.parse(text).detail || text; }} catch (_) {{}}
        if (res.status === 404 && detail === "Not Found") {{
          detail = "endpoint не найден — перезапустите API (scripts\\run_api.ps1)";
        }}
        throw new Error(detail);
      }}
      location.reload();
    }} catch (err) {{
      showToast("Ошибка: " + err.message);
      btn.disabled = false;
      btn.innerHTML = prev;
    }}
  }});
}});

document.getElementById("recalc").addEventListener("click", async () => {{
  const btn = document.getElementById("recalc");
  btn.disabled = true;
  const label = btn.querySelector("span");
  const prev = label ? label.textContent : btn.textContent;
  if (label) label.textContent = "Считаю…";
  else btn.textContent = "Считаю…";
  try {{
    const res = await fetch(`/api/artifacts/${{data.artifact_id}}/speaker-recalculate`, {{ method: "POST" }});
    const text = await res.text();
    if (!res.ok) {{
      let detail = text;
      try {{ detail = JSON.parse(text).detail || text; }} catch (_) {{}}
      throw new Error(detail);
    }}
    showToast("Готово — обновляем страницу");
    location.reload();
  }} catch (err) {{
    showToast("Ошибка: " + err.message);
    btn.disabled = false;
    if (label) label.textContent = prev;
    else btn.textContent = prev;
  }}
}});
</script>
</body>
</html>
"""


def build(*, match_json: Path, out_html: Path, audio_url: str, profiles: list[dict], title: str) -> None:
    payload = json.loads(match_json.read_text(encoding="utf-8"))
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(
        render_html(title=title, audio_url=audio_url, payload=payload, profiles=profiles),
        encoding="utf-8",
    )
    print("wrote", out_html)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--match-json",
        type=Path,
        default=ROOT
        / "data/projects/1/artifacts/art_2026-08-19_211842_54f88c/reports/sortformer_speaker_match.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT
        / "data/projects/1/artifacts/art_2026-08-19_211842_54f88c/reports/speaker_match_test.html",
    )
    parser.add_argument(
        "--audio-url",
        default="/artifacts/art_2026-08-19_211842_54f88c/audio",
    )
    args = parser.parse_args()
    build(
        match_json=args.match_json,
        out_html=args.out,
        audio_url=args.audio_url,
        profiles=[],
        title="Sortformer + запоминание голосов",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
