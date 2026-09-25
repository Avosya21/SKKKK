"""Все LLM-вызовы с JSON/текстом-результатом идут в обход reasoning (пункт плана 1.4).

Причина: у swift-qwen шаблон всегда открывает <think>; через /chat/completions reasoning не
отключить, ответ съедается токенами рассуждений и обрезается — JSON не разбирается, решения теряются.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = [
    ROOT / "asr" / "lab_proof.py",
    ROOT / "asr" / "lab_sense.py",
    ROOT / "asr" / "llm_client.py",
]


def test_asr_lab_llm_calls_use_no_think():
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bchat_from_env\(", text), f"{path.name}: вызов chat_from_env без no_think"
        assert "chat_from_env_no_think" in text, path.name


# --- сторож по всему коду (пункт 2.1) -------------------------------------------------------
import ast

# Функции с зависшим reasoning: вызывать напрямую нельзя, только *_no_think.
FORBIDDEN = {"chat_completion", "chat_from_env", "lmstudio_chat_raw"}
# Разрешено: сам слой llm.py; vision-вызовы (chat_completion_vision — отдельный путь, не в FORBIDDEN).
ALLOWED_FILES = {"app/app/services/llm.py"}
SCAN_DIRS = ["app/app", "asr", "tools", "scripts"]


def _called_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            names.add(fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else "")
    return names


def test_no_direct_reasoning_llm_calls_anywhere():
    offenders = []
    for base in SCAN_DIRS:
        for path in (ROOT / base).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED_FILES or "/tests/" in rel or "venv" in rel or "/vendor/" in rel:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            bad = _called_names(tree) & FORBIDDEN
            if bad:
                offenders.append(f"{rel}: {sorted(bad)}")
    message = "Прямые вызовы LLM с reasoning (используйте *_no_think): " + "; ".join(offenders)
    assert not offenders, message
