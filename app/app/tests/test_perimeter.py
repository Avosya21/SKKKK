from __future__ import annotations


def test_anonymous_html_shell_redirects_to_login(anon_client):
    r = anon_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("location") == "/login"


def test_login_page_is_public(anon_client):
    r = anon_client.get("/login")
    assert r.status_code == 200


def test_anonymous_audio_is_401(anon_client, tmp_data, db_engine):
    r = anon_client.get("/artifacts/art_missing/audio")
    assert r.status_code == 401


def test_anonymous_report_redirects_to_login(anon_client):
    r = anon_client.get("/artifacts/art_missing/report", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("location") == "/login"


def test_anonymous_speaker_enroll_is_401(anon_client):
    r = anon_client.post(
        "/api/artifacts/art_missing/speaker-enroll",
        json={"diar_label": "speaker_0", "display_name": "X"},
    )
    assert r.status_code == 401


def test_anonymous_speakers_confirm_is_401(anon_client):
    r = anon_client.post("/api/artifacts/art_missing/speakers-confirm")
    assert r.status_code == 401


def test_download_and_report_both_need_session(anon_client):
    assert anon_client.get("/artifacts/art_x/download").status_code == 401
    r = anon_client.get("/artifacts/art_x/report", follow_redirects=False)
    assert r.status_code == 302
