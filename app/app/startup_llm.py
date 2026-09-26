"""Выбор LLM при запуске SKKKK: LM Studio (локально) или Cursor API."""

from __future__ import annotations

import os
import sys

from app.app.llm_settings import (
    DEFAULT_LLM_PROVIDER,
    LlmProvider,
    normalize_llm_provider,
    read_llm_provider_from_env_file,
    write_llm_provider_to_env,
)

_WINDOWS_SERVICE_SESSION = "services"


def can_prompt() -> bool:
    """PyCharm и обычная консоль — да; служба Windows без TTY — нет."""
    if sys.stdin.isatty():
        return True
    if os.environ.get("PYCHARM_HOSTED"):
        return True
    session = os.environ.get("SESSIONNAME", "").strip().lower()
    return bool(session) and session != _WINDOWS_SERVICE_SESSION


def _pick_cli(current: LlmProvider | None) -> LlmProvider:
    saved = current or DEFAULT_LLM_PROVIDER
    print("", flush=True)
    print("=" * 52, flush=True)
    print("  Primary Analytics — выбор LLM", flush=True)
    print("=" * 52, flush=True)
    print(f"  Сохранено в .env: {saved}", flush=True)
    print("  1 — LM Studio (локально)", flush=True)
    print("  2 — Cursor API (облако)", flush=True)
    print("  Enter — оставить сохранённое", flush=True)
    print("=" * 52, flush=True)
    try:
        choice = input("Выбор [1/2/Enter]: ").strip()
    except EOFError:
        return saved
    if choice in {"", "0"}:
        return saved
    if choice == "1":
        return "lmstudio"
    if choice == "2":
        return "cursor"
    print(f"Неизвестный ввод {choice!r}, используем {saved}", file=sys.stderr)
    return saved


def _pick_tkinter(current: LlmProvider | None) -> LlmProvider:
    import tkinter as tk

    saved = current or DEFAULT_LLM_PROVIDER
    result: LlmProvider = saved

    root = tk.Tk()
    root.title("Primary Analytics — выбор LLM")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    frame = tk.Frame(root, padx=24, pady=20)
    frame.pack()

    tk.Label(
        frame,
        text="Какой LLM использовать для обработки?",
        font=("Segoe UI", 11, "bold"),
    ).pack(anchor="w", pady=(0, 8))
    tk.Label(
        frame,
        text=f"Сейчас в .env: {saved}",
        font=("Segoe UI", 10),
        fg="#555555",
    ).pack(anchor="w", pady=(0, 16))

    buttons = tk.Frame(frame)
    buttons.pack()

    def choose(provider: LlmProvider) -> None:
        nonlocal result
        result = provider
        root.destroy()

    tk.Button(
        buttons,
        text="LM Studio\n(локально)",
        width=22,
        height=3,
        command=lambda: choose("lmstudio"),
    ).pack(side=tk.LEFT, padx=(0, 12))
    tk.Button(
        buttons,
        text="Cursor API\n(облако)",
        width=22,
        height=3,
        command=lambda: choose("cursor"),
    ).pack(side=tk.LEFT)

    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (w // 2)
    y = (root.winfo_screenheight() // 2) - (h // 2)
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    return result


def resolve_llm_provider_at_startup(
    *,
    cli_choice: str | None = None,
    use_saved: bool = False,
    interactive: bool = True,
) -> LlmProvider:
    """Вернуть провайдер и записать его в `.env`."""
    saved = read_llm_provider_from_env_file()

    if cli_choice is not None:
        provider = normalize_llm_provider(cli_choice)
    elif use_saved:
        provider = saved or DEFAULT_LLM_PROVIDER
    elif interactive and can_prompt():
        # PyCharm Run: stdin не TTY, но input() в консоли работает.
        if os.environ.get("PYCHARM_HOSTED") and not sys.stdin.isatty():
            provider = _pick_cli(saved)
        else:
            try:
                provider = _pick_tkinter(saved)
            except Exception:
                # Headless Windows/Python installations may have tkinter
                # imported but no Tcl runtime. Never block service startup on stdin.
                provider = saved or DEFAULT_LLM_PROVIDER
    else:
        provider = saved or DEFAULT_LLM_PROVIDER

    write_llm_provider_to_env(provider)
    return provider
