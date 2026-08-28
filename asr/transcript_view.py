"""Статическая страница стенограммы: цвета спикеров, клик к таймкоду, фильтр."""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

from speakers import Turn, format_clock

COLORS = (
    "#5B7FA6",
    "#A67B7B",
    "#6B9080",
    "#B08968",
    "#7C6B9E",
    "#9B8AA5",
    "#5C8A8A",
    "#8B7FA8",
)


_PLACEHOLDER_SPEAKERS = frozenset({"", "unknown", "unk", "none"})


def _is_placeholder_speaker(name: str) -> bool:
    return str(name or "").strip().lower() in _PLACEHOLDER_SPEAKERS


def _color_map(turns: list[Turn]) -> dict[str, str]:
    names = []
    for turn in turns:
        if _is_placeholder_speaker(turn.speaker):
            continue
        if turn.speaker not in names:
            names.append(turn.speaker)
    return {name: COLORS[i % len(COLORS)] for i, name in enumerate(names)}


def _strip_llm_artifacts(text: str) -> str:
    """Gemma иногда просится в LaTeX, хотя промпт это запрещает — подчищаем как подстраховку."""
    try:
        root = Path(__file__).resolve().parents[1]
        root_s = str(root)
        if root_s not in sys.path:
            sys.path.insert(0, root_s)
        from app.app.services.plain_text import clean_summary_output

        return clean_summary_output(text)
    except Exception:
        # Fallback if app package is unavailable in a bare asr script run.
        text = re.sub(r"\$\\(?:rightarrow|to)\$|\\(?:rightarrow|to)\b", "→", text)
        text = re.sub(r"\$\\times\$|\\times\b", "×", text)
        text = re.sub(r"\$\\sim\$|\\sim\b", "∼", text)
        text = re.sub(r"\$\\le(?:q)?\$|\\le(?:q)?\b", "≤", text)
        text = re.sub(r"\$([^$\n]{1,80})\$", r"\1", text)
        return text

_NUMBERED = re.compile(r"^(\d+)[.)]\s+(.*)$")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _inline_md(text: str) -> str:
    """Жирный, курсив, код, ссылки как текст — тот же набор, что в Word-экспорте."""
    escaped = html.escape(_strip_llm_artifacts(text))
    escaped = _INLINE_LINK.sub(r"\1", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def _split_md_pipe_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [cell.strip() for cell in s.split("|")]


def _is_md_table_sep(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _looks_md_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def parse_md_table(raw_rows: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """GFM-таблица: шапка + тело. Строка `---` необязательна (старый промпт её не требовал)."""
    rows = [_split_md_pipe_row(r) for r in raw_rows]
    if len(rows) < 2:
        return None
    header = rows[0]
    if not header:
        return None
    rest = rows[1:]
    if rest and _is_md_table_sep(rest[0]):
        rest = rest[1:]
    if not rest:
        return None
    ncols = len(header)
    body = [(row + [""] * ncols)[:ncols] for row in rest]
    return header, body


def _md_table_to_html(raw_rows: list[str]) -> str | None:
    parsed = parse_md_table(raw_rows)
    if parsed is None:
        return None
    header, body = parsed
    thead = "".join(f'<th scope="col">{_inline_md(cell)}</th>' for cell in header)
    tbody = "".join(
        "<tr>" + "".join(f"<td>{_inline_md(cell)}</td>" for cell in row) + "</tr>"
        for row in body
    )
    return (
        '<div class="summary-table-wrap">'
        '<table class="summary-table">'
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{tbody}</tbody>"
        "</table></div>"
    )


def load_call_summary_files(asr_dir: Path) -> tuple[str, str]:
    """Итоговый протокол и краткое резюме из asr/ (пусто, если файла нет)."""

    def _read(name: str) -> str:
        path = asr_dir / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    return _read("call_summary.md"), _read("call_brief.md")


def _summary_empty_html(message: str) -> str:
    return f'<p class="summary-empty">{html.escape(message)}</p>'


def _summary_tabs_html(summary_md: str, brief_md: str) -> str:
    full = summary_md.strip()
    brief = brief_md.strip()
    if not full and not brief:
        return ""
    full_body = _summary_to_html(full) if full else _summary_empty_html(
        "Итоговое резюме ещё не собрано."
    )
    brief_body = _summary_to_html(brief) if brief else _summary_empty_html(
        "Краткое резюме ещё не собрано. Нажмите «Пересобрать выжимку»."
    )
    return (
        '<section class="summary" aria-label="Резюме созвона">'
        '<div class="summary-tabs" role="tablist" aria-label="Вид резюме">'
        '<button type="button" class="summary-tab" role="tab" id="tab-summary-full" '
        'aria-controls="panel-summary-full" aria-selected="true" data-summary-tab="full">'
        "Итоговое резюме созвона</button>"
        '<button type="button" class="summary-tab" role="tab" id="tab-summary-brief" '
        'aria-controls="panel-summary-brief" aria-selected="false" data-summary-tab="brief" tabindex="-1">'
        "Краткое резюме созвона</button>"
        "</div>"
        '<div class="summary-panel" role="tabpanel" id="panel-summary-full" '
        'aria-labelledby="tab-summary-full">'
        f"{full_body}</div>"
        '<div class="summary-panel" role="tabpanel" id="panel-summary-brief" '
        'aria-labelledby="tab-summary-brief" hidden>'
        f"{brief_body}</div>"
        "</section>"
    )


def _summary_to_html(summary_md: str) -> str:
    """Мини-рендер markdown выжимки: заголовки, списки, таблицы `|`, **жирный**."""
    parts: list[str] = []
    in_list: str | None = None
    lines = summary_md.strip().splitlines()
    i = 0

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append(f"</{in_list}>")
            in_list = None

    def open_list(kind: str) -> None:
        nonlocal in_list
        if in_list != kind:
            close_list()
            parts.append(f"<{kind}>")
            in_list = kind

    while i < len(lines):
        line = lines[i].strip()
        if not line or re.fullmatch(r"-{3,}", line):
            close_list()
            i += 1
            continue
        if _looks_md_table_row(line):
            table_lines = [line]
            i += 1
            while i < len(lines) and _looks_md_table_row(lines[i].strip()):
                table_lines.append(lines[i].strip())
                i += 1
            rendered = _md_table_to_html(table_lines)
            close_list()
            if rendered:
                parts.append(rendered)
            else:
                for row in table_lines:
                    parts.append(f"<p>{_inline_md(row)}</p>")
            continue
        if line.startswith("#### "):
            close_list()
            parts.append(f"<h4>{_inline_md(line[5:].strip())}</h4>")
        elif line.startswith("### "):
            close_list()
            parts.append(f"<h4>{_inline_md(line[4:].strip())}</h4>")
        elif line.startswith("## "):
            close_list()
            parts.append(f"<h3>{_inline_md(line[3:].strip())}</h3>")
        elif line.startswith("# "):
            close_list()
            parts.append(f"<h3>{_inline_md(line[2:].strip())}</h3>")
        elif line.startswith("- ") or line.startswith("* "):
            open_list("ul")
            parts.append(f"<li>{_inline_md(line[2:].strip())}</li>")
        else:
            numbered = _NUMBERED.match(line)
            if numbered:
                open_list("ol")
                parts.append(f"<li>{_inline_md(numbered.group(2).strip())}</li>")
            else:
                close_list()
                parts.append(f"<p>{_inline_md(line)}</p>")
        i += 1
    close_list()
    return "\n".join(parts)


def _registry_summary_meta(sp_count: int = 0) -> str:
    count = (
        f'<span class="registry-sp-count">{sp_count} спик.</span>'
        if sp_count
        else ""
    )
    return (
        f'<span class="registry-summary-meta">{count}'
        '<span class="registry-expand-hint">(развернуть)</span>'
        '<span class="registry-collapse-hint">(свернуть)</span>'
        "</span>"
    )


def _merge_target_options(primary: str, labels: list[str], registry: list[dict]) -> str:
    own = set(labels)
    opts = ['<option value="">— объединить с —</option>']
    for row in registry:
        tgt = row["primary"]
        if tgt == primary or tgt in own:
            continue
        dname = row.get("display_name") or tgt
        opts.append(f'<option value="{html.escape(tgt, quote=True)}">{html.escape(dname)}</option>')
    return "".join(opts) if len(opts) > 1 else ""


def _registry_panel(registry: list[dict], colors: dict[str, str]) -> str:
    cards: list[str] = []
    for row in registry:
        primary = row["primary"]
        labels = row.get("labels") or [primary]
        dname = row.get("display_name") or primary
        input_val = row.get("input_value") or ""
        hint = row.get("suggested_hint") or ""
        color = colors.get(dname, colors.get(primary, "#64748B"))
        st = row.get("status") or "new"
        btn = "Сохранить" if st == "confirmed" else "Запомнить"
        ph = html.escape(hint) if hint else "Имя"
        cs, ce = row.get("clip_start"), row.get("clip_end")
        if cs is not None and ce is not None:
            clip_title = f"Эталон {format_clock(float(cs))}–{format_clock(float(ce))}"
            play = (
                f'<button type="button" class="act play-clip" data-start="{float(cs):.3f}" '
                f'data-end="{float(ce):.3f}" title="{html.escape(clip_title, quote=True)}" '
                f'aria-label="Прослушать {html.escape(clip_title, quote=True)}">▶</button>'
            )
        else:
            play = '<span class="muted play-miss" title="Нет эталона">—</span>'
        merge_opts = _merge_target_options(primary, labels, registry) if len(labels) == 1 else ""
        if merge_opts:
            merge_block = (
                f'<div class="sp-line sp-merge-line">'
                f'<select class="merge-target" aria-label="Объединить {html.escape(primary, quote=True)} с">'
                f"{merge_opts}</select>"
                '<button type="button" class="act sp-merge action-btn" '
                'title="Считать оба ID одним человеком">Слить</button>'
                "</div>"
            )
        elif len(labels) > 1:
            merge_block = (
                f'<div class="sp-line muted sp-merged" title="{html.escape(" · ".join(labels), quote=True)}">'
                f"слито: {html.escape(' · '.join(labels))}</div>"
            )
        else:
            merge_block = ""
        cards.append(
            f'<article class="sp-row sp-item" data-diar="{html.escape(primary, quote=True)}">'
            f'<div class="sp-line sp-meta">'
            f'<span class="dot" style="background:{color}" aria-hidden="true"></span>'
            f'<code class="sp-id">{html.escape(primary)}</code>'
            f"{play}"
            f"</div>"
            f'<div class="sp-line sp-name-line">'
            f'<input class="sp-name" type="text" value="{html.escape(input_val)}" '
            f'placeholder="{ph}" autocomplete="name" '
            f'aria-label="Имя {html.escape(primary, quote=True)}" />'
            f'<button type="button" class="act sp-enroll action-btn" data-idle-label="{html.escape(btn)}">{btn}</button>'
            f"</div>"
            f"{merge_block}"
            f"</article>"
        )
    return (
        '<details class="registry-collapse rename">'
        '<summary class="registry-summary" title="Имена спикеров, слияние ID и действия">'
        '<span class="reg-toggle" aria-hidden="true">▸</span>'
        "<strong>Имена и слияние</strong>"
        f"{_registry_summary_meta(len(registry))}"
        "</summary>"
        '<div class="registry-body">'
        '<details class="registry-help">'
        "<summary>?</summary>"
        '<p class="hint registry-hint">'
        "▶ — эталон голоса. До «Запомнить» в тексте только ID. "
        "«Слить» — два ID одного человека."
        "</p></details>"
        '<div class="sp-list">'
        + "".join(cards)
        + "</div>"
        + '<div class="toolbar registry-tools">'
        '<button type="button" class="act primary action-btn rect-act" id="recalc" data-idle-label="Пересчитать">Пересчитать</button>'
        '<button type="button" class="act action-btn rect-act" id="rebuild-summary" data-idle-label="Пересобрать выжимку">Пересобрать выжимку</button>'
        '<button type="button" class="act action-btn rect-act" id="reindex" data-idle-label="Обновить и переиндексировать">Обновить и переиндексировать</button>'
        '<span class="hint">спикеры ~20 с · выжимка ~1 мин · индекс ~30 с</span>'
        "</div></div></details>"
    )


def render_transcript_html(
    *,
    title: str,
    audio_url: str,
    turns: list[Turn],
    mapping: dict[str, str] | None = None,
    summary_md: str = "",
    brief_md: str = "",
    artifact_id: str | None = None,
    project_id: int | None = None,
    registry: list[dict] | None = None,
) -> str:
    colors = _color_map(turns)
    chips = "".join(
        (
            f'<button type="button" class="chip" data-filter="{html.escape(name, quote=True)}">'
            f'<span class="dot" style="background:{colors[name]}"></span>'
            f"{html.escape(name)}</button>"
        )
        for name in colors
    )
    rename_rows = "".join(
        (
            "<label>"
            f'<span class="dot" style="background:{color}"></span>'
            f'<code>{html.escape(name)}</code>'
            f'<textarea rows="1" data-rename-src="{html.escape(name, quote=True)}">'
            f'{html.escape((mapping or {}).get(name, name))}</textarea></label>'
        )
        for name, color in colors.items()
    )
    rename_block = (
        _registry_panel(registry, colors)
        if registry and artifact_id
        else (
            f'<details class="registry-collapse rename">'
            f'<summary class="registry-summary" title="Имена спикеров и слияние ID">'
            f'<span class="reg-toggle" aria-hidden="true">▸</span>'
            f"<strong>Имена и слияние</strong>"
            f"{_registry_summary_meta()}"
            f"</summary>"
            f'<div class="registry-body">{rename_rows}'
            f'<div class="toolbar rename-tools">'
            f'<button type="button" class="act" id="apply-names">Применить имена</button>'
            f'<div class="merge-inline">'
            f'<select id="merge-from" aria-label="Кого слить"></select>'
            f'<span aria-hidden="true">→</span>'
            f'<select id="merge-to" aria-label="С кем слить"></select>'
            f'<button type="button" class="act" id="merge">Слить</button>'
            f"</div></div></div></details>"
        )
    )
    hint = (
        "Клик по времени или реплике перематывает аудио. Фильтр оставляет одного спикера."
        if registry and artifact_id
        else "Клик по времени или реплике ставит плеер. Фильтр оставляет одного человека. Имена правятся ниже."
    )
    summary_block = _summary_tabs_html(summary_md, brief_md)
    items = []
    for i, turn in enumerate(turns):
        color = colors.get(turn.speaker, "#64748b")
        items.append(
            "<article class='turn' "
            f"data-i='{i}' data-start='{turn.start:.3f}' data-end='{turn.end:.3f}' "
            f"data-speaker='{html.escape(turn.speaker, quote=True)}'>"
            f"<button type='button' class='when' style='border-color:{color}' title='Перейти к {format_clock(turn.start)}'>"
            f"{format_clock(turn.start)}–{format_clock(turn.end)}</button>"
            f"<p class='line'><span class='who' style='color:{color}'></span>: "
            f"{html.escape(turn.text)}</p></article>"
        )
    payload = json.dumps(
        {
            "artifact_id": artifact_id,
            "project_id": project_id,
            "turns": [
                {"start": t.start, "end": t.end, "speaker": t.speaker, "text": t.text} for t in turns
            ],
            "mapping": mapping or {},
            "registry": registry or [],
        },
        ensure_ascii=False,
    )
    use_registry = bool(registry and artifact_id)
    export_tools = ""
    if artifact_id:
        export_tools = (
            '<section class="export-panel">'
            '<div class="export-panel-title">Скачать материалы созвона</div>'
            '<div class="toolbar export-tools">'
            '<button type="button" class="act primary export-download" id="download-summary" '
            'data-idle-label="Выжимка Word">Выжимка Word</button>'
            '<button type="button" class="act export-download" id="download-brief" '
            'data-idle-label="Краткое резюме созвона">Краткое резюме созвона</button>'
            '<button type="button" class="act export-download" id="download-transcript" '
            'data-idle-label="Стенограмма .txt">Стенограмма .txt</button>'
            "</div></section>"
        )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ font-family: Segoe UI, system-ui, sans-serif; color: #0f172a; background: #f8fafc; --accent: #5b7fa6; --border: #e2e8f0; }}
    body {{ margin: 0; font-size: .95rem; }}
    header, main {{ max-width: 980px; margin: 0 auto; padding: 16px; }}
    h1 {{ font-size: 1.35rem; margin: 0 0 8px; }}
    audio {{ width: 100%; margin: 8px 0 16px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 16px; align-items: center; }}
    .chip, .when, button.act {{
      border: 1px solid #cbd5e1; background: #fff; border-radius: 999px;
      padding: 6px 12px; cursor: pointer; font: inherit; font-size: inherit;
      box-sizing: border-box; max-width: 100%;
    }}
    .chip.active, .chip:hover {{ background: #e2e8f0; }}
    button.act.primary {{ background: var(--accent); color: #fff; border-color: transparent; font-weight: 600; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
    .summary {{ background: #fff; border: 1px solid var(--border); border-radius: 12px; margin: 12px 0 20px; overflow: hidden; }}
    .summary-tabs {{ display: flex; flex-wrap: wrap; gap: 0; border-bottom: 1px solid var(--border); padding: 0 8px; }}
    .summary-tab {{
      border: 0; background: transparent; font: inherit; font-size: 1rem; font-weight: 600;
      color: #64748b; cursor: pointer; padding: 12px 14px 10px; margin-bottom: -1px;
      border-bottom: 2px solid transparent;
    }}
    .summary-tab:hover {{ color: #0f172a; }}
    .summary-tab[aria-selected="true"] {{ color: #0f172a; border-bottom-color: var(--accent); }}
    .summary-tab:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .summary-panel {{ padding: 14px 18px; }}
    .summary-empty {{ color: #64748b; margin: 4px 0; }}
    .summary h3 {{ margin: 14px 0 6px; font-size: 1rem; color: #1e293b; }}
    .summary h3:first-of-type {{ margin-top: 4px; }}
    .summary h4 {{ margin: 8px 0 4px; font-size: .92rem; }}
    .summary ul, .summary ol {{ margin: 4px 0 10px 1.25rem; padding: 0; }}
    .summary li {{ margin: 3px 0; line-height: 1.5; }}
    .summary p {{ margin: 4px 0 8px; line-height: 1.55; }}
    .summary code {{ font-family: Consolas, "Courier New", monospace; font-size: .9em; }}
    /* summary-format-v2 */
    .summary-table-wrap {{ overflow-x: auto; margin: 8px 0 12px; -webkit-overflow-scrolling: touch; }}
    .summary-table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
    .summary-table th, .summary-table td {{
      border: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: top;
    }}
    .summary-table th {{ background: #f1f5f9; font-weight: 650; white-space: nowrap; }}
    .summary-table tbody tr:nth-child(even) {{ background: #f8fafc; }}
    .turn {{ display: grid; grid-template-columns: 92px 1fr; gap: 8px; align-items: baseline; padding: 3px 4px; border-radius: 6px; cursor: pointer; }}
    .turn:hover {{ background: rgba(255,255,255,.6); }}
    .turn.active {{ background: #e0f2fe; }}
    .turn.hidden {{ display: none; }}
    .when {{ align-self: baseline; justify-self: start; font-size: .75rem; padding: 1px 6px; white-space: nowrap; color: #64748b; line-height: 1.3; }}
    .line {{ margin: 0; line-height: 1.35; font-size: .92rem; }}
    .who {{ font-weight: 650; }}
    p {{ margin: 0; }}
    .rename {{ display: block; background: #fff; border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px; }}
    .registry-summary {{
      cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
      padding: 10px 12px; font-size: inherit; border-radius: 12px;
      transition: background .12s ease;
    }}
    .registry-summary:hover {{ background: #f8fafc; }}
    .registry-summary:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .registry-summary::-webkit-details-marker {{ display: none; }}
    .reg-toggle {{
      flex: 0 0 auto; width: .85rem; color: #64748b; font-size: .7rem;
      transition: transform .15s ease;
    }}
    .registry-collapse[open] .reg-toggle {{ transform: rotate(90deg); }}
    .registry-summary-meta {{
      margin-left: auto; display: inline-flex; align-items: center; gap: 6px;
      color: #64748b; font-size: .85rem; font-weight: 400;
    }}
    .registry-expand-hint, .registry-collapse-hint {{
      color: #94a3b8; font-size: .78rem; font-weight: 400;
    }}
    .registry-collapse-hint {{ display: none; }}
    .registry-collapse[open] .registry-expand-hint {{ display: none; }}
    .registry-collapse[open] .registry-collapse-hint {{ display: inline; }}
    .registry-body {{ display: grid; gap: 8px; padding: 0 12px 12px; }}
    .rename label {{ display: grid; grid-template-columns: 16px minmax(0, 7rem) 1fr; gap: 8px; align-items: center; }}
    .registry-help {{ margin: 0; }}
    .registry-help > summary {{
      cursor: pointer; color: #64748b; font-size: .8rem; list-style: none;
      width: 1.4rem; height: 1.4rem; border: 1px solid #cbd5e1; border-radius: 999px;
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .registry-help > summary::-webkit-details-marker {{ display: none; }}
    .registry-hint {{ margin: 8px 0 0; line-height: 1.35; font-size: .82rem; }}
    .sp-list {{ display: grid; gap: 6px; }}
    .sp-item {{
      display: grid; gap: 6px; padding: 8px 10px; border: 1px solid var(--border);
      border-radius: 8px; background: #f8fafc;
    }}
    .sp-line {{
      display: flex; align-items: center; gap: 6px; min-width: 0;
    }}
    .sp-meta {{ flex-wrap: wrap; }}
    .sp-name-line, .sp-merge-line {{ flex-wrap: nowrap; }}
    .sp-id {{ font-size: .75rem; color: #64748b; flex: 0 0 auto; }}
    .sp-name, .merge-target {{
      flex: 1 1 auto; min-width: 0; width: auto; box-sizing: border-box; padding: 6px 8px;
      border: 1px solid #cbd5e1; border-radius: 8px; font: inherit; font-size: .92rem; background: #fff;
    }}
    .merge-target {{ font-size: .85rem; }}
    .sp-enroll, .sp-merge, .play-clip {{ flex: 0 0 auto; white-space: nowrap; }}
    .play-miss {{ display: inline-block; min-width: 1.6rem; text-align: center; }}
    .sp-merged {{ font-size: .8rem; }}
    .registry-tools, .rename-tools {{ margin-top: 2px; }}
    .export-panel {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
      margin: 0 0 16px;
    }}
    .export-panel-title {{
      font-weight: 650;
      font-size: .95rem;
      margin-bottom: 8px;
      color: #1e293b;
    }}
    .export-tools {{
      margin: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: stretch;
    }}
    .export-tools .export-download {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-sizing: border-box;
      flex: 1 1 13rem;
      min-width: min(100%, 11rem);
      max-width: 100%;
      white-space: normal;
      text-align: center;
      line-height: 1.3;
      padding: 8px 14px;
      min-height: 2.6rem;
      overflow-wrap: break-word;
      border-radius: 10px;
    }}
    .action-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-sizing: border-box;
      min-width: 0;
      max-width: 100%;
      flex-shrink: 1;
      white-space: normal;
      text-align: center;
      line-height: 1.3;
      padding: 8px 14px;
      overflow-wrap: break-word;
      border-radius: 10px;
    }}
    .registry-tools {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: stretch;
      margin-top: 2px;
    }}
    .registry-tools .action-btn {{
      flex: 1 1 11rem;
      min-width: min(100%, 10rem);
    }}
    .registry-tools button.act.rect-act {{
      border-radius: 10px;
      white-space: normal;
      text-align: center;
      line-height: 1.3;
      padding: 6px 10px;
      min-height: 2.15rem;
      font-size: .875rem;
      flex: 1 1 9.5rem;
      min-width: min(100%, 8.5rem);
      overflow-wrap: break-word;
    }}
    .sp-enroll.action-btn, .sp-merge.action-btn {{
      flex: 0 0 auto;
      min-width: 6.5rem;
      padding: 6px 12px;
    }}
    .action-spinner, .export-spinner {{
      display: inline-block;
      width: 0.875rem;
      height: 0.875rem;
      border: 2px solid currentColor;
      border-right-color: transparent;
      border-radius: 50%;
      animation: export-spin 0.65s linear infinite;
    }}
    @keyframes export-spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .merge-inline {{
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center; width: 100%;
    }}
    .merge-inline select {{ flex: 1 1 8rem; min-width: 0; max-width: 100%; }}
    .muted {{ color: #94a3b8; font-size: .85rem; }}
    input, select, textarea {{ padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 8px; font: inherit; font-size: inherit; }}
    textarea[data-rename-src], textarea.auto-grow {{
      width: 100%; min-height: 2.4em; resize: none; overflow: hidden;
      line-height: 1.4; white-space: pre-wrap; word-break: break-word;
    }}
    .hint {{ color: #475569; font-size: .9rem; }}
    .task-banner {{
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      border-radius: 12px;
      padding: 10px 14px;
      margin: 0 0 14px;
      color: #1e3a5f;
      font-size: .92rem;
    }}
    .task-banner-row {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .task-banner-row + .task-banner-row {{ margin-top: 6px; }}
    .task-banner-spinner {{
      display: inline-block;
      width: 0.875rem;
      height: 0.875rem;
      border: 2px solid currentColor;
      border-right-color: transparent;
      border-radius: 50%;
      animation: export-spin 0.65s linear infinite;
      flex-shrink: 0;
    }}
    @media (max-width: 640px) {{
      header, main {{ padding: 12px; }}
      h1 {{ font-size: 1.15rem; }}
      .turn {{ grid-template-columns: 1fr; gap: 4px; padding: 8px 2px; }}
      .when {{ font-size: .8rem; }}
      .rename {{ padding: 0; gap: 6px; }}
      .registry-body {{ padding: 0 10px 10px; }}
      .rename label {{ grid-template-columns: 16px 1fr; }}
      .rename label code {{ grid-column: 2; font-size: .78rem; }}
      .rename label textarea {{ grid-column: 1 / -1; }}
      .sp-item {{ padding: 8px; }}
      .sp-enroll, .sp-merge {{ padding: 5px 10px; font-size: .85rem; }}
      .chip, .when, button.act {{ min-height: 34px; padding: 5px 10px; }}
      .export-tools .export-download,
      .registry-tools .action-btn {{
        flex: 1 1 100%;
      }}
      #q {{ width: 100%; min-width: 0 !important; }}
      .summary-table {{ font-size: .85rem; }}
      .summary-table th {{ white-space: normal; }}
      .summary-tab {{ font-size: .92rem; padding: 10px 10px 8px; }}
    }}
  </style>
</head>
<body>
<div id="task-banner" class="task-banner" hidden role="status" aria-live="polite"></div>
<header>
  {export_tools}
  <h1>{html.escape(title)}</h1>
  {summary_block}
  <p class="hint">{html.escape(hint)}</p>
  <audio id="player" controls preload="metadata" playsinline src="{html.escape(audio_url, quote=True)}"></audio>
  <div class="toolbar">
    <button type="button" class="chip active" data-filter="">Все</button>
    {chips}
    <input id="q" placeholder="поиск по тексту" style="flex:1;min-width:180px">
  </div>
  {rename_block}
</header>
<main id="turns">{''.join(items)}</main>
<script id="data" type="application/json">{payload}</script>
<script>
const data = JSON.parse(document.getElementById("data").textContent);
const player = document.getElementById("player");
function initSummaryTabs() {{
  const tabs = [...document.querySelectorAll(".summary-tab")];
  if (!tabs.length) return;
  const key = data.artifact_id ? ("skkkk-summary-tab:" + data.artifact_id) : "";
  const select = (name) => {{
    tabs.forEach((tab) => {{
      const on = tab.dataset.summaryTab === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(tab.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    }});
    if (key) try {{ sessionStorage.setItem(key, name); }} catch (_) {{}}
  }};
  tabs.forEach((tab) => {{
    tab.addEventListener("click", () => select(tab.dataset.summaryTab));
    tab.addEventListener("keydown", (ev) => {{
      const i = tabs.indexOf(tab);
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {{
        ev.preventDefault();
        const next = ev.key === "ArrowRight"
          ? tabs[(i + 1) % tabs.length]
          : tabs[(i - 1 + tabs.length) % tabs.length];
        next.focus();
        select(next.dataset.summaryTab);
      }}
    }});
  }});
  let start = "full";
  if (key) try {{ start = sessionStorage.getItem(key) || "full"; }} catch (_) {{}}
  if (!tabs.some((t) => t.dataset.summaryTab === start)) start = "full";
  select(start);
}}
initSummaryTabs();
const useRegistry = {"true" if use_registry else "false"};
const storeKey = "skkkk-speakers:" + location.pathname;
const saved = useRegistry ? {{}} : JSON.parse(localStorage.getItem(storeKey) || "{{}}");
if (!useRegistry) {{
  data.mapping = Object.assign({{}}, data.mapping, saved.mapping || {{}});
  if (saved.merge) {{
    for (const [src, dst] of Object.entries(saved.merge)) {{
      for (const t of data.turns) if (t.speaker === src) t.speaker = dst;
    }}
  }}
}}
function displayName(id) {{
  if (!id || String(id).toLowerCase() === "unknown") return "—";
  return useRegistry ? id : (data.mapping[id] || id);
}}
function paint() {{
  document.querySelectorAll(".turn").forEach((el, i) => {{
    const t = data.turns[i];
    el.dataset.speaker = t.speaker;
    el.querySelector(".who").textContent = displayName(t.speaker);
  }});
  if (!useRegistry) {{
    const names = [...new Set(data.turns.map(t => t.speaker))];
    for (const sel of ["merge-from", "merge-to"]) {{
      const node = document.getElementById(sel);
      if (node) node.innerHTML = names.map(n => `<option value="${{n}}">${{displayName(n)}}</option>`).join("");
    }}
  }}
}}
function persist() {{
  if (!useRegistry) localStorage.setItem(storeKey, JSON.stringify({{ mapping: data.mapping, merge: saved.merge || {{}} }}));
}}
function seek(sec) {{
  const t = Number(sec);
  if (!Number.isFinite(t)) return;
  const go = () => {{
    try {{ player.currentTime = t; }} catch (_) {{ /* not seekable yet */ }}
    const p = player.play();
    if (p && typeof p.catch === "function") p.catch(() => {{}});
  }};
  if (player.readyState >= 1) {{
    go();
    return;
  }}
  player.addEventListener("loadedmetadata", go, {{ once: true }});
  try {{ player.load(); }} catch (_) {{}}
}}
function toast(msg) {{ alert(msg); }}
function apiDetail(text) {{
  try {{
    const j = JSON.parse(text);
    const d = j.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
    return text;
  }} catch (_) {{ return text || "ошибка запроса"; }}
}}
async function postJson(url, body) {{
  const res = await fetch(url, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json;charset=utf-8", ...authHeaders() }},
    body: body ? JSON.stringify(body) : undefined,
  }});
  const text = await res.text();
  if (!res.ok) {{
    let detail = apiDetail(text);
    if (res.status === 404 && (detail === "Not Found" || detail.includes("Not Found"))) {{
      detail = "API не найден — перезапустите сервер (scripts/run_api.ps1)";
    }}
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }}
  return text;
}}
const ARTIFACT_TASK_BUTTONS = {{
  speaker_recalculate: ["#recalc"],
  call_summary_rebuild: ["#rebuild-summary"],
  artifact_reindex: ["#reindex"],
}};
let artifactTaskPollTimer = null;

function resolveTaskButtonSelector(sel) {{
  if (sel.startsWith("#") || sel.startsWith(".") || sel.includes(" ")) return sel;
  return `#${{sel}}`;
}}

function setButtonsBusy(selectors, busy) {{
  selectors.forEach((sel) => {{
    document.querySelectorAll(resolveTaskButtonSelector(sel)).forEach((btn) => {{
      if (!btn) return;
      if (busy) setActionButtonBusy(btn, true, btn.dataset.idleLabel || "Выполняется…");
      else if (btn.dataset.busy === "1") setActionButtonBusy(btn, false);
    }});
  }});
}}

function applyArtifactTaskBusy(tasks) {{
  const running = new Set((tasks || []).filter((t) => t.status === "running").map((t) => t.task_type));
  Object.entries(ARTIFACT_TASK_BUTTONS).forEach(([taskType, selectors]) => {{
    setButtonsBusy(selectors, running.has(taskType));
  }});
  setButtonsBusy([".sp-enroll"], running.has("speaker_enroll"));
}}

async function syncArtifactTasks() {{
  if (!data.artifact_id) return false;
  const banner = document.getElementById("task-banner");
  try {{
    const res = await fetch(`/api/artifacts/${{data.artifact_id}}/tasks/active`, {{ headers: authHeaders() }});
    if (!res.ok) return false;
    const payload = await res.json();
    const tasks = payload.tasks || [];
    if (!tasks.length) {{
      if (banner) {{
        banner.hidden = true;
        banner.innerHTML = "";
      }}
      applyArtifactTaskBusy([]);
      return false;
    }}
    if (banner) {{
      banner.hidden = false;
      banner.innerHTML = tasks.map((t) =>
        `<div class="task-banner-row"><span class="task-banner-spinner" aria-hidden="true"></span><span>${{t.label}}…</span></div>`
      ).join("");
    }}
    applyArtifactTaskBusy(tasks);
    return true;
  }} catch (_) {{
    return false;
  }}
}}

function startArtifactTaskPoll() {{
  if (artifactTaskPollTimer || !data.artifact_id) return;
  artifactTaskPollTimer = setInterval(async () => {{
    const busy = await syncArtifactTasks();
    if (!busy) {{
      clearInterval(artifactTaskPollTimer);
      artifactTaskPollTimer = null;
      location.reload();
    }}
  }}, 3000);
}}
document.getElementById("turns").addEventListener("click", (ev) => {{
  if (ev.target.closest("button.when")) {{
    seek(Number(ev.target.closest(".turn").dataset.start));
    return;
  }}
  const art = ev.target.closest(".turn");
  if (!art) return;
  seek(Number(art.dataset.start));
}});
document.getElementById("turns").addEventListener("keydown", (ev) => {{
  if (ev.key !== "Enter" && ev.key !== " ") return;
  const btn = ev.target.closest(".when");
  if (!btn) return;
  ev.preventDefault();
  seek(Number(btn.closest(".turn").dataset.start));
}});
player.addEventListener("timeupdate", () => {{
  const t = player.currentTime;
  document.querySelectorAll(".turn").forEach((el) => {{
    const a = Number(el.dataset.start), b = Number(el.dataset.end);
    el.classList.toggle("active", t >= a && t < b);
  }});
}});
let filter = "";
function applyFilter() {{
  const q = document.getElementById("q").value.trim().toLowerCase();
  document.querySelectorAll(".turn").forEach((el) => {{
    const sp = el.dataset.speaker;
    const text = el.innerText.toLowerCase();
    const okS = !filter || sp === filter;
    const okQ = !q || text.includes(q);
    el.classList.toggle("hidden", !(okS && okQ));
  }});
  document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.filter === filter));
}}
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {{
  filter = c.dataset.filter || "";
  applyFilter();
}}));
document.getElementById("q").addEventListener("input", applyFilter);
const applyBtn = document.getElementById("apply-names");
if (applyBtn) applyBtn.addEventListener("click", () => {{
  document.querySelectorAll("[data-rename-src]").forEach((inp) => {{
    const v = inp.value.trim();
    if (v) data.mapping[inp.dataset.renameSrc] = v;
  }});
  persist();
  paint();
}});
function autoGrow(el) {{
  el.style.height = "auto";
  el.style.height = `${{el.scrollHeight}}px`;
}}
document.querySelectorAll("[data-rename-src]").forEach((el) => {{
  autoGrow(el);
  el.addEventListener("input", () => autoGrow(el));
}});
const mergeBtn = document.getElementById("merge");
if (mergeBtn) mergeBtn.addEventListener("click", () => {{
  const src = document.getElementById("merge-from").value;
  const dst = document.getElementById("merge-to").value;
  if (!src || !dst || src === dst) return;
  saved.merge = saved.merge || {{}};
  saved.merge[src] = dst;
  for (const t of data.turns) if (t.speaker === src) t.speaker = dst;
  persist();
  paint();
  applyFilter();
}});
if (useRegistry && data.artifact_id) {{
  document.querySelectorAll(".play-clip").forEach(btn => btn.addEventListener("click", (ev) => {{
    ev.stopPropagation();
    seek(Number(btn.dataset.start));
  }}));
  document.querySelectorAll(".sp-enroll").forEach(btn => btn.addEventListener("click", async () => {{
    const row = btn.closest(".sp-row");
    if (!row) {{ toast("строка спикера не найдена"); return; }}
    const diar = row.dataset.diar;
    const name = row.querySelector(".sp-name")?.value.trim();
    if (!name) {{ toast("Введите имя"); return; }}
    await guardActionButton(btn, async () => {{
      await postJson(`/api/artifacts/${{data.artifact_id}}/speaker-enroll`, {{
        diar_label: diar,
        display_name: name,
      }});
      location.reload();
    }}, "Сохранение…");
  }}));
  document.querySelectorAll(".sp-merge").forEach(btn => btn.addEventListener("click", async () => {{
    const row = btn.closest(".sp-row");
    if (!row) {{ toast("строка спикера не найдена"); return; }}
    const source = row.dataset.diar;
    const target = row.querySelector(".merge-target")?.value;
    if (!target) {{ toast("Выберите спикера"); return; }}
    if (!confirm(`Считать ${{source}} и ${{target}} одним человеком?`)) return;
    await guardActionButton(btn, async () => {{
      await postJson(`/api/artifacts/${{data.artifact_id}}/speaker-merge`, {{
        source_diar_label: source,
        target_diar_label: target,
      }});
      location.reload();
    }}, "Слияние…");
  }}));
  const recalc = document.getElementById("recalc");
  if (recalc) recalc.addEventListener("click", async () => {{
    await guardActionButton(recalc, async () => {{
      await postJson(`/api/artifacts/${{data.artifact_id}}/speaker-recalculate`);
      location.reload();
    }}, "Пересчёт…");
  }});
  const rebuildSummary = document.getElementById("rebuild-summary");
  if (rebuildSummary) rebuildSummary.addEventListener("click", async () => {{
    await guardActionButton(rebuildSummary, async () => {{
      await postJson(`/api/artifacts/${{data.artifact_id}}/rebuild-summary`);
      location.reload();
    }}, "Выжимка…");
  }});
  const reindexBtn = document.getElementById("reindex");
  if (reindexBtn) reindexBtn.addEventListener("click", async () => {{
    await guardActionButton(reindexBtn, async () => {{
      await postJson(`/api/artifacts/${{data.artifact_id}}/reindex`);
      location.reload();
    }}, "Индекс…");
  }});
}}
function setActionButtonBusy(btn, busy, label) {{
  if (!btn) return;
  const idle = btn.dataset.idleLabel || btn.textContent.trim();
  if (!btn.dataset.idleLabel) btn.dataset.idleLabel = idle;
  if (busy) {{
    if (!btn.dataset.minWidth && btn.offsetWidth) {{
      btn.dataset.minWidth = String(Math.ceil(btn.offsetWidth));
      btn.style.minWidth = `${{btn.dataset.minWidth}}px`;
    }}
    btn.disabled = true;
    btn.dataset.busy = "1";
    btn.setAttribute("aria-busy", "true");
    btn.innerHTML = '<span class="action-spinner" role="status" aria-hidden="true"></span>';
    btn.setAttribute("aria-label", label || idle);
  }} else {{
    btn.disabled = false;
    btn.dataset.busy = "0";
    btn.removeAttribute("aria-busy");
    btn.textContent = idle;
    btn.setAttribute("aria-label", idle);
  }}
}}
async function guardActionButton(btn, work, label) {{
  if (!btn || btn.dataset.busy === "1") return;
  if (await syncArtifactTasks()) {{
    toast("Операция уже выполняется…");
    startArtifactTaskPoll();
    return;
  }}
  setActionButtonBusy(btn, true, label);
  try {{
    await work();
  }} catch (err) {{
    if (err.status === 409) {{
      toast(err.message || "Уже выполняется");
      await syncArtifactTasks();
      startArtifactTaskPoll();
    }} else {{
      toast(err.message || String(err));
    }}
    setActionButtonBusy(btn, false);
  }}
}}
function authHeaders() {{
  const token = localStorage.getItem("skkkk_token");
  return token ? {{ Authorization: "Bearer " + token }} : {{}};
}}
function setExportButtonBusy(btn, busy) {{
  setActionButtonBusy(btn, busy, busy ? "Скачивание…" : null);
}}
async function downloadExport(btn, url) {{
  if (btn.disabled || btn.dataset.busy === "1") return;
  setExportButtonBusy(btn, true);
  try {{
    const res = await fetch(url, {{ headers: authHeaders() }});
    if (!res.ok) throw new Error(apiDetail(await res.text()));
    const blob = await res.blob();
    if (!blob.size) throw new Error("Пустой файл");
    let name = "download";
    const cd = res.headers.get("Content-Disposition") || "";
    const star = cd.match(/filename\\*=UTF-8''([^;]+)/i);
    const plain = cd.match(/filename="([^"]+)"/i);
    if (star) name = decodeURIComponent(star[1]);
    else if (plain) name = plain[1];
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = name;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(href);
  }} catch (err) {{
    toast(err.message || String(err));
  }} finally {{
    setExportButtonBusy(btn, false);
  }}
}}
if (data.artifact_id) {{
  const dlSummary = document.getElementById("download-summary");
  if (dlSummary) dlSummary.addEventListener("click", () =>
    downloadExport(dlSummary, `/artifacts/${{data.artifact_id}}/download/summary.docx`)
  );
  const dlBrief = document.getElementById("download-brief");
  if (dlBrief) dlBrief.addEventListener("click", () =>
    downloadExport(dlBrief, `/artifacts/${{data.artifact_id}}/download/brief.docx`)
  );
  const dlTranscript = document.getElementById("download-transcript");
  if (dlTranscript) dlTranscript.addEventListener("click", () =>
    downloadExport(dlTranscript, `/artifacts/${{data.artifact_id}}/download/transcript.txt`)
  );
}}
if (data.artifact_id) {{
  syncArtifactTasks().then((busy) => {{ if (busy) startArtifactTaskPoll(); }});
}}
paint();
</script>
</body>
</html>
"""


def write_transcript_html(path: Path, **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_transcript_html(**kwargs), encoding="utf-8")
