"""Чеклист этапов ASR-пайплайна.

Каждый этап вызывает gate(name) в начале — если предыдущий не прошёл, получает
исключение и пайплайн останавливается.  По завершении — mark_ok(name) или
mark_fail(name, reason).

Состояние хранится в ``asr/out/pipeline_checklist.json``.  При новом прогоне
чеклист пересоздаётся через reset().
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# На Windows консоль часто cp1251; переключаем на utf-8 без BOM, чтобы
# кириллица и ASCII-символы выводились без ошибок.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "asr"))
from paths import OUT  # noqa: E402

_FILE = OUT / "pipeline_checklist.json"

Status = Literal["pending", "ok", "fail", "running"]

STEPS: list[str] = [
    "audio_prep",       # 1. ffmpeg → WAV 16/8 кГц
    "gemma_unload",     # 2. выгрузка Gemma, VRAM < 2500
    "asr_containers",   # 2. старт и healthcheck контейнеров
    "asr_run",          # 3. три ASR параллельно
    "asr_stop",         # 4. стоп whisper/gigaam, освободить VRAM
    "sortformer",       # 5. Sortformer (после ASR, иначе GPU-конфlict)
    "gemma_load",       # 6. загрузка Gemma
    "merge",            # 7. склейка Gemma
    "view_build",       # 8. нарезка по голосам + HTML
]


def _load() -> dict:
    if _FILE.is_file():
        try:
            return json.loads(_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _save(data: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def reset(source: str = "") -> None:
    """Начать новый прогон: пересоздать чеклист."""
    _save({
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "steps": {name: {"status": "pending"} for name in STEPS},
    })


def mark_running(name: str) -> None:
    data = _load()
    data.setdefault("steps", {}).setdefault(name, {})["status"] = "running"
    data["steps"][name]["started_at"] = time.strftime("%H:%M:%S")
    _save(data)


def mark_pending(name: str) -> None:
    data = _load()
    step = data.setdefault("steps", {}).setdefault(name, {})
    step["status"] = "pending"
    step.pop("started_at", None)
    step.pop("finished_at", None)
    _save(data)


def mark_ok(name: str, detail: str = "") -> None:
    data = _load()
    step = data.setdefault("steps", {}).setdefault(name, {})
    step["status"] = "ok"
    step["finished_at"] = time.strftime("%H:%M:%S")
    if detail:
        step["detail"] = detail
    _save(data)
    _print_row(name, "ok", detail)


def mark_fail(name: str, reason: str) -> None:
    data = _load()
    step = data.setdefault("steps", {}).setdefault(name, {})
    step["status"] = "fail"
    step["finished_at"] = time.strftime("%H:%M:%S")
    step["reason"] = reason
    _save(data)
    _print_row(name, "FAIL", reason)


def gate(name: str) -> None:
    """Проверить, что все шаги, идущие перед name, завершились ok."""
    if name not in STEPS:
        raise ValueError(f"неизвестный шаг: {name!r}")
    data = _load()
    steps = data.get("steps", {})
    idx = STEPS.index(name)
    for prev in STEPS[:idx]:
        st = steps.get(prev, {}).get("status", "pending")
        if st != "ok":
            msg = f"шаг {prev!r} не пройден (status={st!r}), {name!r} не может начаться"
            raise PipelineGateError(msg)
    mark_running(name)


def summary() -> dict:
    data = _load()
    steps = data.get("steps", {})
    ok = [n for n in STEPS if steps.get(n, {}).get("status") == "ok"]
    fail = [n for n in STEPS if steps.get(n, {}).get("status") == "fail"]
    return {"ok": ok, "fail": fail, "total": len(STEPS), "done": len(ok) == len(STEPS)}


def print_summary() -> None:
    s = summary()
    print(f"\n{'='*52}", flush=True)
    print(f"  Чеклист пайплайна: {len(s['ok'])}/{s['total']} пройдено", flush=True)
    data = _load()
    steps = data.get("steps", {})
    for name in STEPS:
        st = steps.get(name, {}).get("status", "pending")
        icon = {"ok": "[OK]", "fail": "[!!]", "running": "[..]", "pending": "[  ]"}[st]
        detail = steps.get(name, {}).get("detail") or steps.get(name, {}).get("reason") or ""
        suffix = f"  <- {detail[:60]}" if detail else ""
        print(f"  {icon} {name:<20} {st}{suffix}", flush=True)
    if s["fail"]:
        print(f"\n  [!!] Провалены: {', '.join(s['fail'])}", flush=True)
    elif s["done"]:
        print("\n  [OK] Все этапы пройдены успешно", flush=True)
    print(f"{'='*52}\n", flush=True)


def _print_row(name: str, status: str, detail: str) -> None:
    icon = "[OK]" if status == "ok" else "[!!]"
    suffix = f" -- {detail[:70]}" if detail else ""
    print(f"  {icon} [{name}]{suffix}", flush=True)


class PipelineGateError(RuntimeError):
    """Шаг заблокирован: предыдущий этап не прошёл."""
