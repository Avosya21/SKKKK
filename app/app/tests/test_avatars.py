"""Фото пользователя: метаданные удаляются, поворот по EXIF, форматы, размеры, защита путей."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.app.tests.test_user_profile import jpeg


@pytest.fixture()
def av(tmp_data):
    from app.app.config import get_settings
    from app.app.services import avatars

    get_settings.cache_clear()
    return avatars


def _open(blob: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(blob))
    image.load()
    return image


def test_metadata_removed_and_two_square_sizes(av):
    out = av.process_image(jpeg(maker="SecretPhone"))
    assert set(out) == {512, 96}
    for size, blob in out.items():
        image = _open(blob)
        assert image.format == "WEBP" and image.size == (size, size)
        assert not image.getexif() and "exif" not in image.info and "icc_profile" not in image.info
        assert b"SecretPhone" not in blob


def test_exif_rotation_applied(av):
    # 800×600 с синей полосой слева, orientation=6 (повернуть на 90° по часовой) → полоса сверху.
    image = _open(av.process_image(jpeg(orientation=6))[512])
    top = image.getpixel((256, 10))
    left = image.getpixel((10, 256))
    assert top[2] > 200 and top[0] < 60  # синий сверху
    assert left[0] > 150  # слева красный


def test_png_with_transparency(av):
    image = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    out = _open(av.process_image(buf.getvalue())[96])
    assert out.getpixel((48, 48)) == pytest.approx((255, 255, 255), abs=3)


@pytest.mark.parametrize(
    ("data", "status"),
    [
        (b"", 400),
        (b"not an image at all" * 10, 400),
        (b"%PDF-1.4 fake", 400),
    ],
)
def test_bad_input(av, data, status):
    with pytest.raises(av.AvatarError) as exc:
        av.process_image(data)
    assert exc.value.status == status


def test_too_big_file(av, monkeypatch):
    monkeypatch.setattr(av, "MAX_BYTES", 1000)
    with pytest.raises(av.AvatarError) as exc:
        av.process_image(jpeg())
    assert exc.value.status == 413


def test_too_many_pixels(av, monkeypatch):
    monkeypatch.setattr(av, "MAX_PIXELS", 100 * 100)
    with pytest.raises(av.AvatarError, match="разрешение"):
        av.process_image(jpeg(width=200, height=200))


def test_save_replaces_old_files(av, tmp_data):
    first = av.save_avatar(7, jpeg(color=(1, 1, 1)))
    second = av.save_avatar(7, jpeg(color=(9, 9, 9)), old_key=first)
    names = sorted(p.name for p in (tmp_data / "people" / "7").iterdir())
    assert names == sorted([f"avatar-{second}-512.webp", f"avatar-{second}-96.webp"])


@pytest.mark.parametrize("key", ["../../etc", "a/b", "", "x" * 65])
def test_avatar_path_rejects_bad_keys(av, key):
    assert av.avatar_path(1, key, 96) is None
    assert av.avatar_path(1, "abc123", 97) is None
