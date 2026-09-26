"""Загрузка промптов из docs/prompts/*.md (пункт плана 2.2).

Формат файла: markdown-заголовок и пояснения, а сами промпты — в ограждённых блоках ```.
Раньше в пяти местах был свой хелпер `blocks[0]`, и трижды он вместо промпта возвращал
заголовок файла (чат, нормализация, vision).
"""
from __future__ import annotations

from pathlib import Path

FENCE = "```"


def fenced_blocks(raw: str) -> list[str]:
    """Содержимое ограждённых блоков по порядку; текст между блоками и заголовок не входят."""
    parts = raw.split(FENCE)
    return [parts[i].strip() for i in range(1, len(parts), 2) if parts[i].strip()]


def load_prompt_blocks(path: Path) -> list[str]:
    blocks = fenced_blocks(path.read_text(encoding="utf-8"))
    if not blocks:
        raise ValueError(f"В {path.name} нет ограждённых блоков ``` с промптом")
    return blocks


def load_prompt_block(path: Path, index: int = 0) -> str:
    blocks = load_prompt_blocks(path)
    if index >= len(blocks):
        raise ValueError(f"В {path.name} блоков {len(blocks)}, а запрошен №{index}")
    return blocks[index]
