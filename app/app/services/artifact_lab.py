"""Доступ к лабораторным прогонам артефакта из веба.

Здесь намеренно нет ни одного fallback'а: если запрошенного прогона нет, будет
``FileNotFoundError`` с именем каталога. Подставлять «похожий» прогон нельзя —
именно так в отчёты и попадали чужие расшифровки.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.app.config import ROOT, Settings
from app.app.models.artifact import Artifact
from app.app.services.storage import artifact_dir

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.paths import LabRun, is_run_id, latest_path, list_run_ids  # noqa: E402


def lab_root(settings: Settings, artifact: Artifact) -> Path:
    return artifact_dir(settings, artifact.project_id, artifact.artifact_id)


def list_runs(settings: Settings, artifact: Artifact) -> list[str]:
    return list_run_ids(lab_root(settings, artifact))


def latest_run_id(settings: Settings, artifact: Artifact) -> str | None:
    """Последний прогон: сначала отметка latest.json, иначе самый свежий каталог."""
    root = lab_root(settings, artifact)
    marker = latest_path(root)
    if marker.is_file():
        import json

        try:
            candidate = json.loads(marker.read_text(encoding="utf-8")).get("run_id")
        except (json.JSONDecodeError, OSError):
            candidate = None
        if isinstance(candidate, str) and (root / "lab" / "runs" / candidate).is_dir():
            return candidate
    runs = list_run_ids(root)
    return runs[0] if runs else None


def vision_run_id(settings: Settings, artifact: Artifact) -> str | None:
    """Прогон с vision-описаниями экрана (если есть), иначе None.

    Кадры на всех прогонах идентичны, поэтому экранные описания можно брать
    из любого прогона; предпочитаем самый свежий с ``text_source == "vision"``.
    """
    import json

    for run_id in list_run_ids(lab_root(settings, artifact)):
        run = LabRun(lab_root(settings, artifact), run_id)
        path = run.resolve("video_transcript.json")
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            continue
        if any(
            isinstance(s, dict)
            and s.get("text_source") == "vision"
            and str(s.get("text") or "").strip()
            for s in segments
        ):
            return run_id
    return None


def resolve_run(settings: Settings, artifact: Artifact, run_id: str | None) -> LabRun:
    root = lab_root(settings, artifact)
    chosen = run_id or latest_run_id(settings, artifact)
    if not chosen:
        raise FileNotFoundError(f"у артефакта {artifact.artifact_id} нет лабораторных прогонов")
    if not is_run_id(chosen):
        raise FileNotFoundError(f"некорректный run_id {chosen!r}")
    run = LabRun(root, chosen)
    if not run.run_json.is_file():
        raise FileNotFoundError(f"нет прогона {chosen} у артефакта {artifact.artifact_id}")
    return run


def ensure_lab_report(settings: Settings, artifact: Artifact, run_id: str | None = None) -> Path:
    """Собрать страницу прогона заново. Кеша нет: отчёт обязан совпадать с stage.json."""
    run = resolve_run(settings, artifact, run_id)

    import importlib

    import asr.build_lab_view as lab_view

    importlib.reload(lab_view)
    return lab_view.write_lab_report(run.artifact_root, run.run_id, artifact_id=artifact.artifact_id)


def lab_file(settings: Settings, artifact: Artifact, run_id: str, relative: str) -> Path:
    """Файл внутри прогона. Выход за каталог отсекается LabRun.resolve."""
    run = resolve_run(settings, artifact, run_id)
    path = run.resolve(relative)
    if not path.is_file():
        raise FileNotFoundError(f"нет файла {relative} в прогоне {run.run_id}")
    return path


def lab_voice_match_path(settings: Settings, artifact: Artifact, run_id: str) -> Path:
    """match.json этапа 4. Боевой sortformer_speaker_match.json это не он."""
    run = resolve_run(settings, artifact, run_id)
    path = run.resolve("voice/match.json")
    if not path.is_file():
        raise FileNotFoundError(
            f"нет voice/match.json в прогоне {run.run_id} — сначала asr/lab_voice.py"
        )
    return path
