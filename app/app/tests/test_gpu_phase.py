from __future__ import annotations

from types import SimpleNamespace

from app.app.services import gpu_phase as gp


def _no_unload(monkeypatch):
    boom = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("prepare_for_asr не выгружает модель сам"))  # noqa: E731
    monkeypatch.setattr(gp, "lms_unload_all", boom)
    monkeypatch.setattr("app.app.services.llm_keeper.unload", boom)


def test_prepare_for_asr_keeps_model_when_card_allows(monkeypatch):
    monkeypatch.setattr(gp, "nvidia_memory_used_mib", lambda: 22_400)
    _no_unload(monkeypatch)
    assert gp.prepare_for_asr(settings=SimpleNamespace(lmstudio_keep_during_asr=True)) == 22_400


def test_prepare_for_asr_leaves_unload_to_gpu_step_hold(monkeypatch):
    """Выгрузку делает llm_keeper.asr_gpu_hold вокруг GPU-шага: он дожидается ответа чата. Прежняя выгрузка здесь
    обрывала ответ и при нехватке памяти останавливала LM Studio."""
    monkeypatch.setattr(gp, "nvidia_memory_used_mib", lambda: 22_400)
    _no_unload(monkeypatch)
    assert gp.prepare_for_asr(settings=SimpleNamespace(lmstudio_keep_during_asr=False)) == 22_400


def test_wait_vram_below_returns_when_free(monkeypatch):
    seq = iter([20_000, 9_000])
    monkeypatch.setattr(gp, "nvidia_memory_used_mib", lambda: next(seq, 9_000))
    monkeypatch.setattr(gp.time, "sleep", lambda _s: None)
    assert gp.wait_vram_below(10_000, timeout_sec=30) == 9_000


def test_prepare_for_llm_loads_through_keeper(monkeypatch):
    calls = []
    monkeypatch.setattr("app.app.services.llm_keeper.ensure_loaded", lambda s, reason="": calls.append(reason))
    gp.prepare_for_llm("любая", settings=SimpleNamespace())
    assert calls == ["этап LLM"]
