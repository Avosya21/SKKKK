from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path

from app.app.config import ROOT, Settings
from app.app.services.llm import chat_completion_vision

logger = logging.getLogger("skkkk.documents")

PROMPT_PATH = ROOT / "docs" / "prompts" / "extract_image_vision.md"
OCR_MIN_USEFUL_CHARS = 40
OCR_MIN_USEFUL_RATIO = 0.45
OCR_MAX_SHORT_WORD_RATIO = 0.6
VISION_MAX_SIDE = 1600


def ocr_text_is_weak(text: str, *, min_useful_chars: int = OCR_MIN_USEFUL_CHARS) -> bool:
    """True when OCR looks empty or like glyph soup — worth a vision pass."""
    t = (text or "").strip()
    if not t:
        return True

    letters = sum(1 for c in t if c.isalpha())
    digits = sum(1 for c in t if c.isdigit())
    useful = letters + digits
    if useful < min_useful_chars:
        return True

    solid = re.sub(r"\s+", "", t)
    if not solid:
        return True
    if useful / len(solid) < OCR_MIN_USEFUL_RATIO:
        return True

    words = re.findall(r"\S+", t)
    if len(words) >= 8:
        short = sum(1 for w in words if len(re.sub(r"[^\wА-Яа-яЁё]", "", w, flags=re.UNICODE)) <= 2)
        if short / len(words) > OCR_MAX_SHORT_WORD_RATIO and letters < 80:
            return True
    return False


def _load_vision_prompt() -> str:
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    blocks = [b.strip() for b in raw.split("```") if b.strip()]
    return blocks[0] if blocks else raw


def image_to_data_url(path: Path, *, max_side: int = VISION_MAX_SIDE) -> str:
    from PIL import Image

    img = Image.open(path)
    if img.mode not in {"RGB", "L"}:
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    w, h = img.size
    scale = min(1.0, max_side / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def extract_image_via_vision(
    settings: Settings,
    path: Path,
    *,
    ocr_hint: str = "",
) -> str:
    system = _load_vision_prompt()
    data_url = image_to_data_url(path)
    hint = (ocr_hint or "").strip()
    if hint and not ocr_text_is_weak(hint):
        user = (
            "Извлеки текст и смысл с изображения. "
            f"Черновик OCR (может быть неточным):\n{hint[:1500]}"
        )
    elif hint:
        user = (
            "OCR дал мусор или почти ничего. Прочитай изображение заново. "
            f"Черновик OCR (игнорировать при противоречии):\n{hint[:800]}"
        )
    else:
        user = "Извлеки текст и смысл с изображения."

    text = chat_completion_vision(
        settings,
        system=system,
        user_text=user,
        image_data_url=data_url,
        max_tokens=6144,
    ).strip()
    if not text:
        raise RuntimeError("vision returned empty text")
    return text
