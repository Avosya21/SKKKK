from __future__ import annotations

from pathlib import Path


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_create_meeting_returns_public_link_once(client, manager_token):
    pid = client.post("/projects", json={"name": "Meet"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token))
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "created"
    assert body["public_token"]
    assert body["public_url"].endswith("/m/" + body["public_token"])
    listed = client.get(f"/projects/{pid}/meetings", headers=_auth(manager_token))
    assert listed.status_code == 200
    assert listed.json()[0]["public_token"] is None
    assert "public_token_hash" not in listed.json()[0]


def test_guest_public_page_has_no_project_nav(client, manager_token):
    pid = client.post("/projects", json={"name": "MeetNav"}, headers=_auth(manager_token)).json()["id"]
    token = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()["public_token"]
    page = client.get(f"/m/{token}")
    assert page.status_code == 200
    assert "Встреча" in page.text
    assert "Выдать доступ" not in page.text
    assert 'href="/projects"' not in page.text
    assert "SKKKK_MEETING_VOICE" in page.text
    assert 'id="meeting-denoise"' in page.text
    info = client.get(f"/api/meetings/public/{token}")
    assert info.status_code == 200
    assert info.json()["project_id"] is None


def test_guest_cannot_open_project(client, manager_token, user_token):
    pid = client.post("/projects", json={"name": "Secret"}, headers=_auth(manager_token)).json()["id"]
    token = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()["public_token"]
    assert client.get(f"/projects/{pid}", headers=_auth(user_token)).status_code == 404
    assert client.get(f"/api/meetings/public/{token}").status_code == 200
    assert client.get("/projects/999999/meetings", headers=_auth(user_token)).status_code == 404


def test_revoke_and_regenerate_public_token(client, manager_token):
    pid = client.post("/projects", json={"name": "Revoke"}, headers=_auth(manager_token)).json()["id"]
    first = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    mid = first["id"]
    old = first["public_token"]
    revoked = client.post(f"/projects/{pid}/meetings/{mid}/revoke-link", headers=_auth(manager_token))
    assert revoked.status_code == 200
    assert client.get(f"/m/{old}").status_code == 404
    fresh = client.post(f"/projects/{pid}/meetings/{mid}/regenerate-link", headers=_auth(manager_token))
    assert fresh.status_code == 200
    new = fresh.json()["public_token"]
    assert new != old
    assert client.get(f"/m/{new}").status_code == 200


def test_guest_join_same_id_reuses_identity(client, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.meeting import MeetingParticipant

    pid = client.post("/projects", json={"name": "GidSame"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    token = created["public_token"]
    mid = created["id"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=_auth(manager_token))
    first = client.post(
        f"/api/meetings/public/{token}/join",
        json={"display_name": "Анна", "guest_id": "same-guest-1"},
    )
    second = client.post(
        f"/api/meetings/public/{token}/join",
        json={"display_name": "Анна 2", "guest_id": "same-guest-1"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["identity"] == second.json()["identity"]
    assert second.json()["display_name"] == "Анна 2"
    db = SessionLocal()
    try:
        rows = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == mid).all()
        assert len(rows) == 1
        assert rows[0].identity == first.json()["identity"]
        assert rows[0].display_name == "Анна 2"
    finally:
        db.close()


def test_guest_join_different_ids_differ(client, manager_token):
    from app.app.db import SessionLocal
    from app.app.models.meeting import MeetingParticipant

    pid = client.post("/projects", json={"name": "GidDiff"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    token = created["public_token"]
    mid = created["id"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=_auth(manager_token))
    first = client.post(
        f"/api/meetings/public/{token}/join",
        json={"display_name": "А", "guest_id": "guest-aaa"},
    ).json()
    second = client.post(
        f"/api/meetings/public/{token}/join",
        json={"display_name": "Б", "guest_id": "guest-bbb"},
    ).json()
    assert first["identity"] != second["identity"]
    db = SessionLocal()
    try:
        assert db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == mid).count() == 2
    finally:
        db.close()


def test_guest_join_invalid_id_is_400(client, manager_token):
    pid = client.post("/projects", json={"name": "GidBad"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    token = created["public_token"]
    client.post(f"/projects/{pid}/meetings/{created['id']}/start", headers=_auth(manager_token))
    bad = client.post(
        f"/api/meetings/public/{token}/join",
        json={"display_name": "Гость", "guest_id": "a b/../"},
    )
    assert bad.status_code == 400


def test_guest_join_without_id_still_works(client, manager_token):
    pid = client.post("/projects", json={"name": "GidOld"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    token = created["public_token"]
    client.post(f"/projects/{pid}/meetings/{created['id']}/start", headers=_auth(manager_token))
    joined = client.post(f"/api/meetings/public/{token}/join", json={"display_name": "Гость"})
    assert joined.status_code == 200
    assert joined.json()["identity"].startswith("guest:")


def test_join_before_start_is_409(client, manager_token):
    pid = client.post("/projects", json={"name": "Wait"}, headers=_auth(manager_token)).json()["id"]
    token = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()["public_token"]
    mid = client.get(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()[0]["id"]
    early = client.post(f"/api/meetings/public/{token}/join", json={"display_name": "Гость"})
    assert early.status_code == 409
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=_auth(manager_token))
    joined = client.post(f"/api/meetings/public/{token}/join", json={"display_name": "Гость"})
    assert joined.status_code == 200
    payload = joined.json()
    assert payload["identity"].startswith("guest:")
    assert payload["display_name"] == "Гость"
    assert "livekit" in payload


def test_join_returns_livekit_when_configured(client, manager_token, monkeypatch):
    from app.app.config import get_settings
    from jose import jwt

    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_url", "wss://example.test/livekit")
    monkeypatch.setattr(settings, "livekit_api_key", "devkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "devsecret")

    pid = client.post("/projects", json={"name": "LkRoom"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    mid = created["id"]
    token = created["public_token"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=_auth(manager_token))
    guest = client.post(f"/api/meetings/public/{token}/join", json={"display_name": "Гость"}).json()
    lk = guest["livekit"]
    assert lk["configured"] is True
    assert lk["url"] == "wss://example.test/livekit"
    claims = jwt.decode(lk["token"], "devsecret", algorithms=["HS256"])
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canUpdateOwnMetadata"] is True
    assert "canPublishSources" not in claims["video"]

    staff = client.post(f"/projects/{pid}/meetings/{mid}/join", headers=_auth(manager_token))
    assert staff.status_code == 200
    body = staff.json()
    assert body["identity"].startswith("member:")
    assert body["livekit"]["configured"] is True


def test_user_cannot_create_meeting(client, manager_token, user_token, db_engine):
    from app.app.db import SessionLocal
    from app.app.models.user import User

    pid = client.post("/projects", json={"name": "NoUserMeet"}, headers=_auth(manager_token)).json()["id"]
    db = SessionLocal()
    try:
        uid = db.query(User).filter(User.telegram_id == "tg-user").one().id
    finally:
        db.close()
    client.post(
        f"/projects/{pid}/members",
        json={"user_id": uid, "role": "user"},
        headers=_auth(manager_token),
    )
    r = client.post(f"/projects/{pid}/meetings", headers=_auth(user_token))
    assert r.status_code == 403


def test_delete_project_removes_meetings(client, admin_token, manager_token):
    from app.app.config import get_settings
    from app.app.services import meeting_recordings as rec

    pid = client.post("/projects", json={"name": "WipeMeet"}, headers=_auth(manager_token)).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=_auth(manager_token)).json()
    token = created["public_token"]
    dest = rec.room_dir(get_settings().data_dir, created["room_name"])
    dest.mkdir(parents=True)
    (dest / rec.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    assert client.get(f"/m/{token}").status_code == 200
    assert client.delete(f"/projects/{pid}", headers=_auth(admin_token)).status_code == 204
    assert client.get(f"/m/{token}").status_code == 404
    assert dest.exists() is False


def test_meetings_templates_keep_guest_isolated():
    templates = Path(__file__).resolve().parents[1] / "templates"
    project = (templates / "project.html").read_text(encoding="utf-8")
    guest = (templates / "meeting_public.html").read_text(encoding="utf-8")
    assert 'id="meetings-panel"' in project
    assert "Создать встречу" in project
    assert "Встреча" in guest
    assert "Выдать доступ" not in guest
    assert 'href="/projects"' not in guest
    assert "meeting_room.js?v=voice8" in guest
    assert "meeting-room" in guest
    assert 'id="guest-mic"' in guest
    assert 'id="guest-cam"' in guest
    assert 'id="guest-cam-preview"' in guest
    assert 'id="guest-mic-meter"' in guest
    assert 'id="guest-devices-check"' in guest
    assert 'id="meeting-tiles"' in guest
    assert 'id="meeting-stage"' in guest
    assert 'id="meeting-dock"' in guest
    assert 'id="meeting-leave"' in guest
    assert 'id="meeting-fs"' in guest
    staff_room = (templates / "meeting_room.html").read_text(encoding="utf-8")
    assert "meeting_room.js?v=voice8" in staff_room
    assert 'id="meeting-tiles"' in staff_room
    assert 'id="meeting-dock"' in staff_room
    assert 'id="meeting-roster"' in guest
    assert 'id="meeting-copy"' in guest
    assert 'id="meeting-layout"' in guest
    assert 'id="meeting-more"' in guest
    assert 'id="meeting-more-tray"' in guest
    assert 'id="meeting-roster-backdrop"' in guest
    assert "meeting-lobby-lead" in guest
    assert 'id="meeting-end"' in staff_room
    assert 'id="meeting-end"' not in guest
    assert 'id="meeting-more"' in staff_room
    assert 'id="meeting-roster-backdrop"' in staff_room
    assert 'id="meeting-denoise"' in guest
    assert 'id="meeting-denoise"' in staff_room
    assert 'id="meeting-sound"' in guest
    assert 'id="meeting-sound"' in staff_room
    assert 'id="meeting-speaker-btn"' in guest
    assert 'id="meeting-speaker-btn"' in staff_room
    assert 'id="meeting-speaker-menu"' in guest
    assert 'id="meeting-speaker-menu"' in staff_room
    assert "meeting_room.js?v=voice8" in guest
    assert "meeting_room.js?v=voice8" in staff_room
    assert "SKKKK_MEETING_VOICE" in guest
    assert "SKKKK_MEETING_VOICE" in staff_room
    assert "skkkk-guest-" in guest
    assert "guest_id" in guest
    assert "skkkk-meeting-invite-" in project
    css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
    assert ".meeting-stage.is-immersive" in css
    assert ".meeting-roster" in css
    assert ".meeting-more-tray" in css
    assert "meeting-roster-backdrop" in css
    assert "100dvh" in css
    assert "safe-area-inset-bottom" in css
    assert "flex-wrap: nowrap" in css
    assert ".meeting-speaker-menu" in css
    assert ".meeting-chip svg" in css


def test_meeting_room_js_waits_for_ice_tcp():
    js = (Path(__file__).resolve().parents[1] / "static" / "meeting_room.js").read_text(
        encoding="utf-8"
    )
    assert "ICE_TIMEOUT_MS = 45000" in js
    assert "timeoutMs = 15000" not in js
    assert "50020" not in js
    assert "this._joined" in js
    assert "KIND_AUDIO_IN" in js
    assert "startPreview" in js
    assert "stopPreview" in js
    assert "audioDeviceId" in js
    assert "openPreviewTracks" in js
    assert "_renderTiles" in js
    assert "meeting-tile" in js
    assert "leaveRoom" in js
    assert "toggleStageFullscreen" in js
    assert "aria-pressed" in js
    assert "is-off" in js
    assert "deviceId: { ideal:" in js
    assert "deviceId: { exact:" not in js
    assert "METER_HOLD_MS" in js
    assert "noiseSuppression: false" in js
    assert "canShareScreen" in js
    assert "getDisplayMedia" in js
    assert "SCREEN_HINT" in js
    assert "insufficient permissions" in js
    assert "person === this.room.localParticipant && track.kind === \"audio\"" in js
    assert "echoCancellation: true" in js
    assert "IMMERSIVE_CLASS" in js
    assert "enterImmersive" in js
    assert "webkitRequestFullscreen" in js
    assert "webkit-playsinline" in js
    assert "requestFullscreen()" not in js or "enterNativeFullscreen" in js
    assert "copyInvite" in js
    assert "toggleLayout" in js
    assert "LAYOUT_SPEAKER" in js
    assert "setSinkId" in js
    assert "watchEnded" in js
    assert "endForAll" in js
    assert "toggleMore" in js
    assert "toggleSpeakerMenu" in js
    assert "meeting-chip-icon" in js
    assert "isMobileLayout" in js
    assert "MOBILE_MQ" in js
    assert "DOCK_MORE_MQ" in js
    assert "skkkk-meeting-invite-" in js
    assert "canUseDfn3" in js
    assert "canUseLoudness" in js
    assert "toggleDenoise" in js
    assert "toggleSound" in js
    assert "_soundOn" in js
    assert "node.muted = !this._soundOn" in js
    assert "DENOISE_TOPIC" in js
    assert "publishData" in js
    assert "isOwnIdentity" in js
    assert "pub.trackName" in js
    assert "src.connect(this._playComp)" not in js
    assert "dfn3: raw.dfn3 === true" in js
    assert "DFN3_MODULE" in js
    assert "noiseSuppression: !dfn3On" in js
    assert "/static/vendor/dfn3/index.esm.js" in js
    assert "CLEANER_ID" in js
    assert "hasCleaner" in js
    assert "isCleaner" in js
    assert "01_audio" not in js
    assert "function phoneGridCols" in js
    assert "_layout: LAYOUT_GRID" in js
    assert "applyPhoneGrid" in js
    assert "is-screen-lead" in js
    assert "is-dense" in js
    assert "if (isMobileLayout()) {\n        this._layout = LAYOUT_SPEAKER" not in js


def test_phone_meeting_grid_fits_all_tiles():
    css = (Path(__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
    js = (Path(__file__).resolve().parents[1] / "static" / "meeting_room.js").read_text(
        encoding="utf-8"
    )
    phone = css.split("@media (max-width: 575.98px)")[1]
    assert "repeat(var(--meeting-cols" in phone
    assert "grid-auto-rows: minmax(0, 1fr)" in phone
    assert ".meeting-tiles.is-screen-lead .meeting-tile-screen" in phone
    assert "display: none" not in phone.split(".meeting-tiles.has-focus .meeting-tile")[1][:180]
    assert "if (n <= 2) return landscape ? 2 : 1" in js
    assert "if (n <= 6) return landscape ? 3 : 2" in js


def test_lab_audio_stage_stays_raw_copy():
    src = Path(__file__).resolve().parents[3] / "asr" / "lab" / "stages" / "s01_audio.py"
    text = src.read_text(encoding="utf-8")
    assert '"mode": "copy_16k"' in text
    assert "DeepFilterNet" in text
    assert "без денойза" in text


def test_voice_cleaner_is_server_side_not_asr():
    src = Path(__file__).resolve().parents[3] / "tools" / "livekit" / "voice_cleaner.py"
    text = src.read_text(encoding="utf-8")
    assert 'CLEANER_ID = "skkkk-dfn"' in text
    assert "CUDA_VISIBLE_DEVICES" in text
    assert "01_audio" not in text


def test_known_speaker_match_is_confirmed():
    from app.app.services.meeting_analyze import known_speaker_match

    payload = known_speaker_match("Анна")
    assert payload["matches"]["speaker_0"]["status"] == "confirmed"
    assert payload["matches"]["speaker_0"]["profile_name"] == "Анна"
    assert payload["display_names"]["speaker_0"] == "Анна"


def test_analyzable_tracks_skip_screen_and_cleaner(tmp_path: Path):
    from app.app.services import meeting_recordings as rec
    from app.app.services.meeting_analyze import analyzable_tracks

    room = "skkkk-analyze01"
    manifest = rec.empty_manifest(room)
    rec.upsert_track(
        manifest,
        identity="member:1",
        rel_path="tracks/member-1.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=1000.0,
    )
    rec.upsert_track(
        manifest,
        identity="member:1",
        rel_path="tracks/member-1-screen.wav",
        source=rec.SOURCE_SCREEN,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=1000.0,
    )
    rec.upsert_track(
        manifest,
        identity=rec.CLEANER_ID,
        rel_path="tracks/skkkk-dfn.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=1000.0,
    )
    dest = rec.room_dir(tmp_path, room) / rec.MANIFEST_NAME
    rec.write_manifest(dest, manifest)
    writer = rec.PcmWavWriter(rec.room_dir(tmp_path, room) / "tracks" / "member-1.wav")
    writer.write(b"\x00\x00" * 480)
    writer.close()
    (rec.room_dir(tmp_path, room) / "tracks" / "member-1-screen.wav").write_bytes(b"xxxx")
    rows = analyzable_tracks(tmp_path, room)
    assert [row["identity"] for row in rows] == ["member:1"]


def test_project_meetings_table_has_analyze_column():
    html = (Path(__file__).resolve().parents[1] / "templates" / "project.html").read_text(
        encoding="utf-8"
    )
    assert "<th>Проанализировать</th>" in html
    assert "meeting-analyze" in html
    assert "MEETINGS_COLSPAN = 5" in html
    assert "meetingAction(btn, 'analyze'" in html
    assert "MEETING_RETRY" in html


def _seed_room_track(room_name: str, identity: str) -> None:
    from app.app.config import get_settings
    from app.app.services import meeting_recordings as rec

    settings = get_settings()
    dest = rec.track_path(settings.data_dir, room_name, identity)
    writer = rec.PcmWavWriter(dest)
    writer.write(b"\x00\x00" * 480)
    writer.close()
    room = rec.room_dir(settings.data_dir, room_name)
    manifest_path = room / rec.MANIFEST_NAME
    manifest = rec.read_manifest(manifest_path) or rec.empty_manifest(room_name)
    rel = dest.relative_to(room).as_posix()
    rec.upsert_track(
        manifest,
        identity=identity,
        rel_path=rel,
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=__import__("time").time(),
    )
    rec.set_track_frames(manifest, identity, rec.SOURCE_MICROPHONE, 480, rec.SAMPLE_RATE)
    rec.write_manifest(manifest_path, manifest)


def test_analyze_ended_meeting_enqueues_speaker_jobs(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.services.storage import artifact_dir

    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "Analyze"}, headers=headers).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=headers).json()
    mid = created["id"]
    token = created["public_token"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=headers)
    guest = client.post(
        f"/api/meetings/public/{token}/join", json={"display_name": "Гость Анна"}
    ).json()
    member = client.post(f"/projects/{pid}/meetings/{mid}/join", headers=headers).json()
    client.post(f"/projects/{pid}/meetings/{mid}/end", headers=headers)
    room = client.get(f"/projects/{pid}/meetings/{mid}", headers=headers).json()["room_name"]
    listed = client.get(f"/projects/{pid}/meetings", headers=headers).json()[0]
    assert listed["can_analyze"] is False
    assert listed["analyze_status"] == "none"
    guest_id = guest["identity"]
    member_id = member["identity"]
    member_name = member["display_name"]
    _seed_room_track(room, guest_id)
    _seed_room_track(room, member_id)
    ready = client.get(f"/projects/{pid}/meetings/{mid}", headers=headers).json()
    assert ready["can_analyze"] is True
    assert ready["analyze_status"] == "ready"
    assert ready["track_count"] == 2
    analyzed = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert analyzed.status_code == 200
    body = analyzed.json()
    assert body["analyze_status"] == "queued"
    assert body["can_analyze"] is False
    assert len(body["analyze_jobs"]) == 2
    identities = {row["identity"] for row in body["analyze_jobs"]}
    assert identities == {guest_id, member_id}
    parent_ids = {row["artifact_id"] for row in body["analyze_jobs"]}
    assert len(parent_ids) == 1
    settings = get_settings()
    assert settings.data_dir == tmp_data
    parent = next(iter(parent_ids))
    meeting_json = artifact_dir(settings, pid, parent) / "meeting.json"
    payload = __import__("json").loads(meeting_json.read_text(encoding="utf-8"))
    assert payload["meeting_id"] == mid
    assert payload["room_name"] == room
    assert payload["meeting_started_epoch"]
    assert payload["events"] == f"meetings/{room}/events.jsonl"
    by_id = {row["identity"]: row for row in payload["tracks"]}
    assert by_id[guest_id]["speaker"] == "Гость Анна"
    assert by_id[member_id]["speaker"] == member_name
    assert by_id[guest_id]["source"] == "microphone"
    assert by_id[guest_id]["frames"] == 480
    spans = [
        float(row.get("offset_sec") or 0) + float(row.get("duration_sec") or 0)
        for row in payload["tracks"]
        if row.get("source") != "screen"
    ]
    assert payload["duration_sec"] == max(spans)
    again = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert again.status_code == 409


def test_analyze_done_meeting_can_retry(client, manager_token, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.job import Job

    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "RetryDone"}, headers=headers).json()["id"]
    mid, _room = _ended_meeting_with_track(client, headers, pid, "Гость")
    first = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert first.status_code == 200
    job_row = first.json()["analyze_jobs"][0]
    db = SessionLocal()
    try:
        job = db.get(Job, job_row["job_id"])
        assert job is not None
        job.status = "done"
        db.commit()
    finally:
        db.close()
    listed = client.get(f"/projects/{pid}/meetings/{mid}", headers=headers).json()
    assert listed["analyze_status"] == "done"
    assert listed["can_analyze"] is True
    retry = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert retry.status_code == 200
    body = retry.json()
    assert body["analyze_status"] == "queued"
    assert body["analyze_jobs"][0]["artifact_id"] == job_row["artifact_id"]
    assert body["analyze_jobs"][0]["job_id"] != job_row["job_id"]


def test_analyze_without_tracks_or_before_end_is_409(client, manager_token):
    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "NoTracks"}, headers=headers).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=headers).json()
    mid = created["id"]
    early = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert early.status_code == 409
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=headers)
    client.post(f"/projects/{pid}/meetings/{mid}/end", headers=headers)
    empty = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert empty.status_code == 409


def test_user_cannot_analyze_meeting(client, manager_token, user_token):
    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "StaffOnly"}, headers=headers).json()["id"]
    mid = client.post(f"/projects/{pid}/meetings", headers=headers).json()["id"]
    denied = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=_auth(user_token))
    assert denied.status_code in (403, 404)


def test_project_meetings_table_has_delete_icon():
    html = (Path(__file__).resolve().parents[1] / "templates" / "project.html").read_text(
        encoding="utf-8"
    )
    assert 'id="deleteMeetingModal"' in html
    assert "meeting-delete" in html
    assert "method: 'DELETE'" in html
    assert "/meetings/${target.id}" in html


def test_user_cannot_delete_meeting(client, manager_token, user_token):
    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "NoDel"}, headers=headers).json()["id"]
    mid = client.post(f"/projects/{pid}/meetings", headers=headers).json()["id"]
    denied = client.delete(f"/projects/{pid}/meetings/{mid}", headers=_auth(user_token))
    assert denied.status_code in (403, 404)


def test_delete_meeting_unknown_404(client, manager_token):
    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "MissMeet"}, headers=headers).json()["id"]
    missing = client.delete(f"/projects/{pid}/meetings/999999", headers=headers)
    assert missing.status_code == 404


def test_delete_meeting_purges_recordings_artifact_and_queue(client, manager_token, tmp_data):
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.models.meeting import Meeting, MeetingParticipant
    from app.app.services import meeting_recordings as rec
    from app.app.services.storage import artifact_dir

    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "WipeCall"}, headers=headers).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=headers).json()
    mid = created["id"]
    token = created["public_token"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=headers)
    guest = client.post(
        f"/api/meetings/public/{token}/join", json={"display_name": "Гость"}
    ).json()
    client.post(f"/projects/{pid}/meetings/{mid}/join", headers=headers)
    client.post(f"/projects/{pid}/meetings/{mid}/end", headers=headers)
    room = client.get(f"/projects/{pid}/meetings/{mid}", headers=headers).json()["room_name"]
    _seed_room_track(room, guest["identity"])
    analyzed = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert analyzed.status_code == 200
    job_row = analyzed.json()["analyze_jobs"][0]
    parent_id = job_row["artifact_id"]
    job_id = job_row["job_id"]
    settings = get_settings()
    child_dir = artifact_dir(settings, pid, job_row["child_id"])
    child_dir.mkdir(parents=True, exist_ok=True)
    (child_dir / "keep.txt").write_text("track", encoding="utf-8")
    room_path = rec.room_dir(settings.data_dir, room)
    assert room_path.is_dir()

    gone = client.delete(f"/projects/{pid}/meetings/{mid}", headers=headers)
    assert gone.status_code == 204
    listed = client.get(f"/projects/{pid}/meetings", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []
    assert client.get(f"/m/{token}").status_code == 404
    arts = client.get(f"/projects/{pid}/artifacts", headers=headers).json()
    assert arts == []
    assert room_path.exists() is False
    assert child_dir.exists() is False

    db = SessionLocal()
    try:
        assert db.get(Meeting, mid) is None
        leftover_people = list(
            db.scalars(select(MeetingParticipant).where(MeetingParticipant.meeting_id == mid))
        )
        assert leftover_people == []
        assert db.get(Job, job_id) is None
        leftover_art = db.scalar(select(Artifact).where(Artifact.artifact_id == parent_id))
        assert leftover_art is None
    finally:
        db.close()


def _ended_meeting_with_track(client, headers, pid: int, guest_name: str) -> tuple[int, str]:
    created = client.post(f"/projects/{pid}/meetings", headers=headers).json()
    mid = created["id"]
    token = created["public_token"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=headers)
    guest = client.post(
        f"/api/meetings/public/{token}/join", json={"display_name": guest_name}
    ).json()
    client.post(f"/projects/{pid}/meetings/{mid}/end", headers=headers)
    room = client.get(f"/projects/{pid}/meetings/{mid}", headers=headers).json()["room_name"]
    _seed_room_track(room, guest["identity"])
    return mid, room


def test_analyze_status_missing_job_is_retryable(client, manager_token, tmp_data):
    from sqlalchemy import delete as sql_delete

    from app.app.db import SessionLocal
    from app.app.models.job import Job

    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "StaleQ"}, headers=headers).json()["id"]
    mid, _room = _ended_meeting_with_track(client, headers, pid, "Гость")
    analyzed = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert analyzed.status_code == 200
    job_id = analyzed.json()["analyze_jobs"][0]["job_id"]
    db = SessionLocal()
    try:
        db.execute(sql_delete(Job).where(Job.id == job_id))
        db.commit()
    finally:
        db.close()
    listed = client.get(f"/projects/{pid}/meetings/{mid}", headers=headers).json()
    assert listed["analyze_status"] == "error"
    assert listed["can_analyze"] is True
    retry = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers)
    assert retry.status_code == 200
    assert retry.json()["analyze_status"] == "queued"
    new_id = retry.json()["analyze_jobs"][0]["job_id"]
    db = SessionLocal()
    try:
        job = db.get(Job, new_id)
        assert job is not None
        assert job.status == "queued"
    finally:
        db.close()


def test_delete_other_meeting_keeps_foreign_job(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.job import Job
    from app.app.services.meeting_analyze import write_analyze

    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "KeepJob"}, headers=headers).json()["id"]
    keep_id, _keep_room = _ended_meeting_with_track(client, headers, pid, "Аня")
    drop_id, drop_room = _ended_meeting_with_track(client, headers, pid, "Боря")
    analyzed = client.post(f"/projects/{pid}/meetings/{keep_id}/analyze", headers=headers)
    assert analyzed.status_code == 200
    job_row = analyzed.json()["analyze_jobs"][0]
    write_analyze(
        get_settings().data_dir,
        drop_room,
        {"status": "queued", "kind": "meeting", "jobs": [job_row]},
    )
    gone = client.delete(f"/projects/{pid}/meetings/{drop_id}", headers=headers)
    assert gone.status_code == 204
    db = SessionLocal()
    try:
        assert db.get(Job, job_row["job_id"]) is not None
    finally:
        db.close()
    keep = client.get(f"/projects/{pid}/meetings/{keep_id}", headers=headers).json()
    assert keep["analyze_jobs"][0]["job_id"] == job_row["job_id"]


def test_track_offset_counts_from_first_sample():
    """Ноль дорожки — первый сэмпл WAV, а не момент входа в комнату."""
    from datetime import datetime, timezone

    from app.app.services.meeting_analyze import track_offset_sec

    started = datetime(2026, 9, 13, 9, 0, 0, tzinfo=timezone.utc)
    assert track_offset_sec(started, started.timestamp() + 15.81) == 15.81
    assert track_offset_sec(started.replace(tzinfo=None), started.timestamp() + 4.5) == 4.5
    assert track_offset_sec(started, started.timestamp() - 2.0) == 0.0
    assert track_offset_sec(started, None) is None
    assert track_offset_sec(None, started.timestamp()) is None
