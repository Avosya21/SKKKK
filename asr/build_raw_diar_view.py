"""Сырая диаризация для прослушивания: без Gemma, без сглаживания, несколько треков.

python asr/build_raw_diar_view.py
python asr/build_raw_diar_view.py --manifest experiments/diar-presets/out/manifest.json
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from speakers import Turn, format_clock, talk_time  # noqa: E402

COLORS = (
    "#2563eb",
    "#dc2626",
    "#059669",
    "#d97706",
    "#7c3aed",
    "#db2777",
    "#0d9488",
    "#4f46e5",
)


def _turn_from_row(row: dict) -> Turn:
    start = float(row.get("start") or 0.0)
    end = float(row.get("end") or start)
    return Turn(
        start=start,
        end=end,
        speaker=str(row.get("speaker") or ""),
        text="",
    )


def load_segments(path: Path) -> tuple[list[Turn], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = {
        "source": data.get("source", ""),
        "params": data.get("params") or {},
        "speaker_count": data.get("speaker_count"),
        "model": data.get("model"),
    }
    rows = data.get("segments") or []
    turns: list[Turn] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        speaker = str(row.get("speaker") or "").strip()
        if not speaker:
            continue
        turns.append(_turn_from_row(row))
    return turns, meta


def load_raw_clusters(path: Path) -> list[Turn]:
    data = json.loads(path.read_text(encoding="utf-8"))
    block = data.get("raw_clusters") or {}
    turns: list[Turn] = []
    for row in block.get("segments") or []:
        turns.append(
            Turn(
                start=float(row["start"]),
                end=float(row["end"]),
                speaker=str(row["speaker"]),
                text="",
            )
        )
    return turns


def switch_count(turns: list[Turn]) -> int:
    if len(turns) < 2:
        return 0
    prev = turns[0].speaker
    count = 0
    for turn in turns[1:]:
        if turn.speaker != prev:
            count += 1
            prev = turn.speaker
    return count


def color_map(turns: list[Turn]) -> dict[str, str]:
    names: list[str] = []
    for turn in turns:
        if turn.speaker not in names:
            names.append(turn.speaker)
    return {name: COLORS[i % len(COLORS)] for i, name in enumerate(names)}


def clip_turns(turns: list[Turn], end_sec: float | None) -> list[Turn]:
    if end_sec is None:
        return turns
    clipped: list[Turn] = []
    for turn in turns:
        if turn.start >= end_sec:
            break
        clipped.append(
            Turn(
                start=turn.start,
                end=min(turn.end, end_sec),
                speaker=turn.speaker,
                text="",
            )
        )
    return [t for t in clipped if t.end > t.start]


def track_stats(turns: list[Turn]) -> dict:
    duration = max((t.end for t in turns), default=0.0)
    return {
        "segments": len(turns),
        "switches": switch_count(turns),
        "duration_sec": round(duration, 1),
        "talk_time": talk_time(turns),
    }


def build_manifest(
    *,
    preset_dir: Path,
    sortformer_json: Path | None,
    pyannote_fallback: Path | None,
) -> dict:
    tracks: list[dict] = []
    manifest_path = preset_dir / "manifest.json"
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    for json_path in sorted(preset_dir.glob("*.json")):
        if json_path.name == "manifest.json":
            continue
        turns, meta = load_segments(json_path)
        if not turns:
            continue
        tracks.append(
            {
                "id": json_path.stem,
                "label": json_path.stem.replace("_", " "),
                "description": _describe(meta),
                "file": str(json_path.relative_to(ROOT)).replace("\\", "/"),
                "segments": [_turn_dict(t) for t in turns],
            }
        )
        raw = load_raw_clusters(json_path)
        if raw:
            tracks.append(
                {
                    "id": f"{json_path.stem}_raw_clusters",
                    "label": f"{json_path.stem} — кластеры PyAnnote",
                    "description": "До resegment_by_voice, только кластеризация PyAnnote",
                    "file": str(json_path.relative_to(ROOT)).replace("\\", "/"),
                    "segments": [_turn_dict(t) for t in raw],
                }
            )

    if sortformer_json and sortformer_json.is_file():
        turns, meta = load_segments(sortformer_json)
        if turns:
            tracks.append(
                {
                    "id": "sortformer_gguf",
                    "label": "Sortformer v2.1 GGUF",
                    "description": meta.get("model") or "Sortformer offline GGUF",
                    "file": str(sortformer_json.relative_to(ROOT)).replace("\\", "/"),
                    "segments": [_turn_dict(t) for t in turns],
                }
            )

    if pyannote_fallback and pyannote_fallback.is_file():
        turns, meta = load_segments(pyannote_fallback)
        if turns and not any(t["id"] == "pyannote_current" for t in tracks):
            tracks.insert(
                0,
                {
                    "id": "pyannote_current",
                    "label": "PyAnnote (последний прогон)",
                    "description": _describe(meta),
                    "file": str(pyannote_fallback.relative_to(ROOT)).replace("\\", "/"),
                    "segments": [_turn_dict(t) for t in turns],
                },
            )
            raw = load_raw_clusters(pyannote_fallback)
            if raw:
                tracks.insert(
                    1,
                    {
                        "id": "pyannote_current_raw",
                        "label": "PyAnnote — кластеры до resegment",
                        "description": "Сырые интервалы PyAnnote до resegment_by_voice",
                        "file": str(pyannote_fallback.relative_to(ROOT)).replace("\\", "/"),
                        "segments": [_turn_dict(t) for t in raw],
                    },
                )

    if not tracks:
        return {"tracks": []}

    return {"tracks": tracks}


def _turn_dict(turn: Turn) -> dict:
    return {"start": round(turn.start, 3), "end": round(turn.end, 3), "speaker": turn.speaker}


def _describe(meta: dict) -> str:
    params = meta.get("params") or {}
    parts: list[str] = []
    if meta.get("source"):
        parts.append(str(meta["source"]))
    if params.get("preset_id"):
        parts.append(f"preset={params['preset_id']}")
    cl = params.get("clustering") or {}
    if cl:
        parts.append(f"cluster thr={cl.get('threshold')}")
    for key in ("reseg_hold_sec", "reseg_sim_floor", "reseg_window_sec"):
        if key in params:
            parts.append(f"{key.replace('_sec', '')}={params[key]}")
    return " · ".join(parts) if parts else "PyAnnote resegmented"


def render_html(
    *,
    title: str,
    audio_url: str,
    manifest: dict,
    clip_sec: float | None = None,
) -> str:
    tracks_payload = []
    for track in manifest.get("tracks") or []:
        turns = [_turn_from_row(row) for row in track.get("segments") or []]
        turns = clip_turns(turns, clip_sec)
        stats = track_stats(turns)
        colors = color_map(turns)
        duration = stats["duration_sec"] or 1.0
        timeline = []
        for turn in turns:
            left = 100.0 * turn.start / duration
            width = max(0.15, 100.0 * (turn.end - turn.start) / duration)
            color = colors.get(turn.speaker, "#94a3b8")
            timeline.append(
                f'<div class="bar-seg" style="left:{left:.3f}%;width:{width:.3f}%;background:{color}" '
                f'title="{html.escape(turn.speaker)} {format_clock(turn.start)}"></div>'
            )
        stat_rows = "".join(
            f"<tr><td><span class='dot' style='background:{colors.get(r['speaker'], '#64748b')}'></span>"
            f"{html.escape(r['speaker'])}</td>"
            f"<td>{format_clock(r['seconds'])}</td>"
            f"<td>{int(round(r['share'] * 100))}%</td></tr>"
            for r in stats["talk_time"]
        )
        items = []
        for i, turn in enumerate(turns):
            color = colors.get(turn.speaker, "#64748b")
            items.append(
                f"<article class='seg' data-i='{i}' data-start='{turn.start:.3f}' data-end='{turn.end:.3f}' "
                f"data-speaker='{html.escape(turn.speaker, quote=True)}'>"
                f"<button type='button' class='when' style='border-color:{color}'>"
                f"{format_clock(turn.start)}–{format_clock(turn.end)}</button>"
                f"<span class='who' style='color:{color}'>{html.escape(turn.speaker)}</span>"
                f"<span class='dur'>{turn.duration:.1f} с</span></article>"
            )
        tracks_payload.append(
            {
                "id": track["id"],
                "label": track.get("label") or track["id"],
                "description": track.get("description") or "",
                "stats": stats,
                "timeline_html": "".join(timeline),
                "stat_rows_html": stat_rows,
                "segments_html": "".join(items),
                "duration": duration,
            }
        )

    tabs = "".join(
        f"<button type='button' class='tab{' active' if i == 0 else ''}' data-track='{html.escape(t['id'], quote=True)}'>"
        f"{html.escape(t['label'])}</button>"
        for i, t in enumerate(tracks_payload)
    )
    panels = []
    for i, track in enumerate(tracks_payload):
        panels.append(
            f"<section class='panel{' active' if i == 0 else ''}' data-track='{html.escape(track['id'], quote=True)}'>"
            f"<p class='hint'>{html.escape(track['description'])}</p>"
            f"<p class='meta'>Сегментов: {track['stats']['segments']} · "
            f"смен спикера: {track['stats']['switches']} · "
            f"длительность: {format_clock(track['duration'])}</p>"
            f"<div class='timeline'>{track['timeline_html']}</div>"
            f"<table><thead><tr><th>Кто</th><th>Говорил</th><th>Доля</th></tr></thead>"
            f"<tbody>{track['stat_rows_html']}</tbody></table>"
            f"<div class='segs'>{track['segments_html']}</div></section>"
        )

    payload = json.dumps({"tracks": tracks_payload}, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ font-family: Segoe UI, system-ui, sans-serif; color: #0f172a; background: #f8fafc; }}
    body {{ margin: 0; }}
    header, main {{ max-width: 1100px; margin: 0 auto; padding: 16px; }}
    h1 {{ font-size: 1.35rem; margin: 0 0 8px; }}
    .links {{ margin: 8px 0; font-size: .9rem; }}
    .links a {{ margin-right: 12px; }}
    audio {{ width: 100%; margin: 8px 0 12px; }}
    .tabs {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }}
    .tab {{ border: 1px solid #cbd5e1; background: #fff; border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: .88rem; }}
    .tab.active {{ background: #dbeafe; border-color: #2563eb; }}
    .panel {{ display: none; }}
    .panel.active {{ display: block; }}
    .hint, .meta {{ color: #475569; font-size: .9rem; margin: 4px 0; }}
    .timeline {{ position: relative; height: 28px; background: #e2e8f0; border-radius: 6px; margin: 12px 0; overflow: hidden; }}
    .bar-seg {{ position: absolute; top: 0; bottom: 0; min-width: 1px; opacity: .92; }}
    table {{ border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: .92rem; }}
    td {{ padding: 4px 8px; border-bottom: 1px solid #e2e8f0; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
    .segs {{ max-height: 55vh; overflow: auto; border: 1px solid #e2e8f0; border-radius: 10px; background: #fff; }}
    .seg {{ display: grid; grid-template-columns: 130px 1fr 60px; gap: 10px; padding: 6px 10px; border-bottom: 1px solid #f1f5f9; align-items: center; }}
    .seg.active {{ background: #e0f2fe; }}
    .when {{ border: 2px solid #cbd5e1; background: #fff; border-radius: 8px; padding: 2px 8px; cursor: pointer; font: inherit; font-size: .85rem; }}
    .who {{ font-weight: 600; }}
    .dur {{ color: #64748b; font-size: .85rem; text-align: right; }}
    .toolbar {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin: 8px 0; }}
    label.chk {{ font-size: .9rem; }}
  </style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <p class="hint">Только интервалы диаризации — без текста Gemma и без сглаживания UI. Клик по времени ставит плеер.</p>
  <div class="links">
    <a href="report">Стенogramma PyAnnote</a>
    <a href="report-sortformer">Стенogramma Sortformer</a>
  </div>
  <audio id="player" controls preload="metadata" playsinline src="{html.escape(audio_url, quote=True)}"></audio>
  <div class="toolbar">
    <label class="chk"><input type="checkbox" id="clip10"> Только первые 10 минут</label>
  </div>
  <div class="tabs">{tabs}</div>
</header>
<main>{''.join(panels)}</main>
<script id="data" type="application/json">{payload}</script>
<script>
const data = JSON.parse(document.getElementById("data").textContent);
const player = document.getElementById("player");
function seek(sec) {{
  const t = Number(sec);
  if (!Number.isFinite(t)) return;
  const go = () => {{
    try {{ player.currentTime = t; }} catch (_) {{}}
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
document.querySelectorAll(".tab").forEach((btn) => {{
  btn.addEventListener("click", () => {{
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.dataset.track === btn.dataset.track));
  }});
}});
document.querySelectorAll(".segs").forEach((box) => {{
  box.addEventListener("click", (ev) => {{
    const seg = ev.target.closest(".seg");
    if (!seg) return;
    seek(Number(seg.dataset.start));
  }});
}});
player.addEventListener("timeupdate", () => {{
  const t = player.currentTime;
  document.querySelectorAll(".seg").forEach((el) => {{
    const a = Number(el.dataset.start), b = Number(el.dataset.end);
    el.classList.toggle("active", t >= a && t < b);
  }});
}});
document.getElementById("clip10").addEventListener("change", applyClip);
function applyClip() {{
  const limit = document.getElementById("clip10").checked ? 600 : null;
  document.querySelectorAll(".panel").forEach((panel) => {{
    panel.querySelectorAll(".seg").forEach((el) => {{
      const start = Number(el.dataset.start);
      el.style.display = limit === null || start < limit ? "" : "none";
    }});
  }});
}}
applyClip();
</script>
</body>
</html>
"""


def build(
    *,
    out_html: Path,
    audio_url: str,
    manifest_path: Path | None = None,
    preset_dir: Path | None = None,
    sortformer_json: Path | None = None,
    pyannote_fallback: Path | None = None,
    title: str = "Сырая диаризация — сравнение треков",
    clip_sec: float | None = None,
) -> None:
    preset_dir = preset_dir or ROOT / "experiments" / "diar-presets" / "out"
    if manifest_path and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = build_manifest(
            preset_dir=preset_dir,
            sortformer_json=sortformer_json,
            pyannote_fallback=pyannote_fallback,
        )
    if not manifest.get("tracks"):
        raise SystemExit("нет треков диаризации для отображения")
    html_body = render_html(title=title, audio_url=audio_url, manifest=manifest, clip_sec=clip_sec)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html_body, encoding="utf-8")
    print("wrote", out_html, "tracks", len(manifest["tracks"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--preset-dir", type=Path, default=ROOT / "experiments" / "diar-presets" / "out")
    parser.add_argument(
        "--sortformer-json",
        type=Path,
        default=ROOT / "experiments/nemo-diar/out/pilot_22_07_26/sortformer_gguf_vulkan_input_16k_report.json",
    )
    parser.add_argument("--pyannote-fallback", type=Path, default=ROOT / "asr/out/pyannote_segments.json")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data/projects/1/artifacts/art_2026-08-19_211842_54f88c/reports/diar_raw.html",
    )
    parser.add_argument(
        "--audio-url",
        default="/artifacts/art_2026-08-19_211842_54f88c/audio",
    )
    parser.add_argument("--clip-sec", type=float, default=None)
    args = parser.parse_args()
    build(
        out_html=args.out,
        audio_url=args.audio_url,
        manifest_path=args.manifest,
        preset_dir=args.preset_dir,
        sortformer_json=args.sortformer_json if args.sortformer_json.is_file() else None,
        pyannote_fallback=args.pyannote_fallback if args.pyannote_fallback.is_file() else None,
        clip_sec=args.clip_sec,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
