# Установка проекта на новом ПК

## Быстрый путь

1. Скопировать папку проекта **целиком** (вместе с `tools\`, `asr\models`, `data\`, папками моделей).
2. Запустить **`INSTALL.bat`** двойным щелчком. Скрипт сам определит, чего не хватает, и доустановит.
   - `INSTALL.bat -Yes` — без вопросов;
   - `INSTALL.bat -Yes -Asr` — плюс сборка Docker-образов ASR (долго);
   - `INSTALL.bat -SkipSystem` — без winget (только окружения, `tools\`, `.env`).
3. Выполнить шаги из блока «Осталось сделать вручную» (они же в `data\logs\install-report.txt`).
4. Проверить: `powershell -ExecutionPolicy Bypass -File scripts\check_env.ps1`.
5. Запустить: `scripts\SKKKK-3-Запуск-LM-Studio.bat` (локальная модель) или `scripts\SKKKK-2-Запуск-Cursor.bat` (Cursor API).

Проект можно класть в любую папку: скрипты запуска и установки считают пути от своего расположения, а старый путь в `.env` заменяется автоматически.

## Что и в каком порядке ставится (то, что делает `INSTALL.bat`)

| # | Что | Откуда | Зачем |
|---|---|---|---|
| 0 | Драйвер NVIDIA (+CUDA) | **вручную**, nvidia.com/drivers, затем перезагрузка | GPU для ASR и локальной LLM. Скрипт только проверяет `nvidia-smi` |
| 1 | Python **3.10** | winget `Python.Python.3.10` | Основа всех окружений; сами окружения переносить нельзя |
| 2 | Git | winget `Git.Git` | нужен для `venvs\nemo-diar` (NeMo ставится с GitHub) |
| 3 | Docker Desktop | winget `Docker.DockerDesktop` | ASR-контейнеры (whisper, gigaam, tone). После установки запустить один раз, включить WSL2 |
| 4 | Ollama | winget `Ollama.Ollama` | эмбеддинги; модель `bge-m3` скачивается скриптом |
| 5 | LM Studio | winget `ElementLabs.LMStudio` | локальная LLM (модель из `LMSTUDIO_MODEL` в `.env` скачивается **вручную**) |
| 6 | LibreOffice, Tesseract, ffmpeg | уже лежат в `tools\`; если нет — копируются из системы или ставятся через winget | `.ppt`→`.pptx`, OCR (rus+eng), аудио |
| 7 | Окружения `venv\` и `venvs\*` | `scripts\setup_env.ps1` по `app\requirements.txt` и `scripts\requirements_lock\*.txt` | все Python-библиотеки проекта (в т.ч. `python-pptx`, torch с нужной CUDA) |
| 8 | `.env` | из `.env.example` | секреты и адреса сервисов |
| 9 | Docker-образы ASR | `INSTALL.bat -Asr` | сборка `asr\docker-compose.yml` |

## Вручную после скрипта

- `.env`: `SECRET_KEY`, хеши паролей (`ADMIN/MANAGER/USER_PASSWORD_HASH`), `HF_TOKEN`; `CURSOR_API_KEY`, `TELEGRAM_BOT_TOKEN` — по необходимости.
- LM Studio: скачать модель, включить локальный сервер на порту 1234.
- Публичный HTTPS (Caddy): `scripts\install_caddy.ps1`, проброс портов на роутере — только если нужен внешний доступ.
- Службы Windows: `scripts\install_services.ps1` — если нужен автозапуск.

## Если что-то не встало

- Повторный запуск `INSTALL.bat` безопасен: рабочее пропускается.
- Пересоздать окружение: `powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1 -Only <venv|имя из venvs> -Force`.
- Обновить список версий для `venvs\*` после изменения зависимостей: `venvs\<имя>\Scripts\python.exe -m pip freeze --exclude-editable > scripts\requirements_lock\<имя>.txt`.
