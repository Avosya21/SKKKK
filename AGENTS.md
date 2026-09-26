# SKKKK — агент проекта

Читай и следуй:

1. `docs/PLAN.md` — единый пошаговый план
2. `docs/PROJECT-MAP.md` — карта проекта: каталоги, пайплайны, API, зависимости (обновлять вместе с кодом)
3. `docs/stages/STAGE-NN-*.md` — детальное исполнение текущего этапа: шаги, проверки, тесты, критерий готовности
4. `docs/DECISIONS.md` — что нельзя менять без пользователя
5. `docs/TESTING.md` — как и что тестируем
6. Правила в `.cursor/rules/`, скилы в `.cursor/skills/`

## Как ориентироваться в проекте (индекс)

Сначала индекс, потом `grep`. Индекс знает код Python/JS/шаблонов/CSS, документы, решения, связи и историю; каждый ответ — с адресом `путь:строка`. План и устройство: `docs/plans/PROJECT-INDEX-PLAN.md`.

```powershell
venv\Scripts\python.exe scripts\search_project_map.py "почему исчерпывается пул соединений"   # вопрос по смыслу
venv\Scripts\python.exe scripts\search_project_map.py --symbol log_activity                     # где определено
venv\Scripts\python.exe scripts\search_project_map.py --callers log_activity                    # кто вызывает
venv\Scripts\python.exe scripts\search_project_map.py --route "GET /projects/1/artifacts"       # роут: права, вызовы, таблицы
venv\Scripts\python.exe scripts\search_project_map.py --table meeting_participants              # столбцы, кто пишет/читает
venv\Scripts\python.exe scripts\search_project_map.py --env WEB_PORT                            # переменная или константа
venv\Scripts\python.exe scripts\search_project_map.py --ui "выбрать микрофон"                   # кнопка -> шаблон -> JS -> API
venv\Scripts\python.exe scripts\search_project_map.py --tests rate_limit                        # какие тесты и как запустить
venv\Scripts\python.exe scripts\search_project_map.py --changed                                 # незакоммиченное по функциям
venv\Scripts\python.exe scripts\search_project_map.py --invariants                              # что нельзя ломать
```

- В запрос клади имена из репозитория (функция, файл, URL, `id` элемента), а не бытовое описание. Слабый ответ помечен «уверенности нет».
- Найденное проверяй в самом файле: индекс — указатель, а не источник истины.
- После правок кода: `venv\Scripts\python.exe scripts\index_project_map.py` (только изменённые файлы, секунды). Хуки git и `restart.ps1` делают это сами.
- Диагностика: `scripts\index_doctor.py`; расхождения карты с кодом: `scripts\check_map_drift.py` (`--fix` пересоздаёт реестр модулей).
- Для ИИ-агентов тот же поиск доступен как MCP-сервер `skkkk-index` (`.mcp.json`, `tools/projindex_mcp.py`); для людей — страница `/admin/map`.


**LightRAG (граф кнопок/API/БД):** Docker `tools/lightrag`, запросы — `py scripts/lightrag_query.py "вопрос"`. См. `tools/lightrag/README.md`.

Работай только внутри `C:\1C_NEW_SKKKK`.

## Список «Исправлено» (кнопка в шапке списка проектов)

После правки, которая меняет функционал или качество сервиса для пользователя, добавь запись в `app/app/changelog.json` (`{"date": "ГГГГ-ММ-ДД", "text": "..."}`, дата — сегодняшняя) и покажи формулировку пользователю в ответе. Рефакторинг, тесты, документацию, индекс, внутренние скрипты — не вносить.

Формулировка — одна короткая фраза для человека, без имён файлов, функций и моделей-внутрянки:
- «Добавлена возможность …» — новая функция (что теперь можно сделать и в какой ситуации);
- «Исправлена проблема с …» — ошибка, которую видел пользователь;
- «Повышена точность/скорость …» — улучшение качества; допустимо коротко: «Диаризация до 8 спикеров. Повышена точность и скорость диаризации.»

Реализация идёт по этапам: стенд → каркас → очередь → **двойной слух ASR (Parakeet + GigaAM) + Sortformer + LLM** → документы → сущности/индекс → граф → ТЗ/чат → укрепление → службы.

Следующий этап не начинается, пока не выполнен «Критерий готовности» текущего. Не предлагай вернуть тройной ASR (Whisper + T-one + merge окон).
