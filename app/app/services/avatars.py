"""Фото пользователя: проверка, выпрямление, удаление метаданных, квадрат 512 и 96 px WebP в `data/people/<user_id>/`.

Метаданные снимаются целиком: в фото с телефона там GPS и модель телефона. Тип определяется по содержимому,
а не по расширению. Имя файла — хеш содержимого, он же версия для кеша браузера (`avatar_key`).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.app.config import get_settings

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000  # защита от «бомб»: картинка 20000×20000 в 100 КБ
SIZES = (512, 96)
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "MPO"}  # MPO — JPEG с двух камер телефона


class AvatarError(ValueError):
    """Фото не подходит; текст — для показа пользователю. `status` — HTTP-код ответа."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def people_root() -> Path:
    return get_settings().data_dir / "people"


def user_dir(user_id: int) -> Path:
    return people_root() / str(int(user_id))


def avatar_path(user_id: int, key: str, size: int) -> Path | None:
    """Путь к файлу фото; None — ключ или размер не наши (защита от подстановки путей)."""
    if size not in SIZES or not key or not key.isalnum() or len(key) > 64:
        return None
    return user_dir(user_id) / f"avatar-{key}-{size}.webp"


def process_image(data: bytes) -> dict[int, bytes]:
    """Проверить и подготовить фото: {размер: WebP без метаданных}."""
    if not data:
        raise AvatarError("Выберите фото")
    if len(data) > MAX_BYTES:
        raise AvatarError(f"Фото больше {MAX_BYTES // (1024 * 1024)} МБ", status=413)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except Image.DecompressionBombError:
        raise AvatarError("Слишком большое разрешение фото") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise AvatarError("Это не фото или формат не поддерживается: сохраните как JPEG или PNG") from None
    if fmt not in ALLOWED_FORMATS:
        raise AvatarError("Формат не поддерживается: сохраните фото как JPEG или PNG")
    if image.width * image.height > MAX_PIXELS:
        raise AvatarError("Слишком большое разрешение фото")
    image = ImageOps.exif_transpose(image)  # поворот по EXIF до того, как метаданные выброшены
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    square = image.crop((left, top, left + side, top + side))
    out: dict[int, bytes] = {}
    for size in SIZES:
        buf = io.BytesIO()
        # Новый Image без info: ни EXIF, ни ICC, ни XMP в файл не попадают.
        resized = square.resize((size, size), Image.Resampling.LANCZOS)
        clean = Image.new("RGB", resized.size)
        clean.paste(resized)
        clean.save(buf, format="WEBP", quality=85, method=6)
        out[size] = buf.getvalue()
    return out


def save_avatar(user_id: int, data: bytes, *, old_key: str | None = None) -> str:
    """Сохранить фото, вернуть новый `avatar_key`. Старые файлы удаляются после записи новых."""
    files = process_image(data)
    key = hashlib.sha1(files[SIZES[0]]).hexdigest()[:16]
    folder = user_dir(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    for size, blob in files.items():
        target = avatar_path(user_id, key, size)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, target)
    if old_key and old_key != key:
        remove_avatar(user_id, old_key)
    return key


def remove_avatar(user_id: int, key: str | None) -> None:
    if not key:
        return
    for size in SIZES:
        path = avatar_path(user_id, key, size)
        if path is not None:
            path.unlink(missing_ok=True)


def move_user_files(from_user_id: int, to_user_id: int) -> None:
    """Склейка дублей пользователей: каталог проигравшего переходит победителю."""
    src, dst = user_dir(from_user_id), user_dir(to_user_id)
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        os.replace(item, dst / item.name)
    shutil.rmtree(src, ignore_errors=True)


def remove_user_files(user_id: int) -> None:
    shutil.rmtree(user_dir(user_id), ignore_errors=True)
