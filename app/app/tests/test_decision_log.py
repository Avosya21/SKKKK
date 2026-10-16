"""Журнал событий и свёртка состояния (docs/plans/DECISION-MEMORY-PLAN.md, Э4). Чистый код; все имена вымышленные."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.app.services import decision_log as dl


def ev(i, kind, at, side=None, *, by="rule", precision="minute"):
    return SimpleNamespace(id=i, kind=kind, occurred_at=at, actor_side=side, decided_by=by, precision=precision, is_active=True)


def state(*events, as_of=None):
    return dl.fold(list(events), as_of=as_of).state


def test_no_events_is_open():
    res = dl.fold([])
    assert res.state == "open" and res.since is None and res.trail == [] and res.flags == []


def test_agreement_needs_customer_or_other_side():
    assert state(ev(1, "proposed", "2026-01-01T10:00", "executor"), ev(2, "agreed", "2026-01-01T10:05", "customer")) == "agreed"
    assert state(ev(1, "proposed", "2026-01-01T10:00", "customer"), ev(2, "agreed", "2026-01-01T10:05", "executor")) == "agreed"  # заказчик попросил — исполнитель согласился
    own = dl.fold([ev(1, "proposed", "2026-01-01T10:00", "executor"), ev(2, "agreed", "2026-01-01T10:05", "executor")])
    assert own.state == "proposed" and "своей же стороны" in own.trail[-1]["note"]  # согласие своей стороны предложение не закрывает
    assert state(ev(1, "agreed", "2026-01-01T10:00", "customer")) == "agreed"  # согласие без записанного предложения
    assert state(ev(1, "agreed", "2026-01-01T10:00", None)) == "open"  # сторона неизвестна — не засчитывается


def test_rejection_by_other_side_vs_own_side_withdrawal():
    assert state(ev(1, "proposed", "2026-01-01T10:00", "executor"), ev(2, "rejected", "2026-01-01T10:05", "customer")) == "rejected"
    own = dl.fold([ev(1, "proposed", "2026-01-01T10:00", "customer"), ev(2, "rejected", "2026-01-01T10:05", "customer")])
    assert own.state == "withdrawn" and "своей же стороны" in own.trail[-1]["note"]


def test_full_story_reject_repropose_agree_implement_reopen():
    events = [
        ev(1, "proposed", "2026-01-01T10:00", "executor"), ev(2, "rejected", "2026-01-02T10:00", "customer"),
        ev(3, "proposed", "2026-01-03T10:00", "executor"), ev(4, "agreed", "2026-01-03T10:30", "customer"),
        ev(5, "implemented", "2026-01-10T10:00", "executor"),
    ]
    res = dl.fold(events)
    assert res.state == "implemented" and res.basis_event_id == 5 and res.since == "2026-01-10T10:00"
    assert [t["to"] for t in res.trail] == ["proposed", "rejected", "proposed", "agreed", "implemented"]
    assert dl.fold(events + [ev(6, "reopened", "2026-01-12T09:00")]).state == "proposed"
    assert dl.fold(events + [ev(6, "superseded", "2026-01-12T09:00")]).state == "superseded"
    assert dl.fold(events + [ev(6, "withdrawn", "2026-01-12T09:00")]).state == "withdrawn"


def test_state_as_of_date_includes_whole_day_and_ignores_later_events():
    events = [ev(1, "proposed", "2026-01-01T10:00", "executor"), ev(2, "agreed", "2026-01-05T23:30", "customer"), ev(3, "rejected", "2026-01-07T10:00", "customer")]
    assert state(*events, as_of="2026-01-04") == "proposed"
    assert state(*events, as_of="2026-01-05") == "agreed"  # весь день 5-го включён, даже вечернее событие
    assert state(*events, as_of="2026-01-07") == "rejected" and state(*events) == "rejected"
    assert state(*events, as_of="2025-12-31") == "open"
    assert state(*events, as_of="2026-01-05T10:00") == "proposed"  # точный момент: вечернее событие ещё не наступило


def test_events_are_ordered_by_time_not_by_arrival():
    late_first = [ev(2, "agreed", "2026-01-03T10:00", "customer"), ev(1, "proposed", "2026-01-01T10:00", "executor")]
    assert state(*late_first) == "agreed"
    reversed_ = [ev(1, "agreed", "2026-01-01T10:00", "customer"), ev(2, "rejected", "2026-01-03T10:00", "executor")]
    assert state(*reversed_) == "rejected"


def test_human_event_beats_later_model_events():
    events = [ev(1, "proposed", "2026-01-01T10:00", "executor"), ev(2, "agreed", "2026-01-02T10:00", "customer", by="human"),
              ev(3, "rejected", "2026-01-05T10:00", "customer"), ev(4, "withdrawn", "2026-01-06T10:00", "executor")]
    res = dl.fold(events)
    assert res.state == "agreed" and res.source == "human" and res.basis_event_id == 2
    later_human = dl.fold(events + [ev(5, "rejected", "2026-01-09T10:00", None, by="human")])
    assert later_human.state == "rejected" and later_human.basis_event_id == 5  # человек сам может перерешить
    assert dl.fold(events[:1] + events[2:3]).state == "rejected"  # без решения человека модельные события действуют


def test_same_day_events_without_time_are_flagged_ambiguous():
    day = [ev(1, "proposed", "2026-01-01", "executor", precision="day"), ev(2, "rejected", "2026-01-01", "customer", precision="day")]
    assert "ambiguous_order" in dl.fold(day).flags
    same_kind = [ev(1, "proposed", "2026-01-01", "executor", precision="day"), ev(2, "proposed", "2026-01-01", "executor", precision="day")]
    assert dl.fold(same_kind).flags == []
    timed = [ev(1, "proposed", "2026-01-01T10:00", "executor"), ev(2, "rejected", "2026-01-01T11:00", "customer")]
    assert dl.fold(timed).flags == []


def test_inactive_events_are_ignored():
    off = ev(2, "rejected", "2026-01-02T10:00", "customer")
    off.is_active = False
    assert state(ev(1, "proposed", "2026-01-01T10:00", "executor"), off) == "proposed"


def test_helpers():
    assert dl.normalize_side("customer") == "customer" and dl.normalize_side("unknown") is None and dl.normalize_side(None) is None
    assert dl.ACT_TO_EVENT["done"] == "implemented" and "asked" not in dl.ACT_TO_EVENT
    assert dl.occurred_for_act(SimpleNamespace(said_at="2026-01-01T10:00:00"), "2026-01-01") == ("2026-01-01T10:00:00", "minute")
    assert dl.occurred_for_act(SimpleNamespace(said_at=None), "2026-01-01") == ("2026-01-01", "day")
    assert dl.occurred_for_act(SimpleNamespace(said_at="2026-02-03"), "2026-10-08") == ("2026-02-03", "day")  # дата документа без времени


def test_documented_rules():
    """Пункт документа: «в документе»; принимает его заказчик или другая сторона, своя сторона — нет; согласованное не понижает."""
    assert state(ev(1, "documented", "2026-01-01", "executor", precision="day")) == "documented"
    assert state(ev(1, "documented", "2026-01-01", "executor", precision="day"), ev(2, "agreed", "2026-02-01T10:00", "customer")) == "agreed"
    own = dl.fold([ev(1, "documented", "2026-01-01", "executor", precision="day"), ev(2, "agreed", "2026-02-01T10:00", "executor")])
    assert own.state == "documented" and "своей же стороны" in own.trail[-1]["note"]
    assert state(ev(1, "documented", "2026-01-01", None, precision="day"), ev(2, "rejected", "2026-02-01T10:00", "customer")) == "rejected"
    assert state(ev(1, "agreed", "2026-01-01T10:00", "customer"), ev(2, "documented", "2026-02-01", "executor", precision="day")) == "agreed"
    assert state(ev(1, "proposed", "2026-01-01T10:00", "customer"), ev(2, "rejected", "2026-01-02T10:00", "executor"),
                 ev(3, "documented", "2026-03-01", "executor", precision="day")) == "documented"  # новый документ снова фиксирует


def ev_by(i, kind, at, side, *, person=None, label=None):
    return SimpleNamespace(id=i, kind=kind, occurred_at=at, actor_side=side, actor_person_id=person, actor_label=label, decided_by="rule",
                           precision="minute", is_active=True)


def test_internal_project_counts_colleague_agreement_not_authors_own():
    """Внутренний проект (все — исполнитель): согласие коллеги закрывает предложение, «согласие» самого автора — нет."""
    offer = ev_by(1, "proposed", "2026-01-01T10:00", "executor", person=7, label="Аналитик А")
    colleague = ev_by(2, "agreed", "2026-01-01T10:05", "executor", person=8, label="Аналитик Б")
    author = ev_by(2, "agreed", "2026-01-01T10:05", "executor", person=7, label="Аналитик А")
    assert dl.fold([offer, colleague], internal=True).state == "agreed"
    own = dl.fold([offer, author], internal=True)
    assert own.state == "proposed" and "автора предложения" in own.trail[-1]["note"]
    # без человека реестра сравнивается подпись автора
    by_label = ev_by(1, "documented", "2026-01-01T10:00", "executor", label="Аналитик А")
    assert dl.fold([by_label, ev_by(2, "agreed", "2026-01-02T10:00", "executor", label="Аналитик Б")], internal=True).state == "agreed"
    assert dl.fold([by_label, ev_by(2, "agreed", "2026-01-02T10:00", "executor", label=" аналитик  а ")], internal=True).state == "documented"
    assert dl.fold([offer, ev_by(2, "agreed", "2026-01-01T10:05", "executor")], internal=True).state == "proposed"  # автор согласия неизвестен
    done = ev_by(3, "implemented", "2026-01-01T10:10", "executor", person=7)
    assert dl.fold([offer, done, ev_by(4, "agreed", "2026-01-01T10:15", "executor", person=8)], internal=True).state == "implemented"  # «ок» после «сделал»
    # проект с заказчиком — как раньше: коллега своей стороны предложение не закрывает
    assert dl.fold([offer, colleague]).state == "proposed"
    assert dl.fold([offer, ev_by(2, "agreed", "2026-01-01T10:05", "customer", person=9)]).state == "agreed"


# ---------------------------------------------------------------- база: события и пересборка из актов

@pytest.fixture()
def world(db_engine, tmp_data):
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.decision_memory import Subject
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    user = User(name="log-user", telegram_id="tg-log", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Журнал", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_log", project_id=project.id, user_id=user.id, original_filename="чат.html", stored_filename="c", file_path="/x",
                   file_type="html", source_type="document", sha256="log", status="done")
    db.add(art)
    db.commit()
    subject = Subject(project_id=project.id, title="Приложение для менеджеров")
    other = Subject(project_id=project.id, title="Пометка старой номенклатуры")
    db.add_all([subject, other])
    db.commit()
    yield db, project, art, subject, other
    db.close()


def test_add_event_validates_and_refresh_subject_writes_state(world):
    db, project, art, subject, _other = world
    with pytest.raises(ValueError):
        dl.add_event(db, subject, "придумано", "2026-01-01T10:00")
    with pytest.raises(ValueError):
        dl.add_event(db, subject, "proposed", "2026-01-01T10:00", decided_by="кто-то")
    dl.add_event(db, subject, "proposed", "2026-01-01T10:00", actor_side="executor", artifact_id=art.id, anchor="M1", quote="давайте сделаем")
    dl.add_event(db, subject, "agreed", "2026-01-02T10:00", actor_side="customer", anchor="M2", quote="согласен")
    res = dl.refresh_subject(db, subject)
    db.commit()
    assert res.state == "agreed" and subject.state == "agreed" and subject.state_since == "2026-01-02T10:00" and subject.state_source == "fold"
    assert dl.state_at(db, subject.id, "2026-01-01").state == "proposed"


def test_refresh_subject_reads_internal_project_flag(world):
    db, project, _art, subject, _other = world
    dl.add_event(db, subject, "proposed", "2026-01-01T10:00", actor_side="executor", actor_label="Аналитик А")
    dl.add_event(db, subject, "agreed", "2026-01-02T10:00", actor_side="executor", actor_label="Аналитик Б")
    assert dl.refresh_subject(db, subject).state == "proposed"  # проект с заказчиком: своя сторона не закрывает
    project.set_settings({**project.settings(), "internal_project": True})
    db.commit()
    assert dl.refresh_subject(db, subject).state == "agreed" and dl.state_at(db, subject.id, "2026-01-02").state == "agreed"
    assert dl.state_at(db, subject.id, "2026-01-01").state == "proposed"


def test_rebuild_events_from_acts_skips_context_acts_and_keeps_human_events(world):
    from app.app.models.decision_memory import DecisionEvent
    from app.app.models.speech_act import SpeechAct

    db, project, art, subject, other = world
    mk = lambda ref, act, said, side: SpeechAct(project_id=project.id, artifact_id=art.id, unit_ref=ref, unit_key=int(ref[1:]), act=act, quote="цитата " + ref,  # noqa: E731
                                                  author="А", side=side, said_at=said, prompt_version="v", model="m")
    acts = [mk("M1", "proposed", "2026-01-01T10:00:00", "executor"), mk("M2", "asked", "2026-01-01T10:01:00", "customer"),
            mk("M3", "agreed", "2026-01-01T10:05:00", "customer"), mk("M4", "done", None, "executor"), mk("M5", "info", "2026-01-02T10:00:00", None)]
    db.add_all(acts)
    db.commit()
    human = dl.add_event(db, other, "agreed", "2026-01-09", precision="day", decided_by="human", note="владелец")
    db.commit()
    ids = {a.unit_ref: a.id for a in acts}
    result = dl.rebuild_events_from_acts(db, project.id, {ids["M1"]: subject.id, ids["M2"]: subject.id, ids["M3"]: subject.id, ids["M4"]: subject.id},
                                         day_of_artifact={art.id: "2026-01-05"})
    db.commit()
    assert result == {"events": 3, "skipped": 2}  # вопрос и факт событиями не становятся
    events = db.query(DecisionEvent).filter(DecisionEvent.subject_id == subject.id).order_by(DecisionEvent.occurred_at).all()
    assert [(e.kind, e.anchor, e.precision) for e in events] == [("proposed", "M1", "minute"), ("agreed", "M3", "minute"), ("implemented", "M4", "day")]
    assert events[2].occurred_at == "2026-01-05" and events[0].speech_act_id == ids["M1"]  # у акта без времени — день материала
    db.refresh(subject)
    assert subject.state == "implemented"
    again = dl.rebuild_events_from_acts(db, project.id, {ids["M1"]: subject.id}, day_of_artifact={})
    db.commit()
    assert again["events"] == 1 and db.query(DecisionEvent).filter(DecisionEvent.subject_id == subject.id).count() == 1  # события актов заменены
    assert db.get(DecisionEvent, human.id) is not None and db.get(type(other), other.id).state == "agreed"  # событие человека осталось
