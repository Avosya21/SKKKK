from __future__ import annotations

from app.app.services.plain_text import (
    clean_summary_output,
    has_llm_markup_artifacts,
    plainify_llm_markup,
    sanitize_summary_markdown,
)


def test_plainify_common_arrows_and_ops():
    src = r"Срок 01.10 $\rightarrow$ **15.11**. Нагрузка $\le$ 15, шум $\sim$ 0, площадь $10\times20$."
    out = plainify_llm_markup(src)
    assert r"$\rightarrow$" not in out
    assert r"\rightarrow" not in out
    assert "→" in out
    assert "≤" in out
    assert "∼" in out
    assert "×" in out
    assert "**15.11**" in out


def test_plainify_bare_commands_without_dollars():
    assert plainify_llm_markup(r"A \to B \times C") == "A → B × C"


def test_plainify_paren_math():
    assert "→" in plainify_llm_markup(r"Шаг \( \rightarrow \) дальше")


def test_has_artifacts_detects_latex():
    assert has_llm_markup_artifacts(r"foo $\sim$ bar")
    assert has_llm_markup_artifacts(r"foo \times bar")
    assert not has_llm_markup_artifacts("обычный текст со стрелкой →")


def test_plainify_times_inside_inline_math():
    assert plainify_llm_markup(r"$10\times20$") == "10×20"


def test_plainify_leaves_normal_text():
    text = "Участники → Решения. Бюджет 1 450 000 руб."
    assert plainify_llm_markup(text) == text


def test_sanitize_summary_strips_emoji_and_checkboxes():
    src = "## 📄 Раздел\n\n- [ ] Дмитрий: сделать задачу\n- [x] Готово\n🤝 Со стороны заказчика"
    out = sanitize_summary_markdown(src)
    assert "📄" not in out
    assert "🤝" not in out
    assert "[ ]" not in out
    assert "[x]" not in out
    assert out.startswith("## Раздел")
    assert "- Дмитрий: сделать задачу" in out


def test_clean_summary_output_chains_plainify_and_sanitize():
    src = r"- [ ] Шаг $\rightarrow$ дальше 📄"
    out = clean_summary_output(src)
    assert "[ ]" not in out
    assert "→" in out
    assert "📄" not in out
