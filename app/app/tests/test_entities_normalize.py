from app.app.services.entities import (
    EntitiesPayload,
    TaskItem,
    is_duration_estimate,
    is_informational_schedule,
    is_stage_sequence_phrase,
    merge_related_tasks,
    normalize_entities_payload,
)


def test_duration_estimate_is_not_a_deadline():
    assert is_duration_estimate("4–6 нед.")
    assert is_duration_estimate("8–12 недель")
    assert is_duration_estimate("8–12 недель после приёмки этапа 1")
    assert is_duration_estimate("на этапе MVP (4–6 недель)")
    assert not is_duration_estimate("до конца недели")
    assert not is_duration_estimate("перед этапом 2")
    assert not is_duration_estimate("к пятнице")
    assert not is_duration_estimate("за неделю, возможно меньше (до 2026-09-03)")


def test_stage_sequence_is_not_a_deadline():
    assert is_stage_sequence_phrase("перед этапом 2")
    assert is_stage_sequence_phrase("после этапа 1")
    assert is_stage_sequence_phrase("после приёмки этапа 1")
    assert is_stage_sequence_phrase("на этапе MVP")
    assert is_informational_schedule("перед этапом 2")
    assert not is_stage_sequence_phrase("до конца недели")
    assert not is_stage_sequence_phrase("к пятнице")
    assert not is_informational_schedule("к 15.03, перед этапом 2")


def test_normalize_strips_stage_duration_deadline():
    payload = EntitiesPayload(
        tasks=[
            TaskItem(
                description="Этап 1 MVP: настройка коннектора",
                assignee="Исполнитель",
                deadline="4–6 нед.",
            )
        ]
    )
    out = normalize_entities_payload(payload)
    assert out.tasks[0].deadline is None


def test_normalize_strips_stage_sequence_deadline():
    payload = EntitiesPayload(
        tasks=[
            TaskItem(
                description="Перед этапом 2 нужно сделать нормализацию номенклатуры",
                assignee="Заказчик",
                deadline="перед этапом 2",
            )
        ]
    )
    out = normalize_entities_payload(payload)
    assert out.tasks[0].deadline is None


def test_normalize_moves_deadline_from_decision_to_matching_task():
    payload = EntitiesPayload(
        decisions=[
            "Подготовить КП с верхней планкой оценки к четвергу–пятнице текущей недели",
        ],
        tasks=[
            TaskItem(
                description="Подготовить комплексное коммерческое предложение по интеграции",
                assignee="Acme",
                deadline=None,
            )
        ],
    )
    out = normalize_entities_payload(payload)
    assert out.tasks[0].deadline
    assert "четверг" in out.tasks[0].deadline.casefold() or "недел" in out.tasks[0].deadline.casefold()


def test_normalize_extracts_deadline_from_task_description():
    payload = EntitiesPayload(
        tasks=[
            TaskItem(
                description="Направить КП до конца текущей недели",
                assignee="Елена",
                deadline=None,
            )
        ]
    )
    out = normalize_entities_payload(payload)
    assert out.tasks[0].deadline
    assert "недел" in out.tasks[0].deadline.casefold()


def test_normalize_merges_duplicate_tasks_and_keeps_deadline():
    payload = EntitiesPayload(
        tasks=[
            TaskItem(description="Подготовить КП", assignee=None, deadline=None),
            TaskItem(
                description="Подготовить КП с кейсами",
                assignee="Acme",
                deadline="к четвергу–пятнице",
            ),
        ]
    )
    out = normalize_entities_payload(payload)
    assert len(out.tasks) == 1
    assert out.tasks[0].deadline == "к четвергу–пятнице"
    assert out.tasks[0].assignee == "Acme"


def test_merge_related_tasks_kp_and_cases_same_deadline():
    """Пример: подготовить КП и включить кейсы — одно обязательство."""
    tasks = [
        TaskItem(
            description="Подготовить коммерческое предложение с верхней планкой оценки без демо-стенда",
            assignee="Антон Морозов",
            deadline="четверг-пятница",
        ),
        TaskItem(
            description="Включить в КП кейсы (Альфа, Бета) для подтверждения компетенций",
            assignee="Акме",
            deadline="четверг-пятница",
        ),
    ]
    out = merge_related_tasks(tasks)
    assert len(out) == 1
    assert "коммерческ" in out[0].description.casefold()
    assert "кейс" in out[0].description.casefold()
    assert out[0].deadline == "четверг-пятница"


def test_merge_related_tasks_keeps_unrelated_same_deadline_separate():
    tasks = [
        TaskItem(description="Подготовить КП", assignee="А", deadline="к пятнице"),
        TaskItem(description="Созвониться с юристами", assignee="Б", deadline="к пятнице"),
    ]
    out = merge_related_tasks(tasks)
    assert len(out) == 2
