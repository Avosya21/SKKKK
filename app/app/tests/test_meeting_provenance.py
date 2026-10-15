import json

from app.app.config import get_settings
from app.app.models.entity import Entity
from app.app.services.entities import EntitiesPayload, TaskItem, persist_entities
from app.app.services.meeting_merge import attach_turn_ids
from app.app.services.storage import artifact_dir


def _call_art(db, source_type="call"):
    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    user = db.scalar(select(User).limit(1))
    project = Project(name="Prov", created_by=user.id)
    db.add(project)
    db.flush()
    art = Artifact(
        artifact_id="art_prov_1",
        project_id=project.id,
        user_id=user.id,
        original_filename="встреча.wav",
        stored_filename="встреча.wav",
        file_path="встреча.wav",
        file_type="audio",
        source_type=source_type,
        sha256="a" * 64,
        status="transcript_ready",
    )
    db.add(art)
    db.flush()
    return project, art


def _write_proof(settings, project_id, artifact_id, replicas, leaks=None, attach_ids=True):
    from asr.lab.paths import LabRun, new_run_id
    from asr.lab.run import init_run

    root = artifact_dir(settings, project_id, artifact_id)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    wav = root / "audio" / "input_16k.wav"
    wav.write_bytes(b"RIFFfake")
    run = LabRun(root, new_run_id())
    init_run(run, artifact_id=artifact_id, project_id=project_id, source_audio=wav)
    proof = run.resolve("proof/transcript.json")
    proof.parent.mkdir(parents=True, exist_ok=True)
    rows = attach_turn_ids(replicas, artifact_id) if attach_ids else list(replicas)
    proof.write_text(
        json.dumps({"replicas": rows, "words": [], "leaks": leaks or []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "lab" / "latest.json").write_text(json.dumps({"run_id": run.run_id}), encoding="utf-8")
    return rows


def _write_final(settings, art, text):
    from app.app.services.storage import parsed_dir

    path = parsed_dir(settings, art.project_id, art.source_type)
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{art.artifact_id}_final.txt").write_text(text, encoding="utf-8")


UPLOADED_FINAL = (
    "[00:10 - 00:20] Елена Соколова: Предлагаю длину листа вынести в реквизит строки заказа.\n"
    "[00:21 - 00:24] Павел Пилот: Да, согласен, так и делаем.\n"
    "[00:25 - 00:30] unknown: угу\n"
    "[00:31 - 00:40] Елена Соколова: Тогда я подготовлю описание к пятнице.\n"
)


def test_numbered_turn_parts_cover_whole_call(tmp_data, client, admin_token):
    """Все реплики созвона идут в LLM с номерами T<n> — без прежнего лимита в 120 реплик."""
    from app.app.db import SessionLocal
    from app.app.services.entities import _numbered_turn_parts

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db)
        db.commit()
        lines = [f"[{i // 60:02d}:{i % 60:02d} - {i // 60:02d}:{i % 60:02d}] Анна: реплика {i}" for i in range(200)]
        _write_final(settings, art, "\n".join(lines))
        parts = _numbered_turn_parts(settings, art, max_chars=2000)
        joined = "\n".join(parts)
        assert len(parts) > 1
        assert "T0 [00:00] Анна: реплика 0" in joined
        assert "T199 " in joined
    finally:
        db.close()


def test_uploaded_call_binds_speaker_time_and_decided_by(tmp_data, client, admin_token):
    """Загруженный созвон (proof без source_span_id): speaker и время из канона, speaker_source=voice, decided_by проверен."""
    from app.app.db import SessionLocal
    from app.app.services.entities import DecisionItem

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(settings, project.id, art.artifact_id, [{"speaker": "speaker_0", "start": 10.0, "end": 20.0, "text": "x"}], attach_ids=False)
        _write_final(settings, art, UPLOADED_FINAL)
        payload = EntitiesPayload(
            artifact_id=art.artifact_id,
            source_type="call",
            decisions=[
                DecisionItem.model_validate(
                    {
                        "text": "длину листа вынести в реквизит строки заказа",
                        "turns": ["T0"],
                        "decided_by": [
                            {"name": "Павел", "turn": 1},
                            {"name": "Елена Соколова", "turn": 1},  # имя не совпадает с говорящим T1 — отбросить
                            {"name": "Иван", "turn": 99},  # нет такой реплики — отбросить
                        ],
                    }
                )
            ],
            tasks=[TaskItem(description="подготовить описание к пятнице", assignee="Елена Соколова", turns=[3])],
        )
        persist_entities(db, settings, art, payload)
        db.commit()
        dec = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(dec.value_json)
        assert value["speaker"] == "Елена Соколова"
        assert value["speaker_source"] == "voice"
        assert value["start_sec"] == 10.0
        assert value["decided_by"] == [{"name": "Павел Пилот", "start_sec": 21.0}]
        assert "turns" not in value
        assert "needs_review" not in value
        task = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "task").one()
        tvalue = json.loads(task.value_json)
        assert tvalue["speaker"] == "Елена Соколова" and tvalue["start_sec"] == 31.0
    finally:
        db.close()


def test_uploaded_call_unmatched_is_unbound_not_review(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db)
        _write_final(settings, art, UPLOADED_FINAL)
        persist_entities(
            db, settings, art, EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["совсем другая тема бюджета"])
        )
        db.commit()
        value = json.loads(db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one().value_json)
        assert value["source_status"] == "unbound"
        assert "speaker" not in value and "needs_review" not in value
    finally:
        db.close()


def test_unknown_speaker_is_not_named(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db)
        _write_final(settings, art, "[00:25 - 00:30] unknown: переносим релиз на вторник\n")
        persist_entities(
            db, settings, art, EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["переносим релиз на вторник"])
        )
        db.commit()
        value = json.loads(db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one().value_json)
        assert value["speaker"] is None
        assert value["source_status"] == "speaker_unknown"
    finally:
        db.close()


def test_rebind_updates_names_in_place_after_rename(tmp_data, client, admin_token):
    """Переименование спикера: имя в сущности меняется на месте, id и связи не пересоздаются, LLM не нужен."""
    from app.app.db import SessionLocal
    from app.app.services.entities import DecisionItem, rebind_call_entities

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db)
        _write_final(settings, art, UPLOADED_FINAL)
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                decisions=[DecisionItem(text="длину листа вынести в реквизит строки заказа", decided_by=[{"name": "Павел Пилот", "turn": 1}])],
            ),
        )
        db.commit()
        ent = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        ent_id = ent.id
        _write_final(settings, art, UPLOADED_FINAL.replace("Павел Пилот", "Павел Петров") + "\n")
        assert rebind_call_entities(db, settings, art) == 1
        db.commit()
        ent = db.get(Entity, ent_id)
        value = json.loads(ent.value_json)
        assert value["decided_by"][0]["name"] == "Павел Петров"
        assert value["speaker"] == "Елена Соколова"
    finally:
        db.close()


def test_room_call_keeps_track_source(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(settings, project.id, art.artifact_id, [{"speaker": "Анна", "start": 12.0, "end": 14.0, "text": "ставим задачу X"}])
        persist_entities(db, settings, art, EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["ставим задачу X"]))
        db.commit()
        value = json.loads(db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one().value_json)
        assert value["speaker_source"] == "track"
        assert rebind_noop(db, settings, art) == 0
    finally:
        db.close()


def rebind_noop(db, settings, art):
    from app.app.services.entities import rebind_call_entities

    return rebind_call_entities(db, settings, art)


def test_call_entity_keeps_turn_source(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        replicas = _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 12.0, "end": 14.0, "text": "ставим задачу X"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", tasks=[]),
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["ставим задачу X"]),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert value["source_span_id"] == replicas[0]["source_span_id"]
        assert value["speaker"] == "Анна"
        assert value["start_sec"] == 12.0
    finally:
        db.close()


def test_unmatched_call_entity_kept_for_review(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "привет"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["выдуманный факт"]),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert value["source_span_id"] is None
        assert row.needs_review is True
    finally:
        db.close()


def test_paraphrase_task_keeps_source_span(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        replicas = _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 8.0, "end": 11.0, "text": "выгрузку надо к пятнице сделать"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                tasks=[TaskItem(description="сделать выгрузку до пятницы")],
            ),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "task").one()
        value = json.loads(row.value_json)
        assert value["source_span_id"] == replicas[0]["source_span_id"]
        assert value["speaker"] == "Анна"
    finally:
        db.close()


def test_external_call_without_span_leaves_payload(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "привет"}],
            attach_ids=False,
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                decisions=["внешний факт"],
                summary="итог внешней записи",
            ),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert "source_span_id" not in value
        assert value["text"] == "внешний факт"
        summary = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "summary").one()
        assert json.loads(summary.value_json)["text"] == "итог внешней записи"
    finally:
        db.close()


def test_legacy_call_without_proof_leaves_payload(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db)
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                decisions=["legacy решение"],
                summary="legacy выжимка",
            ),
        )
        db.commit()
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one()
        value = json.loads(row.value_json)
        assert "source_span_id" not in value
        assert value["text"] == "legacy решение"
        summary = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "summary").one()
        assert json.loads(summary.value_json)["text"] == "legacy выжимка"
    finally:
        db.close()


def test_summary_survives_unmatched_room_call(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "привет"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                summary="краткая выжимка всего созвона",
            ),
        )
        db.commit()
        from app.app.services.storage import structured_dir

        dumped = json.loads(
            (structured_dir(settings, project.id) / f"{art.artifact_id}_entities.json").read_text(encoding="utf-8")
        )
        assert dumped["summary"] == "краткая выжимка всего созвона"
        row = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "summary").one()
        assert json.loads(row.value_json)["text"] == "краткая выжимка всего созвона"
    finally:
        db.close()


def test_leaked_text_does_not_become_entity(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [{"speaker": "Анна", "start": 1.0, "end": 2.0, "text": "один два три"}],
            leaks=[{"text": "один два три", "start": 1.0, "end": 2.0, "speaker": "B", "from_speaker": "A", "by": "rank"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=["один два три"]),
        )
        db.commit()
        assert db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").count() == 0
    finally:
        db.close()


def test_leaked_text_stays_out_of_file_and_tz(tmp_data, client, admin_token):
    from app.app.db import SessionLocal
    from app.app.services.storage import structured_dir
    from app.app.services.tz import _collect_project_material

    settings = get_settings()
    db = SessionLocal()
    leak_text = "один два три"
    keep_text = "ставим задачу X"
    try:
        project, art = _call_art(db)
        _write_proof(
            settings,
            project.id,
            art.artifact_id,
            [
                {"speaker": "Анна", "start": 1.0, "end": 2.0, "text": keep_text},
                {"speaker": "Анна", "start": 3.0, "end": 4.0, "text": leak_text},
            ],
            leaks=[{"text": leak_text, "start": 3.0, "end": 4.0, "speaker": "B", "from_speaker": "A", "by": "rank"}],
        )
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(
                artifact_id=art.artifact_id,
                source_type="call",
                decisions=[keep_text, leak_text],
            ),
        )
        db.commit()
        dumped = (structured_dir(settings, project.id) / f"{art.artifact_id}_entities.json").read_text(
            encoding="utf-8"
        )
        assert keep_text in dumped
        assert leak_text not in dumped
        material = "\n".join(body for _title, body in _collect_project_material(db, settings, project.id))
        assert keep_text in material
        assert leak_text not in material
    finally:
        db.close()


def test_document_entity_still_persists_without_turn(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        _project, art = _call_art(db, source_type="document")
        persist_entities(
            db,
            settings,
            art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="document", decisions=["из документа"]),
        )
        db.commit()
        assert db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").count() == 1
    finally:
        db.close()


def test_room_call_short_replicas_do_not_attract_items(tmp_data, client, admin_token):
    """15.10.2026: «По», «Ну», «Да» «содержались» в любом пункте — все пункты созвона привязывались к ним."""
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_proof(settings, project.id, art.artifact_id, [
            {"speaker": "Олег", "start": 5.0, "end": 5.5, "text": "По"},
            {"speaker": "Олег", "start": 6.0, "end": 6.4, "text": "Ну"},
            {"speaker": "Анна", "start": 7.0, "end": 7.3, "text": "Да."},
            {"speaker": "Анна", "start": 30.0, "end": 40.0,
             "text": "Данные Волжского филиала загружаем в отдельную ветку по коду базы G03"},
        ])
        persist_entities(db, settings, art, EntitiesPayload(artifact_id=art.artifact_id, source_type="call", decisions=[
            "Данные Волжского филиала загружаются в отдельную ветку по коду базы G03"]))
        db.commit()
        value = json.loads(db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "decision").one().value_json)
        assert value["start_sec"] == 30.0 and value["speaker"] == "Анна" and value["speaker_source"] == "track"
    finally:
        db.close()


def test_match_replica_picks_best_not_first():
    from app.app.services.entities import _match_replica, _replica_at

    reps = [{"start": 1.0, "end": 2.0, "text": "По"},
            {"start": 3.0, "end": 6.0, "text": "обмен заказами идёт раз в сутки"},
            {"start": 9.0, "end": 12.0, "text": "обмен заказами идёт раз в час по расписанию"}]
    assert _match_replica("Обмен заказами раз в час по расписанию", reps)["start"] == 9.0
    assert _match_replica("Совсем другая тема про склад", reps) is None
    assert _match_replica("По", reps) is None  # одно слово ничего не доказывает
    assert _replica_at(reps, 10.5)["start"] == 9.0 and _replica_at(reps, 100.0) is None


def test_turn_hint_seconds_survive_and_guide_rebind():
    """Номера реплик от модели сохраняются как моменты (turn_hint_sec): после «Слить» номера меняются, моменты — нет."""
    from types import SimpleNamespace

    from app.app.services.entities import turn_hint_seconds, turn_ids_from_hint

    turns = [SimpleNamespace(index=1, start_sec=10.0), SimpleNamespace(index=2, start_sec=21.0), SimpleNamespace(index=3, start_sec=None)]
    assert turn_hint_seconds([2, 3, 99], turns) == [21.0]
    renumbered = [SimpleNamespace(index=7, start_sec=10.0), SimpleNamespace(index=8, start_sec=21.4)]  # после «Слить»
    assert turn_ids_from_hint([21.0], renumbered) == [8]
    assert turn_ids_from_hint([50.0, "x"], renumbered) == []


def test_uploaded_call_keeps_turn_hint(tmp_data, client, admin_token):
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_final(settings, art, UPLOADED_FINAL)
        persist_entities(db, settings, art, EntitiesPayload(artifact_id=art.artifact_id, source_type="call",
                                                            tasks=[TaskItem(description="Подготовить описание к пятнице", turns=[3])]))
        db.commit()
        value = json.loads(db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "task").one().value_json)
        assert value["turn_hint_sec"] == [31.0] and "turns" not in value and value["start_sec"] == 31.0
    finally:
        db.close()


def test_hint_on_screen_block_moves_to_speech_in_its_span():
    """Модель ссылается на блок «Речь на экране» — подсказка переносится на реплики речи из его отрезка."""
    from types import SimpleNamespace

    from app.app.services.entities import _match_turn

    turns = [
        SimpleNamespace(index=0, is_speech=True, start_sec=5.0, end_sec=8.0, text="Добрый день, начинаем"),
        SimpleNamespace(index=1, is_speech=True, start_sec=40.0, end_sec=50.0, text="код базы для госсектора добавляем отдельным полем"),
        SimpleNamespace(index=2, is_speech=False, start_sec=38.0, end_sec=55.0, text="Речь на экране: перемешанные слова"),
    ]
    hit = _match_turn("Код базы госсектора хранить в поле", turns, [2])
    assert hit is not None and hit.index == 1


def test_rebind_names_label_assignee_by_confirmed_speaker(tmp_data, client, admin_token):
    """Ответственный — метка голоса, а стенограмма уже с именами: имя берётся из подтверждённых спикеров созвона."""
    from app.app.db import SessionLocal
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.services.entities import rebind_call_entities

    settings = get_settings()
    db = SessionLocal()
    try:
        project, art = _call_art(db)
        _write_final(settings, art, UPLOADED_FINAL)
        persist_entities(
            db, settings, art,
            EntitiesPayload(artifact_id=art.artifact_id, source_type="call",
                            tasks=[TaskItem(description="подготовить описание к пятнице", assignee="speaker_3")]),
        )
        db.commit()
        ent = db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type == "task").one()
        assert json.loads(ent.value_json)["assignee"] == "speaker_3"
        profile = SpeakerProfile(display_name="Елена Соколова", project_id=project.id, embedding=bytes(1024))
        db.add(profile)
        db.flush()
        db.add(ArtifactSpeakerLink(artifact_id=art.artifact_id, project_id=project.id, diar_label="speaker_3",
                                   profile_id=profile.id, status="confirmed"))
        db.commit()
        assert rebind_call_entities(db, settings, art) == 1
        db.commit()
        assert json.loads(db.get(Entity, ent.id).value_json)["assignee"] == "Елена Соколова"
    finally:
        db.close()
