from app.app.llm_labels import (
    asr_step_labels,
    knowledge_steps_audio,
    llm_runtime_label,
    llm_short_name,
)


def test_llm_short_name():
    assert llm_short_name("cursor") == "Cursor"
    assert llm_short_name("lmstudio") == "Gemma"


def test_asr_merge_label_cursor():
    labels = asr_step_labels("cursor")
    assert "Cursor" in labels["merge"]
    assert "LM Studio" in labels["gemma_unload"]


def test_asr_merge_label_gemma():
    labels = asr_step_labels("lmstudio")
    assert labels["merge"] == "Склейка Gemma по окнам"


def test_knowledge_steps_cursor():
    steps = dict(knowledge_steps_audio("cursor"))
    assert "Cursor" in steps["entities"]
    assert "Cursor" in steps["summary"]


def test_llm_runtime_label():
    assert "LM Studio" in llm_runtime_label("lmstudio")
    assert llm_runtime_label("cursor") == "Cursor API"
