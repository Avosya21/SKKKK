# Этап 0. Стенд Windows

## Цель

Убедиться, что машина способна выполнить конвейер, **до** написания кода. Дефекты стенда (нет cuDNN, нет rus Tesseract, LM Studio с JIT) проявляются позже как «необъяснимые» падения. LLM-провайдер выбирается на старте (`LLM_PROVIDER=lmstudio` | `cursor`); стенд проверяет **активный** провайдер.

## Предусловия

Ничего. Это первый этап.

## Шаги

1. **Драйвер NVIDIA.** Установить актуальный Game Ready или Studio. Проверить `nvidia-smi`: видно имя карты, 24 ГБ, версия драйвера.

2. **Проверить свободную VRAM в покое.** Бюджет lmstudio: ~19 ГБ под Gemma и 6–8 ГБ под три ASR. В режиме cursor Gemma в VRAM не нужна — достаточно ~8 ГБ под ASR.

3. **Python 3.11** (3.10 допустим), обязательно с галочкой Add to PATH.

4. **Git для Windows.**

5. **FFmpeg.** Распаковать, добавить `bin` в PATH, проверить `ffmpeg -version`.

6. **Tesseract OCR** с языковыми пакетами **rus** и **eng**. Проверить `tesseract --list-langs` — в списке должен быть `rus`. Без него скан-PDF и скриншоты дадут мусор вместо текста.

7. **LM Studio** (если `LLM_PROVIDER=lmstudio`):
   - Модель `google/gemma-4-31b-qat` загружена и отвечает.
   - Сервер на `:1234`, **JIT выключен**, CLI `lms` (`lms ps`, `lms unload --all`, `lms load ...`).

8. **Cursor API** (если `LLM_PROVIDER=cursor`):
   - `CURSOR_API_KEY` в `.env` (Dashboard → Integrations).
   - Preflight: `python -m app.app.main check` — пункт `llm` зелёный.
   - Опционально: `CURSOR_RUNTIME=local|cloud`, `CURSOR_CLOUD_REPO` для cloud.

9. **Ollama.** `ollama list` содержит `bge-m3`, эмбеддинг на CPU (`num_gpu: 0`).

10. **Свободное место.** Минимум 100 ГБ на диске C:.

11. **`.env`.** Из `.env.example`: `LLM_PROVIDER`, секреты, три хеша паролей.

## Что появляется на диске

```
C:\1C_NEW_SKKKK\data\logs\env-check.txt
C:\1C_NEW_SKKKK\.env
```

## Полезные проекты на GitHub

Этап без своего кода, поэтому это разовые инструменты для диагностики, а не зависимости приложения.

| Проект | Что даёт | Как используем |
|---|---|---|
| [XuehaiPan/nvitop](https://github.com/XuehaiPan/nvitop) | Интерактивный TUI поверх `nvidia-smi`: живой график VRAM по процессам | Вместо разового `nvidia-smi --query-gpu=...` — держать открытым во время написания и отладки этапа 3, сразу видно, кто именно держит память |
| [ollama/ollama](https://github.com/ollama/ollama) | Официальный движок Ollama, уже используется для `bge-m3` | Смотреть `docs/api.md` в репозитории для точного формата `options.num_gpu=0` в запросе эмбеддинга |
| [lmstudio-ai/lms](https://github.com/lmstudio-ai/lms) | Исходники CLI `lms`, которым управляем загрузкой/выгрузкой Gemma | Смотреть, какие коды возврата и сообщения даёт `lms unload/load`, чтобы `check_env.ps1` и позже `gpu_phase.py` парсили их надёжно, а не по догадке |

## Проверки

Запустить единый скрипт:

```powershell
powershell -ExecutionPolicy Bypass -File C:\1C_NEW_SKKKK\scripts\check_env.ps1
```

Он проверяет и пишет отчёт в `data\logs\env-check.txt`:

| Проверка | Ожидание |
|---|---|
| GPU | Карта видна, память ≥ 20 ГБ |
| GPU в покое | Занято < 4 ГБ |
| Python | 3.11 или 3.10 |
| Git, FFmpeg | Команды отвечают |
| Tesseract | Установлен, в языках есть `rus` |
| LM Studio API | `/v1/models` отвечает, в списке `gemma-4-31b-qat` |
| LM Studio chat | Реальный ответ на запрос |
| `lms` CLI | Команда доступна |
| Ollama API | `/api/tags` отвечает, есть `bge-m3` |
| bge-m3 | Размерность вектора ровно 1024 |
| bge-m3 на CPU | Прирост VRAM после эмбеддинга < 500 МБ |
| Диск | ≥ 100 ГБ свободно |
| Дерево проекта | `docs`, `app`, `data`, `.cursor` на месте |

Скрипт возвращает код 1, если провалено что-то обязательное.

### Ручная проверка выгрузки и загрузки

Ключевая механика всего проекта — снять Gemma с карты и вернуть обратно. Проверяется руками один раз:

```powershell
nvidia-smi --query-gpu=memory.used --format=csv    # запомнить: занято ~19-20 ГБ
lms unload --all
Start-Sleep -Seconds 5
nvidia-smi --query-gpu=memory.used --format=csv    # должно упасть до единиц ГБ
lms load gemma-4-31b-qat
nvidia-smi --query-gpu=memory.used --format=csv    # снова ~19-20 ГБ
```

Если после `unload` память не освобождается — этап 3 работать не будет, разбираться нужно здесь.

### Проверка автозагрузки JIT

```powershell
lms unload --all
# затем обратиться к API:
Invoke-RestMethod -Uri 'http://127.0.0.1:1234/v1/chat/completions' -Method Post -ContentType 'application/json' -Body '{"model":"gemma-4-31b-qat","messages":[{"role":"user","content":"привет"}]}'
```

Ожидание: **запрос падает с ошибкой «модель не загружена»**. Если вместо этого пришёл ответ — JIT включён, его надо выключить в настройках сервера LM Studio.

## Тесты

Автотестов на этом этапе нет — тестировать нечего, кода ещё нет. Роль тестов выполняет `scripts/check_env.ps1`, он же позже войдёт в состав диагностики этапа 8.

## Критерий готовности

- [ ] `check_env.ps1` завершается с `RESULT: PASS`
- [ ] `lms unload --all` реально освобождает VRAM, `lms load` возвращает модель
- [ ] Обращение к LM Studio при выгруженной модели даёт ошибку, а не автозагрузку
- [ ] `tesseract --list-langs` содержит `rus`
- [ ] Эмбеддинг `bge-m3` не увеличивает занятость VRAM
- [ ] `.env` создан из шаблона

## Типичные отказы

| Симптом | Причина | Что делать |
|---|---|---|
| `nvidia-smi` не найден | Драйвер не установлен или PATH | Переустановить драйвер |
| В покое занято 6+ ГБ | Браузер, игры, другие LLM-приложения | Закрыть, отключить аппаратное ускорение в браузере |
| `lms` не найден | CLI не установлен | LM Studio → Developer → Install CLI |
| После `unload` память занята | Модель загружена вторым процессом (например, ещё одна копия LM Studio или Ollama держит GPU-модель) | `nvidia-smi` покажет процесс; закрыть |
| В Tesseract нет `rus` | Установщик без языковых данных | Доустановить `rus.traineddata` в `tessdata` |
| Ollama занимает VRAM | Модель уехала на GPU | Передавать `options.num_gpu = 0` в каждом запросе |
