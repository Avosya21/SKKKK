"""Страница лабораторного прогона: выбор прогона, отдача файлов, отсутствие fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.app.models  # noqa: F401 — пакет app должен загрузиться до prepend tests/


def _seed_artifact(artifact_id: str = "art_lab_test") -> tuple[int, str]:
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    try:
        user = db.query(User).first()
        assert user is not None, "conftest создаёт пользователей при старте приложения"
        project = Project(name="Лаборатория", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(
            Artifact(
                artifact_id=artifact_id,
                project_id=project.id,
                user_id=user.id,
                original_filename="call.wav",
                stored_filename="call.wav",
                file_path="call.wav",
                file_type="audio",
                source_type="call",
                sha256="0" * 64,
                status="done",
            )
        )
        db.commit()
        return project.id, artifact_id
    finally:
        db.close()


def _make_run(tmp_data: Path, project_id: int, artifact_id: str):
    from app.app.config import get_settings
    from app.app.services.storage import artifact_dir
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    root = artifact_dir(get_settings(), project_id, artifact_id)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    source = root / "audio" / "input_16k.wav"
    source.write_bytes(b"RIFFfake")

    run = LabRun(root, new_run_id())
    init_run(run, artifact_id=artifact_id, project_id=project_id, source_audio=source)
    (run.stage_dir("01_audio") / "clean_16k.wav").write_bytes(b"RIFFclean")
    return run


def test_report_page_renders_run(client: TestClient, tmp_data: Path, admin_token: str):
    project_id, artifact_id = _seed_artifact()
    run = _make_run(tmp_data, project_id, artifact_id)

    response = client.get(f"/artifacts/{artifact_id}/report-lab")
    assert response.status_code == 200, response.text
    assert run.run_id in response.text
    assert "Лаборатория расшифровки" in response.text
    assert "смысловой разноски" not in response.text
    assert (run.root / "report.html").is_file()


def test_report_page_shows_sense_overlay(client: TestClient, tmp_data: Path, admin_token: str):
    project_id, artifact_id = _seed_artifact("art_lab_sense")
    run = _make_run(tmp_data, project_id, artifact_id)
    sense = run.resolve("sense")
    sense.mkdir(parents=True, exist_ok=True)
    (sense / "sense.json").write_text(
        '{"assigned_words": 2, "decisions": [{"word_from": 0, "word_to": 0, "speaker_index": 1}]}',
        encoding="utf-8",
    )
    (sense / "transcript.json").write_text(
        '{"replica_count": 1, "replicas": [{"speaker": "speaker_1", "start": 0.0, "flagged": null, '
        '"text": "привет", "words": [{"i": 0, "word": "привет", "start": 0.0, "end": 0.4, '
        '"verdict": "unassigned", "sense": true}]}]}',
        encoding="utf-8",
    )

    response = client.get(f"/artifacts/{artifact_id}/report-lab", params={"run": run.run_id})
    assert response.status_code == 200
    assert "смысловой разноски" in response.text
    assert "Cursor разнёс" in response.text
    assert "id=\"sense-transcript\"" in response.text


def test_report_page_shows_giga_stage(client: TestClient, tmp_data: Path, admin_token: str):
    project_id, artifact_id = _seed_artifact("art_lab_giga")
    run = _make_run(tmp_data, project_id, artifact_id)
    giga = run.resolve("giga")
    giga.mkdir(parents=True, exist_ok=True)
    (giga / "giga.json").write_text(
        '{"summary": {"turn_count": 3, "speaker_count": 2, "chars_plain": 42, '
        '"speakers": [{"speaker": "giga_4", "seconds": 10.0, "share": 0.8}]}, '
        '"turns": [{"i": 0, "start": 0.0, "end": 5.0, "speaker": "giga_4", '
        '"speaker_label": "Спикер №4", "text": "ширина стандартная"}]}',
        encoding="utf-8",
    )
    reconcile = run.resolve("reconcile")
    reconcile.mkdir(parents=True, exist_ok=True)
    (reconcile / "speakers.json").write_text(
        '{"span_counts": {"confirmed": 4, "conflict": 1, "closed": 2, "unverified": 3, "open": 0}, '
        '"speaker_map": {"giga_4": {"speaker_index": 1, "share": 0.9}}, '
        '"spans": [{"start": 1.0, "end": 1.5, "text": "спорное", "our_speaker_index": 0, '
        '"from_sense": true, "verdict": "conflict", '
        '"giga": {"speaker_index": 1, "giga_speaker": "giga_4", "reason": "внутри однородной"}}]}',
        encoding="utf-8",
    )
    proof = run.resolve("proof")
    proof.mkdir(parents=True, exist_ok=True)
    (proof / "proof.json").write_text(
        '{"summary": {"asked": 10, "taken_giga": 3, "kept_lab": 7}, '
        '"diffs": [{"id": "d1:1", "op": "replace", "pick": "giga", "start": 2.0, '
        '"lab": "стандарта.", "giga": "стандартная.", "left": "ширина", "right": "режем"}, '
        '{"id": "d2:2", "op": "replace", "pick": "lab", "start": 3.0, '
        '"lab": "гонять", "giga": "глинять", "left": "нет времени", "right": "между"}]}',
        encoding="utf-8",
    )
    (proof / "transcript.json").write_text(
        '{"replica_count": 1, "replicas": [{"speaker": "speaker_1", "start": 0.0, '
        '"text": "ширина стандартная.", "words": [{"i": 0, "word": "стандарта.", '
        '"final": "стандартная.", "start": 0.0, "end": 0.4, "verdict": "assigned", '
        '"proof": "giga"}]}]}',
        encoding="utf-8",
    )

    response = client.get(f"/artifacts/{artifact_id}/report-lab", params={"run": run.run_id})
    assert response.status_code == 200
    assert "GigaAM услышал то же аудио сам" in response.text
    assert "Этап 3 — сверка с GigaAM" in response.text
    assert 'id="proof-transcript"' in response.text
    # На странице только принятые правки, отклонённые в JSON не уходят.
    assert "стандартная." in response.text
    assert "глинять" not in response.text
    assert "fillTurns" in response.text
    assert 'class="turn"' in response.text
    assert 'class="when"' in response.text
    assert "Синим подчёркнуто" not in response.text


def test_report_page_without_giga_has_no_stage_three(
    client: TestClient, tmp_data: Path, admin_token: str
):
    project_id, artifact_id = _seed_artifact("art_lab_nogiga")
    run = _make_run(tmp_data, project_id, artifact_id)

    response = client.get(f"/artifacts/{artifact_id}/report-lab", params={"run": run.run_id})
    assert response.status_code == 200
    assert "Этап 3 — сверка с GigaAM" not in response.text


def test_report_page_accepts_explicit_run(client: TestClient, tmp_data: Path, admin_token: str):
    project_id, artifact_id = _seed_artifact()
    first = _make_run(tmp_data, project_id, artifact_id)
    second = _make_run(tmp_data, project_id, artifact_id)

    latest = client.get(f"/artifacts/{artifact_id}/report-lab")
    assert second.run_id in latest.text

    explicit = client.get(f"/artifacts/{artifact_id}/report-lab", params={"run": first.run_id})
    assert explicit.status_code == 200
    assert first.run_id in explicit.text


@pytest.mark.parametrize("run_id", ["", "не-прогон", "20260101T000000Z_aaaaaa"])
def test_missing_run_is_404_not_substituted(client: TestClient, tmp_data: Path, admin_token: str, run_id: str):
    project_id, artifact_id = _seed_artifact()
    _make_run(tmp_data, project_id, artifact_id)

    response = client.get(f"/artifacts/{artifact_id}/report-lab", params={"run": run_id or "x"})
    assert response.status_code == 404


def test_artifact_without_runs_is_404(client: TestClient, tmp_data: Path, admin_token: str):
    _, artifact_id = _seed_artifact("art_lab_empty")
    response = client.get(f"/artifacts/{artifact_id}/report-lab")
    assert response.status_code == 404
    assert "прогон" in response.json()["detail"]


def test_lab_file_is_served_from_run(client: TestClient, tmp_data: Path, admin_token: str):
    project_id, artifact_id = _seed_artifact()
    run = _make_run(tmp_data, project_id, artifact_id)

    response = client.get(f"/artifacts/{artifact_id}/lab/{run.run_id}/file/01_audio/clean_16k.wav")
    assert response.status_code == 200
    assert response.content == b"RIFFclean"
    assert response.headers["content-type"].startswith("audio/wav")


def test_report_page_shows_lab_enroll_cards(client: TestClient, tmp_data: Path, admin_token: str):
    project_id, artifact_id = _seed_artifact("art_lab_voice_cards")
    run = _make_run(tmp_data, project_id, artifact_id)
    voice = run.resolve("voice")
    voice.mkdir(parents=True, exist_ok=True)
    (voice / "voice.json").write_text(
        '{"clip_count": 2, "profiles_compared": 1, "cross_speaker_cosine": []}',
        encoding="utf-8",
    )
    (voice / "match.json").write_text(
        json.dumps(
            {
                "source": "lab",
                "run_id": run.run_id,
                "matches": {
                    "speaker_0": {
                        "diar_label": "speaker_0",
                        "clip_start": 1.0,
                        "clip_end": 5.0,
                        "clip_duration": 4.0,
                        "status": "new",
                        "suggested_profile_name": "Елена Соколова",
                        "suggested_score": 0.81,
                    }
                },
                "embeddings": {"speaker_0": [1.0, 0.0]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get(f"/artifacts/{artifact_id}/report-lab", params={"run": run.run_id})
    assert response.status_code == 200
    assert "Голоса лаборатории" in response.text
    assert "Запомнить" in response.text
    assert "speaker_0" in response.text
    assert "/lab/${data.run_id}/speaker-enroll" in response.text
    assert "[1.0, 0.0]" not in response.text


def test_lab_file_cannot_escape_run(client: TestClient, tmp_data: Path, admin_token: str):
    """Проверяем сервис напрямую: HTTP-клиент схлопывает ``..`` ещё до роутера."""
    project_id, artifact_id = _seed_artifact()
    run = _make_run(tmp_data, project_id, artifact_id)

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.services.artifact_lab import lab_file

    db = SessionLocal()
    try:
        artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).one()
        with pytest.raises((ValueError, FileNotFoundError)):
            lab_file(get_settings(), artifact, run.run_id, "../../audio/input_16k.wav")
    finally:
        db.close()

    escape = client.get(
        f"/artifacts/{artifact_id}/lab/{run.run_id}/file/../../../audio/input_16k.wav"
    )
    assert escape.status_code == 404
