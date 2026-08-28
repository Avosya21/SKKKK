"""Сквозной прогон: видео → три ASR → Gemma → спикеры PyAnnote → HTML.

  python asr/run_pipeline.py
  python asr/run_pipeline.py --source "C:\\1C_NEW_SKKKK\\Test_video\\файл.webm"
  python asr/run_pipeline.py --serve
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app.services.docker_runtime import ensure_asr_containers
from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool
from app.app.services.media_cleanup import maybe_discard_video_source

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_checklist import (  # noqa: E402
    PipelineGateError,
    STEPS,
    gate,
    mark_fail,
    mark_ok,
    mark_pending,
    print_summary,
    reset,
    summary,
)

ASR = ROOT / "asr"
ENV = ROOT / ".env"
TEST = ROOT / "Test_video"

sys.path.insert(0, str(ASR))
from docker_stage import stage_tone_docker_input  # noqa: E402
from paths import OUT, WORK  # noqa: E402

WHISPER = "http://127.0.0.1:18101"
GIGAAM = "http://127.0.0.1:18102"
TONE = "http://127.0.0.1:18103"
LMSTUDIO = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
MODEL = os.environ.get("LMSTUDIO_MODEL", "google/gemma-4-31b-qat")


def _uses_local_gemma() -> bool:
    return os.environ.get("LLM_PROVIDER", "lmstudio").strip().lower() != "cursor"


def _llm_name() -> str:
    return "Cursor" if not _uses_local_gemma() else "Gemma"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if not ENV.is_file():
        return data
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    return data


def pick_source(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"нет файла {explicit}")
        return explicit
    videos = [p for p in TEST.iterdir() if p.suffix.lower() in {".webm", ".mp4", ".mkv", ".mov", ".wav", ".m4a"}]
    if not videos:
        raise SystemExit(f"в {TEST} нет видео")
    current = WORK / "input.webm"
    current_size = current.stat().st_size if current.is_file() else -1
    fresh = [p for p in videos if p.stat().st_size != current_size]
    chosen = max(fresh or videos, key=lambda p: p.stat().st_mtime)
    return chosen


def ffprobe_duration(path: Path) -> float:
    ffprobe = resolve_ffmpeg_tool("ffprobe")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def extract_wav(src: Path, dst: Path, sample_rate: int) -> None:
    if src.resolve() == dst.resolve():
        log(f"audio_prep skip: source already {dst.name}")
        return
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst),
        ],
        check=True,
    )


def wait_vram(max_mib: int, timeout: int = 90) -> int:
    deadline = time.time() + timeout
    used = 99999
    while time.time() < deadline:
        raw = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
        ).strip()
        used = int(raw.splitlines()[0].strip())
        log(f"vram {used} MiB")
        if used <= max_mib:
            return used
        time.sleep(3)
    return used


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_file(url: str, path: Path, headers: dict[str, str], extra_fields: dict[str, str] | None = None, timeout: int = 3600) -> dict:
    boundary = "----skkkkPipeline7MA4YWxk"
    chunks: list[bytes] = []
    for name, value in (extra_fields or {}).items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    data = path.read_bytes()
    chunks += [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: audio/wav\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        url,
        data=b"".join(chunks),
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_http(url: str, timeout: int = 180, *, label: str = "") -> None:
    deadline = time.time() + timeout
    last = ""
    name = label or url
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=5).read()
            log(f"ready {name}")
            return
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            time.sleep(3)
    raise SystemExit(f"не дождались {url}: {last}")


def _restart_asr_containers() -> None:
    """Перезапуск ASR. По умолчанию только GigaAM; тройка — ALLOW_LEGACY_ASR=1."""
    if os.environ.get("ALLOW_LEGACY_ASR") == "1":
        log("перезапуск ASR-контейнеров (whisper, gigaam, tone) ALLOW_LEGACY_ASR=1")
        ensure_asr_containers(restart=True)
        wait_http(f"{WHISPER}/health", timeout=300, label="whisper")
        wait_http(f"{GIGAAM}/health", timeout=300, label="gigaam")
        wait_http(f"{TONE}/health", timeout=120, label="tone")
        return
    log("перезапуск ASR-контейнера gigaam (Whisper/T-one не поднимаем)")
    ensure_asr_containers(restart=True)
    wait_http(f"{GIGAAM}/health", timeout=300, label="gigaam")


def run_whisper() -> dict:
    t0 = time.time()
    wav = WORK / "input_16k.wav"
    body = post_file(
        f"{WHISPER}/v1/audio/transcriptions",
        wav,
        {},
        extra_fields={"language": "ru", "response_format": "verbose_json"},
        timeout=3600,
    )
    text = (body.get("text") or "").strip()
    elapsed = round(time.time() - t0, 1)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "docker_whisper.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "docker_whisper.txt").write_text(text + ("\n" if text else ""), encoding="utf-8")
    log(f"whisper chars={len(text)} in {elapsed}s")
    return {"ok": True, "engine": "whisper", "chars": len(text), "elapsed_sec": elapsed}


def run_tone() -> dict:
    t0 = time.time()
    staged = stage_tone_docker_input(WORK / "input_8k.wav")
    log(f"tone docker input staged {staged} ({staged.stat().st_size} bytes)")
    payload = json.dumps({"audio": "/data/input_8k.wav", "out_dir": "/out"}).encode("utf-8")
    req = urllib.request.Request(
        f"{TONE}/transcribe",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    result["elapsed_sec"] = round(time.time() - t0, 1)
    text = result.get("text") or ""
    (OUT / "docker_tone.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "docker_tone.txt").write_text(text + ("\n" if text else ""), encoding="utf-8")
    log(f"tone chars={len(text)} in {result['elapsed_sec']}s")
    return {"ok": True, "engine": "tone", "chars": len(text), "elapsed_sec": result["elapsed_sec"]}


def run_gigaam(api_key: str) -> dict:
    t0 = time.time()
    headers = {"X-API-Key": api_key}
    qs = "asr_backend=pytorch&asr_model=v3_e2e_rnnt&enable_diarization=false"
    uploaded = post_file(f"{GIGAAM}/api/v1/transcribe?{qs}", WORK / "input_16k.wav", headers, timeout=120)
    task_id = uploaded["task_id"]
    log(f"gigaam task {task_id}")
    while True:
        st = get_json(f"{GIGAAM}/api/v1/tasks/{task_id}", headers=headers, timeout=30)
        status = st.get("status")
        if status == "completed":
            data = get_json(f"{GIGAAM}/api/v1/tasks/{task_id}/result", headers=headers, timeout=60)
            elapsed = round(time.time() - t0, 1)
            text = data.get("transcription") or ""
            timed = data.get("transcription_with_timecodes") or data.get("transcription_timecoded") or ""
            payload = {**data, "elapsed_sec": elapsed, "task_id": task_id}
            (OUT / "docker_gigaam.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            (OUT / "docker_gigaam.txt").write_text(text + ("\n" if text else ""), encoding="utf-8")
            if timed:
                (OUT / "docker_gigaam_diar_timecodes.txt").write_text(timed, encoding="utf-8")
            log(f"gigaam chars={len(text)} in {elapsed}s")
            return {"ok": True, "engine": "gigaam", "chars": len(text), "elapsed_sec": elapsed, "task_id": task_id}
        if status == "failed":
            raise RuntimeError(st.get("error") or st)
        time.sleep(2)


def _load_checklist_steps() -> dict:
    from pipeline_checklist import _load

    return _load().get("steps", {})


def _step_ok(name: str) -> bool:
    return _load_checklist_steps().get(name, {}).get("status") == "ok"


def _sortformer_timeout_sec(duration_sec: float) -> int:
    # ~3× длины аудио, минимум 15 мин, максимум 2 ч на длинные созвоны.
    return int(min(max(900, duration_sec * 3), 7200))


def _run_sortformer(duration_sec: float) -> None:
    gate("sortformer")
    timeout = _sortformer_timeout_sec(duration_sec)
    log(f"sortformer timeout {timeout}s ({timeout / 60:.0f} min)")
    try:
        dump = subprocess.run(
            [sys.executable, str(ASR / "dump_sortformer.py")],
            cwd=str(ROOT),
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        mark_ok("sortformer", f"WARN: timeout {timeout}s, view uses GigaAM chunks")
        log(f"WARN sortformer timeout after {timeout}s, продолжаем без Sortformer")
        return
    if dump.returncode != 0:
        mark_ok("sortformer", "WARN: failed, view uses GigaAM chunks")
        log("WARN dump_sortformer failed, view без Sortformer")
    else:
        mark_ok("sortformer", "сегменты записаны")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--continue", dest="resume", action="store_true", help="продолжить прерванный прогон по чеклисту")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=18105)
    args = parser.parse_args()

    env = load_env()
    from app.app.llm_settings import apply_llm_env_from_dotenv

    apply_llm_env_from_dotenv()
    os.environ.setdefault("GIGAAM_API_KEY", env.get("GIGAAM_API_KEY", ""))
    key = os.environ.get("GIGAAM_API_KEY") or env.get("GIGAAM_API_KEY", "")
    if not key:
        raise SystemExit("GIGAAM_API_KEY пуст")

    source = pick_source(args.source)
    log(f"source {source.name} ({source.stat().st_size} bytes)")
    duration = 0.0
    asr_summary: dict[str, object] = {}
    if args.resume:
        steps = _load_checklist_steps()
        for name in STEPS:
            if steps.get(name, {}).get("status") == "running":
                mark_pending(name)
                log(f"continue: сброс running → pending для {name}")
        if summary()["done"]:
            log("continue: все этапы уже ok")
            print_summary()
            return 0
    else:
        reset(str(source))

    meta_path = WORK / "input_meta.json"
    if meta_path.is_file():
        duration = float(json.loads(meta_path.read_text(encoding="utf-8")).get("duration_sec") or 0)
    summary_path = OUT / "pipeline_asr_summary.json"
    if summary_path.is_file():
        asr_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    # ── 1. Подготовка аудио ──────────────────────────────────────────────────
    if _step_ok("audio_prep"):
        log("=== 1. аудио — skip (ok) ===")
    else:
        log("=== 1. аудио 16 кГц / 8 кГц ===")
        try:
            gate("audio_prep")
            extract_wav(source, WORK / "input_16k.wav", 16000)
            extract_wav(source, WORK / "input_8k.wav", 8000)
            try:
                shutil.copy2(source, WORK / "input.webm")
            except OSError:
                pass
            duration = ffprobe_duration(WORK / "input_16k.wav")
            meta_path.write_text(
                json.dumps(
                    {"source": str(source), "duration_sec": round(duration, 2),
                     "wav_16k": str(WORK / "input_16k.wav"), "wav_8k": str(WORK / "input_8k.wav")},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            mark_ok("audio_prep", f"{duration / 60:.1f} мин")
            log(f"duration {duration / 60:.1f} min")
            artifact_id = os.environ.get("SKKKK_ARTIFACT_ID", "").strip()
            if artifact_id and maybe_discard_video_source(
                source, WORK / "input_16k.wav", artifact_id=artifact_id
            ):
                log(f"удалён видео-оригинал {source.name} (звук в {WORK / 'input_16k.wav'})")
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("audio_prep", str(exc))
            print_summary()
            raise SystemExit(f"FAIL audio_prep: {exc}") from exc

    # ── 2а. Освободить GPU под ASR ───────────────────────────────────────────
    if not _uses_local_gemma():
        if _step_ok("gemma_unload"):
            log("=== 2. gemma_unload — skip (ok) ===")
        else:
            log("=== 2. gemma_unload — Cursor LLM: выгрузка LM Studio, GPU для ASR ===")
            try:
                gate("gemma_unload")
                subprocess.run(["lms", "unload", "--all"], check=False)
                used = wait_vram(2500)
                mark_ok("gemma_unload", f"Cursor LLM, VRAM {used} MiB")
            except PipelineGateError:
                raise
            except Exception as exc:
                mark_fail("gemma_unload", str(exc))
                print_summary()
                raise SystemExit(f"FAIL gemma_unload: {exc}") from exc
    elif _step_ok("gemma_unload"):
        log("=== 2. gemma_unload — skip (ok) ===")
    else:
        log("=== 2. выгрузить Gemma, поднять ASR ===")
        try:
            gate("gemma_unload")
            subprocess.run(["lms", "unload", "--all"], check=False)
            used = wait_vram(2500)
            mark_ok("gemma_unload", f"VRAM {used} MiB")
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("gemma_unload", str(exc))
            print_summary()
            raise SystemExit(f"FAIL gemma_unload: {exc}") from exc

    # ── 2б. Поднять ASR-контейнеры ───────────────────────────────────────────
    if _step_ok("asr_containers") and _step_ok("asr_run"):
        log("asr_containers — skip (ok)")
    else:
        try:
            gate("asr_containers")
            _restart_asr_containers()
            mark_ok("asr_containers", "gigaam healthy")
            log("asr containers healthy")
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("asr_containers", str(exc))
            print_summary()
            raise SystemExit(f"FAIL asr_containers: {exc}") from exc

    # ── 3. ASR: канон GigaAM; Whisper/T-one только ALLOW_LEGACY_ASR=1 ──
    if _step_ok("asr_run"):
        log("=== 3. ASR — skip (ok) ===")
    else:
        try:
            gate("asr_run")
            OUT.mkdir(parents=True, exist_ok=True)
            if os.environ.get("ALLOW_LEGACY_ASR") == "1":
                log("=== 3. три ASR параллельно (ALLOW_LEGACY_ASR=1) ===")
                with ThreadPoolExecutor(max_workers=3) as pool:
                    futs = {
                        pool.submit(run_whisper): "whisper",
                        pool.submit(run_tone): "tone",
                        pool.submit(run_gigaam, key): "gigaam",
                    }
                    for fut in as_completed(futs):
                        eng = futs[fut]
                        try:
                            asr_summary[eng] = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            asr_summary[eng] = {"ok": False, "engine": eng, "error": str(exc)}
                            log(f"FAIL {eng}: {exc}")
                alive = [n for n, row in asr_summary.items() if isinstance(row, dict) and row.get("ok")]
                if not alive:
                    raise RuntimeError("все три ASR упали")
                if len(alive) < 3:
                    log(f"продолжаем с живыми: {alive}")
                mark_ok("asr_run", f"живых: {', '.join(alive)}")
            else:
                log("=== 3. только GigaAM (Whisper/T-one отключены) ===")
                try:
                    asr_summary["gigaam"] = run_gigaam(key)
                except Exception as exc:  # noqa: BLE001
                    asr_summary["gigaam"] = {"ok": False, "engine": "gigaam", "error": str(exc)}
                    raise RuntimeError(f"GigaAM упал: {exc}") from exc
                mark_ok("asr_run", "gigaam")
            (OUT / "pipeline_asr_summary.json").write_text(
                json.dumps(asr_summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("asr_run", str(exc))
            print_summary()
            raise SystemExit(f"FAIL asr_run: {exc}") from exc

    # ── 4. Снять ASR с GPU (до Sortformer — иначе GPU-конфlict) ───────────────
    if _step_ok("asr_stop"):
        log("=== 4. asr_stop — skip (ok) ===")
    else:
        log("=== 4. снять ASR с GPU ===")
        try:
            gate("asr_stop")
            if os.environ.get("ALLOW_LEGACY_ASR") == "1":
                subprocess.run(["docker", "stop", "skkkk-whisper", "skkkk-gigaam"], check=False)
            else:
                subprocess.run(["docker", "stop", "skkkk-gigaam"], check=False)
            used = wait_vram(2500)
            mark_ok("asr_stop", f"VRAM после выгрузки {used} MiB")
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("asr_stop", str(exc))
            print_summary()
            raise SystemExit(f"FAIL asr_stop: {exc}") from exc

    # ── 5. Sortformer диаризация ─────────────────────────────────────────────
    if _step_ok("sortformer"):
        log("=== 5. Sortformer — skip (ok) ===")
    else:
        log("=== 5. Sortformer v2.1 ===")
        try:
            _run_sortformer(duration)
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_ok("sortformer", f"WARN: {exc}")
            log(f"WARN sortformer: {exc}")

    # ── 6. Загрузить Gemma ──────────────────────────────────────────────────
    if not _uses_local_gemma():
        if _step_ok("gemma_load"):
            log("=== 6. gemma_load — skip (ok) ===")
        else:
            log("=== 6. gemma_load — Cursor LLM, не нужен ===")
            mark_ok("gemma_load", "cursor LLM")
    elif _step_ok("gemma_load"):
        log("=== 6. gemma_load — skip (ok) ===")
    else:
        log("=== 6. загрузить Gemma ===")
        try:
            gate("gemma_load")
            load = subprocess.run(["lms", "load", MODEL, "-y"], cwd=str(ROOT))
            if load.returncode != 0:
                raise RuntimeError("lms load вернул ненулевой код")
            models_resp = urllib.request.urlopen(f"{LMSTUDIO}/models", timeout=10).read().decode("utf-8")
            if "gemma" not in models_resp.lower():
                raise RuntimeError("Gemma не видна в /v1/models после load")
            mark_ok("gemma_load", MODEL)
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("gemma_load", str(exc))
            print_summary()
            raise SystemExit(f"FAIL gemma_load: {exc}") from exc

    # ── 7. Склейка Gemma ─────────────────────────────────────────────────────
    if _step_ok("merge"):
        log("=== 7. merge — skip (ok) ===")
    else:
        log(f"=== 7. склейка {_llm_name()} ===")
        try:
            gate("merge")
            merge = subprocess.run([sys.executable, str(ASR / "merge_ensemble.py")], cwd=str(ROOT))
            if merge.returncode != 0:
                raise RuntimeError(f"merge_ensemble вернул код {merge.returncode}")
            mark_ok("merge")
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("merge", str(exc))
            print_summary()
            raise SystemExit(f"FAIL merge: {exc}") from exc

    # ── 8. Нарезка по голосам + HTML ────────────────────────────────────────
    if _step_ok("view_build"):
        log("=== 8. view_build — skip (ok) ===")
    else:
        log("=== 8. нарезка по голосам ===")
        try:
            gate("view_build")
            from build_transcript_view import build, serve

            view = build(None)
            (OUT / "pipeline_summary.json").write_text(
                json.dumps(
                    {"source": str(source), "duration_sec": round(duration, 2), "asr": asr_summary, "view": view},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            mark_ok("view_build", f"спикеров {view.get('speaker_count', '?')}, реплик {view.get('turns', '?')}")
            log(json.dumps(view, ensure_ascii=False, indent=2))
        except PipelineGateError:
            raise
        except Exception as exc:
            mark_fail("view_build", str(exc))
            print_summary()
            raise SystemExit(f"FAIL view_build: {exc}") from exc

    # ── Итог ─────────────────────────────────────────────────────────────────
    print_summary()
    if args.serve:
        from build_transcript_view import serve
        serve("127.0.0.1", args.port)
    else:
        log(f"готово. просмотр: http://127.0.0.1:{args.port}/out/transcript.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
