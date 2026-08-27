from app.app.llm_labels import (
    asr_step_labels,
    knowledge_steps_audio,
    llm_runtime_label,
    llm_short_name,
)


def test_llm_short_name():
    assert llm_short_name("cursor") == "Cursor"
    assert llm_short_name("lmstudio") == "Gemma"


def test_asr_lab_labels_cursor():
    labels = asr_step_labels("cursor")
    assert "Parakeet" in labels["lab_onnx"]
    assert "Cursor" in labels["lab_sense"]
    assert "GigaAM" in labels["lab_giga"]
    assert "merge" not in labels


def test_asr_lab_labels_gemma():
    labels = asr_step_labels("lmstudio")
    assert labels["lab_onnx"].startswith("Parakeet")
    assert "Gemma" in labels["lab_proof"]


def test_knowledge_steps_cursor():
    steps = dict(knowledge_steps_audio("cursor"))
    assert "Cursor" in steps["entities"]
    assert "Cursor" in steps["summary"]


def test_llm_runtime_label():
    assert "LM Studio" in llm_runtime_label("lmstudio")
    assert llm_runtime_label("cursor") == "Cursor API"
