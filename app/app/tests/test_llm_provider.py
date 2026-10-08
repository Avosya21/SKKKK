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


def test_lmstudio_chat_body_enables_thinking_and_keeps_effort():
    body = llm._lmstudio_chat_body(
        model="qwen/qwen3.8-27b",
        system="s",
        user="u",
        temperature=0.2,
        max_tokens=512,
        reasoning_effort="medium",
        enable_thinking=True,
    )
    assert body["reasoning_effort"] == "medium"
    assert body["chat_template_kwargs"] == {"enable_thinking": True}


def test_lmstudio_chat_body_keeps_none_effort():
    body = llm._lmstudio_chat_body(
        model="qwen/qwen3.8-27b",
        system="s",
        user="u",
        temperature=0.2,
        max_tokens=512,
        reasoning_effort="none",
        enable_thinking=False,
    )
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_lmstudio_chat_body_keeps_supported_xhigh():
    body = llm._lmstudio_chat_body(
        model="qwen/qwen3.8-27b",
        system="s",
        user="u",
        temperature=0.2,
        max_tokens=512,
        reasoning_effort="xhigh",
    )
    assert body["reasoning_effort"] == "xhigh"


def test_lmstudio_chat_body_drops_unsupported_effort():
    body = llm._lmstudio_chat_body(
        model="qwen/qwen3.8-27b",
        system="s",
        user="u",
        temperature=0.2,
        max_tokens=512,
        reasoning_effort="bogus",
    )
    assert "reasoning_effort" not in body


def test_lmstudio_chat_body_omits_reasoning_when_none():
    body = llm._lmstudio_chat_body(
        model="qwen/qwen3.8-27b",
        system="s",
        user="u",
        temperature=0.2,
        max_tokens=512,
        reasoning_effort=None,
    )
    assert "reasoning_effort" not in body
    assert "chat_template_kwargs" not in body


def test_chat_completion_resolves_reasoning_from_settings(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_enable_thinking=True,
        lmstudio_reasoning_effort="high",
    )
    seen: dict[str, object] = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return ("text", {})

    monkeypatch.setattr(llm, "lmstudio_chat_raw", _fake)
    llm.chat_completion(settings, system="s", user="u")
    assert seen["reasoning_effort"] == "high"
    assert seen["enable_thinking"] is True


def test_chat_completion_disabled_thinking_forces_none(monkeypatch, tmp_path):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_enable_thinking=False,
        lmstudio_reasoning_effort="high",
    )
    seen: dict[str, object] = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return ("text", {})

    monkeypatch.setattr(llm, "lmstudio_chat_raw", _fake)
    llm.chat_completion(settings, system="s", user="u")
    assert seen["reasoning_effort"] == "none"
    assert seen["enable_thinking"] is False


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


def test_ensure_lmstudio_loads_through_keeper(monkeypatch, tmp_path):
    """Процессы ASR загружают модель тем же хранителем, что и портал, — строго по профилю, а не по /v1/models."""
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_model="qwen/qwen3.8-27b",
    )
    calls = []
    monkeypatch.setattr("app.app.services.llm_keeper.ensure_loaded", lambda s, reason="": calls.append(reason))
    llm.ensure_lmstudio_gemma_loaded(settings)
    assert calls == ["ASR"]


@pytest.mark.parametrize(
    ("status", "expected", "needle"),
    [
        ({"state": "loaded", "jit": False, "mismatch": []}, "ok", "в памяти"),
        ({"state": "not-loaded", "jit": False}, "ok", "по требованию"),
        ({"state": "loaded", "jit": True}, "warn", "JIT"),
        ({"state": "loaded", "jit": False, "mismatch": ["потоков 1 вместо 2"]}, "warn", "не по профилю"),
        ({"state": None, "jit": False}, "fail", "недоступен"),
    ],
)
def test_check_llm_lmstudio_reports_keeper_state(monkeypatch, tmp_path, status, expected, needle):
    settings = Settings(
        secret_key="test-secret-key-32chars!!",
        data_dir=tmp_path,
        llm_provider="lmstudio",
        lmstudio_model="qwen/qwen3.8-27b",
    )
    monkeypatch.setattr("app.app.services.llm_keeper.keeper_status", lambda _s: dict(status))
    result = llm.check_llm_provider(settings)
    assert result.status == expected
    assert needle in result.detail


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


def test_no_think_selfcheck_falls_back_when_template_differs(monkeypatch):
    from app.app.config import Settings
    from app.app.services import llm

    settings = Settings(llm_provider="lmstudio")
    monkeypatch.delenv("LMSTUDIO_NO_THINK", raising=False)
    monkeypatch.setitem(llm._no_think_state, "ok", None)

    # Другой шаблон модели: обход возвращает <think> в тексте -> самопроверка забраковала.
    monkeypatch.setattr(llm, "_completions_http", lambda **kw: ("<think>рассуждаю", {}))
    ok, detail = llm.lmstudio_no_think_selfcheck(settings)
    assert ok is False and "think" in detail
    assert llm.no_think_enabled() is False

    calls = {}
    monkeypatch.setattr(llm, "chat_completion", lambda s, **kw: calls.update(kw) or "через chat")
    text = llm.chat_completion_no_think(settings, system="s", user="u", max_tokens=100)
    assert text == "через chat" and calls["max_tokens"] == 100 + llm.REASONING_FALLBACK_EXTRA_TOKENS

    # Исправная модель -> обход снова включён.
    monkeypatch.setattr(llm, "_completions_http", lambda **kw: ("готово", {}))
    assert llm.lmstudio_no_think_selfcheck(settings)[0] is True
    assert llm.no_think_enabled() is True


def test_no_think_env_switch_disables_bypass(monkeypatch):
    from app.app.services import llm

    monkeypatch.setitem(llm._no_think_state, "ok", True)
    monkeypatch.setenv("LMSTUDIO_NO_THINK", "0")
    assert llm.no_think_enabled() is False


def test_selfcheck_unreachable_does_not_disable(monkeypatch):
    from app.app.config import Settings
    from app.app.services import llm

    monkeypatch.setitem(llm._no_think_state, "ok", None)
    monkeypatch.delenv("LMSTUDIO_NO_THINK", raising=False)

    def boom(**kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(llm, "_completions_http", boom)
    assert llm.lmstudio_no_think_selfcheck(Settings(llm_provider="lmstudio"))[0] is None
    assert llm.no_think_enabled() is True
