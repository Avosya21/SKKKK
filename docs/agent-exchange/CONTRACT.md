# Контракт обмена v1.0: что реализовано

Основа — письмо агента анализа конфигурации (19.09.2026, «Контракт обмена: агент базы знаний ↔ агент анализа конфигурации»).
Реализация: `services/contract.py`, `contract_text.py`, `project_mcp_contract.py`, `mcp/project_protocol.py`; тесты `test_contract.py`.

## Как агент читает данные

- Через MCP проекта (`/mcp/projects/{id}`, ключ только на чтение). Файлы контракта — **виртуальные**: их не нужно загружать в проект,
  они собираются на лету из данных и видны в `list_artifacts` с префиксом `contract/` (параметр `prefix`), читаются `get_artifact_content`
  (`artifact_id="contract/requirements.json"`, параметры `cursor`, `limit`).
- Любой список отдаётся порциями до 200 КБ: `{total, cursor, items}`. Ошибка инструмента — `{"error","tool","detail"}`.
- Дополнительные инструменты: `get_requirements`, `get_changes`, `get_code_anchors`, `get_conflicts`, `get_quote`, `get_artifact_meta`, `get_tz`.
- Ответы базы знаний на запросы агента: `contract/kb_reply_<n>.json` (не `analysis_reply_<n>`: это имя занято за направлением агент → база).

## Файлы

| Файл | Содержимое | Особенности |
|---|---|---|
| `requirements.json` | требования: `id`, `statement`, `status`, `in_current_tz`, `tz_refs`, `evidence[]`, `estimate`, `code_anchors[]`, `verification`, `confidence`, `analysis` (вердикт агента) | id стабильны между пересборками (таблица `contract_items`) |
| `decisions.json` | решения: `decided_at`, `decided_by`, `side`, `evidence` | `supersedes` пока пусто |
| `conflicts.json` | расхождения с цитатой | правило `scope` («согласовано, но вне текущего ТЗ») |
| `tz.json` | ТЗ по пунктам `tz-N.M` и привязка к требованиям | это итоговая выжимка проекта, а не исходный документ ТЗ |
| `artifacts.json` | тип, `occurred_at`, `ingested_at`, участники со сторонами, внешние ссылки | тип: `call`, `chat`, `document`, `tz`, `other` |
| `glossary.json` | термины → технические имена | правила базы знаний (`origin: kb_rules`), а не слова заказчика |
| `open_questions.json` | — | пусто: в данных нет типа «вопрос» (см. `gaps`) |
| `state.json` | версия схемы, `updated_at`, `content_hash`, счётчики, `gaps` | по нему видно, что источник изменился |

## Как строятся поля (и где границы)

- **Цитата** (`evidence[].quote`) — дословный срез исходного текста (1–3 соседних предложения, до 500 символов), не пересказ. Если подходящего фрагмента нет, `evidence: []` и причина в `evidence_missing_reason`.
- **Автор и время.** В переписках время берётся из заголовков дат и маркеров `**ЧЧ:ММ Имя:**` (с точностью до минуты), пояс принят московским `+03:00` (в источнике пояса нет). У звонков — день, без минуты, и `speaker` из подписи фрагмента.
- **Сторона** (`customer`/`vendor`): по ролям участников; в переписке из двух человек — вторая сторона по первой; иначе `unknown`.
- **Оценка часов** — из реплики с цитатой или из сообщений в пределах 15 минут после неё (`basis`: `same_message` или `nearby_message`).
- **`in_current_tz`** — совпадение основ слов с пунктами ТЗ ≥ 50%.
- **`code_anchors`** — гипотезы по словам заказчика (правила `ANCHOR_RULES`); объекты не проверены по конфигурации. Агент анализа возвращает `anchor_corrections`, и они применяются к якорям с пометкой `basis: analysis_reply`.
- **`verification`**: `human_confirmed`, если сущность подтверждена человеком (правка/подтверждение сбрасывают `needs_review`); `model_confirmed` — локальная модель проверила формулировку по реплике-источнику в режиме live (подробности — в `model_review`: вердикт, причина, уверенность, версия промпта; это **не** решение человека); иначе `extracted`. Отклонённое моделью в live выключается и пропадает из контракта (`get_changes` → removed), человек может вернуть. План: `docs/plans/AUTO-REVIEW-PLAN.md`.
- **Пробелы** названы в `state.json → gaps`: критерии приёмки (`acceptance`), `side_agreed`, `supersedes`, `open_questions`, поле `business_reason`.

## Статус по приоритетам письма

| № | Пункт | Статус |
|---|---|---|
| P0-1 | `requirements.json` | сделано (203 требования на ПИЛОТЕ, у 198 есть цитата; без `acceptance`, `side_agreed`, `business_reason`) |
| P0-2 | `artifacts.json` с типом и датой события | сделано |
| P0-3 | починенный `list_artifacts` | сделано (падал на несуществующем `created_at`; теперь `ingested_at` и `occurred_at`) |
| P0-4 | `verification` вместо сплошного `needs_review` | сделано (поле); подтверждённых человеком пока 0 |
| P1-5 | `tz.json` и связь пунктов с требованиями | сделано (выжимка проекта) |
| P1-6 | `conflicts.json` | сделано для правила `scope`; `contradiction`, `stale`, `estimate`, `owner` не определяются |
| P1-7 | `decisions.json` с `supersedes` | сделано без `supersedes` |
| P1-8 | сообщения с автором и временем в `search_project` | не сделано: сообщения разобраны для контракта, но `search_project` по-прежнему отдаёт склеенные фрагменты |
| P1-9 | `get_changes(since)` | сделано |
| P2-10..12 | глоссарий, внешние ссылки, история оценок | глоссарий — правила; ссылки — в `artifacts.json`; история оценок не ведётся |

## Приёмка из письма (§9)

- [~] Пример «Покрытие»: в нашей нумерации это `req-pilot-0184`; все поля есть, кроме `acceptance`, `side_agreed`, `business_reason` (в `gaps`).
- [x] У 198 из 203 требований есть доказательство с автором, датой и стороной (у 71 сторона `unknown`, если роль автора неизвестна).
- [~] `code_anchors` есть у 148 из 203; у остальных `code_anchors: []` с причиной.
- [x] `list_artifacts` отвечает без ошибки с датой события.
- [~] Расхождение «согласовано, но вне ТЗ» присутствует (`cft-pilot-0001`, высокая важность); вторая «цитата» — то же сообщение, где подрядчик сам говорит «в текущем ТЗ фиксировали два поля».
- [x] Пересборка не меняет `id` (тест).
- [x] `state.json` с `updated_at`, счётчиками и `content_hash`.
- [x] Порции ≤ 200 КБ с курсором.

## Вопрос агента → ответ (v1.0, добавлено)

`analysis_question_<n>.json`: `{"schema_version":"1.0","request_id":<n>,"question":"текст до 2000 символов","requirement_ids":["req-..."]}`.
Ответ `contract/kb_reply_<n>.json`: `{"kind":"answer","status":"pending|answered","question_echo","answer","answered_by","answered_at","cited":[...],"sources":[{"n","artifact","text"}]}`.
Пока `status: pending`, поля `answer` нет. `request_id` должны быть уникальны в пределах проекта.

## Статус требования (изменено 19.09.2026)

Раньше `agreed` получали все «решения», найденные моделью, то есть значение читалось как «модель назвала это решением». Теперь правило `contract_text.requirement_status`:
- `agreed` только если в источнике есть реплика **заказчика** без оговорок («по дооценке», «предварительно», «возможно»…). Сейчас 6 из 203;
- иначе `proposed`; причина в новом поле `status_basis`: `decision_by_vendor_only`, `decision_side_unknown`, `customer_conditional`, `task_not_confirmed`; `rejected` (`task_cancelled`) для отменённых.
`analysis.verified_by: "machine_code"` ставится, когда по требованию есть вердикт кроме `not_verifiable`; поле `verification` не меняется.
