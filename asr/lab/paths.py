"""Пути лабораторного прогона. Единственный источник — этот модуль.

Ни одна функция не умеет вернуть путь вне каталога прогона: попытка выйти
наружу через ``..`` или абсолютный путь поднимает ``PathEscape``. Глобальных
каталогов вроде ``asr/out`` здесь нет намеренно — именно из-за них расшифровки
подмешивались друг в друга.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LAB_DIR = "lab"
RUNS_DIR = "runs"
LATEST_FILE = "latest.json"

STAGES: tuple[str, ...] = (
    "01_audio",
    "02_asr",
    "03_diar",
    "04_paint",
    "05_voice",
    "06_transcript",
    "07_verify",
)

STAGE_TITLES: dict[str, str] = {
    "01_audio": "Рабочий WAV 16 кГц",
    "02_asr": "Распознавание (Parakeet-TDT v3)",
    "03_diar": "Диаризация (Nemotron 3 / Sortformer)",
    "04_paint": "Покраска слов спикерами",
    "05_voice": "Слепки голосов (WeSpeaker)",
    "06_transcript": "Сборка стенограммы",
    "07_verify": "Проверка (VAD)",
}

# Каталог, который монтируется в контейнер на стадии 05_voice. Вне артефакта,
# потому что Docker видит только фиксированные mount'ы, но по каталогу на прогон.
STAGING_ROOT = ROOT / "data" / "lab-staging"

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{6}$")


class PathEscape(ValueError):
    """Попытка собрать путь вне каталога прогона."""


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{secrets.token_hex(3)}"


def is_run_id(value: str) -> bool:
    return bool(_RUN_ID_RE.match(value))


def require_run_id(value: str) -> str:
    if not is_run_id(value):
        raise ValueError(f"некорректный run_id: {value!r}")
    return value


def lab_dir(artifact_root: Path) -> Path:
    return Path(artifact_root) / LAB_DIR


def runs_dir(artifact_root: Path) -> Path:
    return lab_dir(artifact_root) / RUNS_DIR


def latest_path(artifact_root: Path) -> Path:
    return lab_dir(artifact_root) / LATEST_FILE


def list_run_ids(artifact_root: Path) -> list[str]:
    base = runs_dir(artifact_root)
    if not base.is_dir():
        return []
    return sorted((p.name for p in base.iterdir() if p.is_dir() and is_run_id(p.name)), reverse=True)


class LabRun:
    """Каталог одного прогона. Все пути строятся только через него."""

    def __init__(self, artifact_root: Path, run_id: str) -> None:
        self.artifact_root = Path(artifact_root).resolve()
        self.run_id = require_run_id(run_id)
        self.root = (runs_dir(self.artifact_root) / self.run_id).resolve()

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"LabRun({self.artifact_root}, {self.run_id})"

    def ensure(self) -> LabRun:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(exist_ok=True)
        for stage in STAGES:
            (self.root / stage).mkdir(exist_ok=True)
        return self

    def resolve(self, relative: str | Path) -> Path:
        """Путь внутри прогона. Выход наружу — ошибка, не молчаливая склейка."""
        candidate = Path(relative)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise PathEscape(f"{relative!r} ведёт за пределы прогона {self.root}")
        return resolved

    def relative(self, path: Path) -> str:
        """Путь относительно корня прогона — в таком виде он попадает в stage.json."""
        return self.resolve(path).relative_to(self.root).as_posix()

    def stage_dir(self, stage: str) -> Path:
        if stage not in STAGES:
            raise ValueError(f"неизвестная стадия {stage!r}")
        path = self.root / stage
        path.mkdir(parents=True, exist_ok=True)
        return path

    def stage_json(self, stage: str) -> Path:
        return self.stage_dir(stage) / "stage.json"

    def stage_log(self, stage: str) -> Path:
        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs / f"{stage}.log"

    @property
    def run_json(self) -> Path:
        return self.root / "run.json"

    @property
    def staging_dir(self) -> Path:
        """Per-run каталог для Docker. Имена файлов внутри тоже несут run_id."""
        path = STAGING_ROOT / self.run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def stage_index(stage: str) -> int:
    return STAGES.index(stage)


def stages_from(stage: str) -> tuple[str, ...]:
    return STAGES[stage_index(stage) :]
