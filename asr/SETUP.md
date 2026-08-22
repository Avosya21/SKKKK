# Установка и первый прогон трёх ASR

Три изолированных окружения. Это не перестраховка: `faster-whisper`/CTranslate2, PyTorch для GigaAM и ONNX-стек T-one тянут разные сборки CUDA и cuDNN, а в одном процессе Windows это приводит к конфликту DLL. Отдельные venv плюс отдельные процессы решают проблему и заодно гарантируют возврат VRAM после завершения.

Все команды выполняются в PowerShell из `C:\1C_NEW_SKKKK`.

---

## Прежде чем начать

**Отзовите токен Hugging Face.** В `AudioToText\docker-compose.yaml` и `AudioToText\копия.yaml` записан рабочий токен `hf_LbPnWW...`. Он лежит в папке, где инициализирован git. Зайдите в настройки Hugging Face, удалите этот токен, выпустите новый и держите его в `.env` (`HF_TOKEN=...`), а не в yaml. В файлах ниже токен нигде не хардкодится.

---

## 1. Общие зависимости

```powershell
cd C:\1C_NEW_SKKKK
mkdir venvs -Force
```

FFmpeg должен быть в PATH (`ffmpeg -version`, `ffprobe -version`).

---

## 2. Окружение Whisper Podlodka

Модель: [bond005/whisper-large-v3-ru-podlodka](https://huggingface.co/bond005/whisper-large-v3-ru-podlodka) — дообученный на русском Whisper large-v3, около 1.5 млрд параметров. В fp16 занимает примерно 3–4 ГБ VRAM.

```powershell
python -m venv venvs\whisper
venvs\whisper\Scripts\activate
pip install --upgrade pip
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install transformers accelerate librosa soundfile
deactivate
```

Первый запуск скачает веса (несколько ГБ) в кэш Hugging Face.

---

## 3. Окружение GigaAM

Модель: [ai-sage/GigaAM-v3](https://huggingface.co/ai-sage/GigaAM-v3), ревизия `e2e_rnnt` — версия с пунктуацией и нормализацией.

```powershell
python -m venv venvs\gigaam
venvs\gigaam\Scripts\activate
pip install --upgrade pip
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install transformers accelerate soundfile sentencepiece hydra-core omegaconf
pip install pyannote.audio
deactivate
```

`pyannote.audio` нужен только для `transcribe_longform` (разбиение длинного аудио по речи). Модели pyannote закрыты лицензией: нужно принять условия на Hugging Face и задать `HF_TOKEN` в окружении. Если этого не сделать, `run_gigaam.py` сообщит причину и перейдёт на обычный `transcribe`.

Рабочий GigaAM **должен идти на GPU**. Старые CPU-флаги в `AudioToText/` (`CUDA_VISIBLE_DEVICES=""`, `model.to("cpu")`, `devices: []` в compose) — наследие чужой слабой машины, не дизайн. И `AudioToText\main.py`, и `asr\run_gigaam.py` берут CUDA при наличии (`float16`), CPU только как fallback. В конвейере SKKKK используется нативный `asr\run_gigaam.py`.

---

## 4. Окружение T-one

Модель: [t-tech/T-one](https://huggingface.co/t-tech/T-one) — потоковый CTC для русской телефонии, работает с 8 кГц.

Официальный пакет `tone` разработчики поддерживают на Linux и macOS. На Windows сначала пробуем его, при неудаче переходим на sherpa-onnx с конвертированной моделью.

Путь A, официальный:

```powershell
python -m venv venvs\tone
venvs\tone\Scripts\activate
pip install --upgrade pip
pip install tone numpy
deactivate
```

Путь Б, запасной для Windows:

```powershell
venvs\tone\Scripts\activate
pip install sherpa-onnx numpy
deactivate
```

Затем скачать `sherpa-onnx-streaming-t-one-russian-2025-09-08` из релизов `k2-fsa/sherpa-onnx` (раздел asr-models), распаковать в `C:\1C_NEW_SKKKK\asr\models\t-one\` так, чтобы там оказались `model.onnx` и `tokens.txt`.

`run_tone.py` сам выберет доступный путь.

---

## 5. Тестовый файл

Папка `C:\1C_NEW_SKKKK\Test_video` сейчас пуста — положите туда видео или аудио созвона.

---

## 6. Прогон

Все три движка параллельно:

```powershell
cd C:\1C_NEW_SKKKK\asr
python run_all.py --source "C:\1C_NEW_SKKKK\Test_video\ваш_файл.mp4"
```

По одному, если нужно разобраться с конкретным движком:

```powershell
python prepare_audio.py "C:\1C_NEW_SKKKK\Test_video\ваш_файл.mp4" --out-dir work

..\venvs\whisper\Scripts\python.exe run_whisper.py --audio work\ваш_файл_16k.wav --out-dir out
..\venvs\gigaam\Scripts\python.exe  run_gigaam.py  --audio work\ваш_файл_16k.wav --out-dir out
..\venvs\tone\Scripts\python.exe    run_tone.py    --audio work\ваш_файл_8k.wav  --out-dir out
```

---

## 7. Что должно получиться

```
asr\out\
  ваш_файл_raw_whisper.txt   .json
  ваш_файл_raw_gigaam.txt    .json
  ваш_файл_raw_tone.txt      .json
  ваш_файл_COMPARE.md        сводка: время, RTF, первые строки каждого
  whisper.log  gigaam.log  tone.log
```

## 8. Проверки этого прогона

- [ ] Три `_raw_*.txt` непустые
- [ ] В диспетчере задач видно три процесса Python одновременно
- [ ] Пиковая занятость VRAM не превысила примерно 8 ГБ (Gemma в LM Studio на это время выгружена)
- [ ] В `COMPARE.md` тексты трёх движков относятся к одному и тому же разговору
- [ ] Ни один движок не выдал латиницу вместо кириллицы

## 9. Если что-то не пошло

| Симптом | Причина | Что делать |
|---|---|---|
| `CUDA out of memory` | Gemma осталась в VRAM | `lms unload --all` перед прогоном |
| Whisper очень медленный | Считает на CPU | Проверить `torch.cuda.is_available()` в этом venv |
| GigaAM: ошибка про pyannote | Не принята лицензия или нет токена | Принять условия на HF, задать `HF_TOKEN`, либо запустить с `--no-longform` |
| `pip install tone` падает | Пакет рассчитан на Linux/macOS | Путь Б: sherpa-onnx |
| T-one: пустой текст | Подан не 8 кГц | Использовать файл `*_8k.wav` |
| Все три текста разные до неузнаваемости | Разные файлы на входе | Все три должны идти из одного `prepare_audio.py` |
