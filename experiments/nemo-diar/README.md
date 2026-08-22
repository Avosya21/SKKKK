# Sortformer GGUF (OpenResearchTools Engine)

## GPU backends

| Backend | Папка | Требования |
|---------|-------|------------|
| **Vulkan** (рекомендуется сейчас) | `data/tools/openresearchtools-engine-vulkan` | RTX 3090 + драйвер 577+ |
| **CUDA** | `data/tools/openresearchtools-engine` | Драйвер **>= 580** (CUDA 13 runtime в bundle) |
| **CPU** | любая из папок + `--Prefer cpu` | без GPU |

### Почему CUDA-сборка не стартовала

Engine CUDA поставляется с `cudart64_13.dll` (CUDA **13**).  
У вас драйвер **577** → nvidia-smi показывает max CUDA **12.9**.  
Для CUDA 13 нужен драйвер **580+** ([NVIDIA CUDA 13 release notes](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/)).

После обновления драйвера до 580+ CUDA-сборка должна показать `backend=CUDA` в `example-cli list-devices`.

## Запуск

```powershell
# auto: Vulkan GPU, если CUDA13 недоступен
powershell -ExecutionPolicy Bypass -File experiments\nemo-diar\run_sortformer_gguf.ps1 -ClipSec 600

# явно Vulkan / CPU
powershell -ExecutionPolicy Bypass -File experiments\nemo-diar\run_sortformer_gguf.ps1 -Prefer vulkan -ClipSec 0
powershell -ExecutionPolicy Bypass -File experiments\nemo-diar\run_sortformer_gguf.ps1 -Prefer cpu -ClipSec 120

# после обновления драйвера до 580+
powershell -ExecutionPolicy Bypass -File experiments\nemo-diar\run_sortformer_gguf.ps1 -Prefer cuda
```

Проверка backend:

```powershell
powershell -ExecutionPolicy Bypass -File experiments\nemo-diar\pick_engine_backend.ps1
```

## Скорость (10 мин телемоста, v2.1 GGUF)

| Backend | Время |
|---------|-------|
| CPU | ~105 с |
| Vulkan (RTX 3090) | ~56 с |
