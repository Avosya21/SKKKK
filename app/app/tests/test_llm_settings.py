import pytest

from app.app.llm_settings import (
    apply_llm_provider_to_process,
    normalize_llm_provider,
    read_llm_provider_from_env_file,
    uses_local_gemma,
    write_llm_provider_to_env,
)


def test_normalize_llm_provider():
    assert normalize_llm_provider("lmstudio") == "lmstudio"
    assert normalize_llm_provider("cursor") == "cursor"
    assert normalize_llm_provider("gemma") == "lmstudio"
    assert normalize_llm_provider("  CURSOR  ") == "cursor"
    with pytest.raises(ValueError):
        normalize_llm_provider("openai")


def test_uses_local_gemma():
    assert uses_local_gemma("lmstudio") is True
    assert uses_local_gemma("cursor") is False
    assert uses_local_gemma(None) is True


def test_apply_llm_provider_overrides_os_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    apply_llm_provider_to_process("cursor")
    import os

    assert os.environ["LLM_PROVIDER"] == "cursor"


def test_write_and_read_llm_provider(tmp_path, monkeypatch):
    from app.app import bootstrap, llm_settings

    env_path = tmp_path / ".env"
    env_path.write_text("SECRET_KEY=test-secret-key-32chars!!\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ENV_PATH", env_path)
    monkeypatch.setattr(llm_settings, "ENV_PATH", env_path)

    write_llm_provider_to_env("cursor")
    assert read_llm_provider_from_env_file() == "cursor"

    write_llm_provider_to_env("lmstudio")
    assert read_llm_provider_from_env_file() == "lmstudio"


def test_resolve_llm_provider_cli(monkeypatch):
    from app.app import bootstrap
    from app.app.startup_llm import resolve_llm_provider_at_startup

    env_path = bootstrap.ENV_PATH
    monkeypatch.setattr(
        bootstrap,
        "_read_env",
        lambda: "SECRET_KEY=test-secret-key-32chars!!\nLLM_PROVIDER=lmstudio\n",
    )
    monkeypatch.setattr(bootstrap, "ENV_PATH", env_path)

    provider = resolve_llm_provider_at_startup(cli_choice="cursor", use_saved=True, interactive=False)
    assert provider == "cursor"


def test_can_prompt_without_tty_in_pycharm(monkeypatch):
    from app.app import startup_llm

    monkeypatch.setattr(startup_llm.sys.stdin, "isatty", lambda: False)
    monkeypatch.setenv("PYCHARM_HOSTED", "1")
    monkeypatch.delenv("SESSIONNAME", raising=False)
    assert startup_llm.can_prompt() is True


def test_can_prompt_false_for_windows_service(monkeypatch):
    from app.app import startup_llm

    monkeypatch.setattr(startup_llm.sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("PYCHARM_HOSTED", raising=False)
    monkeypatch.setenv("SESSIONNAME", "Services")
    assert startup_llm.can_prompt() is False


def test_resolve_prompts_when_pycharm_has_no_tty(monkeypatch):
    from app.app.startup_llm import resolve_llm_provider_at_startup
    from app.app import startup_llm

    monkeypatch.setattr(startup_llm.sys.stdin, "isatty", lambda: False)
    monkeypatch.setenv("PYCHARM_HOSTED", "1")
    monkeypatch.setattr(startup_llm, "read_llm_provider_from_env_file", lambda: "cursor")
    monkeypatch.setattr(startup_llm, "_pick_cli", lambda saved: "lmstudio")
    monkeypatch.setattr(startup_llm, "write_llm_provider_to_env", lambda provider: None)

    provider = resolve_llm_provider_at_startup(interactive=True, use_saved=False)
    assert provider == "lmstudio"
