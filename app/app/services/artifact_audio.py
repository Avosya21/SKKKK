"""Подготовка звука артефакта: атомарное извлечение WAV из исходника с блокировкой и проверкой полноты, MP3 для скачивания, AAC для проигрывания в браузере и построение аудио-отчётов (стенограмма, Sortformer, сверка спикеров)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from app.app.config import ROOT, Settings
from app.app.models.artifact import Artifact
from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool
from app.app.services.media_cleanup import verify_extracted_wav
from app.app.services.storage import (
    artifact_audio_download_mp3_path,
    artifact_audio_path,
    artifact_audio_playback_path,
    artifact_dir,
    artifact_raw_diar_path,
    artifact_report_url,
    artifact_sortformer_transcript_path,
    artifact_speaker_match_path,
    artifact_transcript_path,
    artifact_two_voice_diar_path,
    ensure_artifact_workspace,
    parsed_dir,
)
from app.app.services.upload import ffprobe_duration

logger = logging.getLogger("skkkk.artifact_audio")

# Speech-oriented AAC: ~10–20× smaller than 16 kHz PCM WAV, streams on iOS/Android.
_PLAYBACK_BITRATE = "48k"
_PLAYBACK_MEDIA_TYPE = "audio/mp4"
_DOWNLOAD_MP3_BITRATE = "64k"
_EXTRACT_LOCK_TIMEOUT_SEC = 3600.0


def _extract_wav(src: Path, dst: Path, sample_rate: int = 16000) -> None:
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # ``.partial`` is not a known muxer; ffmpeg 9+ needs ``-f wav``.
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(dst),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, err)


@contextmanager
def _exclusive_lock(path: Path, *, timeout_sec: float = _EXTRACT_LOCK_TIMEOUT_SEC):
    """Блокировка на файле: API и воркер не пишут input_16k.wav одновременно."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0)
    if handle.read(1) == b"":
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    deadline = time.monotonic() + timeout_sec
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timeout waiting for {path}") from None
                time.sleep(0.2)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _extract_wav_atomic(src: Path, dst: Path, sample_rate: int = 16000) -> None:
    """ffmpeg пишет во временный файл; `input_16k.wav` появляется только после успеха."""
    partial = dst.with_name(dst.name + ".partial")
    try:
        _extract_wav(src, partial, sample_rate)
        os.replace(partial, dst)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def wav_is_complete(wav: Path, source: Path | None = None) -> bool:
    """Готовый мастер, а не обрезок, который ffmpeg ещё пишет на диск."""
    if not wav.is_file():
        return False
    if source is not None and source.is_file() and source.resolve() == wav.resolve():
        return wav.stat().st_size > 0
    ok, _ = verify_extracted_wav(wav)
    if not ok:
        return False
    if source is None or not source.is_file():
        return True
    src_dur = ffprobe_duration(source)
    wav_dur = ffprobe_duration(wav)
    if src_dur is None or wav_dur is None:
        return True
    slack = max(2.0, 0.02 * src_dur)
    return wav_dur + slack >= src_dur


def ensure_audio_wav(settings: Settings, artifact: Artifact) -> Path | None:
    wav = artifact_audio_path(settings, artifact.artifact_id, artifact.project_id)
    src = Path(artifact.file_path)
    lock = wav.with_name("input_16k.wav.lock")
    with _exclusive_lock(lock):
        source = src if src.is_file() else None
        if wav_is_complete(wav, source):
            return wav
        if source is None:
            return wav if wav.is_file() else None
        if src.resolve() == wav.resolve():
            return wav if wav.is_file() else None
        ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
        try:
            _extract_wav_atomic(src, wav)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            logger.error("wav extract failed for %s: %s", artifact.artifact_id, detail)
            return None
        return wav if wav_is_complete(wav, src) else None


def _encode_download_mp3(src_wav: Path, dst_mp3: Path) -> None:
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    dst_mp3.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src_wav),
            "-vn",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            _DOWNLOAD_MP3_BITRATE,
            str(dst_mp3),
        ],
        check=True,
    )


def _encode_playback_aac(src_wav: Path, dst_m4a: Path) -> None:
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    dst_m4a.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src_wav),
            "-vn",
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            _PLAYBACK_BITRATE,
            "-movflags",
            "+faststart",
            str(dst_m4a),
        ],
        check=True,
    )


def _fix_audio_src(html: str, audio_url: str) -> str:
    """Point <audio> at the streaming URL and mark it mobile-friendly."""

    def _rewrite(match: re.Match[str]) -> str:
        tag = match.group(0)
        tag = re.sub(r'\ssrc="[^"]*"', f' src="{audio_url}"', tag, count=1)
        if "preload=" not in tag:
            tag = tag.replace("<audio", '<audio preload="metadata"', 1)
        if "playsinline" not in tag.lower():
            tag = tag.replace("<audio", "<audio playsinline", 1)
        return tag

    return re.sub(r"<audio\b[^>]*>", _rewrite, html, count=1, flags=re.IGNORECASE)


def ensure_audio_playback(settings: Settings, artifact: Artifact) -> tuple[Path, str] | None:
    """Return (path, media_type) for HTML players — AAC when possible, else WAV."""
    wav = ensure_audio_wav(settings, artifact)
    if not wav:
        return None
    m4a = artifact_audio_playback_path(settings, artifact.artifact_id, artifact.project_id)
    needs = not m4a.is_file() or m4a.stat().st_mtime < wav.stat().st_mtime
    if needs:
        try:
            _encode_playback_aac(wav, m4a)
        except (OSError, subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning(
                "playback aac failed for %s, falling back to wav: %s",
                artifact.artifact_id,
                exc,
            )
            return wav, "audio/wav"
    if m4a.is_file():
        return m4a, _PLAYBACK_MEDIA_TYPE
    return wav, "audio/wav"


def ensure_audio_download_mp3(settings: Settings, artifact: Artifact) -> Path | None:
    """MP3 для кнопки «Скачать» на созвонах."""
    wav = ensure_audio_wav(settings, artifact)
    if not wav:
        return None
    mp3 = artifact_audio_download_mp3_path(settings, artifact.artifact_id, artifact.project_id)
    needs = not mp3.is_file() or mp3.stat().st_mtime < wav.stat().st_mtime
    if needs:
        try:
            _encode_download_mp3(wav, mp3)
        except (OSError, subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning(
                "download mp3 failed for %s: %s",
                artifact.artifact_id,
                exc,
            )
            return None
    return mp3 if mp3.is_file() and mp3.stat().st_size > 0 else None


def _rebuild_transcript_report(settings: Settings, artifact: Artifact) -> Path | None:
    """Пересобрать reports/transcript.html из asr/ + match JSON."""
    import os
    import sys

    from app.app.services.artifact_lab import latest_run_id  # noqa: PLC0415

    if latest_run_id(settings, artifact):
        try:
            from app.app.services.pipeline_lab import republish_named_transcript  # noqa: PLC0415

            republish_named_transcript(settings, artifact)
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "lab transcript republish failed for %s: %s",
                artifact.artifact_id,
                exc,
            )
        report = artifact_transcript_path(settings, artifact.artifact_id, artifact.project_id)
        return report if report.is_file() else None

    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    asr_out = ws / "asr"
    match_json = ws / "reports" / "sortformer_speaker_match.json"

    asr_dir = str(ROOT / "asr")
    if asr_dir not in sys.path:
        sys.path.insert(0, asr_dir)
    from seed_artifact_asr import ensure_artifact_asr_inputs  # noqa: PLC0415

    ensure_artifact_asr_inputs(
        root=ROOT,
        project_id=artifact.project_id,
        artifact_id=artifact.artifact_id,
        ws=ws,
        match_json=match_json if match_json.is_file() else None,
    )

    env = os.environ.copy()
    env["SKKKK_ASR_WORK"] = str(ws / "audio")
    env["SKKKK_ASR_OUT"] = str(asr_out)
    env["SKKKK_AUDIO_URL"] = f"/artifacts/{artifact.artifact_id}/audio"
    env["SKKKK_ARTIFACT_ID"] = artifact.artifact_id
    env["SKKKK_PROJECT_ID"] = str(artifact.project_id)
    env["SKKKK_MATCH_JSON"] = str(match_json)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "asr" / "build_transcript_view.py")],
        cwd=str(ROOT),
        env=env,
        check=False,
        timeout=120,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        logger.warning(
            "transcript rebuild failed for %s: exit=%s %s",
            artifact.artifact_id,
            proc.returncode,
            tail,
        )
        return None

    built = asr_out / "transcript.html"
    report = artifact_transcript_path(settings, artifact.artifact_id, artifact.project_id)
    if built.is_file():
        report.parent.mkdir(parents=True, exist_ok=True)
        html = _fix_audio_src(
            built.read_text(encoding="utf-8"),
            f"/artifacts/{artifact.artifact_id}/audio",
        )
        report.write_text(html, encoding="utf-8")
        return report
    logger.warning("transcript rebuild missing HTML for %s: %s", artifact.artifact_id, built)
    return None


def ensure_audio_report(settings: Settings, artifact: Artifact) -> Path:
    report = artifact_transcript_path(settings, artifact.artifact_id, artifact.project_id)
    audio_url = f"/artifacts/{artifact.artifact_id}/audio"
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    match_json = ws / "reports" / "sortformer_speaker_match.json"
    asr_html = ws / "asr" / "transcript.html"
    if not asr_html.is_file():
        asr_html = ROOT / "asr" / "out" / "transcript.html"

    report_body = report.read_text(encoding="utf-8") if report.is_file() else ""
    # Compact row layout (sp-item); older sp-card / sp-table versions must rebuild.
    is_current_layout = "sp-name-line" in report_body and "sp-item" in report_body
    has_registry_ui = (
        "sp-enroll" in report_body
        or "sp-table" in report_body
        or "sp-list" in report_body
        or "sp-item" in report_body
    )
    from app.app.services.plain_text import has_llm_markup_artifacts

    needs_rebuild = False
    if match_json.is_file() and asr_html.is_file():
        if not report.is_file():
            needs_rebuild = True
        elif match_json.stat().st_mtime > report.stat().st_mtime:
            needs_rebuild = True
        elif not has_registry_ui or not is_current_layout:
            needs_rebuild = True
        elif (
            "download-transcript" not in report_body
            or "export-panel" not in report_body
            or "download-brief" not in report_body
            or "overflow-wrap: break-word" not in report_body
            or "tab-summary-brief" not in report_body
            or "rect-act" not in report_body
            or "summary-format-v2" not in report_body
        ):
            needs_rebuild = True
        elif has_llm_markup_artifacts(report_body):
            needs_rebuild = True
    elif report.is_file() and has_llm_markup_artifacts(report_body) and match_json.is_file():
        # Summary may still contain $...$ even if ASR html is missing elsewhere.
        needs_rebuild = True
    if needs_rebuild:
        rebuilt = _rebuild_transcript_report(settings, artifact)
        if rebuilt and rebuilt.is_file():
            ensure_audio_wav(settings, artifact)
            return rebuilt
        # Do not fall through to stale asr/transcript.html with the old table layout.
        if report.is_file() and is_current_layout and audio_url in report_body:
            return report
        if report.is_file() and not is_current_layout:
            logger.warning(
                "transcript rebuild failed for %s; serving stale report until next successful rebuild",
                artifact.artifact_id,
            )

    if report.is_file() and is_current_layout and audio_url in report_body:
        return report

    # Avoid re-copying an outdated wide-table HTML as if it were fresh.
    if report.is_file() and has_registry_ui and not is_current_layout:
        rebuilt = _rebuild_transcript_report(settings, artifact)
        if rebuilt and rebuilt.is_file():
            ensure_audio_wav(settings, artifact)
            return rebuilt

    ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    report.parent.mkdir(parents=True, exist_ok=True)

    legacy = parsed_dir(settings, artifact.project_id, artifact.source_type) / (
        f"{artifact.artifact_id}_transcript.html"
    )
    if legacy.is_file():
        html = _fix_audio_src(legacy.read_text(encoding="utf-8"), audio_url)
        report.write_text(html, encoding="utf-8")
        ensure_audio_wav(settings, artifact)
        return report

    ws_asr_html = ws / "asr" / "transcript.html"
    if ws_asr_html.is_file():
        html = _fix_audio_src(ws_asr_html.read_text(encoding="utf-8"), audio_url)
        report.write_text(html, encoding="utf-8")
        ensure_audio_wav(settings, artifact)
        return report

    if report.is_file():
        html = _fix_audio_src(report.read_text(encoding="utf-8"), audio_url)
        report.write_text(html, encoding="utf-8")
        ensure_audio_wav(settings, artifact)
        return report

    raise FileNotFoundError(f"transcript report missing for {artifact.artifact_id}")


def _default_sortformer_json(settings: Settings, artifact: Artifact) -> Path | None:
    """Только файлы этого артефакта. Чужой эталон из experiments/ подставлять нельзя."""
    ws = artifact_dir(settings, artifact.project_id, artifact.artifact_id)
    candidates = (
        ws / "asr" / "sortformer_segments.json",
        ws / "reports" / "sortformer_segments.json",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def ensure_sortformer_report(settings: Settings, artifact: Artifact) -> Path:
    """Тестовая стенogramma: Sortformer v2.1 вместо PyAnnote."""
    from app.app.services.artifact_lab import latest_run_id

    if latest_run_id(settings, artifact):
        raise FileNotFoundError(
            f"report-sortformer — legacy; lab {artifact.artifact_id} uses /report"
        )
    report = artifact_sortformer_transcript_path(settings, artifact.artifact_id, artifact.project_id)
    sortformer_json = _default_sortformer_json(settings, artifact)
    if sortformer_json is None:
        raise FileNotFoundError(f"Sortformer segments missing for {artifact.artifact_id}")

    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    asr_out = ws / "asr"
    if not (asr_out / "docker_merged_windows.json").is_file():
        raise FileNotFoundError(
            f"нет docker_merged_windows.json у {artifact.artifact_id} — чужой asr/out не подставляем"
        )
    summary = parsed_dir(settings, artifact.project_id, artifact.source_type) / (
        f"{artifact.artifact_id}_call_summary.md"
    )
    audio_url = f"/artifacts/{artifact.artifact_id}/audio"

    needs_build = not report.is_file() or report.stat().st_mtime < sortformer_json.stat().st_mtime
    if needs_build:
        import sys

        asr_dir = str(ROOT / "asr")
        if asr_dir not in sys.path:
            sys.path.insert(0, asr_dir)
        from build_sortformer_transcript_view import build  # noqa: PLC0415

        report.parent.mkdir(parents=True, exist_ok=True)
        build(
            sortformer_json=sortformer_json,
            asr_out=asr_out,
            out_html=report,
            audio_url=audio_url,
            summary_path=summary if summary.is_file() else None,
        )

    html = _fix_audio_src(report.read_text(encoding="utf-8"), audio_url)
    if html != report.read_text(encoding="utf-8"):
        report.write_text(html, encoding="utf-8")
    ensure_audio_wav(settings, artifact)
    return report


def ensure_raw_diar_report(settings: Settings, artifact: Artifact) -> Path:
    """Сырые интервалы диаризации — без Gemma и без сглаживания UI."""
    from app.app.services.artifact_lab import latest_run_id

    if latest_run_id(settings, artifact):
        raise FileNotFoundError(
            f"report-raw-diar — legacy; lab {artifact.artifact_id} uses /report"
        )
    report = artifact_raw_diar_path(settings, artifact.artifact_id, artifact.project_id)
    manifest = ROOT / "experiments" / "diar-presets" / "out" / "manifest.json"
    preset_dir = ROOT / "experiments" / "diar-presets" / "out"
    sortformer_json = _default_sortformer_json(settings, artifact)
    pyannote_fallback = ROOT / "asr" / "out" / "pyannote_segments.json"
    audio_url = f"/artifacts/{artifact.artifact_id}/audio"

    src_mtime = 0.0
    for path in (manifest, sortformer_json, pyannote_fallback):
        if path and path.is_file():
            src_mtime = max(src_mtime, path.stat().st_mtime)
    if preset_dir.is_dir():
        for path in preset_dir.glob("*.json"):
            src_mtime = max(src_mtime, path.stat().st_mtime)

    needs_build = not report.is_file() or report.stat().st_mtime < src_mtime
    if needs_build:
        import sys

        asr_dir = str(ROOT / "asr")
        if asr_dir not in sys.path:
            sys.path.insert(0, asr_dir)
        from build_raw_diar_view import build  # noqa: PLC0415

        ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
        report.parent.mkdir(parents=True, exist_ok=True)
        build(
            out_html=report,
            audio_url=audio_url,
            manifest_path=manifest if manifest.is_file() else None,
            preset_dir=preset_dir,
            sortformer_json=sortformer_json,
            pyannote_fallback=pyannote_fallback if pyannote_fallback.is_file() else None,
            title="Сырая диаризация — сравнение треков",
        )

    html = _fix_audio_src(report.read_text(encoding="utf-8"), audio_url)
    if html != report.read_text(encoding="utf-8"):
        report.write_text(html, encoding="utf-8")
    ensure_audio_wav(settings, artifact)
    return report


def ensure_two_voice_diar_report(settings: Settings, artifact: Artifact) -> Path:
    report = artifact_two_voice_diar_path(settings, artifact.artifact_id, artifact.project_id)
    manifest = ROOT / "experiments" / "diar-presets" / "out" / "two_voice_4min" / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"two-voice manifest missing: {manifest}")
    audio_url = f"/artifacts/{artifact.artifact_id}/audio"
    needs_build = not report.is_file() or report.stat().st_mtime < manifest.stat().st_mtime
    if needs_build:
        import sys

        asr_dir = str(ROOT / "asr")
        if asr_dir not in sys.path:
            sys.path.insert(0, asr_dir)
        from build_raw_diar_view import build  # noqa: PLC0415

        ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
        report.parent.mkdir(parents=True, exist_ok=True)
        build(
            out_html=report,
            audio_url=audio_url,
            manifest_path=manifest,
            title="Диаризация — первые 4 мин (2 голоса M+F)",
        )
    html = _fix_audio_src(report.read_text(encoding="utf-8"), audio_url)
    if html != report.read_text(encoding="utf-8"):
        report.write_text(html, encoding="utf-8")
    return report


def ensure_speaker_match_report(settings: Settings, artifact: Artifact, db: Session) -> Path:
    """Голоса созвона: диаризация (Nemotron/Sortformer) + WeSpeaker match + запоминание голосов."""
    report = artifact_speaker_match_path(settings, artifact.artifact_id, artifact.project_id)
    match_json = (
        artifact_dir(settings, artifact.project_id, artifact.artifact_id)
        / "reports"
        / "sortformer_speaker_match.json"
    )
    if not match_json.is_file():
        raise FileNotFoundError(f"sortformer_speaker_match.json missing for {artifact.artifact_id}")

    from app.app.services.speaker_profiles import list_profiles

    profiles = list_profiles(db, artifact.project_id)
    audio_url = f"/artifacts/{artifact.artifact_id}/audio"
    import sys

    asr_dir = str(ROOT / "asr")
    if asr_dir not in sys.path:
        sys.path.insert(0, asr_dir)
    import build_speaker_match_view  # noqa: PLC0415
    import importlib

    importlib.reload(build_speaker_match_view)

    ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    report.parent.mkdir(parents=True, exist_ok=True)
    build_speaker_match_view.build(
        match_json=match_json,
        out_html=report,
        audio_url=audio_url,
        profiles=profiles,
    )

    html = _fix_audio_src(report.read_text(encoding="utf-8"), audio_url)
    if html != report.read_text(encoding="utf-8"):
        report.write_text(html, encoding="utf-8")
    ensure_audio_wav(settings, artifact)
    return report


def sync_transcript_view_reports(settings: Settings, artifact: Artifact) -> None:
    """Скопировать transcript.html и call_summary.md / call_brief.md из asr/ в reports/ и parsed/."""
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    asr_dir = ws / "asr"
    reports = ws / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    audio_url = f"/artifacts/{artifact.artifact_id}/audio"

    asr_html = asr_dir / "transcript.html"
    report_path = reports / "transcript.html"
    if asr_html.is_file():
        html = _fix_audio_src(asr_html.read_text(encoding="utf-8"), audio_url)
        report_path.write_text(html, encoding="utf-8")

    summary_src = asr_dir / "call_summary.md"
    if summary_src.is_file():
        shutil.copy2(summary_src, reports / "call_summary.md")

    brief_src = asr_dir / "call_brief.md"
    if brief_src.is_file():
        shutil.copy2(brief_src, reports / "call_brief.md")

    dest_dir = parsed_dir(settings, artifact.project_id, artifact.source_type)
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = artifact.artifact_id
    if summary_src.is_file():
        shutil.copy2(summary_src, dest_dir / f"{base}_call_summary.md")
    if brief_src.is_file():
        shutil.copy2(brief_src, dest_dir / f"{base}_call_brief.md")
    if report_path.is_file():
        shutil.copy2(report_path, dest_dir / f"{base}_transcript.html")


def finalize_audio_outputs(settings: Settings, artifact: Artifact) -> tuple[Path, str]:
    """Перенести HTML/summary в reports/ и вернуть путь к финальному тексту."""
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    asr_dir = ws / "asr"
    reports = ws / "reports"

    sync_transcript_view_reports(settings, artifact)
    report_path = reports / "transcript.html"

    final_candidates = (
        asr_dir / "docker_gigaam_diar_smooth_timecodes.txt",
        asr_dir / "docker_gigaam_diar_smooth.txt",
        asr_dir / "docker_merged_timecodes.txt",
        asr_dir / "docker_merged.txt",
        asr_dir / "docker_gigaam_merged.txt",
        asr_dir / "merged_final.txt",
    )
    final_src = next((p for p in final_candidates if p.is_file()), None)
    if not final_src:
        raise FileNotFoundError("no merged transcript in artifact asr dir")

    dest_dir = parsed_dir(settings, artifact.project_id, artifact.source_type)
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = artifact.artifact_id
    final_dst = dest_dir / f"{base}_final.txt"
    shutil.copy2(final_src, final_dst)

    mapping = {
        "docker_whisper.txt": f"{base}_raw_whisper.txt",
        "docker_tone.txt": f"{base}_raw_tone.txt",
        "docker_gigaam.txt": f"{base}_raw_gigaam.txt",
        "docker_gigaam_speakers.json": f"{base}_speakers.json",
        "docker_merged_speakers.json": f"{base}_speakers.json",
        "call_summary.md": f"{base}_call_summary.md",
        "call_brief.md": f"{base}_call_brief.md",
        "voice_audit.json": f"{base}_voice_audit.json",
    }
    for src_name, dst_name in mapping.items():
        src = asr_dir / src_name
        if src.is_file():
            dst = dest_dir / dst_name
            if not dst.exists() or src_name.endswith("speakers.json"):
                shutil.copy2(src, dst)

    # Pre-build AAC so the first phone play is not blocked on ffmpeg.
    ensure_audio_playback(settings, artifact)
    ensure_audio_download_mp3(settings, artifact)

    return final_dst, artifact_report_url(artifact.artifact_id)
