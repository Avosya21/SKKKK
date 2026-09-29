from app.app.services.tz import (
    _estimate_tokens,
    _humanize_artifact_refs,
    _joined_drafts,
    _pack_draft_groups,
    _pack_map_chunks,
    _parse_prompts,
    tz_paths,
)


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


def test_humanize_artifact_refs_replaces_ids_with_filenames():
    text = "Решение принято. (art_2026-08-23_022008_d78232)"
    mapping = {"art_2026-08-23_022008_d78232": "ТЗ_интеграция.docx"}
    assert _humanize_artifact_refs(text, mapping) == "Решение принято. (ТЗ_интеграция.docx)"


def test_humanize_artifact_refs_keeps_unknown_ids():
    text = "Факт. (art_2026-08-23_999999_abcdef)"
    assert _humanize_artifact_refs(text, {}) == text


def test_parse_prompts_returns_six_blocks():
    map_sys, map_user, merge_sys, merge_user, reduce_sys, reduce_user = _parse_prompts()
    assert "{chunk_text}" in map_user
    assert "{drafts}" in merge_user
    assert "{drafts}" in reduce_user
    assert "слияние" in merge_sys.lower() or "объединяешь" in merge_sys.lower()
    assert "Суть проекта" in reduce_sys


def test_pack_draft_groups_respects_budget():
    drafts = ["a" * 3500, "b" * 3500, "c" * 3500, "d" * 3500, "e" * 3500, "f" * 3500]
    groups = _pack_draft_groups(drafts, budget_tokens=4500)
    assert len(groups) >= 2
    assert all(_estimate_tokens(_joined_drafts(g)) <= 4500 for g in groups)
    flat = [d for g in groups for d in g]
    assert flat == drafts


def test_pack_draft_groups_single_oversized():
    drafts = ["x" * 50000, "y" * 100]
    groups = _pack_draft_groups(drafts, budget_tokens=4500)
    assert len(groups) == 2
    assert groups[0] == ["x" * 50000]


def test_pack_draft_groups_preserves_order():
    drafts = [f"draft-{i}" for i in range(20)]
    groups = _pack_draft_groups(drafts, budget_tokens=2000)
    flat = [d for g in groups for d in g]
    assert flat == drafts


def test_collapse_drafts_merges_when_over_budget(monkeypatch):
    from app.app.services.tz import REDUCE_INPUT_BUDGET_TOKENS, _collapse_drafts

    settings = object()
    calls = []

    def _fake_chat_nonempty(settings_, *, system, user, max_tokens):
        calls.append(len(user))
        return "## Решения и подходы\n\nслитый пункт.\n"

    monkeypatch.setattr("app.app.services.tz._chat_nonempty", _fake_chat_nonempty)
    # Черновики достаточно маленькие, чтобы несколько влезли в одну группу
    # (MERGE_INPUT_BUDGET_TOKENS=12000) — иначе группы схлопнутся в одиночные
    # (pass-through, без вызова _chat_nonempty) и тест перестанет проверять
    # реальное слияние. Их достаточно много, чтобы упаковка дала 2+ группы —
    # иначе всё уйдёт в одну группу и упрётся в "не умещается" до первого вызова.
    drafts = ["f" * 2000 for _ in range(20)]
    assert _estimate_tokens(_joined_drafts(drafts)) > REDUCE_INPUT_BUDGET_TOKENS
    out = _collapse_drafts(
        settings,
        merge_sys="merge-sys",
        merge_user_tmpl="{drafts}",
        drafts=drafts,
    )
    assert len(out) >= 1
    assert all(_estimate_tokens(_joined_drafts(o)) <= REDUCE_INPUT_BUDGET_TOKENS for o in out)
    assert calls, "merge должен вызывать _chat_nonempty"


def test_collapse_drafts_raises_when_no_progress(monkeypatch):
    from app.app.services.tz import REDUCE_INPUT_BUDGET_TOKENS, _collapse_drafts

    settings = object()

    def _fake_chat_nonempty(settings_, *, system, user, max_tokens):
        return user

    monkeypatch.setattr("app.app.services.tz._chat_nonempty", _fake_chat_nonempty)
    drafts = ["f" * 12000 for _ in range(10)]
    assert _estimate_tokens(_joined_drafts(drafts)) > REDUCE_INPUT_BUDGET_TOKENS
    import pytest

    with pytest.raises(RuntimeError, match="не уменьшает объём"):
        _collapse_drafts(
            settings,
            merge_sys="merge-sys",
            merge_user_tmpl="{drafts}",
            drafts=drafts,
        )


def test_chat_nonempty_retries_on_empty(monkeypatch):
    from app.app.services.tz import _chat_nonempty

    settings = object()
    calls = []

    def _fake_chat(settings_, *, system, user, max_tokens, temperature):
        calls.append(max_tokens)
        return "" if len(calls) == 1 else "готово"

    monkeypatch.setattr("app.app.services.tz.chat_completion_no_think", _fake_chat)
    out = _chat_nonempty(settings, system="sys", user="user", max_tokens=800)
    assert out == "готово"
    assert calls == [800, 4896]


def test_chat_nonempty_raises_if_double_empty(monkeypatch):
    from app.app.services.tz import _chat_nonempty

    settings = object()
    monkeypatch.setattr(
        "app.app.services.tz.chat_completion_no_think",
        lambda settings_, *, system, user, max_tokens, temperature: "",
    )
    import pytest

    with pytest.raises(RuntimeError, match="пустой текст дважды"):
        _chat_nonempty(settings, system="sys", user="user", max_tokens=800)


def test_collect_project_material_keeps_decision_task_only(tmp_data, db_engine, client, admin_token):
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.tz import _collect_project_material

    db = SessionLocal()
    try:
        user = db.scalar(select(User).limit(1))
        project = Project(name="TzFilter", description="", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        art = Artifact(
            artifact_id="art_tz_filter",
            project_id=project.id,
            user_id=user.id,
            original_filename="notes.docx",
            stored_filename="notes.docx",
            file_path="notes.docx",
            file_type="docx",
            source_type="document",
            status="done",
            sha256="tzfilter",
        )
        db.add(art)
        db.flush()
        rows = [
            ("decision", '{"text": "делаем обмен через OData"}'),
            ("task", '{"text": "согласовать права обмена"}'),
            ("fact", '{"text": "в базе 120 номенклатур"}'),
            ("mention", '{"text": "упомянули Bitrix"}'),
            ("participant", '{"text": "Иванов"}'),
            ("summary", '{"text": "коротко о встрече"}'),
        ]
        for etype, value in rows:
            db.add(
                Entity(
                    artifact_id=art.id,
                    entity_type=etype,
                    value_json=value,
                    confidence=0.8,
                    is_active=True,
                    needs_review=True,
                )
            )
        db.commit()
        material = _collect_project_material(db, get_settings(), project.id)
        joined = "\n".join(body for _title, body in material)
        assert "[decision]" in joined
        assert "[task]" in joined
        assert "[mention]" not in joined
        assert "[participant]" not in joined
        assert "[summary]" not in joined
        assert "[fact]" not in joined
    finally:
        db.close()


def test_full_rebuild_preflight_refuses_big_projects_fast():
    import time

    import pytest

    from app.app.services import tz

    start = time.time()
    with pytest.raises(tz.TzTooLargeError, match="rebuild_tz_fast"):
        tz.preflight_full_rebuild(67)  # проект 18: 67 черновиков не сходились
    assert time.time() - start < 2
    tz.preflight_full_rebuild(5)  # песочница проходит
    tz.preflight_full_rebuild(30)


def test_auto_rebuild_waits_for_batch_then_runs_once(client, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import tz

    calls = []
    monkeypatch.setattr(tz, "rebuild_tz_fast", lambda *a, **k: calls.append(k.get("summary")))
    db = SessionLocal()
    try:
        import uuid

        tag = uuid.uuid4().hex[:8]
        user = User(name=f"batch-{tag}", telegram_id=f"tg-{tag}", role="user", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="Batch", description="", created_by=user.id)
        db.add(project)
        db.flush()
        arts = []
        for i in range(3):
            a = Artifact(project_id=project.id, artifact_id=f"batch-{tag}-{i}", file_type="document", status="done",
                         original_filename=f"f{i}.txt", stored_filename=f"f{i}.txt", file_path=f"f{i}.txt",
                         source_type="document", sha256=f"{tag}{i:056d}", user_id=user.id)
            db.add(a)
            arts.append(a)
        db.flush()
        for a in arts:
            db.add(Job(artifact_id=a.id, kind="process_artifact", status="running" if a is arts[0] else "queued"))
        db.commit()
        from app.app.config import get_settings

        settings = get_settings()
        tz.maybe_auto_rebuild_tz(db, settings, arts[0])  # есть ожидающие -> пропуск
        assert calls == []
        for job in db.query(Job).filter(Job.artifact_id.in_([arts[0].id, arts[1].id])):
            job.status = "done"
        db.commit()
        tz.maybe_auto_rebuild_tz(db, settings, arts[2])  # партия закончилась -> одна пересборка
        assert len(calls) == 1
    finally:
        db.close()


def test_rebuild_tz_fast_empty_project_writes_stub_without_model(db_engine, monkeypatch):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import tz
    from app.app.services.vector_index import init_vector_store

    init_vector_store(db_engine)
    monkeypatch.setattr(tz, "embed_texts", lambda _s, texts: [[0.1] * 1024 for _ in texts])
    calls = []
    monkeypatch.setattr(tz, "chat_completion_no_think", lambda *a, **k: calls.append(k) or "выжимка")
    db = SessionLocal()
    try:
        user = User(name="tz-empty", telegram_id="tg-tz-empty", role="user", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="TzEmpty", description="", created_by=user.id)
        db.add(project)
        db.commit()
        settings = get_settings()

        body, stats = tz.build_tz_fast_body(db, settings, project.id)  # без материалов — нечего собирать
        assert body is None
        assert stats["artifacts_total"] == 0

        path = tz.rebuild_tz_fast(db, settings, project.id, manual=True)
        assert "Материалов для сборки пока нет" in path.read_text(encoding="utf-8")
        assert calls == []
    finally:
        db.close()
