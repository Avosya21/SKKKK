"""Кадры screen_video → vision. Не во время живой комнаты."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.app.bootstrap import ENV_PATH, _read_env
from app.app.config import ROOT, Settings
from app.app.models.artifact import Artifact
from app.app.logging_setup import log_event
from app.app.services.meeting_recordings import SOURCE_SCREEN_VIDEO, safe_sid
from app.app.services.prompts import load_prompt_block

VISION_ENV = "MEETING_SCREEN_VISION"
_dotenv_cache: dict[str, str] | None = None


def _dotenv_value(key: str) -> str | None:
    global _dotenv_cache
    try:
        if _dotenv_cache is None:
            _dotenv_cache = {}
            if ENV_PATH.is_file():
                for line in _read_env().splitlines():
                    if "=" in line and not line.strip().startswith("#"):
                        k, _, v = line.partition("=")
                        _dotenv_cache[k.strip()] = v.strip()
        return _dotenv_cache.get(key)
    except Exception:
        return None
FRAMES_MAX_ENV = "MEETING_FRAMES_MAX"
FRAMES_MAX_DEFAULT = 60
SCENE_THRESHOLD = 0.15
SPEECH_CLUSTER_GAP_SEC = 8.0
DEDUP_SEC = 2.0
SILENCE_FLOOR_SEC = 15.0
TIME_ROUND = 3
PRI_EDGE = 1
PRI_OWN = 2
PRI_SCENE = 3
PRI_OTHER = 4
PRI_FLOOR = 5
RELATION_OWN = "own_share"
RELATION_OTHER = "over_others_share"
OFF_VALUES = {"0", "false", "off"}
PROMPT_PATH = ROOT / "docs" / "prompts" / "screen_frame.md"
FRAMES_JSON = "frames.json"
LINKS_JSON = "speech_screen_links.json"
VIDEO_TRANSCRIPT_JSON = "video_transcript.json"
VIDEO_TRANSCRIPT_MD = "video_transcript.md"
FFMPEG_TIMEOUT = 120
logger = logging.getLogger("skkkk.meeting_video")


def screen_vision_enabled(raw: str | None = None) -> bool:
    if raw is None:
        value = os.environ.get(VISION_ENV)
        if value is None:
            value = _dotenv_value(VISION_ENV)
    else:
        value = raw
    value = (value or "0").strip().lower()
    return bool(value) and value not in OFF_VALUES


def frames_max(raw: str | None = None) -> int:
    if raw is None:
        text = os.environ.get(FRAMES_MAX_ENV)
        if text is None:
            text = _dotenv_value(FRAMES_MAX_ENV) or ""
    else:
        text = raw
    if not text:
        return FRAMES_MAX_DEFAULT
    try:
        return max(1, min(60, int(float(text))))
    except ValueError:
        return FRAMES_MAX_DEFAULT


def cluster_onsets(times: list[float], gap_sec: float = SPEECH_CLUSTER_GAP_SEC) -> list[float]:
    ordered = sorted({_ts(item) for item in times})
    if not ordered:
        return []
    out = [ordered[0]]
    last = ordered[0]
    gap = max(0.0, float(gap_sec))
    for item in ordered[1:]:
        if item - last >= gap:
            out.append(item)
            last = item
    return out


def silence_floor_times(
    duration_sec: float,
    spans: list[tuple[float, float]],
    step_sec: float = SILENCE_FLOOR_SEC,
) -> list[float]:
    duration = max(0.0, float(duration_sec))
    step = max(0.1, float(step_sec))
    if duration <= 0:
        return []
    merged: list[list[float]] = []
    for start, end in sorted((max(0.0, float(a)), min(duration, max(float(a), float(b)))) for a, b in spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start - cursor >= step:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= step:
        gaps.append((cursor, duration))
    ticks: list[float] = []
    for start, end in gaps:
        t = start + step
        while t < end - 1e-6:
            ticks.append(_ts(t))
            t += step
    return ticks


def select_share_frames(
    duration_sec: float,
    *,
    scene: list[float] | None = None,
    own_onsets: list[float] | None = None,
    own_spans: list[tuple[float, float]] | None = None,
    other_onsets: list[float] | None = None,
    max_n: int = FRAMES_MAX_DEFAULT,
    cluster_gap: float = SPEECH_CLUSTER_GAP_SEC,
    dedup_sec: float = DEDUP_SEC,
    floor_step: float = SILENCE_FLOOR_SEC,
) -> list[float]:
    """Ярусы: края → речь издателя → сцена → чужая речь → пол 15 с. Потолок max_n на шаринг."""
    duration = max(0.0, float(duration_sec))
    cap = max(1, int(max_n))
    if duration <= 0:
        return [0.0]
    marks: list[tuple[float, int]] = [(_ts(0.0), PRI_EDGE), (_ts(duration), PRI_EDGE)]
    for item in cluster_onsets(own_onsets or [], cluster_gap):
        marks.append((_clamp(item, duration), PRI_OWN))
    for item in scene or []:
        marks.append((_clamp(item, duration), PRI_SCENE))
    for item in cluster_onsets(other_onsets or [], cluster_gap):
        marks.append((_clamp(item, duration), PRI_OTHER))
    for item in silence_floor_times(duration, own_spans or [], floor_step):
        marks.append((_clamp(item, duration), PRI_FLOOR))
    kept = _dedup_marks(marks, dedup_sec)
    kept = _cap_marks(kept, cap)
    return [item[0] for item in kept]


def pick_frame_times(
    duration_sec: float,
    scene_times: list[float],
    *,
    max_n: int = FRAMES_MAX_DEFAULT,
) -> list[float]:
    return select_share_frames(duration_sec, scene=scene_times, max_n=max_n)


def same_person(replica: dict, track: dict) -> bool:
    rid = str(replica.get("identity") or "").strip()
    tid = str(track.get("identity") or "").strip()
    if rid and tid:
        return rid == tid
    return str(replica.get("speaker") or "").strip() == str(track.get("speaker") or "").strip()


def bind_replicas(frames: list[dict], replicas: list[dict], track: dict) -> list[dict]:
    """Реплика ↔ кадр по пересечению [t_k, t_{k+1}) на шкале встречи."""
    offset = float(track.get("offset_sec") or 0.0)
    duration = float(track.get("duration_sec") or 0.0)
    share_end = offset + duration
    ordered = sorted(frames, key=lambda row: float(row.get("t_rel") or 0.0))
    links: list[dict] = []
    for index, frame in enumerate(ordered):
        t0 = float(frame.get("t_rel") or 0.0)
        t1 = float(ordered[index + 1].get("t_rel") or share_end) if index + 1 < len(ordered) else share_end
        if t1 <= t0:
            t1 = t0 + 1e-3
        for replica in replicas:
            start = float(replica.get("start") or 0.0)
            end = float(replica.get("end") or start)
            if end <= offset or start >= share_end:
                continue
            if end <= t0 or start >= t1:
                continue
            links.append(
                {
                    "source_span_id": replica.get("source_span_id"),
                    "frame_id": frame.get("frame_id"),
                    "shown_by": track.get("identity"),
                    "spoken_by": replica.get("speaker"),
                    "relation": RELATION_OWN if same_person(replica, track) else RELATION_OTHER,
                }
            )
    return links


def _ts(value: float) -> float:
    return round(float(value), TIME_ROUND)


def _clamp(value: float, duration: float) -> float:
    return _ts(max(0.0, min(duration, float(value))))


def _dedup_marks(marks: list[tuple[float, int]], dedup_sec: float) -> list[tuple[float, int]]:
    ordered = sorted(marks, key=lambda item: (item[0], item[1]))
    kept: list[tuple[float, int]] = []
    gap = max(0.0, float(dedup_sec))
    for ts, pri in ordered:
        if not kept:
            kept.append((ts, pri))
            continue
        prev_t, prev_pri = kept[-1]
        if ts - prev_t < gap:
            if pri < prev_pri:
                kept[-1] = (ts, pri)
            continue
        kept.append((ts, pri))
    return kept


def _even_sample(rows: list[tuple[float, int]], keep_n: int) -> list[tuple[float, int]]:
    if keep_n <= 0:
        return []
    if keep_n >= len(rows):
        return list(rows)
    if keep_n == 1:
        return [rows[0]]
    out = []
    last = -1
    for i in range(keep_n):
        idx = round(i * (len(rows) - 1) / (keep_n - 1))
        if idx == last:
            idx = min(len(rows) - 1, last + 1)
        last = idx
        out.append(rows[idx])
    return out


def _cap_marks(kept: list[tuple[float, int]], cap: int) -> list[tuple[float, int]]:
    if len(kept) <= cap:
        return kept
    current = list(kept)
    for pri in (PRI_FLOOR, PRI_OTHER, PRI_SCENE, PRI_OWN, PRI_EDGE):
        if len(current) <= cap:
            break
        tier = [row for row in current if row[1] == pri]
        rest = [row for row in current if row[1] != pri]
        keep_n = cap - len(rest)
        if keep_n <= 0:
            current = rest
            continue
        current = sorted(rest + _even_sample(tier, keep_n), key=lambda item: item[0])
    return current[:cap]


def _speech_marks(track: dict, replicas: list[dict], duration: float) -> tuple[list[float], list[tuple[float, float]], list[float]]:
    offset = float(track.get("offset_sec") or 0.0)
    share_end = offset + duration
    own_onsets: list[float] = []
    own_spans: list[tuple[float, float]] = []
    other_onsets: list[float] = []
    for replica in replicas:
        start = float(replica.get("start") or 0.0)
        end = float(replica.get("end") or start)
        if end <= offset or start >= share_end:
            continue
        file_start = max(0.0, start - offset)
        file_end = min(duration, max(file_start, end - offset))
        if same_person(replica, track):
            own_onsets.append(file_start)
            own_spans.append((file_start, file_end))
        else:
            other_onsets.append(file_start)
    return own_onsets, own_spans, other_onsets


def extract_meeting_screen_frames(
    settings: Settings,
    artifact: Artifact,
    manifest: dict | None = None,
    *,
    enabled: bool | None = None,
    describe: bool = True,
    max_n: int | None = None,
    replicas: list[dict] | None = None,
) -> list[dict]:
    started = time.monotonic()
    log_event(logger, "meeting.video_transcript.start", artifact=artifact.artifact_id)
    if enabled is False:
        log_event(logger, "meeting.video_transcript.skip", artifact=artifact.artifact_id, reason="disabled")
        return []
    if describe is True:
        describe = screen_vision_enabled() if enabled is None else True
    data = manifest if isinstance(manifest, dict) else _load_manifest(settings, artifact)
    tracks = [
        row
        for row in (data.get("tracks") or [])
        if (row.get("source") == SOURCE_SCREEN_VIDEO) and Path(str(row.get("src") or "")).is_file()
    ]
    if not tracks:
        log_event(logger, "meeting.video_transcript.skip", artifact=artifact.artifact_id, reason="no_video_tracks")
        return []
    from app.app.services.artifact_lab import latest_run_id, resolve_run

    run_id = latest_run_id(settings, artifact)
    if not run_id:
        log_event(logger, "meeting.video_transcript.failed", level=logging.WARNING, artifact=artifact.artifact_id, error="no_lab_run")
        return []
    run = resolve_run(settings, artifact, run_id)
    cap = frames_max() if max_n is None else max(1, int(max_n))
    speech = list(replicas or [])
    rows: list[dict] = []
    links: list[dict] = []
    for track in tracks:
        track_started = time.monotonic()
        try:
            grabbed = _extract_track(settings, run, track, cap=cap, describe=describe, replicas=speech)
        except Exception as exc:
            log_event(logger, "meeting.video_track.failed", level=logging.WARNING,
                      artifact=artifact.artifact_id, track_sid=track.get("track_sid"),
                      identity=track.get("identity"), error=str(exc))
            continue
        rows.extend(grabbed)
        track_links = bind_replicas(grabbed, speech, track)
        links.extend(track_links)
        log_event(logger, "meeting.video_track.done", artifact=artifact.artifact_id,
                  track_sid=track.get("track_sid"), identity=track.get("identity"),
                  frames=len(grabbed), links=len(track_links),
                  seconds=round(time.monotonic() - track_started, 2))
    dest = run.resolve(FRAMES_JSON)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    run.resolve(LINKS_JSON).write_text(
        json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_video_transcript(run, rows)
    log_event(logger, "meeting.video_transcript.done", artifact=artifact.artifact_id,
              tracks=len(tracks), frames=len(rows), links=len(links),
              seconds=round(time.monotonic() - started, 2),
              transcript=VIDEO_TRANSCRIPT_JSON)
    return rows


def _extract_track(
    settings: Settings,
    run,
    track: dict,
    *,
    cap: int,
    describe: bool,
    replicas: list[dict] | None = None,
) -> list[dict]:
    src = Path(track["src"])
    sid = str(track.get("track_sid") or "screen")
    offset = float(track.get("offset_sec") or 0.0)
    duration = float(track.get("duration_sec") or 0.0) or _probe_duration(src)
    folder = run.resolve(f"frames/{safe_sid(sid)}")
    folder.mkdir(parents=True, exist_ok=True)
    own_onsets, own_spans, other_onsets = _speech_marks(track, replicas or [], duration)
    times = select_share_frames(
        duration,
        scene=scene_times(src),
        own_onsets=own_onsets,
        own_spans=own_spans,
        other_onsets=other_onsets,
        max_n=cap,
    )
    rows = []
    for ts in times:
        ts_ms = int(round(ts * 1000))
        frame_id = f"{safe_sid(sid)}_{ts_ms}"
        jpg = folder / f"{ts_ms}.jpg"
        if not _grab_frame(src, ts, jpg):
            continue
        item = {
            "frame_id": frame_id,
            "track_sid": sid,
            "identity": track.get("identity"),
            "speaker": track.get("speaker"),
            "t_rel": round(offset + ts, TIME_ROUND),
            "ts_in_file": ts,
            "sha256": _sha256(jpg),
            "path": str(jpg.relative_to(run.root)) if hasattr(run, "root") else str(jpg),
        }
        text, text_source = _frame_text(settings, jpg, describe=describe)
        if text:
            item["text"] = text
            item["text_source"] = text_source
            md = folder / f"{ts_ms}.md"
            md.write_text(text, encoding="utf-8")
        rows.append(item)
    return rows


def _frame_text(settings: Settings, path: Path, *, describe: bool) -> tuple[str, str]:
    ocr = _ocr_frame(path)
    if not describe:
        return ocr, "ocr" if ocr else ""
    try:
        vision = _describe_frame(settings, path)
    except Exception as exc:  # noqa: BLE001 — кадр остаётся с OCR, но сбой модели должен быть виден в журнале
        log_event(logger, "meeting.video_frame.vision_failed", level=logging.WARNING, frame=path.name, error=str(exc)[:300])
        return ocr, "ocr" if ocr else ""
    return vision or ocr, "vision" if vision else ("ocr" if ocr else "")


def _ocr_frame(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
        from app.app.services.tesseract_tools import configure_pytesseract

        text = pytesseract.image_to_string(
            Image.open(path), lang="rus+eng", config=configure_pytesseract()
        )
        return re.sub(r"\s+", " ", text or "").strip()
    except (ImportError, OSError, RuntimeError):
        return ""


def _write_video_transcript(run, rows: list[dict]) -> None:
    segments = []
    for index, row in enumerate(rows):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        start = float(row.get("t_rel") or 0.0)
        end = float(next_row.get("t_rel") or start) if next_row else start
        segments.append({
            "segment_id": f"video:{row.get('frame_id')}",
            "frame_id": row.get("frame_id"),
            "track_sid": row.get("track_sid"),
            "identity": row.get("identity"),
            "speaker": row.get("speaker"),
            "start": round(start, TIME_ROUND),
            "end": round(max(start, end), TIME_ROUND),
            "text": text,
            "text_source": row.get("text_source") or "ocr",
        })
    run.resolve(VIDEO_TRANSCRIPT_JSON).write_text(
        json.dumps({"segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [f"[{item['start']:.3f}–{item['end']:.3f}] {item['text']}" for item in segments]
    run.resolve(VIDEO_TRANSCRIPT_MD).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def redescribe_existing_frames(settings: Settings, run, *, max_n: int | None = None) -> int:
    """Перечитать уже снятые кадры run vision-моделью (без повторного ffmpeg-захвата).

    Читает frames.json, для каждого кадра с текстом вызывает ``_frame_text`` с
    ``describe=True``, обновляет text/text_source, переписывает frames.json и
    video_transcript.json. Возвращает число обновлённых кадров.
    """
    frame_path = run.resolve(FRAMES_JSON)
    if not frame_path.is_file():
        return 0
    try:
        rows = json.loads(frame_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(rows, list):
        return 0
    if max_n is not None:
        rows = rows[: max(1, int(max_n))]
    updated = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if str(row.get("text_source") or "") == "vision":
            continue
        rel = str(row.get("path") or "")
        if not rel:
            continue
        jpg = run.resolve(rel)
        if not jpg.is_file():
            continue
        try:
            described, source = _frame_text(settings, jpg, describe=True)
        except Exception as exc:
            log_event(logger, "meeting.video_frame.redescribe.failed",
                      level=logging.WARNING, frame_id=row.get("frame_id"), error=str(exc))
            continue
        if not described or described == text:
            continue
        row["text"] = described
        row["text_source"] = source or "vision"
        rows[index] = row
        updated += 1
    if not updated:
        return 0
    frame_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_video_transcript(run, rows)
    log_event(logger, "meeting.video_frames.redescribed", frames=updated)
    return updated


def scene_times(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    exe = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        exe,
        "-hide_banner",
        "-i",
        str(path),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-fps_mode",
        "vfr",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = re.findall(r"pts_time:(\d+(?:\.\d+)?)", (proc.stderr or "") + (proc.stdout or ""))
    return [float(item) for item in found]


def _grab_frame(src: Path, ts: float, dest: Path) -> bool:
    exe = shutil.which("ffmpeg") or "ffmpeg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, ts):.3f}",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


def _describe_frame(settings: Settings, path: Path) -> str:
    from app.app.services.image_extract import image_to_data_url
    from app.app.services.llm import chat_completion_vision

    system = _load_prompt()
    text = chat_completion_vision(
        settings,
        system=system,
        user_text="Опиши этот кадр шаринга экрана.",
        image_data_url=image_to_data_url(path),
        max_tokens=4096,
    ).strip()
    return text


def _load_prompt() -> str:
    """Первый ограждённый блок промпта (не заголовок файла) — см. services/prompts.py."""
    return load_prompt_block(PROMPT_PATH)


def _load_manifest(settings: Settings, artifact: Artifact) -> dict:
    from app.app.services.meeting_analyze import meeting_manifest_path

    path = meeting_manifest_path(settings, artifact.project_id, artifact.artifact_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _probe_duration(path: Path) -> float:
    exe = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        exe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return max(0.0, float((proc.stdout or "").strip() or 0))
    except ValueError:
        return 0.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
