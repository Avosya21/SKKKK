"""dump_sortformer: выбор отчёта GGUF-прогона и понятная ошибка без аудио.

Заменяет `test_dump_sortformer_feed.py`: нормализатор WAV (`_sortformer_feed_wav`, `_wav_data_ok`) из скрипта убран —
GGUF-движок отдаёт отчёт сам, а боевой контур Sortformer (ONNX) тестируется в `test_lab_stages.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "asr"))

import dump_sortformer as mod  # noqa: E402
from dump_sortformer import _find_report  # noqa: E402


def test_find_report_prefers_vulkan_over_cuda(tmp_path):
    (tmp_path / "sortformer_gguf_cuda_call_report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "sortformer_gguf_vulkan_call_report.json").write_text("{}", encoding="utf-8")
    assert _find_report(tmp_path, "call").name == "sortformer_gguf_vulkan_call_report.json"


def test_find_report_falls_back_to_any_tag(tmp_path):
    assert _find_report(tmp_path, "call") is None
    (tmp_path / "sortformer_gguf_cpu_call_report.json").write_text("{}", encoding="utf-8")
    assert _find_report(tmp_path, "call").name == "sortformer_gguf_cpu_call_report.json"


def test_find_report_ignores_other_audio(tmp_path):
    (tmp_path / "sortformer_gguf_vulkan_other_report.json").write_text("{}", encoding="utf-8")
    assert _find_report(tmp_path, "call") is None


def test_main_explains_missing_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "WORK", tmp_path)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert "input_16k.wav" in str(exc.value)
