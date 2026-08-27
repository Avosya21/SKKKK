"""Синхронизация аудио в asr/work для Docker-контейнеров ASR.

Whisper и GigaAM ASR получают WAV через multipart upload.
T-one и GigaAM embed читают файл только из смонтированного asr/work → /data или /work.
При SKKKK_ASR_WORK в per-artifact каталоге без staging контейнер видит чужой/stale файл.

Инвариант: перед любым HTTP/docker exec, где контейнер читает /data или /work,
вызывать stage_* из этого модуля. Один GPU-воркер — последовательные job'ы; параллельные
dev-скрипты на том же хосте без staging рискуют смешать арtefactы.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from paths import ASR, WORK

DOCKER_WORK = ASR / "work"


def _file_fingerprint(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def stage_docker_work_file(source: Path, dest_name: str) -> Path:
    """Скопировать source в asr/work/dest_name, если это не тот же файл."""
    if not source.is_file():
        raise FileNotFoundError(f"missing audio for docker staging: {source}")
    DOCKER_WORK.mkdir(parents=True, exist_ok=True)
    dest = DOCKER_WORK / dest_name
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    assert_docker_work_matches(source, dest_name)
    return dest


def assert_docker_work_matches(source: Path, dest_name: str) -> None:
    """Проверить, что asr/work совпадает с арtefact-источником (размер + hash)."""
    dest = DOCKER_WORK / dest_name
    if not dest.is_file():
        raise FileNotFoundError(f"docker work missing after stage: {dest}")
    src_size, src_hash = _file_fingerprint(source)
    dst_size, dst_hash = _file_fingerprint(dest)
    if src_size != dst_size or src_hash != dst_hash:
        raise RuntimeError(
            f"docker work out of sync for {dest_name}: "
            f"artifact={src_size}B hash={src_hash[:12]} "
            f"asr/work={dst_size}B hash={dst_hash[:12]}"
        )


def stage_tone_docker_input(source_8k: Path | None = None) -> Path:
    src = source_8k or (WORK / "input_8k.wav")
    return stage_docker_work_file(src, "input_8k.wav")


def stage_gigaam_docker_input(source_16k: Path | None = None) -> Path:
    src = source_16k or (WORK / "input_16k.wav")
    return stage_docker_work_file(src, "input_16k.wav")
