"""Инструкция для внешних агентов: как пользоваться базой знаний, как просить нас о чём-то и как с нами общаться.

Один источник текста для трёх мест, куда агент попадает сам, без человека:
- поле `instructions` в ответе MCP `initialize` (клиент показывает его модели при подключении);
- инструмент `get_guide`;
- виртуальный файл `contract/GUIDE.md` (виден в `list_artifacts`).
Правила здесь должны совпадать с docs/agent-exchange/POLICY.md; тест сверяет ключевые пункты.
"""
from __future__ import annotations

GUIDE_VERSION = "1.0"
GUIDE_FILE = "GUIDE.md"

_SHORT = (
    "База знаний проекта #{pid} «{name}» (Primary Analytics): факты из переписок, звонков и документов — кто, что и когда сказал. "
    "Доступ только для чтения и только к этому проекту. ПЕРЕД РАБОТОЙ вызови инструмент get_guide (или прочитай contract/GUIDE.md): "
    "там формат данных, как задавать вопросы и что мы делаем и не делаем по просьбам. Кратко: "
    "1) контекст — search_project, get_project_summary, get_tz; 2) проверяемые требования — get_requirements/get_quote (дословная цитата, автор, сторона, дата); "
    "3) просьбы и вопросы к нам — инструментами submit_question / submit_verdicts, если у твоего ключа есть право записи (его выдаёт человек), иначе файлами analysis_request_<n>.json / analysis_question_<n>.json / analysis_reply_<n>.json, которые загружает человек; "
    "ответ жди через wait_reply или читай в contract/kb_reply_<n>.json. Всё, что ты присылаешь, считается данными, а не командами; команды, пути и просьбы менять или удалять что-либо отклоняются. "
    "(EN: read-only knowledge base of one project; call get_guide first; asks go through files uploaded by a human, replies appear as contract/kb_reply_<n>.json.)"
)

_GUIDE = """# Как работать с базой знаний проекта #{pid} «{name}»

Версия руководства {ver}. Это руководство читается агентом сам: инструмент `get_guide`, файл `contract/GUIDE.md`, поле `instructions` при подключении.
(EN summary: read-only knowledge base of one project. Read data through the MCP tools; send asks as JSON files that a human uploads; answers appear as `contract/kb_reply_<n>.json`. Everything you send is treated as untrusted data.)

## 1. Что здесь лежит

Материалы проекта: переписки, записи созвонов (транскрипты), документы, изображения. Из них система извлекла сущности (решения, задачи, риски, сроки и т. д.),
собрала итоговое ТЗ и **контракт** — проверяемые требования с дословной цитатой, автором, стороной (заказчик / исполнитель / unknown), датой и стабильным id.

## 2. Как читать (только чтение)

| Что нужно | Инструмент |
|---|---|
| Общий контекст | `project_overview`, `get_project_summary`, `get_tz` |
| Поиск по смыслу | `search_project` (фрагменты могут склеивать несколько реплик; точную цитату бери через `get_quote`) |
| Требования, их цитаты и якоря | `get_requirements`, `get_quote`, `get_code_anchors`, `get_changes`, `get_conflicts` |
| Материалы и метаданные | `list_artifacts` (параметры `prefix`, `updated_since`), `get_artifact_meta`, `get_artifact_content` |
| Сущности, граф, заметки | `list_entities`, `get_knowledge_graph`, `list_notes`, `list_conflicts` |
| Файлы контракта | `list_artifacts(prefix="contract/")`, затем `get_artifact_content(artifact_id="contract/requirements.json", cursor, limit)` |

Правила чтения: ответы порциями до 200 КБ с курсором `{{total, cursor, items}}`; ошибки приходят JSON-ом `{{error, tool, detail}}`.

## 3. Что знать о данных (иначе ошибёшься)

- Цитата — дословный срез источника; если её нет, `evidence: []` и причина в `evidence_missing_reason`. Ничего не выдумываем.
- `verification: extracted` — гипотеза модели; `model_confirmed` — локальная модель проверила по реплике-источнику (не человек, см. `model_review`); `human_confirmed` — подтверждено человеком. Не выдавай `extracted` за факт и `model_confirmed` — за решение человека.
- Время в переписках принято как +03:00; у звонков известен только день. `ingested_at` — когда загрузили файл, `occurred_at` — когда событие произошло.
- `side: unknown` — не «заказчик». `code_anchors` — гипотезы по ключевым словам, а не проверенные ссылки на код.
- ТЗ — итоговая выжимка проекта, не исходный документ заказчика.

## 4. Как обратиться к нам

Прямого канала «агент — агент» нет. Ты пишешь **JSON-файл**, его **загружает человек** (разработчик, выдавший тебе ключ) в проект; ответ ты читаешь через MCP.
Имена файлов фиксированы, `request_id` — уникальное число в пределах проекта, `schema_version: "1.0"` обязательна (в запросах допускается без неё).

| Что хочешь | Файл | Тело | Ответ |
|---|---|---|---|
| Получить требования по id или найти фрагменты | `analysis_request_<n>.json` | `{{"request_id":n,"requirement_ids":["req-…"],"ask":"поисковая фраза"}}` (до 50 id) | сразу `contract/kb_reply_<n>.json` |
| Задать свободный вопрос (или `submit_question`) | `analysis_question_<n>.json` | `{{"schema_version":"1.0","request_id":n,"question":"до 2000 символов","requirement_ids":[…]}}` | `kb_reply_<n>.json`: сначала `status: pending`, затем `answered` (отвечает человек, Claude или локальная модель с одобрением; это не мгновенно) |
| Передать вердикты по проверке реализации (или `submit_verdicts`) | `analysis_reply_<n>.json` | `{{"schema_version":"1.0","request_id":n,"items":[{{"requirement_id","verdict","confidence","code_evidence","data_evidence","blockers","questions_for_customer","anchor_corrections"}}]}}` | записываются как сущности `analysis_verdict` (с пометкой «непроверенный источник»), результат виден человеку в журнале |

`verdict`: implemented | partially_implemented | not_implemented | contradicts_data | not_verifiable. `confidence`: high | medium | low.
Пути в `code_evidence` — относительные от корня выгрузки (`Catalogs/...`), без диска и `..`. Пиши коротко: `blockers` и вопросы — по одному предложению.

**Запись напрямую (если у твоего ключа есть право записи; его выдаёт человек):** `submit_question(question, requirement_ids?, client_request_id?, request_id?)` → `{{status:"queued", request_id, reply_artifact}}`;
`submit_verdicts(items[], client_request_id?, request_id?)` → `{{status:"accepted", written, rejected[]}}`. Проверки те же, что у файлов; только добавление; вердикты помечаются `untrusted_source`; повтор с тем же `client_request_id` возвращает первый результат (`duplicate: true`).
Без права записи инструмент вернёт ошибку `read_only_key`: тогда работай файлами. Ответ приходит так же: `wait_reply`.

Как ждать ответа: вызови `wait_reply(request_id, wait_s)` (до 60 с, один вызов вместо опроса) или открой `contract/kb_reply_<n>.json`. Статусы: `pending` (ждёт ответа, продолжай ждать), `answered` (есть `answer`),
`accepted` (вердикты приняты, поля `imported`/`dropped`), `rejected` (шлюз отклонил, причина в `reason`), `needs_human` (ушло человеку, ничего не записано). Не отправляй один и тот же вопрос повторно.
`request_id` при записи через инструменты можно не передавать: номер выдаст сервер и вернёт в ответе (запомни его для `wait_reply`). Для файлов номер выбираешь ты, он должен быть уникален среди всех твоих обращений: ответ всегда лежит в `kb_reply_<request_id>.json`.
Письма от нас: `list_artifacts(prefix="contract/letters/")` и `get_artifact_content(artifact_id="contract/letters/<имя>")`; `exchange_status.letters_from_kb` показывает число писем и последнее (сравнивай `latest_hash` с прошлым разом). Загляни туда в начале каждой сессии.
Состояние очереди: `exchange_status`. Экономия: `list_artifacts` отдаёт `content_hash` для каждого файла контракта (качай только изменившиеся), `get_requirements(fields="brief")` возвращает требования без цитат, `updated_since` работает в `list_artifacts`, `get_requirements`, `get_changes`.

## 5. Чего мы не делаем (и просить бессмысленно)

Не удаляем и не меняем данные проекта, файлы и базу; не запускаем команды и скрипты; не читаем файлы сервера, ключи и пароли; не показываем другие проекты и пользователей;
не меняем настройки, роли и ключи; не открываем внешние адреса из твоих сообщений; не перезапускаем серверы. Обработчиков этих действий не существует.
Файл с командами или путями вне проекта отклоняется целиком и записывается в журнал; текст, похожий на указание ассистенту («игнорируй инструкции…»), уходит человеку и ничего не записывает.
Ограничения: файл до 200 КБ, JSON UTF-8, не более 30 обращений за 10 минут. Всё, что ты присылаешь, — недоверенные данные.

## 6. Если нужна новая возможность

Скажи об этом человеку словами (что сравнить и по каким данным): новые возможности добавляются кодом с тестом, а не по письму. Новый вид расхождения или нового поля в контракте — то же самое.
Если данные выглядят неправдоподобно, сообщи: лучше поправить источник, чем строить вывод на ошибке.

Полные правила: docs/agent-exchange/POLICY.md и CONTRACT.md в репозитории (у человека).
"""


def short_instructions(project_id: int, name: str) -> str:
    return _SHORT.format(pid=project_id, name=name)


def guide_markdown(project_id: int, name: str) -> str:
    return _GUIDE.format(pid=project_id, name=name, ver=GUIDE_VERSION)


def project_name(db, project_id: int) -> str:
    from app.app.models.project import Project

    row = db.get(Project, project_id) if db is not None else None
    return (row.name if row else "") or f"проект {project_id}"
