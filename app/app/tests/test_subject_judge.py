"""Судья предметов (docs/plans/DECISION-MEMORY-PLAN.md, Э5). Модель подменена: проверяется код вокруг неё; имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import subject_judge as sjg


def test_parse_verdict_validates_state_and_basis():
    ok = sjg.parse_verdict('{"state": "Rejected", "basis": ["A12", 15, "A12", "A99"], "summary": "  Отказались.  "}', {12, 15})
    assert ok == {"state": "rejected", "basis": [12, 15], "summary": "Отказались."}  # номера только из ленты, без повторов
    with pytest.raises(ValueError, match="не из списка"):
        sjg.parse_verdict('{"state": "победа", "basis": ["A1"]}', {1})
    with pytest.raises(ValueError, match="основани"):
        sjg.parse_verdict('{"state": "agreed", "basis": ["A99"]}', {1})  # ссылка на реплику, которой в ленте нет
    with pytest.raises(ValueError):
        sjg.parse_verdict('["не объект"]', {1})
    with pytest.raises(json.JSONDecodeError):
        sjg.parse_verdict("не json", {1})
    assert sjg.parse_verdict('```json\n{"state": "open", "basis": [3]}\n```', {3})["summary"] == ""


@pytest.fixture()
def world(db_engine, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.decision_memory import Subject
    from app.app.models.project import Project
    from app.app.models.speech_act import SpeechAct
    from app.app.models.user import User

    db = SessionLocal()
    user = User(name="judge-user", telegram_id="tg-judge", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Судья", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_judge", project_id=project.id, user_id=user.id, original_filename="чат.html", stored_filename="c", file_path="/x",
                   file_type="html", source_type="document", sha256="judge", status="done")
    db.add(art)
    db.commit()
    subject = Subject(project_id=project.id, title="Подпапки каталога по толщине")
    db.add(subject)
    db.commit()
    spec = [("customer", "Заказчик", "proposed", "А мы можем их дополнительно по толщине разбить?", "2026-09-11T12:30:00"),
            ("executor", "Исполнитель", "deferred", "мы пока прорабатываем вопрос с толщиной", "2026-09-11T13:02:00"),
            ("executor", "Исполнитель", "rejected", "Сделать папки по толщине — неправильно", "2026-09-11T13:14:00"),
            ("customer", "Заказчик", "agreed", "Давайте тогда сделаем как вы показали выше", "2026-09-11T13:53:00")]
    acts = []
    for i, (side, author, act, quote, said) in enumerate(spec, 1):
        row = SpeechAct(project_id=project.id, artifact_id=art.id, unit_ref=f"M{i}", unit_key=i, author=author, side=side, act=act, quote=quote, said_at=said,
                        subject_id=subject.id, prompt_version="v", model="m")
        db.add(row)
        acts.append(row)
    db.commit()
    yield db, get_settings(), project, subject, acts
    db.close()


def test_build_user_lists_acts_with_sides_and_ids(world):
    db, settings, project, subject, acts = world
    text = sjg.build_user(subject, acts, {})
    lines = text.splitlines()
    assert lines[0] == "Предмет: Подпапки каталога по толщине" and lines[2] == "Лента:"
    assert lines[3] == f"A{acts[0].id} 2026-09-11 12:30 [заказчик] Заказчик — предложил: «А мы можем их дополнительно по толщине разбить?»"
    assert "— отложил:" in lines[4] and "— согласился:" in lines[6] and len(lines) == 7


def test_timeline_keeps_first_two_and_last_when_too_long():
    from types import SimpleNamespace

    acts = [SimpleNamespace(id=i) for i in range(1, 30)]
    shown = sjg.timeline_acts(acts)
    assert len(shown) == sjg.MAX_LINES and [a.id for a in shown[:2]] == [1, 2] and shown[-1].id == 29
    assert sjg.timeline_acts(acts[:5]) == acts[:5]


def test_judge_applies_valid_verdict_and_can_override_fold(world):
    db, settings, project, subject, acts = world
    from app.app.services import decision_log as dl

    for a in acts:  # журнал по актам: свёртка по сторонам скажет «согласовано» (заказчик согласился)
        kind = dl.ACT_TO_EVENT.get(a.act)
        if kind:
            dl.add_event(db, subject, kind, a.said_at, actor_side=a.side, anchor=a.unit_ref, speech_act_id=a.id)
    assert dl.refresh_subject(db, subject).state == "agreed"
    asked: list[str] = []

    def ask(system, user):
        asked.append(user)
        return json.dumps({"state": "rejected", "basis": [f"A{acts[2].id}", f"A{acts[3].id}"],
                           "summary": "Заказчик согласился с отказом: папки по толщине не делаем."}, ensure_ascii=False)

    result = sjg.judge_subject(db, settings, subject, ask=ask)
    db.commit()
    assert result == {"status": "applied", "state": "rejected", "was": "agreed", "changed": True, "basis": [acts[2].id, acts[3].id]}
    assert subject.state == "rejected" and subject.state_source == "judge" and subject.state_since == "2026-09-11T13:53:00"
    assert subject.summary.startswith("Заказчик согласился с отказом") and json.loads(subject.basis_json) == [acts[2].id, acts[3].id]
    assert len(asked) == 1 and "Предмет:" in asked[0]


def test_invalid_answer_is_retried_then_state_is_kept(world):
    db, settings, project, subject, acts = world
    subject.state, subject.state_source = "agreed", "fold"
    db.commit()
    answers = iter(["не json", '{"state": "победа", "basis": ["A1"]}'])
    result = sjg.judge_subject(db, settings, subject, ask=lambda s, u: next(answers))
    assert result["status"] == "error" and subject.state == "agreed" and subject.state_source == "fold"
    flaky = iter(["не json", json.dumps({"state": "rejected", "basis": [acts[2].id]})])
    assert sjg.judge_subject(db, settings, subject, ask=lambda s, u: next(flaky))["status"] == "applied"  # со второй попытки принят


def test_human_decision_and_context_only_subjects_are_skipped(world):
    from app.app.models.decision_memory import Subject
    from app.app.models.speech_act import SpeechAct
    from app.app.services import decision_log as dl

    db, settings, project, subject, acts = world
    dl.add_event(db, subject, "agreed", "2026-09-12", precision="day", decided_by="human")
    db.commit()
    never = lambda s, u: (_ for _ in ()).throw(AssertionError("судью звать не нужно"))  # noqa: E731
    assert sjg.judge_subject(db, settings, subject, ask=never) == {"status": "skipped", "reason": "решение человека"}
    ctx = Subject(project_id=project.id, title="Только справки")
    db.add(ctx)
    db.commit()
    db.add(SpeechAct(project_id=project.id, artifact_id=acts[0].artifact_id, unit_ref="M9", unit_key=9, act="info", quote="цена 100", subject_id=ctx.id, prompt_version="v", model="m"))
    db.commit()
    assert sjg.judge_subject(db, settings, ctx, ask=never)["status"] == "skipped"


def test_judge_project_counts_and_refolds_first(world):
    from app.app.models.decision_memory import Subject

    db, settings, project, subject, acts = world
    from app.app.services import decision_log as dl

    dl.add_event(db, subject, "proposed", "2026-09-11T12:30:00", actor_side="customer")
    other = Subject(project_id=project.id, title="Без актов")
    db.add(other)
    db.commit()
    progress: list[str] = []
    counts = sjg.judge_project(db, settings, project.id, ask=lambda s, u: json.dumps({"state": "rejected", "basis": [acts[2].id], "summary": "итог"}), progress=progress.append)
    assert counts == {"subjects": 2, "applied": 1, "changed": 1, "skipped": 1, "errors": 0}
    assert progress == [f"предмет #{subject.id} «Подпапки каталога по толщине»: proposed -> rejected"]  # до судьи состояние пересчитано свёрткой
