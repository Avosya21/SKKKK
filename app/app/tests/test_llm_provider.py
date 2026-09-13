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


def test_ensure_lmstudio_accepts_configured_qwen(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_model="qwen/qwen3.8-27b",
    )
    monkeypatch.setattr(
        llm,
        "lmstudio_models",
        lambda _settings, timeout=10: ["qwen/qwen3.8-27b@q4_k_m"],
    )
    llm.ensure_lmstudio_gemma_loaded(settings)


def test_ensure_lmstudio_rejects_missing_configured_model(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_model="qwen/qwen3.8-27b",
    )
    monkeypatch.setattr(
        llm,
        "lmstudio_models",
        lambda _settings, timeout=10: ["google/gemma-4-31b-qat"],
    )
    with pytest.raises(RuntimeError, match="qwen/qwen3.8-27b"):
        llm.ensure_lmstudio_gemma_loaded(settings)


def test_check_llm_lmstudio_matches_quantized_id(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_model="qwen/qwen3.8-27b",
    )
    monkeypatch.setattr(
        llm,
        "lmstudio_models",
        lambda _settings, timeout=5: ["qwen/qwen3.8-27b@q4_k_m"],
    )
    result = llm.check_llm_provider(settings)
    assert result.status == "ok"
    assert "qwen/qwen3.8-27b" in result.detail
    assert "Gemma" not in result.detail


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


def test_cursor_chat_retries_then_succeeds(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="cursor",
        cursor_api_key="cursor_test",
    )
    calls = {"n": 0}

    class _Result:
        def __init__(self, status: str, text: str):
            self.id = f"run-{calls['n']}"
            self.status = status
            self.result = text
            self.duration_ms = 12
            self.model = None

    def _prompt(_message, _options):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Result("error", "")
        return _Result("finished", '{"picks": {}}')

    monkeypatch.setattr("cursor_sdk.Agent.prompt", _prompt)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    text = llm.cursor_chat_raw(settings, system="s", user="u")
    assert text == '{"picks": {}}'
    assert calls["n"] == 2
