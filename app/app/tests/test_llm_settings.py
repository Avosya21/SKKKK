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


def test_resolve_llm_provider_cli(tmp_path, monkeypatch):
    from app.app import bootstrap, llm_settings
    from app.app.startup_llm import resolve_llm_provider_at_startup

    # Раньше тест писал в боевой .env (llm_settings.ENV_PATH не подменялся) и сбрасывал провайдер.
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SECRET_KEY=test-secret-key-32chars!!" + chr(10) + "LLM_PROVIDER=lmstudio" + chr(10), encoding="utf-8"
    )
    monkeypatch.setattr(bootstrap, "ENV_PATH", env_path)
    monkeypatch.setattr(llm_settings, "ENV_PATH", env_path)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

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
    monkeypatch.setattr(startup_llm, "_pick_cli", lambda saved, actions=None: "lmstudio")
    monkeypatch.setattr(startup_llm, "write_llm_provider_to_env", lambda provider: None)

    provider = resolve_llm_provider_at_startup(interactive=True, use_saved=False)
    assert provider == "lmstudio"


def test_pick_cli_runs_action_then_shows_menu_again(monkeypatch):
    from app.app import startup_llm

    answers = iter(["9", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    calls = []
    provider = startup_llm._pick_cli("lmstudio", {"9": ("пароли", lambda: calls.append("9"))})
    assert calls == ["9"]
    assert provider == "cursor"


def test_pick_cli_action_error_does_not_stop_startup(monkeypatch):
    from app.app import startup_llm

    def boom():
        raise RuntimeError("нет .env")

    answers = iter(["9", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert startup_llm._pick_cli("cursor", {"9": ("пароли", boom)}) == "cursor"


def test_reset_role_passwords_needs_confirmation(monkeypatch):
    from app.app import main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "_run_credentials", lambda **kw: calls.append(kw))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    main_mod._reset_role_passwords_interactive("127.0.0.1", 1777)
    assert calls == []

    monkeypatch.setattr("builtins.input", lambda _prompt="": "да")
    main_mod._reset_role_passwords_interactive("127.0.0.1", 1777)
    assert calls == [{"reset": True, "host": "127.0.0.1", "port": 1777}]
