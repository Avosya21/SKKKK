# Установка проекта на новом ПК

## Важно: clone с GitHub ≠ готовый стенд

В git **нет** секретов, `venv`, весов моделей (`asr/models`, `data/models`), части бинарников в `tools\` и рабочих данных. После `git clone` обязательно:

1. `INSTALL.bat` (окружения, системные программы, `.env` из примера);
2. скачать модели ASR/LLM или скопировать их с рабочего ПК;
3. заполнить `.env` и `Login_up.txt`.

Надёжный путь для полной копии стенда — перенести папку проекта **целиком** с машины, где уже всё стоит, затем `INSTALL.bat` / `setup_env.ps1 -Force` (окружения Python после смены пути всегда пересоздаются).

**Не копируйте** чужой `venv\` / `venvs\` «как есть»: они привязаны к абсолютному пути Python и после переноса дают `ModuleNotFoundError: encodings` / `Unable to create process`. Всегда пересоздавайте через `scripts\setup_env.ps1`.

## Быстрый путь

1. Скопировать папку проекта **целиком** (вместе с `tools\`, `asr\models`, `data\`, папками моделей) **или** clone + докачать модели.
2. Запустить **`INSTALL.bat`** двойным щелчком. Скрипт сам определит, чего не хватает, и доустановит.
   - `INSTALL.bat -Yes` — без вопросов;
   - `INSTALL.bat -Yes -Asr` — плюс сборка Docker-образов ASR (долго);
   - `INSTALL.bat -SkipSystem` — без winget (только окружения, `tools\`, `.env`).
3. Выполнить шаги из блока «Осталось сделать вручную» (они же в `data\logs\install-report.txt`).
4. Проверить: `powershell -ExecutionPolicy Bypass -File scripts\check_env.ps1`.
5. Перед порталом при `LLM_PROVIDER=lmstudio`: `run_server.ps1` (сервер `:1234`). Опционально автозапуск при входе в Windows: `scripts\install_lmstudio_autostart.ps1`.
6. Запустить: `scripts\PrimaryAnalitics-3-Запуск-LM-Studio.bat` (локальная модель) или `scripts\PrimaryAnalitics-2-Запуск-Cursor.bat` (Cursor API).

Проект можно класть в любую папку: скрипты запуска и установки считают пути от своего расположения, а старый путь в `.env` заменяется автоматически (`-OldRoot`, по умолчанию прежний путь разработки).

## Что поднять до старта портала

| Компонент | Кто поднимает | Нужно до `main`? |
|---|---|---|
| Сервер **LM Studio** `:1234` | Вручную / `run_server.ps1` / задача `install_lmstudio_autostart.ps1` | **Да**, если `LLM_PROVIDER=lmstudio` (иначе preflight `fail`) |
| Модель в LM Studio | Портал (`llm_keeper`) по запросу; опционально сразу — `run_server.ps1` без `-SkipLoad` | Нет |
| **Ollama** `:11434` + `bge-m3` | Обычно приложение Ollama; портал может дозапустить перед индексом | Желательно до старта (preflight проверяет) |
| **Docker Desktop** + GigaAM | Портал перед аудио (`docker_runtime`) | Желательно; один раз принять лицензию/WSL2 |
| Caddy, LiveKit, запись | `main` (`edge_runtime`) | Нет |

Закрытие окна программы ≠ выход: если иконка в трее и сервис жив — портал обычно продолжает работать. Полный Quit Docker/Ollama/LM Studio нужные части отключит.

## Что и в каком порядке ставится (то, что делает `INSTALL.bat`)

| # | Что | Откуда | Зачем |
|---|---|---|---|
| 0 | Драйвер NVIDIA (+CUDA) | **вручную**, nvidia.com/drivers, затем перезагрузка | GPU для ASR и локальной LLM. Скрипт только проверяет `nvidia-smi` |
| 1 | Python **3.10** | `tools\Python310` (если есть) или winget `Python.Python.3.10` | Основа всех окружений; сами окружения переносить нельзя |
| 2 | Git | winget `Git.Git` | нужен для `venvs\nemo-diar` (NeMo ставится с GitHub) |
| 3 | Docker Desktop | winget `Docker.DockerDesktop` | ASR-контейнеры (GigaAM и др.). После установки запустить один раз, включить WSL2 |
| 4 | Ollama | winget `Ollama.Ollama` | эмбеддинги; модель `bge-m3` скачивается скриптом |
| 5 | LM Studio | winget `ElementLabs.LMStudio` | локальная LLM (модель из `LMSTUDIO_MODEL` в `.env` скачивается **вручную**) |
| 6 | LibreOffice, Tesseract, ffmpeg | уже лежат в `tools\`; если нет — копируются из системы или ставятся через winget | `.ppt`→`.pptx`, OCR (rus+eng), аудио |
| 7 | Окружения `venv\` и `venvs\*` | `scripts\setup_env.ps1` по `app\requirements.txt` и `scripts\requirements_lock\*.txt` | все Python-библиотеки проекта (в т.ч. `python-pptx`, torch с нужной CUDA) |
| 8 | `.env` | из `.env.example` | секреты и адреса сервисов |
| 9 | Docker-образы ASR | `INSTALL.bat -Asr` | сборка `asr\docker-compose.yml` |
| 10 | Git-хуки | `scripts\install_hooks.ps1` (сам вызывается из `INSTALL.bat` и `restart.ps1`) | индекс проекта после коммита и **проверка карты проекта перед коммитом**: коммит без актуальной `docs\PROJECT-MAP.md` не проходит |

## Вручную после скрипта

- `.env`: `SECRET_KEY`, хеши паролей (`ADMIN/MANAGER/USER_PASSWORD_HASH`), `HF_TOKEN`; `CURSOR_API_KEY`, `TELEGRAM_BOT_TOKEN` — по необходимости.
- LM Studio: скачать модель; перед порталом — `run_server.ps1` или Start Server на порту 1234.
- Автозапуск сервера LM Studio (по желанию): `powershell -ExecutionPolicy Bypass -File scripts\install_lmstudio_autostart.ps1`.
- Публичный HTTPS (Caddy): `scripts\install_caddy.ps1`, проброс портов на роутере — только если нужен внешний доступ.
- Службы Windows портала: `scripts\install_services.ps1` — если нужен автозапуск API/воркера.

## Если что-то не встало

- Повторный запуск `INSTALL.bat` безопасен: рабочее пропускается.
- Пересоздать окружение: `powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1 -Only <venv|имя из venvs> -Force`.
- Ошибка `No module named 'encodings'` / в `sys.path` старый диск/папка — битый `venv` после переноса: удалите `venv` и снова `setup_env.ps1 -Force` (скрипт предпочтёт `tools\Python310`, а не сломанный `py -3.10`).
- `pip` завис на `cursor-sdk` (~46 МБ): прервите, повторите; или  
  `.\venv\Scripts\python.exe -m pip install cursor-sdk==1.0.28`  
  затем снова `-r app\requirements.txt`. При очень медленной сети скачайте wheel отдельно и установите из файла.
- Обновить список версий для `venvs\*` после изменения зависимостей: `venvs\<имя>\Scripts\python.exe -m pip freeze --exclude-editable > scripts\requirements_lock\<имя>.txt`.

## PyCharm / IDE

- Интерпретатор проекта: `venv\Scripts\python.exe` (после `setup_env`, не системный Python и не чужой venv).
- Надёжный запуск портала — bat / `scripts\restart.ps1`, а не «голый» `main.py` без рабочей папки корня проекта и без preflight-окружения.
