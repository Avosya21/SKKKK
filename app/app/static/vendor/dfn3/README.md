# DeepFilterNet 3 (комната, не ASR)

Клиентский денойз микрофона. Lab `01_audio` по-прежнему сырая копия 16 кГц.

Источник: `deepfilternet3-noise-filter` 1.3.0 (MIT / Apache-2.0), модель Rikorose/DeepFilterNet.

Скачать веса:

```
venv\Scripts\python.exe scripts\fetch_meeting_dfn3.py
```

Без `v3/pkg/df_bg.wasm` кнопка «Очистка» откатывается на обычный микрофон.
