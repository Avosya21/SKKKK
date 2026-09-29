"""Страница лабораторного прогона: стадии, хеши, A/B, тепловая карта, стенограмма.

Отчёт собирается внутрь самого прогона (``<run>/report.html``) и читает только
файлы этого прогона. Глобального «последнего отчёта», в который могли бы затечь
чужие данные, здесь нет — страница всегда принадлежит конкретному run_id.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.paths import STAGE_TITLES, STAGES, LabRun, list_run_ids  # noqa: E402
from asr.lab.run import run_status  # noqa: E402

BOOTSTRAP = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
BOOTSTRAP_JS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
FONTS = "https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;500;600&family=Poppins:wght@500;600;700&display=swap"

# 8 цветов: Nemotron даёт до 8 спикеров; с 5-го цвета при 4 повторялись.
SPEAKER_COLORS = ("#5B7FA6", "#A67B7B", "#6B9080", "#B08968", "#7E6BA6", "#4F8FA0", "#A6906B", "#8A8A8A")

REPORT_NAME = "report.html"

STATUS_BADGE = {
    "ok": ("success", "готово"),
    "fail": ("danger", "упало"),
    "running": ("warning", "идёт"),
    "missing": ("secondary", "не запускалась"),
}


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _without(payload: dict[str, Any] | None, *keys: str) -> dict[str, Any]:
    """Копия без тяжёлых массивов: на странице они не нужны, файл рядом скачивается."""
    if not payload:
        return {}
    return {key: value for key, value in payload.items() if key not in keys}


def _mark_sense_words(transcript: dict[str, Any], sense_doc: dict[str, Any]) -> dict[str, Any]:
    """Пометить слова, которым оверлей дал спикера. Файлы снимка не трогаем."""
    assigned: set[int] = set()
    for decision in sense_doc.get("decisions") or []:
        if decision.get("speaker_index") is None:
            continue
        try:
            first = int(decision["word_from"])
            last = int(decision["word_to"])
        except (KeyError, TypeError, ValueError):
            continue
        assigned.update(range(first, last + 1))
    if not assigned:
        return transcript
    for replica in transcript.get("replicas") or []:
        for word in replica.get("words") or []:
            try:
                if int(word.get("i")) in assigned:
                    word["sense"] = True
            except (TypeError, ValueError):
                continue
    return transcript


def _taken_from_giga(proof: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Только принятые правки: остальные 400 расхождений на странице не нужны."""
    rows = [row for row in (proof or {}).get("diffs") or [] if row.get("pick") == "giga"]
    return [
        {
            key: row.get(key)
            for key in ("id", "op", "start", "lab", "giga", "left", "right")
        }
        for row in rows
    ]


def collect(run: LabRun, artifact_id: str) -> dict[str, Any]:
    """Всё, что показывает страница, собирается заранее и уходит в JSON."""
    meta = _read_json(run.run_json) or {}
    sense_doc = _read_json(run.resolve("sense/sense.json")) or {}
    sense_transcript = _mark_sense_words(
        _read_json(run.resolve("sense/transcript.json")) or {},
        sense_doc,
    )
    proof_doc = _read_json(run.resolve("proof/proof.json")) or {}
    voice_overlay = _read_json(run.resolve("voice/voice.json")) or {}
    if not voice_overlay:
        voice_overlay = _without(_read_json(run.stage_dir("05_voice") / "voice.json"), "clips")
    voice_match = _without(_read_json(run.resolve("voice/match.json")), "embeddings")
    return {
        "giga": _read_json(run.resolve("giga/giga.json")) or {},
        "reconcile": _read_json(run.resolve("reconcile/speakers.json")) or {},
        "reconcile_text": _without(
            _read_json(run.resolve("reconcile/text.json")), "windows"
        ),
        "proof": _without(proof_doc, "diffs"),
        "proof_taken": _taken_from_giga(proof_doc),
        "proof_transcript": _read_json(run.resolve("proof/transcript.json")) or {},
        "artifact_id": artifact_id,
        "run_id": run.run_id,
        "meta": meta,
        "stages": run_status(run),
        "runs": list_run_ids(run.artifact_root),
        "audio": _read_json(run.stage_json("01_audio")) or {},
        "diar": _without(_read_json(run.stage_dir("03_diar") / "diar.json"), "segments"),
        "heatmap": _read_json(run.stage_dir("03_diar") / "heatmap.json") or {},
        "compare": _read_json(run.stage_dir("03_diar") / "compare.json") or {},
        "paint": _without(_read_json(run.stage_dir("04_paint") / "paint.json"), "words"),
        "transcript": _read_json(run.stage_dir("06_transcript") / "transcript.json") or {},
        "verify": _without(_read_json(run.stage_dir("07_verify") / "verify.json"), "pass_two_text"),
        "sense": sense_doc,
        "sense_transcript": sense_transcript,
        "voice": voice_overlay,
        "voice_match": voice_match,
    }


def _fmt_bytes(value: int) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def _file_rows(rows: list[dict[str, Any]], artifact_id: str, run_id: str) -> str:
    if not rows:
        return '<span class="text-muted small">—</span>'
    items = []
    for row in rows:
        name = html.escape(row["path"])
        sha = html.escape(str(row.get("sha256", ""))[:8])
        size = _fmt_bytes(int(row.get("bytes") or 0))
        if row.get("external"):
            link = name
        else:
            href = f"/artifacts/{artifact_id}/lab/{run_id}/file/{row['path']}"
            link = f'<a href="{html.escape(href)}">{name}</a>'
        items.append(
            f'<li>{link} <code class="text-muted">{sha}</code> '
            f'<span class="text-muted small">{size}</span></li>'
        )
    return '<ul class="mb-0 ps-3 small">' + "".join(items) + "</ul>"


def _metrics_list(metrics: dict[str, Any]) -> str:
    if not metrics:
        return '<span class="text-muted small">—</span>'
    parts = [
        f"<div><span class='text-muted'>{html.escape(str(key))}</span> "
        f"<strong>{html.escape(json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value))}</strong></div>"
        for key, value in metrics.items()
    ]
    return '<div class="small">' + "".join(parts) + "</div>"


def _stage_cards(data: dict[str, Any]) -> str:
    cards = []
    for stage in STAGES:
        row = data["stages"].get(stage) or {"status": "missing"}
        state = row.get("status") or "missing"
        tone, label = STATUS_BADGE.get(state, ("secondary", state))
        elapsed = row.get("elapsed_sec")
        error = row.get("error")
        params = row.get("params") or {}
        cards.append(
            f"""
<div class="card stage-card mb-2">
  <div class="card-body py-3">
    <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
      <span class="badge text-bg-{tone}">{html.escape(label)}</span>
      <strong>{html.escape(stage)}</strong>
      <span class="text-muted">{html.escape(STAGE_TITLES.get(stage, ""))}</span>
      <span class="ms-auto text-muted small">{'' if elapsed is None else f'{elapsed} с'}</span>
    </div>
    {f'<div class="alert alert-danger py-2 small mb-2">{html.escape(str(error))}</div>' if error else ''}
    <div class="row g-3">
      <div class="col-md-4">
        <div class="label">Параметры</div>
        <pre class="mini">{html.escape(json.dumps(params, ensure_ascii=False, indent=1))}</pre>
      </div>
      <div class="col-md-4">
        <div class="label">Метрики</div>
        {_metrics_list(row.get("metrics") or {})}
      </div>
      <div class="col-md-4">
        <div class="label">Входы</div>
        {_file_rows(row.get("inputs") or [], data["artifact_id"], data["run_id"])}
        <div class="label mt-2">Выходы</div>
        {_file_rows(row.get("outputs") or [], data["artifact_id"], data["run_id"])}
      </div>
    </div>
  </div>
</div>"""
        )
    return "".join(cards)


def render(data: dict[str, Any]) -> str:
    artifact_id = data["artifact_id"]
    run_id = data["run_id"]
    meta = data["meta"]
    source = meta.get("source") or {}
    audio_metrics = (data["audio"].get("metrics") or {}) if data["audio"] else {}
    verify = data["verify"]
    verdict = verify.get("verdict")
    verdict_tone = {"ok": "success", "warn": "warning", "fail": "danger"}.get(verdict, "secondary")

    run_options = "".join(
        f'<option value="{html.escape(rid)}"{" selected" if rid == run_id else ""}>{html.escape(rid)}</option>'
        for rid in data["runs"]
    )
    noise_floor = audio_metrics.get("noise_floor_db")
    if noise_floor is None:
        noise_floor = (audio_metrics.get("after") or {}).get("noise_floor_db", "—")
    duration = audio_metrics.get("duration_sec", "—")
    sense = data.get("sense") or {}
    sense_tr = data.get("sense_transcript") or {}
    frozen_replicas = (data.get("transcript") or {}).get("replica_count") or "—"
    sense_banner = ""
    sense_block = ""
    if sense_tr.get("replicas"):
        sense_banner = (
            '<div class="alert alert-success py-2 mb-3">'
            f"Cursor разнёс <strong>{html.escape(str(sense.get('assigned_words', '—')))}</strong> спорных слов. "
            f"Реплик стало <strong>{html.escape(str(sense_tr.get('replica_count', '—')))}</strong> "
            f"(в снимке {html.escape(str(frozen_replicas))}). "
            "Зелёным подчёркнуто то, что присвоила разноска. Файлы 01–07 не менялись. "
            '<a href="#sense-block">к разноске</a>'
            "</div>"
        )
        sense_block = """
  <h2 class="h5 mt-4 mb-2" id="sense-block">Стенограмма после смысловой разноски</h2>
  <p class="small text-muted mb-2">Оверлей Cursor. Снимок 01–07 ниже, без изменений.</p>
  <div class="card mb-3"><div class="card-body" id="sense-transcript"></div></div>
"""

    giga = data.get("giga") or {}
    giga_summary = giga.get("summary") or {}
    reconcile = data.get("reconcile") or {}
    span_counts = reconcile.get("span_counts") or {}
    proof = data.get("proof") or {}
    proof_summary = proof.get("summary") or {}
    giga_banner = ""
    giga_block = ""
    if giga_summary:
        parts = [
            f"GigaAM услышал то же аудио сам: <strong>{giga_summary.get('turn_count', '—')}</strong> реплик, "
            f"<strong>{giga_summary.get('speaker_count', '—')}</strong> спикеров "
            f"(у нас {html.escape(str((data.get('paint') or {}).get('speakers', '—')))}), "
            f"{giga_summary.get('chars_plain', '—')} символов."
        ]
        if span_counts:
            parts.append(
                "Спорные куски снимка: подтверждено "
                f"<strong>{span_counts.get('confirmed', 0)}</strong>, "
                f"закрыто <strong>{span_counts.get('closed', 0)}</strong>, "
                f"конфликт <strong>{span_counts.get('conflict', 0)}</strong>, "
                f"без вердикта {span_counts.get('unverified', 0) + span_counts.get('open', 0)}."
            )
        if proof_summary:
            parts.append(
                f"Вычитка: из <strong>{proof_summary.get('asked', '—')}</strong> расхождений "
                f"взято у GigaAM <strong>{proof_summary.get('taken_giga', '—')}</strong>."
            )
        giga_banner = (
            '<div class="alert alert-secondary py-2 mb-3">'
            + " ".join(parts)
            + ' <a href="#giga-block">к этапу 3</a></div>'
        )
        giga_block = """
  <h2 class="h5 mt-4 mb-2" id="giga-block">Этап 3 — сверка с GigaAM</h2>
  <p class="small text-muted mb-2">
    Свой прогон GigaAM на том же аудио. Снимок 01–07 и разноска Cursor не менялись.
  </p>
  <div class="card mb-3"><div class="card-body">
    <div class="label">Спикеры GigaAM против лабораторных</div>
    <div id="giga-speakers" class="small"></div>
  </div></div>
  <div class="card mb-3"><div class="card-body">
    <div class="label">Спорные куски: что сказал GigaAM</div>
    <div id="giga-spans" class="small"></div>
  </div></div>
  <div class="card mb-3"><div class="card-body">
    <div class="label">Вычитка: что забрали у GigaAM</div>
    <div id="giga-taken" class="small"></div>
  </div></div>
  <h2 class="h5 mt-4 mb-2">Итоговая стенограмма (снимок + разноска + вычитка)</h2>
  <p class="small text-muted mb-2">
    Как на боевой стенограмме: клик по времени или реплике перематывает аудио.
    Имена — после «Запомнить». Правки GigaAM собраны в блоке вычитки выше.
  </p>
  <div id="proof-transcript" class="mb-3"></div>
  <h2 class="h5 mt-4 mb-2">Как услышал GigaAM (его собственная нарезка)</h2>
  <p class="small text-muted mb-2">Показано для сверки, в итог его нарезка не идёт.</p>
  <div class="card mb-3"><div class="card-body" id="giga-turns"></div></div>
"""
    clean_url = f"/artifacts/{artifact_id}/lab/{run_id}/file/01_audio/clean_16k.wav"
    voice_match = data.get("voice_match") or {}
    voice_banner = ""
    if voice_match.get("matches"):
        voice_banner = (
            '<div class="alert alert-info py-2 mb-3">'
            "Голоса лаборатории: слепки WeSpeaker готовы. "
            "Имя руками и «Запомнить» пишет в ту же базу, что прод, метка "
            "<code>speaker_N</code>. Боевой match JSON не трогается. "
            '<a href="#voice-block">к карточкам</a></div>'
        )
    voice_block = """
  <h2 class="h5 mt-4 mb-2" id="voice-block">Голоса лаборатории</h2>
  <p class="small text-muted mb-2">
    Клипы из уверенных слов снимка и разноски Cursor. «Запомнить» и «Слить»
    пишут в speaker_profiles, файл — voice/match.json этого прогона.
  </p>
  <div class="card mb-3"><div class="card-body" id="voice"></div></div>
"""

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Лаборатория расшифровки — {html.escape(artifact_id)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{FONTS}" rel="stylesheet">
<link href="{BOOTSTRAP}" rel="stylesheet">
<style>
  body {{ font-family: "Open Sans", system-ui, sans-serif; background:#F7F6F3; color:#2B2B2B; }}
  h1,h2,h3,.h5 {{ font-family: Poppins, "Open Sans", sans-serif; }}
  .label {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; color:#8A8A8A; margin-bottom:.15rem; }}
  .mini {{ font-size:.72rem; background:#EFEDE8; padding:.4rem .5rem; border-radius:.35rem; margin:0; max-height:9rem; overflow:auto; }}
  .stage-card {{ border-color:#E3E0D9; }}
  .card {{ border-color:#E3E0D9; }}
  .word {{ cursor:pointer; border-radius:.2rem; padding:0 .1rem; }}
  .word:hover {{ background:#E3E0D9; }}
  .w-contested {{ background:#FDF3D8; border-bottom:2px solid #C9A227; }}
  .w-unassigned {{ background:#FBE3E3; border-bottom:2px solid #B3403F; }}
  .w-sense {{ background:#E7F3EA; border-bottom:2px solid #6B9080; }}
  .w-proof {{ background:#E6EDF5; border-bottom:2px solid #4A6FA5; }}
  .diffrow {{ border-bottom:1px solid #EFEDE8; padding:.35rem 0; }}
  .diffrow:last-child {{ border-bottom:0; }}
  .old {{ color:#B3403F; text-decoration:line-through; }}
  .new {{ color:#2F6B4F; font-weight:600; }}
  .replica {{ border-left:3px solid var(--spk,#999); padding-left:.7rem; margin-bottom:.8rem; }}
  .turn {{ display:grid; grid-template-columns:92px 1fr; gap:8px; align-items:baseline; padding:3px 4px; border-radius:6px; cursor:pointer; }}
  .turn:hover {{ background:rgba(255,255,255,.6); }}
  .turn.active {{ background:#e0f2fe; }}
  .turn:focus-visible {{ outline:2px solid #5B7FA6; outline-offset:2px; }}
  .when {{
    border:1px solid #cbd5e1; background:#fff; border-radius:999px;
    padding:1px 6px; cursor:pointer; font:inherit; font-size:.75rem;
    white-space:nowrap; color:#64748b; line-height:1.3;
    align-self:baseline; justify-self:start;
  }}
  .line {{ margin:0; line-height:1.35; font-size:.92rem; color:#2B2B2B; }}
  .who {{ font-weight:650; }}
  @media (max-width:768px) {{
    .turn {{ grid-template-columns:1fr; gap:4px; padding:8px 2px; }}
  }}
  #heat {{ width:100%; height:110px; display:block; background:#fff; border:1px solid #E3E0D9; border-radius:.35rem; }}
  .sticky-player {{ position:sticky; top:0; z-index:20; background:#F7F6F3; padding:.6rem 0; border-bottom:1px solid #E3E0D9; }}
  code {{ color:#6B6B6B; }}
  .lab-speaker-card {{ border-left:4px solid var(--spk,#8A8A8A); }}
  #lab-toast {{ position:fixed; right:1rem; bottom:1rem; z-index:40; display:none; }}
</style>
</head>
<body>
<div class="container py-4">

  <div class="d-flex flex-wrap align-items-center gap-3 mb-3">
    <div>
      <h1 class="h4 mb-0">Лаборатория расшифровки</h1>
      <div class="text-muted small">{html.escape(artifact_id)} · прогон <code>{html.escape(run_id)}</code></div>
    </div>
    <div class="ms-auto d-flex align-items-center gap-2">
      <label class="text-muted small mb-0" for="run-picker">Прогон</label>
      <select id="run-picker" class="form-select form-select-sm" style="width:auto">{run_options}</select>
    </div>
  </div>

  <div class="row g-3 mb-3">
    <div class="col-md-3"><div class="card h-100"><div class="card-body py-3">
      <div class="label">Исходник</div>
      <div class="small">sha256 <code>{html.escape(str(source.get("sha256", ""))[:12])}</code></div>
      <div class="small text-muted">{_fmt_bytes(int(source.get("bytes") or 0))}</div>
      <div class="small text-muted">создан {html.escape(str(meta.get("created_at", "")))}</div>
    </div></div></div>
    <div class="col-md-3"><div class="card h-100"><div class="card-body py-3">
      <div class="label">Аудио прогона</div>
      <div class="h5 mb-0">{duration} с</div>
      <div class="small text-muted">шумовая полка {noise_floor} дБ</div>
    </div></div></div>
    <div class="col-md-3"><div class="card h-100"><div class="card-body py-3">
      <div class="label">Покраска слов</div>
      <div class="h5 mb-0">{(data["paint"].get("counts") or {}).get("assigned", "—")} / {data["paint"].get("word_count", "—")}</div>
      <div class="small text-muted">спорных {(data["paint"].get("counts") or {}).get("contested", "—")}, бесхозных {(data["paint"].get("counts") or {}).get("unassigned", "—")}</div>
    </div></div></div>
    <div class="col-md-3"><div class="card h-100"><div class="card-body py-3">
      <div class="label">Проверка</div>
      <div><span class="badge text-bg-{verdict_tone}">{html.escape(str(verdict or "не запускалась"))}</span></div>
      <div class="small text-muted">{html.escape("; ".join(verify.get("reasons") or []) or "—")}</div>
    </div></div></div>
  </div>

  <div class="sticky-player">
    <div class="label">Аудио прогона</div>
    <audio id="player" class="w-100" controls preload="none" src="{html.escape(clean_url)}"></audio>
  </div>

  {giga_banner}
  {sense_banner}
  {voice_banner}

  {giga_block}

  {sense_block}

  {voice_block}

  <h2 class="h5 mt-4 mb-2">Стадии</h2>
  {_stage_cards(data)}

  <h2 class="h5 mt-4 mb-2">Вероятности спикеров</h2>
  <div class="card mb-3"><div class="card-body">
    <canvas id="heat"></canvas>
    <div id="heat-legend" class="d-flex gap-3 mt-2 small"></div>
    <div class="text-muted small mt-1">Клик по полосе — перемотка. Ярче — выше вероятность.</div>
  </div></div>

  <h2 class="h5 mt-4 mb-2">Стенограмма снимка (без Cursor)</h2>
  <div class="card mb-3"><div class="card-body" id="transcript"></div></div>

  <h2 class="h5 mt-4 mb-2">Слова вне речи</h2>
  <div class="card mb-5"><div class="card-body" id="orphans"></div></div>

</div>
<div id="lab-toast" class="toast align-items-center text-bg-dark border-0" role="status">
  <div class="d-flex"><div class="toast-body" id="lab-toast-body"></div></div>
</div>

<script id="data" type="application/json">{json.dumps(data, ensure_ascii=False)}</script>
<script src="{BOOTSTRAP_JS}"></script>
<script>
const data = JSON.parse(document.getElementById("data").textContent);
const colors = {json.dumps(list(SPEAKER_COLORS))};
const player = document.getElementById("player");

function seek(sec) {{
  const value = Number(sec);
  if (!Number.isFinite(value)) return;
  player.currentTime = Math.max(0, value);
  player.play().catch(() => {{}});
}}

document.getElementById("run-picker").addEventListener("change", (event) => {{
  window.location.search = "?run=" + encodeURIComponent(event.target.value);
}});

function speakerColor(name) {{
  const match = /speaker_(\\d+)/.exec(name || "");
  return match ? colors[Number(match[1]) % colors.length] : "#64748b";
}}

function speakerLabel(name) {{
  const matchDoc = data.voice_match || {{}};
  const row = (matchDoc.matches || {{}})[name] || {{}};
  const mapped = (matchDoc.merge_map || {{}})[name];
  const target = mapped && mapped !== name ? (matchDoc.matches || {{}})[mapped] || {{}} : {{}};
  return row.profile_name || target.profile_name
    || (matchDoc.display_names || {{}})[name]
    || name || "unknown";
}}

function esc(value) {{
  return String(value ?? "").replace(/[&<>"]/g, (ch) => (
    {{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }}[ch]
  ));
}}

function drawHeatmap() {{
  const heat = data.heatmap || {{}};
  const canvas = document.getElementById("heat");
  const values = heat.values || [];
  if (!values.length) {{
    canvas.replaceWith(Object.assign(document.createElement("div"),
      {{ className: "text-muted small", textContent: "Диаризация ещё не запускалась." }}));
    return;
  }}
  const speakers = heat.speakers || values[0].length;
  const width = canvas.clientWidth || 900;
  const rowHeight = Math.floor(110 / speakers);
  canvas.width = width;
  canvas.height = rowHeight * speakers;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const step = width / values.length;
  for (let column = 0; column < values.length; column += 1) {{
    for (let speaker = 0; speaker < speakers; speaker += 1) {{
      const probability = values[column][speaker] || 0;
      ctx.globalAlpha = Math.min(1, Math.max(0.03, probability));
      ctx.fillStyle = colors[speaker % colors.length];
      ctx.fillRect(column * step, speaker * rowHeight, Math.max(1, step), rowHeight - 1);
    }}
  }}
  ctx.globalAlpha = 1;
  canvas.addEventListener("click", (event) => {{
    const rect = canvas.getBoundingClientRect();
    seek(((event.clientX - rect.left) / rect.width) * values.length * (heat.column_sec || 0.08));
  }});
  document.getElementById("heat-legend").innerHTML = Array.from({{ length: speakers }})
    .map((_, index) => `<span><span style="display:inline-block;width:.7rem;height:.7rem;background:${{colors[index % colors.length]}};border-radius:.15rem"></span> ${{speakerLabel("speaker_" + index)}}</span>`)
    .join("");
}}

function renderTranscript() {{
  fillReplicas("transcript", (data.transcript || {{}}).replicas || [], "Стенограмма ещё не собрана.");
}}

function renderSenseTranscript() {{
  const replicas = (data.sense_transcript || {{}}).replicas || [];
  fillReplicas("sense-transcript", replicas, "Оверлей Cursor ещё не запускался. Этап 01–07 не меняется.");
}}

function fillReplicas(boxId, replicas, emptyText, useFinal) {{
  const box = document.getElementById(boxId);
  if (!box) return;
  if (!replicas.length) {{
    box.innerHTML = `<span class="text-muted small">${{emptyText}}</span>`;
    return;
  }}
  box.innerHTML = replicas.map((replica) => {{
    const words = replica.words.map((word) => {{
      const shown = useFinal ? (word.final ?? word.word) : word.word;
      if (useFinal && !shown) return "";
      const cls = useFinal && word.proof ? "word w-proof"
        : word.sense ? "word w-sense"
        : word.verdict === "contested" ? "word w-contested"
        : word.verdict === "unassigned" ? "word w-unassigned" : "word";
      const heard = word.proof ? ` · было «${{word.word}}»` : "";
      const title = `${{word.start.toFixed(2)}}–${{word.end.toFixed(2)}} с · ${{word.verdict}}${{word.sense ? " · cursor" : ""}}${{heard}}`;
      return `<span class="${{cls}}" title="${{title}}" data-t="${{word.start}}">${{shown}}</span>`;
    }}).filter(Boolean).join(" ");
    const flag = replica.flagged
      ? `<span class="badge text-bg-warning ms-2">${{replica.flagged}} под вопросом</span>` : "";
    return `<div class="replica" style="--spk:${{speakerColor(replica.speaker)}}">
      <div class="small text-muted">
        <span class="fw-semibold" style="color:${{speakerColor(replica.speaker)}}">${{speakerLabel(replica.speaker)}}</span>
        · <a href="#" data-t="${{replica.start}}" class="text-decoration-none">${{replica.start.toFixed(1)}} с</a>${{flag}}
      </div>
      <div>${{words}}</div>
    </div>`;
  }}).join("");
  box.querySelectorAll("[data-t]").forEach((node) => node.addEventListener("click", (event) => {{
    event.preventDefault();
    seek(node.dataset.t);
  }}));
}}

const SPAN_BADGE = {{
  confirmed: ["success", "GigaAM подтвердил"],
  closed: ["primary", "GigaAM закрыл"],
  conflict: ["danger", "конфликт"],
  unverified: ["secondary", "GigaAM не судья"],
  open: ["warning", "так и открыт"],
}};

function timeLink(seconds) {{
  return `<a href="#" data-t="${{seconds}}" class="text-decoration-none">${{Number(seconds).toFixed(1)}} с</a>`;
}}

function bindSeek(box) {{
  box.querySelectorAll("[data-t]").forEach((node) => node.addEventListener("click", (event) => {{
    event.preventDefault();
    seek(node.dataset.t);
  }}));
}}

function renderGigaSpeakers() {{
  const box = document.getElementById("giga-speakers");
  if (!box) return;
  const rows = ((data.giga || {{}}).summary || {{}}).speakers || [];
  const map = (data.reconcile || {{}}).speaker_map || {{}};
  if (!rows.length) {{
    box.innerHTML = '<span class="text-muted">Прогон GigaAM не запускался.</span>';
    return;
  }}
  box.innerHTML = rows.map((row) => {{
    const mapped = map[row.speaker] || {{}};
    const target = mapped.speaker_index === null || mapped.speaker_index === undefined
      ? '<span class="text-muted">ни с кем не сошёлся</span>'
      : `→ ${{speakerLabel("speaker_" + mapped.speaker_index)}}`;
    const share = mapped.share === undefined ? "" : ` <span class="text-muted">совпадение ${{mapped.share}}</span>`;
    return `<div class="diffrow">${{row.speaker}} <span class="text-muted">(${{row.seconds}} с, ${{Math.round(row.share * 100)}}%)</span> ${{target}}${{share}}</div>`;
  }}).join("");
}}

function renderGigaSpans() {{
  const box = document.getElementById("giga-spans");
  if (!box) return;
  const rows = (data.reconcile || {{}}).spans || [];
  if (!rows.length) {{
    box.innerHTML = '<span class="text-muted">Сверка не запускалась.</span>';
    return;
  }}
  const order = ["conflict", "closed", "confirmed", "open", "unverified"];
  const sorted = rows.slice().sort((a, b) =>
    order.indexOf(a.verdict) - order.indexOf(b.verdict) || a.start - b.start);
  box.innerHTML = sorted.map((row) => {{
    const [tone, label] = SPAN_BADGE[row.verdict] || ["secondary", row.verdict];
    const ours = row.our_speaker_index === null
      ? "у нас ответа нет"
      : `у нас ${{speakerLabel("speaker_" + row.our_speaker_index)}}${{row.from_sense ? " (разноска)" : ""}}`;
    const theirs = row.giga.speaker_index === null || row.giga.speaker_index === undefined
      ? `<span class="text-muted">${{row.giga.reason}}</span>`
      : `GigaAM: ${{speakerLabel("speaker_" + row.giga.speaker_index)}} <span class="text-muted">(${{row.giga.giga_speaker}})</span>`;
    return `<div class="diffrow">
      <span class="badge text-bg-${{tone}} me-1">${{label}}</span>
      ${{timeLink(row.start)}} «${{row.text}}» — ${{ours}}; ${{theirs}}
    </div>`;
  }}).join("");
  bindSeek(box);
}}

function renderGigaTaken() {{
  const box = document.getElementById("giga-taken");
  if (!box) return;
  const rows = data.proof_taken || [];
  const summary = (data.proof || {{}}).summary || {{}};
  if (!rows.length) {{
    box.innerHTML = '<span class="text-muted">Вычитка не запускалась или ничего не взято.</span>';
    return;
  }}
  box.innerHTML = `<div class="text-muted mb-2">Спрошено ${{summary.asked}}, взято у GigaAM ${{summary.taken_giga}}, оставлено своё ${{summary.kept_lab}}.</div>` +
    rows.map((row) => `<div class="diffrow">
      ${{timeLink(row.start)}}
      <span class="old">${{row.lab || "∅"}}</span> → <span class="new">${{row.giga || "∅"}}</span>
      <div class="text-muted">…${{row.left.slice(-60)}} | ${{row.right.slice(0, 60)}}…</div>
    </div>`).join("");
  bindSeek(box);
}}

function renderGigaTurns() {{
  const box = document.getElementById("giga-turns");
  if (!box) return;
  const rows = (data.giga || {{}}).turns || [];
  if (!rows.length) {{
    box.innerHTML = '<span class="text-muted">Прогон GigaAM не запускался.</span>';
    return;
  }}
  box.innerHTML = rows.map((row) => `<div class="diffrow">
    <span class="fw-semibold">${{row.speaker_label || row.speaker}}</span> ${{timeLink(row.start)}}
    <div>${{row.text}}</div></div>`).join("");
  bindSeek(box);
}}

function replicaText(replica, useFinal) {{
  const words = replica.words || [];
  if (useFinal && words.length) {{
    return words.map((word) => (word.final ?? word.word) || "").filter(Boolean).join(" ");
  }}
  return replica.text || words.map((word) => word.word).filter(Boolean).join(" ");
}}

function fillTurns(boxId, replicas, emptyText, useFinal) {{
  const box = document.getElementById(boxId);
  if (!box) return;
  if (!replicas.length) {{
    box.innerHTML = `<span class="text-muted small">${{emptyText}}</span>`;
    return;
  }}
  box.innerHTML = replicas.map((replica) => {{
    const text = replicaText(replica, useFinal);
    if (!text) return "";
    const color = speakerColor(replica.speaker);
    const start = replica.start;
    const end = replica.end != null ? replica.end : start;
    return `<article class="turn" tabindex="0" data-start="${{start}}" data-end="${{end}}" data-speaker="${{esc(replica.speaker)}}">
      <button type="button" class="when" style="border-color:${{color}}" title="Перейти к ${{fmtClock(start)}}" aria-label="Перейти к ${{fmtClock(start)}}–${{fmtClock(end)}}">
        ${{fmtClock(start)}}–${{fmtClock(end)}}
      </button>
      <p class="line"><span class="who" style="color:${{color}}">${{esc(speakerLabel(replica.speaker))}}</span>: ${{esc(text)}}</p>
    </article>`;
  }}).join("");
  box.querySelectorAll(".turn").forEach((node) => {{
    const go = () => seek(node.dataset.start);
    node.addEventListener("click", go);
    node.addEventListener("keydown", (event) => {{
      if (event.key === "Enter" || event.key === " ") {{
        event.preventDefault();
        go();
      }}
    }});
  }});
}}

function renderProofTranscript() {{
  fillTurns(
    "proof-transcript",
    (data.proof_transcript || {{}}).replicas || [],
    "Вычитка не запускалась.",
    true,
  );
}}

function fmtClock(sec) {{
  const t = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  if (h) {{
    return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }}
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}}

function showToast(text) {{
  const box = document.getElementById("lab-toast");
  const body = document.getElementById("lab-toast-body");
  if (!box || !body) {{ window.alert(text); return; }}
  body.textContent = text;
  box.style.display = "block";
  window.setTimeout(() => {{ box.style.display = "none"; }}, 2500);
}}

async function postLab(path, payload) {{
  const res = await fetch(path, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json;charset=utf-8" }},
    body: JSON.stringify(payload),
  }});
  const text = await res.text();
  if (!res.ok) {{
    let detail = text;
    try {{ detail = JSON.parse(text).detail || text; }} catch (_) {{}}
    throw new Error(detail);
  }}
  return text;
}}

function renderVoice() {{
  const box = document.getElementById("voice");
  if (!box) return;
  const voice = data.voice || {{}};
  const match = data.voice_match || {{}};
  const matches = match.matches || {{}};
  const labels = Object.keys(matches);
  if (!labels.length) {{
    if (!voice.clip_count) {{
      box.innerHTML = '<span class="text-muted small">Слепки голосов не считались. Запустите <code>asr/lab_voice.py</code>.</span>';
      return;
    }}
    const cross = (voice.cross_speaker_cosine || [])
      .map((row) => `<li>${{row.a}} ↔ ${{row.b}}: <strong>${{row.cosine}}</strong></li>`).join("");
    box.innerHTML = `<div class="small text-muted">Клипов ${{voice.clip_count}}. Файл match.json ещё не собран.</div>
      <ul class="small ps-3 mb-0">${{cross || "<li class='text-muted'>—</li>"}}</ul>`;
    return;
  }}
  const mergeOpts = (self) => labels.filter((label) => label !== self)
    .map((label) => {{
      const row = matches[label] || {{}};
      const name = row.profile_name || row.suggested_profile_name || label;
      return `<option value="${{label}}">${{name}} (${{label}})</option>`;
    }}).join("");
  const cards = labels.map((label) => {{
    const row = matches[label] || {{}};
    const status = row.status || "new";
    const name = (row.profile_name || row.suggested_profile_name || "").trim();
    const headline = name || label;
    const enroll = status === "confirmed" ? "Обновить" : "Запомнить";
    const badge = status === "confirmed" ? "success" : (status === "merged_duplicate" ? "secondary" : "light");
    const score = row.suggested_score != null ? row.suggested_score : row.score;
    const hint = score != null && status !== "confirmed"
      ? `<div class="small text-muted mb-2">Похож на <strong>${{row.suggested_profile_name || "—"}}</strong> (${{score}})</div>`
      : "";
    const cands = ((match.profile_candidates || {{}})[label] || (voice.profile_candidates || {{}})[label] || [])
      .map((item) => `<li>${{item.display_name}} <span class="text-muted">(${{item.scope}})</span>: <strong>${{item.cosine}}</strong></li>`)
      .join("");
    const others = mergeOpts(label);
    const merge = others
      ? `<div class="mt-2 pt-2 border-top">
           <label class="form-label small text-muted mb-1">Слить с</label>
           <div class="input-group input-group-sm">
             <select class="form-select merge-target"><option value="">— выбрать —</option>${{others}}</select>
             <button type="button" class="btn btn-outline-secondary merge-btn">Слить</button>
           </div>
         </div>`
      : "";
    return `<div class="col-md-6">
      <article class="card lab-speaker-card h-100" data-speaker="${{label}}" style="--spk:${{speakerColor(label)}}">
        <div class="card-body d-flex flex-column">
          <div class="d-flex justify-content-between gap-2 mb-1">
            <h3 class="h6 mb-0">${{headline}}</h3>
            <span class="badge text-bg-${{badge}}">${{status}}</span>
          </div>
          <p class="font-monospace small text-muted mb-2">${{label}}</p>
          ${{hint}}
          <p class="small mb-2">Эталон ${{fmtClock(row.clip_start)}}–${{fmtClock(row.clip_end)}}
            <span class="text-muted">(${{(Number(row.clip_duration) || 0).toFixed(1)}} с)</span></p>
          <button type="button" class="btn btn-sm btn-outline-secondary play-clip align-self-start mb-2"
            data-start="${{row.clip_start}}" data-end="${{row.clip_end}}">Прослушать</button>
          <ul class="small ps-3 mb-2">${{cands || "<li class='text-muted'>в базе нет профилей</li>"}}</ul>
          <div class="mt-auto pt-2 border-top">
            <label class="form-label small mb-1">Имя спикера</label>
            <div class="input-group input-group-sm">
              <input class="form-control name-inp" type="text" autocomplete="name"
                placeholder="Иван Петров" value="${{name.replaceAll('"', "&quot;")}}">
              <button type="button" class="btn btn-primary enroll">${{enroll}}</button>
            </div>
            ${{merge}}
          </div>
        </div>
      </article>
    </div>`;
  }}).join("");
  const cross = (voice.cross_speaker_cosine || [])
    .map((row) => `<li>${{row.a}} ↔ ${{row.b}}: <strong>${{row.cosine}}</strong></li>`).join("");
  box.innerHTML = `
    <div class="small text-muted mb-2">Клипов ${{voice.clip_count || labels.length}}. Запись в БД только по кнопке, боевой match не трогается.</div>
    <div class="row g-3">${{cards}}</div>
    <div class="label mt-3">Между спикерами</div>
    <ul class="small ps-3 mb-0">${{cross || "<li class='text-muted'>—</li>"}}</ul>`;
  box.querySelectorAll(".play-clip").forEach((btn) =>
    btn.addEventListener("click", () => seek(btn.dataset.start)));
  box.querySelectorAll(".enroll").forEach((btn) => {{
    btn.addEventListener("click", async () => {{
      const card = btn.closest("[data-speaker]");
      const diar = card.dataset.speaker;
      const inp = card.querySelector(".name-inp");
      const name = inp.value.trim();
      if (!name) {{ showToast("Введите имя"); inp.focus(); return; }}
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = "…";
      try {{
        await postLab(`/api/artifacts/${{data.artifact_id}}/lab/${{data.run_id}}/speaker-enroll`, {{
          diar_label: diar, display_name: name,
        }});
        showToast("Сохранено: " + name);
        location.reload();
      }} catch (err) {{
        showToast("Ошибка: " + err.message);
        btn.disabled = false;
        btn.textContent = prev;
      }}
    }});
  }});
  box.querySelectorAll(".merge-btn").forEach((btn) => {{
    btn.addEventListener("click", async () => {{
      const card = btn.closest("[data-speaker]");
      const source = card.dataset.speaker;
      const sel = card.querySelector(".merge-target");
      const target = sel ? sel.value : "";
      if (!target) {{ showToast("Выберите спикера"); sel && sel.focus(); return; }}
      if (!confirm("Слить " + source + " с " + target + "?")) return;
      btn.disabled = true;
      try {{
        await postLab(`/api/artifacts/${{data.artifact_id}}/lab/${{data.run_id}}/speaker-merge`, {{
          source_diar_label: source, target_diar_label: target,
        }});
        location.reload();
      }} catch (err) {{
        showToast("Ошибка: " + err.message);
        btn.disabled = false;
      }}
    }});
  }});
}}

function renderOrphans() {{
  const box = document.getElementById("orphans");
  const verify = data.verify || {{}};
  const orphans = verify.orphans || [];
  if (!verify.verdict) {{
    box.innerHTML = '<span class="text-muted small">Проверка не запускалась.</span>';
    return;
  }}
  if (!orphans.length) {{
    box.innerHTML = '<span class="text-success small">Ни одно слово не попало в тишину.</span>';
    return;
  }}
  let summary = `Сходство двух проходов ${{verify.pass_similarity}}.`;
  if (verify.loud_orphan_count !== undefined) {{
    // Громкие слова вне речи — искажённый голос, который VAD не узнал; провал считается только по тишине.
    const spans = (verify.loud_spans || []).filter((s) => s.words >= 5).map((s) =>
      `<span class="word text-decoration-underline" data-t="${{s.start}}">${{fmtClock(s.start)}}–${{fmtClock(s.end)}}</span> (${{s.words}})`);
    summary += ` На тишине: ${{verify.silence_orphan_count}} (порог тишины ${{(verify.levels || {{}}).silence_below_db}} дБ),` +
      ` на громком звуке без речи по VAD: ${{verify.loud_orphan_count}}` + (spans.length ? ` — ${{spans.join(", ")}}` : "") + ".";
  }}
  box.innerHTML = `<div class="small text-muted mb-2">${{summary}}</div>` +
    orphans.map((row) => `<span class="badge ${{row.silence === false ? "bg-warning-subtle text-body" : "text-bg-light"}} border me-1 mb-1 word" data-t="${{row.start}}">
      ${{row.word}} <span class="text-muted">${{row.start.toFixed(1)}} с · покрытие ${{row.vad_coverage}}${{row.level_db === undefined ? "" : ` · ${{row.level_db}} дБ`}}</span></span>`).join("");
  box.querySelectorAll("[data-t]").forEach((node) =>
    node.addEventListener("click", () => seek(node.dataset.t)));
}}

drawHeatmap();
renderTranscript();
renderSenseTranscript();
renderGigaSpeakers();
renderGigaSpans();
renderGigaTaken();
renderGigaTurns();
renderProofTranscript();
renderVoice();
renderOrphans();
player.addEventListener("timeupdate", () => {{
  const t = player.currentTime;
  document.querySelectorAll("#proof-transcript .turn").forEach((el) => {{
    const a = Number(el.dataset.start);
    const b = Number(el.dataset.end);
    el.classList.toggle("active", t >= a && t < b);
  }});
}});
window.addEventListener("resize", drawHeatmap);
</script>
</body>
</html>
"""


def write_lab_report(artifact_root: Path, run_id: str, *, artifact_id: str) -> Path:
    """Собрать report.html внутри прогона. Всегда заново — устаревший отчёт хуже, чем медленный."""
    run = LabRun(artifact_root, run_id)
    if not run.run_json.is_file():
        raise FileNotFoundError(f"нет прогона {run_id} в {artifact_root}")
    path = run.root / REPORT_NAME
    path.write_text(render(collect(run, artifact_id)), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать страницу лабораторного прогона")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", help="по умолчанию последний прогон")
    args = parser.parse_args()

    from app.app.config import get_settings
    from app.app.services.storage import artifact_dir

    root = artifact_dir(get_settings(), args.project_id, args.artifact_id)
    run_id = args.run_id
    if not run_id:
        runs = list_run_ids(root)
        if not runs:
            print(f"нет прогонов в {root}", file=sys.stderr)
            return 2
        run_id = runs[0]
    print(write_lab_report(root, run_id, artifact_id=args.artifact_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
