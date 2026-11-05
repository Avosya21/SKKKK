"""Три автономных ASR-прогона: разные арtefactы, общий asr/work — проверка изоляции."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

RUNS = (
    {
        "label": "primary-analytics",
        "project_id": 12,
        "artifact_id": "art_2026-08-24_154408_f02d28",
        "marker": "антон",
        "forbidden": ("jira", "semrush", "google drive", "восьмидесят", "добрый день"),
    },
    {
        "label": "support-call-11",
        "project_id": 11,
        "artifact_id": "art_2026-08-23_022435_e8ef22",
        "marker": "добрый день",
        "forbidden": ("первичной аналитики", "primary analytics", "антон", "восьмидесят", "крутить звук"),
    },
    {
        "label": "task-80-call",
        "project_id": 10,
        "artifact_id": "art_2026-08-21_094718_29258f",
        "marker": "крутить звук",
        "forbidden": ("первичной аналитики", "primary analytics", "антон", "добрый день"),
    },
)

CLIP_SEC = 45
BASE = ROOT / "data" / "tmp" / "isolation_triple"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _clip_wav(src: Path, dst_16k: Path, dst_8k: Path, seconds: int) -> None:
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    dst_16k.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-t", str(seconds), "-ar", "16000", "-ac", "1", str(dst_16k)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [ffmpeg, "-y", "-i", str(dst_16k), "-ar", "8000", "-ac", "1", str(dst_8k)],
        check=True,
        capture_output=True,
    )


def _prepare_workspace(run: dict, index: int) -> Path:
    src = (
        ROOT
        / "data"
        / "projects"
        / str(run["project_id"])
        / "artifacts"
        / run["artifact_id"]
        / "audio"
        / "input_16k.wav"
    )
    if not src.is_file():
        raise FileNotFoundError(f"missing source wav: {src}")

    ws = BASE / f"run_{index}_{run['label']}"
    audio = ws / "audio"
    asr_out = ws / "asr"
    if ws.exists():
        shutil.rmtree(ws)
    audio.mkdir(parents=True)
    asr_out.mkdir(parents=True)

    clip_16k = audio / "input_16k.wav"
    clip_8k = audio / "input_8k.wav"
    _clip_wav(src, clip_16k, clip_8k, CLIP_SEC)
    meta = {
        "artifact_id": run["artifact_id"],
        "project_id": run["project_id"],
        "source": str(src),
        "clip_sec": CLIP_SEC,
        "sha256_16k": _sha256(clip_16k),
        "sha256_8k": _sha256(clip_8k),
        "bytes_8k": clip_8k.stat().st_size,
    }
    (ws / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return ws


def _run_asr_trio(ws: Path, run: dict) -> dict:
    os.environ["PRIMARY_ANALITICS_ASR_WORK"] = str(ws / "audio")
    os.environ["PRIMARY_ANALITICS_ASR_OUT"] = str(ws / "asr")
    os.environ["PRIMARY_ANALITICS_ARTIFACT_ID"] = run["artifact_id"]
    os.environ["PRIMARY_ANALITICS_PROJECT_ID"] = str(run["project_id"])

    # paths.py читает env при импорте — перезагрузить.
    for mod in ("paths", "docker_stage", "run_pipeline"):
        sys.modules.pop(mod, None)

    import paths  # noqa: WPS433
    from docker_stage import assert_docker_work_matches, stage_tone_docker_input  # noqa: WPS433
    from run_pipeline import load_env, run_gigaam, run_tone, run_whisper  # noqa: WPS433

    env = load_env()
    os.environ.setdefault("GIGAAM_API_KEY", env.get("GIGAAM_API_KEY", ""))
    key = os.environ.get("GIGAAM_API_KEY") or ""
    if not key:
        raise RuntimeError("GIGAAM_API_KEY пуст")

    audio_8k = paths.WORK / "input_8k.wav"
    staged = stage_tone_docker_input(audio_8k)
    assert_docker_work_matches(audio_8k, "input_8k.wav")

    t0 = time.monotonic()
    whisper = run_whisper()
    tone = run_tone()
    giga = run_gigaam(key)
    elapsed = round(time.monotonic() - t0, 1)

    tone_txt = (paths.OUT / "docker_tone.txt").read_text(encoding="utf-8").strip().lower()
    whisper_txt = (paths.OUT / "docker_whisper.txt").read_text(encoding="utf-8").strip().lower()
    giga_txt = (paths.OUT / "docker_gigaam.txt").read_text(encoding="utf-8").strip().lower()

    return {
        "label": run["label"],
        "artifact_id": run["artifact_id"],
        "staged_bytes": staged.stat().st_size,
        "staged_sha256": _sha256(staged),
        "source_sha256_8k": _sha256(audio_8k),
        "elapsed_sec": elapsed,
        "whisper": whisper,
        "tone": tone,
        "gigaam": giga,
        "tone_preview": tone_txt[:240],
        "whisper_preview": whisper_txt[:240],
        "giga_preview": giga_txt[:240],
        "tone_chars": len(tone_txt),
    }


def _validate_run(result: dict, run: dict, prior_tone_previews: list[str]) -> list[str]:
    errors: list[str] = []
    tone = result["tone_preview"]
    if result["staged_sha256"] != result["source_sha256_8k"]:
        errors.append("staging hash mismatch")
    if not tone:
        errors.append("empty tone transcript")
    marker = run["marker"].lower()
    if marker and marker not in tone:
        errors.append(f"expected marker {marker!r} missing in tone output")
    for bad in run["forbidden"]:
        if bad.lower() in tone:
            errors.append(f"forbidden marker {bad!r} found in tone (cross-contamination)")
    for prev in prior_tone_previews:
        if prev and tone == prev:
            errors.append("tone output identical to previous run")
    if not result["whisper"].get("ok") or not result["tone"].get("ok") or not result["gigaam"].get("ok"):
        errors.append("one or more ASR engines failed")
    return errors


def main() -> int:
    from app.app.llm_settings import apply_llm_env_from_dotenv

    apply_llm_env_from_dotenv()
    BASE.mkdir(parents=True, exist_ok=True)

    print(f"=== triple isolation: {len(RUNS)} runs, clip={CLIP_SEC}s ===", flush=True)
    results: list[dict] = []
    prior_previews: list[str] = []
    all_errors: list[str] = []

    for i, run in enumerate(RUNS, start=1):
        print(f"\n--- run {i}/{len(RUNS)}: {run['label']} ({run['artifact_id']}) ---", flush=True)
        ws = _prepare_workspace(run, i)
        try:
            result = _run_asr_trio(ws, run)
        except Exception as exc:  # noqa: BLE001
            all_errors.append(f"run {i} crashed: {exc}")
            print(f"FAIL run {i}: {exc}", flush=True)
            continue

        errs = _validate_run(result, run, prior_previews)
        result["errors"] = errs
        results.append(result)
        prior_previews.append(result["tone_preview"])

        status = "OK" if not errs else "FAIL"
        print(
            f"{status} tone_chars={result['tone_chars']} staged={result['staged_bytes']}B "
            f"elapsed={result['elapsed_sec']}s",
            flush=True,
        )
        print(f"  preview: {result['tone_preview'][:120]}...", flush=True)
        for err in errs:
            print(f"  ERROR: {err}", flush=True)
            all_errors.append(f"run {i}: {err}")

    report = {"runs": results, "errors": all_errors, "ok": not all_errors}
    out = BASE / "report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport: {out}", flush=True)
    if all_errors:
        print("TRIPLE ISOLATION: FAIL", flush=True)
        for err in all_errors:
            print(f"  - {err}", flush=True)
        return 1
    print("TRIPLE ISOLATION: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
