"""Выбор модели диаризации и 8 каналов Nemotron по конвейеру — без моделей."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab import diar_models  # noqa: E402
from asr.lab.sortformer import binarize  # noqa: E402
from asr.lab.stages.s03_diar import _heatmap  # noqa: E402
from asr.lab.stages.s04_paint import LEGACY_PAINT_DEFAULTS, _paint_defaults  # noqa: E402


def test_binarize_keeps_speakers_beyond_fourth():
    probs = np.zeros((50, 8), dtype=np.float32)
    probs[5:20, 1] = 0.9
    probs[25:45, 6] = 0.9
    rules = diar_models.MODELS[diar_models.NEMOTRON].binarization
    segments = binarize(probs, rules=rules)
    assert {row["speaker_index"] for row in segments} == {1, 6}
    assert any(row["speaker"] == "speaker_6" for row in segments)


def test_heatmap_reports_all_eight_channels():
    heat = _heatmap(np.random.default_rng(0).random((100, 8)).astype(np.float32))
    assert heat["speakers"] == 8


def test_default_model_is_nemotron(monkeypatch):
    monkeypatch.delenv("DIAR_MODEL", raising=False)
    spec = diar_models.selected_model()
    assert spec.key == diar_models.NEMOTRON
    assert spec.num_speakers == 8
    assert spec.min_prob == pytest.approx(0.20)


def test_env_rolls_back_to_sortformer(monkeypatch):
    monkeypatch.setenv("DIAR_MODEL", "sortformer4")
    spec = diar_models.selected_model()
    assert spec.key == diar_models.SORTFORMER
    assert spec.num_speakers == 4
    assert spec.binarization.onset == pytest.approx(0.64)
    assert spec.min_prob == pytest.approx(0.35)


def test_explicit_name_beats_env(monkeypatch):
    monkeypatch.setenv("DIAR_MODEL", "sortformer4")
    assert diar_models.selected_model("nemotron3").key == diar_models.NEMOTRON


def test_unknown_model_is_an_error():
    with pytest.raises(ValueError, match="неизвестная модель"):
        diar_models.selected_model("whisper")


def test_paint_defaults_from_diar_json(tmp_path):
    diar = tmp_path / "diar.json"
    diar.write_text(json.dumps({"paint_defaults": {"min_prob": 0.2, "min_margin": 0.15}}), encoding="utf-8")
    assert _paint_defaults(diar) == {"min_prob": 0.2, "min_margin": 0.15}


def test_old_runs_keep_sortformer_paint_thresholds(tmp_path):
    diar = tmp_path / "diar.json"
    diar.write_text(json.dumps({"model": "diar_streaming_sortformer_4spk-v2.1.onnx"}), encoding="utf-8")
    assert _paint_defaults(diar) == LEGACY_PAINT_DEFAULTS
    assert _paint_defaults(tmp_path / "missing.json") == LEGACY_PAINT_DEFAULTS


def test_nemotron_refuses_wrong_checksum(tmp_path, monkeypatch):
    fake = tmp_path / "fake.nemo"
    fake.write_bytes(b"not a model")
    monkeypatch.setenv("NEMOTRON_DIAR_MODEL", str(fake))
    if not diar_models.NEMOTRON_PYTHON.is_file():
        pytest.skip("нет venvs\\nemo-diar")
    with pytest.raises(RuntimeError, match="контрольная сумма"):
        diar_models.nemotron_model_path()
