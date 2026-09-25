from __future__ import annotations

from app.app.services import gpu_phase as gp


def test_asr_threshold_on_24gb_allows_gigaam_container():
    total = 24_576
    threshold = gp.asr_vram_threshold_mib(total)
    assert threshold > 3_178
    assert threshold >= gp.DEFAULT_ASR_FALLBACK_MIB


def test_asr_threshold_fallback_when_total_unknown():
    assert gp.asr_vram_threshold_mib(0) == gp.DEFAULT_ASR_FALLBACK_MIB
    assert gp.asr_vram_threshold_mib(-1) == gp.DEFAULT_ASR_FALLBACK_MIB


def test_asr_threshold_on_small_card_uses_fallback():
    assert gp.asr_vram_threshold_mib(6_000) >= gp.DEFAULT_ASR_FALLBACK_MIB


def test_llm_need_from_model_name():
    assert gp.llm_vram_need_mib("qwen/qwen3.8-27b") == int(27 * 0.6 * 1024) + 2048


def test_llm_threshold_on_24gb_allows_gigaam_container():
    total = 24_576
    threshold = gp.llm_vram_threshold_mib("qwen/qwen3.8-27b", total)
    assert threshold > 3_178
    assert threshold >= gp.DEFAULT_LLM_FALLBACK_MIB


def test_llm_threshold_fallback_when_total_unknown():
    assert gp.llm_vram_threshold_mib("qwen/qwen3.8-27b", 0) == gp.DEFAULT_LLM_FALLBACK_MIB


def test_prepare_for_asr_uses_auto_threshold(monkeypatch):
    calls: dict[str, list] = {"waits": []}

    monkeypatch.setattr(gp, "nvidia_memory_total_mib", lambda: 24_576)
    monkeypatch.setattr(gp, "lms_unload_all", lambda: None)
    monkeypatch.setattr(gp.time, "sleep", lambda _s: None)

    def fake_wait(max_mib, timeout_sec=120):
        calls["waits"].append(max_mib)
        return 3_178

    monkeypatch.setattr(gp, "wait_vram_below", fake_wait)
    result = gp.prepare_for_asr()
    assert result == 3_178
    assert calls["waits"][0] == gp.asr_vram_threshold_mib(24_576)


def test_prepare_for_asr_explicit_threshold_wins(monkeypatch):
    calls: dict[str, list] = {"waits": []}

    monkeypatch.setattr(gp, "nvidia_memory_total_mib", lambda: 24_576)
    monkeypatch.setattr(gp, "lms_unload_all", lambda: None)

    def fake_wait(max_mib, timeout_sec=120):
        calls["waits"].append(max_mib)
        return 4_000

    monkeypatch.setattr(gp, "wait_vram_below", fake_wait)
    gp.prepare_for_asr(vram_threshold_mib=4_000)
    assert calls["waits"] == [4_000]


def test_prepare_for_asr_unloads_then_force_kills(monkeypatch):
    steps: list[str] = []

    monkeypatch.setattr(gp, "nvidia_memory_total_mib", lambda: 24_576)
    monkeypatch.setattr(gp, "lms_unload_all", lambda: steps.append("unload"))
    monkeypatch.setattr(gp, "lms_stop_process", lambda: steps.append("kill"))
    monkeypatch.setattr(gp.time, "sleep", lambda _s: None)

    def fake_wait(max_mib, timeout_sec=120):
        steps.append("wait")
        if len(steps) <= 2:
            raise RuntimeError(f"VRAM still 20_000 MiB (need <= {max_mib})")
        return 3_178

    monkeypatch.setattr(gp, "wait_vram_below", fake_wait)
    gp.prepare_for_asr()
    assert steps == ["unload", "wait", "kill", "wait"]


def test_prepare_for_llm_uses_auto_threshold(monkeypatch):
    calls: dict[str, list] = {"waits": [], "loads": []}
    settings = type("S", (), {"llm_provider": "lmstudio"})

    monkeypatch.setattr(gp, "nvidia_memory_total_mib", lambda: 24_576)
    monkeypatch.setattr(gp, "lms_load_model", lambda m: calls["loads"].append(m))

    def fake_wait(max_mib, timeout_sec=120):
        calls["waits"].append(max_mib)
        return 3_178

    monkeypatch.setattr(gp, "wait_vram_below", fake_wait)
    gp.prepare_for_llm("qwen/qwen3.8-27b", settings=settings)
    assert calls["waits"][0] == gp.llm_vram_threshold_mib("qwen/qwen3.8-27b", 24_576)
    assert calls["loads"] == ["qwen/qwen3.8-27b"]