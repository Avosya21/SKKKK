"""Схема платформы памяти решений (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ1): таблицы, колонки, миграции."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.app.db as db_mod
import app.app.models  # noqa: F401  (регистрирует все таблицы в Base.metadata)

NEW_TABLES = {"people", "person_aliases", "project_parties", "unknown_actors", "people_log"}
NEW_COLUMNS = [("speaker_profiles", "person_id"), ("meeting_participants", "person_id"), ("artifacts", "recorded_at_precision")]


def _use(monkeypatch, path: Path):
    monkeypatch.setattr(db_mod, "engine", create_engine(f"sqlite:///{path.as_posix()}"))
    monkeypatch.setattr(db_mod.settings, "database_url", f"sqlite:///{path.as_posix()}")
    return db_mod.engine


def _tables(path: Path, *, real_only: bool = False) -> set[str]:
    """Имена таблиц; real_only — без виртуальных (векторы vec0, FTS) и их служебных таблиц: их строки в тесте не считаем."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
        virtual = {n for n, sql in rows if sql and "VIRTUAL" in sql.upper()}
        out = {n for n, _sql in rows}
        if real_only:
            out = {n for n in out if not n.startswith("sqlite_") and not any(n.startswith(v + "_") for v in virtual)} - virtual
        return out
    finally:
        con.close()


def _cols(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_new_columns_added_to_old_tables_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    for table in ("speaker_profiles", "meeting_participants", "artifacts"):
        con.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    _use(monkeypatch, path)
    db_mod.apply_schema_migrations()
    db_mod.apply_schema_migrations()  # повторный запуск безопасен
    for table, column in NEW_COLUMNS:
        assert column in _cols(path, table)


def test_create_all_makes_platform_tables_and_uniques(tmp_path, monkeypatch):
    from app.app.models.person import Person, PersonAlias, ProjectParty

    path = tmp_path / "new.db"
    engine = _use(monkeypatch, path)
    db_mod.Base.metadata.create_all(engine)
    db_mod.apply_schema_migrations()
    assert NEW_TABLES <= _tables(path)
    for table, column in NEW_COLUMNS:
        assert column in _cols(path, table)

    with Session(engine) as db:
        db.execute(db_mod.Base.metadata.tables["projects"].insert().values(id=1, name="p", created_by=1, is_active=True))
        person = Person(project_id=1, display_name="Иван Пример")
        db.add(person)
        db.commit()
        db.add(PersonAlias(project_id=1, person_id=person.id, alias="Иван", alias_key="иван"))
        db.commit()
        db.add(PersonAlias(project_id=1, person_id=person.id, alias="ИВАН", alias_key="иван"))  # тот же ключ в проекте
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(ProjectParty(project_id=1, company_name="Пример", company_key="пример", side="customer"))
        db.commit()
        db.add(ProjectParty(project_id=1, company_name="ПРИМЕР", company_key="пример", side="executor"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_migration_keeps_existing_data_on_copy_of_real_db(tmp_path, monkeypatch):
    """Прогон на копии боевой БД (если есть): строки существующих таблиц не теряются, миграция ничего не дописывает."""
    real = Path(__file__).resolve().parents[3] / "data" / "app.db"
    if not real.exists():
        pytest.skip("нет data/app.db")
    copy = tmp_path / "copy.db"
    src = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    before_con = sqlite3.connect(copy)
    before = {t: before_con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _tables(copy, real_only=True)}
    before_con.close()
    engine = _use(monkeypatch, copy)
    db_mod.Base.metadata.create_all(engine)
    db_mod.apply_schema_migrations()
    db_mod.apply_schema_migrations()
    con = sqlite3.connect(copy)
    try:
        for table, count in before.items():
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, table
        for table in NEW_TABLES:  # новые таблицы существуют (могут быть уже заполнены реестром боевой БД, но не изменились)
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == before.get(table, 0)
    finally:
        con.close()
    for table, column in NEW_COLUMNS:
        assert column in _cols(copy, table)
