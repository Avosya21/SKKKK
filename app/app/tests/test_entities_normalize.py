from app.app.services.entities import (
    EntitiesPayload,
    TaskItem,
    merge_related_tasks,
    normalize_entities_payload,
)


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
