from app.app.services.tz import _pack_map_chunks, tz_paths


def test_tz_paths_created(tmp_path, monkeypatch):
    from app.app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()
    current, versions = tz_paths(settings, 1)
    assert current.parent.name == "project_state"
    assert versions.is_dir()


def test_pack_map_chunks_respects_limit():
    parts = [
        ("a", "x" * 3000),
        ("b", "y" * 3000),
        ("c", "z" * 1000),
    ]
    chunks = _pack_map_chunks(parts, max_chars=6000)
    assert len(chunks) >= 2
    assert all(len(c) <= 6500 for c in chunks)


def test_pack_map_chunks_splits_huge_section():
    parts = [("big", "word " * 5000)]
    chunks = _pack_map_chunks(parts, max_chars=6000)
    assert len(chunks) >= 2
