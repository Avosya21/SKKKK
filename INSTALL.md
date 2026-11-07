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

## Что скачать вручную и какие версии

Клонирование репозитория **не** кладёт на диск веса моделей и часть `tools\`. Ниже — что добрать самим; пины pip — в файлах, не в этой таблице.

### Системное и приложения

| Что | Версия / ориентир | Кто ставит |
|---|---|---|
| ОС | Windows 10/11, GPU NVIDIA (для ASR и локальной LLM желательно ≥16–24 ГБ VRAM) | вручную |
| Драйвер NVIDIA | актуальный с поддержкой CUDA 12.x | **вручную** (nvidia.com) |
| Python | **3.10.x** (проверено 3.10.11) | `INSTALL.bat` / `tools\Python310` / winget |
| Git | текущий winget `Git.Git` | `INSTALL.bat` |
| Docker Desktop | с WSL2 | `INSTALL.bat`, один раз принять лицензию |
| Ollama | winget `Ollama.Ollama` | `INSTALL.bat`; модель эмбеддингов — ниже |
| LM Studio | winget `ElementLabs.LMStudio` | `INSTALL.bat`; **веса LLM — вручную** |
| LibreOffice, Tesseract (rus+eng), FFmpeg | портативно в `tools\` или winget; FFmpeg ориентир **Gyan full_build 9+** | `INSTALL.bat` / копирование |

### Модели (не в git)

| Назначение | Что поставить | Где лежит / как |
|---|---|---|
| Локальная LLM | GGUF **UD-Q4_K_XL** (предпочтительно) или Q4_K_S того же ключа; рядом **mmproj-BF16** | ключ `LMSTUDIO_MODEL` = `swift-qwen3.8-27b-uncensored-dynamic-mtp`; профиль `tools/lmstudio/qwen-profile.json` (окно **32768**, parallel 2, thinking выкл.); ссылки на HF — в том же JSON (`weights_download`, `mmproj_download`) |
| Эмбеддинги | Ollama **`bge-m3`** (`OLLAMA_EMBED_MODEL`) | обычно дотягивает install/preflight |
| Слова ASR (канон) | Parakeet-TDT **0.6B v3** ONNX | HF `istupakov/parakeet-tdt-0.6b-v3-onnx` rev `8f23f0c` → `asr/models` / кэш lab; ONNX Runtime GPU **1.23.2**, onnx-asr **0.12.0** (`venvs\lab`) |
| Диаризация | **Nemotron 3** (по умолчанию, до 8 спикеров) | файл `Nemotron-3-Diarization/Nemotron-3-Diarization.nemo`, SHA256 в `asr/lab/diar_models.py`; окружение `venvs\nemo-diar`. Откат: `DIAR_MODEL=sortformer4`, ONNX Sortformer 4spk v2.1 rev `3be92087` |
| Второй слух | GigaAM-v3 e2e RNNT | HF `ai-sage/GigaAM-v3` rev **`e2e_rnnt`**; Docker `primaryanalitics-gigaam` `:18102`, CUDA **12.4** / torch **2.6+cu124** (`INSTALL.bat -Asr`) |
| Шумодав в комнате | DeepFilterNet3 WASM | `venv\Scripts\python.exe scripts\fetch_meeting_dfn3.py` → `app/app/static/vendor/dfn3/` |
| Эмулятор для QA | Android SDK + AVD **pixel7** (API 34) | `tools/android\` (не в git); после смены диска поправить пути в `pixel7.ini` / `hardware-qemu.ini` |

Снимок версий ASR (freeze): `asr/lab/FREEZE.md`, `asr/lab/freeze.json`. Карта стека — `docs/PROJECT-MAP.md` §1 / §8.

### Версии Python-библиотек (источник истины)

| Окружение | Файл с пинами |
|---|---|
| Портал, смоук, скрипты | `app/requirements.txt` (например FastAPI **0.141.1**, SQLAlchemy **2.0.52**, sqlite-vec **0.1.9**, cursor-sdk **1.0.28**, livekit-api **1.2.1**) |
| Dev/pytest extras | `app/requirements-dev.txt` |
| Lab ASR / Sortformer | `scripts/requirements_lock/lab.txt` |
| Nemotron diar | `scripts/requirements_lock/nemo-diar.txt` |
| Прочие движки | `scripts/requirements_lock/*.txt` (`gigaam`, `voice`, …) |

Ставить только через `scripts\setup_env.ps1` (или `INSTALL.bat`), не смешивая системный Python с `venv`.

### Секреты и списки доступа (не в git)

Скопировать из примеров и заполнить: `.env` ← `.env.example`, `Login_up.txt` ← `Login_up.example.txt`, при ролях входа — `Login_roles.txt` ← `Login_roles.example.txt`, `Companies.txt` ← `Companies.example.txt`. В `.env` минимум: `SECRET_KEY`, пароли/хеши ролей или личные пароли по вашей схеме, `HF_TOKEN` при скачивании с Hugging Face; для `LLM_PROVIDER=cursor` — `CURSOR_API_KEY`.

## После переноса на другой диск или ПК

1. **Не копировать** чужие `venv\` / `venvs\` — только `setup_env.ps1 -Force`.
2. Прогнать замену старого корня в `.env` (`INSTALL` / `setup_env` с `-OldRoot`).
3. Починить абсолютные пути в **Android AVD** (`tools/android/avd/pixel7*`), снять stale `*.lock` у эмулятора.
4. Проверки: `scripts\check_env.ps1`, `venv\Scripts\python.exe scripts\audit_integrity.py`, `scripts\health_report.py`.
5. Сверить папки `data/projects/{id}` со строками в БД (сироты на диске — удалить или восстановить проект).
6. Предупреждение `sqlite-vec not loaded` при «голом» `sqlite3` нормально, если через портал поиск/чат работают (расширение грузится на соединении приложения).
7. Закрыть зависшие `live`-встречи песочницы и не оставлять e2e-артефакты в проде.

Краткий чеклист «можно работать»:

```text
setup_env / INSTALL → .env и Login_* заполнены → LM Studio или Cursor по LLM_PROVIDER
restart/start → audit_integrity без проблем → health без красных → поиск/чат на пилотном проекте
```

## Поведение, важное для смоука и комнаты

- Песочница для автопроверок — отдельный проект (по умолчанию id **29**); клиентские проекты смоуком не трогать.
- Смоук без OTP: `E2E_ALLOW_MINT=1` и JWT из `scripts/e2e/mint_smoke_token.py`. Токен выпускается **на роль**: один bearer admin не подходит для проверки manager (нужен отдельный mint с `SMOKE_ROLE=manager` или логика в `scripts/smoke/common.py`).
- Полный приёмочный прогон: `scripts/acceptance_all.py` / `qa_all.py --acceptance` — см. `docs/TESTING.md`. После прогона завершайте live-встречи песочницы.
- Шумодав в браузере **по умолчанию выключен**; DFN3 грузится после включения кнопки. Сырая дорожка записи может публиковаться и при выключенном шумодаве.
- «Речь не обнаружена» на тихом файле — штатный исход (`done` + сообщение), не обязательно авария стенда.
- Список «Исправлено» в шапке портала (`changelog.json`) пополняют только осознанно; восстановление стенда и правки тестов туда не пишут.

## Карта проекта и коммиты

Новый роут, кнопка, модуль или шаг конвейера отражайте в `docs/PROJECT-MAP.md` и `docs/project-map/manifest.yaml` **в том же коммите**. Хуки (`scripts/install_hooks.ps1`) отклонят коммит при расхождении. Инвентарь машины: `docs/project-map/stand.yaml` + `scripts/check_stand_inventory.py` (временные `data/tmp_*` и опциональные тулы — в skip/optional).

## PyCharm / IDE

- Интерпретатор проекта: `venv\Scripts\python.exe` (после `setup_env`, не системный Python и не чужой venv).
- Надёжный запуск портала — bat / `scripts\restart.ps1`, а не «голый» `main.py` без рабочей папки корня проекта и без preflight-окружения.
