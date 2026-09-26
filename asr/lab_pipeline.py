"""CLI лабораторного конвейера расшифровки.

Новый прогон артефакта целиком:

    venvs\\lab\\Scripts\\python.exe asr/lab_pipeline.py --project-id 1 --artifact-id art_...

Повторить одну стадию в существующем прогоне с другими порогами:

    ... --run-id 20260826T101500Z_ab12cd --only 03_diar --onset 0.55 --force

Список прогонов артефакта:

    ... --project-id 1 --artifact-id art_... --list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.onnx_env import ensure_nvidia_dlls  # noqa: E402
from asr.lab.paths import STAGES, LabRun, list_run_ids  # noqa: E402
from asr.lab.pipeline import LabOptions, create_run, execute, status  # noqa: E402
from asr.lab.run import MissingInput, StageFailed  # noqa: E402


def artifact_paths(project_id: int, artifact_id: str) -> tuple[Path, Path]:
    """Пути артефакта без импорта FastAPI: в venvs\\lab нет pydantic."""
    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    root = data_dir / "projects" / str(project_id) / "artifacts" / artifact_id
    audio = root / "audio" / "input_16k.wav"
    return root, audio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Лабораторный конвейер расшифровки")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", help="существующий прогон; без него создаётся новый")
    parser.add_argument("--list", action="store_true", help="показать прогоны и выйти")
    parser.add_argument("--status", action="store_true", help="показать состояние стадий и выйти")

    parser.add_argument("--only", choices=STAGES, help="только одна стадия")
    parser.add_argument("--from", dest="start_from", choices=STAGES, help="начиная со стадии")
    parser.add_argument("--force", action="store_true", help="игнорировать совпадение отпечатка")

    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--asr-model", default=LabOptions.asr_model)
    parser.add_argument("--quantization", default=None)
    parser.add_argument(
        "--diar-model",
        default=None,
        choices=("nemotron3", "sortformer4"),
        help="модель диаризации; по умолчанию DIAR_MODEL или nemotron3",
    )
    parser.add_argument("--onset", type=float, default=LabOptions.onset)
    parser.add_argument("--offset", type=float, default=LabOptions.offset)
    parser.add_argument(
        "--compare-source",
        action="store_true",
        help="второй проход Sortformer по исходному WAV (для отладки)",
    )
    parser.add_argument("--min-prob", type=float, default=LabOptions.min_prob)
    parser.add_argument("--min-margin", type=float, default=LabOptions.min_margin)
    parser.add_argument("--no-voice", action="store_true", help="без слепков голоса (без Docker)")
    parser.add_argument("--clips-per-speaker", type=int, default=LabOptions.per_speaker_clips)
    parser.add_argument("--replica-gap", type=float, default=LabOptions.replica_gap)
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--report", action="store_true", help="пересобрать lab.html после прогона")
    return parser


def _configure_stdio() -> None:
    """Windows cp1251 ломается на стрелках в логах стадий."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


def main() -> int:
    _configure_stdio()
    ensure_nvidia_dlls()
    args = build_parser().parse_args()
    root, audio = artifact_paths(args.project_id, args.artifact_id)

    if args.list:
        print(json.dumps({"artifact_root": str(root), "runs": list_run_ids(root)}, ensure_ascii=False, indent=2))
        return 0

    if args.run_id:
        run = LabRun(root, args.run_id)
        if not run.run_json.is_file():
            print(f"нет прогона {args.run_id} в {root}", file=sys.stderr)
            return 2
    else:
        if args.status:
            print("--status требует --run-id", file=sys.stderr)
            return 2
        run = create_run(
            root,
            artifact_id=args.artifact_id,
            project_id=args.project_id,
            source_audio=audio,
        )
        print(f"новый прогон {run.run_id} в {run.root}")

    if args.status:
        print(json.dumps(status(run), ensure_ascii=False, indent=2))
        return 0

    options = LabOptions(
        device=args.device,
        asr_model=args.asr_model,
        quantization=args.quantization,
        diar_model=args.diar_model,
        onset=args.onset,
        offset=args.offset,
        compare_with_source=args.compare_source,
        min_prob=args.min_prob,
        min_margin=args.min_margin,
        voice=not args.no_voice,
        per_speaker_clips=args.clips_per_speaker,
        replica_gap=args.replica_gap,
        verify=not args.no_verify,
    )

    exit_code = 0
    try:
        summary = execute(
            run, options, only=args.only, start_from=args.start_from, force=args.force
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except MissingInput as exc:
        print(f"не хватает входа: {exc}", file=sys.stderr)
        exit_code = 3
    except StageFailed as exc:
        print(f"стадия упала: {exc}", file=sys.stderr)
        exit_code = 4
    except Exception as exc:  # noqa: BLE001 - CLI обязан показать причину, а не трейс
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = 5

    if args.report:
        from asr.build_lab_view import write_lab_report

        report = write_lab_report(root, run.run_id, artifact_id=args.artifact_id)
        print(f"отчёт: {report}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
