from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.app.config import get_settings
from app.app.services.meeting_analyze import MEETING_FILE, MEETING_KIND
from app.app.services.meeting_lab import CHILD_SEP, build_meeting_lab, meeting_lab_url
from app.app.services.storage import artifact_dir


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_meeting_art(name: str = "art_meet_lab", *, role: str | None = None):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.role == role).one() if role else db.query(User).first()
        assert user is not None
        project = Project(name="Лаб созвона", created_by=user.id)
        db.add(project)
        db.flush()
        db.add(
            Artifact(
                artifact_id=name,
                project_id=project.id,
                user_id=user.id,
                original_filename="встреча-1.wav",
                stored_filename="встреча-1.wav",
                file_path="встреча-1.wav",
                file_type="audio",
                source_type="call",
                sha256="1" * 64,
                status="asr_running",
                asr_engines_json=json.dumps({"kind": MEETING_KIND, "meeting_id": 1}),
            )
        )
        db.commit()
        return project.id, name
    finally:
        db.close()


def _write_track(tmp_data: Path, project_id: int, parent_id: str, identity: str, speaker: str):
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    settings = get_settings()
    child_id = parent_id + CHILD_SEP + identity.replace(":", "-")
    root = artifact_dir(settings, project_id, child_id)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    wav = root / "audio" / "input_16k.wav"
    wav.write_bytes(b"RIFFfake")
    run = LabRun(root, new_run_id())
    init_run(run, artifact_id=child_id, project_id=project_id, source_audio=wav)
    (run.stage_dir("02_asr") / "stage.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "inputs": [{"name": "clean_16k", "path": "01_audio/clean_16k.wav"}],
                "outputs": [{"name": "words", "path": "02_asr/words.json"}],
                "metrics": {"word_count": 2},
            }
        ),
        encoding="utf-8",
    )
    (run.stage_dir("02_asr") / "words.json").write_text(
        json.dumps({"word_count": 2, "words": [{"word": "привет"}, {"word": "мир"}]}),
        encoding="utf-8",
    )
    marker = root / "lab" / "latest.json"
    marker.write_text(json.dumps({"run_id": run.run_id}), encoding="utf-8")
    return child_id, speaker, identity, run


def test_meeting_lab_snapshot_lists_tracks_in_order(tmp_data, admin_token):
    pid, parent_id = _seed_meeting_art()
    first = _write_track(tmp_data, pid, parent_id, "guest:aa", "Даша")
    second = _write_track(tmp_data, pid, parent_id, "member:1", "Хост")
    ws = artifact_dir(get_settings(), pid, parent_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MEETING_FILE).write_text(
        json.dumps(
            {
                "meeting_id": 1,
                "tracks": [
                    {"identity": first[2], "speaker": first[1], "child_id": first[0]},
                    {"identity": second[2], "speaker": second[1], "child_id": second[0]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    art = SimpleNamespace(
        project_id=pid, artifact_id=parent_id, status="asr_running", original_filename="встреча-1.wav"
    )
    snap = build_meeting_lab(get_settings(), art)
    assert [row["speaker"] for row in snap["tracks"]] == ["Даша", "Хост"]
    asr = next(s for s in snap["tracks"][0]["stages"] if s["id"] == "02_asr")
    assert asr["status"] == "ok"
    assert asr["metrics"]["word_count"] == 2
    assert "привет" in asr["preview"]
    assert snap["complete"] is False


def test_meeting_lab_page_admin_only(client, tmp_data, admin_token, manager_token):
    pid, parent_id = _seed_meeting_art("art_meet_page", role="manager")
    ws = artifact_dir(get_settings(), pid, parent_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MEETING_FILE).write_text('{"tracks":[]}', encoding="utf-8")
    url = meeting_lab_url(parent_id)
    denied = client.get(url, headers=_auth(manager_token))
    assert denied.status_code == 403
    ok = client.get(url, headers=_auth(admin_token))
    assert ok.status_code == 200
    assert "Лаборатория созвона" in ok.text
    assert "Итоговая расшифровка" in ok.text
    assert "Проверки шкалы" in ok.text
    assert "нет данных ≠ пройдено" in ok.text
    assert "Протечки" in (
        Path(__file__).resolve().parents[1] / "templates" / "meeting_lab.html"
    ).read_text(encoding="utf-8")


def test_meeting_lab_file_stays_in_child_run(client, tmp_data, admin_token):
    pid, parent_id = _seed_meeting_art("art_meet_file")
    child_id, _speaker, identity, run = _write_track(tmp_data, pid, parent_id, "guest:bb", "Гость")
    ws = artifact_dir(get_settings(), pid, parent_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MEETING_FILE).write_text(
        json.dumps({"tracks": [{"identity": identity, "speaker": "Гость", "child_id": child_id}]}),
        encoding="utf-8",
    )
    good = client.get(
        f"/artifacts/{parent_id}/meeting-lab/{child_id}/lab/{run.run_id}/file/02_asr/words.json",
        headers=_auth(admin_token),
    )
    assert good.status_code == 200
    assert good.json()["word_count"] == 2
    stolen = client.get(
        f"/artifacts/{parent_id}/meeting-lab/other__x/lab/{run.run_id}/file/02_asr/words.json",
        headers=_auth(admin_token),
    )
    assert stolen.status_code == 404


def _write_parent_proof(project_id: int, parent_id: str, payload: dict):
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    root = artifact_dir(get_settings(), project_id, parent_id)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    wav = root / "audio" / "input_16k.wav"
    wav.write_bytes(b"RIFFfake")
    run = LabRun(root, new_run_id())
    init_run(run, artifact_id=parent_id, project_id=project_id, source_audio=wav)
    proof = run.resolve("proof/transcript.json")
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (root / "lab" / "latest.json").write_text(json.dumps({"run_id": run.run_id}), encoding="utf-8")


def test_meeting_lab_final_shows_clock_and_speaker(client, tmp_data, admin_token):
    pid, parent_id = _seed_meeting_art("art_meet_clock")
    ws = artifact_dir(get_settings(), pid, parent_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MEETING_FILE).write_text('{"tracks":[]}', encoding="utf-8")
    _write_parent_proof(
        pid,
        parent_id,
        {
            "words": [{"final": "секунду", "speaker": "Иван", "start": 1.5, "end": 2.0}],
            "replicas": [
                {"speaker": "Иван", "start": 1.5, "end": 2.0, "text": "секунду"},
            ],
        },
    )
    snap = build_meeting_lab(
        get_settings(),
        SimpleNamespace(
            project_id=pid, artifact_id=parent_id, status="transcript_ready", original_filename="встреча-1.wav"
        ),
    )
    assert snap["complete"] is True
    assert snap["merge"]["replicas"][0]["clock"] == "00:02"
    assert snap["merge"]["replicas"][0]["speaker"] == "Иван"
    page = client.get(meeting_lab_url(parent_id), headers=_auth(admin_token))
    assert page.status_code == 200
    assert "00:02" in page.text
    assert "Иван" in page.text
    assert "секунду" in page.text


def test_meeting_lab_hides_rejected_final(tmp_data, admin_token):
    pid, parent_id = _seed_meeting_art("art_meet_empty")
    ws = artifact_dir(get_settings(), pid, parent_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MEETING_FILE).write_text('{"tracks":[]}', encoding="utf-8")
    _write_parent_proof(
        pid,
        parent_id,
        {"words": [{"final": "", "word": "шум", "speaker": "Иван", "start": 1.0, "end": 1.2}]},
    )
    snap = build_meeting_lab(
        get_settings(),
        SimpleNamespace(
            project_id=pid, artifact_id=parent_id, status="transcript_ready", original_filename="встреча-1.wav"
        ),
    )
    assert snap["merge"]["words"][0]["text"] == ""


def test_meeting_lab_checks_and_leaks(tmp_data, admin_token):
    from app.app.services.meeting_checks import STATUS_NO_DATA

    pid, parent_id = _seed_meeting_art("art_meet_checks")
    ws = artifact_dir(get_settings(), pid, parent_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MEETING_FILE).write_text(
        json.dumps({"tracks": [{"identity": "guest:a", "speaker": "Анна", "silent": True, "source": "microphone"}]}),
        encoding="utf-8",
    )
    _write_parent_proof(
        pid,
        parent_id,
        {
            "replicas": [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "один два три"}],
            "leaks": [
                {
                    "text": "один два три",
                    "start": 1.0,
                    "end": 2.0,
                    "from_speaker": "A",
                    "speaker": "B",
                    "by": "rank",
                }
            ],
        },
    )
    snap = build_meeting_lab(
        get_settings(),
        SimpleNamespace(
            project_id=pid, artifact_id=parent_id, status="transcript_ready", original_filename="встреча-1.wav"
        ),
    )
    assert snap["checks"]
    assert all("id" in row and "status" in row for row in snap["checks"])
    assert any(row["status"] == STATUS_NO_DATA for row in snap["checks"])
    assert snap["leaks"][0]["text"] == "один два три"
    assert snap["tracks"][0]["silent"] is True


def test_meeting_lab_screen_video_has_no_asr_stages(tmp_data, admin_token):
    pid, parent_id = _seed_meeting_art("art_meet_screen")
    ws = artifact_dir(get_settings(), pid, parent_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / MEETING_FILE).write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "identity": "member:28",
                        "speaker": "demo_user",
                        "source": "screen_video",
                        "child_id": parent_id + "__member-28-TRone",
                        "duration_sec": 40,
                        "fps": 1,
                        "src": "C:/tmp/member-28-screen_video-TRone.mp4",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    snap = build_meeting_lab(
        get_settings(),
        SimpleNamespace(
            project_id=pid, artifact_id=parent_id, status="transcript_ready", original_filename="встреча-1.wav"
        ),
    )
    row = snap["tracks"][0]
    assert row["source"] == "screen_video"
    assert row["speaker"] == "demo_user"
    assert row["stages"] == []
    assert row["duration_sec"] == 40
    html = (Path(__file__).resolve().parents[1] / "templates" / "meeting_lab.html").read_text(
        encoding="utf-8"
    )
    assert "screen_video" in html
    assert "видео, ASR пропущен" in html


def test_project_page_has_meeting_lab_button():
    html = (Path(__file__).resolve().parents[1] / "templates" / "project.html").read_text(
        encoding="utf-8"
    )
    assert "meeting-lab" in html or "report-meeting-lab" in html
    assert "лаб встречи" in html
