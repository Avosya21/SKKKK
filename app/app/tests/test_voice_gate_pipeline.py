from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.app.config import Settings


class _Art:
    def __init__(self, **kwargs):
        defaults = {"source_type": "call"}
        defaults.update(kwargs)
        self.__dict__.update(defaults)
        if not hasattr(self, "id"):
            self.id = 1


def test_process_artifact_runs_lab_on_upload(tmp_path):
    from app.app.workers.pipeline import process_artifact

    settings = Settings(secret_key="x" * 16, voice_gate_before_asr=True, data_dir=tmp_path)
    artifact = _Art(
        artifact_id="art_gate",
        project_id=1,
        file_type="audio",
        status="uploaded",
        original_filename="call.webm",
    )
    db = MagicMock()

    with patch("app.app.services.pipeline_lab.process_lab_artifact", return_value="") as lab_mock:
        with patch("app.app.services.pipeline_audio.process_audio_artifact") as asr_mock:
            process_artifact(db, settings, artifact)

    lab_mock.assert_called_once_with(db, settings, artifact)
    asr_mock.assert_not_called()


def test_process_artifact_retries_error_via_lab(tmp_path):
    from app.app.workers.pipeline import process_artifact

    settings = Settings(secret_key="x" * 16, voice_gate_before_asr=True, data_dir=tmp_path)
    artifact = _Art(
        artifact_id="art_err",
        project_id=1,
        file_type="audio",
        status="error",
        original_filename="call.webm",
    )
    db = MagicMock()

    with patch("app.app.services.pipeline_lab.process_lab_artifact", return_value="") as lab_mock:
        with patch("app.app.services.pipeline_audio.process_audio_artifact") as asr_mock:
            process_artifact(db, settings, artifact)

    lab_mock.assert_called_once_with(db, settings, artifact)
    asr_mock.assert_not_called()


def test_process_artifact_retries_error_via_lab(tmp_path):
    from app.app.workers.pipeline import process_artifact

    settings = Settings(secret_key="x" * 16, voice_gate_before_asr=True, data_dir=tmp_path)
    artifact = _Art(
        artifact_id="art_err",
        project_id=1,
        file_type="audio",
        status="error",
        original_filename="call.webm",
    )
    db = MagicMock()

    with patch("app.app.services.pipeline_lab.process_lab_artifact", return_value="") as lab_mock:
        with patch("app.app.services.pipeline_audio.process_audio_artifact") as asr_mock:
            process_artifact(db, settings, artifact)

    lab_mock.assert_called_once_with(db, settings, artifact)
    asr_mock.assert_not_called()


def test_process_artifact_stops_on_speakers_pending(tmp_path):
    from app.app.workers.pipeline import process_artifact

    settings = Settings(secret_key="x" * 16, voice_gate_before_asr=True, data_dir=tmp_path)
    artifact = _Art(
        artifact_id="art_pending",
        project_id=1,
        file_type="audio",
        status="speakers_pending",
        original_filename="call.webm",
    )
    db = MagicMock()

    with patch("app.app.services.pipeline_audio.process_audio_artifact") as asr_mock:
        with patch("app.app.services.pipeline_lab.process_lab_artifact") as lab_mock:
            process_artifact(db, settings, artifact)

    lab_mock.assert_not_called()
    asr_mock.assert_not_called()


def test_process_artifact_confirm_applies_names_then_knowledge(tmp_path):
    from app.app.workers.pipeline import process_artifact

    settings = Settings(secret_key="x" * 16, voice_gate_before_asr=True, data_dir=tmp_path)
    artifact = _Art(
        artifact_id="art_named",
        project_id=1,
        file_type="audio",
        status="speakers_confirmed",
        original_filename="call.webm",
        transcription_text="",
    )
    db = MagicMock()

    with patch(
        "app.app.services.pipeline_lab.apply_confirmed_speaker_names",
        return_value="[00:00] Елена: привет",
    ) as names_mock:
        with patch("app.app.services.pipeline_audio.process_audio_artifact") as asr_mock:
            with patch("app.app.workers.pipeline.run_knowledge_pipeline") as knowledge_mock:

                def _set_status(_db, art, status, **_kw):
                    art.status = status

                with patch("app.app.workers.pipeline.set_status", side_effect=_set_status):
                    process_artifact(db, settings, artifact)

    names_mock.assert_called_once()
    asr_mock.assert_not_called()
    knowledge_mock.assert_called_once()


def test_confirm_speakers_enqueues_knowledge_job():
    from app.app.models.job import Job
    from app.app.services.speaker_profiles import confirm_speakers_and_enqueue

    artifact = _Art(
        artifact_id="art_confirm",
        project_id=1,
        user_id=1,
        file_type="audio",
        source_type="call",
        status="speakers_pending",
        original_filename="call.webm",
        file_path="x.webm",
        id=42,
    )
    db = MagicMock()
    added: list[Job] = []

    def _add(obj):
        if isinstance(obj, Job):
            obj.id = 7
            added.append(obj)

    db.add.side_effect = _add
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.get.return_value = None

    with patch(
        "app.app.services.speaker_profiles.speakers_confirm_status",
        return_value={"ready": True, "pending": [], "confirmed": ["speaker_0"], "labels": ["speaker_0"]},
    ):
        with patch("app.app.services.speaker_profiles.sync_match_json_from_links"):
            with patch("app.app.services.speaker_profiles.rebuild_match_report_html"):
                with patch("app.app.services.status.set_status"):
                    settings = Settings(secret_key="x" * 16)
                    result = confirm_speakers_and_enqueue(db, settings, artifact)

    assert result["job_id"] == 7
    assert len(added) == 1
    assert added[0].phase == "knowledge"
