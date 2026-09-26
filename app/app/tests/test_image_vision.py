from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.app.config import Settings
from app.app.services.image_extract import ocr_text_is_weak
import app.app.services.pipeline_documents as docs


def test_ocr_text_is_weak_empty_and_short():
    assert ocr_text_is_weak("")
    assert ocr_text_is_weak("   ")
    assert ocr_text_is_weak("ab 12")
    assert ocr_text_is_weak("!!! ??? ###")


def test_ocr_text_is_weak_rejects_glyph_soup():
    soup = "\n".join(["й", "х", "i", "}", "{", "a", "PALS", "Nik", "hig", "Be", "Hh"] * 3)
    assert ocr_text_is_weak(soup)


def test_ocr_text_is_weak_accepts_normal_russian():
    text = (
        "Свидетельство о государственной регистрации акта гражданского состояния. "
        "Орган ЗАГС № 2 администрации городского округа — город Волжский. "
        "Дата записи: 19.08.1978. Сумма госпошлины 350 руб."
    )
    assert not ocr_text_is_weak(text)


def test_extract_image_uses_vision_when_ocr_weak(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 64), color=(30, 30, 30)).save(img_path, format="JPEG")

    monkeypatch.setattr("app.app.services.tesseract_tools.configure_pytesseract", lambda: "")
    monkeypatch.setattr("pytesseract.image_to_string", lambda *a, **k: "й\n}\ni\n#")

    called = {"vision": False}

    def fake_vision(settings, path, *, ocr_hint=""):
        called["vision"] = True
        assert path == img_path
        return "Текст с фото: договор поставки № 15 от 01.02.2026"

    monkeypatch.setattr(
        "app.app.services.image_extract.extract_image_via_vision",
        fake_vision,
    )

    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        dry_run=False,
        data_dir=tmp_path / "data",
    )
    (tmp_path / "data").mkdir(exist_ok=True)

    out = docs._extract_image(img_path, settings)
    assert called["vision"] is True
    assert "договор поставки" in out


def test_extract_image_keeps_ocr_when_good(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "scan.png"
    Image.new("RGB", (32, 32), color=(255, 255, 255)).save(img_path)

    good = (
        "Акт выполненных работ № 42 от 12.03.2026. "
        "Подрядчик ООО Ромашка. Сумма 150000 руб. без НДС. "
        "Объект: доработка конфигурации УТ 11."
    )
    monkeypatch.setattr("app.app.services.tesseract_tools.configure_pytesseract", lambda: "")
    monkeypatch.setattr("pytesseract.image_to_string", lambda *a, **k: good)

    def boom(*_a, **_k):
        raise AssertionError("vision must not run")

    monkeypatch.setattr("app.app.services.image_extract.extract_image_via_vision", boom)

    settings = Settings(secret_key="test-secret-key-32chars!!", dry_run=False, data_dir=tmp_path)
    out = docs._extract_image(img_path, settings)
    assert out == good


def test_extract_image_dry_run_skips_vision(tmp_path: Path, monkeypatch):
    img_path = tmp_path / "x.jpg"
    Image.new("RGB", (16, 16), color=(0, 0, 0)).save(img_path, format="JPEG")
    monkeypatch.setattr("app.app.services.tesseract_tools.configure_pytesseract", lambda: "")
    monkeypatch.setattr("pytesseract.image_to_string", lambda *a, **k: "x")

    def boom(*_a, **_k):
        raise AssertionError("vision must not run in dry_run")

    monkeypatch.setattr("app.app.services.image_extract.extract_image_via_vision", boom)
    settings = Settings(secret_key="test-secret-key-32chars!!", dry_run=True, data_dir=tmp_path)
    assert docs._extract_image(img_path, settings) == "x"
