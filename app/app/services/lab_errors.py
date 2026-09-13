"""User-facing lab errors. ONNX warnings and ANSI stay out of the UI."""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ONNX_WARN_RE = re.compile(r"\[W:onnxruntime:[^\]]+\][^\n]*")
NO_WORDS = "onnx-asr не собрал ни одного слова"
NO_REPLICAS = "из покрашенных слов не собралось ни одной реплики"
NO_SPEECH = "речь не обнаружена"
GIGAAM_DOWN = "GigaAM не запущен (контейнер skkkk-gigaam, порт 18102)"
ERROR_MARKERS = ("RuntimeError:", "StageFailed:", "FileNotFoundError:", "ValueError:")
MAX_ERROR_LEN = 400


def sanitize_lab_error(raw: str, *, step: str = "") -> str:
    text = ANSI_RE.sub("", raw or "")
    if NO_WORDS in text or NO_REPLICAS in text:
        return NO_SPEECH
    if "GigaAM не запущен" in text or _is_gigaam_refused(text, step):
        return GIGAAM_DOWN
    text = ONNX_WARN_RE.sub("", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        for marker in ERROR_MARKERS:
            if marker not in line:
                continue
            msg = line.split(marker, 1)[-1].strip()
            if NO_WORDS in msg:
                return NO_SPEECH
            return msg[:MAX_ERROR_LEN]
    joined = " ".join(lines).strip()
    if NO_WORDS in joined:
        return NO_SPEECH
    if not joined:
        return f"{step} failed" if step else "ошибка расшифровки"
    if step and joined.startswith(step):
        return joined[:MAX_ERROR_LEN]
    if step:
        return f"{step}: {joined}"[:MAX_ERROR_LEN]
    return joined[:MAX_ERROR_LEN]


def _is_gigaam_refused(text: str, step: str) -> bool:
    blob = text.casefold()
    refused = "connectionrefused" in blob or "10061" in blob
    if not refused:
        return False
    return step == "lab_giga" or "lab_giga" in blob or "18102" in blob or "gigaam" in blob
