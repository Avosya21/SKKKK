from transcript_integrity import (
    audit_and_repair_merge_plain,
    audit_merge_plain,
    audit_view_turns,
    audit_window_seams,
    repair_plain_from_engines,
)


def test_audit_merge_detects_short_plain():
    log = [{"window": 0, "start": 0, "end": 160, "text": "короткий текст про обмен."}]
    audit = audit_merge_plain(
        "короткий текст про обмен.",
        giga_plain=" ".join(
            f"Фраза {i} про обмен свойств и сайт 1С." for i in range(20)
        ),
        log=log,
        duration=1600.0,
    )
    assert audit["status"] == "fail"
    assert any(i["kind"] == "plain_short_vs_giga" for i in audit["issues"])


def test_repair_adds_missing_sentences():
    plain = "Елена сказала про выгрузку."
    ref = (
        plain
        + " Ну, давайте глянем демо-базу для наглядности. "
        "Ольга дополнит файл рекомендаций."
    )
    out, meta = repair_plain_from_engines(plain, ref)
    assert meta["added_chars"] > 0
    assert "глянем" in out


def test_seam_discontinuity_flagged():
    log = [
        {
            "window": 0,
            "start": 0,
            "end": 160,
            "text": "А" * 100 + " обсуждаем только номенклатуру и каталог товаров на сайте.",
        },
        {
            "window": 1,
            "start": 160,
            "end": 320,
            "text": "Б" * 100 + " совсем другая тема про налоги и бухгалтерию без связи.",
        },
    ]
    seams = audit_window_seams(log)
    assert seams


def test_view_audit_detects_text_loss():
    class T:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    plain = "один два три четыре пять шесть семь восемь девять десять"
    turns = [T(0, 5, "один два")]
    audit = audit_view_turns(turns, plain=plain, duration=60.0)
    assert audit["status"] == "fail"


def test_cursor_combed_plain_ok_despite_bloated_whisper(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cursor")
    giga = " ".join(f"фраза{i} обсуждаем обмен данными сайт" for i in range(120))
    clean = giga
    whisper = giga + " " + giga
    log = [{"window": 0, "start": 0, "end": 1280, "text": clean}]
    audit = audit_merge_plain(
        clean,
        giga_plain=giga,
        whisper_plain=whisper,
        log=log,
        duration=1280.0,
    )
    assert audit["status"] != "fail"
    out, repaired = audit_and_repair_merge_plain(
        clean,
        giga_plain=giga,
        whisper_plain=whisper,
        log=log,
        duration=1280.0,
    )
    assert repaired["status"] != "fail"
    assert out == clean
    assert repaired.get("repair", {}).get("added_chars", 0) == 0


def test_gemma_short_plain_repaired_from_giga(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    plain = "Елена сказала про выгрузку."
    giga = (
        plain
        + " Ну, давайте глянем демо-базу для наглядности. "
        "Ольга дополнит файл рекомендаций."
    )
    log = [{"window": 0, "start": 0, "end": 160, "text": plain}]
    out, audit = audit_and_repair_merge_plain(
        plain,
        giga_plain=giga,
        log=log,
        duration=160.0,
    )
    assert len(out) > len(plain)
    assert "глянем" in out
    assert audit.get("repair", {}).get("added_chars", 0) > 0


def test_no_repair_from_whisper_loop(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    plain = "короткий текст про обмен."
    giga = plain + " Дополнительно обсуждаем свойства и каталог на сайте 1С."
    loop = "повторяется одна и та же фраза про обмен свойств. " * 40
    log = [{"window": 0, "start": 0, "end": 160, "text": plain}]
    out, audit = audit_and_repair_merge_plain(
        plain,
        giga_plain=giga,
        whisper_plain=loop,
        log=log,
        duration=160.0,
    )
    assert "каталог" in out
    assert loop[:80] not in out
    assert audit.get("repair", {}).get("action") != "skip_loop_or_empty_ref"
