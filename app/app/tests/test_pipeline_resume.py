from __future__ import annotations

import json

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.services.pipeline_resume import should_continue_asr


def test_should_continue_asr_uses_artifacts_dir(tmp_path):
    settings = Settings(secret_key="test-secret-key-32chars!!", data_dir=tmp_path)
    artifact = Artifact(
        artifact_id="art_resume",
        project_id=7,
        user_id=1,
        original_filename="call.webm",
        stored_filename="call.webm",
        file_path="/x",
        file_type="audio",
        source_type="call",
        sha256="abc",
    )
    asr_dir = tmp_path / "projects" / "7" / "artifacts" / "art_resume" / "asr"
    asr_dir.mkdir(parents=True)
    (asr_dir / "pipeline_checklist.json").write_text(
        json.dumps(
            {
                "steps": {
                    "audio_prep": {"status": "ok"},
                    "merge": {"status": "fail", "reason": "cursor timeout"},
                }
            }
        ),
        encoding="utf-8",
    )
    assert should_continue_asr(settings, artifact) is True
