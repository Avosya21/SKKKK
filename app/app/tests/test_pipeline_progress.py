import json
from pathlib import Path

from app.app.config import Settings
from app.app.services.pipeline_progress import build_artifact_progress


class _Art:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_progress_from_asr_checklist(tmp_path, monkeypatch):
    settings = Settings(secret_key="x" * 16)
    project_id = 8
    artifact_id = "art_test"
    ws = tmp_path / "data" / "projects" / str(project_id) / "artifacts" / artifact_id
    asr = ws / "asr"
    asr.mkdir(parents=True)
    (asr / "pipeline_checklist.json").write_text(
        json.dumps(
            {
                "steps": {
                    "audio_prep": {"status": "ok", "detail": "28.5 мин"},
                    "asr_run": {"status": "running"},
                    "merge": {"status": "pending"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def _ws(_settings, pid, aid):
        assert aid == artifact_id
        return ws

    monkeypatch.setattr("app.app.services.pipeline_progress.ensure_artifact_workspace", _ws)

    art = _Art(
        artifact_id=artifact_id,
        project_id=project_id,
        file_type="audio",
        status="asr_running",
        original_filename="call.webm",
    )
    progress = build_artifact_progress(settings, art)
    assert progress is not None
    assert progress.phase == "asr"
    assert "ASR" in progress.current_label or progress.current_label
    running = [s for s in progress.steps if s.status == "running"]
    assert any(s.id == "asr_run" for s in running)


def test_progress_queue_when_uploaded(tmp_path, monkeypatch):
    settings = Settings(secret_key="x" * 16)
    ws = tmp_path / "ws"
    ws.mkdir()

    monkeypatch.setattr(
        "app.app.services.pipeline_progress.ensure_artifact_workspace",
        lambda *_a, **_k: ws,
    )
    art = _Art(
        artifact_id="art_q",
        project_id=1,
        file_type="audio",
        status="uploaded",
        original_filename="x.webm",
    )
    progress = build_artifact_progress(settings, art)
    assert progress is not None
    assert progress.phase == "queue"
    assert progress.steps[0].status == "running"
