from merge_guard import (
    engine_reference_text,
    finalize_window_text,
    merge_budget_sec,
    merge_completeness,
    recover_failed_audit,
    supplement_gemma_from_engines,
)


def test_engine_reference_ignores_bloated_whisper():
    giga = "короткий нормальный текст gigaam " * 20
    whis = giga + " петля whisper " * 200
    ref = engine_reference_text(whis, "", giga)
    assert len(ref) <= len(giga) * 1.05
    assert "петля" not in ref


def test_cursor_audit_accepts_combed_shorter_than_whisper_loop(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cursor")
    giga = (
        "полностью по реестру договоров так что тут мы еще одно тестирование напишем тогда "
        "что ожидаем от комплексную проверку и тогда получается со сменой договоров"
    )
    whis = (giga + " ") * 3  # раздутый whisper-loop
    llm = (
        "полностью по реестру договоров так что тут мы еще одно тестирование напишем тогда "
        "что ожидаем от комплексную проверку и тогда получается со сменой договоров на универсальные "
        "доработка загрузчика продаж из excel"
    )
    ref = engine_reference_text(whis, "", giga)
    audit = merge_completeness(llm, ref)
    assert audit["ok"]
    assert audit["word_recall"] >= 0.82


def test_recover_keeps_cursor_llm_instead_of_whisper_loop(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cursor")
    giga = "фраза один два три четыре пять " * 8
    whis = giga * 3
    llm = giga + " дополнительная фраза про excel и предпрод"
    ref = engine_reference_text(whis, "", giga)
    meta = {"audit_after": merge_completeness(llm, ref), "budget_sec": 90.0, "action": "ok"}
    out, fixed = recover_failed_audit(
        llm,
        whis,
        "",
        giga,
        speech_sec=40.0,
        window_span=160.0,
        alive_n=2,
        merge_meta=meta,
    )
    assert fixed["recovery"] in ("none", "audit_fail_kept_llm", "supplement_recovered")
    assert "дополнительная" in out
    assert out.count("фраза один") < whis.count("фраза один")
def test_merge_budget_uses_window_span_not_only_diar():
    sec = merge_budget_sec(
        55.0,
        160.0,
        "",
        "",
        "x" * 1800,
        alive_n=1,
    )
    assert sec >= 160.0 * 0.45
    assert sec >= 1800 / 20.0


def test_supplement_restores_missing_sentences():
    gemma = "Елена сказала про сайт. Вот."
    ref = (
        "Елена сказала про сайт. Вот. "
        "Ну, давайте глянем демо-базу, чтобы была наглядность. "
        "У тебя версия, которая появилась до 1С."
    )
    out, action = supplement_gemma_from_engines(gemma, ref)
    assert "глянем" in out
    assert action in ("supplemented", "ok", "supplement_weak")
    after = merge_completeness(out, ref)
    assert after["char_ratio"] > merge_completeness(gemma, ref)["char_ratio"]


def test_finalize_keeps_budget_for_long_engine_ref():
    sec = merge_budget_sec(55.0, 160.0, "", "", "z" * 1800, alive_n=1)
    assert sec * 20 >= 1800
    gemma = (
        "Елена спросила про выгрузку множественных свойств на тестовый сайт. "
        "Виктор ответил, что доработает модуль обмена с 1С."
    )
    ref = (
        gemma
        + " Ну, давайте глянем демо-базу, чтобы была наглядность. "
        "Ольга дополнит файл с рекомендациями."
    )
    out, meta = finalize_window_text(
        gemma,
        "",
        "",
        ref,
        speech_sec=55.0,
        window_span=160.0,
        alive_n=1,
    )
    assert "глянем" in out
    assert meta["audit_after"]["ok"]


def test_recover_uses_engine_when_audit_still_fails():
    gemma = "Короткий ответ про обмен."
    ref = (
        "Короткий ответ про обмен. "
        "Ну, давайте глянем демо-базу, чтобы была наглядность. "
        "Ольга дополнит файл с рекомендациями по множественным свойствам."
    )
    meta = {
        "audit_after": merge_completeness(gemma, ref),
        "budget_sec": 90.0,
        "action": "ok",
    }
    out, fixed = recover_failed_audit(
        gemma,
        "",
        "",
        ref,
        speech_sec=40.0,
        window_span=160.0,
        alive_n=1,
        merge_meta=meta,
    )
    assert fixed["recovery"] in ("engine_fallback", "supplement_recovered")
    assert len(out) > len(gemma)
    assert fixed["audit_after"]["char_ratio"] >= meta["audit_after"]["char_ratio"]
