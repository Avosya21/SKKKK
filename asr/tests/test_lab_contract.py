"""Контракт лаборатории: изоляция прогонов, честное падение, пропуск по отпечатку."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.paths import PathEscape, LabRun, is_run_id, list_run_ids, new_run_id  # noqa: E402
from asr.lab.run import (  # noqa: E402
    MissingInput,
    execute_stage,
    init_run,
    outputs_intact,
    read_stage,
    source_audio,
)


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts" / "art_test"
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "input_16k.wav").write_bytes(b"RIFFfake-wav-bytes")
    return root


def make_run(artifact: Path) -> LabRun:
    run = LabRun(artifact, new_run_id())
    init_run(
        run,
        artifact_id="art_test",
        project_id=1,
        source_audio=artifact / "audio" / "input_16k.wav",
    )
    return run


def test_run_id_format():
    value = new_run_id()
    assert is_run_id(value)
    assert not is_run_id("../etc")
    assert not is_run_id("20260826T101500Z")


def test_resolve_rejects_escape(artifact: Path):
    run = make_run(artifact)
    assert run.resolve("02_asr/words.json").is_relative_to(run.root)
    with pytest.raises(PathEscape):
        run.resolve("../../../secrets.txt")
    with pytest.raises(PathEscape):
        run.resolve(artifact / "audio" / "input_16k.wav")


def test_runs_are_isolated(artifact: Path):
    first = make_run(artifact)
    second = make_run(artifact)
    assert first.root != second.root

    (first.stage_dir("02_asr") / "words.json").write_text("{}", encoding="utf-8")
    assert not (second.stage_dir("02_asr") / "words.json").exists()
    assert sorted(list_run_ids(artifact)) == sorted([first.run_id, second.run_id])


def test_missing_input_fails_loudly(artifact: Path):
    run = make_run(artifact)
    with pytest.raises(MissingInput) as excinfo:
        execute_stage(
            run,
            "02_asr",
            inputs={"clean_16k": run.stage_dir("01_audio") / "clean_16k.wav"},
            params={},
            work=lambda ctx: None,
        )
    assert "clean_16k" in str(excinfo.value)


def test_source_change_is_detected(artifact: Path):
    run = make_run(artifact)
    assert source_audio(run).is_file()
    (artifact / "audio" / "input_16k.wav").write_bytes(b"RIFFdifferent")
    with pytest.raises(Exception) as excinfo:
        source_audio(run)
    assert "изменился" in str(excinfo.value)


def _touch_stage(run: LabRun, payload: str = "hello") -> None:
    source = run.stage_dir("01_audio") / "clean_16k.wav"
    source.write_text(payload, encoding="utf-8")


def test_stage_records_hashes_and_skips_on_repeat(artifact: Path):
    run = make_run(artifact)
    _touch_stage(run)
    calls: list[int] = []

    def work(ctx):
        calls.append(1)
        ctx.write_json("words.json", {"words": []})
        ctx.metrics = {"word_count": 0}

    inputs = {"clean_16k": run.stage_dir("01_audio") / "clean_16k.wav"}
    first = execute_stage(run, "02_asr", inputs=inputs, params={"model": "x"}, work=work)
    assert first["status"] == "ok"
    assert first["inputs"][0]["sha256"]
    assert first["outputs"][0]["path"] == "02_asr/words.json"

    second = execute_stage(run, "02_asr", inputs=inputs, params={"model": "x"}, work=work)
    assert second.get("skipped") is True
    assert len(calls) == 1

    forced = execute_stage(
        run, "02_asr", inputs=inputs, params={"model": "x"}, work=work, force=True
    )
    assert forced.get("skipped") is not True
    assert len(calls) == 2


def test_changed_params_rerun_stage(artifact: Path):
    run = make_run(artifact)
    _touch_stage(run)
    calls: list[str] = []

    def work(ctx):
        calls.append(ctx.params["model"])
        ctx.write_json("words.json", {"model": ctx.params["model"]})

    inputs = {"clean_16k": run.stage_dir("01_audio") / "clean_16k.wav"}
    execute_stage(run, "02_asr", inputs=inputs, params={"model": "a"}, work=work)
    execute_stage(run, "02_asr", inputs=inputs, params={"model": "b"}, work=work)
    assert calls == ["a", "b"]


def test_changed_input_reruns_stage(artifact: Path):
    run = make_run(artifact)
    _touch_stage(run, "one")
    calls: list[int] = []

    def work(ctx):
        calls.append(1)
        ctx.write_json("words.json", {})

    inputs = {"clean_16k": run.stage_dir("01_audio") / "clean_16k.wav"}
    execute_stage(run, "02_asr", inputs=inputs, params={}, work=work)
    _touch_stage(run, "two")
    execute_stage(run, "02_asr", inputs=inputs, params={}, work=work)
    assert len(calls) == 2


def test_tampered_output_invalidates_skip(artifact: Path):
    run = make_run(artifact)
    _touch_stage(run)

    def work(ctx):
        ctx.write_json("words.json", {"words": []})

    inputs = {"clean_16k": run.stage_dir("01_audio") / "clean_16k.wav"}
    execute_stage(run, "02_asr", inputs=inputs, params={}, work=work)
    record = read_stage(run, "02_asr")
    assert outputs_intact(run, record)

    (run.stage_dir("02_asr") / "words.json").write_text('{"words": ["подмена"]}', encoding="utf-8")
    assert not outputs_intact(run, record)


def test_failure_is_recorded_and_raised(artifact: Path):
    run = make_run(artifact)
    _touch_stage(run)

    def work(ctx):
        raise ValueError("модель не отдала токены")

    with pytest.raises(ValueError):
        execute_stage(
            run,
            "02_asr",
            inputs={"clean_16k": run.stage_dir("01_audio") / "clean_16k.wav"},
            params={},
            work=work,
        )
    record = read_stage(run, "02_asr")
    assert record["status"] == "fail"
    assert "модель не отдала токены" in record["error"]


def test_latest_marker_points_to_new_run(artifact: Path):
    first = make_run(artifact)
    marker = json.loads((artifact / "lab" / "latest.json").read_text(encoding="utf-8"))
    assert marker["run_id"] == first.run_id
    second = make_run(artifact)
    marker = json.loads((artifact / "lab" / "latest.json").read_text(encoding="utf-8"))
    assert marker["run_id"] == second.run_id
