"""Регрессии на счётчик автопересборки ТЗ и на порог выдачи поиска."""

from contextlib import contextmanager

from app.app.config import Settings, get_settings


@contextmanager
def _noop_task_tracker(*_args, **_kwargs):
    yield None


def _patch_tz_task_guards(monkeypatch):
    monkeypatch.setattr(
        "app.app.services.project_tasks.get_running_task",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "app.app.services.project_tasks.track_project_task",
        _noop_task_tracker,
    )


def test_search_threshold_is_lower_than_graph_threshold():
    """Порог графа отсекал всю выдачу: реальные оценки bge-m3 около 0.4–0.6."""
    settings = get_settings()
    assert settings.search_min_score < settings.similarity_threshold
    assert 0.0 < settings.search_min_score <= 0.6


def test_call_summary_runs_after_index_for_audio(monkeypatch):
    """Выжимка созвона — после индекса, не до спикеров."""
    import inspect

    from app.app.services import pipeline_audio, pipeline_knowledge

    audio_src = inspect.getsource(pipeline_audio.process_audio_artifact)
    assert "summarize_call.py" not in audio_src

    knowledge_src = inspect.getsource(pipeline_knowledge.run_knowledge_pipeline)
    index_at = knowledge_src.index("index_artifact(")
    summary_at = knowledge_src.index("run_call_summary(")
    graph_at = knowledge_src.index('set_status(db, artifact, "graph_seeded")')
    assert index_at < summary_at < graph_at


def test_auto_rebuild_counts_the_current_artifact(monkeypatch):
    """`maybe_auto_rebuild_tz` вызывается после перевода артефакта в done."""
    import inspect

    from app.app.services import pipeline_knowledge

    source = inspect.getsource(pipeline_knowledge.run_knowledge_pipeline)
    done_at = source.index('set_status(db, artifact, "done")')
    rebuild_at = source.index("maybe_auto_rebuild_tz(")
    assert done_at < rebuild_at


def test_rebuild_fires_on_every_nth_done_artifact(monkeypatch):
    from app.app.services import tz as tz_mod

    calls: list[int] = []

    class _Db:
        def __init__(self, count: int) -> None:
            self._count = count

        def scalar(self, _stmt):
            return self._count

    monkeypatch.setattr(tz_mod, "rebuild_tz", lambda db, s, pid, **kw: calls.append(pid))
    _patch_tz_task_guards(monkeypatch)
    settings = Settings(secret_key="x" * 16, tz_rebuild_every_n=3)
    artifact = type("ArtifactStub", (), {"file_type": "audio", "project_id": 7, "artifact_id": "art_audio"})()

    for count in (1, 2, 3, 4, 5, 6):
        tz_mod.maybe_auto_rebuild_tz(_Db(count), settings, artifact)

    assert calls == [7, 7]


def test_rebuild_always_runs_for_documents(monkeypatch):
    from app.app.services import tz as tz_mod

    calls: list[tuple[int, str]] = []

    class _Db:
        def scalar(self, _stmt):
            return 99

    monkeypatch.setattr(
        tz_mod,
        "rebuild_tz",
        lambda db, s, pid, **kw: calls.append((pid, kw.get("summary", ""))),
    )
    _patch_tz_task_guards(monkeypatch)
    settings = Settings(secret_key="x" * 16, tz_rebuild_every_n=3)
    artifact = type("ArtifactStub", (), {"file_type": "docx", "project_id": 11, "artifact_id": "art_doc"})()

    tz_mod.maybe_auto_rebuild_tz(_Db(), settings, artifact)

    assert calls == [(11, "auto after document art_doc")]
