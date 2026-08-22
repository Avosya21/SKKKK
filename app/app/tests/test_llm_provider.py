import pytest

from app.app.config import Settings
from app.app.services import llm


def test_chat_completion_routes_to_lmstudio(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
    )
    monkeypatch.setattr(
        llm,
        "lmstudio_chat_raw",
        lambda **kwargs: ("merged text", {}),
    )
    assert llm.chat_completion(settings, system="s", user="u") == "merged text"


def test_chat_completion_routes_to_cursor(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="cursor",
        cursor_api_key="cursor_test",
    )
    monkeypatch.setattr(
        llm,
        "cursor_chat_raw",
        lambda settings, **kwargs: "cloud text",
    )
    assert llm.chat_completion(settings, system="s", user="u") == "cloud text"


def test_chat_completion_vision_routes_to_cursor(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="cursor",
        cursor_api_key="cursor_test",
    )
    seen: dict[str, str] = {}

    def _fake_cursor(settings, **kwargs):
        seen.update(kwargs)
        return "ocr from image"

    monkeypatch.setattr(llm, "cursor_chat_raw", _fake_cursor)
    out = llm.chat_completion_vision(
        settings,
        system="sys",
        user_text="read",
        image_data_url="data:image/jpeg;base64,abc",
    )
    assert out == "ocr from image"
    assert seen["image_data_url"] == "data:image/jpeg;base64,abc"


def test_sdk_image_from_data_url():
    img = llm._sdk_image_from_data_url("data:image/png;base64,QQ==")
    assert img.mime_type == "image/png"
    assert img.data == "QQ=="


def test_check_llm_cursor_without_key(tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="cursor",
        cursor_api_key="",
    )
    result = llm.check_llm_provider(settings)
    assert result.status == "fail"
    assert "CURSOR_API_KEY" in result.detail
