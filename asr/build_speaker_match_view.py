"""Страница голосов созвона: диаризация + match/запоминание голосов (Bootstrap 5).

Какая модель разметила спикеров (Nemotron 3 до 8 голосов или Sortformer 4spk
до 4), сколько говорил каждый — из ``lab/runs/{run_id}/03_diar/diar.json``
того прогона, на который ссылается match JSON. Старые прогоны без поля
``model_key`` распознаются по имени файла модели.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from speakers import format_clock  # noqa: E402

STATUS_BADGE = {
    "confirmed": ("bg-success", "подтверждён"),
    "suggested": ("bg-warning text-dark", "подсказка"),
    "new": ("bg-secondary", "новый"),
    "merged_duplicate": ("bg-info text-dark", "дубль"),
    "check": ("bg-warning text-dark", "проверьте"),
}

BOOTSTRAP = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
FONTS = "https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;500;600&family=Poppins:wght@500;600;700&display=swap"

# Приглушённая палитра (Swiss / enterprise); 8 цветов — Nemotron даёт до 8 спикеров
COLORS = ("#5B7FA6", "#A67B7B", "#6B9080", "#B08968", "#7C6B9E", "#4F8FA0", "#A6906B", "#9B8AA5")

# Меньше этого — слепок голоса ненадёжен: человек реальный, но подсказку имени проверять на слух.
LOW_SPEECH_SEC = 15.0
SHORT_CLIP_SEC = 8.0

DIAR_MODELS = {
    "nemotron3": {"label": "Nemotron 3 Diarization", "short": "Nemotron 3", "max": 8},
    "sortformer4": {"label": "Sortformer 4spk v2.1", "short": "Sortformer", "max": 4},
    "native_track": {"label": "дорожка участника комнаты", "short": "дорожка", "max": 1},
}

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


def _resolve_color_key(diar: str, matches: dict, merge_map: dict) -> str:
    m = matches.get(diar) or matches.get(merge_map.get(diar, diar), {}) or {}
    pid = m.get("profile_id")
    if pid:
        return f"p{pid}"
    pname = (m.get("profile_name") or "").strip()
    if pname:
        return f"n:{pname}"
    return f"d:{diar}"


def _build_palette(keys: list[str]) -> dict[str, str]:
    def sort_key(k: str) -> tuple:
        if k.startswith("p") and k[1:].isdigit():
            return (0, int(k[1:]))
        if k.startswith("n:"):
            return (1, k[2:])
        return (2, k)

    uniq = sorted({k for k in keys if k}, key=sort_key)
    return {k: COLORS[i % len(COLORS)] for i, k in enumerate(uniq)}


def diar_info(match_json: Path, payload: dict) -> dict | None:
    """Модель и время речи из diar.json прогона. None — прогона нет (старый контур)."""
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    diar_path = match_json.parent.parent / "lab" / "runs" / run_id / "03_diar" / "diar.json"
    try:
        doc = json.loads(diar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    key = str(doc.get("model_key") or "").strip()
    if not key:
        model = str(doc.get("model") or "").lower()
        key = "native_track" if model == "native_track" else "nemotron3" if "nemotron" in model else "sortformer4"
    spec = DIAR_MODELS.get(key, {"label": str(doc.get("model") or key), "short": key, "max": None})
    seconds = {
        str(row.get("speaker")): float(row.get("seconds") or 0.0)
        for row in doc.get("speakers") or []
        if row.get("speaker")
    }
    return {
        "key": key,
        "label": spec["label"],
        "short": spec["short"],
        "max_speakers": int(doc.get("max_speakers") or spec["max"] or 0) or None,
        "seconds": seconds,
        "found": len(seconds),
    }


def _speech_text(seconds: float, total: float) -> str:
    minutes, sec = divmod(int(round(seconds)), 60)
    spoken = f"{minutes} мин {sec:02d} с" if minutes else f"{sec} с"
    ratio = seconds / total if total > 0 else 0.0
    share = (" · <1%" if ratio < 0.005 else f" · {ratio:.0%}") if total > 0 else ""
    return f"{spoken}{share}"


def _diar_summary(info: dict | None) -> str:
    """Строка под заголовком: чья разметка, сколько голосов, упёрлась ли модель в потолок."""
    if not info:
        return ""
    found, cap = info["found"], info.get("max_speakers")
    text = f"Кто говорит: <strong>{html.escape(info['label'])}</strong>"
    if cap and info["key"] != "native_track":
        text += f", до {cap} голосов"
    text += f". Найдено голосов: <strong>{found}</strong>."
    out = f'<p class="diar-summary small mt-3 mb-0">{text}</p>'
    if info["key"] == "sortformer4" and cap and found >= cap:
        out += (
            '<div class="alert alert-warning small mt-3 mb-0 py-2" role="status">'
            f"Заняты все {cap} канала Sortformer: людей на созвоне могло быть больше, и тогда их голоса "
            "слиты с этими. Пересчёт на Nemotron 3 разделит до 8 голосов."
            "</div>"
        )
    return out


def _badge(status: str) -> str:
    css, label = STATUS_BADGE.get(status, ("bg-secondary", status))
    return f'<span class="badge {css}">{html.escape(label)}</span>'


def _speaker_card_groups(matches: dict, merge_map: dict) -> list[dict]:
    """Одна карточка на каждый speaker_N диаризации — не скрывать voice-merge alias."""
    groups: list[dict] = []
    for sp in sorted(matches.keys()):
        m = matches.get(sp) or {}
        canon = merge_map.get(sp, sp)
        groups.append(
            {
                "primary": sp,
                "labels": [sp],
                "match": m,
                "profile_id": m.get("profile_id"),
                "voice_merge_target": canon if canon != sp else None,
            }
        )
    return groups


def _initials(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[()\[\]«»\"'`]", " ", name.replace("_", " "))
    parts = [p for p in cleaned.split() if p]
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
    groups: list[dict],
    palette: dict[str, str],
    matches: dict,
    merge_map: dict,
    speech: dict[str, float] | None = None,
) -> str:
    """Карточка на каждый speaker_N; ``speech`` — секунды речи из diar.json (если прогон найден)."""
    speech = speech or {}
    total_speech = sum(speech.values())
    parts: list[str] = []
    for group in groups:
        sp = group["primary"]
        labels = group["labels"]
        m = group["match"]
        pid = group.get("profile_id")
        voice_merge_target = group.get("voice_merge_target")
        ck = f"p{pid}" if pid else _resolve_color_key(sp, matches, merge_map)
        color = palette.get(ck, TOKENS["muted_fg"])
        st = m.get("status") or "new"
        spoken = speech.get(sp)
        clip_len = float(m.get("clip_duration") or 0.0)
        low_speech = (spoken is not None and spoken < LOW_SPEECH_SEC) or (0 < clip_len < SHORT_CLIP_SEC)
        pname = (m.get("profile_name") or "").strip()
        weak_suggestion = ""
        if not pname and st == "new":
            suggested = (m.get("suggested_profile_name") or "").strip()
            # По короткому образцу подсказка ненадёжна: не подставлять её в поле, чтобы «Запомнить» не записал чужое имя.
            if low_speech:
                weak_suggestion = suggested
            else:
                pname = suggested
        score = m.get("score")
        sug_score = m.get("suggested_score")
        cs, ce = m.get("clip_start"), m.get("clip_end")
        inp_id = f"name-{sp.replace('_', '-')}"
        headline = html.escape(pname) if pname else html.escape(sp)
        unchecked = st == "confirmed" and bool(m.get("needs_human"))  # узнан автоматически, ждёт проверки человеком
        enroll_btn = "Подтвердить" if st != "confirmed" or unchecked else "Обновить"
        labels_txt = html.escape(" · ".join(labels))
        merge_note = (
            f'<p class="speaker-merge mb-2"><span class="badge rounded-pill text-bg-light border">'
            f"Объединено: {labels_txt}</span></p>"
            if len(labels) > 1
            else f'<p class="speaker-id mb-2 font-monospace">{labels_txt}</p>'
        )
        if voice_merge_target:
            merge_note += (
                f'<p class="small text-muted mb-2">Голос похож на '
                f"<strong>{html.escape(voice_merge_target)}</strong> (авто, можно разделить)</p>"
            )
        score_txt = (
            f'<p class="small text-muted mb-2">Совпадение: <strong>{int(score * 100)}%</strong></p>'
            if score is not None and st != "confirmed"
            else (
                f'<p class="small text-muted mb-2">Похож на <strong>{html.escape(pname)}</strong> '
                f"({int(sug_score * 100)}%)</p>"
                if sug_score is not None and st == "new" and pname
                else ""
            )
        )
        auto = m.get("auto") if st == "confirmed" else None
        if auto:
            score_txt += (
                f'<p class="small text-muted mb-2">Узнан автоматически по голосу '
                f'({int(float(auto.get("score") or 0) * 100)}%, отрыв {int(float(auto.get("margin") or 0) * 100)}%). '
                + ("Проверьте имя, компанию и сторону и нажмите «Подтвердить».</p>" if unchecked
                   else "Если это не он — введите имя и нажмите «Обновить».</p>")
            )
        hint = m.get("llm_hint") if st == "new" else None
        if hint and not (m.get("profile_name") or "").strip():
            quote = html.escape(str(hint.get("quote") or ""))
            score_txt += (
                f'<p class="small mb-2"><span class="text-muted">Подсказка модели:</span> '
                f"<strong>{html.escape(str(hint.get('name') or ''))}</strong>"
                + (f' <span class="text-muted">— «{quote}»</span>' if quote else "")
                + "</p>"
            )
            if not pname:
                pname = str(hint.get("name") or "").strip()
        speech_txt = (
            f'<p class="speaker-speech small mb-2"><span class="text-muted">Говорил</span> '
            f"{html.escape(_speech_text(spoken, total_speech))}</p>"
            if spoken is not None
            else ""
        )
        if low_speech:
            speech_txt += (
                '<p class="small mb-2 low-speech"><span class="badge text-bg-warning">мало речи</span> '
                '<span class="text-muted">Это реальный участник, но образец голоса короткий — '
                "подсказку имени проверьте на слух.</span></p>"
            )
            if weak_suggestion:
                weak_score = m.get("suggested_score")
                weak_pct = f" ({int(float(weak_score) * 100)}%)" if weak_score is not None else ""
                speech_txt += (
                    f'<p class="small text-muted mb-2">Похож на <strong>{html.escape(weak_suggestion)}</strong>'
                    f"{weak_pct} — в поле не подставлено.</p>"
                )
                score_txt = ""
        score_txt = speech_txt + score_txt
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
            f"{_badge('check' if unchecked else st)}"
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


def render_html(
    *, title: str, audio_url: str, payload: dict, profiles: list[dict], diar: dict | None = None
) -> str:
    matches = payload.get("matches") or {}
    diar_short = diar["short"] if diar else "Sortformer"
    diar_summary = _diar_summary(diar)
    merge_map = payload.get("merge_map") or {}
    is_lab = payload.get("source") == "lab"
    voices_title = "Голоса встречи" if is_lab else "Голоса Sortformer"
    recalc_label = "Пересчитать подсказки" if is_lab else "Обновить и пересчитать"
    recalc_hint = (
        "неотмеченные ↔ база голосов; подтверждённые не сбрасываются"
        if is_lab
        else "~20 с · Docker WeSpeaker"
    )
    workflow_3 = (
        "«Пересчитать подсказки» — только чтобы обновить совпадения с базой, жать не обязательно"
        if is_lab
        else "Когда все подписаны — «Обновить и пересчитать»"
    )
    card_groups = _speaker_card_groups(matches, merge_map)

    color_keys: list[str] = []
    for g in card_groups:
        pid = g.get("profile_id")
        if pid:
            color_keys.append(f"p{pid}")
        else:
            color_keys.append(_resolve_color_key(g["primary"], matches, merge_map))
    palette = _build_palette(color_keys)

    prof_rows = "".join(
        f"<tr><td class='fw-medium'>{html.escape(p['display_name'])}</td>"
        f"<td><span class='badge rounded-pill profile-scope'>{html.escape(p['scope'])}</span></td>"
        f"<td class='text-muted font-monospace small text-end'>{p['id']}</td></tr>"
        for p in profiles
    ) or "<tr><td colspan='3' class='text-muted fst-italic py-4 text-center'>Пока пусто</td></tr>"

    confirmed_n = len({g["profile_id"] for g in card_groups if g.get("profile_id")})
    total_n = len(card_groups)
    progress_pct = int(round(100 * confirmed_n / total_n)) if total_n else 0
    project_id = payload.get("project_id") or ""
    project_href = f"/projects/{html.escape(str(project_id), quote=True)}" if project_id != "" else "/"

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
    .profile-pool {{
      max-height: min(52vh, 28rem);
      overflow: auto;
      overscroll-behavior: contain;
    }}
    .profile-pool table {{ margin-bottom: 0; }}
    .profile-pool thead th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--color-bg);
      box-shadow: 0 1px 0 var(--color-border);
    }}
    .sidebar-sticky {{ position: sticky; top: 1rem; }}
    .play-clip, .enroll, .merge-btn, #recalc, .nav-pill-link {{ cursor: pointer; }}
    .play-clip:focus-visible, .enroll:focus-visible, .merge-btn:focus-visible, #recalc:focus-visible, .nav-pill-link:focus-visible {{
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
    <span class="navbar-text text-white-50 small d-none d-sm-inline">Голоса · {html.escape(diar_short)}</span>
  </div>
</nav>

<main class="container-fluid px-3 px-lg-4 py-4 py-lg-5" style="max-width: 1320px">
  <header class="page-hero p-4 p-lg-5 mb-4">
    <div class="row align-items-center g-4">
      <div class="col-lg-8">
        <p class="text-uppercase small fw-semibold mb-2" style="color:var(--color-primary);letter-spacing:.06em">Диаризация и база голосов</p>
        <h1 class="page-title h3 mb-2">{html.escape(title)}</h1>
        <p class="mb-0" style="color:var(--color-muted-fg);max-width:42rem">
          Подпишите голоса встречи и сверьте с общей базой.
        </p>
        {diar_summary}
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
            <a class="nav-pill-link" href="{project_href}">← Проект</a>
            <a class="nav-pill-link" href="report">Стенограмма</a>
          </nav>
          <audio id="player" class="w-100 mb-4" controls preload="metadata" playsinline
            src="{html.escape(audio_url, quote=True)}">Ваш браузер не поддерживает аудио.</audio>
          <div class="d-flex flex-wrap align-items-center gap-3">
            <button id="recalc" type="button" class="btn btn-accent d-inline-flex align-items-center gap-2">
              {ICON_REFRESH}<span>{html.escape(recalc_label)}</span>
            </button>
            <span class="small" style="color:var(--color-muted-fg)">{html.escape(recalc_hint)}</span>
          </div>
        </div>
      </section>

      <section class="panel" aria-labelledby="speakers-heading">
        <div class="panel-header d-flex justify-content-between align-items-center" id="speakers-heading">
          <span>{html.escape(voices_title)}</span>
          <span class="badge rounded-pill text-bg-light border fw-normal">голосов: {total_n}</span>
        </div>
        <div class="panel-body">
          <div class="row g-3">{_speaker_cards(card_groups, palette, matches, merge_map, (diar or {}).get("seconds"))}</div>
        </div>
      </section>
    </div>

    <aside class="col-lg-4">
      <div class="sidebar-sticky d-flex flex-column gap-4">
        <section class="panel" aria-labelledby="workflow-heading">
          <div class="panel-header" id="workflow-heading">Как работать</div>
          <div>
            <div class="workflow-step"><span class="workflow-num">1</span><span>Прослушайте эталонный клип каждого спикера</span></div>
            <div class="workflow-step"><span class="workflow-num">2</span><span>Проверьте имя, компанию и сторону и нажмите «Подтвердить»</span></div>
            <div class="workflow-step"><span class="workflow-num">3</span><span>{html.escape(workflow_3)}</span></div>
          </div>
        </section>

        <section class="panel" aria-labelledby="db-heading">
          <div class="panel-header d-flex justify-content-between align-items-center" id="db-heading">
            <span>База голосов</span>
            <span class="badge rounded-pill text-bg-light border">{len(profiles)}</span>
          </div>
          <div class="profile-pool" tabindex="0" role="region" aria-labelledby="db-heading">
            <table class="table table-sm table-hover mb-0 align-middle">
              <thead><tr><th scope="col">Имя</th><th scope="col">scope</th><th class="text-end" scope="col">id</th></tr></thead>
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


PAGE_TITLE = "Голоса созвона"


def build(
    *, match_json: Path, out_html: Path, audio_url: str, profiles: list[dict], title: str = PAGE_TITLE
) -> None:
    payload = json.loads(match_json.read_text(encoding="utf-8"))
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(
        render_html(
            title=title,
            audio_url=audio_url,
            payload=payload,
            profiles=profiles,
            diar=diar_info(match_json, payload),
        ),
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
