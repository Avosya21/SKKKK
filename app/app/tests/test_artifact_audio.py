from __future__ import annotations

from app.app.services.artifact_audio import _fix_audio_src


def test_fix_audio_src_replaces_player_source():
    html = '<audio controls src="../work/input_16k.wav"></audio>'
    fixed = _fix_audio_src(html, "/artifacts/art_123/audio")
    assert 'src="/artifacts/art_123/audio"' in fixed
    assert "preload=" in fixed
    assert "playsinline" in fixed


def test_fix_audio_src_leaves_other_attrs():
    html = '<audio id="player" controls preload="metadata" src="old.wav"></audio>'
    fixed = _fix_audio_src(html, "/artifacts/x/audio")
    assert 'id="player"' in fixed
    assert 'src="/artifacts/x/audio"' in fixed
    assert fixed.count("preload=") == 1
    assert "playsinline" in fixed