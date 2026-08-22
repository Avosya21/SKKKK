# Промпт: извлечение сущностей

```
Извлеки из текста структурированную информацию. Ответь ТОЛЬКО валидным JSON без markdown-ограждений.
Если поле неизвестно — пустой массив или пустой объект, не выдумывай.
Поля:
{
  "artifact_id": "<подставить>",
  "source_type": "call|document|note|screenshot",
  "timestamp": "<ISO8601 или null>",
  "summary": "3-5 предложений",
  "decisions": ["..."],
  "tasks": [{"description": "", "assignee": null, "deadline": null, "status": "unknown"}],
  "facts": {},
  "mentions": [{"name": "", "relation": "mentions|refines|contradicts|supports"}],
  "participants": [{"name": "", "role": null}]
}
```
