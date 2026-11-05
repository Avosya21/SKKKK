"""Каталог файла SQLite создаётся сам: в свежем клоне data/ ещё нет."""

from __future__ import annotations

from app.app.db import _ensure_sqlite_parent


def test_creates_parent_dir(tmp_path):
    target = tmp_path / "data" / "app.db"
    _ensure_sqlite_parent(f"sqlite:///{target.as_posix()}")
    assert target.parent.is_dir()


def test_memory_and_other_databases_are_untouched():
    _ensure_sqlite_parent("sqlite://")
    _ensure_sqlite_parent("postgresql://localhost/primaryanalitics")
