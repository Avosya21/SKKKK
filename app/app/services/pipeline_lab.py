"""Боевой ASR: лабораторный контур → публикация → speakers_pending."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.app.config import ROOT, Settings
from app.app.llm_labels import llm_runtime_label
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.artifact_audio import ensure_audio_wav, finalize_audio_outputs
from app.app.services.artifact_lab import latest_run_id, resolve_run, vision_run_id
from app.app.services.media_cleanup import maybe_discard_video_source, reconcile_artifact_source_path
from app.app.services.notify import notify_artifact_user
from app.app.services.pipeline_audio import _asr_env
from app.app.services.status import set_status
from app.app.services.storage import (
    artifact_audio_path,
    artifact_report_url,
    ensure_artifact_workspace,
)

logger = logging.getLogger("skkkk.pipeline")

LAB_PYTHON = ROOT / "venvs" / "lab" / "Scripts" / "python.exe"
APP_PYTHON = Path(sys.executable)

LAB_STEPS: tuple[tuple[str, str], ...] = (
    ("audio_prep", "Подготовка WAV 16 кГц"),
    ("lab_onnx", "Parakeet + диаризация Nemotron"),
    ("lab_sense", "Cursor: спорные спикеры"),
    ("lab_giga", "GigaAM — второй слух"),
    ("lab_reconcile", "Сверка lab vs GigaAM"),
    ("lab_proof", "Вычитка текста"),
    ("lab_voice", "WeSpeaker — голоса"),
    ("speakers_confirm", "Подтверждение спикеров"),
)

OVERLAY_SCRIPTS: tuple[tuple[str, str, int], ...] = (
    ("lab_sense", "asr/lab_sense.py", 3600),
    ("lab_giga", "asr/lab_giga.py", 7200),
    ("lab_reconcile", "asr/lab_reconcile.py", 600),
    ("lab_proof", "asr/lab_proof.py", 3600),
    ("lab_voice", "asr/lab_voice.py", 1800),
)

OVERLAY_DONE_FILES: dict[str, str] = {
    "lab_sense": "sense/sense.json",
    "lab_giga": "giga/giga.json",
    "lab_reconcile": "reconcile/text.json",
    "lab_proof": "proof/transcript.json",
    "lab_voice": "voice/match.json",
}


def overlay_output_ready(settings: Settings, artifact: Artifact, run_id: str, step_id: str) -> bool:
    """True, если оверлей уже записал канонический файл — шаг можно не гонять."""
    relative = OVERLAY_DONE_FILES.get(step_id)
    if not relative:
        return False
    path = resolve_run(settings, artifact, run_id).resolve(relative)
    return path.is_file() and path.stat().st_size > 8


def lab_python() -> Path:
    if not LAB_PYTHON.is_file():
        raise FileNotFoundError(f"нет {LAB_PYTHON} — создайте venvs\\lab")
    return LAB_PYTHON


def _checklist_path(ws: Path) -> Path:
    return ws / "asr" / "lab_checklist.json"


def _read_checklist(ws: Path) -> dict:
    path = _checklist_path(ws)
    if not path.is_file():
        return {"phase": "lab", "steps": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"phase": "lab", "steps": {}}


def _write_checklist(ws: Path, payload: dict) -> None:
    path = _checklist_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def init_lab_checklist(ws: Path, *, steps: tuple[tuple[str, str], ...] | None = None) -> None:
    payload = _read_checklist(ws)
    payload["phase"] = "lab"
    rows = payload.setdefault("steps", {})
    for step_id, _label in steps or LAB_STEPS:
        rows.setdefault(step_id, {"status": "pending"})
    _write_checklist(ws, payload)


def mark_lab_step(ws: Path, step_id: str, status: str, *, detail: str | None = None) -> None:
    payload = _read_checklist(ws)
    row = payload.setdefault("steps", {}).setdefault(step_id, {})
    row["status"] = status
    if detail:
        row["detail"] = detail
    _write_checklist(ws, payload)


def _run_cmd(
    cmd: list[str],
    env: dict[str, str],
    *,
    timeout: int,
    artifact_id: str,
    step: str,
) -> str:
    log_event(logger, "lab.step.start", artifact=artifact_id, step=step, cmd=cmd[-1])
    started = time.monotonic()
    log_dir = ROOT / "data" / "logs" / "lab_steps"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_art = "".join(c if c.isalnum() or c in "-_" else "_" for c in artifact_id)[:80]
    safe_step = "".join(c if c.isalnum() or c in "-_" else "_" for c in step)[:40]
    stdout_path = log_dir / f"{safe_art}_{safe_step}.out.log"
    stderr_path = log_dir / f"{safe_art}_{safe_step}.err.log"
    from app.app.config import get_settings
    from app.app.services.llm_keeper import asr_gpu_hold

    # GPU-шаг ASR: модель на это время выгружена и не загружается (llm_keeper.asr_gpu_hold, Р1 по замеру 2026-10-05)
    with asr_gpu_hold(get_settings(), step=step),             stdout_path.open("w", encoding="utf-8") as out_f, stderr_path.open("w", encoding="utf-8") as err_f:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=out_f,
            stderr=err_f,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    elapsed = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        try:
            tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            if not tail.strip():
                tail = stdout_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            tail = ""
        from app.app.services.lab_errors import sanitize_lab_error

        message = sanitize_lab_error(tail, step=step)
        log_event(logger, "lab.step.failed", artifact=artifact_id, step=step, error=message)
        raise RuntimeError(message)
    log_event(logger, "lab.step.ok", artifact=artifact_id, step=step, seconds=elapsed)
    try:
        return stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _prepare_wav(settings: Settings, artifact: Artifact, src: Path) -> Path:
    ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    wav = ensure_audio_wav(settings, artifact)
    if wav is None:
        raise FileNotFoundError(
            f"cannot prepare wav for {artifact.artifact_id}: ffmpeg не смог извлечь 16 kHz WAV"
        )
    if maybe_discard_video_source(src, wav, artifact_id=artifact.artifact_id):
        artifact.file_path = str(wav)
    new_path = reconcile_artifact_source_path(artifact.file_path, wav)
    if new_path:
        artifact.file_path = new_path
    return wav


def resumable_lab_run_id(settings: Settings, artifact: Artifact, wav: Path) -> str | None:
    """Вернуть latest run_id только если исходник в run.json совпадает с WAV на диске."""
    existing = latest_run_id(settings, artifact)
    if not existing or not wav.is_file():
        return None
    try:
        run = resolve_run(settings, artifact, existing)
        meta = json.loads(run.run_json.read_text(encoding="utf-8"))
        expected = str((meta.get("source") or {}).get("sha256") or "")
    except (OSError, FileNotFoundError, json.JSONDecodeError, TypeError):
        return None
    if not expected:
        return None
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from asr.lab.run import sha256_file  # noqa: PLC0415

    actual = sha256_file(wav)
    if actual == expected:
        return existing
    log_event(
        logger,
        "lab.run.stale",
        artifact=artifact.artifact_id,
        run_id=existing,
        expected=expected[:12],
        actual=actual[:12],
    )
    return None


def _lab_env(settings: Settings, artifact: Artifact) -> dict[str, str]:
    env = _asr_env(settings, artifact)
    env["DATA_DIR"] = str(settings.data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _load_proof_replicas(settings: Settings, artifact: Artifact, run_id: str) -> list[dict]:
    run = resolve_run(settings, artifact, run_id)
    path = run.resolve("proof/transcript.json")
    if not path.is_file():
        raise FileNotFoundError(f"нет proof/transcript.json в прогоне {run_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [row for row in (payload.get("replicas") or []) if str(row.get("text") or "").strip()]


def replicas_to_turns(replicas: list[dict]):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(ROOT / "asr") not in sys.path:
        sys.path.insert(0, str(ROOT / "asr"))
    from speakers import Turn  # noqa: PLC0415

    return [
        Turn(
            start=float(row.get("start") or 0.0),
            end=float(row.get("end") or row.get("start") or 0.0),
            speaker=str(row.get("speaker") or "unknown"),
            text=str(row.get("text") or "").strip(),
        )
        for row in replicas
    ]


def replicas_to_turn_words(replicas: list[dict]) -> list[list[dict]]:
    """Слова реплик (с таймингами) параллельно turns — для точечной подсказки экрана."""
    out: list[list[dict]] = []
    for row in replicas:
        words: list[dict] = []
        for w in row.get("words") or []:
            if not isinstance(w, dict):
                continue
            text = str(w.get("final") or w.get("word") or "").strip()
            if not text:
                continue
            try:
                start = float(w.get("start") or 0.0)
                end = float(w.get("end") or start)
            except (TypeError, ValueError):
                continue
            if end < start:
                start, end = end, start
            words.append({"start": start, "end": end, "text": text})
        out.append(words)
    return out


def _frame_url_map(run) -> dict[str, str]:
    """frame_id -> относительный путь к jpg (posix) из frames.json прогона."""
    frame_path = run.resolve("frames.json")
    if not frame_path.is_file():
        return {}
    try:
        rows = json.loads(frame_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("frame_id") or "")
        rel = row.get("path")
        if not fid or not rel or not str(rel).lower().endswith(".jpg"):
            continue
        out[fid] = Path(rel).as_posix()
    return out


def load_run_screen_segments(settings: Settings, artifact: Artifact, run_id: str | None) -> list[dict]:
    """Сегменты экрана (текст кадров) из lab run для HTML-подсказок."""
    if not run_id:
        return []
    try:
        run = resolve_run(settings, artifact, run_id)
    except FileNotFoundError:
        return []
    path = run.resolve("video_transcript.json")
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list):
        return []
    out = [s for s in segments if isinstance(s, dict) and str(s.get("text") or "").strip()]
    if not out:
        return out
    relmap = _frame_url_map(run)
    if not relmap:
        return out
    for seg in out:
        rel = relmap.get(str(seg.get("frame_id") or ""))
        if rel:
            seg["frame_url"] = f"/artifacts/{artifact.artifact_id}/lab/{run_id}/frame/{rel}"
    return out


def write_prod_view(
    asr_dir: Path,
    turns,
    *,
    match_payload: dict | None,
    artifact_id: str,
    project_id: int,
    screen_segments: list[dict] | None = None,
    turn_words: list[list[dict]] | None = None,
) -> None:
    if str(ROOT / "asr") not in sys.path:
        sys.path.insert(0, str(ROOT / "asr"))
    from speaker_labels import apply_match_labels, speaker_registry_rows  # noqa: PLC0415
    from speakers import format_turns_timecodes, format_turns_txt, turns_to_json  # noqa: PLC0415
    from transcript_view import load_call_summary_files, write_transcript_html  # noqa: PLC0415
    from paths import default_audio_url  # noqa: PLC0415

    labeled = apply_match_labels(list(turns), match_payload) if match_payload else list(turns)
    asr_dir.mkdir(parents=True, exist_ok=True)
    payload = turns_to_json(labeled, None)
    (asr_dir / "docker_gigaam_speakers.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (asr_dir / "docker_merged_speakers.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    timed = format_turns_timecodes(labeled)
    plain = format_turns_txt(labeled)
    (asr_dir / "docker_gigaam_diar_smooth_timecodes.txt").write_text(timed, encoding="utf-8")
    (asr_dir / "docker_gigaam_diar_smooth.txt").write_text(plain, encoding="utf-8")
    (asr_dir / "docker_merged_timecodes.txt").write_text(timed, encoding="utf-8")
    (asr_dir / "docker_merged.txt").write_text(plain, encoding="utf-8")
    (asr_dir / "docker_merged_plain.txt").write_text(plain, encoding="utf-8")

    summary_md, brief_md = load_call_summary_files(asr_dir)
    registry = speaker_registry_rows(match_payload) if match_payload else None
    write_transcript_html(
        asr_dir / "transcript.html",
        title="Стенограмма",
        audio_url=default_audio_url(),
        turns=labeled,
        mapping=None,
        summary_md=summary_md,
        brief_md=brief_md,
        artifact_id=artifact_id,
        project_id=project_id,
        registry=registry,
        screen_segments=screen_segments or [],
        turn_words=turn_words or None,
    )


def _append_screen_context(
    asr_dir: Path,
    run,
    replicas: list[dict],
) -> None:
    """Добавить блоки экранного контекста в docker_merged*.txt.

    Формат совместим с ``chunk_transcript``: ``[таймкод] Спикер: текст``.
    Каждый кадр экрана → блок «Экран» (полное vision-описание) +
    блок «Речь на экране» (реплики, совпадающие по времени с кадром).
    """
    path = run.resolve("video_transcript.json")
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list):
        return
    segments = [s for s in segments if isinstance(s, dict) and str(s.get("text") or "").strip()]
    if not segments:
        return

    def _fmt(sec: float) -> str:
        m, s = divmod(int(sec), 60)
        return f"{m:02d}:{s:02d}"

    blocks: list[str] = []
    for seg in segments:
        s = float(seg.get("start") or 0)
        e = float(seg.get("end") or s)
        speaker = seg.get("speaker") or seg.get("identity") or "экран"
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        text = " ".join(part for part in text.splitlines() if part.strip())
        overlap_snippets: list[str] = []
        for row in replicas:
            r_start = float(row.get("start") or 0)
            r_end = float(row.get("end") or r_start)
            if r_end <= s or r_start >= e:
                continue
            words = row.get("words") or []
            if words:
                snippet = " ".join(
                    str(w.get("final") or w.get("word") or "").strip()
                    for w in words
                    if isinstance(w, dict)
                    and float(w.get("end") or 0) > s
                    and float(w.get("start") or 0) < e
                )
            else:
                snippet = str(row.get("text") or "").strip()
            if snippet:
                overlap_snippets.append(
                    f"{row.get('speaker') or 'спикер'}: {snippet}"
                )
        blocks.append(f"[{_fmt(s)} - {_fmt(e)}] Экран (показывал {speaker}): {text}")
        if overlap_snippets:
            blocks.append(
                f"[{_fmt(s)} - {_fmt(e)}] Речь на экране: "
                + " ".join(overlap_snippets)
            )
        blocks.append("")

    if not blocks:
        return

    screen_text = "\n".join(blocks)
    for name in (
        "docker_gigaam_diar_smooth_timecodes.txt",
        "docker_gigaam_diar_smooth.txt",
        "docker_merged_timecodes.txt",
        "docker_merged.txt",
        "docker_merged_plain.txt",
    ):
        p = asr_dir / name
        if p.is_file():
            p.write_text(
                p.read_text(encoding="utf-8") + "\n" + screen_text,
                encoding="utf-8",
            )


def publish_lab_to_prod(db: Session, settings: Settings, artifact: Artifact, run_id: str) -> str:
    """proof + voice/match → боевые файлы артефакта. Возвращает текст стенограммы."""
    replicas = _load_proof_replicas(settings, artifact, run_id)
    if not replicas:
        return ""

    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    run = resolve_run(settings, artifact, run_id)
    match_src = run.resolve("voice/match.json")
    match_dst = ws / "reports" / "sortformer_speaker_match.json"
    match_dst.parent.mkdir(parents=True, exist_ok=True)
    payload: dict | None = None
    if match_src.is_file():
        payload = json.loads(match_src.read_text(encoding="utf-8"))
        payload["written_to_db"] = True
        payload["source"] = "lab"
        match_dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        from app.app.services.speaker_profiles import (  # noqa: PLC0415
            _save_links_from_payload,
            rebuild_match_report_html,
        )

        _save_links_from_payload(
            db,
            project_id=artifact.project_id,
            artifact_id=artifact.artifact_id,
            payload=payload,
        )
        rebuild_match_report_html(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)

    turns = replicas_to_turns(replicas)
    screen_run_id = vision_run_id(settings, artifact) or run_id
    screen_segments = load_run_screen_segments(settings, artifact, screen_run_id)
    turn_words = replicas_to_turn_words(replicas)
    write_prod_view(
        ws / "asr",
        turns,
        match_payload=payload,
        artifact_id=artifact.artifact_id,
        project_id=artifact.project_id,
        screen_segments=screen_segments,
        turn_words=turn_words,
    )
    screen_run = resolve_run(settings, artifact, screen_run_id)
    _append_screen_context(ws / "asr", screen_run, replicas)
    final_path, transcript_url = finalize_audio_outputs(settings, artifact)
    text = final_path.read_text(encoding="utf-8")
    artifact.transcription_file_path = str(final_path)
    artifact.transcription_text = text
    artifact.transcript_html_url = transcript_url or artifact_report_url(artifact.artifact_id)
    db.commit()
    return text


def republish_named_transcript(settings: Settings, artifact: Artifact) -> tuple[Path, str]:
    """Proof + текущий match JSON → speakers.json, HTML, parsed/_final.txt.

    Lab-канон: не вызывать ``build_transcript_view`` — он нарезает Sortformer
    ``SPEAKER_00`` поверх proof и вытирает сохранённые имена.
    """
    run_id = latest_run_id(settings, artifact)
    if not run_id:
        raise FileNotFoundError(f"нет лабораторного прогона у {artifact.artifact_id}")
    replicas = _load_proof_replicas(settings, artifact, run_id)
    if not replicas:
        raise FileNotFoundError("пустая стенограмма proof — нечего подписывать")
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    match_path = ws / "reports" / "sortformer_speaker_match.json"
    payload = json.loads(match_path.read_text(encoding="utf-8")) if match_path.is_file() else None
    turns = replicas_to_turns(replicas)
    screen_run_id = vision_run_id(settings, artifact) or run_id
    screen_segments = load_run_screen_segments(settings, artifact, screen_run_id)
    turn_words = replicas_to_turn_words(replicas)
    write_prod_view(
        ws / "asr",
        turns,
        match_payload=payload,
        artifact_id=artifact.artifact_id,
        project_id=artifact.project_id,
        screen_segments=screen_segments,
        turn_words=turn_words,
    )
    screen_run = resolve_run(settings, artifact, screen_run_id)
    _append_screen_context(ws / "asr", screen_run, replicas)
    final_path, transcript_url = finalize_audio_outputs(settings, artifact)
    return final_path, transcript_url or artifact_report_url(artifact.artifact_id)


def apply_confirmed_speaker_names(db: Session, settings: Settings, artifact: Artifact) -> str:
    """Имена из match JSON в turns, HTML и parsed/_final.txt (enroll и confirm)."""
    final_path, transcript_url = republish_named_transcript(settings, artifact)
    text = final_path.read_text(encoding="utf-8")
    artifact.transcription_file_path = str(final_path)
    artifact.transcription_text = text
    artifact.transcript_html_url = transcript_url
    # «Запомнить», «Слить», подтверждение имён чанки не переиндексируют — но имена в привязанных сущностях
    # (кто сказал / кто утвердил) обновляем сразу: дёшево, без LLM и Ollama.
    from app.app.services.entities import rebind_call_entities  # noqa: PLC0415

    rebind_call_entities(db, settings, artifact)
    db.commit()
    return text


def process_lab_artifact(db: Session, settings: Settings, artifact: Artifact) -> str:
    """Полный lab-контур. При voice_gate — стоп на speakers_pending (пустое возвращение)."""
    try:
        return _process_lab_core(db, settings, artifact)
    finally:
        from app.app.services.docker_runtime import stop_gigaam

        stop_gigaam()


def _process_lab_core(db: Session, settings: Settings, artifact: Artifact) -> str:
    src = Path(artifact.file_path)
    wav = artifact_audio_path(settings, artifact.artifact_id, artifact.project_id)
    if not src.is_file():
        if wav.is_file():
            src = wav
        else:
            raise FileNotFoundError(f"missing audio {artifact.file_path}")

    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    init_lab_checklist(ws)
    env = _lab_env(settings, artifact)

    set_status(db, artifact, "preprocessing")
    mark_lab_step(ws, "audio_prep", "running")
    db.commit()
    wav = _prepare_wav(settings, artifact, src)
    mark_lab_step(ws, "audio_prep", "ok")
    db.commit()

    set_status(db, artifact, "asr_running")
    from app.app.llm_settings import uses_local_gemma  # noqa: PLC0415

    if uses_local_gemma(settings.llm_provider):
        from app.app.services.gpu_phase import prepare_for_asr  # noqa: PLC0415

        prepare_for_asr(settings=settings)

    mark_lab_step(ws, "lab_onnx", "running")
    onnx_cmd = [
        str(lab_python()),
        str(ROOT / "asr" / "lab_pipeline.py"),
        "--project-id",
        str(artifact.project_id),
        "--artifact-id",
        artifact.artifact_id,
        "--no-voice",
    ]
    existing = resumable_lab_run_id(settings, artifact, wav)
    if existing:
        onnx_cmd.extend(["--run-id", existing])
    log_event(
        logger,
        "lab.start",
        artifact=artifact.artifact_id,
        llm_provider=settings.llm_provider,
        llm=llm_runtime_label(settings.llm_provider),
    )
    _run_cmd(onnx_cmd, env, timeout=7200, artifact_id=artifact.artifact_id, step="lab_onnx")
    run_id = latest_run_id(settings, artifact)
    if not run_id:
        raise RuntimeError("lab_pipeline не оставил run_id в latest.json")
    mark_lab_step(ws, "lab_onnx", "ok", detail=run_id)

    from app.app.services.docker_runtime import ensure_gigaam_ready  # noqa: PLC0415

    ensure_gigaam_ready(settings.gigaam_url)

    for step_id, rel, timeout in OVERLAY_SCRIPTS:
        if overlay_output_ready(settings, artifact, run_id, step_id):
            log_event(logger, "lab.step.skip", artifact=artifact.artifact_id, step=step_id, run_id=run_id)
            mark_lab_step(ws, step_id, "ok", detail="already on disk")
            continue
        mark_lab_step(ws, step_id, "running")
        cmd = [
            str(APP_PYTHON),
            str(ROOT / rel),
            "--project-id",
            str(artifact.project_id),
            "--artifact-id",
            artifact.artifact_id,
            "--run-id",
            run_id,
        ]
        _run_cmd(cmd, env, timeout=timeout, artifact_id=artifact.artifact_id, step=step_id)
        mark_lab_step(ws, step_id, "ok")

    from app.app.services.meeting_analyze import inject_known_speaker_into_lab_match

    known_speaker = inject_known_speaker_into_lab_match(settings, artifact, run_id)
    if not known_speaker:
        from app.app.services.speaker_auto import auto_accept_lab_voices

        auto_accept_lab_voices(settings, artifact, run_id)
    text = publish_lab_to_prod(db, settings, artifact, run_id)
    if not text:
        artifact.error_message = "речь не обнаружена"
        set_status(db, artifact, "done", reason="речь не обнаружена")
        return ""

    if known_speaker:
        set_status(db, artifact, "transcript_ready")
        notify_artifact_user(db, settings, artifact, f"Стенограмма готова: {artifact.original_filename}")
        return text

    from app.app.services.speaker_auto import GATE_OFF, gate_mode, gate_proceeds, unnamed_labels

    gate = gate_mode(settings)
    match_path = ws / "reports" / "sortformer_speaker_match.json"
    payload = json.loads(match_path.read_text(encoding="utf-8")) if match_path.is_file() else None
    unnamed = unnamed_labels(payload) if payload else []
    if gate != GATE_OFF and gate_proceeds(settings, unnamed):
        # R3: неизвестные голоса не останавливают конвейер — остаются speaker_N, имя дадут позже (знания пересчитаются сами)
        mark_lab_step(ws, "speakers_confirm", "ok", detail="без ожидания" if unnamed else "все голоса узнаны")
        set_status(db, artifact, "transcript_ready")
        db.commit()
        text_msg = (
            f"Стенограмма готова: {artifact.original_filename}. Назовите голоса: {len(unnamed)}"
            if unnamed
            else f"Стенограмма готова: {artifact.original_filename}"
        )
        notify_artifact_user(db, settings, artifact, text_msg)
        log_event(logger, "lab.speakers_gate", artifact=artifact.artifact_id, gate=gate, unnamed=",".join(unnamed))
        return text

    if gate != GATE_OFF:
        mark_lab_step(ws, "speakers_confirm", "running", detail="ожидание пользователя")
        set_status(db, artifact, "speakers_pending")
        db.commit()
        notify_artifact_user(
            db,
            settings,
            artifact,
            f"Подтвердите спикеров: {artifact.original_filename}",
        )
        log_event(logger, "lab.speakers_pending", artifact=artifact.artifact_id, run_id=run_id)
        return ""

    set_status(db, artifact, "transcript_ready")
    notify_artifact_user(db, settings, artifact, f"Стенограмма готова: {artifact.original_filename}")
    return text
