# _archive — то, что осталось от разработки

Сюда перенесено (не удалено) всё, что **не используется** рабочим кодом: ни приложением (`app/`), ни ASR-пайплайном (`asr/`),
ни скриптами запуска, ни Docker. Структура внутри повторяет структуру проекта, поэтому вернуть файл — значит переместить его
на то же относительное место.

Проверка перед переносом: поиск ссылок на каждый файл/папку в `app/`, `asr/`, `scripts/`, `docs/`, `.env*`, `docker-compose.yml`;
после переноса — полный прогон тестов.

## Что здесь лежит

| Путь в архиве | Что это | Размер |
|---|---|---|
| `whisper-large-v3-ru-podlodka/`, `faster-whisper-large-v3-turbo-ct2/` | веса Whisper из ранних экспериментов (в работе Whisper идёт из контейнера `asr/models/faster-whisper-large-v3`) | ~13,5 ГБ |
| `nvidiadiar_sortformer_4spk-v1/`, `pyannotesegmentation3.0/`, `speaker-diarization-3.1/`, `wespeaker-voxceleb-resnet34-LM/` | модели диаризации ранних экспериментов (рабочие лежат в `data/models/onnx-diar`, `data/gigaam/models`) | ~1 ГБ |
| `venvs/ifw/` (+ `scripts/requirements_lock/ifw.txt`) | окружение insanely-fast-whisper для экспериментов | 5,6 ГБ |
| `venvs/llmpm/` (+ lock) | окружение менеджера LLM-пакетов | 0,8 ГБ |
| `tools/wav2vec2.cpp/`, `data/models/wav2vec2-*` | эксперимент wav2vec2 | ~3,7 ГБ |
| `logs/`, `p.out`, `data/llmpm-*.log` | логи прогонов экспериментов | мало |
| `data/_gap_*`, `data/_invest_*`, `data/_tmp_*`, `data/_pytest_out.txt`, `data/skkkk.db` (пустая БД) | разовые отладочные файлы | мало |
| `scripts/` (11 файлов) | разовые скрипты: `backfill_audio_reports`, `confirm_test_speakers`, `debug_search`, `export_qa_manual_docx`, `migrate_fusion_artifacts`, `run_triple_isolation`, `run_wav2vec2_*`, `reextract_entities`, `requeue_audio_artifact`, `requeue_text_artifacts` | мало |
| `asr/` (3 файла) | `format_whisper_speakers.py`, `run_docker_gigaam_diar.py`, `run_docker_gigaam_tone.py` | мало |

## Как вернуть

Один объект (пример):

```powershell
Move-Item _archive\scripts\requeue_audio_artifact.py scripts\
Move-Item _archive\venvs\ifw venvs\        # окружение потом лучше пересоздать: scripts\setup_env.ps1 -Only ifw -Force
```

Всё сразу:

```powershell
robocopy _archive . /E /MOVE /XF README.md
```

## Что осталось на месте, хотя выглядит «старым»

- `experiments/` — приложение читает оттуда эталон диаризации (`experiments/diar-presets/out`, `experiments/nemo-diar/out/pilot_22_07_26`) и `run_sortformer_gguf.ps1`.
- `Login_up.txt` — список допущенных логинов (`login_allowlist_path`).
- `vendor/GigaAMGUI/` — из неё собирается Docker-образ GigaAM.
- `run_server.ps1` — запуск LM Studio (описан в PROJECT-MAP).
- `asr/*.py` — используются приложением (в т.ч. `build_*_view`, `lab_*`, `merge_*`).
- `venvs/nemo-diar`, `venvs/voice` и остальные окружения — на них ссылается код.
