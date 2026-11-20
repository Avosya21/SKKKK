from unittest.mock import MagicMock

from app.app.services.status import set_status


def test_set_status_clears_stale_error_on_retry():
    artifact = MagicMock(
        status="error",
        error_message="lab_proof failed",
        artifact_id="art_x",
        original_filename="call.webm",
    )
    db = MagicMock()
    set_status(db, artifact, "asr_running")
    assert artifact.status == "asr_running"
    assert artifact.error_message is None
    db.commit.assert_called()


def test_set_status_keeps_error_text_on_failure():
    artifact = MagicMock(
        status="asr_running",
        error_message="lab_proof failed",
        artifact_id="art_x",
        original_filename="call.webm",
    )
    db = MagicMock()
    set_status(db, artifact, "error")
    assert artifact.status == "error"
    assert artifact.error_message == "lab_proof failed"


def test_set_status_keeps_explicit_message_on_done_no_speech():
    """Штатный исход «речь не обнаружена»: done + текст в error_message для UI."""
    from app.app.services.lab_errors import NO_SPEECH

    artifact = MagicMock(
        status="asr_running",
        error_message=None,
        artifact_id="art_x",
        original_filename="call.webm",
    )
    db = MagicMock()
    set_status(db, artifact, "done", error_message=NO_SPEECH, reason=NO_SPEECH)
    assert artifact.status == "done"
    assert artifact.error_message == NO_SPEECH
