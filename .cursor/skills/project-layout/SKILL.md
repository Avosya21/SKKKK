---
name: project-layout
description: Where SKKKK files live and how to add new docs, rules, skills, or app code. Use when creating files, choosing paths, or deciding app vs data vs docs.
---

# Раскладка SKKKK

Корень: `C:\1C_NEW_SKKKK`.

| Что | Куда |
|---|---|
| Единый план | `docs/PLAN.md` |
| **Карта проекта** | `docs/PROJECT-MAP.md` + `docs/project-map/manifest.yaml` |
| Зафиксированные решения | `docs/DECISIONS.md` |
| Промпты Gemma | `docs/prompts/` |
| Правила Cursor | `.cursor/rules/*.mdc` |
| Скилы Cursor | `.cursor/skills/<name>/SKILL.md` |
| Код API | `app/` |
| SQLite, логи, проекты | `data/` |
| Секреты | `.env` (не коммитить) |
| Шаблон секретов | `.env.example` |

Новый документ по продукту — в `docs/`, не в корень (кроме `README.md` и `AGENTS.md`).
Новый код — под `app/`, не рядом с `data/projects`.
Не создавать второй корень проекта.
